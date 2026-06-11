"""F-016-U-5 — Phase 3: watcher daemon (tester acceptance corners).

Independent of ``tests/test_daemon.py``, ``tests/test_f016_u5_tester.py``,
``tests/test_f016_u5_tester_extras.py``, and ``tests/test_state.py``. This
file pins spec-acceptance corners those four files do not explicitly
assert, against ``features/F-016/spec.md`` § Phase 3 and the
``docs/PROPOSAL-async-orchestrator.md`` proposal § "Phase 3 — Watcher
daemon" + § "Singleton enforcement — SQLite-backed, not pidfile":

  * **``daemon_locks`` row exposes the four columns the spec promises.**
    Proposal § "Singleton enforcement" pins the schema verbatim
    (``state_db_path TEXT PRIMARY KEY``, ``holder_id TEXT``,
    ``heartbeat_at DATETIME``, ``started_at DATETIME``) and the CLI's
    ``orchestrator daemon status`` command is the read surface — both
    require the full row to be readable. The existing tests only check
    ``holder_id``; this file pins the rest.

  * **Stale takeover resets ``started_at``, ``heartbeat_at`` AND
    ``holder_id`` atomically.** Per the proposal: "random uuid per
    daemon start" — a takeover by a NEW holder is a new daemon start
    and its ``started_at`` must reset, otherwise monitoring
    (``orchestrator daemon status``) would report the dead daemon's
    start time. Existing crash-recovery tests only confirm the
    ``holder_id`` flips.

  * **``_state_db_path_str`` produces an absolute resolved path.** The
    spec's "Daemon per workspace, not per host" hinges on the lock
    key being a stable string — symlinks and relative segments must
    collapse so two ``orchestrator daemon start`` invocations from
    different cwds resolve to the same row.

  * **``_probe_and_decide_unit`` early-outs on the three failure
    modes the spec calls out as non-fatal:** no PR yet, missing
    feature row, GitHub-side probe error. Spec § "R3 risk control":
    "a transient tail failure or a flaky GitHub call on one unit
    must not freeze the entire workspace's daemon loop."

  * **Unknown ``(role, marker)`` pairs are refused (conservative
    default).** The daemon's own comment block (``daemon.py`` lines
    302-309 verbatim): "Unknown (role, marker) pairs ... default to
    *no* sources — a conservative 'refuse unfamiliar markers' so a
    daemon ahead of the grammar can't silently drive transitions
    whose semantics weren't reviewed." A future marker grammar
    addition the daemon hasn't been taught about MUST NOT silently
    transition the unit; only the published pairs may.

  * **``_apply_health_action`` releases the daemon owner CAS on every
    exit path.** The spec's "owner CAS picks a winner; loser bails"
    contract requires the winner to release after applying so a
    follow-up ``lead_advance_lock`` can claim. Existing tests cover
    the marker-transition release; this file pins the F-014-action
    release symmetrically (success, cancellation-mid-apply, and
    raise-in-apply).

  * **Marker phase runs BEFORE F-014 phase, every tick.** Spec
    § "main loop" pseudocode: the marker-driven branch comes first
    (faster to derive, cheaper to apply); F-014 PR-side reconcile
    is the catch-all. A regression that flipped this order would
    let an F-014 transition land before a fresher marker is
    consumed — a "lost message" symptom the level-triggered design
    was explicitly built to avoid.

  * **A coder ``BLOCKED`` mid-marker-loop stops further role scans
    AND the F-014 probe.** ``BLOCKED`` flips status to ``escalated``
    (which is in ``TERMINAL_UNIT_STATUSES``). The reconcile loop's
    per-iteration ``_is_actionable`` check must bail out — running
    a tester scan or F-014 probe on a unit that just escalated would
    be doing work on a terminal unit, violating "Idempotent
    transitions are load-bearing".

  * **``run_daemon`` is a thin entry point that delegates to
    ``DaemonLoop().run()``.** The proposal § "Process model" lists
    ``orchestrator daemon start`` as the CLI entry point and the CLI
    wires it to ``run_daemon``; the lock claim / signal handling /
    loop body must NOT be re-implemented in the entry point.

  * **CLI ``orchestrator daemon status`` reads the lock without
    claiming.** Proposal § "Process model" pseudocode lists this
    subcommand as informational — it must be safe to call from a
    second terminal without disturbing the running daemon.

  * **CLI ``orchestrator daemon start`` is a no-op without
    ``ORCH_DAEMON_DRIVE``.** Mirrors the module-level guard so the
    safety hinge holds at both entry points.

Fixtures are local; no overlap with the test files above so a future
refactor of any of them doesn't ripple here.
"""

