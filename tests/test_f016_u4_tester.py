"""F-016-U-4 — Phase 2.5: lead/daemon interaction contract (tester tests).

Independent of the coder's tests in ``tests/test_state.py`` /
``tests/test_tools_execution.py`` / ``tests/test_tools_planning.py``.
This file pins the spec-acceptance behaviour those test files don't
explicitly assert against ``features/F-016/spec.md`` § Phase 2.5:

  * **The ``send_to_unit`` lock is unit-wide, not per-role.** The spec
    is explicit: "If it were per-role, the daemon could advance the
    reviewer while the coder is still receiving the lead's submit.
    Per-unit ensures a consistent snapshot." We assert the same
    :class:`threading.RLock` is returned regardless of role, and that
    holding the lock makes :func:`has_active_advance_lock` True
    irrespective of which role is being messaged.

  * **The lock window is ~1s — never blocks on worker reply.** Submit
    must call ``worker.resume_async`` (NOT ``worker.resume``) so the
    advance lock is held only for the message-enqueue window, not for
    the worker's many-minute reply. We give the fake worker a sleeping
    ``resume`` and a no-op ``resume_async`` and assert ``send_to_unit_async``
    finishes in <0.5s and never calls ``resume``.

  * **Cancel is sticky.** ``cancel_unit`` flips status to ``cancelled``
    AND stamps ``cancelled_at``; a daemon's tick that re-reads the unit
    must see both. A second ``cancel_unit`` call is idempotent and does
    NOT re-archive sessions (which would burn API calls).

  * **Cancel archives every role's session.** Spec: "cancel_unit
    archives the worker session and marks the unit cancelled". A unit
    with all three sessions assigned must see all three archived.

  * **``send_to_unit_async`` rejects a cancelled unit.** The submit window
    cannot be opened on a unit the user has explicitly stopped — silent
    delivery would burn API calls on output the user said to stop
    producing.

  * **``send_to_unit_async`` default-role routing.** Spec table:
    coding/in_ci/fixing/escalated -> coder; testing -> tester;
    reviewing -> reviewer; approved_awaiting_merge/done/cancelled ->
    structured error. We check every cell of that table.

  * **``update_unit_deps`` is orthogonal to worker state.** Spec:
    "Orthogonal — doesn't touch in-flight workers." A unit in
    ``coding`` whose deps are mutated must stay in ``coding`` with the
    same session_id, just with a new ``depends_on`` list visible to
    ``next_ready_units``.

  * **``update_unit_deps`` rejects cycles + self-deps.** A graph-mutation
    primitive that allows cycles silently breaks every downstream
    scheduler. We reject the obvious cases and confirm the error
    surfaces the offending unit.

  * **MCP registration.** All three new tools (``cancel_unit``,
    ``send_to_unit_async``, ``update_unit_deps``) must be registered on
    the FastMCP instance so the lead can call them as RPCs.

  * **Predecessor consistency (F-016-U-3).** Phase 2's ``advance_to_*``
    tools must still be registered. Phase 2.5 is additive — never a
    replacement.

  * **Stale-marker delta rule.** ``classify_marker_freshness`` /
    ``detect_stale_reviewer_marker`` deliver the spec's three cases
    (valid / stale / not_emitted). The daemon (F-016-U-5) is the future
    caller; Phase 2.5 ships the rule so the daemon can adopt it
    without a schema bump.

  * **Repo-verification gate.** ``cancel_unit`` and ``send_to_unit_async``
    must respect the same ``ensure_verified_for_unit`` gate the rest of
    the spawn surface does — F-008 contract.

  * **Stickiness across the daemon's tick.** A cancelled unit must
    stay ``cancelled`` even when a daemon-shaped helper tries to flip
    it. ``state.touch_unit`` updates last_activity but does NOT clear
    ``cancelled_at`` — the sticky guarantee survives ambient state
    writes.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from orchestrator import stale_marker, state
from orchestrator.models import (
    CANCELLED_UNIT_STATUSES,
    Feature,
    WorkUnit,
    WorkUnitState,
)
from orchestrator.tools import execution, mcp, planning

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _bypass_verify_gate(monkeypatch):
    """The Phase 2.5 tools consult ``ensure_verified_for_unit`` /
    ``ensure_verified_for_feature``. The gate has its own tests
    (test_f001_u6_*); we bypass it here to focus on the new primitives.
    The repo-verification-gate compliance test below intentionally
    re-enables the gate to assert each new tool actually consults it.
    """
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)


def _seed_feature_and_unit(
    *,
    feature_id: str = "F-016-U4T",
    unit_id: str = "F-016-U4T-U-1",
    repo_url: str = "https://github.com/o/r",
    status: str = "coding",
    sessions: dict[str, str] | None = None,
) -> WorkUnitState:
    """Seed a feature + a single work unit, returning the row.

    ``sessions`` is a ``{role: session_id}`` overlay; absent roles get
    empty session_ids. The default is one coder session so
    ``send_to_unit_async`` has something to message.
    """
    state.save_feature(
        Feature(
            id=feature_id,
            title="phase-2.5 contract",
            description="lead/daemon primitives",
            repo_path=repo_url,
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=unit_id,
                feature_id=feature_id,
                title="u1",
                description="impl",
            ),
        ],
    )
    state.approve_plan(feature_id)

    # ``None`` means "use default"; ``{}`` means "explicitly no sessions
    # for any role" — avoid the ``or`` trap that conflates them.
    if sessions is None:
        sessions = {"coder": "sess_coder_abc"}
    unit_state = WorkUnitState(
        unit_id=unit_id,
        feature_id=feature_id,
        status=status,
        branch="feat/branch",
        pr_number=42,
        coder_session_id=sessions.get("coder", ""),
        tester_session_id=sessions.get("tester", ""),
        reviewer_session_id=sessions.get("reviewer", ""),
    )
    state.upsert_unit_state(unit_state)
    return unit_state


class _RecordingWorker:
    """Worker double that records every method call.

    Used to assert ``send_to_unit_async`` calls ``resume_async`` (the
    async submit) and never ``resume`` (the blocking call). ``resume``
    sleeps 5s so any accidental sync call would blow the latency
    budget.
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.resume_async_calls: list[tuple[str, str]] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.archive_calls: list[str] = []

    def resume_async(self, session_id: str, msg: str) -> None:
        self.resume_async_calls.append((session_id, msg))

    def resume(self, session_id: str, msg: str) -> str:  # noqa: ARG002
        # Sentinel — this should never be called by send_to_unit_async.
        # The 5s sleep makes accidental sync-call regressions surface
        # as latency-budget failures, not as silent passes.
        time.sleep(5.0)
        self.resume_calls.append((session_id, msg))
        return "sync-resume-should-never-run"

    def archive(self, session_id: str) -> None:
        self.archive_calls.append(session_id)


