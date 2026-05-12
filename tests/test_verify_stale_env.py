"""Tests for the stale-env check in `verify_repo` (and `detect_stale_env`).

Background (F-002): the MCP server reads `.env` once at startup via
`load_dotenv()`. If the user has rotated `GITHUB_TOKEN` in `.env` but
hasn't restarted the server, the in-process `os.environ['GITHUB_TOKEN']`
still holds the OLD value — invisible to the user, surfaces only as
confusing downstream auth failures.

`detect_stale_env` returns a warning string when the on-disk value differs
from the loaded value; the `verify_repo` MCP tool prepends that warning
to its report.

Covered:
  * mismatch → warning is returned/prepended.
  * match → no warning.
  * `.env` missing → silent (no warning).
  * `.env` has no GITHUB_TOKEN line → silent.
  * Token VALUE never appears in the warning (don't leak the secret).

Also includes the F-002-U-1 snapshot test: full `verify_repo` report
covering identity-mismatch + stale-env in the same scenario.
"""

from __future__ import annotations

from orchestrator import repo_verify
from orchestrator.repo_verify import detect_stale_env
from orchestrator.tools import ops

# --------------------------- detect_stale_env (unit) ---------------------------


def test_stale_env_warns_when_env_differs(tmp_path, monkeypatch):
    """`.env` GITHUB_TOKEN differs from os.environ — warn."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=new_value_on_disk\n")
    monkeypatch.setenv("GITHUB_TOKEN", "old_value_in_process")

    warning = detect_stale_env(env)
    assert warning is not None
    assert "Loaded GITHUB_TOKEN differs from the value currently in .env" in warning
    assert "Restart the server" in warning


def test_stale_env_silent_when_values_match(tmp_path, monkeypatch):
    """No warning when on-disk and in-process tokens are identical."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=same_value\n")
    monkeypatch.setenv("GITHUB_TOKEN", "same_value")

    assert detect_stale_env(env) is None


def test_stale_env_silent_when_env_file_missing(tmp_path, monkeypatch):
    """Silent when .env doesn't exist — nothing to compare against."""
    monkeypatch.setenv("GITHUB_TOKEN", "whatever")
    missing = tmp_path / "does-not-exist.env"
    assert not missing.exists()
    assert detect_stale_env(missing) is None


def test_stale_env_silent_when_env_has_no_token_line(tmp_path, monkeypatch):
    """`.env` exists but has no GITHUB_TOKEN line — can't compare; silent."""
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-foo\nNTFY_TOPIC=t\n")
    monkeypatch.setenv("GITHUB_TOKEN", "something")

    assert detect_stale_env(env) is None


def test_stale_env_does_not_leak_token_value(tmp_path, monkeypatch):
    """The warning text must NEVER contain either token value.

    Security invariant: parse-only, never log. The whole point of the
    check is to alert about a stale value without exposing it.
    """
    env = tmp_path / ".env"
    on_disk = "ghp_ondisk_sekret_AAA"
    in_proc = "ghp_inprocess_sekret_BBB"
    env.write_text(f"GITHUB_TOKEN={on_disk}\n")
    monkeypatch.setenv("GITHUB_TOKEN", in_proc)

    warning = detect_stale_env(env)
    assert warning is not None
    assert on_disk not in warning
    assert in_proc not in warning


def test_stale_env_with_default_path_uses_cwd(tmp_path, monkeypatch):
    """Default path is `.env` resolved against the current working dir.

    The MCP server is launched with the project root as cwd; that's where
    `.env` lives. Confirm calling with no args picks up that file.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=disk_val\n")
    monkeypatch.setenv("GITHUB_TOKEN", "proc_val")

    warning = detect_stale_env()
    assert warning is not None
    assert "differs" in warning.lower()


def test_stale_env_accepts_string_path(tmp_path, monkeypatch):
    """The helper accepts a `str` path as well as a `Path`."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=disk_val\n")
    monkeypatch.setenv("GITHUB_TOKEN", "proc_val")

    warning = detect_stale_env(str(env))
    assert warning is not None


# --------------------------- verify_repo integration ---------------------------


