"""Tests for orchestrator/cli.py — Click commands via CliRunner."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from orchestrator.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fake_httpx_get(monkeypatch):
    """Make orchestrator.cli's httpx.get return a fake response."""

    class FakeResponse:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self._json = json_data or {"login": "fakeuser"}

        def json(self):
            return self._json

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr("httpx.get", fake_get)
    return calls


# --------------------------- version ---------------------------


def test_version_command(runner):
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "agent-orchestrator" in result.output


def test_top_level_version_flag(runner):
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0


# --------------------------- doctor ---------------------------


def test_doctor_passes_with_complete_env(runner, tmp_path, monkeypatch, fake_httpx_get):
    """All-green doctor run when env + state.db + .mcp.json + claude all present."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-fake\nGITHUB_TOKEN=github_pat_fake\nNTFY_TOPIC=test-topic\n"
    )
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    db_file = tmp_path / "state.db"
    monkeypatch.setattr("orchestrator.state.STATE_DB", db_file)
    from orchestrator import state

    state.init_db()

    monkeypatch.setattr("shutil.which", lambda name: "/fake/path/" + name)

    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "all checks passed" in result.output


def test_doctor_fails_with_missing_env(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No .env file
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "some checks failed" in result.output


def test_doctor_reports_bad_token_format(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=not-a-real-key\nGITHUB_TOKEN=alsobad\n")
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY format" in result.output


def test_doctor_does_not_create_state_db(runner, tmp_path, monkeypatch):
    """`orchestrator doctor` is a read-only health check; it must NOT create
    `state.db` as a side effect of importing modules.

    Previously, the "package importable" probe did `from orchestrator import
    mcp_server`, which ran `state.init_db()` at module-import time and
    silently wrote state.db to disk. After fix, mcp_server's import is
    side-effect-free w.r.t. the DB; init_db only runs from `main()`.
    """
    import sys

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-fake\nGITHUB_TOKEN=github_pat_fake\n")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    db_file = tmp_path / "state.db"
    monkeypatch.setattr("orchestrator.state.STATE_DB", db_file)
    monkeypatch.setattr("shutil.which", lambda name: None)

    # Mock httpx so doctor doesn't try real network
    class Fake500:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr("httpx.get", lambda *a, **k: Fake500())

    # Force a fresh import of mcp_server so any module-level side effect runs
    sys.modules.pop("orchestrator.mcp_server", None)

    assert not db_file.exists()
    runner.invoke(cli, ["doctor"])
    assert not db_file.exists(), (
        "doctor created state.db as a side effect — health check should be read-only"
    )


def test_doctor_handles_github_auth_failure(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-fake\nGITHUB_TOKEN=github_pat_invalid\n"
    )

    class Fake401:
        status_code = 401

        def json(self):
            return {}

    monkeypatch.setattr("httpx.get", lambda *a, **k: Fake401())
    monkeypatch.setattr("shutil.which", lambda name: None)  # claude missing

    result = runner.invoke(cli, ["doctor"])
    # Should report 401 from /user
    assert "GITHUB_TOKEN authenticates" in result.output


# --------------------------- init ---------------------------


def test_init_writes_env_and_state_db(runner, tmp_path, monkeypatch, fake_httpx_get):
    monkeypatch.chdir(tmp_path)
    # Pretend we're in an orchestrator project dir
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    # Point state at the temp dir
    db_file = tmp_path / "state.db"
    monkeypatch.setattr("orchestrator.state.STATE_DB", db_file)

    # Inputs: API key, auth choice (p=PAT), GH token, ntfy topic (empty),
    # worker backend (m=managed_agents default).
    inputs = "sk-ant-fake-key\np\ngithub_pat_fake\n\nm\n"
    result = runner.invoke(cli, ["init"], input=inputs)
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".env").exists()
    env_text = (tmp_path / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-fake-key" in env_text
    assert "GITHUB_TOKEN=github_pat_fake" in env_text
    assert "ORCH_WORKER_BACKEND=managed_agents" in env_text
    assert db_file.exists()


def test_init_rejects_bad_api_key_format(runner, tmp_path, monkeypatch, fake_httpx_get):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")

    # 1st API key bad → re-prompt; good one; auth choice (p); GH token; blank ntfy;
    # worker backend (m).
    inputs = "notvalid\nsk-ant-good\np\ngithub_pat_good\n\nm\n"
    result = runner.invoke(cli, ["init"], input=inputs)
    assert result.exit_code == 0, result.output
    assert "Must start with sk-ant-" in result.output


def test_init_prompts_for_overwrite_when_env_exists(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=existing\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')

    # Say "no" to overwrite
    result = runner.invoke(cli, ["init"], input="n\n")
    assert result.exit_code == 0
    assert "aborted" in result.output
    # .env should be untouched
    assert "existing" in (tmp_path / ".env").read_text()


def test_init_force_skips_overwrite_prompt(runner, tmp_path, monkeypatch, fake_httpx_get):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=old\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")

    inputs = "sk-ant-new\np\ngithub_pat_new\n\nm\n"
    result = runner.invoke(cli, ["init", "--force"], input=inputs)
    assert result.exit_code == 0, result.output
    assert "sk-ant-new" in (tmp_path / ".env").read_text()


def test_init_outside_project_dir_can_abort(runner, tmp_path, monkeypatch):
    """No pyproject.toml in cwd → warning + abort if user says n."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["init"], input="n\n")
    assert result.exit_code == 0
    assert "doesn't look like" in result.output


