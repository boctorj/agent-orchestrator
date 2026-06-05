"""F-016-U-4 — Phase 2.5: lead/daemon interaction contract (extra tester tests).

Independent of ``tests/test_f016_u4_tester.py`` and the module-level tests
in ``tests/test_state.py`` / ``tests/test_tools_execution.py`` /
``tests/test_tools_planning.py``. This file locks down spec-acceptance
corners those files don't explicitly assert against
``features/F-016/spec.md`` § Phase 2.5 + the
``docs/PROPOSAL-async-orchestrator.md`` proposal it references:

  * **Sticky cancel survives ambient ``upsert_unit_state`` writes.** Per
    ``orchestrator/state.py`` § ``upsert_unit_state`` docstring:
    "``cancelled_at`` and ``owner`` (F-016 Phase 2.5) are intentionally
    NOT overwritten on UPDATE — they are managed via the dedicated
    ``cancel_unit`` / ``lead_advance_lock`` helpers and a stray
    ``upsert_unit_state`` call from somewhere else in the codebase
    (which constructs a fresh :class:`WorkUnitState` from the model
    defaults) must not silently clear a sticky cancel or break the
    daemon's CAS." A regression here = the sticky guarantee silently
    breaks, leaving the daemon free to resume a cancelled unit.

  * **``update_unit_deps`` does NOT reset the plan to ``draft``.** Per
    ``orchestrator/tools/planning.py`` § ``update_unit_deps`` docstring:
    "deliberately does NOT call approve_plan again". A regression that
    accidentally bundles graph-edit with save_plan logic would force
    re-approval on every dep tweak.

  * **``update_unit_deps`` with empty list clears all deps.** A graph
    primitive that can only ADD edges (never REMOVE) silently breaks
    the "U-3 no longer depends on U-2" use case.

  * **Concurrent ``send_to_unit_async`` on the same unit serialize.**
    Per the spec, the lock window is ~1s and the daemon must not race
    the lead's submit. The DB-visible ``owner='lead'`` claim is the
    Phase-3-daemon contract; the in-process :class:`threading.RLock` is
    the same-MCP-server-process contract. Two threads racing on the
    same unit must serialize.

  * **``next_ready_units_all`` aggregates ``cancelled`` across multiple
    features.** Spec § "downstream dep-evaluation treats cancelled as
    not-done". A user with cancelled units across two features must see
    BOTH in the global digest, not just whichever feature was iterated
    first.

  * **Stale-marker freshness rule excludes ``REVIEWER_REQUEST_CHANGES``.**
    Per ``orchestrator/stale_marker.py`` § ``REVIEWER_TERMINAL_EVENT_TYPES``
    docstring: "REQUEST_CHANGES is intentionally absent — a
    request-changes marker leaves the unit in ``fixing`` and the
    delta-review machinery already handles that path via the coder
    loop." A regression that adds REQUEST_CHANGES would emit spurious
    "stale_marker_pending_delta" rows on units that are healthily
    iterating tester/reviewer cycles.

  * **Stale-marker freshness rule includes ``reviewer_comment``.** Per
    the same frozenset, COMMENT counts as an emitted marker (the
    reviewer left a verdict that goes terminal as
    ``approved_awaiting_merge``). A regression dropping COMMENT would
    miss the staleness on comment-only endorsements.

  * **Stale-marker dedupe key includes unit_id.** Two different units
    with identical (reviewer_event_type, later_push_event_type) shapes
    must record SEPARATE audit rows. A key that omitted unit_id would
    silently swallow one unit's pending-delta row.

  * **``send_to_unit_async`` explicit ``role`` overrides status default.**
    Spec § "The lead overrides explicitly when its intent diverges from
    the current phase." Lead targeting the reviewer while the unit is
    in ``coding`` is legitimate (sending context for a later review);
    silent route-to-coder would lose that intent.

  * **``send_to_unit_async`` on ``pending`` status returns
    ``no_default_role``.** The route table only covers active /
    escalated statuses; ``pending`` (never spawned) and the cancelled /
    terminal trio are all "no default role" — silently picking one
    would corrupt the not-spawned-yet semantics.

  * **``send_to_unit_async`` audit row includes the message body.** The
    audit trail proves the lead actually sent something on this branch
    (vs. silent failure); a regression that records the event without
    the body would leave the cycle log mute.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from orchestrator import stale_marker, state
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, planning, scheduling

# --------------------------- shared helpers ---------------------------


@pytest.fixture(autouse=True)
def _bypass_verify_gate(monkeypatch):
    """Bypass the repo-verification gate for the extras tester file —
    its own coverage lives in ``tests/test_f001_u6_*`` and in
    ``tests/test_f016_u4_tester.py``. We focus on the new primitives."""
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)


def _seed_unit(
    *,
    feature_id: str = "F-EXT",
    unit_id: str = "F-EXT-U-1",
    status: str = "coding",
    sessions: dict[str, str] | None = None,
    repo_url: str = "https://github.com/o/r",
) -> WorkUnitState:
    """Single-unit feature for ``send_to_unit_async`` / ``cancel_unit`` tests."""
    if not state.get_feature(feature_id):
        state.save_feature(
            Feature(
                id=feature_id,
                title="extras",
                description="phase 2.5 extras",
                repo_path=repo_url,
                status="approved",
            )
        )
        state.save_plan(
            feature_id,
            [WorkUnit(id=unit_id, feature_id=feature_id, title="u", description="")],
        )
        state.approve_plan(feature_id)
    if sessions is None:
        sessions = {"coder": "sess_c"}
    s = WorkUnitState(
        unit_id=unit_id,
        feature_id=feature_id,
        status=status,
        branch="feat/x",
        pr_number=1,
        coder_session_id=sessions.get("coder", ""),
        tester_session_id=sessions.get("tester", ""),
        reviewer_session_id=sessions.get("reviewer", ""),
    )
    state.upsert_unit_state(s)
    return s


class _NoopAsyncWorker:
    """Records resume_async / archive calls; resume_async is a no-op."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.resume_async_calls: list[tuple[str, str]] = []
        self.archive_calls: list[str] = []

    def resume_async(self, session_id: str, msg: str) -> None:
        self.resume_async_calls.append((session_id, msg))

    def archive(self, session_id: str) -> None:
        self.archive_calls.append(session_id)


