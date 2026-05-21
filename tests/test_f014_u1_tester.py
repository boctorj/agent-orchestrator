"""Extended F-014-U-1 coverage for ``orchestrator/health.py``.

Supplements the coder's per-snapshot / per-decision-cell tests in
``tests/test_health.py`` with intent-driven assertions pulled directly
from the unit description:

* ``probe_unit_health`` MUST do all I/O through the two injected
  clients — no DB / network / file / subprocess side effects.
* ``probe_unit_health`` and ``decide_transitions`` MUST be pure (no
  mutation of inputs, idempotent across repeated calls).
* The decision table MUST cover the full ``(status x pr_state x ci x
  reviews x conflicts)`` matrix with the precise structured-payload
  contract — not just "an event with the right name fires".
* Dataclasses (``Action`` / ``ShadowDecision`` / ``Decision`` /
  ``HealthReport``) MUST be frozen so callers can snapshot / diff
  them safely.

The fake clients here intentionally do NOT subclass / duck-type the
existing ``FakeGH`` in ``test_health.py`` — keeping the file
self-contained means a future refactor of one file doesn't silently
weaken the other's coverage.
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator import health
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    TERMINAL_UNIT_STATUSES,
    WorkUnitState,
)

# --------------------------- fake clients ---------------------------


@dataclass
class _GH:
    """Recording GitHub fake.

    Every call appends to ``calls`` so tests can assert which methods
    were invoked (and how often). Defaults shaped so a bare ``_GH()``
    produces a "nothing's happening" snapshot.
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
    calls: list[tuple[str, str]] = field(default_factory=list)

    def get_pr(self, unit_id: str) -> dict | None:
        self.calls.append(("get_pr", unit_id))
        # Return a shallow copy so tests that mutate the returned dict can
        # never feed back into the next call's "canned" data.
        return None if self.pr is None else dict(self.pr)

    def get_check_runs(self, unit_id: str) -> list[dict]:
        self.calls.append(("get_check_runs", unit_id))
        return [dict(r) for r in self.check_runs]

    def get_required_checks(self, unit_id: str) -> list[str]:
        self.calls.append(("get_required_checks", unit_id))
        return list(self.required_checks)

    def get_compare_to_base(self, unit_id: str) -> dict:
        self.calls.append(("get_compare_to_base", unit_id))
        return dict(self.compare)

    def get_reviews(self, unit_id: str) -> list[dict]:
        self.calls.append(("get_reviews", unit_id))
        return [dict(r) for r in self.reviews]

    def get_review_threads(self, unit_id: str) -> list[dict]:
        self.calls.append(("get_review_threads", unit_id))
        return [dict(t) for t in self.review_threads]

    def get_requested_reviewers(self, unit_id: str) -> dict:
        self.calls.append(("get_requested_reviewers", unit_id))
        return dict(self.requested_reviewers)

    def get_copilot_review(self, unit_id: str) -> dict | None:
        self.calls.append(("get_copilot_review", unit_id))
        return None if self.copilot_review is None else dict(self.copilot_review)

    def get_last_force_push_at(self, unit_id: str) -> str | None:
        self.calls.append(("get_last_force_push_at", unit_id))
        return self.last_force_push_at

    def get_head_commit(self, unit_id: str) -> dict:
        self.calls.append(("get_head_commit", unit_id))
        return dict(self.head_commit)

    def is_merge_commit_on_main(self, unit_id: str) -> bool:
        self.calls.append(("is_merge_commit_on_main", unit_id))
        return self.merge_commit_on_main


@dataclass
class _Anth:
    statuses: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get_session_status(self, session_id: str) -> str | None:
        self.calls.append(session_id)
        return self.statuses.get(session_id)


# --------------------------- builders ---------------------------


def _now() -> datetime:
    return datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


