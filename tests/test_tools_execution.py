"""Tests for orchestrator/tools/execution.py — spawn + address + cycle_review.

All `ManagedAgentWorker` instances are replaced with a `FakeWorker` that
returns canned strings. github.* helpers are patched to no-op so tests
don't touch the real GitHub API.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- autouse: pretend CI is green ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green for tests in this module.

    Most tests in this file pre-date the CI gate (added in commit that
    introduced orchestrator/ci_wait.py). They don't care about CI semantics —
    they exercise the spawn / address_review / cycle_review state machine.
    Forcing CI to "green" here keeps those tests focused.

    Tests that DO exercise the CI gate (TestCycleReviewCIGate below) override
    this fixture by monkeypatching `wait_for_ci` themselves AFTER this
    fixture runs.
    """

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


# --------------------------- fakes ---------------------------


class FakeWorker:
    """Stub for ManagedAgentWorker that returns a pre-canned response."""

    def __init__(self, role: str, spawn_response: str = "", resume_response: str = ""):
        self.role = role
        self._spawn_response = spawn_response
        self._resume_response = resume_response
        self.spawn_calls: list[tuple[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        sid = f"sesn-{self.role}-{len(self.spawn_calls)}"
        self.spawn_calls.append((task, title))
        return sid, self._spawn_response

    def resume(self, session_id: str, msg: str) -> str:
        self.resume_calls.append((session_id, msg))
        return self._resume_response

    def archive(self, session_id: str) -> None:
        pass


def _install_fake_worker(monkeypatch, spawn_response="", resume_response=""):
    """Return a factory that builds a FakeWorker for any role.

    Each role gets its own FakeWorker instance so tests can inspect calls.
    """
    instances: dict[str, FakeWorker] = {}

    def factory(role: str) -> FakeWorker:
        if role not in instances:
            instances[role] = FakeWorker(role, spawn_response, resume_response)
        return instances[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return instances


def _stub_github(monkeypatch, copilot_review=None):
    """Patch github.* helpers to no-op stubs."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.request_copilot_review",
        lambda *a, **k: {"requested": True, "status_code": 201, "note": ""},
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.wait_for_copilot_review",
        lambda *a, **k: copilot_review,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("owner", "repo"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.get_pr_state",
        lambda *a, **k: {"head_sha": "deadbeefcafe", "state": "open", "merged": False},
    )


def _setup_feature(feature_id="F-001", repo="https://github.com/o/r"):
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=repo,
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description="impl this"
            ),
        ],
    )
    state.approve_plan(feature_id)


# --------------------------- spawn_unit ---------------------------


class TestSpawnUnit:
    def test_no_feature(self, tmp_state_db):
        assert "feature F-XXX not found" in execution.spawn_unit("F-XXX", "U-1")

    def test_feature_not_approved(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description="d", status="draft"))
        msg = execution.spawn_unit("F", "U-1")
        assert "ERROR" in msg and "approved" in msg

    def test_no_repo_path(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description="d", status="approved"))
        msg = execution.spawn_unit("F", "U-1")
        assert "no repo_path" in msg

    def test_no_plan(self, tmp_state_db):
        state.save_feature(
            Feature(
                id="F",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            ),
        )
        msg = execution.spawn_unit("F", "U-1")
        assert "no plan" in msg

    def test_unit_not_in_plan(self, tmp_state_db):
        _setup_feature()
        msg = execution.spawn_unit("F-001", "F-001-U-9")
        assert "not in plan" in msg

    def test_unit_already_has_coder(self, tmp_state_db):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="coding",
                coder_session_id="existing",
            )
        )
        msg = execution.spawn_unit("F-001", "F-001-U-1")
        assert "already has coder session" in msg

    def test_missing_github_token(self, tmp_state_db, no_github_token):
        _setup_feature()
        msg = execution.spawn_unit("F-001", "F-001-U-1")
        assert "no GitHub auth" in msg

    def test_happy_path_with_pr_url(self, tmp_state_db, with_github_token, monkeypatch):
        _setup_feature()
        _install_fake_worker(
            monkeypatch,
            spawn_response="PR_URL: https://github.com/o/r/pull/42",
        )
        _stub_github(monkeypatch)

        out = execution.spawn_unit("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["pr_number"] == 42
        assert parsed["pr_url"] == "https://github.com/o/r/pull/42"

        # State now reflects in_ci + session id
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci"
        assert s.pr_number == 42
        assert s.coder_session_id.startswith("sesn-coder-")

    def test_coder_blocked(self, tmp_state_db, with_github_token, monkeypatch):
        _setup_feature()
        _install_fake_worker(monkeypatch, spawn_response="i tried but\nBLOCKED: spec ambiguous")
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        out = execution.spawn_unit("F-001", "F-001-U-1")
        assert "BLOCKED" in out
        assert "spec ambiguous" in out
        assert state.get_unit_state("F-001-U-1").status == "escalated"

    def test_coder_no_marker(self, tmp_state_db, with_github_token, monkeypatch):
        _setup_feature()
        _install_fake_worker(monkeypatch, spawn_response="I wandered off topic.")
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        out = execution.spawn_unit("F-001", "F-001-U-1")
        assert "ESCALATED" in out
        assert state.get_unit_state("F-001-U-1").status == "escalated"

    def test_worker_raises(self, tmp_state_db, with_github_token, monkeypatch):
        _setup_feature()

        class BlowUpWorker:
            def __init__(self, role):
                pass

            def spawn(self, *a, **k):
                raise RuntimeError("anthropic 503")

        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            BlowUpWorker,
        )
        out = execution.spawn_unit("F-001", "F-001-U-1")
        assert "ERROR spawning coder" in out
        assert state.get_unit_state("F-001-U-1").status == "escalated"


# --------------------------- spawn_tester ---------------------------


def _seed_coded_unit(unit_id="F-001-U-1", feature_id="F-001"):
    """Set up a feature + unit that already has a PR (coder ran successfully)."""
    _setup_feature(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="in_ci",
            branch="feat/branch",
            pr_number=5,
            coder_session_id="sesn-c",
        )
    )


class TestSpawnTester:
    def test_no_feature(self, tmp_state_db):
        assert "feature F-XXX not found" in execution.spawn_tester("F-XXX", "U-1")

    def test_no_state(self, tmp_state_db):
        _setup_feature()
        assert "no state for unit" in execution.spawn_tester("F-001", "F-001-U-1")

    def test_no_pr_yet(self, tmp_state_db):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="coding")
        )
        msg = execution.spawn_tester("F-001", "F-001-U-1")
        assert "no branch/PR yet" in msg

    def test_tester_session_already_exists(self, tmp_state_db):
        _seed_coded_unit()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                branch="b",
                pr_number=5,
                tester_session_id="existing",
            )
        )
        msg = execution.spawn_tester("F-001", "F-001-U-1")
        assert "tester session already exists" in msg

    def test_missing_token(self, tmp_state_db, no_github_token):
        _seed_coded_unit()
        assert "no GitHub auth" in execution.spawn_tester("F-001", "F-001-U-1")

    def test_tests_pass(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, spawn_response="all good\nTESTS_PASS")
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "TESTS_PASS"
        assert state.get_unit_state("F-001-U-1").status == "in_ci"

    def test_bug_found(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(
            monkeypatch,
            spawn_response="failing test:\nBUG_FOUND: divide-by-zero on n=0",
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "BUG_FOUND"
        assert "divide-by-zero" in parsed["bug"]

    def test_blocked(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, spawn_response="BLOCKED: pytest not installed")
        _stub_github(monkeypatch)

        msg = execution.spawn_tester("F-001", "F-001-U-1")
        assert "BLOCKED" in msg
        assert state.get_unit_state("F-001-U-1").status == "escalated"

    def test_no_marker(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, spawn_response="i didn't say anything useful")
        _stub_github(monkeypatch)

        msg = execution.spawn_tester("F-001", "F-001-U-1")
        assert "ESCALATED" in msg

    def test_worker_raises(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()

        class BlowUp:
            def __init__(self, role):
                pass

            def spawn(self, *a, **k):
                raise RuntimeError("network")

        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            BlowUp,
        )
        msg = execution.spawn_tester("F-001", "F-001-U-1")
        assert "ERROR spawning tester" in msg


# --------------------------- spawn_reviewer ---------------------------


class TestSpawnReviewer:
    def test_no_pr(self, tmp_state_db):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="coding")
        )
        msg = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "no PR yet" in msg

    def test_reviewer_already_exists(self, tmp_state_db):
        _seed_coded_unit()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                pr_number=5,
                reviewer_session_id="existing",
            )
        )
        msg = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "already exists" in msg

    def test_review_approved_escalates_as_prompt_drift(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """REVIEW_APPROVED is deprecated. A reviewer that emits it (prompt
        drift / regression) should fall through to no-marker escalation,
        not be silently treated as a passing outcome."""
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, spawn_response="looks great\nREVIEW_APPROVED")
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "ESCALATED" in out
        assert "no marker" in out

    def test_review_recommend_merge(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(
            monkeypatch,
            spawn_response="endorsed\nREVIEW_RECOMMEND_MERGE: tests cover everything",
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"
        assert "tests cover everything" in parsed["reason"]

    def test_review_request_changes(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(
            monkeypatch,
            spawn_response="REVIEW_REQUEST_CHANGES: missing edge case for empty input",
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_REQUEST_CHANGES"
        assert "empty input" in parsed["issue"]

    def test_review_comment_only(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, spawn_response="REVIEW_COMMENT")
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_COMMENT"

    def test_review_blocked(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, spawn_response="BLOCKED: PR diff unreadable")
        _stub_github(monkeypatch)

        msg = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "BLOCKED" in msg
        assert state.get_unit_state("F-001-U-1").status == "escalated"

    def test_review_no_marker(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, spawn_response="some prose with no marker")
        _stub_github(monkeypatch)

        msg = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "ESCALATED" in msg

    def test_worker_raises(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()

        class BlowUp:
            def __init__(self, role):
                pass

            def spawn(self, *a, **k):
                raise RuntimeError("boom")

        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            BlowUp,
        )
        msg = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "ERROR spawning reviewer" in msg


# --------------------------- address_review ---------------------------


class TestAddressReview:
    def test_bad_source(self, tmp_state_db):
        msg = execution.address_review("U1", "hacker", "fix it")
        assert "source must be" in msg

    def test_no_state(self, tmp_state_db):
        msg = execution.address_review("nope", "tester", "fix it")
        assert "no state for" in msg

    def test_no_coder_session(self, tmp_state_db):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="in_ci")
        )
        msg = execution.address_review("F-001-U-1", "tester", "fix")
        assert "no coder session" in msg

    def test_no_pr_number(self, tmp_state_db):
        """address_review must refuse to resume the coder if no PR exists
        yet — composer needs a real pr_number, and fix-loop instructions
        in the system prompt operate against PR-scoped endpoints."""
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="coding",
                coder_session_id="s-1",
                # branch set, pr_number deliberately unset
                branch="feat/F-001-foo-u-1",
            )
        )
        msg = execution.address_review("F-001-U-1", "tester", "fix")
        assert "no PR" in msg
        assert "spawn coder first" in msg

    def test_fix_pushed(self, tmp_state_db, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, resume_response="ok\nFIX_PUSHED\ndone")
        _stub_github(monkeypatch)

        out = execution.address_review("F-001-U-1", "tester", "fix the bug")
        parsed = json.loads(out)
        assert parsed["outcome"] == "FIX_PUSHED"
        assert parsed["cycle"] == 1

    def test_blocked_on_fix(self, tmp_state_db, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, resume_response="BLOCKED: needs human to redesign")
        _stub_github(monkeypatch)

        msg = execution.address_review("F-001-U-1", "tester", "fix")
        assert "BLOCKED" in msg
        assert state.get_unit_state("F-001-U-1").status == "escalated"

    def test_no_marker_on_fix(self, tmp_state_db, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, resume_response="mumbled some prose")
        _stub_github(monkeypatch)

        msg = execution.address_review("F-001-U-1", "tester", "fix")
        assert "ESCALATED" in msg

    def test_pr_url_during_fix_escalates_without_spurious_pr_opened(
        self, tmp_state_db, monkeypatch
    ):
        """A coder resume returning PR_URL (instead of FIX_PUSHED) is anomalous
        — the unit already has a PR from spawn_unit. address_review must
        escalate without writing a spurious `pr_opened` event or flipping the
        unit to `in_ci`. (Regression: PR #34 reviewer H1.)"""
        _seed_coded_unit()
        _install_fake_worker(
            monkeypatch,
            resume_response="PR_URL: https://github.com/o/r/pull/99",
        )
        _stub_github(monkeypatch)

        msg = execution.address_review("F-001-U-1", "tester", "fix")

        # Behaviour matches pre-helper: escalated + fix_no_marker audit.
        assert "ESCALATED" in msg
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"

        # No `pr_opened` was written (the PR already existed; matching the
        # spawn_unit-flavoured branch here would lie about lifecycle).
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "pr_opened" not in types
        assert "fix_no_marker" in types

    def test_worker_resume_raises(self, tmp_state_db, monkeypatch):
        _seed_coded_unit()

        class BlowUp:
            def __init__(self, role):
                pass

            def resume(self, *a, **k):
                raise RuntimeError("retrieve failed")

        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            BlowUp,
        )
        msg = execution.address_review("F-001-U-1", "tester", "fix")
        assert "ERROR resuming coder" in msg


# --------------------------- send_to_unit ---------------------------


class TestSendToUnit:
    def test_bad_role(self, tmp_state_db):
        msg = execution.send_to_unit("U1", "hacker", "hi")
        assert "role must be" in msg

    def test_no_state(self, tmp_state_db):
        msg = execution.send_to_unit("nope", "coder", "hi")
        assert "no state for" in msg

    def test_no_session(self, tmp_state_db):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="coding")
        )
        msg = execution.send_to_unit("F-001-U-1", "tester", "hi")
        assert "no tester session" in msg

    def test_happy_path(self, tmp_state_db, monkeypatch):
        _seed_coded_unit()
        _install_fake_worker(monkeypatch, resume_response="coder responded")

        out = execution.send_to_unit("F-001-U-1", "coder", "what's up?")
        assert out == "coder responded"

    def test_worker_raises(self, tmp_state_db, monkeypatch):
        _seed_coded_unit()

        class BlowUp:
            def __init__(self, role):
                pass

            def resume(self, *a, **k):
                raise RuntimeError("session expired")

        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            BlowUp,
        )
        msg = execution.send_to_unit("F-001-U-1", "coder", "hi")
        assert "ERROR resuming coder" in msg


