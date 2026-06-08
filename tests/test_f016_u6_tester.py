"""F-016-U-6 — Phase 4: lead becomes pure dispatcher (tester-extra coverage).

The coder shipped ``tests/test_f016_u6_spec.py`` with 18 spec-acceptance
points covering the happy paths + the NTFY routing logic. This file
adds the tester-side coverage gaps the spec also pins:

  * **Verification gate is enforced** by all three tools. F-016's spec
    explicitly leans on the existing verification gate; the runtime
    persona (``CLAUDE.md`` § "Verification gating") lists
    ``cycle_review`` as one of the gated spawn surfaces. The async and
    blocking variants must NOT bypass it.

  * **All three tools register as MCP tools.** Proposal § "Tool
    semantics change (additive — old behavior reachable via new
    explicit names)": ``cycle_review`` / ``cycle_review_async`` /
    ``cycle_review_blocking`` must all be callable from the lead's MCP
    surface, otherwise the "always available" contract is broken at the
    integration boundary the lead actually uses.

  * **``cycle_review_async`` does NOT mutate state.** Spec § Phase 3
    describes the level-triggered design — the daemon does the state
    machine work, the dispatcher just hands off. An async handoff that
    silently writes a status flip or a ``unit_events`` row would
    duplicate the daemon's work and risk Phase 0 dedupe collisions.

  * **``_daemon_health`` shape is well-formed across the failure
    modes.** The dispatcher routes on ``running`` and the lead persona
    surfaces ``daemon_info`` to the user — wrong shape on a missing /
    stale / corrupted row degrades the operator's mental model of the
    workspace.

  * **Dispatcher does NOT fall back to blocking when async returns
    ``delivered: false`` for a non-daemon reason** (unit terminal /
    cancelled / unit_not_found). The fallback nudge is specifically for
    the daemon-down case (Risk R1); other failure reasons must
    propagate verbatim so the lead sees the actual cause.

  * **``cycle_review_blocking`` does NOT consult daemon health.** The
    explicit opt-out must work even when the daemon is alive (so an
    operator can deliberately run blocking semantics for a single unit
    without stopping the daemon) and when the daemon is dead (so the
    Risk R1 fallback the dispatcher relies on actually exists).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, mcp

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green for tests in this module."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _setup_feature(
    feature_id: str = "F-001",
    unit_id: str = "F-016-U-6-T",
    repo: str = "https://github.com/o/r",
) -> None:
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


def _seed_coded_unit(
    unit_id: str = "F-016-U-6-T",
    feature_id: str = "F-001",
    status: str = "in_ci",
) -> None:
    _setup_feature(feature_id=feature_id, unit_id=unit_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="feat/branch",
            pr_number=5,
            coder_session_id="sesn-c",
        )
    )


def _seed_daemon_lock(
    holder_id: str = "test-holder",
    *,
    heartbeat_age_s: float = 0.0,
) -> None:
    path = str(Path(state.STATE_DB).resolve())
    state.claim_daemon_lock(path, holder_id)
    if heartbeat_age_s > 0.0:
        past = (datetime.now(UTC) - timedelta(seconds=heartbeat_age_s)).isoformat()
        with state._connect() as conn:
            conn.execute(
                "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ?",
                (past, path),
            )


def _stub_github(monkeypatch) -> None:
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
        "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
    )
    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True)


# ============================================================================
# MCP tool registration — proposal § "Tool semantics change (additive)"
# ============================================================================


class TestMCPRegistration:
    """All three names must register as MCP tools so the lead's MCP
    surface can call them. A symbol that exists at module scope but
    isn't ``@mcp.tool()`` decorated is invisible to the lead — the
    "always available" contract in the unit description is meaningless
    if the tool isn't on the wire."""

    @pytest.fixture
    def _registered_tool_names(self):
        import asyncio

        async def _go():
            tools = await mcp.list_tools()
            return {t.name for t in tools}

        return asyncio.run(_go())

    def test_cycle_review_registered(self, _registered_tool_names):
        assert "cycle_review" in _registered_tool_names

    def test_cycle_review_async_registered(self, _registered_tool_names):
        assert "cycle_review_async" in _registered_tool_names, (
            "cycle_review_async missing from MCP surface; proposal § "
            "'Tool semantics change' requires explicit variant be always-available"
        )

    def test_cycle_review_blocking_registered(self, _registered_tool_names):
        assert "cycle_review_blocking" in _registered_tool_names, (
            "cycle_review_blocking missing from MCP surface; proposal § "
            "'Tool semantics change' requires explicit variant be always-available"
        )


