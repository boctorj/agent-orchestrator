"""Static checks on scripts/run-worker-dns.sh (F-001-U-3).

The script does three things:

  1. Picks up the right config path
     (`orchestrator/network/allowlist.dnsmasq.conf`).
  2. Binds dnsmasq on `127.0.0.1` by default.
  3. Ensures the `orch-net` Docker bridge exists (idempotent guard).

Running the script for real requires `dnsmasq` + `docker` on PATH; that's
U-6 integration territory. Here we just read the script as text and pin
the contract: any refactor that drifts the constants or drops the
network-creation guard fails fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run-worker-dns.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    assert SCRIPT_PATH.exists(), f"missing launcher script: {SCRIPT_PATH}"
    return SCRIPT_PATH.read_text()


# ---------------------------------------------------------------------------
# Filesystem — the script is shipped + executable.
# ---------------------------------------------------------------------------


def test_script_exists_at_expected_path():
    assert SCRIPT_PATH.is_file(), f"{SCRIPT_PATH} not found"


def test_script_has_shebang(script_text: str):
    first_line = script_text.splitlines()[0]
    assert first_line.startswith("#!"), "script must start with a shebang"
    assert "bash" in first_line or "sh" in first_line, (
        f"unexpected shebang: {first_line!r} — expected bash/sh"
    )


def test_script_is_executable():
    import os
    import stat

    mode = SCRIPT_PATH.stat().st_mode
    # On Windows the executable bit isn't meaningful — the test still
    # passes there because the file is readable; on Unix we assert the
    # owner-execute bit so chmod +x didn't get lost.
    if os.name == "posix":
        assert mode & stat.S_IXUSR, "script must be chmod +x"


# ---------------------------------------------------------------------------
# Constants — the script must use the documented config path + bind addr.
# ---------------------------------------------------------------------------


def test_script_default_config_path_is_orchestrator_network(script_text: str):
    """The default config path must point at
    `orchestrator/network/allowlist.dnsmasq.conf` — the file the
    unit description names + the test_allowlist_config validates."""
    # The script computes ${REPO_ROOT}/orchestrator/network/allowlist.dnsmasq.conf
    # as the default for ORCH_DNSMASQ_CONFIG. Pin both halves.
    assert "orchestrator/network/allowlist.dnsmasq.conf" in script_text, (
        "script must reference the canonical config path"
    )
    # Override knob is named ORCH_DNSMASQ_CONFIG.
    assert "ORCH_DNSMASQ_CONFIG" in script_text


def test_script_default_bind_address_is_loopback(script_text: str):
    """`--dns=127.0.0.1` on every worker invocation must match the
    bind address the script defaults to. Drifting one without the
    other silently breaks resolution inside the container."""
    assert "127.0.0.1" in script_text, "script must bind on 127.0.0.1 by default"
    # Override knob is named ORCH_DNSMASQ_BIND.
    assert "ORCH_DNSMASQ_BIND" in script_text


def test_script_passes_conf_file_to_dnsmasq(script_text: str):
    """`--conf-file=<path>` is what tells dnsmasq to read our config
    instead of /etc/dnsmasq.conf. Without it the launcher silently
    runs against the host's config — security regression."""
    assert "--conf-file" in script_text


def test_script_runs_dnsmasq_in_foreground(script_text: str):
    """Process supervisors (systemd, launchd, tmux) need a foreground
    process — `--keep-in-foreground` / `--no-daemon` keep dnsmasq
    attached to this script's stdout/stderr."""
    assert "--keep-in-foreground" in script_text or "--no-daemon" in script_text


# ---------------------------------------------------------------------------
# Network bridge — the idempotent guard.
# ---------------------------------------------------------------------------


def test_script_ensures_orch_net_bridge_idempotently(script_text: str):
    """`docker network inspect orch-net >/dev/null 2>&1 || docker network
    create orch-net --driver bridge` is the exact pattern the unit
    description names. Both halves must appear in the script."""
    assert "docker network inspect" in script_text, (
        "script must check for orch-net before creating it"
    )
    assert "docker network create" in script_text, "script must create orch-net when missing"
    assert "orch-net" in script_text, "script must name the orch-net network"
    assert "--driver bridge" in script_text, (
        "network must be created as a bridge — matches DEFAULT_NETWORK"
    )


def test_script_documents_raw_ip_soft_boundary_caveat(script_text: str):
    """The unit description requires the doc note that raw-IP egress
    is still possible. Pin the header comment so it doesn't get
    quietly stripped during a refactor."""
    lowered = script_text.lower()
    assert "soft boundary" in lowered, "script header must call out 'soft boundary' raw-IP caveat"
    assert "raw" in lowered and "ip" in lowered, (
        "script header must mention raw-IP egress is not blocked"
    )


def test_script_uses_set_euo_pipefail(script_text: str):
    """Standard bash safety — fail loudly on unset vars + pipeline errors.
    Skipping this turns a bad config path into a silent dnsmasq launch
    with no config at all."""
    assert "set -euo pipefail" in script_text


# ---------------------------------------------------------------------------
# Override knob — ORCH_DOCKER_NETWORK lets ops swap networks per-host.
# ---------------------------------------------------------------------------


def test_script_supports_orch_docker_network_override(script_text: str):
    """Same env-var-override pattern as the rest of the script. The
    name must match the variable other tools key off."""
    assert "ORCH_DOCKER_NETWORK" in script_text
