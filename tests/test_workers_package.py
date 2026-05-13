"""Tester-authored verification for F-001-U-1 (Worker abstraction cleanup).

Independent from the coder-authored `tests/test_worker_factory.py`. These
tests verify the four behavioral requirements of the unit description AND
the structural promises ("pure refactor, no behavior change"):

  Structural:
    * `Worker` protocol lives in `orchestrator.workers.base`.
    * `ManagedAgentWorker` lives in `orchestrator.workers.managed_agent`.
    * `make_worker(role)` is exposed by the `orchestrator.workers` package.
    * `orchestrator.agents` keeps re-exporting `Worker` and
      `ManagedAgentWorker` so existing import sites do not break.

  Behavioral (the four cases the unit description spells out;
  case (c) updated by F-001-U-2 once the docker backend landed):
    (a) `ORCH_WORKER_BACKEND` unset             -> ManagedAgentWorker
    (b) `ORCH_WORKER_BACKEND=managed_agents`    -> ManagedAgentWorker
    (c) `ORCH_WORKER_BACKEND=docker`            -> DockerClaudeCodeWorker
    (d) any other value                         -> clear error

  Edge case:
    * The `role` argument is propagated unchanged onto the worker.
    * Two calls return two distinct worker instances (factory, not singleton).

`ManagedAgentWorker.__init__` instantiates `anthropic.Anthropic()`, which
will look for `ANTHROPIC_API_KEY`. The fixture below replaces it with a
no-op stub so the factory can be exercised hermetically.
"""

from __future__ import annotations

import importlib
from typing import get_type_hints

import pytest


@pytest.fixture
def stub_anthropic(monkeypatch):
    """Replace `anthropic.Anthropic` in the managed_agent module so the
    constructor does not try to read credentials or touch the network."""

    class _FakeAnthropic:
        def __init__(self, *args, **kwargs):  # noqa: D401 - test stub
            pass

    monkeypatch.setattr(
        "orchestrator.workers.managed_agent.Anthropic",
        _FakeAnthropic,
    )


# ---------------------------------------------------------------------------
# Structural requirements — "Move ManagedAgentWorker to
# orchestrator/workers/managed_agent.py. Extract Worker protocol into
# workers/base.py. Add make_worker(role) factory..."
# ---------------------------------------------------------------------------


class TestPackageLayout:
    def test_worker_protocol_lives_in_workers_base(self):
        base = importlib.import_module("orchestrator.workers.base")
        assert hasattr(base, "Worker"), (
            "Worker protocol must be importable from orchestrator.workers.base"
        )

    def test_managed_agent_worker_lives_in_workers_managed_agent(self):
        mod = importlib.import_module("orchestrator.workers.managed_agent")
        assert hasattr(mod, "ManagedAgentWorker"), (
            "ManagedAgentWorker must live in orchestrator/workers/managed_agent.py"
        )

    def test_make_worker_exposed_from_workers_package(self):
        pkg = importlib.import_module("orchestrator.workers")
        assert callable(getattr(pkg, "make_worker", None)), (
            "make_worker(role) must be exposed by orchestrator.workers"
        )

    def test_worker_protocol_methods(self):
        """The Worker protocol must declare the methods downstream code
        depends on: spawn / resume / archive, plus a `role` attribute."""
        from orchestrator.workers.base import Worker

        for method in ("spawn", "resume", "archive"):
            assert hasattr(Worker, method), f"Worker protocol missing `{method}`"
        # `role` is declared as an attribute annotation on the protocol.
        # get_type_hints surfaces it.
        hints = get_type_hints(Worker)
        assert "role" in hints, "Worker protocol must declare a `role` attribute"


# ---------------------------------------------------------------------------
# Backwards-compatibility shim — the unit description says
# "Pure refactor, no behavior change." Callers using
# `from orchestrator.agents import ...` must still work and yield the
# SAME class objects as the new home, otherwise isinstance() checks across
# the codebase silently break.
# ---------------------------------------------------------------------------


