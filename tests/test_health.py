"""Tests for orchestrator/health.py — probe + decision table (with shadow output).

The module is two pure functions:

* ``probe_unit_health`` does *all* its I/O through two injected clients
  (a ``GitHubHealthClient`` and an ``AnthropicHealthClient``) — no network,
  no shell, no state.db reads of its own. Local context (the unit's
  ``WorkUnitState`` row, the downstream-blocked count) is passed in
  explicitly by the caller.
* ``decide_transitions`` is a pure decision table over the
  ``(local_state, report)`` pair, returning a ``Decision`` with two
  buckets: ``actions_to_apply`` (live transitions / events the MCP layer
  should execute) and ``shadow_decisions`` (rules-in-flight that the
  module *would* fire but we want to observe-only before promoting).

Tests use lightweight fake clients (plain classes with the protocol's
method signatures) rather than ``MagicMock`` so the table tests double as
executable documentation of the protocol shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator import health
from orchestrator.models import WorkUnitState

# --------------------------- fake clients ---------------------------


@dataclass
class FakeGH:
    """Stand-in for the production ``GitHubHealthClient``.

    Every field is a per-unit canned response. Tests build one of these,
    pass it to ``probe_unit_health``, and assert against the resulting
    ``HealthReport``. None / missing fields propagate as ``None`` /
    empty list — the real client makes the same choice when GitHub
    returns 404 / empty arrays.
    """

    pr: dict | None = None
    check_runs: list[dict] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    compare: dict = field(default_factory=lambda: {"ahead_by": 0, "behind_by": 0})
    reviews: list[dict] = field(default_factory=list)
    review_threads: list[dict] = field(default_factory=list)
    requested_reviewers: dict = field(default_factory=lambda: {"users": [], "teams": []})
    copilot_review: dict | None = None
    last_force_push_at: str | None = None
    head_commit: dict = field(default_factory=dict)
    merge_commit_on_main: bool = True

    def get_pr(self, unit_id: str) -> dict | None:
        return self.pr

    def get_check_runs(self, unit_id: str) -> list[dict]:
        return list(self.check_runs)

    def get_required_checks(self, unit_id: str) -> list[str]:
        return list(self.required_checks)

    def get_compare_to_base(self, unit_id: str) -> dict:
        return dict(self.compare)

    def get_reviews(self, unit_id: str) -> list[dict]:
        return list(self.reviews)

    def get_review_threads(self, unit_id: str) -> list[dict]:
        return list(self.review_threads)

    def get_requested_reviewers(self, unit_id: str) -> dict:
        return dict(self.requested_reviewers)

    def get_copilot_review(self, unit_id: str) -> dict | None:
        return self.copilot_review

    def get_last_force_push_at(self, unit_id: str) -> str | None:
        return self.last_force_push_at

    def get_head_commit(self, unit_id: str) -> dict:
        return dict(self.head_commit)

    def is_merge_commit_on_main(self, unit_id: str) -> bool:
        return self.merge_commit_on_main


@dataclass
class FakeAnthropic:
    """Stand-in for the production ``AnthropicHealthClient``.

    Maps session_id → status. Unknown session ids return ``None``
    (matches the production client's "no such session" path).
    """

    statuses: dict[str, str] = field(default_factory=dict)

    def get_session_status(self, session_id: str) -> str | None:
        return self.statuses.get(session_id)


# --------------------------- builders ---------------------------


def _now() -> datetime:
    return datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


def _state(
    unit_id: str = "F-001-U-1",
    feature_id: str = "F-001",
    status: str = "in_ci",
    pr_number: int | None = 42,
    branch: str = "feat/f-001-u-1",
    review_round: int = 0,
    last_activity: str | None = None,
    last_error: str = "",
    coder_session_id: str = "sesn_coder",
    tester_session_id: str = "",
    reviewer_session_id: str = "",
) -> WorkUnitState:
    return WorkUnitState(
        unit_id=unit_id,
        feature_id=feature_id,
        status=status,
        branch=branch,
        pr_number=pr_number,
        coder_session_id=coder_session_id,
        tester_session_id=tester_session_id,
        reviewer_session_id=reviewer_session_id,
        review_round=review_round,
        last_activity=last_activity or (_now() - timedelta(minutes=5)).isoformat(),
        last_error=last_error,
    )


def _pr(
    *,
    state_: str = "open",
    merged: bool = False,
    mergeable: bool | None = True,
    mergeable_state: str = "clean",
    head_sha: str = "deadbeef",
    merge_commit_sha: str | None = None,
    merged_at: str | None = None,
    base: str = "main",
) -> dict:
    return {
        "state": state_,
        "merged": merged,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "head_sha": head_sha,
        "merge_commit_sha": merge_commit_sha,
        "merged_at": merged_at,
        "base": base,
    }


def _run(name: str, conclusion: str | None, status: str = "completed", url: str = "u") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "details_url": url}


# ============================================================================
# probe_unit_health
# ============================================================================


class TestProbeUnitHealthPR:
    def test_pr_snapshot_passthrough(self):
        gh = FakeGH(pr=_pr(state_="open", mergeable=True, mergeable_state="clean"))
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.pr is not None
        assert report.pr.state == "open"
        assert report.pr.merged is False
        assert report.pr.mergeable is True
        assert report.pr.mergeable_state == "clean"
        assert report.pr.conflict_files == []
        assert report.pr.head_sha == "deadbeef"

    def test_pr_missing_returns_none(self):
        report = health.probe_unit_health(
            "U-1", FakeGH(pr=None), FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.pr is None

    def test_conflict_files_carried_when_mergeable_state_dirty(self):
        gh = FakeGH(
            pr={**_pr(mergeable=False, mergeable_state="dirty"), "conflict_files": ["a.py", "b.py"]}
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.pr is not None
        assert report.pr.conflict_files == ["a.py", "b.py"]
        assert report.pr.mergeable_state == "dirty"

    def test_pr_merged_state(self):
        gh = FakeGH(
            pr=_pr(
                state_="closed",
                merged=True,
                merge_commit_sha="abc123",
                merged_at="2026-05-20T10:00:00Z",
            )
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(status="in_ci"), now=_now()
        )
        assert report.pr is not None
        assert report.pr.merged is True
        assert report.pr.merge_commit_sha == "abc123"


class TestProbeUnitHealthGit:
    def test_ahead_behind_passthrough(self):
        gh = FakeGH(pr=_pr(), compare={"ahead_by": 3, "behind_by": 1})
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.git.ahead_by == 3
        assert report.git.behind_by == 1

    def test_head_sha_and_age_from_head_commit(self):
        committed = _now() - timedelta(hours=2)
        gh = FakeGH(
            pr=_pr(head_sha="cafef00d"),
            head_commit={"sha": "cafef00d", "committed_at": committed.isoformat()},
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.git.head_sha == "cafef00d"
        # 2 hours = 7200 seconds
        assert report.git.head_age_seconds == 7200

    def test_last_force_push_at_carried(self):
        gh = FakeGH(pr=_pr(), last_force_push_at="2026-05-21T11:30:00+00:00")
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.git.last_force_push_at == "2026-05-21T11:30:00+00:00"

    def test_head_age_none_when_committed_at_missing(self):
        gh = FakeGH(pr=_pr(), head_commit={"sha": "x"})
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.git.head_age_seconds is None


class TestProbeUnitHealthCI:
    def test_runs_summary_and_pending_failing_split(self):
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                _run("lint", "success"),
                _run("test", "failure", url="https://ci/logs/123"),
                _run("build", None, status="in_progress"),
            ],
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        names = [r.name for r in report.ci.runs]
        assert names == ["lint", "test", "build"]
        # failing run's details_url is preserved (log link for tester / lead)
        failing = [r for r in report.ci.runs if r.conclusion == "failure"][0]
        assert failing.details_url == "https://ci/logs/123"
        assert "lint" not in report.ci.failing
        assert report.ci.failing == ["test"]
        assert report.ci.pending == ["build"]

    def test_required_vs_actual_delta(self):
        gh = FakeGH(
            pr=_pr(),
            check_runs=[_run("lint", "success"), _run("test", "success")],
            required_checks=["lint", "test", "type-check"],
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.ci.required == ["lint", "test", "type-check"]
        assert report.ci.missing_required == ["type-check"]

    def test_no_required_means_no_delta(self):
        gh = FakeGH(pr=_pr(), check_runs=[_run("lint", "success")])
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.ci.required == []
        assert report.ci.missing_required == []

    def test_skipped_and_neutral_conclusions_count_as_passing(self):
        # Matches ``ci_wait.PASSING_CONCLUSIONS`` — ``skipped`` covers
        # path-filtered checks GitHub never ran; ``neutral`` covers
        # decided-not-to-fail outcomes.
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                _run("lint", "success"),
                _run("typecheck", "skipped"),
                _run("docs", "neutral"),
            ],
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.ci.failing == []
        assert report.ci.pending == []

    def test_cancelled_timed_out_action_required_stale_count_as_failing(self):
        # Matches ``ci_wait.FAILURE_CONCLUSIONS`` — ``cancelled`` /
        # ``timed_out`` / ``action_required`` / ``stale`` are all
        # treated as failures alongside the canonical ``failure``.
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                _run("a", "cancelled"),
                _run("b", "timed_out"),
                _run("c", "action_required"),
                _run("d", "stale"),
                _run("e", "failure"),
            ],
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert set(report.ci.failing) == {"a", "b", "c", "d", "e"}
        assert report.ci.pending == []


class TestProbeUnitHealthReviews:
    def test_approval_and_changes_counts(self):
        gh = FakeGH(
            pr=_pr(),
            reviews=[
                {"state": "APPROVED", "user": {"login": "alice"}, "dismissed": False},
                {"state": "APPROVED", "user": {"login": "bob"}, "dismissed": False},
                {"state": "CHANGES_REQUESTED", "user": {"login": "carol"}, "dismissed": False},
                {"state": "DISMISSED", "user": {"login": "dave"}, "dismissed": True},
            ],
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.reviews.approvals == 2
        assert report.reviews.changes_requested == 1
        assert report.reviews.dismissed == 1

    def test_unresolved_threads_counted(self):
        gh = FakeGH(
            pr=_pr(),
            review_threads=[
                {"is_resolved": True, "is_outdated": False},
                {"is_resolved": False, "is_outdated": False},
                {"is_resolved": False, "is_outdated": False},
                {"is_resolved": False, "is_outdated": True},  # outdated, skipped
            ],
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        # Only non-resolved, non-outdated threads count as "open work".
        assert report.reviews.unresolved_threads == 2

    def test_codeowner_requested_reviewers_listed(self):
        gh = FakeGH(
            pr=_pr(),
            requested_reviewers={"users": ["alice"], "teams": ["sec-eng"]},
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        # Users + teams concatenated; teams get a ``team:`` prefix to
        # disambiguate from user logins (a team name and a user login can collide).
        assert "alice" in report.reviews.codeowner_requested
        assert "team:sec-eng" in report.reviews.codeowner_requested

    def test_copilot_review_present(self):
        gh = FakeGH(
            pr=_pr(),
            copilot_review={"state": "COMMENTED", "inline_count": 4},
        )
        report = health.probe_unit_health(
            "U-1", gh, FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.reviews.copilot_present is True
        assert report.reviews.copilot_state == "COMMENTED"

    def test_copilot_review_absent(self):
        report = health.probe_unit_health(
            "U-1", FakeGH(pr=_pr()), FakeAnthropic(), local_state=_state(), now=_now()
        )
        assert report.reviews.copilot_present is False
        assert report.reviews.copilot_state is None


class TestProbeUnitHealthWorkers:
    def test_worker_sessions_per_role(self):
        anth = FakeAnthropic(statuses={"c": "running", "t": "idle", "r": "terminated"})
        st = _state(coder_session_id="c", tester_session_id="t", reviewer_session_id="r")
        report = health.probe_unit_health("U-1", FakeGH(pr=_pr()), anth, local_state=st, now=_now())
        by_role = {w.role: w for w in report.workers}
        assert by_role["coder"].session_status == "running"
        assert by_role["tester"].session_status == "idle"
        assert by_role["reviewer"].session_status == "terminated"

    def test_worker_role_skipped_when_no_session_id(self):
        # Tester role has no session_id stored — not yet spawned. Skip it
        # rather than emitting a noisy "not_found" worker entry.
        st = _state(coder_session_id="c", tester_session_id="", reviewer_session_id="")
        report = health.probe_unit_health(
            "U-1",
            FakeGH(pr=_pr()),
            FakeAnthropic(statuses={"c": "idle"}),
            local_state=st,
            now=_now(),
        )
        roles = [w.role for w in report.workers]
        assert roles == ["coder"]


class TestProbeUnitHealthAgeParsing:
    def test_naive_timestamp_assumed_utc(self):
        # A pre-UTC fixture (no offset suffix) should still produce a
        # finite age — the parser normalizes naive datetimes to UTC
        # rather than raising on the tz-aware subtraction.
        st = _state(last_activity="2026-05-21T11:00:00")
        report = health.probe_unit_health(
            "U-1", FakeGH(pr=_pr()), FakeAnthropic(), local_state=st, now=_now()
        )
        # 1 hour difference vs the fixed _now() at 12:00 UTC.
        assert report.orchestrator.last_activity_age_seconds == 3600


class TestProbeUnitHealthOrchestrator:
    def test_cycle_and_cap_proximity(self):
        st = _state(review_round=2)
        report = health.probe_unit_health(
            "U-1", FakeGH(pr=_pr()), FakeAnthropic(), local_state=st, now=_now(), cycle_cap=3
        )
        assert report.orchestrator.cycle == 2
        assert report.orchestrator.cycle_cap == 3
        assert report.orchestrator.cycles_remaining == 1

    def test_last_activity_age_in_seconds(self):
        st = _state(last_activity=(_now() - timedelta(minutes=15)).isoformat())
        report = health.probe_unit_health(
            "U-1", FakeGH(pr=_pr()), FakeAnthropic(), local_state=st, now=_now()
        )
        assert report.orchestrator.last_activity_age_seconds == 900

    def test_downstream_blocked_passthrough(self):
        report = health.probe_unit_health(
            "U-1",
            FakeGH(pr=_pr()),
            FakeAnthropic(),
            local_state=_state(),
            downstream_blocked=4,
            now=_now(),
        )
        assert report.orchestrator.downstream_blocked == 4

    def test_unparseable_last_activity_age_is_none(self):
        st = _state(last_activity="not-a-timestamp")
        report = health.probe_unit_health(
            "U-1", FakeGH(pr=_pr()), FakeAnthropic(), local_state=st, now=_now()
        )
        assert report.orchestrator.last_activity_age_seconds is None


# ============================================================================
# decide_transitions — actions_to_apply
# ============================================================================


def _report(
    *,
    status: str = "in_ci",
    pr: dict | None = None,
    review_round: int = 0,
    last_error: str = "",
    check_runs: list[dict] | None = None,
    required: list[str] | None = None,
    reviews: list[dict] | None = None,
    threads: list[dict] | None = None,
    workers_statuses: dict[str, str] | None = None,
    merge_commit_on_main: bool = True,
):
    gh = FakeGH(
        pr=pr,
        check_runs=check_runs or [],
        required_checks=required or [],
        reviews=reviews or [],
        review_threads=threads or [],
        merge_commit_on_main=merge_commit_on_main,
    )
    anth = FakeAnthropic(statuses=workers_statuses or {})
    st = _state(status=status, review_round=review_round, last_error=last_error)
    return st, health.probe_unit_health("U-1", gh, anth, local_state=st, now=_now())


class TestDecideTransitionsMergedTransitions:
    """The three existing reconcile cells from ``ops.reconcile_unit_pr``."""

    def test_merged_plus_in_ci_emits_done_transition_and_merged_event(self):
        st, rep = _report(
            status="in_ci",
            pr=_pr(state_="closed", merged=True, merged_at="2026-05-20T10:00:00Z"),
        )
        decision = health.decide_transitions(st, rep)
        kinds = [(a.kind, a.target_status, a.event_type) for a in decision.actions_to_apply]
        assert ("transition", "done", None) in kinds
        assert ("event", None, "merged") in kinds
        # No spurious shadow cell for the canonical happy path.
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" not in names

    def test_merged_plus_approved_awaiting_merge_emits_done_and_merged(self):
        st, rep = _report(
            status="approved_awaiting_merge",
            pr=_pr(state_="closed", merged=True, merged_at="2026-05-20T10:00:00Z"),
        )
        decision = health.decide_transitions(st, rep)
        statuses = [a.target_status for a in decision.actions_to_apply if a.kind == "transition"]
        events = [a.event_type for a in decision.actions_to_apply if a.kind == "event"]
        assert statuses == ["done"]
        assert "merged" in events
        # No recovered_from_escalated on this branch (was not escalated).
        assert "recovered_from_escalated" not in events

    def test_merged_plus_escalated_emits_recovered_event_and_clears_last_error(self):
        st, rep = _report(
            status="escalated",
            pr=_pr(state_="closed", merged=True, merged_at="2026-05-20T10:00:00Z"),
            last_error="cycle-3 cap hit on reviewer feedback",
        )
        decision = health.decide_transitions(st, rep)
        transitions = [a for a in decision.actions_to_apply if a.kind == "transition"]
        events = [a for a in decision.actions_to_apply if a.kind == "event"]
        assert len(transitions) == 1
        assert transitions[0].target_status == "done"
        assert transitions[0].clear_error is True
        event_types = [a.event_type for a in events]
        assert "merged" in event_types
        assert "recovered_from_escalated" in event_types
        # Prior last_error preserved as the recovered event's details so
        # the audit log keeps the diagnostic.
        recovered = [a for a in events if a.event_type == "recovered_from_escalated"][0]
        assert "cycle-3 cap hit" in recovered.details

    def test_merged_with_merge_commit_sha_emits_cycle_log_side_effect(self):
        st, rep = _report(
            status="in_ci",
            pr=_pr(
                state_="closed",
                merged=True,
                merge_commit_sha="abc123",
                merged_at="2026-05-20T10:00:00Z",
            ),
        )
        decision = health.decide_transitions(st, rep)
        side_effects = [a for a in decision.actions_to_apply if a.kind == "side_effect"]
        assert len(side_effects) == 1
        assert side_effects[0].side_effect == "write_cycle_log"
        assert side_effects[0].payload.get("merge_commit_sha") == "abc123"
        # Payload carries the full call shape so a downstream executor
        # can splat into ``cycle_log.write_cycle_log`` without an
        # implicit local_state lookup (matches the ops.py call site at
        # ``ops.py:246-251``).
        assert side_effects[0].payload.get("unit_id") == st.unit_id
        assert (
            side_effects[0].payload.get("commit_message")
            == f"cycle-log: backfill merge SHA for {st.unit_id}"
        )

    def test_cycle_log_side_effect_skipped_on_active_role_refusal(self):
        # ``merged + active-role`` emits ``reconcile_refused`` and ops.py
        # does NOT call the cycle-log writer on that cell. Health must
        # match ops.py — finalising a log from an in-flight unit_events
        # tail would capture a partial cycle.
        for status in ("coding", "testing", "opening_pr", "reviewing", "fixing"):
            st, rep = _report(
                status=status,
                pr=_pr(state_="closed", merged=True, merge_commit_sha="abc123"),
            )
            decision = health.decide_transitions(st, rep)
            side_effects = [a for a in decision.actions_to_apply if a.kind == "side_effect"]
            assert side_effects == [], f"unexpected cycle-log side_effect for status {status!r}"

    def test_cycle_log_side_effect_skipped_on_pending(self):
        # ``merged + pending`` falls through ops.reconcile_unit_pr's
        # action table (no merged-from-*, no no-op-already-done) so the
        # writer doesn't fire there either.
        st, rep = _report(
            status="pending",
            pr=_pr(state_="closed", merged=True, merge_commit_sha="abc123"),
        )
        decision = health.decide_transitions(st, rep)
        side_effects = [a for a in decision.actions_to_apply if a.kind == "side_effect"]
        assert side_effects == []

    def test_merged_with_missing_merge_commit_sha_no_cycle_log_side_effect(self):
        # GitHub races merge_commit_sha population — first poll right after
        # merge can carry merged=True with the sha still null. We don't
        # schedule the writer until the sha arrives.
        st, rep = _report(
            status="in_ci",
            pr=_pr(state_="closed", merged=True, merge_commit_sha=None),
        )
        decision = health.decide_transitions(st, rep)
        side_effects = [a for a in decision.actions_to_apply if a.kind == "side_effect"]
        assert side_effects == []

    def test_open_pr_no_transitions(self):
        st, rep = _report(status="in_ci", pr=_pr(state_="open", merged=False))
        decision = health.decide_transitions(st, rep)
        assert [a for a in decision.actions_to_apply if a.kind == "transition"] == []

    def test_closed_unmerged_pr_no_transitions(self):
        st, rep = _report(status="in_ci", pr=_pr(state_="closed", merged=False))
        decision = health.decide_transitions(st, rep)
        assert [a for a in decision.actions_to_apply if a.kind == "transition"] == []

    def test_merged_plus_done_is_idempotent(self):
        # A second probe after a unit has already flipped to done emits no
        # transition (status would not change) and no events — matches
        # reconcile_unit_pr's "no-op-already-done" branch.
        st, rep = _report(
            status="done",
            pr=_pr(state_="closed", merged=True, merged_at="2026-05-20T10:00:00Z"),
        )
        decision = health.decide_transitions(st, rep)
        transitions = [a for a in decision.actions_to_apply if a.kind == "transition"]
        events = [
            a for a in decision.actions_to_apply if a.kind == "event" and a.event_type == "merged"
        ]
        assert transitions == []
        assert events == []

    def test_merged_without_merged_at_uses_fallback_summary(self):
        # GitHub can race the merged_at column null on the first poll
        # right after a merge — the renderer must still emit a useful
        # summary string rather than e.g. "merged at None".
        st, rep = _report(
            status="in_ci",
            pr=_pr(state_="closed", merged=True, merged_at=None),
        )
        decision = health.decide_transitions(st, rep)
        merged_events = [a for a in decision.actions_to_apply if a.event_type == "merged"]
        assert len(merged_events) == 1
        assert "merged" in merged_events[0].summary.lower()
        assert "None" not in merged_events[0].summary

    def test_merged_plus_active_role_emits_refusal_event_no_transition(self):
        # An active-role status (coding/testing/reviewing/fixing) observing
        # a merged PR is racy — refuse to advance, document the policy.
        # Matches reconcile_unit_pr's ``_RECONCILE_REFUSED_STATUSES`` branch.
        for status in ("coding", "testing", "reviewing", "fixing", "opening_pr"):
            st, rep = _report(
                status=status,
                pr=_pr(state_="closed", merged=True, merged_at="2026-05-20T10:00:00Z"),
            )
            decision = health.decide_transitions(st, rep)
            transitions = [a for a in decision.actions_to_apply if a.kind == "transition"]
            assert transitions == [], f"unexpected transition from active status {status!r}"
            event_types = [a.event_type for a in decision.actions_to_apply if a.kind == "event"]
            assert "reconcile_refused" in event_types, f"missing refusal for status {status!r}"


class TestDecideTransitionsConflict:
    def test_conflict_detected_event_only(self):
        st, rep = _report(
            status="in_ci",
            pr={**_pr(mergeable=False, mergeable_state="dirty"), "conflict_files": ["x.py"]},
        )
        decision = health.decide_transitions(st, rep)
        conflict = [a for a in decision.actions_to_apply if a.event_type == "pr_conflict_detected"]
        assert len(conflict) == 1
        assert conflict[0].kind == "event"
        # File list carried in payload (structured) AND in details (audit summary).
        assert conflict[0].payload.get("conflict_files") == ["x.py"]
        assert "x.py" in conflict[0].details
        # Event-only signal — no status change.
        transitions = [a for a in decision.actions_to_apply if a.kind == "transition"]
        assert transitions == []

    def test_conflicting_mergeable_state_also_fires(self):
        st, rep = _report(
            status="in_ci",
            pr={
                **_pr(mergeable=False, mergeable_state="conflicting"),
                "conflict_files": ["a", "b"],
            },
        )
        decision = health.decide_transitions(st, rep)
        names = [a.event_type for a in decision.actions_to_apply]
        assert "pr_conflict_detected" in names

    def test_clean_pr_no_conflict_event(self):
        st, rep = _report(status="in_ci", pr=_pr(mergeable=True, mergeable_state="clean"))
        decision = health.decide_transitions(st, rep)
        names = [a.event_type for a in decision.actions_to_apply]
        assert "pr_conflict_detected" not in names


class TestDecideTransitionsRequiredCheckMissing:
    def test_required_check_missing_event(self):
        st, rep = _report(
            status="in_ci",
            pr=_pr(),
            check_runs=[_run("lint", "success")],
            required=["lint", "test"],
        )
        decision = health.decide_transitions(st, rep)
        missing = [a for a in decision.actions_to_apply if a.event_type == "required_check_missing"]
        assert len(missing) == 1
        assert missing[0].payload.get("missing") == ["test"]
        # Event-only — no transition.
        assert [a for a in decision.actions_to_apply if a.kind == "transition"] == []

    def test_all_required_checks_present_no_event(self):
        st, rep = _report(
            status="in_ci",
            pr=_pr(),
            check_runs=[_run("lint", "success"), _run("test", "success")],
            required=["lint", "test"],
        )
        decision = health.decide_transitions(st, rep)
        names = [a.event_type for a in decision.actions_to_apply]
        assert "required_check_missing" not in names


class TestDecideTransitionsCIDrift:
    def test_ci_drift_event_sets_last_error_no_status_change(self):
        # Status reports the unit is happy (approved_awaiting_merge) but CI
        # has gone red — drift. Flag it via event + last_error; don't
        # silently flip status back to "fixing" (the decision table is
        # observe-and-log, not auto-repair).
        st, rep = _report(
            status="approved_awaiting_merge",
            pr=_pr(),
            check_runs=[_run("lint", "success"), _run("test", "failure", url="https://ci/789")],
        )
        decision = health.decide_transitions(st, rep)
        drift = [a for a in decision.actions_to_apply if a.event_type == "ci_drift_detected"]
        assert len(drift) == 1
        # The action carries an error string the executor will write into
        # ``last_error`` — this is what "sets last_error" means at the
        # decision-table layer (no status change in the same action).
        assert drift[0].set_last_error
        assert "test" in drift[0].set_last_error
        assert [a for a in decision.actions_to_apply if a.kind == "transition"] == []

    def test_ci_drift_quiet_when_status_is_actively_fixing(self):
        # Status already reflects the failure (fixing); CI red is expected
        # and not "drift" — don't double-flag.
        st, rep = _report(
            status="fixing",
            pr=_pr(),
            check_runs=[_run("test", "failure")],
        )
        decision = health.decide_transitions(st, rep)
        names = [a.event_type for a in decision.actions_to_apply]
        assert "ci_drift_detected" not in names

    def test_ci_green_no_drift_event(self):
        st, rep = _report(
            status="approved_awaiting_merge",
            pr=_pr(),
            check_runs=[_run("lint", "success"), _run("test", "success")],
        )
        decision = health.decide_transitions(st, rep)
        names = [a.event_type for a in decision.actions_to_apply]
        assert "ci_drift_detected" not in names


# ============================================================================
# decide_transitions — shadow_decisions
# ============================================================================


class TestShadowDecisionEscalatedReset:
    def test_fires_when_escalated_ci_green_approved_no_threads(self):
        st, rep = _report(
            status="escalated",
            pr=_pr(),
            check_runs=[_run("lint", "success"), _run("test", "success")],
            reviews=[{"state": "APPROVED", "user": {"login": "alice"}, "dismissed": False}],
            threads=[],
            last_error="prior cap-3",
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" in names
        shadow = [
            s for s in decision.shadow_decisions if s.rule_name == "escalated_to_in_ci_reset"
        ][0]
        assert shadow.predicted_action.kind == "transition"
        assert shadow.predicted_action.target_status == "in_ci"
        # Trigger inputs are structured (not free-form) so a future
        # promote-to-live commit can table-test the same predicate.
        assert shadow.trigger_inputs == {
            "status": "escalated",
            "ci_green": True,
            "approvals": 1,
            "unresolved_threads": 0,
            "changes_requested": 0,
        }
        # Not in actions_to_apply (shadow-only by design).
        applied_statuses = [
            a.target_status for a in decision.actions_to_apply if a.kind == "transition"
        ]
        assert "in_ci" not in applied_statuses

    def test_no_shadow_when_ci_red(self):
        st, rep = _report(
            status="escalated",
            pr=_pr(),
            check_runs=[_run("test", "failure")],
            reviews=[{"state": "APPROVED", "user": {"login": "a"}, "dismissed": False}],
            threads=[],
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" not in names

    def test_no_shadow_when_no_approval(self):
        st, rep = _report(
            status="escalated",
            pr=_pr(),
            check_runs=[_run("test", "success")],
            reviews=[],
            threads=[],
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" not in names

    def test_no_shadow_when_open_threads(self):
        st, rep = _report(
            status="escalated",
            pr=_pr(),
            check_runs=[_run("test", "success")],
            reviews=[{"state": "APPROVED", "user": {"login": "a"}, "dismissed": False}],
            threads=[{"is_resolved": False, "is_outdated": False}],
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" not in names

    def test_no_shadow_when_outstanding_changes_requested(self):
        st, rep = _report(
            status="escalated",
            pr=_pr(),
            check_runs=[_run("test", "success")],
            reviews=[
                {"state": "APPROVED", "user": {"login": "a"}, "dismissed": False},
                {"state": "CHANGES_REQUESTED", "user": {"login": "b"}, "dismissed": False},
            ],
            threads=[],
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" not in names

    def test_no_shadow_when_ci_still_pending(self):
        # ``_ci_is_green`` returns False for any pending run — the
        # reset rule must wait for CI to settle before considering
        # the unit healed.
        st, rep = _report(
            status="escalated",
            pr=_pr(),
            check_runs=[_run("lint", "success"), _run("test", None, status="in_progress")],
            reviews=[{"state": "APPROVED", "user": {"login": "a"}, "dismissed": False}],
            threads=[],
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" not in names

    def test_no_shadow_when_status_not_escalated(self):
        st, rep = _report(
            status="in_ci",
            pr=_pr(),
            check_runs=[_run("test", "success")],
            reviews=[{"state": "APPROVED", "user": {"login": "a"}, "dismissed": False}],
            threads=[],
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" not in names


class TestShadowDecisionMergeReverted:
    def test_fires_when_done_but_merge_commit_not_on_main(self):
        st, rep = _report(
            status="done",
            pr=_pr(
                state_="closed",
                merged=True,
                merge_commit_sha="abc123",
                merged_at="2026-05-20T10:00:00Z",
            ),
            merge_commit_on_main=False,
        )
        decision = health.decide_transitions(st, rep)
        shadows = [s for s in decision.shadow_decisions if s.rule_name == "merge_reverted_flag"]
        assert len(shadows) == 1
        assert shadows[0].trigger_inputs.get("merge_commit_sha") == "abc123"
        # Shadow only — no actions_to_apply transition.
        applied_statuses = [
            a.target_status for a in decision.actions_to_apply if a.kind == "transition"
        ]
        assert applied_statuses == []

    def test_no_shadow_when_merge_commit_still_reachable(self):
        st, rep = _report(
            status="done",
            pr=_pr(state_="closed", merged=True, merge_commit_sha="abc123"),
            merge_commit_on_main=True,
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "merge_reverted_flag" not in names

    def test_no_shadow_when_status_not_done(self):
        # The revert-detection rule is anchored to ``done``: a unit not yet
        # advanced past the merge poll has no expectation that its merge
        # commit is on main.
        st, rep = _report(
            status="in_ci",
            pr=_pr(state_="closed", merged=True, merge_commit_sha="abc123"),
            merge_commit_on_main=False,
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "merge_reverted_flag" not in names

    def test_no_shadow_when_no_merge_commit_sha(self):
        # Pre-merge state — nothing to verify on main.
        st, rep = _report(
            status="done",
            pr=_pr(state_="closed", merged=True, merge_commit_sha=None),
            merge_commit_on_main=False,
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "merge_reverted_flag" not in names


class TestShadowDecisionDeadWorker:
    """Shadow rule for risky cell identified during implementation:
    a worker whose Anthropic session is ``terminated`` while the local
    status still reflects "agent is working" (coding/testing/reviewing/
    fixing). The orchestrator's restart-recovery flow already triages
    this manually via ``resume_unit`` + ``tail_worker``; the shadow rule
    documents the predicate without auto-flipping anyone to escalated.
    """

    def test_fires_when_coding_status_with_terminated_coder(self):
        st = _state(status="coding", coder_session_id="c1")
        rep = health.probe_unit_health(
            "U-1",
            FakeGH(pr=_pr()),
            FakeAnthropic(statuses={"c1": "terminated"}),
            local_state=st,
            now=_now(),
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "dead_worker_during_active_status" in names

    def test_no_shadow_when_worker_idle(self):
        st = _state(status="coding", coder_session_id="c1")
        rep = health.probe_unit_health(
            "U-1",
            FakeGH(pr=_pr()),
            FakeAnthropic(statuses={"c1": "idle"}),
            local_state=st,
            now=_now(),
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "dead_worker_during_active_status" not in names

    def test_no_shadow_when_status_terminal(self):
        st = _state(status="done", coder_session_id="c1")
        rep = health.probe_unit_health(
            "U-1",
            FakeGH(pr=_pr()),
            FakeAnthropic(statuses={"c1": "terminated"}),
            local_state=st,
            now=_now(),
        )
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "dead_worker_during_active_status" not in names


# ============================================================================
# Cross-cell table — (status x pr_state x ci x reviews x conflicts)
# ============================================================================


@pytest.mark.parametrize(
    ("status", "pr_kind", "ci_kind", "reviews_kind", "conflict_kind", "expected_event_types"),
    [
        # Happy merged path — no shadow / no extra events beyond the
        # canonical merged transition.
        (
            "in_ci",
            "merged",
            "green",
            "approved",
            "clean",
            {"merged"},
        ),
        # Open PR with clean CI + clean conflict + no reviews — nothing
        # to do, no events at all.
        (
            "in_ci",
            "open",
            "green",
            "none",
            "clean",
            set(),
        ),
        # Open PR with conflict → conflict event only.
        (
            "in_ci",
            "open",
            "green",
            "none",
            "dirty",
            {"pr_conflict_detected"},
        ),
        # Open PR with CI red + reviewed-and-approved + status drifted
        # past in_ci → ci_drift event.
        (
            "approved_awaiting_merge",
            "open",
            "red",
            "approved",
            "clean",
            {"ci_drift_detected"},
        ),
        # Open PR with required-check missing → required_check_missing event.
        (
            "in_ci",
            "open",
            "missing_required",
            "none",
            "clean",
            {"required_check_missing"},
        ),
        # Merged + escalated → merged + recovered_from_escalated.
        (
            "escalated",
            "merged",
            "green",
            "none",
            "clean",
            {"merged", "recovered_from_escalated"},
        ),
        # Merged but status is active (coding) → refusal event.
        (
            "coding",
            "merged",
            "green",
            "none",
            "clean",
            {"reconcile_refused"},
        ),
    ],
)
def test_decide_transitions_table(
    status, pr_kind, ci_kind, reviews_kind, conflict_kind, expected_event_types
):
    """Cell coverage for the (status x pr_state x ci x reviews x conflicts)
    matrix.

    Each row asserts the *set of event types* emitted in ``actions_to_apply``.
    Transition-vs-event-vs-side_effect split is covered by the focused
    tests above; this table is the matrix-level smoke check.
    """
    # PR fixture
    if pr_kind == "merged":
        pr = _pr(
            state_="closed", merged=True, merge_commit_sha=None, merged_at="2026-05-20T10:00:00Z"
        )
    elif pr_kind == "open":
        pr = _pr(state_="open", merged=False)
    else:
        pr = _pr(state_="closed", merged=False)

    # Conflict fixture
    if conflict_kind == "dirty":
        pr = {**pr, "mergeable": False, "mergeable_state": "dirty", "conflict_files": ["x.py"]}

    # CI fixture
    if ci_kind == "green":
        runs = [_run("lint", "success")]
        required: list[str] = []
    elif ci_kind == "red":
        runs = [_run("lint", "success"), _run("test", "failure")]
        required = []
    elif ci_kind == "missing_required":
        runs = [_run("lint", "success")]
        required = ["lint", "test"]
    else:
        runs, required = [], []

    # Reviews fixture
    if reviews_kind == "approved":
        reviews = [{"state": "APPROVED", "user": {"login": "alice"}, "dismissed": False}]
    else:
        reviews = []

    st, rep = _report(
        status=status,
        pr=pr,
        check_runs=runs,
        required=required,
        reviews=reviews,
    )
    decision = health.decide_transitions(st, rep)
    actual = {a.event_type for a in decision.actions_to_apply if a.kind == "event"}
    assert actual == expected_event_types, (
        f"row [{status}, {pr_kind}, {ci_kind}, {reviews_kind}, {conflict_kind}]"
    )


# ============================================================================
# Decision dataclass invariants
# ============================================================================


class TestDecisionShape:
    def test_decision_has_two_fields(self):
        decision = health.Decision(actions_to_apply=[], shadow_decisions=[])
        assert decision.actions_to_apply == []
        assert decision.shadow_decisions == []

    def test_shadow_decision_carries_required_fields(self):
        action = health.Action(kind="transition", target_status="in_ci", summary="reset")
        shadow = health.ShadowDecision(
            rule_name="escalated_to_in_ci_reset",
            predicted_action=action,
            trigger_inputs={"foo": 1},
            rationale="example",
        )
        assert shadow.rule_name == "escalated_to_in_ci_reset"
        assert shadow.predicted_action is action
        assert shadow.trigger_inputs == {"foo": 1}
        assert shadow.rationale == "example"

    def test_action_payload_is_read_only(self):
        # ``frozen=True`` blocks field reassignment but the underlying
        # dict was previously mutable — defeating the snapshot contract
        # the module docstring promises. ``_FrozenDict`` enforces it.
        action = health.Action(kind="event", event_type="x", payload={"a": 1})
        with pytest.raises(TypeError):
            action.payload["b"] = 2  # type: ignore[index]
        with pytest.raises(TypeError):
            del action.payload["a"]  # type: ignore[attr-defined]
        # ``isinstance(payload, dict)`` is preserved — the tester-set
        # contract continues to hold.
        assert isinstance(action.payload, dict)

    def test_shadow_decision_trigger_inputs_is_read_only(self):
        shadow = health.ShadowDecision(
            rule_name="r",
            predicted_action=health.Action(kind="event"),
            trigger_inputs={"k": "v"},
            rationale="why",
        )
        with pytest.raises(TypeError):
            shadow.trigger_inputs["k"] = "other"  # type: ignore[index]
        assert isinstance(shadow.trigger_inputs, dict)

    def test_empty_report_yields_empty_decision(self):
        # Defensive: a unit with no PR and no signals should produce no
        # actions and no shadow decisions — the function is a decision
        # *table*, not a state mutator.
        st = _state(status="pending", pr_number=None)
        report = health.probe_unit_health(
            "U-1", FakeGH(pr=None), FakeAnthropic(), local_state=st, now=_now()
        )
        decision = health.decide_transitions(st, report)
        assert decision.actions_to_apply == []
        assert decision.shadow_decisions == []
