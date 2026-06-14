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
    """No row, or a row with a non-active / non-terminal status
    (``pending`` / ``cancelled``), accepts a fresh spawn — the guard
    is a *refusal* of in-flight + terminal-record state, not a blanket
    re-dispatch ban.

    Note ``done`` and ``approved_awaiting_merge`` are intentionally NOT
    in this parametrize list — they carry session_id / pr_number / merge
    evidence from the original spawn, and re-spawn on them would blank
    those columns via the upsert in ``spawn_unit``. They're covered by
    :class:`TestTerminalRowsRefuseRespawn` below.
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

    @pytest.mark.parametrize("status", ("pending", "cancelled"))
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
# (1d) Terminal-merge rows refuse re-spawn (C1 regression guard)
# ============================================================================


class TestTerminalRowsRefuseRespawn:
    """The pre-U-9 guard refused re-spawn on ANY row whose
    ``coder_session_id`` was set — including ``done`` /
    ``approved_awaiting_merge``, both of which carry the session id
    from the original ``pr_opened`` event. PR #69 C1: the status-based
    guard initially excluded those two terminal statuses, which would
    have let a stray re-spawn upsert overwrite ``coder_session_id`` /
    ``pr_number`` / ``review_round`` with constructor defaults —
    destroying the merge record.

    These tests pin the fix: ``done`` and ``approved_awaiting_merge``
    rows REFUSE re-spawn even when the row carries a real session id
    (the realistic case the original C1 finding pointed at).
    """

    @pytest.mark.parametrize("status", ("done", "approved_awaiting_merge"))
    def test_spawn_unit_refuses_terminal_row_with_session(self, tmp_state_db, monkeypatch, status):
        _seed_feature()
        # Realistic terminal row: session_id populated, pr_number set,
        # review_round non-zero (the columns the upsert would blank).
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status=status,
                branch="b",
                coder_session_id="sesn-merge-record",
                pr_number=42,
                review_round=2,
            )
        )
        _install_failing_blocking_worker(monkeypatch)

        msg = execution.spawn_unit("F-016", "F-016-U-1")

        assert "ERROR" in msg
        assert f"status={status!r}" in msg
        # Worker NOT invoked — guard fired pre-dispatch.
        assert _FailingWorker.spawn_calls == 0
        # Merge record preserved bit-for-bit.
        row = state.get_unit_state("F-016-U-1")
        assert row.coder_session_id == "sesn-merge-record"
        assert row.pr_number == 42
        assert row.review_round == 2

    @pytest.mark.parametrize("status", ("done", "approved_awaiting_merge"))
    def test_spawn_unit_async_refuses_terminal_row_with_session(
        self, tmp_state_db, monkeypatch, status
    ):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status=status,
                branch="b",
                coder_session_id="sesn-merge-record",
                pr_number=42,
                review_round=2,
            )
        )
        _install_failing_async_worker(monkeypatch)

        msg = execution.spawn_unit_async("F-016", "F-016-U-1")

        assert "ERROR" in msg
        assert f"status={status!r}" in msg
        assert _FailingAsyncWorker.spawn_async_calls == 0
        row = state.get_unit_state("F-016-U-1")
        assert row.coder_session_id == "sesn-merge-record"
        assert row.pr_number == 42
        assert row.review_round == 2


# ============================================================================
# (1c) cancel_unit → re-dispatch works
# ============================================================================


def _stub_archive_for_cancel(monkeypatch) -> None:
    """The MCP ``cancel_unit`` calls ``_archive_unit_sessions`` which
    invokes ``make_worker(role).archive(session_id)`` for each role. The
    tests don't care about archival semantics — short-circuit it so the
    cancel path itself is what's exercised.
    """
    monkeypatch.setattr(
        "orchestrator.tools.execution._archive_unit_sessions",
        lambda _u: {"coder": "no_session", "tester": "no_session", "reviewer": "no_session"},
    )


def test_cancel_then_respawn_works(tmp_state_db, monkeypatch):
    """The documented recovery path: cancel a stuck unit, then re-dispatch.

    Exercises the MCP-tool ``execution.cancel_unit`` (not the state-level
    ``state.cancel_unit`` helper) so the test catches a regression in
    the user-facing surface — that surface layers a terminal-status
    refusal on top of the state helper, which is what the F-016-U-9 H1
    carve-out has to thread through.

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
    _stub_archive_for_cancel(monkeypatch)

    # MCP tool surface, not the bare state helper.
    cancel_result = execution.cancel_unit("F-016-U-1")
    assert '"outcome": "cancelled"' in cancel_result
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


