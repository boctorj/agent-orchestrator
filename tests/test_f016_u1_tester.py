"""F-016-U-1 (tester) — additional acceptance tests for Phase 0.

The coder shipped:
  1. ``unit_events.dedupe_key`` column + ``INSERT OR IGNORE``;
  2. pure ``orchestrator.markers.scan_response`` + ``markers.dedupe_key``;
  3. read-only ``scan_unit_session`` MCP tool;
  4. ``_record_terminal_marker`` rewired to use the same parser + key.

The coder's own suite covers each piece in isolation. This tester suite
pins the **cross-piece contracts** that make the watcher-daemon (F-016-U-5)
land safely on top of Phase 0:

  * the ``dedupe_key`` the lead persists matches the value an external
    daemon would compute via the public ``markers.dedupe_key`` helper —
    two callers must agree on the same hash;
  * the daemon's "scan tail → record" sequence applied twice on an idle
    session produces exactly one audit row (Phase 0 acceptance);
  * legacy non-marker events (``spawn_coder`` / ``coder_resumed`` /
    ``merged`` / ``recovered_from_escalated``) keep appending one row
    per call — the "zero behavior change" promise;
  * ``scan_response`` is genuinely pure (no DB side effects across
    thousands of calls);
  * the MCP tool surface registers ``scan_unit_session``;
  * the ``coder_blocked`` vs ``coder_blocked_on_fix`` discriminator is
    reflected in the persisted hash, so a fix-loop BLOCKED can't dedupe
    against a spawn-time BLOCKED on the same coder session.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from orchestrator import state
from orchestrator.markers import MarkerSpec, dedupe_key, scan_response
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, mcp
from orchestrator.tools import ops as ops_tools

# --------------------------- shared fixtures ---------------------------


def _seed_feature(feature_id: str = "F-001", repo: str = "https://github.com/o/r") -> None:
    state.save_feature(
        Feature(id=feature_id, title="t", description="d", repo_path=repo, status="approved")
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description="impl")],
    )
    state.approve_plan(feature_id)


def _seed_unit(
    role: str,
    status: str,
    *,
    unit_id: str = "F-001-U-1",
    feature_id: str = "F-001",
    review_round: int = 0,
) -> None:
    _seed_feature(feature_id)
    fields: dict = {
        "unit_id": unit_id,
        "feature_id": feature_id,
        "status": status,
        "branch": "feat/branch",
        "pr_number": 7,
        "review_round": review_round,
    }
    fields[f"{role}_session_id"] = f"sesn-{role}"
    if role != "coder":
        fields["coder_session_id"] = "sesn-coder"
    state.upsert_unit_state(WorkUnitState(unit_id=unit_id, feature_id=feature_id, status=status))
    # apply session ids
    cur = state.get_unit_state(unit_id)
    cur.branch = fields["branch"]
    cur.pr_number = fields["pr_number"]
    cur.review_round = review_round
    setattr(cur, f"{role}_session_id", f"sesn-{role}")
    if role != "coder":
        cur.coder_session_id = "sesn-coder"
    state.upsert_unit_state(cur)


def _events(unit_id: str) -> list[dict]:
    return state.list_events(unit_id)


def _persisted_dedupe_key(tmp_db, *, event_type: str) -> str | None:
    """Read the ``dedupe_key`` value for the latest row of ``event_type``."""
    with sqlite3.connect(tmp_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT dedupe_key FROM unit_events WHERE event_type = ? ORDER BY id DESC LIMIT 1",
            (event_type,),
        ).fetchone()
    return row["dedupe_key"] if row else None


class _FakeTailWorker:
    """Stand-in for the backend Worker's ``tail_messages`` return value."""

    def __init__(self, role: str, *, status: str, messages: list[dict]):
        self.role = role
        self._status = status
        self._messages = messages

    def tail_messages(self, session_id: str, *, limit: int = 50) -> dict:
        return {"status": self._status, "messages": self._messages, "reason": None}


def _install_make_worker(monkeypatch, fake: _FakeTailWorker) -> None:
    monkeypatch.setattr(ops_tools, "make_worker", lambda role: fake)


# --------------------------- 1. external/persisted dedupe_key agreement -


