"""Doctor's fourth probe: the orch-net Docker network exists (F-001-U-3).

Folded from PR #11 reviewer SUGGESTION 2: `audit.render()` prints
'Network: orch-net' like a fact, but if the user skipped
`scripts/run-worker-dns.sh` the spawn errors with
'network orch-net not found'. The probe surfaces the gap pre-flight.

The probe runs `docker network inspect <name>` and reports pass/fail
based on the exit code. Tests mock the subprocess call so no real
docker is required.
"""

from __future__ import annotations

import subprocess

import pytest

from orchestrator.workers.docker_claude_code import (
    DEFAULT_IMAGE,
    DEFAULT_NETWORK,
    run_doctor_probes,
)

# ---------------------------------------------------------------------------
# Fake subprocess.run — per-subcommand return values so each probe runs.
# ---------------------------------------------------------------------------


class _Proc:
    """Minimal CompletedProcess look-alike for the runner injection."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_runner(*, network_returncode: int, network_stdout: str = "", network_stderr: str = ""):
    """Return a fake subprocess.run that drives every probe through to
    the network-inspect step, then yields the requested outcome there."""

    def _run(argv, **_kwargs):
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stdout="sha256:abcdef0123456789abcdef", returncode=0)
        if argv[:2] == ["docker", "run"]:
            # `claude --version` invocation
            return _Proc(stdout="claude 1.2.3", returncode=0)
        if argv[:3] == ["docker", "network", "inspect"]:
            return _Proc(
                stdout=network_stdout, stderr=network_stderr, returncode=network_returncode
            )
        return _Proc(returncode=0)

    return _run


@pytest.fixture
def fake_docker_on_path(monkeypatch):
    """Pretend `docker` is on PATH so the probes don't short-circuit."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


# ---------------------------------------------------------------------------
# Pass case — network present.
# ---------------------------------------------------------------------------


def test_network_probe_passes_when_inspect_succeeds(fake_docker_on_path):
    runner = _make_runner(network_returncode=0, network_stdout="orch-net\n")
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)

    network_probe = _network_probe(results)
    assert network_probe.ok is True
    # Detail surfaces the network name so the user can confirm at a glance.
    assert "orch-net" in network_probe.detail


# ---------------------------------------------------------------------------
# Fail case — network missing — must give a usable fix-it.
# ---------------------------------------------------------------------------


def test_network_probe_fails_with_fix_it_when_missing(fake_docker_on_path):
    runner = _make_runner(
        network_returncode=1,
        network_stderr="Error: No such network: orch-net",
    )
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)

    network_probe = _network_probe(results)
    assert network_probe.ok is False
    # Fix-it must name the docker command OR the launcher script — both
    # let the user proceed without having to read source.
    detail = network_probe.detail.lower()
    assert "docker network create" in detail or "run-worker-dns" in detail, (
        f"missing fix-it hint in detail: {network_probe.detail!r}"
    )
    # And the failing network name must appear so the user can paste it.
    assert DEFAULT_NETWORK in network_probe.detail or DEFAULT_NETWORK in network_probe.name


# ---------------------------------------------------------------------------
# Subprocess exception — exceptions don't bubble out of the doctor; they
# convert to a failed probe so the human sees a row instead of a stack.
# ---------------------------------------------------------------------------


def test_network_probe_reports_exception_as_failure(fake_docker_on_path):
    def _run(argv, **_kwargs):
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stdout="sha256:abc", returncode=0)
        if argv[:2] == ["docker", "run"]:
            return _Proc(stdout="claude 1.0", returncode=0)
        if argv[:3] == ["docker", "network", "inspect"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
        return _Proc(returncode=0)

    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=_run)
    network_probe = _network_probe(results)
    assert network_probe.ok is False
    # The exception text should make it into the detail so the user
    # can see WHY (timeout vs daemon-down vs permission).
    assert network_probe.detail, "exception detail must not be empty"


# ---------------------------------------------------------------------------
# Custom network name — DEFAULT_NETWORK is the obvious case, but the
# `network=` kwarg lets ops point at a sibling bridge. Pin that.
# ---------------------------------------------------------------------------


def test_network_probe_uses_supplied_network_name(fake_docker_on_path):
    captured: list[list[str]] = []

    def _run(argv, **_kwargs):
        captured.append(list(argv))
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stdout="sha256:abc", returncode=0)
        if argv[:2] == ["docker", "run"]:
            return _Proc(stdout="claude 1.0", returncode=0)
        if argv[:3] == ["docker", "network", "inspect"]:
            return _Proc(stdout="custom-net\n", returncode=0)
        return _Proc(returncode=0)

    run_doctor_probes(image=DEFAULT_IMAGE, network="custom-net", run=_run)

    inspect_calls = [c for c in captured if c[:3] == ["docker", "network", "inspect"]]
    assert inspect_calls, "network probe never ran `docker network inspect`"
    assert "custom-net" in inspect_calls[0], (
        f"probe inspected the wrong network: {inspect_calls[0]!r}"
    )


# ---------------------------------------------------------------------------
# Probe is the *fourth* probe — comes after daemon/image/claude. Pin
# the ordering so the doctor renders consistently.
# ---------------------------------------------------------------------------


def test_network_probe_runs_after_image_and_claude_probes(fake_docker_on_path):
    runner = _make_runner(network_returncode=0, network_stdout="orch-net\n")
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)

    names = [r.name for r in results]
    # 4-probe order: CLI on PATH, daemon, image, claude, network.
    # (CLI-on-PATH is an early return; under the fake on-PATH fixture
    # it appears + we then run the rest.)
    network_idx = next(i for i, n in enumerate(names) if "network" in n and DEFAULT_NETWORK in n)
    claude_idx = next(i for i, n in enumerate(names) if "claude --version" in n)
    image_idx = next(i for i, n in enumerate(names) if "image" in n and "built" in n)
    assert image_idx < claude_idx < network_idx, (
        f"probes out of order: {names!r} — network must come after image + claude"
    )


# ---------------------------------------------------------------------------
# Helper.
# ---------------------------------------------------------------------------


def _network_probe(results):
    matches = [r for r in results if "network" in r.name and DEFAULT_NETWORK in r.name]
    assert matches, f"no network probe in results: {[r.name for r in results]!r}"
    assert len(matches) == 1, f"multiple network probes: {[r.name for r in matches]!r}"
    return matches[0]
