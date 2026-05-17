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
        worker, calls = _build_managed_worker(monkeypatch, session=err, events=[])

        result = worker.tail_messages("sess_missing")

        assert result["status"] == "not_found"
        assert result["messages"] == []
        assert result["reason"] is not None
        assert "not found" in result["reason"].lower()
        # C6: the NotFound branch is the only one with `messages` guaranteed
        # empty per the docstring contract; assert the short-circuit so a
        # refactor that moves events.list ahead of retrieve regresses loudly.
        assert calls.calls == [], (
            "events.list must NOT be called after retrieve raises NotFoundError"
        )

    def test_rescheduling_status_maps_to_running(self, monkeypatch):
        """A rescheduled session is still in-flight — surface as running."""
        session = SimpleNamespace(status="rescheduling")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=[])

        result = worker.tail_messages("sess_abc")

        assert result["status"] == "running"
        # rescheduling IS in the known map, so reason stays None
        assert result["reason"] is None

    def test_unknown_status_maps_to_running_with_reason(self, monkeypatch):
        """C4: future SDK status values (e.g. ``requires_action``) must
        NOT silently default to ``idle`` — that tells callers "safe to
        archive" when the work hasn't finished. Default to ``running``
        and surface the raw value via ``reason`` so observability is
        retained.
        """
        session = SimpleNamespace(status="requires_action")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=[])

        result = worker.tail_messages("sess_abc")

        assert result["status"] == "running"
        assert result["reason"] == "session.status=requires_action"

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


# ---------------------------------------------------------------------------
# C1: docker JSONL must filter to assistant/agent records — no user prompts.
# ---------------------------------------------------------------------------


class TestDockerAssistantOnlyFilter:
    def test_user_prompts_are_filtered_out(self, docker_worker):
        """C1: a representative claude-code JSONL with user prompts +
        assistant replies + tool turns must yield only the assistant
        replies. Matches the managed-agent backend's
        ``types=['agent.message']`` semantics."""
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-mixed",
            [
                {
                    "type": "user",
                    "timestamp": "2025-01-01T12:00:00Z",
                    "message": {"role": "user", "content": "please open a PR"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "starting work"}],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2025-01-01T12:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "text": "build ok"}],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:03Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "finished"}],
                    },
                },
            ],
        )
        docker_worker.run = lambda *a, **kw: _inspect_proc("running", 0)

        result = docker_worker.tail_messages("sess-mixed")

        assert [m["text"] for m in result["messages"]] == ["starting work", "finished"]
        assert all(m["role"] == "assistant" for m in result["messages"])

    def test_system_and_tool_records_are_filtered_out(self, docker_worker):
        """Defensive: shapes other than ``user`` / ``assistant`` (system,
        tool_use, tool_result) must also drop out."""
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-tools",
            [
                {
                    "type": "system",
                    "timestamp": "2025-01-01T12:00:00Z",
                    "message": {"role": "system", "content": "init"},
                },
                {
                    "type": "tool_use",
                    "timestamp": "2025-01-01T12:00:01Z",
                    "message": {"role": "assistant", "content": "calling bash"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:02Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "real reply"}],
                    },
                },
            ],
        )
        docker_worker.run = lambda *a, **kw: _inspect_proc("running", 0)

        result = docker_worker.tail_messages("sess-tools")

        assert [m["text"] for m in result["messages"]] == ["real reply"]


# ---------------------------------------------------------------------------
# C2: container State.Status mapping for the non-running, non-exited cases.
# ---------------------------------------------------------------------------


class TestDockerContainerStateMapping:
    @pytest.mark.parametrize(
        "state,expected_status",
        [
            ("created", "terminated"),  # never started — no work to tail
            ("dead", "terminated"),  # daemon couldn't clean up — won't recover
            ("removing", "terminated"),  # mid-teardown
            ("paused", "terminated"),  # suspended; caller shouldn't wait
            ("restarting", "running"),  # transient
        ],
    )
    def test_non_running_non_exited_states_map_per_table(
        self, docker_worker, state, expected_status
    ):
        """C2: each non-running, non-exited state has a deterministic
        mapping in the four-value taxonomy. Reason carries the raw
        container_state so callers can disambiguate."""
        _write_session_jsonl(docker_worker.home_dir, f"sess-{state}", [])
        docker_worker.run = lambda *a, **kw: _inspect_proc(state, 0)

        result = docker_worker.tail_messages(f"sess-{state}")

        assert result["status"] == expected_status
        assert result["reason"] == f"container_state={state}"

    def test_unknown_future_state_defaults_to_terminated(self, docker_worker):
        """Future Docker versions may add states this code doesn't know
        about. The fail-safe default is ``terminated`` so callers don't
        wait forever on an unrecognized state."""
        _write_session_jsonl(docker_worker.home_dir, "sess-fut", [])
        docker_worker.run = lambda *a, **kw: _inspect_proc("hypothetical-future-state", 0)

        result = docker_worker.tail_messages("sess-fut")

        assert result["status"] == "terminated"
        assert result["reason"] == "container_state=hypothetical-future-state"


