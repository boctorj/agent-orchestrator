#!/usr/bin/env bash
# Launch the dnsmasq sidecar that enforces the worker DNS allowlist.
#
# Workers attach via `--dns=127.0.0.1 --dns-search=.` (see
# orchestrator/workers/docker_claude_code.py:build_docker_argv). Every
# DNS query from the container is forwarded to this dnsmasq, which
# answers only for hosts on the allowlist (config:
# orchestrator/network/allowlist.dnsmasq.conf). Everything else gets
# 0.0.0.0 — a non-routable address that fails outbound connect.
#
# Bind: 127.0.0.1:5353 (the alt port avoids clashing with a system
#   resolver on :53). Override via ORCH_DNSMASQ_BIND.
# Config: orchestrator/network/allowlist.dnsmasq.conf. Override via
#   ORCH_DNSMASQ_CONFIG.
# Docker network: orch-net. Override via ORCH_DOCKER_NETWORK. The
#   network is created idempotently if it doesn't exist
#   (`docker network inspect orch-net || docker network create ...`)
#   so the spawn path never fails with "network orch-net not found".
#
# SOFT BOUNDARY (important — read this before treating it as a fence):
# DNS-level filtering blocks named-host exfiltration. It does NOT
# block egress to raw IPs — a compromised worker that hardcodes
# 8.8.8.8 (or any reachable IP) can still connect. The kernel-side
# egress rules Anthropic's Managed Agents enforce are NOT replicated
# here. See SECURITY.md "Non-defenses" and
# docs/PROPOSAL-docker-workers.md "Network policy".

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_PATH="${ORCH_DNSMASQ_CONFIG:-${REPO_ROOT}/orchestrator/network/allowlist.dnsmasq.conf}"
BIND_ADDR="${ORCH_DNSMASQ_BIND:-127.0.0.1}"
NETWORK_NAME="${ORCH_DOCKER_NETWORK:-orch-net}"

if [ ! -f "${CONFIG_PATH}" ]; then
  echo "missing dnsmasq config: ${CONFIG_PATH}" >&2
  exit 1
fi

# Ensure the orch-net Docker bridge exists. Idempotent guard: only
# create if `docker network inspect` exits non-zero. Without this,
# the doctor command prints "Network: orch-net" like a fact but the
# spawn errors with "network orch-net not found" — the gap this
# script and the doctor probe both plug.
if command -v docker >/dev/null 2>&1; then
  if ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
    docker network create "${NETWORK_NAME}" --driver bridge
  fi
else
  echo "warning: docker CLI not on PATH; skipping ${NETWORK_NAME} bridge check" >&2
fi

# Exec into dnsmasq in the foreground so process supervisors (systemd,
# launchd, tmux) can see it. --conf-file pins the source of truth and
# stops dnsmasq from picking up /etc/dnsmasq.conf.
exec dnsmasq \
  --keep-in-foreground \
  --no-daemon \
  --conf-file="${CONFIG_PATH}" \
  --listen-address="${BIND_ADDR}"