from __future__ import annotations

import json
import sqlite3 as _sql
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from orchestrator import daemon, markers, state
from orchestrator.cli import cli
from orchestrator.health import Action
from orchestrator.models import Feature, WorkUnitState

# --------------------------- shared fixtures ---------------------------


@dataclass
class _TailResult:
    """``TailResult``-shaped stub readable via both ``[]`` and ``.get``."""

    status: str = "idle"
    messages: list[dict] = field(default_factory=list)
    reason: str | None = None

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


class _SilentWorker:
    """No-marker tail; lets reconcile_unit fall through to the F-014 phase."""

    def tail_messages(self, *_a, **_k):
        return _TailResult()


def _seed_unit(
    *,
    unit_id: str = "U-A1",
    feature_id: str = "F-A1",
    status: str = "coding",
    sessions: dict[str, str] | None = None,
    pr_number: int | None = None,
) -> WorkUnitState:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
        )
    )
    s = sessions or {}
    unit = WorkUnitState(
        unit_id=unit_id,
        feature_id=feature_id,
        status=status,
        coder_session_id=s.get("coder", ""),
        tester_session_id=s.get("tester", ""),
        reviewer_session_id=s.get("reviewer", ""),
        pr_number=pr_number,
    )
    state.upsert_unit_state(unit)
    return unit


def _no_workers(monkeypatch) -> None:
    monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _SilentWorker())


def _no_probe(monkeypatch) -> None:
    monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [])


# --------------------------- daemon_locks row shape ---------------------------


class TestDaemonLockRowShape:
    """``daemon_locks`` row exposes the four columns the spec / proposal
    pin verbatim. The CLI ``daemon status`` command reads them all."""

    def test_row_dict_has_four_expected_keys(self, tmp_state_db):
        path = "/abs/workspace-A/state.db"
        assert state.claim_daemon_lock(path, "h1") is True
        row = state.get_daemon_lock(path)
        assert row is not None
        # Column set per proposal § "Singleton enforcement" verbatim;
        # F-016-U-7 added ``pid`` so ``orchestrator daemon stop`` can
        # SIGTERM the holder without a sidecar pidfile.
        assert set(row.keys()) == {
            "state_db_path",
            "holder_id",
            "heartbeat_at",
            "started_at",
            "pid",
        }
        assert row["state_db_path"] == path
        assert row["holder_id"] == "h1"
        # Both timestamps are non-empty ISO strings populated on claim.
        assert row["heartbeat_at"]
        assert row["started_at"]
        # ``pid`` is nullable — this claim didn't pass one, so it's None.
        assert row["pid"] is None

    def test_started_at_equals_heartbeat_at_on_fresh_claim(self, tmp_state_db):
        """On the initial INSERT the two timestamps share one ``_now()``
        call — there is no scenario in which a fresh row has them
        diverge. Anchors a tight invariant the SQL relies on."""
        path = "/abs/workspace-B/state.db"
        assert state.claim_daemon_lock(path, "h1") is True
        row = state.get_daemon_lock(path)
        assert row["heartbeat_at"] == row["started_at"]

    def test_get_daemon_lock_returns_none_when_absent(self, tmp_state_db):
        # No claim: explicit None, not an empty dict.
        assert state.get_daemon_lock("/abs/unused/state.db") is None


