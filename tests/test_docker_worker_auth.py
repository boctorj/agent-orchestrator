"""Auth-mode selection tests for the Docker worker.

The unit description spells out two branches at spawn time:

  - `ANTHROPIC_API_KEY` set on host  →  API-key mode:
        forward `--env ANTHROPIC_API_KEY` into the container,
        do NOT mount `~/.claude`.
  - `ANTHROPIC_API_KEY` unset        →  OAuth mode:
        mount `~/.claude` read-only into the container,
        do NOT pass `ANTHROPIC_API_KEY`.

These tests exercise both branches via `build_docker_argv` (the
deterministic seam) and verify the auth-mode log line is emitted at
spawn time per the unit description ("Log chosen mode at spawn").
"""

from __future__ import annotations

import pytest

from orchestrator.workers.docker_claude_code import select_auth_mode
from tests.conftest import _FakeProc, _make_worker

# ---------------------------------------------------------------------------
# Helpers (worker factory + subprocess stub live in tests/conftest.py).
# ---------------------------------------------------------------------------


def _argv_contains_mount(argv: list[str], substr: str) -> bool:
    """True if any --mount value contains substr (substring match)."""
    for i, tok in enumerate(argv):
        if tok == "--mount" and i + 1 < len(argv) and substr in argv[i + 1]:
            return True
    return False


def _argv_passes_env(argv: list[str], name: str) -> bool:
    """True if argv carries `--env <NAME>` (the value-less passthrough form)."""
    for i, tok in enumerate(argv):
        if tok == "--env" and i + 1 < len(argv) and argv[i + 1] == name:
            return True
    return False


# ---------------------------------------------------------------------------
# select_auth_mode — the underlying decision function
# ---------------------------------------------------------------------------


class TestSelectAuthMode:
    def test_picks_api_key_when_anthropic_key_present(self):
        assert select_auth_mode({"ANTHROPIC_API_KEY": "sk-ant-abc"}) == "api_key"

    def test_picks_oauth_when_anthropic_key_absent(self):
        assert select_auth_mode({"GITHUB_TOKEN": "ghp_xyz"}) == "oauth"

    def test_empty_string_anthropic_key_is_oauth_mode(self):
        """Empty value should be treated the same as unset — claude won't
        authenticate with an empty key, so we should fall back to OAuth."""
        assert select_auth_mode({"ANTHROPIC_API_KEY": ""}) == "oauth"


# ---------------------------------------------------------------------------
# API-key mode: env forwarded, NO ~/.claude mount.
# ---------------------------------------------------------------------------


