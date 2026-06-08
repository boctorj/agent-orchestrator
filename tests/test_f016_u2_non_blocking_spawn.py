"""F-016-U-2 — Phase 1: non-blocking spawn primitives.

Acceptance criteria from ``features/F-016/spec.md`` (Phase 1):

  * ``spawn_unit_async`` returns in ≤2s and persists ``coder_session_id``
    BEFORE returning — kills the ghost-row failure class (status=coding
    with empty session_id when the lead is killed mid-spawn).
  * ``wait_unit`` blocks for the worker's terminal marker OR returns a
    structured ``still_running`` on timeout — caller decides next step.
  * ``ManagedAgentWorker.resume_async`` mirrors ``spawn_async``: it
    submits the user-message event and returns without waiting for the
    worker's response.

These tests pin the contract: the session_id MUST be persisted before
``worker.spawn_async`` returns to the caller (the ghost-row guarantee
depends on a write-then-call ordering inside ``spawn_unit_async``).
"""

from __future__ import annotations

import json

import pytest

from orchestrator import state
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- fixtures ---------------------------


@pytest.fixture(autouse=True)
def _force_verified(monkeypatch, tmp_state_db):
    """Bypass the verify_repo gate — these tests exercise the async
    spawn/wait primitives, not the verification surface.
    """
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)