# --------------------------- dashboard ---------------------------


def test_dashboard_command_no_state_db(runner, tmp_path, monkeypatch):
    """dashboard subcommand should exit nonzero if state.db is missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")

    result = runner.invoke(cli, ["dashboard"])
    # `dashboard` calls dashboard.main() which returns 1 when STATE_DB missing
    assert result.exit_code == 1
    assert "state.db not found" in result.output


# --------------------------- verify-repo ---------------------------


def _stub_verify_pass_cli(monkeypatch):
    from orchestrator.models import CheckResult, VerificationResult

    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/owner/repo",
            default_branch="main",
            auth_mode=auth_mode,
            auth_identity="user:tester",
            checks=[
                CheckResult("read access", True),
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

    monkeypatch.setattr("orchestrator.repo_verify.verify", fake_verify)


def test_verify_repo_cli_success(runner, tmp_path, monkeypatch, with_github_token):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    _stub_verify_pass_cli(monkeypatch)

    result = runner.invoke(cli, ["verify-repo", "github.com/owner/repo"])
    assert result.exit_code == 0, result.output
    assert "✓" in result.output
    assert "cached" in result.output


def test_verify_repo_cli_failure(runner, tmp_path, monkeypatch, with_github_token):
    from orchestrator.models import CheckResult, VerificationResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")

    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/owner/repo",
            checks=[CheckResult("branch protection exists", False, "no rule")],
        )

    monkeypatch.setattr("orchestrator.repo_verify.verify", fake_verify)
    result = runner.invoke(cli, ["verify-repo", "github.com/owner/repo"])
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_verify_repo_cli_no_token(runner, tmp_path, monkeypatch, no_github_token):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    result = runner.invoke(cli, ["verify-repo", "github.com/owner/repo"])
    assert result.exit_code == 1
    assert "GitHub auth" in result.output or "GITHUB" in result.output


def test_verify_repo_cli_bad_url(runner, tmp_path, monkeypatch, with_github_token):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    result = runner.invoke(cli, ["verify-repo", "git@github.com:owner/repo"])
    assert result.exit_code == 1
    assert "SSH" in result.output


# --------------------------- run ---------------------------


def test_run_missing_env(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["run"])
    assert result.exit_code == 1
    assert "Missing .env" in result.output


def test_run_bad_api_key_format(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=not-a-real-key\n")
    result = runner.invoke(cli, ["run"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_run_claude_not_installed(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\n")
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = runner.invoke(cli, ["run"])
    assert result.exit_code == 1
    assert "claude" in result.output.lower()


def test_run_execs_claude_when_ready(runner, tmp_path, monkeypatch):
    """When .env is valid and claude exists, we should call os.execvpe."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\n")
    monkeypatch.setattr("shutil.which", lambda name: "/fake/claude")

    exec_calls = []

    def fake_exec(prog, args, env):
        exec_calls.append({"prog": prog, "args": args, "env": dict(env)})
        # Don't actually exec — simulate by returning
        raise SystemExit(0)

    monkeypatch.setattr("os.execvpe", fake_exec)

    runner.invoke(cli, ["run"])
    # exec was attempted
    assert len(exec_calls) == 1
    assert exec_calls[0]["prog"] == "claude"
    assert "--remote-control" in exec_calls[0]["args"]
    # ANTHROPIC_API_KEY should NOT be in the env passed to claude
    assert "ANTHROPIC_API_KEY" not in exec_calls[0]["env"]