@pytest.fixture
def noop_workers(monkeypatch):
    """make_worker / ManagedAgentWorker -> per-role _NoopAsyncWorker map."""
    workers: dict[str, _NoopAsyncWorker] = {}

    def factory(role: str) -> _NoopAsyncWorker:
        if role not in workers:
            workers[role] = _NoopAsyncWorker(role)
        return workers[role]

    monkeypatch.setattr("orchestrator.tools.execution.make_worker", factory)
    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return workers


# --------------------------- sticky cancel survives upsert ----------------


class TestUpsertDoesNotClobberStickyColumns:
    """``upsert_unit_state`` documented contract: ``cancelled_at`` and
    ``owner`` are NOT overwritten on UPDATE. A regression would silently
    break the cancel-stickiness + daemon-CAS contracts."""

    def test_cancelled_at_survives_subsequent_upsert(self, tmp_state_db):
        """A stray ``upsert_unit_state`` (e.g. status-bump from another
        helper) must not silently clear ``cancelled_at`` — otherwise the
        daemon's per-tick "if unit.cancelled_at: skip" check fails and a
        cancelled unit resumes."""
        _seed_unit()
        state.cancel_unit("F-EXT-U-1")
        cancelled_at = state.get_unit_state("F-EXT-U-1").cancelled_at
        assert cancelled_at is not None

        # Someone elsewhere in the codebase builds a fresh model (model
        # default cancelled_at=None) and upserts a status change. The
        # spec REQUIRES the sticky column survives.
        stray = WorkUnitState(
            unit_id="F-EXT-U-1",
            feature_id="F-EXT",
            status="coding",  # try to un-cancel by status flip
            coder_session_id="sess_c",
        )
        state.upsert_unit_state(stray)

        refreshed = state.get_unit_state("F-EXT-U-1")
        assert refreshed is not None
        assert refreshed.cancelled_at == cancelled_at, (
            "upsert_unit_state must NOT clear cancelled_at — sticky-cancel "
            "is load-bearing per Phase 2.5 § cancel_unit"
        )

    def test_owner_survives_subsequent_upsert(self, tmp_state_db):
        """The Phase 3 daemon's CAS target (``owner``) must survive a
        stray ``upsert_unit_state``. A regression that overwrote it
        would erase the lead's advance-lock claim mid-window, letting
        the daemon race ``advance_state_machine`` on the same tick."""
        _seed_unit()
        assert state.claim_unit_owner("F-EXT-U-1", "lead") is True
        # Stray upsert with model default owner="".
        stray = WorkUnitState(
            unit_id="F-EXT-U-1",
            feature_id="F-EXT",
            status="coding",
            coder_session_id="sess_c",
        )
        state.upsert_unit_state(stray)
        s = state.get_unit_state("F-EXT-U-1")
        assert s is not None
        assert s.owner == "lead", (
            "upsert_unit_state must NOT clear owner — Phase 3 daemon's "
            "CAS target survives ambient status writes"
        )


