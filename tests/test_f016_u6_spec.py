"""F-016-U-6 — Phase 4: lead becomes pure dispatcher (spec tests).

Pins the spec-acceptance behaviour from
``features/F-016/spec.md`` § Phase 4 and the proposal's per-phase
acceptance criteria
(``docs/PROPOSAL-async-orchestrator.md`` § "Phase 4 — Lead becomes
pure dispatcher"):

  * **``cycle_review`` defaults to non-blocking when ``NTFY_TOPIC`` is
    set.** Spec § "Decisions": "Default-flip gated on NTFY_TOPIC".
    With a feedback channel, the lead's standard flow stops blocking
    — ``cycle_review`` returns in ≤2s and the daemon takes over.
    Without a feedback channel, blocking-but-nudge is a better UX
    than a silent ≤1s return (the user has no way to learn the work
    finished).

  * **``cycle_review`` stays blocking + nudges when ``NTFY_TOPIC``
    is unset.** Proposal § "Phase 4": "the lead emits a one-time
    setup nudge in chat". The nudge MUST appear in the response so
    a lead persona can surface it to the user; without it, the user
    never learns they could be running non-blocking.

  * **Explicit ``cycle_review_async`` / ``cycle_review_blocking``
    always available.** Proposal § "Tool semantics change (additive
    — old behavior reachable via new explicit names)". An operator
    who wants the opposite of the env-derived default must be able
    to ask for it explicitly without first editing ``.env``.

  * **``cycle_review_async`` returns in ≤2s.** Proposal § "Phase 4
    acceptance": "``cycle_review`` p95 latency < 2 seconds." The
    async path must NOT do any long-running work inline — no
    ``_run_*_advance`` calls, no worker waits, no full F-014 probe
    of the unit.

  * **``cycle_review_async`` nudges when the daemon is not running.**
    Risk R1 in the proposal: "If the daemon dies, units freeze until
    it restarts." A handoff to a dead daemon would silently strand
    the unit; surface the failure so the lead can run
    ``cycle_review_blocking`` as the fallback (or start the daemon).

  * **``cycle_review_blocking`` calls the same engine.** Spec
    § "Constraints" (3): "No parallel state machine.
    ``cycle_review_blocking`` and the daemon call the *same*
    ``derive_next_action`` + ``execute`` engine." The blocking path
    must NOT carry its own transition table — it shares
    ``_run_tester_advance`` / ``_run_reviewer_advance`` /
    ``_run_terminal_advance`` with ``cycle_review`` and the
    ``advance_to_*`` phase commands (proposal § "calls the daemon's
    engine in-process so there is no duplicate transition table").
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green for tests in this module."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _setup_feature(feature_id="F-001", unit_id="F-016-U-6-T", repo="https://github.com/o/r"):
    """Seed a minimal feature + approved plan."""
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=repo,
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=unit_id,
                feature_id=feature_id,
                title="u1",
                description="impl this",
            )
        ],
    )
    state.approve_plan(feature_id)


def _seed_coded_unit(unit_id="F-016-U-6-T", feature_id="F-001"):
    """Set up a feature + unit that already has a PR (coder ran successfully)."""
    _setup_feature(feature_id=feature_id, unit_id=unit_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="in_ci",
            branch="feat/branch",
            pr_number=5,
            coder_session_id="sesn-c",
        )
    )


def _seed_daemon_lock(holder_id="test-holder", *, heartbeat_age_s: float = 0.0) -> None:
    """Write a fresh ``daemon_locks`` row so ``cycle_review_async`` sees a
    live daemon. ``heartbeat_age_s`` lets a test simulate a stale daemon
    by inserting then ageing the heartbeat row in-place."""
    from pathlib import Path

    path = str(Path(state.STATE_DB).resolve())
    state.claim_daemon_lock(path, holder_id)
    if heartbeat_age_s > 0.0:
        past = (datetime.now(UTC) - timedelta(seconds=heartbeat_age_s)).isoformat()
        with state._connect() as conn:
            conn.execute(
                "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ?",
                (past, path),
            )


def _stub_github(monkeypatch):
    """Patch github.* helpers to no-op stubs."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.request_copilot_review",
        lambda *a, **k: {"requested": True, "status_code": 201, "note": ""},
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.wait_for_copilot_review",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda *a, **k: True,
    )


