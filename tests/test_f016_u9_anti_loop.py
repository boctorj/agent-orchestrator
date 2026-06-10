"""F-016-U-9 — Anti-loop hardening: ghost-row guard + retry cap + ci_drift dedupe.

Pins the three defects from the 2026-06-10 incident triage:

1. **Ghost-row guard.** ``spawn_unit`` / ``spawn_unit_async`` refuse to
   re-spawn a row already in ``{coding, opening_pr, in_ci, testing,
   reviewing, fixing, escalated}``. Pre-U-9 the only guard was a
   non-empty ``coder_session_id`` check, which a failed blocking spawn
   never set — so the row stayed re-spawnable forever (root cause of
   the 12h re-spawn loop).
2. **Attempt cap.** After ``SPAWN_FAILURE_CAP`` (3) consecutive
   ``coder_error`` events at ``cycle_number=0`` with no persisted
   session, the unit is force-escalated, an ntfy push fires, and
   further auto-spawn is refused. Counter resets once a spawn
   successfully persists a ``session_id``.
3. **ci_drift_detected dedupe.** A unit parked ``in_ci`` with a
   persistently-red CI no longer generates one ``ci_drift_detected``
   row + GitHub-API hit per ~6s daemon tick. Emitted only when the
   failing-check-set changes from the last recorded drift OR the
   prior row is older than ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator import state
from orchestrator.health import Action
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution
from orchestrator.tools import health as tools_health

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _force_verified(monkeypatch, tmp_state_db):
    """Bypass the verify_repo gate — these tests exercise the U-9
    anti-loop primitives, not the verification surface."""
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)


@pytest.fixture(autouse=True)
def _fake_github_token(monkeypatch):
    """``need_github_token`` must pass without exposing a real PAT."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_fake_for_tests")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _stub_github(monkeypatch):
    """Don't touch real GitHub on spawn paths."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")


def _seed_feature(feature_id: str = "F-016") -> None:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
            branch_prefix="u9",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=f"{feature_id}-U-1",
                feature_id=feature_id,
                title="u1",
                description="impl",
            )
        ],
    )
    state.approve_plan(feature_id)


# --------------------------- fakes ---------------------------


class _FailingWorker:
    """Blocking ``ManagedAgentWorker`` stub whose ``spawn`` always raises.

    Mirrors the 2026-06-10 failure mode: a worker-backend network read
    timeout that dies before any session id is returned. Records call
    counts so tests can verify the cap was checked.
    """

    spawn_calls = 0

    def __init__(self, role: str) -> None:
        self.role = role

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        _FailingWorker.spawn_calls += 1
        raise RuntimeError("read timeout (network)")


class _FailingAsyncWorker:
    """``make_worker`` stub whose ``spawn_async`` always raises."""

    spawn_async_calls = 0

    def __init__(self, role: str) -> None:
        self.role = role

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        _FailingAsyncWorker.spawn_async_calls += 1
        raise RuntimeError("read timeout (network)")


class _SuccessAsyncWorker:
    """``make_worker`` stub whose ``spawn_async`` returns a fresh session id.

    Used to test the counter-reset path: a successful spawn after N
    prior failures must zero the cap counter.
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.calls += 1
        return f"sesn-success-{self.calls}"


def _install_failing_blocking_worker(monkeypatch) -> None:
    _FailingWorker.spawn_calls = 0
    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _FailingWorker)


def _install_failing_async_worker(monkeypatch) -> None:
    _FailingAsyncWorker.spawn_async_calls = 0
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", _FailingAsyncWorker)


# ============================================================================
# (1) Ghost-row guard — re-spawn refused on each active/escalated status
# ============================================================================


