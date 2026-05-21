"""Unit-health probe + decision table — pure logic, no I/O of its own.

Two public functions:

* :func:`probe_unit_health` — assembles a :class:`HealthReport` from data
  pulled via two injected clients (:class:`GitHubHealthClient` and
  :class:`AnthropicHealthClient`) plus the unit's local
  :class:`~orchestrator.models.WorkUnitState`. The function performs no
  network, shell, or DB I/O itself — every external call goes through
  the clients, every local fact comes in via ``local_state`` /
  ``downstream_blocked``. Production callers wire the real GitHub /
  Anthropic clients; tests inject fakes.

* :func:`decide_transitions` — a pure decision table over the
  ``(local_state, report)`` pair. Returns a :class:`Decision` with two
  buckets:

  - ``actions_to_apply`` — live transitions and events the MCP layer
    should execute on this call. Covers the three reconcile cells from
    :func:`orchestrator.tools.ops.reconcile_unit_pr` plus three new
    event-only signals (``pr_conflict_detected``,
    ``required_check_missing``, ``ci_drift_detected``) and the
    ``write_cycle_log`` side-effect on a populated ``merge_commit_sha``.

  - ``shadow_decisions`` — rules we want to *observe* but not yet
    promote into live transitions. Each carries ``rule_name``,
    ``predicted_action``, structured ``trigger_inputs``, and a
    free-text ``rationale``. The MCP tool surfaces these alongside the
    applied actions so the lead can sanity-check the proposed rule
    against reality before we promote it.

Both functions are stateless and frozen-dataclass-friendly so the
results are easy to snapshot, diff, and table-test.

See the feature spec (``inspect_unit_health`` MCP tool) for how this
module composes with ``reconcile_unit_pr`` — same logical
transitions, but probed against a much richer signal set
(conflicts, required-checks, codeowner reviewers, copilot, worker
sessions) and with the shadow channel as a safe runway for new
rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from orchestrator.ci_wait import FAILURE_CONCLUSIONS, PASSING_CONCLUSIONS
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    TERMINAL_UNIT_STATUSES,
    WorkUnitState,
)

# Default cycle cap matches ``cycle_review`` (see CLAUDE.md "Cap-3 mechanics").
# Kept overridable so tests can probe edge proximity (cycle = cap - 0 / -1).
DEFAULT_CYCLE_CAP = 3

# ``mergeable_state`` values that GitHub uses to signal a real conflict on the
# PR head. ``dirty`` is the historical name; ``conflicting`` is what the
# Checks-style endpoints sometimes emit. Either fires
# ``pr_conflict_detected``.
_CONFLICTING_MERGEABLE_STATES: frozenset[str] = frozenset({"dirty", "conflicting"})

# Statuses where the orchestrator's view is "agent is actively driving the
# fix loop" — CI red is expected on these and not "drift". Outside this
# set, a red CI run means the (status, ci) pair has drifted apart.
_STATUSES_WHERE_RED_CI_IS_EXPECTED: frozenset[str] = frozenset({"coding", "fixing", "testing"})

# Active-role statuses that must not be silently advanced to ``done`` even
# if the PR is merged — matches ``ops._RECONCILE_REFUSED_STATUSES``. An
# observation here means the human merged while a worker was mid-flight;
# refuse with an event rather than racing the agent.
_REFUSE_MERGE_FROM_STATUSES: frozenset[str] = ACTIVE_UNIT_STATUSES - {"in_ci"}

# Statuses for which ``ops.reconcile_unit_pr`` calls ``write_cycle_log``
# after observing a merge: the three status-flipping transitions plus the
# idempotent re-render on the post-``done`` SHA-backfill poll. ``merged +
# active-role`` (reconcile_refused) and ``merged + pending`` are excluded
# because finalising a log from an in-flight ``unit_events`` tail would
# capture a partial cycle.
_LOG_WRITABLE_STATUSES: frozenset[str] = frozenset(
    {"in_ci", "approved_awaiting_merge", "escalated", "done"}
)

# Statuses where the ``merged → done`` transition fires (vs. the
# escalated path that also clears ``last_error`` and emits the recovered
# event). Used to keep ``_merge_transitions`` data-driven instead of
# branching three times on the same status set.
_MERGED_TRANSITION_STATUSES: frozenset[str] = frozenset(
    {"in_ci", "approved_awaiting_merge", "escalated"}
)


# --------------------------- time helpers ---------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # ``datetime.fromisoformat`` accepts ``Z`` only on Python 3.11+ with
        # a ``+00:00`` replacement. Normalize so older fixtures (and
        # GitHub's own ``2026-...Z`` returns) parse.
        normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _age_seconds(ts: str | None, now: datetime) -> int | None:
    parsed = _parse_iso(ts)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int((now - parsed).total_seconds())


# --------------------------- client protocols ---------------------------


@runtime_checkable
class GitHubHealthClient(Protocol):
    """Per-unit GitHub data source.

    The production implementation reads ``state.db`` to map ``unit_id``
    onto ``(repo_url, pr_number)`` and then calls the appropriate
    REST / GraphQL endpoints. Tests inject a fake that returns canned
    responses keyed by ``unit_id``.

    Every method returns plain dicts / lists so this module stays free
    of GitHub SDK types — the dicts are versionable, the SDKs aren't.
    """

    def get_pr(self, unit_id: str) -> dict | None:
        """PR snapshot or ``None`` when the unit has no PR."""
        ...

    def get_check_runs(self, unit_id: str) -> list[dict]:
        """All check_runs on the PR head commit."""
        ...

    def get_required_checks(self, unit_id: str) -> list[str]:
        """Check-run names required by the base branch's protection rule."""
        ...

    def get_compare_to_base(self, unit_id: str) -> dict:
        """``{"ahead_by": int, "behind_by": int}`` for head vs. base."""
        ...

    def get_reviews(self, unit_id: str) -> list[dict]:
        """All reviews on the PR (APPROVED / CHANGES_REQUESTED / DISMISSED / ...)."""
        ...

    def get_review_threads(self, unit_id: str) -> list[dict]:
        """GraphQL ``reviewThreads`` nodes (``is_resolved``, ``is_outdated``)."""
        ...

    def get_requested_reviewers(self, unit_id: str) -> dict:
        """``{"users": [...], "teams": [...]}`` — codeowner / human asks."""
        ...

    def get_copilot_review(self, unit_id: str) -> dict | None:
        """Latest Copilot review or ``None`` when Copilot didn't review."""
        ...

    def get_last_force_push_at(self, unit_id: str) -> str | None:
        """ISO timestamp of the most recent force-push on the branch."""
        ...

    def get_head_commit(self, unit_id: str) -> dict:
        """``{"sha": str, "committed_at": iso_str}`` for the PR head."""
        ...

    def is_merge_commit_on_main(self, unit_id: str) -> bool:
        """Whether the PR's ``merge_commit_sha`` is still reachable from main."""
        ...