# --------------------------- cycle_review_async (≤2s) ---------------------------


class TestCycleReviewAsync:
    """Spec § Phase 4: ``cycle_review_async`` returns in ≤2s, hands off to
    the watcher daemon. No worker waits, no full F-014 probe."""

    def test_returns_delivered_when_daemon_is_running(self, tmp_state_db, with_github_token):
        _seed_coded_unit()
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["mode"] == "async_daemon"
        assert parsed["daemon"]["running"] is True
        assert parsed["status"] == "in_ci"

    def test_returns_fast(self, tmp_state_db, with_github_token):
        """``cycle_review_async`` MUST NOT block; the spec acceptance is
        p95 < 2s on the dispatcher. Even on a slow box, well under 2s."""
        _seed_coded_unit()
        _seed_daemon_lock()
        start = time.monotonic()
        execution.cycle_review_async("F-001", "F-016-U-6-T")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, (
            f"cycle_review_async took {elapsed:.2f}s; spec § Phase 4 acceptance: p95 < 2 seconds."
        )

    def test_does_not_call_run_advance_helpers(self, tmp_state_db, with_github_token, monkeypatch):
        """The async path is a handoff — the daemon does the work. The
        blocking ``_run_*_advance`` helpers MUST NOT fire inline (they
        would defeat the spec's "≤2s, daemon-driven" property)."""
        _seed_coded_unit()
        _seed_daemon_lock()
        called: list[str] = []
        monkeypatch.setattr(
            execution,
            "_run_tester_advance",
            lambda *a, **k: called.append("tester") or (True, None),
        )
        monkeypatch.setattr(
            execution,
            "_run_reviewer_advance",
            lambda *a, **k: called.append("reviewer") or (True, None, "REVIEW_RECOMMEND_MERGE"),
        )
        monkeypatch.setattr(
            execution,
            "_run_terminal_advance",
            lambda *a, **k: called.append("terminal") or (True, None),
        )
        execution.cycle_review_async("F-001", "F-016-U-6-T")
        assert called == [], (
            "cycle_review_async invoked the blocking phase helpers; the "
            "async handoff must not run them inline."
        )

    def test_daemon_not_running_returns_undelivered_with_nudge(
        self, tmp_state_db, with_github_token
    ):
        """Risk R1: a handoff to a dead daemon strands the unit. Surface
        the failure with a next-step that mentions the blocking fallback
        or starting the daemon."""
        _seed_coded_unit()
        # No daemon_locks row seeded.
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["daemon"]["running"] is False
        next_steps = " ".join(parsed.get("next_steps") or []).lower()
        assert "daemon" in next_steps, f"next_steps must mention the daemon; got {next_steps!r}"
        assert "cycle_review_blocking" in next_steps or "blocking" in next_steps, (
            f"next_steps must point at the blocking fallback; got {next_steps!r}"
        )

    def test_stale_daemon_heartbeat_treated_as_not_running(self, tmp_state_db, with_github_token):
        """A lock row whose heartbeat is older than ``DEFAULT_DAEMON_LOCK_STALE_AFTER_S``
        is a crashed daemon — the row exists but the holder isn't ticking.
        Treating that as "running" would silently strand the unit (Risk R1)."""
        _seed_coded_unit()
        _seed_daemon_lock(heartbeat_age_s=state.DEFAULT_DAEMON_LOCK_STALE_AFTER_S + 10)
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["daemon"]["running"] is False

    def test_unit_not_found(self, tmp_state_db, with_github_token):
        _setup_feature()
        out = execution.cycle_review_async("F-001", "F-001-U-NOPE")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "unit_not_found"

    def test_unit_already_terminal(self, tmp_state_db, with_github_token):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-6-T",
                feature_id="F-001",
                status="done",
                pr_number=5,
            )
        )
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "unit_terminal"
        assert parsed["status"] == "done"

    def test_unit_cancelled(self, tmp_state_db, with_github_token):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-6-T",
                feature_id="F-001",
                status="cancelled",
                pr_number=5,
            )
        )
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "unit_terminal"

    def test_unit_awaiting_merge_still_delivered(self, tmp_state_db, with_github_token):
        """A unit in ``approved_awaiting_merge`` is NOT terminal — the
        daemon's F-014 probe still has work (flip to ``done`` after the
        human merges). Async handoff must be honored."""
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-6-T",
                feature_id="F-001",
                status="approved_awaiting_merge",
                pr_number=5,
            )
        )
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["status"] == "approved_awaiting_merge"


