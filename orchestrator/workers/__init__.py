"""Worker backends + the `make_worker(role)` factory.

The factory reads `ORCH_WORKER_BACKEND` from the environment (populated
from `.env` by `mcp_server.py` / `cli.py` via `python-dotenv`) and returns
a fresh `Worker` instance for the requested role. Everything downstream
sees the opaque `Worker` protocol — see `workers/base.py`.

Supported backends:

  - `managed_agents` (default): `ManagedAgentWorker` against Anthropic
    Managed Agents.
  - `docker`: `DockerClaudeCodeWorker` against a locally-managed
    `orchestrator/worker:latest` container image. See
    `orchestrator.workers.docker_claude_code` and
    `docs/PROPOSAL-docker-workers.md` for the threat model.

Any other value raises a `ValueError` naming the supported options.
"""

from __future__ import annotations

import os

from orchestrator.workers.base import Worker
from orchestrator.workers.docker_claude_code import DockerClaudeCodeWorker
from orchestrator.workers.managed_agent import ManagedAgentWorker

DEFAULT_BACKEND = "managed_agents"

# The set of backend names we accept. Keep in sync with the docstring above
# and `docs/PROPOSAL-docker-workers.md`.
KNOWN_BACKENDS = frozenset({"managed_agents", "docker"})

__all__ = [
    "DEFAULT_BACKEND",
    "KNOWN_BACKENDS",
    "DockerClaudeCodeWorker",
    "ManagedAgentWorker",
    "Worker",
    "make_worker",
]


def make_worker(role: str) -> Worker:
    """Return a `Worker` for `role` based on `ORCH_WORKER_BACKEND`.

    Backend selection:
      - unset or `managed_agents` → `ManagedAgentWorker` (default).
      - `docker` → `DockerClaudeCodeWorker`.
      - anything else → raises `ValueError` listing supported values.
    """
    backend = os.getenv("ORCH_WORKER_BACKEND", DEFAULT_BACKEND)

    if backend == "managed_agents":
        return ManagedAgentWorker(role=role)
    if backend == "docker":
        return DockerClaudeCodeWorker(role=role)
    raise ValueError(
        f"Unknown ORCH_WORKER_BACKEND value: {backend!r}. "
        f"Supported values: {sorted(KNOWN_BACKENDS)}."
    )
