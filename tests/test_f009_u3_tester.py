"""F-009-U-3 tester suite — independent verification.

Pinned to the UNIT DESCRIPTION, not to the implementation. Five required
scenarios from the spec:

    1. spawn_tester on escalated unit with prior tester_session_id calls
       worker.resume (NOT worker.spawn); marker parsed via U-1; status
       flips per marker.
    2. Same for spawn_reviewer.
    3. spawn_tester on unit with NO tester_session_id calls worker.spawn
       (initial-spawn path unchanged).
    4. cycle_review retry after simulated transient failure recovers
       cleanly on second call.
    5. build_recovery_prompt returns correct stock message for each
       (role, reason) pair.

Plus the contract-change tests called out in the spec:

    * "duplicate-spawn while role is running" (status='testing' AND
      session_id set) is no longer a refusal — it's a resume too.
    * address_review on an escalated unit reaches the resume path (no
      guard rejects).
    * build_recovery_prompt has variants per role for the three reasons
      (post-escalation / transient-retry / ci-fix).

The fixtures stub the ManagedAgentWorker boundary so no real network call
happens — every test captures spawn vs. resume call counts and inspects
the persisted state row directly.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green so the gate never intercepts."""

    def fake_wait(*_a, **_kw):
        return CIWaitResult(status="green", elapsed_seconds=0.1, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


@pytest.fixture(autouse=True)
def _silence_ntfy(monkeypatch):
    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True)
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
    )