# --------------------------- send_to_unit terminal-marker recording -----------


def _seed_unit_for_role(role: str, status: str) -> None:
    """Set up F-001 + F-001-U-1 with a session id assigned for ``role``.

    The unit starts in ``status`` so tests can assert the helper's status
    transition (testing/reviewing/fixing/coding → in_ci on success).
    """
    _setup_feature()
    kwargs = {
        "unit_id": "F-001-U-1",
        "feature_id": "F-001",
        "status": status,
        "branch": "feat/branch",
        "pr_number": 5,
    }
    sid_key = f"{role}_session_id"
    kwargs[sid_key] = f"sesn-{role}"
    if role != "coder":
        # tester/reviewer paths still need a coder session on the unit so
        # state shape matches real-world post-spawn_unit conditions.
        kwargs["coder_session_id"] = "sesn-coder"
    state.upsert_unit_state(WorkUnitState(**kwargs))


def _events_of_type(unit_id: str, event_type: str) -> list[dict]:
    return [e for e in state.list_events(unit_id) if e["event_type"] == event_type]


class TestSendToUnitTerminalMarker:
    """`send_to_unit` runs the per-role marker chain after `worker.resume`.

    Closes audit Gaps B/I: endorsements that previously went through
    `send_to_unit` were invisible to the orchestrator's state (only the
    ``_manual_message`` event was recorded, no status transition fired).
    """

    # --- success markers per role flip status to in_ci AND record both events

    @pytest.mark.parametrize(
        ("role", "from_status", "response", "expected_event"),
        [
            (
                "reviewer",
                "reviewing",
                "looks good\nREVIEW_RECOMMEND_MERGE: tests cover the new path",
                "reviewer_recommend_merge",
            ),
            ("tester", "testing", "all assertions hold\nTESTS_PASS", "tests_pass"),
            (
                "reviewer",
                "reviewing",
                "comment only\nREVIEW_COMMENT",
                "reviewer_comment",
            ),
            ("coder", "fixing", "pushed\nFIX_PUSHED", "fix_pushed"),
        ],
    )
    def test_success_marker_records_event_and_flips_to_in_ci(
        self, tmp_state_db, monkeypatch, role, from_status, response, expected_event
    ):
        _seed_unit_for_role(role, status=from_status)
        _install_fake_worker(monkeypatch, resume_response=response)

        out = execution.send_to_unit("F-001-U-1", role, "carry on")
        assert out == response  # worker output still returned verbatim

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci"

        # Both events recorded (structured marker first, manual_message second
        # — chronological replay order).
        events = state.list_events("F-001-U-1")
        types = [e["event_type"] for e in events]
        assert expected_event in types
        assert f"{role}_manual_message" in types
        assert types.index(expected_event) < types.index(f"{role}_manual_message")

    def test_coder_pr_url_via_send_to_unit_does_not_drift_state(self, tmp_state_db, monkeypatch):
        """A coder resume returning PR_URL (instead of FIX_PUSHED) is anomalous
        — the unit already has a PR from spawn_unit. The helper's PR_URL branch
        never updates ``WorkUnitState.pr_number``, so accepting it here would
        leave the audit log claiming a new PR opened (with the URL parsed from
        the response) while the row's ``pr_number`` stays stale. Downstream
        tools like ``spawn_tester`` query ``pr_number``, not the event log —
        the drift would surface as ``ERROR: no branch/PR yet`` (PR #34 reviewer
        M1).

        ``send_to_unit`` narrows the coder marker set to ``{FIX_PUSHED,
        BLOCKED}`` so PR_URL does not match; the response is still returned
        to the caller verbatim and the manual_message audit row still lands.
        """
        # Seed with an EXISTING pr_number that differs from the URL in the
        # response so a drift would be observable.
        _seed_unit_for_role("coder", status="coding")
        existing = state.get_unit_state("F-001-U-1")
        existing.pr_number = 7
        state.upsert_unit_state(existing)

        _install_fake_worker(monkeypatch, resume_response="PR_URL: https://github.com/o/r/pull/123")

        execution.send_to_unit("F-001-U-1", "coder", "any update?")

        s = state.get_unit_state("F-001-U-1")
        # No state drift: pr_number stays at 7, status stays in coding (the
        # from-state guard wouldn't flip it anyway, but check both ways).
        assert s.pr_number == 7
        assert s.status == "coding"

        # No spurious pr_opened event. Only the manual_message audit row.
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "pr_opened" not in types
        assert types == ["coder_manual_message"]

    # --- non-flipping success-side markers (BUG_FOUND, REQUEST_CHANGES) still record

    def test_bug_found_records_event_without_flipping_status(self, tmp_state_db, monkeypatch):
        _seed_unit_for_role("tester", status="testing")
        _install_fake_worker(
            monkeypatch, resume_response="failing test:\nBUG_FOUND: off-by-one in counter"
        )

        execution.send_to_unit("F-001-U-1", "tester", "rerun please")

        # BUG_FOUND keeps the unit in testing (caller's loop drives address_review)
        assert state.get_unit_state("F-001-U-1").status == "testing"
        assert _events_of_type("F-001-U-1", "tester_bug_found")
        assert _events_of_type("F-001-U-1", "tester_manual_message")

    def test_request_changes_records_event_without_flipping_status(self, tmp_state_db, monkeypatch):
        _seed_unit_for_role("reviewer", status="reviewing")
        _install_fake_worker(
            monkeypatch,
            resume_response="REVIEW_REQUEST_CHANGES: rename the public symbol",
        )

        execution.send_to_unit("F-001-U-1", "reviewer", "re-review please")

        assert state.get_unit_state("F-001-U-1").status == "reviewing"
        assert _events_of_type("F-001-U-1", "reviewer_request_changes")
        assert _events_of_type("F-001-U-1", "reviewer_manual_message")

    # --- BLOCKED markers per role escalate AND record both events

    @pytest.mark.parametrize(
        "role,from_status",
        [
            ("coder", "fixing"),
            ("tester", "testing"),
            ("reviewer", "reviewing"),
        ],
    )
    def test_blocked_marker_escalates(self, tmp_state_db, monkeypatch, role, from_status):
        _seed_unit_for_role(role, status=from_status)
        _install_fake_worker(monkeypatch, resume_response="BLOCKED: reason=auth_failure | 401")

        execution.send_to_unit("F-001-U-1", role, "please retry")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        assert "[auth_failure]" in s.last_error
        assert _events_of_type("F-001-U-1", f"{role}_blocked")
        assert _events_of_type("F-001-U-1", f"{role}_manual_message")

    # --- cross-role markers are ignored: status unchanged, only _manual_message

    def test_cross_role_marker_ignored_for_tester(self, tmp_state_db, monkeypatch):
        """A tester response containing REVIEW_RECOMMEND_MERGE is NOT a
        recognised tester marker — status stays in testing, no
        reviewer_* event recorded."""
        _seed_unit_for_role("tester", status="testing")
        _install_fake_worker(
            monkeypatch, resume_response="REVIEW_RECOMMEND_MERGE: not mine to emit"
        )

        execution.send_to_unit("F-001-U-1", "tester", "anything")

        assert state.get_unit_state("F-001-U-1").status == "testing"
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "reviewer_recommend_merge" not in types
        assert "tester_manual_message" in types

    def test_cross_role_marker_ignored_for_coder(self, tmp_state_db, monkeypatch):
        """A coder response containing TESTS_PASS does NOT flip status —
        TESTS_PASS is the tester's marker, not the coder's."""
        _seed_unit_for_role("coder", status="coding")
        _install_fake_worker(monkeypatch, resume_response="some prose\nTESTS_PASS")

        execution.send_to_unit("F-001-U-1", "coder", "anything")

        assert state.get_unit_state("F-001-U-1").status == "coding"
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "tests_pass" not in types
        assert "coder_manual_message" in types

    def test_no_marker_only_records_manual_message(self, tmp_state_db, monkeypatch):
        """No recognised marker in the response means the helper records
        nothing — only the standard `_manual_message` audit row fires."""
        _seed_unit_for_role("reviewer", status="reviewing")
        _install_fake_worker(monkeypatch, resume_response="just chatting, no marker")

        execution.send_to_unit("F-001-U-1", "reviewer", "anything")

        assert state.get_unit_state("F-001-U-1").status == "reviewing"
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert types == ["reviewer_manual_message"]

    def test_worker_failure_records_no_marker_event(self, tmp_state_db, monkeypatch):
        """If worker.resume raises, neither the structured marker nor the
        manual_message event should land — early return."""
        _seed_unit_for_role("reviewer", status="reviewing")

        class BlowUp:
            def __init__(self, role):
                pass

            def resume(self, *a, **k):
                raise RuntimeError("session expired")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", BlowUp)

        msg = execution.send_to_unit("F-001-U-1", "reviewer", "x")
        assert "ERROR resuming reviewer" in msg
        assert state.list_events("F-001-U-1") == []

    # --- terminal-status guard: don't clobber `done`/`escalated` from send_to_unit

    @pytest.mark.parametrize(
        ("role", "response"),
        [
            ("reviewer", "thanks!\nREVIEW_RECOMMEND_MERGE: glad to help"),
            ("tester", "everything still green\nTESTS_PASS"),
            ("reviewer", "REVIEW_COMMENT"),
            ("coder", "queued\nFIX_PUSHED"),
        ],
    )
    def test_success_marker_does_not_clobber_done_status(
        self, tmp_state_db, monkeypatch, role, response
    ):
        """Concrete drift scenario (PR #34 reviewer M1):
          1. PR merges; check_unit_pr flips unit to `done`.
          2. User runs send_to_unit to thank the agent.
          3. Agent (prompted to end with a marker) emits its terminal marker.
        Unit must stay `done` — re-flipping to `in_ci` would silently
        depopulate the dashboard's awaiting-merge bucket (and once F-009-U-2
        lands, the approved_awaiting_merge bucket). The structured event
        still records for the audit trail.
        """
        _seed_unit_for_role(role, status="done")
        _install_fake_worker(monkeypatch, resume_response=response)

        execution.send_to_unit("F-001-U-1", role, "appreciation")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "done"  # terminal preserved

        # Audit trail still complete: structured marker event AND _manual_message
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert f"{role}_manual_message" in types
        # Exactly one of the structured events should also land
        assert any(
            t in types
            for t in (
                "reviewer_recommend_merge",
                "tests_pass",
                "reviewer_comment",
                "fix_pushed",
            )
        )

    def test_blocked_marker_does_not_re_escalate_done_unit(self, tmp_state_db, monkeypatch):
        """Same principle as the success-side guard: a stray BLOCKED line
        in a send_to_unit reply against an already-`done` unit shouldn't
        flip it back to `escalated`. The audit trail still captures both
        events for human review."""
        _seed_unit_for_role("reviewer", status="done")
        _install_fake_worker(
            monkeypatch,
            resume_response="hmm\nBLOCKED: reason=unknown | weird state",
        )

        execution.send_to_unit("F-001-U-1", "reviewer", "huh?")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "done"  # terminal preserved
        # No last_error set either — status flip and error-population are
        # both gated.
        assert s.last_error == ""

        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "reviewer_blocked" in types
        assert "reviewer_manual_message" in types

    def test_active_status_flip_still_works_after_guard(self, tmp_state_db, monkeypatch):
        """Regression guard for the guard: on an `in_ci` unit (active), the
        helper must still bump to `in_ci` — the gate allows transitions
        from any active state, not just non-terminal ones."""
        _seed_unit_for_role("reviewer", status="in_ci")
        _install_fake_worker(monkeypatch, resume_response="REVIEW_RECOMMEND_MERGE: looks good")

        execution.send_to_unit("F-001-U-1", "reviewer", "carry on")

        assert state.get_unit_state("F-001-U-1").status == "in_ci"
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "reviewer_recommend_merge" in types