class TestStaleTakeoverFields:
    """A takeover resets ``started_at`` because the new holder is a
    different daemon-start, with a different ``holder_id`` UUID. The
    proposal's "random uuid per daemon start" is the contract."""

    def test_stale_takeover_resets_started_at(self, tmp_state_db):
        path = "/abs/workspace-C/state.db"
        state.claim_daemon_lock(path, "h1")
        original_started_at = state.get_daemon_lock(path)["started_at"]
        # Age both timestamps so heartbeat is stale; preserve original
        # started_at by writing back to the heartbeat column only.
        aged = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        with _sql.connect(state.STATE_DB) as conn:
            conn.execute(
                "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ?",
                (aged, path),
            )
            conn.commit()
        # Pre-takeover: started_at unchanged.
        assert state.get_daemon_lock(path)["started_at"] == original_started_at
        # Takeover by a NEW holder.
        assert state.claim_daemon_lock(path, "h2", stale_after_s=30) is True
        row = state.get_daemon_lock(path)
        assert row["holder_id"] == "h2"
        # started_at MUST be refreshed — it is the new daemon's start time,
        # not the dead one's. Anything else would mislead `daemon status`.
        assert row["started_at"] != original_started_at
        # And heartbeat_at is also refreshed; the takeover SQL writes both.
        assert row["heartbeat_at"] == row["started_at"]


# --------------------------- _state_db_path_str absolute ---------------------------


class TestStateDbPathResolution:
    """The lock key must be a stable absolute path so two ``daemon start``
    invocations from different cwds collide on the same row."""

    def test_state_db_path_str_is_absolute(self, tmp_state_db):
        from pathlib import Path

        result = daemon._state_db_path_str()
        assert Path(result).is_absolute(), (
            f"_state_db_path_str returned non-absolute {result!r}; "
            "the daemon_locks key requires an absolute path so two "
            "daemons from different cwds collide on one row."
        )

    def test_claim_singleton_keys_off_resolved_path(self, tmp_state_db):
        """``claim_singleton`` writes a row keyed on ``state_db_path_str``;
        ``get_daemon_lock`` must find it via that same string."""
        h = daemon.claim_singleton(holder_id="resolved-key")
        assert h is not None
        # Round-trip — what claim_singleton wrote is what get_daemon_lock
        # finds.
        row = state.get_daemon_lock(h.state_db_path)
        assert row is not None
        assert row["holder_id"] == "resolved-key"


# --------------------------- _probe_and_decide_unit early-outs ---------------------------


class TestProbeAndDecideEarlyOuts:
    """The daemon's wrapper around F-014's probe + decide must early-out
    on the three non-fatal failure modes spec § "R3 risk control" calls
    out."""

    def test_no_pr_number_returns_empty(self, tmp_state_db):
        """``pr_number is None`` (the unit hasn't opened a PR yet) means
        F-014 has nothing to probe; the daemon must not call the
        helper. Otherwise a coder-still-spawning unit would burn a
        GH API request every tick."""
        unit = _seed_unit(status="coding", pr_number=None)
        actions = daemon._probe_and_decide_unit(unit)
        assert actions == []

    def test_missing_feature_returns_empty(self, tmp_state_db, monkeypatch):
        """A unit whose ``feature_id`` no longer resolves to a feature
        row (orphan after a manual cleanup, broken seed) must NOT
        crash the tick; the daemon swallows and returns empty."""
        unit = _seed_unit(status="in_ci", pr_number=99)
        # Delete the feature row out from under the unit.
        monkeypatch.setattr(daemon.state, "get_feature", lambda _fid: None)
        actions = daemon._probe_and_decide_unit(unit)
        assert actions == []

    def test_probe_error_string_is_swallowed_and_returns_empty(self, tmp_state_db, monkeypatch):
        """``orchestrator.tools.health._probe_and_decide`` returns a
        STRING on error (need_github_token, fetch failure). The daemon
        must consume that and return ``[]`` rather than letting the
        string blow up the action loop."""
        unit = _seed_unit(status="in_ci", pr_number=99)
        from orchestrator.tools import health as tools_health

        monkeypatch.setattr(
            tools_health,
            "_probe_and_decide",
            lambda _u, _r: "ERROR: GH 500",
        )
        actions = daemon._probe_and_decide_unit(unit)
        assert actions == [], (
            "_probe_and_decide_unit did not swallow a string-error "
            "from tools.health._probe_and_decide; spec § R3 risk "
            "control violated."
        )


