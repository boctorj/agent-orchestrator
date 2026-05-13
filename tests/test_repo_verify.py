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


# --------------------------- ruleset path (F-003-U-1) ---------------------------


def _good_ruleset_rules(ruleset_id: int = 42) -> list[dict]:
    """A rules list returned by /rules/branches/{branch} that satisfies policy."""
    return [
        {
            "type": "pull_request",
            "ruleset_id": ruleset_id,
            "ruleset_source_type": "Repository",
            "parameters": {"required_approving_review_count": 1},
        },
        {"type": "non_fast_forward", "ruleset_id": ruleset_id},
        {"type": "deletion", "ruleset_id": ruleset_id},
    ]


def _good_ruleset_detail(ruleset_id: int = 42, bypass_actors: list[dict] | None = None) -> dict:
    """A ruleset detail JSON returned by /rulesets/{id}."""
    return {
        "id": ruleset_id,
        "name": "main protection",
        "enforcement": "active",
        "bypass_actors": bypass_actors or [],
    }


class TestVerifyRulesetSupport:
    """F-003-U-1: verify_repo should accept a Ruleset that satisfies policy.

    Either the classic /branches/{branch}/protection API or the modern
    /rules/branches/{branch} (Rulesets) API may carry the protection — the
    orchestrator's policy is satisfied if EITHER system passes. The verifier
    output gains a `source: classic|ruleset` annotation on the
    "branch protection exists" check so the user knows which system passed.
    """

    def test_ruleset_only_passes_with_source_ruleset(self, monkeypatch):
        """No classic protection, but a ruleset satisfies policy → green."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules()),
            "/repos/owner/repo/rulesets/42": _FakeResponse(200, _good_ruleset_detail()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        prot_check = next(c for c in result.checks if c.name == "branch protection exists")
        assert prot_check.passed
        assert "ruleset" in prot_check.detail.lower()
        # And the sub-checks derived from the ruleset must be present
        names = [c.name for c in result.checks]
        assert "≥1 approving review required" in names
        assert "force push blocked" in names
        assert "deletion blocked" in names
        assert "admin bypass blocked" in names

    def test_classic_only_pass_shows_source_classic(self, monkeypatch):
        """Existing classic-only path now annotates source: classic on the check."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        prot_check = next(c for c in result.checks if c.name == "branch protection exists")
        assert "classic" in prot_check.detail.lower()

    def test_both_present_prefers_classic_for_display(self, monkeypatch):
        """If classic AND ruleset both satisfy policy, surface classic."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules()),
            "/repos/owner/repo/rulesets/42": _FakeResponse(200, _good_ruleset_detail()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        prot_check = next(c for c in result.checks if c.name == "branch protection exists")
        assert "classic" in prot_check.detail.lower()
        assert "ruleset" not in prot_check.detail.lower()

    def test_neither_classic_nor_ruleset_fails(self, monkeypatch):
        """Classic 404 + empty ruleset list → no protection at all."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, []),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        prot_check = next(c for c in result.checks if c.name == "branch protection exists")
        assert not prot_check.passed
        # Detail should mention the absence — classic test asserted this same substring
        assert "no branch protection" in prot_check.detail.lower()

    def test_ruleset_with_zero_approvals_fails(self, monkeypatch):
        bad_rules = [
            {
                "type": "pull_request",
                "ruleset_id": 7,
                "parameters": {"required_approving_review_count": 0},
            },
            {"type": "non_fast_forward", "ruleset_id": 7},
            {"type": "deletion", "ruleset_id": 7},
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, bad_rules),
            "/repos/owner/repo/rulesets/7": _FakeResponse(200, _good_ruleset_detail(7)),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        approvals = next(c for c in result.checks if c.name == "≥1 approving review required")
        assert not approvals.passed
        assert "= 0" in approvals.detail

    def test_ruleset_missing_non_fast_forward_fails(self, monkeypatch):
        """A ruleset with no non_fast_forward rule → force-push not blocked."""
        bad_rules = [
            {
                "type": "pull_request",
                "ruleset_id": 9,
                "parameters": {"required_approving_review_count": 1},
            },
            {"type": "deletion", "ruleset_id": 9},
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, bad_rules),
            "/repos/owner/repo/rulesets/9": _FakeResponse(200, _good_ruleset_detail(9)),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        force_check = next(c for c in result.checks if c.name == "force push blocked")
        assert not force_check.passed

    def test_ruleset_missing_deletion_rule_fails(self, monkeypatch):
        bad_rules = [
            {
                "type": "pull_request",
                "ruleset_id": 10,
                "parameters": {"required_approving_review_count": 1},
            },
            {"type": "non_fast_forward", "ruleset_id": 10},
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, bad_rules),
            "/repos/owner/repo/rulesets/10": _FakeResponse(200, _good_ruleset_detail(10)),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        del_check = next(c for c in result.checks if c.name == "deletion blocked")
        assert not del_check.passed

    def test_ruleset_with_admin_bypass_actor_fails(self, monkeypatch):
        """A bypass actor at admin tier (RepositoryRole id=5) disqualifies."""
        rs_detail = _good_ruleset_detail(
            ruleset_id=11,
            bypass_actors=[
                {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
            ],
        )
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules(11)),
            "/repos/owner/repo/rulesets/11": _FakeResponse(200, rs_detail),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        bypass_check = next(c for c in result.checks if c.name == "admin bypass blocked")
        assert not bypass_check.passed
        assert "bypass" in bypass_check.detail.lower()

    def test_ruleset_with_maintain_bypass_actor_fails(self, monkeypatch):
        """A bypass actor at maintain tier (RepositoryRole id=4) also disqualifies."""
        rs_detail = _good_ruleset_detail(
            ruleset_id=12,
            bypass_actors=[
                {"actor_id": 4, "actor_type": "RepositoryRole", "bypass_mode": "always"}
            ],
        )
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules(12)),
            "/repos/owner/repo/rulesets/12": _FakeResponse(200, rs_detail),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        bypass_check = next(c for c in result.checks if c.name == "admin bypass blocked")
        assert not bypass_check.passed

    def test_ruleset_with_write_tier_bypass_does_not_disqualify(self, monkeypatch):
        """write-tier (RepositoryRole id=3) bypass is below the cut-off → still passes.

        The orchestrator policy only blocks maintain/admin tier bypass actors;
        a write-tier bypass is informational, not disqualifying.
        """
        rs_detail = _good_ruleset_detail(
            ruleset_id=13,
            bypass_actors=[
                {"actor_id": 3, "actor_type": "RepositoryRole", "bypass_mode": "always"}
            ],
        )
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules(13)),
            "/repos/owner/repo/rulesets/13": _FakeResponse(200, rs_detail),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed

    def test_ruleset_source_annotation_in_formatted_output(self, monkeypatch):
        """The `source: classic|ruleset` annotation is visible in formatted output."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules()),
            "/repos/owner/repo/rulesets/42": _FakeResponse(200, _good_ruleset_detail()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        lines = repo_verify.format_result_lines(result)
        assert any("branch protection exists" in line and "ruleset" in line for line in lines)


# --------------------------- F-003-U-1 tester triangulation ---------------------------
#
# These tests were written by the tester agent independently of the coder's
# tests above. They re-assert the unit description's matrix from a separate
# angle and add two cases the coder's tests don't cover:
#   - classic-fails-but-ruleset-passes  (description: "Accept either system if
#     it satisfies the orchestrator policy")
#   - the /rules/branches/{branch} endpoint is actually queried (regression
#     against silently skipping the new API call)


class _RecordingClient:
    """httpx.Client double that records every URL requested.

    Used to assert that verify() actually hits /rules/branches/{branch}, not
    just to canned-response the URL.
    """

    requested_urls: list[str] = []

    def __init__(self, *args, **kwargs):
        self.headers = kwargs.get("headers", {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # Per-instance request log lives in the class so it can be inspected
    # after the `with` block exits.
    @classmethod
    def reset(cls):
        cls.requested_urls = []


def _recording_client_factory(responses: dict[str, _FakeResponse]):
    """Build a recording httpx.Client double that returns canned responses."""

    class RecordingClient(_RecordingClient):
        def get(self, url, params=None, **kw):
            _RecordingClient.requested_urls.append(url)
            path = url.split("?", 1)[0]
            for key, resp in responses.items():
                if path.endswith(key):
                    return resp
            return _FakeResponse(404)

    _RecordingClient.reset()
    return RecordingClient


class TestF003U1RulesetTesterTriangulation:
    """F-003-U-1 — independently re-check the description's matrix.

    The unit description is the source of truth: verify_repo must also query
    /rules/branches/{branch}, accept either system that satisfies policy
    (pull_request rule + ≥1 approval, non_fast_forward, deletion, no
    maintain/admin bypass), and annotate the "branch protection exists"
    check with `source: classic` or `source: ruleset`. Both present →
    prefer classic for display.
    """

    # --- 1. classic-only pass ---

    def test_classic_only_path_passes_and_shows_source_classic(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        prot = next(c for c in result.checks if c.name == "branch protection exists")
        # Description: "source: classic | ruleset" annotation
        assert prot.detail == "source: classic"

    # --- 2. ruleset-only pass ---

    def test_ruleset_only_path_passes_and_shows_source_ruleset(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules()),
            "/repos/owner/repo/rulesets/42": _FakeResponse(200, _good_ruleset_detail()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        prot = next(c for c in result.checks if c.name == "branch protection exists")
        assert prot.detail == "source: ruleset"
        # All four policy sub-checks must be derived from the ruleset:
        sub_names = {c.name for c in result.checks}
        for required in (
            "≥1 approving review required",
            "force push blocked",
            "deletion blocked",
            "admin bypass blocked",
        ):
            assert required in sub_names, f"{required} sub-check missing"

    # --- 3. both present — prefer classic for display ---

    def test_both_systems_present_classic_is_displayed(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules()),
            "/repos/owner/repo/rulesets/42": _FakeResponse(200, _good_ruleset_detail()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed
        prot = next(c for c in result.checks if c.name == "branch protection exists")
        # Description: "If both exist, prefer classic for display;
        # report ruleset only when classic is absent."
        assert prot.detail == "source: classic"

    # --- 4. neither (still fails) ---

    def test_neither_classic_nor_ruleset_still_fails(self, monkeypatch):
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, []),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        prot = next(c for c in result.checks if c.name == "branch protection exists")
        assert not prot.passed
        # No sub-checks should be appended when neither system applies — the
        # verifier short-circuits because policy is meaningless.
        assert not any(c.name == "≥1 approving review required" for c in result.checks)

    def test_neither_when_rules_endpoint_404s(self, monkeypatch):
        """Some accounts may 404 on /rules/branches; that path must also fail."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(404),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        prot = next(c for c in result.checks if c.name == "branch protection exists")
        assert not prot.passed

    # --- 5. ruleset with approvals=0 (fails) ---

    def test_ruleset_pull_request_rule_with_zero_approvals_fails(self, monkeypatch):
        rules = [
            {
                "type": "pull_request",
                "ruleset_id": 100,
                "parameters": {"required_approving_review_count": 0},
            },
            {"type": "non_fast_forward", "ruleset_id": 100},
            {"type": "deletion", "ruleset_id": 100},
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, rules),
            "/repos/owner/repo/rulesets/100": _FakeResponse(200, _good_ruleset_detail(100)),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        approvals = next(c for c in result.checks if c.name == "≥1 approving review required")
        assert not approvals.passed
        # state.py regex parses "= N" out of detail — must use that exact form
        assert "= 0" in approvals.detail

    def test_ruleset_with_no_pull_request_rule_fails(self, monkeypatch):
        """A ruleset that omits the pull_request rule entirely doesn't satisfy
        the description's "pull_request rule present with required_approving_review_count >= 1"
        requirement."""
        rules = [
            {"type": "non_fast_forward", "ruleset_id": 101},
            {"type": "deletion", "ruleset_id": 101},
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, rules),
            "/repos/owner/repo/rulesets/101": _FakeResponse(200, _good_ruleset_detail(101)),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        approvals = next(c for c in result.checks if c.name == "≥1 approving review required")
        assert not approvals.passed

    # --- 6. ruleset with bypass actor (fails) — admin AND maintain ---

    def test_ruleset_admin_tier_bypass_actor_disqualifies(self, monkeypatch):
        rs_detail = _good_ruleset_detail(
            ruleset_id=200,
            bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole"}],
        )
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules(200)),
            "/repos/owner/repo/rulesets/200": _FakeResponse(200, rs_detail),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        bypass = next(c for c in result.checks if c.name == "admin bypass blocked")
        assert not bypass.passed

    def test_ruleset_maintain_tier_bypass_actor_disqualifies(self, monkeypatch):
        rs_detail = _good_ruleset_detail(
            ruleset_id=201,
            bypass_actors=[{"actor_id": 4, "actor_type": "RepositoryRole"}],
        )
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules(201)),
            "/repos/owner/repo/rulesets/201": _FakeResponse(200, rs_detail),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        bypass = next(c for c in result.checks if c.name == "admin bypass blocked")
        assert not bypass.passed

    # --- 7. ruleset missing non_fast_forward (fails) ---

    def test_ruleset_missing_non_fast_forward_rule_fails(self, monkeypatch):
        rules = [
            {
                "type": "pull_request",
                "ruleset_id": 300,
                "parameters": {"required_approving_review_count": 2},
            },
            {"type": "deletion", "ruleset_id": 300},
            # no non_fast_forward
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, rules),
            "/repos/owner/repo/rulesets/300": _FakeResponse(200, _good_ruleset_detail(300)),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        force = next(c for c in result.checks if c.name == "force push blocked")
        assert not force.passed
        # Approvals & deletion should still pass — only force-push fails
        approvals = next(c for c in result.checks if c.name == "≥1 approving review required")
        deletion = next(c for c in result.checks if c.name == "deletion blocked")
        assert approvals.passed
        assert deletion.passed

    # --- Additional tester-discovered cases ---

    def test_rules_branches_endpoint_is_actually_queried(self, monkeypatch):
        """Regression: the new /rules/branches/{branch} endpoint must be hit.

        Earlier verify_repo only called /branches/{branch}/protection. This
        test would have caught the bug from F-001 onboarding where the
        ruleset went undetected.
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(404),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules()),
            "/repos/owner/repo/rulesets/42": _FakeResponse(200, _good_ruleset_detail()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _recording_client_factory(responses))

        verify("github.com/owner/repo", token="ghp_fake")
        urls = _RecordingClient.requested_urls
        assert any("/rules/branches/main" in u for u in urls), (
            f"verify() never queried /rules/branches/main; URLs hit: {urls}"
        )

    def test_classic_fails_but_ruleset_passes_overall_passes(self, monkeypatch):
        """Description: "Accept either system if it satisfies the orchestrator policy."

        Classic protection exists but is misconfigured (approvals=0). A
        properly-configured ruleset also exists. Overall verification must
        pass, with the ruleset chosen as the display source.
        """
        bad_classic = _good_protection()
        bad_classic["required_pull_request_reviews"] = {"required_approving_review_count": 0}
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, bad_classic),
            "/repos/owner/repo/rules/branches/main": _FakeResponse(200, _good_ruleset_rules()),
            "/repos/owner/repo/rulesets/42": _FakeResponse(200, _good_ruleset_detail()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed, (
            f"verify should pass when ruleset satisfies policy even if classic doesn't; "
            f"failed checks: {result.failure_summary}"
        )
        prot = next(c for c in result.checks if c.name == "branch protection exists")
        assert prot.detail == "source: ruleset", (
            "When classic exists but fails policy and ruleset passes, display "
            "source must be 'ruleset' so the user can see which system carried it."
        )

    def test_format_result_lines_renders_source_in_parens(self, monkeypatch):
        """Description gives the exact line shape:
        '✓ branch protection exists (source: classic | ruleset)'
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        lines = repo_verify.format_result_lines(result)
        prot_lines = [line for line in lines if "branch protection exists" in line and "✓" in line]
        assert prot_lines, f"no formatted branch-protection line found in: {lines}"
        # Must show parenthesized source annotation, not a bullet "·"
        assert any("(source: classic)" in line for line in prot_lines), (
            f"expected '(source: classic)' in formatted line; got: {prot_lines}"
        )


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


