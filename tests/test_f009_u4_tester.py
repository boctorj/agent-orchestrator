"""Independent tester-agent tests for F-009-U-4.

This unit promotes ``approved_awaiting_merge`` from a cycle_review return-string
to a first-class persistent ``UnitStatus``. The tests below are written from
the unit description independently of the coder's own test file
(``tests/test_f009_u4_approved_awaiting_merge.py``) — divergences flag drift
in either direction.

Contracts pinned (one test class per spec bullet):

  1. ``UnitStatus`` Literal in ``orchestrator/models.py`` includes
     ``approved_awaiting_merge``, and the new ``READY_TO_MERGE_STATUSES``
     constant exists.
  2. ``cycle_review._emit_terminal(outcome='approved_awaiting_merge', ...)``
     writes ``status='approved_awaiting_merge'`` to the unit row.
  3. ``send_to_unit(reviewer, msg)`` whose response contains
     ``REVIEW_RECOMMEND_MERGE`` writes ``status='approved_awaiting_merge'``
     (NOT ``in_ci``) — same semantic as cycle_review's terminal.
  4. Downstream blocking: ``next_ready_units`` / ``next_ready_units_all`` does
     NOT unblock a unit whose dep is ``approved_awaiting_merge`` (only ``done``
     counts as a satisfied dep).
  5. ``list_in_flight`` surfaces ``approved_awaiting_merge`` (awaiting human
     action, not idle).
  6. Dashboard: ``show_dashboard``'s "Awaiting your merge" bucket queries the
     new status.
  7. ``reconcile_unit_pr``: ``approved_awaiting_merge`` + merged →
     ``done`` (emits ``merged`` event); leaves it alone when the PR is still
     open.
"""

from __future__ import annotations

import json
import typing

import pytest

from orchestrator import dashboard, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    READY_TO_MERGE_STATUSES,
    TERMINAL_UNIT_STATUSES,
    Feature,
    UnitStatus,
    WorkUnit,
    WorkUnitState,
)
from orchestrator.tools import execution, ops, scheduling

# --------------------------- shared fixtures / helpers ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """All CI gates pass — orthogonal to the state writes this unit tests."""

    def _green(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=0.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", _green)


@pytest.fixture(autouse=True)
def _silence_side_effects(monkeypatch):
    """Neutralise the ntfy, cycle-log, and GitHub side effects fired by the
    code under test. Each spec bullet only cares about state-machine writes."""
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda *a, **k: True,
    )
    monkeypatch.setattr("orchestrator.tools.execution._write_cycle_log_safe", lambda *a, **k: None)
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests",
        lambda *a, **k: 0,
    )


class _StubWorker:
    """Minimal stand-in for ManagedAgentWorker so send_to_unit's resume path
    runs without contacting Anthropic."""

    def __init__(self, role: str, *, resume_response: str = ""):
        self.role = role
        self.resume_response = resume_response

    def spawn(self, task: str, *, title: str | None = None):
        return f"sesn-{self.role}", ""

    def resume(self, session_id: str, message: str) -> str:
        return self.resume_response


def _install_worker(monkeypatch, *, resume_response: str = "") -> None:
    """Patch ManagedAgentWorker constructor so every role returns a stub
    whose resume() returns the canned reply."""
    cache: dict[str, _StubWorker] = {}

    def factory(role: str) -> _StubWorker:
        if role not in cache:
            cache[role] = _StubWorker(role, resume_response=resume_response)
        return cache[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)


# Standard fixture-repo URL pre-verified by the tmp_state_db fixture.
_REPO = "https://github.com/o/r"


def _seed_feature(feature_id: str = "F-100", repo: str = _REPO) -> None:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=repo,
            status="approved",
        )
    )


def _seed_unit_state(
    unit_id: str = "F-100-U-1",
    feature_id: str = "F-100",
    *,
    status: str = "reviewing",
    pr_number: int | None = 11,
) -> None:
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="feat/x",
            pr_number=pr_number,
            coder_session_id="sesn-coder",
            reviewer_session_id="sesn-reviewer",
            tester_session_id="sesn-tester",
        )
    )