# --------------------------- cycle_review_blocking ---------------------------


class TestCycleReviewBlocking:
    """Explicit blocking variant — today's pre-Phase-4 ``cycle_review``
    behavior. Always available regardless of ``NTFY_TOPIC``."""

    def test_happy_path_no_cycles(
        self, tmp_state_db, with_github_token, monkeypatch, with_ntfy_topic
    ):
        """Even with NTFY_TOPIC set, the explicit blocking variant blocks
        and runs the full pipeline."""
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS", "session_id": "t"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "r"}
            ),
        )
        _stub_github(monkeypatch)
        out = execution.cycle_review_blocking("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

    def test_shares_engine_with_advance_helpers(self, tmp_state_db, with_github_token, monkeypatch):
        """Spec § "No parallel state machine": ``cycle_review_blocking``
        and the daemon call the SAME engine. Patching the shared
        ``_run_*_advance`` helpers to sentinels MUST observe them being
        called — proves the blocking variant delegates rather than
        carrying its own transition table."""
        _seed_coded_unit()
        called: list[str] = []

        def fake_tester(ctx):
            called.append("tester")
            return True, None

        def fake_reviewer(ctx):
            called.append("reviewer")
            return True, None, "REVIEW_RECOMMEND_MERGE"

        def fake_terminal(ctx, outcome):
            called.append("terminal")
            return True, None

        monkeypatch.setattr(execution, "_run_tester_advance", fake_tester)
        monkeypatch.setattr(execution, "_run_reviewer_advance", fake_reviewer)
        monkeypatch.setattr(execution, "_run_terminal_advance", fake_terminal)
        _stub_github(monkeypatch)
        execution.cycle_review_blocking("F-001", "F-016-U-6-T")
        assert called == ["tester", "reviewer", "terminal"], (
            f"cycle_review_blocking did not delegate to the shared engine; "
            f"called={called}. Spec § 'No parallel state machine' violated."
        )

    def test_tester_blocked_escalates(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: "BLOCKED — tester for U: spec ambiguous",
        )
        _stub_github(monkeypatch)
        out = execution.cycle_review_blocking("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"


# --------------------------- cycle_review dispatcher ---------------------------


class TestCycleReviewDispatcher:
    """The default ``cycle_review`` tool dispatches on ``NTFY_TOPIC``.

    Spec § "Decisions": "Default-flip gated on NTFY_TOPIC".
    Proposal § "Phase 4": ``NTFY_TOPIC`` set → daemon mode; unset →
    blocking + nudge."""

    def test_ntfy_set_routes_to_async(
        self, tmp_state_db, with_github_token, monkeypatch, with_ntfy_topic
    ):
        """With NTFY_TOPIC set AND the daemon running, ``cycle_review``
        MUST delegate to ``cycle_review_async``. The blocking helpers
        MUST NOT fire."""
        _seed_coded_unit()
        _seed_daemon_lock()
        blocking_called: list[bool] = []
        monkeypatch.setattr(
            execution,
            "_run_tester_advance",
            lambda *a, **k: blocking_called.append(True) or (True, None),
        )
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed.get("mode") == "async_daemon", (
            "cycle_review with NTFY_TOPIC set + daemon running did not route to async mode"
        )
        assert blocking_called == [], (
            "cycle_review with NTFY_TOPIC set + daemon running fell through to the blocking "
            "phase helpers; spec § 'Default-flip gated on NTFY_TOPIC' violated."
        )

    def test_ntfy_set_but_daemon_down_falls_back_to_blocking(
        self, tmp_state_db, with_github_token, monkeypatch, with_ntfy_topic
    ):
        """Risk R1 safety net: NTFY set but the watcher daemon isn't
        running. Routing to async would silently strand the unit, so
        the dispatcher must fall back to blocking and surface a
        daemon-not-running warning."""
        _seed_coded_unit()
        # No daemon_locks row seeded.
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS", "session_id": "t"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "r"}
            ),
        )
        _stub_github(monkeypatch)
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed.get("outcome") == "approved_awaiting_merge", (
            "dispatcher did not fall back to blocking when daemon was down"
        )
        nudge_text = (parsed.get("nudge") or "").lower()
        assert "daemon" in nudge_text, (
            f"fallback nudge must mention the daemon; got nudge={nudge_text!r}"
        )

    def test_ntfy_unset_routes_to_blocking_with_nudge(
        self, tmp_state_db, with_github_token, monkeypatch, no_ntfy_topic
    ):
        """Without NTFY_TOPIC, the dispatcher blocks AND surfaces a one-
        line nudge directing the user at the env var. Without the nudge
        the user never learns the non-blocking mode exists."""
        _seed_coded_unit()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS", "session_id": "t"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "r"}
            ),
        )
        _stub_github(monkeypatch)
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        # Blocking ran (status flipped to approved_awaiting_merge).
        assert parsed["outcome"] == "approved_awaiting_merge"
        # Nudge is present somewhere in the payload.
        nudge_text = json.dumps(parsed).lower()
        assert "ntfy_topic" in nudge_text, (
            "cycle_review (NTFY unset) response missing NTFY_TOPIC nudge; "
            "proposal § Phase 4 requires a one-time setup nudge."
        )

    def test_ntfy_unset_does_not_call_async(
        self, tmp_state_db, with_github_token, monkeypatch, no_ntfy_topic
    ):
        """Defensive: with NTFY_TOPIC unset, the dispatcher MUST NOT
        silently fall through to async (which would return ≤1s with no
        user-facing notification path)."""
        _seed_coded_unit()
        async_called: list[bool] = []
        monkeypatch.setattr(
            execution,
            "cycle_review_async",
            lambda *a, **k: (
                async_called.append(True) or json.dumps({"delivered": True, "mode": "async_daemon"})
            ),
        )
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS", "session_id": "t"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "r"}
            ),
        )
        _stub_github(monkeypatch)
        execution.cycle_review("F-001", "F-016-U-6-T")
        assert async_called == [], (
            "cycle_review (NTFY unset) routed to async; spec § "
            "'Default-flip gated on NTFY_TOPIC' violated."
        )


