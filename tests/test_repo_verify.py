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