def test_post_cap_cancel_unit_then_respawn_works(tmp_state_db, monkeypatch):
    """H1 dead-end fix: a cap-escalated unit must be recoverable via the
    documented ``cancel_unit → spawn_unit`` path.

    Pre-fix: the cap escalation flipped status to ``escalated`` →
    ``cancel_unit`` (MCP) refused (``escalated`` ∈
    ``_CANCEL_REFUSED_STATUSES``) → ``spawn_unit`` refused
    (``escalated`` ∈ ``_RESPAWN_REFUSED_STATUSES``) → the only escape
    was manual SQLite surgery.

    Post-fix: the ``cancel_unit`` MCP tool detects the spawn-failure-cap
    sentinel on ``last_error`` and allows the flip back to
    ``cancelled``; the ``unit_cancelled`` event also resets the failed-
    spawn counter so the next ``spawn_unit_async`` doesn't immediately
    re-trip the cap.
    """
    _seed_feature()
    pushes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda u, reason, *, reason_slug="unknown", **k: pushes.append((u, reason, reason_slug)),
    )
    _install_failing_blocking_worker(monkeypatch)
    _stub_archive_for_cancel(monkeypatch)

    # Drive the unit to cap-escalated via the same path the incident took.
    # Bypass the ghost-row guard between attempts so the cap is what fires,
    # but NOT after the final iteration — the cap's ``status=escalated``
    # is what we need to land on for the recovery test.
    for i in range(3):
        execution.spawn_unit("F-016", "F-016-U-1")
        if i < 2:
            state.cancel_unit("F-016-U-1")

    post_cap = state.get_unit_state("F-016-U-1")
    assert post_cap.status == "escalated"
    assert post_cap.last_error.startswith(execution._SPAWN_FAILURE_CAP_LAST_ERROR_PREFIX)
    # One cap-specific ntfy push so far.
    cap_pushes = [p for p in pushes if p[2] == "spawn_failure_cap"]
    assert len(cap_pushes) == 1

    # MCP-tool cancel_unit must accept the cap-escalated row — without
    # this, the documented recovery path is a dead-end.
    cancel_result = execution.cancel_unit("F-016-U-1")
    assert '"outcome": "cancelled"' in cancel_result, cancel_result
    cancelled = state.get_unit_state("F-016-U-1")
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at is not None

    # Re-dispatch with a working worker → succeeds, NOT re-tripped by the cap.
    worker = _SuccessAsyncWorker("coder")
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

    msg = execution.spawn_unit_async("F-016", "F-016-U-1")
    assert "ERROR" not in msg, msg
    row = state.get_unit_state("F-016-U-1")
    assert row.status == "coding"
    assert row.coder_session_id == "sesn-success-1"

    # No additional cap pushes from the recovery attempt.
    cap_pushes_after = [p for p in pushes if p[2] == "spawn_failure_cap"]
    assert len(cap_pushes_after) == 1


def test_mcp_cancel_unit_still_refuses_non_cap_escalation(tmp_state_db, monkeypatch):
    """The cap carve-out is narrow: a non-cap ``escalated`` row (e.g.
    coder BLOCKED, manual escalation) must STILL be refused so the
    triage anchor (``last_error``) isn't silently rewritten.
    """
    _seed_feature()
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-016-U-1",
            feature_id="F-016",
            status="escalated",
            branch="b",
            coder_session_id="sesn-blocked",
            last_error="BLOCKED [worker_blocked]: coder emitted explicit BLOCKED marker",
        )
    )
    _stub_archive_for_cancel(monkeypatch)

    result = execution.cancel_unit("F-016-U-1")
    assert '"outcome": "refused"' in result
    # Row untouched.
    row = state.get_unit_state("F-016-U-1")
    assert row.status == "escalated"
    assert row.last_error.startswith("BLOCKED [worker_blocked]:")
    assert row.coder_session_id == "sesn-blocked"