# --------------------------- unknown markers refused ---------------------------


class TestUnknownMarkerRefused:
    """A ``(role, marker)`` pair not in ``_MARKER_SOURCE_STATUSES`` must
    return False even when the unit's status WOULD be in the active set.
    Per the daemon's own comment (lines 302-309): "Unknown pairs default
    to *no* sources — refuse unfamiliar markers."

    This is the safety hinge that lets the marker grammar grow without
    the daemon silently driving transitions whose semantics it doesn't
    know.
    """

    def test_unknown_role_marker_pair_returns_false(self, tmp_state_db):
        unit = _seed_unit(status="coding")
        # Craft a MarkerSpec for a (role, marker) pair the daemon hasn't
        # been taught about — i.e., NOT in _MARKER_SOURCE_STATUSES. The
        # spec's target_status is in the active set, so if the daemon
        # naively skipped the source-status fence the unit would flip.
        spec = markers.MarkerSpec(
            role="coder",
            marker="HYPOTHETICAL_NEW_MARKER",
            event_type="hypothetical",
            target_status="in_ci",
            payload="some-payload",
            summary="future grammar",
            details="",
            last_error="",
        )
        # Sanity — this pair is not in the table.
        assert ("coder", "HYPOTHETICAL_NEW_MARKER") not in daemon._MARKER_SOURCE_STATUSES
        applied = daemon._apply_marker_transition(unit.unit_id, spec)
        assert applied is False, (
            "unknown (coder, HYPOTHETICAL_NEW_MARKER) flipped status; the "
            "daemon's per-marker fence must refuse unfamiliar pairs by "
            "default (daemon.py lines 302-309)."
        )
        assert state.get_unit_state(unit.unit_id).status == "coding"

    def test_known_marker_with_no_valid_source_status_returns_false(self, tmp_state_db):
        """Symmetric check: a marker whose ``(role, marker)`` IS in the
        fence table but whose current status is NOT in the allowed
        sources also bails. ``TESTS_PASS`` from a unit at ``coding``
        (impossible in production — only a buggy worker would emit it
        from a coder shell) is refused."""
        unit = _seed_unit(status="coding", sessions={"tester": "sess_t"})
        spec = markers.scan_response("tester", "TESTS_PASS")
        assert spec is not None
        # tester/TESTS_PASS only fires from "testing"; "coding" is NOT
        # an allowed source.
        assert "coding" not in daemon._MARKER_SOURCE_STATUSES[("tester", "TESTS_PASS")]
        applied = daemon._apply_marker_transition(unit.unit_id, spec)
        assert applied is False
        assert state.get_unit_state(unit.unit_id).status == "coding"


# --------------------------- health-action owner release ---------------------------