# --------------------------- update_unit_deps semantics --------------------


class TestUpdateUnitDepsApprovalPreservation:
    """Spec § Phase 2.5 / planning.py docstring: ``update_unit_deps``
    deliberately does NOT call ``approve_plan`` again."""

    def _seed_three_unit_approved_plan(self, feature_id: str = "F-DEPS") -> None:
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

    def test_plan_status_stays_approved(self, tmp_state_db):
        """A graph mutation is "scheduler should consider these edges
        next time", NOT a material rewrite needing human re-approval.
        ``plan.status`` must remain ``approved``."""
        self._seed_three_unit_approved_plan()
        plan_before = state.get_plan("F-DEPS")
        assert plan_before is not None and plan_before.status == "approved"
        approved_at_before = plan_before.approved_at

        out = planning.update_unit_deps("F-DEPS", "F-DEPS-U-2", ["F-DEPS-U-1"])
        parsed = json.loads(out)
        assert parsed["outcome"] == "updated"

        plan_after = state.get_plan("F-DEPS")
        assert plan_after is not None
        assert plan_after.status == "approved", (
            "update_unit_deps must NOT flip plan back to draft — "
            "spec § Phase 2.5: 'deliberately does NOT call approve_plan again'"
        )
        # approved_at must also be preserved (no silent re-approval).
        assert plan_after.approved_at == approved_at_before

    def test_feature_status_stays_approved(self, tmp_state_db):
        """The corresponding ``features.status`` row must also stay
        approved — otherwise downstream tools that gate on
        ``feature.status in ('approved', 'in_progress')`` would drop
        the feature out of the scheduler the moment the user tweaked a
        dep."""
        self._seed_three_unit_approved_plan()
        planning.update_unit_deps("F-DEPS", "F-DEPS-U-2", ["F-DEPS-U-1"])
        feat = state.get_feature("F-DEPS")
        assert feat is not None and feat.status == "approved"

    def test_empty_deps_list_clears_all_deps(self, tmp_state_db):
        """An empty ``depends_on`` list must clear all deps for the unit.
        Otherwise the primitive is ADD-only and "U-3 no longer depends
        on U-2" is silently un-implementable. Spec § Phase 2.5 calls
        the primitive "re-shape the DAG" — re-shape includes removal."""
        self._seed_three_unit_approved_plan()
        # Establish a dep, then remove it.
        planning.update_unit_deps("F-DEPS", "F-DEPS-U-3", ["F-DEPS-U-1", "F-DEPS-U-2"])
        plan = state.get_plan("F-DEPS")
        assert plan is not None
        u3 = next(u for u in plan.units if u.id == "F-DEPS-U-3")
        assert u3.depends_on == ["F-DEPS-U-1", "F-DEPS-U-2"]
        # Now clear.
        out = planning.update_unit_deps("F-DEPS", "F-DEPS-U-3", [])
        parsed = json.loads(out)
        assert parsed["outcome"] == "updated"
        assert parsed["depends_on"] == []
        plan = state.get_plan("F-DEPS")
        assert plan is not None
        u3 = next(u for u in plan.units if u.id == "F-DEPS-U-3")
        assert u3.depends_on == [], (
            f"empty depends_on must CLEAR existing deps; got {u3.depends_on}"
        )

    def test_unaffected_units_keep_their_deps(self, tmp_state_db):
        """Editing one unit's deps must not silently mutate any other
        unit's deps — the in-memory rewrite-then-persist path must scope
        the write to ``unit_id``."""
        self._seed_three_unit_approved_plan()
        # Establish U-2 -> U-1.
        planning.update_unit_deps("F-DEPS", "F-DEPS-U-2", ["F-DEPS-U-1"])
        # Now edit U-3, NOT U-2.
        planning.update_unit_deps("F-DEPS", "F-DEPS-U-3", ["F-DEPS-U-1"])
        plan = state.get_plan("F-DEPS")
        assert plan is not None
        u2 = next(u for u in plan.units if u.id == "F-DEPS-U-2")
        assert u2.depends_on == ["F-DEPS-U-1"], (
            f"U-2's deps must be untouched when editing U-3; got {u2.depends_on}"
        )


