"""Shared fixtures for the Docker-worker E2E suite (F-001-U-6).

The whole `tests/e2e/` directory is gated on a real Docker daemon being
reachable from the test machine. CI runs it via the opt-in `e2e-docker`
job (workflow file pending — see PR #16 body); developer laptops
without Docker installed or without ``ORCH_RUN_E2E=1`` see the
module-level skip and move on.

Fixtures here:

  * ``docker_available`` — autouse, module-scoped skip gate. Every E2E
    test silently skips when ``ORCH_RUN_E2E`` isn't set OR the daemon
    isn't responding.
  * ``worker_image`` — session-scoped, builds
    `orchestrator/worker:test` from `docker/worker.Dockerfile` once
    per pytest invocation. The image build dominates the wall-clock
    budget on a cold cache; reusing across tests keeps the suite under
    the 90s target named in the unit description.
  * ``dnsmasq_sidecar`` — session-scoped, launches the dnsmasq sidecar
    from U-3 on the orch-net bridge. Returns a `SidecarHandle` with the
    container's orch-net IP — that IP is what worker containers must
    pass as `--dns=` (NOT 127.0.0.1, which is the worker container's
    OWN loopback and never reaches the sibling sidecar).
  * ``start_dnsmasq_sidecar`` — function-scoped factory; tests that
    need a sidecar with bespoke ``--server=`` flags (e.g. the
    internal-registry opt-in case from U-4) call this to spawn a fresh
    sidecar, then must invoke ``.stop()`` on the returned handle.
  * ``sandbox_repo`` — function-scoped copy of the tiny fixture under
    ``tests/fixtures/sandbox-repo/`` into a tmp_path so writes by the
    container don't pollute the checked-in fixture.

Every fixture uses ``shutil.which`` / ``subprocess.run(..., check=False)``
so a missing dependency causes a `pytest.skip` rather than a hard error.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 — invoking `docker` is the whole point
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
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
DNSMASQ_CONFIG = REPO_ROOT / "orchestrator" / "network" / "allowlist.dnsmasq.conf"
TEST_IMAGE_TAG = "orchestrator/worker:test"
TEST_NETWORK = "orch-net"
DNSMASQ_CONTAINER_NAME = "orchestrator-e2e-dnsmasq"
DNSMASQ_IMAGE = "4km3/dnsmasq:2.90-r3"


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


E2E_OPT_IN_ENV = "ORCH_RUN_E2E"


@pytest.fixture(autouse=True)
def docker_available() -> None:
    """Skip every E2E test unless explicitly opted into.

    Two-gate predicate, both must hold for the suite to run:

      (a) ``ORCH_RUN_E2E=1`` on the host. The main CI matrix doesn't set
          this, so the heavy E2E suite stays out of every Ubuntu / macOS
          / Windows runner the per-PR matrix spawns (Ubuntu runners DO
          ship a working Docker daemon, so the daemon-only check isn't
          sufficient to keep them out). The opt-in ``e2e-docker`` workflow
          sets the env var explicitly.

      (b) Docker daemon reachable on the host (``docker version`` returns
          a server version within 5s). Even with the opt-in flag set,
          a missing daemon skips cleanly rather than failing every test
          at the fixture level.

    Autouse keeps the gate consistent across the suite so individual
    tests don't need to remember the marker.
    """
    if os.environ.get(E2E_OPT_IN_ENV) != "1":
        pytest.skip(
            f"E2E suite is opt-in (set {E2E_OPT_IN_ENV}=1); main CI matrix runs the unit suite only"
        )
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


@dataclass(frozen=True)
class SidecarHandle:
    """Reference to a running dnsmasq sidecar container.

    ``orchnet_ip`` is the address worker containers must pass as
    ``--dns=`` — it is the sidecar's IP on the ``orch-net`` bridge,
    NOT ``127.0.0.1``. The 127.0.0.1 default in
    ``DockerClaudeCodeWorker`` is what a host-side sidecar would use
    (`scripts/run-worker-dns.sh`); inside a worker container,
    127.0.0.1 is that container's OWN loopback and never reaches a
    sibling sidecar (PR #16 review H3).

    ``host_port`` is the loopback-published host:port (e.g.
    ``127.0.0.1:5353``) for any host-side probe a test wants to run;
    it is NOT what worker containers should target.
    """

    name: str
    orchnet_ip: str
    host_port: str | None = None

    def stop(self) -> None:
        """Tear down the sidecar container. Idempotent."""
        subprocess.run(
            ["docker", "rm", "-f", self.name],
            capture_output=True,
            timeout=30,
            check=False,
        )


def _inspect_container_ip(name: str, network: str) -> str | None:
    """Return the container's IPv4 address on ``network``, or None."""
    proc = subprocess.run(
        [
            "docker",
            "inspect",
            name,
            "--format",
            f"{{{{.NetworkSettings.Networks.{network}.IPAddress}}}}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return None
    ip = proc.stdout.strip()
    return ip or None


def _start_dnsmasq_container(
    *,
    name: str,
    network: str,
    publish_host_port: int | None,
    extra_server_flags: tuple[str, ...] = (),
) -> tuple[int, str, str]:
    """Spawn a dnsmasq container; return (rc, stdout, stderr).

    Caller is responsible for tearing the container down via the
    returned ``name`` (use the ``SidecarHandle.stop`` helper).

    ``extra_server_flags`` is appended verbatim after the standard
    dnsmasq args so callers can drive bespoke ``--server=`` flags for
    internal-registry opt-in tests (the same expansion
    ``scripts/run-worker-dns.sh`` performs from
    ``ORCH_INTERNAL_REGISTRY_HOSTS``).
    """
    argv: list[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        network,
    ]
    if publish_host_port is not None:
        argv += [
            "-p",
            f"127.0.0.1:{publish_host_port}:53/udp",
            "-p",
            f"127.0.0.1:{publish_host_port}:53/tcp",
        ]
    argv += [
        "--mount",
        f"type=bind,source={DNSMASQ_CONFIG},target=/etc/dnsmasq.conf,readonly",
        "--cap-add=NET_ADMIN",
        DNSMASQ_IMAGE,
        "--conf-file=/etc/dnsmasq.conf",
        "--keep-in-foreground",
        "--no-daemon",
        # Override listen-address so the container binds 0.0.0.0 (the
        # orch-net peers reach it via the bridge interface).
        "--listen-address=0.0.0.0",
        "--port=53",
        *extra_server_flags,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture(scope="session")
def dnsmasq_sidecar(orch_net: str) -> Iterator[SidecarHandle]:
    """Run the U-3 dnsmasq sidecar in a container on the orch-net bridge.

    Worker containers reach the sidecar via its orch-net IP (NOT
    127.0.0.1 — see ``SidecarHandle`` docstring + PR #16 review H3).
    A host-side port publish on 127.0.0.1:5353 lets tests run host-
    side probes against the same sidecar if needed.

    Yields a ``SidecarHandle`` so tests can read the orch-net IP and
    pass it to ``DockerClaudeCodeWorker(dns=...)``.
    """
    if not DNSMASQ_CONFIG.is_file():
        pytest.skip(f"missing dnsmasq config at {DNSMASQ_CONFIG}")

    name = f"{DNSMASQ_CONTAINER_NAME}-{uuid.uuid4().hex[:6]}"
    rc, _stdout, stderr = _start_dnsmasq_container(
        name=name,
        network=orch_net,
        publish_host_port=5353,
    )
    if rc != 0:
        pytest.skip(
            f"could not start dnsmasq sidecar (probably no public-image pull): "
            f"{stderr.strip()[:400]}"
        )

    # Give dnsmasq a beat to bind + Docker a beat to attach the
    # container to the orch-net bridge (otherwise the IP inspect
    # races the network plumbing).
    time.sleep(1.0)
    ip = _inspect_container_ip(name, orch_net)
    if not ip:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
        pytest.skip(f"could not resolve {orch_net} IP for sidecar {name!r}")

    handle = SidecarHandle(name=name, orchnet_ip=ip, host_port="127.0.0.1:5353")
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture
def start_dnsmasq_sidecar(orch_net: str) -> Iterator:
    """Function-scoped factory for a per-test dnsmasq sidecar.

    Used by tests that need ``--server=`` flag injection — e.g. the
    internal-registry opt-in case from U-4, which validates that
    ``ORCH_INTERNAL_REGISTRY_HOSTS=internal.example`` actually makes
    that host resolve from inside a worker container. The session-
    scoped ``dnsmasq_sidecar`` fixture can't be reconfigured mid-
    session (dnsmasq reads its server flags at startup, not at
    runtime), so each opt-in test pays the cost of a fresh sidecar.

    Returns a callable: ``handle = start(extra_hosts=[...],
    upstream="1.1.1.1")``. Every handle is registered for cleanup at
    fixture teardown — tests don't have to remember to ``.stop()``
    explicitly, but may, to free the sidecar before the test ends.
    """
    if not DNSMASQ_CONFIG.is_file():
        pytest.skip(f"missing dnsmasq config at {DNSMASQ_CONFIG}")

    started: list[SidecarHandle] = []

    def _start(
        *,
        extra_hosts: list[str] | tuple[str, ...] = (),
        upstream: str = "1.1.1.1",
    ) -> SidecarHandle:
        name = f"{DNSMASQ_CONTAINER_NAME}-fn-{uuid.uuid4().hex[:6]}"
        flags = tuple(f"--server=/{host}/{upstream}" for host in extra_hosts)
        rc, _stdout, stderr = _start_dnsmasq_container(
            name=name,
            network=orch_net,
            publish_host_port=None,  # no host publish for per-test sidecars
            extra_server_flags=flags,
        )
        if rc != 0:
            pytest.skip(f"could not start per-test dnsmasq sidecar: {stderr.strip()[:400]}")
        time.sleep(1.0)
        ip = _inspect_container_ip(name, orch_net)
        if not ip:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=30,
                check=False,
            )
            pytest.skip(f"could not resolve {orch_net} IP for per-test sidecar {name!r}")
        handle = SidecarHandle(name=name, orchnet_ip=ip)
        started.append(handle)
        return handle

    try:
        yield _start
    finally:
        for h in started:
            h.stop()


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
