"""Canonical ``inspect_unit_health`` MCP tool.

Wires the pure probe + decision table from :mod:`orchestrator.health` (built
in F-014-U-1) into the MCP layer with full event persistence:

* applies ``actions_to_apply`` via the existing ``state.touch_unit`` /
  ``state.record_event`` / ``cycle_log.write_cycle_log`` code paths
  (reused, not duplicated, so the alias-vs-canonical behavior cannot drift);
* records every ``ShadowDecision`` as a ``shadow_transition_proposed``
  event with the structured payload in ``details`` JSON so F-015 has a
  durable backtest set;
* records one ``health_report_snapshot`` event per unit per UTC day for
  forensics retention — toggleable via :data:`SNAPSHOT_RETENTION_ENV`.

Returns a markdown digest (HealthReport summary + applied actions +
shadow decisions) so the lead sees both what changed and what was
deferred.

Companion modules:

* :mod:`orchestrator.health` — pure probe + decision table.
* :mod:`orchestrator.tools.ops` — deprecated ``check_unit_pr`` /
  ``reconcile_unit_pr`` aliases. Both keep their legacy JSON return shape
  for backward compatibility; their state writes flow through the same
  ``state.touch_unit`` / ``state.record_event`` primitives this module
  uses, so calling either tool advances the unit identically to calling
  ``inspect_unit_health(dry_run=True/False)``.
"""

from __future__ import annotations

import contextlib
import json
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

SNAPSHOT_RETENTION_ENV = "ORCH_HEALTH_SNAPSHOT_DAILY"
"""Env var that toggles the per-day ``health_report_snapshot`` event.

Default (unset or any truthy value) records one snapshot per unit per
UTC day. Set to ``0`` / ``false`` / ``no`` / ``off`` to disable snapshot
retention entirely — useful for dev runs and CI smoke tests that don't
want the event log peppered with full HealthReport JSON.
"""

# Values that count as "snapshot disabled". Lower-cased before lookup so
# ``False`` / ``FALSE`` / ``False`` all behave the same.
_SNAPSHOT_DISABLED_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off"})

SHADOW_EVENT_TYPE = "shadow_transition_proposed"
"""``unit_events.event_type`` for shadow-decision audit rows."""

SNAPSHOT_EVENT_TYPE = "health_report_snapshot"
"""``unit_events.event_type`` for the per-day HealthReport snapshot row."""


def _snapshot_enabled() -> bool:
    raw = (os.getenv(SNAPSHOT_RETENTION_ENV, "") or "").strip().lower()
    if not raw:
        return True
    return raw not in _SNAPSHOT_DISABLED_VALUES


# --------------------------- production clients ---------------------------


class _ProductionGitHubHealthClient:
    """:class:`GitHubHealthClient` adapter over :mod:`orchestrator.github`.

    Memoizes the PR payload so the probe makes at most one round-trip
    per endpoint family. Endpoints the production github module doesn't
    yet expose (review threads, requested reviewers, compare-to-base,
    force-push history, merge-commit reachability) return the safe
    empty default — health rules built on those signals stay silent
    until the underlying query ships rather than crashing the probe
    today.
    """

    def __init__(self, repo_url: str, pr_number: int) -> None:
        self._repo = repo_url
        self._pr = pr_number
        self._pr_payload: dict | None = None

    def _payload(self) -> dict:
        if self._pr_payload is None:
            with contextlib.suppress(Exception):
                self._pr_payload = github.get_pr_state(self._repo, self._pr)
        return self._pr_payload or {}

    def get_pr(self, unit_id: str) -> dict | None:
        payload = self._payload()
        if not payload:
            return None
        return {
            "state": payload.get("state"),
            "merged": payload.get("merged", False),
            "mergeable": payload.get("mergeable"),
            "mergeable_state": payload.get("mergeable_state"),
            "head_sha": payload.get("head_sha"),
            "merge_commit_sha": payload.get("merge_commit_sha"),
            "merged_at": payload.get("merged_at"),
            "base": payload.get("base"),
        }

    def get_check_runs(self, unit_id: str) -> list[dict]:
        with contextlib.suppress(Exception):
            data = github.get_pr_check_runs(self._repo, self._pr)
            return list(data.get("runs") or [])
        return []

    def get_required_checks(self, unit_id: str) -> list[str]:
        return []

    def get_compare_to_base(self, unit_id: str) -> dict:
        return {"ahead_by": None, "behind_by": None}

    def get_reviews(self, unit_id: str) -> list[dict]:
        return []

    def get_review_threads(self, unit_id: str) -> list[dict]:
        return []

    def get_requested_reviewers(self, unit_id: str) -> dict:
        return {"users": [], "teams": []}

    def get_copilot_review(self, unit_id: str) -> dict | None:
        with contextlib.suppress(Exception):
            return github.get_copilot_review(self._repo, self._pr)
        return None

    def get_last_force_push_at(self, unit_id: str) -> str | None:
        return None

    def get_head_commit(self, unit_id: str) -> dict:
        return {"sha": self._payload().get("head_sha"), "committed_at": None}

    def is_merge_commit_on_main(self, unit_id: str) -> bool:
        return True


