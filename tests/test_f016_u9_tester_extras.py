"""F-016-U-9 tester (extras) — gap-fill tests for the anti-loop hardening.

The primary tester file (``test_f016_u9_tester.py``) covers the headline
spec items: ghost-row refusal on every active/escalated status, the
3-attempt cap that escalates + pushes ntfy on the 3rd consecutive
failure, the counter reset after a successful session persist, the
cancel→re-dispatch recovery path, and the ``ci_drift_detected``
content + rate-limit dedupe.

This file probes the *boundary* behaviour those tests don't lock in:

  * **dedupe_key on the cap-hit event.** The implementation passes
    ``dedupe_key=f"spawn_failure_cap_hit:{unit_id}:{attempts}"`` so a
    same-process double-fire (e.g. ``spawn_unit``'s in-except cap check
    racing with a follow-up attempt) inserts exactly one row. The
    UNIQUE index on ``unit_events.dedupe_key`` enforces this; tests
    here pin the contract by attempting the second write directly.
  * **``cycle_number`` discrimination.** Spec § attempt cap says
    "Count consecutive spawn_coder → coder_error cycles at
    cycle_number=0". A ``coder_error`` row at cycle 1 or 2 (mid-fix-
    loop) MUST NOT increment the spawn-cap counter — otherwise a
    cycle_review fix-loop that hits transient errors would falsely
    trip the cap built for blocking-spawn timeouts.
  * **Cap fires immediately on pre-seeded failure history.** The cap
    counter is event-table-derived, not in-memory; a fresh
    ``spawn_unit`` against a row whose history already shows 3 failed
    coder_errors MUST cap-escalate without invoking the worker even
    once.
  * **Any session-bearing event resets the counter.** Spec § U-9
    reset rule: "any row whose session_id column is non-empty" zeroes
    the counter. The blocking happy-path ``pr_opened`` and the async
    path ``coder_session_persisted`` are the obvious ones; this file
    confirms a downstream non-spawn event (e.g. a coder resume) with
    session_id ALSO resets — proving the reset is by-column, not
    by-event-type.
  * **CI dedupe with disabled rate-limit window.** Setting
    ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS=0`` MUST disable the
    time-window backstop but keep content-dedupe: an unchanged set
    stays suppressed forever, a changed set still re-emits. The
    primary tester only covers ``=24``; this locks the ``=0`` knob's
    behaviour.
  * **Ghost-row refusal recovery surfaces.** The refusal string must
    name ``inspect_unit_health``, ``resume_unit``, AND ``cancel_unit``
    so a chat-only caller can recover without grepping source.
  * **Async path writes ``coder_session_persisted`` audit event.**
    The async happy path's reset signal is this event (see spec § U-9
    cap reset comment); pin its structure (event_type, session_id
    column, cycle_number).

Every test re-derives its fixtures from the spec, never reusing any
helper from ``test_f016_u9_tester.py`` or ``test_f016_u9_anti_loop.py``.
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
# Bypass the verify-repo gate + GH-token guard so we exercise only the
# F-016-U-9 surface. Each fixture mirrors the production wiring that
# would otherwise short-circuit the test before reaching the U-9 code.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_verify_gate(monkeypatch, tmp_state_db):
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)


@pytest.fixture(autouse=True)
def _fake_gh_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_extras_stub")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _silence_gh_amend(monkeypatch):
    """The blocking happy path posts a ``_Coder session_`` comment via
    ``safe_amend_pr_body``; stub it so happy-path tests stay offline."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")


# ---------------------------------------------------------------------------
# Seeding helpers — same shape as the existing tester file but redefined
# here so this file stands alone (per CLAUDE.md "tests must be readable
# without cross-referencing").
# ---------------------------------------------------------------------------


def _seed_plan() -> None:
    state.save_feature(
        Feature(
            id=FEATURE_ID,
            title="t",
            description="d",
            repo_path=REPO_URL,
            status="approved",
            branch_prefix="extras",
        )
    )
    state.save_plan(
        FEATURE_ID,
        [WorkUnit(id=UNIT_ID, feature_id=FEATURE_ID, title="u", description="d")],
    )
    state.approve_plan(FEATURE_ID)


def _seed_unit(status: str = "pending", **extra) -> WorkUnitState:
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=UNIT_ID,
            feature_id=FEATURE_ID,
            status=status,
            branch="b",
            **extra,
        )
    )
    return state.get_unit_state(UNIT_ID)


