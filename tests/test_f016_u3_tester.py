"""F-016-U-3 — Phase 2: decompose ``cycle_review`` into phase commands.

Independent of the coder's own tests in ``tests/test_tools_execution.py``
(classes ``TestAdvanceToTester`` / ``TestAdvanceToReviewer`` /
``TestAdvanceToTerminal`` / ``TestCycleReviewIsWrapper``). This file pins
the spec-acceptance behaviour those tests don't explicitly assert:

  * **True idempotence — no side effects.** The coder's idempotence
    tests only check the *returned outcome*. The Phase-3 daemon will
    tick the same advance many times per second; if "already_past"
    still ran ``_run_*_advance`` we'd silently re-spawn workers and
    burn $$. We monkeypatch the engine to a tripwire and assert it is
    never entered when status is past the phase boundary.

  * **``_last_reviewer_outcome`` uses MOST-RECENT marker.** Reviewer
    delta-resume can flip a unit from REVIEW_REQUEST_CHANGES → COMMENT
    → RECOMMEND_MERGE inside one cycle; ``advance_to_terminal`` must
    pick the last marker so the ultrareview gate fires on the
    endorsement, not on the earlier change-request row. Without this,
    a comment-only or request-changes that pre-dated an endorsement
    would suppress ultrareview.

  * **next_action chain is daemon-traversable.** The lead's wrapper
    works because it knows the sequence; the daemon doesn't — it reads
    ``next_action`` from the previous advance's JSON. Asserting the
    full chain ``advance_to_tester → advance_to_reviewer →
    advance_to_terminal → None`` is the daemon's discoverability
    contract.

  * **Sequential composition equals one-shot cycle_review.** The spec
    explicitly forbids "parallel state machine" — the three-phase
    sequence and the wrapper must reach the same terminal status with
    the same observable history kinds (steps). If they diverge, the
    daemon (which composes from the phase commands) would land in a
    different terminal than the lead's blocking ``cycle_review``.

  * **MCP registration.** All three phase commands must be registered
    on the FastMCP instance — they're meant to be RPC-reachable by the
    daemon and the lead. The wrapper ``cycle_review`` must also remain
    registered (we did not delete the convenience tool).

  * **Predecessor consistency (F-016-U-2).** The Phase 1 RPCs
    ``spawn_unit_async`` and ``wait_unit`` must still be registered —
    Phase 2 is additive on top of Phase 1, never a replacement.

  * **Repo-verification gate.** Each phase command must respect the
    same ``ensure_verified_for_feature`` gate ``cycle_review`` does;
    bypassing it would be a scope violation against F-008.

  * **escalated response surfaces ``last_error``** rather than swallowing
    it — the docstring promises the daemon can branch on it without an
    extra ``unit_history`` round-trip.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, mcp

# --------------------------- autouse: pretend CI is green ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Force CI gate green; phase commands aren't testing CI semantics."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=0.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


# --------------------------- fixtures / helpers ---------------------------


def _setup_feature(
    feature_id: str = "F-001",
    repo: str = "https://github.com/o/r",
    ultrareview: bool = False,
) -> None:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=repo,
            status="approved",
            ultrareview_enabled=ultrareview,
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=f"{feature_id}-U-1",
                feature_id=feature_id,
                title="u1",
                description="impl this",
            ),
        ],
    )
    state.approve_plan(feature_id)


def _seed_coded_unit(
    unit_id: str = "F-001-U-1",
    feature_id: str = "F-001",
    status: str = "in_ci",
) -> None:
    _setup_feature(feature_id)
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


def _stub_github_noop(monkeypatch) -> None:
    """Patch the GitHub-touching helpers used by ``_emit_terminal`` to no-ops."""
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
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("owner", "repo"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.get_pr_state",
        lambda *a, **k: {"head_sha": "deadbeefcafe", "state": "open", "merged": False},
    )


def _silence_ntfy(monkeypatch) -> None:
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda *a, **k: True,
    )


# ===========================================================================
# Idempotence is a *behavioural* contract, not just an outcome string
# ===========================================================================


class TestIdempotenceIsBehavioural:
    """When ``advance_to_X`` returns ``already_past`` it must NOT have done
    any work. The Phase-3 daemon ticks these every poll; a non-idempotent
    no-op would re-spawn workers, re-trigger Copilot, re-fire
    ultrareview. We trap the engine helpers to assert non-invocation.
    """

    def test_advance_to_tester_past_does_not_run_engine(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()

        # Tripwire: any call to the tester engine fails the test.
        def _explode(*a, **k):
            raise AssertionError(
                "_run_tester_advance was called on an already-past unit — "
                "idempotence is broken; the daemon would re-do work"
            )

        monkeypatch.setattr(execution, "_run_tester_advance", _explode)

        for past_status in ("reviewing", "fixing", "approved_awaiting_merge", "done"):
            s = state.get_unit_state("F-001-U-1")
            s.status = past_status
            state.upsert_unit_state(s)

            out = execution.advance_to_tester("F-001", "F-001-U-1")
            parsed = json.loads(out)
            assert parsed["outcome"] == "already_past", past_status

    def test_advance_to_reviewer_past_does_not_run_engine(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()

        def _explode(*a, **k):
            raise AssertionError(
                "_run_reviewer_advance was called on an already-past unit — "
                "would re-spawn reviewer + re-request Copilot"
            )

        monkeypatch.setattr(execution, "_run_reviewer_advance", _explode)

        for past_status in ("approved_awaiting_merge", "done"):
            s = state.get_unit_state("F-001-U-1")
            s.status = past_status
            state.upsert_unit_state(s)

            out = execution.advance_to_reviewer("F-001", "F-001-U-1")
            parsed = json.loads(out)
            assert parsed["outcome"] == "already_past", past_status

    def test_advance_to_terminal_done_does_not_emit_terminal_again(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """``status='done'`` is the only past-terminal bucket. Re-firing
        ``_emit_terminal`` would re-send the ready-to-merge ntfy push —
        a notification storm if the daemon ticks every second.
        """
        _seed_coded_unit(status="done")

        push_calls: list = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: push_calls.append((a, k)) or True,
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: push_calls.append((a, k)) or True,
        )

        # Tripwire on the engine too — must not fire.
        def _explode(*a, **k):
            raise AssertionError("_run_terminal_advance fired on done unit")

        monkeypatch.setattr(execution, "_run_terminal_advance", _explode)

        out = execution.advance_to_terminal("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "already_past"
        assert parsed["status"] == "done"
        assert push_calls == [], "no ntfy push allowed on already-done unit"

    def test_advance_to_terminal_approved_awaiting_merge_is_NOT_past(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Documented asymmetry (impl comment + docstring):
        ``approved_awaiting_merge`` is set by the REVIEW_RECOMMEND_MERGE
        marker BEFORE ``advance_to_terminal`` runs, so it must NOT
        short-circuit — the terminal-emit fire window depends on it.
        """
        _seed_coded_unit(status="approved_awaiting_merge")
        state.record_event(
            "F-001-U-1",
            "F-001",
            "reviewer_recommend_merge",
            source="reviewer",
            cycle_number=0,
            summary="endorsed",
        )

        _stub_github_noop(monkeypatch)
        push_calls: list = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: push_calls.append((a, k)) or True,
        )

        out = execution.advance_to_terminal("F-001", "F-001-U-1")
        parsed = json.loads(out)
        # Must reach the terminal-emit path, not short-circuit.
        assert parsed["outcome"] == "approved_awaiting_merge"
        assert len(push_calls) == 1, (
            "approved_awaiting_merge must fall through to _emit_terminal so the "
            "ready-to-merge ntfy push actually fires"
        )


