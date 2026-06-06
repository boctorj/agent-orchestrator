"""Tests for orchestrator/daemon.py — the F-016 Phase 3 watcher daemon.

The daemon is a level-triggered reconciler that calls F-014's pure
:func:`~orchestrator.health.decide_transitions` engine each tick and
executes the resulting actions idempotently. These tests exercise the
seams:

  * Env-gate behaviour — ``ORCH_DAEMON_DRIVE`` opt-in.
  * Per-unit reconcile flow — marker scan → record → status flip;
    F-014 probe → decide → apply.
  * Short-circuit guards — ``cancelled_at`` / ``has_active_advance_lock``
    / terminal status.
  * Singleton — ``claim_singleton`` returns ``None`` on contention.
  * Crash recovery — stale heartbeat → takeover.

A :class:`_FakeWorker` lets us drive ``tail_messages`` deterministically;
production never reaches Anthropic in these tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from orchestrator import daemon, markers, state
from orchestrator.models import Feature, WorkUnitState

# --------------------------- shared fakes ---------------------------


@dataclass
class _FakeTailResult:
    status: str = "idle"
    messages: list[dict] = field(default_factory=list)
    reason: str | None = None

    def __getitem__(self, key: str):  # mimic TypedDict access
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


@dataclass
class _FakeWorker:
    """One per role, returns canned ``tail_messages`` per session id."""

    role: str = "coder"
    canned: dict[str, _FakeTailResult] = field(default_factory=dict)

    def tail_messages(self, session_id: str, *, limit: int = 50):  # noqa: ARG002
        return self.canned.get(session_id, _FakeTailResult())


# --------------------------- seeding helpers ---------------------------


def _seed(
    *,
    unit_id: str = "U-1",
    feature_id: str = "F-D",
    status: str = "coding",
    repo: str = "https://github.com/o/r",
    sessions: dict[str, str] | None = None,
    pr_number: int | None = None,
    review_round: int = 0,
) -> WorkUnitState:
    state.save_feature(Feature(id=feature_id, title="t", description="d", repo_path=repo))
    s = sessions or {}
    unit = WorkUnitState(
        unit_id=unit_id,
        feature_id=feature_id,
        status=status,
        coder_session_id=s.get("coder", ""),
        tester_session_id=s.get("tester", ""),
        reviewer_session_id=s.get("reviewer", ""),
        review_round=review_round,
        pr_number=pr_number,
    )
    state.upsert_unit_state(unit)
    return unit


def _install_fake_worker(monkeypatch, worker: _FakeWorker) -> None:
    """Make ``make_worker(role)`` return ``worker`` regardless of role."""
    monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)


def _stub_no_probe(monkeypatch) -> None:
    """No-op the F-014 probe so marker-only tests don't need GH fixtures."""
    monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _unit: [])


# --------------------------- env gate ---------------------------


class TestDriveEnv:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        assert daemon.is_drive_enabled() is False

    @pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on", "ON"])
    def test_truthy_values(self, monkeypatch, val):
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, val)
        assert daemon.is_drive_enabled() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "maybe", "True "])
    def test_falsy_values(self, monkeypatch, val):
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, val)
        # "True " strips to "true" → True. Adjust expectation.
        expected = val.strip().lower() in ("1", "true", "yes", "on")
        assert daemon.is_drive_enabled() is expected


class TestPollInterval:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(daemon.POLL_INTERVAL_ENV, raising=False)
        assert daemon._poll_interval_s() == daemon.POLL_INTERVAL_DEFAULT_S

    def test_parses_override(self, monkeypatch):
        monkeypatch.setenv(daemon.POLL_INTERVAL_ENV, "1.5")
        assert daemon._poll_interval_s() == 1.5

    def test_floors_to_min(self, monkeypatch):
        monkeypatch.setenv(daemon.POLL_INTERVAL_ENV, "0")
        assert daemon._poll_interval_s() == 0.1

    def test_unparseable_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(daemon.POLL_INTERVAL_ENV, "not-a-number")
        assert daemon._poll_interval_s() == daemon.POLL_INTERVAL_DEFAULT_S


# --------------------------- marker scan + record ---------------------------