def test_mcp_cancel_unit_still_refuses_done_and_awaiting_merge(tmp_state_db, monkeypatch):
    """The H1 carve-out applies ONLY to spawn-failure-cap escalations.
    Real merge / endorsement records must STILL be protected.
    """
    _seed_feature()
    _stub_archive_for_cancel(monkeypatch)

    for status in ("done", "approved_awaiting_merge"):
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status=status,
                branch="b",
                coder_session_id="sesn-merge",
                pr_number=99,
            )
        )

        result = execution.cancel_unit("F-016-U-1")
        assert '"outcome": "refused"' in result, f"status={status} unexpectedly accepted"

        row = state.get_unit_state("F-016-U-1")
        assert row.status == status
        assert row.coder_session_id == "sesn-merge"
        assert row.pr_number == 99


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


# ============================================================================
# (4) state.tail_events helper — newest-first event tail
# ============================================================================


class TestStateTailEvents:
    """The dedupe / counter helpers must read the most-recent N events,
    not the oldest N. ``list_events`` returns the oldest 200 (it's an
    ascending ORDER BY with LIMIT) — a long-lived unit past the 200-row
    threshold would have the most recent activity invisible to a
    ``reversed(list_events(...))`` walker.
    """

    def _seed_unit(self) -> None:
        """``unit_events`` has a FOREIGN KEY on ``work_units(unit_id)`` —
        the row must exist before any ``record_event`` insert. Tests that
        write raw events without going through the spawn path seed the
        row explicitly here."""
        state.save_feature(
            Feature(id="F-016", title="t", description="d", repo_path="https://github.com/o/r")
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-016-U-1", feature_id="F-016", status="pending", branch="b")
        )

    def test_returns_newest_first(self, tmp_state_db):
        self._seed_unit()
        for i in range(5):
            state.record_event(
                "F-016-U-1",
                "F-016",
                f"event_{i}",
                source="orchestrator",
                summary=f"row {i}",
            )

        events = state.tail_events("F-016-U-1", limit=3)
        assert [e["event_type"] for e in events] == ["event_4", "event_3", "event_2"]

    def test_returns_tail_past_list_events_window(self, tmp_state_db):
        """The point of ``tail_events``: walks the recent tail even past
        the ~200-row ``list_events`` LIMIT. Old failures buried beyond
        row 200 must NOT be visible to a tail-only consumer.
        """
        self._seed_unit()
        # 250 old rows + 3 recent ones the dedupe should see.
        for i in range(250):
            state.record_event(
                "F-016-U-1", "F-016", "old_filler", source="orchestrator", summary=f"f{i}"
            )
        for i in range(3):
            state.record_event(
                "F-016-U-1", "F-016", "recent", source="orchestrator", summary=f"r{i}"
            )

        events = state.tail_events("F-016-U-1", limit=5)
        types = [e["event_type"] for e in events]
        # The first three are the recent rows; the 4th/5th are the most-
        # recent old_filler rows — still part of the tail, not the head.
        assert types[:3] == ["recent", "recent", "recent"]
        assert types[3:] == ["old_filler", "old_filler"]


def _count_event_type(unit_id: str, event_type: str) -> int:
    """Raw ``COUNT(*)`` for an event_type on a unit — bypasses any
    ``LIMIT``-bound helper so the count reflects what's actually in
    ``unit_events``. The pre-fix regression tests used
    ``list_events`` (oldest 200) which itself was windowed, so a new
    drift row at position 252+ was invisible and the assertion passed
    whether the dedupe held or not (PR #69 reviewer H1).
    """
    with state._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM unit_events WHERE unit_id = ? AND event_type = ?",
            (unit_id, event_type),
        ).fetchone()
    return int(row["c"]) if row else 0