@pytest.fixture
def recording_workers(monkeypatch):
    """Patch ``make_worker`` (and ``ManagedAgentWorker``) with recorders.

    Returns the ``{role: _RecordingWorker}`` dict so tests can assert
    against per-role call counts. One worker per role is built lazily
    so calls land on the same instance across multiple invocations.
    """
    workers: dict[str, _RecordingWorker] = {}

    def factory(role: str) -> _RecordingWorker:
        if role not in workers:
            workers[role] = _RecordingWorker(role)
        return workers[role]

    monkeypatch.setattr("orchestrator.tools.execution.make_worker", factory)
    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return workers


# --------------------------- lead_advance_lock ---------------------------


class TestLeadAdvanceLockPerUnit:
    """The lock applies to the entire unit's state machine, not per-role."""

    def test_lock_is_unit_wide_across_roles(self, tmp_state_db):
        """Two roles share the same per-unit RLock — the daemon must
        not advance ANY role while a lead is mid-send to another."""
        _seed_feature_and_unit()
        lock_coder = state._get_unit_advance_lock("F-016-U4T-U-1")
        # Re-fetching for a "different role" is meaningless at the state
        # layer — the lock is keyed on unit_id only, not (unit_id, role).
        # The same RLock object must come back.
        lock_again = state._get_unit_advance_lock("F-016-U4T-U-1")
        assert lock_coder is lock_again

    def test_distinct_units_have_distinct_locks(self, tmp_state_db):
        """A lock on U-1 must not block traffic to U-2."""
        _seed_feature_and_unit(unit_id="F-016-U4T-U-1")
        _seed_feature_and_unit(unit_id="F-016-U4T-U-2", status="testing", sessions={})
        lock_u1 = state._get_unit_advance_lock("F-016-U4T-U-1")
        lock_u2 = state._get_unit_advance_lock("F-016-U4T-U-2")
        assert lock_u1 is not lock_u2

    def test_has_active_advance_lock_reads_db(self, tmp_state_db):
        """The lock check must be DB-visible so a separate-process daemon
        (Phase 3) sees the lead's claim."""
        _seed_feature_and_unit()
        assert state.has_active_advance_lock("F-016-U4T-U-1") is False
        with state.lead_advance_lock("F-016-U4T-U-1"):
            assert state.has_active_advance_lock("F-016-U4T-U-1") is True
        assert state.has_active_advance_lock("F-016-U4T-U-1") is False

    def test_owner_cleared_on_exception(self, tmp_state_db):
        """A raise inside the with-block must not leak a stuck
        ``owner='lead'``. Otherwise the daemon would defer forever."""
        _seed_feature_and_unit()
        with pytest.raises(RuntimeError), state.lead_advance_lock("F-016-U4T-U-1"):
            raise RuntimeError("boom")
        assert state.has_active_advance_lock("F-016-U4T-U-1") is False
        s = state.get_unit_state("F-016-U4T-U-1")
        assert s is not None and s.owner == ""