class TestScanAndRecord:
    def test_no_session_id_returns_none(self, tmp_state_db, monkeypatch):
        unit = _seed(sessions={})  # no role-session ids
        _install_fake_worker(monkeypatch, _FakeWorker())
        spec, sid = daemon._scan_role(unit, "coder")
        assert spec is None
        assert sid == ""

    def test_tail_exception_swallowed(self, tmp_state_db, monkeypatch):
        unit = _seed(sessions={"coder": "sess_c"})

        class _Boom:
            def tail_messages(self, *_a, **_k):
                raise RuntimeError("backend down")

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _Boom())
        spec, sid = daemon._scan_role(unit, "coder")
        assert spec is None
        assert sid == "sess_c"

    def test_pr_url_marker_parsed_and_recorded(self, tmp_state_db, monkeypatch):
        unit = _seed(sessions={"coder": "sess_c"})
        worker = _FakeWorker(
            canned={
                "sess_c": _FakeTailResult(
                    status="idle",
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": "PR_URL: https://github.com/o/r/pull/42",
                        }
                    ],
                )
            }
        )
        _install_fake_worker(monkeypatch, worker)
        spec, sid = daemon._scan_role(unit, "coder")
        assert spec is not None
        assert spec.marker == "PR_URL"
        assert daemon._record_marker(unit, spec, sid) is True
        # Second call dedupes.
        assert daemon._record_marker(unit, spec, sid) is False
        events = state.list_events(unit.unit_id)
        assert sum(1 for e in events if e["event_type"] == "pr_opened") == 1


# --------------------------- marker transition ---------------------------


class TestApplyMarkerTransition:
    def test_pr_url_flips_coding_to_in_ci(self, tmp_state_db):
        unit = _seed(status="coding")
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/7")
        assert spec is not None
        assert daemon._apply_marker_transition(unit.unit_id, spec) is True
        assert state.get_unit_state(unit.unit_id).status == "in_ci"

    def test_no_target_status_no_op(self, tmp_state_db):
        """BUG_FOUND / REVIEW_REQUEST_CHANGES carry ``target_status=None``."""
        unit = _seed(status="testing")
        spec = markers.scan_response("tester", "BUG_FOUND: divide by zero")
        assert spec is not None and spec.target_status is None
        assert daemon._apply_marker_transition(unit.unit_id, spec) is False
        # Status unchanged.
        assert state.get_unit_state(unit.unit_id).status == "testing"

    def test_skip_if_lead_holds_owner(self, tmp_state_db):
        """Per spec § 2.5: daemon defers when ``owner='lead'``."""
        unit = _seed(status="coding")
        state.claim_unit_owner(unit.unit_id, "lead", expected_owner="")
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/7")
        assert spec is not None
        assert daemon._apply_marker_transition(unit.unit_id, spec) is False
        # Status preserved; lead still owns.
        assert state.get_unit_state(unit.unit_id).status == "coding"
        assert state.get_unit_state(unit.unit_id).owner == "lead"

    def test_skip_if_already_flipped(self, tmp_state_db):
        """A re-tick on the same marker after the flip is a no-op."""
        unit = _seed(status="in_ci")  # already past coding
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/7")
        assert spec is not None
        assert daemon._apply_marker_transition(unit.unit_id, spec) is False
        assert state.get_unit_state(unit.unit_id).status == "in_ci"

    def test_blocked_sets_last_error_and_status(self, tmp_state_db):
        """BLOCKED carries ``last_error`` and flips status atomically."""
        unit = _seed(status="coding")
        spec = markers.scan_response("coder", "BLOCKED: reason=auth_failure | token rejected")
        assert spec is not None
        assert spec.target_status == "escalated"
        assert spec.last_error
        assert daemon._apply_marker_transition(unit.unit_id, spec) is True
        latest = state.get_unit_state(unit.unit_id)
        assert latest.status == "escalated"
        assert latest.last_error == spec.last_error

    def test_releases_owner_on_success(self, tmp_state_db):
        unit = _seed(status="coding")
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/7")
        assert daemon._apply_marker_transition(unit.unit_id, spec) is True
        # ``owner`` cleared so a follow-up lead send_to_unit can take it.
        assert state.get_unit_state(unit.unit_id).owner == ""

    def test_skip_if_cancelled_inside_cas(self, tmp_state_db, monkeypatch):
        """A unit cancelled between CAS-claim and the re-read must NOT flip."""
        unit = _seed(status="coding")
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/7")

        # Patch get_unit_state inside daemon to return a cancelled snapshot.
        cancelled = WorkUnitState(
            unit_id=unit.unit_id,
            feature_id=unit.feature_id,
            status="coding",
            cancelled_at="2024-01-01T00:00:00",
        )
        monkeypatch.setattr("orchestrator.daemon.state.get_unit_state", lambda _u: cancelled)
        assert daemon._apply_marker_transition(unit.unit_id, spec) is False


