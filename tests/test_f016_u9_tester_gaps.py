"""F-016-U-9 tester (gap-fill) — incremental regression guards.

Two prior tester cycles already shipped extensive coverage of the
explicit spec § U-9 acceptance criteria in ``test_f016_u9_tester.py``
and ``test_f016_u9_tester_extras.py`` (plus the coder's
``test_f016_u9_anti_loop.py``). This file is a third pass that pins a
few real-but-low-priority regression edges those files don't lock in:

  * **Failing-set ordering invariance.** ``_should_emit_ci_drift``
    parses the prior row's ``details`` via :func:`_parse_failing_set`
    which returns a ``frozenset`` — meaning a producer that writes
    ``"failing checks: a, b"`` and a later one that writes
    ``"failing checks: b, a"`` for the same set MUST dedupe. If a
    future refactor swaps the parser to a list, this test catches it.
  * **Cross-event-type isolation in drift dedupe.** The dedupe walks
    ``tail_events`` looking for the most-recent ``ci_drift_detected``
    row only. A sibling event with overlapping ``details`` payload
    (``pr_conflict_detected`` / ``required_check_missing``) MUST NOT
    suppress the next ``ci_drift_detected`` emit — they're different
    signals with different recovery actions.
  * **Ghost-row refusal is side-effect-free.** A refused spawn MUST
    NOT write a ``coder_error`` event or touch the cap counter —
    otherwise repeated refused calls would silently push the unit
    into a cap-3 escalation it never deserved.
  * **`pending` status accepts a fresh spawn.** The new status-based
    guard refuses every ``_RESPAWN_REFUSED_STATUSES`` entry; a
    plan-saved row that hasn't been spawned yet sits in ``pending``
    and MUST be accepted (the documented "first spawn" path).
  * **Refused spawn preserves ``cancelled_at``.** The sticky-cancel
    invariant (F-016 Phase 2.5): once ``cancelled_at`` is set, no
    state-mutating path may clear it. The ghost-row refusal is purely
    read-side, but a regression that moved a write before the guard
    would break this. We pin it by attempting a refused spawn against
    an escalated row that *also* carries a ``cancelled_at`` (a
    pathological but legitimate state from a manual SQLite poke).

Each test re-derives its fixtures from the spec; no helpers borrowed
from the other ``test_f016_u9_*.py`` files.
"""

from __future__ import annotations

import pytest

from orchestrator import state
from orchestrator.health import Action
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution
from orchestrator.tools import health as tools_health

FEATURE_ID = "F-016"
UNIT_ID = "F-016-U-1"
REPO_URL = "https://github.com/o/r"


# ---------------------------------------------------------------------------
# Auto-fixtures: bypass the verify-repo gate + provide a fake GH token so
# we exercise only the U-9 surface. Mirror what the other U-9 tester files
# do, intentionally NOT shared via conftest so the regression guard for
# the gate-bypass shape stays explicit here.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_verify_gate(monkeypatch, tmp_state_db):
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)


@pytest.fixture(autouse=True)
def _fake_gh_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_gaps_stub")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _silence_pr_amend(monkeypatch):
    """The blocking happy-path posts a coder-session comment via
    ``safe_amend_pr_body``; stub it so any happy-path tests stay offline.
    """
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")


# ---------------------------------------------------------------------------
# Per-test plan / unit seeders. Re-derive shape from the orchestrator
# models so a future schema bump fails loudly here rather than silently
# in production.
# ---------------------------------------------------------------------------


def _seed_plan() -> None:
    state.save_feature(
        Feature(
            id=FEATURE_ID,
            title="F-016",
            description="anti-loop hardening",
            repo_path=REPO_URL,
            status="approved",
            branch_prefix="u9",
        )
    )
    state.save_plan(
        FEATURE_ID,
        [WorkUnit(id=UNIT_ID, feature_id=FEATURE_ID, title="u1", description="impl")],
    )
    state.approve_plan(FEATURE_ID)