class TestApiKeyMode:
    def test_argv_forwards_anthropic_api_key(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(
            ["claude", "-p", "hi"],
            host_env={"ANTHROPIC_API_KEY": "sk-ant-deadbeef", "GITHUB_TOKEN": "ghp_x"},
        )
        assert _argv_passes_env(argv, "ANTHROPIC_API_KEY"), (
            f"--env ANTHROPIC_API_KEY missing from argv: {argv!r}"
        )

    def test_argv_does_not_mount_claude_directory(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(
            ["claude", "-p", "hi"],
            host_env={"ANTHROPIC_API_KEY": "sk-ant-deadbeef"},
        )
        # The credentials directory `~/.claude` must NOT be bind-mounted
        # in API-key mode. The sessions sub-directory IS still mounted
        # (writable, for resume support) so checking for the bare
        # `/home/agent/.claude` *target* is what catches a regression.
        for i, tok in enumerate(argv):
            if tok != "--mount" or i + 1 >= len(argv):
                continue
            mount_value = argv[i + 1]
            if "target=/home/agent/.claude," in mount_value:
                pytest.fail(
                    f"API-key mode must NOT mount ~/.claude as creds dir; "
                    f"saw mount: {mount_value!r}"
                )
            if mount_value.endswith("target=/home/agent/.claude"):
                pytest.fail(
                    f"API-key mode must NOT mount ~/.claude as creds dir; "
                    f"saw mount: {mount_value!r}"
                )

    def test_argv_still_forwards_github_token(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(
            ["claude", "-p", "hi"],
            host_env={"ANTHROPIC_API_KEY": "sk-ant-deadbeef", "GITHUB_TOKEN": "ghp_x"},
        )
        # GITHUB_TOKEN is whitelisted in BOTH modes; verify it's still here.
        assert _argv_passes_env(argv, "GITHUB_TOKEN")

    def test_spawn_logs_api_key_mode(self, tmp_path, monkeypatch):
        """`Log chosen mode at spawn ("Auth: API key" / "Auth: claude.ai OAuth")`."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deadbeef")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")

        logs: list[str] = []
        worker = _make_worker(tmp_path)
        worker.log = logs.append
        # Stub subprocess.run so spawn doesn't actually try to reach Docker.
        worker.run = lambda *a, **kw: _FakeProc(
            stdout='{"session_id": "abc", "result": "ok"}', returncode=0
        )
        worker.spawn("hi")

        assert "Auth: API key" in logs


# ---------------------------------------------------------------------------
# OAuth mode: ~/.claude mounted read-only, NO ANTHROPIC_API_KEY passed.
# ---------------------------------------------------------------------------


class TestOAuthMode:
    def test_argv_mounts_claude_directory_readonly(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(
            ["claude", "-p", "hi"],
            host_env={"GITHUB_TOKEN": "ghp_x"},  # no ANTHROPIC_API_KEY
        )
        claude_dir = worker.home_dir / ".claude"
        found = False
        for i, tok in enumerate(argv):
            if tok != "--mount" or i + 1 >= len(argv):
                continue
            mount_value = argv[i + 1]
            if (
                f"source={claude_dir}" in mount_value
                and "target=/home/agent/.claude" in mount_value
                and "readonly" in mount_value
            ):
                found = True
        assert found, f"OAuth mode must mount ~/.claude read-only; argv={argv!r}"

    def test_argv_does_not_pass_anthropic_api_key(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(
            ["claude", "-p", "hi"],
            host_env={"GITHUB_TOKEN": "ghp_x"},
        )
        assert "ANTHROPIC_API_KEY" not in argv, (
            f"OAuth mode must NOT forward ANTHROPIC_API_KEY; argv={argv!r}"
        )

    def test_subprocess_env_in_oauth_mode_drops_anthropic_api_key(self, tmp_path):
        """Even if the host had an empty ANTHROPIC_API_KEY (treated as
        OAuth), it must not appear in the subprocess env we hand docker."""
        worker = _make_worker(tmp_path)
        env = worker.build_subprocess_env(host_env={"GITHUB_TOKEN": "ghp_x"})
        assert "ANTHROPIC_API_KEY" not in env

    def test_spawn_logs_oauth_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")

        logs: list[str] = []
        worker = _make_worker(tmp_path)
        worker.log = logs.append
        worker.run = lambda *a, **kw: _FakeProc(
            stdout='{"session_id": "abc", "result": "ok"}', returncode=0
        )
        worker.spawn("hi")

        assert "Auth: claude.ai OAuth" in logs


# ---------------------------------------------------------------------------
# Shared invariant: the writable sessions sub-directory IS bound in BOTH
# modes (so `claude --resume` works regardless of auth path).
# ---------------------------------------------------------------------------


class TestSessionsMount:
    @pytest.mark.parametrize(
        "host_env",
        [
            {"ANTHROPIC_API_KEY": "sk-ant-x"},
            {"GITHUB_TOKEN": "ghp_x"},
        ],
        ids=["api_key", "oauth"],
    )
    def test_sessions_dir_always_mounted(self, tmp_path, host_env):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(["claude", "-p", "hi"], host_env=host_env)
        assert _argv_contains_mount(argv, "target=/home/agent/.claude/sessions"), (
            f"sessions mount missing in argv: {argv!r}"
        )


# ---------------------------------------------------------------------------
# Fake subprocess result used by the log-line tests above lives in
# tests/conftest.py (imported as `_FakeProc` at the top of this file).
# ---------------------------------------------------------------------------
