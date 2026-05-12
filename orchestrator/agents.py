"""Backwards-compatible shim for the worker abstraction.

The implementation moved to `orchestrator/workers/` (see
`docs/PROPOSAL-docker-workers.md` Phase 1):

  - `Worker` protocol           → `orchestrator.workers.base`
  - `ManagedAgentWorker` + helpers → `orchestrator.workers.managed_agent`
  - `make_worker(role)` factory → `orchestrator.workers` package

This module re-exports those names so callers using the historical
`from orchestrator.agents import ...` / `orchestrator.agents.X` paths
keep working without churn. New code should import from
`orchestrator.workers` directly.
"""

from __future__ import annotations

from orchestrator.workers.base import Worker
from orchestrator.workers.managed_agent import (
    ALLOWED_NETWORK_HOSTS,
    DEFAULT_ENV_CONFIG,
    DEFAULT_MODEL,
    DEFAULT_TOOLS,
    PROMPTS_DIR,
    ManagedAgentWorker,
    _resource_signature,
    load_role_prompt,
)

__all__ = [
    "ALLOWED_NETWORK_HOSTS",
    "DEFAULT_ENV_CONFIG",
    "DEFAULT_MODEL",
    "DEFAULT_TOOLS",
    "PROMPTS_DIR",
    "ManagedAgentWorker",
    "Worker",
    "_resource_signature",
    "load_role_prompt",
]