def _seed_unit(
    *,
    status: str,
    coder_session_id: str = "",
    pr_number: int | None = None,
    last_error: str = "",
    cancelled_at: str | None = None,
) -> WorkUnitState:
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=UNIT_ID,
            feature_id=FEATURE_ID,
            status=status,
            branch="u9-b",
            coder_session_id=coder_session_id,
            pr_number=pr_number,
            last_error=last_error,
            cancelled_at=cancelled_at,
        )
    )
    return state.get_unit_state(UNIT_ID)


def _drift_action(failing: list[str]) -> Action:
    """Mirror the ``ci_drift_detected`` action shape the F-014 decision
    table emits, so the dedupe tests are invariant to the producer's
    summary wording."""
    return Action.event(
        "ci_drift_detected",
        "CI red while approved_awaiting_merge",
        details=f"failing checks: {', '.join(failing)}",
        set_last_error=f"CI drift: {', '.join(failing)} failing",
        payload={"failing": list(failing), "status": "approved_awaiting_merge"},
    )


# ===========================================================================
# (A) Failing-set ordering invariance
# ===========================================================================


class TestFailingSetOrderingInvariant:
    """The ``ci_drift_detected`` dedupe compares failing-check-sets as
    :class:`frozenset` (see :func:`tools_health._parse_failing_set`).
    A producer that writes the same checks in a different order MUST
    still dedupe — otherwise a probe whose ordering depends on dict /
    list iteration (e.g. an upstream GitHub API tweak) would silently
    re-introduce the storm.
    """

    def test_reordered_same_set_is_deduped(self, tmp_state_db, monkeypatch):
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_plan()
        unit = _seed_unit(status="approved_awaiting_merge", pr_number=42)

        tools_health._apply_action(unit, _drift_action(["test", "lint", "type"]))
        # Same set, different order — MUST dedupe.
        tools_health._apply_action(unit, _drift_action(["lint", "type", "test"]))
        tools_health._apply_action(unit, _drift_action(["type", "test", "lint"]))

        drift_rows = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 1, (
            f"reordered same set MUST dedupe; got {len(drift_rows)} rows "
            f"with details {[r['details'] for r in drift_rows]!r}"
        )

    def test_subset_or_superset_re_emits(self, tmp_state_db, monkeypatch):
        """A subset / superset of the prior failing set IS a change —
        a check recovering or a new one breaking is meaningful drift
        evolution, not noise."""
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_plan()
        unit = _seed_unit(status="approved_awaiting_merge", pr_number=42)

        tools_health._apply_action(unit, _drift_action(["test", "lint"]))
        # Superset (new check broke)
        tools_health._apply_action(unit, _drift_action(["test", "lint", "type"]))
        # Subset (lint recovered)
        tools_health._apply_action(unit, _drift_action(["test", "type"]))

        drift_rows = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 3


# ===========================================================================
# (B) Cross-event-type isolation in drift dedupe
# ===========================================================================


class TestDriftDedupeIsolatedByEventType:
    """:func:`tools_health._should_emit_ci_drift` walks the event tail
    looking for the *most-recent* ``ci_drift_detected`` row. Sibling
    F-014 event types (``pr_conflict_detected``, ``required_check_missing``)
    carry their own semantics; their presence MUST NOT influence the
    drift dedupe decision.
    """

    def test_intervening_sibling_event_does_not_suppress_drift(self, tmp_state_db, monkeypatch):
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_plan()
        unit = _seed_unit(status="approved_awaiting_merge", pr_number=42)

        # First drift lands.
        tools_health._apply_action(unit, _drift_action(["test"]))
        # A sibling event arrives — same `details` string shape but
        # different event_type. MUST NOT register as a prior drift.
        state.record_event(
            UNIT_ID,
            FEATURE_ID,
            "pr_conflict_detected",
            source="orchestrator",
            details="failing checks: test",  # deliberately overlapping payload
            summary="conflict",
        )
        # A changed-set drift fires after — the sibling row must not
        # have been mistaken for a prior ``ci_drift_detected`` and the
        # dedupe MUST evaluate against the FIRST drift row (set={"test"}).
        tools_health._apply_action(unit, _drift_action(["lint"]))

        drift_rows = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"
        ]
        # Original {"test"} + changed {"lint"} = 2 rows.
        assert len(drift_rows) == 2

    def test_no_prior_drift_but_sibling_present_still_emits(self, tmp_state_db, monkeypatch):
        """A unit with sibling F-014 events but no prior
        ``ci_drift_detected`` MUST always fire on the first drift —
        the dedupe walker never finds a drift row to compare against
        and falls through to the "first emit always fires" branch."""
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "24")
        _seed_plan()
        unit = _seed_unit(status="approved_awaiting_merge", pr_number=42)
        # Sibling rows but no drift.
        for _ in range(5):
            state.record_event(
                UNIT_ID,
                FEATURE_ID,
                "pr_conflict_detected",
                source="orchestrator",
                details="failing checks: test",
                summary="conflict",
            )

        tools_health._apply_action(unit, _drift_action(["test"]))

        drift_rows = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"
        ]
        assert len(drift_rows) == 1


