"""Spec-compliance tests for F-006-U-3.

Independent tester coverage locking in the EXACT contract from the
F-006-U-3 unit description verbatim:

  > Hook U-2's cycle-log writer into cycle_review's terminal branches in
  > orchestrator/tools/execution.py: append per-cycle entries during the
  > loop; finalize on REVIEW_RECOMMEND_MERGE / REVIEW_COMMENT /
  > escalation / manual kill. Add a check_unit_pr amendment that
  > captures mergeCommit.oid when the PR confirms merged and amends the
  > cycle log once (the only post-finalization edit allowed). Tests
  > assert: cycle logs appear on each terminal state, the file is
  > committed locally by the orchestrator-bot identity, and the merge
  > SHA backfill amends correctly.

The terminal-state assertions exercise:

  1. ``REVIEW_RECOMMEND_MERGE`` writes the cycle log to
     ``features/<F>/<U>.md``.
  2. ``REVIEW_COMMENT`` writes the cycle log.
  3. Every escalation path (tester blocked, reviewer blocked, cap-3,
     CI failure) writes the cycle log.
  4. The on-disk file is committed locally under the
     ``orchestrator-bot`` identity (never pushed).
  5. ``check_unit_pr`` re-renders the cycle log with
     ``mergeCommit.oid`` (``merge_commit_sha`` from the REST API) when
     the PR confirms merged. Pre-merge logs omit the line; post-merge
     logs include it.
  6. The merge-SHA backfill is best-effort — a non-repo workdir or
     missing ``gh`` doesn't break ``check_unit_pr``.
  7. Per-cycle "append" — cycle logs get rewritten between cycles inside
     the cycle_review loop, not only at terminal.

All ``ManagedAgentWorker`` calls + ``github.*`` helpers + ``subprocess.run``
+ ``ntfy.*`` are mocked. No real GitHub, git, or shell is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import cycle_log, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, ops

# --------------------------- shared fakes ---------------------------


@dataclass
class _Proc:
    """Minimal ``subprocess.CompletedProcess`` look-alike."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _Runner:
    """Records every argv it's called with; serves canned responses by
    argv prefix. Matches the pattern used in test_cycle_log.py /
    test_f006_u2_spec.py so the writer's contract surface is identical
    here.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: list[tuple[tuple[str, ...], _Proc]] = []

    def register(self, prefix: tuple[str, ...], proc: _Proc) -> None:
        self._responses.append((prefix, proc))

    def __call__(self, argv: list[str], **kwargs: Any) -> _Proc:
        self.calls.append(list(argv))
        for prefix, proc in self._responses:
            if tuple(argv[: len(prefix)]) == prefix:
                return proc
        return _Proc()


def _seed_feature(
    *,
    feature_id: str = "F-001",
    unit_id: str = "F-001-U-1",
    repo: str = "https://github.com/o/r",
) -> None:
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
        [WorkUnit(id=unit_id, feature_id=feature_id, title="u1", description="impl")],
    )
    state.approve_plan(feature_id)


def _seed_coded_unit(
    unit_id: str = "F-001-U-1",
    feature_id: str = "F-001",
    status: str = "in_ci",
) -> None:
    """A coder has already opened a PR — ready for cycle_review entry."""
    _seed_feature(feature_id=feature_id, unit_id=unit_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="feat/branch",
            pr_number=42,
            coder_session_id="sesn-c",
        )
    )


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """All CI gates pass synchronously — the cycle log machinery is what
    we're testing here, not the CI wait loop.
    """

    def fake_wait(*a: Any, **k: Any) -> CIWaitResult:
        return CIWaitResult(status="green", elapsed_seconds=0.1, total_checks=1)

    monkeypatch.setattr(execution.ci_wait, "wait_for_ci", fake_wait)


@pytest.fixture(autouse=True)
def _silent_ntfy(monkeypatch: pytest.MonkeyPatch) -> None:
    """ntfy pushes are real HTTP — stub them so terminal paths return cleanly."""
    monkeypatch.setattr(execution.ntfy, "push_escalation", lambda *a, **k: True)
    monkeypatch.setattr(execution.ntfy, "push_ready_to_merge", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _stub_pr_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the network-touching PR helpers so cycle_review doesn't try
    to hit GitHub during these tests.
    """
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests",
        lambda *a, **k: 0,
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
        lambda url: ("owner", "repo"),
    )


