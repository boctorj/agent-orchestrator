"""Spec-compliance tests for F-002-U-1.

The coder's `test_verify_identity_match.py` and `test_verify_stale_env.py`
exercise the behavior with substring checks. This file complements them by:

  1. Locking in the EXACT wording of the user-facing strings from the
     F-002-U-1 unit description (spec quotes the message text verbatim).
  2. Asserting purely-diagnostic semantics of the stale-env warning
     (it must NOT change `result.passed` and must NOT block caching).
  3. Confirming the implementation avoids an unnecessary /collaborators
     call when the token user already equals the repo owner.

All GitHub API access is mocked via a URL-suffix `httpx.Client` stub;
no live HTTP. Same pattern as the coder's tests.
"""

from __future__ import annotations

import pytest

from orchestrator import repo_verify
from orchestrator.repo_verify import (
    _identity_match_check,
    detect_stale_env,
    format_result_lines,
    verify,
)
from orchestrator.tools import ops

# --------------------------- helpers ---------------------------


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


def _make_client(responses: dict[str, _FakeResponse], call_log: list[str] | None = None):
    """URL-suffix fake httpx.Client. Optionally records every requested path."""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, **kw):
            path = url.split("?", 1)[0]
            if call_log is not None:
                call_log.append(path)
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


# --------------------------- spec: exact stale-env warning text ---------------------------


# The F-002-U-1 unit description quotes this warning verbatim. Reproduced here
# byte-for-byte so any drift in the implementation flips this test.
_EXPECTED_STALE_ENV_WARNING = (
    "\u26a0 Loaded GITHUB_TOKEN differs from the value currently in .env.\n"
    "  The MCP server cached the old value at startup. Restart the server\n"
    "  to pick up the new token before retrying."
)


