"""Tests for orchestrator/repo_verify.py — URL normalization + policy checks."""

from __future__ import annotations

import httpx
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

    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers if headers is not None else {}

    def json(self):
        return self._json


def _make_client(responses: dict[str, _FakeResponse], raise_for=None):
    """Build a fake httpx.Client that returns canned responses by URL-suffix match.

    A response key matches if it is a strict suffix of the request URL's path
    (query string ignored). That way `/repos/o/r` does NOT accidentally match
    `/repos/o/r/branches/main/protection` or `/repos/o/r/contents/CODEOWNERS` —
    the test must explicitly mock those longer URLs (or leave them to fall
    through to the default 404).

    `raise_for` is an optional mapping of URL-suffix -> Exception instance to
    raise from .get() when that suffix matches; lets tests exercise the
    transport-error path of pre-flight probes without needing a real socket.
    """
    raise_map = raise_for or {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, **kw):
            path = url.split("?", 1)[0]
            for key, exc in raise_map.items():
                if path.endswith(key):
                    raise exc
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


# --------------------------- pre-flight probes (F-005-U-3) ---------------------------


class TestPreflightProbes:
    """WARN-only probes for the planning-time-catchable BLOCKED reasons.

    Each probe emits a slug-tagged WARN; none of them should ever flip a
    pass/fail check or block verification. The slugs match the reason
    taxonomy used by escalation summaries / ntfy bodies.
    """

    def test_feature_branch_pr_rule_warns_with_slug(self, monkeypatch):
        """`pull_request` rule on a feature-branch ruleset -> WARN, not FAIL."""
        rule_payload = [
            {"type": "pull_request", "ruleset_id": 42, "ruleset_source": "ORG"},
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
            "/rules/branches/agent-orchestrator-preflight": _FakeResponse(200, rule_payload),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")

        # Probe is WARN-only; the underlying repo still passes.
        assert result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH in slugs
        # Warning message names the offending API surface so the lead can
        # diagnose without running the coder.
        bp_warn = next(
            w
            for w in result.warnings
            if repo_verify.extract_warning_slug(w)
            == repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH
        )
        assert "pull request" in bp_warn.lower()

    def test_feature_branch_no_pr_rule_no_warning(self, monkeypatch):
        """Empty / unrelated rule list -> no branch-protection WARN."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
            "/rules/branches/agent-orchestrator-preflight": _FakeResponse(
                200, [{"type": "non_fast_forward"}]
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH not in slugs

    def test_feature_branch_endpoint_unsupported_no_warning(self, monkeypatch):
        """Non-200 from the Rules API is not a probe failure — skip silently."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
            # Rules endpoint returns 404 (default in _make_client) — probe
            # must NOT warn.
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH not in slugs

    def test_pat_missing_repo_scope_warns_auth_failure(self, monkeypatch):
        """Classic PAT with `read:org` only -> auth_failure WARN, not FAIL."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(
                200,
                {"login": "owner"},
                headers={"X-OAuth-Scopes": "read:org, gist"},
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed  # WARN only
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_AUTH_FAILURE in slugs

    def test_pat_with_repo_scope_no_warning(self, monkeypatch):
        """`repo` scope present -> no auth_failure WARN."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(
                200,
                {"login": "owner"},
                headers={"X-OAuth-Scopes": "repo, workflow"},
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_AUTH_FAILURE not in slugs

    def test_pat_with_public_repo_scope_no_warning(self, monkeypatch):
        """`public_repo` is sufficient for public-only repos -> no WARN."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(
                200,
                {"login": "owner"},
                headers={"X-OAuth-Scopes": "public_repo"},
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_AUTH_FAILURE not in slugs

    def test_fine_grained_pat_empty_scopes_no_warning(self, monkeypatch):
        """Fine-grained PATs return empty X-OAuth-Scopes — probe must skip."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(
                200,
                {"login": "owner"},
                headers={"X-OAuth-Scopes": ""},
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_AUTH_FAILURE not in slugs

    def test_app_auth_skips_scope_probe(self, monkeypatch):
        """App tokens don't expose OAuth scopes — probe must not warn."""
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
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_AUTH_FAILURE not in slugs

    def test_default_branch_network_error_warns(self, monkeypatch):
        """Transport error on /branches/<default> -> network_error WARN, not FAIL.

        The verify() main flow still completes — branch-protection check
        happens on a separate URL — so this exercises the probe in isolation.
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        # Raise on the reachability probe's GET, but let the protection
        # call (a longer URL ending in /protection) succeed.
        boom = httpx.ConnectError("DNS resolution failed for api.github.com")
        client_cls = _make_client(responses, raise_for={"/branches/main": boom})
        monkeypatch.setattr("httpx.Client", client_cls)

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed  # WARN only
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_NETWORK_ERROR in slugs

    def test_default_branch_reachable_no_warning(self, monkeypatch):
        """Successful reachability probe -> no network_error WARN."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main": _FakeResponse(200, {"name": "main"}),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_NETWORK_ERROR not in slugs

    def test_probes_are_never_blocking(self, monkeypatch):
        """All three probes firing simultaneously must NOT flip passed=False."""
        rule_payload = [{"type": "pull_request"}]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(
                200,
                {"login": "owner"},
                headers={"X-OAuth-Scopes": "gist"},  # missing repo
            ),
            "/rules/branches/agent-orchestrator-preflight": _FakeResponse(200, rule_payload),
        }
        boom = httpx.ConnectTimeout("timeout")
        client_cls = _make_client(responses, raise_for={"/branches/main": boom})
        monkeypatch.setattr("httpx.Client", client_cls)

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed  # all probes are WARN-only
        slugs = {repo_verify.extract_warning_slug(w) for w in result.warnings}
        assert repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH in slugs
        assert repo_verify.REASON_AUTH_FAILURE in slugs
        assert repo_verify.REASON_NETWORK_ERROR in slugs


