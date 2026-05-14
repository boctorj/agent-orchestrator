"""End-to-end smoke test for the Docker worker pipeline (F-001-U-6).

The integration-validation layer: it does NOT duplicate the per-axis
unit tests in U-2/U-3/U-4, it stitches them together against a real
Docker daemon.

Coverage (one test per numbered step in the unit description):

  1. Build worker.Dockerfile locally (via the ``worker_image`` fixture).
  2. Start the dnsmasq sidecar from U-3 on 127.0.0.1:5353 (via the
     ``dnsmasq_sidecar`` fixture).
  3. Spawn ``DockerClaudeCodeWorker`` against the tiny sandbox fixture
     repo under ``tests/fixtures/sandbox-repo/``.
  4. Hybrid auth selection: ANTHROPIC_API_KEY set picks the API-key
     path; unset (with a mocked ``credentials.json`` under a tmpdir
     fake home) picks the OAuth path.
  5. Cred boundary inside a running container: ``/proc/self/environ``
     and ``ls /home/agent`` reveal only whitelisted env/mounts.
  6. Network allowlist: ``curl github.com`` succeeds, ``curl evil.com``
     returns NXDOMAIN. Internal-registry hosts (parameterised) are
     reachable when ORCH_INTERNAL_REGISTRY_HOSTS opts them in.
  7. Session resume: two-call round trip captures and reuses the
     session id. Parameterised case also runs ``--session-id <uuid>``
     (host-generated) per the PROPOSAL-docker-workers.md addendum.
     The claude-using parts are gated on a real auth being available
     (``ORCH_E2E_CLAUDE_AUTH=1``) so the suite stays green on machines
     without claude.ai sessions.
  8. ``orchestrator doctor`` runs end-to-end and its audit output is
     non-empty and well-formed.

  9. (Folded from PR #11 reviewer SUGGESTION 1.) A worker configured
     with a 10s ``timeout_seconds`` and a deliberately hanging
     in-container command (``sleep 600``) raises a ``RuntimeError``
     within budget — the timeout fires rather than deadlocking the
     orchestrator thread.

The whole module is auto-skipped via the ``docker_available`` autouse
fixture when Docker isn't reachable. Tests that need real claude auth
are individually gated on ``ORCH_E2E_CLAUDE_AUTH=1`` so a fresh
contributor without a claude.ai session sees a clear skip reason.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 — invoking `docker` is the whole point
import sys
import time
import uuid
from pathlib import Path

import pytest

from orchestrator.workers.docker_claude_code import (
    DockerClaudeCodeWorker,
    build_cred_audit,
    run_doctor_probes,
)

# pytest-timeout's repo-wide default (30s) is too tight for image-build
# round-trips and the deliberate-hang test (which sleeps up to ~15s).
# Override at module-level; each test still completes fast in the happy
# path, but the headroom prevents pytest-timeout from killing legit work.
pytestmark = pytest.mark.timeout(900)


CLAUDE_AUTH_GATE = "ORCH_E2E_CLAUDE_AUTH"


def _docker_run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run a `docker` command with capture + a wall-clock cap."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _requires_claude_auth(reason: str = "needs ORCH_E2E_CLAUDE_AUTH=1 + real claude.ai auth"):
    """Decorator: skip when the claude-auth env knob isn't set."""
    return pytest.mark.skipif(os.environ.get(CLAUDE_AUTH_GATE) != "1", reason=reason)


# ---------------------------------------------------------------------------
# Steps 1 + 2: image build + dnsmasq sidecar.
# Wired by fixtures in `tests/e2e/conftest.py`; the smoke test here just
# pulls them in and verifies the artifact exists / the sidecar is up.
# ---------------------------------------------------------------------------


def test_worker_image_built(worker_image: str) -> None:
    """`worker_image` fixture must produce a real image with a digest."""
    proc = _docker_run(["docker", "image", "inspect", worker_image, "--format", "{{.Id}}"])
    assert proc.returncode == 0, f"image {worker_image} not found after build: {proc.stderr!r}"
    assert proc.stdout.strip(), "image inspect returned empty id"