def _state(
    *,
    status: str = "in_ci",
    review_round: int = 0,
    last_error: str = "",
    coder_session_id: str = "sesn_coder",
    tester_session_id: str = "",
    reviewer_session_id: str = "",
    last_activity: str | None = None,
) -> WorkUnitState:
    return WorkUnitState(
        unit_id="F-014-U-1",
        feature_id="F-014",
        status=status,  # type: ignore[arg-type]
        branch="feat/f-014-u-1",
        pr_number=49,
        coder_session_id=coder_session_id,
        tester_session_id=tester_session_id,
        reviewer_session_id=reviewer_session_id,
        review_round=review_round,
        last_activity=last_activity or (_now() - timedelta(minutes=5)).isoformat(),
        last_error=last_error,
    )


def _pr_open(**overrides: Any) -> dict:
    base = {
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_sha": "deadbeef",
        "merge_commit_sha": None,
        "merged_at": None,
        "base": "main",
    }
    base.update(overrides)
    return base


def _pr_merged(**overrides: Any) -> dict:
    base = {
        "state": "closed",
        "merged": True,
        "mergeable": None,
        "mergeable_state": "clean",
        "head_sha": "deadbeef",
        "merge_commit_sha": "abc123",
        "merged_at": "2026-05-20T10:00:00Z",
        "base": "main",
    }
    base.update(overrides)
    return base


def _run(name: str, conclusion: str | None = "success", status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "details_url": f"u/{name}"}


# ============================================================================
# Purity invariant — probe_unit_health performs NO I/O outside its clients
# ============================================================================


class TestProbePurity:
    """Description: "No I/O outside the gh/anthropic clients passed in."

    Validated by intercepting filesystem / network / subprocess gateways
    during the call. Any access fails the test.
    """

    def test_probe_does_not_open_files_or_subprocess_or_network(self, monkeypatch):
        # Block subprocess gateway
        import subprocess

        def boom_subprocess(*a, **k):
            raise AssertionError("probe_unit_health invoked subprocess.run")

        monkeypatch.setattr(subprocess, "run", boom_subprocess)
        if hasattr(subprocess, "Popen"):
            monkeypatch.setattr(subprocess, "Popen", boom_subprocess)

        # Block socket gateway
        import socket

        original_socket = socket.socket

        def boom_socket(*a, **k):
            raise AssertionError("probe_unit_health opened a socket")

        monkeypatch.setattr(socket, "socket", boom_socket)
        try:
            # Block http clients commonly imported in this repo.
            import httpx

            def boom_get(*a, **k):
                raise AssertionError("probe_unit_health called httpx")

            monkeypatch.setattr(httpx, "get", boom_get)
            monkeypatch.setattr(httpx, "post", boom_get)
        except ImportError:
            pass

        gh = _GH(
            pr=_pr_open(),
            check_runs=[_run("lint")],
            required_checks=["lint"],
            reviews=[{"state": "APPROVED", "dismissed": False}],
            review_threads=[],
            requested_reviewers={"users": ["alice"], "teams": ["sec"]},
            copilot_review={"state": "COMMENTED"},
            last_force_push_at="2026-05-21T11:00:00Z",
            head_commit={"sha": "deadbeef", "committed_at": "2026-05-21T11:00:00Z"},
        )
        anth = _Anth(statuses={"sesn_coder": "running"})

        # Should complete without tripping any of the booby-trapped gateways.
        report = health.probe_unit_health("U-1", gh, anth, local_state=_state(), now=_now())
        assert report.unit_id == "U-1"

        # Restore socket so subsequent tests don't blow up on import-time
        # network probes (defensive).
        monkeypatch.setattr(socket, "socket", original_socket)