# ============================================================================
# Verification-gate enforcement — CLAUDE.md § "Verification gating"
# ============================================================================


class TestVerificationGate:
    """CLAUDE.md § "Verification gating (automatic — you don't call this)"
    lists ``cycle_review`` as one of the gated spawn surfaces. The new
    async + blocking explicit variants are part of the same surface; an
    operator who skips ``verify_repo`` shouldn't be able to side-step
    branch-protection enforcement by choosing a different cycle_review
    name."""

    def test_async_blocks_on_unverified_repo(self, tmp_state_db, with_github_token):
        _seed_coded_unit()
        _seed_daemon_lock()
        # Drop the pre-seeded verified row for this feature's repo.
        state.forget_verified_repo("https://github.com/o/r")
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        assert "ERROR" in out and "not verified" in out, (
            "cycle_review_async did not enforce the verification gate; "
            "CLAUDE.md § 'Verification gating' lists cycle_review as gated."
        )

    def test_blocking_blocks_on_unverified_repo(self, tmp_state_db, with_github_token):
        _seed_coded_unit()
        state.forget_verified_repo("https://github.com/o/r")
        out = execution.cycle_review_blocking("F-001", "F-016-U-6-T")
        assert "ERROR" in out and "not verified" in out, (
            "cycle_review_blocking did not enforce the verification gate; "
            "CLAUDE.md § 'Verification gating' lists cycle_review as gated."
        )

    def test_dispatcher_blocks_on_unverified_repo_ntfy_unset(
        self, tmp_state_db, with_github_token, no_ntfy_topic
    ):
        """NTFY unset → routes to blocking → blocking enforces gate."""
        _seed_coded_unit()
        state.forget_verified_repo("https://github.com/o/r")
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        assert "ERROR" in out and "not verified" in out

    def test_dispatcher_blocks_on_unverified_repo_ntfy_set(
        self, tmp_state_db, with_github_token, with_ntfy_topic
    ):
        """NTFY set + daemon up → routes to async → async enforces gate."""
        _seed_coded_unit()
        _seed_daemon_lock()
        state.forget_verified_repo("https://github.com/o/r")
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        assert "ERROR" in out and "not verified" in out


# ============================================================================
# Level-triggered handoff — cycle_review_async MUST NOT mutate state
# ============================================================================


