"""Tests for the `make_worker(role)` factory in orchestrator/workers.

These cover the four cases called out in F-001-U-1 (with case (c)
updated by F-001-U-2 once the docker backend landed):
  (a) `ORCH_WORKER_BACKEND` unset      → `ManagedAgentWorker`
  (b) `ORCH_WORKER_BACKEND=managed_agents` → `ManagedAgentWorker`
  (c) `ORCH_WORKER_BACKEND=docker`     → `DockerClaudeCodeWorker`
  (d) unknown backend value            → clear `ValueError`

`ManagedAgentWorker.__init__` constructs an `Anthropic` client which would
normally need an `ANTHROPIC_API_KEY`. Tests patch `anthropic.Anthropic`
with a sentinel so the factory can be exercised offline.
"""

from __future__ import annotations

import pytest

from orchestrator import workers
from orchestrator.workers import DockerClaudeCodeWorker, ManagedAgentWorker, make_worker


@pytest.fixture
def _stub_anthropic(monkeypatch):
    """Replace `anthropic.Anthropic` so `ManagedAgentWorker()` is cheap.

    The real client would raise without credentials; tests don't actually
    invoke any methods on the client, they just need construction to succeed.
    """

    class _FakeAnthropic:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("orchestrator.workers.managed_agent.Anthropic", _FakeAnthropic)


def test_unset_backend_returns_managed_agent_worker(monkeypatch, _stub_anthropic):
    """(a) When `ORCH_WORKER_BACKEND` is unset, the factory defaults to
    `ManagedAgentWorker`."""
    monkeypatch.delenv("ORCH_WORKER_BACKEND", raising=False)

    worker = make_worker("coder")

    assert isinstance(worker, ManagedAgentWorker)
    assert worker.role == "coder"


def test_managed_agents_backend_returns_managed_agent_worker(monkeypatch, _stub_anthropic):
    """(b) `ORCH_WORKER_BACKEND=managed_agents` explicitly selects the
    Anthropic backend."""
    monkeypatch.setenv("ORCH_WORKER_BACKEND", "managed_agents")

    worker = make_worker("tester")

    assert isinstance(worker, ManagedAgentWorker)
    assert worker.role == "tester"


def test_docker_backend_returns_docker_worker(monkeypatch):
    """(c) After F-001-U-2, `ORCH_WORKER_BACKEND=docker` returns the
    `DockerClaudeCodeWorker` implementation; it no longer raises."""
    monkeypatch.setenv("ORCH_WORKER_BACKEND", "docker")

    worker = make_worker("coder")

    assert isinstance(worker, DockerClaudeCodeWorker)
    assert worker.role == "coder"


def test_unknown_backend_raises_clear_value_error(monkeypatch):
    """(d) Any value outside the known set raises `ValueError` and lists
    the supported options so the user can self-correct."""
    monkeypatch.setenv("ORCH_WORKER_BACKEND", "totally-not-a-backend")

    with pytest.raises(ValueError) as excinfo:
        make_worker("coder")

    msg = str(excinfo.value)
    assert "totally-not-a-backend" in msg
    assert "ORCH_WORKER_BACKEND" in msg
    # Supported values are listed so the user can fix `.env`.
    assert "managed_agents" in msg
    assert "docker" in msg


def test_default_backend_constant_matches_unset_behavior():
    """`DEFAULT_BACKEND` is the source of truth for the unset case;
    keep them in sync."""
    assert workers.DEFAULT_BACKEND == "managed_agents"
    assert workers.DEFAULT_BACKEND in workers.KNOWN_BACKENDS
