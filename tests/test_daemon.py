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

    @pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
    def test_non_finite_rejected(self, monkeypatch, raw):
        """PR #61 Copilot 2: ``nan`` / ``inf`` parse as valid ``float`` but
        propagating either into :meth:`threading.Event.wait` would either
        hang the loop forever (``inf``) or burn CPU on instant wake-ups
        (``nan``). Operator-controlled env vars must NOT silently break
        the loop semantics — fall back to the default and warn."""
        monkeypatch.setenv(daemon.POLL_INTERVAL_ENV, raw)
        assert daemon._poll_interval_s() == daemon.POLL_INTERVAL_DEFAULT_S

    def test_negative_finite_floors_to_min(self, monkeypatch):
        """A finite negative value still gets floored by ``max(0.1, ...)``
        so the loop never gets a zero or negative wait — keeps the
        Event.wait contract intact even if an operator sets
        ``ORCH_DAEMON_POLL_INTERVAL_S=-5``."""
        monkeypatch.setenv(daemon.POLL_INTERVAL_ENV, "-5")
        assert daemon._poll_interval_s() == 0.1


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
        """Drive-disabled returns the EXIT_DRIVE_DISABLED sentinel (PR #61 M2).

        Distinct from a clean-shutdown ``0`` so a supervisor can branch
        on ``$?`` between "operator forgot the opt-in env var" and
        "service ran to completion".
        """
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        loop = daemon.DaemonLoop()
        assert loop.run() == daemon.EXIT_DRIVE_DISABLED
        # Nothing in state.db lock table.
        path = str(state.STATE_DB.resolve())
        assert state.get_daemon_lock(path) is None

    def test_run_noop_when_lock_held(self, tmp_state_db, monkeypatch):
        """Another daemon holds the lock → run returns EXIT_LOCK_HELD (PR #61 M2)."""
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        # Pre-seed an unrelated holder.
        path = str(state.STATE_DB.resolve())
        state.claim_daemon_lock(path, "other-holder")
        loop = daemon.DaemonLoop(holder_id="us")
        assert loop.run() == daemon.EXIT_LOCK_HELD
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


# --------------------------- H1: F-014 shadow + snapshot persistence ---------------------------


def _build_fake_health_report(unit_id: str):
    """Construct a minimal valid :class:`HealthReport` for tests.

    Centralised so the H1 test suite below + future call sites stay in
    sync with the dataclass shape.
    """
    from orchestrator.health import (
        CISnapshot,
        GitSnapshot,
        HealthReport,
        OrchestratorSnapshot,
        ReviewSnapshot,
    )

    return HealthReport(
        unit_id=unit_id,
        pr=None,
        git=GitSnapshot(
            ahead_by=None,
            behind_by=None,
            head_sha=None,
            head_age_seconds=None,
            last_force_push_at=None,
        ),
        ci=CISnapshot(runs=[], pending=[], failing=[], required=[], missing_required=[]),
        reviews=ReviewSnapshot(
            approvals=0,
            changes_requested=0,
            dismissed=0,
            unresolved_threads=0,
            codeowner_requested=[],
            copilot_present=False,
            copilot_state=None,
        ),
        workers=[],
        orchestrator=OrchestratorSnapshot(
            cycle=0,
            cycle_cap=3,
            cycles_remaining=3,
            last_activity="",
            last_activity_age_seconds=None,
            downstream_blocked=0,
        ),
    )