def _seed_plan(
    feature_id: str = "F-100",
    *,
    extra_units: list[WorkUnit] | None = None,
) -> None:
    """Save a plan with at least one unit; ``extra_units`` lets us add deps."""
    units = [WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description="")]
    if extra_units:
        units.extend(extra_units)
    state.save_plan(feature_id, units)
    state.approve_plan(feature_id)


# --------------------------- (1) model literal + constant ---------------------------


class TestUnitStatusLiteralAndConstant:
    """models.py is the single source of truth for status names; pin both
    the Literal membership and the READY_TO_MERGE_STATUSES constant the
    spec asked for."""

    def test_unit_status_literal_includes_approved_awaiting_merge(self):
        # If this regresses, no other static-typed call site (touch_unit,
        # WorkUnitState.status) will accept the new value — fix the
        # Literal here first.
        assert "approved_awaiting_merge" in typing.get_args(UnitStatus)

    def test_ready_to_merge_statuses_constant_exists_and_contains_status(self):
        # The constant is *the* anchor next_ready_units / list_in_flight
        # branch on. Either it doesn't exist (import error) or it must
        # include the new status — anything else breaks the helpers.
        assert "approved_awaiting_merge" in READY_TO_MERGE_STATUSES

    def test_awaiting_merge_is_not_active_and_not_terminal(self):
        """The spec calls awaiting-merge "not in flight, not blocked, but
        not done until merge" — verify it's its own bucket, distinct from
        the agent-active set and from the terminal set."""
        assert "approved_awaiting_merge" not in ACTIVE_UNIT_STATUSES
        assert "approved_awaiting_merge" not in TERMINAL_UNIT_STATUSES


# --------------------------- (2) cycle_review._emit_terminal writes status ---------------------------


class TestEmitTerminalWritesAwaitingMergeStatus:
    """``_emit_terminal(outcome='approved_awaiting_merge', ...)`` persists
    ``status='approved_awaiting_merge'`` so the dashboard / list_in_flight /
    reconcile_unit_pr all see it (write site 1 in the unit description)."""

    def test_in_ci_to_approved_awaiting_merge(self, tmp_state_db):
        """The canonical path: cycle_review's reviewer endorsement runs
        while the unit is in_ci. Terminal call must flip it."""
        _seed_feature()
        _seed_unit_state(status="in_ci")

        ctx = execution.CycleContext(feature_id="F-100", unit_id="F-100-U-1", history=[])
        execution._emit_terminal(ctx, "approved_awaiting_merge", "endorsed")

        assert state.get_unit_state("F-100-U-1").status == "approved_awaiting_merge"

    def test_does_not_drift_done_back_to_awaiting_merge(self, tmp_state_db):
        """Active-only gate (``_flip_status_if_active``) protects against
        a stray re-entry on an already-merged unit. ``done`` is terminal —
        a late ``_emit_terminal`` must NOT walk it back."""
        _seed_feature()
        _seed_unit_state(status="done")

        ctx = execution.CycleContext(feature_id="F-100", unit_id="F-100-U-1", history=[])
        execution._emit_terminal(ctx, "approved_awaiting_merge", "late echo")

        # Status stays at done; the gate is the property under test.
        assert state.get_unit_state("F-100-U-1").status == "done"

    def test_non_endorsement_outcomes_do_not_set_awaiting_merge(self, tmp_state_db):
        """``_emit_terminal('escalated', ...)`` must NOT also write
        approved_awaiting_merge — only the endorsement branch should.
        Pinning the negative branch as well so a future refactor that
        unconditionally writes the new status fails this test."""
        _seed_feature()
        _seed_unit_state(status="in_ci")

        ctx = execution.CycleContext(feature_id="F-100", unit_id="F-100-U-1", history=[])
        execution._emit_terminal(ctx, "escalated", "cap-3 hit")

        # Unit stays in_ci — _emit_terminal doesn't write status for
        # escalations (escalated status would have been set upstream by
        # whatever decided to escalate).
        assert state.get_unit_state("F-100-U-1").status != "approved_awaiting_merge"