# ===========================================================================
# (C) Ghost-row refusal is side-effect-free
# ===========================================================================


class _UnusedBlockingWorker:
    """Worker stub whose ``spawn`` MUST never be called. If it is, the
    test fails loudly — proves the guard short-circuits *before* worker
    invocation.
    """

    calls = 0

    def __init__(self, role: str) -> None:
        self.role = role

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        _UnusedBlockingWorker.calls += 1
        raise AssertionError("worker MUST NOT be invoked when the ghost-row guard refuses")


class _UnusedAsyncWorker:
    calls = 0

    def __init__(self, role: str) -> None:
        self.role = role

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        _UnusedAsyncWorker.calls += 1
        raise AssertionError("worker MUST NOT be invoked when the ghost-row guard refuses")


class TestGhostRowRefusalIsPure:
    """The ghost-row guard fires before any state mutation. A refused
    spawn MUST NOT:

      * write a ``coder_error`` event (which would pollute the cap counter
        and trick the cap-3 backstop into firing on a unit the guard
        actually protected),
      * touch ``last_activity`` / ``status`` / ``last_error``,
      * invoke the worker backend.

    Regression risk: if the guard ever moved past the ``upsert_unit_state``
    seed (or a future maintainer added "audit the refusal" without
    suppressing the cap-counter event_type), the pre-U-9 ghost-row
    failure mode would silently re-emerge.
    """

    REFUSED = ("coding", "opening_pr", "in_ci", "testing", "reviewing", "fixing", "escalated")

    @pytest.mark.parametrize("status", REFUSED)
    def test_refusal_does_not_write_coder_error(self, tmp_state_db, monkeypatch, status):
        _seed_plan()
        # Seed the unit pre-refusal; capture the baseline event set.
        original_last_error = "pre-existing context"
        _seed_unit(status=status, last_error=original_last_error)
        baseline_events = set((e["event_type"], e["summary"]) for e in state.list_events(UNIT_ID))
        _UnusedBlockingWorker.calls = 0
        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker", _UnusedBlockingWorker
        )

        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)

        assert msg.startswith("ERROR"), msg
        # No worker call.
        assert _UnusedBlockingWorker.calls == 0
        # The event list is byte-identical to baseline — no audit row,
        # no coder_error.
        after_events = set((e["event_type"], e["summary"]) for e in state.list_events(UNIT_ID))
        assert after_events == baseline_events
        # last_error preserved (the triage anchor for an escalated row,
        # or empty for an active one).
        row = state.get_unit_state(UNIT_ID)
        assert row.last_error == original_last_error
        assert row.status == status

    def test_repeated_refusals_do_not_trip_cap(self, tmp_state_db, monkeypatch):
        """A misbehaving caller that re-tries the spawn 10 times on an
        ``escalated`` row MUST NOT see the cap-3 backstop fire — the
        guard's refusal isn't a "failed spawn" in the cap-counter sense.
        """
        _seed_plan()
        _seed_unit(status="escalated", last_error="manual escalation")
        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker", _UnusedBlockingWorker
        )
        pushes: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda u, reason, *, reason_slug="unknown", **k: pushes.append(
                (u, reason, reason_slug)
            ),
        )

        for _ in range(10):
            msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
            assert msg.startswith("ERROR"), msg

        # No cap-hit events / pushes.
        cap_events = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "spawn_failure_cap_hit"
        ]
        assert cap_events == []
        cap_pushes = [p for p in pushes if p[2] == "spawn_failure_cap"]
        assert cap_pushes == []
        # Counter stays at zero.
        assert execution._consecutive_failed_spawns(UNIT_ID) == 0

    @pytest.mark.parametrize("status", REFUSED)
    def test_async_refusal_does_not_write_coder_error(self, tmp_state_db, monkeypatch, status):
        """Same invariant for the async path."""
        _seed_plan()
        _seed_unit(status=status)
        baseline_events = set((e["event_type"], e["summary"]) for e in state.list_events(UNIT_ID))
        _UnusedAsyncWorker.calls = 0
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", _UnusedAsyncWorker)

        msg = execution.spawn_unit_async(FEATURE_ID, UNIT_ID)
        assert msg.startswith("ERROR"), msg
        assert _UnusedAsyncWorker.calls == 0
        after_events = set((e["event_type"], e["summary"]) for e in state.list_events(UNIT_ID))
        assert after_events == baseline_events


