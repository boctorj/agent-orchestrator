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
import logging
import os
import signal
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from orchestrator import markers, state
from orchestrator.health import Action
from orchestrator.markers import MarkerSpec
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    TERMINAL_UNIT_STATUSES,
    WorkUnitState,
)
from orchestrator.workers import make_worker

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


# Active statuses that a marker's ``target_status`` should flip the unit
# *from*. Pre-flight check (``status`` re-read inside the CAS makes the
# guard a race-safe fence) so an already-flipped unit isn't bounced
# backwards by a stale tail.
_FLIPPABLE_STATUSES: frozenset[str] = ACTIVE_UNIT_STATUSES


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
    """
    raw = os.getenv(POLL_INTERVAL_ENV, "").strip()
    if not raw:
        return POLL_INTERVAL_DEFAULT_S
    try:
        parsed = float(raw)
    except ValueError:
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
    """
    sid = _role_session_id(unit, role)
    if not sid:
        return None, ""
    try:
        worker = make_worker(role)
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

    Returns ``True`` when the flip landed. The five short-circuits — in
    decreasing locality — together fence every race:

      * ``spec.target_status`` is ``None`` → marker has no status flip
        (BUG_FOUND / REVIEW_REQUEST_CHANGES leave status to the
        fix-loop).
      * :func:`~orchestrator.state.claim_unit_owner` returns False →
        lead holds the advance lock (``owner='lead'``) OR another daemon
        instance is mid-tick on the same unit. Skip; next poll retries.
      * Re-read inside the CAS: ``cancelled_at`` set OR status not in
        :data:`_FLIPPABLE_STATUSES` → user cancelled or another writer
        already flipped. Skip and release.
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
        if latest.status not in _FLIPPABLE_STATUSES:
            return False
        if latest.status == spec.target_status:
            # Already at target — a re-tick on the same marker after the
            # flip is a no-op rather than a wasted UPDATE. Keeps the
            # idempotence guarantee explicit at the call site.
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
    """Run F-014 probe + decide for ``unit``. Empty list on any non-success.

    No-ops when the unit has no PR yet (F-014's probe needs one). The
    GitHub-side ``need_github_token`` / fetch errors are returned as
    strings by the shared helper; the daemon swallows them and logs —
    a missing token shouldn't crash the tick loop.

    Lazy import of ``orchestrator.tools.health`` for the same MCP-
    decorator reason as :func:`_apply_health_action`.
    """
    if not unit.pr_number:
        return []
    from orchestrator.tools.health import _probe_and_decide  # noqa: PLC0415

    feature = state.get_feature(unit.feature_id)
    if feature is None:
        return []
    result = _probe_and_decide(unit, feature.repo_path)
    if isinstance(result, str):
        logger.info("daemon: probe skipped for %s: %s", unit.unit_id, result)
        return []
    _report, decision = result
    return list(decision.actions_to_apply)


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
    """
    unit = state.get_unit_state(unit_id)
    if unit is None:
        return
    if not _is_actionable(unit):
        return

    # Stage 1: observe — scan worker sessions, record new markers,
    # apply any status flips the marker implies.
    for role in ROLES:
        spec, sid = _scan_role(unit, role)
        if spec is None or not sid:
            continue
        _record_marker(unit, spec, sid)
        # Re-check guards between markers — a cancel observed on the
        # tester role must stop the reviewer flip on the same tick.
        unit = state.get_unit_state(unit_id) or unit
        if not _is_actionable(unit):
            return
        if _apply_marker_transition(unit_id, spec):
            unit = state.get_unit_state(unit_id) or unit
            if not _is_actionable(unit):
                return

    # Stage 2: F-014 probe + decide — PR / CI / merge reconciliation.
    if not _is_actionable(unit):
        return
    for action in _probe_and_decide_unit(unit):
        unit = state.get_unit_state(unit_id) or unit
        if not _is_actionable(unit):
            return
        _apply_health_action(unit, action)


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
        """Acquire the singleton, then loop until stopped. Returns total ticks.

        ``ORCH_DAEMON_DRIVE`` gate is checked first: an unintentional
        ``python -m orchestrator.daemon`` on a workspace that hasn't
        migrated returns 0 without touching the lock table. Same goes
        for a workspace whose lock is already held by a fresh daemon —
        :func:`claim_singleton` returns ``None`` and we bail. Both
        outcomes log at INFO so an operator can tell the difference
        between "I forgot the env var" and "another daemon is running".
        """
        if not is_drive_enabled():
            logger.info(
                "daemon: %s not set to a truthy value — refusing to drive state",
                DAEMON_DRIVE_ENV,
            )
            return 0
        self.handle = claim_singleton(holder_id=self._holder_id_override)
        if self.handle is None:
            existing = state.get_daemon_lock(_state_db_path_str())
            logger.warning(
                "daemon: another instance holds the lock for %s (holder=%s heartbeat=%s)",
                state.STATE_DB,
                existing.get("holder_id") if existing else "?",
                existing.get("heartbeat_at") if existing else "?",
            )
            return 0

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
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s daemon: %(message)s",
        datefmt="%H:%M:%S",
    )
    return DaemonLoop().run()


if __name__ == "__main__":  # pragma: no cover — exercised via ``orchestrator daemon start``
    # Mirror the standard MCP server's load_dotenv so an operator
    # running the daemon directly from a workspace's terminal sees the
    # same ``GITHUB_TOKEN`` / ``ANTHROPIC_API_KEY`` / ``ORCH_DAEMON_DRIVE``
    # surface as ``orchestrator run`` does.
    from dotenv import load_dotenv

    load_dotenv()
    state.init_db()
    ticks = run_daemon()
    # Exit 0 on a clean shutdown (any positive tick count, or a no-op
    # exit because the gate wasn't set); exit 1 if claim_singleton
    # failed (another daemon owns the lock).
    raise SystemExit(0 if ticks >= 0 else 1)
