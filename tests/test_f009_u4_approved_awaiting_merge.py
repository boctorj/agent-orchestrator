"""F-009-U-4: ``approved_awaiting_merge`` as a real persistent status.

This unit promotes ``approved_awaiting_merge`` from a cycle_review return-string
to a first-class ``UnitStatus`` literal. It's the missing status the audit
caught in Gap H: every reviewer endorsement that ended a cycle should land in
this bucket so the dashboard's "Awaiting your merge" panel, ``list_in_flight``,
``next_ready_units``, and ``reconcile_unit_pr`` all see the same row.

Tests pinned here (per the unit description):

  * ``cycle_review._emit_terminal('approved_awaiting_merge', ...)`` flips the
    unit's status to ``approved_awaiting_merge`` (writes the new status).
  * ``send_to_unit(reviewer, msg)`` with a response containing
    ``REVIEW_RECOMMEND_MERGE`` flips status to ``approved_awaiting_merge``
    (NOT ``in_ci``) — same semantic as cycle_review's terminal.
  * ``show_dashboard``'s "Awaiting your merge" bucket surfaces the unit.
  * ``next_ready_units`` does NOT unblock a downstream unit whose dep is
    ``approved_awaiting_merge`` (only ``done`` counts as a satisfied dep).
  * ``list_in_flight`` surfaces ``approved_awaiting_merge`` (awaiting human
    action, not idle).
  * ``reconcile_unit_pr`` flips ``approved_awaiting_merge`` → ``done`` on an
    observed merge and emits a ``merged`` event.
  * ``reconcile_unit_pr`` does NOT flip ``approved_awaiting_merge`` → ``done``
    while the PR is still open.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import dashboard, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import (
    READY_TO_MERGE_STATUSES,
    Feature,
    WorkUnit,
    WorkUnitState,
)
from orchestrator.tools import execution, ops, scheduling

# --------------------------- shared fixtures / helpers ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """All CI gates pass — they're orthogonal to the state-machine writes
    this unit tests."""

    def _green(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", _green)


class _StubWorker:
    def __init__(self, role: str, *, spawn_response: str = "", resume_response: str = ""):
        self.role = role
        self.spawn_response = spawn_response
        self.resume_response = resume_response

    def spawn(self, task: str, *, title: str | None = None):
        return f"sesn-{self.role}", self.spawn_response

    def resume(self, session_id: str, message: str) -> str:
        return self.resume_response


def _install_worker_factory(monkeypatch, *, spawn_response="", resume_response=""):
    """Make ManagedAgentWorker(role=...) return the same stub per role."""
    cache: dict[str, _StubWorker] = {}

    def factory(role: str) -> _StubWorker:
        if role not in cache:
            cache[role] = _StubWorker(
                role,
                spawn_response=spawn_response,
                resume_response=resume_response,
            )
        return cache[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)


def _stub_github_sideeffects(monkeypatch):
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests", lambda *a, **k: 0
    )


def _seed_feature(feature_id="F-001", repo="https://github.com/o/r"):
    state.save_feature(
        Feature(id=feature_id, title="t", description="d", repo_path=repo, status="approved")
    )


def _seed_unit(
    *,
    unit_id="F-001-U-1",
    feature_id="F-001",
    status="reviewing",
    pr_number=7,
    review_round=0,
    depends_on=None,
):
    """Seed a unit with a plan entry (so next_ready_units can see deps)."""
    if depends_on is None:
        depends_on = []
    plan = state.get_plan(feature_id)
    if not plan:
        units = [
            WorkUnit(
                id=unit_id,
                feature_id=feature_id,
                title="t",
                description="d",
                depends_on=depends_on,
            )
        ]
        state.save_plan(feature_id, units)
        state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="feat/branch",
            pr_number=pr_number,
            coder_session_id="sesn-coder",
            reviewer_session_id="sesn-reviewer",
            review_round=review_round,
        )
    )


def _seed_two_unit_plan(feature_id="F-100"):
    """Plan with U-1 (no deps) -> U-2 (depends on U-1). Returns the unit ids."""
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
        [
            WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description=""),
            WorkUnit(
                id=f"{feature_id}-U-2",
                feature_id=feature_id,
                title="u2",
                description="",
                depends_on=[f"{feature_id}-U-1"],
            ),
        ],
    )
    state.approve_plan(feature_id)
    return f"{feature_id}-U-1", f"{feature_id}-U-2"


# --------------------------- write site 1: cycle_review._emit_terminal ---------------------------


class TestCycleReviewTerminalWritesStatus:
    """The unit description's write site 1: when ``_emit_terminal`` is invoked
    with ``outcome='approved_awaiting_merge'``, the unit's row must be flipped
    to ``status='approved_awaiting_merge'``."""

    def test_emit_terminal_approved_awaiting_merge_flips_status(self, tmp_state_db, monkeypatch):
        _seed_feature()
        _seed_unit(status="in_ci")  # post-reviewer state before terminal
        # Silence the ntfy / cycle-log side effects — orthogonal here.
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution._write_cycle_log_safe", lambda *a, **k: None
        )

        ctx = execution.CycleContext(feature_id="F-001", unit_id="F-001-U-1", history=[])
        execution._emit_terminal(ctx, "approved_awaiting_merge", "review terminal")

        assert state.get_unit_state("F-001-U-1").status == "approved_awaiting_merge"

    def test_emit_terminal_escalated_does_not_set_awaiting_merge(self, tmp_state_db, monkeypatch):
        """Symmetric guard: only the approved branch writes the new status —
        escalated paths must leave their already-set ``escalated`` row alone."""
        _seed_feature()
        _seed_unit(status="escalated")
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution._write_cycle_log_safe", lambda *a, **k: None
        )

        ctx = execution.CycleContext(feature_id="F-001", unit_id="F-001-U-1", history=[])
        execution._emit_terminal(ctx, "escalated", "cap-3")

        assert state.get_unit_state("F-001-U-1").status == "escalated"

    def test_cycle_review_happy_path_lands_unit_in_awaiting_merge(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """End-to-end pin: a full cycle_review run that exits with
        ``approved_awaiting_merge`` must leave the unit's status row in
        ``approved_awaiting_merge`` (not the legacy ``in_ci`` it lived in
        pre-F-009-U-4)."""
        _seed_feature()
        _seed_unit(status="in_ci")
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
        # Neutralise GitHub + Copilot touches inside the cycle.
        for name in ("safe_comment_pr", "safe_submit_pr_review", "safe_amend_pr_body"):
            monkeypatch.setattr(f"orchestrator.tools.execution.{name}", lambda *a, **k: "")
        monkeypatch.setattr(
            "orchestrator.tools.execution.safe_dismiss_own_change_requests", lambda *a, **k: 0
        )
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
            lambda *a, **k: {"head_sha": "deadbeefcafe", "state": "open", "merged": False},
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution._write_cycle_log_safe", lambda *a, **k: None
        )

        # Cycle_review's terminal in cycle_review uses _emit_terminal +
        # the new status flip; this test's whole point is that the unit
        # status reflects the awaiting-merge bucket after the call.
        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"
        assert state.get_unit_state("F-001-U-1").status == "approved_awaiting_merge"


# --------------------------- write site 2: send_to_unit(reviewer) ---------------------------


class TestSendToUnitReviewerWritesAwaitingMerge:
    """The unit description's write site 2: send_to_unit's reviewer path must
    write ``approved_awaiting_merge`` (NOT ``in_ci``) when the response
    contains ``REVIEW_RECOMMEND_MERGE`` — same semantic as cycle_review's
    terminal."""

    def test_review_recommend_merge_via_send_to_unit_lands_in_awaiting_merge(
        self, tmp_state_db, monkeypatch
    ):
        _seed_feature()
        _seed_unit(status="reviewing")
        _install_worker_factory(
            monkeypatch,
            resume_response="endorsed\nREVIEW_RECOMMEND_MERGE: tests cover the new path",
        )
        _stub_github_sideeffects(monkeypatch)

        execution.send_to_unit("F-001-U-1", "reviewer", "what's the verdict?")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "approved_awaiting_merge"
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "reviewer_recommend_merge" in types
        assert "reviewer_manual_message" in types

    def test_review_comment_via_send_to_unit_still_lands_in_in_ci(self, tmp_state_db, monkeypatch):
        """Negative pin: only REVIEW_RECOMMEND_MERGE targets the new bucket —
        REVIEW_COMMENT (the other reviewer success-side marker) stays at
        ``in_ci`` so the cycle's downstream CI gate still owns the terminal."""
        _seed_feature()
        _seed_unit(status="reviewing")
        _install_worker_factory(monkeypatch, resume_response="REVIEW_COMMENT\n")
        _stub_github_sideeffects(monkeypatch)

        execution.send_to_unit("F-001-U-1", "reviewer", "any thoughts?")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci"

    def test_send_to_unit_review_recommend_merge_on_done_stays_done(
        self, tmp_state_db, monkeypatch
    ):
        """Terminal-status guard: a stray REVIEW_RECOMMEND_MERGE via
        send_to_unit on an already-``done`` unit must NOT drift status back
        to ``approved_awaiting_merge``. The status-flip gate (active-only
        from-state) applies symmetrically to the new target."""
        _seed_feature()
        _seed_unit(status="done")
        _install_worker_factory(monkeypatch, resume_response="REVIEW_RECOMMEND_MERGE: late echo")
        _stub_github_sideeffects(monkeypatch)

        execution.send_to_unit("F-001-U-1", "reviewer", "thanks")

        assert state.get_unit_state("F-001-U-1").status == "done"