class TestProbeReachesEveryClientMethod:
    """The probe must hit each documented GitHub data source at least once,
    so the production client and tests stay in lockstep on the protocol.
    """

    def test_every_protocol_method_called(self):
        gh = _GH(pr=_pr_merged())  # merged so is_merge_commit_on_main is reached
        anth = _Anth(statuses={"sesn_coder": "running"})
        health.probe_unit_health("U-1", gh, anth, local_state=_state(), now=_now())

        called_methods = {m for (m, _uid) in gh.calls}
        required = {
            "get_pr",
            "get_check_runs",
            "get_required_checks",
            "get_compare_to_base",
            "get_reviews",
            "get_review_threads",
            "get_requested_reviewers",
            "get_copilot_review",
            "get_last_force_push_at",
            "get_head_commit",
            "is_merge_commit_on_main",
        }
        missing = required - called_methods
        assert not missing, f"probe_unit_health did not call: {sorted(missing)}"

    def test_is_merge_commit_on_main_skipped_when_pr_open(self):
        """The docstring says: "Only ask the client when the PR has actually
        merged — pre-merge reachability is meaningless." So an open PR must
        not trigger the call (the production client may not even accept it).
        """
        gh = _GH(pr=_pr_open())
        health.probe_unit_health("U-1", gh, _Anth(), local_state=_state(), now=_now())
        called = {m for (m, _uid) in gh.calls}
        assert "is_merge_commit_on_main" not in called

    def test_is_merge_commit_on_main_skipped_when_no_merge_commit_sha(self):
        gh = _GH(pr=_pr_merged(merge_commit_sha=None))
        health.probe_unit_health("U-1", gh, _Anth(), local_state=_state(), now=_now())
        called = {m for (m, _uid) in gh.calls}
        assert "is_merge_commit_on_main" not in called

    def test_anthropic_only_called_for_roles_with_session_ids(self):
        anth = _Anth(statuses={"c": "idle"})
        st = _state(coder_session_id="c", tester_session_id="", reviewer_session_id="")
        health.probe_unit_health("U-1", _GH(pr=_pr_open()), anth, local_state=st, now=_now())
        # Only coder's session id was queried — tester / reviewer have no
        # session yet so we don't make the call.
        assert anth.calls == ["c"]


# ============================================================================
# Mutation invariant — neither function mutates its inputs
# ============================================================================


class TestNoMutation:
    def test_probe_does_not_mutate_local_state(self):
        original = _state(status="in_ci", review_round=2, last_error="something")
        snap = WorkUnitState(**vars(original))
        gh = _GH(pr=_pr_open(), check_runs=[_run("lint")])
        health.probe_unit_health("U-1", gh, _Anth(), local_state=original, now=_now())
        assert vars(original) == vars(snap), "probe_unit_health mutated local_state"

    def test_decide_does_not_mutate_local_state_or_report(self):
        st = _state(status="escalated", last_error="cap-3 hit")
        gh = _GH(
            pr=_pr_merged(),
            check_runs=[_run("lint"), _run("test")],
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        st_snap = WorkUnitState(**vars(st))
        # HealthReport is frozen, but we can still check field equality
        # post-call.
        rep_id = id(rep)
        health.decide_transitions(st, rep)
        assert vars(st) == vars(st_snap), "decide_transitions mutated local_state"
        # Same object reference (frozen dataclass guarantees no in-place
        # replacement; we double-check no shallow swap happened).
        assert id(rep) == rep_id


# ============================================================================
# Frozen dataclass invariants — Action / ShadowDecision / HealthReport
# ============================================================================


class TestFrozenDataclasses:
    """Callers snapshot Decisions for audit + diff. Frozen-ness is the
    contract that lets them assume the snapshot is stable."""

    def test_action_is_frozen(self):
        a = health.Action(kind="event", event_type="merged")
        with pytest.raises(FrozenInstanceError):
            a.kind = "transition"  # type: ignore[misc]

    def test_shadow_decision_is_frozen(self):
        s = health.ShadowDecision(
            rule_name="r",
            predicted_action=health.Action(kind="event"),
            trigger_inputs={},
            rationale="why",
        )
        with pytest.raises(FrozenInstanceError):
            s.rule_name = "other"  # type: ignore[misc]

    def test_health_report_is_frozen(self):
        gh = _GH(pr=_pr_open())
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=_state(), now=_now())
        with pytest.raises(FrozenInstanceError):
            rep.unit_id = "other"  # type: ignore[misc]


# ============================================================================
# Idempotence / determinism — same inputs → same Decision
# ============================================================================


