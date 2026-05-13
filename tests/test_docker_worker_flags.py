"""Hardening-flag tests for the Docker worker.

The unit description lists exactly which `docker run` flags must appear
on every worker invocation:

    --cap-drop=ALL
    --security-opt=no-new-privileges
    --read-only
    --user 1000:1000
    --memory=4g
    --cpus=2
    --pids-limit=512
    --network=orch-net

This file pins each one. The flags are security invariants — a
regression here is a real security regression, not a cosmetic one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.workers.docker_claude_code import (
    AGENT_GID,
    AGENT_UID,
    DEFAULT_CPUS,
    DEFAULT_DNS,
    DEFAULT_DNS_SEARCH,
    DEFAULT_MEMORY,
    DEFAULT_NETWORK,
    DEFAULT_PIDS_LIMIT,
    DockerClaudeCodeWorker,
)
from tests.conftest import _make_worker  # _FakeProc not used here, just _make_worker


@pytest.fixture
def worker(tmp_path: Path) -> DockerClaudeCodeWorker:
    # Thin alias for the shared `_make_worker` helper so existing
    # `argv_oauth` / `argv_api_key` fixtures keep their names.
    return _make_worker(tmp_path)


@pytest.fixture
def argv_oauth(worker: DockerClaudeCodeWorker) -> list[str]:
    return worker.build_docker_argv(["claude", "-p", "hi"], host_env={"GITHUB_TOKEN": "ghp_x"})


@pytest.fixture
def argv_api_key(worker: DockerClaudeCodeWorker) -> list[str]:
    return worker.build_docker_argv(
        ["claude", "-p", "hi"],
        host_env={"ANTHROPIC_API_KEY": "sk-ant-x", "GITHUB_TOKEN": "ghp_x"},
    )


# ---------------------------------------------------------------------------
# Token-level presence checks: every flag must appear verbatim in argv.
# ---------------------------------------------------------------------------


def _flag_in_argv(argv: list[str], flag: str) -> bool:
    """True if `flag` appears as a standalone argv token."""
    return flag in argv


@pytest.mark.parametrize("argv_fixture", ["argv_oauth", "argv_api_key"])
@pytest.mark.parametrize(
    "flag",
    [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        f"--memory={DEFAULT_MEMORY}",
        f"--cpus={DEFAULT_CPUS}",
        f"--pids-limit={DEFAULT_PIDS_LIMIT}",
        f"--network={DEFAULT_NETWORK}",
        # F-001-U-3 — DNS allowlist sidecar
        f"--dns={DEFAULT_DNS}",
        f"--dns-search={DEFAULT_DNS_SEARCH}",
    ],
)
def test_hardening_flag_present(request, argv_fixture, flag):
    argv = request.getfixturevalue(argv_fixture)
    assert _flag_in_argv(argv, flag), f"hardening flag {flag!r} missing from argv: {argv!r}"


# ---------------------------------------------------------------------------
# Composite checks: --user takes two argv tokens; verify both halves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv_fixture", ["argv_oauth", "argv_api_key"])
def test_user_flag_is_1000_1000(request, argv_fixture):
    argv = request.getfixturevalue(argv_fixture)
    # The default is "--user 1000:1000" rendered as two tokens, but a
    # `--user=1000:1000` single-token form is equally valid; both pass.
    expected = f"{AGENT_UID}:{AGENT_GID}"
    flat = " ".join(argv)
    assert f"--user {expected}" in flat or f"--user={expected}" in flat, (
        f"--user {expected} not found in argv: {argv!r}"
    )


# ---------------------------------------------------------------------------
# Anti-regression: the docker invocation must start with the safe prefix
# (`docker run --rm`) and target an image, not arbitrary user input.
# ---------------------------------------------------------------------------


def test_argv_starts_with_docker_run_rm(argv_oauth):
    assert argv_oauth[:3] == ["docker", "run", "--rm"]


def test_argv_ends_with_image_then_command(worker, argv_oauth):
    """The image token must immediately precede the `claude` invocation."""
    # Find the image token (assumes default image; works for any worker).
    image = worker.image
    assert image in argv_oauth
    image_idx = argv_oauth.index(image)
    # `claude` must be the first token AFTER the image.
    assert image_idx < len(argv_oauth) - 1
    assert argv_oauth[image_idx + 1] == "claude"


# ---------------------------------------------------------------------------
# Defense-in-depth: ensure rootfs and capability defaults are not
# overridable by mistake — they're declared as constants on the module.
# ---------------------------------------------------------------------------


def test_no_writable_rootfs_flag(argv_oauth):
    """No `--read-write` / removal of `--read-only` shall appear."""
    assert "--read-write" not in argv_oauth
    assert any(tok == "--read-only" for tok in argv_oauth)


def test_no_privileged_flag(argv_oauth):
    """The hardened spawn must NEVER set `--privileged`."""
    assert "--privileged" not in argv_oauth
    # Also no `--cap-add` lurking — the description is `--cap-drop=ALL`.
    assert not any(tok.startswith("--cap-add") for tok in argv_oauth)


def test_tmpfs_mounts_present_for_writable_scratch(argv_oauth):
    """`--read-only` rootfs requires tmpfs mounts for the agent's scratch
    paths or claude can't write a single byte."""
    tmpfs_targets = [tok for tok in argv_oauth if tok.startswith("--tmpfs=")]
    joined = " ".join(tmpfs_targets)
    assert "/tmp" in joined, f"tmpfs /tmp missing: {tmpfs_targets!r}"
    assert "/home/agent/.cache" in joined, f"tmpfs /home/agent/.cache missing: {tmpfs_targets!r}"