class TestBackwardsCompatibility:
    def test_agents_module_reexports_worker_protocol(self):
        from orchestrator import agents
        from orchestrator.workers.base import Worker

        assert agents.Worker is Worker

    def test_agents_module_reexports_managed_agent_worker(self):
        from orchestrator import agents
        from orchestrator.workers.managed_agent import ManagedAgentWorker

        assert agents.ManagedAgentWorker is ManagedAgentWorker

    def test_existing_agents_helpers_still_importable(self):
        """`tests/test_agents.py` uses these symbols off `orchestrator.agents`.
        The shim must keep them reachable so the existing test suite keeps
        passing unchanged (a stated requirement of the unit)."""
        from orchestrator import agents

        for name in (
            "ALLOWED_NETWORK_HOSTS",
            "DEFAULT_ENV_CONFIG",
            "DEFAULT_MODEL",
            "_resource_signature",
        ):
            assert hasattr(agents, name), f"orchestrator.agents must re-export {name}"


# ---------------------------------------------------------------------------
# Factory behavior — the four cases the unit description requires.
# ---------------------------------------------------------------------------


class TestMakeWorkerFactory:
    # (a) unset -> ManagedAgentWorker
    def test_unset_env_returns_managed_agent_worker(self, monkeypatch, stub_anthropic):
        from orchestrator.workers import make_worker
        from orchestrator.workers.managed_agent import ManagedAgentWorker

        monkeypatch.delenv("ORCH_WORKER_BACKEND", raising=False)

        worker = make_worker("coder")

        assert isinstance(worker, ManagedAgentWorker)

    # (b) managed_agents -> ManagedAgentWorker
    def test_managed_agents_value_returns_managed_agent_worker(self, monkeypatch, stub_anthropic):
        from orchestrator.workers import make_worker
        from orchestrator.workers.managed_agent import ManagedAgentWorker

        monkeypatch.setenv("ORCH_WORKER_BACKEND", "managed_agents")

        worker = make_worker("reviewer")

        assert isinstance(worker, ManagedAgentWorker)

    # (c) docker -> DockerClaudeCodeWorker (since F-001-U-2)
    def test_docker_value_returns_docker_worker(self, monkeypatch):
        from orchestrator.workers import DockerClaudeCodeWorker, make_worker

        monkeypatch.setenv("ORCH_WORKER_BACKEND", "docker")

        worker = make_worker("coder")

        assert isinstance(worker, DockerClaudeCodeWorker)
        assert worker.role == "coder"

    # (d) unknown -> clear error
    def test_unknown_backend_raises_with_actionable_message(self, monkeypatch):
        from orchestrator.workers import make_worker

        monkeypatch.setenv("ORCH_WORKER_BACKEND", "not-a-real-backend")

        with pytest.raises((ValueError, RuntimeError)) as excinfo:
            make_worker("coder")

        msg = str(excinfo.value)
        # The bad value must appear so the user can find their typo,
        # and the env var name must appear so they know what to fix.
        assert "not-a-real-backend" in msg
        assert "ORCH_WORKER_BACKEND" in msg
        # And the supported options must be enumerated.
        assert "managed_agents" in msg


# ---------------------------------------------------------------------------
# Edge cases / interface contract.
# ---------------------------------------------------------------------------


class TestFactoryContract:
    def test_role_is_propagated_to_worker(self, monkeypatch, stub_anthropic):
        from orchestrator.workers import make_worker

        monkeypatch.delenv("ORCH_WORKER_BACKEND", raising=False)

        for role in ("coder", "tester", "reviewer"):
            worker = make_worker(role)
            assert worker.role == role, f"role should be set to {role!r} on the worker"

    def test_factory_returns_fresh_instance_each_call(self, monkeypatch, stub_anthropic):
        """The description calls this a *factory*. Two calls must produce
        two distinct objects so callers can hold per-unit state without
        accidental sharing."""
        from orchestrator.workers import make_worker

        monkeypatch.delenv("ORCH_WORKER_BACKEND", raising=False)

        a = make_worker("coder")
        b = make_worker("coder")
        assert a is not b