class _ProductionAnthropicHealthClient:
    """:class:`AnthropicHealthClient` adapter over :class:`ManagedAgentWorker`.

    The Anthropic SDK's sessions API is role-independent, so we instantiate
    one worker (under the ``coder`` role) and call ``sessions.retrieve``
    for every session id the probe needs.
    """

    def __init__(self) -> None:
        self._worker = ManagedAgentWorker(role="coder")

    def get_session_status(self, session_id: str) -> str | None:
        try:
            session = self._worker.client.beta.sessions.retrieve(session_id)
        except Exception:  # noqa: BLE001 — unknown / expired sessions return None
            return None
        return getattr(session, "status", None)


def _make_gh_client(repo_url: str, pr_number: int) -> GitHubHealthClient:
    """Production GitHubHealthClient factory.

    Lifted to module scope so tests can monkeypatch the client
    construction without monkeypatching the underlying GitHub helpers.
    """
    return _ProductionGitHubHealthClient(repo_url, pr_number)


def _make_anthropic_client() -> AnthropicHealthClient:
    """Production AnthropicHealthClient factory — see :func:`_make_gh_client`."""
    return _ProductionAnthropicHealthClient()


# --------------------------- action applier ---------------------------


def _apply_action(unit_state: WorkUnitState, action: Action) -> None:
    """Apply one :class:`Action` to durable state.

    Routes ``transition`` / ``event`` / ``side_effect`` through the
    existing ``state.touch_unit`` / ``state.record_event`` /
    ``cycle_log.write_cycle_log`` helpers so the canonical surface and
    the deprecated ``reconcile_unit_pr`` alias cannot diverge in how
    they advance the unit row or write events.
    """
    unit_id = unit_state.unit_id
    feature_id = unit_state.feature_id
    cycle = unit_state.review_round

    if action.kind == "transition":
        state.touch_unit(
            unit_id,
            status=action.target_status,
            clear_error=action.clear_error,
        )
        return

    if action.kind == "event":
        if action.set_last_error:
            state.touch_unit(unit_id, error=action.set_last_error)
        state.record_event(
            unit_id,
            feature_id,
            action.event_type or "",
            source=action.event_source,
            cycle_number=cycle,
            summary=action.summary,
            details=action.details,
        )
        return

    if action.kind == "side_effect" and action.side_effect == "write_cycle_log":
        payload = dict(action.payload)
        merge_commit_sha = payload.get("merge_commit_sha")
        if not merge_commit_sha:
            return
        with contextlib.suppress(Exception):
            cycle_log.write_cycle_log(
                payload.get("unit_id", unit_id),
                base_dir=cycle_log.cycle_log_base_dir(),
                merge_commit_sha=merge_commit_sha,
                commit_message=payload.get("commit_message"),
            )


def _apply_decision(unit_state: WorkUnitState, decision: Decision) -> list[Action]:
    """Apply every action in ``decision.actions_to_apply``.

    Returns the applied list so the caller can surface it in the digest
    without re-iterating the decision. Order matters: transitions land
    before their companion ``merged`` / ``recovered_from_escalated``
    events so the event-time row already reflects ``status=done``.
    """
    for action in decision.actions_to_apply:
        _apply_action(unit_state, action)
    return list(decision.actions_to_apply)


# --------------------------- shadow + snapshot persistence ---------------------------


def _shadow_details(decision: ShadowDecision) -> str:
    """JSON ``details`` payload for a ``shadow_transition_proposed`` event.

    Carries the full :class:`ShadowDecision` so F-015's promotion path
    can replay the predicate against the live state without re-probing.
    """
    return json.dumps(
        {
            "rule_name": decision.rule_name,
            "predicted_action": asdict(decision.predicted_action),
            "trigger_inputs": dict(decision.trigger_inputs),
            "rationale": decision.rationale,
        },
        sort_keys=True,
    )