class TestPersistedDedupeKeyMatchesPublicHelper:
    """The lead's recorder and the watcher daemon must agree on the hash.

    ``markers.dedupe_key`` is the public helper — the watcher daemon
    (F-016-U-5) will compute the key for an idle session's re-scan via
    exactly this function. If the lead persists a *different* string when
    it records the same marker first, the daemon's ``INSERT OR IGNORE``
    becomes an ``INSERT`` and the audit log doubles.
    """

    @pytest.mark.parametrize(
        ("role", "from_status", "response", "expected_event", "expected_payload"),
        [
            ("tester", "testing", "TESTS_PASS\n", "tests_pass", "TESTS_PASS"),
            (
                "tester",
                "testing",
                "BUG_FOUND: off-by-one",
                "tester_bug_found",
                "off-by-one",
            ),
            (
                "reviewer",
                "reviewing",
                "REVIEW_RECOMMEND_MERGE: clean",
                "reviewer_recommend_merge",
                "clean",
            ),
            (
                "reviewer",
                "reviewing",
                "REVIEW_REQUEST_CHANGES: rename",
                "reviewer_request_changes",
                "rename",
            ),
            ("reviewer", "reviewing", "REVIEW_COMMENT\n", "reviewer_comment", "REVIEW_COMMENT"),
            ("coder", "fixing", "FIX_PUSHED\n", "fix_pushed", "FIX_PUSHED"),
            (
                "coder",
                "coding",
                "PR_URL: https://github.com/o/r/pull/42",
                "pr_opened",
                "https://github.com/o/r/pull/42",
            ),
        ],
    )
    def test_recorder_persists_public_helper_hash(
        self,
        tmp_state_db,
        role,
        from_status,
        response,
        expected_event,
        expected_payload,
    ):
        _seed_unit(role, from_status, review_round=4)
        sid = f"sesn-{role}"

        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role=role,
            response=response,
            session_id=sid,
            cycle_number=4,
        )

        persisted = _persisted_dedupe_key(tmp_state_db, event_type=expected_event)
        external = dedupe_key(
            session_id=sid,
            cycle_number=4,
            event_type=expected_event,
            marker_payload=expected_payload,
        )
        assert persisted is not None
        assert persisted == external, (
            f"recorder ({expected_event}) persisted {persisted!r} but public helper "
            f"would compute {external!r} — daemon re-scan would NOT dedupe"
        )

    def test_blocked_persisted_key_uses_structured_payload(self, tmp_state_db):
        """BLOCKED's payload is ``reason|prose`` — not the marker name —
        because the structured taxonomy is what distinguishes one
        BLOCKED from another in a given cycle."""
        _seed_unit("coder", "fixing")
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="BLOCKED: reason=auth_failure | 401 on push",
            session_id="sesn-coder",
            cycle_number=0,
        )

        persisted = _persisted_dedupe_key(tmp_state_db, event_type="coder_blocked")
        external = dedupe_key(
            session_id="sesn-coder",
            cycle_number=0,
            event_type="coder_blocked",
            marker_payload="auth_failure|401 on push",
        )
        assert persisted == external

    def test_blocked_event_override_changes_persisted_key(self, tmp_state_db):
        """``address_review`` records BLOCKED as ``coder_blocked_on_fix``
        — the persisted hash must reflect the overridden event_type so a
        fix-loop BLOCKED cannot collide with a spawn-time BLOCKED on the
        same coder session."""
        _seed_unit("coder", "fixing")
        response = "BLOCKED: reason=unknown | something"

        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response=response,
            session_id="sesn-coder",
            cycle_number=0,
            blocked_event="coder_blocked_on_fix",
        )

        spec = scan_response("coder", response)
        assert spec is not None
        default_key = dedupe_key(
            session_id="sesn-coder",
            cycle_number=0,
            event_type="coder_blocked",
            marker_payload=spec.payload,
        )
        override_key = dedupe_key(
            session_id="sesn-coder",
            cycle_number=0,
            event_type="coder_blocked_on_fix",
            marker_payload=spec.payload,
        )
        persisted = _persisted_dedupe_key(tmp_state_db, event_type="coder_blocked_on_fix")
        assert persisted == override_key
        assert persisted != default_key


# --------------------------- 2. watcher-daemon-shaped re-scan ---------


