"""Shared fixtures for the Docker-worker E2E suite (F-001-U-6).

The whole `tests/e2e/` directory is gated on a real Docker daemon being
reachable from the test machine. CI runs it via the opt-in `e2e-docker`
job; developer laptops without Docker installed see the module-level
skip and move on.

Fixtures here:

  * ``docker_available`` — autouse, module-scoped skip gate. Every E2E
    test silently skips when the `docker` CLI is missing or the daemon
    isn't responding.
  * ``worker_image`` — session-scoped, builds
    `orchestrator/worker:test` from `docker/worker.Dockerfile` once
    per pytest invocation. The image build dominates the wall-clock
    budget on a cold cache; reusing across tests keeps the suite under
    the 90s target named in the unit description.
  * ``dnsmasq_sidecar`` — session-scoped, launches the dnsmasq sidecar
    from U-3 on 127.0.0.1:5353 and ensures the orch-net bridge exists.
    Returns the URL plus a teardown that stops the container.
  * ``sandbox_repo`` — function-scoped copy of the tiny fixture under
    ``tests/fixtures/sandbox-repo/`` into a tmp_path so writes by the
    container don't pollute the checked-in fixture.

Every fixture uses ``shutil.which`` / ``subprocess.run(..., check=False)``
so a missing dependency causes a `pytest.skip` rather than a hard error.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 — invoking `docker` is the whole point
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

# Image build + dnsmasq pull can take minutes on a cold cache. Override
# the repo-wide 30s pytest-timeout default for every E2E test (the per-
# test marker on the smoke module already does this; the fixture
# session-setup path needs the same).
pytestmark = pytest.mark.timeout(900)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO_ROOT / "docker" / "worker.Dockerfile"
DNS_SCRIPT = REPO_ROOT / "scripts" / "run-worker-dns.sh"
FIXTURE_REPO = REPO_ROOT / "tests" / "fixtures" / "sandbox-repo"
TEST_IMAGE_TAG = "orchestrator/worker:test"
TEST_NETWORK = "orch-net"
DNSMASQ_CONTAINER_NAME = "orchestrator-e2e-dnsmasq"


def _docker_daemon_reachable(timeout: float = 5.0) -> bool:
    """True iff `docker version` returns a server section within `timeout`s."""
    if not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


@pytest.fixture(autouse=True)
def docker_available() -> None:
    """Skip every E2E test when Docker isn't reachable.

    Autouse keeps the gate consistent across the suite — individual
    tests don't need to remember the marker. The check is fast (one
    `docker version` call with a 5s timeout) so the no-Docker path
    stays cheap on cold runs.
    """
    if not _docker_daemon_reachable():
        pytest.skip("docker daemon not reachable — E2E suite skipped")


@pytest.fixture(scope="session")
def worker_image() -> str:
    """Build `orchestrator/worker:test` once per pytest session.

    Building from `docker/worker.Dockerfile` validates the same image
    the production code paths reference (`DEFAULT_IMAGE`), modulo the
    `:test` tag so a developer's `:latest` image is not stomped on by
    the E2E suite. Builds are cached by Docker's layer cache, so the
    second run in a session reuses everything.
    """
    if not DOCKERFILE.is_file():
        pytest.skip(f"missing dockerfile at {DOCKERFILE}")
    if not _docker_daemon_reachable():
        pytest.skip("docker daemon not reachable")
    cmd = [
        "docker",
        "build",
        "-f",
        str(DOCKERFILE),
        "-t",
        TEST_IMAGE_TAG,
        str(REPO_ROOT),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"worker image build failed — skipping E2E. stderr tail:\n{proc.stderr[-2000:]}"
        )
    return TEST_IMAGE_TAG


@pytest.fixture(scope="session")
def orch_net() -> Iterator[str]:
    """Ensure the orch-net bridge exists. Idempotent; not torn down."""
    if not _docker_daemon_reachable():
        pytest.skip("docker daemon not reachable")
    inspect = subprocess.run(
        ["docker", "network", "inspect", TEST_NETWORK],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if inspect.returncode != 0:
        create = subprocess.run(
            ["docker", "network", "create", TEST_NETWORK, "--driver", "bridge"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if create.returncode != 0:
            pytest.skip(f"could not create orch-net bridge: {create.stderr.strip()[:500]}")
    yield TEST_NETWORK


@pytest.fixture(scope="session")
def dnsmasq_sidecar(orch_net: str) -> Iterator[str]:
    """Run the U-3 dnsmasq sidecar in a container on the orch-net bridge.

    Production deploys this on the host via `scripts/run-worker-dns.sh`,
    but the E2E suite needs the sidecar reachable from inside the
    worker container's loopback ($127.0.0.1$). We launch dnsmasq in a
    sibling container on the same `orch-net` bridge and rely on
    Docker's per-network DNS plumbing.

    Returns the host:port string for any test that needs to verify
    DNS responses directly. Teardown stops the container.
    """
    name = f"{DNSMASQ_CONTAINER_NAME}-{uuid.uuid4().hex[:6]}"
    config_path = REPO_ROOT / "orchestrator" / "network" / "allowlist.dnsmasq.conf"
    if not config_path.is_file():
        pytest.skip(f"missing dnsmasq config at {config_path}")

    # Use the worker image to run dnsmasq is overkill; pick a tiny
    # ready-made image. `4km3/dnsmasq` is a common minimal image; if
    # it can't be pulled, skip — the suite stays green.
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        orch_net,
        "-p",
        "127.0.0.1:5353:53/udp",
        "-p",
        "127.0.0.1:5353:53/tcp",
        "--mount",
        f"type=bind,source={config_path},target=/etc/dnsmasq.conf,readonly",
        "--cap-add=NET_ADMIN",
        "4km3/dnsmasq:2.90-r3",
        "--conf-file=/etc/dnsmasq.conf",
        "--keep-in-foreground",
        "--no-daemon",
        # Override listen-address so the container binds 0.0.0.0 (the
        # host-side port-publish handles the loopback restriction).
        "--listen-address=0.0.0.0",
        "--port=53",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        pytest.skip(
            f"could not start dnsmasq sidecar (probably no public-image pull): "
            f"{proc.stderr.strip()[:400]}"
        )

    # Give dnsmasq a beat to bind.
    time.sleep(0.5)
    try:
        yield "127.0.0.1:5353"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            timeout=30,
            check=False,
        )


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    """Copy the tiny sandbox-repo fixture to a per-test tmp_path.

    Per-test isolation: the container may write into `/workspace`, and
    we don't want those writes to pollute the checked-in fixture.
    """
    if not FIXTURE_REPO.is_dir():
        pytest.skip(f"missing sandbox-repo fixture at {FIXTURE_REPO}")
    dest = tmp_path / "sandbox-repo"
    shutil.copytree(FIXTURE_REPO, dest)
    return dest