def _drift_action(failing: list[str], status: str = "in_ci") -> Action:
    """Build the ``ci_drift_detected`` Action the F-014 producer emits."""
    return Action.event(
        "ci_drift_detected",
        f"CI red while status={status!r}",
        details=f"failing checks: {', '.join(failing)}",
        set_last_error=f"CI drift: {', '.join(failing)} failing",
        payload={"failing": list(failing), "status": status},
    )


def _capture_pushes(monkeypatch) -> list[dict]:
    pushes: list[dict] = []

    def _capture(unit_id: str, reason: str, *args, reason_slug: str = "unknown", **kwargs) -> bool:
        pushes.append({"unit_id": unit_id, "reason": reason, "reason_slug": reason_slug})
        return True

    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", _capture)
    return pushes


# ---------------------------------------------------------------------------
# Worker doubles
# ---------------------------------------------------------------------------


class _FailingBlocking:
    """``ManagedAgentWorker`` that always raises (mimics the 06-10 timeout)."""

    calls = 0

    def __init__(self, role: str) -> None:
        self.role = role

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        type(self).calls += 1
        raise RuntimeError("read timeout (network)")


class _SucceedingAsync:
    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.calls += 1
        return f"sesn-extras-{self.calls}"


# ===========================================================================
# (A) cap-hit event has a dedupe_key
# ===========================================================================


class TestCapHitEventHasDedupeKey:
    """Spec § U-9 cap escalation: emits the ``spawn_failure_cap_hit`` row
    with a ``dedupe_key`` so a same-process retry on the same lockstep
    counter writes exactly one event row.

    The implementation passes
    ``dedupe_key=f"spawn_failure_cap_hit:{unit_id}:{attempts}"``. We
    don't pin that exact string here (it's an implementation detail),
    but we DO pin two contracts:

      * the row has a non-NULL dedupe_key column, AND
      * a second INSERT with the same key is a no-op (the UNIQUE
        index on ``unit_events.dedupe_key`` is what enforces the
        dedupe — without a key the second insert succeeds and the
        single-event invariant breaks).
    """

    def test_cap_hit_event_carries_dedupe_key(self, tmp_state_db, monkeypatch):
        """After the cap fires, the ``spawn_failure_cap_hit`` row's
        ``dedupe_key`` column MUST be non-NULL.
        """
        _seed_plan()
        _capture_pushes(monkeypatch)
        _FailingBlocking.calls = 0
        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _FailingBlocking)
        for i in range(execution.SPAWN_FAILURE_CAP):
            execution.spawn_unit(FEATURE_ID, UNIT_ID)
            if i < execution.SPAWN_FAILURE_CAP - 1:
                state.cancel_unit(UNIT_ID)

        with state._connect() as conn:
            rows = conn.execute(
                "SELECT dedupe_key FROM unit_events "
                "WHERE unit_id = ? AND event_type = 'spawn_failure_cap_hit'",
                (UNIT_ID,),
            ).fetchall()
        assert len(rows) == 1, f"expected exactly 1 cap-hit row, got {len(rows)}"
        assert rows[0]["dedupe_key"], (
            "cap-hit event MUST carry a non-NULL dedupe_key so a same-"
            "process retry doesn't double-record"
        )

    def test_cap_hit_dedupe_key_blocks_second_insert(self, tmp_state_db, monkeypatch):
        """A second ``record_event`` call with the SAME dedupe_key MUST
        be a no-op via the UNIQUE index. This is what makes the cap-hit
        path idempotent under same-process retries.
        """
        _seed_plan()
        _capture_pushes(monkeypatch)
        _FailingBlocking.calls = 0
        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _FailingBlocking)
        for i in range(execution.SPAWN_FAILURE_CAP):
            execution.spawn_unit(FEATURE_ID, UNIT_ID)
            if i < execution.SPAWN_FAILURE_CAP - 1:
                state.cancel_unit(UNIT_ID)

        # Grab the dedupe_key the cap path wrote, then try to write a
        # duplicate row by hand. INSERT OR IGNORE must drop it (rowcount=0).
        with state._connect() as conn:
            key_row = conn.execute(
                "SELECT dedupe_key FROM unit_events "
                "WHERE unit_id = ? AND event_type = 'spawn_failure_cap_hit'",
                (UNIT_ID,),
            ).fetchone()
        dedupe_key = key_row["dedupe_key"]
        assert dedupe_key  # sanity — earlier test already asserted this

        inserted = state.record_event(
            UNIT_ID,
            FEATURE_ID,
            "spawn_failure_cap_hit",
            cycle_number=0,
            summary="duplicate same-process attempt",
            dedupe_key=dedupe_key,
        )
        assert inserted is False, (
            "duplicate cap-hit insert with the same dedupe_key MUST be a no-op"
        )

        with state._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM unit_events "
                "WHERE unit_id = ? AND event_type = 'spawn_failure_cap_hit'",
                (UNIT_ID,),
            ).fetchone()["n"]
        assert count == 1, (
            f"expected exactly 1 cap-hit row after duplicate INSERT OR IGNORE, got {count}"
        )


