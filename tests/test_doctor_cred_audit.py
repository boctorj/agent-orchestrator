"""Snapshot tests for the docker worker credential audit.

The unit description says "doctor output matches the audit format
snapshot in both auth modes." That's what this file pins.

The audit is produced by `build_cred_audit()` and rendered by
`CredAudit.render()`. We construct the audit with explicit inputs
(host_env, home_dir, image, network, workdir) so the snapshot is
deterministic — no real `$HOME`, no real `os.environ`, no real CWD
leakage.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator.cli import cli
from orchestrator.workers.docker_claude_code import build_cred_audit

# ---------------------------------------------------------------------------
# Common fixture inputs — render against fixed values so snapshots are stable.
# ---------------------------------------------------------------------------


# Intentional non-existent home path: with the F-001-U-4 receipt fix
# (PR #11 review SUGGESTION 3) the audit only lists NEVER-MOUNTED paths
# that actually exist on the host. /home/lead doesn't exist on the test
# machine, so the NEVER list correctly renders as "(none present on host)".
HOME = Path("/home/lead")
WORKDIR = Path("/repo")
IMAGE = "orchestrator/worker:latest"
NETWORK = "orch-net"


def _render(host_env: dict[str, str]) -> str:
    audit = build_cred_audit(
        host_env=host_env,
        home_dir=HOME,
        workdir=WORKDIR,
        image=IMAGE,
        network=NETWORK,
    )
    return audit.render()


# ---------------------------------------------------------------------------
# Snapshot — API-key mode
# ---------------------------------------------------------------------------


def test_audit_api_key_mode_snapshot():
    host_env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "GITHUB_TOKEN": "ghp_test",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "SSH_AUTH_SOCK": "/tmp/ssh.sock",
        # Vars that AREN'T sensitive (PATH, HOME) shouldn't appear in
        # the "dropped" list to keep the audit signal-to-noise high.
        "EDITOR": "vim",
    }
    rendered = _render(host_env)

    # Path-bearing lines are computed from the same Path objects the
    # audit uses so the snapshot matches identically on every OS
    # (Windows renders Path with backslashes; Unix with slashes).
    workspace_mount = f"  + {WORKDIR} -> /workspace (rw)"
    sessions_mount = f"  + {HOME / '.claude' / 'sessions'} -> /home/agent/.claude/sessions (rw)"

    expected = "\n".join(
        [
            "Worker backend: docker",
            "Auth: API key",
            "Image: orchestrator/worker:latest",
            "Network: orch-net",
            "",
            "Env vars passed into worker:",
            "  + GITHUB_TOKEN",
            "  + ANTHROPIC_API_KEY",
            "",
            "Env vars dropped (host has them, worker will NOT receive):",
            "  - AWS_ACCESS_KEY_ID",
            "  - SSH_AUTH_SOCK",
            "",
            "Mounts into worker:",
            workspace_mount,
            sessions_mount,
            "  + tmpfs -> /tmp (rw, 512M)",
            "  + tmpfs -> /home/agent/.cache (rw, 512M)",
            "",
            "Paths NEVER mounted (host has them, worker will NOT see them):",
            # F-001-U-4: filtered by existence — HOME=/home/lead has none.
            "  (none present on host)",
        ]
    )
    assert rendered == expected, (
        f"audit snapshot drifted:\n--- expected ---\n{expected}\n--- got ---\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Snapshot — OAuth mode
# ---------------------------------------------------------------------------


def test_audit_oauth_mode_snapshot():
    host_env = {
        # NO ANTHROPIC_API_KEY -> OAuth mode
        "GITHUB_TOKEN": "ghp_test",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "SSH_AUTH_SOCK": "/tmp/ssh.sock",
        "OPENAI_API_KEY": "sk-openai-test",
    }
    rendered = _render(host_env)

    # Path-bearing lines computed from the same Path objects the audit
    # uses, so the snapshot matches on Windows (backslashes) as well.
    workspace_mount = f"  + {WORKDIR} -> /workspace (rw)"
    claude_mount = f"  + {HOME / '.claude'} -> /home/agent/.claude (ro)"
    sessions_mount = f"  + {HOME / '.claude' / 'sessions'} -> /home/agent/.claude/sessions (rw)"

    expected = "\n".join(
        [
            "Worker backend: docker",
            "Auth: claude.ai OAuth",
            "Image: orchestrator/worker:latest",
            "Network: orch-net",
            "",
            "Env vars passed into worker:",
            "  + GITHUB_TOKEN",
            "",
            "Env vars dropped (host has them, worker will NOT receive):",
            "  - AWS_ACCESS_KEY_ID",
            "  - OPENAI_API_KEY",
            "  - SSH_AUTH_SOCK",
            "",
            "Mounts into worker:",
            workspace_mount,
            claude_mount,
            sessions_mount,
            "  + tmpfs -> /tmp (rw, 512M)",
            "  + tmpfs -> /home/agent/.cache (rw, 512M)",
            "",
            "Paths NEVER mounted (host has them, worker will NOT see them):",
            # F-001-U-4: filtered by existence — HOME=/home/lead has none.
            "  (none present on host)",
        ]
    )
    assert rendered == expected, (
        f"audit snapshot drifted:\n--- expected ---\n{expected}\n--- got ---\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Behavioural anti-regressions — the audit is the receipts, so it must
# reflect what the worker actually does.
# ---------------------------------------------------------------------------


def test_audit_lists_no_sensitive_env_when_host_is_clean():
    """No AWS_/SSH_/cloud-sdk vars on the host -> empty dropped list."""
    rendered = _render({"GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-x"})
    assert "(none present on host)" in rendered


def test_audit_mode_string_uses_user_facing_labels():
    """The "Auth:" line must say "API key" or "claude.ai OAuth" — the
    exact strings the unit description names for spawn-time logging."""
    api = _render({"ANTHROPIC_API_KEY": "sk-x"})
    oauth = _render({})
    assert "Auth: API key" in api
    assert "Auth: claude.ai OAuth" in oauth


# ---------------------------------------------------------------------------
# CLI integration — `orchestrator doctor` should surface the audit when
# the docker backend is selected. Probes are subprocess-mocked so no real
# Docker is required.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_httpx_get(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"login": "fakeuser"}

    def _get(url, headers=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr("httpx.get", _get)


@pytest.fixture
def stub_docker_subprocess(monkeypatch):
    """Make every `docker ...` subprocess call appear to succeed."""

    class _Proc:
        def __init__(self, stdout="ok", returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def _run(argv, **kw):
        # Per-subcommand return values
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stdout="sha256:abcdef0123456789", returncode=0)
        if argv[:2] == ["docker", "run"]:
            return _Proc(stdout="claude 1.2.3", returncode=0)
        return _Proc(stdout="", returncode=0)

    monkeypatch.setattr("orchestrator.workers.docker_claude_code.subprocess.run", _run)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


def test_doctor_renders_cred_audit_under_docker_backend(
    monkeypatch, tmp_path, fake_httpx_get, stub_docker_subprocess
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-fake\nGITHUB_TOKEN=github_pat_fake\n")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    db_file = tmp_path / "state.db"
    monkeypatch.setattr("orchestrator.state.STATE_DB", db_file)
    from orchestrator import state

    state.init_db()

    monkeypatch.setenv("ORCH_WORKER_BACKEND", "docker")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

    result = CliRunner().invoke(cli, ["doctor"])

    # Doctor surfaces the audit (looks for stable section headers).
    assert "Worker credential audit" in result.output
    assert "Auth: API key" in result.output
    assert "Env vars passed into worker:" in result.output
    assert "GITHUB_TOKEN" in result.output
    assert "Paths NEVER mounted" in result.output
    # Probes ran via the stub and report success.
    assert "docker daemon reachable" in result.output
    assert "image " in result.output  # "image orchestrator/worker:latest built"
    assert "claude --version inside container" in result.output


def test_doctor_warns_when_repo_needs_registry_passthrough(
    monkeypatch, tmp_path, fake_httpx_get, stub_docker_subprocess
):
    """F-001-U-4: a repo whose `package.json` declares an internal
    registry but has no passthrough wired surfaces a yellow warning
    in the doctor output."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-fake\nGITHUB_TOKEN=github_pat_fake\n")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    (tmp_path / "package.json").write_text(
        '{"name": "test", "registry": "https://artifactory.internal/repo/"}'
    )
    db_file = tmp_path / "state.db"
    monkeypatch.setattr("orchestrator.state.STATE_DB", db_file)
    from orchestrator import state

    state.init_db()

    monkeypatch.setenv("ORCH_WORKER_BACKEND", "docker")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    # Force a clean home so no real ~/.npmrc / .docker/config.json silences
    # the warning. Patch Path.home() directly — setting $HOME alone isn't
    # enough on Windows, where Path.home() reads $USERPROFILE.
    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: clean_home)
    monkeypatch.setenv("HOME", str(clean_home))
    monkeypatch.delenv("ORCH_WORKER_EXTRA_MOUNTS", raising=False)

    result = CliRunner().invoke(cli, ["doctor"])
    assert "Internal-registry passthrough" in result.output
    assert "package.json" in result.output
    assert "artifactory.internal" in result.output