def test_consecutive_failed_spawns_uses_tail_past_event_window(tmp_state_db):
    """Regression guard for H2: the cap counter must walk the tail of
    ``unit_events``, not the head. A unit with >200 events whose tail
    contains 3 fresh ``coder_error`` rows MUST still report 3 — the
    pre-fix ``reversed(list_events(...))`` walked the oldest 200 rows
    and missed the recent failures, so the cap silently never fired.

    Also seeds a session-id-bearing reset signal 250+ events back to
    pin the PR #69 H1 follow-up: the reset signal must remain visible
    even when buried beyond the default ``tail_events`` window — pre-
    fix the cap counter used the default ``limit=200`` and would miss
    a buried reset signal, then falsely cap-escalate.
    """
    state.save_feature(
        Feature(id="F-016", title="t", description="d", repo_path="https://github.com/o/r")
    )
    state.upsert_unit_state(
        WorkUnitState(unit_id="F-016-U-1", feature_id="F-016", status="pending", branch="b")
    )
    # Reset signal at the very bottom of the timeline (oldest row).
    state.record_event(
        "F-016-U-1",
        "F-016",
        "pr_opened",
        source="coder",
        cycle_number=0,
        summary="early reset signal",
        session_id="sesn-buried",
    )
    # 250 ancient unrelated events on top of the reset signal.
    for i in range(250):
        state.record_event(
            "F-016-U-1", "F-016", "old_filler", source="orchestrator", summary=f"f{i}"
        )
    # Three recent coder_error rows at cycle 0 — the cap-counting tail.
    for i in range(3):
        state.record_event(
            "F-016-U-1",
            "F-016",
            "coder_error",
            source="orchestrator",
            cycle_number=0,
            summary=f"e{i}",
        )

    # With the buried reset signal visible (wide tail), the count is
    # exactly the 3 fresh failures since the reset — NOT 3 plus any
    # phantom older errors, and crucially NOT 0 (which would happen
    # if the walker stopped early on a row it couldn't see).
    assert execution._consecutive_failed_spawns("F-016-U-1") == 3


def test_consecutive_failed_spawns_sees_buried_reset_signal(tmp_state_db):
    """Direct pin for the PR #69 H1 follow-up on the cap-counter side.

    Layout: reset_signal → 3 failures → 9_000 filler rows. With a
    9_000-row tail the failures are nowhere near the head; the cap
    counter MUST still report 3 (not 0 from missing the failures, and
    not >3 from missing the reset). Pre-fix at ``limit=200`` the
    failures+reset would be nowhere in view → reported 0.
    """
    state.save_feature(
        Feature(id="F-016", title="t", description="d", repo_path="https://github.com/o/r")
    )
    state.upsert_unit_state(
        WorkUnitState(unit_id="F-016-U-1", feature_id="F-016", status="pending", branch="b")
    )
    state.record_event(
        "F-016-U-1",
        "F-016",
        "pr_opened",
        source="coder",
        cycle_number=0,
        summary="reset",
        session_id="sesn-deep",
    )
    for i in range(3):
        state.record_event(
            "F-016-U-1",
            "F-016",
            "coder_error",
            source="orchestrator",
            cycle_number=0,
            summary=f"e{i}",
        )
    # Heavy filler tail on top of the failures — the failures sit
    # near the bottom of the most-recent window.
    for i in range(9_000):
        state.record_event(
            "F-016-U-1",
            "F-016",
            "noise",
            source="orchestrator",
            summary=f"n{i}",
        )

    # Counter walks the wide tail, reaches the 3 failures, then stops
    # at the buried reset signal. Pre-fix this would have been 0
    # (default 200-row window saw only the most-recent noise).
    assert execution._consecutive_failed_spawns("F-016-U-1") == 3


def test_should_emit_ci_drift_dedupes_past_tail_events_window(tmp_state_db, monkeypatch):
    """PR #69 reviewer H1 (follow-up): the ci_drift dedupe MUST survive
    a deep event history between two same-set drift candidates.

    Layout: 1 prior ``ci_drift_detected`` (set ``["test"]``) → 250
    unrelated events → re-fire with the same set. Pre-fix (default
    ``tail_events(limit=200)``) the walker couldn't see the prior
    drift row → "no prior" → emitted a fresh drift → 2 rows. The
    post-fix targeted ``last_event_of_type`` query is unaffected by
    the event volume — sees the prior, dedupes, total stays at 1.

    Asserts via a raw ``COUNT(*)`` query (not ``list_events`` /
    ``tail_events``) so the regression actually fails if the dedupe
    fails — the pre-fix companion test used ``list_events`` whose
    own 200-row window hid the false-emitted row, masking the bug.
    """
    monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
    unit = _seed_unit_for_drift()
    state.record_event(
        "F-016-U-1",
        "F-016",
        "ci_drift_detected",
        source="orchestrator",
        details="failing checks: test",
    )
    for i in range(250):
        state.record_event(
            "F-016-U-1", "F-016", "old_filler", source="orchestrator", summary=f"f{i}"
        )

    tools_health._apply_action(unit, _drift_action(["test"]))

    # Raw COUNT — unaffected by any windowed helper.
    assert _count_event_type("F-016-U-1", "ci_drift_detected") == 1


