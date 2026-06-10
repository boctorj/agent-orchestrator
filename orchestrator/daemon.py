"""Level-triggered reconciliation daemon (F-016 Phase 3).

A separate process that tails active worker sessions, applies marker
grammar, and advances ``work_units`` state without the lead chat in the
loop. Built directly on F-014's pure :func:`~orchestrator.health.decide_transitions`
engine — same transition table the blocking ``cycle_review_blocking``
will call in-process (F-016-U-6). No parallel state machine.

Mental model — the Kubernetes-controller pattern:

  * **Stateless between ticks.** Restart it, kill it, run it concurrently
    with a stale instance — the worst case is duplicate work that gets
    deduped by Phase 0's ``unit_events.dedupe_key`` UNIQUE constraint
    and the ``work_units.owner`` CAS column. There is no
    ``last_observed_event_id`` cursor to drift out of sync with reality.
  * **Level-triggered.** Each tick re-derives the correct next action
    from ``(unit_state, observed markers, PR snapshot)``. A partially-
    applied transition from a crashed previous tick is finished on the
    next tick — no special recovery path.
  * **Idempotent transitions.** Every ``Apply*`` action checks its
    pre-condition before mutating. Applying ``ApplyTesterMarker(TESTS_PASS)``
    when the unit is already ``reviewing`` is a no-op.

Each tick the daemon walks every row :func:`~orchestrator.state.list_active_units`
returns, and for each:

  1. **Observe.** Scans the coder / tester / reviewer worker sessions
     via ``worker.tail_messages`` + the pure
     :func:`orchestrator.markers.scan_response`. Newly-observed markers
     are written to ``unit_events`` with a stable ``dedupe_key`` so the
     same response is recorded exactly once across the lifetime of the
     unit (Phase 0).
  2. **Decide & apply (marker-driven).** When a marker carries a
     ``target_status`` and the unit is in an active flippable status, a
     CAS-guarded transition lands the new status (and ``last_error`` for
     BLOCKED).
  3. **Decide & apply (F-014).** When the unit has a PR number, the
     F-014 :func:`~orchestrator.health.probe_unit_health` /
     :func:`~orchestrator.health.decide_transitions` engine runs against
     the live PR snapshot and applies the resulting ``actions_to_apply``
     via the same :func:`orchestrator.tools.health._apply_action`
     ``inspect_unit_health`` calls. Reuses the canonical executor — one
     applier, two callers.

Pre-conditions checked before any state mutation, per tick AND per
action:

  * ``unit.cancelled_at`` set → skip; the user has stopped this unit.
  * ``unit.status`` already terminal (``done`` / ``escalated``) → skip.
  * :func:`~orchestrator.state.has_active_advance_lock` → skip; a lead
    is mid-``send_to_unit_async`` and the daemon must not race the
    ~1s submit window. The next tick (after the lead's lock releases)
    re-derives the correct action from fresh state.

Singleton enforcement (spec § "Singleton enforcement — SQLite-backed,
not pidfile"): the workspace-wide ``daemon_locks`` row keyed by the
absolute ``state.db`` path. ``ORCH_DAEMON_DRIVE`` is the opt-in gate —
without it set to ``true``, :func:`run_daemon` is a no-op so an
accidental ``python -m orchestrator.daemon`` doesn't quietly take over
a workspace that hasn't migrated to Phase 3.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import signal
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from orchestrator import markers, stale_marker, state
from orchestrator.health import Action, Decision, HealthReport, ShadowDecision
from orchestrator.markers import MarkerSpec
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    READY_TO_MERGE_STATUSES,
    TERMINAL_UNIT_STATUSES,
    WorkUnitState,
)
from orchestrator.tools import CAP_3, compose_fix_task, compose_reviewer_delta_task
from orchestrator.workers import make_worker
from orchestrator.workers.base import Worker

logger = logging.getLogger(__name__)


# --------------------------- knobs ---------------------------

DAEMON_DRIVE_ENV = "ORCH_DAEMON_DRIVE"
"""Opt-in gate for the daemon loop. ``true`` / ``1`` / ``yes`` / ``on``
(case-insensitive) lets :func:`run_daemon` actually take the lock; any
other value (including absence) makes it a no-op."""

POLL_INTERVAL_ENV = "ORCH_DAEMON_POLL_INTERVAL_S"
POLL_INTERVAL_DEFAULT_S = 5.0
"""Default reconcile cadence (seconds). Matches the spec's "constant 5s
is the simplest start"; tunable per workspace via the env var."""

DAEMON_OWNER = "daemon"
"""``work_units.owner`` value while a tick applies a transition. Mirrors
:data:`orchestrator.state.LEAD_OWNER` ("lead") — the two are the only
legal non-empty values; the CAS in :func:`~orchestrator.state.claim_unit_owner`
keeps them from clobbering each other."""

ROLES: tuple[str, ...] = ("coder", "tester", "reviewer")
"""Roles whose worker sessions the daemon scans each tick. Pinned here
rather than reading from ``WorkUnitState`` so adding a future role
forces an explicit edit (the marker grammar in :mod:`orchestrator.markers`
is per-role and must grow with it)."""


# --------------------------- exit code sentinels ---------------------------
#
# ``DaemonLoop.run`` / ``run_daemon`` return ``int`` tick counts on the
# happy paths; the two no-op paths return distinct sentinels so the CLI
# (and a systemd ``ExecStart`` / shell script) can branch on intent via
# ``$?`` rather than collapsing every outcome to exit code 0.
#
# Sentinels are negative so ``ticks >= 0`` still discriminates "ran the
# loop" from "noped out" without per-callsite branching. The CLI surface
# maps them: drive-disabled → exit 2 (config / nudge), lock-held →
# exit 3 (another daemon owns the workspace).

EXIT_DRIVE_DISABLED = -2
"""Returned by :func:`DaemonLoop.run` when ``ORCH_DAEMON_DRIVE`` is unset
or falsy. CLI maps to exit-2 so a systemd ``ExecStart`` retry policy can
distinguish "operator forgot the env var" from "lock contention"."""

EXIT_LOCK_HELD = -3
"""Returned by :func:`DaemonLoop.run` when :func:`claim_singleton`
returns ``None`` — another daemon owns the workspace. CLI maps to
exit-3 so an operator running ``orchestrator daemon start`` from a
second terminal sees the failure code without parsing logs."""


# --------------------------- per-role worker cache ---------------------------
#
# ``make_worker`` constructs a fresh :class:`ManagedAgentWorker` (and
# Anthropic SDK client) per call. At the 5s default tick with N active
# units, the unmemoized call site was 3·N client constructions per tick
# (one per role per unit). The SDK client is lightweight today, but a
# future release that opens an ``httpx`` keepalive pool on construction
# would turn this into per-tick connection churn.
#
# Memoize per role for the daemon process's lifetime. The cache is keyed
# on role name and protected by a lock so two concurrent test ticks
# can't double-construct. The same pattern lives in
# ``tools.health._ProductionAnthropicClient._worker``; sharing the
# pattern keeps the engine's two surfaces drift-free.

_WORKER_CACHE: dict[str, Worker] = {}
_WORKER_CACHE_LOCK = threading.Lock()