# --------------------------- dashboard surfaces the unit ---------------------------


class TestDashboardAwaitingMergeBucket:
    """``show_dashboard``'s 'Awaiting your merge' bucket queries the new
    status — closes the contradiction the audit caught (Gap H: the bucket
    previously queried for events no codepath wrote)."""

    def test_data_fetcher_picks_up_approved_awaiting_merge_status(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="approved_awaiting_merge",
                pr_number=42,
                branch="feat/x",
            )
        )

        rows = dashboard._awaiting_merge_data()
        assert len(rows) == 1
        assert rows[0]["unit_id"] == "F-001-U-1"
        assert rows[0]["pr"] == "#42"

    def test_data_fetcher_ignores_other_statuses(self, tmp_state_db):
        """A unit with a stale reviewer_recommend_merge event but a different
        status must NOT appear — status is authoritative."""
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="in_ci", pr_number=42)
        )
        state.record_event("F-001-U-1", "F-001", "reviewer_recommend_merge", source="reviewer")

        assert dashboard._awaiting_merge_data() == []

    def test_render_markdown_surfaces_the_pr(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="approved_awaiting_merge",
                pr_number=99,
                branch="feat/x",
            )
        )

        md = dashboard.render_markdown()
        # PR number lands inside the "Awaiting your merge" section.
        assert "## 🟢 Awaiting your merge" in md
        section = md.split("## 🟢 Awaiting your merge", 1)[1].split("## ", 1)[0]
        assert "#99" in section
        assert "F-001-U-1" in section