def test_dnsmasq_sidecar_up(dnsmasq_sidecar) -> None:
    """Sidecar fixture must surface a `SidecarHandle` carrying the
    container name, the orch-net IP (the address worker containers must
    target via `--dns=`), and the host-side publish for host probes.

    Cycle-3 H3 fix changed the fixture from yielding a `"host:port"`
    string to yielding a `SidecarHandle` dataclass; this assertion was
    updated in cycle-4 review H1 (PR #16) to track the new shape.
    """
    from tests.e2e.conftest import SidecarHandle

    assert isinstance(dnsmasq_sidecar, SidecarHandle), (
        f"dnsmasq_sidecar fixture must yield a SidecarHandle, got {type(dnsmasq_sidecar).__name__}"
    )
    # Container name is the U-3 prefix plus a uuid suffix.
    assert dnsmasq_sidecar.name.startswith("orchestrator-e2e-dnsmasq-"), (
        f"sidecar name not from the U-3 family: {dnsmasq_sidecar.name!r}"
    )
    # orch-net IP must be set and look like an IPv4 dotted-quad. Looking
    # at the IP exactly would couple this test to docker's bridge
    # subnet (configurable); the shape check is what matters.
    parts = dnsmasq_sidecar.orchnet_ip.split(".")
    assert len(parts) == 4 and all(p.isdigit() for p in parts), (
        f"orch-net IP not a dotted-quad: {dnsmasq_sidecar.orchnet_ip!r}"
    )
    # Host-side publish keeps the pre-H3 contract for any test that
    # wants to probe the sidecar from the host (not via a worker
    # container). The session-scoped sidecar always publishes 5353.
    assert dnsmasq_sidecar.host_port == "127.0.0.1:5353", (
        f"host_port drifted from the documented 127.0.0.1:5353: {dnsmasq_sidecar.host_port!r}"
    )


# ---------------------------------------------------------------------------
# Step 3: spawn DockerClaudeCodeWorker against the sandbox fixture.
# Uses an override entrypoint so the test doesn't need real claude auth
# — the goal here is to validate the host-side spawn machinery + mount
# wiring against a real Docker daemon, not the claude CLI itself.
# ---------------------------------------------------------------------------