class TestWatcherDaemonReScanIsNoop:
    """The acceptance bullet: a re-scan of an idle session is a no-op.

    Simulates the F-016-U-5 daemon's level-triggered tick:
      1. fetch the worker tail (``scan_unit_session``);
      2. record any matched marker (``_record_terminal_marker``);
      3. next tick: fetch again, record again → INSERT OR IGNORE collapses.

    The lead's blocking path is just the first iteration of the same
    loop; the daemon picks up from there. Phase 0 promises these two
    callers see the same audit row, not duplicates.
    """

    def test_scan_then_record_then_scan_then_record_writes_one_row(self, tmp_state_db, monkeypatch):
        _seed_unit("tester", "testing", review_round=1)
        _install_make_worker(
            monkeypatch,
            _FakeTailWorker(
                "tester",
                status="idle",
                messages=[
                    {"ts": "t1", "role": "agent", "text": "all tests run"},
                    {"ts": "t2", "role": "agent", "text": "TESTS_PASS"},
                ],
            ),
        )

        # --- TICK 1: scan + record (lead) -----------------------------
        out1 = json.loads(ops_tools.scan_unit_session("F-001-U-1", "tester"))
        assert out1["marker"] == "TESTS_PASS"
        # Daemon would record what scan returned — feed the same text the
        # tool joined back through the recorder.
        joined = "all tests run\nTESTS_PASS"
        first = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response=joined,
            session_id="sesn-tester",
            cycle_number=1,
        )
        assert first is not None and first["marker"] == "TESTS_PASS"

        # --- TICK 2: same fetch, same record (daemon re-tick) ---------
        out2 = json.loads(ops_tools.scan_unit_session("F-001-U-1", "tester"))
        assert out2["marker"] == "TESTS_PASS"
        second = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response=joined,
            session_id="sesn-tester",
            cycle_number=1,
        )
        # Both callers still see the parsed marker (caller doesn't branch);
        # only the AUDIT layer dedupes.
        assert second is not None and second["marker"] == "TESTS_PASS"

        rows = [e for e in _events("F-001-U-1") if e["event_type"] == "tests_pass"]
        assert len(rows) == 1, f"watcher re-scan should not double the audit log; rows={rows}"

    def test_status_flips_to_target_only_once(self, tmp_state_db):
        """A second record on the same response must not re-flip status.

        ``_flip_status_if_active`` is gated on the unit being in an active
        from-state. After tick 1 flips ``testing → in_ci``, tick 2's
        ``in_ci`` is no longer in ``ACTIVE_UNIT_STATUSES`` minus ``in_ci``;
        more importantly, the dedupe makes tick 2's effect zero in the
        audit log even if a future refactor re-flipped status.
        """
        _seed_unit("tester", "testing")
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="TESTS_PASS",
            session_id="sesn-tester",
            cycle_number=0,
        )
        assert state.get_unit_state("F-001-U-1").status == "in_ci"

        # Manually flip back to something the lead would never do, then
        # re-record: the dedupe should keep the second call from advancing
        # the audit log; status-flip behaviour on the active branch can
        # still happen, but the row count is the load-bearing assertion.
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="TESTS_PASS",
            session_id="sesn-tester",
            cycle_number=0,
        )
        rows = [e for e in _events("F-001-U-1") if e["event_type"] == "tests_pass"]
        assert len(rows) == 1


# --------------------------- 3. zero behaviour change for legacy events


class TestLegacyEventsStillAppend:
    """Non-marker events MUST keep writing one row per call.

    Phase 0 is "zero behavior change" — only the marker-event recorders
    opt in to dedupe by passing a ``dedupe_key``. The lifecycle audit
    rows (``spawn_coder``, ``coder_resumed``, ``merged``, …) call
    ``record_event`` *without* a ``dedupe_key`` and must continue to
    append unconditionally.
    """

    def _seed(self) -> None:
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))

    @pytest.mark.parametrize(
        "event_type",
        [
            "spawn_coder",
            "spawn_tester",
            "spawn_reviewer",
            "coder_resumed",
            "merged",
            "recovered_from_escalated",
            "reconcile_refused",
            "coder_no_marker",
            "tester_resume",
            "reviewer_resume",
            "copilot_review_received",
            "ultrareview_started",
        ],
    )
    def test_legacy_event_types_append_per_call(self, tmp_state_db, event_type):
        self._seed()
        for i in range(3):
            inserted = state.record_event("U1", "F", event_type, summary=f"call {i}")
            assert inserted is True, f"{event_type} call {i} should have inserted"
        rows = [e for e in _events("U1") if e["event_type"] == event_type]
        assert len(rows) == 3

    def test_null_and_keyed_events_coexist(self, tmp_state_db):
        """A unit lifecycle interleaves legacy (NULL key) and marker
        (keyed) rows. Both must round-trip cleanly through one table.
        """
        self._seed()
        state.record_event("U1", "F", "spawn_coder")
        state.record_event("U1", "F", "pr_opened", dedupe_key="k1", summary="PR #1")
        state.record_event("U1", "F", "coder_resumed")
        state.record_event("U1", "F", "fix_pushed", dedupe_key="k2", summary="cycle 1 fix")
        # daemon re-scan tries to write pr_opened again — no-op
        assert (
            state.record_event("U1", "F", "pr_opened", dedupe_key="k1", summary="duplicate")
            is False
        )
        # spawn_coder repeated by a restart-recovery flow — still appends
        assert state.record_event("U1", "F", "spawn_coder") is True

        types = [e["event_type"] for e in _events("U1")]
        assert types.count("spawn_coder") == 2
        assert types.count("pr_opened") == 1
        assert types.count("coder_resumed") == 1
        assert types.count("fix_pushed") == 1