def _record_shadow_decisions(unit_state: WorkUnitState, decision: Decision) -> list[ShadowDecision]:
    """Persist one ``shadow_transition_proposed`` event per shadow decision.

    Returns the list so the caller can include it in the markdown
    digest. Recorded under ``source='orchestrator'`` (this is engine-
    emitted observability, not a human/agent signal).
    """
    for shadow in decision.shadow_decisions:
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            SHADOW_EVENT_TYPE,
            source="orchestrator",
            cycle_number=unit_state.review_round,
            summary=f"shadow: {shadow.rule_name}",
            details=_shadow_details(shadow),
        )
    return list(decision.shadow_decisions)


def _snapshot_already_today(unit_id: str, today: str) -> bool:
    """True if a ``health_report_snapshot`` event was recorded today (UTC)."""
    events = state.list_events(unit_id)
    return any(
        e["event_type"] == SNAPSHOT_EVENT_TYPE and (e.get("ts") or "").startswith(today)
        for e in events
    )


def _record_snapshot_if_first_today(
    unit_state: WorkUnitState, report: HealthReport, *, now: datetime
) -> bool:
    """Append a ``health_report_snapshot`` event when it's the first today.

    Returns True if a snapshot was written (lets the digest say so).
    Skips entirely when :data:`SNAPSHOT_RETENTION_ENV` disables retention.
    """
    if not _snapshot_enabled():
        return False
    today = now.astimezone(UTC).date().isoformat()
    if _snapshot_already_today(unit_state.unit_id, today):
        return False
    state.record_event(
        unit_state.unit_id,
        unit_state.feature_id,
        SNAPSHOT_EVENT_TYPE,
        source="orchestrator",
        cycle_number=unit_state.review_round,
        summary=f"health snapshot for {today}",
        details=json.dumps(asdict(report), sort_keys=True, default=str),
    )
    return True


# --------------------------- digest rendering ---------------------------


def _format_status_line(report: HealthReport) -> str:
    pr = report.pr
    if pr is None:
        return "PR: (none)"
    pr_state = pr.state or "unknown"
    flag = "merged" if pr.merged else pr_state
    return f"PR: {flag} (mergeable_state={pr.mergeable_state or 'unknown'})"


def _format_ci_line(report: HealthReport) -> str:
    ci = report.ci
    return (
        f"CI: {len(ci.runs)} run(s), failing={len(ci.failing)}, "
        f"pending={len(ci.pending)}, missing_required={len(ci.missing_required)}"
    )


def _format_reviews_line(report: HealthReport) -> str:
    r = report.reviews
    return (
        f"Reviews: approvals={r.approvals}, changes_requested={r.changes_requested}, "
        f"unresolved_threads={r.unresolved_threads}, copilot={r.copilot_present}"
    )


def _format_orchestrator_line(report: HealthReport) -> str:
    o = report.orchestrator
    return f"Orchestrator: cycle={o.cycle}/{o.cycle_cap}, downstream_blocked={o.downstream_blocked}"


def _format_applied_action(action: Action) -> str:
    if action.kind == "transition":
        target = action.target_status or "?"
        suffix = " (clears last_error)" if action.clear_error else ""
        return f"  - transition → {target}{suffix}"
    if action.kind == "event":
        return f"  - event: {action.event_type} — {action.summary}"
    if action.kind == "side_effect":
        return f"  - side_effect: {action.side_effect}"
    return f"  - {action.kind}: {action.summary}"


def _format_shadow_decision(shadow: ShadowDecision) -> str:
    predicted = shadow.predicted_action
    if predicted.kind == "transition":
        predicted_str = f"transition → {predicted.target_status}"
    elif predicted.kind == "event":
        predicted_str = f"event {predicted.event_type}"
    else:
        predicted_str = predicted.kind
    return f"  - **{shadow.rule_name}** (would: {predicted_str})\n    rationale: {shadow.rationale}"