# --------------------------- registration / availability ---------------------------


class TestExplicitVariantsAlwaysExist:
    """Spec § "Phase 4": "explicit ``cycle_review_async`` /
    ``cycle_review_blocking`` always available". An operator must be
    able to opt into either path regardless of the env-derived default
    — both names live as MCP-registered tools on the module."""

    def test_async_callable_at_module_scope(self):
        assert callable(execution.cycle_review_async)

    def test_blocking_callable_at_module_scope(self):
        assert callable(execution.cycle_review_blocking)


# --------------------------- PR #64 reviewer regressions ---------------------------


class TestAsyncHandoffMessageIsHonest:
    """Reviewer H1: the async-handoff response previously claimed
    "The daemon will drive the unit to terminal and ntfy will push on
    completion." The U-5 daemon (``orchestrator.daemon``) only observes
    existing worker sessions and applies F-014 PR-merge transitions; it
    does NOT spawn the next-phase worker, so a unit handed off after the
    coder finishes strands in ``in_ci`` until something kicks the
    pipeline forward. The honest message tells the operator that and
    points them at ``advance_to_*`` / ``cycle_review_blocking`` for
    actual progression."""

    def test_handoff_message_does_not_promise_terminal_drive(self, tmp_state_db, with_github_token):
        _seed_coded_unit()
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        msg = parsed["message"]
        assert "drive the unit to terminal" not in msg, (
            f"async-handoff message claims terminal drive (reviewer H1); got: {msg!r}"
        )

    def test_handoff_message_points_at_advance_or_blocking(self, tmp_state_db, with_github_token):
        _seed_coded_unit()
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        msg = parsed["message"].lower()
        assert "advance_to_tester" in msg or "cycle_review_blocking" in msg, (
            f"async-handoff message must direct the user at the actual "
            f"forward path; got: {parsed['message']!r}"
        )

    def test_handoff_message_names_u7_for_full_drive(self, tmp_state_db, with_github_token):
        """The U-7 reference tells the operator when end-to-end drive
        actually lands; without it the message reads like a permanent
        limitation."""
        _seed_coded_unit()
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert "U-7" in parsed["message"] or "F-016-U-7" in parsed["message"], (
            f"async-handoff message must reference U-7 (the unit that lands "
            f"full daemon drive); got: {parsed['message']!r}"
        )