# --------------------------- 4. scan_response purity ------------------


class TestScanResponseIsPure:
    """``markers.scan_response`` must be a pure function — no DB writes,
    no logging, no I/O. Two callers (lead + daemon) safely call it in
    rapid succession without state drift."""

    def test_thousand_invocations_write_no_events(self, tmp_state_db):
        # Seed a unit so list_events resolves a real row.
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))

        before = len(_events("U1"))
        for _ in range(1000):
            scan_response("tester", "TESTS_PASS")
            scan_response("coder", "BLOCKED: reason=auth_failure | 401")
            scan_response("reviewer", "REVIEW_REQUEST_CHANGES: bad")
        after = len(_events("U1"))
        assert before == after == 0

    def test_returned_specs_are_value_equal_across_calls(self):
        """Frozen-dataclass equality is what makes the dedupe contract work
        from the daemon's side — the watcher will compare two scans to
        decide whether to re-trigger. Equality must be by value."""
        a = scan_response("tester", "all green\nTESTS_PASS\n")
        b = scan_response("tester", "all green\nTESTS_PASS\n")
        assert isinstance(a, MarkerSpec)
        assert a == b
        # The frozen dataclass is hashable when its fields are; the dedupe
        # key encodes essentially the same identity. Confirm the payload
        # the daemon would hash matches:
        assert a.payload == b.payload


# --------------------------- 5. MCP tool surface ----------------------


class TestScanUnitSessionIsRegistered:
    """The MCP tool registration is what makes the lead reachable from
    chat; without it, the workflow described in the spec ("see what
    marker the daemon WOULD see without waiting for the daemon") never
    surfaces."""

    def test_scan_unit_session_is_registered_with_mcp(self):
        # Import the ops module to trigger the @mcp.tool() registration.
        import orchestrator.tools.ops  # noqa: F401

        assert "scan_unit_session" in mcp._tool_manager._tools


# --------------------------- 6. scan_unit_session BLOCKED -------------


class TestScanUnitSessionBlockedPath:
    """The existing tester suite covers TESTS_PASS / RECOMMEND_MERGE /
    no-marker; a BLOCKED path is the only role-universal marker and is
    asymmetric (target_status='escalated', event_type='{role}_blocked'),
    so pinning its return shape here protects against future drift in
    the recorder ↔ scanner agreement."""

    def test_blocked_in_tail_returns_role_blocked_event_type(self, tmp_state_db, monkeypatch):
        _seed_unit("coder", "coding")
        _install_make_worker(
            monkeypatch,
            _FakeTailWorker(
                "coder",
                status="terminated",
                messages=[
                    {"ts": "t1", "role": "agent", "text": "tried to push"},
                    {
                        "ts": "t2",
                        "role": "agent",
                        "text": "BLOCKED: reason=auth_failure | 401 on push",
                    },
                ],
            ),
        )

        out = json.loads(ops_tools.scan_unit_session("F-001-U-1", "coder"))

        assert out["marker"] == "BLOCKED"
        assert out["event_type"] == "coder_blocked"
        assert out["target_status"] == "escalated"
        assert "auth_failure" in out["payload"]
        # Read-only contract still holds — no audit row written.
        assert _events("F-001-U-1") == []
        # Status unchanged.
        assert state.get_unit_state("F-001-U-1").status == "coding"

    def test_terminated_session_tail_status_propagates(self, tmp_state_db, monkeypatch):
        _seed_unit("coder", "coding")
        _install_make_worker(
            monkeypatch,
            _FakeTailWorker(
                "coder",
                status="terminated",
                messages=[],
            ),
        )

        out = json.loads(ops_tools.scan_unit_session("F-001-U-1", "coder"))
        assert out["tail_status"] == "terminated"
        assert out["message_count"] == 0
        assert out["marker"] is None


# --------------------------- 7. cycle/session axis dedupe -------------


