"""Independent tester-agent tests for F-009-U-1.

The unit extracts the marker-regex chain (PR_URL_RE / TESTS_PASS_RE / BUG_FOUND_RE
/ REVIEW_RECOMMEND_MERGE_RE / REVIEW_CHANGES_RE / REVIEW_COMMENT_RE / FIX_PUSHED_RE
/ parse_blocked_marker) into a single helper

    ``_record_terminal_marker(unit_id, feature_id, role, response, session_id,
                              cycle_number)``

next to ``_escalate_no_marker`` in ``orchestrator/tools/execution.py`` and wires
it into ``send_to_unit``. The helper:

  * inspects only role-appropriate markers (per-role scope, NOT cross-role);
  * writes the corresponding ``unit_event`` row;
  * flips ``work_units.status`` (testing/reviewing/fixing/coding -> in_ci on
    success-side markers; -> escalated on BLOCKED); BUG_FOUND and
    REVIEW_REQUEST_CHANGES leave the status alone for the caller's fix loop;
  * returns ``None`` when no marker matched.

``send_to_unit`` calls the helper AFTER ``worker.resume`` returns and BEFORE
recording ``{role}_manual_message`` so the structured marker lands chronologically
before the human-issued audit row.

These tests are written from the unit description independently of the coder's
test file (``tests/test_tools_execution.py::TestSendToUnitTerminalMarker``) so
divergences flag drift in either direction.
"""

from __future__ import annotations

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared fixtures / helpers ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend CI is green so the (unused-by-send_to_unit) gate stays out of the way."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