def test_doctor_skips_audit_under_managed_agents_backend(monkeypatch, tmp_path, fake_httpx_get):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-fake\nGITHUB_TOKEN=github_pat_fake\n")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    db_file = tmp_path / "state.db"
    monkeypatch.setattr("orchestrator.state.STATE_DB", db_file)
    from orchestrator import state

    state.init_db()

    monkeypatch.delenv("ORCH_WORKER_BACKEND", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    result = CliRunner().invoke(cli, ["doctor"])

    assert "Worker backend: managed_agents" in result.output
    # The detailed audit should NOT run for managed_agents.
    assert "Worker credential audit" not in result.output


def test_run_doctor_probes_reports_missing_image(monkeypatch):
    """If `docker image inspect` returns non-zero, the probe reports a
    fix-it hint pointing at the build command."""
    from orchestrator.workers.docker_claude_code import run_doctor_probes

    class _Proc:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def _run(argv, **kw):
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stderr="Error: No such image", returncode=1)
        return _Proc(returncode=0)

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    results = run_doctor_probes(image="orchestrator/worker:latest", run=_run)

    image_probe = next(r for r in results if "built" in r.name)
    assert image_probe.ok is False
    assert "docker build" in image_probe.detail


def test_run_doctor_probes_reports_no_docker_cli(monkeypatch):
    from orchestrator.workers.docker_claude_code import run_doctor_probes

    monkeypatch.setattr("shutil.which", lambda name: None)
    # subprocess.run shouldn't even be called once the CLI isn't found.
    sentinel = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _run(*a, **kw):
        return sentinel

    results = run_doctor_probes(run=_run)
    assert len(results) == 1
    assert results[0].ok is False
    assert "docker" in results[0].detail.lower()