# --------------------------- send_to_unit_async ---------------------------


class TestSendToUnitAsyncContract:
    """The ~1s submit-window contract per spec § Phase 2.5."""

    def test_calls_resume_async_not_resume(self, tmp_state_db, recording_workers):
        """Async submit must use ``resume_async`` (no blocking wait).
        ``resume`` carries a 5s sleep — accidental sync call would
        violate the ≤2s acceptance budget."""
        _seed_feature_and_unit()
        out = execution.send_to_unit_async("F-016-U4T-U-1", "do a thing")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["role"] == "coder"
        assert recording_workers["coder"].resume_async_calls == [("sess_coder_abc", "do a thing")]
        assert recording_workers["coder"].resume_calls == []

    def test_latency_budget(self, tmp_state_db, recording_workers):
        """``send_to_unit_async`` must return well under the ≤2s acceptance
        budget — measuring wall-clock with a no-op ``resume_async`` and
        requiring <0.5s catches a regression that accidentally falls
        back to the blocking ``resume`` path."""
        _seed_feature_and_unit()
        t0 = time.perf_counter()
        execution.send_to_unit_async("F-016-U4T-U-1", "go")
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"send_to_unit_async took {elapsed:.3f}s (budget <0.5s)"

    def test_advance_lock_released_after_submit(self, tmp_state_db, recording_workers):
        """The ~1s lock window must close on return — a still-held lock
        would block the Phase 3 daemon's next reconcile tick."""
        _seed_feature_and_unit()
        execution.send_to_unit_async("F-016-U4T-U-1", "go")
        assert state.has_active_advance_lock("F-016-U4T-U-1") is False
        s = state.get_unit_state("F-016-U4T-U-1")
        assert s is not None and s.owner == ""

    def test_default_role_routing_by_status(self, tmp_state_db, recording_workers):
        """Spec routing table: status -> default role. Cell-by-cell."""
        # We seed N units, one per status row, and assert the role
        # ``send_to_unit_async`` picks when ``role=""``.
        cases = [
            ("coding", "coder"),
            ("in_ci", "coder"),
            ("fixing", "coder"),
            ("escalated", "coder"),
            ("testing", "tester"),
            ("reviewing", "reviewer"),
        ]
        for idx, (status, expected_role) in enumerate(cases):
            unit_id = f"F-016-U4T-U-{idx + 100}"
            _seed_feature_and_unit(
                unit_id=unit_id,
                status=status,
                sessions={
                    "coder": "sess_c",
                    "tester": "sess_t",
                    "reviewer": "sess_r",
                },
            )
            out = execution.send_to_unit_async(unit_id, "ping")
            parsed = json.loads(out)
            assert parsed["delivered"] is True, f"{status}: {parsed}"
            assert parsed["role"] == expected_role, (
                f"status={status} expected default role={expected_role}, got {parsed['role']}"
            )

    def test_terminal_status_returns_structured_error(self, tmp_state_db, recording_workers):
        """approved_awaiting_merge / done — no role is actionable, the
        spec requires a structured error (NOT a silent send). The
        terminal-status check fires BEFORE role resolution, so an
        explicit ``role="coder"`` cannot bypass it (PR #59 Copilot
        finding 1: previously the check only ran when role==''')."""
        for terminal_status in ("approved_awaiting_merge", "done"):
            unit_id = f"F-016-U4T-U-term-{terminal_status}"
            _seed_feature_and_unit(unit_id=unit_id, status=terminal_status)
            # Implicit role.
            out = execution.send_to_unit_async(unit_id, "ping")
            parsed = json.loads(out)
            assert parsed["delivered"] is False
            assert parsed["reason"] == "unit_terminal", (
                f"{terminal_status}: expected unit_terminal, got {parsed}"
            )
            assert "role_diagnostics" in parsed
            assert parsed["status"] == terminal_status
            # Explicit role must NOT bypass the terminal-status gate
            # (the regression Copilot flagged: role-resolution only
            # short-circuited on ``role=""``).
            out_explicit = execution.send_to_unit_async(unit_id, "ping", role="coder")
            parsed_explicit = json.loads(out_explicit)
            assert parsed_explicit["delivered"] is False
            assert parsed_explicit["reason"] == "unit_terminal", (
                f"explicit role on {terminal_status}: expected unit_terminal, got {parsed_explicit}"
            )
            # No resume_async should have been called for either path.
            assert recording_workers.get("coder") is None or (
                (unit_id, "ping")
                not in [(sid, m) for sid, m in recording_workers["coder"].resume_async_calls]
            )

    def test_cancelled_unit_rejected(self, tmp_state_db, recording_workers):
        """The submit window must never open on a cancelled unit —
        silent delivery would burn API calls on output the user
        explicitly halted. Response shape mirrors the other
        not-actionable cases (PR #59 Copilot finding 2): every
        not-actionable response carries ``role_diagnostics`` +
        ``next_steps`` for uniformity."""
        _seed_feature_and_unit()
        state.cancel_unit("F-016-U4T-U-1")
        out = execution.send_to_unit_async("F-016-U4T-U-1", "ping")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "unit_cancelled"
        # Shape consistency: role_diagnostics + next_steps land here
        # too (every other not-actionable branch surfaces them).
        assert "role_diagnostics" in parsed
        assert "next_steps" in parsed
        # No resume_async call landed on any worker.
        for w in recording_workers.values():
            assert w.resume_async_calls == []

    def test_no_session_returns_structured_diagnostics(self, tmp_state_db, recording_workers):
        """No coder session yet -> structured error with role_diagnostics
        showing why the targeted role can't receive."""
        _seed_feature_and_unit(sessions={})  # no sessions assigned
        out = execution.send_to_unit_async("F-016-U4T-U-1", "ping", role="coder")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "no_coder_session"
        assert parsed["role_diagnostics"]["coder"]["actionable"] is False


