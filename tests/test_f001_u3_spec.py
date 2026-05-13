"""Spec-compliance tests for F-001-U-3 (DNS allowlist sidecar).

These are independent tester-written tests that complement the coder's
test files (`test_allowlist_config.py`, `test_run_worker_dns_script.py`,
`test_doctor_network_probe.py`) by locking in the EXACT semantics from
the F-001-U-3 unit description verbatim:

  * `scripts/run-worker-dns.sh` launches dnsmasq on `127.0.0.1:5353`
    from `orchestrator/network/allowlist.dnsmasq.conf`.
  * Allowlist mirrors `ALLOWED_NETWORK_HOSTS` + package-manager hosts:
      pypi.org, files.pythonhosted.org, registry.npmjs.org,
      github.com, api.github.com, raw.githubusercontent.com,
      objects.githubusercontent.com, codeload.github.com.
  * Worker containers launch with `--dns=127.0.0.1 --dns-search=.`,
    wired through the U-2 docker run command (i.e. into
    `DockerClaudeCodeWorker.build_docker_argv`).
  * Network setup ensures the `orch-net` bridge exists idempotently
    via `docker network inspect || docker network create --driver bridge`.
  * Doctor probe (folded from PR #11 SUGGESTION 2) runs
    `docker network inspect orch-net` and fails fast pre-flight.
  * Raw-IP egress caveat is documented (soft boundary, not a kernel
    guarantee) — the unit description requires this in writing.

No live network, no live docker. Subprocess calls are injected via the
`run` kwarg / `run` attribute on the worker.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from orchestrator.network import (
    ALLOWLIST_FILENAME,
    PACKAGE_MANAGER_HOSTS,
    allowlist_config_path,
)
from orchestrator.workers.docker_claude_code import (
    DEFAULT_DNS,
    DEFAULT_DNS_SEARCH,
    DEFAULT_IMAGE,
    DEFAULT_NETWORK,
    DockerClaudeCodeWorker,
    DoctorProbeResult,
    run_doctor_probes,
)
from orchestrator.workers.managed_agent import ALLOWED_NETWORK_HOSTS

# Hosts the F-001-U-3 unit description NAMES VERBATIM. The dnsmasq
# allowlist must cover every one of them. Source of truth — do not edit
# without updating the unit description.
SPEC_REQUIRED_HOSTS: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run-worker-dns.sh"
CONFIG_PATH = REPO_ROOT / "orchestrator" / "network" / "allowlist.dnsmasq.conf"


# =============================================================================
# 1. CONFIG — spec mandates dnsmasq on 127.0.0.1:5353 from the bundled path.
# =============================================================================


def test_allowlist_config_file_lives_at_canonical_path():
    """The unit description names this path verbatim:
    `orchestrator/network/allowlist.dnsmasq.conf`. Drift breaks the
    documented launch command."""
    assert CONFIG_PATH.is_file(), f"expected config at {CONFIG_PATH}"
    # The python accessor must agree with the literal path.
    assert allowlist_config_path() == CONFIG_PATH
    assert allowlist_config_path().name == ALLOWLIST_FILENAME == "allowlist.dnsmasq.conf"


def test_config_binds_to_loopback_address():
    """`listen-address=127.0.0.1` — workers point `--dns=127.0.0.1`
    at this exact bind address. A mismatch silently breaks resolution."""
    text = CONFIG_PATH.read_text()
    # Strip comments + blank lines, then look for the exact directive.
    directives = _directive_lines(text)
    assert "listen-address=127.0.0.1" in directives, (
        "config must contain `listen-address=127.0.0.1` — workers route DNS here"
    )


def test_config_listens_on_alt_port_5353():
    """`port=5353` — the unit description names :5353 explicitly to
    avoid clashing with a system resolver on :53."""
    directives = _directive_lines(CONFIG_PATH.read_text())
    assert "port=5353" in directives, "unit description requires port 5353 binding"


def test_config_does_not_read_system_resolver():
    """`no-resolv` — without it, dnsmasq falls back to /etc/resolv.conf
    and the allowlist is moot (default-deny becomes default-allow)."""
    directives = _directive_lines(CONFIG_PATH.read_text())
    assert "no-resolv" in directives, (
        "config must include `no-resolv` so the allowlist is the source of truth"
    )


def test_config_default_deny_uses_unroutable_address():
    """`address=/#/0.0.0.0` (or equivalent unroutable IP) is the
    catch-all. Any host not on the allowlist resolves to a non-routable
    address, so the downstream connect() fails. Without it the deny
    semantics flip to whatever dnsmasq's defaults provide."""
    directives = _directive_lines(CONFIG_PATH.read_text())
    catch_alls = [d for d in directives if d.startswith("address=/#/")]
    assert catch_alls, "config must include `address=/#/<ip>` default-deny catch-all"
    # The address must be a sink — 0.0.0.0, ::, or 127.0.0.1 are all
    # non-routable from a container; a real public IP would defeat the
    # purpose of the allowlist.
    for d in catch_alls:
        # `address=/#/0.0.0.0` → IP is the last `/`-separated piece.
        ip = d.split("/")[-1]
        assert ip in ("0.0.0.0", "::", "127.0.0.1"), (
            f"default-deny IP {ip!r} is routable — must be a sink"
        )