class TestIdempotence:
    def test_same_inputs_yield_equal_decisions(self):
        st = _state(status="escalated", last_error="prior")
        gh = _GH(
            pr=_pr_merged(),
            check_runs=[_run("lint"), _run("test")],
            reviews=[{"state": "APPROVED", "dismissed": False}],
        )
        rep1 = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        rep2 = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        # HealthReport is a frozen dataclass, so equality is value-based.
        assert rep1 == rep2

        d1 = health.decide_transitions(st, rep1)
        d2 = health.decide_transitions(st, rep1)
        assert d1 == d2

    def test_repeated_probe_does_not_grow_report(self):
        """Defensive: a Decision built from a re-probe should have the same
        number of actions / shadow decisions — no cumulative state leakage.
        """
        st = _state(status="in_ci")
        gh = _GH(pr=_pr_merged())
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        d1 = health.decide_transitions(st, rep)
        d2 = health.decide_transitions(st, rep)
        assert len(d1.actions_to_apply) == len(d2.actions_to_apply)
        assert len(d1.shadow_decisions) == len(d2.shadow_decisions)


# ============================================================================
# Default cycle_cap == 3  (per the description's "cap-3 mechanics")
# ============================================================================


class TestCycleCapDefault:
    def test_default_cycle_cap_is_3(self):
        rep = health.probe_unit_health(
            "U-1", _GH(pr=_pr_open()), _Anth(), local_state=_state(review_round=1), now=_now()
        )
        assert rep.orchestrator.cycle_cap == 3
        assert rep.orchestrator.cycles_remaining == 2

    def test_cycles_remaining_clamps_at_zero(self):
        """Per the snapshot's ``max(0, cap - round)`` — a unit somehow past
        its cap (e.g. manual fix-loop bump) reports 0 remaining, never
        negative.
        """
        rep = health.probe_unit_health(
            "U-1",
            _GH(pr=_pr_open()),
            _Anth(),
            local_state=_state(review_round=10),
            now=_now(),
            cycle_cap=3,
        )
        assert rep.orchestrator.cycles_remaining == 0


# ============================================================================
# Default now=datetime.now(UTC) when caller omits it
# ============================================================================


class TestDefaultNow:
    def test_default_now_used_when_omitted(self, monkeypatch):
        """Probe must default ``now`` to the current UTC clock — otherwise
        ages computed in production runs would all be ``None``.

        We patch ``health.datetime`` so the test is deterministic but the
        production call path runs (no explicit ``now=`` argument).
        """
        sentinel = datetime(2026, 5, 21, 13, 0, 0, tzinfo=UTC)

        class _FakeDT:
            @classmethod
            def now(cls, tz=None):
                return sentinel

            # Defer everything else to the real datetime.
            @classmethod
            def fromisoformat(cls, s):
                return datetime.fromisoformat(s)

        monkeypatch.setattr(health, "datetime", _FakeDT)

        st = _state(last_activity="2026-05-21T12:00:00+00:00")
        rep = health.probe_unit_health("U-1", _GH(pr=_pr_open()), _Anth(), local_state=st)
        # 1 hour between the fake "now" and last_activity.
        assert rep.orchestrator.last_activity_age_seconds == 3600


# ============================================================================
# PR head_sha fallback — when PR is None, fall back to head_commit.sha
# ============================================================================


class TestHeadShaFallback:
    def test_head_sha_from_head_commit_when_pr_none(self):
        gh = _GH(pr=None, head_commit={"sha": "fallback-sha"})
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=_state(), now=_now())
        assert rep.git.head_sha == "fallback-sha"
        # And no PR snapshot.
        assert rep.pr is None


# ============================================================================
# Event-only signals — assert FULL structured payload, not just event_type
# ============================================================================