class TestGhostRowGuardRefusesActiveStatus:
    """Status-based guard: every status in
    :data:`execution._RESPAWN_REFUSED_STATUSES` must refuse a re-spawn.
    """

    REFUSED = ("coding", "opening_pr", "in_ci", "testing", "reviewing", "fixing", "escalated")

    @pytest.mark.parametrize("status", REFUSED)
    def test_spawn_unit_refuses(self, tmp_state_db, monkeypatch, status):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status=status,
                branch="b",
            )
        )
        # Even with no coder_session_id (the ghost-row case), refuse.
        _install_failing_blocking_worker(monkeypatch)

        msg = execution.spawn_unit("F-016", "F-016-U-1")

        assert "ERROR" in msg
        assert f"status={status!r}" in msg
        # Caller is pointed at the right recovery surfaces.
        assert "cancel_unit" in msg
        assert "inspect_unit_health" in msg
        # Worker was NEVER invoked — guard fired pre-dispatch.
        assert _FailingWorker.spawn_calls == 0

    @pytest.mark.parametrize("status", REFUSED)
    def test_spawn_unit_async_refuses(self, tmp_state_db, monkeypatch, status):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status=status,
                branch="b",
            )
        )
        _install_failing_async_worker(monkeypatch)

        msg = execution.spawn_unit_async("F-016", "F-016-U-1")

        assert "ERROR" in msg
        assert f"status={status!r}" in msg
        assert _FailingAsyncWorker.spawn_async_calls == 0

    def test_refusal_does_not_clobber_existing_session(self, tmp_state_db, monkeypatch):
        """Guard must not upsert a fresh row that wipes ``coder_session_id``.

        The OLD guard's purpose was specifically to preserve an existing
        session — the new status-based guard must keep the same
        invariant on the happy-path-of-the-old-check (session populated,
        status active).
        """
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status="coding",
                branch="b",
                coder_session_id="sesn-precious",
            )
        )
        _install_failing_async_worker(monkeypatch)

        execution.spawn_unit_async("F-016", "F-016-U-1")

        row = state.get_unit_state("F-016-U-1")
        assert row is not None
        assert row.coder_session_id == "sesn-precious"
        assert row.status == "coding"


# ============================================================================
# (1b) Clean first spawn still works
# ============================================================================


class TestCleanFirstSpawnSucceeds:
    """No row, or a row with a non-active status (``pending`` /
    ``cancelled`` / ``done``), accepts a fresh spawn — the guard is
    a *refusal* of active state, not a blanket re-dispatch ban.
    """

    def test_no_row_succeeds(self, tmp_state_db, monkeypatch):
        _seed_feature()
        worker = _SuccessAsyncWorker("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

        msg = execution.spawn_unit_async("F-016", "F-016-U-1")

        # The result is JSON on the happy path; no error.
        assert "ERROR" not in msg
        row = state.get_unit_state("F-016-U-1")
        assert row is not None
        assert row.coder_session_id.startswith("sesn-success-")

    @pytest.mark.parametrize("status", ("pending", "cancelled", "done", "approved_awaiting_merge"))
    def test_non_active_row_succeeds(self, tmp_state_db, monkeypatch, status):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status=status,
                branch="b",
            )
        )
        worker = _SuccessAsyncWorker("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

        msg = execution.spawn_unit_async("F-016", "F-016-U-1")

        assert "ERROR" not in msg
        row = state.get_unit_state("F-016-U-1")
        assert row is not None
        assert row.status == "coding"
        assert row.coder_session_id.startswith("sesn-success-")


# ============================================================================
# (1c) cancel_unit → re-dispatch works
# ============================================================================


def test_cancel_then_respawn_works(tmp_state_db, monkeypatch):
    """The documented recovery path: cancel a stuck unit, then re-dispatch.

    After ``cancel_unit`` the row is in ``cancelled`` (NOT in the
    refuse set), so a fresh ``spawn_unit_async`` succeeds and lands a
    new session id.
    """
    _seed_feature()
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-016-U-1",
            feature_id="F-016",
            status="coding",
            branch="b",
            coder_session_id="sesn-stuck",
        )
    )
    # Simulate cancel: flip status + sticky-stamp cancelled_at.
    assert state.cancel_unit("F-016-U-1") is True
    cancelled = state.get_unit_state("F-016-U-1")
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at is not None

    worker = _SuccessAsyncWorker("coder")
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

    msg = execution.spawn_unit_async("F-016", "F-016-U-1")

    assert "ERROR" not in msg
    row = state.get_unit_state("F-016-U-1")
    assert row is not None
    assert row.status == "coding"
    assert row.coder_session_id == "sesn-success-1"


# ============================================================================
# (2) Attempt cap — 3 consecutive failed spawns escalate + ntfy
# ============================================================================


