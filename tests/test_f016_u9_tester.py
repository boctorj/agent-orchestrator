"""F-016-U-9 tester — independent spec-driven tests.

Independent black-box tests for the U-9 anti-loop hardening:

  (1) **Ghost-row guard** — ``spawn_unit`` / ``spawn_unit_async`` MUST
      refuse re-spawn on every status in ``ACTIVE_UNIT_STATUSES``
      *plus* ``escalated``, returning an actionable error pointing
      the caller at ``inspect_unit_health`` / ``resume_unit`` /
      ``cancel_unit``. A clean first spawn (no row, or a fresh /
      non-active row) must still work, and ``cancel_unit → re-dispatch``
      must work as the documented recovery path.

  (2) **Attempt cap (cap-3)** — three consecutive failed coder spawns
      at cycle 0 with no persisted ``session_id`` MUST force the unit
      to ``escalated`` (+ ``last_error``), emit a single
      ``spawn_failure_cap_hit`` event, and fire one ntfy escalation
      push with ``reason_slug="spawn_failure_cap"``. Counter MUST
      reset once any spawn path persists a session_id.

  (3) **ci_drift_detected dedupe** — a persistently-red CI MUST emit
      ``ci_drift_detected`` at most once for an unchanged failing-
      check-set within ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS``. A
      changed failing set re-emits; an unchanged set past the
      window re-emits. Dedupe lives at the shared ``_apply_action``
      layer so both ``inspect_unit_health`` and the daemon's
      reconcile tick respect it.

These tests do not rely on the coder's :mod:`test_f016_u9_anti_loop`
file — they re-derive every fixture and assertion from the spec so
the two suites cross-check each other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator import state
from orchestrator.health import Action
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    Feature,
    WorkUnit,
    WorkUnitState,
)
from orchestrator.tools import execution
from orchestrator.tools import health as tools_health

# ---------------------------------------------------------------------------
# Constants from the spec
# ---------------------------------------------------------------------------

FEATURE_ID = "F-016"
UNIT_ID = "F-016-U-1"
REPO_URL = "https://github.com/o/r"
SPAWN_CAP_EXPECTED = 3  # spec § attempt cap
GUARDED_STATUSES = sorted(ACTIVE_UNIT_STATUSES | {"escalated"})
NON_GUARDED_STATUSES = ("pending", "cancelled", "done", "approved_awaiting_merge")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_verify_gate(monkeypatch, tmp_state_db):
    """``ensure_verified_for_feature`` is enforced before any spawn —
    bypass it so the test exercises the U-9 guard, not the verify gate."""
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    """Satisfy ``need_github_token`` without exposing a real PAT."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_tester_stub")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _silence_gh_amend(monkeypatch):
    """``safe_amend_pr_body`` shells out to real ``gh`` — stub it."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")


def _seed_plan(feature_id: str = FEATURE_ID, unit_id: str = UNIT_ID) -> None:
    """Persist a minimal approved feature + one-unit plan suitable for spawn."""
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=REPO_URL,
            status="approved",
            branch_prefix="tester",
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title="u", description="d")],
    )
    state.approve_plan(feature_id)


def _seed_row(unit_id: str, status: str, **kwargs) -> None:
    """Force the unit row into ``status`` (bypassing normal transitions)."""
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=FEATURE_ID,
            status=status,
            branch="b",
            **kwargs,
        )
    )


# ---------------------------------------------------------------------------
# Worker doubles
# ---------------------------------------------------------------------------


class _AlwaysFailingBlockingWorker:
    """``ManagedAgentWorker`` double whose ``spawn`` raises every call.

    Mirrors the 2026-06-10 failure profile: a managed-agents network
    read timeout that dies before any session id is returned. The class-
    level ``calls`` counter lets tests assert pre-dispatch refusals.
    """

    calls = 0

    def __init__(self, role: str) -> None:
        self.role = role

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        type(self).calls += 1
        raise RuntimeError("read timeout (network)")


class _AlwaysFailingAsyncWorker:
    """``make_worker`` double whose ``spawn_async`` raises every call."""

    calls = 0

    def __init__(self, role: str) -> None:
        self.role = role

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        type(self).calls += 1
        raise RuntimeError("read timeout (network)")


class _SucceedingAsyncWorker:
    """``make_worker`` double whose ``spawn_async`` returns a fresh session.

    Used as the counter-reset signal: a successful async spawn writes
    both the ``coder_session_id`` column AND a ``coder_session_persisted``
    event, either of which zeroes :func:`_consecutive_failed_spawns`.
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.calls += 1
        return f"sesn-tester-{self.calls}"