# =============================================================================
# 2. ALLOWLIST COVERAGE — every spec-named host must appear in the config.
# =============================================================================


@pytest.mark.parametrize("host", SPEC_REQUIRED_HOSTS)
def test_spec_required_host_appears_in_config(host: str):
    """Each of the 8 hosts the F-001-U-3 unit description names must
    have a matching `server=/<host>/...` or `address=/<host>/...`
    directive. Per-host parametrization gives precise failure surface."""
    hosts = _hosts_referenced(_directive_lines(CONFIG_PATH.read_text()))
    assert host in hosts, (
        f"spec-required host {host!r} not allowlisted in dnsmasq config. "
        f"Add `server=/{host}/1.1.1.1` (or similar)."
    )


def test_every_allowed_network_host_appears_in_config():
    """Every entry in `ALLOWED_NETWORK_HOSTS` must round-trip through
    the dnsmasq config — that's the mirroring contract the unit
    description states ('Allowlist mirrors ALLOWED_NETWORK_HOSTS')."""
    hosts = _hosts_referenced(_directive_lines(CONFIG_PATH.read_text()))
    missing = [h for h in ALLOWED_NETWORK_HOSTS if h not in hosts]
    assert not missing, (
        f"hosts in ALLOWED_NETWORK_HOSTS not allowlisted in dnsmasq config: {missing}"
    )


def test_package_manager_hosts_constant_matches_spec():
    """The spec names exactly three package-manager hosts. Anything
    else in `PACKAGE_MANAGER_HOSTS` is scope creep; anything missing
    is a regression."""
    assert set(PACKAGE_MANAGER_HOSTS) == {
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
    }, "PACKAGE_MANAGER_HOSTS must exactly match the 3 hosts the F-001-U-3 unit description names"


# =============================================================================
# 3. DOCKER WORKER WIRING — --dns=127.0.0.1 --dns-search=. in argv.
# =============================================================================


@pytest.fixture
def worker(tmp_path: Path) -> DockerClaudeCodeWorker:
    """Stub worker with a temp workdir + fake .claude/sessions dir."""
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    return DockerClaudeCodeWorker(role="coder", workdir=workdir, home_dir=home)


def test_default_dns_constant_is_loopback():
    """`DEFAULT_DNS = '127.0.0.1'` — the module-level constant is the
    documented contract; pin it so a typo can't slip into a release."""
    assert DEFAULT_DNS == "127.0.0.1"


def test_default_dns_search_is_empty_domain():
    """`DEFAULT_DNS_SEARCH = '.'` — the empty search domain (`.`) stops
    glibc from appending host search-path entries to bare names, so
    every query routes verbatim through the dnsmasq sidecar."""
    assert DEFAULT_DNS_SEARCH == "."


def test_argv_includes_dns_flag_pointing_at_loopback(worker: DockerClaudeCodeWorker):
    """`--dns=127.0.0.1` must appear as a standalone argv token on
    every spawn — the spec wires this through the U-2 docker run cmd."""
    argv = worker.build_docker_argv(["claude", "-p", "hi"], host_env={"GITHUB_TOKEN": "ghp_x"})
    assert "--dns=127.0.0.1" in argv, f"--dns=127.0.0.1 missing from argv: {argv!r}"


def test_argv_includes_dns_search_flag(worker: DockerClaudeCodeWorker):
    """`--dns-search=.` must appear too — without it the host's
    search-path leaks through."""
    argv = worker.build_docker_argv(["claude", "-p", "hi"], host_env={"GITHUB_TOKEN": "ghp_x"})
    assert "--dns-search=." in argv, f"--dns-search=. missing from argv: {argv!r}"