def _render_digest(
    unit_id: str,
    report: HealthReport,
    *,
    dry_run: bool,
    applied_actions: list[Action],
    shadow_decisions: list[ShadowDecision],
    snapshot_recorded: bool,
) -> str:
    """Markdown digest surfaced to the lead.

    Dry-run renders skip the ``Applied actions`` block (which is always
    empty) so the digest reads as a pure observation; non-dry-run shows
    both buckets so the lead sees what landed and what's still shadow-
    only.
    """
    mode = "dry-run" if dry_run else "applied"
    lines = [
        f"# Health for {unit_id} ({mode})",
        "",
        _format_status_line(report),
        _format_ci_line(report),
        _format_reviews_line(report),
        _format_orchestrator_line(report),
    ]
    if snapshot_recorded:
        lines.append("Snapshot: recorded (first probe today)")
    lines.append("")
    if not dry_run:
        if applied_actions:
            lines.append("## Applied actions")
            lines.extend(_format_applied_action(a) for a in applied_actions)
        else:
            lines.append("## Applied actions: none")
        lines.append("")
    if shadow_decisions:
        lines.append("## Shadow decisions (observed, not applied)")
        lines.extend(_format_shadow_decision(s) for s in shadow_decisions)
    else:
        lines.append("## Shadow decisions: none")
    return "\n".join(lines)


# --------------------------- entry point ---------------------------


@mcp.tool()
def inspect_unit_health(unit_id: str, dry_run: bool = False) -> str:
    """Canonical full-spectrum health probe for one work unit.

    Probes GitHub (PR state / CI / reviews / conflicts) and Anthropic
    (worker session liveness) via :func:`orchestrator.health.probe_unit_health`,
    runs the pure decision table from :func:`decide_transitions`, then
    (when ``dry_run=False``):

      * applies every ``actions_to_apply`` action — the same
        ``status='done'`` / ``merged`` / ``recovered_from_escalated`` /
        ``reconcile_refused`` transitions ``reconcile_unit_pr`` does,
        plus the new event-only signals (``pr_conflict_detected``,
        ``required_check_missing``, ``ci_drift_detected``) and the
        ``write_cycle_log`` side effect on a populated
        ``merge_commit_sha``;
      * records one ``shadow_transition_proposed`` event per
        :class:`~orchestrator.health.ShadowDecision` returned by U-1
        (rule_name + predicted_action + trigger_inputs + rationale in
        ``details`` JSON) so F-015 can promote these from observation
        to action with a backtest;
      * records a ``health_report_snapshot`` event (full serialized
        :class:`~orchestrator.health.HealthReport` in ``details``) on
        the first probe per unit per UTC day for forensics retention —
        toggle via the ``ORCH_HEALTH_SNAPSHOT_DAILY`` env var.

    Returns a markdown digest with the HealthReport summary, the
    applied-actions list, and the shadow-decisions list so the lead
    sees both what changed and what was deferred.

    Set ``dry_run=True`` to skip every write (transitions, events,
    snapshots) — useful after a restart when you want to see what
    *would* happen before letting the orchestrator act.

    Supersedes the narrow ``check_unit_pr`` / ``reconcile_unit_pr``
    tools, which remain available as deprecated aliases for backward
    compatibility.
    """
    unit_state = state.get_unit_state(unit_id)
    if not unit_state or not unit_state.pr_number:
        return f"ERROR: unit {unit_id} has no PR"

    feature = state.get_feature(unit_state.feature_id)
    if not feature:
        return f"ERROR: feature for unit {unit_id} not found"

    if err := need_github_token():
        return err

    now = datetime.now(UTC)
    try:
        gh_client = _make_gh_client(feature.repo_path, unit_state.pr_number)
        anthropic_client = _make_anthropic_client()
        report = probe_unit_health(
            unit_id,
            gh_client,
            anthropic_client,
            local_state=unit_state,
            now=now,
        )
    except Exception as e:  # noqa: BLE001 — surface upstream errors to the lead
        return f"ERROR probing unit health: {e}"

    decision = decide_transitions(unit_state, report)

    if dry_run:
        return _render_digest(
            unit_id,
            report,
            dry_run=True,
            applied_actions=[],
            shadow_decisions=list(decision.shadow_decisions),
            snapshot_recorded=False,
        )

    applied = _apply_decision(unit_state, decision)
    shadows = _record_shadow_decisions(unit_state, decision)
    snapshot_recorded = _record_snapshot_if_first_today(unit_state, report, now=now)
    return _render_digest(
        unit_id,
        report,
        dry_run=False,
        applied_actions=applied,
        shadow_decisions=shadows,
        snapshot_recorded=snapshot_recorded,
    )


__all__ = [
    "SHADOW_EVENT_TYPE",
    "SNAPSHOT_EVENT_TYPE",
    "SNAPSHOT_RETENTION_ENV",
    "inspect_unit_health",
]