# ===========================================================================
# (B) cycle_number discrimination on the cap counter
# ===========================================================================


class TestCapCounterCycleDiscrimination:
    """Spec § attempt cap: "Count consecutive spawn_coder → coder_error
    cycles at cycle_number=0".

    A ``coder_error`` at cycle 1 or 2 (mid-fix-loop) MUST NOT
    increment the spawn-cap counter — the cap is for ghost-row blocking-
    spawn timeouts, not for fix-loop failures (those have their own
    CAP_3 governance in ``cycle_review``).
    """

    def test_coder_error_at_cycle_1_does_not_increment_counter(self, tmp_state_db):
        """Two coder_error rows at cycle_number=1 MUST count as zero
        spawn-cap failures."""
        _seed_plan()
        _seed_unit(status="fixing")
        for _ in range(2):
            state.record_event(
                UNIT_ID,
                FEATURE_ID,
                "coder_error",
                cycle_number=1,  # mid-fix-loop, NOT a fresh spawn
                summary="fix-loop transient error",
            )
        assert execution._consecutive_failed_spawns(UNIT_ID) == 0, (
            "coder_error at cycle_number>0 MUST NOT contribute to spawn cap"
        )

    def test_mixed_cycle_errors_only_count_cycle_zero(self, tmp_state_db):
        """Interleaved cycle-0 and cycle-1 coder_errors: only the
        cycle-0 ones count."""
        _seed_plan()
        _seed_unit(status="escalated")
        # Two cycle-0 (count) + two cycle-1 (skip) + one cycle-0 (count) = 3
        for cyc in (0, 1, 0, 1, 0):
            state.record_event(
                UNIT_ID,
                FEATURE_ID,
                "coder_error",
                cycle_number=cyc,
                summary=f"err at cycle {cyc}",
            )
        assert execution._consecutive_failed_spawns(UNIT_ID) == 3


# ===========================================================================
# (C) cap fires immediately on pre-seeded failure history
# ===========================================================================


class TestCapFiresOnPreSeededHistory:
    """Spec § U-9: the cap counter is event-table-derived, not held in
    memory. A fresh ``spawn_unit`` against a unit whose history already
    shows ``SPAWN_FAILURE_CAP`` coder_error rows at cycle 0 MUST
    cap-escalate BEFORE invoking the worker, even on the first
    in-process call.

    This is the contract that lets a daemon-restart or a fresh MCP
    process pick up where the previous one left off.
    """

    def test_cap_escalates_without_invoking_worker(self, tmp_state_db, monkeypatch):
        _seed_plan()
        # Row exists in cancelled state so ghost-row guard lets the
        # spawn through to the cap check.
        _seed_unit(status="cancelled")
        for _ in range(execution.SPAWN_FAILURE_CAP):
            state.record_event(
                UNIT_ID,
                FEATURE_ID,
                "coder_error",
                cycle_number=0,
                summary="pre-seeded failure",
            )

        pushes = _capture_pushes(monkeypatch)
        _FailingBlocking.calls = 0
        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _FailingBlocking)

        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
        assert msg.startswith("ESCALATED"), f"expected cap-escalation message, got: {msg!r}"
        assert _FailingBlocking.calls == 0, (
            "cap-3 backstop MUST short-circuit before invoking the worker"
        )

        cap_pushes = [p for p in pushes if p["reason_slug"] == "spawn_failure_cap"]
        assert len(cap_pushes) == 1
        cap_events = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "spawn_failure_cap_hit"
        ]
        assert len(cap_events) == 1