# --------------------------- cycle_review ---------------------------


class TestCycleReview:
    """Integration-ish tests — exercise the whole _tester_phase → _copilot_phase
    → _reviewer_phase flow with mocked spawn_tester/spawn_reviewer/address_review.
    """

    def test_happy_path_no_cycles(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        # Tester: pass immediately. Reviewer: approve immediately.
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS", "session_id": "t"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "r"}
            ),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

    def test_tester_blocked_escalates(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: "BLOCKED — tester for U: spec ambiguous",
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        assert "tester" in parsed["message"]

    def test_bug_found_fix_then_pass(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        # First tester run finds bug; address_review pushes fix; second tester passes
        tester_responses = iter(
            [
                json.dumps({"unit_id": "U", "outcome": "BUG_FOUND", "bug": "div by zero"}),
                json.dumps({"unit_id": "U", "outcome": "TESTS_PASS"}),
            ]
        )
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: next(tester_responses),
        )

        def fake_address_review(uid, src, fb):
            state.increment_review_round(uid)
            return json.dumps({"outcome": "FIX_PUSHED", "cycle": 1})

        monkeypatch.setattr(execution, "address_review", fake_address_review)
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

    def test_cap_3_hit_on_tester_bugs(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        # Tester always finds a bug; fix always pushes; review_round must
        # actually increment so the cap check trips.
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "BUG_FOUND", "bug": "x"}),
        )

        def fake_address_review(uid, src, fb):
            state.increment_review_round(uid)
            return json.dumps({"outcome": "FIX_PUSHED", "cycle": 1})

        monkeypatch.setattr(execution, "address_review", fake_address_review)
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        assert "cap" in parsed["message"].lower()

    def test_reviewer_request_changes_then_approve(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """First reviewer turn = cold spawn_reviewer; retry = delta resume.

        F-012-U-2 split the retry onto _resume_reviewer_for_delta so the
        existing reviewer session can be reused; this test exercises both
        legs and confirms cycle_review terminates approved.
        """
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_REQUEST_CHANGES", "issue": "rename x"}
            ),
        )
        monkeypatch.setattr(
            execution,
            "_resume_reviewer_for_delta",
            lambda *a, **k: json.dumps(
                {"unit_id": "U", "outcome": "REVIEW_RECOMMEND_MERGE", "reason": "fix landed"}
            ),
        )
        monkeypatch.setattr(
            execution,
            "address_review",
            lambda u, src, fb: json.dumps(
                {"outcome": "FIX_PUSHED", "cycle": 1, "summary": "renamed x to y"}
            ),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"
        steps = [s.get("step") for s in parsed["history"]]
        assert "reviewer (delta resume)" in steps, (
            "retry must go through _resume_reviewer_for_delta, not cold spawn"
        )

    def test_copilot_review_recorded(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        copilot_review = {
            "state": "COMMENTED",
            "inline_count": 2,
            "body": "found two nits",
        }
        _stub_github(monkeypatch, copilot_review=copilot_review)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        # History should include the copilot_review step
        copilot_steps = [s for s in parsed["history"] if s.get("step") == "copilot_review"]
        assert len(copilot_steps) == 1
        assert copilot_steps[0]["outcome"] == "received"
        assert copilot_steps[0]["inline_count"] == 2

    def test_copilot_review_timeout(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        _stub_github(monkeypatch, copilot_review=None)  # timeout
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        copilot_steps = [s for s in parsed["history"] if s.get("step") == "copilot_review"]
        assert copilot_steps[0]["outcome"] == "timeout"

    def test_review_recommend_merge_is_terminal_success(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {
                    "unit_id": u,
                    "outcome": "REVIEW_RECOMMEND_MERGE",
                    "reason": "self-approval blocked",
                }
            ),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

    def test_clean_run_makes_exactly_two_ci_waits(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """F-012-U-1: clean cycle hits GATE 1 (coder push) + GATE 2 (tester push) only.

        GATE 3's defensive final pre-merge re-check was removed: the reviewer
        phase's own fix-loop already gates on green. Counting `wait_for_ci`
        calls is the load-bearing assertion — a regression that re-adds the
        third call would silently re-pay the ~poll-interval-rounded wait.
        """
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        calls: list[tuple] = []

        def counting_wait(*args, **kwargs):
            calls.append((args, kwargs))
            return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

        monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", counting_wait)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"
        assert len(calls) == 2, f"expected 2 CI waits on clean run, got {len(calls)}"


# --------------------------- ultrareview gate (F-007-U-3) ---------------------------


def _enable_ultrareview(feature_id="F-001"):
    """Flip `ultrareview_enabled` on an already-seeded feature."""
    feat = state.get_feature(feature_id)
    feat.ultrareview_enabled = True
    state.save_feature(feat)


def _stub_ultrareview(monkeypatch, *, passed: bool, findings=None):
    """Patch ultrareview.trigger/wait_for_result to return a canned verdict.

    Tests stub at the orchestrator/tools/execution module's bound name so the
    real `claude ultrareview` subprocess is never spawned. Records every call
    so tests can assert on argv shape.
    """
    findings = list(findings or [])
    calls: dict[str, list] = {"trigger": [], "wait": []}

    def fake_trigger(pr_url, **kw):
        calls["trigger"].append((pr_url, kw))

    def fake_wait(pr_url, **kw):
        calls["wait"].append((pr_url, kw))
        return {"passed": passed, "findings": findings}

    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.trigger", fake_trigger)
    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.wait_for_result", fake_wait)
    return calls


class TestCycleReviewUltrareviewGate:
    """F-007-U-3 wires `/ultrareview` into `cycle_review` as the terminal pass.

    The gate runs only when ``feature.ultrareview_enabled`` is True and the
    reviewer endorsed via ``REVIEW_RECOMMEND_MERGE``. On PASS the cycle
    terminates as ``approved_awaiting_merge`` (today's behaviour); on FAIL
    this initial impl escalates (the full FAIL fix-loop ships in U-4).
    """

    def _seed_for_reviewer_recommend(self, monkeypatch):
        """Common scaffold: reviewer endorses; tester passes; gh helpers no-op."""
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

    def test_flag_off_skips_ultrareview_entirely(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """When the flag is False (default), ultrareview is not triggered at all
        — today's `approved_awaiting_merge` behaviour is preserved.
        """
        self._seed_for_reviewer_recommend(monkeypatch)
        calls = _stub_ultrareview(monkeypatch, passed=True)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"
        assert calls["trigger"] == [], "ultrareview must not fire when flag is off"
        assert calls["wait"] == []
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "ultrareview_started" not in types

    def test_flag_on_passes_terminates_approved(self, tmp_state_db, with_github_token, monkeypatch):
        """Flag on + ultrareview PASS → `approved_awaiting_merge`, plus
        ``ultrareview_started`` and ``ultrareview_passed`` events for cost +
        cycle-log attribution.
        """
        self._seed_for_reviewer_recommend(monkeypatch)
        _enable_ultrareview()
        calls = _stub_ultrareview(monkeypatch, passed=True)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

        # Subprocess was actually invoked, keyed by the PR URL.
        assert len(calls["trigger"]) == 1
        assert calls["trigger"][0][0] == "https://github.com/owner/repo/pull/5"
        assert len(calls["wait"]) == 1

        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "ultrareview_started" in types
        assert "ultrareview_passed" in types
        assert "ultrareview_failed" not in types

    def test_flag_on_fails_escalates_with_findings(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Flag on + ultrareview FAIL → escalation (initial impl; U-4 adds the
        fix-loop). ``ultrareview_failed`` event carries the findings list.
        """
        self._seed_for_reviewer_recommend(monkeypatch)
        _enable_ultrareview()
        findings = ["src/x.py:42 — leaks fd on retry", "src/y.py:7 — off-by-one"]
        calls = _stub_ultrareview(monkeypatch, passed=False, findings=findings)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        assert "ultrareview" in parsed["message"].lower()
        for f in findings:
            assert f in parsed["message"], "escalation message must surface findings"

        assert len(calls["trigger"]) == 1

        events = state.list_events("F-001-U-1")
        types = [e["event_type"] for e in events]
        assert "ultrareview_started" in types
        assert "ultrareview_failed" in types
        assert "ultrareview_passed" not in types
        failed_evt = next(e for e in events if e["event_type"] == "ultrareview_failed")
        for f in findings:
            assert f in failed_evt["details"], "findings must land in event.details"

    def test_review_comment_does_not_trigger_ultrareview(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Spec says "after reviewer emits REVIEW_RECOMMEND_MERGE". A
        ``REVIEW_COMMENT`` terminal (comment-only, not an endorsement) must NOT
        fire the gate — even when the flag is on.
        """
        _seed_coded_unit()
        _enable_ultrareview()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_COMMENT"}),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )
        calls = _stub_ultrareview(monkeypatch, passed=True)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"
        assert calls["trigger"] == [], "ultrareview only runs after REVIEW_RECOMMEND_MERGE"

    def test_ultrareview_wrapper_exception_escalates(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """If the subprocess wrapper raises (CLI missing, parse error, ...),
        cycle_review escalates rather than crashing or silently endorsing —
        same fail-closed direction as `_parse_bugs`.
        """
        self._seed_for_reviewer_recommend(monkeypatch)
        _enable_ultrareview()

        def boom(pr_url, **kw):
            raise RuntimeError("claude CLI not on PATH")

        monkeypatch.setattr("orchestrator.tools.execution.ultrareview.trigger", boom)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "ultrareview_failed" in types

    def test_missing_pr_url_records_failed_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Reviewer M1 regression: the defensive ``pr_url is None`` escape
        must record an ``ultrareview_failed`` event before returning. Without
        it the escalation ntfy push lands with no event trail explaining why
        — every False-return path in ``_ultrareview_phase`` is supposed to
        be fail-closed *with evidence*.
        """
        _seed_coded_unit()
        _enable_ultrareview()
        # Wipe the PR number to force ``_pr_url_for`` to return None.
        s = state.get_unit_state("F-001-U-1")
        s.pr_number = None
        state.upsert_unit_state(s)

        ctx = execution.CycleContext(feature_id="F-001", unit_id="F-001-U-1", history=[])
        passed, msg = execution._ultrareview_phase(ctx)

        assert passed is False
        assert "no PR URL" in msg

        events = state.list_events("F-001-U-1")
        types = [e["event_type"] for e in events]
        assert "ultrareview_failed" in types, (
            "every False-return path must leave an ultrareview_failed event "
            "trail (fail-closed with evidence)"
        )
        assert "ultrareview_started" not in types, (
            "the started event fires after the PR-URL check; a missing PR "
            "means we never reached the trigger"
        )

    def test_failed_event_details_truncates_per_finding_not_mid_string(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Reviewer M2 regression: the FAIL-path event ``details`` must keep
        whole findings up to the budget and append a `(N more findings
        truncated)` marker, not char-slice the joined blob mid-finding.
        """
        self._seed_for_reviewer_recommend(monkeypatch)
        _enable_ultrareview()
        # Each finding is 100 chars; 30 of them = 3000+ chars when joined —
        # well over the 1500-char budget, so truncation must kick in.
        findings = [f"src/file_{i:03d}.py:42 — " + "x" * 70 for i in range(30)]
        _stub_ultrareview(monkeypatch, passed=False, findings=findings)

        execution.cycle_review("F-001", "F-001-U-1")

        events = state.list_events("F-001-U-1")
        failed_evt = next(e for e in events if e["event_type"] == "ultrareview_failed")
        details = failed_evt["details"]

        # Every line in details must be either a complete finding from the
        # input or the truncation marker — no partial findings (which would
        # be the char-slice failure mode).
        complete_findings = set(findings)
        for line in details.splitlines():
            if line.startswith("..."):
                continue
            assert line in complete_findings, (
                f"truncated mid-finding: {line!r} is not a complete finding "
                "from the input list (char-slice bug regressed)"
            )

        # And the marker must appear, naming how many were dropped.
        assert "more findings truncated" in details
        assert "(no findings reported)" not in details


# --------------------------- _resume_reviewer_for_delta (F-012-U-2) ---------------------------


class TestResumeReviewerForDelta:
    """The delta-resume helper replaces the cold-start spawn_reviewer call on
    retry. It must:
      - reuse the existing reviewer_session_id (no fresh sandbox)
      - send a delta-scoped message via compose_reviewer_delta_task
      - record reviewer_resumed_for_delta + the marker event
      - return JSON in the same shape as spawn_reviewer
    """

    def _seed_reviewing_unit(self, reviewer_session_id="sesn-r-existing"):
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.status = "reviewing"
        s.reviewer_session_id = reviewer_session_id
        state.upsert_unit_state(s)
        return s

    def _build_ctx(self, prior_sha="cafe1234abcd"):
        ctx = execution.CycleContext(
            feature_id="F-001",
            unit_id="F-001-U-1",
            history=[],
            last_reviewed_sha=prior_sha,
        )
        return ctx

    def _unit_and_feature(self):
        feature = state.get_feature("F-001")
        plan = state.get_plan("F-001")
        unit = next(u for u in plan.units if u.id == "F-001-U-1")
        return unit, feature

    def test_recommend_merge_on_resume_returns_approved_json(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        self._seed_reviewing_unit()
        workers = _install_fake_worker(
            monkeypatch,
            resume_response="all addressed\nREVIEW_RECOMMEND_MERGE: prior fix landed cleanly",
        )
        _stub_github(monkeypatch)
        ctx = self._build_ctx()
        unit, feature = self._unit_and_feature()
        unit_state = state.get_unit_state("F-001-U-1")

        out = execution._resume_reviewer_for_delta(
            ctx,
            unit_state,
            feature,
            unit,
            prior_findings="rename x",
            fix_summary="renamed x to y in 3 files",
        )

        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"
        assert parsed["session_id"] == "sesn-r-existing"
        # The reviewer worker was *resumed*, not spawned
        reviewer_worker = workers["reviewer"]
        assert reviewer_worker.resume_calls, "expected worker.resume to fire"
        sid, msg = reviewer_worker.resume_calls[0]
        assert sid == "sesn-r-existing"
        assert "DELTA RE-REVIEW" in msg
        assert "cafe1234abcd" in msg  # prior_sha echoed
        assert "deadbeefcafe" in msg  # current_sha from stub
        assert "renamed x to y" in msg  # fix_summary echoed
        assert not reviewer_worker.spawn_calls, "delta path must never cold-spawn"

    def test_request_changes_on_resume(self, tmp_state_db, with_github_token, monkeypatch):
        self._seed_reviewing_unit()
        _install_fake_worker(
            monkeypatch,
            resume_response="still missing edge case\nREVIEW_REQUEST_CHANGES: empty input still crashes",
        )
        _stub_github(monkeypatch)
        ctx = self._build_ctx()
        unit, feature = self._unit_and_feature()
        unit_state = state.get_unit_state("F-001-U-1")

        out = execution._resume_reviewer_for_delta(
            ctx,
            unit_state,
            feature,
            unit,
            prior_findings="empty input",
            fix_summary="added guard",
        )

        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_REQUEST_CHANGES"
        assert parsed["issue"] == "empty input still crashes"

    def test_records_resumed_for_delta_event(self, tmp_state_db, with_github_token, monkeypatch):
        self._seed_reviewing_unit()
        _install_fake_worker(monkeypatch, resume_response="REVIEW_RECOMMEND_MERGE: ok")
        _stub_github(monkeypatch)
        ctx = self._build_ctx()
        unit, feature = self._unit_and_feature()
        unit_state = state.get_unit_state("F-001-U-1")

        execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )

        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "reviewer_resumed_for_delta" in types
        # And the marker event landed too
        assert "reviewer_recommend_merge" in types

    def test_no_session_id_errors_cleanly(self, tmp_state_db, with_github_token, monkeypatch):
        """Defensive: caller must have spawned the reviewer first.

        Programming bug if reached; helper returns an ERROR string rather
        than blowing up inside ManagedAgentWorker.resume() with an empty id.
        """
        self._seed_reviewing_unit(reviewer_session_id="")
        _stub_github(monkeypatch)
        ctx = self._build_ctx()
        unit, feature = self._unit_and_feature()
        unit_state = state.get_unit_state("F-001-U-1")

        out = execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )
        assert "ERROR" in out
        assert "no reviewer session" in out

    def test_worker_resume_exception_escalates(self, tmp_state_db, with_github_token, monkeypatch):
        self._seed_reviewing_unit()

        class BlowUp:
            def __init__(self, role):
                pass

            def resume(self, *a, **k):
                raise RuntimeError("session expired")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", BlowUp)
        _stub_github(monkeypatch)
        ctx = self._build_ctx()
        unit, feature = self._unit_and_feature()
        unit_state = state.get_unit_state("F-001-U-1")

        out = execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )

        assert "ERROR resuming reviewer" in out
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "reviewer_resume_error" in types


class TestReviewerPhasePreservesSession:
    """End-to-end: _reviewer_phase must NOT clear reviewer_session_id on retry.

    Pre-F-012 behavior cleared the id + cold-started spawn_reviewer (957s +
    999s on F-009-U-1). The new behavior keeps the session and resumes.
    """

    def test_reviewer_session_id_survives_retry(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        # Force spawn_reviewer to actually populate reviewer_session_id by
        # routing through a FakeWorker (rather than monkeypatching the whole
        # spawn_reviewer function).
        _install_fake_worker(
            monkeypatch,
            spawn_response="REVIEW_REQUEST_CHANGES: needs a fix",
            resume_response="REVIEW_RECOMMEND_MERGE: delta clean",
        )
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "address_review",
            lambda u, src, fb: json.dumps(
                {"outcome": "FIX_PUSHED", "cycle": 1, "summary": "fixed"}
            ),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        execution.cycle_review("F-001", "F-001-U-1")

        s = state.get_unit_state("F-001-U-1")
        # Session id is the SAME one set by the initial spawn; not cleared
        # between turns. (FakeWorker assigns "sesn-reviewer-0" on the first
        # spawn; if the retry had cold-spawned, it would be "sesn-reviewer-1".)
        assert s.reviewer_session_id == "sesn-reviewer-0", (
            f"retry must reuse the existing reviewer session, got {s.reviewer_session_id!r}"
        )


# --------------------------- internal helpers ---------------------------


class TestRecordStep:
    def test_records_parsed_json(self, tmp_state_db):
        ctx = execution.CycleContext(feature_id="F", unit_id="U", history=[])
        r = execution._record_step(ctx, "test", json.dumps({"outcome": "X"}))
        assert r == {"outcome": "X"}
        assert ctx.history[-1]["step"] == "test"

    def test_records_raw_when_non_json(self, tmp_state_db):
        ctx = execution.CycleContext(feature_id="F", unit_id="U", history=[])
        r = execution._record_step(ctx, "test", "not json output")
        assert r["outcome"] == "RAW"
        assert r["raw"] == "not json output"


class TestPrUrlFor:
    def test_returns_none_without_pr(self, tmp_state_db):
        assert execution._pr_url_for("F", None) is None

    def test_returns_none_without_state(self, tmp_state_db):
        from orchestrator.models import WorkUnitState as W

        s = W(unit_id="U", feature_id="F", status="coding")  # no pr_number
        assert execution._pr_url_for("F", s) is None

    def test_reconstructs_url(self, tmp_state_db):
        state.save_feature(
            Feature(id="F", title="t", description="d", repo_path="https://github.com/joe/repo"),
        )
        from orchestrator.models import WorkUnitState as W

        s = W(unit_id="U", feature_id="F", status="in_ci", pr_number=42)
        url = execution._pr_url_for("F", s)
        assert url == "https://github.com/joe/repo/pull/42"

    def test_bad_repo_url_returns_none(self, tmp_state_db):
        state.save_feature(
            Feature(id="F", title="t", description="d", repo_path="not-a-url"),
        )
        from orchestrator.models import WorkUnitState as W

        s = W(unit_id="U", feature_id="F", status="in_ci", pr_number=42)
        assert execution._pr_url_for("F", s) is None


# --------------------------- verification gate ---------------------------


class TestVerificationGate:
    """Every spawn surface must refuse to act on an unverified target repo.

    The conftest `tmp_state_db` fixture pre-seeds `https://github.com/o/r`
    as verified. We use a *different* URL here so the gate triggers.
    """

    UNVERIFIED = "https://github.com/never/verified"

    def _seed_unverified(self, feature_id="F-001"):
        state.save_feature(
            Feature(
                id=feature_id,
                title="t",
                description="d",
                repo_path=self.UNVERIFIED,
                status="approved",
            )
        )
        state.save_plan(
            feature_id,
            [WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u", description="d")],
        )
        state.approve_plan(feature_id)

    def test_spawn_unit_blocks(self, tmp_state_db, with_github_token):
        self._seed_unverified()
        msg = execution.spawn_unit("F-001", "F-001-U-1")
        assert "ERROR" in msg
        assert "not verified" in msg
        assert "verify_repo" in msg
        # The unit must not have been created
        assert state.get_unit_state("F-001-U-1") is None

    def test_spawn_tester_blocks(self, tmp_state_db, with_github_token):
        self._seed_unverified()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                branch="b",
                pr_number=1,
                coder_session_id="sesn-c",
            )
        )
        msg = execution.spawn_tester("F-001", "F-001-U-1")
        assert "ERROR" in msg and "not verified" in msg

    def test_spawn_reviewer_blocks(self, tmp_state_db, with_github_token):
        self._seed_unverified()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                branch="b",
                pr_number=1,
                coder_session_id="sesn-c",
            )
        )
        msg = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "ERROR" in msg and "not verified" in msg

    def test_address_review_blocks(self, tmp_state_db, with_github_token):
        self._seed_unverified()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="reviewing",
                branch="b",
                pr_number=1,
                coder_session_id="sesn-c",
            )
        )
        msg = execution.address_review("F-001-U-1", "tester", "fix the bug")
        assert "ERROR" in msg and "not verified" in msg

    def test_cycle_review_blocks(self, tmp_state_db, with_github_token):
        self._seed_unverified()
        msg = execution.cycle_review("F-001", "F-001-U-1")
        assert "ERROR" in msg and "not verified" in msg

    def test_send_to_unit_blocks(self, tmp_state_db, with_github_token):
        self._seed_unverified()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="coding",
                branch="b",
                coder_session_id="sesn-c",
            )
        )
        msg = execution.send_to_unit("F-001-U-1", "coder", "do a thing")
        assert "ERROR" in msg and "not verified" in msg

    def test_stale_verification_blocks(self, tmp_state_db, with_github_token):
        """A verified row older than TTL should be treated as unverified."""
        from datetime import UTC, datetime, timedelta

        from orchestrator.models import CheckResult, VerificationResult

        state.save_verified_repo(
            VerificationResult(
                repo_url=self.UNVERIFIED,
                default_branch="main",
                auth_mode="pat",
                auth_identity="u:t",
                checks=[
                    CheckResult("read access", True),
                    CheckResult("write access", True),
                    CheckResult("branch protection exists", True),
                    CheckResult(
                        "≥1 approving review required",
                        True,
                        "required_approving_review_count = 1",
                    ),
                    CheckResult("force push blocked", True),
                    CheckResult("deletion blocked", True),
                    CheckResult("admin bypass blocked", True),
                ],
            )
        )
        # Backdate the row to past the TTL
        import sqlite3

        old = (datetime.now(UTC) - timedelta(hours=state.VERIFY_TTL_HOURS + 1)).isoformat()
        with sqlite3.connect(tmp_state_db) as conn:
            conn.execute(
                "UPDATE verified_repos SET verified_at = ? WHERE repo_url = ?",
                (old, self.UNVERIFIED),
            )

        self._seed_unverified()
        msg = execution.spawn_unit("F-001", "F-001-U-1")
        assert "ERROR" in msg
        assert "not verified" in msg or "expired" in msg.lower()