def test_dns_flags_precede_the_image_token(worker: DockerClaudeCodeWorker):
    """The --dns flags are docker-run flags, not container command
    args. They MUST appear before the image token, otherwise docker
    treats them as args to `claude` (which silently ignores them)."""
    argv = worker.build_docker_argv(["claude", "-p", "hi"], host_env={"GITHUB_TOKEN": "ghp_x"})
    image_idx = argv.index(worker.image)
    dns_idx = argv.index("--dns=127.0.0.1")
    dns_search_idx = argv.index("--dns-search=.")
    assert dns_idx < image_idx, (
        f"--dns=127.0.0.1 at idx {dns_idx} appears AFTER image at idx {image_idx} — "
        f"docker will treat it as a container arg. argv={argv!r}"
    )
    assert dns_search_idx < image_idx, (
        f"--dns-search=. at idx {dns_search_idx} appears AFTER image at idx {image_idx}"
    )


def test_dns_flags_present_in_both_auth_modes(tmp_path: Path):
    """DNS allowlist is a security invariant, not an auth-mode-dependent
    behavior. Both OAuth and API-key spawns must wire the flags."""
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    w = DockerClaudeCodeWorker(role="tester", workdir=workdir, home_dir=home)

    oauth_argv = w.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_a"})
    api_argv = w.build_docker_argv(
        ["claude", "-p", "x"],
        host_env={"ANTHROPIC_API_KEY": "sk-ant-y", "GITHUB_TOKEN": "ghp_a"},
    )
    for label, argv in (("oauth", oauth_argv), ("api_key", api_argv)):
        assert "--dns=127.0.0.1" in argv, f"--dns missing in {label} argv: {argv!r}"
        assert "--dns-search=." in argv, f"--dns-search missing in {label} argv: {argv!r}"


def test_dns_value_can_be_overridden(tmp_path: Path):
    """The DNS bind address is parameterized so ops can point at an
    alternate resolver. Pin the override semantics — without it a
    `dns=` kwarg silently no-ops."""
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    w = DockerClaudeCodeWorker(
        role="coder", workdir=workdir, home_dir=home, dns="10.0.0.53", dns_search="example.test"
    )
    argv = w.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_a"})
    assert "--dns=10.0.0.53" in argv
    assert "--dns-search=example.test" in argv
    # And the default must NOT also slip in (no duplicate --dns flags).
    assert "--dns=127.0.0.1" not in argv


def test_network_flag_targets_orch_net_bridge(worker: DockerClaudeCodeWorker):
    """`--network=orch-net` ties the worker to the bridge the U-3
    network setup creates. Without this the DNS sidecar is bypassed."""
    argv = worker.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_a"})
    assert f"--network={DEFAULT_NETWORK}" in argv
    assert DEFAULT_NETWORK == "orch-net"  # pin the constant


# =============================================================================
# 4. LAUNCHER SCRIPT — picks up config path + bind address.
# =============================================================================


@pytest.fixture(scope="module")
def script_text() -> str:
    assert SCRIPT_PATH.exists(), f"launcher script missing: {SCRIPT_PATH}"
    return SCRIPT_PATH.read_text()


def test_script_references_canonical_config_path(script_text: str):
    """The unit description names
    `orchestrator/network/allowlist.dnsmasq.conf` as the config path."""
    assert "orchestrator/network/allowlist.dnsmasq.conf" in script_text


def test_script_default_bind_is_127_0_0_1(script_text: str):
    """The spec says 127.0.0.1:5353. The script's default bind must
    therefore match — workers `--dns=127.0.0.1` at it."""
    assert "127.0.0.1" in script_text, "script must default to 127.0.0.1 bind"


def test_script_runs_dnsmasq_with_conf_file_flag(script_text: str):
    """`--conf-file=<path>` is what binds the config; without it
    dnsmasq reads /etc/dnsmasq.conf and the allowlist is moot."""
    assert "--conf-file" in script_text


def test_script_ensures_orch_net_idempotently(script_text: str):
    """`docker network inspect orch-net || docker network create orch-net
    --driver bridge` is the EXACT pattern named in the spec.

    Both halves of the OR must appear AND the bridge driver must be
    pinned — anything else (overlay, macvlan) breaks loopback DNS."""
    assert "docker network inspect" in script_text, "script must check before creating"
    assert "docker network create" in script_text, "script must create the network"
    assert "orch-net" in script_text, "spec names the network 'orch-net'"
    assert "--driver bridge" in script_text, "spec names bridge driver explicitly"


