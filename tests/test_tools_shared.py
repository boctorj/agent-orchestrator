"""Tests for shared infra in orchestrator/tools/__init__.py."""

from __future__ import annotations

import pytest

from orchestrator.models import Feature, WorkUnit
from orchestrator.tools import (
    BLOCKED_RE,
    BUG_FOUND_RE,
    CAP_3,
    FIX_PUSHED_RE,
    PR_URL_RE,
    REVIEW_APPROVED_RE,
    REVIEW_CHANGES_RE,
    REVIEW_COMMENT_RE,
    REVIEW_RECOMMEND_MERGE_RE,
    TESTS_PASS_RE,
    branch_for,
    compose_coder_task,
    compose_fix_task,
    compose_reviewer_task,
    compose_tester_task,
    need_github_token,
    tail,
)

# --------------------------- helpers ---------------------------


class TestBranchFor:
    def test_uses_branch_prefix_and_unit_tail(self):
        f = Feature(id="F-001", title="t", description="d", branch_prefix="feat/F-001-foo")
        u = WorkUnit(id="F-001-U-3", feature_id="F-001", title="t", description="d")
        assert branch_for(f, u) == "feat/F-001-foo-u-3"

    def test_falls_back_to_lowercased_unit_id(self):
        f = Feature(id="F-001", title="t", description="d", branch_prefix="")
        u = WorkUnit(id="F-001-U-1", feature_id="F-001", title="t", description="d")
        assert branch_for(f, u) == "f-001-u-1"


class TestTail:
    def test_returns_full_text_when_under_limit(self):
        assert tail("short", n=100) == "short"

    def test_truncates_with_ellipsis_prefix(self):
        result = tail("a" * 1000, n=100)
        assert result.startswith("...\n")
        assert len(result) == len("...\n") + 100

    def test_default_limit_800(self):
        assert tail("x" * 800) == "x" * 800
        assert tail("x" * 801).startswith("...\n")