@runtime_checkable
class AnthropicHealthClient(Protocol):
    """Per-session Anthropic data source.

    Returns ``"idle"`` / ``"running"`` / ``"terminated"`` for a known
    session, or ``None`` for an unknown session id. The shape mirrors
    the existing ``resume_unit`` MCP tool's view of session status.
    """

    def get_session_status(self, session_id: str) -> str | None: ...


# --------------------------- snapshots ---------------------------


@dataclass(frozen=True)
class PRSnapshot:
    state: str | None
    merged: bool
    mergeable: bool | None
    mergeable_state: str | None
    conflict_files: list[str]
    head_sha: str | None
    merge_commit_sha: str | None
    merged_at: str | None
    base: str | None

    @classmethod
    def from_dict(cls, pr: dict | None) -> PRSnapshot | None:
        """Build from the dict :meth:`GitHubHealthClient.get_pr` returns.

        ``None`` propagates as ``None`` so callers can use the snapshot
        as a "PR exists?" guard. ``conflict_files`` is a GH-side
        enrichment (not part of the vanilla PR payload) — the
        production client populates it when ``mergeable_state``
        indicates a conflict, otherwise an absent key means "no list
        available".
        """
        if pr is None:
            return None
        return cls(
            state=pr.get("state"),
            merged=bool(pr.get("merged")),
            mergeable=pr.get("mergeable"),
            mergeable_state=pr.get("mergeable_state"),
            conflict_files=list(pr.get("conflict_files") or []),
            head_sha=pr.get("head_sha"),
            merge_commit_sha=pr.get("merge_commit_sha"),
            merged_at=pr.get("merged_at"),
            base=pr.get("base"),
        )


@dataclass(frozen=True)
class GitSnapshot:
    ahead_by: int | None
    behind_by: int | None
    head_sha: str | None
    head_age_seconds: int | None
    last_force_push_at: str | None