class _StubWorker:
    """Minimal ManagedAgentWorker stand-in.

    ``spawn`` returns ``(session_id, response)`` like the real thing. ``resume``
    returns the canned ``resume_response`` so ``send_to_unit`` sees a marker it
    can parse.
    """

    def __init__(self, role: str, *, spawn_response: str = "", resume_response: str = ""):
        self.role = role
        self.spawn_response = spawn_response
        self.resume_response = resume_response
        self.spawn_calls: list[tuple[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []

    def spawn(self, task: str, *, title: str | None = None):
        sid = f"sesn-{self.role}-{len(self.spawn_calls)}"
        self.spawn_calls.append((task, title))
        return sid, self.spawn_response

    def resume(self, session_id: str, message: str) -> str:
        self.resume_calls.append((session_id, message))
        return self.resume_response


def _install_worker_factory(monkeypatch, *, spawn_response="", resume_response=""):
    """Make ManagedAgentWorker(role=...) return a per-role stub."""
    instances: dict[str, _StubWorker] = {}

    def factory(role: str) -> _StubWorker:
        if role not in instances:
            instances[role] = _StubWorker(
                role,
                spawn_response=spawn_response,
                resume_response=resume_response,
            )
        return instances[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return instances


def _stub_github_sideeffects(monkeypatch):
    """Neutralise PR-side-effect helpers — _record_terminal_marker doesn't touch
    them, but the caller (spawn_tester/spawn_reviewer/address_review) might."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests",
        lambda *a, **k: 0,
    )


def _seed_feature(feature_id="F-001", repo="https://github.com/o/r") -> None:
    state.save_feature(
        Feature(id=feature_id, title="t", description="d", repo_path=repo, status="approved")
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description="impl")],
    )
    state.approve_plan(feature_id)


def _seed_unit_with_session(
    *,
    role: str,
    status: str,
    review_round: int = 0,
    unit_id: str = "F-001-U-1",
    feature_id: str = "F-001",
) -> None:
    """Persist a WorkUnitState with the requested session id slot pre-filled.

    Tester/reviewer slots imply the coder already opened a PR, so we set
    ``coder_session_id`` too — matching the real-world post-spawn_unit shape.
    """
    _seed_feature(feature_id)
    fields: dict = {
        "unit_id": unit_id,
        "feature_id": feature_id,
        "status": status,
        "branch": "feat/branch",
        "pr_number": 7,
        "review_round": review_round,
    }
    sid_attr = f"{role}_session_id"
    fields[sid_attr] = f"sesn-{role}"
    if role != "coder":
        fields["coder_session_id"] = "sesn-coder"
    state.upsert_unit_state(WorkUnitState(**fields))


def _event_types(unit_id: str) -> list[str]:
    return [e["event_type"] for e in state.list_events(unit_id)]


def _event_of(unit_id: str, event_type: str) -> dict | None:
    for e in state.list_events(unit_id):
        if e["event_type"] == event_type:
            return e
    return None


# --------------------------- _record_terminal_marker (helper-level) ---------------------------


class TestRecordTerminalMarkerHelper:
    """Direct tests of the extracted helper.

    Calls ``execution._record_terminal_marker`` without going through
    ``send_to_unit`` so we can assert the helper's own contract (return shape,
    event recorded, status transition) regardless of caller plumbing.
    """

    # --- success markers per role flip status to in_ci ---

    def test_tester_tests_pass_flips_to_in_ci_and_records_event(self, tmp_state_db):
        _seed_unit_with_session(role="tester", status="testing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="passed everything\nTESTS_PASS\n",
            session_id="sesn-tester",
            cycle_number=2,
        )

        assert out is not None
        assert out["marker"] == "TESTS_PASS"
        assert state.get_unit_state("F-001-U-1").status == "in_ci"
        ev = _event_of("F-001-U-1", "tests_pass")
        assert ev is not None
        assert ev["source"] == "tester"
        assert ev["session_id"] == "sesn-tester"
        assert ev["cycle_number"] == 2

    def test_reviewer_recommend_merge_flips_to_awaiting_merge_and_records_reason(
        self, tmp_state_db
    ):
        """F-009-U-4 promoted REVIEW_RECOMMEND_MERGE's target from ``in_ci`` to
        ``approved_awaiting_merge``: the marker is terminal for the cycle, so
        the unit lands in the bucket that cycle_review's ``_emit_terminal``
        would otherwise have set."""
        _seed_unit_with_session(role="reviewer", status="reviewing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="reviewer",
            response="endorsed for merge\nREVIEW_RECOMMEND_MERGE: tests cover the new path",
            session_id="sesn-reviewer",
            cycle_number=1,
        )

        assert out is not None
        assert out["marker"] == "REVIEW_RECOMMEND_MERGE"
        assert "tests cover the new path" in out["reason"]
        assert state.get_unit_state("F-001-U-1").status == "approved_awaiting_merge"
        ev = _event_of("F-001-U-1", "reviewer_recommend_merge")
        assert ev is not None
        assert "tests cover the new path" in ev["summary"]
        assert ev["cycle_number"] == 1

    def test_reviewer_comment_only_flips_to_in_ci(self, tmp_state_db):
        _seed_unit_with_session(role="reviewer", status="reviewing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="reviewer",
            response="REVIEW_COMMENT",
            session_id="sesn-reviewer",
            cycle_number=0,
        )

        assert out is not None
        assert out["marker"] == "REVIEW_COMMENT"
        # REVIEW_COMMENT is a success-side terminal — the reviewer endorsed
        # implicitly by not requesting changes. Status flips to in_ci.
        assert state.get_unit_state("F-001-U-1").status == "in_ci"
        assert _event_of("F-001-U-1", "reviewer_comment") is not None

    def test_coder_fix_pushed_flips_to_in_ci(self, tmp_state_db):
        _seed_unit_with_session(role="coder", status="fixing", review_round=2)

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="patch applied and pushed\nFIX_PUSHED\n",
            session_id="sesn-coder",
            cycle_number=2,
        )

        assert out is not None
        assert out["marker"] == "FIX_PUSHED"
        assert state.get_unit_state("F-001-U-1").status == "in_ci"
        ev = _event_of("F-001-U-1", "fix_pushed")
        assert ev is not None
        assert ev["cycle_number"] == 2

    def test_coder_pr_url_records_pr_opened_event(self, tmp_state_db):
        _seed_unit_with_session(role="coder", status="coding")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="PR_URL: https://github.com/o/r/pull/123",
            session_id="sesn-coder",
            cycle_number=0,
        )

        assert out is not None
        assert out["marker"] == "PR_URL"
        assert out["pr_number"] == 123
        assert out["pr_url"] == "https://github.com/o/r/pull/123"
        assert state.get_unit_state("F-001-U-1").status == "in_ci"
        ev = _event_of("F-001-U-1", "pr_opened")
        assert ev is not None
        assert "123" in ev["summary"] or "123" in (ev.get("details") or "")

    # --- non-flipping success-side markers ---

    def test_tester_bug_found_records_event_without_flipping_status(self, tmp_state_db):
        _seed_unit_with_session(role="tester", status="testing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="found a regression\nBUG_FOUND: divide-by-zero on n=0",
            session_id="sesn-tester",
            cycle_number=0,
        )

        assert out is not None
        assert out["marker"] == "BUG_FOUND"
        assert "divide-by-zero" in out["bug"]
        # status MUST stay 'testing' — the caller's fix loop is what
        # drives the next address_review cycle.
        assert state.get_unit_state("F-001-U-1").status == "testing"
        ev = _event_of("F-001-U-1", "tester_bug_found")
        assert ev is not None
        assert "divide-by-zero" in ev["summary"]

    def test_reviewer_request_changes_does_not_flip_status(self, tmp_state_db):
        _seed_unit_with_session(role="reviewer", status="reviewing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="reviewer",
            response="REVIEW_REQUEST_CHANGES: rename the public symbol",
            session_id="sesn-reviewer",
            cycle_number=0,
        )

        assert out is not None
        assert out["marker"] == "REVIEW_REQUEST_CHANGES"
        assert "rename" in out["issue"]
        assert state.get_unit_state("F-001-U-1").status == "reviewing"
        assert _event_of("F-001-U-1", "reviewer_request_changes") is not None

    # --- BLOCKED is universal across roles ---

    @pytest.mark.parametrize(
        "role,from_status",
        [
            ("coder", "fixing"),
            ("tester", "testing"),
            ("reviewer", "reviewing"),
        ],
    )
    def test_blocked_marker_escalates_for_every_role(self, tmp_state_db, role, from_status):
        _seed_unit_with_session(role=role, status=from_status)

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role=role,
            response="ran into a wall\nBLOCKED: reason=auth_failure | 401 from GitHub",
            session_id=f"sesn-{role}",
            cycle_number=0,
        )

        assert out is not None
        assert out["marker"] == "BLOCKED"
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        assert "[auth_failure]" in s.last_error
        assert _event_of("F-001-U-1", f"{role}_blocked") is not None

    def test_blocked_event_name_override_used_by_address_review(self, tmp_state_db):
        """``blocked_event`` kwarg lets address_review keep its historic
        ``coder_blocked_on_fix`` discriminator. Default ``coder_blocked``
        must still be the bare default."""
        _seed_unit_with_session(role="coder", status="fixing")

        execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="BLOCKED: reason=unknown | couldn't apply",
            session_id="sesn-coder",
            cycle_number=1,
            blocked_event="coder_blocked_on_fix",
        )

        types = _event_types("F-001-U-1")
        assert "coder_blocked_on_fix" in types
        assert "coder_blocked" not in types

    # --- cross-role markers ignored ---

    def test_tester_response_with_reviewer_marker_returns_none(self, tmp_state_db):
        """A tester response containing REVIEW_RECOMMEND_MERGE is NOT a tester
        marker. Helper returns None, status unchanged, no event recorded."""
        _seed_unit_with_session(role="tester", status="testing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="tester",
            response="REVIEW_RECOMMEND_MERGE: not mine to emit",
            session_id="sesn-tester",
            cycle_number=0,
        )

        assert out is None
        assert state.get_unit_state("F-001-U-1").status == "testing"
        assert "reviewer_recommend_merge" not in _event_types("F-001-U-1")

    def test_coder_response_with_tests_pass_returns_none(self, tmp_state_db):
        _seed_unit_with_session(role="coder", status="coding")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="coder",
            response="prose\nTESTS_PASS\n",
            session_id="sesn-coder",
            cycle_number=0,
        )

        assert out is None
        assert state.get_unit_state("F-001-U-1").status == "coding"
        assert "tests_pass" not in _event_types("F-001-U-1")

    def test_reviewer_response_with_fix_pushed_returns_none(self, tmp_state_db):
        _seed_unit_with_session(role="reviewer", status="reviewing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="reviewer",
            response="just chatting and saying FIX_PUSHED maybe",
            session_id="sesn-reviewer",
            cycle_number=0,
        )

        assert out is None
        assert state.get_unit_state("F-001-U-1").status == "reviewing"
        assert "fix_pushed" not in _event_types("F-001-U-1")

    def test_no_marker_at_all_returns_none(self, tmp_state_db):
        _seed_unit_with_session(role="reviewer", status="reviewing")

        out = execution._record_terminal_marker(
            unit_id="F-001-U-1",
            feature_id="F-001",
            role="reviewer",
            response="I had thoughts but emitted no marker.",
            session_id="sesn-reviewer",
            cycle_number=0,
        )

        assert out is None
        assert state.get_unit_state("F-001-U-1").status == "reviewing"
        assert _event_types("F-001-U-1") == []


# --------------------------- send_to_unit integration ---------------------------


class TestSendToUnitWiring:
    """``send_to_unit`` calls ``_record_terminal_marker`` after ``worker.resume``
    succeeds and BEFORE recording ``{role}_manual_message`` (chronological replay).

    This is the audit-Gap-B/I closure: previously every endorsement that went
    through send_to_unit was invisible to the orchestrator's state.
    """

    @pytest.mark.parametrize(
        ("role", "from_status", "response", "expected_event", "expected_status"),
        [
            (
                "reviewer",
                "reviewing",
                "endorsed\nREVIEW_RECOMMEND_MERGE: ship it",
                "reviewer_recommend_merge",
                # Reviewer endorsement is terminal — F-009-U-4.
                "approved_awaiting_merge",
            ),
            ("tester", "testing", "all clean\nTESTS_PASS\n", "tests_pass", "in_ci"),
            (
                "reviewer",
                "reviewing",
                "comment only\nREVIEW_COMMENT\n",
                "reviewer_comment",
                "in_ci",
            ),
            ("coder", "fixing", "pushed\nFIX_PUSHED\n", "fix_pushed", "in_ci"),
        ],
    )
    def test_success_marker_writes_structured_event_AND_manual_message_AND_flips_status(
        self,
        tmp_state_db,
        monkeypatch,
        role,
        from_status,
        response,
        expected_event,
        expected_status,
    ):
        _seed_unit_with_session(role=role, status=from_status)
        _install_worker_factory(monkeypatch, resume_response=response)

        ret = execution.send_to_unit("F-001-U-1", role, "anything")

        # 1. send_to_unit still returns the worker's raw response verbatim.
        assert ret == response

        # 2. status flipped per marker-spec target (REVIEW_RECOMMEND_MERGE ->
        #    approved_awaiting_merge; the rest -> in_ci).
        assert state.get_unit_state("F-001-U-1").status == expected_status

        # 3. BOTH events recorded — structured marker, then manual_message.
        types = _event_types("F-001-U-1")
        assert expected_event in types
        manual = f"{role}_manual_message"
        assert manual in types
        # chronological replay order: structured marker FIRST.
        assert types.index(expected_event) < types.index(manual)

    def test_bug_found_records_event_keeps_status_in_testing(self, tmp_state_db, monkeypatch):
        _seed_unit_with_session(role="tester", status="testing")
        _install_worker_factory(
            monkeypatch,
            resume_response="failing assertion\nBUG_FOUND: off-by-one in counter",
        )

        execution.send_to_unit("F-001-U-1", "tester", "please rerun")

        assert state.get_unit_state("F-001-U-1").status == "testing"
        types = _event_types("F-001-U-1")
        assert "tester_bug_found" in types
        assert "tester_manual_message" in types
        assert types.index("tester_bug_found") < types.index("tester_manual_message")

    def test_request_changes_records_event_keeps_status_in_reviewing(
        self, tmp_state_db, monkeypatch
    ):
        _seed_unit_with_session(role="reviewer", status="reviewing")
        _install_worker_factory(
            monkeypatch,
            resume_response="REVIEW_REQUEST_CHANGES: needs a docstring",
        )

        execution.send_to_unit("F-001-U-1", "reviewer", "re-review please")

        assert state.get_unit_state("F-001-U-1").status == "reviewing"
        assert "reviewer_request_changes" in _event_types("F-001-U-1")
        assert "reviewer_manual_message" in _event_types("F-001-U-1")

    @pytest.mark.parametrize(
        "role,from_status",
        [
            ("coder", "fixing"),
            ("tester", "testing"),
            ("reviewer", "reviewing"),
        ],
    )
    def test_blocked_marker_escalates_and_records_manual_message(
        self, tmp_state_db, monkeypatch, role, from_status
    ):
        _seed_unit_with_session(role=role, status=from_status)
        _install_worker_factory(
            monkeypatch,
            resume_response="couldn't proceed\nBLOCKED: reason=auth_failure | 401",
        )

        execution.send_to_unit("F-001-U-1", role, "retry?")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        assert "[auth_failure]" in s.last_error
        types = _event_types("F-001-U-1")
        assert f"{role}_blocked" in types
        assert f"{role}_manual_message" in types
        assert types.index(f"{role}_blocked") < types.index(f"{role}_manual_message")

    # --- cross-role isolation ---

    def test_cross_role_tester_with_reviewer_marker_only_records_manual_message(
        self, tmp_state_db, monkeypatch
    ):
        """Per spec: send_to_unit(tester, ...) whose response contains
        REVIEW_RECOMMEND_MERGE records only ``tester_manual_message``."""
        _seed_unit_with_session(role="tester", status="testing")
        _install_worker_factory(
            monkeypatch,
            resume_response="endorsed\nREVIEW_RECOMMEND_MERGE: tests cover new path",
        )

        execution.send_to_unit("F-001-U-1", "tester", "anything")

        assert state.get_unit_state("F-001-U-1").status == "testing"
        types = _event_types("F-001-U-1")
        assert types == ["tester_manual_message"]

    def test_cross_role_coder_with_tester_marker_does_not_flip(self, tmp_state_db, monkeypatch):
        _seed_unit_with_session(role="coder", status="coding")
        _install_worker_factory(monkeypatch, resume_response="some prose\nTESTS_PASS\n")

        execution.send_to_unit("F-001-U-1", "coder", "anything")

        assert state.get_unit_state("F-001-U-1").status == "coding"
        assert _event_types("F-001-U-1") == ["coder_manual_message"]

    def test_no_marker_only_records_manual_message_event(self, tmp_state_db, monkeypatch):
        _seed_unit_with_session(role="reviewer", status="reviewing")
        _install_worker_factory(monkeypatch, resume_response="just thinking out loud, no marker")

        execution.send_to_unit("F-001-U-1", "reviewer", "ping")

        assert state.get_unit_state("F-001-U-1").status == "reviewing"
        assert _event_types("F-001-U-1") == ["reviewer_manual_message"]

    def test_worker_resume_raise_records_nothing(self, tmp_state_db, monkeypatch):
        """Early return on ``worker.resume`` exception — neither the
        structured marker nor the audit-log row should land."""
        _seed_unit_with_session(role="reviewer", status="reviewing")

        class _BlowUp:
            def __init__(self, role):
                pass

            def resume(self, *a, **k):
                raise RuntimeError("session expired")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _BlowUp)

        ret = execution.send_to_unit("F-001-U-1", "reviewer", "ping")
        assert "ERROR resuming reviewer" in ret
        assert _event_types("F-001-U-1") == []

    def test_cycle_number_propagates_from_unit_state(self, tmp_state_db, monkeypatch):
        """The structured marker event must carry the unit's current
        ``review_round`` as ``cycle_number`` (chronological cycle context)."""
        _seed_unit_with_session(role="reviewer", status="reviewing", review_round=3)
        _install_worker_factory(monkeypatch, resume_response="REVIEW_COMMENT\n")

        execution.send_to_unit("F-001-U-1", "reviewer", "ping")

        ev = _event_of("F-001-U-1", "reviewer_comment")
        assert ev is not None
        assert ev["cycle_number"] == 3

    def test_manual_message_details_capture_first_500_chars(self, tmp_state_db, monkeypatch):
        """``send_to_unit`` truncates the message it stores in the audit row's
        ``details`` field to 500 chars (existing behavior preserved by refactor)."""
        _seed_unit_with_session(role="reviewer", status="reviewing")
        _install_worker_factory(monkeypatch, resume_response="REVIEW_COMMENT\n")

        long_message = "x" * 1234
        execution.send_to_unit("F-001-U-1", "reviewer", long_message)

        ev = _event_of("F-001-U-1", "reviewer_manual_message")
        assert ev is not None
        assert len(ev["details"]) == 500


# --------------------------- refactor invariant: spawn_* / address_review ---------------------------


class TestRefactorInvariant:
    """The unit refactors ``spawn_tester`` / ``spawn_reviewer`` /
    ``address_review`` to call ``_record_terminal_marker``. Their externally
    observable behavior — status transitions, event-log writes, JSON return
    shape — must be unchanged."""

    def test_spawn_tester_tests_pass_still_records_and_flips(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit_with_session(role="coder", status="in_ci")
        _install_worker_factory(monkeypatch, spawn_response="all good\nTESTS_PASS\n")
        _stub_github_sideeffects(monkeypatch)

        execution.spawn_tester("F-001", "F-001-U-1")

        assert state.get_unit_state("F-001-U-1").status == "in_ci"
        assert _event_of("F-001-U-1", "tests_pass") is not None

    def test_spawn_tester_bug_found_records_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """spawn_tester BUG_FOUND must record ``tester_bug_found`` via the helper.

        We don't assert post-call status here — spawn_tester's pre-existing
        ``upsert_unit_state`` ordering writes ``unit_state`` back at the end of
        the function (with the captured-earlier status), which is independent of
        the refactor under test. The helper-level test
        ``test_tester_bug_found_records_event_without_flipping_status`` covers
        the in-helper status invariant.
        """
        _seed_unit_with_session(role="coder", status="in_ci")
        _install_worker_factory(
            monkeypatch,
            spawn_response="failing test:\nBUG_FOUND: off-by-one",
        )
        _stub_github_sideeffects(monkeypatch)

        execution.spawn_tester("F-001", "F-001-U-1")

        ev = _event_of("F-001-U-1", "tester_bug_found")
        assert ev is not None
        assert "off-by-one" in ev["summary"]

    def test_spawn_reviewer_recommend_merge_records_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """F-009-U-4: spawn_reviewer's REVIEW_RECOMMEND_MERGE path now lands
        the unit in ``approved_awaiting_merge`` (same status cycle_review's
        terminal would set)."""
        _seed_unit_with_session(role="coder", status="in_ci")
        _install_worker_factory(
            monkeypatch,
            spawn_response="endorsed\nREVIEW_RECOMMEND_MERGE: tests cover everything",
        )
        _stub_github_sideeffects(monkeypatch)

        execution.spawn_reviewer("F-001", "F-001-U-1")

        assert state.get_unit_state("F-001-U-1").status == "approved_awaiting_merge"
        ev = _event_of("F-001-U-1", "reviewer_recommend_merge")
        assert ev is not None
        assert "tests cover everything" in ev["summary"]

    def test_address_review_fix_pushed_records_event(self, tmp_state_db, monkeypatch):
        _seed_unit_with_session(role="coder", status="in_ci")
        _install_worker_factory(monkeypatch, resume_response="ok\nFIX_PUSHED\n")
        _stub_github_sideeffects(monkeypatch)

        execution.address_review("F-001-U-1", "tester", "fix the bug")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci"
        assert _event_of("F-001-U-1", "fix_pushed") is not None
        # address_review increments review_round before the helper fires
        assert s.review_round >= 1

    def test_address_review_blocked_uses_coder_blocked_on_fix_event_name(
        self, tmp_state_db, monkeypatch
    ):
        """address_review historically distinguished a fix-time BLOCKED via
        ``coder_blocked_on_fix`` (not ``coder_blocked``). The refactor must
        preserve that discriminator via the ``blocked_event`` override."""
        _seed_unit_with_session(role="coder", status="in_ci")
        _install_worker_factory(
            monkeypatch,
            resume_response="couldn't apply\nBLOCKED: reason=unknown | needs human",
        )
        _stub_github_sideeffects(monkeypatch)

        execution.address_review("F-001-U-1", "reviewer", "rework")

        types = _event_types("F-001-U-1")
        assert "coder_blocked_on_fix" in types
        assert "coder_blocked" not in types
        assert state.get_unit_state("F-001-U-1").status == "escalated"
