"""F-016-U-5 — Phase 3: watcher daemon (tester tests).

Independent of the coder's tests in ``tests/test_state.py`` and
``tests/test_daemon.py``. This file pins the spec-acceptance behaviour
those test files don't explicitly assert against
``features/F-016/spec.md`` § Phase 3 and the proposal's per-phase
acceptance criteria:

  * **The daemon does NOT drive without ``ORCH_DAEMON_DRIVE=true``.**
    Spec § "Decisions": "Default-flip gated on NTFY_TOPIC" and proposal
    § "Migration sequence" step 5: "Daemon-driven mode opt-in via env
    var ORCH_DAEMON_DRIVE=true." The opt-in must hold even when a unit
    has a clear next action; without it ``run_daemon`` is a no-op so
    accidental launches against an unmigrated workspace don't race
    a lead.

  * **Singleton enforcement is SQLite-backed, not pidfile.** Spec
    § "Singleton enforcement": "Use a SQLite lock table instead". A
    second :func:`~orchestrator.daemon.claim_singleton` while a fresh
    holder owns the row must return ``None`` (block) — never silently
    overwrite the row.

  * **One daemon per workspace.** Spec § "Daemon per workspace, not per
    host": "Two workspaces = two daemons, transparently". Two distinct
    ``state_db_path`` claims coexist; same-path claims contend.

  * **Crash recovery falls out of the level-triggered design.** Spec
    § "Crash recovery": "There is no special startup path." A stale
    heartbeat (>30s by default) is reclaimable by a new instance via
    :func:`~orchestrator.state.claim_daemon_lock`; the recovered daemon
    drives the unit forward without manual intervention.

  * **Stateless between ticks.** Spec § "Idempotent transitions":
    "Re-derive the correct next action from the unit's current state
    and the latest observed markers". A duplicate scan of the same
    response writes exactly one event (Phase 0 dedupe) and a re-flip
    of an already-transitioned unit is a no-op.

  * **Cancellation is honored on every tick.** Spec § "Phase 2.5
    sticky-cancel": "The daemon's per-tick check (``if unit.cancelled_at:
    continue``) guarantees no further transitions land." The daemon
    must skip cancelled units even when their worker session would
    have emitted a terminal marker.

  * **Lead advance-lock blocks the daemon mid-tick.** Spec § "The lock
    collapses because ``worker.resume`` becomes async": the daemon's
    pre-derive guard reads ``has_active_advance_lock`` and defers; the
    transition must not land while ``owner='lead'``.

  * **Owner CAS prevents lead/daemon double-write.** Spec § "Edge cases"
    (3): "Lead and daemon both reach terminal marker simultaneously.
    CAS on owner picks a winner; Phase 0 dedupe_key makes the loser's
    write a no-op." After a successful daemon-driven transition the
    ``owner`` column is cleared so a follow-up
    ``lead_advance_lock`` can claim it.

  * **F-014 engine is reused, not duplicated.** Spec § "No parallel
    state machine": "cycle_review_blocking and the daemon call the
    *same* derive_next_action + execute engine." The daemon delegates
    to :func:`orchestrator.tools.health._apply_action` and
    :func:`~orchestrator.tools.health._probe_and_decide` rather than
    reimplementing the transition table.

  * **Active-unit query covers the right buckets.** Spec § "main loop":
    "for unit in state.list_in_flight()". Our ``list_active_units``
    extends that to include ``approved_awaiting_merge`` so the F-014
    merged → done reconcile fires once the human merges, but excludes
    ``pending`` / ``done`` / ``escalated`` / ``cancelled``.

  * **Reconciler is exception-safe.** Spec § "R3"-style risk control:
    a broken row, a transient tail failure, or a flaky GitHub call on
    one unit must not freeze the entire workspace's daemon loop.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator import daemon, markers, state
from orchestrator.health import Action
from orchestrator.markers import MarkerSpec
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    READY_TO_MERGE_STATUSES,
    Feature,
    WorkUnitState,
)

# --------------------------- shared fixtures ---------------------------


@dataclass
class _TailResult:
    """``TailResult`` look-alike acceptable both via ``[]`` and ``.get``."""

    status: str = "idle"
    messages: list[dict] = field(default_factory=list)
    reason: str | None = None

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


class _SilentWorker:
    """Returns empty tail results — used when a test stubs the marker scan."""

    def tail_messages(self, *_a, **_k):
        return _TailResult()


def _seed_unit(
    *,
    unit_id: str = "U-D",
    feature_id: str = "F-D",
    status: str = "coding",
    sessions: dict[str, str] | None = None,
    pr_number: int | None = None,
) -> WorkUnitState:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
        )
    )
    s = sessions or {}
    unit = WorkUnitState(
        unit_id=unit_id,
        feature_id=feature_id,
        status=status,
        coder_session_id=s.get("coder", ""),
        tester_session_id=s.get("tester", ""),
        reviewer_session_id=s.get("reviewer", ""),
        pr_number=pr_number,
    )
    state.upsert_unit_state(unit)
    return unit


def _stub_no_workers(monkeypatch) -> None:
    monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _SilentWorker())


def _stub_no_probe(monkeypatch) -> None:
    monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [])


# --------------------------- spec-acceptance tests ---------------------------


class TestOptInGate:
    """``ORCH_DAEMON_DRIVE`` is load-bearing — without it the daemon refuses
    to claim the workspace lock or drive any unit."""

    def test_default_off(self, monkeypatch):
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        assert daemon.is_drive_enabled() is False

    def test_truthy_values_enable_drive(self, monkeypatch):
        for v in ("true", "TRUE", "True", "1", "yes", "on"):
            monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, v)
            assert daemon.is_drive_enabled() is True

    def test_run_noop_when_drive_disabled(self, tmp_state_db, monkeypatch):
        """A unit ripe for transition must NOT be touched when the env is off.

        Returns :data:`~orchestrator.daemon.EXIT_DRIVE_DISABLED` (PR #61
        reviewer M2) so a supervisor can distinguish "operator forgot
        the opt-in" from a clean-shutdown ``0``.
        """
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        unit = _seed_unit(status="coding", sessions={"coder": "sess_c"})

        # Tail would emit PR_URL — if the daemon ran, status would flip.
        class _ReadyWorker:
            def tail_messages(self, *_a, **_k):
                return _TailResult(
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": "PR_URL: https://github.com/o/r/pull/77",
                        }
                    ]
                )

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _ReadyWorker())
        loop = daemon.DaemonLoop(holder_id="us")
        assert loop.run() == daemon.EXIT_DRIVE_DISABLED
        # Status untouched — daemon refused to drive.
        assert state.get_unit_state(unit.unit_id).status == "coding"


class TestSingleton:
    """One daemon per workspace, enforced via the ``daemon_locks`` row."""

    def test_second_claim_blocks_while_first_is_fresh(self, tmp_state_db):
        h1 = daemon.claim_singleton(holder_id="d1")
        assert h1 is not None
        h2 = daemon.claim_singleton(holder_id="d2")
        assert h2 is None  # blocked — d1's heartbeat is fresh

    def test_distinct_workspaces_coexist(self, tmp_state_db):
        """Two ``state_db_path`` rows = two independent daemons."""
        assert state.claim_daemon_lock("/ws/a/state.db", "da") is True
        assert state.claim_daemon_lock("/ws/b/state.db", "db") is True
        assert state.get_daemon_lock("/ws/a/state.db")["holder_id"] == "da"
        assert state.get_daemon_lock("/ws/b/state.db")["holder_id"] == "db"


class TestCrashRecovery:
    """Spec § "Crash recovery": stale heartbeat → next claim reclaims."""

    def test_stale_heartbeat_allows_takeover(self, tmp_state_db):
        h1 = daemon.claim_singleton(holder_id="d1")
        assert h1 is not None
        # Age the heartbeat past the default stale window (30s).
        aged = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        import sqlite3 as _sql

        with _sql.connect(state.STATE_DB) as conn:
            conn.execute(
                "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ?",
                (aged, h1.state_db_path),
            )
            conn.commit()
        h2 = daemon.claim_singleton(holder_id="d2")
        assert h2 is not None
        assert h2.holder_id == "d2"
        # d1's heartbeat can no longer succeed.
        assert daemon.heartbeat(h1) is False
        # d2's heartbeat does.
        assert daemon.heartbeat(h2) is True

    def test_loop_drives_after_crash_with_no_special_recovery_path(self, tmp_state_db, monkeypatch):
        """Spec § "Crash recovery is automatic" — startup just runs ``tick``.

        Pre-seed a "ghost" lock (simulating a crashed previous daemon)
        with a stale heartbeat, then a fresh ``DaemonLoop.run`` reclaims
        and drives the unit's pending transition on its first tick.
        """
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        unit = _seed_unit(status="coding", sessions={"coder": "sess_c"})
        # Ghost daemon with a 60s-old heartbeat.
        path = str(state.STATE_DB.resolve())
        state.claim_daemon_lock(path, "ghost")
        aged = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        import sqlite3 as _sql

        with _sql.connect(state.STATE_DB) as conn:
            conn.execute(
                "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ?",
                (aged, path),
            )
            conn.commit()

        # Tail emits PR_URL → status should flip on the first tick.
        class _ReadyWorker:
            def tail_messages(self, *_a, **_k):
                return _TailResult(
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": "PR_URL: https://github.com/o/r/pull/1",
                        }
                    ]
                )

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _ReadyWorker())
        _stub_no_probe(monkeypatch)

        loop = daemon.DaemonLoop(poll_interval_s=0.01, holder_id="rebooted")
        orig_tick = loop.tick

        def tick_then_stop() -> int:
            result = orig_tick()
            loop.stop()
            return result

        monkeypatch.setattr(loop, "tick", tick_then_stop)
        ticks = loop.run()
        assert ticks == 1
        # Reclaimed daemon advanced the unit on its first reconciler pass.
        assert state.get_unit_state(unit.unit_id).status == "in_ci"


class TestStatelessIdempotency:
    """A duplicate tick on the same response must NOT duplicate events
    or backward-flip the unit's status."""

    def test_double_tick_writes_one_event(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="coding", sessions={"coder": "sess_c"})

        class _PrUrl:
            def tail_messages(self, *_a, **_k):
                return _TailResult(
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": "PR_URL: https://github.com/o/r/pull/12",
                        }
                    ]
                )

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _PrUrl())
        _stub_no_probe(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        daemon.reconcile_unit(unit.unit_id)
        events = state.list_events(unit.unit_id)
        assert sum(1 for e in events if e["event_type"] == "pr_opened") == 1
        assert state.get_unit_state(unit.unit_id).status == "in_ci"

    def test_marker_idempotent_when_already_at_target(self, tmp_state_db):
        """A re-flip of an already-transitioned unit returns False."""
        unit = _seed_unit(status="in_ci")
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/9")
        assert spec is not None
        assert daemon._apply_marker_transition(unit.unit_id, spec) is False
        assert state.get_unit_state(unit.unit_id).status == "in_ci"


class TestCancellationGuard:
    """Spec § Phase 2.5 sticky cancel — daemon must skip every cancelled unit."""

    def test_cancelled_unit_not_advanced_even_with_marker(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="coding", sessions={"coder": "sess_c"})
        state.cancel_unit(unit.unit_id)

        called: list[str] = []

        class _PrUrl:
            def tail_messages(self, *_a, **_k):
                called.append("tail")
                return _TailResult(
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": "PR_URL: https://github.com/o/r/pull/12",
                        }
                    ]
                )

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _PrUrl())
        _stub_no_probe(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        # Daemon short-circuits at the cancelled_at guard before tail.
        assert called == []
        # Status remains cancelled.
        assert state.get_unit_state(unit.unit_id).status == "cancelled"


class TestLeadAdvanceLockGuard:
    """Spec § "The lock collapses": daemon defers while owner='lead'."""

    def test_daemon_skips_when_lead_holds_owner(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="coding", sessions={"coder": "sess_c"})
        state.claim_unit_owner(unit.unit_id, state.LEAD_OWNER, expected_owner="")
        try:

            class _PrUrl:
                def tail_messages(self, *_a, **_k):
                    return _TailResult(
                        messages=[
                            {
                                "ts": "2024-01-01T00:00:00",
                                "role": "agent",
                                "text": "PR_URL: https://github.com/o/r/pull/12",
                            }
                        ]
                    )

            monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _PrUrl())
            _stub_no_probe(monkeypatch)
            daemon.reconcile_unit(unit.unit_id)
            # Status preserved; lead lock still held.
            assert state.get_unit_state(unit.unit_id).status == "coding"
            assert state.get_unit_state(unit.unit_id).owner == "lead"
        finally:
            state.release_unit_owner(unit.unit_id, expected_owner=state.LEAD_OWNER)