# --------------------------- next_ready_units treats it as inactive dep ---------------------------


class TestNextReadyUnitsRespectsAwaitingMerge:
    """A unit in ``approved_awaiting_merge`` does NOT count as a satisfied
    dep — downstream work waits for the actual merge. The unit itself
    surfaces in its own ``awaiting_merge`` bucket: distinct from
    ``in_flight`` (no agent running) and from ``escalated`` (cycle
    finished cleanly), and explicitly NOT under ``in_flight`` (matching
    the assertions below)."""

    def test_downstream_dep_stays_blocked_when_dep_is_awaiting_merge(self, tmp_state_db):
        u1, _u2 = _seed_two_unit_plan("F-100")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=u1,
                feature_id="F-100",
                status="approved_awaiting_merge",
                pr_number=5,
            )
        )

        parsed = json.loads(scheduling.next_ready_units("F-100"))
        # U-2 must NOT be in ready_to_spawn (its dep U-1 is awaiting merge,
        # not yet done).
        ready_ids = [u["unit_id"] for u in parsed["ready_to_spawn"]]
        assert "F-100-U-2" not in ready_ids
        # U-1 itself surfaces in its own ``awaiting_merge`` bucket — distinct
        # from ``in_flight`` (no agent is running) and from ``escalated``
        # (the cycle finished cleanly); the lead can see it's pending
        # human action.
        awaiting_ids = [u["unit_id"] for u in parsed["awaiting_merge"]]
        assert u1 in awaiting_ids
        in_flight_ids = [u["unit_id"] for u in parsed["in_flight"]]
        assert u1 not in in_flight_ids

    def test_downstream_unblocks_after_dep_flips_to_done(self, tmp_state_db):
        """Mirror: once the human merges and reconcile flips to ``done``,
        the downstream unit IS unblocked. Pins the contrast with the
        awaiting-merge branch above."""
        u1, u2 = _seed_two_unit_plan("F-101")
        state.upsert_unit_state(
            WorkUnitState(unit_id=u1, feature_id="F-101", status="done", pr_number=5)
        )

        parsed = json.loads(scheduling.next_ready_units("F-101"))
        ready_ids = [u["unit_id"] for u in parsed["ready_to_spawn"]]
        assert u2 in ready_ids

    def test_next_ready_units_all_aggregates_awaiting_merge(self, tmp_state_db):
        """next_ready_units_all delegates per-feature — the awaiting-merge
        bucket must compose across features without surprises."""
        u1, _u2 = _seed_two_unit_plan("F-102")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=u1,
                feature_id="F-102",
                status="approved_awaiting_merge",
                pr_number=5,
            )
        )

        parsed = json.loads(scheduling.next_ready_units_all())
        ready_ids = [u["unit_id"] for u in parsed["ready_to_spawn"]]
        assert "F-102-U-2" not in ready_ids
        # The awaiting-merge unit lands in the aggregated bucket with its
        # feature_id annotation.
        awaiting = parsed["awaiting_merge"]
        assert any(u["unit_id"] == u1 and u["feature_id"] == "F-102" for u in awaiting)
        assert parsed["total_awaiting_merge"] == 1