def test_script_documents_raw_ip_soft_boundary_caveat(script_text: str):
    """The unit description says: 'Document that raw-IP egress is still
    possible — DNS filtering is a soft boundary against named-host
    exfil, not a kernel guarantee.' Pin the doc note so it can't be
    quietly stripped during a refactor."""
    lowered = script_text.lower()
    # The phrase 'soft boundary' is the documented term; pin it.
    assert "soft boundary" in lowered, (
        "script header must use the phrase 'soft boundary' (spec language)"
    )
    # And the raw-IP caveat must be spelled out.
    assert ("raw-ip" in lowered) or ("raw ip" in lowered), (
        "script header must mention raw-IP egress is not blocked"
    )


def test_script_is_executable_on_posix():
    """`chmod +x` must persist — without it `scripts/run-worker-dns.sh`
    won't launch via shebang. Skipped on non-POSIX (Windows doesn't
    have the executable bit)."""
    import os
    import stat

    if os.name != "posix":
        pytest.skip("executable bit not meaningful on non-POSIX")
    mode = SCRIPT_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/run-worker-dns.sh must be chmod +x"


# =============================================================================
# 5. DOCTOR PROBE — fourth probe: `docker network inspect orch-net`.
# =============================================================================


class _Proc:
    """Tiny CompletedProcess look-alike for runner injection."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_happy_runner(network_returncode: int, *, stdout: str = "", stderr: str = ""):
    """Fake subprocess.run where the first three probes pass and the
    network-inspect probe yields the requested outcome."""

    def _run(argv: list[str], **_kwargs: Any) -> _Proc:
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stdout="sha256:deadbeef", returncode=0)
        if argv[:2] == ["docker", "run"]:
            return _Proc(stdout="claude 1.2.3", returncode=0)
        if argv[:3] == ["docker", "network", "inspect"]:
            return _Proc(stdout=stdout, stderr=stderr, returncode=network_returncode)
        return _Proc(returncode=0)

    return _run


@pytest.fixture
def fake_docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub shutil.which so the CLI-on-PATH probe doesn't short-circuit."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


def _find_probe(results: list[DoctorProbeResult], substr: str) -> DoctorProbeResult:
    matches = [r for r in results if substr in r.name]
    assert len(matches) == 1, (
        f"expected exactly one probe matching {substr!r}; got {[r.name for r in matches]!r} "
        f"(all probe names: {[r.name for r in results]!r})"
    )
    return matches[0]


def test_doctor_runs_network_inspect_subprocess_with_correct_argv(fake_docker_on_path):
    """The probe MUST invoke `docker network inspect <name>`. Capture
    every call and verify the exact argv shape."""
    captured: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: Any) -> _Proc:
        captured.append(list(argv))
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stdout="sha256:abc", returncode=0)
        if argv[:2] == ["docker", "run"]:
            return _Proc(stdout="claude 1.0", returncode=0)
        if argv[:3] == ["docker", "network", "inspect"]:
            return _Proc(stdout="orch-net\n", returncode=0)
        return _Proc(returncode=0)

    run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=_run)
    inspect_calls = [c for c in captured if c[:3] == ["docker", "network", "inspect"]]
    assert inspect_calls, f"probe never invoked `docker network inspect`; calls: {captured!r}"
    assert "orch-net" in inspect_calls[0], f"probe inspected wrong network: {inspect_calls[0]!r}"


def test_doctor_network_probe_passes_on_returncode_zero(fake_docker_on_path):
    """`returncode == 0` from `docker network inspect orch-net` → probe
    ok. This is the happy path: network exists."""
    runner = _make_happy_runner(network_returncode=0, stdout="orch-net\n")
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)
    probe = _find_probe(results, "network")
    assert probe.ok is True, f"expected ok=True; got {probe!r}"


def test_doctor_network_probe_fails_on_nonzero_returncode(fake_docker_on_path):
    """`returncode != 0` (e.g. `Error: No such network: orch-net`) →
    probe MUST report failure. This is the bug the probe was folded
    in to catch (PR #11 reviewer SUGGESTION 2)."""
    runner = _make_happy_runner(network_returncode=1, stderr="Error: No such network: orch-net")
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)
    probe = _find_probe(results, "network")
    assert probe.ok is False, f"expected ok=False; got {probe!r}"


def test_doctor_network_probe_failure_gives_actionable_fix_it(fake_docker_on_path):
    """A failing probe must tell the user how to fix it — either name
    the `docker network create` command or point at the launcher
    script. Without a fix-it the doctor surface is just blame."""
    runner = _make_happy_runner(network_returncode=1, stderr="Error: No such network: orch-net")
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)
    probe = _find_probe(results, "network")
    detail = probe.detail.lower()
    assert "docker network create" in detail or "run-worker-dns" in detail, (
        f"failure detail must include a fix-it hint; got {probe.detail!r}"
    )