# --------------------------- CI gate ---------------------------


def _set_ci(monkeypatch, status: str, **kw):
    """Override the autouse `_ci_green` fixture with a different status.

    Use inside a test to drive the CI gate into the failed/timeout/no_ci
    branches.
    """

    def fake(*args, **kwargs):
        return CIWaitResult(
            status=status,
            elapsed_seconds=kw.get("elapsed", 1.0),
            **{k: v for k, v in kw.items() if k != "elapsed"},
        )

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake)


class TestCIGateStandalone:
    """`spawn_tester` and `spawn_reviewer` refuse to spawn when CI is red.

    The standalone gate is no-fix-loop — it just returns an ERROR so the
    lead surfaces it. `cycle_review` is where the automated fix loop lives.
    """

    def _seed_unit_with_pr(self, feature_id="F", unit_id="U"):
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
            [WorkUnit(id=unit_id, feature_id=feature_id, title="u", description="d")],
        )
        state.approve_plan(feature_id)
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=unit_id,
                feature_id=feature_id,
                status="in_ci",
                branch="b",
                pr_number=42,
                coder_session_id="sesn-c",
            )
        )

    def test_spawn_tester_refuses_on_red_ci(self, tmp_state_db, with_github_token, monkeypatch):
        self._seed_unit_with_pr()
        _set_ci(
            monkeypatch,
            status="failed",
            total_checks=2,
            failing_runs=[{"name": "tests", "details_url": "https://x"}],
        )

        msg = execution.spawn_tester("F", "U")
        assert "ERROR" in msg
        assert "refusing to spawn tester" in msg.lower() or "ci is failing" in msg.lower()
        assert "tests" in msg  # failing check name surfaced

    def test_spawn_reviewer_refuses_on_red_ci(self, tmp_state_db, with_github_token, monkeypatch):
        self._seed_unit_with_pr()
        _set_ci(
            monkeypatch,
            status="failed",
            total_checks=1,
            failing_runs=[{"name": "lint", "details_url": "https://x"}],
        )

        msg = execution.spawn_reviewer("F", "U")
        assert "ERROR" in msg
        assert "lint" in msg

    def test_spawn_tester_refuses_on_ci_timeout(self, tmp_state_db, with_github_token, monkeypatch):
        self._seed_unit_with_pr()
        _set_ci(monkeypatch, status="timeout", elapsed=600.0, total_checks=3)

        msg = execution.spawn_tester("F", "U")
        assert "ERROR" in msg
        assert "did not settle" in msg or "timeout" in msg.lower()

    def test_no_ci_configured_lets_spawn_proceed(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Sandbox repos without GH Actions still work — `no_ci` is a pass-through."""
        self._seed_unit_with_pr()
        _set_ci(monkeypatch, status="no_ci", elapsed=30.0)
        _install_fake_worker(monkeypatch, spawn_response="TESTS_PASS")
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F", "U")
        # No ERROR — proceeds to actual spawn
        assert "ERROR" not in out
        parsed = json.loads(out)
        assert parsed["outcome"] == "TESTS_PASS"


class TestCycleReviewCIGate:
    """`cycle_review` waits for CI between phases AND runs an embedded
    fix loop when CI is red (counts toward CAP_3).
    """

    def _seed_full_unit(self, feature_id="F-001", unit_id="F-001-U-1"):
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
            [WorkUnit(id=unit_id, feature_id=feature_id, title="u", description="d")],
        )
        state.approve_plan(feature_id)
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=unit_id,
                feature_id=feature_id,
                status="in_ci",
                branch="b",
                pr_number=42,
                coder_session_id="sesn-c",
            )
        )

    def test_ci_red_at_gate1_triggers_fix_loop_then_recovers(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Coder PR push is red → address_review(ci) → push fix → CI green → continue."""
        self._seed_full_unit()

        # Per-role responses: each role gets its terminal marker
        def factory(role: str):
            spawn_response = {
                "coder": "PR_URL: https://github.com/o/r/pull/42",
                "tester": "TESTS_PASS",
                "reviewer": "REVIEW_RECOMMEND_MERGE: clean",
            }.get(role, "")
            return FakeWorker(role, spawn_response, "FIX_PUSHED")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
        _stub_github(monkeypatch)

        # Drive CI: first call (gate 1, pre-tester) = failed, then green for
        # all subsequent calls (post-fix wait, gate 2 after tester).
        results = iter(
            [
                CIWaitResult(
                    status="failed",
                    elapsed_seconds=20.0,
                    total_checks=2,
                    failing_runs=[{"name": "tests", "details_url": "https://x"}],
                ),
            ]
        )

        def driver(*args, **kwargs):
            try:
                return next(results)
            except StopIteration:
                return CIWaitResult(status="green", elapsed_seconds=10.0, total_checks=2)

        monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", driver)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"
        # History should include the CI fix step
        steps = [h.get("step", "") for h in parsed["history"]]
        assert any("ci" in s.lower() for s in steps)

    def test_ci_timeout_escalates_cycle_review(self, tmp_state_db, with_github_token, monkeypatch):
        self._seed_full_unit()
        _install_fake_worker(monkeypatch, spawn_response="ignored")
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )
        _set_ci(monkeypatch, status="timeout", elapsed=600.0, total_checks=2)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        assert "ci timeout" in parsed["message"].lower() or "timeout" in parsed["message"].lower()

    def test_ci_fix_failures_count_toward_cap_3(self, tmp_state_db, with_github_token, monkeypatch):
        """Three CI fixes in a row exhaust CAP_3 → escalate (cap-hit)."""
        self._seed_full_unit()
        _install_fake_worker(
            monkeypatch,
            spawn_response="ignored",
            resume_response="FIX_PUSHED",
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        # CI is ALWAYS failing — each fix push triggers a new CI wait that fails
        # again. After CAP_3 attempts, the helper bails.
        _set_ci(
            monkeypatch,
            status="failed",
            total_checks=1,
            failing_runs=[{"name": "tests", "details_url": "https://x"}],
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        assert "cap of 3" in parsed["message"].lower() or "ci" in parsed["message"].lower()

    def test_no_ci_configured_passes_through_cycle(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Sandbox repos with no GH Actions still complete a full cycle."""
        self._seed_full_unit()
        _install_fake_worker(
            monkeypatch,
            spawn_response="TESTS_PASS",
            resume_response="REVIEW_RECOMMEND_MERGE: clean",
        )
        _stub_github(monkeypatch)

        # spawn_tester returns TESTS_PASS; spawn_reviewer returns REVIEW_RECOMMEND_MERGE.
        # The FakeWorker's spawn() returns the canned response for EVERY spawn
        # though — so reviewer would also return TESTS_PASS. Override per-role.
        def two_role_factory(role: str):
            spawn_response = {
                "coder": "PR_URL: https://github.com/o/r/pull/42",
                "tester": "TESTS_PASS",
                "reviewer": "REVIEW_RECOMMEND_MERGE: clean",
            }.get(role, "")
            return FakeWorker(role, spawn_response, "FIX_PUSHED")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", two_role_factory)
        _set_ci(monkeypatch, status="no_ci", elapsed=30.0)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"


# --------------------------- structured BLOCKED end-to-end ---------------------------


class TestStructuredBlockedPayload:
    """End-to-end coverage for F-005-U-1: a coder/tester/reviewer emits a
    BLOCKED marker → the orchestrator parses it (structured tag OR
    recogniser fallback OR unknown), stores the reason slug + structured
    fields on the unit_event, and surfaces the reason code in last_error.

    Three required scenarios:
      (a) Prompt-emitted ``reason=`` end-to-end.
      (b) Free-form prose classified by a built-in recogniser.
      (c) Unrecognised prose falls back to ``reason='unknown'`` without
          losing the worker's own message.
    """

    def _last_blocked_event_details(self, unit_id: str) -> dict:
        """Return the JSON-decoded details payload for the most recent
        blocked event on ``unit_id`` (covers all four event_types:
        ``coder_blocked``, ``tester_blocked``, ``reviewer_blocked``,
        ``coder_blocked_on_fix``)."""
        events = state.list_events(unit_id)
        blocked = [e for e in events if "blocked" in e["event_type"]]
        assert blocked, f"no blocked event recorded for {unit_id}"
        return json.loads(blocked[-1]["details"])

    # ----- (a) structured reason= tag, coder -----

    def test_coder_structured_branch_protection_reason(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _setup_feature()
        response = (
            "tried to push, got 403 from github.\n"
            "BLOCKED: reason=branch_protection_blocked_push "
            "branch=feat/F-001-foo-u-1 "
            "rule_type=required_pull_request_reviews "
            "api_used=git_push "
            "| push rejected; ask user to scope rule to main only"
        )
        _install_fake_worker(monkeypatch, spawn_response=response)
        _stub_github(monkeypatch)

        pushed = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda unit_id, reason, **k: pushed.append((unit_id, reason)) or True,
        )

        out = execution.spawn_unit("F-001", "F-001-U-1")

        # Returned string carries both slug + prose for the lead
        assert "branch_protection_blocked_push" in out
        assert "scope rule to main only" in out

        # State: escalated + last_error includes the slug
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        assert "[branch_protection_blocked_push]" in s.last_error
        assert "scope rule to main only" in s.last_error

        # Event payload: structured fields preserved
        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["fields"] == {
            "branch": "feat/F-001-foo-u-1",
            "rule_type": "required_pull_request_reviews",
            "api_used": "git_push",
        }
        assert "scope rule to main only" in details["prose"]
        assert "recognized_by" not in details  # prompt-emitted, not pattern-matched

        # ntfy push surfaces the slug to the human's phone
        assert pushed
        _, reason_pushed = pushed[0]
        assert "branch_protection_blocked_push" in reason_pushed

    # ----- (a) structured reason= tag, tester -----

    def test_tester_structured_auth_failure_reason(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()
        response = (
            "gh pr view returned 401\n"
            "BLOCKED: reason=auth_failure | token rejected by GitHub, ask user for fresh PAT"
        )
        _install_fake_worker(monkeypatch, spawn_response=response)
        _stub_github(monkeypatch)

        msg = execution.spawn_tester("F-001", "F-001-U-1")
        assert "auth_failure" in msg
        assert "token rejected" in msg

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        assert "[auth_failure]" in s.last_error

        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "auth_failure"
        assert details["prose"] == "token rejected by GitHub, ask user for fresh PAT"
        assert details.get("fields", {}) == {}

    # ----- (a) structured reason= tag, reviewer -----

    def test_reviewer_structured_rate_limited_reason(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()
        response = (
            "gh api hammered until 429\n"
            "BLOCKED: reason=rate_limited host=api.github.com | secondary rate limit; "
            "retry after backoff"
        )
        _install_fake_worker(monkeypatch, spawn_response=response)
        _stub_github(monkeypatch)

        msg = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert "rate_limited" in msg

        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "rate_limited"
        assert details["fields"] == {"host": "api.github.com"}
        assert "secondary rate limit" in details["prose"]

    # ----- (a) structured reason= tag, address_review (coder resume) -----

    def test_address_review_structured_merge_conflict_reason(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()
        response = (
            "rebase conflicted on three files I can't merge mechanically\n"
            "BLOCKED: reason=merge_conflict_unresolved | conflicts in "
            "coder.md/tester.md need human guidance"
        )
        _install_fake_worker(monkeypatch, resume_response=response)
        _stub_github(monkeypatch)

        msg = execution.address_review("F-001-U-1", "tester", "fix this bug")
        assert "merge_conflict_unresolved" in msg

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        assert "[merge_conflict_unresolved]" in s.last_error

        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "merge_conflict_unresolved"
        assert "conflicts" in details["prose"]

    # ----- (b) recogniser classifies free-form output -----

    def test_recognizer_classifies_legacy_branch_protection_prose(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """A worker that pre-dates F-005-U-1 emits bare prose; the
        orchestrator's recogniser still tags the event with the
        branch_protection_blocked_push slug so the dashboard / push
        body can call out the failure mode."""
        _setup_feature()
        response = (
            "ran git push origin feat/...\n"
            "remote: error: GH013: Changes must be made through a pull request.\n"
            "BLOCKED: remote rejected push: Changes must be made through a pull request"
        )
        _install_fake_worker(monkeypatch, spawn_response=response)
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        execution.spawn_unit("F-001", "F-001-U-1")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        # The recogniser tags this as branch_protection_blocked_push
        assert "[branch_protection_blocked_push]" in s.last_error
        # Prose preserved
        assert "Changes must be made through a pull request" in s.last_error

        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["recognized_by"] == "branch_protection_pr_required"
        assert details.get("fields", {}) == {}

    def test_recognizer_required_pull_request_reviews_token(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()
        response = (
            "BLOCKED: GitHub API rejected the push citing "
            "required_pull_request_reviews on the feature branch"
        )
        _install_fake_worker(monkeypatch, spawn_response=response)
        _stub_github(monkeypatch)

        execution.spawn_tester("F-001", "F-001-U-1")

        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["recognized_by"] == "branch_protection_required_reviews"

    def test_recognizer_enforce_admins_token(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        response = (
            "BLOCKED: cannot push - branch protection set with "
            "enforce_admins=true blocks even admins from pushing direct"
        )
        _install_fake_worker(monkeypatch, spawn_response=response)
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-001", "F-001-U-1")

        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["recognized_by"] == "branch_protection_enforce_admins"

    # ----- (c) unknown fallback preserves prose -----

    def test_unknown_fallback_preserves_prose(self, tmp_state_db, with_github_token, monkeypatch):
        """No structured tag, no recogniser match -> reason='unknown' but
        the worker's prose is still surfaced to the human verbatim."""
        _setup_feature()
        response = "BLOCKED: spec is too vague; cannot decide what to build"
        _install_fake_worker(monkeypatch, spawn_response=response)
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        out = execution.spawn_unit("F-001", "F-001-U-1")

        # Reason code surfaced as 'unknown'
        assert "unknown" in out
        # Prose preserved verbatim -- the whole reason a human reads this
        assert "spec is too vague" in out

        s = state.get_unit_state("F-001-U-1")
        assert "[unknown]" in s.last_error
        assert "spec is too vague" in s.last_error

        details = self._last_blocked_event_details("F-001-U-1")
        assert details["reason"] == "unknown"
        assert "spec is too vague" in details["prose"]
        # No recogniser fired -> key omitted
        assert "recognized_by" not in details

    def test_unknown_fallback_in_address_review(self, tmp_state_db, monkeypatch):
        _seed_coded_unit()
        response = "BLOCKED: the failure mode is unprecedented"
        _install_fake_worker(monkeypatch, resume_response=response)
        _stub_github(monkeypatch)

        msg = execution.address_review("F-001-U-1", "tester", "fix")
        assert "unknown" in msg
        assert "unprecedented" in msg

        s = state.get_unit_state("F-001-U-1")
        assert "[unknown]" in s.last_error


# --------------------------- F-013-U-1: _resume_or_spawn_tester ---------------------------


class TestResumeOrSpawnTester:
    """F-013-U-1: helper that detects an orphaned tester session on the unit
    and resumes it (re-emitting the verdict marker) instead of letting the
    initial spawn_tester call inside `_tester_phase` error out with
    "session already exists" — which cycle_review previously surfaced as
    an unhelpful "tester ended with unexpected outcome: RAW" escalation.
    """

    def test_delegates_to_spawn_tester_when_no_session(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """With `tester_session_id` empty, the helper just calls spawn_tester
        and returns its raw response unchanged."""
        _seed_coded_unit()  # no tester_session_id set
        calls: list[tuple[str, str]] = []

        def fake_spawn_tester(fid: str, uid: str) -> str:
            calls.append((fid, uid))
            return json.dumps({"unit_id": uid, "outcome": "TESTS_PASS", "session_id": "fresh"})

        monkeypatch.setattr(execution, "spawn_tester", fake_spawn_tester)

        out = execution._resume_or_spawn_tester("F-001", "F-001-U-1")
        assert calls == [("F-001", "F-001-U-1")]
        parsed = json.loads(out)
        assert parsed["outcome"] == "TESTS_PASS"
        assert parsed["session_id"] == "fresh"

    def test_resumes_session_and_records_tests_pass(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """With `tester_session_id` set, the helper resumes the worker and
        returns a JSON outcome of TESTS_PASS that `_record_step` parses the
        same way as a fresh `spawn_tester` output. Also records a
        `tests_pass` event for the audit trail and a `tester_resume`
        breadcrumb so the recovery branch is visible to operators."""
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.tester_session_id = "stale-tester-sid"
        state.upsert_unit_state(s)

        instances = _install_fake_worker(monkeypatch, resume_response="TESTS_PASS")
        _stub_github(monkeypatch)

        # Must NOT delegate to spawn_tester — fail loudly if it does
        def boom(*a, **k):
            raise AssertionError("should resume, not spawn")

        monkeypatch.setattr(execution, "spawn_tester", boom)

        out = execution._resume_or_spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "TESTS_PASS"
        assert parsed["session_id"] == "stale-tester-sid"

        # Worker was resumed with the stale session id and the recovery prompt
        tester_worker = instances["tester"]
        assert len(tester_worker.resume_calls) == 1
        sid, msg = tester_worker.resume_calls[0]
        assert sid == "stale-tester-sid"
        assert "re-emit" in msg and "TESTS_PASS" in msg

        # Audit trail: tester_resume breadcrumb fires BEFORE the marker event
        # (chronological replay order). The tester_resume row is what tells
        # an operator the recovery branch was chosen over a fresh spawn —
        # without it there's no breadcrumb for "why is there a tests_pass
        # against the orphan session and no spawn_tester event".
        events = state.list_events("F-001-U-1")
        types = [e["event_type"] for e in events]
        assert "tester_resume" in types
        assert "tests_pass" in types
        assert types.index("tester_resume") < types.index("tests_pass")

    def test_resume_tests_pass_fires_dismiss_and_comment_side_effects(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The TESTS_PASS branch must mirror `spawn_tester:317-327`'s PR-side
        actions on a resume hit too: dismiss any prior tester REQUEST_CHANGES
        review (so branch protection's "Require resolution of changes requested"
        doesn't block the eventual merge) and post the operator-visible
        same-user COMMENT breadcrumb.

        The orphan tester is typically the *retry* tester from a cycle N-1
        BUG_FOUND → fix-loop, so the prior REQUEST_CHANGES is real. Without
        the dismissal, `cycle_review` can drive the PR to
        `approved_awaiting_merge` while a stale tester review still blocks
        the merge — defeating the point of F-013.
        """
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.tester_session_id = "stale-tester-sid"
        state.upsert_unit_state(s)

        _install_fake_worker(monkeypatch, resume_response="TESTS_PASS")

        dismiss_calls: list[tuple] = []
        review_calls: list[tuple] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.safe_dismiss_own_change_requests",
            lambda *a, **k: dismiss_calls.append((a, k)) or 0,
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.safe_submit_pr_review",
            lambda *a, **k: review_calls.append((a, k)) or "",
        )
        monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
        monkeypatch.setattr(
            "orchestrator.tools.execution.github.parse_repo_url",
            lambda url: ("owner", "repo"),
        )

        execution._resume_or_spawn_tester("F-001", "F-001-U-1")

        # Both side-effect counters fire exactly once on the resume path
        # (matched 1:1 with the spawn path — see TestSpawnTester.test_tests_pass).
        assert len(dismiss_calls) == 1, (
            f"resume TESTS_PASS must dismiss prior REQUEST_CHANGES (M5); "
            f"got {len(dismiss_calls)} calls"
        )
        assert len(review_calls) == 1, (
            f"resume TESTS_PASS must post same-user COMMENT breadcrumb (M5); "
            f"got {len(review_calls)} calls"
        )

        # Dismissal targets the unit's actual PR with the expected reason text
        args, _ = dismiss_calls[0]
        assert args[0] == "https://github.com/o/r"
        assert args[1] == 5  # pr_number from _seed_coded_unit
        assert "Tests pass on retry" in args[2]

        # COMMENT review carries the session-id breadcrumb and event=COMMENT
        args, kwargs = review_calls[0]
        assert args[0] == "https://github.com/o/r"
        assert args[1] == 5
        assert "stale-tester-sid" in args[2]
        assert kwargs.get("event") == "COMMENT"

    def test_resume_blocked_returns_json_outcome_not_raw(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """A BLOCKED resume must return a JSON outcome (not the bare
        `BLOCKED — ...` string that `spawn_tester` returns). `_record_step`
        falls back to `outcome="RAW"` on non-JSON, and `_tester_phase`'s
        `outcome.startswith("BLOCKED")` short-circuit checks the parsed
        outcome — not the raw blob — so a bare string would re-surface
        the very "unexpected outcome: RAW" escalation this PR exists to
        eliminate (M1).
        """
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.tester_session_id = "stale-tester-sid"
        state.upsert_unit_state(s)

        _install_fake_worker(
            monkeypatch,
            resume_response="tried again\nBLOCKED: reason=ci_tool_missing tool=pytest | gone",
        )

        comment_calls: list[tuple] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.safe_comment_pr",
            lambda *a, **k: comment_calls.append((a, k)) or "",
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.github.parse_repo_url",
            lambda url: ("owner", "repo"),
        )

        out = execution._resume_or_spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)  # Must be valid JSON, not bare prose
        assert parsed["outcome"] == "BLOCKED"
        assert parsed["reason"] == "ci_tool_missing"
        assert "gone" in parsed["prose"]
        assert parsed["session_id"] == "stale-tester-sid"

        # PR comment fires on the resume BLOCKED path too (mirrors spawn_tester:356-360)
        assert len(comment_calls) == 1
        args, _ = comment_calls[0]
        assert args[1] == 5  # pr_number
        assert "Tester BLOCKED" in args[2]
        assert "ci_tool_missing" in args[2]

        # _record_step + _tester_phase parse this as outcome="BLOCKED",
        # which `startswith("BLOCKED")` correctly catches — closing the M1 gap.
        ctx = execution.CycleContext(feature_id="F-001", unit_id="F-001-U-1", history=[])
        parsed_step = execution._record_step(ctx, "tester", out)
        assert parsed_step["outcome"] == "BLOCKED"
        assert parsed_step["outcome"] != "RAW"

    def test_resume_flips_status_to_testing_during_resume(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """While the resume worker is running (minutes on the real wire),
        the unit must show `testing` — not the prior `in_ci` from
        cycle_review's GATE 1 — so dashboards/`unit_summary` aren't
        misleading. Captured via a hook in the resume call (M4).
        """
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.tester_session_id = "stale-tester-sid"
        # Start in in_ci as cycle_review's GATE 1 would leave it
        s.status = "in_ci"
        state.upsert_unit_state(s)

        observed_during_resume: list[str] = []

        class StatusObservingWorker:
            def __init__(self, role: str) -> None:
                self.role = role

            def resume(self, sid: str, msg: str) -> str:
                cur = state.get_unit_state("F-001-U-1")
                observed_during_resume.append(cur.status if cur else "<none>")
                return "TESTS_PASS"

            def spawn(self, *a, **k):  # pragma: no cover — not used on resume path
                raise AssertionError("must resume, not spawn")

        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker", StatusObservingWorker
        )
        _stub_github(monkeypatch)

        execution._resume_or_spawn_tester("F-001", "F-001-U-1")

        assert observed_during_resume == ["testing"], (
            f"unit must be in `testing` during resume (M4); observed {observed_during_resume}"
        )

    def test_resume_phase_blocked_surfaces_tester_blocked_not_raw(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """End-to-end `_tester_phase` assertion for M1/M3: a BLOCKED resume
        must surface `(False, "tester blocked")` from `_tester_phase`, not
        `(False, "tester ended with unexpected outcome: RAW")`. This is the
        observable behaviour M3 asked for — the explicit phase-level test
        that the looser tests in the broader matrix were silently allowing
        to regress."""
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.tester_session_id = "stale-tester-sid"
        state.upsert_unit_state(s)

        _install_fake_worker(
            monkeypatch,
            resume_response="BLOCKED: reason=auth_failure | gh token expired",
        )
        _stub_github(monkeypatch)

        ctx = execution.CycleContext(feature_id="F-001", unit_id="F-001-U-1", history=[])
        passed, msg = execution._tester_phase(ctx)

        assert passed is False
        assert msg == "tester blocked", (
            f"_tester_phase must surface 'tester blocked' on a BLOCKED resume, "
            f"not the misleading 'unexpected outcome: RAW' fallback; got msg={msg!r}"
        )


class TestCycleReviewRecoversOrphanedTesterSession:
    """Regression for the F-013 root-cause bug: `cycle_review` re-entering a
    unit whose previous `_tester_phase` died on a network timeout (leaving a
    non-empty `tester_session_id` but no terminal marker recorded) used to
    escalate with `outcome=escalated` / `message="tester ended with
    unexpected outcome: RAW"` because the FIRST `spawn_tester` call hit
    "tester session already exists" and `_record_step` parsed the error
    string as RAW.

    With `_resume_or_spawn_tester` wired into `_tester_phase`, the orphaned
    session is resumed (re-emits its verdict) and the cycle proceeds normally.
    """

    def test_does_not_escalate_with_raw_outcome(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        # Mimic the post-timeout state: tester session id persisted, CI green
        # (the autouse `_ci_green` fixture handles that), no terminal marker.
        s = state.get_unit_state("F-001-U-1")
        s.tester_session_id = "stale-tester-sid"
        state.upsert_unit_state(s)

        # Per-role workers: tester resume re-emits TESTS_PASS; reviewer spawns
        # fresh and recommends merge so cycle_review can reach the happy path.
        def factory(role: str) -> FakeWorker:
            spawn = {
                "tester": "TESTS_PASS",
                "reviewer": "REVIEW_RECOMMEND_MERGE: clean",
            }.get(role, "")
            resume = "TESTS_PASS" if role == "tester" else ""
            return FakeWorker(role, spawn, resume)

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        # The original failure mode produced both of these — neither should land
        assert parsed["outcome"] != "escalated"
        assert "unexpected outcome: RAW" not in parsed["message"]
        # Positive assertion: cycle reached terminal success
        assert parsed["outcome"] == "approved_awaiting_merge"


# --------------------------- F-013-U-2: _resume_or_spawn_reviewer ---------------------------


class TestResumeOrSpawnReviewer:
    """F-013-U-2: helper symmetric to ``_resume_or_spawn_tester`` (U-1) that
    detects an orphaned reviewer session on the unit and resumes it instead
    of letting the initial ``spawn_reviewer`` call inside ``_reviewer_phase``
    error out with "reviewer session already exists" — which cycle_review
    previously surfaced as a misleading "unexpected outcome: RAW" escalation.
    """

    def test_delegates_to_spawn_reviewer_when_no_session(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """With ``reviewer_session_id`` empty, the helper just calls
        ``spawn_reviewer`` and returns its raw response unchanged."""
        _seed_coded_unit()  # no reviewer_session_id set
        calls: list[tuple[str, str]] = []

        def fake_spawn_reviewer(fid: str, uid: str) -> str:
            calls.append((fid, uid))
            return json.dumps(
                {"unit_id": uid, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "fresh"}
            )

        monkeypatch.setattr(execution, "spawn_reviewer", fake_spawn_reviewer)

        out = execution._resume_or_spawn_reviewer("F-001", "F-001-U-1")
        assert calls == [("F-001", "F-001-U-1")]
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"
        assert parsed["session_id"] == "fresh"

    def test_resumes_session_and_records_recommend_merge(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """With ``reviewer_session_id`` set, the helper resumes the worker
        and returns a JSON outcome of REVIEW_RECOMMEND_MERGE that
        ``_record_step`` parses the same way as a fresh ``spawn_reviewer``
        output. Also records a ``reviewer_recommend_merge`` event for the
        audit trail and a ``reviewer_resume`` breadcrumb so the recovery
        branch is visible to operators (mirrors U-1's ``tester_resume`` /
        ``tests_pass`` chronology assertion)."""
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.reviewer_session_id = "stale-reviewer-sid"
        state.upsert_unit_state(s)

        instances = _install_fake_worker(
            monkeypatch,
            resume_response="endorsed\nREVIEW_RECOMMEND_MERGE: tests cover everything",
        )
        _stub_github(monkeypatch)

        # Must NOT delegate to spawn_reviewer — fail loudly if it does
        def boom(*a, **k):
            raise AssertionError("should resume, not spawn")

        monkeypatch.setattr(execution, "spawn_reviewer", boom)

        out = execution._resume_or_spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"
        assert parsed["session_id"] == "stale-reviewer-sid"
        assert "tests cover everything" in parsed["reason"]

        # Worker was resumed with the stale session id and the recovery prompt
        reviewer_worker = instances["reviewer"]
        assert len(reviewer_worker.resume_calls) == 1
        sid, msg = reviewer_worker.resume_calls[0]
        assert sid == "stale-reviewer-sid"
        # Recovery prompt must list every reviewer marker so the resumed
        # agent knows the marker vocabulary it's expected to re-emit.
        assert "re-emit" in msg
        for marker_name in (
            "REVIEW_RECOMMEND_MERGE",
            "REVIEW_REQUEST_CHANGES",
            "REVIEW_COMMENT",
            "BLOCKED",
        ):
            assert marker_name in msg, f"recovery prompt must list {marker_name}"

        # Audit trail: reviewer_resume breadcrumb fires BEFORE the marker event
        # (chronological replay order). Without it there's no breadcrumb
        # for "why is there a reviewer_recommend_merge against the orphan
        # session and no spawn_reviewer event".
        events = state.list_events("F-001-U-1")
        types = [e["event_type"] for e in events]
        assert "reviewer_resume" in types
        assert "reviewer_recommend_merge" in types
        assert types.index("reviewer_resume") < types.index("reviewer_recommend_merge")

    def test_resume_blocked_returns_json_outcome_not_raw(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """A BLOCKED resume must return a JSON outcome (not the bare
        ``"BLOCKED — …"`` string that ``spawn_reviewer`` returns).
        ``_record_step`` falls back to ``outcome="RAW"`` on non-JSON, and
        ``_reviewer_phase``'s ``outcome.startswith("BLOCKED")``
        short-circuit checks the parsed outcome — not the raw blob — so a
        bare string would re-surface the very "unexpected outcome: RAW"
        escalation this unit exists to eliminate.
        """
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.reviewer_session_id = "stale-reviewer-sid"
        state.upsert_unit_state(s)

        _install_fake_worker(
            monkeypatch,
            resume_response="tried again\nBLOCKED: reason=auth_failure | gh token expired",
        )

        comment_calls: list[tuple] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.safe_comment_pr",
            lambda *a, **k: comment_calls.append((a, k)) or "",
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.github.parse_repo_url",
            lambda url: ("owner", "repo"),
        )

        out = execution._resume_or_spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)  # Must be valid JSON, not bare prose
        assert parsed["outcome"] == "BLOCKED"
        assert parsed["reason"] == "auth_failure"
        assert "gh token expired" in parsed["prose"]
        assert parsed["session_id"] == "stale-reviewer-sid"

        # PR comment fires on the resume BLOCKED path too (mirrors the
        # spawn_reviewer BLOCKED breadcrumb in _format_reviewer_marker_response).
        assert len(comment_calls) == 1
        args, _ = comment_calls[0]
        assert args[1] == 5  # pr_number
        assert "Reviewer BLOCKED" in args[2]
        assert "auth_failure" in args[2]

        # _record_step + _reviewer_phase parse this as outcome="BLOCKED",
        # which `startswith("BLOCKED")` correctly catches.
        ctx = execution.CycleContext(feature_id="F-001", unit_id="F-001-U-1", history=[])
        parsed_step = execution._record_step(ctx, "reviewer", out)
        assert parsed_step["outcome"] == "BLOCKED"
        assert parsed_step["outcome"] != "RAW"


class TestCycleReviewRecoversOrphanedReviewerSession:
    """Regression for the reviewer half of the F-013 root-cause bug:
    ``cycle_review`` re-entering a unit whose previous ``_reviewer_phase``
    died on a network timeout (leaving a non-empty ``reviewer_session_id``
    but no terminal marker recorded) used to escalate with
    ``outcome=escalated`` / ``message="reviewer ended with unexpected
    outcome: RAW"`` because the FIRST ``spawn_reviewer`` call hit "reviewer
    session already exists" and ``_record_step`` parsed the error string
    as RAW.

    With ``_resume_or_spawn_reviewer`` wired into ``_reviewer_phase``, the
    orphaned session is resumed (re-emits its verdict) and the cycle
    proceeds normally.
    """

    def test_does_not_escalate_with_raw_outcome(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        # Mimic the post-timeout state: reviewer session id persisted,
        # tester session also set (so _tester_phase resumes too — the
        # observed bugs hit this on a unit that had already passed tests
        # in a prior cycle), CI green (autouse fixture).
        s = state.get_unit_state("F-001-U-1")
        s.tester_session_id = "stale-tester-sid"
        s.reviewer_session_id = "stale-reviewer-sid"
        state.upsert_unit_state(s)

        # Per-role workers: tester + reviewer resume re-emit happy-path
        # markers so cycle_review can reach the terminal.
        def factory(role: str) -> FakeWorker:
            resume = {
                "tester": "TESTS_PASS",
                "reviewer": "REVIEW_RECOMMEND_MERGE: clean",
            }.get(role, "")
            return FakeWorker(role, "", resume)

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        # The original failure mode produced both of these — neither should land
        assert parsed["outcome"] != "escalated"
        assert "unexpected outcome: RAW" not in parsed["message"]
        # Positive assertion: cycle reached terminal success
        assert parsed["outcome"] == "approved_awaiting_merge"