class _RecordingWorker:
    """Drop-in for ``ManagedAgentWorker`` recording every spawn/resume call.

    One instance per role (the factory below memoises by role) so a test
    that exercises spawn_tester + spawn_reviewer back-to-back gets two
    independent recorders.
    """

    def __init__(self, role: str, *, spawn_response: str = "", resume_response: str = ""):
        self.role = role
        self._spawn_response = spawn_response
        self._resume_response = resume_response
        self.spawn_calls: list[tuple[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        sid = f"spawned-{self.role}-{len(self.spawn_calls)}"
        self.spawn_calls.append((task, title))
        return sid, self._spawn_response

    def resume(self, session_id: str, message: str) -> str:
        self.resume_calls.append((session_id, message))
        return self._resume_response

    def archive(self, _sid: str) -> None:  # pragma: no cover
        pass


def _install_workers(monkeypatch, *, per_role: dict[str, dict]) -> dict[str, _RecordingWorker]:
    """Install role-keyed _RecordingWorker stubs, return the role→instance map."""
    instances: dict[str, _RecordingWorker] = {}

    def factory(role: str) -> _RecordingWorker:
        if role not in instances:
            instances[role] = _RecordingWorker(role, **per_role.get(role, {}))
        return instances[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return instances


def _stub_github(monkeypatch):
    """No-op every github / safe_* call execution.py touches."""
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
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("o", "r"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.get_pr_state",
        lambda *a, **k: {"head_sha": "cafebabe", "state": "open", "merged": False},
    )


def _seed(
    *,
    status: str = "in_ci",
    coder_session_id: str = "sesn-coder-orig",
    tester_session_id: str = "",
    reviewer_session_id: str = "",
    last_error: str = "",
    review_round: int = 0,
) -> None:
    """Save a Feature + Plan + WorkUnitState for F-001-U-1 in the test DB."""
    state.save_feature(
        Feature(
            id="F-001",
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
        )
    )
    state.save_plan(
        "F-001",
        [WorkUnit(id="F-001-U-1", feature_id="F-001", title="u", description="d")],
    )
    state.approve_plan("F-001")
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-001-U-1",
            feature_id="F-001",
            status=status,
            branch="feat/u1",
            pr_number=7,
            coder_session_id=coder_session_id,
            tester_session_id=tester_session_id,
            reviewer_session_id=reviewer_session_id,
            review_round=review_round,
            last_error=last_error,
        )
    )


# =====================================================================
# Requirement 5: build_recovery_prompt — per (role, reason) stock messages
# =====================================================================


class TestBuildRecoveryPrompt:
    """Pin every (role, reason) pair documented in the spec.

    The spec calls out three reasons (post-escalation / transient-retry /
    ci-fix) and three roles (coder / tester / reviewer). The helper must
    return a non-empty string for each pair, and the marker vocabulary
    for that role must appear in the prompt so the agent knows what to
    emit on resume.
    """

    @pytest.mark.parametrize("role", ["coder", "tester", "reviewer"])
    @pytest.mark.parametrize("reason", ["post-escalation", "transient-retry", "ci-fix"])
    def test_every_pair_returns_nonempty_string(self, role, reason):
        msg = execution.build_recovery_prompt(role, reason, last_error="x", details="y")
        assert isinstance(msg, str)
        assert msg.strip(), f"{(role, reason)} returned an empty prompt"

    def test_coder_prompt_mentions_coder_markers(self):
        for reason in ("post-escalation", "transient-retry", "ci-fix"):
            msg = execution.build_recovery_prompt("coder", reason, last_error="x", details="y")
            assert "FIX_PUSHED" in msg, (
                f"coder/{reason} prompt must name FIX_PUSHED so the agent re-emits a recognised marker"
            )
            assert "BLOCKED" in msg

    def test_tester_prompt_mentions_tester_markers(self):
        for reason in ("post-escalation", "transient-retry", "ci-fix"):
            msg = execution.build_recovery_prompt("tester", reason, last_error="x", details="y")
            for marker in ("TESTS_PASS", "BUG_FOUND", "BLOCKED"):
                assert marker in msg, (
                    f"tester/{reason} prompt must name {marker} so the agent re-emits a recognised marker"
                )

    def test_reviewer_prompt_mentions_reviewer_markers(self):
        for reason in ("post-escalation", "transient-retry", "ci-fix"):
            msg = execution.build_recovery_prompt("reviewer", reason, last_error="x", details="y")
            for marker in (
                "REVIEW_RECOMMEND_MERGE",
                "REVIEW_REQUEST_CHANGES",
                "REVIEW_COMMENT",
                "BLOCKED",
            ):
                assert marker in msg, (
                    f"reviewer/{reason} prompt must name {marker} for marker vocabulary completeness"
                )

    def test_post_escalation_template_uses_last_error(self):
        """The spec says: 'post-escalation: You were stuck on <last_error>;
        CI is now green, please verify/re-run/continue.' — i.e. the
        last_error string must appear in the rendered prompt."""
        for role in ("coder", "tester", "reviewer"):
            msg = execution.build_recovery_prompt(
                role, "post-escalation", last_error="auth token expired"
            )
            assert "auth token expired" in msg, (
                f"post-escalation/{role} must interpolate last_error into the prompt"
            )

    def test_ci_fix_template_uses_details(self):
        """The spec says: 'ci-fix: CI has failed: <details>. Push a focused
        fix.' — the details placeholder must be substituted, not left as a
        literal '{details}' brace."""
        for role in ("coder", "tester", "reviewer"):
            msg = execution.build_recovery_prompt(role, "ci-fix", details="ruff E501 on mod.py:42")
            assert "ruff E501 on mod.py:42" in msg
            assert "{details}" not in msg, "unsubstituted format placeholder is a prompt-drift bug"

    def test_post_escalation_template_clean_when_no_last_error(self):
        """Calling build_recovery_prompt without last_error must NOT leave a
        literal '{last_error}' in the rendered prompt — drift in the
        template renders the brace verbatim, which an LLM treats as
        garbage."""
        for role in ("coder", "tester", "reviewer"):
            msg = execution.build_recovery_prompt(role, "post-escalation")
            assert "{last_error}" not in msg

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError):
            execution.build_recovery_prompt("janitor", "post-escalation")  # type: ignore[arg-type]

    def test_unknown_reason_raises(self):
        with pytest.raises(ValueError):
            execution.build_recovery_prompt("tester", "vibes-check")  # type: ignore[arg-type]


# =====================================================================
# Requirement 1: spawn_tester resume-or-spawn on escalated unit
# =====================================================================