class TestAsyncDoesNotMutateState:
    """Spec § Phase 3 + § "No parallel state machine": the daemon owns
    the state machine. The dispatcher hands off — it MUST NOT write the
    status flip itself, nor record events that would race the daemon's
    deduped per-tick writes."""

    def test_async_does_not_change_unit_status(self, tmp_state_db, with_github_token):
        _seed_coded_unit(status="in_ci")
        _seed_daemon_lock()
        before = state.get_unit_state("F-016-U-6-T").status
        execution.cycle_review_async("F-001", "F-016-U-6-T")
        after = state.get_unit_state("F-016-U-6-T").status
        assert after == before == "in_ci", (
            f"cycle_review_async mutated unit status {before!r} → {after!r}; "
            f"the dispatcher hand-off must be a pure read of state."
        )

    def test_async_does_not_record_events(self, tmp_state_db, with_github_token):
        _seed_coded_unit()
        _seed_daemon_lock()
        with state._connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM unit_events WHERE unit_id = ?",
                ("F-016-U-6-T",),
            ).fetchone()[0]
        execution.cycle_review_async("F-001", "F-016-U-6-T")
        with state._connect() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM unit_events WHERE unit_id = ?",
                ("F-016-U-6-T",),
            ).fetchone()[0]
        assert after == before, (
            f"cycle_review_async wrote {after - before} unit_events row(s); "
            f"the level-triggered design (spec § 'No parallel state machine') "
            f"requires only the daemon's tick to record marker / transition "
            f"events. A dispatcher write risks duplicating the daemon's work."
        )

    def test_async_does_not_call_record_event(self, tmp_state_db, with_github_token, monkeypatch):
        """Patch ``state.record_event`` and confirm the async handoff
        doesn't invoke it. Stronger than the row-count check — catches
        a future drift where the dispatcher writes a sentinel and
        immediately overwrites it."""
        _seed_coded_unit()
        _seed_daemon_lock()
        called: list[tuple] = []
        real_record = state.record_event

        def trap(*args, **kwargs):
            called.append((args, kwargs))
            return real_record(*args, **kwargs)

        monkeypatch.setattr("orchestrator.state.record_event", trap)
        execution.cycle_review_async("F-001", "F-016-U-6-T")
        assert called == [], (
            f"cycle_review_async called state.record_event {len(called)} time(s); "
            f"the dispatcher hand-off must be a pure read."
        )


# ============================================================================
# _daemon_health shape correctness — dispatcher routes on these fields
# ============================================================================


class TestDaemonHealthShape:
    """The dispatcher routes on ``daemon_info["running"]`` and the lead
    persona surfaces the rest of the shape to the user. A shape
    regression flips the routing decision silently."""

    def test_no_lock_row_returns_not_running(self, tmp_state_db):
        info = execution._daemon_health()
        assert info["running"] is False
        assert info["reason"] == "no_lock_holder"

    def test_fresh_lock_returns_running(self, tmp_state_db):
        _seed_daemon_lock()
        info = execution._daemon_health()
        assert info["running"] is True
        assert info["reason"] == "fresh_heartbeat"
        assert info["holder_id"] == "test-holder"
        assert "state_db_path" in info

    def test_just_under_stale_threshold_still_running(self, tmp_state_db):
        """The 30s boundary IS the spec contract for "alive". Right
        before the threshold the holder must still be alive — otherwise
        a slow tick under GC pressure looks dead and the dispatcher
        falls back to blocking on a perfectly healthy workspace."""
        _seed_daemon_lock(heartbeat_age_s=state.DEFAULT_DAEMON_LOCK_STALE_AFTER_S - 2)
        info = execution._daemon_health()
        assert info["running"] is True

    def test_just_over_stale_threshold_treated_as_dead(self, tmp_state_db):
        _seed_daemon_lock(heartbeat_age_s=state.DEFAULT_DAEMON_LOCK_STALE_AFTER_S + 5)
        info = execution._daemon_health()
        assert info["running"] is False
        assert info["reason"] == "stale_heartbeat"

    def test_invalid_heartbeat_treated_as_dead(self, tmp_state_db):
        """Corrupted ``heartbeat_at`` → conservative-dead. Matches
        ``state.claim_daemon_lock``'s posture: a row a new daemon could
        reclaim isn't "running" for dispatch purposes."""
        _seed_daemon_lock()
        path = str(Path(state.STATE_DB).resolve())
        with state._connect() as conn:
            conn.execute(
                "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ?",
                ("not-a-timestamp", path),
            )
        info = execution._daemon_health()
        assert info["running"] is False
        assert info["reason"] == "invalid_heartbeat"


# ============================================================================
# Dispatcher pass-through semantics for non-daemon failure reasons
# ============================================================================