class TestProbePersistsShadowAndSnapshot:
    """PR #61 reviewer H1: ``_probe_and_decide_unit`` must persist the
    F-014 ``shadow_transition_proposed`` and ``health_report_snapshot``
    events the canonical ``inspect_unit_health`` caller produces. The
    daemon's tick produces strictly less audit data than the blocking
    path otherwise — silently breaking the spec's F-015 absorption
    clause (F-014's shadow-decision telemetry IS the validation harness
    for the daemon's rollout)."""

    def _stub_probe(self, monkeypatch, decision):
        from orchestrator.tools import health as tools_health

        report = _build_fake_health_report("U-S")

        def _fake(_unit, _repo):
            return report, decision

        monkeypatch.setattr(tools_health, "_probe_and_decide", _fake)

    def test_shadow_decisions_persist_as_events(self, tmp_state_db, monkeypatch):
        """Every shadow rule the decision table fires must land as a
        ``shadow_transition_proposed`` event."""
        from orchestrator.health import Action, Decision, ShadowDecision

        state.save_feature(
            Feature(id="F-S", title="t", description="d", repo_path="https://github.com/o/r")
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="U-S", feature_id="F-S", status="in_ci", pr_number=42)
        )
        shadow = ShadowDecision(
            rule_name="test_shadow_rule",
            predicted_action=Action.transition("done", "would have flipped"),
            trigger_inputs={"k": "v"},
            rationale="exercising H1 fix",
        )
        decision = Decision(actions_to_apply=[], shadow_decisions=[shadow])
        self._stub_probe(monkeypatch, decision)
        unit = state.get_unit_state("U-S")
        actions = daemon._probe_and_decide_unit(unit)
        assert actions == []  # no live actions; only the shadow fired
        events = state.list_events("U-S")
        shadow_events = [e for e in events if e["event_type"] == "shadow_transition_proposed"]
        assert len(shadow_events) == 1, (
            f"expected one shadow_transition_proposed event; got {len(shadow_events)}"
        )
        assert "test_shadow_rule" in shadow_events[0]["summary"]

    def test_snapshot_persisted_once_per_interval(self, tmp_state_db, monkeypatch):
        """First probe in the window writes a ``health_report_snapshot``;
        a second probe within the rate-limit window does NOT."""
        from orchestrator.health import Decision

        state.save_feature(
            Feature(id="F-S", title="t", description="d", repo_path="https://github.com/o/r")
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="U-S", feature_id="F-S", status="in_ci", pr_number=42)
        )
        # 24h default — both probes in the same test fall in the same window.
        self._stub_probe(monkeypatch, Decision(actions_to_apply=[], shadow_decisions=[]))
        unit = state.get_unit_state("U-S")
        daemon._probe_and_decide_unit(unit)
        daemon._probe_and_decide_unit(unit)
        events = state.list_events("U-S")
        snapshots = [e for e in events if e["event_type"] == "health_report_snapshot"]
        assert len(snapshots) == 1, (
            f"expected exactly one rate-limited snapshot event; got {len(snapshots)}"
        )

    def test_no_persistence_when_probe_returns_none(self, tmp_state_db, monkeypatch):
        """A unit with no PR → ``_probe_unit_health`` returns ``None`` →
        neither shadow nor snapshot writes happen (the daemon must NOT
        crash or write spurious events when there's no PR to probe)."""
        state.save_feature(
            Feature(id="F-S", title="t", description="d", repo_path="https://github.com/o/r")
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="U-S", feature_id="F-S", status="coding", pr_number=None)
        )
        unit = state.get_unit_state("U-S")
        actions = daemon._probe_and_decide_unit(unit)
        assert actions == []
        events = state.list_events("U-S")
        assert not any(
            e["event_type"] in ("shadow_transition_proposed", "health_report_snapshot")
            for e in events
        )


# --------------------------- M2: exit code sentinels ---------------------------