class TestSpawnTesterResumeOnEscalated:
    """The spec's primary test case: 'spawn_tester on escalated unit with
    prior tester_session_id calls worker.resume (NOT worker.spawn);
    marker parsed via U-1; status flips per marker.'
    """

    def test_resume_not_spawn_on_escalated_unit_with_tester_sid(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed(
            status="escalated",
            tester_session_id="orphan-tester",
            last_error="cap-3 hit on tester bug",
        )
        workers = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)

        # 1) Resume, not spawn.
        tester = workers["tester"]
        assert tester.spawn_calls == [], (
            "spawn_tester on escalated+tester_session_id must NOT cold-start "
            "a fresh worker.spawn — it must resume the existing session"
        )
        assert len(tester.resume_calls) == 1, "exactly one worker.resume call expected"
        sid, _msg = tester.resume_calls[0]
        assert sid == "orphan-tester", f"resume must target the persisted session id, got {sid!r}"

        # 2) Marker parsed via U-1 (TESTS_PASS recognised).
        assert parsed["outcome"] == "TESTS_PASS"
        assert parsed["session_id"] == "orphan-tester"

        # 3) Status flips per marker (TESTS_PASS → in_ci).
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci", f"TESTS_PASS must flip status to in_ci, got {s.status!r}"
        assert s.tester_session_id == "orphan-tester", "session id must persist (no replacement)"

    def test_resume_records_marker_event_via_record_terminal_marker(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """U-1 wire-up: _record_terminal_marker is called from the resume
        branch and writes the role-specific structured event."""
        _seed(status="escalated", tester_session_id="orphan-t", last_error="boom")
        _install_workers(monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}})
        _stub_github(monkeypatch)

        execution.spawn_tester("F-001", "F-001-U-1")
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "tests_pass" in types, (
            "marker chain must record a 'tests_pass' event on the resume's TESTS_PASS — "
            "this is what U-1's _record_terminal_marker writes"
        )

    def test_resume_with_bug_found_keeps_status_active(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """BUG_FOUND is non-flipping (caller's loop runs the fix-cycle); the
        marker chain must still parse it but status stays 'testing' — i.e.
        does NOT regress to escalated."""
        _seed(status="escalated", tester_session_id="orphan", last_error="prior")
        _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "BUG_FOUND: off-by-one in foo"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "BUG_FOUND"
        assert "off-by-one" in parsed["bug"]

        s = state.get_unit_state("F-001-U-1")
        # Active (testing) is correct — the fix-cycle owns the status flip.
        # The key invariant: NOT back to 'escalated'.
        assert s.status != "escalated"

    def test_resume_with_blocked_re_escalates(self, tmp_state_db, with_github_token, monkeypatch):
        """If the recovered session also BLOCKS, the marker chain writes
        the new last_error and the unit goes back to escalated."""
        _seed(status="escalated", tester_session_id="orphan", last_error="prior boom")
        _install_workers(
            monkeypatch,
            per_role={
                "tester": {"resume_response": "BLOCKED: dependency_install_failed | pip oops"}
            },
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        assert "BLOCKED" in out, f"BLOCKED marker must propagate to caller, got {out!r}"

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        # New error reflects the new BLOCKED payload (replacing the prior).
        assert s.last_error and s.last_error != "prior boom"


# =====================================================================
# Requirement 1bis: duplicate-spawn while role is running is now a resume
# =====================================================================


class TestSpawnTesterDuplicateWhileTesting:
    """Spec contract change:

        > the 'duplicate-spawn while role is running' case (status=='testing'
        > AND session_id set) is no longer a refusal — it's a resume too.

    Pre-F-009-U-3 this returned 'ERROR: tester session already exists ...'.
    """

    def test_status_testing_with_sid_resumes_not_refuses(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed(status="testing", tester_session_id="mid-flight")
        workers = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")

        assert "already exists" not in out, (
            "the 'duplicate-spawn while role is running' refusal was removed by F-009-U-3"
        )
        tester = workers["tester"]
        assert len(tester.resume_calls) == 1
        assert tester.resume_calls[0][0] == "mid-flight"
        assert tester.spawn_calls == []


# =====================================================================
# Requirement 2: spawn_reviewer resume-or-spawn (symmetric)
# =====================================================================


class TestSpawnReviewerResumeOnEscalated:
    """Symmetric to spawn_tester: escalated+reviewer_session_id → resume."""

    def test_resume_not_spawn_on_escalated_unit_with_reviewer_sid(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed(
            status="escalated",
            reviewer_session_id="orphan-reviewer",
            last_error="cap-3 on reviewer changes",
        )
        workers = _install_workers(
            monkeypatch,
            per_role={
                "reviewer": {
                    "resume_response": "looks good\nREVIEW_RECOMMEND_MERGE: spec satisfied"
                }
            },
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)

        reviewer = workers["reviewer"]
        assert reviewer.spawn_calls == [], (
            "spawn_reviewer on escalated+reviewer_session_id must resume, not cold-start"
        )
        assert len(reviewer.resume_calls) == 1
        sid, _msg = reviewer.resume_calls[0]
        assert sid == "orphan-reviewer"

        # Marker parsed via U-1, status flips on REVIEW_RECOMMEND_MERGE.
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"
        assert parsed["session_id"] == "orphan-reviewer"

        s = state.get_unit_state("F-001-U-1")
        # F-009-U-4 retargeted REVIEW_RECOMMEND_MERGE from in_ci to
        # approved_awaiting_merge — the marker is terminal for the cycle,
        # so the unit lands in the same status cycle_review's
        # _emit_terminal would set.
        assert s.status == "approved_awaiting_merge"
        assert s.reviewer_session_id == "orphan-reviewer"

    def test_resume_with_request_changes_keeps_status_active(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed(status="reviewing", reviewer_session_id="rev-orphan")
        _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_REQUEST_CHANGES: missing edge case"}},
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_REQUEST_CHANGES"
        assert "missing edge case" in parsed["issue"]

        s = state.get_unit_state("F-001-U-1")
        # REVIEW_REQUEST_CHANGES is non-flipping; status stays active so the
        # caller's fix-loop can call address_review.
        assert s.status == "reviewing"

    def test_resume_records_marker_event_via_record_terminal_marker(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed(status="escalated", reviewer_session_id="rev-orphan", last_error="boom")
        _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_RECOMMEND_MERGE: ok"}},
        )
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-001", "F-001-U-1")
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "reviewer_recommend_merge" in types


# =====================================================================
# Requirement 3: spawn_tester on unit with NO session calls worker.spawn
# =====================================================================


class TestSpawnTesterInitialPathUnchanged:
    """The spec says: 'spawn_tester on unit with NO tester_session_id calls
    worker.spawn (initial-spawn path unchanged).'

    Resume-or-spawn must NOT regress the cold-start path — without a
    session id, worker.spawn is called with the composed tester task.
    """

    def test_no_tester_session_id_cold_starts_via_spawn(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed(status="in_ci", tester_session_id="")
        workers = _install_workers(
            monkeypatch, per_role={"tester": {"spawn_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "TESTS_PASS"

        tester = workers["tester"]
        assert len(tester.spawn_calls) == 1, (
            "initial spawn path must call worker.spawn exactly once"
        )
        assert tester.resume_calls == [], "no resume on the initial-spawn path"

        # The new session id is persisted on the unit.
        s = state.get_unit_state("F-001-U-1")
        assert s.tester_session_id, "spawn_tester must persist session id on initial spawn"
        assert s.tester_session_id.startswith("spawned-tester-"), (
            f"persisted sid must come from worker.spawn, got {s.tester_session_id!r}"
        )

    def test_no_reviewer_session_id_cold_starts_via_spawn(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Symmetric: spawn_reviewer initial path is also unchanged."""
        _seed(status="in_ci", reviewer_session_id="")
        workers = _install_workers(
            monkeypatch,
            per_role={"reviewer": {"spawn_response": "REVIEW_RECOMMEND_MERGE: clean"}},
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"

        reviewer = workers["reviewer"]
        assert len(reviewer.spawn_calls) == 1
        assert reviewer.resume_calls == []


# =====================================================================
# address_review on escalated unit: no guard rejects
# =====================================================================


class TestAddressReviewReachesEscalatedUnit:
    """Spec says: 'Same routing in address_review (around :574): if
    coder_session_id is set, resume — ensure escalated units reach this
    path (no guard rejects).'

    Gap-C closer: a human surfacing feedback after escalation must be
    able to drive the coder thread forward. The unit's status='escalated'
    is the *reason* the human is calling — it must not also be a refusal.
    """

    def test_address_review_on_escalated_unit_resumes_coder(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed(status="escalated", coder_session_id="coder-sid", last_error="cap-3")
        workers = _install_workers(
            monkeypatch, per_role={"coder": {"resume_response": "applied fix\nFIX_PUSHED"}}
        )
        _stub_github(monkeypatch)

        out = execution.address_review("F-001-U-1", "human", "try approach B")

        # Must not be rejected with a guard error.
        assert not out.startswith("ERROR:"), (
            f"address_review on an escalated unit must NOT be refused by a guard; got: {out!r}"
        )
        parsed = json.loads(out)
        assert parsed["outcome"] == "FIX_PUSHED"

        # The coder thread was resumed at its persisted session id.
        coder = workers["coder"]
        assert len(coder.resume_calls) == 1
        sid, _msg = coder.resume_calls[0]
        assert sid == "coder-sid"
        assert coder.spawn_calls == []

        # Marker chain advanced the unit out of escalated on FIX_PUSHED.
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci", (
            f"FIX_PUSHED on an escalated unit must transition to in_ci, got {s.status!r}"
        )


# =====================================================================
# Requirement 4: cycle_review retry recovers cleanly on second call
# =====================================================================


class TestCycleReviewTransientRetry:
    """Spec says: 'cycle_review retry after simulated transient failure
    recovers cleanly on second call.'

    Pre-F-009-U-3 the second cycle_review escalated immediately because
    spawn_tester refused on the existing tester_session_id (the orphan
    of the first call). Post-fix, the resume branch picks up where the
    worker left off and the cycle reaches the happy terminal.
    """

    def test_second_cycle_review_recovers_with_orphaned_tester_session(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        # Simulate the post-first-call state: tester_session_id is set
        # (from a worker.spawn that did happen) but no terminal marker
        # was recorded because the network dropped the response.
        _seed(status="in_ci", tester_session_id="orphan-from-first-call")

        _install_workers(
            monkeypatch,
            per_role={
                "tester": {"resume_response": "TESTS_PASS"},
                "reviewer": {"spawn_response": "REVIEW_RECOMMEND_MERGE: looks good"},
            },
        )
        _stub_github(monkeypatch)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        assert parsed["outcome"] == "approved_awaiting_merge", (
            "cycle_review retry must converge on the happy terminal after a transient "
            f"failure left tester_session_id populated; got {parsed!r}"
        )
        # And absolutely not the legacy RAW-escalation regression.
        assert "unexpected outcome: RAW" not in parsed.get("message", "")

    def test_second_cycle_review_recovers_with_orphaned_reviewer_session(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Symmetric: a transient failure during the reviewer phase leaves
        reviewer_session_id populated. The retry must resume the reviewer,
        not refuse."""
        _seed(
            status="in_ci",
            reviewer_session_id="orphan-reviewer-from-first-call",
        )

        _install_workers(
            monkeypatch,
            per_role={
                "tester": {"spawn_response": "TESTS_PASS"},
                "reviewer": {"resume_response": "REVIEW_RECOMMEND_MERGE: spec ok"},
            },
        )
        _stub_github(monkeypatch)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge", (
            "cycle_review must recover from an orphaned reviewer_session_id by "
            f"resuming, not refusing; got {parsed!r}"
        )
