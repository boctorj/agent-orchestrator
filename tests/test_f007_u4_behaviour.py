"""Tester-authored behavior tests for F-007-U-4 — ultrareview fix-loop.

Complements ``tests/test_f007_u4_ultrareview_fix_loop.py`` (coder-authored)
with assertions that protect the parts of the unit description not yet
pinned by tests:

  * The fix-loop budget is **shared** with prior tester-bug / reviewer-
    change cycles (not a fresh CAP_3 just for ultrareview).
  * After each coder fix push the orchestrator **waits for CI green
    before re-running ultrareview** (spec: "after coder fix lands and
    CI is green, re-run ultrareview").
  * Findings travel into ``address_review`` as **structured feedback**
    (verbatim — the variant prompt's "fix without scope creep" guidance
    is only useful if the coder can see what the audit named).
  * Each FAIL cycle posts a **fresh** PR comment scoped to that cycle's
    findings — so a multi-cycle audit history is legible on the PR.
  * The orchestrator-visible side effects (PR comment, ``coder_resumed``
    event with ``source='ultrareview'``, ``ultrareview_fix_cycle_N``
    event) happen in the order a human reading the PR + the event log
    would expect.
  * ``README.md`` and ``CLAUDE.md`` document the new
    ``source='ultrareview'`` route + the fix-loop semantics.

All tests stub the worker / GitHub / ultrareview CLI surfaces — no
network, no real ``claude`` invocations, no real PR comments.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import CAP_3, execution

# --------------------------- shared scaffolding ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Default to "CI green" — individual tests override to count calls."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _stub_github(monkeypatch, *, comment_log=None):
    """No-op github helpers; optionally log PR comments."""

    def fake_comment(repo_url, pr_number, body):
        if comment_log is not None:
            comment_log.append({"repo_url": repo_url, "pr_number": pr_number, "body": body})
        return ""

    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", fake_comment)
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
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
        lambda *a, **k: {"head_sha": "deadbeef", "state": "open", "merged": False},
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda *a, **k: True,
    )


def _seed_endorsing_unit(*, feature_id="F-001", review_round=0):
    """Create feature + plan + unit already past coder spawn.

    ``review_round`` lets a test pre-consume part of the shared CAP_3
    budget (mimicking the "tester bug then reviewer change then
    ultrareview" path); ``_ultrareview_phase`` should see it and short-
    circuit the loop accordingly.
    """
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
            ultrareview_enabled=True,
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description="impl")],
    )
    state.approve_plan(feature_id)
    unit_state = WorkUnitState(
        unit_id=f"{feature_id}-U-1",
        feature_id=feature_id,
        status="in_ci",
        branch="feat/branch",
        pr_number=42,
        coder_session_id="sesn-c",
    )
    state.upsert_unit_state(unit_state)
    # Pre-seed the review_round counter to mimic a partly-consumed cap.
    for _ in range(review_round):
        state.increment_review_round(f"{feature_id}-U-1")


def _endorse_tester_and_reviewer(monkeypatch):
    """tester=TESTS_PASS, reviewer=REVIEW_RECOMMEND_MERGE — focus on the gate."""
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


def _stub_ultrareview(monkeypatch, verdicts):
    """Sequence of ``(passed, findings)`` returned by successive wait_for_result.

    Calls beyond the list repeat the last entry (so a "persistent FAIL"
    test can pass a single tuple and let the loop iterate).
    """
    calls = {"trigger": [], "wait": []}
    it = iter(verdicts)
    last = {"value": verdicts[-1]}

    def fake_trigger(pr_url, **kw):
        calls["trigger"].append(pr_url)

    def fake_wait(pr_url, **kw):
        calls["wait"].append(pr_url)
        try:
            v = next(it)
            last["value"] = v
        except StopIteration:
            v = last["value"]
        passed, findings = v
        return {"passed": passed, "findings": list(findings)}

    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.trigger", fake_trigger)
    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.wait_for_result", fake_wait)
    return calls


def _stub_address_review_fix_pushed(monkeypatch, *, log=None):
    """Replace ``address_review`` with a FIX_PUSHED stub that bumps review_round."""

    def fake(unit_id, source, feedback):
        if log is not None:
            log.append({"unit_id": unit_id, "source": source, "feedback": feedback})
        round_num = state.increment_review_round(unit_id)
        return json.dumps(
            {
                "unit_id": unit_id,
                "cycle": round_num,
                "outcome": "FIX_PUSHED",
                "summary": "fixed",
            },
            indent=2,
        )

    monkeypatch.setattr(execution, "address_review", fake)


# ============================================================================
# Shared CAP_3 budget — the unit description's key non-obvious requirement
# ============================================================================


class TestSharedCap3:
    """The unit description says the ultrareview fix-loop "loop[s] until
    PASS or shared cap-3 hits" — the cap is shared with tester-bug /
    reviewer-change / CI-fail fix cycles, not a fresh CAP_3 per loop.

    A regression that gave ultrareview its own CAP_3 would still pass the
    "persistent FAIL escalates" test but burn 3 extra cycles after a
    reviewer-changes loop already exhausted them, defeating the "user as
    final escalation target" guarantee.
    """

    def test_pre_consumed_budget_reduces_ultrareview_fix_cycles(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        # Reviewer-changes loop already consumed 2/3 cycles; only ONE
        # ultrareview fix cycle should fire before cap-3.
        _seed_endorsing_unit(review_round=CAP_3 - 1)
        _endorse_tester_and_reviewer(monkeypatch)
        _stub_github(monkeypatch)
        address_log: list[dict] = []
        _stub_address_review_fix_pushed(monkeypatch, log=address_log)
        _stub_ultrareview(monkeypatch, [(False, ["never-clears"])])

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        assert parsed["outcome"] == "escalated", (
            f"persistent FAIL with pre-consumed budget must escalate; got {parsed['outcome']!r}"
        )
        # The CAP_3 is SHARED — only the remaining budget (1 here) should be
        # spent on ultrareview fixes. A fresh-CAP_3 regression would call
        # address_review 3 times here.
        assert len(address_log) == 1, (
            f"shared cap-3 must respect prior consumption; "
            f"expected 1 ultrareview fix cycle (CAP_3 - {CAP_3 - 1}), got {len(address_log)}"
        )
        # And the escalation message must surface the cap.
        assert "cap" in parsed["message"].lower()

    def test_fully_consumed_budget_escalates_without_any_ultrareview_fix(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """If prior cycles already burned the full CAP_3, the first
        ultrareview FAIL must escalate immediately — no further fix
        attempts, the budget is gone.
        """
        _seed_endorsing_unit(review_round=CAP_3)
        _endorse_tester_and_reviewer(monkeypatch)
        _stub_github(monkeypatch)
        address_log: list[dict] = []
        _stub_address_review_fix_pushed(monkeypatch, log=address_log)
        ur_calls = _stub_ultrareview(monkeypatch, [(False, ["leak"])])

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        assert parsed["outcome"] == "escalated"
        assert address_log == [], (
            "exhausted shared CAP_3 must not allow ANY ultrareview fix cycles; "
            f"got {len(address_log)}"
        )
        # The first ultrareview verdict still ran — that's the signal that
        # told us we need a fix in the first place. We just couldn't spend
        # any budget on it.
        assert len(ur_calls["wait"]) == 1, (
            "initial ultrareview verdict must still fire even with no budget "
            "to fix the FAIL — that's how we discover whether escalation is warranted"
        )


# ============================================================================
# CI gate between fix push and ultrareview re-run
# ============================================================================


class TestCIWaitBeforeReRunningUltrareview:
    """Spec: "after coder fix lands and CI is green, re-run ultrareview".

    A regression that re-fires ultrareview before CI settles would burn
    cloud-billing on a half-built tree and produce findings that don't
    reflect the coder's actual fix. We verify the ordering by counting
    CI waits between the fix push and the second ultrareview trigger.
    """

    def test_ci_is_waited_between_fix_push_and_re_run(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_endorsing_unit()
        _endorse_tester_and_reviewer(monkeypatch)
        _stub_github(monkeypatch)
        _stub_address_review_fix_pushed(monkeypatch)

        # Record the relative ordering of CI waits vs ultrareview triggers.
        events: list[str] = []

        def fake_ci(*args, **kwargs):
            events.append("ci_wait")
            return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

        monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_ci)

        def fake_trigger(pr_url, **kw):
            events.append("ultrareview_trigger")

        def fake_wait_ur(pr_url, **kw):
            # First call: FAIL → fix-loop. Second: PASS.
            n = events.count("ultrareview_trigger")
            events.append("ultrareview_wait")
            if n == 1:
                return {"passed": False, "findings": ["leak"]}
            return {"passed": True, "findings": []}

        monkeypatch.setattr("orchestrator.tools.execution.ultrareview.trigger", fake_trigger)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ultrareview.wait_for_result", fake_wait_ur
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

        # Trim to just the ultrareview-loop region: everything after the
        # FIRST ultrareview_trigger. We're asserting the inner-loop ordering:
        # initial verdict → fix → CI green → second verdict.
        first_ur_idx = events.index("ultrareview_trigger")
        ur_region = events[first_ur_idx:]
        # At least one CI wait must occur between the first and second
        # ultrareview trigger.
        between = ur_region[: ur_region.index("ultrareview_trigger", 1)]
        assert "ci_wait" in between, (
            f"expected ci_wait between the FAIL and the re-run ultrareview trigger; "
            f"got the event sequence: {between!r}"
        )


# ============================================================================
# Findings become structured feedback
# ============================================================================


class TestFindingsAsStructuredFeedback:
    """Spec: "Format findings as structured feedback and post to the PR as
    a comment so the human sees the meta-audit."

    The coder receives the findings via ``compose_fix_task``'s ``feedback``
    argument (see the FEEDBACK block in the rendered task). Each finding
    must be preserved verbatim so the variant prompt's "fix the listed
    findings" instruction is actionable.
    """

    def test_each_finding_reaches_coder_verbatim(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_endorsing_unit()
        _endorse_tester_and_reviewer(monkeypatch)
        _stub_github(monkeypatch)
        address_log: list[dict] = []
        _stub_address_review_fix_pushed(monkeypatch, log=address_log)
        findings = [
            "src/a.py:7 — TOCTOU on lockfile",
            "src/b.py:31 — silent integer wrap",
            "src/c.py:88 — drops trailing newline on Windows",
        ]
        _stub_ultrareview(monkeypatch, [(False, findings), (True, [])])

        execution.cycle_review("F-001", "F-001-U-1")

        assert len(address_log) == 1
        assert address_log[0]["source"] == "ultrareview"
        feedback = address_log[0]["feedback"]
        for f in findings:
            assert f in feedback, (
                f"finding {f!r} must reach the coder verbatim — without it "
                f"the variant prompt's 'fix the listed findings' is unactionable"
            )


# ============================================================================
# PR meta-audit comment is scoped per cycle
# ============================================================================


class TestPRCommentPerFailCycle:
    """Multiple FAIL cycles must each produce a fresh meta-audit comment
    surfacing **that cycle's** findings. Re-using a stale comment would
    leave the human looking at the wrong audit when a fix only resolves
    some findings.
    """

    def test_findings_in_each_comment_match_that_cycles_verdict(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_endorsing_unit()
        _endorse_tester_and_reviewer(monkeypatch)
        comment_log: list[dict] = []
        _stub_github(monkeypatch, comment_log=comment_log)
        _stub_address_review_fix_pushed(monkeypatch)

        cycle_1 = ["src/a.py:7 — leak", "src/b.py:31 — wrap"]
        cycle_2 = ["src/c.py:88 — newline"]
        _stub_ultrareview(monkeypatch, [(False, cycle_1), (False, cycle_2), (True, [])])

        execution.cycle_review("F-001", "F-001-U-1")

        # Pull just the ultrareview meta-audit comments (cycle_review may
        # post other comments — coder-fix-pushed receipts etc.).
        meta_comments = [
            c
            for c in comment_log
            if "ultrareview" in c["body"].lower() and "audit" in c["body"].lower()
        ]
        assert len(meta_comments) >= 2, (
            f"each FAIL cycle must post its own meta-audit comment; got {len(meta_comments)}"
        )

        # Cycle-1 comment carries cycle-1 findings, NOT cycle-2's.
        c1_body = meta_comments[0]["body"]
        for f in cycle_1:
            assert f in c1_body, f"cycle-1 comment missing {f!r}"
        assert cycle_2[0] not in c1_body, (
            "cycle-1 comment must not contain cycle-2's findings — that would "
            "mean the comments are joined or out of order"
        )

        # Cycle-2 comment carries cycle-2 findings, NOT cycle-1's.
        c2_body = meta_comments[1]["body"]
        for f in cycle_2:
            assert f in c2_body
        assert cycle_1[0] not in c2_body, (
            "cycle-2 comment must not contain cycle-1's findings — implies "
            "the renderer is leaking earlier-cycle state into later comments"
        )

    def test_meta_audit_comment_posted_before_address_review(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Ordering matters for the human reading the PR: the audit
        findings should be visible *before* the coder's fix-push comment
        lands, so the conversation reads "ultrareview caught X → coder
        fixed it" rather than "coder pushed → here's why".
        """
        _seed_endorsing_unit()
        _endorse_tester_and_reviewer(monkeypatch)

        ordering: list[str] = []

        def fake_comment(repo_url, pr_number, body):
            if "ultrareview" in body.lower() and "audit" in body.lower():
                ordering.append("meta_audit_comment")
            return ""

        def fake_address_review(unit_id, source, feedback):
            ordering.append(f"address_review:{source}")
            round_num = state.increment_review_round(unit_id)
            return json.dumps(
                {"unit_id": unit_id, "cycle": round_num, "outcome": "FIX_PUSHED", "summary": "ok"}
            )

        _stub_github(monkeypatch)
        monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", fake_comment)
        monkeypatch.setattr(execution, "address_review", fake_address_review)
        _stub_ultrareview(monkeypatch, [(False, ["leak"]), (True, [])])

        execution.cycle_review("F-001", "F-001-U-1")

        # The meta-audit comment must precede the address_review call for
        # source='ultrareview'.
        try:
            audit_idx = ordering.index("meta_audit_comment")
        except ValueError:
            pytest.fail(f"meta-audit comment never posted; ordering: {ordering!r}")
        try:
            addr_idx = ordering.index("address_review:ultrareview")
        except ValueError:
            pytest.fail(
                f"address_review(source='ultrareview', ...) never called; ordering: {ordering!r}"
            )
        assert audit_idx < addr_idx, (
            f"meta-audit comment must post BEFORE address_review(ultrareview); "
            f"got ordering: {ordering!r}"
        )


# ============================================================================
# coder_resumed event carries source='ultrareview' for cycle-log filtering
# ============================================================================


class TestCoderResumedEventSourceAttribution:
    """The orchestrator's cycle-log + cost-attribution pipelines filter
    events by ``source`` to break out per-driver spend. ``address_review``
    writes a ``coder_resumed`` event carrying its ``source=`` argument;
    a regression that hardcoded ``source='reviewer'`` or dropped the kwarg
    would silently merge ultrareview spend into reviewer spend.
    """

    def test_real_address_review_records_source_ultrareview(self, tmp_state_db, monkeypatch):
        _seed_endorsing_unit()
        _stub_github(monkeypatch)
        # Use the real address_review (not the FIX_PUSHED stub) so the
        # coder_resumed event lands. Stub only the worker.
        from tests.test_tools_execution import _install_fake_worker

        _install_fake_worker(monkeypatch, resume_response="ok\nFIX_PUSHED\n")

        execution.address_review("F-001-U-1", "ultrareview", "leak at a.py:7")

        events = state.list_events("F-001-U-1")
        coder_resumed = [e for e in events if e["event_type"] == "coder_resumed"]
        assert coder_resumed, "address_review must record a coder_resumed event"
        assert coder_resumed[-1]["source"] == "ultrareview", (
            f"coder_resumed event from address_review(source='ultrareview', ...) "
            f"must carry source='ultrareview', got {coder_resumed[-1]['source']!r} — "
            "merging spend into the wrong driver"
        )


# ============================================================================
# Documentation updates — README + CLAUDE.md
# ============================================================================


class TestDocsMentionUltrareviewFixLoop:
    """The unit description explicitly lists "Update README + CLAUDE.md
    persona" as a deliverable. The runtime persona (CLAUDE.md) must
    surface the new ``source='ultrareview'`` route so the lead agent
    knows the option exists for manual re-runs; README documents the
    behaviour for human contributors.

    These tests are deliberately content-based rather than diff-based:
    pinning the exact wording would create noisy churn on every docs
    polish pass. We assert the meaningful tokens stay present.
    """

    @pytest.fixture
    def repo_root(self) -> Path:
        # The test file lives at <repo>/tests/test_*.py
        return Path(__file__).resolve().parent.parent

    def test_claude_md_lists_ultrareview_as_address_review_source(self, repo_root: Path):
        body = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
        assert "address_review" in body
        # The new source must appear in the documented signature alongside
        # the existing tester|reviewer|ci|human options.
        assert "ultrareview" in body, (
            "CLAUDE.md (runtime persona) must mention the new "
            "source='ultrareview' route for address_review so the lead "
            "knows the option exists"
        )

    def test_readme_documents_fix_loop_and_shared_cap(self, repo_root: Path):
        body = (repo_root / "README.md").read_text(encoding="utf-8").lower()
        # The README's ultrareview section must mention the FAIL → fix-loop
        # behavior (was "FAIL escalates immediately" pre-U-4).
        assert "ultrareview" in body
        # The shared-cap-3 semantics need to be discoverable so a user
        # reading the README doesn't think the loop runs unbounded.
        assert "cap-3" in body or "cap of 3" in body or "shared" in body, (
            "README must document the shared cap-3 budget for the "
            "ultrareview fix-loop — the unit description lists this as a "
            "deliverable and it's a user-facing concern (cost / runtime)"
        )
        # The meta-audit PR comment is also user-visible — readme should
        # note it lands on the PR so a human merging the PR knows to look
        # for it.
        assert "meta-audit" in body or "pr comment" in body or "comment" in body, (
            "README must document that ultrareview findings post to the PR "
            "as a comment (spec: 'so the human sees the meta-audit')"
        )


# ============================================================================
# PASS-on-second-attempt does NOT post a stale comment
# ============================================================================


class TestNoStaleCommentOnPass:
    """A FAIL followed by a PASS posts exactly one meta-audit comment
    (the FAIL's). The PASS path must not post anything — there are no
    findings to surface, and a "passed!" comment on top of the FAIL
    comment would be noise.
    """

    def test_fail_then_pass_posts_exactly_one_meta_audit_comment(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_endorsing_unit()
        _endorse_tester_and_reviewer(monkeypatch)
        comment_log: list[dict] = []
        _stub_github(monkeypatch, comment_log=comment_log)
        _stub_address_review_fix_pushed(monkeypatch)
        _stub_ultrareview(monkeypatch, [(False, ["leak"]), (True, [])])

        execution.cycle_review("F-001", "F-001-U-1")

        meta = [
            c
            for c in comment_log
            if "ultrareview" in c["body"].lower() and "audit" in c["body"].lower()
        ]
        assert len(meta) == 1, (
            f"FAIL → PASS must post exactly one meta-audit comment (the FAIL's), got {len(meta)}"
        )