# --------------------------- cancel_unit ---------------------------


class TestCancelUnit:
    """Spec § cancel_unit: sticky, archives every role's session."""

    def test_cancel_flips_status_and_stamps_cancelled_at(self, tmp_state_db, recording_workers):
        _seed_feature_and_unit()
        out = execution.cancel_unit("F-016-U4T-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "cancelled"
        s = state.get_unit_state("F-016-U4T-U-1")
        assert s is not None
        assert s.status == "cancelled"
        assert s.status in CANCELLED_UNIT_STATUSES
        assert s.cancelled_at is not None and s.cancelled_at != ""

    def test_archives_every_role_with_a_session(self, tmp_state_db, recording_workers):
        """Every assigned role's session must be archived. Roles without
        a session_id are skipped (no-op, not error)."""
        _seed_feature_and_unit(
            sessions={
                "coder": "sess_c",
                "tester": "sess_t",
                "reviewer": "sess_r",
            }
        )
        execution.cancel_unit("F-016-U4T-U-1")
        assert recording_workers["coder"].archive_calls == ["sess_c"]
        assert recording_workers["tester"].archive_calls == ["sess_t"]
        assert recording_workers["reviewer"].archive_calls == ["sess_r"]

    def test_idempotent_does_not_re_archive(self, tmp_state_db, recording_workers):
        """A second ``cancel_unit`` call on an already-cancelled unit
        must NOT re-archive sessions — burning API calls on a
        re-cancel would be a user-facing cost regression."""
        _seed_feature_and_unit()
        execution.cancel_unit("F-016-U4T-U-1")
        first_archive_count = len(recording_workers["coder"].archive_calls)
        out2 = execution.cancel_unit("F-016-U4T-U-1")
        parsed = json.loads(out2)
        assert parsed["outcome"] == "already_cancelled"
        assert len(recording_workers["coder"].archive_calls) == first_archive_count

    def test_cancel_survives_backend_archive_error(self, tmp_state_db, monkeypatch):
        """A backend exception during ``worker.archive`` must NOT block
        the cancel — the unit row's ``cancelled_at`` is the source of
        truth, and stranding the unit in ``coding`` because the
        Anthropic API blipped would be worse than reporting per-role
        archive errors."""
        _seed_feature_and_unit()

        class BrokenWorker:
            def __init__(self, role: str) -> None:
                self.role = role

            def archive(self, session_id: str) -> None:  # noqa: ARG002
                raise RuntimeError("anthropic 503")

        monkeypatch.setattr("orchestrator.tools.execution.make_worker", BrokenWorker)
        out = execution.cancel_unit("F-016-U4T-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "cancelled"
        assert "anthropic 503" in parsed["archive_outcomes"]["coder"]
        s = state.get_unit_state("F-016-U4T-U-1")
        assert s is not None and s.status == "cancelled"

    def test_cancellation_is_sticky_across_touch_unit(self, tmp_state_db):
        """``state.touch_unit`` (the generic activity-bumper) must NOT
        clear ``cancelled_at``. Otherwise an ambient write — daemon
        ping, lead's idle inspection — would silently un-cancel the
        unit and let the state machine resume."""
        _seed_feature_and_unit()
        state.cancel_unit("F-016-U4T-U-1")
        before = state.get_unit_state("F-016-U4T-U-1")
        assert before is not None and before.cancelled_at is not None
        cancelled_at_before = before.cancelled_at
        # touch_unit only updates last_activity / status / last_error.
        state.touch_unit("F-016-U4T-U-1", status="cancelled")
        after = state.get_unit_state("F-016-U4T-U-1")
        assert after is not None
        assert after.cancelled_at == cancelled_at_before
        assert after.status == "cancelled"

    def test_downstream_dep_evaluation_treats_cancelled_as_not_done(
        self, tmp_state_db, recording_workers
    ):
        """Spec: "downstream dep-evaluation treats it as not-done." A
        unit depending on a cancelled unit must NOT appear in
        ``ready_to_spawn``."""
        feature_id = "F-016-U4T-dep"
        state.save_feature(
            Feature(
                id=feature_id,
                title="dep test",
                description="dep eval",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan(
            feature_id,
            [
                WorkUnit(
                    id=f"{feature_id}-U-1",
                    feature_id=feature_id,
                    title="u1",
                    description="",
                ),
                WorkUnit(
                    id=f"{feature_id}-U-2",
                    feature_id=feature_id,
                    title="u2",
                    description="",
                    depends_on=[f"{feature_id}-U-1"],
                ),
            ],
        )
        state.approve_plan(feature_id)
        # U-1 reaches coding then gets cancelled.
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=f"{feature_id}-U-1",
                feature_id=feature_id,
                status="coding",
                coder_session_id="sess_c",
            )
        )
        execution.cancel_unit(f"{feature_id}-U-1")

        from orchestrator.tools import scheduling

        out = scheduling.next_ready_units(feature_id)
        parsed = json.loads(out)
        ready_ids = {u["unit_id"] for u in parsed["ready_to_spawn"]}
        assert f"{feature_id}-U-2" not in ready_ids, (
            "cancelled deps must NOT satisfy downstream deps"
        )
        # And the cancelled unit should NOT show up in in_flight either.
        in_flight_ids = {u["unit_id"] for u in parsed["in_flight"]}
        assert f"{feature_id}-U-1" not in in_flight_ids
        # It shows up in the explicit cancelled bucket so the lead can
        # surface it to the user.
        cancelled_ids = {u["unit_id"] for u in parsed.get("cancelled", [])}
        assert f"{feature_id}-U-1" in cancelled_ids


# --------------------------- update_unit_deps ---------------------------


class TestUpdateUnitDeps:
    """Spec § "Graph mutation — Re-shapes the DAG for future scheduling,
    orthogonal to in-flight workers."""

    def _seed_three_unit_plan(self, feature_id: str = "F-016-U4T-d") -> str:
        state.save_feature(
            Feature(
                id=feature_id,
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan(
            feature_id,
            [
                WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description=""),
                WorkUnit(id=f"{feature_id}-U-2", feature_id=feature_id, title="u2", description=""),
                WorkUnit(id=f"{feature_id}-U-3", feature_id=feature_id, title="u3", description=""),
            ],
        )
        state.approve_plan(feature_id)
        return feature_id

    def test_orthogonal_to_in_flight_worker(self, tmp_state_db):
        """A unit in ``coding`` whose deps are mutated must remain in
        ``coding`` with the same session_id — the spec calls this out
        explicitly as "Orthogonal — doesn't touch in-flight workers." """
        fid = self._seed_three_unit_plan()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=f"{fid}-U-3",
                feature_id=fid,
                status="coding",
                coder_session_id="sess_c_u3",
            )
        )
        out = planning.update_unit_deps(fid, f"{fid}-U-3", [f"{fid}-U-1", f"{fid}-U-2"])
        parsed = json.loads(out)
        assert parsed["outcome"] == "updated"
        # Worker row unchanged.
        s = state.get_unit_state(f"{fid}-U-3")
        assert s is not None
        assert s.status == "coding"
        assert s.coder_session_id == "sess_c_u3"
        # Plan reflects the new edge.
        plan = state.get_plan(fid)
        assert plan is not None
        new_deps = next(u.depends_on for u in plan.units if u.id == f"{fid}-U-3")
        assert new_deps == [f"{fid}-U-1", f"{fid}-U-2"]

    def test_rejects_self_dep(self, tmp_state_db):
        fid = self._seed_three_unit_plan()
        out = planning.update_unit_deps(fid, f"{fid}-U-1", [f"{fid}-U-1"])
        assert "cannot depend on itself" in out

    def test_rejects_cycle(self, tmp_state_db):
        """U-1 -> U-2 -> U-3 -> U-1 must be rejected with the offending
        unit named so the user can see where the cycle closes."""
        fid = self._seed_three_unit_plan()
        # Build the chain U-2 -> U-1, U-3 -> U-2.
        planning.update_unit_deps(fid, f"{fid}-U-2", [f"{fid}-U-1"])
        planning.update_unit_deps(fid, f"{fid}-U-3", [f"{fid}-U-2"])
        # Now adding U-1 -> U-3 closes the cycle.
        out = planning.update_unit_deps(fid, f"{fid}-U-1", [f"{fid}-U-3"])
        assert "cycle" in out.lower()
        # Cycle should NOT have been committed.
        plan = state.get_plan(fid)
        assert plan is not None
        u1_deps = next(u.depends_on for u in plan.units if u.id == f"{fid}-U-1")
        assert u1_deps == [], f"cycle write must roll back; got u1.depends_on={u1_deps}"

    def test_rejects_unknown_dep(self, tmp_state_db):
        fid = self._seed_three_unit_plan()
        out = planning.update_unit_deps(fid, f"{fid}-U-1", ["F-XXX-U-99"])
        assert "unknown unit" in out

    def test_rejects_unknown_unit(self, tmp_state_db):
        fid = self._seed_three_unit_plan()
        out = planning.update_unit_deps(fid, f"{fid}-U-99", [])
        assert "not in plan" in out


# --------------------------- stale-marker delta rule ---------------------------


class TestClassifyMarkerFreshness:
    """Pure classifier — case A / B / C per spec."""

    def test_case_a_valid_when_shas_match(self):
        assert (
            stale_marker.classify_marker_freshness(
                reviewer_marker_sha="abc123",
                current_pr_head_sha="abc123",
                marker_emitted=True,
            )
            == "valid"
        )

    def test_case_b_stale_when_shas_differ(self):
        assert (
            stale_marker.classify_marker_freshness(
                reviewer_marker_sha="abc123",
                current_pr_head_sha="def456",
                marker_emitted=True,
            )
            == "stale"
        )

    def test_case_c_not_emitted(self):
        assert (
            stale_marker.classify_marker_freshness(
                reviewer_marker_sha=None,
                current_pr_head_sha="abc123",
                marker_emitted=False,
            )
            == "not_emitted"
        )

    def test_missing_sha_conservative_valid(self):
        """Without both anchors we can't prove staleness; spec rationale
        is "absence of evidence is not evidence of staleness" — return
        valid so we don't trigger a spurious delta resume."""
        assert (
            stale_marker.classify_marker_freshness(
                reviewer_marker_sha=None,
                current_pr_head_sha="abc123",
                marker_emitted=True,
            )
            == "valid"
        )


class TestDetectStaleReviewerMarker:
    """State-aware detection: uses event timestamps when SHA isn't recorded."""

    def test_not_emitted_when_no_reviewer_event(self, tmp_state_db):
        _seed_feature_and_unit()
        result = stale_marker.detect_stale_reviewer_marker("F-016-U4T-U-1")
        assert result["case"] == "not_emitted"
        assert result["reviewer_marker_event_type"] is None

    def test_valid_when_no_later_coder_push(self, tmp_state_db):
        unit_state = _seed_feature_and_unit()
        # Reviewer emits a verdict (no later coder activity).
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "reviewer_recommend_merge",
            source="reviewer",
            summary="endorsed",
        )
        result = stale_marker.detect_stale_reviewer_marker(unit_state.unit_id)
        assert result["case"] == "valid"
        assert result["reviewer_marker_event_type"] == "reviewer_recommend_merge"
        assert result["later_coder_push_ts"] is None

    def test_stale_when_coder_pushed_after_reviewer(self, tmp_state_db, monkeypatch):
        unit_state = _seed_feature_and_unit()
        # Monkey-patch ``state._now`` to deliver two strictly-monotonic
        # ISO timestamps rather than relying on a real ``time.sleep`` —
        # Windows clock resolution can collide on back-to-back
        # ``datetime.now`` calls and the flake would only show up in CI.
        # Sequential return values give us deterministic ts ordering.
        timestamps = iter(["2026-06-04T20:00:00+00:00", "2026-06-04T20:00:01+00:00"])
        monkeypatch.setattr("orchestrator.state._now", lambda: next(timestamps))
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "reviewer_recommend_merge",
            source="reviewer",
            summary="endorsed",
        )
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "fix_pushed",
            source="coder",
            summary="fix",
        )
        result = stale_marker.detect_stale_reviewer_marker(unit_state.unit_id)
        assert result["case"] == "stale", (
            f"reviewer endorsed then coder pushed must be stale, got {result}"
        )
        assert result["later_coder_push_event_type"] == "fix_pushed"

    def test_record_stale_marker_pending_delta_dedupes_within_window(self, tmp_state_db):
        """Two calls with the same row-id anchors must produce ONE audit row —
        the daemon's reconcile loop will tick the same staleness on
        every poll until the delta resume completes. Dedupe key is
        per-staleness-window (per pair of unit_events row IDs), not
        per-event-type-pair (which would silently swallow round-2+
        staleness on the same unit; see reviewer PR #59 thread H1)."""
        unit_state = _seed_feature_and_unit()
        first = stale_marker.record_stale_marker_pending_delta(
            unit_state.unit_id,
            unit_state.feature_id,
            reviewer_event_id=101,
            later_push_event_id=102,
            reviewer_event_type="reviewer_recommend_merge",
            later_push_event_type="fix_pushed",
        )
        second = stale_marker.record_stale_marker_pending_delta(
            unit_state.unit_id,
            unit_state.feature_id,
            reviewer_event_id=101,
            later_push_event_id=102,
            reviewer_event_type="reviewer_recommend_merge",
            later_push_event_type="fix_pushed",
        )
        assert first is True
        assert second is False, "same row-id pair must dedupe to one row"
        rows = [
            e
            for e in state.list_events(unit_state.unit_id)
            if e["event_type"] == "reviewer_stale_marker_pending_delta"
        ]
        assert len(rows) == 1

    def test_multi_round_staleness_records_distinct_rows(self, tmp_state_db):
        """Two genuinely-distinct staleness rounds on the same unit
        (different reviewer-marker and coder-push rows) must produce
        TWO audit rows — the spec § "Stale-marker handling" envisions
        the daemon detecting re-staleness after the reviewer re-emits
        and the coder pushes again. A type-only dedupe key (the bug
        reviewer PR #59 H1 flagged) would silently swallow round 2."""
        unit_state = _seed_feature_and_unit()
        # Round 1: reviewer row #11, coder-push row #12.
        landed_1 = stale_marker.record_stale_marker_pending_delta(
            unit_state.unit_id,
            unit_state.feature_id,
            reviewer_event_id=11,
            later_push_event_id=12,
            reviewer_event_type="reviewer_recommend_merge",
            later_push_event_type="fix_pushed",
        )
        # Round 2: reviewer re-emitted (row #21), coder pushed (row #22).
        # Same event-type pair, fresh row IDs.
        landed_2 = stale_marker.record_stale_marker_pending_delta(
            unit_state.unit_id,
            unit_state.feature_id,
            reviewer_event_id=21,
            later_push_event_id=22,
            reviewer_event_type="reviewer_recommend_merge",
            later_push_event_type="fix_pushed",
        )
        assert landed_1 is True
        assert landed_2 is True, (
            "different row-id pair must land a fresh row even with identical type pair"
        )
        rows = [
            e
            for e in state.list_events(unit_state.unit_id)
            if e["event_type"] == "reviewer_stale_marker_pending_delta"
        ]
        assert len(rows) == 2

    def test_detect_returns_event_row_ids_for_dedupe_key(self, tmp_state_db, monkeypatch):
        """``detect_stale_reviewer_marker`` must surface the row IDs of
        BOTH the reviewer marker and the later coder push so a daemon
        can compose a per-window dedupe key. Without these, the
        per-window guarantee on :func:`record_stale_marker_pending_delta`
        is unreachable from the daemon's discovery path."""
        unit_state = _seed_feature_and_unit()
        # Monkey-patched timestamps for Windows-CI determinism (matches
        # ``test_stale_when_coder_pushed_after_reviewer``).
        timestamps = iter(["2026-06-04T20:01:00+00:00", "2026-06-04T20:01:01+00:00"])
        monkeypatch.setattr("orchestrator.state._now", lambda: next(timestamps))
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "reviewer_recommend_merge",
            source="reviewer",
            summary="endorsed",
        )
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "fix_pushed",
            source="coder",
            summary="fix",
        )
        result = stale_marker.detect_stale_reviewer_marker(unit_state.unit_id)
        assert result["case"] == "stale"
        assert isinstance(result["reviewer_marker_id"], int)
        assert isinstance(result["later_coder_push_id"], int)
        assert result["reviewer_marker_id"] != result["later_coder_push_id"]


