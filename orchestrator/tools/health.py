"""Canonical unit-health MCP surface — wires the pure :mod:`orchestrator.health`
probe + decision table into the running orchestrator.

Public tool:

* :func:`inspect_unit_health` — probes the unit's PR, CI, reviews, worker
  sessions, and orchestrator-side counters; applies the
  ``actions_to_apply`` half of the decision via the existing
  :func:`orchestrator.state.touch_unit` / :func:`orchestrator.state.record_event`
  code paths; persists each :class:`~orchestrator.health.ShadowDecision`
  as a ``shadow_transition_proposed`` event for forensics; and at most
  once per UTC day per unit, snapshots the full :class:`HealthReport`
  payload as a ``health_report_snapshot`` event for audit retention.

Aliases (in :mod:`orchestrator.tools.ops`):

* ``check_unit_pr`` is documented as an alias for
  ``inspect_unit_health(dry_run=True)`` (deprecated).
* ``reconcile_unit_pr`` is documented as an alias for
  ``inspect_unit_health(dry_run=False)`` (deprecated). Both keep their
  legacy JSON return shape — the deprecation is the chat-visible
  warning, not a behavioural break.

The production GitHub data source wraps the few :mod:`orchestrator.github`
calls available today (PR state + check_runs); the richer probes
(``get_review_threads`` / ``get_required_checks`` / ...) ship sensible
defaults that match the protocol while leaving the deep GH-side probing
to F-015 / F-016 once the daemon needs them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime

from orchestrator import cycle_log, github, state
from orchestrator.agents import ManagedAgentWorker
from orchestrator.health import (
    Action,
    AnthropicHealthClient,
    Decision,
    GitHubHealthClient,
    HealthReport,
    ShadowDecision,
    decide_transitions,
    probe_unit_health,
)
from orchestrator.models import WorkUnitState
from orchestrator.tools import mcp, need_github_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot retention — once per N hours per unit. ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS``
# env var: ``0`` disables, default ``24`` matches the spec's "first probe per
# unit per UTC day".
# ---------------------------------------------------------------------------
_SNAPSHOT_INTERVAL_ENV = "ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS"
_SNAPSHOT_INTERVAL_DEFAULT_HOURS = 24


def _snapshot_interval_hours() -> int:
    raw = os.getenv(_SNAPSHOT_INTERVAL_ENV, "").strip()
    if not raw:
        return _SNAPSHOT_INTERVAL_DEFAULT_HOURS
    try:
        n = int(raw)
    except ValueError:
        return _SNAPSHOT_INTERVAL_DEFAULT_HOURS
    return max(0, n)


# ---------------------------------------------------------------------------
# F-016-U-9: ci_drift_detected dedupe.
#
# The 2026-06-10 incident triage surfaced a second anti-loop defect: a unit
# parked in_ci with persistently-red CI was re-emitting ``ci_drift_detected``
# on every ~6s daemon poll (no dedupe). Hammered the GitHub API and bloated
# unit_events with hundreds of identical rows.
#
# Dedupe rules (both must agree to write a new row):
#   1. **Failing-check-set changed.** If the failing checks for THIS probe
#      match the most-recent prior ci_drift_detected row, the situation has
#      not evolved and the event is a no-op.
#   2. **Rate-limit window expired.** Same as ``health_report_snapshot``,
#      governed by ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS``. A re-emit fires
#      only when the prior drift row is older than the window OR the set
#      changes.
# ---------------------------------------------------------------------------


def _parse_failing_set(details: str) -> frozenset[str]:
    """Extract the failing check-set from a ``ci_drift_detected`` event's details.

    The producer writes ``f"failing checks: {', '.join(failing)}"``; this
    helper is the inverse. Returns an empty set when ``details`` doesn't
    match (a malformed legacy row, or a future details-format change) —
    that yields a conservative "treat as different set" outcome, so a
    real drift always fires through.
    """
    prefix = "failing checks: "
    if not details.startswith(prefix):
        return frozenset()
    payload = details[len(prefix) :].strip()
    if not payload:
        return frozenset()
    return frozenset(item.strip() for item in payload.split(",") if item.strip())


def _should_emit_ci_drift(unit_state: WorkUnitState, action: Action) -> bool:
    """F-016-U-9 dedupe: emit ``ci_drift_detected`` only on a real change.

    Returns ``True`` when EITHER the failing-check-set differs from the
    last ``ci_drift_detected`` row for this unit OR the prior row is
    older than ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS`` (the same rate-
    limit window the snapshot path uses, so the two surfaces share a
    single throttle knob).

    The first ``ci_drift_detected`` for a unit (no prior row) always
    fires — there's nothing to dedupe against.
    """
    interval = _snapshot_interval_hours()
    now = datetime.now(UTC)
    incoming = _parse_failing_set(action.details)
    events = state.list_events(unit_state.unit_id)
    for ev in reversed(events):  # most-recent first
        if ev.get("event_type") != "ci_drift_detected":
            continue
        prior_set = _parse_failing_set(ev.get("details") or "")
        if prior_set != incoming:
            return True  # set changed → real drift evolution → emit
        if interval <= 0:
            # Rate-limit disabled — still respect set-equality (which
            # just failed), so unchanged-set + disabled-throttle is a
            # no-op. This preserves the dedupe-by-content guarantee
            # while letting operators opt-out of the time window.
            return False
        ts = ev.get("ts") or ""
        try:
            normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            # Unparseable timestamp on the prior row — treat the prior
            # as out-of-window so a real drift always re-surfaces.
            return True
        age = now.timestamp() - parsed.timestamp()
        return age >= interval * 3600
    return True  # no prior ci_drift_detected row — first emit always fires


# ---------------------------------------------------------------------------
# Production clients — minimal wiring around the existing github helpers.
# Methods we can't satisfy from today's :mod:`orchestrator.github` surface
# return empty / safe defaults; the protocol still type-checks and the
# decision table's "no data → no signal" branches handle the absence
# gracefully. F-015 / F-016 will widen the real probes as the daemon
# needs them.
# ---------------------------------------------------------------------------


class _ProductionGitHubClient(GitHubHealthClient):
    """Wraps :mod:`orchestrator.github` for one ``(repo_url, pr_number)``.

    Per-instance caching on the two real API calls (``get_pr_state`` and
    ``get_pr_check_runs``) keeps the probe to two GitHub round-trips even
    though the protocol exposes 11 methods.
    """

    def __init__(self, repo_url: str, pr_number: int) -> None:
        self._repo_url = repo_url
        self._pr_number = pr_number
        self._pr: dict | None = None
        self._checks: dict | None = None

    def _ensure_pr(self) -> dict:
        if self._pr is None:
            self._pr = github.get_pr_state(self._repo_url, self._pr_number)
        return self._pr

    def _ensure_checks(self) -> dict:
        if self._checks is None:
            self._checks = github.get_pr_check_runs(self._repo_url, self._pr_number)
        return self._checks

    def get_pr(self, unit_id: str) -> dict | None:  # noqa: ARG002
        return self._ensure_pr()

    def get_check_runs(self, unit_id: str) -> list[dict]:  # noqa: ARG002
        return list(self._ensure_checks().get("runs") or [])

    def get_required_checks(self, unit_id: str) -> list[str]:  # noqa: ARG002
        # Branch-protection-required check names: deferred to F-015 / F-016
        # when the daemon needs the drift signal. Empty list → no
        # ``required_check_missing`` event.
        return []

    def get_compare_to_base(self, unit_id: str) -> dict:  # noqa: ARG002
        # ``ahead_by`` / ``behind_by``: not surfaced by today's
        # ``orchestrator.github`` helpers. ``None`` propagates through the
        # snapshot without firing any decision-table rule.
        return {"ahead_by": None, "behind_by": None}

    def get_reviews(self, unit_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def get_review_threads(self, unit_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def get_requested_reviewers(self, unit_id: str) -> dict:  # noqa: ARG002
        return {"users": [], "teams": []}

    def get_copilot_review(self, unit_id: str) -> dict | None:  # noqa: ARG002
        # Available via ``github.get_copilot_review`` but it's a separate
        # GH round-trip on every probe. Held off until F-015 wires it
        # behind a probe-frequency knob.
        return None

    def get_last_force_push_at(self, unit_id: str) -> str | None:  # noqa: ARG002
        return None

    def get_head_commit(self, unit_id: str) -> dict:  # noqa: ARG002
        pr = self._ensure_pr()
        return {"sha": pr.get("head_sha"), "committed_at": None}

    def is_merge_commit_on_main(self, unit_id: str) -> bool:  # noqa: ARG002
        # Optimistic default — the ``merge_reverted_flag`` shadow rule
        # only fires when this returns ``False``, so we never false-flag
        # a unit before F-015 wires the real reachability check.
        return True


class _ProductionAnthropicClient(AnthropicHealthClient):
    """Reuses :class:`ManagedAgentWorker`'s session-retrieval path.

    One worker per role since each role keeps its own client + auth.
    Status lookups are best-effort: SDK errors yield ``None`` rather
    than escalating to the MCP layer (a health probe must never fail
    because Anthropic returned 5xx on one session).
    """

    def __init__(self) -> None:
        self._workers: dict[str, ManagedAgentWorker] = {}

    def _worker(self, role: str) -> ManagedAgentWorker:
        if role not in self._workers:
            self._workers[role] = ManagedAgentWorker(role=role)
        return self._workers[role]

    def get_session_status(self, session_id: str) -> str | None:
        # The role isn't stored on the session id alone; coder is the
        # default backend used by ``ops.resume_unit`` for the same lookup.
        try:
            worker = self._worker("coder")
            session = worker.client.beta.sessions.retrieve(session_id)
        except Exception:  # noqa: BLE001 — observability tool, must not crash chat
            return None
        return getattr(session, "status", None)


# ---------------------------------------------------------------------------
# State-mutation helpers — every applied transition / event / side effect
# flows through these so the canonical tool and the deprecated aliases
# stay behaviourally identical to ``ops.reconcile_unit_pr``.
# ---------------------------------------------------------------------------


def _apply_action(unit_state: WorkUnitState, action: Action) -> None:
    """Apply one :class:`Action` via the existing state-write primitives.

    Reuses :func:`state.touch_unit` / :func:`state.record_event` directly
    so the on-disk effect is bit-identical to ``reconcile_unit_pr``'s
    existing transitions. ``side_effect`` actions go through
    :func:`cycle_log.write_cycle_log` under a best-effort suppress —
    matches the wrap at the alias's ``ops.py`` call site so a missing
    ``gh``, non-repo workdir, or disk error never breaks the
    chat-visible response.
    """
    cycle = unit_state.review_round

    if action.kind == "transition" and action.target_status:
        state.touch_unit(
            unit_state.unit_id,
            status=action.target_status,
            clear_error=action.clear_error,
        )
        return

    if action.kind == "event" and action.event_type:
        # F-016-U-9: throttle ``ci_drift_detected`` so a persistently-red
        # PR doesn't generate one event + GitHub-API storm per ~6s
        # daemon poll. The check_runs round-trip already happened by the
        # time we reach here — the dedupe just suppresses the
        # ``unit_events`` row AND the ``last_error`` rewrite when the
        # failing-check-set is unchanged within the rate-limit window.
        if action.event_type == "ci_drift_detected" and not _should_emit_ci_drift(
            unit_state, action
        ):
            return
        if action.set_last_error:
            state.touch_unit(unit_state.unit_id, error=action.set_last_error)
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            action.event_type,
            source=action.event_source,
            cycle_number=cycle,
            summary=action.summary,
            details=action.details,
        )
        return

    if action.kind == "side_effect" and action.side_effect == "write_cycle_log":
        merge_sha = action.payload.get("merge_commit_sha")
        if merge_sha:
            with contextlib.suppress(Exception):
                cycle_log.write_cycle_log(
                    unit_state.unit_id,
                    base_dir=cycle_log.cycle_log_base_dir(),
                    merge_commit_sha=merge_sha,
                    commit_message=action.payload.get("commit_message"),
                )


def _record_shadow_event(unit_state: WorkUnitState, shadow: ShadowDecision) -> None:
    """Persist one ShadowDecision as a ``shadow_transition_proposed`` event.

    The full payload (rule_name + predicted_action + trigger_inputs +
    rationale) lands in the event's ``details`` JSON column so F-015's
    promote-to-live audit can replay the predicate offline.
    """
    payload = {
        "rule_name": shadow.rule_name,
        "predicted_action": _action_to_dict(shadow.predicted_action),
        "trigger_inputs": dict(shadow.trigger_inputs),
        "rationale": shadow.rationale,
    }
    state.record_event(
        unit_state.unit_id,
        unit_state.feature_id,
        "shadow_transition_proposed",
        source="orchestrator",
        cycle_number=unit_state.review_round,
        summary=f"shadow rule {shadow.rule_name!r} fired",
        details=json.dumps(payload),
    )


def _action_to_dict(action: Action) -> dict:
    """Serialise an :class:`Action` for JSON storage.

    ``Action.payload`` is a :class:`_FrozenDict`; ``dict(payload)``
    copies it to a plain dict so :func:`json.dumps` works without the
    custom subclass tripping a serialiser later.
    """
    return {
        "kind": action.kind,
        "target_status": action.target_status,
        "clear_error": action.clear_error,
        "event_type": action.event_type,
        "event_source": action.event_source,
        "summary": action.summary,
        "details": action.details,
        "set_last_error": action.set_last_error,
        "side_effect": action.side_effect,
        "payload": dict(action.payload),
    }


def _maybe_record_snapshot(unit_state: WorkUnitState, report: HealthReport) -> bool:
    """Persist a ``health_report_snapshot`` event at most once per interval.

    Returns ``True`` when a snapshot was recorded this call. The dedupe
    is by ``(unit_id, event_type='health_report_snapshot')`` over the
    interval window — first probe in the window writes, subsequent
    probes are no-ops.
    """
    interval = _snapshot_interval_hours()
    if interval <= 0:
        return False
    now = datetime.now(UTC)
    events = state.list_events(unit_state.unit_id)
    cutoff = now.timestamp() - interval * 3600
    for ev in reversed(events):  # newest snapshots last; scan tail first
        if ev.get("event_type") != "health_report_snapshot":
            continue
        ts = ev.get("ts") or ""
        try:
            normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.timestamp() >= cutoff:
            return False  # already snapshotted within the window
    state.record_event(
        unit_state.unit_id,
        unit_state.feature_id,
        "health_report_snapshot",
        source="orchestrator",
        cycle_number=unit_state.review_round,
        summary="health report snapshot",
        details=json.dumps(_report_to_dict(report)),
    )
    return True


def _report_to_dict(report: HealthReport) -> dict:
    """Serialise a :class:`HealthReport` for the snapshot event.

    :func:`dataclasses.asdict` already handles the nested snapshot
    dataclasses; the only adjustment is that ``HealthReport.pr`` may be
    ``None`` (no PR yet), which ``asdict`` propagates correctly.
    """
    return asdict(report)


# ---------------------------------------------------------------------------
# Shared core — used by ``inspect_unit_health`` and by the deprecated
# aliases when they need probe + decide without the markdown wrapping.
# ---------------------------------------------------------------------------


def _load_context(unit_id: str) -> tuple[WorkUnitState, str] | str:
    """Validate the unit + feature + token; return ``(unit_state, repo_url)`` or an error string."""
    unit_state = state.get_unit_state(unit_id)
    if not unit_state or not unit_state.pr_number:
        return f"ERROR: unit {unit_id} has no PR"
    feature = state.get_feature(unit_state.feature_id)
    if not feature:
        return f"ERROR: feature for unit {unit_id} not found"
    if err := need_github_token():
        return err
    return unit_state, feature.repo_path


def _probe_and_decide(
    unit_state: WorkUnitState, repo_url: str
) -> tuple[HealthReport, Decision] | str:
    """Run probe + decide. Returns ``(report, decision)`` or an error string."""
    assert unit_state.pr_number is not None  # noqa: S101  # nosec B101 — guarded by _load_context
    gh_client = _ProductionGitHubClient(repo_url, unit_state.pr_number)
    anth_client = _ProductionAnthropicClient()
    try:
        report = probe_unit_health(
            unit_state.unit_id, gh_client, anth_client, local_state=unit_state
        )
    except Exception as e:  # noqa: BLE001
        return f"ERROR querying GitHub: {e}"
    decision = decide_transitions(unit_state, report)
    return report, decision


# ---------------------------------------------------------------------------
# Markdown digest — what the lead sees in chat.
# ---------------------------------------------------------------------------


def _format_pr_line(report: HealthReport) -> str:
    pr = report.pr
    if pr is None:
        return "PR: none"
    parts = [f"state={pr.state!r}", f"merged={pr.merged}"]
    if pr.mergeable_state:
        parts.append(f"mergeable_state={pr.mergeable_state!r}")
    if pr.conflict_files:
        parts.append(f"conflict_files={pr.conflict_files}")
    if pr.merged_at:
        parts.append(f"merged_at={pr.merged_at}")
    return "PR: " + ", ".join(parts)


def _format_ci_line(report: HealthReport) -> str:
    ci = report.ci
    return (
        f"CI: {len(ci.runs)} runs, {len(ci.failing)} failing, "
        f"{len(ci.pending)} pending, {len(ci.missing_required)} missing required"
    )


def _format_reviews_line(report: HealthReport) -> str:
    r = report.reviews
    return (
        f"Reviews: approvals={r.approvals}, changes_requested={r.changes_requested}, "
        f"unresolved_threads={r.unresolved_threads}, copilot_present={r.copilot_present}"
    )


def _format_workers_line(report: HealthReport) -> str:
    if not report.workers:
        return "Workers: none"
    pairs = ", ".join(f"{w.role}={w.session_status or 'unknown'}" for w in report.workers)
    return f"Workers: {pairs}"


def _format_orch_line(report: HealthReport) -> str:
    o = report.orchestrator
    return (
        f"Orchestrator: cycle {o.cycle}/{o.cycle_cap}, "
        f"cycles_remaining={o.cycles_remaining}, "
        f"last_activity_age_seconds={o.last_activity_age_seconds}, "
        f"downstream_blocked={o.downstream_blocked}"
    )


def _format_action(action: Action) -> str:
    if action.kind == "transition":
        suffix = " (clear_error)" if action.clear_error else ""
        return f"- transition → {action.target_status}: {action.summary}{suffix}"
    if action.kind == "event":
        last_err = f" (set_last_error={action.set_last_error!r})" if action.set_last_error else ""
        return f"- event {action.event_type}: {action.summary}{last_err}"
    if action.kind == "side_effect":
        return f"- side_effect {action.side_effect}: {action.summary}"
    return f"- {action.kind}: {action.summary}"


def _format_shadow(shadow: ShadowDecision) -> str:
    pred = shadow.predicted_action
    pred_str = pred.target_status if pred.kind == "transition" else pred.event_type
    return (
        f"- {shadow.rule_name} (predicted {pred.kind} → {pred_str!r})\n"
        f"  rationale: {shadow.rationale}"
    )


def _render_markdown(
    unit_id: str,
    *,
    dry_run: bool,
    report: HealthReport,
    decision: Decision,
    snapshotted: bool,
) -> str:
    mode = "dry_run" if dry_run else "live"
    actions = decision.actions_to_apply
    applied_count = 0 if dry_run else len(actions)
    summary_lines = [
        "## HealthReport summary",
        f"- {_format_pr_line(report)}",
        f"- {_format_ci_line(report)}",
        f"- {_format_reviews_line(report)}",
        f"- {_format_workers_line(report)}",
        f"- {_format_orch_line(report)}",
    ]
    applied_lines = [f"## Applied actions ({applied_count})"]
    if dry_run:
        applied_lines.append("_dry_run — no actions applied; the following would fire:_")
        empty_label = "- (none would fire)"
    else:
        empty_label = "- (none)"
    if actions:
        applied_lines.extend(_format_action(a) for a in actions)
    else:
        applied_lines.append(empty_label)
    shadow_lines = [f"## Shadow decisions ({len(decision.shadow_decisions)})"]
    if decision.shadow_decisions:
        shadow_lines.extend(_format_shadow(s) for s in decision.shadow_decisions)
    else:
        shadow_lines.append("- (none)")
    parts = [
        f"# inspect_unit_health: {unit_id} ({mode})",
        "",
        *summary_lines,
        "",
        *applied_lines,
        "",
        *shadow_lines,
    ]
    if snapshotted:
        parts.extend(["", "_health_report_snapshot recorded for forensics._"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


@mcp.tool()
def inspect_unit_health(unit_id: str, dry_run: bool = False) -> str:
    """Canonical unit-health probe. Reconciles state with GitHub reality.

    Probes the unit's PR (merge state, conflicts, CI check_runs, reviews,
    Copilot presence) and worker sessions, then runs the
    :mod:`orchestrator.health` decision table.

    On ``dry_run=False`` (the default), each ``actions_to_apply`` is
    executed via :func:`orchestrator.state.touch_unit` /
    :func:`orchestrator.state.record_event`, each
    :class:`~orchestrator.health.ShadowDecision` is persisted as a
    ``shadow_transition_proposed`` event with the full structured
    payload in ``details``, and once per
    ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS`` (default 24h) per unit a
    full :class:`~orchestrator.health.HealthReport` is stored as a
    ``health_report_snapshot`` event for forensics retention.

    On ``dry_run=True``, the probe + decision still runs, but no state
    is mutated and no events are emitted. Used by the deprecated
    ``check_unit_pr`` alias for read-only inspection.

    Covers the three reconcile cells from the deprecated
    ``reconcile_unit_pr`` (``merged + in_ci`` / ``approved_awaiting_merge``
    / ``escalated``) plus new event-only signals
    (``pr_conflict_detected``, ``required_check_missing``,
    ``ci_drift_detected``) and the cycle-log writer side effect on
    merged polls.

    Returns a markdown digest with the HealthReport summary, the
    applied actions (what changed in state.db), and the shadow
    decisions (what the decision table would do once promoted out of
    shadow mode).
    """
    ctx = _load_context(unit_id)
    if isinstance(ctx, str):
        return ctx
    unit_state, repo_url = ctx

    result = _probe_and_decide(unit_state, repo_url)
    if isinstance(result, str):
        return result
    report, decision = result

    snapshotted = False
    if not dry_run:
        for action in decision.actions_to_apply:
            _apply_action(unit_state, action)
        for shadow in decision.shadow_decisions:
            _record_shadow_event(unit_state, shadow)
        snapshotted = _maybe_record_snapshot(unit_state, report)

    return _render_markdown(
        unit_id,
        dry_run=dry_run,
        report=report,
        decision=decision,
        snapshotted=snapshotted,
    )