def test_run_with_no_remote_control_flag(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\n")
    monkeypatch.setattr("shutil.which", lambda name: "/fake/claude")

    exec_calls = []

    def fake_exec(prog, args, env):
        exec_calls.append({"args": args})
        raise SystemExit(0)

    monkeypatch.setattr("os.execvpe", fake_exec)

    runner.invoke(cli, ["run", "--no-remote-control"])
    assert "--remote-control" not in exec_calls[0]["args"]


# --------------------------- F-016-U-7: run strips ANTHROPIC_AUTH_TOKEN ---------------------------


def test_run_strips_anthropic_auth_token(runner, tmp_path, monkeypatch):
    """F-016-U-7 (a): a stale ``ANTHROPIC_AUTH_TOKEN`` OAuth token in
    the parent env would shadow the API-key flow we just validated.
    ``orchestrator run`` strips both ``ANTHROPIC_API_KEY`` AND
    ``ANTHROPIC_AUTH_TOKEN`` from the env passed to Claude Code so the
    MCP server sees credentials only from ``.env``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\n")
    monkeypatch.setattr("shutil.which", lambda name: "/fake/claude")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-stale-oauth-token")

    exec_calls = []

    def fake_exec(prog, args, env):
        exec_calls.append({"env": dict(env)})
        raise SystemExit(0)

    monkeypatch.setattr("os.execvpe", fake_exec)

    runner.invoke(cli, ["run"])
    assert len(exec_calls) == 1
    assert "ANTHROPIC_API_KEY" not in exec_calls[0]["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in exec_calls[0]["env"]


def test_run_validates_resolved_key_format(runner, tmp_path, monkeypatch):
    """F-016-U-7 (b): ``run`` refuses to start when the resolved
    ``ANTHROPIC_API_KEY`` doesn't match ``sk-ant-``. The diagnostic
    names the shell-rc files so the operator knows where to look."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-good\n")
    # Shell env shadows the .env with a stale value — exactly the
    # foot-gun this guard exists to catch.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "lkj")

    result = runner.invoke(cli, ["run"])
    assert result.exit_code == 1
    # The spec-mandated diagnostic mentions the shell-rc files
    assert "~/.zshrc" in result.output