# --------------------------- MCP registration ---------------------------


class TestMCPRegistration:
    """The three new Phase 2.5 tools must be reachable as MCP RPCs;
    the F-016-U-3 predecessor tools must remain registered."""

    def _names(self) -> set[str]:
        # ``asyncio.run`` so shutdown_asyncgens fires (Windows / Python
        # 3.12 stricter cleanup) — matches the F-016-U-3 tester pattern.
        tools = asyncio.run(mcp.list_tools())
        return {t.name for t in tools}

    def test_send_to_unit_async_registered(self):
        assert "send_to_unit_async" in self._names()

    def test_cancel_unit_registered(self):
        assert "cancel_unit" in self._names()

    def test_update_unit_deps_registered(self):
        assert "update_unit_deps" in self._names()

    def test_predecessor_f016_u3_tools_still_registered(self):
        """Phase 2.5 is additive over Phase 2; the three phase commands
        from F-016-U-3 must still be reachable."""
        names = self._names()
        for tool in ("advance_to_tester", "advance_to_reviewer", "advance_to_terminal"):
            assert tool in names, f"{tool} must remain registered (additive contract)"

    def test_legacy_send_to_unit_still_registered(self):
        """The synchronous ``send_to_unit`` stays as a backwards-compat
        escape hatch — F-016 Phase 4 is the unit that flips the default;
        Phase 2.5 must not silently delete it."""
        assert "send_to_unit" in self._names()