@pytest.fixture(autouse=True)
def _stub_safe_amend(monkeypatch):
    """Don't touch GitHub from these tests."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _fake_github_token(monkeypatch):
    """Make ``need_github_token`` pass without exposing a real PAT.

    Clears App env so the PAT path wins deterministically (mirrors the
    ``with_github_token`` conftest fixture, autouse'd for the whole module).
    """
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_fake_for_tests")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _seed_feature(feature_id: str = "F-016") -> None:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
            branch_prefix="async",
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description="impl")],
    )
    state.approve_plan(feature_id)


# --------------------------- fakes ---------------------------


class _RecordingAsyncWorker:
    """Stub for ``ManagedAgentWorker`` that records every async call.

    ``spawn_async`` records when it was entered and returns a canned
    ``session_id``. ``wait_idle`` returns a canned response or raises a
    ``TimeoutError``.
    """

    def __init__(
        self,
        role: str,
        *,
        session_id: str = "sesn-async",
        wait_response: str = "",
        wait_raises: BaseException | None = None,
        spawn_observer=None,
    ):
        self.role = role
        self._session_id = session_id
        self._wait_response = wait_response
        self._wait_raises = wait_raises
        self._spawn_observer = spawn_observer
        self.spawn_async_calls: list[tuple[str, str | None]] = []
        self.wait_idle_calls: list[tuple[str, int]] = []
        self.resume_async_calls: list[tuple[str, str]] = []

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        if self._spawn_observer is not None:
            self._spawn_observer()
        self.spawn_async_calls.append((task, title))
        return self._session_id

    def wait_idle(self, session_id: str, *, timeout_seconds: int = 1800) -> str:
        self.wait_idle_calls.append((session_id, timeout_seconds))
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._wait_response

    def resume_async(self, session_id: str, msg: str) -> None:
        self.resume_async_calls.append((session_id, msg))

    # Unused on this surface but defined for protocol completeness.
    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        raise AssertionError("spawn (blocking) must not be called by async primitives")

    def resume(self, session_id: str, msg: str) -> str:
        raise AssertionError("resume (blocking) must not be called by async primitives")

    def archive(self, session_id: str) -> None:
        return None


def _install_worker_factory(monkeypatch, worker: _RecordingAsyncWorker) -> None:
    # spawn_unit_async / wait_unit go through ``make_worker(role)`` so
    # ORCH_WORKER_BACKEND is honored — see the F-016-U-2 reviewer thread
    # on ``orchestrator/tools/execution.py``. Tests patch the factory at
    # its execution-module import site.
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda role: worker)


# --------------------------- spawn_unit_async ---------------------------


class TestSpawnUnitAsyncPersistsSessionId:
    """The ghost-row defence: ``coder_session_id`` is persisted to
    state.db BEFORE ``spawn_unit_async`` returns, so a lead killed
    *between this tool's return and the next call* cannot leave a row
    with ``status=coding`` and ``coder_session_id=""``. The
    *mid-spawn_async* window (during the worker call, before the id is
    known) is the only remaining gap, and it's recoverable via
    ``scan_unit_session`` from a fresh lead.
    """

    def test_returns_session_id_and_coding_status(self, tmp_state_db, monkeypatch):
        _seed_feature()
        worker = _RecordingAsyncWorker("coder", session_id="sesn-coder-abc")
        _install_worker_factory(monkeypatch, worker)

        result_json = execution.spawn_unit_async("F-016", "F-016-U-1")
        result = json.loads(result_json)
        assert result["unit_id"] == "F-016-U-1"
        assert result["session_id"] == "sesn-coder-abc"
        assert result["status"] == "coding"

        unit_state = state.get_unit_state("F-016-U-1")
        assert unit_state is not None
        assert unit_state.coder_session_id == "sesn-coder-abc"
        assert unit_state.status == "coding"

    def test_session_id_persisted_before_spawn_async_returns(self, tmp_state_db, monkeypatch):
        """Pin both halves of the ghost-row defence in one test:

        1. **During** ``worker.spawn_async`` — the row exists with
           ``status="coding"`` and ``branch`` set, BUT
           ``coder_session_id=""`` (we don't have the id yet). A lead
           killed in this window leaves a row recoverable via
           ``scan_unit_session`` from a fresh lead, since the worker
           session is live on the backend.
        2. **After** ``spawn_unit_async`` returns — the row carries the
           ``coder_session_id`` returned by ``worker.spawn_async``. A
           future regression that moved the post-spawn upsert later (or
           dropped it) would now fail HERE, not silently pass.

        The two assertions together defend the spec contract (*"session
        id persisted before spawn_unit_async returns"*) without
        overclaiming the impossible (*"persisted before worker.spawn_async
        returns"* — that would require knowing the id before getting it).
        """
        _seed_feature()

        observed: dict[str, object] = {}

        def observer() -> None:
            # Called from inside the worker.spawn_async stub, BEFORE it
            # returns the session_id. Snapshot the in-window row so the
            # post-call assertions can compare both sides.
            observed["row"] = state.get_unit_state("F-016-U-1")

        worker = _RecordingAsyncWorker("coder", session_id="sesn-x", spawn_observer=observer)
        _install_worker_factory(monkeypatch, worker)

        execution.spawn_unit_async("F-016", "F-016-U-1")

        # (1) In-window state: row exists, status=coding, branch set,
        # but session_id is still empty (we haven't received it yet).
        row = observed["row"]
        assert row is not None
        assert row.status == "coding"
        assert row.branch  # branch is set pre-submit
        assert row.coder_session_id == "", (
            "in-window state must be (status=coding, session_id='') — the "
            "row exists for recovery, but the id isn't known yet"
        )

        # (2) Post-return state: coder_session_id now carries the value
        # spawn_async returned. This is the load-bearing assertion for
        # the spec contract.
        post = state.get_unit_state("F-016-U-1")
        assert post is not None
        assert post.coder_session_id == "sesn-x"
        assert post.status == "coding"

    def test_records_spawn_coder_event(self, tmp_state_db, monkeypatch):
        _seed_feature()
        worker = _RecordingAsyncWorker("coder", session_id="sesn-y")
        _install_worker_factory(monkeypatch, worker)

        execution.spawn_unit_async("F-016", "F-016-U-1")

        events = [e["event_type"] for e in state.list_events("F-016-U-1")]
        assert "spawn_coder_async" in events

    def test_uses_spawn_async_not_blocking_spawn(self, tmp_state_db, monkeypatch):
        """``spawn_unit_async`` must use ``worker.spawn_async`` — never the
        blocking ``worker.spawn``. The fake raises on .spawn() so this catches
        a regression that silently falls back to the blocking primitive.
        """
        _seed_feature()
        worker = _RecordingAsyncWorker("coder", session_id="sesn-z")
        _install_worker_factory(monkeypatch, worker)

        execution.spawn_unit_async("F-016", "F-016-U-1")

        assert len(worker.spawn_async_calls) == 1

    def test_rejects_unknown_feature(self, tmp_state_db):
        msg = execution.spawn_unit_async("F-NOPE", "F-NOPE-U-1")
        assert msg.startswith("ERROR")

    def test_rejects_unapproved_feature(self, tmp_state_db):
        state.save_feature(
            Feature(
                id="F-016",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="draft",
            )
        )
        msg = execution.spawn_unit_async("F-016", "F-016-U-1")
        assert "ERROR" in msg and "approved" in msg

    def test_rejects_unit_missing_from_plan(self, tmp_state_db):
        _seed_feature()
        msg = execution.spawn_unit_async("F-016", "F-016-U-NOPE")
        assert "ERROR" in msg

    def test_rejects_unit_with_existing_coder_session(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status="coding",
                branch="async-u-1",
                coder_session_id="sesn-existing",
            )
        )
        msg = execution.spawn_unit_async("F-016", "F-016-U-1")
        assert "ERROR" in msg
        assert "sesn-existing" in msg

    def test_spawn_async_failure_escalates(self, tmp_state_db, monkeypatch):
        """If worker.spawn_async raises (e.g. Anthropic API down), the unit
        must end up escalated with the error captured — NOT silently stuck
        in coding state.
        """
        _seed_feature()

        class _BoomWorker(_RecordingAsyncWorker):
            def spawn_async(self, task, *, title=None):  # type: ignore[override]
                raise RuntimeError("api down")

        worker = _BoomWorker("coder")
        _install_worker_factory(monkeypatch, worker)

        msg = execution.spawn_unit_async("F-016", "F-016-U-1")

        assert "ERROR" in msg
        assert "api down" in msg
        row = state.get_unit_state("F-016-U-1")
        assert row is not None
        assert row.status == "escalated"
        assert "api down" in row.last_error


# --------------------------- wait_unit ---------------------------


class TestWaitUnit:
    """``wait_unit`` is the explicit-wait counterpart of ``spawn_unit_async``.

    On a terminal marker → returns the parsed marker JSON (and the unit
    row is advanced via the existing ``_record_terminal_marker``).

    On timeout → returns ``{status: "still_running", reason: "timeout"}``
    WITHOUT touching the unit row (the daemon, or the lead's follow-up
    call, retains responsibility).
    """

    def _seed_coding_unit(self) -> None:
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1",
                feature_id="F-016",
                status="coding",
                branch="async-u-1",
                coder_session_id="sesn-coder",
            )
        )

    def test_terminal_marker_advances_state_and_returns_marker(self, tmp_state_db, monkeypatch):
        self._seed_coding_unit()
        worker = _RecordingAsyncWorker(
            "coder",
            session_id="sesn-coder",
            wait_response="PR_URL: https://github.com/o/r/pull/42",
        )
        _install_worker_factory(monkeypatch, worker)

        result_json = execution.wait_unit("F-016-U-1", "coder", timeout_s=60)
        result = json.loads(result_json)

        assert result["marker"] == "PR_URL"
        assert result["status"] == "in_ci"
        row = state.get_unit_state("F-016-U-1")
        assert row is not None
        assert row.status == "in_ci"

    def test_timeout_returns_still_running_without_flipping_status(self, tmp_state_db, monkeypatch):
        self._seed_coding_unit()
        worker = _RecordingAsyncWorker(
            "coder",
            session_id="sesn-coder",
            wait_raises=TimeoutError("did not idle"),
        )
        _install_worker_factory(monkeypatch, worker)

        result_json = execution.wait_unit("F-016-U-1", "coder", timeout_s=1)
        result = json.loads(result_json)

        assert result["status"] == "still_running"
        assert result["reason"] == "timeout"
        # Status unchanged — caller decides retry / escalate / handoff
        row = state.get_unit_state("F-016-U-1")
        assert row is not None
        assert row.status == "coding"

    def test_no_marker_in_response_returns_still_running_no_marker(self, tmp_state_db, monkeypatch):
        """When the worker idled but didn't emit a recognised marker,
        ``wait_unit`` must NOT flip the unit to escalated — that's a
        cycle_review concern. Phase-1 wait is the read-side: report
        what was observed and let the caller decide.
        """
        self._seed_coding_unit()
        worker = _RecordingAsyncWorker(
            "coder",
            session_id="sesn-coder",
            wait_response="just some chatter, no marker",
        )
        _install_worker_factory(monkeypatch, worker)

        result_json = execution.wait_unit("F-016-U-1", "coder", timeout_s=60)
        result = json.loads(result_json)

        assert result["status"] == "still_running"
        assert result["reason"] == "no_marker"
        assert result.get("marker") in (None, "")
        row = state.get_unit_state("F-016-U-1")
        assert row is not None
        assert row.status == "coding"

    def test_unknown_role_returns_error(self, tmp_state_db):
        self._seed_coding_unit()
        msg = execution.wait_unit("F-016-U-1", "tooling", timeout_s=10)
        assert "ERROR" in msg

    def test_unknown_unit_returns_error(self, tmp_state_db):
        msg = execution.wait_unit("F-NOPE-U-1", "coder", timeout_s=10)
        assert "ERROR" in msg

    def test_missing_session_returns_error(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-1", feature_id="F-016", status="coding", branch="async-u-1"
            )
        )

        msg = execution.wait_unit("F-016-U-1", "coder", timeout_s=10)
        assert "ERROR" in msg
        assert "no coder session" in msg


# --------------------------- worker resume_async ---------------------------


class TestManagedAgentResumeAsync:
    """``ManagedAgentWorker.resume_async`` mirrors ``spawn_async``'s send
    half: submits the user.message event, returns ``None``. Does NOT
    open an event stream or wait for the response.
    """

    def test_resume_async_sends_user_message_without_streaming(self, monkeypatch):

        from orchestrator.workers.managed_agent import ManagedAgentWorker

        sent: list[dict] = []

        class _FakeEvents:
            def send(self, session_id: str, *, events: list[dict]) -> None:
                sent.append({"session_id": session_id, "events": events})

            def stream(self, _session_id: str):
                raise AssertionError("resume_async must not open an event stream")

        class _FakeSessions:
            events = _FakeEvents()

        class _FakeBeta:
            sessions = _FakeSessions()

        class _FakeAnthropic:
            beta = _FakeBeta()

        monkeypatch.setattr(
            "orchestrator.workers.managed_agent.Anthropic",
            lambda *a, **kw: _FakeAnthropic(),
        )

        worker = ManagedAgentWorker(role="coder")
        out = worker.resume_async("sess_abc", "ping")

        # No return value — submit-only.
        assert out is None
        assert len(sent) == 1
        assert sent[0]["session_id"] == "sess_abc"
        assert sent[0]["events"][0]["type"] == "user.message"
        assert sent[0]["events"][0]["content"][0]["text"] == "ping"


# --------------------------- docker backend stubs ---------------------------


class TestDockerAsyncRaisesNotImplemented:
    """The docker backend is synchronous (subprocess-based ``docker run``).
    Until F-016 follow-up wiring lands, the async primitives MUST raise
    a clear ``NotImplementedError`` instead of silently blocking — that
    way a lead configured for docker who calls ``spawn_unit_async`` gets
    an actionable error rather than a 30-minute hang.
    """

    def test_spawn_async_raises(self):
        from orchestrator.workers.docker_claude_code import DockerClaudeCodeWorker

        worker = DockerClaudeCodeWorker(role="coder")
        with pytest.raises(NotImplementedError, match="docker"):
            worker.spawn_async("hello")

    def test_wait_idle_raises(self):
        from orchestrator.workers.docker_claude_code import DockerClaudeCodeWorker

        worker = DockerClaudeCodeWorker(role="coder")
        with pytest.raises(NotImplementedError, match="docker"):
            worker.wait_idle("sess_abc")

    def test_resume_async_raises(self):
        from orchestrator.workers.docker_claude_code import DockerClaudeCodeWorker

        worker = DockerClaudeCodeWorker(role="coder")
        with pytest.raises(NotImplementedError, match="docker"):
            worker.resume_async("sess_abc", "hi")