# ===========================================================================
# (D) ANY session-bearing event resets the counter
# ===========================================================================


class TestAnySessionBearingEventResetsCounter:
    """Spec § U-9 reset rule: "any row whose ``session_id`` column is
    non-empty" zeroes the counter — not just the specific events the
    spawn paths happen to write.

    This proves the reset is by-column, not by-event-type, so future
    role-event additions (resume, marker scan, ...) that carry a
    session_id legitimately reset too.
    """

    def test_non_spawn_event_with_session_id_resets_counter(self, tmp_state_db):
        _seed_plan()
        _seed_unit(status="cancelled")
        # 2 cycle-0 coder_errors
        for _ in range(2):
            state.record_event(
                UNIT_ID,
                FEATURE_ID,
                "coder_error",
                cycle_number=0,
                summary="failed spawn",
            )
        assert execution._consecutive_failed_spawns(UNIT_ID) == 2

        # An unrelated event with session_id — e.g. coder resume —
        # resets the counter via the by-column rule.
        state.record_event(
            UNIT_ID,
            FEATURE_ID,
            "coder_resumed",
            cycle_number=0,
            summary="resume after manual intervention",
            session_id="sesn-resumed-xyz",
        )
        assert execution._consecutive_failed_spawns(UNIT_ID) == 0, (
            "any session-bearing event MUST reset the counter, not just the "
            "specific 'happy path' events the spawn primitives emit"
        )

    def test_failure_after_reset_starts_from_zero(self, tmp_state_db):
        _seed_plan()
        _seed_unit(status="cancelled")
        for _ in range(2):
            state.record_event(UNIT_ID, FEATURE_ID, "coder_error", cycle_number=0, summary="fail")
        # Reset signal
        state.record_event(
            UNIT_ID,
            FEATURE_ID,
            "coder_session_persisted",
            cycle_number=0,
            summary="ok",
            session_id="sesn-reset",
        )
        # One new failure → counter is 1, not 3
        state.record_event(UNIT_ID, FEATURE_ID, "coder_error", cycle_number=0, summary="new fail")
        assert execution._consecutive_failed_spawns(UNIT_ID) == 1


# ===========================================================================
# (E) ci_drift dedupe with disabled rate-limit window
# ===========================================================================


class TestCiDriftDedupeDisabledWindow:
    """Spec § U-9 dedupe rules: "Rate-limit window expired" is one of
    two conditions; "failing-check-set changed" is the other. Setting
    ``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS=0`` MUST disable the time
    backstop but PRESERVE content-dedupe: an unchanged set stays
    suppressed forever, a changed set still re-emits.

    Without this, an operator who turns off the time window to debug
    a stale-event problem would re-introduce the original storm.
    """

    def test_disabled_window_still_suppresses_unchanged_set(self, tmp_state_db, monkeypatch):
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "0")
        _seed_plan()
        unit = _seed_unit(status="in_ci", pr_number=42)

        for _ in range(20):
            tools_health._apply_action(unit, _drift_action(["test"]))

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        assert len(rows) == 1, (
            f"with ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS=0, an unchanged set "
            f"MUST still be suppressed by content-dedupe; got {len(rows)} rows"
        )

    def test_disabled_window_still_emits_on_changed_set(self, tmp_state_db, monkeypatch):
        monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "0")
        _seed_plan()
        unit = _seed_unit(status="in_ci", pr_number=42)

        tools_health._apply_action(unit, _drift_action(["test"]))
        tools_health._apply_action(unit, _drift_action(["lint"]))
        # back to original set — already-seen, so suppressed
        tools_health._apply_action(unit, _drift_action(["test"]))

        rows = [e for e in state.list_events(UNIT_ID) if e["event_type"] == "ci_drift_detected"]
        # First emit + changed-set emit → 2. The "back to test" doesn't
        # re-emit because the LAST drift row's set is {"lint"}, which
        # differs from the new {"test"} — so this does re-emit. Reading
        # the impl carefully: dedupe compares against the most-recent
        # prior row only. So we expect 3, not 2.
        assert len(rows) == 3, (
            f"changed-set re-emits MUST hold regardless of window; got {len(rows)} "
            f"rows (details: {[r['details'] for r in rows]!r})"
        )


