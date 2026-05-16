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

    def retrieve(self, _session_id: str) -> SimpleNamespace:
        return SimpleNamespace(status="running")


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


def test_send_precedes_retrieve_precedes_stream(monkeypatch):
    """Structural fix: ``_send_and_collect`` must (1) send the user.message,
    (2) poll ``sessions.retrieve`` until the session leaves ``idle``,
    (3) subscribe to the SSE stream — in that order.

    PR #21 ignored a spurious initial ``status_idle``. PR #23 sent before
    stream-open so fresh sessions had time to flip ``idle`` → ``running``.
    Long-idle resumes broke both: the server's send → state-update window
    is wider on a session idle for hours, so the SSE endpoint still saw
    an ``idle`` session at subscribe-time, closed the stream with zero
    events, and the orchestrator escalated with ``fix_no_marker``
    (F-006-U-2). The poll closes the race.
    """
    call_order: list[str] = []

    class _OrderingEvents:
        def __init__(self) -> None:
            self._events = [
                _status("running"),
                _agent_message("ok"),
                _status("idle"),
            ]

        def send(self, session_id: str, *, events: list[dict]) -> None:
            call_order.append("send")

        def stream(self, _session_id: str) -> _FakeStream:
            call_order.append("stream")
            return _FakeStream(self._events)

    class _OrderingSessions:
        events = _OrderingEvents()

        def retrieve(self, _session_id: str) -> SimpleNamespace:
            call_order.append("retrieve")
            return SimpleNamespace(status="running")

    fake = SimpleNamespace(beta=SimpleNamespace(sessions=_OrderingSessions()))
    monkeypatch.setattr(
        "orchestrator.workers.managed_agent.Anthropic",
        lambda *a, **kw: fake,
    )
    worker = ManagedAgentWorker(role="coder")

    worker._send_and_collect("sess_abc", "go")

    assert call_order == ["send", "retrieve", "stream"], (
        f"expected send → retrieve(poll for active) → stream; got call order: {call_order}"
    )


def test_wait_for_session_active_polls_until_running(monkeypatch):
    """Long-idle resume: ``sessions.retrieve`` may return ``idle`` for a
    beat after ``events.send`` while the server propagates the state
    transition. Poll until the status flips away from ``idle`` before
    opening the stream. The F-006-U-2 escalation (session idle ~15h,
    ``fix_no_marker`` 4s after ``address_review``) is exactly this case.
    """
    statuses = iter(["idle", "idle", "running"])
    retrieve_calls = 0

    class _Sessions:
        class events:
            sent: list[dict] = []

            @classmethod
            def send(cls, session_id: str, *, events: list[dict]) -> None:
                cls.sent.append({"session_id": session_id, "events": events})

            @staticmethod
            def stream(_session_id: str) -> _FakeStream:
                return _FakeStream(
                    [
                        _status("running"),
                        _agent_message("FIX_PUSHED"),
                        _status("idle"),
                    ]
                )

        def retrieve(self, _session_id: str) -> SimpleNamespace:
            nonlocal retrieve_calls
            retrieve_calls += 1
            return SimpleNamespace(status=next(statuses))

    fake = SimpleNamespace(beta=SimpleNamespace(sessions=_Sessions()))
    monkeypatch.setattr(
        "orchestrator.workers.managed_agent.Anthropic",
        lambda *a, **kw: fake,
    )
    # Make the poll near-instant so the test doesn't burn wall-clock.
    monkeypatch.setattr(ManagedAgentWorker, "SESSION_ACTIVE_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(ManagedAgentWorker, "SESSION_ACTIVE_POLL_TIMEOUT", 1.0)

    worker = ManagedAgentWorker(role="coder")
    result = worker._send_and_collect("sess_long_idle", "address review comments")

    assert retrieve_calls == 3, (
        f"expected poll to run until status != 'idle' (3 retrieves: "
        f"idle, idle, running); got {retrieve_calls}"
    )
    assert result == "FIX_PUSHED"


def test_wait_for_session_active_times_out_when_status_stays_idle(monkeypatch):
    """Pathological: ``sessions.retrieve`` never reports a non-idle status.
    The poll must bail out at the configured timeout rather than hanging
    forever; the existing stream-open path then runs and the
    orchestrator's no-marker handler surfaces the stuck session as
    escalation.
    """
    retrieve_calls = 0

    class _Sessions:
        class events:
            sent: list[dict] = []

            @classmethod
            def send(cls, session_id: str, *, events: list[dict]) -> None:
                cls.sent.append({"session_id": session_id, "events": events})

            @staticmethod
            def stream(_session_id: str) -> _FakeStream:
                return _FakeStream([])  # zero events — the original bug shape

        def retrieve(self, _session_id: str) -> SimpleNamespace:
            nonlocal retrieve_calls
            retrieve_calls += 1
            return SimpleNamespace(status="idle")

    fake = SimpleNamespace(beta=SimpleNamespace(sessions=_Sessions()))
    monkeypatch.setattr(
        "orchestrator.workers.managed_agent.Anthropic",
        lambda *a, **kw: fake,
    )
    # Tight bounds so the test completes quickly; assert the loop respects them.
    monkeypatch.setattr(ManagedAgentWorker, "SESSION_ACTIVE_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(ManagedAgentWorker, "SESSION_ACTIVE_POLL_TIMEOUT", 0.05)

    worker = ManagedAgentWorker(role="coder")
    result = worker._send_and_collect("sess_stuck", "go")

    assert retrieve_calls >= 1, "poll must call retrieve at least once"
    assert result == "", (
        "stuck-idle session should fall through to the empty-stream path "
        "so the orchestrator's no-marker handler surfaces escalation"
    )


def test_wait_for_session_active_tolerates_retrieve_errors(monkeypatch):
    """Robustness: ``sessions.retrieve`` raising (network blip, SDK
    incompatibility) must not bring down ``_send_and_collect``. The poll
    is best-effort; on error the stream-open path still runs.
    """

    class _Sessions:
        class events:
            sent: list[dict] = []

            @classmethod
            def send(cls, session_id: str, *, events: list[dict]) -> None:
                cls.sent.append({"session_id": session_id, "events": events})

            @staticmethod
            def stream(_session_id: str) -> _FakeStream:
                return _FakeStream([_status("running"), _agent_message("done"), _status("idle")])

        def retrieve(self, _session_id: str) -> SimpleNamespace:
            raise RuntimeError("transient SDK failure")

    fake = SimpleNamespace(beta=SimpleNamespace(sessions=_Sessions()))
    monkeypatch.setattr(
        "orchestrator.workers.managed_agent.Anthropic",
        lambda *a, **kw: fake,
    )
    monkeypatch.setattr(ManagedAgentWorker, "SESSION_ACTIVE_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(ManagedAgentWorker, "SESSION_ACTIVE_POLL_TIMEOUT", 1.0)

    worker = ManagedAgentWorker(role="coder")
    result = worker._send_and_collect("sess_flaky_retrieve", "go")

    assert result == "done", (
        "retrieve errors must be swallowed; the stream-open path should still execute"
    )
