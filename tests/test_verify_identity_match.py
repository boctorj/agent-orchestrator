"""Tests for the identity-match check in `repo_verify.verify()`.

Background (F-002): the verifier used to print the token's identity AND the
repo owner separately but never compared them, so an identity mismatch
showed up only as a downstream "write access" failure. F-002-U-1 adds an
"identity match" first-class check that runs BEFORE the write and
branch-protection checks.

Scenarios covered (parameterized over owner, token user, collaborator list):
  * owner-match — token user IS the repo owner.
  * collaborator-with-push — token user is a push-capable collaborator.
  * collaborator-read-only — token user is in the collaborators list but
    without push permission. Must FAIL with the actionable detail message.
  * stranger — token user is not in the collaborators list at all. Must
    FAIL.

Each scenario is a single `verify()` call with the GitHub API mocked via
the `_make_client` URL-suffix fake; no live HTTP.
"""

from __future__ import annotations

import pytest

from orchestrator.repo_verify import verify


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


def _make_client(responses: dict[str, _FakeResponse]):
    """Build a fake httpx.Client returning canned responses by URL-suffix match."""

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
            return _FakeResponse(404)

    return FakeClient


def _good_repo_meta():
    return {
        "default_branch": "main",
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


def _get_check(result, name):
    return next((c for c in result.checks if c.name == name), None)


# --------------------------- parameterized scenarios ---------------------------


@pytest.mark.parametrize(
    ("owner", "token_user", "collaborators", "expected_passed", "expected_detail_substring"),
    [
        # owner-match: trivial pass; collaborators endpoint is never consulted.
        (
            "boctorj",
            "boctorj",
            [],
            True,
            "is the repo owner",
        ),
        # collaborator-with-push: token user is in /collaborators with push perm.
        (
            "boctorj",
            "joeboctor",
            [
                {
                    "login": "joeboctor",
                    "permissions": {"pull": True, "push": True, "admin": False},
                },
            ],
            True,
            "collaborator with push access",
        ),
        # collaborator-read-only: in /collaborators but push=False — must FAIL,
        # with the actionable fix-it instructions in the detail string.
        (
            "boctorj",
            "joeboctor",
            [
                {
                    "login": "joeboctor",
                    "permissions": {"pull": True, "push": False, "admin": False},
                },
            ],
            False,
            "not a collaborator with push access",
        ),
        # stranger: not in /collaborators at all — FAIL.
        (
            "boctorj",
            "joeboctor",
            [
                {
                    "login": "someone-else",
                    "permissions": {"pull": True, "push": True, "admin": False},
                },
            ],
            False,
            "not a collaborator with push access",
        ),
    ],
    ids=["owner-match", "collaborator-with-push", "collaborator-read-only", "stranger"],
)
def test_identity_match_scenarios(
    monkeypatch, owner, token_user, collaborators, expected_passed, expected_detail_substring
):
    responses = {
        f"/repos/{owner}/repo/collaborators": _FakeResponse(200, collaborators),
        f"/repos/{owner}/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        f"/repos/{owner}/repo": _FakeResponse(200, _good_repo_meta()),
        "/user": _FakeResponse(200, {"login": token_user}),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))

    result = verify(f"github.com/{owner}/repo", token="ghp_fake")

    check = _get_check(result, "identity match")
    assert check is not None, "identity match must be a first-class check"
    assert check.passed is expected_passed
    assert expected_detail_substring in check.detail


# --------------------------- detail message structure ---------------------------


def test_identity_mismatch_detail_names_both_parties(monkeypatch):
    """The actionable detail must surface BOTH the token user and the owner.

    The whole point of the check is to give the user a one-line message
    they can act on. That requires naming the actual offending logins.
    """
    responses = {
        "/repos/boctorj/repo": _FakeResponse(200, _good_repo_meta()),
        "/repos/boctorj/repo/collaborators": _FakeResponse(200, []),
        "/repos/boctorj/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        "/user": _FakeResponse(200, {"login": "joeboctor"}),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))

    result = verify("github.com/boctorj/repo", token="ghp_fake")
    check = _get_check(result, "identity match")
    assert check is not None
    assert not check.passed
    # Both parties named — the user must know whose account to fix and what to do.
    assert "joeboctor" in check.detail
    assert "boctorj" in check.detail
    # The fix-it instructions are present.
    assert "Generate a PAT" in check.detail
    assert "push access" in check.detail


def test_identity_match_runs_before_write_and_branch_protection(monkeypatch):
    """The identity-match check must come BEFORE write and branch-protection.

    Per F-002: 'so root cause surfaces first instead of as a downstream
    symptom.' We assert the order of checks in `result.checks`.
    """
    responses = {
        "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
        "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        "/user": _FakeResponse(200, {"login": "owner"}),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))

    result = verify("github.com/owner/repo", token="ghp_fake")
    names = [c.name for c in result.checks]

    assert "identity match" in names
    assert "write access" in names
    assert "branch protection exists" in names
    assert names.index("identity match") < names.index("write access")
    assert names.index("identity match") < names.index("branch protection exists")


def test_identity_match_case_insensitive_owner_compare(monkeypatch):
    """Repo owners are matched case-insensitively against the token user.

    `normalize_repo_url` lowercases the URL path, so the in-memory `owner`
    string is "myorg"; if the token user is "MyOrg" we still want the
    check to pass (GitHub logins are case-insensitive).
    """
    responses = {
        "/repos/myorg/repo": _FakeResponse(200, _good_repo_meta()),
        "/repos/myorg/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        "/user": _FakeResponse(200, {"login": "MyOrg"}),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))

    result = verify("github.com/MyOrg/repo", token="ghp_fake")
    check = _get_check(result, "identity match")
    assert check is not None
    assert check.passed


def test_identity_match_skipped_in_app_mode(monkeypatch):
    """App-mode identity is `app:installation:N`, not a user login.

    Comparing an installation id to a repo owner is meaningless; the App
    has its own access model (`/installation/repositories`). We skip the
    identity-match check in App mode rather than emit a misleading line.
    """
    responses = {
        "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
        "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        "/installation/repositories": _FakeResponse(
            200, {"repositories": [{"full_name": "owner/repo"}]}
        ),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "999")

    result = verify("github.com/owner/repo", token="ghs_fake", auth_mode="app")

    names = [c.name for c in result.checks]
    assert "identity match" not in names


def test_identity_match_handles_collaborators_endpoint_failure(monkeypatch):
    """If /collaborators returns non-200 (e.g., 403, missing scope), the check
    must still emit a FAILING identity-match line — we can't confirm the
    user is a collaborator, so we conservatively report mismatch.
    """
    responses = {
        "/repos/owner/repo": _FakeResponse(200, _good_repo_meta()),
        "/repos/owner/repo/collaborators": _FakeResponse(403, {"message": "Forbidden"}),
        "/repos/owner/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        "/user": _FakeResponse(200, {"login": "stranger"}),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))

    result = verify("github.com/owner/repo", token="ghp_fake")
    check = _get_check(result, "identity match")
    assert check is not None
    assert not check.passed
    assert "stranger" in check.detail
    assert "owner" in check.detail