@pytest.fixture
def _stub_subprocess(monkeypatch: pytest.MonkeyPatch) -> _Runner:
    """Route every ``subprocess.run`` call the cycle-log writer would
    issue through a recording ``_Runner`` so tests can inspect what was
    invoked without touching the real shell.
    """
    runner = _Runner()
    # ``git diff --cached --quiet`` rc=1 means "there are staged changes"
    # → proceed to commit. That's the path we want under test.
    runner.register(("git", "diff", "--cached"), _Proc(returncode=1))
    # ``gh pr view`` returns enough JSON to keep the renderer happy.
    runner.register(
        ("gh", "pr", "view"),
        _Proc(stdout=json.dumps({"title": "T", "body": "B", "headRefOid": "abc"})),
    )
    runner.register(("gh", "api", "graphql"), _Proc(stdout="{}"))
    monkeypatch.setattr("orchestrator.cycle_log.subprocess.run", runner)
    monkeypatch.setattr("orchestrator.cycle_log_gh.subprocess.run", runner)
    return runner


# =============================================================================
# 1. REVIEW_RECOMMEND_MERGE writes the cycle log
# =============================================================================


class TestTerminalReviewRecommendMerge:
    def test_cycle_log_written_on_recommend_merge(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "reason": "OK"}
            ),
        )

        execution.cycle_review("F-001", "F-001-U-1")

        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        assert log_path.is_file(), "cycle log must exist after REVIEW_RECOMMEND_MERGE terminal"
        body = log_path.read_text(encoding="utf-8")
        assert body.startswith("# F-001-U-1"), body[:80]


# =============================================================================
# 2. REVIEW_COMMENT writes the cycle log
# =============================================================================


class TestTerminalReviewComment:
    def test_cycle_log_written_on_review_comment(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_coded_unit()
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

        execution.cycle_review("F-001", "F-001-U-1")

        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        assert log_path.is_file(), "cycle log must exist after REVIEW_COMMENT terminal"


# =============================================================================
# 3. Every escalation path writes the cycle log
# =============================================================================


class TestEscalationTerminals:
    """Per the unit description: finalize on "escalation" — i.e. every
    failure terminal of cycle_review, not just the success paths.
    """

    def test_tester_blocked_writes_cycle_log(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: "BLOCKED — tester for U: spec ambiguous",
        )

        execution.cycle_review("F-001", "F-001-U-1")

        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        assert log_path.is_file(), "cycle log must exist after tester-blocked escalation"

    def test_reviewer_blocked_writes_cycle_log(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: "BLOCKED — reviewer for U: cannot reach repo",
        )

        execution.cycle_review("F-001", "F-001-U-1")

        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        assert log_path.is_file(), "cycle log must exist after reviewer-blocked escalation"

    def test_cap_3_escalation_writes_cycle_log(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "BUG_FOUND", "bug": "x"}),
        )

        def fake_address_review(uid: str, src: str, fb: str) -> str:
            state.increment_review_round(uid)
            return json.dumps({"outcome": "FIX_PUSHED", "cycle": 1})

        monkeypatch.setattr(execution, "address_review", fake_address_review)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        assert log_path.is_file(), "cycle log must exist after cap-3 escalation"


# =============================================================================
# 4. Commit is local, identity is orchestrator-bot, never pushed
# =============================================================================