# --------------------------- send_to_unit_async ---------------------------


class TestSendToUnitAsyncExtras:
    def test_explicit_role_overrides_status_default(self, tmp_state_db, noop_workers):
        """Lead intent diverging from current phase is legitimate (e.g.
        prep notes for the reviewer while the coder is iterating). An
        explicit ``role`` MUST override the status-based default."""
        _seed_unit(
            status="coding",  # status default would be coder
            sessions={"coder": "sess_c", "reviewer": "sess_r"},
        )
        out = execution.send_to_unit_async("F-EXT-U-1", "reviewer prep", role="reviewer")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["role"] == "reviewer"
        # Submitted to the reviewer worker, NOT the coder worker.
        assert noop_workers["reviewer"].resume_async_calls == [("sess_r", "reviewer prep")]
        # The coder worker did not see anything (its instance may not even exist).
        assert "coder" not in noop_workers or noop_workers["coder"].resume_async_calls == []

    def test_pending_status_returns_no_default_role(self, tmp_state_db, noop_workers):
        """``pending`` (never spawned) is NOT in the route table —
        ``_DEFAULT_ROLE_BY_STATUS.get("pending")`` returns ``None``.
        Silently routing to coder would create a "delivered" response
        on a unit the lead never started, leaking the not-spawned-yet
        semantics."""
        _seed_unit(status="pending", sessions={"coder": "sess_c"})
        out = execution.send_to_unit_async("F-EXT-U-1", "ping")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "no_default_role"
        # Diagnostic shape must be present.
        assert "role_diagnostics" in parsed
        # And nothing actually shipped.
        for w in noop_workers.values():
            assert w.resume_async_calls == []

    def test_audit_event_captures_message_body(self, tmp_state_db, noop_workers):
        """The ``{role}_manual_message`` audit row must contain the body.
        A regression that recorded the event without the body would
        leave the cycle log mute about what the lead sent."""
        _seed_unit()
        execution.send_to_unit_async("F-EXT-U-1", "tighten the error path", role="coder")
        events = state.list_events("F-EXT-U-1")
        manuals = [e for e in events if e["event_type"] == "coder_manual_message"]
        assert len(manuals) == 1
        assert "tighten the error path" in manuals[0]["details"]
        # And the source is human, not orchestrator — the lead issued it.
        assert manuals[0]["source"] == "human"

    def test_concurrent_sends_on_same_unit_serialize(self, tmp_state_db, monkeypatch):
        """Per-unit lock contract: two threads calling
        ``send_to_unit_async`` on the same unit must serialize. The
        Phase 3 daemon depends on this property to not race
        ``advance_state_machine`` against an in-flight submit."""
        _seed_unit()

        # We instrument the resume_async path to verify only one is
        # in flight at a time. The barrier is released when the first
        # call enters; the second call should NOT enter until the first
        # has exited the with-block.
        in_flight = threading.Lock()
        observed_overlap = []  # populated if anyone overlaps
        first_entered = threading.Event()
        second_can_start = threading.Event()

        class SerializerWorker:
            def __init__(self, role: str) -> None:
                self.role = role

            def resume_async(self, sid: str, msg: str) -> None:
                got = in_flight.acquire(blocking=False)
                if not got:
                    # Another thread is already inside the critical
                    # section — the lock is NOT serializing per-unit.
                    observed_overlap.append((sid, msg))
                    return
                try:
                    if not first_entered.is_set():
                        first_entered.set()
                        # Hold the section so the second call would
                        # observe overlap if the lock weren't unit-wide.
                        # Wait at most 1s — enough to let the second
                        # thread try-and-block on the lock.
                        second_can_start.wait(timeout=1.0)
                finally:
                    in_flight.release()

        monkeypatch.setattr("orchestrator.tools.execution.make_worker", SerializerWorker)

        results: list[str] = []

        def call() -> None:
            results.append(execution.send_to_unit_async("F-EXT-U-1", "m", role="coder"))

        t1 = threading.Thread(target=call, name="t1")
        t2 = threading.Thread(target=call, name="t2")
        t1.start()
        # Wait until t1 is INSIDE resume_async holding the section.
        assert first_entered.wait(timeout=2.0), "t1 never entered resume_async"
        t2.start()
        # Give t2 ~0.1s to enter (it should block on the per-unit lock).
        time.sleep(0.1)
        # Release t1.
        second_can_start.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        assert not t1.is_alive() and not t2.is_alive(), "threads did not finish"
        assert observed_overlap == [], (
            f"per-unit lock did not serialize concurrent submits; overlap={observed_overlap}"
        )
        # Both calls should report delivered.
        for raw in results:
            parsed = json.loads(raw)
            assert parsed["delivered"] is True
        # And lock must be fully released.
        assert state.has_active_advance_lock("F-EXT-U-1") is False