class TestSpawnFailureCap:
    """After ``SPAWN_FAILURE_CAP`` consecutive ``coder_error`` events at
    cycle 0 with no persisted session, the unit force-escalates and
    ntfy fires. The loop self-terminates even if a caller bypasses the
    ghost-row guard between attempts.
    """

    def _failing_spawn_unit(self, monkeypatch):
        """Set up a worker whose ``spawn`` always raises and a unit row
        cleared each time so the ghost-row guard doesn't pre-empt.
        """
        _install_failing_blocking_worker(monkeypatch)

    def test_first_two_failures_do_not_escalate_via_cap(self, tmp_state_db, monkeypatch):
        """The cap fires on the THIRD consecutive failure, not earlier.

        The first two attempts escalate the unit via the existing
        ``except`` branch (a worker raise sets status=escalated + last_error),
        but the cap-specific path (``spawn_failure_cap_hit`` event + ntfy)
        is reserved for the third.
        """
        _seed_feature()
        self._failing_spawn_unit(monkeypatch)
        pushes: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda u, reason, *, reason_slug="unknown", **k: pushes.append(
                (u, reason, reason_slug)
            ),
        )

        # Attempt 1: fails → escalated, no cap push
        msg1 = execution.spawn_unit("F-016", "F-016-U-1")
        assert "ERROR spawning coder" in msg1
        # The ghost-row guard would refuse the next attempt — manually
        # clear the row to simulate a caller that bypasses it (the cap
        # is the BACKSTOP, independent of guard 1).
        state.cancel_unit("F-016-U-1")

        msg2 = execution.spawn_unit("F-016", "F-016-U-1")
        assert "ERROR spawning coder" in msg2

        events = state.list_events("F-016-U-1")
        cap_events = [e for e in events if e["event_type"] == "spawn_failure_cap_hit"]
        assert cap_events == []
        # Only the cap-3 fire pushes the cap-specific ntfy with the
        # ``spawn_failure_cap`` slug.
        cap_pushes = [p for p in pushes if p[2] == "spawn_failure_cap"]
        assert cap_pushes == []

    def test_third_failure_escalates_via_cap_and_pushes(self, tmp_state_db, monkeypatch):
        _seed_feature()
        self._failing_spawn_unit(monkeypatch)
        pushes: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda u, reason, *, reason_slug="unknown", **k: pushes.append(
                (u, reason, reason_slug)
            ),
        )

        # Bypass the ghost-row guard between each attempt so the cap is
        # what fires, not guard 1.
        for _ in range(3):
            execution.spawn_unit("F-016", "F-016-U-1")
            state.cancel_unit("F-016-U-1")

        events = state.list_events("F-016-U-1")
        cap_hits = [e for e in events if e["event_type"] == "spawn_failure_cap_hit"]
        # Exactly one cap event (the 3rd failure's same-attempt re-check
        # OR a subsequent 4th call — but the 3rd attempt's in-call
        # re-check fires the cap immediately).
        assert len(cap_hits) == 1
        assert "cap-3" in cap_hits[0]["summary"]

        cap_pushes = [p for p in pushes if p[2] == "spawn_failure_cap"]
        assert len(cap_pushes) == 1
        assert cap_pushes[0][0] == "F-016-U-1"

    def test_cap_refuses_further_spawn(self, tmp_state_db, monkeypatch):
        """Once the cap is hit and the unit is escalated, a subsequent
        spawn call must NOT auto-resurrect the unit — guard 1 catches
        ``status=escalated``.
        """
        _seed_feature()
        self._failing_spawn_unit(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        for i in range(3):
            execution.spawn_unit("F-016", "F-016-U-1")
            # Cancel between attempts so guard 1 doesn't pre-empt the
            # cap check, but NOT after the final iteration — the cap's
            # ``status=escalated`` is what we're asserting on.
            if i < 2:
                state.cancel_unit("F-016-U-1")

        # After the cap fires on attempt 3, status is ``escalated``.
        post_cap = state.get_unit_state("F-016-U-1")
        assert post_cap.status == "escalated"

        # The next spawn call must be refused by the ghost-row guard.
        msg = execution.spawn_unit("F-016", "F-016-U-1")
        assert "ERROR" in msg
        assert "status='escalated'" in msg

    def test_cap_counter_resets_after_session_persisted(self, tmp_state_db, monkeypatch):
        """Counter resets once a spawn writes a session_id.

        After 2 failed attempts → 1 successful async spawn (persists
        session_id) → the next attempt's counter is back to zero.
        """
        _seed_feature()
        pushes: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda u, reason, *, reason_slug="unknown", **k: pushes.append(
                (u, reason, reason_slug)
            ),
        )

        # 2 failed blocking spawns (each followed by cancel to bypass guard 1).
        _install_failing_blocking_worker(monkeypatch)
        for _ in range(2):
            execution.spawn_unit("F-016", "F-016-U-1")
            state.cancel_unit("F-016-U-1")
        assert execution._consecutive_failed_spawns("F-016-U-1") == 2

        # Switch to a successful async worker → it persists session_id.
        worker = _SuccessAsyncWorker("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)
        msg = execution.spawn_unit_async("F-016", "F-016-U-1")
        assert "ERROR" not in msg

        # Counter is now zero — the session_id-bearing row is the reset signal.
        assert execution._consecutive_failed_spawns("F-016-U-1") == 0

        # No cap escalation fired across the run.
        cap_pushes = [p for p in pushes if p[2] == "spawn_failure_cap"]
        assert cap_pushes == []

    def test_async_path_also_caps_at_three(self, tmp_state_db, monkeypatch):
        """The async spawn path enforces the same cap-3 contract."""
        _seed_feature()
        _install_failing_async_worker(monkeypatch)
        pushes: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda u, reason, *, reason_slug="unknown", **k: pushes.append(
                (u, reason, reason_slug)
            ),
        )

        for _ in range(3):
            execution.spawn_unit_async("F-016", "F-016-U-1")
            state.cancel_unit("F-016-U-1")

        cap_hits = [
            e for e in state.list_events("F-016-U-1") if e["event_type"] == "spawn_failure_cap_hit"
        ]
        assert len(cap_hits) == 1
        cap_pushes = [p for p in pushes if p[2] == "spawn_failure_cap"]
        assert len(cap_pushes) == 1