# --------------------------- (3) send_to_unit reviewer write ---------------------------


class TestSendToUnitReviewerRecommendMergeWritesAwaitingMerge:
    """Write site 2: send_to_unit (reviewer) routing through the U-1
    helper. Per spec — REVIEW_RECOMMEND_MERGE writes
    ``approved_awaiting_merge`` (NOT ``in_ci``), keeping the
    send_to_unit-path and cycle_review-terminal-path on the same status."""

    def test_review_recommend_merge_lands_in_approved_awaiting_merge(
        self, tmp_state_db, monkeypatch
    ):
        _seed_feature()
        _seed_unit_state(status="reviewing")
        _install_worker(
            monkeypatch,
            resume_response="OK, looks good.\nREVIEW_RECOMMEND_MERGE: tests cover both branches",
        )

        execution.send_to_unit("F-100-U-1", "reviewer", "your call?")

        s = state.get_unit_state("F-100-U-1")
        # The crux of the spec bullet: NOT in_ci, but approved_awaiting_merge.
        assert s.status == "approved_awaiting_merge"

        # And BOTH events landed (the manual-message audit row + the
        # structured marker event) — spec calls for keeping the audit
        # message and adding the structured one.
        types = [e["event_type"] for e in state.list_events("F-100-U-1")]
        assert "reviewer_recommend_merge" in types
        assert "reviewer_manual_message" in types

    def test_review_comment_via_send_to_unit_still_goes_to_in_ci(self, tmp_state_db, monkeypatch):
        """Negative pin: only REVIEW_RECOMMEND_MERGE targets the new
        bucket. REVIEW_COMMENT (the other reviewer success marker) must
        stay at ``in_ci`` — its cycle's CI gate still owns the terminal."""
        _seed_feature()
        _seed_unit_state(status="reviewing")
        _install_worker(monkeypatch, resume_response="REVIEW_COMMENT\n")

        execution.send_to_unit("F-100-U-1", "reviewer", "any thoughts?")

        assert state.get_unit_state("F-100-U-1").status == "in_ci"

    def test_send_to_unit_tester_tests_pass_does_not_write_awaiting_merge(
        self, tmp_state_db, monkeypatch
    ):
        """Cross-role guard: TESTS_PASS from a tester writes in_ci (its
        existing semantic), NOT the new awaiting-merge status. This
        protects the spec's "same semantic as cycle_review's terminal"
        promise — only the reviewer's endorsement marker maps to
        awaiting-merge."""
        _seed_feature()
        _seed_unit_state(status="testing")
        _install_worker(monkeypatch, resume_response="TESTS_PASS\n")

        execution.send_to_unit("F-100-U-1", "tester", "rerun tests")

        s = state.get_unit_state("F-100-U-1")
        assert s.status == "in_ci"
        assert s.status != "approved_awaiting_merge"

    def test_review_recommend_merge_on_done_unit_does_not_walk_back(
        self, tmp_state_db, monkeypatch
    ):
        """``_flip_status_if_active`` gate: a stray endorsement on a
        ``done`` unit (e.g. late agent reply after merge) must not move
        status to approved_awaiting_merge. Send_to_unit is the
        documented escape hatch the user can fire at any state."""
        _seed_feature()
        _seed_unit_state(status="done")
        _install_worker(
            monkeypatch,
            resume_response="thanks\nREVIEW_RECOMMEND_MERGE: late echo",
        )

        execution.send_to_unit("F-100-U-1", "reviewer", "fyi")

        assert state.get_unit_state("F-100-U-1").status == "done"


# --------------------------- (4) next_ready_units downstream blocking ---------------------------