class TestOwnerCAS:
    """Spec § "Edge cases" (3): owner CAS picks a winner; loser bails."""

    def test_daemon_releases_owner_after_marker_transition(self, tmp_state_db):
        unit = _seed_unit(status="coding")
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/9")
        assert daemon._apply_marker_transition(unit.unit_id, spec) is True
        # Cleared so a follow-up lead_advance_lock can claim freely.
        assert state.get_unit_state(unit.unit_id).owner == ""

    def test_daemon_releases_owner_after_health_action(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="in_ci", pr_number=42)
        action = Action.transition("done", "merged")
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [action])
        _stub_no_workers(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        latest = state.get_unit_state(unit.unit_id)
        assert latest.status == "done"
        assert latest.owner == ""

    def test_lead_owner_survives_failed_daemon_claim(self, tmp_state_db):
        """Daemon's claim must NOT overwrite a lead-held ``owner`` column."""
        unit = _seed_unit(status="coding")
        state.claim_unit_owner(unit.unit_id, state.LEAD_OWNER, expected_owner="")
        try:
            ok = state.claim_unit_owner(unit.unit_id, daemon.DAEMON_OWNER, expected_owner="")
            assert ok is False
            assert state.get_unit_state(unit.unit_id).owner == state.LEAD_OWNER
        finally:
            state.release_unit_owner(unit.unit_id, expected_owner=state.LEAD_OWNER)


class TestF014EngineReuse:
    """Spec § "No parallel state machine" — daemon delegates to the
    F-014 ``_apply_action`` + ``_probe_and_decide`` helpers."""

    def test_apply_health_action_routes_through_tools_health(self, tmp_state_db, monkeypatch):
        """Patch ``tools.health._apply_action`` to a spy; the daemon's
        ``_apply_health_action`` must call it (not reimplement)."""
        unit = _seed_unit(status="in_ci", pr_number=42)
        action = Action.transition("done", "merged")
        spy: list[tuple] = []

        def _spy_apply(state_obj, act):
            spy.append((state_obj.unit_id, act.kind, act.target_status))

        from orchestrator.tools import health as tools_health

        monkeypatch.setattr(tools_health, "_apply_action", _spy_apply)
        ok = daemon._apply_health_action(unit, action)
        assert ok is True
        assert spy == [(unit.unit_id, "transition", "done")]

    def test_probe_and_decide_delegates_to_tools_health(self, tmp_state_db, monkeypatch):
        """Patch ``tools.health._probe_and_decide`` and confirm the daemon
        adapts its ``actions_to_apply`` list.

        Uses a real :class:`~orchestrator.health.HealthReport` (not a
        stub) because the daemon's ``_probe_and_decide_unit`` now also
        persists ``decision.shadow_decisions`` + the rate-limited
        ``health_report_snapshot`` (F-016 spec § "F-015 absorption
        clause", PR #61 reviewer H1) — both reuse F-014's
        :func:`~orchestrator.tools.health._maybe_record_snapshot`
        which calls ``dataclasses.asdict(report)``. A bare class stub
        wouldn't satisfy ``asdict``; we build a minimal real report
        so the test exercises the delegation path end-to-end.
        """
        from orchestrator.health import (
            CISnapshot,
            Decision,
            GitSnapshot,
            HealthReport,
            OrchestratorSnapshot,
            ReviewSnapshot,
        )
        from orchestrator.tools import health as tools_health

        unit = _seed_unit(status="in_ci", pr_number=42)
        sentinel_action = Action.transition("done", "via probe")
        fake_report = HealthReport(
            unit_id=unit.unit_id,
            pr=None,
            git=GitSnapshot(
                ahead_by=None,
                behind_by=None,
                head_sha=None,
                head_age_seconds=None,
                last_force_push_at=None,
            ),
            ci=CISnapshot(runs=[], pending=[], failing=[], required=[], missing_required=[]),
            reviews=ReviewSnapshot(
                approvals=0,
                changes_requested=0,
                dismissed=0,
                unresolved_threads=0,
                codeowner_requested=[],
                copilot_present=False,
                copilot_state=None,
            ),
            workers=[],
            orchestrator=OrchestratorSnapshot(
                cycle=0,
                cycle_cap=3,
                cycles_remaining=3,
                last_activity="",
                last_activity_age_seconds=None,
                downstream_blocked=0,
            ),
        )
        monkeypatch.setattr(
            tools_health,
            "_probe_and_decide",
            lambda _u, _repo: (
                fake_report,
                Decision(actions_to_apply=[sentinel_action], shadow_decisions=[]),
            ),
        )
        actions = daemon._probe_and_decide_unit(unit)
        assert actions == [sentinel_action]


class TestActiveUnitQuery:
    """The daemon reconciles the right rows on each tick."""

    def test_includes_all_active_statuses(self, tmp_state_db):
        state.save_feature(Feature(id="F-A", title="t", description="d"))
        for status in ACTIVE_UNIT_STATUSES | READY_TO_MERGE_STATUSES:
            state.upsert_unit_state(
                WorkUnitState(unit_id=f"U-{status}", feature_id="F-A", status=status)
            )
        ids = {u.unit_id for u in state.list_active_units()}
        expected = {f"U-{s}" for s in (ACTIVE_UNIT_STATUSES | READY_TO_MERGE_STATUSES)}
        assert ids == expected

    def test_excludes_terminal_cancelled_pending(self, tmp_state_db):
        state.save_feature(Feature(id="F-A", title="t", description="d"))
        for status in ("pending", "done", "escalated", "cancelled"):
            state.upsert_unit_state(
                WorkUnitState(unit_id=f"U-{status}", feature_id="F-A", status=status)
            )
        assert state.list_active_units() == []


class TestExceptionSafety:
    """A broken row must NOT freeze the workspace's loop."""

    def test_per_unit_exception_does_not_crash_reconcile_once(self, tmp_state_db, monkeypatch):
        state.save_feature(Feature(id="F-A", title="t", description="d"))
        state.upsert_unit_state(WorkUnitState(unit_id="U-1", feature_id="F-A", status="coding"))
        state.upsert_unit_state(WorkUnitState(unit_id="U-2", feature_id="F-A", status="coding"))

        seen: list[str] = []

        def _maybe_boom(unit_id: str) -> None:
            seen.append(unit_id)
            if unit_id == "U-1":
                raise RuntimeError("simulated tail error")

        monkeypatch.setattr(daemon, "reconcile_unit", _maybe_boom)
        assert daemon.reconcile_once() == 2
        # Both units attempted, despite U-1 raising.
        assert set(seen) == {"U-1", "U-2"}

    def test_tail_exception_does_not_crash_unit_reconcile(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="coding", sessions={"coder": "sess_c"})

        class _Boom:
            def tail_messages(self, *_a, **_k):
                raise ConnectionError("flaky backend")

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _Boom())
        _stub_no_probe(monkeypatch)
        # Must not raise — exception is caught inside _scan_role.
        daemon.reconcile_unit(unit.unit_id)
        # No transition applied (no marker observed).
        assert state.get_unit_state(unit.unit_id).status == "coding"

    def test_probe_error_does_not_crash_unit_reconcile(self, tmp_state_db, monkeypatch):
        """A GitHub-side error from F-014's probe must NOT freeze the unit."""
        unit = _seed_unit(status="in_ci", pr_number=42)

        from orchestrator.tools import health as tools_health

        # Production helper returns a string on error; the daemon converts
        # it to an empty actions list.
        monkeypatch.setattr(tools_health, "_probe_and_decide", lambda _u, _r: "ERROR: GH 500")
        _stub_no_workers(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        # Unit untouched (no action applied).
        assert state.get_unit_state(unit.unit_id).status == "in_ci"


class TestEnvKnobs:
    """Poll interval & drive env vars are read at call time, not module load."""

    def test_poll_interval_default(self, monkeypatch):
        monkeypatch.delenv(daemon.POLL_INTERVAL_ENV, raising=False)
        loop = daemon.DaemonLoop()
        assert loop.poll_interval_s == daemon.POLL_INTERVAL_DEFAULT_S

    def test_poll_interval_override(self, monkeypatch):
        monkeypatch.setenv(daemon.POLL_INTERVAL_ENV, "2.5")
        loop = daemon.DaemonLoop()
        assert loop.poll_interval_s == 2.5


class TestNamedConstants:
    """Lock-in the public surface names downstream callers (and F-016-U-6)
    will depend on."""

    def test_owner_strings_distinct(self):
        assert daemon.DAEMON_OWNER == "daemon"
        assert state.LEAD_OWNER == "lead"
        assert daemon.DAEMON_OWNER != state.LEAD_OWNER

    def test_drive_env_name(self):
        assert daemon.DAEMON_DRIVE_ENV == "ORCH_DAEMON_DRIVE"

    def test_default_stale_window(self):
        # The 30s window matches the spec's heartbeat budget.
        assert state.DEFAULT_DAEMON_LOCK_STALE_AFTER_S == 30


class TestConcurrentDaemonsContend:
    """Spec § acceptance: "Two daemons cannot run simultaneously for the
    same state.db". We exercise the contention via the lock primitives
    directly — running two real ``DaemonLoop.run`` would race the
    test harness."""

    def test_second_daemon_run_is_noop(self, tmp_state_db, monkeypatch):
        """Second daemon's ``run`` returns :data:`~orchestrator.daemon.EXIT_LOCK_HELD`
        (PR #61 reviewer M2) — distinguishable from a clean-shutdown ``0``."""
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        # First daemon takes the lock.
        first = daemon.claim_singleton(holder_id="first")
        assert first is not None
        # Second daemon tries to run — should be a no-op.
        second = daemon.DaemonLoop(holder_id="second", poll_interval_s=0.01)
        ticks = second.run()
        assert ticks == daemon.EXIT_LOCK_HELD
        # First daemon's lock untouched.
        path = str(state.STATE_DB.resolve())
        assert state.get_daemon_lock(path)["holder_id"] == "first"


class TestSpecRequiredColumns:
    """Spec § "State.db additions": ``cancelled_at`` / ``owner`` /
    ``daemon_locks`` — daemon depends on these existing post-init."""

    def test_daemon_locks_table_exists(self, tmp_state_db):
        import sqlite3 as _sql

        with _sql.connect(state.STATE_DB) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daemon_locks'"
            ).fetchone()
        assert row is not None

    def test_work_units_owner_and_cancelled_at_columns_present(self, tmp_state_db):
        import sqlite3 as _sql

        with _sql.connect(state.STATE_DB) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(work_units)").fetchall()}
        assert "cancelled_at" in cols
        assert "owner" in cols