class TestExitCodeSentinels:
    """PR #61 reviewer M2: ``DaemonLoop.run`` must return distinguishable
    non-zero sentinels for "drive disabled" vs "lock held" vs "clean
    shutdown" so a systemd / launchd / shell-script supervisor can
    branch on ``$?`` correctly."""

    def test_drive_disabled_returns_exit_drive_disabled(self, tmp_state_db, monkeypatch):
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        assert daemon.DaemonLoop().run() == daemon.EXIT_DRIVE_DISABLED

    def test_lock_held_returns_exit_lock_held(self, tmp_state_db, monkeypatch):
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        path = str(state.STATE_DB.resolve())
        state.claim_daemon_lock(path, "incumbent")
        assert daemon.DaemonLoop(holder_id="us").run() == daemon.EXIT_LOCK_HELD

    def test_clean_shutdown_returns_non_negative_ticks(self, tmp_state_db, monkeypatch):
        """A loop that started, ticked once, and stopped returns ``>= 0`` —
        never an EXIT_* sentinel — so the CLI maps to exit code 0."""
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        monkeypatch.setattr(daemon, "reconcile_once", lambda: 0)
        loop = daemon.DaemonLoop(poll_interval_s=0.01, holder_id="us")
        orig_tick = loop.tick

        def _tick_then_stop() -> int:
            r = orig_tick()
            loop.stop()
            return r

        monkeypatch.setattr(loop, "tick", _tick_then_stop)
        result = loop.run()
        assert result >= 0
        assert result not in (daemon.EXIT_DRIVE_DISABLED, daemon.EXIT_LOCK_HELD)

    def test_exit_code_mapping(self):
        """``exit_code_for_run`` maps each sentinel to its OS exit code:
        0 (clean) / 2 (drive disabled) / 3 (lock held)."""
        assert daemon.exit_code_for_run(0) == 0
        assert daemon.exit_code_for_run(5) == 0
        assert daemon.exit_code_for_run(daemon.EXIT_DRIVE_DISABLED) == 2
        assert daemon.exit_code_for_run(daemon.EXIT_LOCK_HELD) == 3

    def test_cli_start_lock_held_exits_three(self, tmp_state_db, monkeypatch):
        """End-to-end CLI: another daemon owns the lock → ``orchestrator
        daemon start`` exits 3 (not 0). Same-workspace contention is a
        configuration error, not a transient."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        path = str(state.STATE_DB.resolve())
        state.claim_daemon_lock(path, "incumbent")
        result = CliRunner().invoke(cli, ["daemon", "start"])
        assert result.exit_code == 3, f"expected exit 3 (lock-held); got {result.exit_code}"


# --------------------------- worker cache memoization ---------------------------


class TestWorkerCacheMemoization:
    """PR #61 reviewer 🔵: ``make_worker`` constructed a fresh
    :class:`ManagedAgentWorker` (and Anthropic SDK client) per ``(unit,
    role, tick)`` — at the 5s default tick with N active units that's
    3·N constructions per tick. The daemon now memoizes via
    :func:`_cached_worker`; one construction per role per process."""

    def test_same_role_returns_same_instance(self, tmp_state_db, monkeypatch):
        """Two calls for the same role return the SAME object — not a
        fresh construction."""
        constructed: list[str] = []

        class _CountingWorker:
            def __init__(self, role: str) -> None:
                self.role = role
                constructed.append(role)

            def tail_messages(self, *_a, **_k):  # pragma: no cover — not exercised here
                return {"status": "idle", "messages": [], "reason": None}

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda role: _CountingWorker(role))
        # Cache is cleared by the autouse fixture; first call constructs.
        w1 = daemon._cached_worker("coder")
        w2 = daemon._cached_worker("coder")
        assert w1 is w2, "cache returned a fresh worker for the same role"
        assert constructed == ["coder"]

    def test_different_roles_construct_independently(self, tmp_state_db, monkeypatch):
        constructed: list[str] = []

        class _CountingWorker:
            def __init__(self, role: str) -> None:
                self.role = role
                constructed.append(role)

            def tail_messages(self, *_a, **_k):
                return {"status": "idle", "messages": [], "reason": None}

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda role: _CountingWorker(role))
        daemon._cached_worker("coder")
        daemon._cached_worker("tester")
        daemon._cached_worker("reviewer")
        # Each role constructs exactly once.
        assert sorted(constructed) == ["coder", "reviewer", "tester"]

    def test_reset_worker_cache_drops_entries(self, tmp_state_db, monkeypatch):
        """:func:`reset_worker_cache` lets tests / takeover paths drop the
        memoized workers explicitly. After a reset, the next call
        constructs fresh."""
        constructed: list[int] = []

        class _CountingWorker:
            def __init__(self, role: str) -> None:
                self.role = role
                constructed.append(len(constructed) + 1)

            def tail_messages(self, *_a, **_k):
                return {"status": "idle", "messages": [], "reason": None}

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda role: _CountingWorker(role))
        daemon._cached_worker("coder")
        daemon._cached_worker("coder")
        assert len(constructed) == 1
        daemon.reset_worker_cache()
        daemon._cached_worker("coder")
        assert len(constructed) == 2

    def test_reconcile_unit_does_not_repeatedly_construct(self, tmp_state_db, monkeypatch):
        """The end-to-end shape that motivated the cache: a reconcile
        across multiple ticks with persistent sessions constructs
        each role's worker once, NOT once per tick."""
        constructed_roles: list[str] = []

        class _CountingWorker:
            def __init__(self, role: str) -> None:
                self.role = role
                constructed_roles.append(role)

            def tail_messages(self, _sid: str, *, limit: int = 50):  # noqa: ARG002
                return {"status": "idle", "messages": [], "reason": None}

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda role: _CountingWorker(role))
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [])

        state.save_feature(Feature(id="F-W", title="t", description="d"))
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U-W",
                feature_id="F-W",
                status="coding",
                coder_session_id="sc",
                tester_session_id="st",
                reviewer_session_id="sr",
            )
        )
        # Three ticks. Without the cache, that's 3·3 = 9 constructions;
        # with the cache, exactly 3 (one per role).
        for _ in range(3):
            daemon.reconcile_unit("U-W")
        assert sorted(constructed_roles) == ["coder", "reviewer", "tester"]


# --------------------------- F-018 conflict-fix dispatch ---------------------------