class TestSlugHelpers:
    """Tag/extract round-trip for slug-prefixed warnings."""

    def test_extract_known_slug(self):
        warn = f"[{repo_verify.REASON_AUTH_FAILURE}] PAT scopes [gist] do not include..."
        assert repo_verify.extract_warning_slug(warn) == repo_verify.REASON_AUTH_FAILURE

    def test_extract_unknown_slug_returns_none(self):
        """Bare bracketed text must not be classified as a slug."""
        assert repo_verify.extract_warning_slug("[unknown_thing] msg") is None

    def test_extract_no_brackets_returns_none(self):
        assert repo_verify.extract_warning_slug("plain warning text") is None

    def test_extract_malformed_returns_none(self):
        assert repo_verify.extract_warning_slug("[") is None
        assert repo_verify.extract_warning_slug("[]") is None

    def test_known_slugs_have_hints(self):
        """Every reason slug must have a fix-it hint."""
        for slug in repo_verify.PREFLIGHT_REASON_SLUGS:
            assert slug in repo_verify.PREFLIGHT_HINTS
            assert repo_verify.PREFLIGHT_HINTS[slug]  # non-empty


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

    def test_hint_table_renders_when_tagged_warning_present(self):
        """A tagged warning triggers a `pre-flight hints` section."""
        from orchestrator.models import CheckResult, VerificationResult

        tagged = f"[{repo_verify.REASON_AUTH_FAILURE}] PAT scopes missing"
        result = VerificationResult(
            repo_url="https://github.com/o/r",
            checks=[CheckResult("read access", True)],
            warnings=[tagged],
        )
        lines = repo_verify.format_result_lines(result)
        joined = "\n".join(lines)
        assert "pre-flight hints" in joined.lower()
        assert repo_verify.REASON_AUTH_FAILURE in joined
        # Hint copy from PREFLIGHT_HINTS is rendered too
        assert any(
            repo_verify.PREFLIGHT_HINTS[repo_verify.REASON_AUTH_FAILURE][:20] in line
            for line in lines
        )

    def test_hint_table_skipped_for_untagged_warnings(self):
        """Legacy warnings (no slug prefix) don't trigger the hint table."""
        from orchestrator.models import CheckResult, VerificationResult

        result = VerificationResult(
            repo_url="https://github.com/o/r",
            checks=[CheckResult("read access", True)],
            warnings=["required_signatures is on — agent commits aren't signed"],
        )
        lines = repo_verify.format_result_lines(result)
        joined = "\n".join(lines).lower()
        assert "pre-flight hints" not in joined

    def test_hint_table_dedupes_repeated_slugs(self):
        """Two warnings with the same slug -> hint table renders the slug once."""
        from orchestrator.models import CheckResult, VerificationResult

        slug = repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH
        result = VerificationResult(
            repo_url="https://github.com/o/r",
            checks=[CheckResult("read access", True)],
            warnings=[f"[{slug}] msg one", f"[{slug}] msg two"],
        )
        lines = repo_verify.format_result_lines(result)
        # Count rows in the hint section that start with `• <slug>:`
        slug_rows = [line for line in lines if line.lstrip().startswith(f"• {slug}:")]
        assert len(slug_rows) == 1