def test_should_emit_ci_drift_dedupes_with_very_deep_history(tmp_state_db, monkeypatch):
    """Extreme variant of the H1 follow-up: 9_000 unrelated events
    between the prior drift row and the re-fire. The targeted SQL
    query sees the prior drift regardless of how deep the burial is."""
    monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
    unit = _seed_unit_for_drift()
    state.record_event(
        "F-016-U-1",
        "F-016",
        "ci_drift_detected",
        source="orchestrator",
        details="failing checks: test",
    )
    for i in range(9_000):
        state.record_event(
            "F-016-U-1", "F-016", "old_filler", source="orchestrator", summary=f"f{i}"
        )

    tools_health._apply_action(unit, _drift_action(["test"]))

    assert _count_event_type("F-016-U-1", "ci_drift_detected") == 1


def test_last_event_of_type_returns_most_recent(tmp_state_db):
    """The new ``state.last_event_of_type`` helper underlying
    ``_should_emit_ci_drift``: must return the MOST RECENT event of the
    requested type regardless of how many other events exist."""
    state.save_feature(
        Feature(id="F-016", title="t", description="d", repo_path="https://github.com/o/r")
    )
    state.upsert_unit_state(
        WorkUnitState(unit_id="F-016-U-1", feature_id="F-016", status="pending", branch="b")
    )
    # Oldest target row.
    state.record_event("F-016-U-1", "F-016", "ci_drift_detected", details="failing checks: a")
    # Burial: lots of unrelated events.
    for i in range(500):
        state.record_event("F-016-U-1", "F-016", "noise", summary=f"n{i}")
    # Newest target row.
    state.record_event("F-016-U-1", "F-016", "ci_drift_detected", details="failing checks: b")
    # Final burial.
    for i in range(500):
        state.record_event("F-016-U-1", "F-016", "more_noise", summary=f"m{i}")

    row = state.last_event_of_type("F-016-U-1", "ci_drift_detected")
    assert row is not None
    assert row["details"] == "failing checks: b"


def test_last_event_of_type_returns_none_when_absent(tmp_state_db):
    state.save_feature(
        Feature(id="F-016", title="t", description="d", repo_path="https://github.com/o/r")
    )
    state.upsert_unit_state(
        WorkUnitState(unit_id="F-016-U-1", feature_id="F-016", status="pending", branch="b")
    )
    for i in range(10):
        state.record_event("F-016-U-1", "F-016", "noise", summary=f"n{i}")

    assert state.last_event_of_type("F-016-U-1", "ci_drift_detected") is None


# ============================================================================
# (4b) _failing_set_from_action prefers payload over comma-parsed details
# ============================================================================