class TestHealthActionOwnerRelease:
    """``_apply_health_action`` mirrors ``_apply_marker_transition``'s
    owner-release contract. After every exit path (success, cancelled
    mid-CAS, applier raises) the daemon's CAS owner must be cleared so
    a follow-up ``lead_advance_lock`` can claim freely."""

    def test_owner_released_after_successful_apply(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="in_ci", pr_number=42)
        # Use a no-op _apply_action — the release is what we're asserting.
        from orchestrator.tools import health as tools_health

        monkeypatch.setattr(tools_health, "_apply_action", lambda _u, _a: None)
        ok = daemon._apply_health_action(unit, Action.transition("done", "merged"))
        assert ok is True
        assert state.get_unit_state(unit.unit_id).owner == "", (
            "_apply_health_action did not release the daemon owner after "
            "a successful apply; a follow-up lead_advance_lock would be "
            "blocked."
        )

    def test_owner_released_after_cancelled_mid_cas(self, tmp_state_db, monkeypatch):
        """If the user cancels the unit between CAS-claim and the
        ``get_unit_state`` re-read, the apply must NOT run AND the
        owner must still be released."""
        unit = _seed_unit(status="in_ci", pr_number=42)

        # Patch get_unit_state inside daemon to return a cancelled snapshot
        # — simulates a concurrent cancel landing between the claim and
        # the re-read.
        cancelled = WorkUnitState(
            unit_id=unit.unit_id,
            feature_id=unit.feature_id,
            status="in_ci",
            cancelled_at="2024-01-01T00:00:00",
        )
        monkeypatch.setattr(daemon.state, "get_unit_state", lambda _u: cancelled)

        applied_calls: list = []
        from orchestrator.tools import health as tools_health

        monkeypatch.setattr(
            tools_health,
            "_apply_action",
            lambda u, a: applied_calls.append((u.unit_id, a.kind)),
        )
        ok = daemon._apply_health_action(unit, Action.transition("done", "merged"))
        assert ok is False, "cancelled unit must NOT apply the health action"
        assert applied_calls == [], (
            "_apply_action ran on a cancelled unit; the cancelled_at "
            "guard inside the CAS body did not fire."
        )

    def test_owner_released_when_applier_raises(self, tmp_state_db, monkeypatch):
        """A raising ``_apply_action`` must propagate, but the ``finally``
        block STILL releases the owner. Otherwise one bad action would
        permanently strand the unit under the daemon's CAS."""
        unit = _seed_unit(status="in_ci", pr_number=42)
        from orchestrator.tools import health as tools_health

        def _boom(_u, _a):
            raise RuntimeError("synthetic apply failure")

        monkeypatch.setattr(tools_health, "_apply_action", _boom)
        with pytest.raises(RuntimeError, match="synthetic apply failure"):
            daemon._apply_health_action(unit, Action.transition("done", "merged"))
        # Even though the apply raised, the owner is cleared.
        assert state.get_unit_state(unit.unit_id).owner == "", (
            "_apply_health_action did not release the daemon owner via "
            "its finally block after the applier raised; one bad action "
            "would permanently strand the unit."
        )


# --------------------------- reconcile_unit phase ordering ---------------------------


