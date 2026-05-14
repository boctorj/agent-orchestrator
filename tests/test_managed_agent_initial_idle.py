"""Regression: ``_send_and_collect`` / ``wait_idle`` must not treat a
session's initial ``status_idle`` as completion.

A freshly-created Anthropic Managed Agents session is in ``idle`` state
until the user message we send transitions it to ``running``. The
streaming loop subscribes BEFORE the user message is delivered, so the
stream's first event can legitimately be that initial ``status_idle``.
The old loop body broke on the first ``status_idle`` it saw, returned
``""``, and the orchestrator marked the unit ``coder_no_marker`` ~3s
after spawn — exactly the symptom that escalated F-006-U-1 and U-2.

These tests use a fake Anthropic client whose event stream yields a
configurable sequence of events. The fix: only honor ``status_idle``
as completion after observing any other event in the stream.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.workers.managed_agent import ManagedAgentWorker


class _FakeStream:
    """Context-manager-shaped iterator over a fixed list of events."""

    def __init__(self, events: list[SimpleNamespace]) -> None:
        self._events = events

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self):
        return iter(self._events)


class _FakeEvents:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self._events = events
        self.sent: list[dict] = []

    def stream(self, _session_id: str) -> _FakeStream:
        return _FakeStream(self._events)

    def send(self, session_id: str, *, events: list[dict]) -> None:
        self.sent.append({"session_id": session_id, "events": events})


class _FakeSessions:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self.events = _FakeEvents(events)


class _FakeBeta:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self.sessions = _FakeSessions(events)


class _FakeAnthropic:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self.beta = _FakeBeta(events)


def _agent_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="agent.message",
        content=[SimpleNamespace(text=text)],
    )


def _status(name: str) -> SimpleNamespace:
    return SimpleNamespace(type=f"session.status_{name}")


@pytest.fixture
def _worker(monkeypatch):
    """Build a ``ManagedAgentWorker`` whose Anthropic client we can preload
    with a scripted event stream per test.

    Each test calls ``_worker(events)`` with the events the stream should
    emit, and gets back a worker plus the fake events object so it can
    assert what got sent.
    """

    def _build(events: list[SimpleNamespace]) -> tuple[ManagedAgentWorker, _FakeEvents]:
        fake = _FakeAnthropic(events)
        monkeypatch.setattr(
            "orchestrator.workers.managed_agent.Anthropic",
            lambda *a, **kw: fake,
        )
        worker = ManagedAgentWorker(role="coder")
        return worker, fake.beta.sessions.events

    return _build


def test_initial_status_idle_is_ignored(_worker):
    """The bug: when the stream's first event is ``status_idle`` (the
    session's pre-message idle state), the old loop returned ``""``
    immediately. After the fix it must keep listening until real activity
    happens, then idle.
    """
    events = [
        _status("idle"),  # initial idle — must be ignored
        _status("running"),
        _agent_message("PR_URL: https://github.com/o/r/pull/1"),
        _status("idle"),  # real completion
    ]
    worker, _ = _worker(events)

    result = worker._send_and_collect("sess_abc", "go build U-1")

    assert "PR_URL" in result, (
        "initial status_idle must not short-circuit the stream; the agent's "
        "actual response should be returned"
    )


def test_status_idle_after_activity_terminates(_worker):
    """Happy path: the fix must NOT regress normal completion. When idle
    arrives after activity, the loop exits and returns the collected
    agent.message text.
    """
    events = [
        _status("running"),
        _agent_message("hello"),
        _status("idle"),
    ]
    worker, _ = _worker(events)

    result = worker._send_and_collect("sess_abc", "say hi")

    assert result == "hello"


def test_user_message_is_sent_inside_stream(_worker):
    """Sanity: the user message still gets sent (the original send() call
    is preserved). Catches a refactor that drops the send.
    """
    events = [_status("running"), _agent_message("ok"), _status("idle")]
    worker, fake_events = _worker(events)

    worker._send_and_collect("sess_abc", "do the thing")

    assert len(fake_events.sent) == 1
    sent = fake_events.sent[0]
    assert sent["session_id"] == "sess_abc"
    assert sent["events"][0]["type"] == "user.message"
    assert sent["events"][0]["content"][0]["text"] == "do the thing"


def test_wait_idle_ignores_initial_status_idle(_worker):
    """The async ``wait_idle`` path has the same loop structure and the
    same bug. Spawn → ``spawn_async`` returns a session_id, then
    ``wait_idle`` blocks on the stream until ``status_idle``. If the
    first event the stream emits is the session's pre-message
    ``status_idle``, the old loop returned ``""`` and parallel_units
    saw empty output. Same fix needed.
    """
    events = [
        _status("idle"),  # initial idle — must be ignored
        _agent_message("PR_URL: https://github.com/o/r/pull/2"),
        _status("idle"),  # real completion
    ]
    worker, _ = _worker(events)

    result = worker.wait_idle("sess_xyz", timeout_seconds=5)

    assert "PR_URL" in result


def test_multiple_agent_messages_are_concatenated(_worker):
    """Two ``agent.message`` events before idle → both texts in output.
    Guards against a regression where ``saw_activity = True`` short-
    circuits the text-collection branch.
    """
    events = [
        _status("idle"),  # initial idle
        _agent_message("part one. "),
        _agent_message("part two."),
        _status("idle"),
    ]
    worker, _ = _worker(events)

    result = worker._send_and_collect("sess_abc", "x")

    assert result == "part one. part two."