# --------------------------- per-unit reconcile ---------------------------


class TestReconcileUnit:
    def test_skips_missing_row(self, tmp_state_db):
        # No unit row — reconcile_unit is a quiet no-op.
        daemon.reconcile_unit("NOT-A-UNIT")  # must not raise

    def test_skips_cancelled(self, tmp_state_db, monkeypatch):
        unit = _seed(status="coding", sessions={"coder": "sess_c"})
        state.cancel_unit(unit.unit_id)
        worker_called = {"n": 0}

        class _Counter:
            def tail_messages(self, *_a, **_k):
                worker_called["n"] += 1
                return _FakeTailResult()

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _Counter())
        _stub_no_probe(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        assert worker_called["n"] == 0
        # Status unchanged.
        assert state.get_unit_state(unit.unit_id).status == "cancelled"

    def test_skips_terminal(self, tmp_state_db, monkeypatch):
        unit = _seed(status="done", sessions={"coder": "sess_c"})
        worker_called = {"n": 0}

        class _Counter:
            def tail_messages(self, *_a, **_k):
                worker_called["n"] += 1
                return _FakeTailResult()

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _Counter())
        _stub_no_probe(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        assert worker_called["n"] == 0

    def test_skips_when_lead_holds_advance_lock(self, tmp_state_db, monkeypatch):
        unit = _seed(status="coding", sessions={"coder": "sess_c"})
        # Simulate lead's CAS claim.
        state.claim_unit_owner(unit.unit_id, state.LEAD_OWNER, expected_owner="")
        try:
            worker_called = {"n": 0}

            class _Counter:
                def tail_messages(self, *_a, **_k):
                    worker_called["n"] += 1
                    return _FakeTailResult()

            monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _Counter())
            _stub_no_probe(monkeypatch)
            daemon.reconcile_unit(unit.unit_id)
            assert worker_called["n"] == 0
        finally:
            state.release_unit_owner(unit.unit_id, expected_owner=state.LEAD_OWNER)

    def test_marker_scan_and_flip(self, tmp_state_db, monkeypatch):
        """End-to-end: tail emits PR_URL → marker recorded + status flips."""
        unit = _seed(status="coding", sessions={"coder": "sess_c"})
        worker = _FakeWorker(
            canned={
                "sess_c": _FakeTailResult(
                    status="idle",
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": "PR_URL: https://github.com/o/r/pull/77",
                        }
                    ],
                )
            }
        )
        _install_fake_worker(monkeypatch, worker)
        _stub_no_probe(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        latest = state.get_unit_state(unit.unit_id)
        assert latest.status == "in_ci"
        events = state.list_events(unit.unit_id)
        assert any(e["event_type"] == "pr_opened" for e in events)

    def test_idempotent_double_tick(self, tmp_state_db, monkeypatch):
        """A second tick on the same response writes no duplicate events."""
        unit = _seed(status="coding", sessions={"coder": "sess_c"})
        worker = _FakeWorker(
            canned={
                "sess_c": _FakeTailResult(
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": "PR_URL: https://github.com/o/r/pull/9",
                        }
                    ]
                )
            }
        )
        _install_fake_worker(monkeypatch, worker)
        _stub_no_probe(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        daemon.reconcile_unit(unit.unit_id)
        events = state.list_events(unit.unit_id)
        # Exactly one pr_opened event despite two ticks.
        assert sum(1 for e in events if e["event_type"] == "pr_opened") == 1

    def test_health_action_applied_via_owner_cas(self, tmp_state_db, monkeypatch):
        """An F-014 transition action lands through ``_apply_health_action``."""
        unit = _seed(status="in_ci", pr_number=7)
        # Stub out the marker scan (no sessions seeded anyway).
        from orchestrator.health import Action

        action = Action.transition("done", "merged via test")
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [action])
        # Skip worker calls entirely.
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _FakeWorker())
        daemon.reconcile_unit(unit.unit_id)
        assert state.get_unit_state(unit.unit_id).status == "done"

    def test_health_action_skipped_when_lead_holds_lock(self, tmp_state_db, monkeypatch):
        unit = _seed(status="in_ci", pr_number=7)
        from orchestrator.health import Action

        action = Action.transition("done", "merged via test")
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [action])
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _FakeWorker())
        state.claim_unit_owner(unit.unit_id, state.LEAD_OWNER, expected_owner="")
        try:
            daemon.reconcile_unit(unit.unit_id)
            # Lead lock pre-empts ``reconcile_unit`` entirely.
            assert state.get_unit_state(unit.unit_id).status == "in_ci"
        finally:
            state.release_unit_owner(unit.unit_id, expected_owner=state.LEAD_OWNER)