# ---------------------------------------------------------------------------
# C3: docker inspect failure modes — disambiguate "no such object" from
# daemon-down / permission-denied / timeout / CLI-missing.
# ---------------------------------------------------------------------------


class TestDockerInspectFailureModes:
    def test_daemon_down_surfaces_as_terminated_with_reason(self, docker_worker):
        """``docker inspect`` against a stopped daemon returns non-zero
        with ``Cannot connect to the Docker daemon`` on stderr. This is
        an operational outage — surface as ``terminated`` (caller
        shouldn't wait) with the stderr in ``reason`` so the user
        knows to start their daemon."""
        docker_worker.run = lambda *a, **kw: _FakeProc(
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            returncode=1,
        )

        result = docker_worker.tail_messages("sess-daemon-down")

        assert result["status"] == "terminated"
        assert result["reason"] is not None
        assert "Cannot connect to the Docker daemon" in result["reason"]
        assert "docker inspect failed" in result["reason"]

    def test_permission_denied_surfaces_as_terminated_with_reason(self, docker_worker):
        docker_worker.run = lambda *a, **kw: _FakeProc(
            stdout="",
            stderr="permission denied while trying to connect to the Docker daemon socket",
            returncode=1,
        )

        result = docker_worker.tail_messages("sess-perm")

        assert result["status"] == "terminated"
        assert "permission denied" in (result["reason"] or "")

    def test_inspect_timeout_surfaces_as_terminated_with_reason(self, docker_worker):
        """A 10s ``docker inspect`` timeout (slow daemon) raises
        ``subprocess.TimeoutExpired``. Don't swallow it as
        ``not_found`` — surface so the user sees the daemon-health
        problem."""
        import subprocess

        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="docker inspect", timeout=10)

        docker_worker.run = fake_run

        result = docker_worker.tail_messages("sess-timeout")

        assert result["status"] == "terminated"
        assert result["reason"] is not None
        assert "TimeoutExpired" in result["reason"]

    def test_docker_cli_missing_surfaces_as_terminated_with_reason(self, docker_worker):
        """``docker`` not on ``$PATH`` raises ``FileNotFoundError``.
        Surface as terminated with reason."""

        def fake_run(*a, **kw):
            raise FileNotFoundError("[Errno 2] No such file or directory: 'docker'")

        docker_worker.run = fake_run

        result = docker_worker.tail_messages("sess-no-cli")

        assert result["status"] == "terminated"
        assert "FileNotFoundError" in (result["reason"] or "")

    def test_no_such_object_stderr_still_maps_to_not_found(self, docker_worker):
        """Regression: the genuine "container removed by --rm" case
        must NOT get caught by the new error-surfacing branch."""
        docker_worker.run = lambda *a, **kw: _FakeProc(
            stdout="",
            stderr="Error: No such object: sess-ghost",
            returncode=1,
        )

        result = docker_worker.tail_messages("sess-ghost")

        assert result["status"] == "not_found"
        assert result["reason"] is None

    def test_programmer_errors_propagate(self, docker_worker):
        """A bug introduced into the runner (e.g. argv shape change)
        must NOT be silently swallowed as "no container". The narrowed
        exception handling in ``_inspect_container`` lets non-subprocess
        errors propagate so the test fails loudly."""

        def fake_run(*a, **kw):
            raise TypeError("argv must be a list of strings")

        docker_worker.run = fake_run

        with pytest.raises(TypeError, match="argv must be a list of strings"):
            docker_worker.tail_messages("sess-bug")


# ---------------------------------------------------------------------------
# C5: limit validation must agree across backends.
# ---------------------------------------------------------------------------


class TestLimitValidation:
    @pytest.mark.parametrize("bad_limit", [0, -1, -100])
    def test_managed_agent_rejects_non_positive_limit(self, monkeypatch, bad_limit):
        session = SimpleNamespace(status="running")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=[])

        with pytest.raises(ValueError, match="limit must be an int >= 1"):
            worker.tail_messages("sess_abc", limit=bad_limit)

    @pytest.mark.parametrize("bad_limit", [0, -1, -100])
    def test_docker_worker_rejects_non_positive_limit(self, docker_worker, bad_limit):
        with pytest.raises(ValueError, match="limit must be an int >= 1"):
            docker_worker.tail_messages("sess_abc", limit=bad_limit)

    def test_managed_agent_accepts_limit_of_one(self, monkeypatch):
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [_agent_message_event("only one", ts)]
        session = SimpleNamespace(status="running")
        worker, _ = _build_managed_worker(monkeypatch, session=session, events=events)

        result = worker.tail_messages("sess_abc", limit=1)

        assert len(result["messages"]) == 1

    def test_docker_worker_accepts_limit_of_one(self, docker_worker):
        _write_session_jsonl(
            docker_worker.home_dir,
            "sess-one",
            [
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "only one"}],
                    },
                }
            ],
        )
        docker_worker.run = lambda *a, **kw: _inspect_proc("running", 0)

        result = docker_worker.tail_messages("sess-one", limit=1)

        assert len(result["messages"]) == 1