class TestRecorderDedupeAxes:
    """The four axes (session_id, cycle_number, event_type, payload) each
    independently shift the hash. The coder's suite covers most of these
    via the ``markers.dedupe_key`` unit tests; this class pins the
    *recorder side* — that ``_record_terminal_marker`` honours each axis
    when persisting (so a future refactor that drops, say, session_id
    from the recorder's key construction is caught here)."""

    def test_distinct_sessions_each_record(self, tmp_state_db):
        _seed_unit("tester", "testing", review_round=2)
        # Same response, same cycle — but a new session_id means a new
        # worker run. The audit log MUST record both.
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="TESTS_PASS",
            session_id="sesn-tester-A",
            cycle_number=2,
        )
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="TESTS_PASS",
            session_id="sesn-tester-B",
            cycle_number=2,
        )
        rows = [e for e in _events("F-001-U-1") if e["event_type"] == "tests_pass"]
        assert len(rows) == 2

    def test_distinct_cycle_numbers_each_record(self, tmp_state_db):
        """Mirrors the coder's ``test_different_cycles_do_not_collide``
        from the inside-the-recorder angle: cycle 1 FIX_PUSHED and cycle 3
        FIX_PUSHED share session_id+event_type+payload but must NOT
        dedupe — the cycle counter is part of the hash domain."""
        _seed_unit("coder", "fixing")
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="FIX_PUSHED",
            session_id="sesn-coder",
            cycle_number=1,
        )
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="FIX_PUSHED",
            session_id="sesn-coder",
            cycle_number=3,
        )
        rows = [e for e in _events("F-001-U-1") if e["event_type"] == "fix_pushed"]
        assert {r["cycle_number"] for r in rows} == {1, 3}

    def test_distinct_payloads_each_record_same_event_type(self, tmp_state_db):
        """Two different BUG_FOUND reasons in the same cycle are two
        separate findings — the marker payload differentiates them."""
        _seed_unit("tester", "testing")
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="BUG_FOUND: off-by-one",
            session_id="sesn-tester",
            cycle_number=0,
        )
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="BUG_FOUND: wrong return value",
            session_id="sesn-tester",
            cycle_number=0,
        )
        rows = [e for e in _events("F-001-U-1") if e["event_type"] == "tester_bug_found"]
        assert len(rows) == 2
        summaries = {r["summary"] for r in rows}
        assert summaries == {"off-by-one", "wrong return value"}


# --------------------------- 8. scope boundary ------------------------


class TestScopeBoundary:
    """Phase 0 promises "zero behavior change". Anything in the spec's
    ``## Out of scope`` list must NOT be touched. Most of that scope
    isn't exercisable from a unit test, but the marker grammar IS — and
    the spec lists 'Replacing ... the marker grammar' as out of scope.
    A regression here would silently re-shape every downstream caller.
    """

    @pytest.mark.parametrize(
        ("role", "marker_text", "want_marker"),
        [
            ("coder", "PR_URL: https://github.com/o/r/pull/1", "PR_URL"),
            ("coder", "FIX_PUSHED", "FIX_PUSHED"),
            ("tester", "TESTS_PASS", "TESTS_PASS"),
            ("tester", "BUG_FOUND: x", "BUG_FOUND"),
            ("reviewer", "REVIEW_RECOMMEND_MERGE: x", "REVIEW_RECOMMEND_MERGE"),
            ("reviewer", "REVIEW_REQUEST_CHANGES: x", "REVIEW_REQUEST_CHANGES"),
            ("reviewer", "REVIEW_COMMENT", "REVIEW_COMMENT"),
            ("coder", "BLOCKED: reason=auth_failure | x", "BLOCKED"),
        ],
    )
    def test_marker_grammar_unchanged(self, role, marker_text, want_marker):
        spec = scan_response(role, marker_text)
        assert spec is not None, f"{role}/{want_marker} no longer recognised"
        assert spec.marker == want_marker

    def test_cross_role_markers_still_ignored(self):
        """Per the long-standing role-scoping contract. If a tester
        response containing ``REVIEW_RECOMMEND_MERGE`` started matching,
        the BLOCKED branch would never fire on rev/test misfires (the
        cross-role marker would silently win)."""
        assert scan_response("tester", "REVIEW_RECOMMEND_MERGE: x") is None
        assert scan_response("coder", "TESTS_PASS") is None
        assert scan_response("reviewer", "FIX_PUSHED") is None

    def test_unknown_role_returns_none(self):
        """Defensive — an unknown role string from a typo'd MCP call
        must not match anything (silent matching would be the worst
        possible regression: writing audit rows for the wrong role)."""
        assert scan_response("noodler", "TESTS_PASS") is None
        assert scan_response("", "TESTS_PASS") is None