def _cached_worker(role: str) -> Worker:
    """Return a process-wide :class:`~orchestrator.workers.base.Worker` for ``role``.

    Lazy-constructed and cached for the daemon process's lifetime.
    Tests that want a fresh worker per call (e.g. to spy on
    construction) call :func:`reset_worker_cache` first.
    """
    with _WORKER_CACHE_LOCK:
        worker = _WORKER_CACHE.get(role)
        if worker is None:
            worker = make_worker(role)
            _WORKER_CACHE[role] = worker
        return worker


def reset_worker_cache() -> None:
    """Drop every cached worker. Test-only — production never re-roles.

    Useful in unit tests that patch ``make_worker`` between calls; the
    cache would otherwise return the previous patch's worker after the
    monkeypatch restores.
    """
    with _WORKER_CACHE_LOCK:
        _WORKER_CACHE.clear()


# Per-marker source-status table — the central guard against the
# stale-marker backflip class of regression. Each ``(role, marker)``
# entry lists the ``WorkUnitState.status`` values the marker is
# legitimately *fired from*. A marker observed on a tail whose unit is
# already past those statuses is silently no-op'd: role sessions are
# never archived between phases on the success path, so the coder's
# ``PR_URL`` and the tester's ``TESTS_PASS`` stay in their tails for the
# unit's whole lifetime — a daemon that backflipped ``reviewing → in_ci``
# on each re-scan of those persistent markers would break the spec's
# "Idempotent transitions are load-bearing" acceptance.
#
# Statuses (matching :data:`~orchestrator.models.ACTIVE_UNIT_STATUSES`):
#   * ``coding`` / ``opening_pr`` — coder's PR-opening window.
#   * ``fixing`` — coder's address-review / address-CI window.
#   * ``in_ci`` — waiting on CI / between phases.
#   * ``testing`` — tester is running.
#   * ``reviewing`` — reviewer is reading.
#
# Rationale per entry:
#   * ``PR_URL`` — only fires from a coder mid-spawn (``coding`` /
#     ``opening_pr``). A coder mid-fix that emits PR_URL is a worker
#     bug, not a state transition.
#   * ``FIX_PUSHED`` — primarily fires from ``fixing``; ``coding`` /
#     ``opening_pr`` covered defensively for the edge case where a
#     spawn-time coder pushes a "fix" before opening the PR.
#   * ``TESTS_PASS`` — only ``testing``; the tester session exits idle
#     at that point and the daemon's re-tick on the same persistent
#     marker must not bounce a downstream unit.
#   * ``REVIEW_RECOMMEND_MERGE`` / ``REVIEW_COMMENT`` — only
#     ``reviewing``; applying a stale endorsement to a unit that's now
#     in CI re-run would land ``approved_awaiting_merge`` without the
#     reviewer ever seeing the new SHA.
#   * ``BLOCKED`` — universal across roles; can escalate from any
#     active status, so the source set is the full
#     ``ACTIVE_UNIT_STATUSES``.
#
# Load-bearing caller invariant (PR #61 reviewer M1):
#
#   The source-status fence above CORRECTLY blocks the
#   "downstream-status re-scan" case (cycle 0's TESTS_PASS observed
#   while the unit is now ``reviewing`` — ``reviewing`` ∉ {testing}
#   → no-op).
#
#   It does NOT block the "cross-cycle re-entry with the same session
#   id" case on its own: a unit that has progressed past ``testing``
#   and then returns to ``testing`` in a later cycle with the **same**
#   ``tester_session_id`` would re-fire the original ``TESTS_PASS`` on
#   the next daemon scan (the source status is satisfied, the marker
#   payload is still on the tail).
#
#   That gap WAS historically unreachable because the lead's re-entry
#   helpers (``orchestrator.tools.execution._tester_phase`` and
#   friends) cleared the role's ``<role>_session_id`` before re-
#   spawning. **F-018 Stage 4 broke that invariant** — the daemon-
#   driven autonomous loop closure re-enters ``reviewing`` while
#   reusing the existing ``reviewer_session_id`` (to preserve the
#   F-013 delta-review context the session already has loaded).
#   Without further protection, the persistent OLD
#   ``REVIEW_RECOMMEND_MERGE`` on the reviewer's tail would re-fire
#   ``reviewing → approved_awaiting_merge`` before the reviewer's
#   actual delta-review verdict on the new SHA lands.
#
#   **Closed via dedupe-gate in ``reconcile_unit`` Stage 1 (PR #66 N1):**
#   ``_apply_marker_transition`` is now only invoked when
#   ``_record_marker`` returned True (i.e., the
#   ``unit_events.dedupe_key`` UNIQUE INSERT landed — a fresh marker).
#   A persistent OLD marker re-observed on a later tick collapses to
#   ``INSERT OR IGNORE → False``, the gate skips
#   ``_apply_marker_transition``, and the transition does not re-fire.
#   This is the "proposed ``unit_events`` dedupe-key lookup" the
#   original docstring named as the natural follow-up, now landed.
#
#   **Caller contract (still recommended even with the dedupe-gate):**
#   when the orchestrator re-enters an upstream active status in a
#   new cycle (e.g. ``reviewing → fixing → testing`` on a tester-
#   found bug) AND the marker payload genuinely needs to change
#   between cycles, the lead's helpers SHOULD still clear
#   ``<role>_session_id`` before the role's worker is re-spawned —
#   the dedupe-gate guards against re-applying a stale marker, but a
#   fresh session with no prior marker in its tail is the cleaner
#   surface for downstream debugging. The
#   ``tests/test_f016_u5_tester_extras.py::TestStaleMarkerNoBackflip``
#   suite covers the in-scope downstream-status case;
#   ``tests/test_daemon.py::TestStage1DedupeGate`` covers the
#   cross-cycle re-entry case the dedupe-gate now blocks.
_MARKER_SOURCE_STATUSES: dict[tuple[str, str], frozenset[str]] = {
    ("coder", "PR_URL"): frozenset({"coding", "opening_pr"}),
    ("coder", "FIX_PUSHED"): frozenset({"coding", "opening_pr", "fixing"}),
    ("tester", "TESTS_PASS"): frozenset({"testing"}),
    ("reviewer", "REVIEW_RECOMMEND_MERGE"): frozenset({"reviewing"}),
    ("reviewer", "REVIEW_COMMENT"): frozenset({"reviewing"}),
    ("coder", "BLOCKED"): ACTIVE_UNIT_STATUSES,
    ("tester", "BLOCKED"): ACTIVE_UNIT_STATUSES,
    ("reviewer", "BLOCKED"): ACTIVE_UNIT_STATUSES,
}


# --------------------------- env helpers ---------------------------