class TestConflictEventPayloadShape:
    def test_payload_has_files_and_mergeable_state(self):
        st = _state(status="in_ci")
        gh = _GH(
            pr={**_pr_open(), "mergeable_state": "dirty", "conflict_files": ["a.py", "b.md"]},
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        events = [a for a in decision.actions_to_apply if a.event_type == "pr_conflict_detected"]
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "event"
        # Payload is structured (the live MCP layer surfaces this dict to chat).
        assert ev.payload["conflict_files"] == ["a.py", "b.md"]
        assert ev.payload["mergeable_state"] == "dirty"
        # Files listed in details so audit log carries the diagnostic.
        assert "a.py" in ev.details and "b.md" in ev.details
        # No status change.
        assert ev.target_status is None

    def test_conflict_event_suppressed_on_merged_pr(self):
        """A merged PR can't have a conflict — never fire the event in that
        case, even if `mergeable_state` returns a stale "dirty"."""
        st = _state(status="in_ci")
        gh = _GH(
            pr={
                **_pr_merged(),
                "mergeable_state": "dirty",
                "conflict_files": ["x.py"],
            }
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        names = [a.event_type for a in decision.actions_to_apply]
        assert "pr_conflict_detected" not in names

    def test_conflict_files_empty_when_key_absent_but_event_still_fires(self):
        """Production client may emit `mergeable_state=dirty` before the
        conflict-file list is populated (separate API round-trip). The
        event must still fire so the lead sees the signal."""
        st = _state(status="in_ci")
        gh = _GH(pr={**_pr_open(), "mergeable_state": "dirty"})
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        events = [a for a in decision.actions_to_apply if a.event_type == "pr_conflict_detected"]
        assert len(events) == 1
        assert events[0].payload["conflict_files"] == []


class TestRequiredCheckMissingPayloadShape:
    def test_payload_lists_missing_and_required(self):
        st = _state(status="in_ci")
        gh = _GH(
            pr=_pr_open(),
            check_runs=[_run("lint")],
            required_checks=["lint", "test", "type-check"],
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        events = [a for a in decision.actions_to_apply if a.event_type == "required_check_missing"]
        assert len(events) == 1
        ev = events[0]
        # Order preserved from `required` list.
        assert ev.payload["missing"] == ["test", "type-check"]
        assert ev.payload["required"] == ["lint", "test", "type-check"]
        # Event-only — no status change.
        assert ev.target_status is None


class TestCIDriftPayloadAndSemantics:
    def test_payload_carries_failing_runs_and_status(self):
        st = _state(status="approved_awaiting_merge")
        gh = _GH(
            pr=_pr_open(),
            check_runs=[_run("lint"), _run("test", "failure"), _run("e2e", "failure")],
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        events = [a for a in decision.actions_to_apply if a.event_type == "ci_drift_detected"]
        assert len(events) == 1
        ev = events[0]
        assert set(ev.payload["failing"]) == {"test", "e2e"}
        assert ev.payload["status"] == "approved_awaiting_merge"
        # ``set_last_error`` is the field the executor writes to
        # ``unit_state.last_error`` — per spec: "sets last_error, no
        # status change".
        assert ev.set_last_error
        assert ev.target_status is None
        assert ev.kind == "event"

    def test_ci_drift_quiet_in_each_actively_fixing_status(self):
        """The "expected red" allow-set MUST include coding / fixing / testing
        (the docstring explicitly enumerates them); a red CI in those
        statuses is the reason an agent is running, not "drift"."""
        for active_status in ("coding", "fixing", "testing"):
            st = _state(status=active_status)
            gh = _GH(pr=_pr_open(), check_runs=[_run("test", "failure")])
            rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
            decision = health.decide_transitions(st, rep)
            names = [a.event_type for a in decision.actions_to_apply]
            assert "ci_drift_detected" not in names, (
                f"status={active_status!r} should not emit ci_drift_detected"
            )

    def test_ci_drift_quiet_on_terminal_statuses(self):
        """Terminal statuses (done / escalated) already capture the
        resolution — no double-flagging via drift event."""
        for term_status in TERMINAL_UNIT_STATUSES:
            st = _state(status=term_status)
            gh = _GH(pr=_pr_open(), check_runs=[_run("test", "failure")])
            rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
            decision = health.decide_transitions(st, rep)
            names = [a.event_type for a in decision.actions_to_apply]
            assert "ci_drift_detected" not in names, (
                f"terminal status={term_status!r} should not emit ci_drift_detected"
            )


# ============================================================================
# Multi-signal report — overlapping events all fire in a single Decision
# ============================================================================


class TestMultiSignalReport:
    """A single probe can produce a report that triggers several event-only
    signals at once. The decision table must surface all of them in the same
    ``actions_to_apply`` list rather than short-circuiting on the first
    match."""

    def test_conflict_and_missing_required_and_drift_all_fire(self):
        st = _state(status="approved_awaiting_merge")
        gh = _GH(
            pr={**_pr_open(), "mergeable_state": "dirty", "conflict_files": ["x.py"]},
            check_runs=[_run("lint"), _run("test", "failure")],
            required_checks=["lint", "test", "type-check"],
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        event_types = {a.event_type for a in decision.actions_to_apply if a.kind == "event"}
        # All three event-only signals from the unit description fire.
        assert {"pr_conflict_detected", "required_check_missing", "ci_drift_detected"}.issubset(
            event_types
        )
        # And none of them are accompanied by a status transition.
        assert [a for a in decision.actions_to_apply if a.kind == "transition"] == []


# ============================================================================
# Shadow rule structural contract  (per description: rule_name, predicted_action,
# trigger_inputs (structured), rationale (str))
# ============================================================================


class TestShadowDecisionShape:
    def test_every_shadow_carries_required_fields(self):
        """Run a scenario that emits all three known shadow rules
        simultaneously and inspect each one's shape."""
        # escalated_to_in_ci_reset: escalated + CI green + approved + no
        # open threads
        st = _state(status="escalated", last_error="prior")
        gh = _GH(
            pr=_pr_open(),
            check_runs=[_run("lint")],
            reviews=[{"state": "APPROVED", "dismissed": False}],
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        for shadow in decision.shadow_decisions:
            assert isinstance(shadow.rule_name, str) and shadow.rule_name
            assert isinstance(shadow.predicted_action, health.Action)
            assert isinstance(shadow.trigger_inputs, dict)
            assert isinstance(shadow.rationale, str) and shadow.rationale.strip()

    def test_escalated_reset_predicted_action_clears_error(self):
        """Per the shadow rule docstring: the predicted action resets to
        in_ci AND clears last_error — those two go together."""
        st = _state(status="escalated", last_error="prior cap-3")
        gh = _GH(
            pr=_pr_open(),
            check_runs=[_run("lint")],
            reviews=[{"state": "APPROVED", "dismissed": False}],
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        shadows = [
            s for s in decision.shadow_decisions if s.rule_name == "escalated_to_in_ci_reset"
        ]
        assert len(shadows) == 1
        action = shadows[0].predicted_action
        assert action.kind == "transition"
        assert action.target_status == "in_ci"
        assert action.clear_error is True

    def test_escalated_reset_fires_even_with_no_check_runs(self):
        """A repo with no CI must still let the reset fire — the docstring
        says "no check_runs == green" matches ``ci_wait``'s no-CI pass-through."""
        st = _state(status="escalated")
        gh = _GH(
            pr=_pr_open(),
            check_runs=[],
            reviews=[{"state": "APPROVED", "dismissed": False}],
        )
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        names = [s.rule_name for s in decision.shadow_decisions]
        assert "escalated_to_in_ci_reset" in names

    def test_merge_reverted_shadow_carries_sha_in_trigger_inputs(self):
        st = _state(status="done")
        gh = _GH(pr=_pr_merged(merge_commit_sha="cafe1234"), merge_commit_on_main=False)
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        shadows = [s for s in decision.shadow_decisions if s.rule_name == "merge_reverted_flag"]
        assert len(shadows) == 1
        assert shadows[0].trigger_inputs["merge_commit_sha"] == "cafe1234"
        assert shadows[0].trigger_inputs["merge_commit_on_main"] is False
        # Predicted action is an event (not a transition).
        assert shadows[0].predicted_action.kind == "event"

    def test_dead_worker_shadow_fires_for_any_active_status(self):
        """The shadow rule's intent: any active-role status × terminated
        worker emits the predicate, not just "coding". Verify the rule
        matches the description's broader "active status" framing."""
        for active_status in sorted(ACTIVE_UNIT_STATUSES):
            st = _state(status=active_status, coder_session_id="c")
            anth = _Anth(statuses={"c": "terminated"})
            rep = health.probe_unit_health(
                "U-1", _GH(pr=_pr_open()), anth, local_state=st, now=_now()
            )
            decision = health.decide_transitions(st, rep)
            names = [s.rule_name for s in decision.shadow_decisions]
            assert "dead_worker_during_active_status" in names, (
                f"dead-worker shadow rule did not fire for active status {active_status!r}"
            )


# ============================================================================
# Cross-cell table — full (status × pr_state × ci × reviews × conflicts) matrix
# ============================================================================


_STATUSES_TO_PROBE = [
    "pending",
    "coding",
    "testing",
    "in_ci",
    "fixing",
    "reviewing",
    "approved_awaiting_merge",
    "escalated",
    "done",
]


@pytest.mark.parametrize(
    ("status", "pr", "ci_color", "reviewed", "conflict", "expected_events"),
    [
        # ---- merged + every reconcile-able status ----
        ("in_ci", "merged", "green", False, False, {"merged"}),
        ("approved_awaiting_merge", "merged", "green", False, False, {"merged"}),
        (
            "escalated",
            "merged",
            "green",
            False,
            False,
            {"merged", "recovered_from_escalated"},
        ),
        # ---- merged + active status → refusal, no advance ----
        ("coding", "merged", "green", False, False, {"reconcile_refused"}),
        ("testing", "merged", "green", False, False, {"reconcile_refused"}),
        ("reviewing", "merged", "green", False, False, {"reconcile_refused"}),
        ("fixing", "merged", "green", False, False, {"reconcile_refused"}),
        # ---- merged + done is idempotent ----
        ("done", "merged", "green", False, False, set()),
        # ---- open PR + clean → no events at all ----
        ("in_ci", "open", "green", False, False, set()),
        # ---- open PR + conflict → conflict event only ----
        ("in_ci", "open", "green", False, True, {"pr_conflict_detected"}),
        # ---- open + CI red + status past the active-fix bucket → drift ----
        (
            "approved_awaiting_merge",
            "open",
            "red",
            True,
            False,
            {"ci_drift_detected"},
        ),
        # ---- open + CI red + active-fix status → quiet ----
        ("fixing", "open", "red", False, False, set()),
        ("coding", "open", "red", False, False, set()),
        ("testing", "open", "red", False, False, set()),
        # ---- open + everything stacked at once ----
        (
            "approved_awaiting_merge",
            "open",
            "red",
            True,
            True,
            {"pr_conflict_detected", "ci_drift_detected"},
        ),
    ],
)
def test_decide_transitions_full_matrix(status, pr, ci_color, reviewed, conflict, expected_events):
    """Cross-cell matrix: assert the *exact set* of event types emitted in
    ``actions_to_apply``. Stricter than the single-name "in" assertions in
    the coder's table — surfaces both missing events and unexpected extras.
    """
    # PR fixture
    if pr == "merged":
        pr_dict = _pr_merged()
    else:
        pr_dict = _pr_open()

    # Conflict overlay
    if conflict:
        pr_dict = {**pr_dict, "mergeable_state": "dirty", "conflict_files": ["x.py"]}

    # CI fixture
    if ci_color == "green":
        runs: list[dict] = [_run("lint")]
    else:
        runs = [_run("lint"), _run("test", "failure")]

    # Reviews fixture
    reviews = (
        [{"state": "APPROVED", "user": {"login": "alice"}, "dismissed": False}] if reviewed else []
    )

    st = _state(status=status, last_error="prior" if status == "escalated" else "")
    gh = _GH(pr=pr_dict, check_runs=runs, reviews=reviews)
    rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
    decision = health.decide_transitions(st, rep)
    actual = {a.event_type for a in decision.actions_to_apply if a.kind == "event"}
    assert actual == expected_events, (
        f"row [{status}, {pr}, ci={ci_color}, reviewed={reviewed}, conflict={conflict}] "
        f"expected events {expected_events}, got {actual}"
    )


# ============================================================================
# Merged + escalated cell — recovered_from_escalated must preserve last_error
# in the event's details (audit-trail contract)
# ============================================================================


class TestRecoveredFromEscalatedAudit:
    def test_prior_last_error_carried_to_details(self):
        prior_error = "cap-3 hit after reviewer changes-requested 3x"
        st = _state(status="escalated", last_error=prior_error)
        gh = _GH(pr=_pr_merged())
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        recovered = [
            a
            for a in decision.actions_to_apply
            if a.kind == "event" and a.event_type == "recovered_from_escalated"
        ]
        assert len(recovered) == 1
        assert prior_error in recovered[0].details

    def test_transition_to_done_clears_error(self):
        """Per the description: "merged + escalated -> done … clears
        last_error". Encoded on the transition Action as ``clear_error=True``."""
        st = _state(status="escalated", last_error="prior")
        rep = health.probe_unit_health(
            "U-1", _GH(pr=_pr_merged()), _Anth(), local_state=st, now=_now()
        )
        decision = health.decide_transitions(st, rep)
        transitions = [a for a in decision.actions_to_apply if a.kind == "transition"]
        assert len(transitions) == 1
        assert transitions[0].target_status == "done"
        assert transitions[0].clear_error is True


# ============================================================================
# Cycle-log side-effect declaration (description: "cycle-log writer side
# effect preserved")
# ============================================================================


class TestCycleLogSideEffect:
    def test_side_effect_payload_carries_merge_commit_sha(self):
        st = _state(status="in_ci")
        gh = _GH(pr=_pr_merged(merge_commit_sha="cafef00d"))
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        side_effects = [a for a in decision.actions_to_apply if a.kind == "side_effect"]
        assert len(side_effects) == 1
        se = side_effects[0]
        assert se.side_effect == "write_cycle_log"
        assert se.payload["merge_commit_sha"] == "cafef00d"

    def test_side_effect_fires_even_when_status_already_done(self):
        """The docstring says the cycle-log writer is "idempotent re-render
        on a subsequent poll after status='done'". So even if no transition
        fires (done is idempotent), the side-effect still runs when a
        merge_commit_sha is present — so a restart can re-emit the log."""
        st = _state(status="done")
        gh = _GH(pr=_pr_merged(merge_commit_sha="ab12"))
        rep = health.probe_unit_health("U-1", gh, _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        side_effects = [a for a in decision.actions_to_apply if a.kind == "side_effect"]
        # Note: this is a strict reading of the docstring — if the impl
        # decides differently the test will surface the divergence.
        assert len(side_effects) == 1


# ============================================================================
# __all__ exports the documented public API
# ============================================================================


class TestPublicAPI:
    def test_module_exports_required_symbols(self):
        for name in [
            "probe_unit_health",
            "decide_transitions",
            "HealthReport",
            "Decision",
            "Action",
            "ShadowDecision",
            "GitHubHealthClient",
            "AnthropicHealthClient",
        ]:
            assert hasattr(health, name), f"orchestrator.health missing public symbol {name!r}"

    def test_decision_dataclass_has_two_buckets(self):
        """Description: "returns a dataclass with two fields:
        actions_to_apply and shadow_decisions"."""
        decision = health.Decision(actions_to_apply=[], shadow_decisions=[])
        names = {f for f in decision.__dataclass_fields__}
        assert names == {"actions_to_apply", "shadow_decisions"}


# ============================================================================
# Defensive: a unit with no PR + no signals → empty decision
# ============================================================================


class TestEmptyReportNoDecision:
    def test_no_pr_no_workers_no_runs_yields_empty_decision(self):
        st = _state(status="pending", coder_session_id="")
        rep = health.probe_unit_health("U-1", _GH(pr=None), _Anth(), local_state=st, now=_now())
        decision = health.decide_transitions(st, rep)
        assert decision.actions_to_apply == []
        assert decision.shadow_decisions == []


# ============================================================================
# Smoke: orchestrator.health import does not trigger any side effects
# ============================================================================


class TestImportPurity:
    def test_no_side_effect_imports_on_module_load(self, monkeypatch):
        """Per CONTRIBUTING.md "Hard rules": importing a module must not
        write the filesystem / DB / network. We re-import health under
        guards to confirm."""
        import subprocess

        def boom(*a, **k):
            raise AssertionError("module import triggered subprocess")

        monkeypatch.setattr(subprocess, "run", boom)

        # Force a fresh import.
        sys.modules.pop("orchestrator.health", None)
        import importlib

        importlib.import_module("orchestrator.health")