# --------------------------- format_result_lines hint-table tests (F-005-U-3) ---------------------------


class TestFormatResultLinesHintTable:
    """The hint-table renders fix-it copy keyed off slug-prefixed warnings."""

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
        client_cls = _make_client(responses, raise_for={"/repos/owner/repo/branches/main": boom})
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
        client_cls = _make_client(responses, raise_for={"/repos/owner/repo/branches/main": boom})
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


# --------------------------- F-005-U-3 intended-behavior coverage ---------------------------
#
# The coder's TestPreflightProbes class covers the happy-path firing of each
# probe. This class adds tests for behaviors that follow from the unit
# description (F-005-U-3) but aren't yet pinned down: non-pre-flightable
# reasons explicitly excluded, exact taxonomy slug values, probe URL targeting
# a feature-branch (not default-branch) name, defensive handling of API
# anomalies, short-circuit safety, and public API surface stability.


class TestPreflightTaxonomyContract:
    """The slug taxonomy is shared with escalation summaries + ntfy bodies.

    These tests pin the contract so a future rename / drift in repo_verify
    doesn't silently desync the other surfaces that import from it.
    """

    def test_non_preflightable_reasons_are_not_in_slug_set(self):
        """Spec: probes do NOT cover disk_full or rate_limited.

        These reasons exist in the BLOCKED-reason taxonomy but are
        deliberately non-pre-flightable — verify_repo can't predict
        runtime disk pressure or future API rate state.
        """
        assert "disk_full" not in repo_verify.PREFLIGHT_REASON_SLUGS
        assert "rate_limited" not in repo_verify.PREFLIGHT_REASON_SLUGS
        # And no hint for them either — symmetric absence.
        assert "disk_full" not in repo_verify.PREFLIGHT_HINTS
        assert "rate_limited" not in repo_verify.PREFLIGHT_HINTS

    def test_slug_constants_match_exact_taxonomy_strings(self):
        """Cross-surface contract: the slug string is the canonical reason name.

        Escalation summaries and ntfy bodies match on these strings; if
        they change, the dashboard hint table desyncs.
        """
        assert repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH == "branch_protection_blocked_push"
        assert repo_verify.REASON_AUTH_FAILURE == "auth_failure"
        assert repo_verify.REASON_NETWORK_ERROR == "network_error"

    def test_preflight_slug_set_is_immutable(self):
        """Frozenset, so a caller can't accidentally mutate the shared registry."""
        assert isinstance(repo_verify.PREFLIGHT_REASON_SLUGS, frozenset)

    def test_preflight_slug_set_has_exactly_three_entries(self):
        """The pre-flightable subset is a closed list of three (per the unit)."""
        assert len(repo_verify.PREFLIGHT_REASON_SLUGS) == 3

    def test_preflight_hints_has_no_orphan_keys(self):
        """Every key in PREFLIGHT_HINTS must be a known slug.

        Reverses the existing `test_known_slugs_have_hints` check — together
        they assert PREFLIGHT_HINTS.keys() == PREFLIGHT_REASON_SLUGS.
        """
        for k in repo_verify.PREFLIGHT_HINTS:
            assert k in repo_verify.PREFLIGHT_REASON_SLUGS, (
                f"PREFLIGHT_HINTS has a key {k!r} that isn't in PREFLIGHT_REASON_SLUGS"
            )

    def test_public_api_exports_preflight_symbols(self):
        """Pre-flight surface must be importable from `repo_verify` directly.

        Escalation/ntfy code paths will `from orchestrator.repo_verify import
        PREFLIGHT_HINTS, REASON_*`; pinning __all__ guards that import.
        """
        all_names = set(repo_verify.__all__)
        assert "PREFLIGHT_HINTS" in all_names
        assert "PREFLIGHT_REASON_SLUGS" in all_names
        assert "REASON_BRANCH_PROTECTION_BLOCKED_PUSH" in all_names
        assert "REASON_AUTH_FAILURE" in all_names
        assert "REASON_NETWORK_ERROR" in all_names
        assert "extract_warning_slug" in all_names


