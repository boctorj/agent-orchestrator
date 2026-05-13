"""Validate orchestrator/network/allowlist.dnsmasq.conf (F-001-U-3).

Two anchors:

  1. **Syntax**: every non-blank, non-comment line is a recognized
     dnsmasq directive (`key=value` or `key` form). We don't run
     dnsmasq itself (that's a live integration concern, owned by U-6);
     we just verify the file parses cleanly so a typo can't ship.

  2. **Allowlist coverage**: every entry in
     `managed_agent.ALLOWED_NETWORK_HOSTS` and every package-manager
     host named in the F-001-U-3 unit description has a matching
     directive line (`server=/<host>/...` or `address=/<host>/...`).
     This is the contract `audit.render() -> Network: orch-net` is
     promising — the dnsmasq config must actually cover those hosts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.network import (
    ALLOWLIST_FILENAME,
    PACKAGE_MANAGER_HOSTS,
    allowlist_config_path,
)
from orchestrator.workers.managed_agent import ALLOWED_NETWORK_HOSTS

# ---------------------------------------------------------------------------
# dnsmasq directives we expect in this file. Anything else is a typo
# (or a dependency this test should learn about + bless).
# ---------------------------------------------------------------------------

ALLOWED_DIRECTIVES = frozenset(
    {
        "listen-address",
        "port",
        "bind-interfaces",
        "no-resolv",
        "no-hosts",
        "server",
        "address",
    }
)


# ---------------------------------------------------------------------------
# Helpers — parse the config file once per test session.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def config_text() -> str:
    path = allowlist_config_path()
    assert path.exists(), f"allowlist config missing at {path}"
    return path.read_text()


@pytest.fixture(scope="module")
def config_lines(config_text: str) -> list[str]:
    """Non-blank, non-comment lines, whitespace-stripped."""
    out: list[str] = []
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# Path / filename — the launcher script reads this path; pin the contract.
# ---------------------------------------------------------------------------


def test_allowlist_filename_constant():
    assert ALLOWLIST_FILENAME == "allowlist.dnsmasq.conf"


def test_allowlist_config_path_resolves_to_a_real_file():
    path = allowlist_config_path()
    assert path.name == ALLOWLIST_FILENAME
    assert path.is_file()
    assert path.parent == Path(__file__).resolve().parent.parent / "orchestrator" / "network"


# ---------------------------------------------------------------------------
# Syntax — every directive line uses a known dnsmasq key.
# ---------------------------------------------------------------------------


def test_every_line_is_a_known_dnsmasq_directive(config_lines: list[str]):
    """Tag every line with the leading directive name and fail on unknowns.

    Lines come in three shapes:
      * `key=value` — most directives (e.g. `port=5353`, `server=/h/up`)
      * `key`       — flag-only (e.g. `no-resolv`, `bind-interfaces`)
    """
    unknown: list[str] = []
    for line in config_lines:
        key = line.split("=", 1)[0].strip()
        if key not in ALLOWED_DIRECTIVES:
            unknown.append(line)
    assert not unknown, (
        f"unknown dnsmasq directive(s) — fix the typo or add to ALLOWED_DIRECTIVES: {unknown!r}"
    )


def test_binds_to_loopback_on_alt_port(config_lines: list[str]):
    """The script header + unit description name 127.0.0.1:5353 explicitly;
    pin both so a refactor can't silently move them."""
    assert "listen-address=127.0.0.1" in config_lines, (
        "config must bind 127.0.0.1 — workers point --dns at it"
    )
    assert "port=5353" in config_lines, "config must listen on the alt port 5353"


def test_does_not_read_system_resolver(config_lines: list[str]):
    """`no-resolv` is what makes the allowlist actually deny. Without it
    dnsmasq would fall back to /etc/resolv.conf and resolve everything."""
    assert "no-resolv" in config_lines


def test_has_default_deny_catch_all(config_lines: list[str]):
    """`address=/#/0.0.0.0` (or equivalent) NXDOMAINs everything the
    explicit allowlist didn't claim. Without it the deny semantics
    fall back on dnsmasq's default behavior, which is permissive."""
    has_catch_all = any(line.startswith("address=/#/") for line in config_lines)
    assert has_catch_all, "config must include a default-deny catch-all (e.g. `address=/#/0.0.0.0`)"


# ---------------------------------------------------------------------------
# Allowlist coverage — the heart of the test the unit description names.
# ---------------------------------------------------------------------------


def _hosts_referenced(lines: list[str]) -> set[str]:
    """Hosts that appear in either `server=/<host>/...` or `address=/<host>/...`
    lines. Both forms count — server= forwards upstream, address= pins an IP;
    either is a valid allowlist entry."""
    hosts: set[str] = set()
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in ("server", "address"):
            continue
        # Both directives use the `/<host>/...` form for host-scoped entries.
        if not value.startswith("/"):
            continue
        # Skip the wildcard catch-all (e.g. /#/0.0.0.0).
        parts = value.strip("/").split("/")
        if not parts:
            continue
        host = parts[0]
        if host and host != "#":
            hosts.add(host)
    return hosts


def test_every_allowed_network_host_has_a_matching_directive(config_lines: list[str]):
    """Every entry in `ALLOWED_NETWORK_HOSTS` (the managed-agent allowlist)
    must appear in the dnsmasq config — that's the contract `audit.render()
    -> Network: orch-net` is promising."""
    referenced = _hosts_referenced(config_lines)
    missing = [h for h in ALLOWED_NETWORK_HOSTS if h not in referenced]
    assert not missing, (
        f"hosts in ALLOWED_NETWORK_HOSTS missing from dnsmasq config: {missing}. "
        f"Add a `server=/<host>/<upstream>` or `address=/<host>/<ip>` line for each."
    )


def test_every_package_manager_host_has_a_matching_directive(config_lines: list[str]):
    """The F-001-U-3 unit description names pypi.org / files.pythonhosted.org /
    registry.npmjs.org explicitly. Pin them so a refactor of
    `PACKAGE_MANAGER_HOSTS` can't quietly drop a host."""
    referenced = _hosts_referenced(config_lines)
    missing = [h for h in PACKAGE_MANAGER_HOSTS if h not in referenced]
    assert not missing, (
        f"package-manager hosts missing from dnsmasq config: {missing}. "
        f"Required by F-001-U-3 unit description."
    )


@pytest.mark.parametrize(
    "host",
    [
        # Sanity: every host the unit description names verbatim.
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
    ],
)
def test_unit_description_named_host_present(config_lines: list[str], host: str):
    referenced = _hosts_referenced(config_lines)
    assert host in referenced, f"host named in F-001-U-3 unit description not allowlisted: {host}"
