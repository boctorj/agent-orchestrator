"""Tests for orchestrator/repo_verify.py — URL normalization + policy checks."""

from __future__ import annotations

import pytest

from orchestrator import repo_verify
from orchestrator.repo_verify import normalize_repo_url, verify

# --------------------------- normalize_repo_url ---------------------------


class TestNormalizeRepoUrl:
    @pytest.mark.parametrize(
        ("inp", "expected"),
        [
            ("https://github.com/owner/repo", "https://github.com/owner/repo"),
            ("github.com/owner/repo", "https://github.com/owner/repo"),
            ("https://github.com/owner/repo/", "https://github.com/owner/repo"),
            ("https://github.com/owner/repo.git", "https://github.com/owner/repo"),
            ("https://github.com/Owner/Repo", "https://github.com/owner/repo"),
            ("  https://github.com/owner/repo  ", "https://github.com/owner/repo"),
            ("http://github.com/o/r", "https://github.com/o/r"),
        ],
    )
    def test_canonicalizes_valid_forms(self, inp, expected):
        assert normalize_repo_url(inp) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "https://gitlab.com/owner/repo",
            "https://github.com/owner",
            "https://github.com/",
            "https://github.com/o/r/extra",
            "git@github.com:owner/repo",
            "https://example.com/owner/repo",
        ],
    )
    def test_rejects_invalid_forms(self, bad):
        with pytest.raises(ValueError):
            normalize_repo_url(bad)


# --------------------------- verify() ---------------------------


class _FakeResponse:
    """Mimics enough of httpx.Response for verify()'s code path."""

    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


def _make_client(responses: dict[str, _FakeResponse]):
    """Build a fake httpx.Client that returns canned responses by URL-suffix match.

    A response key matches if it is a strict suffix of the request URL's path
    (query string ignored). That way `/repos/o/r` does NOT accidentally match
    `/repos/o/r/branches/main/protection` or `/repos/o/r/contents/CODEOWNERS` —
    the test must explicitly mock those longer URLs (or leave them to fall
    through to the default 404).
    """

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, **kw):
            path = url.split("?", 1)[0]
            for key, resp in responses.items():
                if path.endswith(key):
                    return resp
            # Default: 404
            return _FakeResponse(404)

    return FakeClient


def _good_repo_meta(default_branch="main"):
    return {
        "default_branch": default_branch,
        "permissions": {"pull": True, "push": True, "admin": False},
    }


def _good_protection():
    return {
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "enforce_admins": {"enabled": True},
        "required_signatures": {"enabled": False},
        "required_status_checks": {"contexts": [], "checks": []},
    }