# ===========================================================================
# (D) `pending` status accepts a fresh spawn
# ===========================================================================


class _OneShotSuccessAsync:
    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.calls += 1
        return f"sesn-pending-{self.calls}"


class TestPendingRowAcceptsFirstSpawn:
    """A plan-saved row that hasn't been spawned yet sits in
    ``pending`` (the documented "fresh first spawn" state). The
    ghost-row guard MUST NOT refuse it — that's the spec's
    "clean first spawn (no row, or a fresh/non-active row) still
    works" requirement, applied to the specific ``pending`` row a
    plan-saved unit lands in via the scheduling surface.
    """

    def test_pending_row_succeeds_via_async(self, tmp_state_db, monkeypatch):
        _seed_plan()
        _seed_unit(status="pending")
        worker = _OneShotSuccessAsync("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)

        msg = execution.spawn_unit_async(FEATURE_ID, UNIT_ID)
        assert "ERROR" not in msg, msg
        row = state.get_unit_state(UNIT_ID)
        assert row.status == "coding"
        assert row.coder_session_id == "sesn-pending-1"


# ===========================================================================
# (E) Refused spawn preserves the sticky-cancel timestamp invariant
# ===========================================================================


class TestRefusedSpawnPreservesCancelledAt:
    """F-016 Phase 2.5 sticky-cancel guarantee (state.py:`is_cancelled`
    docstring): once ``cancelled_at`` is set, no path may clear it —
    ``cancelled_at`` IS the durable cancel-source-of-truth.

    A row with ``status=escalated`` AND a non-null ``cancelled_at``
    (e.g. a manual SQLite poke during triage, or a pathological race
    where the cap-escalate landed after the cancel) MUST have its
    ``cancelled_at`` preserved across a refused spawn — the refusal is
    purely read-side per the ghost-row guard contract.
    """

    def test_refused_spawn_does_not_clear_cancelled_at(self, tmp_state_db, monkeypatch):
        _seed_plan()
        sticky_ts = "2026-06-10T00:00:00+00:00"
        # Seed an escalated row with a stuck cancelled_at — a real-world
        # corner from a manual triage poke.
        _seed_unit(
            status="escalated",
            last_error="some prior escalation",
            cancelled_at=sticky_ts,
        )
        _UnusedBlockingWorker.calls = 0
        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker", _UnusedBlockingWorker
        )

        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
        assert msg.startswith("ERROR"), msg
        assert _UnusedBlockingWorker.calls == 0

        row = state.get_unit_state(UNIT_ID)
        # Sticky-cancel timestamp preserved bit-for-bit.
        assert row.cancelled_at == sticky_ts
        # Status untouched.
        assert row.status == "escalated"
        # last_error preserved (not overwritten by a spurious cap path).
        assert row.last_error == "some prior escalation"