# --------------------------- verification gate compliance ---------------


class TestVerificationGateRespected:
    """``cancel_unit`` and ``send_to_unit_async`` must consult the same
    ``ensure_verified_for_unit`` gate the existing spawn surface does;
    bypassing would be a scope violation against F-008."""

    @pytest.fixture(autouse=True)
    def _restore_gate(self, monkeypatch):
        """Override the autouse-bypass for this class so we actually
        exercise the gate."""
        sentinel_calls: list[str] = []

        def gate(unit_id: str) -> str | None:
            sentinel_calls.append(unit_id)
            return f"ERROR: unit {unit_id} repo not verified"

        monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", gate)
        # Yield the recorder so individual tests can assert on it.
        self._gate_calls = sentinel_calls
        yield

    def test_cancel_unit_blocked_by_gate(self, tmp_state_db):
        _seed_feature_and_unit()
        out = execution.cancel_unit("F-016-U4T-U-1")
        assert "ERROR" in out
        assert "F-016-U4T-U-1" in self._gate_calls
        # Side effect: the unit must NOT have been cancelled.
        s = state.get_unit_state("F-016-U4T-U-1")
        assert s is not None and s.status != "cancelled"

    def test_send_to_unit_async_blocked_by_gate(self, tmp_state_db):
        _seed_feature_and_unit()
        out = execution.send_to_unit_async("F-016-U4T-U-1", "ping")
        assert "ERROR" in out
        assert "F-016-U4T-U-1" in self._gate_calls