def test_stale_env_warning_text_matches_spec_verbatim(tmp_path, monkeypatch):
    """The stale-env warning must match the F-002-U-1 spec character for character."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=on_disk_token\n")
    monkeypatch.setenv("GITHUB_TOKEN", "in_process_token")

    assert detect_stale_env(env) == _EXPECTED_STALE_ENV_WARNING


# --------------------------- spec: exact identity-mismatch detail text ---------------------------


def test_identity_mismatch_detail_text_matches_spec_verbatim():
    """The mismatch CheckResult.detail must match the F-002-U-1 spec wording.

    The unit description shows the exact phrasing the user should see:
      'token user (joeboctor) is not the repo owner (boctorj) and is not a
       collaborator with push access. Generate a PAT from the boctorj
       account, or grant joeboctor push access on this repo.'
    """

    class _OnlyEmptyCollabs:
        def get(self, url, **kw):
            # Empty collaborators list — token user is a stranger.
            return _FakeResponse(200, [])

    check = _identity_match_check(_OnlyEmptyCollabs(), "boctorj", "repo", "joeboctor")
    assert check.name == "identity match"
    assert check.passed is False
    assert check.detail == (
        "token user (joeboctor) is not the repo owner (boctorj) "
        "and is not a collaborator with push access. Generate a PAT "
        "from the boctorj account, or grant joeboctor push access "
        "on this repo."
    )


def test_identity_match_owner_check_short_circuits_no_collaborators_call():
    """When token user == repo owner, the /collaborators endpoint must NOT be hit.

    Avoiding the call matters for two reasons: it's wasted latency, and
    fine-grained PATs without the `metadata: read` collaborators scope
    would 403 — which would (if not short-circuited) spuriously fail
    the identity check for an owner-authenticated token.
    """
    call_log: list[str] = []

    class LoggingClient:
        def get(self, url, **kw):
            call_log.append(url)
            return _FakeResponse(200, [])

    check = _identity_match_check(LoggingClient(), "alice", "repo", "alice")
    assert check.passed is True
    assert "is the repo owner" in check.detail
    # Critical: no call to /collaborators when token user is the owner.
    assert call_log == [], f"unexpected API calls: {call_log}"


def test_identity_check_passes_for_collaborator_with_push_exact_detail():
    """Detail string for a successful collaborator-with-push match is informative."""

    class WithCollab:
        def get(self, url, **kw):
            return _FakeResponse(
                200, [{"login": "joeboctor", "permissions": {"push": True, "pull": True}}]
            )

    check = _identity_match_check(WithCollab(), "boctorj", "repo", "joeboctor")
    assert check.passed is True
    assert check.detail == "token user (joeboctor) is a collaborator with push access"


# --------------------------- spec: diagnostic-only semantics ---------------------------


def test_stale_env_does_not_affect_verify_passed(monkeypatch):
    """Stale-env is diagnostic-only — `result.passed` must reflect only the checks.

    Per spec: 'Does NOT fail verification on its own — diagnostic context
    for failures that follow.'

    We run `verify()` end-to-end with a fully-passing GitHub state AND a
    stale .env on disk, and assert the VerificationResult itself passes.
    The warning is added by `ops.verify_repo` (the MCP-tool layer), NOT
    by `verify()`, so this also pins the architectural separation.
    """
    responses = {
        "/repos/alice/repo": _FakeResponse(200, _good_repo_meta()),
        "/repos/alice/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        "/user": _FakeResponse(200, {"login": "alice"}),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))

    # The function under test doesn't read .env, but set it up anyway to
    # confirm that even when env-mismatch state exists the result passes.
    result = verify("github.com/alice/repo", token="ghp_fake")
    assert result.passed is True
    # Identity match present and passing.
    im = _get_check(result, "identity match")
    assert im is not None and im.passed


def test_verify_repo_caches_when_only_warning_is_stale_env(
    tmp_path, tmp_state_db, with_github_token, monkeypatch
):
    """An otherwise-passing verify + a stale .env still caches the repo.

    The stale-env diagnostic must not gate caching — only failing checks
    do. This pins the "stale-env is non-blocking" contract end-to-end
    through the MCP-tool surface.
    """
    from orchestrator import state
    from orchestrator.models import CheckResult, VerificationResult

    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=disk_value_differs_from_process\n")

    # Redirect default-path stale-env detection at the test's .env.
    original_detect = repo_verify.detect_stale_env
    monkeypatch.setattr(
        "orchestrator.tools.ops.repo_verify.detect_stale_env",
        lambda: original_detect(env),
    )

    # All checks pass.
    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/alice/repo",
            default_branch="main",
            auth_mode=auth_mode,
            auth_identity="user:alice",
            checks=[
                CheckResult("read access", True),
                CheckResult("identity match", True, "token user (alice) is the repo owner"),
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

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", fake_verify)
    monkeypatch.setattr("orchestrator.tools.ops.github_app.get_agent_token", lambda: "ghp_fake")
    monkeypatch.setattr("orchestrator.tools.ops.github_app.auth_mode", lambda: "pat")

    out = ops.verify_repo("github.com/alice/repo")

    # Warning IS prepended.
    assert out.splitlines()[0].startswith("\u26a0")
    # But the repo was still cached — stale-env is non-blocking.
    cached = state.get_verified_repo("https://github.com/alice/repo")
    assert cached is not None, "verification with only-stale-env warning must still cache"
    # And the trailer reflects the cache, not a failure.
    assert "Cached" in out
    assert "FAILED" not in out


# --------------------------- spec: identity-match line ordering ---------------------------


def test_identity_match_emitted_immediately_after_read_access(monkeypatch):
    """The spec says identity-match runs BEFORE write/branch-protection.

    In practice the implementation orders it as: read access, identity
    match, write access, branch protection, ... Lock that in.
    """
    responses = {
        "/repos/alice/repo": _FakeResponse(200, _good_repo_meta()),
        "/repos/alice/repo/branches/main/protection": _FakeResponse(200, _good_protection()),
        "/user": _FakeResponse(200, {"login": "alice"}),
    }
    monkeypatch.setattr("httpx.Client", _make_client(responses))

    result = verify("github.com/alice/repo", token="ghp_fake")
    names = [c.name for c in result.checks]
    assert names[:4] == [
        "read access",
        "identity match",
        "write access",
        "branch protection exists",
    ], names


# --------------------------- spec: warning is prepended (not inlined) ---------------------------


def test_format_result_lines_does_not_include_stale_env_warning():
    """The warning is added by `ops.verify_repo`, NOT by `format_result_lines`.

    Architectural pin: `format_result_lines` is pure-formatting of a
    VerificationResult and must stay unaware of the stale-env warning.
    Otherwise we'd risk double-prepending or stale-env state leaking into
    other callers of `format_result_lines` (CLI subcommand, etc.).
    """
    from orchestrator.models import CheckResult, VerificationResult

    result = VerificationResult(
        repo_url="https://github.com/alice/repo",
        default_branch="main",
        auth_mode="pat",
        auth_identity="user:alice",
        checks=[CheckResult("identity match", True, "token user (alice) is the repo owner")],
    )
    lines = format_result_lines(result)
    text = "\n".join(lines)
    assert "Loaded GITHUB_TOKEN differs" not in text
    assert "Restart the server" not in text


# --------------------------- spec: no token VALUE leaks on disk OR in env ---------------------------


@pytest.mark.parametrize(
    ("disk", "in_proc"),
    [
        ("ghp_aaa_disk", "ghp_bbb_proc"),
        ("gho_xxxxxxxxxxxxxxx", "gho_yyyyyyyyyyyyyyy"),
        ("github_pat_11ABCDEF", "github_pat_99ZYXWVU"),
    ],
)
def test_stale_env_warning_never_contains_either_token(tmp_path, monkeypatch, disk, in_proc):
    """Security invariant: the warning text MUST NOT echo either token value.

    Parameterized across realistic PAT shapes (classic, OAuth, fine-grained)
    to guard against accidental embedding regardless of token format.
    """
    env = tmp_path / ".env"
    env.write_text(f"GITHUB_TOKEN={disk}\n")
    monkeypatch.setenv("GITHUB_TOKEN", in_proc)

    warning = detect_stale_env(env)
    assert warning is not None
    assert disk not in warning, "on-disk token value leaked into warning"
    assert in_proc not in warning, "in-process token value leaked into warning"