class TestPreflightProbeBehaviorEdges:
    """Edge-case behavior of each probe that goes beyond the happy path."""

    def test_branch_protection_probe_targets_non_default_branch(self, monkeypatch):
        """Spec: probe is for *feature*-branch push permissions on non-main branches.

        Must hit a synthetic ref name (not the default branch). Probing the
        default branch wouldn't catch a wildcard feature-branch ruleset.
        """
        seen_urls: list[str] = []

        class CapturingClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None, **kw):
                seen_urls.append(url)
                path = url.split("?", 1)[0]
                routes = {
                    "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
                    "/repos/owner/repo/branches/main/protection": _FakeResponse(
                        200, _good_protection()
                    ),
                    "/user": _FakeResponse(200, {"login": "owner"}),
                }
                for key, resp in routes.items():
                    if path.endswith(key):
                        return resp
                return _FakeResponse(404)

        monkeypatch.setattr("httpx.Client", CapturingClient)

        verify("github.com/owner/repo", token="ghp_fake")

        # At least one Rules API call must have happened.
        rules_calls = [u for u in seen_urls if "/rules/branches/" in u]
        assert rules_calls, (
            "branch-protection probe must hit /rules/branches/<name>; saw URLs: " + str(seen_urls)
        )
        # The branch-protection probe (distinct from the ruleset-eval call
        # that legitimately targets the default branch) must hit a synthetic,
        # non-default ref. Filter out the eval call on the default branch
        # and assert at least one probe call targets a non-default branch.
        probe_calls = [
            u for u in rules_calls if u.rsplit("/rules/branches/", 1)[1].split("?", 1)[0] != "main"
        ]
        assert probe_calls, (
            "branch-protection probe must target a non-default branch; "
            f"saw only default-branch calls in rules_calls={rules_calls}"
        )
        for u in probe_calls:
            tail = u.rsplit("/rules/branches/", 1)[1].split("?", 1)[0]
            assert tail, "branch-protection probe target ref must be non-empty"

    def test_branch_protection_probe_dedupes_multiple_pr_rules(self, monkeypatch):
        """Multiple `pull_request` rules in one payload -> one WARN, not N."""
        rule_payload = [
            {"type": "pull_request", "ruleset_id": 1, "ruleset_source": "ORG"},
            {"type": "pull_request", "ruleset_id": 2, "ruleset_source": "REPO"},
            {"type": "pull_request", "ruleset_id": 3, "ruleset_source": "ORG"},
        ]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
            "/rules/branches/agent-orchestrator-preflight": _FakeResponse(200, rule_payload),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        bp_warns = [
            w
            for w in result.warnings
            if repo_verify.extract_warning_slug(w)
            == repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH
        ]
        assert len(bp_warns) == 1, (
            f"expected exactly one branch_protection_blocked_push WARN, got {len(bp_warns)}: "
            f"{bp_warns}"
        )

    def test_branch_protection_probe_empty_list_no_warning(self, monkeypatch):
        """No feature-branch rulesets configured at all -> no WARN."""
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
            "/rules/branches/agent-orchestrator-preflight": _FakeResponse(200, []),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH not in slugs

    def test_branch_protection_probe_403_silently_skips(self, monkeypatch):
        """A 403 from the Rules API (token can't read rulesets) -> probe skips silently.

        Defensive: missing scope on the Rules endpoint shouldn't itself
        manifest as a misleading branch_protection_blocked_push WARN.
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
            "/rules/branches/agent-orchestrator-preflight": _FakeResponse(403),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_BRANCH_PROTECTION_BLOCKED_PUSH not in slugs

    def test_network_probe_ignores_http_5xx(self, monkeypatch):
        """A 5xx HTTP response on /branches/<default> is NOT a transport error.

        Spec: network_error covers *transport-layer* failure (DNS / refused /
        TLS / timeout) — HTTP error codes are surfaced through the pass/fail
        checks already.
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main": _FakeResponse(500, {"message": "ise"}),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_NETWORK_ERROR not in slugs

    def test_network_probe_handles_read_timeout(self, monkeypatch):
        """Any httpx.RequestError subclass triggers network_error WARN.

        ReadTimeout is a transport-layer failure (slow / unreachable host)
        distinct from a slow-but-eventual HTTP response.
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(200, {"login": "owner"}),
        }
        boom = httpx.ReadTimeout("timed out reading from api.github.com")
        client_cls = _make_client(responses, raise_for={"/repos/owner/repo/branches/main": boom})
        monkeypatch.setattr("httpx.Client", client_cls)

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert result.passed  # WARN-only
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        assert repo_verify.REASON_NETWORK_ERROR in slugs

    def test_auth_failure_warn_names_missing_scope_and_present_scopes(self, monkeypatch):
        """auth_failure WARN must be actionable: name which scopes are present.

        Without that, the lead can't tell which PAT got loaded.
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(
                200,
                {"login": "owner"},
                headers={"X-OAuth-Scopes": "gist, read:user"},
            ),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        auth_warns = [
            w
            for w in result.warnings
            if repo_verify.extract_warning_slug(w) == repo_verify.REASON_AUTH_FAILURE
        ]
        assert len(auth_warns) == 1
        # WARN mentions the missing required scope name.
        assert "repo" in auth_warns[0]
        # WARN includes one of the present scopes so the user knows what
        # PAT is loaded (don't ship a probe whose output is just "broken").
        assert "gist" in auth_warns[0] or "read:user" in auth_warns[0]