class TestVerify:
    def test_all_checks_pass(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            # Token user matches the repo owner so the identity-match check
            # passes trivially without needing to mock /collaborators.
            "/user": _FakeResponse(200, {"login": "owner"}),
            # CODEOWNERS: 404 everywhere
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")

        assert result.passed
        assert result.default_branch == "main"
        assert result.auth_mode == "pat"
        assert result.auth_identity == "user:owner"
        # All check names present
        names = [c.name for c in result.checks]
        assert "read access" in names
        assert "identity match" in names
        assert "write access" in names
        assert "branch protection exists" in names
        assert "≥1 approving review required" in names
        assert "force push blocked" in names
        assert "deletion blocked" in names
        assert "admin bypass blocked" in names
        assert result.warnings == []

    def test_missing_branch_protection_fails(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        # Should short-circuit; no point reporting sub-rules
        prot_check = next(c for c in result.checks if c.name == "branch protection exists")
        assert not prot_check.passed
        assert "no branch protection rule" in prot_check.detail.lower()

    def test_repo_not_found_short_circuits(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(404),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        read = next(c for c in result.checks if c.name == "read access")
        assert "404" in read.detail

    def test_zero_required_approvals_fails(self, monkeypatch):
        prot = _good_protection()
        prot["required_pull_request_reviews"] = {"required_approving_review_count": 0}
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, prot),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        approvals = next(c for c in result.checks if c.name == "≥1 approving review required")
        assert not approvals.passed
        assert "= 0" in approvals.detail

    def test_force_push_allowed_fails(self, monkeypatch):
        prot = _good_protection()
        prot["allow_force_pushes"] = {"enabled": True}
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, prot),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        check = next(c for c in result.checks if c.name == "force push blocked")
        assert not check.passed

    def test_admin_bypass_allowed_fails(self, monkeypatch):
        prot = _good_protection()
        prot["enforce_admins"] = {"enabled": False}
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, prot),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        check = next(c for c in result.checks if c.name == "admin bypass blocked")
        assert not check.passed

    def test_codeowners_present_adds_note(self, monkeypatch):
        """CODEOWNERS is a POSITIVE signal (note), not a warning.

        The "reviewer-as-pre-screener" pivot makes CODEOWNERS the production
        review model — humans gate merges, the reviewer agent pre-screens.
        """
        responses = {
            "/repos/owner/repo/contents/CODEOWNERS": _FakeResponse(200, {"path": "CODEOWNERS"}),
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        # CODEOWNERS lives in notes now, not warnings
        assert any("CODEOWNERS" in n for n in result.notes)
        assert not any("CODEOWNERS" in w for w in result.warnings)
        # The note explicitly calls out the "by design" framing
        assert any("by design" in n for n in result.notes)

    def test_required_signatures_adds_warning(self, monkeypatch):
        prot = _good_protection()
        prot["required_signatures"] = {"enabled": True}
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, prot),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        assert any("required_signatures" in w for w in result.warnings)

    def test_required_status_checks_adds_warning(self, monkeypatch):
        prot = _good_protection()
        prot["required_status_checks"] = {"contexts": ["ci/build", "ci/test"], "checks": []}
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, prot),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        assert any("required status check" in w for w in result.warnings)

    def test_app_auth_requires_installation_match(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/installation/repositories": _FakeResponse(
                200, {"repositories": [{"full_name": "owner/repo"}]}
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "123")

        result = verify("github.com/owner/repo", token="ghs_fake", auth_mode="app")
        assert result.passed
        assert result.auth_identity == "app:installation:123"

    def test_app_auth_fails_if_not_in_installation(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/installation/repositories": _FakeResponse(
                200, {"repositories": [{"full_name": "other/elsewhere"}]}
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghs_fake", auth_mode="app")
        assert not result.passed
        check = next(c for c in result.checks if c.name == "App installation covers this repo")
        assert not check.passed
        assert "not in installation's repo list" in check.detail

    def test_empty_token_raises(self):
        with pytest.raises(ValueError):
            verify("github.com/owner/repo", token="")

    def test_non_default_default_branch(self, monkeypatch):
        """Repos using 'master' or anything else must be detected, not assumed."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta(default_branch="master")),
            "/repos/owner/repo/branches/master/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        assert result.default_branch == "master"


# --------------------------- format_result_lines ---------------------------


class TestFormatResultLines:
    def test_passing_result(self):
        from orchestrator.models import CheckResult, VerificationResult

        result = VerificationResult(
            repo_url="https://github.com/owner/repo",
            default_branch="main",
            auth_mode="pat",
            auth_identity="user:tester",
            checks=[
                CheckResult("read access", True),
                CheckResult("write access", True),
            ],
        )
        lines = repo_verify.format_result_lines(result)
        assert lines[0].startswith("✓")
        assert "owner/repo" in lines[0]
        assert any("default branch: main" in line for line in lines)
        assert any("authenticated as: user:tester (pat)" in line for line in lines)

    def test_failing_result_shows_details(self):
        from orchestrator.models import CheckResult, VerificationResult

        result = VerificationResult(
            repo_url="https://github.com/owner/repo",
            checks=[CheckResult("branch protection exists", False, "no rule on main")],
        )
        lines = repo_verify.format_result_lines(result)
        assert lines[0].startswith("✗")
        # detail should appear inline
        assert any("no rule on main" in line for line in lines)

    def test_warnings_section(self):
        from orchestrator.models import CheckResult, VerificationResult

        result = VerificationResult(
            repo_url="https://github.com/o/r",
            checks=[CheckResult("read access", True)],
            warnings=["CODEOWNERS present at .github/CODEOWNERS"],
        )
        lines = repo_verify.format_result_lines(result)
        assert any("warnings" in line.lower() for line in lines)
        assert any("CODEOWNERS" in line for line in lines)