# --------------------------- reconcile_once ---------------------------


class TestReconcileOnce:
    def test_counts_active_units(self, tmp_state_db, monkeypatch):
        state.save_feature(Feature(id="F-A", title="t", description="d"))
        for s in ("coding", "testing", "done", "cancelled"):
            state.upsert_unit_state(WorkUnitState(unit_id=f"U-{s}", feature_id="F-A", status=s))
        _stub_no_probe(monkeypatch)
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _FakeWorker())
        # done + cancelled are excluded by list_active_units; only 2 ticks.
        assert daemon.reconcile_once() == 2

    def test_per_unit_exception_logged_continues(self, tmp_state_db, monkeypatch):
        """A single raising reconcile_unit must not crash the loop."""
        state.save_feature(Feature(id="F-A", title="t", description="d"))
        state.upsert_unit_state(WorkUnitState(unit_id="U-1", feature_id="F-A", status="coding"))
        state.upsert_unit_state(WorkUnitState(unit_id="U-2", feature_id="F-A", status="coding"))

        calls: list[str] = []

        def _boom(unit_id: str) -> None:
            calls.append(unit_id)
            if unit_id == "U-1":
                raise RuntimeError("boom")

        monkeypatch.setattr(daemon, "reconcile_unit", _boom)
        # Should not raise; should return 2 (count of attempts).
        assert daemon.reconcile_once() == 2
        assert set(calls) == {"U-1", "U-2"}


# --------------------------- singleton ---------------------------


class TestClaimSingleton:
    def test_claim_returns_handle(self, tmp_state_db):
        handle = daemon.claim_singleton(holder_id="h1")
        assert handle is not None
        assert handle.holder_id == "h1"
        # Lock row visible in state.db.
        assert state.get_daemon_lock(handle.state_db_path)["holder_id"] == "h1"

    def test_claim_returns_none_on_contention(self, tmp_state_db):
        first = daemon.claim_singleton(holder_id="h1")
        assert first is not None
        second = daemon.claim_singleton(holder_id="h2")
        assert second is None

    def test_release_succeeds_and_clears_row(self, tmp_state_db):
        h = daemon.claim_singleton(holder_id="h1")
        assert daemon.release_singleton(h) is True
        assert state.get_daemon_lock(h.state_db_path) is None

    def test_heartbeat_returns_true_while_held(self, tmp_state_db):
        h = daemon.claim_singleton(holder_id="h1")
        assert daemon.heartbeat(h) is True

    def test_heartbeat_returns_false_after_takeover(self, tmp_state_db):
        """Crash-recovery shape: lock taken over → old heartbeat fails."""
        h = daemon.claim_singleton(holder_id="h1")
        # Manually clear the row to simulate the takeover landing.
        state.release_daemon_lock(h.state_db_path, "h1")
        assert daemon.heartbeat(h) is False


# --------------------------- DaemonLoop ---------------------------


class TestDaemonLoopTick:
    def test_tick_returns_reconcile_count(self, tmp_state_db, monkeypatch):
        state.save_feature(Feature(id="F-A", title="t", description="d"))
        state.upsert_unit_state(WorkUnitState(unit_id="U-1", feature_id="F-A", status="coding"))
        _stub_no_probe(monkeypatch)
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _FakeWorker())
        loop = daemon.DaemonLoop()
        loop.handle = daemon.claim_singleton(holder_id="h1")
        assert loop.tick() == 1

    def test_tick_sets_stop_on_lost_heartbeat(self, tmp_state_db, monkeypatch):
        """Lock taken over mid-loop → tick stops the loop, returns 0."""
        loop = daemon.DaemonLoop()
        loop.handle = daemon.claim_singleton(holder_id="h1")
        # Steal the lock so heartbeat fails.
        state.release_daemon_lock(loop.handle.state_db_path, "h1")
        # ``tick`` should detect, set stop, and return 0 without
        # running reconcile_once.
        monkeypatch.setattr(daemon, "reconcile_once", lambda: 999)
        assert loop.tick() == 0
        assert loop.is_stopping()