class TestNextReadyUnitsAwaitingMergeBlocksDownstream:
    """LOCKED downstream-blocking semantics: a dep in
    ``approved_awaiting_merge`` keeps a downstream unit blocked
    (it is NOT in ready_to_spawn). Mirrors today's done-only behaviour.
    The awaiting-merge unit itself surfaces in its own bucket — distinct
    from in_flight, distinct from escalated."""

    def _two_unit_plan(self, feature_id: str = "F-200") -> tuple[str, str]:
        """U-1 (no deps) -> U-2 (depends on U-1)."""
        state.save_feature(
            Feature(
                id=feature_id,
                title="t",
                description="d",
                repo_path=_REPO,
                status="approved",
            )
        )
        u1, u2 = f"{feature_id}-U-1", f"{feature_id}-U-2"
        state.save_plan(
            feature_id,
            [
                WorkUnit(id=u1, feature_id=feature_id, title="u1", description=""),
                WorkUnit(
                    id=u2,
                    feature_id=feature_id,
                    title="u2",
                    description="",
                    depends_on=[u1],
                ),
            ],
        )
        state.approve_plan(feature_id)
        return u1, u2

    def test_downstream_dep_stays_blocked_when_dep_awaiting_merge(self, tmp_state_db):
        u1, u2 = self._two_unit_plan("F-201")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=u1,
                feature_id="F-201",
                status="approved_awaiting_merge",
                pr_number=42,
            )
        )

        parsed = json.loads(scheduling.next_ready_units("F-201"))
        # U-2 must not appear in ready_to_spawn (its dep is awaiting merge,
        # not done).
        ready_ids = [u["unit_id"] for u in parsed["ready_to_spawn"]]
        assert u2 not in ready_ids
        # U-1 itself is reported — somewhere — but NOT as ready_to_spawn.
        # The unit description says it should appear in an "in-flight-like
        # bucket" so list_in_flight surfaces it. The scheduling tool's
        # output groups it under the awaiting-merge bucket; either name is
        # acceptable as long as the lead can see it's not ready and not
        # blocked. (Asserting only the absence from ready_to_spawn keeps
        # the test resilient to bucket-name choices.)

    def test_downstream_unblocks_after_dep_flips_to_done(self, tmp_state_db):
        """Contrast pin: once the dep is ``done`` (the merge has
        happened), the downstream IS ready_to_spawn. Captures the
        before/after to prove the gate is exactly the merge flip."""
        u1, u2 = self._two_unit_plan("F-202")
        state.upsert_unit_state(
            WorkUnitState(unit_id=u1, feature_id="F-202", status="done", pr_number=42)
        )

        parsed = json.loads(scheduling.next_ready_units("F-202"))
        assert u2 in [u["unit_id"] for u in parsed["ready_to_spawn"]]

    def test_awaiting_merge_unit_is_not_classified_as_in_flight(self, tmp_state_db):
        """The unit is awaiting human action, not running agent code —
        it must NOT land in the agent-active ``in_flight`` bucket of
        next_ready_units (which otherwise reports active statuses like
        coding/testing/reviewing/fixing/in_ci/opening_pr)."""
        u1, _u2 = self._two_unit_plan("F-203")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=u1,
                feature_id="F-203",
                status="approved_awaiting_merge",
                pr_number=42,
            )
        )

        parsed = json.loads(scheduling.next_ready_units("F-203"))
        in_flight_ids = [u["unit_id"] for u in parsed.get("in_flight", [])]
        assert u1 not in in_flight_ids

    def test_next_ready_units_all_does_not_unblock_via_awaiting_merge(self, tmp_state_db):
        """Multi-feature aggregator must respect the same blocking rule —
        ``approved_awaiting_merge`` does not satisfy a dep at the global
        level either."""
        u1, u2 = self._two_unit_plan("F-204")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=u1,
                feature_id="F-204",
                status="approved_awaiting_merge",
                pr_number=42,
            )
        )

        parsed = json.loads(scheduling.next_ready_units_all())
        ready_ids = [u["unit_id"] for u in parsed["ready_to_spawn"]]
        assert u2 not in ready_ids


# --------------------------- (5) list_in_flight surfaces it ---------------------------


