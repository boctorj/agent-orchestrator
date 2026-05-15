"""Tests for the F-008 ``tail_messages`` worker backend abstraction.

The unit lives in ``orchestrator/workers/{base,managed_agent,docker_claude_code}.py``.
Each backend must, given a ``session_id`` it knows about, return a
``TailResult`` dict shaped:

    {
        "status": "running" | "idle" | "terminated" | "not_found",
        "messages": list[{ts, role, text}],
        "reason": str | None,
    }

These tests cover all four ``status`` paths per backend with fully mocked
external surfaces — no real Anthropic API, no real ``docker`` daemon.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from orchestrator.workers.docker_claude_code import DockerClaudeCodeWorker
from orchestrator.workers.managed_agent import ManagedAgentWorker
from tests.conftest import _FakeProc, _make_worker

# ---------------------------------------------------------------------------
# Managed Agents backend
# ---------------------------------------------------------------------------


def _agent_message_event(text: str, ts: datetime) -> SimpleNamespace:
    """Build a fake ``agent.message`` event matching the Anthropic SDK shape."""
    return SimpleNamespace(
        type="agent.message",
        id="evt_" + text[:6],
        processed_at=ts,
        content=[SimpleNamespace(type="text", text=text)],
    )


class _FakeEventsList:
    """Stand-in for ``client.beta.sessions.events.list`` — iterable + records args."""

    def __init__(self, events: list[SimpleNamespace] | Exception) -> None:
        self._payload = events
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session_id: str, **kwargs: Any) -> Any:
        self.calls.append({"session_id": session_id, **kwargs})
        if isinstance(self._payload, Exception):
            raise self._payload
        return iter(self._payload)


def _build_managed_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session: object | Exception,
    events: list[SimpleNamespace] | Exception,
) -> tuple[ManagedAgentWorker, _FakeEventsList]:
    """Wire a ``ManagedAgentWorker`` with a fake Anthropic client.

    ``session`` is either an object returned by ``sessions.retrieve`` or an
    exception to raise. ``events`` is the iterable yielded by
    ``sessions.events.list`` (or an exception).
    """
    fake_events_list = _FakeEventsList(events)

    def fake_retrieve(_sid: str) -> object:
        if isinstance(session, Exception):
            raise session
        return session

    fake_client = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=SimpleNamespace(
                retrieve=fake_retrieve,
                events=SimpleNamespace(list=fake_events_list),
            )
        )
    )
    monkeypatch.setattr(
        "orchestrator.workers.managed_agent.Anthropic",
        lambda *a, **kw: fake_client,
    )
    return ManagedAgentWorker(role="coder"), fake_events_list


class TestManagedAgentTailMessages:
    def test_running_session_returns_status_running_with_messages(self, monkeypatch):
        t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        t1 = datetime(2025, 1, 1, 12, 0, 1, tzinfo=UTC)
        # Newest-first, as the Anthropic API returns when order='desc'
        events = [
            _agent_message_event("running tests", t1),
            _agent_message_event("opening branch", t0),
        ]
        session = SimpleNamespace(status="running")
        worker, calls = _build_managed_worker(monkeypatch, session=session, events=events)

        result = worker.tail_messages("sess_abc", limit=10)

        assert result["status"] == "running"
        assert result["reason"] is None
        # Result is chronological (oldest first) regardless of API order
        assert [m["text"] for m in result["messages"]] == ["opening branch", "running tests"]
        assert all(m["role"] == "agent" for m in result["messages"])
        assert all(m["ts"] for m in result["messages"])
        # `events.list` was asked for agent.message events only
        assert calls.calls[0]["session_id"] == "sess_abc"
        assert "agent.message" in calls.calls[0]["types"]
        assert calls.calls[0]["limit"] == 10

    def test_idle_session_returns_status_idle(self, monkeypatch):
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [_agent_message_event("PR_URL: https://x/p/1", ts)]
        session = SimpleNamespace(status="idle")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=events)

        result = worker.tail_messages("sess_abc")

        assert result["status"] == "idle"
        assert result["messages"][0]["text"] == "PR_URL: https://x/p/1"
        assert result["reason"] is None

    def test_terminated_session_returns_status_terminated_with_reason(self, monkeypatch):
        session = SimpleNamespace(status="terminated")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=[])

        result = worker.tail_messages("sess_abc")

        assert result["status"] == "terminated"
        # reason carries the raw status string so callers can surface it
        assert result["reason"] is not None
        assert "terminated" in result["reason"]
        assert result["messages"] == []

    def test_not_found_when_retrieve_raises_notfound(self, monkeypatch):
        # NotFoundError requires a real response object; the test only cares
        # about the exception class so a minimal stub works.
        fake_response = SimpleNamespace(
            request=SimpleNamespace(),
            headers={},
            status_code=404,
        )
        err = anthropic.NotFoundError(
            "session not found",
            response=fake_response,  # type: ignore[arg-type]
            body=None,
        )
        worker, _ = _build_managed_worker(monkeypatch, session=err, events=[])

        result = worker.tail_messages("sess_missing")

        assert result["status"] == "not_found"
        assert result["messages"] == []
        assert result["reason"] is not None
        assert "not found" in result["reason"].lower()

    def test_rescheduling_status_maps_to_running(self, monkeypatch):
        """A rescheduled session is still in-flight — surface as running."""
        session = SimpleNamespace(status="rescheduling")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=[])

        result = worker.tail_messages("sess_abc")

        assert result["status"] == "running"

    def test_non_agent_message_events_are_filtered(self, monkeypatch):
        """``events.list`` is asked for ``types=['agent.message']`` but a
        stray event of another type (forwarded by future SDK changes)
        must not slip through the tail.
        """
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [
            SimpleNamespace(type="session.status_idle", processed_at=ts),
            _agent_message_event("real reply", ts),
        ]
        session = SimpleNamespace(status="idle")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=events)

        result = worker.tail_messages("sess_abc")

        assert [m["text"] for m in result["messages"]] == ["real reply"]

    def test_agent_message_with_empty_text_is_skipped(self, monkeypatch):
        """An ``agent.message`` event whose blocks all have empty text
        (e.g. a tool-use turn the SDK serializes with empty text blocks)
        is dropped rather than emitted as an empty-text tail entry.
        """
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [
            SimpleNamespace(
                type="agent.message",
                processed_at=ts,
                content=[SimpleNamespace(type="text", text="")],
            ),
            _agent_message_event("after the empty", ts),
        ]
        session = SimpleNamespace(status="running")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=events)

        result = worker.tail_messages("sess_abc")

        assert [m["text"] for m in result["messages"]] == ["after the empty"]

    def test_limit_caps_collected_messages(self, monkeypatch):
        """Stops collecting after ``limit`` matching events even if the
        cursor yields more.
        """
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [_agent_message_event(f"msg-{i}", ts) for i in range(10)]
        session = SimpleNamespace(status="running")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=events)

        result = worker.tail_messages("sess_abc", limit=3)

        assert len(result["messages"]) == 3

    def test_messages_returned_chronologically_when_api_yields_desc(self, monkeypatch):
        """The Anthropic API yields ``order='desc'`` (newest first); the
        result must be chronological so the caller can tail in reading order.
        """
        t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        t1 = datetime(2025, 1, 1, 12, 0, 1, tzinfo=UTC)
        t2 = datetime(2025, 1, 1, 12, 0, 2, tzinfo=UTC)
        # Newest first, as the Anthropic API returns when order='desc'
        events = [
            _agent_message_event("third", t2),
            _agent_message_event("second", t1),
            _agent_message_event("first", t0),
        ]
        session = SimpleNamespace(status="running")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=events)

        result = worker.tail_messages("sess_abc")

        assert [m["text"] for m in result["messages"]] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------


def _write_session_jsonl(home: Path, session_id: str, lines: list[dict]) -> Path:
    path = home / ".claude" / "sessions" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(ln) for ln in lines))
    return path


def _inspect_proc(state: str, exit_code: int = 0) -> _FakeProc:
    """``docker inspect --format '{{.State.Status}} {{.State.ExitCode}}'`` stub."""
    return _FakeProc(stdout=f"{state} {exit_code}\n", returncode=0)


@pytest.fixture
def docker_worker(tmp_path: Path) -> DockerClaudeCodeWorker:
    return _make_worker(tmp_path)


class TestDockerWorkerTailMessages:
    def test_running_container_returns_status_running(self, docker_worker):
        # Session JSONL with one assistant message
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-xyz",
            [
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working on it"}],
                    },
                }
            ],
        )
        docker_worker.run = lambda *a, **kw: _inspect_proc("running", 0)

        result = docker_worker.tail_messages("sess-xyz")

        assert result["status"] == "running"
        assert result["reason"] is None
        assert len(result["messages"]) == 1
        assert result["messages"][0]["text"] == "working on it"
        assert result["messages"][0]["role"] == "assistant"
        assert result["messages"][0]["ts"] == "2025-01-01T12:00:00Z"

    def test_exited_zero_returns_status_idle(self, docker_worker):
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-done",
            [
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "all done"}],
                    },
                }
            ],
        )
        docker_worker.run = lambda *a, **kw: _inspect_proc("exited", 0)

        result = docker_worker.tail_messages("sess-done")

        assert result["status"] == "idle"
        assert result["reason"] is None
        assert result["messages"][0]["text"] == "all done"

    def test_exited_nonzero_returns_status_terminated_with_reason(self, docker_worker):
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-crash",
            [
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "about to crash"}],
                    },
                }
            ],
        )
        docker_worker.run = lambda *a, **kw: _inspect_proc("exited", 137)

        result = docker_worker.tail_messages("sess-crash")

        assert result["status"] == "terminated"
        assert result["reason"] is not None
        assert "137" in result["reason"]
        # Messages already collected pre-crash are still returned
        assert result["messages"][0]["text"] == "about to crash"

    def test_no_container_no_session_file_returns_not_found(self, docker_worker):
        # docker inspect returns non-zero (no such container)
        docker_worker.run = lambda *a, **kw: _FakeProc(
            stdout="", stderr="No such object: sess-ghost", returncode=1
        )

        result = docker_worker.tail_messages("sess-ghost")

        assert result["status"] == "not_found"
        assert result["messages"] == []

    def test_no_container_but_session_file_exists_returns_idle(self, docker_worker):
        """``--rm`` removes the container on exit so a completed worker
        leaves only the JSONL behind. Infer ``idle`` in that case rather
        than ``not_found`` — the work finished, we just can't ask the
        container what its exit code was."""
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-completed",
            [
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "PR_URL: https://x/p/2"}],
                    },
                }
            ],
        )
        docker_worker.run = lambda *a, **kw: _FakeProc(
            stdout="", stderr="No such object", returncode=1
        )

        result = docker_worker.tail_messages("sess-completed")

        assert result["status"] == "idle"
        assert result["messages"][0]["text"] == "PR_URL: https://x/p/2"

    def test_limit_truncates_messages_to_most_recent(self, docker_worker):
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-many",
            [
                {
                    "type": "assistant",
                    "timestamp": f"2025-01-01T12:00:0{i}Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"msg-{i}"}],
                    },
                }
                for i in range(5)
            ],
        )
        docker_worker.run = lambda *a, **kw: _inspect_proc("running", 0)

        result = docker_worker.tail_messages("sess-many", limit=2)

        # Keep the LAST two (most recent), in chronological order
        assert [m["text"] for m in result["messages"]] == ["msg-3", "msg-4"]

    def test_docker_inspect_called_with_session_id_as_container_name(self, docker_worker):
        """Convention: the container is named after the session_id so
        ``docker inspect <session_id>`` works without a side-channel
        lookup. Future spawn() wiring follows this contract."""
        _write_session_jsonl(docker_worker.home_dir, "sess-arg", [])
        captured: dict[str, list[str]] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return _inspect_proc("running", 0)

        docker_worker.run = fake_run
        docker_worker.tail_messages("sess-arg")

        assert "docker" in captured["argv"][0]
        assert "inspect" in captured["argv"]
        assert "sess-arg" in captured["argv"]
