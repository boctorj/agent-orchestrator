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