# --------------------------- list_in_flight surfaces it ---------------------------


class TestListInFlightSurfacesAwaitingMerge:
    """``list_in_flight`` includes ``approved_awaiting_merge`` so a restart-
    recovery lookup or 'what's pending?' query surfaces units waiting on
    human action alongside the agent-active ones."""

    def test_list_in_flight_includes_awaiting_merge(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="approved_awaiting_merge",
                pr_number=5,
            )
        )

        parsed = json.loads(ops.list_in_flight())
        ids = [r["unit_id"] for r in parsed]
        assert "F-001-U-1" in ids
        row = next(r for r in parsed if r["unit_id"] == "F-001-U-1")
        assert row["status"] == "approved_awaiting_merge"

    def test_list_in_flight_still_excludes_done_and_escalated_by_default(self, tmp_state_db):
        _seed_feature()
        state.upsert_unit_state(WorkUnitState(unit_id="U-done", feature_id="F-001", status="done"))
        state.upsert_unit_state(
            WorkUnitState(unit_id="U-esc", feature_id="F-001", status="escalated")
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="U-awm", feature_id="F-001", status="approved_awaiting_merge")
        )

        parsed = json.loads(ops.list_in_flight())
        ids = {r["unit_id"] for r in parsed}
        assert ids == {"U-awm"}


# --------------------------- reconcile_unit_pr: awaiting_merge + merged ---------------------------


def _stub_pr_merged(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-20T12:00:00Z",
            "head_sha": "deadbeef",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 1, "conclusion_counts": {"success": 1}, "runs": []},
    )