# --------------------------- next_ready_units_all + cancelled ---------------


class TestNextReadyUnitsAllAggregatesCancelled:
    """Spec § Phase 2.5: 'downstream dep-evaluation treats cancelled as
    not-done'. The cross-feature digest must surface cancelled units
    explicitly so the lead can prompt the user to reshape graphs."""

    def test_cancelled_units_appear_in_global_bucket(self, tmp_state_db, monkeypatch):
        """Cancel one unit in each of two features; both must surface in
        ``next_ready_units_all().cancelled``."""

        # Suppress worker side-effects on cancel — the lower-level
        # state.cancel_unit + the MCP wrapper both run.
        class _NoopWorker:
            def __init__(self, role: str) -> None:
                pass

            def archive(self, sid: str) -> None:
                pass

        monkeypatch.setattr("orchestrator.tools.execution.make_worker", _NoopWorker)

        for fid in ("F-A", "F-B"):
            state.save_feature(
                Feature(
                    id=fid,
                    title=fid,
                    description="d",
                    repo_path="https://github.com/o/r",
                    status="approved",
                )
            )
            state.save_plan(
                fid,
                [
                    WorkUnit(id=f"{fid}-U-1", feature_id=fid, title="u1", description=""),
                    WorkUnit(
                        id=f"{fid}-U-2",
                        feature_id=fid,
                        title="u2",
                        description="",
                        depends_on=[f"{fid}-U-1"],
                    ),
                ],
            )
            state.approve_plan(fid)
            # U-1 reaches coding then gets cancelled in each feature.
            state.upsert_unit_state(
                WorkUnitState(
                    unit_id=f"{fid}-U-1",
                    feature_id=fid,
                    status="coding",
                    coder_session_id="sess_c",
                )
            )
            execution.cancel_unit(f"{fid}-U-1")

        raw = scheduling.next_ready_units_all()
        parsed = json.loads(raw)
        cancelled_refs = {(e["feature_id"], e["unit_id"]) for e in parsed.get("cancelled", [])}
        assert ("F-A", "F-A-U-1") in cancelled_refs
        assert ("F-B", "F-B-U-1") in cancelled_refs
        # And no F-?-U-2 in ready_to_spawn (cancelled deps don't satisfy).
        ready_ids = {e["unit_id"] for e in parsed.get("ready_to_spawn", [])}
        assert "F-A-U-2" not in ready_ids
        assert "F-B-U-2" not in ready_ids
        # The cross-feature counter should match the bucket size.
        assert parsed["total_cancelled"] >= 2


# --------------------------- stale-marker frozenset semantics --------------