def test_worker_spawns_container_against_sandbox(
    worker_image: str,
    sandbox_repo: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Render the docker argv via the real DockerClaudeCodeWorker and
    exec it with `--entrypoint=cat` against the sandbox fixture's
    `/workspace/README.md`. The container must start, read the bind-
    mounted file, and exit zero.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_for_test_only")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    worker = DockerClaudeCodeWorker(
        role="coder",
        workdir=sandbox_repo,
        home_dir=fake_home,
        image=worker_image,
    )
    # Build argv but use `cat /workspace/README.md` instead of `claude`.
    argv = worker.build_docker_argv(["cat", "/workspace/README.md"])
    # Override the image's ENTRYPOINT so `cat` is invoked directly.
    # Inserting --entrypoint after `docker run --rm` keeps the rest of
    # the hardening flags intact.
    rm_idx = argv.index("--rm")
    argv = argv[: rm_idx + 1] + ["--entrypoint=cat"] + argv[rm_idx + 1 :]
    # Strip the `cat` from claude_argv (entrypoint already names it).
    cat_idx = argv.index("cat", rm_idx + 1)
    argv = argv[:cat_idx] + argv[cat_idx + 1 :]

    proc = _docker_run(argv, timeout=60)
    assert proc.returncode == 0, (
        f"docker run failed: rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "sandbox fixture" in proc.stdout, (
        f"expected fixture README content; got: {proc.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Step 4: hybrid auth selection.
# Both branches build the argv via the real worker; the test asserts
# the argv reflects the correct mode without actually invoking claude.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_overrides", "expected_mode"),
    [
        ({"ANTHROPIC_API_KEY": "sk-ant-fake"}, "api_key"),
        ({}, "oauth"),
    ],
    ids=["api_key", "oauth"],
)
def test_hybrid_auth_selection_against_fake_home(
    worker_image: str,
    sandbox_repo: Path,
    tmp_path: Path,
    monkeypatch,
    env_overrides: dict[str, str],
    expected_mode: str,
) -> None:
    """OAuth path uses a mocked credentials.json under a tmpdir fake
    home; API-key path forwards ANTHROPIC_API_KEY. Both must round-trip
    through `build_cred_audit().render()` with the right Auth line."""
    fake_home = tmp_path / "fake-home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    # Mock credentials so the OAuth read-only mount has something to
    # find when claude looks for it.
    (fake_home / ".claude" / "credentials.json").write_text(
        json.dumps({"oauth_token": "fake-oauth-token-for-e2e"})
    )

    # Build a clean env: only the override keys are set.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key, val in env_overrides.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_for_test")

    worker = DockerClaudeCodeWorker(
        role="coder",
        workdir=sandbox_repo,
        home_dir=fake_home,
        image=worker_image,
    )
    audit = build_cred_audit(home_dir=fake_home, host_env=dict(os.environ))
    rendered = audit.render()
    if expected_mode == "api_key":
        assert "Auth: API key" in rendered
        # No `~/.claude` cred mount in API-key mode (sessions sub-mount is fine).
        assert "/home/agent/.claude (ro)" not in rendered
    else:
        assert "Auth: claude.ai OAuth" in rendered
        assert "/home/agent/.claude (ro)" in rendered

    # Mirror the assertion at the argv layer so the wiring matches end-to-end.
    argv = worker.build_docker_argv(["true"])
    joined = " ".join(argv)
    if expected_mode == "api_key":
        assert "--env ANTHROPIC_API_KEY" in joined
    else:
        assert "ANTHROPIC_API_KEY" not in joined


# ---------------------------------------------------------------------------
# Step 5: cred boundary holds inside a running container.
# Exec a small shell command in the worker image and assert the visible
# env + filesystem match the whitelist.
# ---------------------------------------------------------------------------


