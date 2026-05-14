"""Snapshot tests for `orchestrator init --dry-run` (F-001-U-5).

The wizard now has a two-branch worker-backend prompt — managed_agents
(default) and docker — and writes `ORCH_WORKER_BACKEND` into the planned
.env. We pin the rendered output of `--dry-run` on both branches so
later changes can't silently break the auto-generated section, and so
the docker branch's "daemon not reachable" warning continues to fire
when expected.

We don't byte-compare the entire transcript (Click prompt formatting +
Rich color codes drift across versions). We assert on the **planned
.env block** that the wizard prints between the two horizontal-rule
lines — that's the user-actionable artefact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_httpx_get(monkeypatch):
    """Make orchestrator.cli's httpx.get return a fake 200."""

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"login": "fakeuser"}

    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse())


def _extract_planned_env_block(output: str) -> list[str]:
    """Pull the wizard's rendered .env block out of CLI output.

    The wizard prints the block between two `---...` lines under the
    "--dry-run: planned .env contents" heading.

    Rich+CliRunner strip ANSI by default, so we work on plain text.
    """
    start = output.find("--dry-run: planned .env contents")
    assert start != -1, f"no --dry-run heading in output:\n{output}"
    after_heading = output[start:]
    # First rule line after the heading
    first_rule = after_heading.find("\n----")
    assert first_rule != -1, f"no first rule line after heading:\n{after_heading}"
    body_start = after_heading.find("\n", first_rule + 1) + 1
    second_rule = after_heading.find("\n----", body_start)
    assert second_rule != -1, f"no closing rule line after heading:\n{after_heading}"
    body = after_heading[body_start:second_rule]
    return body.splitlines()


def _common_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    # State DB stays in the temp dir even though --dry-run shouldn't init it
    # (the test asserts on absence below).
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")


def test_init_dry_run_managed_agents_branch(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_httpx_get: None,
) -> None:
    """Default backend branch: ORCH_WORKER_BACKEND=managed_agents."""
    _common_setup(tmp_path, monkeypatch)
    # API key, auth (p), token, ntfy (blank), backend (m for managed_agents).
    inputs = "sk-ant-fake\np\ngithub_pat_fake\n\nm\n"
    result = runner.invoke(cli, ["init", "--dry-run"], input=inputs)
    assert result.exit_code == 0, result.output

    block = _extract_planned_env_block(result.output)

    # Required env keys are present with expected values.
    joined = "\n".join(block)
    assert "ANTHROPIC_API_KEY=sk-ant-fake" in joined
    assert "GITHUB_TOKEN=github_pat_fake" in joined
    assert "GITHUB_APP_ID=" in joined
    assert "GITHUB_APP_INSTALLATION_ID=" in joined
    assert "GITHUB_APP_PRIVATE_KEY_PATH=" in joined
    assert "NTFY_TOPIC=" in joined
    assert "ORCH_WORKER_BACKEND=managed_agents" in joined

    # The dry-run footer told the user nothing was written.
    assert "No files were written" in result.output

    # Filesystem invariants: --dry-run is read-only.
    assert not (tmp_path / ".env").exists(), "--dry-run wrote .env"
    assert not (tmp_path / "state.db").exists(), "--dry-run created state.db"


def test_init_dry_run_docker_branch(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_httpx_get: None,
) -> None:
    """Docker branch: ORCH_WORKER_BACKEND=docker + the daemon warning fires."""
    _common_setup(tmp_path, monkeypatch)
    # Pretend docker is on PATH so the wizard reaches `docker version`,
    # then return a non-zero exit from `docker version` so the
    # "daemon not reachable" warning is exercised.
    monkeypatch.setattr("shutil.which", lambda name: "/fake/" + name)

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeProc())

    # API key, auth (p), token, ntfy (blank), backend (d for docker).
    inputs = "sk-ant-fake\np\ngithub_pat_fake\n\nd\n"
    result = runner.invoke(cli, ["init", "--dry-run"], input=inputs)
    assert result.exit_code == 0, result.output

    block = _extract_planned_env_block(result.output)
    joined = "\n".join(block)
    assert "ORCH_WORKER_BACKEND=docker" in joined

    # Daemon warning surfaced. Match a substring that's stable across
    # the docker CLI's exact error wording.
    assert "Docker daemon not reachable" in result.output
    assert "orchestrator doctor" in result.output

    # Still a dry run: no files written.
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "state.db").exists()


def test_init_dry_run_docker_when_docker_cli_missing(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_httpx_get: None,
) -> None:
    """The other docker-warning branch: `docker` not on PATH at all."""
    _common_setup(tmp_path, monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: None)

    inputs = "sk-ant-fake\np\ngithub_pat_fake\n\nd\n"
    result = runner.invoke(cli, ["init", "--dry-run"], input=inputs)
    assert result.exit_code == 0, result.output
    assert "`docker` CLI not found on PATH" in result.output

    block = _extract_planned_env_block(result.output)
    assert "ORCH_WORKER_BACKEND=docker" in "\n".join(block)


def test_init_dry_run_docker_when_daemon_reachable_no_warning(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_httpx_get: None,
) -> None:
    """Happy docker path — daemon answers; warning does NOT fire."""
    _common_setup(tmp_path, monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/fake/" + name)

    class FakeProc:
        returncode = 0
        stdout = "25.0.3\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeProc())

    inputs = "sk-ant-fake\np\ngithub_pat_fake\n\nd\n"
    result = runner.invoke(cli, ["init", "--dry-run"], input=inputs)
    assert result.exit_code == 0, result.output
    assert "Docker daemon not reachable" not in result.output
    assert "`docker` CLI not found" not in result.output


def test_init_dry_run_ntfy_suggested_value_is_deterministic(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_httpx_get: None,
) -> None:
    """Snapshot stability: --dry-run shows a fixed placeholder, not random hex.

    Re-running --dry-run with identical input must produce identical
    visible output for snapshot tests — including the suggested
    NTFY_TOPIC. The wizard substitutes a fixed `<random>` token under
    --dry-run so the suggestion doesn't churn the snapshot every run.
    """
    _common_setup(tmp_path, monkeypatch)
    inputs = "sk-ant-fake\np\ngithub_pat_fake\n\nm\n"

    r1 = runner.invoke(cli, ["init", "--dry-run"], input=inputs)
    r2 = runner.invoke(cli, ["init", "--dry-run"], input=inputs)
    assert r1.exit_code == 0 and r2.exit_code == 0

    # Pull just the planned-.env block; outer prompt formatting differs by
    # Click version but the block itself is stable.
    assert _extract_planned_env_block(r1.output) == _extract_planned_env_block(r2.output)
    # And the suggested-topic line uses the deterministic placeholder.
    assert "agent-orch-<random>" in r1.output


def test_init_writes_orch_worker_backend_for_docker_branch(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_httpx_get: None,
) -> None:
    """Smoke check the non-dry-run docker branch writes the right env line."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')
    monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "state.db")
    # Suppress the daemon warning (irrelevant to what's written).
    monkeypatch.setattr("shutil.which", lambda name: "/fake/" + name)

    class FakeProc:
        returncode = 0
        stdout = "25.0.3"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeProc())

    inputs = "sk-ant-fake\np\ngithub_pat_fake\n\nd\n"
    result = runner.invoke(cli, ["init"], input=inputs)
    assert result.exit_code == 0, result.output
    env_text = (tmp_path / ".env").read_text()
    # The env line uses the canonical key.
    assert re.search(r"^ORCH_WORKER_BACKEND=docker\s*$", env_text, re.MULTILINE), env_text