class TestStaleMarkerEventTypeSet:
    """The ``REVIEWER_TERMINAL_EVENT_TYPES`` / ``CODER_PUSH_EVENT_TYPES``
    frozensets encode the spec's "what is a marker" / "what is a push"
    rule. Their exact contents matter — adding REQUEST_CHANGES would
    spam the audit log; dropping COMMENT would miss a real terminal."""

    def test_reviewer_request_changes_is_not_a_marker(self, tmp_state_db):
        """REQUEST_CHANGES leaves the unit in ``fixing`` and the coder
        loop already handles the delta — staleness detection here would
        be a false positive on every iteration cycle. Spec source
        docstring: "REQUEST_CHANGES is intentionally absent." """
        assert "reviewer_request_changes" not in stale_marker.REVIEWER_TERMINAL_EVENT_TYPES
        # Behaviourally: a unit with ONLY a request_changes event must
        # classify as ``not_emitted`` (no terminal marker yet).
        unit_state = _seed_unit()
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "reviewer_request_changes",
            source="reviewer",
            summary="please fix X",
        )
        result = stale_marker.detect_stale_reviewer_marker(unit_state.unit_id)
        assert result["case"] == "not_emitted", (
            "request_changes is not a terminal marker — staleness detection "
            "must ignore it (spec § REVIEWER_TERMINAL_EVENT_TYPES)"
        )

    def test_reviewer_comment_counts_as_marker(self, tmp_state_db):
        """COMMENT is a comment-only terminal endorsement; the freshness
        rule MUST treat it as marker_emitted so a later coder push
        flagged it stale. Frozenset includes ``reviewer_comment``."""
        assert "reviewer_comment" in stale_marker.REVIEWER_TERMINAL_EVENT_TYPES
        unit_state = _seed_unit()
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "reviewer_comment",
            source="reviewer",
            summary="note",
        )
        # Without a later push, valid.
        result = stale_marker.detect_stale_reviewer_marker(unit_state.unit_id)
        assert result["case"] == "valid"
        assert result["reviewer_marker_event_type"] == "reviewer_comment"

        # With a later push, stale.
        time.sleep(0.01)
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            "fix_pushed",
            source="coder",
            summary="fix",
        )
        result = stale_marker.detect_stale_reviewer_marker(unit_state.unit_id)
        assert result["case"] == "stale"

    def test_dedupe_key_scoped_per_unit(self, tmp_state_db):
        """Two different units with identical
        (reviewer_event_type, later_push_event_type) shapes must record
        SEPARATE audit rows — otherwise one unit's pending-delta row
        would silently swallow the other's. The dedupe key includes
        ``unit_id``."""
        a = _seed_unit(feature_id="F-S1", unit_id="F-S1-U-1")
        b = _seed_unit(feature_id="F-S2", unit_id="F-S2-U-1")

        first = stale_marker.record_stale_marker_pending_delta(
            a.unit_id,
            a.feature_id,
            reviewer_event_id=1,
            later_push_event_id=2,
            reviewer_event_type="reviewer_recommend_merge",
            later_push_event_type="fix_pushed",
        )
        second = stale_marker.record_stale_marker_pending_delta(
            b.unit_id,
            b.feature_id,
            reviewer_event_id=1,
            later_push_event_id=2,
            reviewer_event_type="reviewer_recommend_merge",
            later_push_event_type="fix_pushed",
        )
        assert first is True
        assert second is True, (
            "dedupe key must scope per unit_id; otherwise different units "
            "with identical shapes silently swallow each other's audit row"
        )
        # Each unit has exactly one such row.
        a_rows = [
            e
            for e in state.list_events(a.unit_id)
            if e["event_type"] == "reviewer_stale_marker_pending_delta"
        ]
        b_rows = [
            e
            for e in state.list_events(b.unit_id)
            if e["event_type"] == "reviewer_stale_marker_pending_delta"
        ]
        assert len(a_rows) == 1
        assert len(b_rows) == 1


# --------------------------- predecessor-consistency cross-check ----------


class TestPredecessorConsistencyF016U3:
    """F-016-U-3 shipped the three ``advance_to_*`` phase commands as
    idempotent on current status. Phase 2.5 is additive — it MUST NOT
    silently change their idempotency contract."""

    def test_advance_to_tester_still_callable(self, tmp_state_db):
        from orchestrator.tools import execution as exe

        # Smoke: the function still exists and is importable.
        assert callable(getattr(exe, "advance_to_tester", None))
        assert callable(getattr(exe, "advance_to_reviewer", None))
        assert callable(getattr(exe, "advance_to_terminal", None))