def is_drive_enabled() -> bool:
    """``ORCH_DAEMON_DRIVE`` parsed as a coarse boolean.

    Truthy: ``true`` / ``1`` / ``yes`` / ``on`` (case-insensitive).
    Everything else (including absence and the empty string) is False.
    """
    return os.getenv(DAEMON_DRIVE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _poll_interval_s() -> float:
    """Resolved poll interval from ``ORCH_DAEMON_POLL_INTERVAL_S`` or default.

    Floor of 0.1s so a misconfigured zero doesn't busy-spin SQLite.

    Defensive rejection of non-finite values (``nan`` / ``inf`` /
    ``-inf``) — these are valid ``float`` parses but propagating them
    into :meth:`threading.Event.wait` would either hang the loop
    forever (``inf``) or wake immediately on every iteration in a CPU
    spin (``nan`` returns False from ``wait`` instantly on CPython). An
    operator who set ``ORCH_DAEMON_POLL_INTERVAL_S=inf`` to "disable
    polling" must see the daemon fall back to the safe default rather
    than freeze the workspace.
    """
    raw = os.getenv(POLL_INTERVAL_ENV, "").strip()
    if not raw:
        return POLL_INTERVAL_DEFAULT_S
    try:
        parsed = float(raw)
    except ValueError:
        return POLL_INTERVAL_DEFAULT_S
    if not math.isfinite(parsed):
        logger.warning(
            "daemon: %s=%r is not finite; falling back to %.1fs",
            POLL_INTERVAL_ENV,
            raw,
            POLL_INTERVAL_DEFAULT_S,
        )
        return POLL_INTERVAL_DEFAULT_S
    return max(0.1, parsed)


# --------------------------- per-unit helpers ---------------------------


def _role_session_id(unit: WorkUnitState, role: str) -> str:
    """``unit.<role>_session_id`` lookup for the three known roles.

    Returns ``""`` for any role whose session has not been spawned —
    the scan path treats empty as "skip this role" rather than an
    error.
    """
    return {
        "coder": unit.coder_session_id,
        "tester": unit.tester_session_id,
        "reviewer": unit.reviewer_session_id,
    }[role]


def _scan_role(unit: WorkUnitState, role: str) -> tuple[MarkerSpec | None, str]:
    """Tail the role's session and parse for a terminal marker.

    Returns ``(spec_or_None, session_id)``. Catches every backend
    exception — a transient tail failure on one role must not crash
    the whole tick; the next poll re-attempts.

    The pure :func:`orchestrator.markers.scan_response` is the same
    parser the blocking ``_record_terminal_marker`` uses, so the daemon
    and lead see the same marker on the same response — Phase 0's
    determinism is what makes the dedupe key stable across both
    callers.

    Workers are pulled from :func:`_cached_worker` so each role's
    :class:`ManagedAgentWorker` (and its Anthropic SDK client) is
    constructed once per process, not per ``(unit, role, tick)``.
    """
    sid = _role_session_id(unit, role)
    if not sid:
        return None, ""
    try:
        worker = _cached_worker(role)
        tail_result = worker.tail_messages(sid, limit=50)
    except Exception:  # noqa: BLE001 — defensive; observability matters more than a crash
        logger.exception("daemon: tail_messages failed for %s/%s", unit.unit_id, role)
        return None, sid
    text = "\n".join(m["text"] for m in tail_result["messages"])
    return markers.scan_response(role, text), sid


def _record_marker(unit: WorkUnitState, spec: MarkerSpec, session_id: str) -> bool:
    """Append the parsed marker to ``unit_events`` with a Phase 0 dedupe key.

    Returns ``True`` iff a new row was inserted. ``False`` (no-op) when
    the same ``(session_id, cycle_number, event_type, marker_payload)``
    has been recorded before — the UNIQUE index on ``dedupe_key`` makes
    re-scans free.

    Source is set to the role so a downstream audit can tell daemon-
    observed markers apart from the lead-recorded ones written by
    ``_record_terminal_marker`` (lead path keeps source="orchestrator");
    both share the dedupe space.
    """
    key = markers.dedupe_key(
        session_id=session_id,
        cycle_number=unit.review_round,
        event_type=spec.event_type,
        marker_payload=spec.payload,
    )
    return state.record_event(
        unit.unit_id,
        unit.feature_id,
        spec.event_type,
        source=spec.role,
        cycle_number=unit.review_round,
        summary=spec.summary,
        details=spec.details,
        session_id=session_id,
        dedupe_key=key,
    )


def _apply_marker_transition(unit_id: str, spec: MarkerSpec) -> bool:
    """Flip ``work_units.status`` per ``spec.target_status`` under an ``owner`` CAS.

    Returns ``True`` when the flip landed. The short-circuits — in
    decreasing locality — together fence every race:

      * ``spec.target_status`` is ``None`` → marker has no status flip
        (BUG_FOUND / REVIEW_REQUEST_CHANGES leave status to the
        fix-loop). Skip BEFORE the CAS so a non-flipping marker doesn't
        briefly clobber a lead's pending advance-lock attempt.
      * :func:`~orchestrator.state.claim_unit_owner` returns False →
        lead holds the advance lock (``owner='lead'``) OR another daemon
        instance is mid-tick on the same unit. Skip; next poll retries.
      * Re-read inside the CAS: ``cancelled_at`` set → user cancelled
        between CAS-claim and the body. Skip and release.
      * **Per-marker source-status check** (the stale-marker backflip
        fence): the marker is only applied when ``latest.status`` is in
        the marker's :data:`_MARKER_SOURCE_STATUSES` entry. Role
        sessions are never archived on the success path, so the coder's
        ``PR_URL`` and the tester's ``TESTS_PASS`` stay in their tails
        for the unit's whole lifetime; a daemon re-scan that re-fired
        those persistent markers as ``reviewing → in_ci`` flips would
        break the spec's "Idempotent transitions are load-bearing"
        acceptance.
      * BLOCKED markers populate ``last_error`` alongside the status
        flip in one :func:`~orchestrator.state.touch_unit` call so the
        ``(status='escalated', last_error=…)`` pair lands atomically.
    """
    if spec.target_status is None:
        return False
    if not state.claim_unit_owner(unit_id, DAEMON_OWNER, expected_owner=""):
        return False
    try:
        latest = state.get_unit_state(unit_id)
        if latest is None:
            return False
        if latest.cancelled_at is not None:
            return False
        # Per-marker source-status fence — the stale-marker backflip
        # guard. A persistent ``TESTS_PASS`` on the tester's tail must
        # NOT bounce a unit that has moved on to ``reviewing`` back to
        # ``in_ci``; the source-status set restricts each marker to the
        # state it's legitimately emitted from. Unknown ``(role, marker)``
        # pairs (a future grammar addition the daemon hasn't been
        # taught about yet) default to *no* sources — a conservative
        # "refuse unfamiliar markers" so a daemon ahead of the grammar
        # can't silently drive transitions whose semantics weren't
        # reviewed.
        allowed_sources = _MARKER_SOURCE_STATUSES.get((spec.role, spec.marker), frozenset())
        if latest.status not in allowed_sources:
            return False
        if spec.last_error:
            state.touch_unit(unit_id, status=spec.target_status, error=spec.last_error)
        else:
            state.touch_unit(unit_id, status=spec.target_status)
        return True
    finally:
        state.release_unit_owner(unit_id, expected_owner=DAEMON_OWNER)


def _apply_health_action(unit: WorkUnitState, action: Action) -> bool:
    """Execute one F-014 :class:`~orchestrator.health.Action` under daemon CAS.

    Routes the action through :func:`orchestrator.tools.health._apply_action`
    so the on-disk effect matches what ``inspect_unit_health`` produces
    bit-for-bit. The CAS prevents lead/daemon double-write; if a lead
    is mid-``send_to_unit_async`` (``owner='lead'``) the daemon defers
    and re-derives next tick.

    Imported lazily because ``orchestrator.tools.health`` registers
    ``@mcp.tool()`` decorators on import — keeping that side-effect out
    of the module-load path keeps ``import orchestrator.daemon`` free
    of MCP entanglement until execution time.
    """
    from orchestrator.tools.health import _apply_action  # noqa: PLC0415 — see docstring

    if not state.claim_unit_owner(unit.unit_id, DAEMON_OWNER, expected_owner=""):
        return False
    try:
        latest = state.get_unit_state(unit.unit_id) or unit
        if latest.cancelled_at is not None:
            return False
        _apply_action(latest, action)
        return True
    finally:
        state.release_unit_owner(unit.unit_id, expected_owner=DAEMON_OWNER)


def _probe_and_decide_unit(unit: WorkUnitState) -> list[Action]:
    """Run F-014 probe + decide for ``unit``. Returns ``actions_to_apply``.

    Empty list on any non-success (no PR yet, missing feature row, GH
    fetch error). The two F-014 side effects that always accompany the
    ``actions_to_apply`` half — :func:`_persist_shadow_decisions` and
    :func:`_maybe_persist_snapshot` — fire here so the daemon's tick
    produces the same audit footprint
    :func:`~orchestrator.tools.health.inspect_unit_health` does on the
    same unit (spec § "F-015 absorption clause": F-014's shadow-
    decision telemetry IS the validation harness for the daemon's
    rollout; dropping it would silently shrink the operator-visible
    surface).

    Returning just the actions list keeps the public call-site stable
    for the existing test surface; callers that want the
    ``(report, decision)`` pair instead consume
    :func:`_probe_unit_health` directly.

    Lazy import of ``orchestrator.tools.health`` for the same MCP-
    decorator reason as :func:`_apply_health_action`.
    """
    probe = _probe_unit_health(unit)
    if probe is None:
        return []
    report, decision = probe
    _persist_shadow_decisions(unit, decision.shadow_decisions)
    _maybe_persist_snapshot(unit, report)
    return list(decision.actions_to_apply)


def _probe_unit_health(unit: WorkUnitState) -> tuple[HealthReport, Decision] | None:
    """Run F-014 probe + decide for ``unit``. ``None`` on any non-success.

    Pure observer — no state writes, no side-effects beyond the
    GitHub / Anthropic round-trips
    :func:`~orchestrator.tools.health._probe_and_decide` does. The
    caller decides what to do with the report + decision pair;
    :func:`_probe_and_decide_unit` wraps this with the F-014 side-
    effect persistence the daemon's tick path uses, while a future
    debugging tool / dry-run path can call this directly.
    """
    if not unit.pr_number:
        return None
    from orchestrator.tools.health import _probe_and_decide  # noqa: PLC0415

    feature = state.get_feature(unit.feature_id)
    if feature is None:
        return None
    result = _probe_and_decide(unit, feature.repo_path)
    if isinstance(result, str):
        logger.info("daemon: probe skipped for %s: %s", unit.unit_id, result)
        return None
    return result


def _persist_shadow_decisions(unit: WorkUnitState, shadows: list[ShadowDecision]) -> None:
    """Persist every fired shadow rule as a ``shadow_transition_proposed`` event.

    Delegates to :func:`orchestrator.tools.health._record_shadow_event`
    — same writer ``inspect_unit_health`` calls, so the daemon and the
    blocking path produce identical audit rows. Spec § "F-015 absorption
    clause" requires the daemon to mirror this side effect; dropping it
    would silently shrink the operator-visible validation surface
    during Phase 3 rollout (every daemon-driven tick would produce
    strictly less audit data than an ``inspect_unit_health`` call on
    the same unit).
    """
    if not shadows:
        return
    from orchestrator.tools.health import _record_shadow_event  # noqa: PLC0415

    for shadow in shadows:
        _record_shadow_event(unit, shadow)


def _maybe_persist_snapshot(unit: WorkUnitState, report: HealthReport) -> bool:
    """Best-effort F-014 ``health_report_snapshot`` write, ≤ once per interval.

    Delegates to :func:`orchestrator.tools.health._maybe_record_snapshot`,
    which is internally rate-limited by
    ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS`` (default 24h per unit) — so
    wiring it into the daemon's tick does NOT flood ``unit_events``.
    Returns ``True`` if a snapshot was recorded this call.
    """
    from orchestrator.tools.health import _maybe_record_snapshot  # noqa: PLC0415

    return _maybe_record_snapshot(unit, report)


# --------------------------- F-018 conflict-fix dispatch ---------------------------


def _dispatch_conflict_fix(unit: WorkUnitState, conflict_files: list[str]) -> bool:
    """F-018 daemon-side conflict-fix dispatch. Returns True on dispatch.

    Mirrors ``cycle_review``'s mergeable-gate flow for the post-terminal
    case: a unit sitting in ``approved_awaiting_merge`` develops a
    conflict because a sibling unit merged. The daemon transitions the
    unit back to ``fixing`` and submits a rebase request to the coder
    via :meth:`~orchestrator.workers.base.Worker.resume_async` — the
    submit-only variant so the daemon's tick isn't blocked while the
    coder rebases (minutes). The next tick observes the coder's
    ``FIX_PUSHED`` marker via the normal ``_scan_role`` path and the
    reviewer agent's stale-marker delta rule (F-016 Phase 2.5) re-runs
    the review on the new SHA.

    Cap check: ``conflict_fix_attempts`` is bumped via
    :func:`~orchestrator.state.increment_conflict_fix_attempts` and
    capped at ``CAP_3`` independent of ``review_round``. On cap-3 hit
    the unit transitions to ``escalated`` with the
    ``conflict_rebase_diverging`` reason — main is too volatile and a
    human must decide whether to land a sibling first, rebase manually,
    or take over.

    Pre-conditions and mutation ordering:

      * ``coder_session_id`` is set (no coder → can't resume; daemon
        logs + returns False so the next tick re-derives).
      * ``pr_number`` is set + feature row + plan row + unit row exist
        (compose_fix_task requires them).
      * Owner CAS is grabbed before ANY state mutation — both the
        cap-3 escalation event/transition and the
        increment+transition+resume-async path live inside the CAS
        block so an unlanded escalation (lead holds the lock, CAS
        fails) does NOT leave a stray ``conflict_rebase_diverging``
        audit row, and a re-tick under contention does not duplicate
        rows (PR #66 H2).

    Idempotence: the per-unit CAS ensures only one daemon instance (or
    tick) lands the transition + dispatch. A daemon re-tick on the same
    conflict signal observes ``status='fixing'`` and skips Stage 3
    entirely (the ``READY_TO_MERGE_STATUSES`` guard above).
    """
    if not unit.coder_session_id or not unit.pr_number:
        logger.info(
            "daemon: conflict-fix dispatch skipped for %s — missing coder session / PR",
            unit.unit_id,
        )
        return False

    feature = state.get_feature(unit.feature_id)
    plan = state.get_plan(unit.feature_id)
    if feature is None or plan is None:
        logger.info(
            "daemon: conflict-fix dispatch skipped for %s — missing feature/plan",
            unit.unit_id,
        )
        return False
    work_unit = next((u for u in plan.units if u.id == unit.unit_id), None)
    if work_unit is None:
        logger.info(
            "daemon: conflict-fix dispatch skipped for %s — unit not in plan",
            unit.unit_id,
        )
        return False

    # CAS BEFORE any mutation. Both the cap-3 escalation branch and the
    # dispatch-with-CI-fix branch live under one owner claim so a lead
    # holding ``send_to_unit_async`` blocks BOTH paths (PR #66 H2: the
    # original cap-3 ``record_event`` lived outside the CAS, so a
    # contended tick still wrote the audit row even though the escalation
    # itself never landed — and re-ticked under continued contention
    # would write another row each poll until the lead released).
    if not state.claim_unit_owner(unit.unit_id, DAEMON_OWNER, expected_owner=""):
        return False
    try:
        latest = state.get_unit_state(unit.unit_id)
        if latest is None or latest.cancelled_at is not None:
            return False
        if latest.status not in READY_TO_MERGE_STATUSES:
            return False
        # ``pr_number`` was non-None at the entry pre-condition; re-asserting
        # here narrows the type for ``compose_fix_task`` and guards the rare
        # race where another path nulled it between the entry check and the
        # owner CAS.
        if latest.pr_number is None:
            return False

        attempts = latest.conflict_fix_attempts
        if attempts >= CAP_3:
            state.record_event(
                latest.unit_id,
                latest.feature_id,
                "conflict_rebase_diverging",
                source="orchestrator",
                cycle_number=attempts,
                summary=f"cap-{CAP_3} on conflict-fix attempts (daemon-detected)",
                details=json.dumps(
                    {
                        "reason": "conflict_rebase_diverging",
                        "conflict_files": list(conflict_files),
                    }
                ),
                # Per-staleness-window dedupe key. A re-tick under
                # cancelled / lock-contention races would otherwise
                # collapse to no-op via INSERT OR IGNORE, even if this
                # branch were ever entered outside the CAS again.
                dedupe_key=f"conflict_rebase_diverging:{latest.unit_id}:{attempts}",
            )
            state.touch_unit(
                latest.unit_id,
                status="escalated",
                error=(
                    f"BLOCKED [conflict_rebase_diverging]: "
                    f"cap-{CAP_3} rebase attempts on approved_awaiting_merge unit"
                ),
            )
            return False

        new_attempts = state.increment_conflict_fix_attempts(latest.unit_id)
        state.touch_unit(latest.unit_id, status="fixing")
        state.record_event(
            latest.unit_id,
            latest.feature_id,
            "coder_resumed",
            source="merge",
            cycle_number=latest.review_round,
            summary=(
                f"daemon conflict-fix dispatch (attempt {new_attempts}/{CAP_3}); "
                f"sibling-induced conflict on awaiting-merge unit"
            ),
            details=json.dumps({"conflict_files": list(conflict_files)}),
        )

        files_csv = ", ".join(conflict_files) if conflict_files else "(unspecified)"
        feedback = (
            f"A sibling unit merged while this PR was awaiting human merge; "
            f"a conflict is now blocking the merge (attempt {new_attempts}/{CAP_3}). "
            f"Rebase against main; resolve conflicts in: {files_csv}. Force-push "
            f"the rebased HEAD (see `## When resumed with feedback` → `SOURCE: "
            f"merge` in your system prompt)."
        )
        # Re-inject feature spec + predecessor cycle-log context on every
        # resume so a spec edit between the original spawn and this
        # daemon-driven resume reaches the coder (F-006 contract). Lazy
        # import so a missing ``feature_spec`` / ``cycle_log`` module
        # doesn't break the daemon's module load.
        from orchestrator import cycle_log, feature_spec  # noqa: PLC0415

        fix_msg = compose_fix_task(
            feature,
            work_unit,
            latest.branch,
            latest.pr_number,
            "merge",
            feedback,
            feature_spec_text=feature_spec.read_spec(feature.id),
            predecessor_logs=[
                (dep_id, cycle_log.cycle_log_summary(dep_id)) for dep_id in work_unit.depends_on
            ],
        )
        try:
            worker = _cached_worker("coder")
            worker.resume_async(latest.coder_session_id, fix_msg)
        except Exception:  # noqa: BLE001 — log + escalation via the next tick
            logger.exception("daemon: conflict-fix resume_async failed for %s", unit.unit_id)
            return False
        return True
    finally:
        state.release_unit_owner(unit.unit_id, expected_owner=DAEMON_OWNER)


# --------------------------- F-018 stale-marker delta-review dispatch ---------------------------


def _latest_event_summary(unit_id: str, event_types: frozenset[str]) -> str:
    """Most-recent ``summary`` value for events whose type is in ``event_types``.

    Returns ``""`` when no matching event exists — :func:`compose_reviewer_delta_task`
    has a "(none recorded)" / "(no summary)" fallback for empty inputs so the
    delta prompt is still well-formed.
    """
    for event in reversed(state.list_events(unit_id)):
        if event.get("event_type") in event_types:
            return event.get("summary", "") or ""
    return ""


def _dispatch_delta_review_if_stale(unit: WorkUnitState) -> bool:
    """F-018 Stage 4 — autonomous loop closure via the F-016 Phase 2.5 stale-marker rule.

    After a daemon-dispatched conflict-fix (or any other coder push the
    daemon observes), the unit progresses ``fixing → in_ci`` once
    ``_apply_marker_transition`` picks up the coder's ``FIX_PUSHED``.
    The reviewer's prior marker is now stale relative to the new PR
    head — F-014's mergeable probe alone won't drive the unit forward,
    and the marker source-status fence in :data:`_MARKER_SOURCE_STATUSES`
    refuses to re-fire ``REVIEW_RECOMMEND_MERGE`` from ``in_ci``.

    This helper composes
    :func:`~orchestrator.stale_marker.detect_stale_reviewer_marker`
    with a submit-only :meth:`~orchestrator.workers.base.Worker.resume_async`
    against the reviewer session to autonomously close the recovery
    loop: the reviewer reassesses the new SHA via the existing F-013
    delta-review prompt (:func:`~orchestrator.tools.compose_reviewer_delta_task`)
    and emits a fresh terminal marker on a subsequent tick — the same
    ``REVIEW_RECOMMEND_MERGE`` / ``REVIEW_COMMENT`` markers
    :func:`_apply_marker_transition` already drives.

    Spec § Acceptance 4: "After the rebase clears conflicts, the
    reviewer phase resumes via the existing delta-review path on the
    new SHA." Without this Stage, a daemon-driven rebase strands at
    ``in_ci`` until a human re-calls ``cycle_review`` — defeating the
    purpose of the daemon trigger (PR #66 H1).

    Pre-conditions and ordering (mirrors :func:`_dispatch_conflict_fix`):

      * ``status == 'in_ci'`` — the coder's push has landed and CI has
        had a chance to settle; nothing else is in flight on this unit.
      * ``reviewer_session_id`` is set — we can only ``resume_async``
        an existing session. A unit that has never seen the reviewer
        phase has no stale marker by definition.
      * ``pr_number`` is set + feature/plan rows exist —
        :func:`compose_reviewer_delta_task` requires them.
      * Owner CAS is grabbed before ANY mutation so a lead's
        ``send_to_unit_async`` window doesn't race the in_ci → reviewing
        transition + delta-prompt submit.

    Idempotence: F-016 Phase 2.5's ``reviewer_stale_marker_pending_delta``
    audit row dedupes per-staleness-window (the event_ids of the prior
    reviewer marker + the later coder push together form the dedupe
    key). A re-tick that observes the same staleness window dedupes to
    the existing row, and the ``status='reviewing'`` flip is gated by
    the status guard above so a second tick under contention is a no-op.

    Best-effort head_sha fetch: ``prior_sha`` is unavailable today
    (per :mod:`~orchestrator.stale_marker` § "Phase 2.5 ships the
    classifier uncomposed because no caller persists head_sha on
    reviewer-marker events"). The delta prompt's
    "(unknown — diff from your last reviewed state)" fallback handles
    the absence; ``current_sha`` is best-effort via
    :func:`~orchestrator.github.get_pr_state`.
    """
    if unit.status != "in_ci":
        return False
    if not unit.reviewer_session_id or not unit.pr_number:
        return False

    detection = stale_marker.detect_stale_reviewer_marker(unit.unit_id)
    if detection["case"] != "stale":
        return False

    feature = state.get_feature(unit.feature_id)
    plan = state.get_plan(unit.feature_id)
    if feature is None or plan is None:
        logger.info(
            "daemon: delta-review dispatch skipped for %s — missing feature/plan",
            unit.unit_id,
        )
        return False
    work_unit = next((u for u in plan.units if u.id == unit.unit_id), None)
    if work_unit is None:
        logger.info(
            "daemon: delta-review dispatch skipped for %s — unit not in plan",
            unit.unit_id,
        )
        return False

    if not state.claim_unit_owner(unit.unit_id, DAEMON_OWNER, expected_owner=""):
        return False
    try:
        latest = state.get_unit_state(unit.unit_id)
        if latest is None or latest.cancelled_at is not None:
            return False
        if latest.status != "in_ci":
            # State moved underneath us — a peer tick or the lead got
            # there first. The next tick re-derives.
            return False
        if not latest.reviewer_session_id or latest.pr_number is None:
            return False

        # Best-effort current_sha for the delta range. The lazy import
        # mirrors :func:`_dispatch_conflict_fix` — `orchestrator.github`
        # is light, but the helper modules used downstream
        # (`feature_spec` / `cycle_log`) decorate at import time.
        from orchestrator import cycle_log, feature_spec, github  # noqa: PLC0415

        current_sha = ""
        try:
            pr_state = github.get_pr_state(feature.repo_path, latest.pr_number)
            current_sha = pr_state.get("head_sha") or ""
        except Exception:  # noqa: BLE001 — delta prompt has a fallback
            logger.exception("daemon: get_pr_state failed for %s; using empty SHA", unit.unit_id)

        prior_findings = _latest_event_summary(
            latest.unit_id, stale_marker.REVIEWER_TERMINAL_EVENT_TYPES
        )
        fix_summary = _latest_event_summary(latest.unit_id, stale_marker.CODER_PUSH_EVENT_TYPES)

        # Record the stale-marker audit row BEFORE the transition + submit
        # so an operator reading the timeline sees the daemon's intent
        # before any state moves. Dedupe key is per-staleness-window (the
        # two event_ids) so a re-tick under the same window no-ops.
        stale_marker.record_stale_marker_pending_delta(
            latest.unit_id,
            latest.feature_id,
            reviewer_event_id=detection["reviewer_marker_id"],
            later_push_event_id=detection["later_coder_push_id"],
            reviewer_event_type=detection["reviewer_marker_event_type"],
            later_push_event_type=detection["later_coder_push_event_type"],
        )

        delta_msg = compose_reviewer_delta_task(
            feature=feature,
            unit=work_unit,
            pr_number=latest.pr_number,
            prior_sha="",  # not persisted on reviewer-marker events today
            current_sha=current_sha,
            prior_findings=prior_findings,
            fix_summary=fix_summary,
            feature_spec_text=feature_spec.read_spec(feature.id),
            predecessor_logs=[
                (dep_id, cycle_log.cycle_log_summary(dep_id)) for dep_id in work_unit.depends_on
            ],
        )

        state.touch_unit(latest.unit_id, status="reviewing")
        state.record_event(
            latest.unit_id,
            latest.feature_id,
            "reviewer_resumed_for_delta",
            source="orchestrator",
            cycle_number=latest.review_round,
            summary=(f"daemon delta re-review on new SHA (?..{current_sha[:8] or '?'})"),
            session_id=latest.reviewer_session_id,
        )
        try:
            worker = _cached_worker("reviewer")
            worker.resume_async(latest.reviewer_session_id, delta_msg)
        except Exception:  # noqa: BLE001 — log + escalation via the next tick
            logger.exception("daemon: delta-review resume_async failed for %s", unit.unit_id)
            return False
        return True
    finally:
        state.release_unit_owner(unit.unit_id, expected_owner=DAEMON_OWNER)


# --------------------------- reconciler ---------------------------


def reconcile_unit(unit_id: str) -> None:
    """Level-triggered reconcile pass for one unit.

    Per the spec's main-loop pseudocode (§ Phase 3), the daemon is
    stateless between calls: this function re-reads ``unit_id``'s
    state, scans every role's session for new markers, applies any
    marker-driven transition, then runs the F-014 probe + decide engine
    for PR-side reconciliation. Every step is idempotent on its own;
    re-running this call from a dropped tick is safe.

    Short-circuits — no state mutation when:

      * the unit row is missing (race with delete);
      * ``cancelled_at`` is set (user pulled the unit; spec § Phase 2.5
        sticky-cancel);
      * ``status`` is already terminal (``done`` / ``escalated``);
      * :func:`~orchestrator.state.has_active_advance_lock` is True
        (lead's ~1s ``send_to_unit_async`` window).

    The cancel and lock guards are re-checked between stages so a user
    cancellation mid-tick stops further work immediately rather than
    completing the F-014 probe on a unit the user just stopped.

    F-018: when the F-014 probe surfaces a ``pr_conflict_detected``
    event AND the unit is currently in ``approved_awaiting_merge`` (i.e.
    a sibling unit merged while this PR sat awaiting human merge), the
    daemon transitions the unit back to ``fixing`` and submits a rebase
    request to the coder via :meth:`~orchestrator.workers.base.Worker.resume_async`
    (submit-only — distinct from ``cycle_review``'s synchronous
    ``address_review('merge', …)`` because the daemon tick can't block
    on the coder's multi-minute rebase). See :func:`_dispatch_conflict_fix`.

    F-018 Stage 4 closes the loop autonomously after the coder's
    rebase: when status is ``in_ci`` AND a stale reviewer marker is
    detected (reviewer endorsed on a SHA earlier than the latest
    coder push, per F-016 Phase 2.5's classifier), the daemon submits
    a delta-review prompt to the reviewer session via
    ``resume_async``. See :func:`_dispatch_delta_review_if_stale`.
    """
    unit = state.get_unit_state(unit_id)
    if unit is None:
        return
    if not _is_actionable(unit):
        return

    # Stage 1: observe — scan worker sessions, record new markers,
    # apply any status flips the marker implies.
    for role in ROLES:
        # Re-check actionability at the TOP of every iteration so the
        # no-marker path (a role whose ``_scan_role`` returns ``(None,
        # sid)``) also bails on a cancel landed mid-tick — the docstring's
        # promise of "completing the F-014 probe on a unit the user just
        # stopped" must hold whether or not a marker was observed on the
        # previous role.
        unit = state.get_unit_state(unit_id) or unit
        if not _is_actionable(unit):
            return
        spec, sid = _scan_role(unit, role)
        if spec is None or not sid:
            continue
        # PR #66 N1: gate the status flip on ``_record_marker`` returning
        # True (dedupe insert landed). A persistent marker that has
        # already been recorded must NOT re-fire ``_apply_marker_transition``
        # on a subsequent tick — that is precisely the cross-cycle
        # backflip the source-status-fence comment at
        # ``_MARKER_SOURCE_STATUSES`` calls out as "reachable once Stage 4
        # re-enters reviewing with the same reviewer_session_id". Stage 4
        # (F-018 autonomous loop closure) is the path that violates the
        # original caller contract; this dedupe-gate closes the race by
        # treating the unit_events.dedupe_key UNIQUE constraint as the
        # source of truth for "this marker has already been applied".
        # The fence docstring at L222-258 names this as the natural
        # follow-up ("the proposed unit_events dedupe-key lookup in
        # PR #61 review thread M1 is the natural follow-up").
        if not _record_marker(unit, spec, sid):
            continue
        if _apply_marker_transition(unit_id, spec):
            unit = state.get_unit_state(unit_id) or unit
            if not _is_actionable(unit):
                return

    # Stage 2: F-014 probe + decide — PR / CI / merge reconciliation.
    # ``_probe_and_decide_unit`` ALSO persists ``decision.shadow_decisions``
    # and the rate-limited ``health_report_snapshot`` via the same
    # ``tools.health`` helpers ``inspect_unit_health`` uses; the daemon's
    # tick produces the same audit footprint as the blocking caller
    # (spec § "F-015 absorption clause": F-014's shadow-decision
    # telemetry is F-016's validation harness).
    if not _is_actionable(unit):
        return
    conflict_action: Action | None = None
    for action in _probe_and_decide_unit(unit):
        unit = state.get_unit_state(unit_id) or unit
        if not _is_actionable(unit):
            return
        _apply_health_action(unit, action)
        # Capture the F-018 conflict signal so Stage 3 can dispatch the
        # rebase without re-probing GitHub. We only care about the most
        # recent conflict action of the tick — the decision table emits
        # at most one ``pr_conflict_detected`` per probe.
        if action.kind == "event" and action.event_type == "pr_conflict_detected":
            conflict_action = action

    # Stage 3 (F-018): post-terminal conflict recovery. The transition
    # row ``(approved_awaiting_merge, PrConflictDetected) → DispatchConflictFix``
    # fires only when the unit is currently parked awaiting human merge
    # — for an active unit the gate inside ``cycle_review`` already
    # handles the same case synchronously.
    if conflict_action is not None:
        unit = state.get_unit_state(unit_id) or unit
        if not _is_actionable(unit):
            return
        if unit.status in READY_TO_MERGE_STATUSES:
            files_raw = conflict_action.payload.get("conflict_files") or []
            _dispatch_conflict_fix(unit, list(files_raw))
            # Re-read after the dispatch so Stage 4 sees the fixing flip
            # (or stays put when the dispatch was a no-op).
            unit = state.get_unit_state(unit_id) or unit
            if not _is_actionable(unit):
                return

    # Stage 4 (F-018): autonomous loop closure. After Stage 1 picked up
    # the coder's FIX_PUSHED (post-rebase) → status flipped to in_ci, the
    # reviewer's prior endorsement is stale relative to the new SHA.
    # F-016 Phase 2.5's classifier detects it; this Stage submits the
    # delta-review prompt to the reviewer session so the unit can reach
    # ``approved_awaiting_merge`` on the rebased HEAD without lead
    # re-engagement. Spec § Acceptance 4. PR #66 H1.
    if not _is_actionable(unit):
        return
    _dispatch_delta_review_if_stale(unit)


def _is_actionable(unit: WorkUnitState) -> bool:
    """True iff the daemon may mutate ``unit``'s state this tick."""
    if unit.cancelled_at is not None:
        return False
    if unit.status in TERMINAL_UNIT_STATUSES:
        return False
    return not state.has_active_advance_lock(unit.unit_id)


def reconcile_once() -> int:
    """One full reconcile pass over every active unit. Returns the count touched.

    Per-unit exceptions are caught and logged — a single broken row
    must not freeze the daemon. The count covers attempts (including
    short-circuited skips) so monitoring can see the loop is alive even
    when nothing changed.
    """
    count = 0
    for unit in state.list_active_units():
        count += 1
        try:
            reconcile_unit(unit.unit_id)
        except Exception:  # noqa: BLE001 — observability over correctness here
            logger.exception("daemon: reconcile_unit(%s) raised", unit.unit_id)
    return count


# --------------------------- singleton + main loop ---------------------------


@dataclass(frozen=True)
class DaemonHandle:
    """Opaque token returned by :func:`claim_singleton` on a successful claim.

    Tests use it directly to drive ``heartbeat`` / ``release`` without
    constructing a :class:`DaemonLoop`; the loop wraps the handle so
    the same code path serves both surfaces.
    """

    holder_id: str
    state_db_path: str


def _state_db_path_str() -> str:
    """Absolute path to ``state.STATE_DB`` as a string.

    The lock-table column is ``TEXT PRIMARY KEY`` keyed on this exact
    string, so per-workspace isolation hinges on a stable, fully-
    resolved path. ``Path.resolve()`` collapses ``..`` segments and
    symlinks; tests setting ``STATE_DB`` to a ``tmp_path`` file see the
    same resolved string the production daemon would.
    """
    return str(Path(state.STATE_DB).resolve())


def claim_singleton(
    *,
    holder_id: str | None = None,
    stale_after_s: int = state.DEFAULT_DAEMON_LOCK_STALE_AFTER_S,
) -> DaemonHandle | None:
    """Acquire the workspace-scoped daemon lock or return ``None`` on contention.

    Args:
        holder_id: Override the random per-start UUID. Production
            callers never set this; tests pass a stable string so
            assertions can match ``daemon_locks.holder_id``.
        stale_after_s: Forwarded to :func:`~orchestrator.state.claim_daemon_lock`
            — how old the existing holder's heartbeat must be before
            takeover is allowed.
    """
    holder = holder_id or uuid.uuid4().hex
    path = _state_db_path_str()
    if state.claim_daemon_lock(path, holder, stale_after_s=stale_after_s):
        return DaemonHandle(holder_id=holder, state_db_path=path)
    return None


def heartbeat(handle: DaemonHandle) -> bool:
    """Bump the lock's ``heartbeat_at`` while ``handle`` still owns it.

    Returns ``False`` if another daemon took over (stale takeover after
    a crash + the new owner picked up the row). The caller should shut
    down rather than continue driving state.
    """
    return state.heartbeat_daemon_lock(handle.state_db_path, handle.holder_id)


def release_singleton(handle: DaemonHandle) -> bool:
    """Drop the lock row iff ``handle`` still owns it. Returns True on success."""
    return state.release_daemon_lock(handle.state_db_path, handle.holder_id)


class DaemonLoop:
    """The main reconcile loop.

    Stateless between ticks — :meth:`tick` re-reads everything from
    ``state.db``. The class only owns the lock handle, the stop flag,
    and the configured poll interval; nothing about the loop's
    correctness depends on instance state being preserved across
    ticks.

    Tests call :meth:`tick` directly to drive a deterministic single
    pass; production calls :meth:`run` which loops until SIGINT /
    SIGTERM (or :meth:`stop` from another thread).
    """

    def __init__(
        self,
        *,
        poll_interval_s: float | None = None,
        holder_id: str | None = None,
    ) -> None:
        self.poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else _poll_interval_s()
        )
        self._stop = threading.Event()
        self._holder_id_override = holder_id
        self.handle: DaemonHandle | None = None

    def stop(self) -> None:
        """Request a clean shutdown. Safe to call from any thread."""
        self._stop.set()

    def is_stopping(self) -> bool:
        return self._stop.is_set()

    def tick(self) -> int:
        """One reconcile pass + heartbeat. Returns the count of units processed.

        Heartbeats first so a single hung ``reconcile_unit`` doesn't
        starve the lock and provoke a spurious takeover. A False
        heartbeat means another daemon stole the lock — set the stop
        flag so the next loop iteration exits cleanly.
        """
        if self.handle is not None and not heartbeat(self.handle):
            logger.warning(
                "daemon: heartbeat lost (lock taken over); shutting down",
            )
            self._stop.set()
            return 0
        return reconcile_once()

    def run(self) -> int:
        """Acquire the singleton, then loop until stopped.

        Returns one of:

          * the total tick count (``>= 0``) on a clean shutdown;
          * :data:`EXIT_DRIVE_DISABLED` (``-2``) when ``ORCH_DAEMON_DRIVE``
            isn't set to a truthy value — operator forgot the opt-in;
          * :data:`EXIT_LOCK_HELD` (``-3``) when :func:`claim_singleton`
            returned ``None`` — another daemon already owns the
            workspace.

        The two sentinels are negative so the CLI's ``daemon start``
        wrapper can map them to exit codes 2 / 3 (vs. 0 for clean exit
        and a tick count) without ambiguity, letting a systemd /
        launchd / shell-script supervisor branch on ``$?`` to decide
        between "nudge the operator", "retry later", and "stop trying".
        """
        if not is_drive_enabled():
            logger.info(
                "daemon: %s not set to a truthy value — refusing to drive state",
                DAEMON_DRIVE_ENV,
            )
            return EXIT_DRIVE_DISABLED
        self.handle = claim_singleton(holder_id=self._holder_id_override)
        if self.handle is None:
            existing = state.get_daemon_lock(_state_db_path_str())
            logger.warning(
                "daemon: another instance holds the lock for %s (holder=%s heartbeat=%s)",
                state.STATE_DB,
                existing.get("holder_id") if existing else "?",
                existing.get("heartbeat_at") if existing else "?",
            )
            return EXIT_LOCK_HELD

        ticks = 0
        with self._install_signal_handlers():
            try:
                while not self._stop.is_set():
                    try:
                        self.tick()
                    except Exception:  # noqa: BLE001 — never crash the loop
                        logger.exception("daemon: tick raised; continuing")
                    ticks += 1
                    self._stop.wait(self.poll_interval_s)
            finally:
                if self.handle is not None:
                    release_singleton(self.handle)
                    self.handle = None
        return ticks

    @contextlib.contextmanager
    def _install_signal_handlers(self) -> Iterator[None]:
        """Install SIGINT / SIGTERM to call :meth:`stop`.

        ``signal.signal`` only works on the main thread — tests that
        instantiate a loop on a worker thread (or under uvicorn /
        pytest's main-thread guard) get a ``ValueError``; we
        gracefully no-op so those callers can still drive
        :meth:`tick` directly.
        """
        # ``signal.signal`` returns either a callable, ``signal.SIG_DFL``,
        # ``signal.SIG_IGN``, or ``None``. The stub's union accepts every
        # value we'd ever store, so reusing the return type as the entry
        # type keeps the restore call type-clean across CPython versions.
        from typing import Any  # noqa: PLC0415 — keep typing import scoped

        installed: list[tuple[int, Any]] = []

        def _handler(_signum: int, _frame: object) -> None:
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old = signal.signal(sig, _handler)
            except (ValueError, OSError, AttributeError):
                # Not main thread, or signal unsupported on this OS.
                continue
            installed.append((int(sig), old))
        try:
            yield
        finally:
            for sig_num, old in installed:
                with contextlib.suppress(Exception):
                    signal.signal(sig_num, old)