class _SucceedingBlockingWorker:
    """``ManagedAgentWorker`` double that emits a clean ``PR_URL`` marker.

    The blocking happy path persists the session_id via ``record_event(
    pr_opened, session_id=session_id)``; that row is what the counter
    walks back to as a reset signal.
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        self.calls += 1
        return (
            f"sesn-block-{self.calls}",
            f"...\nPR_URL: https://github.com/o/r/pull/{99 + self.calls}\n",
        )


def _capture_pushes(monkeypatch) -> list[dict]:
    """Capture every ``ntfy.push_escalation`` call as a structured dict.

    Returns the list, which tests assert on. ``reason_slug`` defaults
    match the production signature so a caller that omits the slug
    surfaces as ``"unknown"`` (which our cap-3 path explicitly does NOT
    use — it passes ``reason_slug="spawn_failure_cap"``).
    """
    pushes: list[dict] = []

    def _capture(unit_id: str, reason: str, *args, reason_slug: str = "unknown", **kwargs):
        pushes.append({"unit_id": unit_id, "reason": reason, "reason_slug": reason_slug})
        return True

    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", _capture)
    return pushes


# ===========================================================================
# (1) GHOST-ROW GUARD
# ===========================================================================


class TestGhostRowGuard:
    """Spec § Ghost-row guard.

    Refuse re-spawn when a row already exists in an active or escalated
    status, regardless of whether ``coder_session_id`` was ever
    persisted (the pre-U-9 guard checked only that column, which is the
    bug being fixed). The refusal must be actionable: chat-visible error
    pointing at the right recovery primitives.
    """

    @pytest.mark.parametrize("status", GUARDED_STATUSES)
    def test_blocking_spawn_refuses_active_status(self, tmp_state_db, monkeypatch, status):
        """``spawn_unit`` MUST refuse re-spawn on every active + escalated status."""
        _seed_plan()
        # No coder_session_id — the ghost-row scenario.
        _seed_row(UNIT_ID, status=status)
        _AlwaysFailingBlockingWorker.calls = 0
        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            _AlwaysFailingBlockingWorker,
        )

        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)

        assert msg.startswith("ERROR"), f"expected ERROR refusal, got: {msg!r}"
        assert repr(status) in msg, f"refusal must mention status {status!r}, got: {msg!r}"
        # Worker MUST NOT be invoked — guard fires pre-dispatch.
        assert _AlwaysFailingBlockingWorker.calls == 0

    @pytest.mark.parametrize("status", GUARDED_STATUSES)
    def test_async_spawn_refuses_active_status(self, tmp_state_db, monkeypatch, status):
        """``spawn_unit_async`` MUST mirror the blocking refusal."""
        _seed_plan()
        _seed_row(UNIT_ID, status=status)
        _AlwaysFailingAsyncWorker.calls = 0
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", _AlwaysFailingAsyncWorker)

        msg = execution.spawn_unit_async(FEATURE_ID, UNIT_ID)

        assert msg.startswith("ERROR")
        assert repr(status) in msg
        assert _AlwaysFailingAsyncWorker.calls == 0

    def test_refusal_message_points_at_recovery_surfaces(self, tmp_state_db, monkeypatch):
        """The error MUST point the caller at ``inspect_unit_health`` /
        ``resume_unit`` (check real state) and ``cancel_unit`` (reset).

        Spec § U-9: "REFUSE with an actionable error pointing the caller
        at inspect_unit_health / resume_unit (check real state) or
        cancel_unit (explicit reset before re-dispatch)."
        """
        _seed_plan()
        _seed_row(UNIT_ID, status="coding")
        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            _AlwaysFailingBlockingWorker,
        )

        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)

        assert "cancel_unit" in msg, "refusal must mention cancel_unit"
        # The spec mentions BOTH check primitives; at least one must
        # appear so the user knows how to verify before resetting.
        assert "inspect_unit_health" in msg or "resume_unit" in msg, (
            "refusal must mention inspect_unit_health or resume_unit"
        )

    def test_guard_preserves_existing_session_id(self, tmp_state_db, monkeypatch):
        """A refused re-spawn MUST NOT clobber ``coder_session_id``.

        The pre-U-9 guard's actual job was specifically to preserve a
        live session row; the new status-based guard must keep that
        invariant on the populated-session case.
        """
        _seed_plan()
        _seed_row(UNIT_ID, status="coding", coder_session_id="sesn-precious")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", _AlwaysFailingAsyncWorker)

        execution.spawn_unit_async(FEATURE_ID, UNIT_ID)

        row = state.get_unit_state(UNIT_ID)
        assert row.coder_session_id == "sesn-precious"
        assert row.status == "coding"


# ===========================================================================
# (1b) CLEAN FIRST SPAWN STILL WORKS
# ===========================================================================


class TestCleanFirstSpawnSucceeds:
    """Spec § "A clean first spawn (no row, or a fresh/non-active row)
    still works." The guard is a refusal of *active* state, not a
    blanket re-dispatch ban.
    """

    def test_no_row_spawn_succeeds(self, tmp_state_db, monkeypatch):
        """The lead's first spawn (no work_units row yet) must work."""
        _seed_plan()
        worker = _SucceedingAsyncWorker("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

        msg = execution.spawn_unit_async(FEATURE_ID, UNIT_ID)

        assert "ERROR" not in msg, f"first spawn must succeed, got: {msg!r}"
        row = state.get_unit_state(UNIT_ID)
        assert row is not None
        assert row.coder_session_id.startswith("sesn-tester-")
        assert worker.calls == 1

    @pytest.mark.parametrize("status", NON_GUARDED_STATUSES)
    def test_non_active_row_spawn_succeeds(self, tmp_state_db, monkeypatch, status):
        """``pending`` / ``cancelled`` / ``done`` / ``approved_awaiting_merge``
        all accept a fresh spawn — the guard targets in-flight rows only.
        """
        _seed_plan()
        _seed_row(UNIT_ID, status=status)
        worker = _SucceedingAsyncWorker("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

        msg = execution.spawn_unit_async(FEATURE_ID, UNIT_ID)

        assert "ERROR" not in msg, f"status={status!r}: {msg!r}"
        row = state.get_unit_state(UNIT_ID)
        assert row.status == "coding"
        assert row.coder_session_id.startswith("sesn-tester-")


# ===========================================================================
# (1c) cancel_unit → re-dispatch
# ===========================================================================


def test_cancel_then_redispatch_works(tmp_state_db, monkeypatch):
    """Spec § Tests: "After cancel_unit resets the row, re-dispatch must work."

    This is the documented recovery for a stuck unit: the user (or
    lead) calls ``cancel_unit`` to flip the row to ``cancelled``, then
    re-dispatches. The fresh spawn must succeed and persist a new
    session_id.
    """
    _seed_plan()
    _seed_row(UNIT_ID, status="coding", coder_session_id="sesn-stuck")

    # Cancel the stuck unit.
    assert state.cancel_unit(UNIT_ID) is True
    cancelled = state.get_unit_state(UNIT_ID)
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at is not None

    # Re-dispatch — the guard must NOT refuse a cancelled row.
    worker = _SucceedingAsyncWorker("coder")
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

    msg = execution.spawn_unit_async(FEATURE_ID, UNIT_ID)

    assert "ERROR" not in msg, f"re-dispatch must succeed after cancel: {msg!r}"
    refreshed = state.get_unit_state(UNIT_ID)
    assert refreshed.status == "coding"
    assert refreshed.coder_session_id == "sesn-tester-1"


# ===========================================================================
# (2) ATTEMPT CAP
# ===========================================================================


class TestSpawnFailureCap:
    """Spec § Attempt cap (cap-3).

    Three consecutive ``coder_error`` events at ``cycle_number=0`` with
    no persisted session_id MUST force-escalate the unit, fire one
    ntfy push, refuse further auto-spawn, AND record exactly one
    ``spawn_failure_cap_hit`` audit event. The counter MUST reset once
    a spawn successfully persists a session_id.
    """

    def _force_failures(self, monkeypatch, *, n: int, bypass_guard_between: bool = True) -> None:
        """Drive ``n`` consecutive failed blocking spawns. The ghost-row
        guard would otherwise refuse attempts 2..n; ``cancel_unit``
        between attempts simulates a caller bypassing guard 1 so the
        cap (guard 2) is what we're testing.
        """
        _AlwaysFailingBlockingWorker.calls = 0
        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            _AlwaysFailingBlockingWorker,
        )
        for i in range(n):
            execution.spawn_unit(FEATURE_ID, UNIT_ID)
            if bypass_guard_between and i < n - 1:
                state.cancel_unit(UNIT_ID)

    def test_cap_does_not_fire_before_threshold(self, tmp_state_db, monkeypatch):
        """Two failed spawns MUST NOT fire the cap-specific path.

        Each failed spawn DOES escalate the unit via the worker-raise
        ``except`` branch, but the cap-3 surface (``spawn_failure_cap_hit``
        event + ``reason_slug="spawn_failure_cap"`` ntfy push) is
        reserved for the threshold attempt.
        """
        _seed_plan()
        pushes = _capture_pushes(monkeypatch)
        self._force_failures(monkeypatch, n=SPAWN_CAP_EXPECTED - 1)

        cap_events = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "spawn_failure_cap_hit"
        ]
        cap_pushes = [p for p in pushes if p["reason_slug"] == "spawn_failure_cap"]
        assert cap_events == [], f"cap event must NOT fire before threshold: {cap_events!r}"
        assert cap_pushes == [], f"cap ntfy must NOT fire before threshold: {cap_pushes!r}"

    def test_third_failure_escalates_and_pushes(self, tmp_state_db, monkeypatch):
        """The 3rd consecutive failed spawn MUST emit a single
        ``spawn_failure_cap_hit`` event AND one ntfy push with
        ``reason_slug='spawn_failure_cap'``.
        """
        _seed_plan()
        pushes = _capture_pushes(monkeypatch)
        self._force_failures(monkeypatch, n=SPAWN_CAP_EXPECTED)

        events = state.list_events(UNIT_ID)
        cap_events = [e for e in events if e["event_type"] == "spawn_failure_cap_hit"]
        assert len(cap_events) == 1, (
            f"expected exactly 1 spawn_failure_cap_hit, got {len(cap_events)}: "
            f"{[e['event_type'] for e in events]}"
        )
        assert "cap-3" in cap_events[0]["summary"]

        cap_pushes = [p for p in pushes if p["reason_slug"] == "spawn_failure_cap"]
        assert len(cap_pushes) == 1, (
            f"expected exactly 1 cap ntfy push, got {len(cap_pushes)}: {cap_pushes!r}"
        )
        assert cap_pushes[0]["unit_id"] == UNIT_ID

    def test_cap_sets_escalated_status_and_last_error(self, tmp_state_db, monkeypatch):
        """After the cap fires, the row MUST be ``status=escalated``
        with a non-empty ``last_error`` describing the cap hit.

        Spec § U-9: "force status=escalated + last_error, fire the ntfy
        escalation push, refuse further auto-spawn."
        """
        _seed_plan()
        _capture_pushes(monkeypatch)
        self._force_failures(monkeypatch, n=SPAWN_CAP_EXPECTED)

        row = state.get_unit_state(UNIT_ID)
        assert row.status == "escalated"
        assert row.last_error, "cap must set a last_error message"
        # The diagnostic must mention the cap so a forensic log reader
        # can correlate the row to the spawn_failure_cap_hit event.
        assert "spawn_failure_cap" in row.last_error.lower() or "cap" in row.last_error.lower()

    def test_cap_refuses_further_autospawn(self, tmp_state_db, monkeypatch):
        """Post-cap, the unit is ``escalated`` — guard 1 MUST refuse
        further ``spawn_unit`` calls without manual cancel.
        """
        _seed_plan()
        _capture_pushes(monkeypatch)
        # Force the cap to fire and LEAVE the row in escalated state.
        self._force_failures(monkeypatch, n=SPAWN_CAP_EXPECTED)
        assert state.get_unit_state(UNIT_ID).status == "escalated"

        # Now a subsequent spawn must be refused by the ghost-row guard
        # without ever invoking the worker.
        baseline_calls = _AlwaysFailingBlockingWorker.calls
        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
        assert msg.startswith("ERROR")
        assert "'escalated'" in msg
        assert _AlwaysFailingBlockingWorker.calls == baseline_calls, (
            "guard must short-circuit before the worker is invoked"
        )

    def test_counter_resets_after_successful_async_spawn(self, tmp_state_db, monkeypatch):
        """Counter MUST reset once a spawn writes a session_id.

        Spec § U-9: "Counter resets once a spawn successfully persists
        a session_id." So: 2 failures → 1 success → next failure starts
        from zero (no cap escalation).
        """
        _seed_plan()
        pushes = _capture_pushes(monkeypatch)

        # 2 failed blocking spawns.
        self._force_failures(monkeypatch, n=2)
        assert execution._consecutive_failed_spawns(UNIT_ID) == 2

        # Cancel + successful async spawn → persists session_id.
        state.cancel_unit(UNIT_ID)
        worker = _SucceedingAsyncWorker("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)
        assert "ERROR" not in execution.spawn_unit_async(FEATURE_ID, UNIT_ID)

        # Counter is now zero — the session-bearing row is the reset signal.
        assert execution._consecutive_failed_spawns(UNIT_ID) == 0

        # Across the entire scenario, the cap-3 path was never triggered.
        cap_pushes = [p for p in pushes if p["reason_slug"] == "spawn_failure_cap"]
        assert cap_pushes == []

    def test_counter_resets_after_successful_blocking_pr_open(self, tmp_state_db, monkeypatch):
        """Counter MUST reset after the blocking happy-path ``pr_opened`` row.

        The blocking path's reset signal is the ``pr_opened`` event,
        which ``record_event`` writes with ``session_id=session_id``.
        Without this, only the async path's ``coder_session_persisted``
        row would zero the counter, leaving the blocking caller's
        happy-path success silently uncounted.
        """
        _seed_plan()
        pushes = _capture_pushes(monkeypatch)

        # 2 failed blocking spawns (cancel between to bypass guard 1).
        self._force_failures(monkeypatch, n=2)
        assert execution._consecutive_failed_spawns(UNIT_ID) == 2

        # Cancel + successful blocking spawn → records pr_opened with session_id.
        state.cancel_unit(UNIT_ID)
        worker = _SucceedingBlockingWorker("coder")
        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", lambda role: worker)
        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
        assert "ERROR" not in msg, msg

        # Counter is now zero — the pr_opened row carries session_id.
        assert execution._consecutive_failed_spawns(UNIT_ID) == 0

        # No cap escalation across the whole scenario.
        cap_pushes = [p for p in pushes if p["reason_slug"] == "spawn_failure_cap"]
        assert cap_pushes == []

    def test_async_path_also_caps_at_three(self, tmp_state_db, monkeypatch):
        """The cap MUST apply uniformly to ``spawn_unit_async`` — three
        failed async spawns ESC the same as three failed blocking ones.
        """
        _seed_plan()
        pushes = _capture_pushes(monkeypatch)
        _AlwaysFailingAsyncWorker.calls = 0
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", _AlwaysFailingAsyncWorker)

        for i in range(SPAWN_CAP_EXPECTED):
            execution.spawn_unit_async(FEATURE_ID, UNIT_ID)
            if i < SPAWN_CAP_EXPECTED - 1:
                state.cancel_unit(UNIT_ID)

        cap_events = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "spawn_failure_cap_hit"
        ]
        cap_pushes = [p for p in pushes if p["reason_slug"] == "spawn_failure_cap"]
        assert len(cap_events) == 1
        assert len(cap_pushes) == 1