# ============================================================================
# (3) ci_drift_detected dedupe — same failing-set within window is suppressed
# ============================================================================


def _seed_unit_for_drift(status: str = "approved_awaiting_merge"):
    """Seed a unit + feature suitable for direct ``_apply_action`` calls."""
    state.save_feature(
        Feature(
            id="F-016",
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
        )
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-016-U-1",
            feature_id="F-016",
            status=status,
            branch="b",
            pr_number=5,
        )
    )
    return state.get_unit_state("F-016-U-1")


def _drift_action(failing: list[str], status: str = "approved_awaiting_merge") -> Action:
    """Build the same ``ci_drift_detected`` action shape ``_ci_drift_event``
    emits, so the dedupe test is invariant to the producer's wording."""
    return Action.event(
        "ci_drift_detected",
        f"CI red while status={status!r}",
        details=f"failing checks: {', '.join(failing)}",
        set_last_error=f"CI drift: {', '.join(failing)} failing",
        payload={"failing": list(failing), "status": status},
    )


class TestCiDriftDedupe:
    def test_first_emit_lands(self, tmp_state_db):
        """A unit with no prior ``ci_drift_detected`` always fires on
        the first emit — there's nothing to dedupe against."""
        unit = _seed_unit_for_drift()
        action = _drift_action(["test"])
        tools_health._apply_action(unit, action)

        drift_rows = [
            e for e in state.list_events("F-016-U-1") if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 1

    def test_unchanged_set_within_window_suppressed(self, tmp_state_db, monkeypatch):
        """A persistently-red PR with the SAME failing check-set must
        not re-emit on subsequent probes. Spec § U-9: drift is emitted
        only when the failing-check-set changes from the last recorded
        drift for that unit.
        """
        # Force a large rate-limit window so the second emit can't slip
        # through on a clock-edge case.
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        unit = _seed_unit_for_drift()

        for _ in range(10):
            tools_health._apply_action(unit, _drift_action(["test"]))

        drift_rows = [
            e for e in state.list_events("F-016-U-1") if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 1

    def test_changed_set_re_emits(self, tmp_state_db, monkeypatch):
        """If the failing-check-set evolves (e.g. a new check goes red,
        or one recovers), drift IS re-emitted — the dedupe is by
        content, not by event-type."""
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        unit = _seed_unit_for_drift()

        tools_health._apply_action(unit, _drift_action(["test"]))
        tools_health._apply_action(unit, _drift_action(["test", "lint"]))
        tools_health._apply_action(unit, _drift_action(["test", "lint"]))  # unchanged → no re-emit
        tools_health._apply_action(unit, _drift_action(["lint"]))  # changed → re-emit

        drift_rows = [
            e for e in state.list_events("F-016-U-1") if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 3
        # Each row carries the failing set that fired it.
        details = [r["details"] for r in drift_rows]
        assert "failing checks: test" in details
        assert "failing checks: test, lint" in details
        assert "failing checks: lint" in details

    def test_unchanged_set_past_window_re_emits(self, tmp_state_db, monkeypatch):
        """Even with an unchanged set, drift re-emits once the rate-limit
        window has expired — so an operator sees the recurring drift in
        a daily-ish digest rather than silently never again.
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "1")
        unit = _seed_unit_for_drift()

        tools_health._apply_action(unit, _drift_action(["test"]))
        # Backdate the first drift row by 2 hours so the window is past.
        with state._connect() as conn:
            two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            conn.execute(
                "UPDATE unit_events SET ts = ? "
                "WHERE unit_id = ? AND event_type = 'ci_drift_detected'",
                (two_hours_ago, "F-016-U-1"),
            )

        tools_health._apply_action(unit, _drift_action(["test"]))

        drift_rows = [
            e for e in state.list_events("F-016-U-1") if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 2

    def test_dedupe_suppresses_last_error_rewrite(self, tmp_state_db, monkeypatch):
        """When the event is deduped, ``set_last_error`` must NOT fire
        either — otherwise the dedupe shrinks unit_events but still
        hammers ``work_units`` writes on every tick.
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        unit = _seed_unit_for_drift()

        tools_health._apply_action(unit, _drift_action(["test"]))
        # Sentinel: a different last_error written between drifts.
        state.touch_unit("F-016-U-1", error="sentinel-from-other-path")

        # A second drift with the same set must NOT overwrite the sentinel.
        tools_health._apply_action(unit, _drift_action(["test"]))

        refreshed = state.get_unit_state("F-016-U-1")
        assert refreshed.last_error == "sentinel-from-other-path"

    def test_dedupe_holds_for_inspect_unit_health(self, tmp_state_db, monkeypatch):
        """The dedupe lives at the ``_apply_action`` layer, so it covers
        the ``inspect_unit_health`` surface AND the daemon's
        ``_apply_health_action`` (both route through ``_apply_action``).

        Repeated ``inspect_unit_health`` calls on a persistently-red PR
        must produce exactly one ``ci_drift_detected`` row (the first
        emit) within the rate-limit window.
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_unit_for_drift()
        # Stub GitHub: red CI, mergeable PR (so the F-014 decision table
        # fires ``ci_drift_detected`` for an ``approved_awaiting_merge``
        # row, matching the incident profile).
        monkeypatch.setattr(
            "orchestrator.tools.health.github.get_pr_state",
            lambda url, pr: {
                "state": "open",
                "merged": False,
                "head_sha": "abc",
                "mergeable": True,
                "mergeable_state": "clean",
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.health.github.get_pr_check_runs",
            lambda url, pr: {
                "total": 1,
                "conclusion_counts": {"failure": 1},
                "runs": [
                    {
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "failure",
                        "details_url": "",
                    }
                ],
            },
        )

        for _ in range(5):
            tools_health.inspect_unit_health("F-016-U-1")

        drift_rows = [
            e for e in state.list_events("F-016-U-1") if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 1

    def test_dedupe_holds_for_daemon_tick(self, tmp_state_db, monkeypatch):
        """The daemon's ``_apply_health_action`` routes through the same
        ``_apply_action`` helper as ``inspect_unit_health``, so a unit
        sitting ``in_ci`` with a persistently-red PR across repeated
        ``reconcile_unit`` ticks produces exactly one
        ``ci_drift_detected`` event within the rate-limit window —
        cutting the ~600 probes/hr × N REST calls incident profile.
        """
        from orchestrator import daemon

        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_unit_for_drift(status="approved_awaiting_merge")
        # Have the F-014 probe return the same drift action each tick.
        drift = _drift_action(["test"])
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [drift])
        # Skip worker calls entirely — marker scan is irrelevant here.
        monkeypatch.setattr(
            "orchestrator.daemon.make_worker",
            lambda _r: type("W", (), {"tail_messages": lambda *a, **k: []})(),
        )

        for _ in range(10):
            daemon.reconcile_unit("F-016-U-1")

        drift_rows = [
            e for e in state.list_events("F-016-U-1") if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 1