@dataclass(frozen=True)
class CheckRunInfo:
    name: str
    status: str | None
    conclusion: str | None
    details_url: str | None

    @classmethod
    def from_dict(cls, r: dict) -> CheckRunInfo:
        return cls(
            name=r.get("name", ""),
            status=r.get("status"),
            conclusion=r.get("conclusion"),
            details_url=r.get("details_url"),
        )


@dataclass(frozen=True)
class CISnapshot:
    runs: list[CheckRunInfo]
    pending: list[str]
    failing: list[str]
    required: list[str]
    missing_required: list[str]

    @classmethod
    def from_lists(cls, check_runs: list[dict], required: list[str]) -> CISnapshot:
        """Build from raw ``check_runs`` + ``required`` lists.

        Conclusion taxonomy is shared with ``ci_wait``: ``skipped`` /
        ``neutral`` pass; ``cancelled`` / ``timed_out`` /
        ``action_required`` / ``stale`` fail alongside ``failure``.
        Reusing the canonical sets avoids divergent drift / shadow rules
        across the two modules.
        """
        runs = [CheckRunInfo.from_dict(r) for r in check_runs]
        pending = [r.name for r in runs if r.conclusion is None and r.status != "completed"]
        failing = [r.name for r in runs if r.conclusion in FAILURE_CONCLUSIONS]
        actual_names = {r.name for r in runs}
        return cls(
            runs=runs,
            pending=pending,
            failing=failing,
            required=list(required),
            missing_required=[name for name in required if name not in actual_names],
        )


@dataclass(frozen=True)
class ReviewSnapshot:
    approvals: int
    changes_requested: int
    dismissed: int
    unresolved_threads: int
    codeowner_requested: list[str]
    copilot_present: bool
    copilot_state: str | None

    @classmethod
    def from_lists(
        cls,
        reviews: list[dict],
        review_threads: list[dict],
        requested_reviewers: dict,
        copilot_review: dict | None,
    ) -> ReviewSnapshot:
        """Build from the four GitHub-side review-domain dicts.

        Outdated threads are GH's signal that the diff moved past the
        comment's anchor — not "open work the coder needs to address"
        — so they're excluded from the unresolved count. Teams get a
        ``team:`` prefix to disambiguate from user logins (a team name
        and a user login can collide).
        """
        approvals = sum(
            1 for r in reviews if r.get("state") == "APPROVED" and not r.get("dismissed")
        )
        changes_requested = sum(
            1 for r in reviews if r.get("state") == "CHANGES_REQUESTED" and not r.get("dismissed")
        )
        dismissed = sum(1 for r in reviews if r.get("dismissed") or r.get("state") == "DISMISSED")
        unresolved = sum(
            1 for t in review_threads if not t.get("is_resolved") and not t.get("is_outdated")
        )
        users = list(requested_reviewers.get("users") or [])
        teams = [f"team:{t}" for t in (requested_reviewers.get("teams") or [])]
        return cls(
            approvals=approvals,
            changes_requested=changes_requested,
            dismissed=dismissed,
            unresolved_threads=unresolved,
            codeowner_requested=users + teams,
            copilot_present=copilot_review is not None,
            copilot_state=copilot_review.get("state") if copilot_review else None,
        )


@dataclass(frozen=True)
class WorkerSessionInfo:
    role: str
    session_id: str
    session_status: str | None

    @classmethod
    def collect(
        cls, local_state: WorkUnitState, anthropic_client: AnthropicHealthClient
    ) -> list[WorkerSessionInfo]:
        """One :class:`WorkerSessionInfo` per role whose session_id is set.

        Skipping the missing-session rows (rather than emitting
        ``not_found`` placeholders) keeps the report focused on roles
        that have actually been spawned for this unit. The MCP tool
        that surfaces the report can phrase "tester not yet spawned"
        from the absence rather than from a noisy entry.
        """
        role_to_sid = (
            ("coder", local_state.coder_session_id),
            ("tester", local_state.tester_session_id),
            ("reviewer", local_state.reviewer_session_id),
        )
        return [
            cls(role=role, session_id=sid, session_status=anthropic_client.get_session_status(sid))
            for role, sid in role_to_sid
            if sid
        ]