# ===========================================================================
# (3) ci_drift_detected DEDUPE
# ===========================================================================


def _seed_unit_in_state(status: str = "approved_awaiting_merge") -> WorkUnitState:
    """Seed a unit + feature for direct ``_apply_action`` tests."""
    state.save_feature(
        Feature(
            id=FEATURE_ID,
            title="t",
            description="d",
            repo_path=REPO_URL,
            status="approved",
        )
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=UNIT_ID,
            feature_id=FEATURE_ID,
            status=status,
            branch="b",
            pr_number=42,
        )
    )
    return state.get_unit_state(UNIT_ID)


def _drift(failing: list[str], status: str = "approved_awaiting_merge") -> Action:
    """Build a ``ci_drift_detected`` :class:`Action` matching the
    producer's wire shape (see :func:`orchestrator.health._ci_drift_event`).
    """
    return Action.event(
        "ci_drift_detected",
        f"CI red while status={status!r}",
        details=f"failing checks: {', '.join(failing)}",
        set_last_error=f"CI drift: {', '.join(failing)} failing",
        payload={"failing": list(failing), "status": status},
    )


class TestCiDriftDedupe:
    """Spec § ci_drift_detected dedupe.

    A unit parked ``in_ci`` (or any non-actively-fixing status) with a
    persistently-red CI MUST NOT generate one ``ci_drift_detected``
    event + GitHub-API hit per ~6s daemon poll. Dedupe MUST be by
    *content* (same failing-check-set) AND throttled by the rate-limit
    window so a real drift always re-surfaces eventually.
    """

    def test_first_drift_always_emits(self, tmp_state_db):
        """No prior ``ci_drift_detected`` row → first emit always fires."""
        unit = _seed_unit_in_state()
        tools_health._apply_action(unit, _drift(["test"]))

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        assert len(rows) == 1

    def test_repeated_unchanged_set_within_window_is_suppressed(self, tmp_state_db, monkeypatch):
        """Spec § Tests: "ci_drift_detected emitted at most once for an
        unchanged failing-check-set across repeated daemon ticks."

        10 calls with the same failing set MUST produce exactly one row.
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        unit = _seed_unit_in_state()
        for _ in range(10):
            tools_health._apply_action(unit, _drift(["test"]))

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        assert len(rows) == 1

    def test_changed_failing_set_re_emits(self, tmp_state_db, monkeypatch):
        """A real drift evolution (the failing-check-set changes) MUST
        re-emit — the dedupe is by content, not by event-type.
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        unit = _seed_unit_in_state()

        tools_health._apply_action(unit, _drift(["test"]))
        tools_health._apply_action(unit, _drift(["test", "lint"]))
        tools_health._apply_action(unit, _drift(["lint"]))

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        assert len(rows) == 3
        details = {r["details"] for r in rows}
        assert "failing checks: test" in details
        assert "failing checks: test, lint" in details
        assert "failing checks: lint" in details

    def test_unchanged_set_past_window_re_emits(self, tmp_state_db, monkeypatch):
        """Same set + past the rate-limit window MUST re-emit.

        Spec § Tests: "respects the rate-limit window." So a unit that
        sits red for >24h still surfaces in the daily-ish digest rather
        than going silent forever after the first emit.
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "1")
        unit = _seed_unit_in_state()

        tools_health._apply_action(unit, _drift(["test"]))
        # Backdate the first row by 2 hours so the 1-hour window is past.
        with state._connect() as conn:
            two_hours_ago = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            conn.execute(
                "UPDATE unit_events SET ts = ? "
                "WHERE unit_id = ? AND event_type = 'ci_drift_detected'",
                (two_hours_ago, UNIT_ID),
            )

        tools_health._apply_action(unit, _drift(["test"]))

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        assert len(rows) == 2

    def test_dedupe_suppresses_last_error_rewrite(self, tmp_state_db, monkeypatch):
        """Dedupe MUST suppress the ``set_last_error`` rewrite too.

        Otherwise the dedupe shrinks ``unit_events`` but every daemon
        tick still hammers the ``work_units`` table with an UPDATE.
        The whole point of dedupe is "no GitHub-API + DB storm".
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        unit = _seed_unit_in_state()

        tools_health._apply_action(unit, _drift(["test"]))
        # Sentinel: an unrelated last_error written between drifts.
        state.touch_unit(UNIT_ID, error="sentinel-from-other-code-path")

        # A second drift with the same set MUST NOT overwrite the sentinel.
        tools_health._apply_action(unit, _drift(["test"]))

        refreshed = state.get_unit_state(UNIT_ID)
        assert refreshed.last_error == "sentinel-from-other-code-path"

    def test_dedupe_holds_via_inspect_unit_health(self, tmp_state_db, monkeypatch):
        """The dedupe MUST hold at the ``inspect_unit_health`` surface.

        Spec § U-9: "Apply in BOTH the daemon tick and
        inspect_unit_health so a persistently-red PR no longer
        generates a continuous event/API storm."
        """
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_unit_in_state()

        # Stub the two real GH calls: PR is open + mergeable; CI is red.
        # Together they force the F-014 decision table to fire
        # ci_drift_detected on every probe.
        monkeypatch.setattr(
            "orchestrator.tools.health.github.get_pr_state",
            lambda url, pr: {
                "state": "open",
                "merged": False,
                "head_sha": "deadbeef",
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
            tools_health.inspect_unit_health(UNIT_ID)

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        assert len(rows) == 1, (
            f"5 inspect_unit_health calls produced {len(rows)} ci_drift_detected "
            f"events; expected exactly 1 (dedupe broken)"
        )

    def test_dedupe_holds_via_daemon_reconcile(self, tmp_state_db, monkeypatch):
        """The dedupe MUST hold at the daemon's reconcile tick.

        Spec § U-9: dedupe lives in the shared ``_apply_action`` helper
        so the daemon's ``_apply_health_action`` routes through it.
        A persistently-red PR across 10 ticks produces exactly one row.
        """
        from orchestrator import daemon

        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_unit_in_state()
        # Have the F-014 probe return the same drift action each tick,
        # bypassing the real GH probe + decision table.
        drift_action = _drift(["test"])
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [drift_action])
        # Marker scan is irrelevant here — stub the worker too.
        monkeypatch.setattr(
            "orchestrator.daemon.make_worker",
            lambda _r: type("W", (), {"tail_messages": lambda *a, **k: []})(),
        )

        for _ in range(10):
            daemon.reconcile_unit(UNIT_ID)

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        assert len(rows) == 1, (
            f"10 daemon ticks produced {len(rows)} ci_drift_detected events; "
            f"expected exactly 1 (dedupe broken at daemon-tick layer)"
        )