class TestDispatcherPassThrough:
    """The Risk R1 fallback (daemon down → blocking) is specifically for
    the daemon failing. Other failure reasons in ``cycle_review_async``
    (unit not found, unit terminal, unit cancelled) must propagate
    verbatim — silently retrying via the blocking helpers would either
    double-charge the user (re-running the cycle on a terminal unit) or
    return a misleading "blocking succeeded" verdict on a non-existent
    unit."""

    def test_terminal_unit_passes_through_without_blocking_fallback(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
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
        blocking_called: list[bool] = []
        monkeypatch.setattr(
            execution,
            "_run_tester_advance",
            lambda *a, **k: blocking_called.append(True) or (True, None),
        )
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "unit_terminal"
        assert blocking_called == [], (
            "cycle_review fell back to blocking on a unit_terminal async result; "
            "the Risk R1 fallback is specifically for the daemon-down case."
        )

    def test_unit_not_found_passes_through_without_blocking_fallback(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _setup_feature()  # feature + plan, no unit_state row
        _seed_daemon_lock()
        blocking_called: list[bool] = []
        monkeypatch.setattr(
            execution,
            "_run_tester_advance",
            lambda *a, **k: blocking_called.append(True) or (True, None),
        )
        out = execution.cycle_review("F-001", "F-001-U-NOPE")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "unit_not_found"
        assert blocking_called == []


# ============================================================================
# cycle_review_blocking — explicit opt-out from default-flip
# ============================================================================


class TestBlockingIgnoresDaemonState:
    """Spec § Phase 4: "Explicit ``cycle_review_async`` /
    ``cycle_review_blocking`` always available". The opt-out path must
    work regardless of NTFY + daemon configuration:

      * NTFY set + daemon alive: operator deliberately wants blocking
        on this unit (e.g. they want the verdict in chat right now)
      * NTFY unset + daemon dead: the Risk R1 fallback path the
        dispatcher relies on
    """

    def test_blocking_runs_full_pipeline_with_ntfy_set_and_daemon_alive(
        self, tmp_state_db, with_github_token, monkeypatch, with_ntfy_topic
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
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

    def test_blocking_does_not_check_daemon_health(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """``cycle_review_blocking`` must NOT consult ``_daemon_health``
        — it's the opt-out path. Reading the lock is harmless today but
        couples the explicit blocking variant to a daemon detail the
        proposal says it doesn't need."""
        _seed_coded_unit()
        _seed_daemon_lock()
        health_calls: list[bool] = []
        monkeypatch.setattr(
            execution,
            "_daemon_health",
            lambda *a, **k: health_calls.append(True) or {"running": True},
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
        execution.cycle_review_blocking("F-001", "F-016-U-6-T")
        assert health_calls == [], (
            "cycle_review_blocking consulted _daemon_health; the explicit "
            "opt-out variant must be independent of daemon state."
        )


# ============================================================================
# Escalated unit handling — escalated is terminal, same as done
# ============================================================================


class TestEscalatedUnit:
    """``escalated`` is in :data:`TERMINAL_UNIT_STATUSES` alongside
    ``done``. The daemon's ``list_active_units`` deliberately excludes
    escalated rows — once a unit hits cap-3, the human owns the next
    move (per CLAUDE.md § "Cap-3 mechanics"). The async dispatcher must
    refuse rather than silently hand the dead unit to a daemon that
    won't drive it anyway."""

    def test_escalated_returns_unit_terminal(self, tmp_state_db, with_github_token):
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-6-T",
                feature_id="F-001",
                status="escalated",
                pr_number=5,
                last_error="cap-3 hit",
            )
        )
        _seed_daemon_lock()
        out = execution.cycle_review_async("F-001", "F-016-U-6-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is False
        assert parsed["reason"] == "unit_terminal"
        assert parsed["status"] == "escalated"


# ============================================================================
# Cross-check: scheduling._run_one routes through the blocking variant
# ============================================================================


class TestSchedulingRoutesThroughBlocking:
    """The coder's commit message: "scheduling._run_one routes through
    cycle_review_blocking explicitly so the parallel_units /
    parallel_units_global thread pool keeps its 'finish when the unit
    reaches terminal' contract regardless of how the default-flip is
    configured."

    Without this routing, an NTFY-set workspace would have every
    parallel-pool worker return ≤2 s (the async handoff), the pool would
    "complete", and the response JSON would report success while the
    daemon was still in mid-flight."""

    def test_run_one_uses_blocking_variant_not_dispatcher(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        from orchestrator.tools import scheduling

        _seed_coded_unit()
        _seed_daemon_lock()
        monkeypatch.setattr(
            scheduling,
            "spawn_unit",
            lambda f, u: json.dumps({"unit_id": u, "pr_url": "http://x", "pr_number": 5}),
        )
        dispatcher_calls: list[bool] = []
        blocking_calls: list[bool] = []

        monkeypatch.setattr(
            execution,
            "cycle_review",
            lambda f, u: (
                dispatcher_calls.append(True) or json.dumps({"outcome": "approved_awaiting_merge"})
            ),
        )
        monkeypatch.setattr(
            scheduling,
            "cycle_review_blocking",
            lambda f, u: (
                blocking_calls.append(True) or json.dumps({"outcome": "approved_awaiting_merge"})
            ),
        )
        scheduling._run_one("F-001", "F-016-U-6-T")
        assert blocking_calls == [True], (
            "scheduling._run_one did not call cycle_review_blocking; with "
            "NTFY_TOPIC set the thread pool would hand off to async and "
            "the parallel_units response would lie about completion."
        )
        assert dispatcher_calls == [], (
            "scheduling._run_one called the dispatcher; with NTFY_TOPIC set "
            "the dispatcher would route to async and the thread-pool contract "
            "breaks."
        )


# ============================================================================
# Dispatcher response shape — JSON-parseable for the lead persona
# ============================================================================


class TestDispatcherResponseShape:
    """Both the async and the blocking-with-nudge dispatcher paths
    return JSON that the lead persona can parse. The runtime lead is
    instructed to surface ``cycle_review`` output verbatim; a string-
    concat shape would force the persona into ad-hoc parsing."""

    def test_async_path_returns_json_object(self, tmp_state_db, with_github_token, with_ntfy_topic):
        _seed_coded_unit()
        _seed_daemon_lock()
        out = execution.cycle_review("F-001", "F-016-U-6-T")
        parsed = json.loads(out)  # would raise if not JSON
        assert isinstance(parsed, dict)
        assert parsed["mode"] == "async_daemon"

    def test_blocking_path_with_nudge_returns_json_object(
        self,
        tmp_state_db,
        with_github_token,
        no_ntfy_topic,
        monkeypatch,
    ):
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
        assert isinstance(parsed, dict)
        assert parsed["outcome"] == "approved_awaiting_merge"
        assert "nudge" in parsed


# ============================================================================
# Performance — dispatcher path is ≤2s end-to-end
# ============================================================================


class TestDispatcherFastPath:
    """Proposal § "Phase 4 acceptance": ``cycle_review`` p95 latency
    < 2 seconds. The coder's spec tests cover ``cycle_review_async``
    directly; this checks the dispatcher's NTFY+daemon path end-to-end
    so a future refactor that adds a hidden blocking-helper call on the
    happy path gets caught."""

    def test_dispatcher_async_path_under_2s(self, tmp_state_db, with_github_token, with_ntfy_topic):
        _seed_coded_unit()
        _seed_daemon_lock()
        start = time.monotonic()
        execution.cycle_review("F-001", "F-016-U-6-T")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, (
            f"cycle_review (NTFY+daemon path) took {elapsed:.2f}s; "
            f"proposal § 'Phase 4 acceptance': p95 < 2 seconds."
        )