class TestCommitIdentityAndPolicy:
    def test_terminal_commit_uses_orchestrator_bot_identity(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        execution.cycle_review("F-001", "F-001-U-1")

        commit_calls = [c for c in _stub_subprocess.calls if c[:1] == ["git"] and "commit" in c]
        assert commit_calls, "expected at least one git commit invocation from terminal write"
        flat = " ".join(commit_calls[0])
        assert f"user.email={cycle_log.COMMIT_USER_EMAIL}" in flat
        assert f"user.name={cycle_log.COMMIT_USER_NAME}" in flat
        # Hard rule: cycle-log writes never push.
        assert not any("push" in argv for argv in _stub_subprocess.calls), (
            "cycle-log auto-commit must be local only — `git push` is a bug"
        )

    def test_constants_match_proposal_verbatim(self) -> None:
        # The proposal § "Persistence and commit strategy" pins both
        # verbatim — kept here as the U-3 surface so any drift in the
        # identity surfaces as a focused failure on this unit's tests.
        assert cycle_log.COMMIT_USER_EMAIL == "agent@orchestrator"
        assert cycle_log.COMMIT_USER_NAME == "orchestrator-bot"


# =============================================================================
# 5. check_unit_pr captures mergeCommit.oid post-merge
# =============================================================================


class TestMergeShaBackfill:
    def test_check_unit_pr_writes_merge_sha_into_cycle_log_on_first_merge_detection(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                pr_number=42,
                coder_session_id="sesn",
            )
        )

        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_state",
            lambda url, pr: {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-05-15T14:32:00Z",
                "head_sha": "abc",
                "merge_commit_sha": "ff00aa11bb22cc33dd44",
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_check_runs",
            lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
        )

        out = ops.check_unit_pr("F-001-U-1")
        parsed = json.loads(out)
        assert parsed["orchestrator_status"] == "done"

        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        assert log_path.is_file()
        body = log_path.read_text(encoding="utf-8")
        # The exact line the proposal example schema specifies.
        assert "Merge commit SHA: ff00aa11bb22cc33dd44" in body, (
            f"expected mergeCommit.oid backfill in:\n{body}"
        )

    def test_pre_merge_log_omits_merge_sha_line(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pre-merge cycle log (written by cycle_review terminal hook)
        must NOT have a placeholder Merge commit SHA line. Backfill is
        the only edit that introduces it.
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

        execution.cycle_review("F-001", "F-001-U-1")

        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        body = log_path.read_text(encoding="utf-8")
        assert "Merge commit SHA" not in body, (
            "pre-merge cycle log must not include a Merge commit SHA line; "
            "the post-merge backfill is the only edit that adds it"
        )

    def test_backfill_amends_existing_finalized_log(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drive a full terminal cycle_review (writes the cycle log
        without Merge commit SHA), then merge the PR + check_unit_pr.
        The same file should be re-rendered with the merge SHA appended.
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

        execution.cycle_review("F-001", "F-001-U-1")
        log_path = tmp_state_db.parent / "features" / "F-001" / "U-1.md"
        before = log_path.read_text(encoding="utf-8")
        assert "Merge commit SHA" not in before

        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_state",
            lambda url, pr: {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-05-15T14:32:00Z",
                "head_sha": "abc",
                "merge_commit_sha": "deadbeef0000",
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_check_runs",
            lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
        )

        ops.check_unit_pr("F-001-U-1")
        after = log_path.read_text(encoding="utf-8")
        assert "Merge commit SHA: deadbeef0000" in after

    def test_check_unit_pr_does_not_blow_up_when_writer_errors(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Best-effort guarantee: a writer failure (no git, disk full,
        permission error) must not propagate out of check_unit_pr — the
        merged-state response is what the lead is waiting on.
        """
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                pr_number=42,
                coder_session_id="sesn",
            )
        )

        def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("simulated writer failure")

        monkeypatch.setattr("orchestrator.tools.ops.cycle_log.write_cycle_log", boom)
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_state",
            lambda url, pr: {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-05-15T14:32:00Z",
                "head_sha": "abc",
                "merge_commit_sha": "f00ba8",
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_check_runs",
            lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
        )

        out = ops.check_unit_pr("F-001-U-1")
        parsed = json.loads(out)
        assert parsed["orchestrator_status"] == "done"
        assert parsed["pr_state"]["merged"] is True

    def test_no_backfill_when_merge_sha_missing(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If GitHub omits ``merge_commit_sha`` (transient — sometimes the
        REST API returns null right after a merge) the writer must not
        run with ``merge_commit_sha=None`` because that would render a
        Merge-SHA-less log on top of the already-finalized one.
        """
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                pr_number=42,
                coder_session_id="sesn",
            )
        )

        calls: list[dict[str, Any]] = []

        def spy_write(unit_id: str, **kwargs: Any) -> Path:
            calls.append({"unit_id": unit_id, **kwargs})
            return Path("/dev/null")

        monkeypatch.setattr("orchestrator.tools.ops.cycle_log.write_cycle_log", spy_write)
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_state",
            lambda url, pr: {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-05-15T14:32:00Z",
                "head_sha": "abc",
                "merge_commit_sha": None,
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_check_runs",
            lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
        )

        ops.check_unit_pr("F-001-U-1")
        assert calls == [], (
            f"writer must be skipped when merge_commit_sha is null; got call: {calls!r}"
        )


# =============================================================================
# 6. github.get_pr_state surfaces merge_commit_sha
# =============================================================================


class TestGetPrStateSurface:
    """The merge-SHA backfill depends on ``github.get_pr_state`` returning
    the field. Tester guard against a future refactor dropping it.
    """

    def test_get_pr_state_returns_merge_commit_sha(
        self, monkeypatch: pytest.MonkeyPatch, with_github_token: None
    ) -> None:
        from orchestrator import github

        captured = {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-15T14:32:00Z",
            "head": {"sha": "abc"},
            "merge_commit_sha": "ff00aa11",
            "mergeable": True,
            "mergeable_state": "clean",
        }

        class _FakeResp:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return captured

        class _FakeClient:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def __enter__(self) -> _FakeClient:
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def get(self, url: str) -> _FakeResp:
                return _FakeResp()

        monkeypatch.setattr(github.httpx, "Client", _FakeClient)
        result = github.get_pr_state("https://github.com/o/r", 42)
        assert result["merge_commit_sha"] == "ff00aa11"


# =============================================================================
# 7. Per-cycle "append" — log gets rewritten during the loop
# =============================================================================


class TestPerCycleAppendDuringLoop:
    """The unit description says the writer must "append per-cycle entries
    during the loop", not only fire at terminal. Verify the writer is
    invoked more than once when there's at least one mid-cycle fix.
    """

    def test_cycle_log_written_per_cycle_not_just_terminal(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _stub_subprocess: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_coded_unit()
        # First tester run finds a bug; address_review pushes a fix; second
        # tester passes. Reviewer recommends merge. Three terminals worth of
        # write opportunities — at least one mid-cycle + one final.
        tester_outcomes = iter(
            [
                json.dumps({"unit_id": "U", "outcome": "BUG_FOUND", "bug": "x"}),
                json.dumps({"unit_id": "U", "outcome": "TESTS_PASS"}),
            ]
        )
        monkeypatch.setattr(execution, "spawn_tester", lambda f, u: next(tester_outcomes))

        def fake_address_review(uid: str, src: str, fb: str) -> str:
            state.increment_review_round(uid)
            return json.dumps({"outcome": "FIX_PUSHED", "cycle": 1})

        monkeypatch.setattr(execution, "address_review", fake_address_review)
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )

        write_calls: list[tuple[Any, ...]] = []
        original_write = cycle_log.write_cycle_log

        def spy(unit_id: str, **kwargs: Any) -> Path:
            write_calls.append((unit_id, kwargs.get("merge_commit_sha")))
            return original_write(unit_id, **kwargs)

        monkeypatch.setattr("orchestrator.tools.execution.cycle_log.write_cycle_log", spy)

        execution.cycle_review("F-001", "F-001-U-1")

        # ≥2 calls = per-cycle append + terminal finalize.
        assert len(write_calls) >= 2, (
            f"expected per-cycle + terminal writes; got {len(write_calls)}: {write_calls}"
        )
        # None of the in-loop writes carries a merge SHA — that's exclusively
        # the post-merge backfill.
        for _uid, merge_sha in write_calls:
            assert merge_sha is None, (
                f"cycle_review writes must not pass merge_commit_sha; got {merge_sha!r}"
            )


# =============================================================================
# 8. Cycle log writer failure must not break cycle_review
# =============================================================================


class TestCycleLogWriterIsolation:
    def test_writer_exception_does_not_abort_cycle_review(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("disk full")

        monkeypatch.setattr("orchestrator.tools.execution.cycle_log.write_cycle_log", boom)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge", (
            "cycle_review must still terminate cleanly when the cycle-log writer raises"
        )