def test_run_starts_daemon_when_drive_enabled(runner, tmp_path, monkeypatch):
    """F-016-U-7 unified bootstrap: ``orchestrator run`` auto-spawns
    the watcher daemon as a detached child when ``ORCH_DAEMON_DRIVE=true``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\nORCH_DAEMON_DRIVE=true\n")
    monkeypatch.setattr("shutil.which", lambda name: "/fake/claude")
    # Avoid mutating the test process's env with the .env values.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ORCH_DAEMON_DRIVE", raising=False)

    spawn_calls = []

    def fake_start_daemon_detached(console):
        spawn_calls.append(True)

    monkeypatch.setattr("orchestrator.cli._start_daemon_detached", fake_start_daemon_detached)
    monkeypatch.setattr("os.execvpe", lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)))

    runner.invoke(cli, ["run"])
    assert len(spawn_calls) == 1


def test_run_skips_daemon_when_drive_off(runner, tmp_path, monkeypatch):
    """Without ``ORCH_DAEMON_DRIVE=true`` the bootstrap stays out of the way."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\n")
    monkeypatch.setattr("shutil.which", lambda name: "/fake/claude")
    monkeypatch.delenv("ORCH_DAEMON_DRIVE", raising=False)

    spawn_calls = []

    def fake_start_daemon_detached(console):
        spawn_calls.append(True)

    monkeypatch.setattr("orchestrator.cli._start_daemon_detached", fake_start_daemon_detached)
    monkeypatch.setattr("os.execvpe", lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)))

    runner.invoke(cli, ["run"])
    assert spawn_calls == []


# --------------------------- F-016-U-7: daemon stop ---------------------------


def test_daemon_stop_no_daemon_running(runner, tmp_path, monkeypatch):
    """``orchestrator daemon stop`` with no daemon → exit 1 (per spec)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    result = runner.invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 1
    assert "No daemon running" in result.output


def test_daemon_stop_sigterm_clears_lock(runner, tmp_path, monkeypatch):
    """SIGTERM path: kill is signaled, lock row cleared by the (fake)
    daemon's release_singleton, exit 0."""
    import os as _os
    import signal as _signal

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    state.claim_daemon_lock(path, "fake-holder", pid=99999)

    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # Simulate clean shutdown — the (fake) daemon releases its lock
        # in response to SIGTERM.
        if sig == _signal.SIGTERM:
            state.release_daemon_lock(path, "fake-holder")

    monkeypatch.setattr(_os, "kill", fake_kill)

    result = runner.invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 0, result.output
    assert kill_calls[0] == (99999, _signal.SIGTERM)
    assert "stopped" in result.output.lower()


@pytest.mark.skipif(
    not hasattr(__import__("signal"), "SIGKILL"),
    reason="SIGKILL is POSIX-only; on Windows the fallback path uses SIGTERM (already covered by test_daemon_stop_sigterm_clears_lock)",
)
def test_daemon_stop_sigkill_fallback(runner, tmp_path, monkeypatch):
    """When the daemon ignores SIGTERM, ``stop`` escalates to SIGKILL
    after the 10s window (we shrink the timeout in tests)."""
    import os as _os
    import signal as _signal

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    # Shrink both timeouts so the test doesn't actually wait 10s.
    monkeypatch.setattr("orchestrator.cli._DAEMON_STOP_TIMEOUT_S", 0.2)
    monkeypatch.setattr("orchestrator.cli._DAEMON_STOP_AFTER_KILL_S", 0.2)
    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    state.claim_daemon_lock(path, "fake-holder", pid=99998)

    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # SIGTERM: ignored. SIGKILL: clear the lock (simulating the
        # OS reaping the process and the next claim_daemon_lock
        # taking over the stale row).
        if sig == _signal.SIGKILL:
            state.release_daemon_lock(path, "fake-holder")

    monkeypatch.setattr(_os, "kill", fake_kill)

    result = runner.invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 0, result.output
    # Both signals fired
    signals = [s for _, s in kill_calls]
    assert _signal.SIGTERM in signals
    assert _signal.SIGKILL in signals