class TestDispatcherCallsDaemonHealthOnce:
    """Reviewer M1: the dispatcher and ``cycle_review_async`` each used
    to call ``_daemon_health`` independently, doubling SQLite reads on
    the ≤2 s p95 path AND opening a TOCTOU window where the two reads
    could disagree about the daemon's liveness. One call per dispatcher
    entry; the snapshot is threaded through the impl helper."""

    def test_ntfy_set_path_calls_daemon_health_exactly_once(
        self, tmp_state_db, with_github_token, monkeypatch, with_ntfy_topic
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        original = execution._daemon_health
        calls: list[int] = []

        def counting_health():
            calls.append(1)
            return original()

        monkeypatch.setattr(execution, "_daemon_health", counting_health)
        execution.cycle_review("F-001", "F-016-U-6-T")
        assert sum(calls) == 1, (
            f"cycle_review with NTFY set should call _daemon_health "
            f"exactly once per dispatch; got {sum(calls)} (reviewer M1)."
        )


class TestDispatcherShortCircuitsOnVerificationError:
    """Reviewer M2: the dispatcher prepended the NTFY nudge to the
    blocking variant's non-JSON ``ERROR: target repo … not verified``
    string. The verification ERROR is the real blocker; the NTFY nudge
    is unrelated noise that confuses the user about which message to
    act on. Verification short-circuits at the dispatcher entry, before
    any nudge logic runs — matching the NTFY+daemon path which already
    returned the ERROR verbatim (line 2870 in ``cycle_review_async``'s
    entry guard)."""

    def _seed_unverified(self):
        unverified = "https://github.com/never/verified"
        state.save_feature(
            Feature(
                id="F-001",
                title="t",
                description="d",
                repo_path=unverified,
                status="approved",
            )
        )
        state.save_plan(
            "F-001",
            [
                WorkUnit(
                    id="F-016-U-6-T",
                    feature_id="F-001",
                    title="u1",
                    description="impl this",
                ),
            ],
        )
        state.approve_plan("F-001")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-6-T",
                feature_id="F-001",
                status="in_ci",
                branch="b",
                pr_number=5,
                coder_session_id="sesn-c",
            )
        )

    def test_unverified_repo_returns_error_without_nudge(
        self, tmp_state_db, with_github_token, monkeypatch, no_ntfy_topic
    ):
        self._seed_unverified()
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        # The verification gate's canonical ERROR is what the lead should
        # surface; the NTFY nudge must NOT be prepended or appended to it.
        assert "ERROR" in out and "not verified" in out
        assert "NTFY_TOPIC" not in out, (
            f"dispatcher prepended the NTFY nudge to an unrelated "
            f"verification ERROR (reviewer M2); got: {out[:400]!r}"
        )

    def test_dispatcher_skips_blocking_path_redundant_verify(
        self, tmp_state_db, with_github_token, monkeypatch, no_ntfy_topic
    ):
        """Reviewer M2 follow-through: the dispatcher already verified at
        its own entry, so re-verifying inside the blocking call is
        wasted SQLite work. The dispatcher routes through the
        ``_cycle_review_blocking_impl`` private helper that skips the
        gate — patching ``ensure_verified_for_feature`` to a counter and
        invoking the verified-repo happy path must show exactly ONE
        verify call (the dispatcher's own)."""
        _seed_coded_unit()
        from orchestrator.tools import (
            execution as execution_mod,
        )

        verify_calls: list[str] = []
        original = execution_mod.ensure_verified_for_feature

        def counting_verify(fid):
            verify_calls.append(fid)
            return original(fid)

        monkeypatch.setattr(execution_mod, "ensure_verified_for_feature", counting_verify)
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS", "session_id": "t"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "r"}
            ),
        )
        _stub_github(monkeypatch)
        execution.cycle_review("F-001", "F-016-U-6-T")
        assert verify_calls == ["F-001"], (
            f"dispatcher should verify exactly once per call; got "
            f"{verify_calls} (reviewer M2 follow-through)."
        )

    def test_blocking_mcp_tool_still_verifies(self, tmp_state_db, with_github_token, no_ntfy_topic):
        """The public ``cycle_review_blocking`` MCP tool must KEEP its
        own verification gate — direct callers (operators using the
        explicit blocking variant from the lead chat) don't go through
        the dispatcher's entry-gate. Refusing to verify here would
        regress the branch-protection policy on a public MCP surface."""
        self._seed_unverified()
        out = execution.cycle_review_blocking("F-001", "F-016-U-6-T")
        assert "ERROR" in out and "not verified" in out, (
            "cycle_review_blocking lost its verification gate; the "
            "explicit blocking variant is a public MCP surface and must "
            "still enforce verify_repo (reviewer M2 boundary)."
        )


