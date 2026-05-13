"""Worker network policy — dnsmasq allowlist for outbound DNS.

The allowlist file (`allowlist.dnsmasq.conf`) lives next to this module
and is mirrored by `scripts/run-worker-dns.sh`. Worker containers
launched by `DockerClaudeCodeWorker` attach via
`--dns=127.0.0.1 --dns-search=.`, forcing every name resolution through
this allowlist.

SOFT BOUNDARY: DNS filtering is defense-in-depth against named-host
exfiltration only. A worker that hardcodes a raw IP can still reach it.
See SECURITY.md "Non-defenses" for the full caveat.
"""

from __future__ import annotations

from pathlib import Path

# Filename of the dnsmasq config shipped with this package. Kept as a
# constant so tests + the launcher script can reference the same name.
ALLOWLIST_FILENAME = "allowlist.dnsmasq.conf"

# Hostnames that MUST appear (with a matching server=/<host>/ or
# address=/<host>/ directive) in the dnsmasq config. The package-manager
# hosts come from the F-001-U-3 unit description; the rest mirror
# `orchestrator.workers.managed_agent.ALLOWED_NETWORK_HOSTS`.
#
# Kept here (rather than reaching across to managed_agent) so a) the
# network policy is self-contained and b) test_allowlist_config can
# validate the mirroring without circular-import gymnastics. The
# `test_allowlist_mirrors_managed_agent_hosts` test pins the relationship.
PACKAGE_MANAGER_HOSTS: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)


def allowlist_config_path() -> Path:
    """Absolute path to the bundled dnsmasq config file.

    Used by `scripts/run-worker-dns.sh` (indirectly via its
    `ORCH_DNSMASQ_CONFIG` env var) and by the test suite.
    """
    return Path(__file__).parent / ALLOWLIST_FILENAME


__all__ = [
    "ALLOWLIST_FILENAME",
    "PACKAGE_MANAGER_HOSTS",
    "allowlist_config_path",
]