def _conflict_action(files: list[str], mergeable_state: str = "dirty"):
    """Build the ``pr_conflict_detected`` Action the F-014 decision table emits."""
    from orchestrator.health import Action

    return Action.event(
        "pr_conflict_detected",
        f"PR conflict ({mergeable_state})",
        details=f"conflict files: {', '.join(files)}",
        payload={"conflict_files": list(files), "mergeable_state": mergeable_state},
    )


def _seed_awaiting_merge(
    *,
    unit_id: str = "U-AM",
    feature_id: str = "F-AM",
    coder_session: str = "sc",
    branch: str = "feat/x",
    pr_number: int = 7,
    conflict_attempts: int = 0,
) -> WorkUnitState:
    """Seed a unit parked in ``approved_awaiting_merge`` for daemon
    conflict-fix tests. Includes a feature + plan row so
    ``_dispatch_conflict_fix``'s lookup chain succeeds.
    """
    from orchestrator.models import WorkUnit

    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title="u", description="d")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="approved_awaiting_merge",
            branch=branch,
            pr_number=pr_number,
            coder_session_id=coder_session,
        )
    )
    for _ in range(conflict_attempts):
        state.increment_conflict_fix_attempts(unit_id)
    return state.get_unit_state(unit_id)  # type: ignore[return-value]


class _RecordingWorker:
    """Captures ``resume_async`` calls for assertion. Other methods no-op."""

    def __init__(self) -> None:
        self.resume_calls: list[tuple[str, str]] = []

    def resume_async(self, session_id: str, msg: str) -> None:
        self.resume_calls.append((session_id, msg))

    def tail_messages(self, _sid: str, *, limit: int = 50):  # noqa: ARG002
        return _FakeTailResult()