class TestDaemonLoopRun:
    def test_run_noop_when_drive_disabled(self, tmp_state_db, monkeypatch):
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        loop = daemon.DaemonLoop()
        assert loop.run() == 0
        # Nothing in state.db lock table.
        path = str(state.STATE_DB.resolve())
        assert state.get_daemon_lock(path) is None

    def test_run_noop_when_lock_held(self, tmp_state_db, monkeypatch):
        """Another daemon holds the lock → run returns 0 without driving."""
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        # Pre-seed an unrelated holder.
        path = str(state.STATE_DB.resolve())
        state.claim_daemon_lock(path, "other-holder")
        loop = daemon.DaemonLoop(holder_id="us")
        assert loop.run() == 0
        # Pre-seeded holder still owns.
        assert state.get_daemon_lock(path)["holder_id"] == "other-holder"

    def test_run_drives_until_stop(self, tmp_state_db, monkeypatch):
        """Stop flag set before first wait → loop runs exactly one tick.

        The poll interval is overridden to 0.05s so a missed signal
        couldn't extend the test for more than a heartbeat.
        """
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        monkeypatch.setattr(daemon, "reconcile_once", lambda: 0)
        loop = daemon.DaemonLoop(poll_interval_s=0.05, holder_id="us")
        # Wire the stop after the first tick.
        orig_tick = loop.tick

        def tick_then_stop() -> int:
            result = orig_tick()
            loop.stop()
            return result

        monkeypatch.setattr(loop, "tick", tick_then_stop)
        ticks = loop.run()
        assert ticks == 1
        # Lock released on shutdown.
        path = str(state.STATE_DB.resolve())
        assert state.get_daemon_lock(path) is None


# --------------------------- CLI: daemon status JSON ---------------------------


class TestDaemonStatusJSONIsNotSoftWrapped:
    """``orchestrator daemon status`` must emit machine-readable JSON
    that survives long ``state_db_path`` values.

    Rich's ``console.print`` soft-wraps at terminal width (or ~80 cols
    when ``COLUMNS`` is unset / the stream isn't a TTY). On macOS
    (``/private/var/folders/...``) and Windows the workspace's
    ``state.db`` path frequently runs past 100 chars; a wrap landing
    INSIDE the ``state_db_path`` string value injects a raw newline
    into a JSON string and breaks ``json.loads`` on the receiver. The
    JSON branch of ``daemon status`` must therefore go through
    :func:`click.echo` (or another non-wrapping channel), NOT through
    Rich's wrapping print.

    This test fixes ``COLUMNS=40`` (well below any realistic
    ``state.db`` path) and a >100-char workspace path so a Rich-print
    regression deterministically breaks the round-trip.
    """

    def test_long_path_round_trips_through_json_loads(self, monkeypatch, tmp_path):
        import json as _json

        from click.testing import CliRunner

        from orchestrator.cli import cli

        # Force a narrow terminal so a wrapping writer would visibly
        # break the JSON; production Rich uses ``shutil.get_terminal_size``
        # which falls back to ``COLUMNS``.
        monkeypatch.setenv("COLUMNS", "40")

        # Build a workspace path well over 100 characters so a soft
        # wrap at COLUMNS would land mid-string.
        long_dir = tmp_path / ("x" * 120)
        long_dir.mkdir()
        db_path = long_dir / "state.db"
        monkeypatch.setattr("orchestrator.state.STATE_DB", db_path)

        from orchestrator import state as state_mod

        state_mod.init_db()
        resolved_path = str(db_path.resolve())
        assert len(resolved_path) > 100, (
            f"test fixture path too short ({len(resolved_path)} chars); "
            "the regression only reproduces past the wrap column"
        )
        assert state_mod.claim_daemon_lock(resolved_path, "long-path-holder") is True

        result = CliRunner().invoke(cli, ["daemon", "status"])
        assert result.exit_code == 0, f"`daemon status` exited non-zero; output={result.output!r}"
        # The exact assertion: the receiver can ``json.loads`` the
        # output. A wrap injected into the JSON string would raise
        # ``JSONDecodeError: Invalid control character``.
        try:
            parsed = _json.loads(result.output)
        except _json.JSONDecodeError as e:
            pytest.fail(
                f"daemon status output not valid JSON (soft-wrap regression?): "
                f"{e}\nOUTPUT:\n{result.output!r}"
            )
        assert parsed["state_db_path"] == resolved_path
        assert parsed["holder_id"] == "long-path-holder"