class TestListInFlightSurfacesAwaitingMerge:
    """The spec says "Add status to an in-flight-like bucket so
    list_in_flight surfaces it (awaiting human action, not idle)"."""

    def test_list_in_flight_includes_awaiting_merge_row(self, tmp_state_db):
        _seed_feature()
        _seed_unit_state(status="approved_awaiting_merge")

        parsed = json.loads(ops.list_in_flight())
        ids = [r["unit_id"] for r in parsed]
        assert "F-100-U-1" in ids
        # ...and the row carries its status verbatim, so the lead can
        # distinguish "no agent running, awaiting merge" from "agent
        # mid-flight" by inspection.
        row = next(r for r in parsed if r["unit_id"] == "F-100-U-1")
        assert row["status"] == "approved_awaiting_merge"

    def test_list_in_flight_does_not_include_done_or_escalated_by_default(self, tmp_state_db):
        """Negative pin: terminal statuses stay out. Pre-F-009-U-4
        behaviour (active-only) extended only to include awaiting-merge —
        not anything else."""
        _seed_feature()
        state.upsert_unit_state(WorkUnitState(unit_id="u-done", feature_id="F-100", status="done"))
        state.upsert_unit_state(
            WorkUnitState(unit_id="u-esc", feature_id="F-100", status="escalated")
        )
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="u-awm",
                feature_id="F-100",
                status="approved_awaiting_merge",
            )
        )

        parsed = json.loads(ops.list_in_flight())
        ids = {r["unit_id"] for r in parsed}
        # done + escalated are excluded; awaiting-merge is the only
        # non-active status surfaced by default.
        assert "u-done" not in ids
        assert "u-esc" not in ids
        assert "u-awm" in ids


# --------------------------- (6) dashboard 'Awaiting your merge' ---------------------------


class TestDashboardAwaitingMergeBucketIntegration:
    """``show_dashboard``'s "Awaiting your merge" bucket queries the new
    status. Spec line: "today it queries something no code path writes" —
    so the post-fix row must show up in the markdown, and *no* row should
    show when the unit isn't in the status."""

    def test_awaiting_merge_data_returns_only_units_in_status(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-100-U-1",
                feature_id="F-100",
                status="approved_awaiting_merge",
                pr_number=77,
                branch="feat/x",
            )
        )

        rows = dashboard._awaiting_merge_data()
        assert len(rows) == 1
        r = rows[0]
        assert r["unit_id"] == "F-100-U-1"
        assert r["pr"] == "#77"

    def test_awaiting_merge_data_ignores_in_ci_with_stale_recommend_event(self, tmp_state_db):
        """The status row is authoritative — a unit with a stale
        ``reviewer_recommend_merge`` event but status ``in_ci`` (e.g. a
        pre-U-4-migration history) must NOT show up. This is the audit
        Gap H regression test: the bucket previously over-relied on
        events to infer "awaiting merge"."""
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-100-U-1",
                feature_id="F-100",
                status="in_ci",
                pr_number=77,
            )
        )
        state.record_event(
            "F-100-U-1",
            "F-100",
            "reviewer_recommend_merge",
            source="reviewer",
        )

        assert dashboard._awaiting_merge_data() == []

    def test_render_markdown_shows_unit_in_awaiting_merge_section(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-100-U-1",
                feature_id="F-100",
                status="approved_awaiting_merge",
                pr_number=88,
                branch="feat/x",
            )
        )

        md = dashboard.render_markdown()
        assert "Awaiting your merge" in md
        # The unit-and-PR pair appears between the awaiting-merge header
        # and the next section header. Isolate that slice so we're not
        # confused by the same strings appearing elsewhere on the page.
        after_header = md.split("Awaiting your merge", 1)[1]
        section = after_header.split("\n## ", 1)[0]
        assert "F-100-U-1" in section
        assert "#88" in section


# --------------------------- (7) reconcile_unit_pr branch ---------------------------


def _stub_pr_merged(monkeypatch) -> None:
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-20T12:34:56Z",
            "head_sha": "deadbeef",
            "merge_commit_sha": None,
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 1, "conclusion_counts": {"success": 1}, "runs": []},
    )