def test_daemon_stop_kill_failed_exit_2(runner, tmp_path, monkeypatch):
    """When SIGKILL also fails to clear the lock, exit 2 (per spec)."""
    import os as _os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr("orchestrator.cli._DAEMON_STOP_TIMEOUT_S", 0.1)
    monkeypatch.setattr("orchestrator.cli._DAEMON_STOP_AFTER_KILL_S", 0.1)
    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    state.claim_daemon_lock(path, "fake-holder", pid=99997)

    # Both SIGTERM and SIGKILL ignored — lock stays.
    monkeypatch.setattr(_os, "kill", lambda pid, sig: None)

    result = runner.invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 2, result.output


def test_daemon_stop_no_pid_recorded(runner, tmp_path, monkeypatch):
    """Pre-F-016-U-7 lock rows have no ``pid`` column value — ``stop``
    refuses with exit 1 + a clear diagnostic rather than nuking the row."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    state.claim_daemon_lock(path, "legacy-holder")  # no pid kwarg

    result = runner.invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 1
    assert "no recorded pid" in result.output.lower()


def test_daemon_stop_no_pid_diagnostic_mentions_sqlite_delete(runner, tmp_path, monkeypatch):
    """PR #67 Copilot finding: the legacy-lock diagnostic mustn't claim
    ``orchestrator daemon status`` removes rows (it's read-only). The
    fixed copy points the operator at sqlite for the actual delete."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    state.claim_daemon_lock(path, "legacy-holder")  # no pid kwarg
    result = runner.invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 1
    # The diagnostic must NOT claim daemon status performs deletion.
    assert "remove the row with `orchestrator daemon status`" not in result.output
    # The diagnostic must point to a viable cleanup path: either sqlite
    # delete or stale-heartbeat takeover.
    assert "sqlite" in result.output.lower() or "stale-heartbeat takeover" in result.output.lower()


def test_daemon_stop_sigterm_processlookuperror_with_lock_taken_over(runner, tmp_path, monkeypatch):
    """PR #67 Copilot finding (M-1): when SIGTERM raises
    ProcessLookupError AND release_daemon_lock no-ops because the
    workspace was taken over mid-call, we must NOT exit 0 — that
    would falsely report success while a new daemon owns the
    workspace."""
    import os as _os
    import signal as _signal

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    state.claim_daemon_lock(path, "old-holder", pid=99996)

    def fake_kill(pid, sig):
        # Race: by the time we signal the old pid, it's gone, AND a
        # new daemon has claimed the workspace.
        if sig == _signal.SIGTERM:
            # Force stale takeover by hand: bump the old row's
            # heartbeat back, then claim with a fresh holder.
            import sqlite3
            from datetime import UTC, datetime, timedelta

            with sqlite3.connect(state.STATE_DB) as conn:
                conn.execute(
                    "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ?",
                    (
                        (datetime.now(UTC) - timedelta(seconds=120))
                        .isoformat()
                        .replace("+00:00", "+00:00"),
                        path,
                    ),
                )
                conn.commit()
            state.claim_daemon_lock(path, "new-holder", pid=88888)
            raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(_os, "kill", fake_kill)
    result = runner.invoke(cli, ["daemon", "stop"])
    # NOT exit 0 — the workspace still has a daemon (the new one).
    assert result.exit_code == 2, result.output
    # Diagnostic names the takeover so the operator can stop the
    # current holder.
    assert "took over" in result.output.lower() or "new holder" in result.output.lower()


def test_daemon_stop_sigterm_processlookuperror_with_clean_release(runner, tmp_path, monkeypatch):
    """The benign case: SIGTERM raises ProcessLookupError because the
    daemon was already dead, and our row release clears the lock. Exit
    0 is correct here — the original always-exit-0 was right for this
    branch and wrong for the takeover-race branch."""
    import os as _os
    import signal as _signal

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    state.claim_daemon_lock(path, "dead-holder", pid=99995)

    def fake_kill(pid, sig):
        if sig == _signal.SIGTERM:
            raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(_os, "kill", fake_kill)
    result = runner.invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 0, result.output
    assert state.get_daemon_lock(path) is None