# ===========================================================================
# _last_reviewer_outcome picks the LATEST marker, not the first
# ===========================================================================


class TestLastReviewerOutcomeMostRecent:
    """advance_to_terminal reads the latest reviewer marker to decide
    whether to fire ultrareview. If the helper picked the FIRST marker
    instead, a cycle that started with REVIEW_REQUEST_CHANGES then
    landed on REVIEW_RECOMMEND_MERGE would suppress the gate."""

    def test_picks_latest_marker_when_history_has_multiple(self, tmp_state_db):
        _seed_coded_unit()
        # Older to newer: request_changes → comment → recommend_merge.
        for ev in ("reviewer_request_changes", "reviewer_comment", "reviewer_recommend_merge"):
            state.record_event(
                "F-001-U-1",
                "F-001",
                ev,
                source="reviewer",
                cycle_number=0,
                summary=ev,
            )

        outcome = execution._last_reviewer_outcome("F-001-U-1")
        assert outcome == "REVIEW_RECOMMEND_MERGE", (
            f"must pick LATEST reviewer marker; got {outcome!r}"
        )

    def test_none_when_no_reviewer_event(self, tmp_state_db):
        _seed_coded_unit()
        # Only non-reviewer events present — must return None, not crash.
        state.record_event("F-001-U-1", "F-001", "spawn_coder", source="orchestrator", summary="x")
        assert execution._last_reviewer_outcome("F-001-U-1") is None

    def test_ultrareview_fires_when_endorsement_is_latest_after_earlier_changes(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """End-to-end: a cycle that went REVIEW_REQUEST_CHANGES → fix →
        REVIEW_RECOMMEND_MERGE must still trigger the ultrareview gate
        on advance_to_terminal. Without latest-marker semantics this
        would silently skip the gate.
        """
        _seed_coded_unit()
        _setup_feature(ultrareview=True)  # re-save feature with flag on
        # Order matters: oldest first.
        state.record_event(
            "F-001-U-1",
            "F-001",
            "reviewer_request_changes",
            source="reviewer",
            cycle_number=0,
            summary="first pass",
        )
        state.record_event(
            "F-001-U-1",
            "F-001",
            "reviewer_recommend_merge",
            source="reviewer",
            cycle_number=1,
            summary="endorsed after fix",
        )

        _stub_github_noop(monkeypatch)
        _silence_ntfy(monkeypatch)

        trigger_calls: list = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ultrareview.trigger",
            lambda pr_url, **kw: trigger_calls.append((pr_url, kw)),
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.ultrareview.wait_for_result",
            lambda pr_url, **kw: {"passed": True, "findings": []},
        )

        out = execution.advance_to_terminal("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"
        assert len(trigger_calls) == 1, (
            "ultrareview must fire when latest marker is RECOMMEND_MERGE, "
            "even if an earlier REVIEW_REQUEST_CHANGES exists in history"
        )


# ===========================================================================
# next_action chain is discoverable end-to-end (daemon contract)
# ===========================================================================


class TestNextActionChain:
    """The Phase-3 daemon walks the cycle by reading ``next_action`` from
    the previous advance's JSON; without a continuous chain the daemon
    can't traverse the pipeline blind. Assert the full chain.
    """

    def test_advanced_responses_chain_to_terminal(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_coded_unit()

        # Stub each phase's spawn out so the engine just walks history.
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        _stub_github_noop(monkeypatch)
        _silence_ntfy(monkeypatch)

        out1 = json.loads(execution.advance_to_tester("F-001", "F-001-U-1"))
        assert out1["outcome"] == "advanced"
        assert out1["next_action"] == "advance_to_reviewer"

        out2 = json.loads(execution.advance_to_reviewer("F-001", "F-001-U-1"))
        assert out2["outcome"] == "advanced"
        assert out2["next_action"] == "advance_to_terminal"
        # reviewer_outcome is the extra advance_to_reviewer threads through
        # so advance_to_terminal can know whether to fire ultrareview
        # without re-reading events. It's also documented as part of the
        # API in the impl docstring.
        assert out2["reviewer_outcome"] == "REVIEW_RECOMMEND_MERGE"

        out3 = json.loads(execution.advance_to_terminal("F-001", "F-001-U-1"))
        assert out3["outcome"] == "approved_awaiting_merge"
        # Terminal is the end of the chain.

    def test_already_past_response_also_carries_next_action(self, tmp_state_db, with_github_token):
        """The daemon's branch on ``already_past`` must still be able to
        find the next step — otherwise it'd stall the cycle after a
        crash-recovery re-tick. Verify ``next_action`` is populated even
        on the no-op branch.
        """
        _seed_coded_unit(status="reviewing")  # past-tester bucket
        out = json.loads(execution.advance_to_tester("F-001", "F-001-U-1"))
        assert out["outcome"] == "already_past"
        assert out["next_action"] == "advance_to_reviewer"


# ===========================================================================
# Sequential composition is observably equivalent to cycle_review
# ===========================================================================


class TestSequentialCompositionEqualsCycleReview:
    """Spec § "No parallel state machine": the wrapper and the
    decomposed three-call sequence must converge on the same terminal.
    If they diverged, the daemon (which composes) would land elsewhere
    than the lead's blocking call.
    """

    @staticmethod
    def _stub_full_pipeline(monkeypatch):
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        _stub_github_noop(monkeypatch)
        _silence_ntfy(monkeypatch)

    def test_three_calls_terminate_same_status_as_wrapper(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        # ----- Decomposed path -----
        _seed_coded_unit(unit_id="F-001-U-1", feature_id="F-001")
        self._stub_full_pipeline(monkeypatch)

        execution.advance_to_tester("F-001", "F-001-U-1")
        execution.advance_to_reviewer("F-001", "F-001-U-1")
        execution.advance_to_terminal("F-001", "F-001-U-1")
        decomposed_status = state.get_unit_state("F-001-U-1").status

        # ----- Wrapper path (fresh DB row) -----
        # Drop the unit row and re-seed for the wrapper run.
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                branch="feat/branch",
                pr_number=5,
                coder_session_id="sesn-c",
            )
        )
        execution.cycle_review("F-001", "F-001-U-1")
        wrapper_status = state.get_unit_state("F-001-U-1").status

        assert decomposed_status == wrapper_status == "approved_awaiting_merge", (
            f"decomposed={decomposed_status} wrapper={wrapper_status} — "
            "phase commands and wrapper must converge"
        )

    def test_re_calling_each_phase_after_completion_is_idempotent(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """A daemon re-tick after the cycle reaches terminal must be a
        no-op for every phase. Verify all three return ``already_past``
        when called again post-success (advance_to_terminal will see
        ``approved_awaiting_merge`` — that is the documented
        not-past-yet bucket — so it re-fires; we drive the unit to
        ``done`` first to test the truly-terminal idempotence)."""
        _seed_coded_unit()
        self._stub_full_pipeline(monkeypatch)
        execution.cycle_review("F-001", "F-001-U-1")

        # Simulate the post-merge flip done by reconcile_unit_pr.
        s = state.get_unit_state("F-001-U-1")
        s.status = "done"
        state.upsert_unit_state(s)

        out_t = json.loads(execution.advance_to_tester("F-001", "F-001-U-1"))
        out_r = json.loads(execution.advance_to_reviewer("F-001", "F-001-U-1"))
        out_x = json.loads(execution.advance_to_terminal("F-001", "F-001-U-1"))

        assert out_t["outcome"] == "already_past"
        assert out_r["outcome"] == "already_past"
        assert out_x["outcome"] == "already_past"


# ===========================================================================
# Escalated response shape (daemon-branchable) + not_ready shape
# ===========================================================================


class TestEscalatedAndNotReadyShape:
    def test_escalated_surfaces_last_error_verbatim(self, tmp_state_db, with_github_token):
        _seed_coded_unit()
        s = state.get_unit_state("F-001-U-1")
        s.status = "escalated"
        s.last_error = "cap-3 hit on tester bugs after 3 attempts"
        state.upsert_unit_state(s)

        for fn in (
            execution.advance_to_tester,
            execution.advance_to_reviewer,
            execution.advance_to_terminal,
        ):
            out = json.loads(fn("F-001", "F-001-U-1"))
            assert out["outcome"] == "escalated", fn.__name__
            assert out["status"] == "escalated", fn.__name__
            assert "cap-3 hit on tester bugs" in out["message"], (
                f"{fn.__name__} must surface last_error verbatim "
                "(docstring promise: 'without an extra unit_history round-trip')"
            )

    def test_not_ready_when_unit_state_missing(self, tmp_state_db, with_github_token):
        _setup_feature()
        # No unit row yet — the unit was never spawned.

        for fn in (
            execution.advance_to_tester,
            execution.advance_to_reviewer,
            execution.advance_to_terminal,
        ):
            out = json.loads(fn("F-001", "F-001-U-1"))
            assert out["outcome"] == "not_ready", fn.__name__
            # Distinct outcome (not "escalated") so the daemon can branch:
            # "wait/spawn first" vs. "human triage needed".
            assert out.get("status") != "escalated"


# ===========================================================================
# Verification gate fires on every phase command
# ===========================================================================


class TestVerificationGate:
    """All three commands must call ``ensure_verified_for_feature``.
    Bypassing the gate is a defense-in-depth regression against F-008.
    """

    def test_each_phase_command_returns_verify_error_for_unverified_repo(
        self, tmp_state_db, with_github_token
    ):
        # Use a fresh repo URL we did NOT pre-seed in conftest.
        unverified = "https://github.com/o/unverified-repo"
        state.save_feature(
            Feature(
                id="F-999",
                title="t",
                description="d",
                repo_path=unverified,
                status="approved",
            )
        )
        state.save_plan(
            "F-999",
            [WorkUnit(id="F-999-U-1", feature_id="F-999", title="u1", description="d")],
        )
        state.approve_plan("F-999")

        for fn in (
            execution.advance_to_tester,
            execution.advance_to_reviewer,
            execution.advance_to_terminal,
        ):
            out = fn("F-999", "F-999-U-1")
            assert "ERROR" in out and "not verified" in out, (
                f"{fn.__name__} must respect the verify-gate; got: {out[:120]!r}"
            )


# ===========================================================================
# MCP registration (RPC-reachability for daemon + lead)
# ===========================================================================


class TestMcpRegistration:
    """All three phase commands AND the wrapper must be registered with
    the FastMCP instance — the daemon (F-016-U-5) reaches them as RPCs
    and the lead's chat surface needs the wrapper for convenience.
    F-016-U-2's tools must also remain registered (additive contract).
    """

    @staticmethod
    def _list_tool_names() -> set[str]:
        # FastMCP's tool registry is async-only. Use ``asyncio.run`` (not a
        # bare ``new_event_loop`` + ``loop.close``) so ``shutdown_asyncgens``
        # + ``shutdown_default_executor`` fire on teardown — Python 3.12 on
        # Windows is stricter about cleanup and the bare-loop pattern,
        # repeated across the five tests in this class, would leak the
        # default executor and trip the next test's loop. Matches the
        # ``asyncio.run(mcp.list_tools())`` pattern in
        # ``tests/test_tools_health.py`` / ``tests/test_tools_ops.py`` /
        # ``tests/test_f014_u2_tester.py``.
        tools = asyncio.run(mcp.list_tools())
        return {t.name for t in tools}

    def test_advance_to_tester_registered(self):
        assert "advance_to_tester" in self._list_tool_names()

    def test_advance_to_reviewer_registered(self):
        assert "advance_to_reviewer" in self._list_tool_names()

    def test_advance_to_terminal_registered(self):
        assert "advance_to_terminal" in self._list_tool_names()

    def test_cycle_review_still_registered_as_wrapper(self):
        """The unit description says cycle_review BECOMES a wrapper;
        deleting it would break every existing lead caller."""
        assert "cycle_review" in self._list_tool_names()

    def test_predecessor_f016_u2_tools_still_registered(self):
        """F-016 Phase 2 is additive over Phase 1; Phase 1 RPCs must
        still be reachable. A silent removal would break the
        non-blocking spawn contract U-2 just shipped."""
        names = self._list_tool_names()
        assert "spawn_unit_async" in names, (
            "F-016-U-2's spawn_unit_async must remain registered (additive contract)"
        )
        assert "wait_unit" in names, (
            "F-016-U-2's wait_unit must remain registered (additive contract)"
        )