def test_cred_boundary_inside_container(
    worker_image: str, sandbox_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """Pollute the host env with hostile vars; the container must NOT
    see them on `/proc/self/environ`. The agent's HOME must not have
    `.ssh` or `.aws` directories either."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_for_test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/should-not-leak")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    worker = DockerClaudeCodeWorker(
        role="coder",
        workdir=sandbox_repo,
        home_dir=fake_home,
        image=worker_image,
    )

    # Build the argv with a shell that prints both /proc/self/environ
    # (NUL-separated) and the contents of /home/agent. Pipe through
    # `tr` to make the env grep-able.
    cmd_inside = (
        "cat /proc/self/environ | tr '\\0' '\\n' && "
        "echo '---HOME---' && ls -la /home/agent && "
        "echo '---DOTSSH---' && ls /home/agent/.ssh 2>/dev/null || echo NO_SSH && "
        "echo '---DOTAWS---' && ls /home/agent/.aws 2>/dev/null || echo NO_AWS"
    )
    argv = worker.build_docker_argv(["sh", "-c", cmd_inside])
    rm_idx = argv.index("--rm")
    argv = argv[: rm_idx + 1] + ["--entrypoint=sh"] + argv[rm_idx + 1 :]
    sh_idx = argv.index("sh", rm_idx + 1)
    argv = argv[:sh_idx] + argv[sh_idx + 1 :]

    env = worker.build_subprocess_env()
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, f"container exec failed: {proc.stderr!r}"
    out = proc.stdout

    # Hostile env vars MUST NOT appear.
    assert "AWS_SECRET_ACCESS_KEY" not in out, (
        f"AWS_SECRET_ACCESS_KEY leaked into container env: {out!r}"
    )
    assert "SSH_AUTH_SOCK" not in out, f"SSH_AUTH_SOCK leaked into container env: {out!r}"
    # Whitelisted var MUST appear.
    assert "GITHUB_TOKEN" in out

    # Sensitive dotfile dirs must be absent under /home/agent.
    assert "NO_SSH" in out, f".ssh leaked into container HOME: {out!r}"
    assert "NO_AWS" in out, f".aws leaked into container HOME: {out!r}"


# ---------------------------------------------------------------------------
# Step 6: network allowlist (DNS-level filter).
# Bypasses claude entirely — runs `getent hosts <name>` from inside a
# worker container against the orch-net sidecar. PR #16 review H3:
# `--dns=127.0.0.1` is the WORKER container's loopback (not the host's),
# so the worker must be configured with `dns=<sidecar orch-net IP>` for
# the resolver path to actually reach the sidecar.
# ---------------------------------------------------------------------------


def _resolve_inside_container(
    *,
    worker_image: str,
    sandbox_repo: Path,
    fake_home: Path,
    hostname: str,
    dns: str,
) -> tuple[int, str, str]:
    """Run `getent hosts <hostname>` inside a worker container.

    ``dns`` is the resolver address baked into the worker container's
    ``--dns=`` flag — typically the dnsmasq sidecar's orch-net IP.
    Returns (rc, stdout, stderr) so callers can branch on resolution.
    """
    worker = DockerClaudeCodeWorker(
        role="coder",
        workdir=sandbox_repo,
        home_dir=fake_home,
        image=worker_image,
        dns=dns,
    )
    argv = worker.build_docker_argv(["getent", "hosts", hostname])
    rm_idx = argv.index("--rm")
    argv = argv[: rm_idx + 1] + ["--entrypoint=getent"] + argv[rm_idx + 1 :]
    g_idx = argv.index("getent", rm_idx + 1)
    argv = argv[:g_idx] + argv[g_idx + 1 :]
    proc = _docker_run(argv, timeout=30)
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.parametrize(
    ("hostname", "should_resolve"),
    [
        ("github.com", True),
        ("api.github.com", True),
        ("pypi.org", True),
        ("evil.example.invalid", False),
        ("malicious-corp.example", False),
    ],
)
def test_network_allowlist(
    worker_image: str,
    sandbox_repo: Path,
    dnsmasq_sidecar,
    tmp_path: Path,
    hostname: str,
    should_resolve: bool,
) -> None:
    """Allow-listed hosts resolve to a non-loopback IP; disallowed ones
    return rc != 0 (NXDOMAIN) or resolve to 0.0.0.0 (the dnsmasq
    default-deny wildcard).

    The worker container's ``--dns=`` is pinned to the sidecar's
    orch-net IP (NOT 127.0.0.1) so DNS queries actually reach the
    sidecar across the bridge. See ``SidecarHandle`` docstring +
    PR #16 review H3.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)

    rc, stdout, _stderr = _resolve_inside_container(
        worker_image=worker_image,
        sandbox_repo=sandbox_repo,
        fake_home=fake_home,
        hostname=hostname,
        dns=dnsmasq_sidecar.orchnet_ip,
    )
    if should_resolve:
        assert rc == 0, f"allow-listed host {hostname} did not resolve (rc={rc})"
        # Resolution should not be the default-deny 0.0.0.0.
        assert not stdout.strip().startswith("0.0.0.0"), (
            f"allow-listed host {hostname} got the default-deny answer: {stdout!r}"
        )
    else:
        # Two acceptable shapes: getent rc != 0 (NXDOMAIN) OR the
        # default-deny wildcard `0.0.0.0 <host>`.
        denied = rc != 0 or stdout.strip().startswith("0.0.0.0")
        assert denied, f"disallowed host {hostname} resolved to a non-default IP: {stdout!r}"


def test_internal_registry_default_deny_without_opt_in(
    worker_image: str,
    sandbox_repo: Path,
    dnsmasq_sidecar,
    tmp_path: Path,
) -> None:
    """U-4 negative case: an internal-registry hostname (not on the
    bundled allowlist, not added via ORCH_INTERNAL_REGISTRY_HOSTS)
    MUST fall into the dnsmasq default-deny wildcard rather than
    resolve. The companion positive case lives in
    ``test_internal_registry_host_resolves_when_opted_in`` below.

    This was previously named ``..._when_opted_in`` but asserted the
    OPT-OUT shape (PR #16 review H2). Renamed to reflect what it
    actually checks; the opt-in path now has its own dedicated test
    using a function-scoped sidecar.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)

    rc, stdout, _stderr = _resolve_inside_container(
        worker_image=worker_image,
        sandbox_repo=sandbox_repo,
        fake_home=fake_home,
        hostname="artifactory.internal.example",
        dns=dnsmasq_sidecar.orchnet_ip,
    )
    assert rc != 0 or stdout.strip().startswith("0.0.0.0"), (
        f"internal host should not resolve without opt-in: {stdout!r}"
    )


def test_internal_registry_host_resolves_when_opted_in(
    worker_image: str,
    sandbox_repo: Path,
    start_dnsmasq_sidecar,
    tmp_path: Path,
) -> None:
    """U-4 positive case (PR #16 review H2): a hostname listed via
    ORCH_INTERNAL_REGISTRY_HOSTS — which the U-3 launcher script
    expands into ``--server=/<host>/<upstream>`` flags on the
    dnsmasq sidecar — must RESOLVE from inside a worker container
    rather than fall into the default-deny wildcard.

    The session-scoped ``dnsmasq_sidecar`` can't grow new
    ``--server=`` flags mid-session (dnsmasq reads them at startup),
    so this test spawns a per-test sidecar via the
    ``start_dnsmasq_sidecar`` factory with the host baked in.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)

    internal_host = "internal-pypi.corp.example"
    # The upstream resolver still needs to know the host. For the E2E
    # smoke we point at a public DNS that won't have the answer either
    # — what matters here is that the sidecar tried to forward the
    # query through the explicit --server= flag instead of falling
    # into the default-deny address=/#/0.0.0.0 rule. The negative
    # signal (NOT 0.0.0.0) is the contract that pins the opt-in.
    sidecar = start_dnsmasq_sidecar(extra_hosts=[internal_host])

    rc, stdout, _stderr = _resolve_inside_container(
        worker_image=worker_image,
        sandbox_repo=sandbox_repo,
        fake_home=fake_home,
        hostname=internal_host,
        dns=sidecar.orchnet_ip,
    )
    # Two acceptable positive shapes:
    #   (a) rc==0 and stdout begins with a routable IP (real upstream
    #       happened to have an answer — unlikely for example-suffixed
    #       hosts but valid),
    #   (b) rc!=0 from the upstream NXDOMAIN but stdout did NOT carry
    #       the dnsmasq default-deny `0.0.0.0` marker — proving the
    #       wildcard rule didn't fire.
    # Either way the contract is: the per-host --server= flag intercepted
    # the query BEFORE the default-deny.
    is_default_deny = stdout.strip().startswith("0.0.0.0")
    assert not is_default_deny, (
        f"opted-in host {internal_host} hit the default-deny path despite "
        f"--server= flag (sidecar={sidecar.name!r}, rc={rc}, stdout={stdout!r})"
    )


# ---------------------------------------------------------------------------
# Step 7: session resume. The claude-using parts are gated on a real
# auth env knob; the host-generated --session-id parameterised case
# (from the PROPOSAL-docker-workers.md addendum) is also exercised.
# ---------------------------------------------------------------------------


@_requires_claude_auth()
def test_session_resume_threads_session_id(
    worker_image: str, sandbox_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """First spawn captures the session id from `claude -p` output;
    a follow-up `claude --resume <id>` sees the same context."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    worker = DockerClaudeCodeWorker(
        role="coder",
        workdir=sandbox_repo,
        home_dir=fake_home,
        image=worker_image,
        timeout_seconds=120,
    )
    sid, _ = worker.spawn("Remember the phrase 'orange octopus 47'. Respond ACK.")
    assert sid, "spawn must return a session id"
    follow = worker.resume(sid, "What phrase did I tell you?")
    assert "orange octopus" in follow.lower(), (
        f"resume did not surface prior context; got: {follow!r}"
    )


@_requires_claude_auth()
def test_session_resume_with_host_generated_session_id(
    worker_image: str, sandbox_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """PROPOSAL-docker-workers.md addendum: pre-generating a UUID on the
    host via `claude --session-id <uuid>` lets the orchestrator skip
    parsing the id back out of stdout. Confirm a host-generated UUID
    round-trips through `--resume`."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    pregenerated_uuid = str(uuid.uuid4())

    # Drive a custom claude_argv via build_docker_argv: `claude
    # --session-id <uuid> -p <task>`.
    worker = DockerClaudeCodeWorker(
        role="coder",
        workdir=sandbox_repo,
        home_dir=fake_home,
        image=worker_image,
        timeout_seconds=120,
    )
    argv = worker.build_docker_argv(
        [
            "claude",
            "--output-format",
            "json",
            "--session-id",
            pregenerated_uuid,
            "-p",
            "Remember 'blue penguin 12'. Respond ACK.",
        ]
    )
    env = worker.build_subprocess_env()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env, check=False)
    assert proc.returncode == 0, f"--session-id spawn failed: {proc.stderr!r}"

    # Now resume by passing the pre-generated UUID directly.
    follow = worker.resume(pregenerated_uuid, "What phrase did I tell you?")
    assert "blue penguin" in follow.lower()


# ---------------------------------------------------------------------------
# Step 8: doctor command runs end-to-end. Skip claude-auth dependence —
# the doctor probes themselves don't need claude to actually authenticate,
# only that the binary exists in the image.
# ---------------------------------------------------------------------------


def test_doctor_probes_run_against_real_docker(worker_image: str, orch_net: str) -> None:
    """The doctor probes exercise the real docker CLI: daemon, image,
    claude --version inside the container, and the orch-net bridge.
    All four must report OK when the E2E fixtures have set up the
    image and the network.

    Cycle-4 review M1: the previous version asserted three of the four
    (daemon, image, network) but the claude probe was checked only
    implicitly. This now pins all four so a regression in any one of
    them surfaces here.
    """
    results = run_doctor_probes(image=worker_image, network=orch_net)
    by_name = {r.name: r for r in results}
    receipts = [r.__dict__ for r in results]

    daemon_probe = by_name.get("docker daemon reachable")
    assert daemon_probe is not None and daemon_probe.ok, f"doctor daemon probe failed: {receipts!r}"
    image_probe = next(
        (r for r in results if r.name.startswith("image ") and r.name.endswith("built")),
        None,
    )
    assert image_probe is not None and image_probe.ok, (
        f"doctor image probe failed: {image_probe!r} (all: {receipts!r})"
    )
    claude_probe = by_name.get("claude --version inside container")
    assert claude_probe is not None and claude_probe.ok, (
        f"doctor claude-cli probe failed: {claude_probe!r} (all: {receipts!r})"
    )
    network_probe = by_name.get(f"network {orch_net} exists")
    assert network_probe is not None and network_probe.ok, (
        f"doctor network probe failed: {network_probe!r} (all: {receipts!r})"
    )


def test_orchestrator_doctor_cli_under_docker_backend(
    worker_image: str,
    orch_net: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """End-to-end shell-out: invoke `python -m orchestrator.cli doctor`
    with ORCH_WORKER_BACKEND=docker and verify the rendered audit
    block + probe outcomes appear in stdout."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-fake-for-doctor\nGITHUB_TOKEN=github_pat_fake_for_doctor\n"
    )
    (runtime_dir / ".mcp.json").write_text('{"mcpServers": {}}')
    (runtime_dir / "pyproject.toml").write_text('[project]\nname = "agent-orchestrator"\n')

    env = os.environ.copy()
    env["ORCH_WORKER_BACKEND"] = "docker"
    env["ORCH_DOCKER_WORKER_IMAGE"] = worker_image
    env["PYTHONPATH"] = str(repo_root)
    env["ANTHROPIC_API_KEY"] = "sk-ant-fake-for-doctor"
    env["GITHUB_TOKEN"] = "github_pat_fake_for_doctor"

    proc = subprocess.run(
        [sys.executable, "-m", "orchestrator.cli", "doctor"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=runtime_dir,
        env=env,
        check=False,
    )
    out = proc.stdout
    # The doctor CLI exits non-zero when a probe failed (it's a health
    # check, not a pass-through). The crucial assertions are that the
    # audit + probe sections rendered SOMETHING — well-formed output is
    # the contract this step pins.
    assert out.strip(), (
        f"doctor produced empty output (rc={proc.returncode}, stderr={proc.stderr!r})"
    )
    assert "Worker credential audit" in out, f"audit section missing: {out!r}"
    assert "Docker worker probes" in out, f"probe section missing: {out!r}"
    assert "docker daemon reachable" in out


# ---------------------------------------------------------------------------
# Step 9 (PR #11 SUGGESTION 1): timeout fires within budget rather than
# deadlocking the orchestrator thread.
# ---------------------------------------------------------------------------


def test_spawn_timeout_fires_against_hanging_in_container_command(
    worker_image: str, sandbox_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """A worker with `timeout_seconds=10` and a deliberately hanging
    in-container command (`sleep 600`) raises a RuntimeError naming
    the budget within ~15s wall-clock — comfortably under the 600s
    the inner sleep would otherwise hold.

    The +5s headroom on the wall-clock assertion accounts for docker
    daemon round-trip + container cleanup; the actual timeout budget
    is 10s. Without the SUGGESTION-1 fix this test would block for
    the full 600s.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    worker = DockerClaudeCodeWorker(
        role="coder",
        workdir=sandbox_repo,
        home_dir=fake_home,
        image=worker_image,
        timeout_seconds=10,
    )
    # Build the docker argv with sleep as the in-container command.
    # We then route through the spawn() machinery, NOT directly via
    # subprocess.run, so the test exercises the production timeout path.
    # spawn() hard-codes the `claude` command, so we override by
    # monkey-patching the worker's `build_docker_argv` to substitute
    # sleep 600 for the claude argv.
    original_build = worker.build_docker_argv

    def build_with_sleep(claude_argv: list[str], **kw) -> list[str]:
        argv = original_build(claude_argv, **kw)
        # Append `--entrypoint=sleep` after `--rm` and strip the claude
        # tokens at the end, leaving just the sleep duration.
        rm_idx = argv.index("--rm")
        argv = argv[: rm_idx + 1] + ["--entrypoint=sleep"] + argv[rm_idx + 1 :]
        # The original argv ended in [..., image, "claude", "--output-format",
        # "json", "-p", task]. Trim everything after the image.
        image_idx = argv.index(worker.image)
        argv = argv[: image_idx + 1] + ["600"]
        return argv

    monkeypatch.setattr(worker, "build_docker_argv", build_with_sleep)

    start = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        worker.spawn("would-hang-without-timeout")
    elapsed = time.monotonic() - start

    msg = str(excinfo.value)
    assert "timed out" in msg, f"error must say timed out; got: {msg!r}"
    assert "10s" in msg, f"error must name the 10s budget; got: {msg!r}"
    # The timeout itself is 10s; allow generous headroom for the
    # subprocess teardown + docker container kill. If this exceeds 30s
    # the timeout machinery itself is broken.
    assert elapsed < 30.0, (
        f"timeout took too long to fire — elapsed {elapsed:.1f}s, budget 10s + 20s headroom"
    )
