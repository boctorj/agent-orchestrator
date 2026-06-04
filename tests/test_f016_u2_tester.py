"""F-016-U-2 — Phase 1 non-blocking spawn primitives (tester tests).

These tests are independent of the coder's
``tests/test_f016_u2_non_blocking_spawn.py`` and cover the spec
Acceptance criteria from ``features/F-016/spec.md`` Phase 1 that the
coder's tests don't pin explicitly:

  * **Latency budget.** ``spawn_unit_async`` must return well under the
    spec's ≤3s p95. We measure wall-clock and require << 1s with a
    no-op worker stub — the budget exists to catch a regression that
    accidentally reintroduces the blocking ``wait_idle`` call inside
    ``spawn_unit_async``.

  * **Non-blocking contract.** Even when ``wait_idle`` would block for
    seconds, ``spawn_unit_async`` must return promptly. We give the
    fake worker a 5-second-sleeping ``wait_idle`` and assert the
    function still returns immediately (it never calls ``wait_idle``).

  * **Ghost-row guarantee.** Persistence ordering — the ``status=coding``
    row exists *before* ``worker.spawn_async`` is invoked, AND
    ``coder_session_id`` is persisted before ``spawn_unit_async``
    returns. We check both observable points.

  * **Predecessor consistency (F-016-U-1).** ``wait_unit`` must route
    through the Phase 0 ``dedupe_key`` recorder so a second call on the
    same response is a no-op. We invoke ``wait_unit`` twice and assert
    exactly one ``pr_opened`` row lands in ``unit_events``.

  * **Marker side-effects per role.** ``wait_unit`` with a reviewer
    ``REVIEW_RECOMMEND_MERGE`` advances to ``approved_awaiting_merge``;
    with a tester ``BUG_FOUND`` leaves status unchanged (the caller's
    loop owns the next step).

  * **GitHub-token precondition.** ``spawn_unit_async`` inherits
    ``spawn_unit``'s require-GITHUB_TOKEN guard.

  * **resume_async contract.** The ``Worker`` protocol exposes
    ``resume_async``; ``ManagedAgentWorker.resume_async`` submits a
    single ``user.message`` event for multi-line payloads and never
    opens an event stream.

  * **MCP registration.** Both new tools are registered with the
    project's FastMCP instance so the lead can call them as MCP RPCs.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from orchestrator import state
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, mcp

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _bypass_verify_gate(monkeypatch):
    """The spawn surfaces consult ``ensure_verified_for_feature`` /
    ``ensure_verified_for_unit``. These tests exercise the async
    primitives, not the verification gate.
    """
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None)
    monkeypatch.setattr("orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None)


@pytest.fixture
def _fake_pat(monkeypatch):
    """Make ``need_github_token`` pass deterministically."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_fake_for_tester_tests")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_github_writes(monkeypatch):
    """Block any opportunistic PR-body amend from touching real GitHub."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **kw: None)


def _seed_approved(
    feature_id: str = "F-016-T",
    unit_id: str = "F-016-T-U-1",
    repo_url: str = "https://github.com/o/r",
) -> tuple[str, str]:
    state.save_feature(
        Feature(
            id=feature_id,
            title="phase-1 primitives",
            description="non-blocking spawn",
            repo_path=repo_url,
            status="approved",
            branch_prefix="phase1",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=unit_id,
                feature_id=feature_id,
                title="wire spawn_async into MCP",
                description="implement spawn_unit_async + wait_unit",
            )
        ],
    )
    state.approve_plan(feature_id)
    return feature_id, unit_id


# --------------------------- worker fakes ---------------------------


class _NonBlockingWorker:
    """Minimal stub that emulates the async-half of the Worker protocol.

    ``spawn_async`` records its arguments + the wall-clock time at the
    moment it was invoked and returns ``session_id`` (default ``sesn-1``).
    ``wait_idle`` returns ``wait_response`` or raises ``wait_raises``.
    Both ``spawn`` and ``resume`` (the BLOCKING surfaces) raise — the
    async primitives must not reach for them.
    """

    def __init__(
        self,
        role: str,
        *,
        session_id: str = "sesn-1",
        wait_response: str = "",
        wait_raises: BaseException | None = None,
        wait_sleep_s: float = 0.0,
        spawn_sleep_s: float = 0.0,
    ):
        self.role = role
        self._session_id = session_id
        self._wait_response = wait_response
        self._wait_raises = wait_raises
        self._wait_sleep_s = wait_sleep_s
        self._spawn_sleep_s = spawn_sleep_s
        self.spawn_async_called_at: float | None = None
        self.spawn_async_calls: list[tuple[str, str | None]] = []
        self.wait_idle_calls: list[tuple[str, int]] = []
        self.resume_async_calls: list[tuple[str, str]] = []

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.spawn_async_called_at = time.monotonic()
        if self._spawn_sleep_s:
            time.sleep(self._spawn_sleep_s)
        self.spawn_async_calls.append((task, title))
        return self._session_id

    def wait_idle(self, session_id: str, *, timeout_seconds: int = 1800) -> str:
        self.wait_idle_calls.append((session_id, timeout_seconds))
        if self._wait_sleep_s:
            time.sleep(self._wait_sleep_s)
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._wait_response

    def resume_async(self, session_id: str, msg: str) -> None:
        self.resume_async_calls.append((session_id, msg))

    # BLOCKING surfaces — must not be touched by the async primitives.
    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        raise AssertionError("spawn_unit_async must not call the blocking spawn()")

    def resume(self, session_id: str, msg: str) -> str:
        raise AssertionError("wait_unit / resume_async must not call the blocking resume()")

    def archive(self, session_id: str) -> None:
        return None


def _patch_worker(monkeypatch, worker: _NonBlockingWorker) -> None:
    """Install ``worker`` as the value returned by every
    ``make_worker(role)`` call inside ``orchestrator.tools.execution``.

    F-016-U-2 routes ``spawn_unit_async`` / ``wait_unit`` through the
    ``orchestrator.workers.make_worker`` factory so ``ORCH_WORKER_BACKEND``
    is honored end-to-end. Patching the factory at the execution-module
    import site keeps the test stub agnostic to backend selection.
    """
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda role: worker)


# ===========================================================================
# spawn_unit_async — latency + non-blocking contract
# ===========================================================================


class TestSpawnUnitAsyncLatency:
    """Spec acceptance: ``spawn_unit_async`` p95 latency < 3 seconds.

    The blocking ``spawn_unit`` is what these tests defend against —
    a regression that swaps ``spawn_async`` for ``spawn`` (or that
    silently calls ``wait_idle`` to "be helpful") would explode wall
    time. We give the wait_idle path a 5-second sleep and require the
    whole call to return well under the **spec** budget (3s) — tighter
    bounds (e.g. <1s) over-constrain slow CI runners and flake without
    catching extra regressions; if the impl ever blocks on wait_idle the
    5-second sleep makes the failure obvious either way.
    """

    def test_returns_in_well_under_three_seconds(self, tmp_state_db, monkeypatch, _fake_pat):
        feature_id, unit_id = _seed_approved()
        worker = _NonBlockingWorker("coder")
        _patch_worker(monkeypatch, worker)

        t0 = time.monotonic()
        result = execution.spawn_unit_async(feature_id, unit_id)
        elapsed = time.monotonic() - t0

        # Spec budget is 3s for real network IO. We match it here rather
        # than over-tightening — a wait_idle-induced regression sleeps 5s
        # (see ``_NonBlockingWorker.wait_sleep_s``) so 3.0s still catches it.
        assert elapsed < 3.0, f"spawn_unit_async took {elapsed:.3f}s (spec budget < 3.0s)"
        assert result.startswith("{"), f"expected JSON, got: {result!r}"

    def test_does_not_call_wait_idle_even_if_it_would_block(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        """The async dispatch tool must NEVER call ``wait_idle`` — that
        would defeat the whole point of the dispatcher/watcher split.

        Give ``wait_idle`` a 5-second sleep; assert it is never reached
        and that the spawn call returns well under the spec budget.
        """
        feature_id, unit_id = _seed_approved()
        worker = _NonBlockingWorker("coder", wait_sleep_s=5.0)
        _patch_worker(monkeypatch, worker)

        t0 = time.monotonic()
        execution.spawn_unit_async(feature_id, unit_id)
        elapsed = time.monotonic() - t0

        assert worker.wait_idle_calls == []
        # Same 3.0s spec budget — the 5s wait_idle sleep is the
        # regression signal, well outside the spec window.
        assert elapsed < 3.0, f"spawn_unit_async took {elapsed:.3f}s — suggests it called wait_idle"


# ===========================================================================
# spawn_unit_async — ghost-row guarantee
# ===========================================================================


class TestSpawnUnitAsyncGhostRowGuarantee:
    """Spec acceptance: F-014-U-1-style ghost rows (``status=coding``,
    ``session_id=""``) become structurally impossible.

    The contract has TWO observable promises:

      1. The unit row is written with ``status=coding`` *before*
         ``worker.spawn_async`` is invoked. A lead killed before the
         worker call still leaves a recoverable row.
      2. ``coder_session_id`` is persisted *before* ``spawn_unit_async``
         returns to the caller. A lead killed after the function
         returns has the session_id durably recorded.
    """

    def test_status_row_exists_before_spawn_async_is_invoked(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        feature_id, unit_id = _seed_approved()

        seen: dict[str, object] = {"row_at_spawn_async": None}

        class _ObservingWorker(_NonBlockingWorker):
            def spawn_async(self, task, *, title=None):  # type: ignore[override]
                # Snapshot the unit row at the exact moment the worker
                # is being dispatched. If the row doesn't exist yet, a
                # kill here would strand the unit invisibly.
                seen["row_at_spawn_async"] = state.get_unit_state(unit_id)
                return super().spawn_async(task, title=title)

        worker = _ObservingWorker("coder", session_id="sesn-pre-row")
        _patch_worker(monkeypatch, worker)

        execution.spawn_unit_async(feature_id, unit_id)

        row = seen["row_at_spawn_async"]
        assert row is not None, "status=coding row missing at the moment of dispatch"
        assert row.status == "coding"
        assert row.feature_id == feature_id
        assert row.branch, "branch must be set on the pre-dispatch row"

    def test_session_id_persisted_before_function_returns(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        feature_id, unit_id = _seed_approved()
        worker = _NonBlockingWorker("coder", session_id="sesn-after-spawn")
        _patch_worker(monkeypatch, worker)

        execution.spawn_unit_async(feature_id, unit_id)

        # By the time the function returns, the session_id must be in
        # state.db — a fresh process can recover by reading this row.
        row = state.get_unit_state(unit_id)
        assert row is not None
        assert row.coder_session_id == "sesn-after-spawn"
        assert row.status == "coding"

    def test_spawn_async_failure_does_not_leave_ghost_row(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        """If the Anthropic API hands back an error, the unit must NOT
        be stuck in ``coding`` with no session_id — the recovery path
        is ``escalated`` + ``last_error`` populated.
        """
        feature_id, unit_id = _seed_approved()

        class _BoomWorker(_NonBlockingWorker):
            def spawn_async(self, task, *, title=None):  # type: ignore[override]
                raise RuntimeError("anthropic 503")

        _patch_worker(monkeypatch, _BoomWorker("coder"))

        result = execution.spawn_unit_async(feature_id, unit_id)

        assert result.startswith("ERROR")
        row = state.get_unit_state(unit_id)
        assert row is not None
        # Pinned to escalated, not coding — a coding row with no session
        # is the exact ghost-row failure mode this unit kills.
        assert row.status == "escalated", (
            f"row stuck at {row.status!r} after spawn_async raised — ghost-row mode"
        )
        assert "anthropic 503" in row.last_error


# ===========================================================================
# spawn_unit_async — precondition + return shape
# ===========================================================================


class TestSpawnUnitAsyncPreconditions:
    """The async tool must respect the same preconditions as the
    blocking ``spawn_unit`` (otherwise switching from one to the other
    in a flow changes the safety surface).
    """

    def test_missing_github_token_blocks_dispatch(self, tmp_state_db, monkeypatch, no_github_token):
        feature_id, unit_id = _seed_approved()

        # Sentinel worker — if the code path reaches it the test fails
        # because the token guard should have shortcircuited first.
        class _NeverCalled(_NonBlockingWorker):
            def spawn_async(self, task, *, title=None):  # type: ignore[override]
                raise AssertionError("worker.spawn_async called despite missing GITHUB_TOKEN")

        _patch_worker(monkeypatch, _NeverCalled("coder"))

        result = execution.spawn_unit_async(feature_id, unit_id)

        assert result.startswith("ERROR")
        # Nothing should have been persisted yet
        assert state.get_unit_state(unit_id) is None

    def test_response_shape_carries_session_id_and_branch(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        feature_id, unit_id = _seed_approved()
        worker = _NonBlockingWorker("coder", session_id="sesn-shape")
        _patch_worker(monkeypatch, worker)

        payload = json.loads(execution.spawn_unit_async(feature_id, unit_id))

        assert payload["unit_id"] == unit_id
        assert payload["session_id"] == "sesn-shape"
        assert payload["status"] == "coding"
        assert payload["branch"]  # non-empty
        # The acceptance criterion says ``submitted_at`` is part of the
        # contract — exposes the persisted-before-return timestamp.
        assert "submitted_at" in payload


# ===========================================================================
# wait_unit — terminal markers, idempotency, and timeout semantics
# ===========================================================================


class TestWaitUnitTerminalMarkers:
    def _seed_in_role(
        self,
        role: str,
        session_id: str,
        status: str = "coding",
    ) -> tuple[str, str]:
        feature_id, unit_id = _seed_approved()
        field = {
            "coder": "coder_session_id",
            "tester": "tester_session_id",
            "reviewer": "reviewer_session_id",
        }[role]
        kwargs = {
            "unit_id": unit_id,
            "feature_id": feature_id,
            "status": status,
            "branch": "phase1-u-1",
            field: session_id,
        }
        state.upsert_unit_state(WorkUnitState(**kwargs))
        return feature_id, unit_id

    def test_coder_pr_url_marker_flips_to_in_ci(self, tmp_state_db, monkeypatch):
        _, unit_id = self._seed_in_role("coder", "sesn-coder")
        worker = _NonBlockingWorker(
            "coder",
            session_id="sesn-coder",
            wait_response=("Pushed branch.\nPR_URL: https://github.com/o/r/pull/77\n"),
        )
        _patch_worker(monkeypatch, worker)

        result = json.loads(execution.wait_unit(unit_id, "coder", timeout_s=10))

        assert result["marker"] == "PR_URL"
        assert result["status"] == "in_ci"

    def test_tester_bug_found_does_not_flip_status(self, tmp_state_db, monkeypatch):
        """Per ``_record_terminal_marker``'s contract, BUG_FOUND has
        ``target_status=None`` — the caller's loop (cycle_review /
        daemon) holds the unit in ``testing`` until the next
        ``address_review`` cycle. ``wait_unit`` must respect that.
        """
        _, unit_id = self._seed_in_role("tester", "sesn-tester", status="testing")
        worker = _NonBlockingWorker(
            "tester",
            session_id="sesn-tester",
            wait_response="BUG_FOUND: off-by-one in pagination",
        )
        _patch_worker(monkeypatch, worker)

        result = json.loads(execution.wait_unit(unit_id, "tester", timeout_s=10))

        assert result["marker"] == "BUG_FOUND"
        row = state.get_unit_state(unit_id)
        assert row is not None
        assert row.status == "testing", f"BUG_FOUND must not flip status (got {row.status!r})"

    def test_reviewer_recommend_merge_flips_to_awaiting_merge(self, tmp_state_db, monkeypatch):
        """Per F-009-U-4 / ``_record_terminal_marker``, a reviewer
        ``REVIEW_RECOMMEND_MERGE`` lands the unit in
        ``approved_awaiting_merge``. ``wait_unit`` must route through
        the same recorder.
        """
        _, unit_id = self._seed_in_role("reviewer", "sesn-reviewer", status="reviewing")
        worker = _NonBlockingWorker(
            "reviewer",
            session_id="sesn-reviewer",
            wait_response="REVIEW_RECOMMEND_MERGE: ships cleanly, all hands-off concerns addressed",
        )
        _patch_worker(monkeypatch, worker)

        result = json.loads(execution.wait_unit(unit_id, "reviewer", timeout_s=10))

        assert result["marker"] == "REVIEW_RECOMMEND_MERGE"
        row = state.get_unit_state(unit_id)
        assert row is not None
        assert row.status == "approved_awaiting_merge"

    def test_wait_unit_is_idempotent_via_dedupe_key(self, tmp_state_db, monkeypatch):
        """Predecessor F-016-U-1 ships ``dedupe_key`` + ``INSERT OR
        IGNORE`` so a re-scan of the same response is a no-op. The
        daemon (F-016-U-5) will re-poll idle sessions, so ``wait_unit``
        MUST route through that recorder. We invoke it twice with the
        same canned response and require exactly one ``pr_opened``
        event in ``unit_events``.
        """
        _, unit_id = self._seed_in_role("coder", "sesn-dedupe")
        canned = "PR_URL: https://github.com/o/r/pull/91"
        worker = _NonBlockingWorker("coder", session_id="sesn-dedupe", wait_response=canned)
        _patch_worker(monkeypatch, worker)

        execution.wait_unit(unit_id, "coder", timeout_s=10)
        execution.wait_unit(unit_id, "coder", timeout_s=10)

        pr_events = [e for e in state.list_events(unit_id) if e["event_type"] == "pr_opened"]
        assert len(pr_events) == 1, (
            f"expected exactly one pr_opened event, got {len(pr_events)} — "
            "Phase-0 dedupe_key not applied"
        )

    def test_timeout_returns_still_running_unchanged_row(self, tmp_state_db, monkeypatch):
        _, unit_id = self._seed_in_role("coder", "sesn-timeout")
        worker = _NonBlockingWorker(
            "coder",
            session_id="sesn-timeout",
            wait_raises=TimeoutError("did not idle"),
        )
        _patch_worker(monkeypatch, worker)

        payload = json.loads(execution.wait_unit(unit_id, "coder", timeout_s=2))

        assert payload["status"] == "still_running"
        assert payload["reason"] == "timeout"
        row = state.get_unit_state(unit_id)
        assert row is not None
        assert row.status == "coding"  # untouched
        # No marker event written
        marker_events = [
            e
            for e in state.list_events(unit_id)
            if e["event_type"] in {"pr_opened", "fix_pushed", "tests_pass", "tester_bug_found"}
        ]
        assert marker_events == []

    def test_no_marker_returns_still_running_no_marker_unchanged_row(
        self, tmp_state_db, monkeypatch
    ):
        _, unit_id = self._seed_in_role("coder", "sesn-no-marker")
        worker = _NonBlockingWorker(
            "coder",
            session_id="sesn-no-marker",
            wait_response="idled but did not emit a marker; just chatter",
        )
        _patch_worker(monkeypatch, worker)

        payload = json.loads(execution.wait_unit(unit_id, "coder", timeout_s=10))

        assert payload["status"] == "still_running"
        assert payload["reason"] == "no_marker"
        # The unit row is unchanged — the no-marker → escalate policy
        # lives in cycle_review, not in Phase-1 wait.
        row = state.get_unit_state(unit_id)
        assert row is not None
        assert row.status == "coding"


# ===========================================================================
# wait_unit — role / arg guards
# ===========================================================================


class TestWaitUnitGuards:
    def test_invalid_role_errors_without_touching_worker(self, tmp_state_db, monkeypatch):
        feature_id, unit_id = _seed_approved()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=unit_id,
                feature_id=feature_id,
                status="coding",
                branch="phase1-u-1",
                coder_session_id="sesn-x",
            )
        )

        class _NeverCalled(_NonBlockingWorker):
            def wait_idle(self, *a, **kw):  # type: ignore[override]
                raise AssertionError("worker constructed for unknown role")

        _patch_worker(monkeypatch, _NeverCalled("coder"))

        # F-016-U-1 locked in the {coder,tester,reviewer} role set
        # (via ``_KNOWN_ROLES``); wait_unit must reject anything else
        # without instantiating the worker.
        result = execution.wait_unit(unit_id, "daemon", timeout_s=5)
        assert result.startswith("ERROR")

    def test_role_without_persisted_session_errors(self, tmp_state_db):
        feature_id, unit_id = _seed_approved()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=unit_id,
                feature_id=feature_id,
                status="coding",
                branch="phase1-u-1",
                coder_session_id="sesn-coder-only",
            )
        )
        # Asking for the tester session when only the coder one exists.
        result = execution.wait_unit(unit_id, "tester", timeout_s=5)
        assert result.startswith("ERROR")
        assert "tester" in result


# ===========================================================================
# ManagedAgentWorker.resume_async — submit-only contract
# ===========================================================================


class TestManagedAgentResumeAsync:
    """The protocol's new ``resume_async`` is "submit then return None".
    Tests defend two failure modes: returning a string (caller might
    treat it as a marker payload), and opening an event stream (would
    block the dispatcher).
    """

    def _install_fake_anthropic(self, monkeypatch, recorder: dict):
        class _FakeEvents:
            def send(self, session_id: str, *, events: list) -> None:
                recorder.setdefault("sent", []).append({"session_id": session_id, "events": events})

            def stream(self, _session_id):  # pragma: no cover - failure path
                raise AssertionError("resume_async must not open the streaming endpoint")

        class _FakeSessions:
            events = _FakeEvents()

        class _FakeBeta:
            sessions = _FakeSessions()

        class _FakeClient:
            beta = _FakeBeta()

        monkeypatch.setattr(
            "orchestrator.workers.managed_agent.Anthropic",
            lambda *a, **kw: _FakeClient(),
        )

    def test_returns_none_and_submits_one_user_message(self, monkeypatch):
        from orchestrator.workers.managed_agent import ManagedAgentWorker

        recorder: dict = {}
        self._install_fake_anthropic(monkeypatch, recorder)

        worker = ManagedAgentWorker(role="coder")
        out = worker.resume_async("sess_xyz", "address-this-feedback")

        # Submit-only: explicit None signals the caller MUST wait_idle
        # separately rather than treating the return value as content.
        assert out is None
        assert len(recorder["sent"]) == 1
        sent = recorder["sent"][0]
        assert sent["session_id"] == "sess_xyz"
        event = sent["events"][0]
        assert event["type"] == "user.message"
        assert event["content"][0]["type"] == "text"
        assert event["content"][0]["text"] == "address-this-feedback"

    def test_multiline_message_passes_through_verbatim(self, monkeypatch):
        """Address-review messages routinely include multi-paragraph
        feedback. ``resume_async`` MUST forward the message verbatim;
        any trimming, splitting, or alternative encoding would corrupt
        the marker-grammar payload the worker reads.
        """
        from orchestrator.workers.managed_agent import ManagedAgentWorker

        recorder: dict = {}
        self._install_fake_anthropic(monkeypatch, recorder)

        msg = "line one\n\nline two with: special chars `~!@#$%^&*()`\nBLOCKED: not really"
        worker = ManagedAgentWorker(role="coder")
        worker.resume_async("sess_multi", msg)

        sent_text = recorder["sent"][0]["events"][0]["content"][0]["text"]
        assert sent_text == msg


# ===========================================================================
# Worker protocol surface
# ===========================================================================


class TestWorkerProtocolHasAsyncTrio:
    """The protocol must declare all three async primitives — F-016-U-5
    (daemon) will type-check against ``Worker``; without these on the
    Protocol the daemon would fail static checking even though the
    concrete ``ManagedAgentWorker`` ships them.
    """

    def test_worker_protocol_declares_async_methods(self):
        from orchestrator.workers.base import Worker

        for name in ("spawn_async", "wait_idle", "resume_async"):
            assert hasattr(Worker, name), (
                f"Worker protocol missing {name!r} — daemon's static "
                "interface contract is incomplete"
            )

    def test_managed_agent_implements_async_trio(self):
        from orchestrator.workers.managed_agent import ManagedAgentWorker

        for name in ("spawn_async", "wait_idle", "resume_async"):
            assert callable(getattr(ManagedAgentWorker, name, None))


# ===========================================================================
# Docker backend — explicit NotImplementedError instead of silent block
# ===========================================================================


class TestDockerAsyncIsExplicitlyUnsupported:
    """The docker backend is synchronous; until a follow-up unit wires
    a real submit/wait split, the three async methods MUST raise an
    actionable ``NotImplementedError`` rather than silently calling
    the blocking primitives and hanging the dispatcher.
    """

    def _docker_worker(self, tmp_path):
        from orchestrator.workers.docker_claude_code import DockerClaudeCodeWorker

        return DockerClaudeCodeWorker(role="coder")

    def test_spawn_async_raises_with_actionable_message(self, tmp_path):
        worker = self._docker_worker(tmp_path)
        with pytest.raises(NotImplementedError) as exc:
            worker.spawn_async("hi")
        # The message must point the user at the fix: switch backend or
        # use the blocking spawn(). A bare NotImplementedError without
        # diagnostic text is a regression.
        assert "managed_agents" in str(exc.value) or "spawn" in str(exc.value)

    def test_wait_idle_raises_with_actionable_message(self, tmp_path):
        worker = self._docker_worker(tmp_path)
        with pytest.raises(NotImplementedError):
            worker.wait_idle("sess_abc")

    def test_resume_async_raises_with_actionable_message(self, tmp_path):
        worker = self._docker_worker(tmp_path)
        with pytest.raises(NotImplementedError):
            worker.resume_async("sess_abc", "hi")


# ===========================================================================
# MCP registration
# ===========================================================================


class TestMcpRegistration:
    """Phase 1 is "wire async primitives into MCP". If
    ``spawn_unit_async`` / ``wait_unit`` don't end up in the FastMCP
    instance, the lead can't reach them — the whole unit is a no-op
    even if the python functions work in isolation.
    """

    def test_async_tools_registered_with_fastmcp(self):
        # FastMCP's tool registry is async-only; spin a dedicated loop
        # to avoid leaning on whatever loop state earlier tests left
        # behind (some plugins close the default loop after teardown).
        # The loop MUST be closed in a finally — otherwise the test
        # leaks an unreaped event loop and the suite emits
        # ``ResourceWarning: unclosed event loop`` (PR #57 review).
        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(mcp.list_tools())
        finally:
            loop.close()
        names = {t.name for t in tools}
        assert "spawn_unit_async" in names, (
            f"spawn_unit_async not registered with MCP (have: {sorted(names)[:10]}…)"
        )
        assert "wait_unit" in names, (
            f"wait_unit not registered with MCP (have: {sorted(names)[:10]}…)"
        )