def run_daemon() -> int:
    """Entry point for ``python -m orchestrator.daemon`` / CLI ``daemon start``.

    Idempotent on configuration — re-call after fixing ``ORCH_DAEMON_DRIVE``
    is fine. Configures a minimal stderr logger so an operator running
    the daemon in a terminal sees ``daemon: tick ...`` lines without
    plumbing log handlers themselves.

    Return value semantics: see :meth:`DaemonLoop.run` — non-negative
    is a clean shutdown tick count; :data:`EXIT_DRIVE_DISABLED` /
    :data:`EXIT_LOCK_HELD` are the two non-zero exit paths. Pair with
    :func:`exit_code_for_run` to turn the result into a process exit
    code at the CLI / ``__main__`` boundary.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s daemon: %(message)s",
        datefmt="%H:%M:%S",
    )
    return DaemonLoop().run()


def exit_code_for_run(ticks: int) -> int:
    """Map :meth:`DaemonLoop.run`'s return value to a process exit code.

    Three distinct outcomes downstream supervisors (systemd, launchd,
    shell scripts) need to branch on:

      * ``ticks >= 0`` → clean shutdown (whether we ticked or not).
        Exit 0 — the canonical "service ran to completion" signal.
      * ``ticks == EXIT_DRIVE_DISABLED`` (``-2``) → operator forgot the
        opt-in env var. Exit 2 so a systemd ``Restart=on-failure``
        retry policy nudges the operator without busy-looping.
      * ``ticks == EXIT_LOCK_HELD`` (``-3``) → another daemon owns the
        workspace. Exit 3 so a supervisor can stop trying immediately
        (a same-workspace contention is a configuration error, not a
        transient).

    Centralised so the ``__main__`` block and the CLI's ``daemon start``
    wrapper share one mapping — the comment block on the call site
    stays accurate when the sentinel set grows.
    """
    if ticks == EXIT_LOCK_HELD:
        return 3
    if ticks == EXIT_DRIVE_DISABLED:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via ``orchestrator daemon start``
    # Mirror the standard MCP server's load_dotenv so an operator
    # running the daemon directly from a workspace's terminal sees the
    # same ``GITHUB_TOKEN`` / ``ANTHROPIC_API_KEY`` / ``ORCH_DAEMON_DRIVE``
    # surface as ``orchestrator run`` does.
    from dotenv import load_dotenv

    load_dotenv()
    state.init_db()
    raise SystemExit(exit_code_for_run(run_daemon()))