class TestReconcileUnitGuardOrdering:
    """The guards must short-circuit IN ORDER: missing → cancelled →
    terminal → advance-lock. This ordering keeps the lock check cheap
    (it's a SQL UPDATE) when an earlier guard already caught the unit."""

    def test_missing_row_returns_silently(self, tmp_state_db):
        # No raise, no side effect — just a quiet skip.
        daemon.reconcile_unit("NO-SUCH-UNIT")

    def test_cancelled_unit_skipped_before_lock_check(self, tmp_state_db, monkeypatch):
        """A cancelled unit must NOT call has_active_advance_lock — that
        would do an unnecessary SQL read on every tick of every cancelled
        unit. The cancelled_at check is the cheap early-out."""
        unit = _seed_unit(status="coding")
        state.cancel_unit(unit.unit_id)
        called: list[str] = []
        monkeypatch.setattr(state, "has_active_advance_lock", lambda u: called.append(u) or False)
        _stub_no_workers(monkeypatch)
        _stub_no_probe(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        # Lock check never fires — the cancelled guard caught it.
        assert called == []


class TestThreadedLoopShutdown:
    """``DaemonLoop.stop`` releases the lock cleanly even from another thread."""

    def test_stop_from_another_thread_releases_lock(self, tmp_state_db, monkeypatch):
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        monkeypatch.setattr(daemon, "reconcile_once", lambda: 0)
        loop = daemon.DaemonLoop(poll_interval_s=0.05, holder_id="us")

        def runner() -> None:
            loop.run()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        # Give the loop a moment to claim the lock + start ticking.
        for _ in range(50):
            if state.get_daemon_lock(str(state.STATE_DB.resolve())) is not None:
                break
            time.sleep(0.01)
        loop.stop()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        # Lock released on shutdown.
        assert state.get_daemon_lock(str(state.STATE_DB.resolve())) is None


# --------------------------- type-shape checks ---------------------------


class TestPublicSurfaceShape:
    """Lock in the public callable surface F-016-U-6 will consume."""

    @pytest.mark.parametrize(
        "name",
        [
            "is_drive_enabled",
            "claim_singleton",
            "release_singleton",
            "heartbeat",
            "reconcile_unit",
            "reconcile_once",
            "run_daemon",
            "DaemonLoop",
            "DaemonHandle",
            "DAEMON_OWNER",
            "DAEMON_DRIVE_ENV",
            "POLL_INTERVAL_DEFAULT_S",
        ],
    )
    def test_attribute_present(self, name):
        assert hasattr(daemon, name), f"daemon.{name} missing"

    def test_marker_spec_target_status_drives_transitions(self):
        """A new marker added to ``orchestrator.markers`` whose
        ``target_status`` is in the active set will flip when the daemon
        observes it. This ties the daemon to the published grammar rather
        than to an internal marker-name set the two could drift on."""
        for role, text, expected_target in [
            ("coder", "PR_URL: https://github.com/o/r/pull/1", "in_ci"),
            ("coder", "FIX_PUSHED", "in_ci"),
            ("tester", "TESTS_PASS", "in_ci"),
            (
                "reviewer",
                "REVIEW_RECOMMEND_MERGE: clean",
                "approved_awaiting_merge",
            ),
            ("reviewer", "REVIEW_COMMENT", "in_ci"),
        ]:
            spec: MarkerSpec | None = markers.scan_response(role, text)
            assert spec is not None
            assert spec.target_status == expected_target