class TestFailingSetFromAction:
    """PR #69 reviewer observation: ``_parse_failing_set`` is fragile to
    literal commas in check names (``"my, check"`` split on comma yields
    two items). The incoming side of the dedupe now prefers the
    structured ``action.payload['failing']`` over comma-parsing
    ``action.details``, preserving check names verbatim.
    """

    def test_uses_structured_payload_when_present(self):
        action = Action.event(
            "ci_drift_detected",
            "CI red",
            details="failing checks: a, b, c",  # comma-parse view
            payload={"failing": ["a", "b", "c"]},
        )
        assert tools_health._failing_set_from_action(action) == frozenset({"a", "b", "c"})

    def test_payload_preserves_check_names_with_commas(self):
        """A check name with a literal comma (rare but possible) round-
        trips through the structured payload but would mangle through
        ``details.split(',')``."""
        weird = "scan, fast"
        action = Action.event(
            "ci_drift_detected",
            "CI red",
            details=f"failing checks: {weird}, other",  # comma-split would yield 3
            payload={"failing": [weird, "other"]},
        )
        result = tools_health._failing_set_from_action(action)
        assert result == frozenset({weird, "other"})
        # Sanity: the legacy details parser DOES mangle it (proves the bug
        # the new helper sidesteps).
        legacy = tools_health._parse_failing_set(action.details)
        assert legacy == frozenset({weird.split(",")[0], weird.split(",")[1].strip(), "other"})

    def test_falls_back_to_details_when_payload_missing(self):
        action = Action.event(
            "ci_drift_detected",
            "CI red",
            details="failing checks: a, b",
            payload={"status": "in_ci"},  # no 'failing' key
        )
        assert tools_health._failing_set_from_action(action) == frozenset({"a", "b"})

    def test_falls_back_to_details_when_payload_wrong_type(self):
        action = Action.event(
            "ci_drift_detected",
            "CI red",
            details="failing checks: a, b",
            payload={"failing": "a, b"},  # string, not a list
        )
        assert tools_health._failing_set_from_action(action) == frozenset({"a", "b"})

    def test_empty_payload_list_yields_empty_set(self):
        action = Action.event(
            "ci_drift_detected",
            "CI red",
            details="failing checks: ",
            payload={"failing": []},
        )
        assert tools_health._failing_set_from_action(action) == frozenset()


# ============================================================================
# (5) _escalate_spawn_cap uses touch_unit + gates ntfy on record_event (M1, M2)
# ============================================================================


def test_escalate_spawn_cap_preserves_other_columns(tmp_state_db, monkeypatch):
    """M1: ``_escalate_spawn_cap`` must use ``touch_unit`` (column-
    selective UPDATE), NOT ``upsert_unit_state`` with a fresh
    ``WorkUnitState`` (which blanks every column to constructor
    defaults). Hypothetical future caller may have a session-id-bearing
    row land in the cap path; the columns MUST survive.
    """
    state.save_feature(
        Feature(id="F-016", title="t", description="d", repo_path="https://github.com/o/r")
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-016-U-1",
            feature_id="F-016",
            status="coding",
            branch="b",
            coder_session_id="sesn-preserved",
            pr_number=77,
            review_round=3,
        )
    )
    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True)

    execution._escalate_spawn_cap("F-016-U-1", "F-016", attempts=3)

    row = state.get_unit_state("F-016-U-1")
    assert row.status == "escalated"
    assert row.last_error.startswith(execution._SPAWN_FAILURE_CAP_LAST_ERROR_PREFIX)
    # The columns ``upsert_unit_state(WorkUnitState(...))`` would have
    # blanked — these MUST survive the cap escalation.
    assert row.coder_session_id == "sesn-preserved"
    assert row.pr_number == 77
    assert row.review_round == 3


def test_escalate_spawn_cap_ntfy_gated_on_record_event_insert(tmp_state_db, monkeypatch):
    """M2: the ntfy push fires only when the ``spawn_failure_cap_hit``
    event was actually inserted. A same-attempt re-check (or a multi-
    process race) hits the dedupe_key and the event INSERT silently
    drops; the push must drop with it so the user isn't double-paged.
    """
    state.save_feature(
        Feature(id="F-016", title="t", description="d", repo_path="https://github.com/o/r")
    )
    state.upsert_unit_state(
        WorkUnitState(unit_id="F-016-U-1", feature_id="F-016", status="coding", branch="b")
    )
    pushes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda u, reason, *, reason_slug="unknown", **k: pushes.append((u, reason, reason_slug)),
    )

    # First call → inserts the event → pushes.
    execution._escalate_spawn_cap("F-016-U-1", "F-016", attempts=3)
    # Second call with the SAME attempts → dedupe_key collision →
    # record_event returns False → push gated off.
    execution._escalate_spawn_cap("F-016-U-1", "F-016", attempts=3)

    cap_events = [
        e for e in state.list_events("F-016-U-1") if e["event_type"] == "spawn_failure_cap_hit"
    ]
    assert len(cap_events) == 1
    cap_pushes = [p for p in pushes if p[2] == "spawn_failure_cap"]
    assert len(cap_pushes) == 1