@dataclass(frozen=True)
class OrchestratorSnapshot:
    cycle: int
    cycle_cap: int
    cycles_remaining: int
    last_activity: str
    last_activity_age_seconds: int | None
    downstream_blocked: int

    @classmethod
    def build(
        cls,
        local_state: WorkUnitState,
        *,
        downstream_blocked: int,
        cycle_cap: int,
        now: datetime,
    ) -> OrchestratorSnapshot:
        return cls(
            cycle=local_state.review_round,
            cycle_cap=cycle_cap,
            cycles_remaining=max(0, cycle_cap - local_state.review_round),
            last_activity=local_state.last_activity,
            last_activity_age_seconds=_age_seconds(local_state.last_activity, now),
            downstream_blocked=downstream_blocked,
        )


@dataclass(frozen=True)
class HealthReport:
    """Aggregate of every signal :func:`probe_unit_health` collects.

    ``pr`` is ``None`` when the unit has no PR (pre-opening_pr state or
    a unit whose coder never opened one). Every other field has a safe
    empty value so :func:`decide_transitions` can branch without
    ``None`` guards.

    ``merge_commit_on_main`` is the post-merge reachability bit
    consumed by the ``merge_reverted_flag`` shadow rule. ``True`` is
    the safe default — we only flag genuine revert / history-rewrite
    cases, never false-flag a unit because the report didn't carry the
    bit.
    """

    unit_id: str
    pr: PRSnapshot | None
    git: GitSnapshot
    ci: CISnapshot
    reviews: ReviewSnapshot
    workers: list[WorkerSessionInfo]
    orchestrator: OrchestratorSnapshot
    merge_commit_on_main: bool = True


# --------------------------- decisions ---------------------------