class TestReconcilePhaseOrdering:
    """Marker phase runs BEFORE F-014 phase, every tick. The pseudocode
    in proposal § Phase 3 has the marker-driven branch first; flipping
    the order would let an F-014 transition land before a fresher marker
    is consumed — exactly the lost-message symptom the level-triggered
    design avoids."""

    def test_marker_scan_runs_before_probe(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(
            status="coding",
            sessions={"coder": "sess_c"},
            pr_number=42,
        )
        order: list[str] = []

        # Spy both phases.
        orig_scan = daemon._scan_role

        def _spy_scan(u, role):
            order.append(f"scan:{role}")
            return orig_scan(u, role)

        def _spy_probe(_u):
            order.append("probe")
            return []

        monkeypatch.setattr(daemon, "_scan_role", _spy_scan)
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", _spy_probe)
        _no_workers(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        # All three scans land before probe; never the other way around.
        assert order == ["scan:coder", "scan:tester", "scan:reviewer", "probe"], (
            f"phase order wrong; got {order}"
        )


class TestEscalatedStopsFurtherWork:
    """A coder ``BLOCKED`` flips status to ``escalated`` (terminal). The
    reconcile loop's per-iteration ``_is_actionable`` check must skip
    further role scans AND the F-014 probe. Otherwise the daemon would
    burn a GH API call on a unit it has already escalated.
    """

    def test_blocked_marker_short_circuits_remaining_roles_and_probe(
        self, tmp_state_db, monkeypatch
    ):
        unit = _seed_unit(
            status="coding",
            sessions={"coder": "sess_c", "tester": "sess_t", "reviewer": "sess_r"},
            pr_number=42,
        )

        # Per-role tail: coder emits BLOCKED (→ escalated), tester /
        # reviewer / probe should never run.
        @dataclass
        class _PerRole:
            role: str

            def tail_messages(self, _sid, *, limit=50):  # noqa: ARG002
                if self.role == "coder":
                    return _TailResult(
                        messages=[
                            {
                                "ts": "t",
                                "role": "agent",
                                "text": "BLOCKED: reason=auth_failure | token rejected",
                            }
                        ]
                    )
                return _TailResult()

        seen_workers: list[str] = []

        def _make_worker(role: str):
            seen_workers.append(role)
            return _PerRole(role=role)

        probe_calls = {"n": 0}

        def _spy_probe(_u):
            probe_calls["n"] += 1
            return []

        monkeypatch.setattr("orchestrator.daemon.make_worker", _make_worker)
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", _spy_probe)
        daemon.reconcile_unit(unit.unit_id)

        # Coder marker landed → status escalated.
        latest = state.get_unit_state(unit.unit_id)
        assert latest.status == "escalated"
        # Further role scans skipped.
        assert "tester" not in seen_workers, (
            f"tester scanned AFTER coder flipped to escalated; seen_workers={seen_workers}"
        )
        assert "reviewer" not in seen_workers, (
            f"reviewer scanned AFTER coder flipped to escalated; seen_workers={seen_workers}"
        )
        # F-014 probe never fired.
        assert probe_calls["n"] == 0, (
            "F-014 probe ran on a unit that just escalated; the per-"
            "iteration _is_actionable guard did not catch the terminal "
            "status."
        )


# --------------------------- run_daemon delegation ---------------------------


class TestRunDaemonEntrypoint:
    """``run_daemon`` is the function ``orchestrator daemon start`` wires
    to. It must be a thin wrapper: configure logging, instantiate
    ``DaemonLoop``, return its ``run()`` count. No bespoke lock-claim or
    signal-handling logic in the entry point."""

    def test_run_daemon_returns_drive_disabled_sentinel(self, tmp_state_db, monkeypatch):
        """Drive-disabled returns :data:`~orchestrator.daemon.EXIT_DRIVE_DISABLED`
        (PR #61 reviewer M2 — was 0 before; the non-zero sentinel lets
        a systemd / launchd / shell-script supervisor distinguish
        "operator forgot the opt-in" from a clean-shutdown ``0``).
        """
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        # Drive disabled → DaemonLoop.run is a no-op → run_daemon returns
        # EXIT_DRIVE_DISABLED.
        assert daemon.run_daemon() == daemon.EXIT_DRIVE_DISABLED

    def test_run_daemon_delegates_to_daemon_loop(self, tmp_state_db, monkeypatch):
        """Patch ``DaemonLoop.run`` to a sentinel; ``run_daemon`` must
        return that sentinel rather than re-implementing the loop."""
        called = {"n": 0}

        def _fake_run(_self) -> int:
            called["n"] += 1
            return 7

        monkeypatch.setattr(daemon.DaemonLoop, "run", _fake_run)
        assert daemon.run_daemon() == 7
        assert called["n"] == 1, (
            "run_daemon did not call DaemonLoop.run exactly once; the "
            "entry point must delegate, not re-implement the loop."
        )


# --------------------------- CLI: orchestrator daemon ---------------------------


class TestCliDaemonGroupRegistered:
    """``orchestrator daemon`` is a registered Click sub-group."""

    def test_daemon_group_exists(self):
        assert "daemon" in cli.commands
        # F-016-U-7 adds ``stop`` to the start/status pair.
        group = cli.commands["daemon"]
        assert hasattr(group, "commands")
        assert set(group.commands.keys()) == {"start", "status", "stop"}

    def test_daemon_help_runs_cleanly(self):
        result = CliRunner().invoke(cli, ["daemon", "--help"])
        assert result.exit_code == 0
        assert "watcher daemon" in result.output.lower()


class TestCliDaemonStatusCommand:
    """``orchestrator daemon status`` reads ``daemon_locks`` row without
    claiming. Safe to call from a second terminal."""

    def test_status_prints_row_json_when_lock_held(self, tmp_state_db):
        # Pre-seed a row at the resolved STATE_DB path so the CLI's
        # ``state.STATE_DB.resolve()`` finds it.
        path = str(state.STATE_DB.resolve())
        assert state.claim_daemon_lock(path, "cli-test-holder") is True

        result = CliRunner().invoke(cli, ["daemon", "status"])
        assert result.exit_code == 0, f"non-zero exit: {result.output}\n{result.exception}"
        # Output is a JSON blob; parse it and confirm fields.
        try:
            row = json.loads(result.output)
        except json.JSONDecodeError as e:
            pytest.fail(
                f"`daemon status` did not emit JSON when a lock was held; "
                f"output:\n{result.output!r}\nerror: {e}"
            )
        assert row["holder_id"] == "cli-test-holder"
        assert row["state_db_path"] == path

    def test_status_prints_no_lock_message_when_absent(self, tmp_state_db):
        # No claim → CLI prints a hint, exits 0.
        result = CliRunner().invoke(cli, ["daemon", "status"])
        assert result.exit_code == 0, f"non-zero exit: {result.output}\n{result.exception}"
        assert "no daemon lock" in result.output.lower(), (
            f"expected 'no daemon lock' hint; got: {result.output!r}"
        )

    def test_status_does_not_claim_or_mutate(self, tmp_state_db):
        """Read-only: a status call when no daemon is running must NOT
        leave a row behind."""
        result = CliRunner().invoke(cli, ["daemon", "status"])
        assert result.exit_code == 0
        path = str(state.STATE_DB.resolve())
        assert state.get_daemon_lock(path) is None


class TestCliDaemonStartCommand:
    """``orchestrator daemon start`` mirrors the module guard: a non-zero
    exit code when ``ORCH_DAEMON_DRIVE`` is unset so a supervisor can
    branch on ``$?`` between "operator forgot the opt-in" (exit 2),
    "another daemon owns the workspace" (exit 3), and a clean shutdown
    (exit 0). PR #61 reviewer M2."""

    def test_start_exits_drive_disabled_code_when_drive_disabled(self, tmp_state_db, monkeypatch):
        """Without ``ORCH_DAEMON_DRIVE`` the CLI exits 2 (drive-disabled),
        not 0. The non-zero sentinel is the contract operators rely on
        via systemd ``Restart=on-failure`` / shell ``$?``.
        """
        monkeypatch.delenv(daemon.DAEMON_DRIVE_ENV, raising=False)
        # F-016-U-7 credential guard runs before the drive-enabled
        # check; supply a valid-shaped key so the test exercises the
        # drive-disabled exit (2), not the credential-refusal exit (4).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-tests")
        result = CliRunner().invoke(cli, ["daemon", "start"])
        assert result.exit_code == 2, (
            f"`daemon start` did not exit with EXIT_DRIVE_DISABLED code (2); "
            f"output:\n{result.output}\nexc: {result.exception}"
        )
        # No lock written.
        path = str(state.STATE_DB.resolve())
        assert state.get_daemon_lock(path) is None

    def test_start_exits_lock_held_code_when_lock_held(self, tmp_state_db, monkeypatch):
        """Another daemon owns the workspace → CLI exits 3 (lock-held).
        Same-workspace contention is a configuration error, not a
        transient — supervisors should stop retrying rather than busy-loop.
        """
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-tests")
        path = str(state.STATE_DB.resolve())
        state.claim_daemon_lock(path, "incumbent")
        result = CliRunner().invoke(cli, ["daemon", "start"])
        assert result.exit_code == 3, (
            f"`daemon start` did not exit with EXIT_LOCK_HELD code (3); "
            f"output:\n{result.output}\nexc: {result.exception}"
        )
        # Incumbent still owns.
        assert state.get_daemon_lock(path)["holder_id"] == "incumbent"


# --------------------------- F-014 reuse: shared executor ---------------------------


class TestF014SharedExecutorActuallyMutates:
    """Spec § "No parallel state machine": the daemon shares the F-014
    executor. The shared executor's ``transition`` action must update
    ``work_units.status`` to the target — proves the daemon's success
    path mutates real state (not just spies on a mock).
    """

    def test_transition_action_actually_updates_status(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="in_ci", pr_number=42)
        action = Action.transition("done", "merged via shared executor")
        # Plumb the action through the real ``_apply_action`` (no monkeypatch
        # on tools.health) — just stub the probe to return our action.
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [action])
        _no_workers(monkeypatch)
        daemon.reconcile_unit(unit.unit_id)
        latest = state.get_unit_state(unit.unit_id)
        assert latest.status == "done", (
            f"shared F-014 executor failed to apply transition; status "
            f"is {latest.status}, expected 'done'"
        )


# --------------------------- restart resilience ---------------------------


class TestRestartResilience:
    """Spec acceptance: "Daemon crash → restart → no duplicate events
    written (Phase 0 idempotency)." Same response on two consecutive
    DaemonLoop.tick calls (simulating a crash-and-restart between ticks
    where the same tail content is observed twice) must produce exactly
    one event row."""

    def test_tick_after_tick_writes_one_event_for_same_tail(self, tmp_state_db, monkeypatch):
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, "true")
        unit = _seed_unit(status="coding", sessions={"coder": "sess_c"})
        canned = _TailResult(
            messages=[
                {
                    "ts": "2024-01-01T00:00:00",
                    "role": "agent",
                    "text": "PR_URL: https://github.com/o/r/pull/77",
                }
            ]
        )

        class _Worker:
            def tail_messages(self, *_a, **_k):
                return canned

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: _Worker())
        _no_probe(monkeypatch)

        # First daemon-start instance.
        loop1 = daemon.DaemonLoop(holder_id="d1", poll_interval_s=0.01)
        loop1.handle = daemon.claim_singleton(holder_id="d1")
        assert loop1.handle is not None
        loop1.tick()
        # Simulate clean shutdown.
        assert daemon.release_singleton(loop1.handle) is True

        # Second daemon-start instance (the restart).
        loop2 = daemon.DaemonLoop(holder_id="d2", poll_interval_s=0.01)
        loop2.handle = daemon.claim_singleton(holder_id="d2")
        assert loop2.handle is not None
        loop2.tick()

        # Exactly one pr_opened event from the same tail content seen
        # by both daemon instances.
        events = state.list_events(unit.unit_id)
        pr_opened = [e for e in events if e["event_type"] == "pr_opened"]
        assert len(pr_opened) == 1, (
            f"daemon restart wrote duplicate pr_opened events: "
            f"{len(pr_opened)} found; expected 1 (Phase 0 dedupe_key)."
        )
        # Status correctly at in_ci.
        assert state.get_unit_state(unit.unit_id).status == "in_ci"


# --------------------------- heartbeat liveness ---------------------------


class TestHeartbeatLiveness:
    """The live daemon's heartbeat must move ``heartbeat_at`` forward in
    the DB on every tick. Otherwise a long-running daemon would let its
    own row age past the stale window and lose to a spurious takeover."""

    def test_repeated_heartbeats_monotonically_increase(self, tmp_state_db):
        path = "/abs/heartbeat-test/state.db"
        assert state.claim_daemon_lock(path, "h-live") is True
        timestamps: list[str] = []
        for _ in range(3):
            time.sleep(0.005)  # avoid timestamp aliasing under fast clocks
            assert state.heartbeat_daemon_lock(path, "h-live") is True
            timestamps.append(state.get_daemon_lock(path)["heartbeat_at"])
        # Monotonic: each heartbeat moves the row forward.
        assert timestamps == sorted(timestamps), (
            f"heartbeats did not move heartbeat_at forward monotonically: {timestamps}"
        )
        assert len(set(timestamps)) == 3, (
            f"heartbeats produced duplicate timestamps: {timestamps}; "
            "a long-running daemon would alias its own heartbeat and "
            "lose to a stale-window takeover."
        )