def _stub_pr_open(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


class TestReconcileUnitPrAwaitingMerge:
    """reconcile_unit_pr learns the ``approved_awaiting_merge + merged`` branch:
    flips to ``done``, emits ``merged``. Still no-ops on an open PR."""

    def test_reconcile_flips_awaiting_merge_to_done_on_observed_merge(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="approved_awaiting_merge",
                pr_number=5,
                branch="feat/x",
            )
        )
        _stub_pr_merged(monkeypatch)

        parsed = json.loads(ops.reconcile_unit_pr("F-001-U-1"))
        assert parsed["reconciled"] is True
        assert parsed["action"] == "merged-from-approved_awaiting_merge"
        assert parsed["orchestrator_status"] == "done"
        assert state.get_unit_state("F-001-U-1").status == "done"

        event_types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert event_types.count("merged") == 1
        # NOT a recovery from escalated — that's a different branch's audit row.
        assert "recovered_from_escalated" not in event_types

    def test_reconcile_open_pr_keeps_awaiting_merge_status(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """If the human hasn't clicked merge yet, reconcile must leave the
        row alone — no-op, no events."""
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="approved_awaiting_merge",
                pr_number=5,
                branch="feat/x",
            )
        )
        _stub_pr_open(monkeypatch)

        pre_events = state.list_events("F-001-U-1")
        parsed = json.loads(ops.reconcile_unit_pr("F-001-U-1"))

        assert parsed["reconciled"] is False
        assert parsed["action"] == "no-op-pr-not-merged"
        assert state.get_unit_state("F-001-U-1").status == "approved_awaiting_merge"
        assert state.list_events("F-001-U-1") == pre_events

    def test_reconcile_idempotent_after_awaiting_merge_promotion(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Second call after the flip must take the ``no-op-already-done``
        branch — not re-emit ``merged``."""
        _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="approved_awaiting_merge",
                pr_number=5,
            )
        )
        _stub_pr_merged(monkeypatch)

        json.loads(ops.reconcile_unit_pr("F-001-U-1"))  # first call flips
        parsed2 = json.loads(ops.reconcile_unit_pr("F-001-U-1"))
        assert parsed2["action"] == "no-op-already-done"
        assert parsed2["reconciled"] is False

        event_types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert event_types.count("merged") == 1  # exactly one


# --------------------------- constant + literal pinning ---------------------------


class TestModelConstants:
    def test_unit_status_literal_includes_approved_awaiting_merge(self):
        """The Literal lives in models.py — pin the membership so the
        docstring claim 'add to the UnitStatus Literal' can't silently
        regress."""
        import typing

        from orchestrator.models import UnitStatus

        args = typing.get_args(UnitStatus)
        assert "approved_awaiting_merge" in args

    def test_ready_to_merge_statuses_contains_only_awaiting_merge(self):
        """READY_TO_MERGE_STATUSES is the named bucket the unit description
        asked for — pin its membership so future statuses (e.g. an
        ``awaiting_ultrareview`` bucket if F-007's ultrareview ever lands
        as its own status) get added explicitly, not implicitly."""
        assert frozenset({"approved_awaiting_merge"}) == READY_TO_MERGE_STATUSES


def test_show_dashboard_surfaces_approved_awaiting_merge_in_chat_section(tmp_state_db):
    """End-to-end pin on the user-visible markdown: the bucket section is
    populated by the new status. ``show_dashboard`` is the lead's view; if
    the section's table doesn't include the unit row, the lead can't tell
    the user what PRs are pending merge."""
    state.save_feature(
        Feature(
            id="F-001",
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
        )
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-001-U-1",
            feature_id="F-001",
            status="approved_awaiting_merge",
            pr_number=42,
            branch="feat/x",
        )
    )

    md = dashboard.render_markdown()
    section = md.split("## 🟢 Awaiting your merge", 1)[1].split("## ", 1)[0]
    # Both the unit id and the PR are present; the empty-state placeholder
    # must NOT be — proves the table got populated. The placeholder is
    # rendered as ``_none awaiting merge_`` (markdown italics), so we
    # match the literal string the renderer emits — a ``\bword\b`` regex
    # wouldn't fire because ``_`` is a word character.
    assert "F-001-U-1" in section
    assert "#42" in section
    assert "_none awaiting merge_" not in section