# ===========================================================================
# (F) Ghost-row refusal recovery surfaces
# ===========================================================================


class TestRefusalNamesAllRecoveryPrimitives:
    """The ghost-row refusal MUST name every documented recovery
    primitive (``inspect_unit_health``, ``resume_unit``, ``cancel_unit``)
    so a chat-only caller can recover without grepping source. Spec § U-9
    "REFUSE with an actionable error pointing the caller at
    inspect_unit_health / resume_unit (check real state) or cancel_unit
    (explicit reset before re-dispatch)".
    """

    def test_refusal_mentions_all_three_primitives(self, tmp_state_db):
        _seed_plan()
        _seed_unit(status="coding", coder_session_id="")
        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
        assert msg.startswith("ERROR"), msg
        for primitive in ("inspect_unit_health", "resume_unit", "cancel_unit"):
            assert primitive in msg, (
                f"ghost-row refusal MUST name {primitive!r} per spec § U-9; got: {msg!r}"
            )

    def test_refusal_reports_current_status(self, tmp_state_db):
        """Operator-actionability: the refusal MUST name the actual
        status so a chat reader can decide between
        ``inspect_unit_health`` (active row) and ``cancel_unit``
        (escalated row).
        """
        _seed_plan()
        _seed_unit(status="reviewing")
        msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
        assert "'reviewing'" in msg, f"refusal MUST report current status verbatim; got: {msg!r}"


# ===========================================================================
# (G) async happy path writes the coder_session_persisted reset signal
# ===========================================================================


class TestAsyncHappyPathResetSignal:
    """Spec § U-9 cap reset (impl comment): the async happy path records
    a ``coder_session_persisted`` event right after the work_units row
    update. This event is what ``_consecutive_failed_spawns`` walks
    back to as a reset signal — without it, the async happy path
    leaves no event-side proof of success and a later transient
    failure would be falsely counted.
    """

    def test_coder_session_persisted_event_is_recorded(self, tmp_state_db, monkeypatch):
        _seed_plan()
        worker = _SucceedingAsync("coder")
        monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda _r: worker)
        msg = execution.spawn_unit_async(FEATURE_ID, UNIT_ID)
        assert "ERROR" not in msg, msg

        rows = [
            e for e in state.list_events(UNIT_ID) if e["event_type"] == "coder_session_persisted"
        ]
        assert len(rows) == 1, (
            f"async happy path MUST record exactly one coder_session_persisted "
            f"event; got {len(rows)} (events: "
            f"{[e['event_type'] for e in state.list_events(UNIT_ID)]!r})"
        )
        assert rows[0]["session_id"], (
            "coder_session_persisted event MUST carry the session_id in its "
            "session_id column so _consecutive_failed_spawns treats it as a "
            "reset signal"
        )
        assert rows[0]["cycle_number"] == 0


# ===========================================================================
# (H) Cap escalation message is actionable
# ===========================================================================


class TestCapEscalationMessageActionability:
    """The cap-escalation string returned to chat MUST mention the cap
    threshold (so the user knows *why* this fired) and describe the
    likely root cause (the worker-backend network timeout loop). The
    spec calls this out under "force status=escalated + last_error,
    fire the ntfy escalation push, refuse further auto-spawn"; the
    escalation prose IS the human-readable surface.
    """

    def test_escalation_message_mentions_cap_threshold(self, tmp_state_db, monkeypatch):
        _seed_plan()
        _capture_pushes(monkeypatch)
        _FailingBlocking.calls = 0
        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _FailingBlocking)
        msg = ""
        for i in range(execution.SPAWN_FAILURE_CAP):
            msg = execution.spawn_unit(FEATURE_ID, UNIT_ID)
            if i < execution.SPAWN_FAILURE_CAP - 1:
                state.cancel_unit(UNIT_ID)

        assert msg.startswith("ESCALATED"), msg
        assert str(execution.SPAWN_FAILURE_CAP) in msg, (
            f"cap-escalation message MUST surface the cap threshold "
            f"({execution.SPAWN_FAILURE_CAP}); got: {msg!r}"
        )
        # Must also surface enough context for the user to triage.
        assert "session" in msg.lower(), (
            f"escalation message must mention session-id context; got: {msg!r}"
        )