class TestDaemonConflictFixDispatch:
    """The F-018 daemon trigger: a unit sitting in
    ``approved_awaiting_merge`` develops a conflict (sibling unit merged
    underneath). The reconcile loop catches ``pr_conflict_detected`` via
    ``inspect_unit_health``, transitions the unit back to ``fixing``,
    and submits a rebase request to the coder via
    :meth:`~orchestrator.workers.base.Worker.resume_async` (submit-only
    so the daemon's tick isn't blocked).

    Mirrors the cycle_review-side conflict-fix loop's semantics:
    ``conflict_fix_attempts`` is independent of ``review_round``, cap-3
    on the counter escalates with ``conflict_rebase_diverging``, and
    the dispatch is idempotent (per-unit owner CAS).
    """

    def test_conflict_on_awaiting_merge_dispatches_coder_resume_async(
        self, tmp_state_db, monkeypatch
    ):
        unit = _seed_awaiting_merge()
        worker = _RecordingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        monkeypatch.setattr(
            daemon, "_probe_and_decide_unit", lambda _u: [_conflict_action(["a.py", "b.py"])]
        )

        daemon.reconcile_unit(unit.unit_id)

        # Status flipped to fixing.
        got = state.get_unit_state(unit.unit_id)
        assert got is not None
        assert got.status == "fixing"
        assert got.conflict_fix_attempts == 1
        # Coder was resumed asynchronously with the conflict file list.
        assert len(worker.resume_calls) == 1
        sid, msg = worker.resume_calls[0]
        assert sid == "sc"
        assert "SOURCE:    merge" in msg
        assert "a.py" in msg and "b.py" in msg

    def test_no_dispatch_when_status_is_not_awaiting_merge(self, tmp_state_db, monkeypatch):
        """The F-018 daemon trigger fires ONLY for the post-terminal
        sibling-conflict case. An active unit (``in_ci`` / ``coding`` / …)
        is handled by cycle_review's gate; the daemon must not race it.
        """
        from orchestrator.models import WorkUnit

        state.save_feature(
            Feature(
                id="F-A",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan("F-A", [WorkUnit(id="U-A", feature_id="F-A", title="u", description="d")])
        state.approve_plan("F-A")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U-A",
                feature_id="F-A",
                status="in_ci",
                branch="b",
                pr_number=5,
                coder_session_id="sc",
            )
        )

        worker = _RecordingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        monkeypatch.setattr(
            daemon, "_probe_and_decide_unit", lambda _u: [_conflict_action(["a.py"])]
        )

        daemon.reconcile_unit("U-A")

        # Status untouched; no daemon dispatch.
        got = state.get_unit_state("U-A")
        assert got is not None
        assert got.status == "in_ci"
        assert got.conflict_fix_attempts == 0
        assert worker.resume_calls == []

    def test_cap_3_escalates_with_conflict_rebase_diverging(self, tmp_state_db, monkeypatch):
        unit = _seed_awaiting_merge(conflict_attempts=3)
        worker = _RecordingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        monkeypatch.setattr(
            daemon, "_probe_and_decide_unit", lambda _u: [_conflict_action(["x.py"])]
        )

        daemon.reconcile_unit(unit.unit_id)

        got = state.get_unit_state(unit.unit_id)
        assert got is not None
        assert got.status == "escalated"
        assert "conflict_rebase_diverging" in got.last_error
        # No further resume_async on cap-3.
        assert worker.resume_calls == []
        # Audit event for the dashboard.
        events = state.list_events(unit.unit_id)
        assert any(e["event_type"] == "conflict_rebase_diverging" for e in events)

    def test_dispatch_skipped_when_no_coder_session(self, tmp_state_db, monkeypatch):
        """Defensive — without a coder session there's nothing to resume.
        Daemon logs + skips rather than crashing the tick or escalating."""
        unit = _seed_awaiting_merge(coder_session="")
        worker = _RecordingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        monkeypatch.setattr(
            daemon, "_probe_and_decide_unit", lambda _u: [_conflict_action(["a.py"])]
        )

        daemon.reconcile_unit(unit.unit_id)

        got = state.get_unit_state(unit.unit_id)
        assert got is not None
        # Status unchanged — no transition, no dispatch.
        assert got.status == "approved_awaiting_merge"
        assert got.conflict_fix_attempts == 0
        assert worker.resume_calls == []

    def test_dispatch_releases_owner_cas(self, tmp_state_db, monkeypatch):
        """The owner CAS must be released after the dispatch lands so a
        later tick (or a lead's ``send_to_unit_async``) can proceed.
        Symmetric to the daemon's other CAS sites (PR #61 reviewer)."""
        unit = _seed_awaiting_merge()
        worker = _RecordingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        monkeypatch.setattr(
            daemon, "_probe_and_decide_unit", lambda _u: [_conflict_action(["a.py"])]
        )

        daemon.reconcile_unit(unit.unit_id)
        assert state.has_active_advance_lock(unit.unit_id) is False
        got = state.get_unit_state(unit.unit_id)
        assert got is not None
        assert got.owner == ""

    def test_no_dispatch_when_no_conflict_action(self, tmp_state_db, monkeypatch):
        """Sanity — without a ``pr_conflict_detected`` action, the unit
        stays in ``approved_awaiting_merge`` and nothing dispatches."""
        unit = _seed_awaiting_merge()
        worker = _RecordingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [])

        daemon.reconcile_unit(unit.unit_id)

        got = state.get_unit_state(unit.unit_id)
        assert got is not None
        assert got.status == "approved_awaiting_merge"
        assert got.conflict_fix_attempts == 0
        assert worker.resume_calls == []

    def test_dispatch_skipped_when_resume_async_raises(self, tmp_state_db, monkeypatch):
        """A backend hiccup on ``resume_async`` must NOT crash the tick.
        The dispatch returns False and the next tick re-derives — the
        ``conflict_fix_attempts`` bump is already persisted, so the
        retry burns one of the cap-3 mechanically (acceptable; the
        alternative is silently looping on a broken backend)."""
        unit = _seed_awaiting_merge()

        class _RaisingWorker:
            def resume_async(self, _sid: str, _msg: str) -> None:
                raise RuntimeError("backend down")

            def tail_messages(self, _sid: str, *, limit: int = 50):  # noqa: ARG002
                return _FakeTailResult()

        worker = _RaisingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        monkeypatch.setattr(
            daemon, "_probe_and_decide_unit", lambda _u: [_conflict_action(["a.py"])]
        )

        # Should not raise.
        daemon.reconcile_unit(unit.unit_id)
        # Owner CAS released so a later tick can retry.
        assert state.has_active_advance_lock(unit.unit_id) is False

    def test_dispatch_skipped_when_feature_missing(self, tmp_state_db, monkeypatch):
        """Daemon must not crash when the feature row is gone (test fixture
        / cleanup race). Logs + skips, no status change."""
        unit = _seed_awaiting_merge()
        # Drop the feature row (and its cascade-deleted plan).
        from orchestrator import state as _state

        with _state._connect() as conn:
            conn.execute("DELETE FROM features WHERE id = ?", (unit.feature_id,))

        worker = _RecordingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        # The conflict action is on a unit whose feature is gone.
        result = daemon._dispatch_conflict_fix(unit, ["a.py"])
        assert result is False
        assert worker.resume_calls == []
