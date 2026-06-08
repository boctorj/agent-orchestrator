"""F-016-U-1 — idempotent terminal-marker recording.

Phase 0 of the dispatcher/watcher split. Three load-bearing pieces:

  1. ``unit_events.dedupe_key`` + ``INSERT OR IGNORE`` — duplicate
     terminal-marker recording is a no-op.
  2. Pure ``orchestrator.markers.scan_response(role, text)`` — the
     same parser the lead and the future watcher daemon both call.
  3. Read-only ``scan_unit_session(unit_id, role)`` MCP tool —
     fetches the worker tail, runs the parser, returns the would-be
     marker WITHOUT writing.

These tests pin the cross-piece contract: that ``_record_terminal_marker``
called twice with the same response writes one row, and that
``scan_unit_session`` returns the parsed marker without any
``unit_events`` row landing.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import state
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution
from orchestrator.tools import ops as ops_tools


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
    state.upsert_unit_state(WorkUnitState(**fields))


def _events(unit_id: str) -> list[dict]:
    return state.list_events(unit_id)


# --------------------------- _record_terminal_marker idempotency ---------------------------


class TestRecordTerminalMarkerIdempotency:
    """The Phase-0 acceptance bullet — twice on the same response, one row."""

    @pytest.mark.parametrize(
        ("role", "from_status", "response", "expected_event"),
        [
            ("tester", "testing", "all good\nTESTS_PASS\n", "tests_pass"),
            (
                "reviewer",
                "reviewing",
                "endorsed\nREVIEW_RECOMMEND_MERGE: clean",
                "reviewer_recommend_merge",
            ),
            (
                "reviewer",
                "reviewing",
                "REVIEW_REQUEST_CHANGES: rename it",
                "reviewer_request_changes",
            ),
            ("reviewer", "reviewing", "REVIEW_COMMENT", "reviewer_comment"),
            ("tester", "testing", "BUG_FOUND: off-by-one", "tester_bug_found"),
            ("coder", "fixing", "patch up\nFIX_PUSHED\n", "fix_pushed"),
            (
                "coder",
                "coding",
                "PR_URL: https://github.com/o/r/pull/9",
                "pr_opened",
            ),
        ],
    )
    def test_second_call_with_same_response_writes_no_new_row(
        self, tmp_state_db, role, from_status, response, expected_event
    ):
        _seed_unit(role, from_status, review_round=2)

        first = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role=role,
            response=response,
            session_id=f"sesn-{role}",
            cycle_number=2,
        )
        second = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role=role,
            response=response,
            session_id=f"sesn-{role}",
            cycle_number=2,
        )

        # Both calls return the parsed marker so callers don't branch.
        assert first is not None and second is not None
        assert first["marker"] == second["marker"]

        # Exactly one ``expected_event`` row landed.
        events = [e for e in _events("F-001-U-1") if e["event_type"] == expected_event]
        assert len(events) == 1, [e["event_type"] for e in _events("F-001-U-1")]

    def test_blocked_marker_dedupes_on_second_call(self, tmp_state_db):
        _seed_unit("coder", "fixing")

        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="BLOCKED: reason=auth_failure | 401",
            session_id="sesn-coder",
            cycle_number=0,
        )
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="BLOCKED: reason=auth_failure | 401",
            session_id="sesn-coder",
            cycle_number=0,
        )

        blocked = [e for e in _events("F-001-U-1") if e["event_type"] == "coder_blocked"]
        assert len(blocked) == 1

    def test_different_cycles_do_not_collide(self, tmp_state_db):
        """The proposal explicitly flags cycle_number as load-bearing: a
        re-emit of FIX_PUSHED in cycle 3 must NOT dedupe against cycle 1."""
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

        pushes = [e for e in _events("F-001-U-1") if e["event_type"] == "fix_pushed"]
        assert len(pushes) == 2
        assert {e["cycle_number"] for e in pushes} == {1, 3}

    def test_blocked_event_override_keeps_distinct_from_default(self, tmp_state_db):
        """``coder_blocked`` (spawn-time) and ``coder_blocked_on_fix``
        (address_review) must remain distinguishable — same session, same
        cycle, same payload, different event_type."""
        _seed_unit("coder", "fixing")

        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="BLOCKED: reason=unknown | x",
            session_id="sesn-coder",
            cycle_number=0,
        )
        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="BLOCKED: reason=unknown | x",
            session_id="sesn-coder",
            cycle_number=0,
            blocked_event="coder_blocked_on_fix",
        )

        types = {e["event_type"] for e in _events("F-001-U-1")}
        assert "coder_blocked" in types
        assert "coder_blocked_on_fix" in types


# --------------------------- scan_unit_session ---------------------------


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


class TestScanUnitSession:
    """The MCP tool reports the would-be marker WITHOUT writing audit rows."""

    def test_returns_marker_when_session_idle_with_marker(self, tmp_state_db, monkeypatch):
        _seed_unit("tester", "testing")
        _install_make_worker(
            monkeypatch,
            _FakeTailWorker(
                "tester",
                status="idle",
                messages=[
                    {"ts": "t1", "role": "agent", "text": "ran the suite"},
                    {"ts": "t2", "role": "agent", "text": "TESTS_PASS"},
                ],
            ),
        )

        out = json.loads(ops_tools.scan_unit_session("F-001-U-1", "tester"))

        assert out["marker"] == "TESTS_PASS"
        assert out["event_type"] == "tests_pass"
        assert out["target_status"] == "in_ci"
        assert out["tail_status"] == "idle"
        assert out["message_count"] == 2

    def test_does_not_record_any_event(self, tmp_state_db, monkeypatch):
        _seed_unit("tester", "testing")
        _install_make_worker(
            monkeypatch,
            _FakeTailWorker(
                "tester",
                status="idle",
                messages=[{"ts": "t", "role": "agent", "text": "TESTS_PASS"}],
            ),
        )

        ops_tools.scan_unit_session("F-001-U-1", "tester")

        # Read-only contract: no unit_events row from the scan itself.
        assert _events("F-001-U-1") == []

    def test_does_not_flip_status(self, tmp_state_db, monkeypatch):
        _seed_unit("reviewer", "reviewing")
        _install_make_worker(
            monkeypatch,
            _FakeTailWorker(
                "reviewer",
                status="idle",
                messages=[{"ts": "t", "role": "agent", "text": "REVIEW_RECOMMEND_MERGE: clean"}],
            ),
        )

        ops_tools.scan_unit_session("F-001-U-1", "reviewer")

        assert state.get_unit_state("F-001-U-1").status == "reviewing"

    def test_returns_null_marker_when_no_marker_in_tail(self, tmp_state_db, monkeypatch):
        _seed_unit("coder", "coding")
        _install_make_worker(
            monkeypatch,
            _FakeTailWorker(
                "coder",
                status="running",
                messages=[{"ts": "t", "role": "agent", "text": "still working"}],
            ),
        )

        out = json.loads(ops_tools.scan_unit_session("F-001-U-1", "coder"))

        assert out["marker"] is None
        assert out["event_type"] is None
        assert out["target_status"] is None
        assert out["tail_status"] == "running"

    def test_unknown_role_returns_error(self, tmp_state_db):
        _seed_unit("coder", "coding")
        out = ops_tools.scan_unit_session("F-001-U-1", "noodler")
        assert out.startswith("ERROR")
        assert "coder|tester|reviewer" in out

    def test_unknown_unit_returns_error(self, tmp_state_db):
        out = ops_tools.scan_unit_session("F-999-U-9", "coder")
        assert out.startswith("ERROR")
        assert "no state" in out

    def test_no_session_id_for_role_returns_error(self, tmp_state_db):
        _seed_unit("coder", "coding")  # only coder session set
        out = ops_tools.scan_unit_session("F-001-U-1", "tester")
        assert out.startswith("ERROR")
        assert "no tester session" in out