def test_doctor_network_probe_runs_after_image_and_claude(fake_docker_on_path):
    """The spec says it's the FOURTH probe. Daemon → image → claude →
    network. Pin the ordering so a future refactor can't reorder
    and shadow earlier failures."""
    runner = _make_happy_runner(network_returncode=0, stdout="orch-net\n")
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)
    names = [r.name for r in results]

    # Network probe must come AFTER image + claude.
    image_idx = next(i for i, n in enumerate(names) if "image" in n)
    claude_idx = next(i for i, n in enumerate(names) if "claude" in n)
    network_idx = next(i for i, n in enumerate(names) if "network" in n and DEFAULT_NETWORK in n)
    assert image_idx < network_idx, f"network probe must come after image: {names!r}"
    assert claude_idx < network_idx, f"network probe must come after claude: {names!r}"


def test_doctor_network_probe_honors_custom_network_name(fake_docker_on_path):
    """`network=` kwarg lets ops point at a sibling bridge (e.g. for
    blue-green migration). Pin pass-through so the kwarg doesn't
    silently no-op."""
    captured: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: Any) -> _Proc:
        captured.append(list(argv))
        if argv[:3] == ["docker", "version", "--format"]:
            return _Proc(stdout="25.0.0", returncode=0)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _Proc(stdout="sha256:abc", returncode=0)
        if argv[:2] == ["docker", "run"]:
            return _Proc(stdout="claude 1.0", returncode=0)
        if argv[:3] == ["docker", "network", "inspect"]:
            return _Proc(stdout="custom-bridge\n", returncode=0)
        return _Proc(returncode=0)

    results = run_doctor_probes(image=DEFAULT_IMAGE, network="custom-bridge", run=_run)
    inspect_calls = [c for c in captured if c[:3] == ["docker", "network", "inspect"]]
    assert inspect_calls, "probe never invoked `docker network inspect` for custom name"
    assert "custom-bridge" in inspect_calls[0], (
        f"probe used wrong network name in argv: {inspect_calls[0]!r}"
    )
    # And the probe name must reflect the supplied name.
    probe = _find_probe(results, "custom-bridge")
    assert probe.ok is True


def test_doctor_probes_returns_doctor_probe_result_instances(fake_docker_on_path):
    """Sanity — the probe contract is `list[DoctorProbeResult]`. Anything
    else (dict, tuple, plain string) breaks the caller in `cli.doctor`."""
    runner = _make_happy_runner(network_returncode=0, stdout="orch-net\n")
    results = run_doctor_probes(image=DEFAULT_IMAGE, network=DEFAULT_NETWORK, run=runner)
    assert results, "probes returned an empty list"
    for r in results:
        assert isinstance(r, DoctorProbeResult), f"non-result entry: {r!r}"
        assert isinstance(r.name, str) and r.name
        assert isinstance(r.ok, bool)


# =============================================================================
# 6. SOFT-BOUNDARY DOC CAVEAT — must be in writing (multiple surfaces).
# =============================================================================


def test_config_file_documents_soft_boundary_caveat():
    """The dnsmasq config header is the user-touchable surface that
    spells out the raw-IP caveat. Pin it as a comment so editors don't
    treat the file as a 'hardened policy' it doesn't enforce."""
    text = CONFIG_PATH.read_text().lower()
    assert "soft boundary" in text, "config file must mention the soft-boundary caveat"


def test_docker_worker_module_documents_soft_boundary_caveat():
    """Same caveat must surface in the module docstring/comments —
    the place engineers will land when grep'ing for `--dns`."""
    from orchestrator.workers import docker_claude_code as mod

    src = Path(mod.__file__).read_text().lower()
    assert "soft boundary" in src, "docker worker module must document soft-boundary caveat"
    # Both the term 'raw' and 'IP' should appear in proximity.
    assert "raw" in src and "ip" in src


# =============================================================================
# Helpers.
# =============================================================================


def _directive_lines(text: str) -> list[str]:
    """Return non-blank, non-comment lines of a dnsmasq config,
    whitespace-stripped."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _hosts_referenced(directives: list[str]) -> set[str]:
    """Pull the hosts named by `server=/<host>/...` or
    `address=/<host>/...` directives. Skips the wildcard catch-all."""
    hosts: set[str] = set()
    for line in directives:
        match = re.match(r"^(server|address)=/([^/#]+)/", line)
        if match:
            hosts.add(match.group(2))
    return hosts