def _stub_pr_open(monkeypatch) -> None:
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


class TestReconcileUnitPrApprovedAwaitingMergeBranch:
    """Spec: ``reconcile_unit_pr`` adds the
    'approved_awaiting_merge + merged' branch — flips to ``done`` and
    emits a ``merged`` event. And it does NOT flip when the PR is still
    open."""

    def test_merged_pr_flips_awaiting_merge_to_done_and_emits_merged_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature()
        _seed_unit_state(status="approved_awaiting_merge", pr_number=12)
        _stub_pr_merged(monkeypatch)

        parsed = json.loads(ops.reconcile_unit_pr("F-100-U-1"))

        # The PR was merged → status flips to done.
        assert state.get_unit_state("F-100-U-1").status == "done"
        # The action slug is the diagnostic the lead surfaces; pin it so
        # log scraping doesn't fall over on a rename.
        assert parsed["reconciled"] is True
        assert parsed["orchestrator_status"] == "done"
        # A 'merged' event was emitted exactly once.
        event_types = [e["event_type"] for e in state.list_events("F-100-U-1")]
        assert event_types.count("merged") == 1

    def test_open_pr_keeps_awaiting_merge_status_and_emits_no_events(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """If the human hasn't merged yet, reconcile leaves the row
        alone — no status change, no events."""
        _seed_feature()
        _seed_unit_state(status="approved_awaiting_merge", pr_number=12)
        _stub_pr_open(monkeypatch)
        pre = state.list_events("F-100-U-1")

        parsed = json.loads(ops.reconcile_unit_pr("F-100-U-1"))

        assert parsed["reconciled"] is False
        assert state.get_unit_state("F-100-U-1").status == "approved_awaiting_merge"
        assert state.list_events("F-100-U-1") == pre

    def test_reconcile_is_idempotent_after_awaiting_merge_promotion(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Second reconcile after the first one promoted to done must NOT
        re-emit ``merged``. The branch table includes a ``no-op-already-
        done`` guard."""
        _seed_feature()
        _seed_unit_state(status="approved_awaiting_merge", pr_number=12)
        _stub_pr_merged(monkeypatch)

        # First call flips to done + emits 'merged'.
        ops.reconcile_unit_pr("F-100-U-1")
        # Second call must be a no-op event-wise.
        ops.reconcile_unit_pr("F-100-U-1")

        event_types = [e["event_type"] for e in state.list_events("F-100-U-1")]
        assert event_types.count("merged") == 1


# --------------------------- end-to-end cycle_review pin ---------------------------


def test_cycle_review_endorsement_persists_approved_awaiting_merge_in_state(
    tmp_state_db, with_github_token, monkeypatch
):
    """End-to-end pin on the cycle_review path: spawn_tester returns
    TESTS_PASS, spawn_reviewer returns REVIEW_RECOMMEND_MERGE → the cycle
    exits as ``approved_awaiting_merge`` AND the unit row's status field
    holds ``approved_awaiting_merge`` (not the legacy ``in_ci`` it lived
    in pre-F-009-U-4)."""
    _seed_feature()
    # Seed without a tester_session_id so cycle_review's
    # _resume_or_spawn_tester routes to the stubbed spawn_tester rather
    # than attempting a Managed-Agent resume on the canned session id.
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-100-U-1",
            feature_id="F-100",
            status="in_ci",
            branch="feat/x",
            pr_number=12,
            coder_session_id="sesn-coder",
        )
    )

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
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.request_copilot_review",
        lambda *a, **k: {"requested": True, "status_code": 201, "note": ""},
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.wait_for_copilot_review",
        lambda *a, **k: {"present": False, "elapsed_s": 0},
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("owner", "repo"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.get_pr_state",
        lambda *a, **k: {"head_sha": "abc", "state": "open", "merged": False},
    )

    out = execution.cycle_review("F-100", "F-100-U-1")
    parsed = json.loads(out)

    assert parsed["outcome"] == "approved_awaiting_merge"
    assert state.get_unit_state("F-100-U-1").status == "approved_awaiting_merge"