class _FrozenDict(dict):
    """Read-only ``dict`` subclass — preserves ``isinstance(x, dict)`` for
    callers while blocking the mutation paths that would defeat
    :class:`Action` / :class:`ShadowDecision`'s snapshot contract.

    ``frozen=True`` on the surrounding dataclass only blocks field
    reassignment; without this wrapper the underlying dict is still
    mutable in place (``a.payload["x"] = "y"`` would silently corrupt
    the snapshot). Subclassing ``dict`` rather than wrapping in
    ``MappingProxyType`` keeps the test contract that
    ``trigger_inputs`` / ``payload`` is-a ``dict``.
    """

    __slots__ = ()

    def _readonly(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        raise TypeError("Action.payload / ShadowDecision.trigger_inputs are read-only")

    __setitem__ = _readonly
    __delitem__ = _readonly
    pop = _readonly
    popitem = _readonly
    clear = _readonly
    update = _readonly
    setdefault = _readonly


def _freeze_dict_field(instance: object, field_name: str) -> None:
    """Wrap ``instance.<field_name>`` in :class:`_FrozenDict` in-place.

    Shared by :meth:`Action.__post_init__` and
    :meth:`ShadowDecision.__post_init__` so both frozen dataclasses
    enforce the same snapshot-immutability contract through one
    helper. Idempotent — already-frozen dicts pass through untouched
    so ``dataclasses.replace`` (which re-runs ``__post_init__``)
    doesn't double-wrap.
    """
    value = getattr(instance, field_name)
    if not isinstance(value, _FrozenDict):
        object.__setattr__(instance, field_name, _FrozenDict(value))


@dataclass(frozen=True)
class Action:
    """One declarative thing the MCP executor should do.

    ``kind`` discriminates the payload:

    - ``"transition"`` — set ``target_status`` (and optionally
      ``clear_error``) on the unit.
    - ``"event"`` — append a row to ``unit_events`` with ``event_type``,
      ``summary``, ``details``. ``set_last_error`` populates the unit's
      ``last_error`` column without a status change (used by
      ``ci_drift_detected``).
    - ``"side_effect"`` — non-state-table operation (currently only
      ``write_cycle_log``). ``payload`` carries the call's structured
      args.

    Construct via the :meth:`transition` / :meth:`event` /
    :meth:`cycle_log_write` named constructors when building actions
    from inside this module — they encode the per-kind required-field
    sets in one place. Direct ``Action(kind=...)`` construction is kept
    available for tests that want to exercise the raw shape.

    The dataclass is frozen so tests can compare actions by value and
    snapshot decisions diff cleanly. ``payload`` is wrapped in
    :class:`_FrozenDict` post-init to make the immutability the
    docstring promises actually enforced — mutating
    ``action.payload["x"]`` raises rather than silently corrupting a
    snapshot.
    """

    kind: str
    target_status: str | None = None
    clear_error: bool = False
    event_type: str | None = None
    event_source: str = "orchestrator"
    summary: str = ""
    details: str = ""
    set_last_error: str = ""
    side_effect: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _freeze_dict_field(self, "payload")

    # ----- named constructors -----

    @classmethod
    def transition(
        cls,
        target_status: str,
        summary: str,
        *,
        clear_error: bool = False,
    ) -> Action:
        """Build a status-transition action (the executor sets
        ``status=target_status`` on apply)."""
        return cls(
            kind="transition",
            target_status=target_status,
            clear_error=clear_error,
            summary=summary,
        )

    @classmethod
    def event(
        cls,
        event_type: str,
        summary: str,
        *,
        source: str = "orchestrator",
        details: str = "",
        set_last_error: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> Action:
        """Build an event-emission action (appended to ``unit_events``).

        ``set_last_error`` populates the unit's ``last_error`` column
        without a status change — only the ``ci_drift_detected`` event
        uses it today.
        """
        return cls(
            kind="event",
            event_type=event_type,
            event_source=source,
            summary=summary,
            details=details,
            set_last_error=set_last_error,
            payload=payload or {},
        )

    @classmethod
    def cycle_log_write(cls, unit_id: str, merge_commit_sha: str) -> Action:
        """Build the ``write_cycle_log`` side-effect action.

        Payload matches the call shape ``ops.reconcile_unit_pr`` uses
        at ``ops.py:246-251`` (``unit_id`` + ``merge_commit_sha`` +
        backfill commit message) so a downstream executor can splat
        the payload directly into :func:`cycle_log.write_cycle_log`.
        """
        return cls(
            kind="side_effect",
            side_effect="write_cycle_log",
            summary=f"finalize cycle log for {unit_id}",
            payload={
                "unit_id": unit_id,
                "merge_commit_sha": merge_commit_sha,
                "commit_message": f"cycle-log: backfill merge SHA for {unit_id}",
            },
        )


@dataclass(frozen=True)
class ShadowDecision:
    """A rule the decision table evaluated but did not promote to live.

    Surfaced by the MCP tool alongside ``actions_to_apply`` so the lead
    can sanity-check the predicate against reality. Promotion of a
    shadow rule to live is a separate (deliberate) commit; this carrier
    only documents the predicate.
    """

    rule_name: str
    predicted_action: Action
    trigger_inputs: Mapping[str, Any]
    rationale: str

    def __post_init__(self) -> None:
        _freeze_dict_field(self, "trigger_inputs")


@dataclass(frozen=True)
class Decision:
    actions_to_apply: list[Action]
    shadow_decisions: list[ShadowDecision]


# --------------------------- probe ---------------------------


def probe_unit_health(
    unit_id: str,
    gh_client: GitHubHealthClient,
    anthropic_client: AnthropicHealthClient,
    *,
    local_state: WorkUnitState,
    downstream_blocked: int = 0,
    cycle_cap: int = DEFAULT_CYCLE_CAP,
    now: datetime | None = None,
) -> HealthReport:
    """Assemble a :class:`HealthReport` for ``unit_id``.

    Pure with respect to side effects: every external call routes
    through ``gh_client`` / ``anthropic_client``; every local fact (the
    unit's ``WorkUnitState`` row, downstream-blocked count, cycle cap,
    wall-clock for age calculations) is passed in.

    Args:
        unit_id: Identifier used by both clients to look up per-unit
            context (PR number, repo URL, session ids).
        gh_client: Source of GitHub PR / CI / review data.
        anthropic_client: Source of worker-session status.
        local_state: The unit's persisted state row. Read-only here.
        downstream_blocked: Count of units depending on ``unit_id``
            whose dep chain is blocked because this unit isn't ``done``.
            Caller computes from the dep graph.
        cycle_cap: Cycle ceiling for cap-proximity reporting. Defaults
            to :data:`DEFAULT_CYCLE_CAP`.
        now: Wall-clock anchor for age calculations. Defaults to
            ``datetime.now(UTC)`` — tests pass a fixed value for
            reproducible expectations.

    Returns:
        A :class:`HealthReport` snapshot. Never mutates either client.
    """
    when = now or datetime.now(UTC)

    pr = PRSnapshot.from_dict(gh_client.get_pr(unit_id))
    head_commit = gh_client.get_head_commit(unit_id)
    compare = gh_client.get_compare_to_base(unit_id)
    git = GitSnapshot(
        ahead_by=compare.get("ahead_by"),
        behind_by=compare.get("behind_by"),
        head_sha=(pr.head_sha if pr else head_commit.get("sha")),
        head_age_seconds=_age_seconds(head_commit.get("committed_at"), when),
        last_force_push_at=gh_client.get_last_force_push_at(unit_id),
    )
    ci = CISnapshot.from_lists(
        gh_client.get_check_runs(unit_id), gh_client.get_required_checks(unit_id)
    )
    reviews = ReviewSnapshot.from_lists(
        gh_client.get_reviews(unit_id),
        gh_client.get_review_threads(unit_id),
        gh_client.get_requested_reviewers(unit_id),
        gh_client.get_copilot_review(unit_id),
    )
    workers = WorkerSessionInfo.collect(local_state, anthropic_client)
    orch = OrchestratorSnapshot.build(
        local_state, downstream_blocked=downstream_blocked, cycle_cap=cycle_cap, now=when
    )

    # Only ask the client when the PR has actually merged — pre-merge
    # reachability is meaningless (and the production client may not
    # accept the call). Default of ``True`` matches HealthReport's
    # "safe default" semantics.
    merge_on_main = True
    if pr is not None and pr.merged and pr.merge_commit_sha:
        merge_on_main = bool(gh_client.is_merge_commit_on_main(unit_id))

    return HealthReport(
        unit_id=unit_id,
        pr=pr,
        git=git,
        ci=ci,
        reviews=reviews,
        workers=workers,
        orchestrator=orch,
        merge_commit_on_main=merge_on_main,
    )


# --------------------------- decide ---------------------------


def _merged_pr(report: HealthReport) -> bool:
    return report.pr is not None and report.pr.merged


def _ci_is_green(ci: CISnapshot) -> bool:
    """All known runs report a passing conclusion and no run is pending.

    A repo with zero check_runs is treated as green — matches
    ``ci_wait``'s "no-CI repos pass through" gate. Pending runs hold
    the gate closed (CI hasn't settled yet). Passing conclusions are
    the canonical ``PASSING_CONCLUSIONS`` set (``success`` /
    ``skipped`` / ``neutral``) so this module's drift / shadow rules
    stay aligned with ``ci_wait``'s wait-loop semantics.
    """
    if ci.pending:
        return False
    return all(r.conclusion in PASSING_CONCLUSIONS for r in ci.runs)


def _ci_has_failure(ci: CISnapshot) -> bool:
    return bool(ci.failing)


def _merged_summary(report: HealthReport) -> str:
    pr = report.pr
    if pr is None:
        return ""
    return f"PR merged at {pr.merged_at}" if pr.merged_at else "PR merged"


def _merge_transitions(local_state: WorkUnitState, report: HealthReport) -> list[Action]:
    """Build actions for the reconcile cells from ``reconcile_unit_pr``.

    Mirrors that function's status → action table so the new
    health-probe path stays behavior-identical with the existing
    state-advancing call. Cells covered:

    * ``merged + in_ci / approved_awaiting_merge`` → ``done`` + ``merged``
    * ``merged + escalated`` → ``done`` (clears ``last_error``) +
      ``merged`` + ``recovered_from_escalated`` (preserves the prior
      ``last_error`` as audit detail)
    * ``merged + active-role`` → ``reconcile_refused`` event only
    * Any of the above with a populated ``merge_commit_sha`` (and
      ``status`` in :data:`_LOG_WRITABLE_STATUSES`) also gets the
      ``write_cycle_log`` side effect appended.
    """
    if not _merged_pr(report):
        return []
    assert report.pr is not None  # nosec B101 — guarded by _merged_pr
    status = local_state.status
    unit_id = local_state.unit_id
    merged_event_summary = _merged_summary(report)
    actions: list[Action] = []

    if status in _MERGED_TRANSITION_STATUSES:
        is_escalated = status == "escalated"
        transition_summary = (
            f"{unit_id} merged after escalation"
            if is_escalated
            else f"{unit_id} merged from {status}"
        )
        actions.append(Action.transition("done", transition_summary, clear_error=is_escalated))
        actions.append(Action.event("merged", merged_event_summary, source="human"))
        if is_escalated:
            actions.append(
                Action.event(
                    "recovered_from_escalated",
                    "merged after escalation; last_error cleared",
                    source="human",
                    details=local_state.last_error or "",
                )
            )
    elif status in _REFUSE_MERGE_FROM_STATUSES:
        # Active-role status observing a merged PR — refuse to advance.
        actions.append(
            Action.event(
                "reconcile_refused",
                f"refusing to advance unit in active status {status!r} to done",
                source="human",
            )
        )

    # Cycle-log writer side effect (same cells ops.reconcile_unit_pr
    # writes from). Skipped when SHA isn't there yet — a later poll
    # catches up via the no-op-already-done idempotent re-render.
    if status in _LOG_WRITABLE_STATUSES and report.pr.merge_commit_sha:
        actions.append(Action.cycle_log_write(unit_id, report.pr.merge_commit_sha))

    return actions


def _conflict_event(local_state: WorkUnitState, report: HealthReport) -> Action | None:  # noqa: ARG001
    pr = report.pr
    if pr is None or pr.merged:
        return None
    if pr.mergeable_state not in _CONFLICTING_MERGEABLE_STATES:
        return None
    files = pr.conflict_files
    details = (
        f"conflict files: {', '.join(files)}" if files else f"mergeable_state={pr.mergeable_state}"
    )
    return Action.event(
        "pr_conflict_detected",
        f"PR conflict ({pr.mergeable_state})",
        details=details,
        payload={"conflict_files": list(files), "mergeable_state": pr.mergeable_state},
    )


def _required_check_missing_event(
    local_state: WorkUnitState,
    report: HealthReport,  # noqa: ARG001
) -> Action | None:
    if not report.ci.missing_required:
        return None
    missing = report.ci.missing_required
    return Action.event(
        "required_check_missing",
        f"{len(missing)} required check(s) missing from PR",
        details=f"missing: {', '.join(missing)}",
        payload={"missing": list(missing), "required": list(report.ci.required)},
    )


def _ci_drift_event(local_state: WorkUnitState, report: HealthReport) -> Action | None:
    """Status-vs-CI drift: status implies happy/done but CI reports failure.

    Quiet when status is one of the active-fix statuses (coding /
    fixing / testing) — a red run on those is the *reason* the agent is
    running, not drift. Quiet on terminal statuses too (done /
    escalated already capture the resolution).
    """
    if not _ci_has_failure(report.ci):
        return None
    if local_state.status in _STATUSES_WHERE_RED_CI_IS_EXPECTED:
        return None
    if local_state.status in TERMINAL_UNIT_STATUSES:
        return None
    failing = report.ci.failing
    return Action.event(
        "ci_drift_detected",
        f"CI red while status={local_state.status!r}",
        details=f"failing checks: {', '.join(failing)}",
        set_last_error=f"CI drift: {', '.join(failing)} failing",
        payload={"failing": list(failing), "status": local_state.status},
    )


# --------------------------- shadow rules ---------------------------


def _shadow_escalated_to_in_ci_reset(
    local_state: WorkUnitState, report: HealthReport
) -> ShadowDecision | None:
    if local_state.status != "escalated":
        return None
    if not _ci_is_green(report.ci):
        return None
    if report.reviews.approvals < 1:
        return None
    if report.reviews.unresolved_threads > 0:
        return None
    if report.reviews.changes_requested > 0:
        return None
    return ShadowDecision(
        rule_name="escalated_to_in_ci_reset",
        predicted_action=Action.transition(
            "in_ci",
            f"reset {local_state.unit_id} from escalated → in_ci "
            "(CI green, approved, no open threads)",
            clear_error=True,
        ),
        trigger_inputs={
            "status": local_state.status,
            "ci_green": True,
            "approvals": report.reviews.approvals,
            "unresolved_threads": report.reviews.unresolved_threads,
            "changes_requested": report.reviews.changes_requested,
        },
        rationale=(
            "Unit was escalated but the world has since healed — CI is green, GH has "
            "≥1 approval, no open review threads, no outstanding changes-requested. "
            "Auto-reset to in_ci would let the cycle finish without manual intervention. "
            "Shadow-only until we've seen this fire on real units without false positives."
        ),
    )


def _shadow_merge_reverted_flag(
    local_state: WorkUnitState, report: HealthReport
) -> ShadowDecision | None:
    # Anchored to ``status=done`` and a populated ``merge_commit_sha`` so we
    # only flag genuine post-merge anomalies. The reachability bit is on the
    # report directly so this rule stays a pure function.
    if local_state.status != "done":
        return None
    pr = report.pr
    if pr is None or not pr.merge_commit_sha:
        return None
    if report.merge_commit_on_main:
        return None
    return ShadowDecision(
        rule_name="merge_reverted_flag",
        predicted_action=Action.event(
            "merge_reverted",
            f"{local_state.unit_id}'s merge commit no longer reachable from main",
            source="human",
            details=f"merge_commit_sha={pr.merge_commit_sha}",
        ),
        trigger_inputs={
            "status": "done",
            "merge_commit_sha": pr.merge_commit_sha,
            "merge_commit_on_main": False,
        },
        rationale=(
            "Unit is done but its merge commit isn't on main any more — either a "
            "force-push rewrote main or the merge was reverted. Worth flagging because "
            "downstream units that depended on this one may now have a stale ancestry. "
            "Shadow-only until we've decided whether the right reaction is "
            "auto-reopening the unit, emitting an escalation, or just an audit event."
        ),
    )


def _shadow_dead_worker_during_active_status(
    local_state: WorkUnitState, report: HealthReport
) -> ShadowDecision | None:
    if local_state.status not in ACTIVE_UNIT_STATUSES:
        return None
    terminated = [w for w in report.workers if w.session_status == "terminated"]
    if not terminated:
        return None
    terminated_roles = [w.role for w in terminated]
    return ShadowDecision(
        rule_name="dead_worker_during_active_status",
        predicted_action=Action.event(
            "dead_worker_detected",
            f"worker session for {terminated_roles} is terminated "
            f"while unit status={local_state.status!r}",
            details=", ".join(f"{w.role}={w.session_id}" for w in terminated),
        ),
        trigger_inputs={
            "status": local_state.status,
            "terminated_roles": terminated_roles,
        },
        rationale=(
            "An active-role unit (coding/testing/opening_pr/in_ci/reviewing/fixing) "
            "is paired with a terminated worker session — the agent died on Anthropic's "
            "side but the orchestrator hasn't observed it yet. Restart-recovery flow "
            "(resume_unit + tail_worker) already triages this manually; the shadow rule "
            "would automate the escalation. Held in shadow until we've confirmed the "
            "false-positive rate (terminated-but-finished sessions) is low enough to act on."
        ),
    )


# Event-only signal builders. Each returns an :class:`Action` to append
# to ``actions_to_apply`` or ``None`` to skip. Adding a new event-only
# signal is one line here; the dispatch in :func:`decide_transitions`
# stays untouched. Builders share a uniform ``(local_state, report)``
# signature even when they don't read ``local_state`` so this list can
# be a flat tuple of callables.
_EVENT_BUILDERS: tuple[Callable[[WorkUnitState, HealthReport], Action | None], ...] = (
    _conflict_event,
    _required_check_missing_event,
    _ci_drift_event,
)

# Shadow-rule builders. Same shape as ``_EVENT_BUILDERS`` but return
# :class:`ShadowDecision`. New shadow rules append here.
_SHADOW_BUILDERS: tuple[Callable[[WorkUnitState, HealthReport], ShadowDecision | None], ...] = (
    _shadow_escalated_to_in_ci_reset,
    _shadow_merge_reverted_flag,
    _shadow_dead_worker_during_active_status,
)


# --------------------------- entry point ---------------------------


def decide_transitions(local_state: WorkUnitState, report: HealthReport) -> Decision:
    """Pure decision table over ``(local_state, report)``.

    See :mod:`orchestrator.health` for the full contract. Briefly:

    * ``actions_to_apply`` covers the three existing reconcile cells
      (``merged + in_ci``, ``merged + approved_awaiting_merge``,
      ``merged + escalated``), the merge-into-active-role refusal,
      and three new event-only signals
      (``pr_conflict_detected``, ``required_check_missing``,
      ``ci_drift_detected``).

    * ``shadow_decisions`` covers ``escalated_to_in_ci_reset``,
      ``merge_reverted_flag``, and ``dead_worker_during_active_status``.

    The function never mutates either argument and never performs I/O.
    """
    actions: list[Action] = list(_merge_transitions(local_state, report))
    actions.extend(a for build in _EVENT_BUILDERS if (a := build(local_state, report)) is not None)
    shadows = [s for build in _SHADOW_BUILDERS if (s := build(local_state, report)) is not None]
    return Decision(actions_to_apply=actions, shadow_decisions=shadows)


__all__ = [
    "Action",
    "AnthropicHealthClient",
    "CISnapshot",
    "CheckRunInfo",
    "DEFAULT_CYCLE_CAP",
    "Decision",
    "GitHubHealthClient",
    "GitSnapshot",
    "HealthReport",
    "OrchestratorSnapshot",
    "PRSnapshot",
    "ReviewSnapshot",
    "ShadowDecision",
    "WorkerSessionInfo",
    "decide_transitions",
    "probe_unit_health",
]