class TestPreflightShortCircuit:
    """When verify() short-circuits early, probes that come after must not fire."""

    def test_probes_skipped_when_repo_not_found(self, monkeypatch):
        """A 404 on /repos/<o>/<r> short-circuits — no probes ran, no slug WARNs.

        Spec: WARN does not block, but neither should it appear when there's
        nothing to probe yet (the token couldn't even see the repo). Bare
        probe output in that case is noise, not signal.
        """
        responses = {
            "/repos/owner/repo": _FakeResponse(404),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")
        assert not result.passed
        slugs = [repo_verify.extract_warning_slug(w) for w in result.warnings]
        # None of the slugs fired — verify() returned before the probes ran.
        for s in repo_verify.PREFLIGHT_REASON_SLUGS:
            assert s not in slugs, f"probe slug {s} fired despite repo-404 short-circuit"


class TestPreflightDoesNotBlockSpawnGating:
    """WARN must not affect `result.passed` or `result.checks` membership.

    Spec: "WARN does not block (planning continues; spawns are still gated
    only on the existing pass/fail checks)."
    """

    def test_warn_does_not_add_failing_check(self, monkeypatch):
        """A firing probe must only touch `warnings`, never `checks`."""
        rule_payload = [{"type": "pull_request"}]
        responses = {
            "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
            "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
            "/user": _FakeResponse(
                200,
                {"login": "owner"},
                headers={"X-OAuth-Scopes": "gist"},
            ),
            "/rules/branches/agent-orchestrator-preflight": _FakeResponse(200, rule_payload),
        }
        monkeypatch.setattr("httpx.Client", _make_client(responses))

        result = verify("github.com/owner/repo", token="ghp_fake")

        # All probes fired into warnings, none of them registered as a check.
        all_check_names = [c.name for c in result.checks]
        for slug in repo_verify.PREFLIGHT_REASON_SLUGS:
            assert slug not in all_check_names, (
                f"probe slug {slug} leaked into result.checks: {all_check_names}"
            )
        # And `passed` is still True because every check passed.
        assert result.passed
        assert all(c.passed for c in result.checks)


class TestFormatHintRendersConcreteCopy:
    """Spec: the report's hint table renders the PREFLIGHT_HINTS copy directly.

    Other surfaces (escalation summary, ntfy body) will import the same dict
    and produce identical copy. If the formatter ever stops sourcing from
    PREFLIGHT_HINTS (e.g., hardcodes its own strings), this test catches it.
    """

    def test_each_slugs_hint_copy_appears_when_that_slug_fires(self):
        from orchestrator.models import CheckResult, VerificationResult

        for slug in repo_verify.PREFLIGHT_REASON_SLUGS:
            result = VerificationResult(
                repo_url="https://github.com/o/r",
                checks=[CheckResult("read access", True)],
                warnings=[f"[{slug}] sample probe message"],
            )
            lines = repo_verify.format_result_lines(result)
            joined = "\n".join(lines)
            hint = repo_verify.PREFLIGHT_HINTS[slug]
            # A non-trivial substring of the hint must appear in the output;
            # asserts the formatter renders the shared copy rather than its own.
            assert hint[:30] in joined, (
                f"hint copy for {slug!r} missing from format_result_lines output"
            )