def _stub_verify_pass(monkeypatch):
    """Patch repo_verify.verify (called from ops.verify_repo) to return a passing result."""
    from orchestrator.models import CheckResult, VerificationResult

    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/owner/repo",
            default_branch="main",
            auth_mode=auth_mode,
            auth_identity="user:owner",
            checks=[
                CheckResult("read access", True),
                CheckResult("identity match", True, "token user (owner) is the repo owner"),
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


def _stub_token(monkeypatch):
    monkeypatch.setattr("orchestrator.tools.ops.github_app.get_agent_token", lambda: "ghp_fake")
    monkeypatch.setattr("orchestrator.tools.ops.github_app.auth_mode", lambda: "pat")


def _redirect_detect_stale_env_to(monkeypatch, env_path):
    """Make `ops.verify_repo`'s no-arg `detect_stale_env()` call read `env_path`.

    We capture the *original* function first, then install a wrapper that
    delegates to it with the test's path. Without that capture, a naive
    `lambda: detect_stale_env(env_path)` would refer to itself (since we'd
    have replaced the attribute) and infinitely recurse.
    """
    original = repo_verify.detect_stale_env
    monkeypatch.setattr(
        "orchestrator.tools.ops.repo_verify.detect_stale_env",
        lambda: original(env_path),
    )


def test_verify_repo_prepends_stale_env_warning(
    tmp_path, tmp_state_db, with_github_token, monkeypatch
):
    """When `.env` differs from os.environ, the warning appears at the top
    of the verify_repo report — BEFORE the standard pass/fail lines."""
    # `with_github_token` sets GITHUB_TOKEN=github_pat_fake_for_tests which
    # differs from our on-disk value. We need detect_stale_env to read OUR
    # file, so redirect the default-path call.
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=different_disk_value\n")
    _redirect_detect_stale_env_to(monkeypatch, env)

    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)

    out = ops.verify_repo("github.com/owner/repo")
    lines = out.splitlines()
    # First line must be the warning
    assert lines[0].startswith("⚠")
    assert "Loaded GITHUB_TOKEN differs" in lines[0]
    # And the standard report follows below
    assert "owner/repo" in out
    assert "Cached" in out


def test_verify_repo_no_warning_when_env_matches(
    tmp_path, tmp_state_db, with_github_token, monkeypatch
):
    """Matching .env → no warning prepended; report starts as usual."""
    env = tmp_path / ".env"
    # `with_github_token` sets GITHUB_TOKEN=github_pat_fake_for_tests
    env.write_text("GITHUB_TOKEN=github_pat_fake_for_tests\n")
    _redirect_detect_stale_env_to(monkeypatch, env)

    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)

    out = ops.verify_repo("github.com/owner/repo")
    lines = out.splitlines()
    # First line is the standard ✓ header — NOT a warning
    assert not lines[0].startswith("⚠")
    assert "Loaded GITHUB_TOKEN differs" not in out


def test_verify_repo_silent_when_env_missing(
    tmp_path, tmp_state_db, with_github_token, monkeypatch
):
    """`.env` missing entirely → no warning, even with no in-process token match."""
    _redirect_detect_stale_env_to(monkeypatch, tmp_path / "nope.env")

    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)

    out = ops.verify_repo("github.com/owner/repo")
    assert "⚠ Loaded GITHUB_TOKEN" not in out


# --------------------------- snapshot test: both signals together ---------------------------


def test_verify_repo_snapshot_identity_mismatch_plus_stale_env(
    tmp_path, tmp_state_db, with_github_token, monkeypatch
):
    """End-to-end snapshot: an identity-mismatch + a stale .env at the same time.

    The report must:
      * Start with the stale-env ⚠ warning (prepended diagnostic context).
      * Contain a failing ✗ identity match line with the actionable detail
        naming both the token user and the repo owner.
      * NOT cache the result (verification failed overall).
      * End with the standard "Verification FAILED" trailer.
    """
    from orchestrator import state
    from orchestrator.models import CheckResult, VerificationResult

    # Stale .env: disk value != process value.
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=rotated_disk_value\n")
    _redirect_detect_stale_env_to(monkeypatch, env)

    # Force an identity-mismatch failure result without HTTP.
    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/boctorj/repo",
            default_branch="main",
            auth_mode=auth_mode,
            auth_identity="user:joeboctor",
            checks=[
                CheckResult("read access", True),
                CheckResult(
                    "identity match",
                    False,
                    "token user (joeboctor) is not the repo owner (boctorj) "
                    "and is not a collaborator with push access. Generate a "
                    "PAT from the boctorj account, or grant joeboctor push "
                    "access on this repo.",
                ),
            ],
        )

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", fake_verify)
    _stub_token(monkeypatch)

    out = ops.verify_repo("github.com/boctorj/repo")

    # Snapshot the exact report. Multi-line to keep the test diff-friendly.
    expected = (
        "⚠ Loaded GITHUB_TOKEN differs from the value currently in .env.\n"
        "  The MCP server cached the old value at startup. Restart the server\n"
        "  to pick up the new token before retrying.\n"
        "\n"
        "✗ https://github.com/boctorj/repo\n"
        "  default branch: main\n"
        "  authenticated as: user:joeboctor (pat)\n"
        "\n"
        "  ✓ read access\n"
        "  ✗ identity match  · token user (joeboctor) is not the repo owner "
        "(boctorj) and is not a collaborator with push access. Generate a "
        "PAT from the boctorj account, or grant joeboctor push access on "
        "this repo.\n"
        "\n"
        "Verification FAILED — spawns against this repo will be blocked until fixed."
    )
    assert out == expected

    # And: failed result must NOT be cached.
    assert state.get_verified_repo("https://github.com/boctorj/repo") is None