class TestNeedGithubToken:
    @pytest.fixture(autouse=True)
    def _no_github_app(self, monkeypatch):
        """Ensure no GitHub App env vars leak into these tests."""
        for var in (
            "GITHUB_APP_ID",
            "GITHUB_APP_INSTALLATION_ID",
            "GITHUB_APP_PRIVATE_KEY_PATH",
            "GITHUB_APP_PRIVATE_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_returns_error_when_unset(self, no_github_token):
        result = need_github_token()
        assert result is not None
        assert "no GitHub auth" in result

    def test_returns_error_when_blank(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "   ")
        assert need_github_token() is not None

    def test_returns_none_when_pat_set(self, with_github_token):
        assert need_github_token() is None


# --------------------------- regexes ---------------------------


class TestRegexes:
    def test_pr_url(self):
        m = PR_URL_RE.search("blah\nPR_URL: https://github.com/joe/repo/pull/42\n")
        assert m is not None
        assert m.group(1) == "https://github.com/joe/repo/pull/42"
        assert m.group(2) == "42"

    def test_pr_url_case_insensitive(self):
        m = PR_URL_RE.search("pr_url: https://github.com/o/r/pull/1")
        assert m is not None
        assert m.group(2) == "1"

    def test_blocked_captures_reason(self):
        m = BLOCKED_RE.search("intro\nBLOCKED: spec is ambiguous")
        assert m is not None
        assert m.group(1) == "spec is ambiguous"

    def test_tests_pass_only_matches_alone(self):
        assert TESTS_PASS_RE.search("TESTS_PASS") is not None
        assert TESTS_PASS_RE.search("blah\nTESTS_PASS") is not None
        # Whitespace at end OK
        assert TESTS_PASS_RE.search("TESTS_PASS   ") is not None

    def test_bug_found_captures_summary(self):
        m = BUG_FOUND_RE.search("logs...\nBUG_FOUND: division by zero on n=0")
        assert m is not None
        assert m.group(1) == "division by zero on n=0"

    def test_review_approved(self):
        assert REVIEW_APPROVED_RE.search("REVIEW_APPROVED") is not None
        assert REVIEW_APPROVED_RE.search("approved!") is None

    def test_review_changes_captures_issue(self):
        m = REVIEW_CHANGES_RE.search("comments...\nREVIEW_REQUEST_CHANGES: missing tests for n=0")
        assert m is not None
        assert m.group(1) == "missing tests for n=0"

    def test_review_comment(self):
        assert REVIEW_COMMENT_RE.search("REVIEW_COMMENT") is not None

    def test_review_recommend_merge(self):
        m = REVIEW_RECOMMEND_MERGE_RE.search(
            "endorsed, see comment.\nREVIEW_RECOMMEND_MERGE: 16 tests pass, scope clean"
        )
        assert m is not None
        assert m.group(1) == "16 tests pass, scope clean"

    def test_fix_pushed(self):
        assert FIX_PUSHED_RE.search("done. FIX_PUSHED.") is not None
        assert FIX_PUSHED_RE.search("nothing here") is None


# --------------------------- compose templates ---------------------------


@pytest.fixture
def sample_feature():
    return Feature(
        id="F-001",
        title="math utils",
        description="Add math helpers.",
        repo_path="https://github.com/joe/repo",
        branch_prefix="feat/F-001-math",
    )


@pytest.fixture
def sample_unit():
    return WorkUnit(
        id="F-001-U-1",
        feature_id="F-001",
        title="add module",
        description="Create math_utils.py with add().",
        depends_on=[],
    )


class TestComposeTasks:
    def test_coder_task_includes_critical_fields(self, sample_feature, sample_unit):
        out = compose_coder_task(sample_feature, sample_unit, "feat/F-001-math-u-1", "tok123")
        assert "F-001-U-1" in out
        assert "https://github.com/joe/repo" in out
        assert "feat/F-001-math-u-1" in out
        assert "tok123" in out
        assert sample_unit.description in out
        assert sample_feature.description in out
        assert "PR_URL: <url>" in out  # marker hint

    def test_tester_task_lists_three_markers(self, sample_feature, sample_unit):
        out = compose_tester_task(sample_feature, sample_unit, "branch", 42, "tok")
        assert "TESTS_PASS" in out
        assert "BUG_FOUND" in out
        assert "BLOCKED" in out
        assert "PR_NUMBER: 42" in out

    def test_reviewer_task_lists_emittable_markers(self, sample_feature, sample_unit):
        out = compose_reviewer_task(sample_feature, sample_unit, 42, "tok")
        assert "PR #42" in out
        assert "REVIEW_RECOMMEND_MERGE" in out
        assert "REVIEW_REQUEST_CHANGES" in out
        assert "REVIEW_COMMENT" in out
        assert "BLOCKED" in out
        # REVIEW_APPROVED is reserved (orchestrator never uses --approve);
        # we don't list it as an emit option in the task message.
        assert "REVIEW_APPROVED" not in out

    def test_fix_task_includes_feedback(self, sample_feature, sample_unit):
        feedback = "tests fail on n=0; division by zero"
        out = compose_fix_task(sample_feature, sample_unit, "branch", 42, "tester", feedback)
        assert feedback in out
        assert "tester" in out
        assert "FIX_PUSHED" in out
        assert "PR_NUMBER: 42" in out
        assert "SOURCE:    tester" in out

    def test_fix_task_renders_feature_spec_block_when_provided(self, sample_feature, sample_unit):
        """F-006-U-6 review feedback (H1): the coder.md "Re-read FEATURE
        SPEC on every resume" rule promises the orchestrator re-injects
        the block on each fix-loop turn. compose_fix_task must accept the
        spec kwarg and render it so the prompt's contract is true at
        runtime, per features/F-006/spec.md § Constraints.
        """
        out = compose_fix_task(
            sample_feature,
            sample_unit,
            "branch",
            42,
            "human",
            "spec was clarified",
            feature_spec_text="# F-001\n\n## Acceptance\n- add(2, 3) == 5",
        )
        assert "## FEATURE SPEC" in out
        assert "add(2, 3) == 5" in out

    def test_fix_task_renders_predecessor_units_block_when_provided(
        self, sample_feature, sample_unit
    ):
        out = compose_fix_task(
            sample_feature,
            sample_unit,
            "branch",
            42,
            "reviewer",
            "drift on validator",
            predecessor_logs=[("F-001-U-0", "Picked validator Y over X.")],
        )
        assert "## PREDECESSOR UNITS" in out
        assert "F-001-U-0" in out
        assert "validator Y" in out

    def test_fix_task_omits_context_blocks_when_kwargs_empty(self, sample_feature, sample_unit):
        """The new kwargs default to empty, so pre-F-006 call sites
        (and tests that don't pass them) see the original message
        unchanged — no stray block headers."""
        out = compose_fix_task(sample_feature, sample_unit, "branch", 42, "ci", "test failed")
        assert "## FEATURE SPEC" not in out
        assert "## PREDECESSOR UNITS" not in out


# --------------------------- constants ---------------------------


def test_cap_3_value():
    assert CAP_3 == 3