class TestNudgesAreModuleConstants:
    """Reviewer M3: the daemon-down nudge was a literal string buried
    inside the dispatcher body while its peer ``_CYCLE_REVIEW_NTFY_NUDGE``
    was a module constant with a docstring. Asymmetric for two strings
    with identical lifecycle (one-time setup hints) and identical
    surface (the ``nudge`` JSON field). Both promoted to module
    constants so tests can reference them by name."""

    def test_daemon_down_nudge_is_module_constant(self):
        assert hasattr(execution, "_CYCLE_REVIEW_DAEMON_DOWN_NUDGE"), (
            "daemon-down nudge must be a module-level constant peer to "
            "_CYCLE_REVIEW_NTFY_NUDGE (reviewer M3)."
        )
        nudge = execution._CYCLE_REVIEW_DAEMON_DOWN_NUDGE
        assert "daemon" in nudge.lower()

    def test_dispatcher_uses_module_constant_for_daemon_down(
        self, tmp_state_db, with_github_token, monkeypatch, with_ntfy_topic
    ):
        """The dispatcher emits the module constant verbatim — patching
        the constant must change the user-visible nudge."""
        _seed_coded_unit()
        # No daemon_locks row seeded → daemon down branch.
        monkeypatch.setattr(
            execution,
            "_CYCLE_REVIEW_DAEMON_DOWN_NUDGE",
            "SENTINEL daemon-down nudge for reviewer M3 lockdown",
        )
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS", "session_id": "t"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE", "session_id": "r"}
            ),
        )
        _stub_github(monkeypatch)
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        assert "SENTINEL daemon-down nudge for reviewer M3 lockdown" in out
