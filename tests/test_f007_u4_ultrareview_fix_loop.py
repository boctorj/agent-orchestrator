"""Tests for F-007-U-4 — ultrareview FAIL fix-loop in cycle_review.

U-4 extends F-007 so that an ultrareview FAIL no longer escalates immediately:

  * ``address_review`` accepts ``source='ultrareview'`` (in addition to
    tester / reviewer / ci / human).
  * ``compose_fix_task`` emits an ultrareview-specific coder prompt anchoring
    them on "reviewer already endorsed, fix without scope creep".
  * ``_ultrareview_phase`` runs a fix-loop on FAIL: post structured findings
    as a PR comment → ``address_review(source='ultrareview', ...)`` → wait
    for CI green → re-fire ``/ultrareview`` (NOT the reviewer agent).
  * The loop shares the global CAP_3 budget with tester-bug / reviewer-change
    / CI-fail fix cycles. Cap hit → escalate with full ultrareview history.
  * An ``ultrareview_fix_cycle_N`` event lands on each fix iteration (where
    ``N`` is the upcoming ``review_round``) for cost attribution + cycle-log.

Tests are isolated via monkeypatch — no live ``claude`` CLI, no GitHub API,
no real worker session. All ultrareview / address_review / github / ntfy
surfaces are stubbed.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import cycle_log, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import CAP_3, compose_fix_task, execution

# --------------------------- shared scaffolding ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green — focus tests on the fix-loop semantics."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _stub_github(monkeypatch, *, comment_log=None):
    """No-op the github.* + PR-comment surfaces; optionally record PR comments."""

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


def _seed_endorsing_unit(*, ultrareview_enabled=True, feature_id="F-001"):
    """Feature + plan + already-PR'd unit; tester+reviewer stubs endorse."""
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
            ultrareview_enabled=ultrareview_enabled,
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=f"{feature_id}-U-1", feature_id=feature_id, title="u1", description="impl")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=f"{feature_id}-U-1",
            feature_id=feature_id,
            status="in_ci",
            branch="feat/branch",
            pr_number=5,
            coder_session_id="sesn-c",
        )
    )


def _endorse_phases(monkeypatch):
    """Tester always TESTS_PASS, reviewer always REVIEW_RECOMMEND_MERGE."""
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
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda *a, **k: True,
    )


def _stub_ultrareview_sequence(monkeypatch, verdicts):
    """Stub ultrareview.trigger/wait_for_result to return a SEQUENCE of verdicts.

    ``verdicts`` is a list of ``(passed, findings)`` tuples; each successive
    ``wait_for_result`` call returns the next one. Calls beyond the list
    repeat the final verdict.
    """
    calls = {"trigger": [], "wait": []}
    iterator = iter(verdicts)
    last = {"value": verdicts[-1]}

    def fake_trigger(pr_url, **kw):
        calls["trigger"].append(pr_url)

    def fake_wait(pr_url, **kw):
        calls["wait"].append(pr_url)
        try:
            passed, findings = next(iterator)
            last["value"] = (passed, findings)
        except StopIteration:
            passed, findings = last["value"]
        return {"passed": passed, "findings": list(findings)}

    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.trigger", fake_trigger)
    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.wait_for_result", fake_wait)
    return calls


def _stub_address_review_fix_pushed(monkeypatch, *, log=None):
    """Replace address_review with a stub that always FIX_PUSHED's.

    Increments ``review_round`` to mimic the real call's cap-3 accounting,
    and records the (unit_id, source, feedback) tuple if ``log`` is provided.
    """

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


# --------------------------- compose_fix_task variant ---------------------------


class TestComposeFixTaskUltrareviewVariant:
    """The ultrareview-source variant of ``compose_fix_task`` anchors the coder
    on "fix without scope creep" — distinct from the tester/reviewer/human
    inline-comment flow and from the ci flat-FEEDBACK flow. Verifying the
    variant exists as a real branch (not the fallback prose) protects against
    a future refactor silently dropping the anti-scope-creep nudge.
    """

    def _feature_unit(self):
        f = Feature(id="F-1", title="t", description="d", repo_path="https://github.com/o/r")
        u = WorkUnit(id="F-1-U-1", feature_id="F-1", title="u1", description="impl")
        return f, u

    def test_ultrareview_source_emits_no_scope_creep_anchor(self):
        f, u = self._feature_unit()
        out = compose_fix_task(f, u, "feat/branch", 42, "ultrareview", "leak at src/a.py:7")
        assert "SOURCE:    ultrareview" in out
        # The whole point of the variant — anchor the coder on minimal patches.
        assert "scope creep" in out.lower(), (
            "ultrareview variant must explicitly anchor on no-scope-creep — the "
            "reviewer already endorsed, broad refactors would invalidate that"
        )
        # Reviewer-already-endorsed framing surfaces *why* scope creep is wrong.
        assert "REVIEW_RECOMMEND_MERGE" in out or "endorsed" in out.lower(), (
            "ultrareview variant must remind the coder the reviewer endorsed"
        )
        # FEEDBACK is the source of truth — same shape as ci, no inline anchors.
        assert "leak at src/a.py:7" in out
        assert "FIX_PUSHED" in out

    def test_tester_source_keeps_inline_reply_guidance(self):
        """Regression guard: the variant branch must not eat the existing
        tester/reviewer/human guidance about inline replies.
        """
        f, u = self._feature_unit()
        out = compose_fix_task(f, u, "feat/branch", 42, "tester", "fix")
        assert "SOURCE:    tester" in out
        assert "inline" in out.lower(), (
            "tester source must keep the inline-reply guidance (system-prompt path)"
        )
        # And the ultrareview-only nudge must not bleed into the tester variant.
        assert "scope creep" not in out.lower()

    def test_ci_source_unchanged_by_variant(self):
        f, u = self._feature_unit()
        out = compose_fix_task(f, u, "feat/branch", 42, "ci", "build failed: 137")
        assert "SOURCE:    ci" in out
        assert "build failed: 137" in out
        assert "scope creep" not in out.lower()


# --------------------------- address_review accepts ultrareview ---------------------------


class TestAddressReviewAcceptsUltrareview:
    """``address_review`` must accept ``source='ultrareview'``. Without this,
    ``_ultrareview_phase`` can't drive the fix-loop — every call would bounce
    on the pre-F-007-U-4 'source must be tester|reviewer|ci|human' guard.
    """

    def test_ultrareview_source_no_longer_rejected(self, tmp_state_db, monkeypatch):
        _seed_endorsing_unit()
        _stub_github(monkeypatch)
        # Stub the worker so resume doesn't try to talk to Anthropic.
        from tests.test_tools_execution import _install_fake_worker

        _install_fake_worker(monkeypatch, resume_response="ok\nFIX_PUSHED\n")
        out = execution.address_review("F-001-U-1", "ultrareview", "fix x:1, y:2")
        # Pre-F-007-U-4 this returned an "ERROR: source must be ..." string.
        assert "ERROR: source must be" not in out
        parsed = json.loads(out)
        assert parsed["outcome"] == "FIX_PUSHED"

    def test_bad_source_still_rejected(self, tmp_state_db):
        msg = execution.address_review("U1", "hacker", "fix it")
        assert "source must be" in msg
        # The new source name must appear in the allow-list (otherwise the
        # next refactor could silently drop it from the error string and
        # break the variant prompt's discoverability).
        assert "ultrareview" in msg


# --------------------------- fix-loop happy path ---------------------------


class TestUltrareviewFixLoopHappyPath:
    """FAIL → coder fixes → ultrareview re-runs → PASS → approved_awaiting_merge.

    The defining behaviour of U-4: an ultrareview FAIL no longer escalates
    immediately. The coder gets a turn to fix the findings, then ultrareview
    re-runs (NOT the reviewer agent) and we terminate cleanly on the second
    PASS.
    """

    def test_fail_then_pass_terminates_approved(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        _stub_github(monkeypatch)
        address_log: list[dict] = []
        _stub_address_review_fix_pushed(monkeypatch, log=address_log)
        ur_calls = _stub_ultrareview_sequence(
            monkeypatch,
            [(False, ["src/a.py:1 — leak"]), (True, [])],
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge", (
            f"FAIL→fix→PASS must terminate clean; got {parsed['outcome']!r}"
        )

        # ultrareview ran twice (initial verdict + post-fix re-run).
        assert len(ur_calls["trigger"]) == 2
        assert len(ur_calls["wait"]) == 2
        # address_review fired once with source='ultrareview'.
        assert len(address_log) == 1
        assert address_log[0]["source"] == "ultrareview"
        assert "src/a.py:1 — leak" in address_log[0]["feedback"]

    def test_reviewer_agent_is_not_re_engaged_in_fix_loop(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Spec: "re-run ultrareview (NOT the reviewer agent)". Counting the
        spawn_reviewer calls catches a regression where the FAIL loop
        accidentally routes back through ``_reviewer_phase`` (which would
        duplicate work and waste tokens — the reviewer already endorsed).
        """
        _seed_endorsing_unit()
        reviewer_calls = {"count": 0}

        def counting_reviewer(f, u):
            reviewer_calls["count"] += 1
            return json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"})

        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(execution, "spawn_reviewer", counting_reviewer)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )
        _stub_github(monkeypatch)
        _stub_address_review_fix_pushed(monkeypatch)
        _stub_ultrareview_sequence(
            monkeypatch,
            [(False, ["finding-A"]), (False, ["finding-B"]), (True, [])],
        )

        execution.cycle_review("F-001", "F-001-U-1")

        # Reviewer ran exactly once (the initial endorsement). The FAIL loop
        # iterated twice (3 ultrareview runs total, 2 fix cycles), but the
        # reviewer agent must not have been re-engaged.
        assert reviewer_calls["count"] == 1, (
            f"reviewer agent must NOT be re-engaged in the fix loop; "
            f"called {reviewer_calls['count']}x"
        )


# --------------------------- fix-loop cap-3 ---------------------------


class TestUltrareviewFixLoopCap3:
    """The fix-loop shares the global CAP_3 budget. When the cap hits, the
    cycle escalates with the full ultrareview history rather than looping
    forever on a finding ultrareview refuses to clear.
    """

    def test_persistent_fail_escalates_at_cap_3(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        _stub_github(monkeypatch)
        address_log: list[dict] = []
        _stub_address_review_fix_pushed(monkeypatch, log=address_log)
        # Always FAIL — the loop should give up at CAP_3 fix cycles.
        _stub_ultrareview_sequence(monkeypatch, [(False, ["never-clears"])])

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        assert "cap" in parsed["message"].lower() and str(CAP_3) in parsed["message"], (
            "cap-3 escalation message must surface the cap explicitly"
        )

        # We hit cap-3 after exactly CAP_3 coder fix cycles routed through
        # address_review(source='ultrareview', ...).
        assert len(address_log) == CAP_3, (
            f"expected {CAP_3} fix cycles before cap, got {len(address_log)}"
        )
        # The unit's review_round persisted the cap-3 budget — used for
        # later cross-feature scheduling decisions.
        unit_state = state.get_unit_state("F-001-U-1")
        assert unit_state.review_round >= CAP_3

    def test_escalation_message_carries_full_findings_history(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """On escalation the message must include the LAST verdict's
        findings so the human reading the ntfy push / dashboard sees what
        ultrareview is still complaining about — not just "cap-3 hit".
        """
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        _stub_github(monkeypatch)
        _stub_address_review_fix_pushed(monkeypatch)
        last_findings = ["src/a.py:1 — leak", "src/b.py:7 — race"]
        _stub_ultrareview_sequence(monkeypatch, [(False, last_findings)])

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        for f in last_findings:
            assert f in parsed["message"], (
                f"escalation message must surface unresolved finding {f!r}"
            )


# --------------------------- PR comment for meta-audit ---------------------------


class TestUltrareviewPRCommentMetaAudit:
    """Spec: "post to the PR as a comment so the human sees the meta-audit".

    The orchestrator's internal fix-loop is invisible to a human watching the
    PR conversation unless the findings are surfaced there. These tests pin
    that the comment is posted, carries the findings, and identifies itself
    as the meta-audit (so a human merging the PR understands what cycle of
    review they're seeing).
    """

    def test_pr_comment_posted_per_fail_cycle(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        comment_log: list[dict] = []
        _stub_github(monkeypatch, comment_log=comment_log)
        _stub_address_review_fix_pushed(monkeypatch)
        findings = ["src/a.py:1 — leak", "src/b.py:7 — race"]
        _stub_ultrareview_sequence(monkeypatch, [(False, findings), (True, [])])

        execution.cycle_review("F-001", "F-001-U-1")

        # The fix-loop's PR comment must be in the log alongside whatever
        # other safe_comment_pr calls cycle_review makes. Filter to the
        # ultrareview-audit comment by content.
        ur_comments = [c for c in comment_log if "ultrareview" in c["body"].lower()]
        assert len(ur_comments) >= 1, (
            "FAIL fix-loop must post the meta-audit as a PR comment "
            "(spec: 'so the human sees the meta-audit')"
        )
        body = ur_comments[0]["body"]
        for f in findings:
            assert f in body, f"meta-audit comment must surface finding {f!r}"
        # PR number must be correct — wrong target = audit lost.
        assert ur_comments[0]["pr_number"] == 5

    def test_pr_comment_omitted_when_ultrareview_passes_first_try(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """No FAIL → no fix-loop → no meta-audit PR comment. The PASS-path
        ntfy push is the user notification surface; spamming an additional
        PR comment on a clean run would be noise.
        """
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        comment_log: list[dict] = []
        _stub_github(monkeypatch, comment_log=comment_log)
        _stub_address_review_fix_pushed(monkeypatch)
        _stub_ultrareview_sequence(monkeypatch, [(True, [])])

        execution.cycle_review("F-001", "F-001-U-1")

        ur_comments = [c for c in comment_log if "meta-audit" in c["body"].lower()]
        assert ur_comments == [], (
            "PASS path must NOT post the meta-audit comment — that's only for the FAIL fix-loop"
        )

    def test_pr_comment_uses_forward_looking_wording(self):
        """The PR comment posts *before* ``address_review`` runs, so it must
        not assert present-tense action the coder hasn't taken yet — a
        BLOCKED coder or cap-3 exhaustion would leave a stale, factually-
        wrong claim on the PR timeline. (Copilot review on PR #51.)

        Direct-call the formatter rather than going through ``cycle_review``
        so the assertion holds regardless of which call site uses the
        helper. Pins both directions: present-tense "is addressing" is
        absent, and the forward-looking shape (orchestrator will / attempt /
        route / escalates if) is present.
        """
        body = execution._format_ultrareview_pr_comment(
            ["src/a.py:1 — leak", "src/b.py:7 — race"], cycle=1
        )

        # Negative assertions: any present-tense claim about coder action
        # the orchestrator can't yet guarantee.
        forbidden = (
            "coder is addressing them now",
            "the coder is addressing",
            "addressing them now",
        )
        for phrase in forbidden:
            assert phrase not in body.lower(), (
                f"meta-audit comment must NOT make present-tense claims "
                f"about coder action it can't yet guarantee; found {phrase!r}"
            )

        # Positive assertion: the comment must signal what happens on
        # BLOCKED / cap-3 (the failure modes the present-tense wording
        # ignored). Look for one of the forward-looking shapes.
        forward_looking_signals = (
            "will now route",
            "will attempt",
            "escalates",
            "if the coder blocks",
        )
        assert any(s in body.lower() for s in forward_looking_signals), (
            "meta-audit comment must use forward-looking framing — one of: "
            f"{forward_looking_signals}"
        )


# --------------------------- ultrareview_fix_cycle_N events ---------------------------


class TestUltrareviewFixCycleEvents:
    """The unit description names the event ``ultrareview_fix_cycle_N``. This
    feeds the cost-attribution + cycle-log pipelines: a reader filtering
    events by ``event_type LIKE 'ultrareview_fix_cycle_%'`` can count the
    ultrareview-driven fix cycles separately from tester-bug / reviewer-
    change / CI-fail cycles that also share CAP_3.
    """

    def test_event_recorded_per_fix_iteration(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        _stub_github(monkeypatch)
        _stub_address_review_fix_pushed(monkeypatch)
        # Two FAIL verdicts (one fix cycle) then PASS.
        _stub_ultrareview_sequence(
            monkeypatch,
            [(False, ["leak"]), (True, [])],
        )

        execution.cycle_review("F-001", "F-001-U-1")

        events = state.list_events("F-001-U-1")
        fix_cycle_events = [
            e for e in events if e["event_type"].startswith("ultrareview_fix_cycle_")
        ]
        assert len(fix_cycle_events) == 1, (
            f"expected exactly one ultrareview_fix_cycle_N event, got "
            f"{[e['event_type'] for e in fix_cycle_events]}"
        )
        # The N suffix is non-empty (the cycle number must be a real value,
        # not a default 0).
        evt = fix_cycle_events[0]
        suffix = evt["event_type"].rsplit("_", 1)[-1]
        assert suffix.isdigit() and int(suffix) >= 1, (
            f"event_type N suffix must be ≥1, got {evt['event_type']!r}"
        )
        # Source attribution — feeds the cost-break-out open question in spec.md.
        assert evt["source"] == "ultrareview"
        # Findings on the event so the cycle log can render them without
        # joining back to the verdict event.
        assert "leak" in (evt["details"] or "")

    def test_event_n_matches_review_round(self, tmp_state_db, with_github_token, monkeypatch):
        """The N suffix must align with the ``review_round`` cycle counter
        so the cycle-log renderer can correlate fix-cycle events with the
        ``coder_resumed`` events ``address_review`` records.
        """
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        _stub_github(monkeypatch)
        _stub_address_review_fix_pushed(monkeypatch)
        _stub_ultrareview_sequence(monkeypatch, [(False, ["x"]), (False, ["y"]), (True, [])])

        execution.cycle_review("F-001", "F-001-U-1")

        events = state.list_events("F-001-U-1")
        fix_cycles = [e for e in events if e["event_type"].startswith("ultrareview_fix_cycle_")]
        # Two FAILs → two fix iterations → cycle numbers 1, 2.
        ns = sorted(int(e["event_type"].rsplit("_", 1)[-1]) for e in fix_cycles)
        assert ns == [1, 2], f"expected cycles [1, 2]; got {ns}"

    def test_fix_cycle_event_renders_in_cycle_log_markdown(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The docstring on ``_ultrareview_phase`` names two consumers for
        ``ultrareview_fix_cycle_N``: cost-attribution + cycle-log. The
        earlier test pins the cost-attribution half (event in
        ``state.list_events``); this one pins the cycle-log half by
        rendering the markdown and checking the heading + summary land.

        Without this, the renderer can silently drop the event (U-3's H1
        pattern recurring for the new event type) and the
        ``state.list_events`` assertion in the sibling test still passes —
        cleanly slipping the regression through coverage. The recurrence
        on PR #51 cycle 0 (H1 finding from boctorj) is the reason this
        test exists.
        """
        _seed_endorsing_unit()
        _endorse_phases(monkeypatch)
        _stub_github(monkeypatch)
        _stub_address_review_fix_pushed(monkeypatch)
        _stub_ultrareview_sequence(monkeypatch, [(False, ["leak"]), (True, [])])

        # Patch parse_repo_url on the cycle_log renderer's import path too —
        # render_cycle_log goes through it for the PR URL line.
        monkeypatch.setattr(
            "orchestrator.cycle_log_render.parse_repo_url",
            lambda url: ("owner", "repo"),
        )

        execution.cycle_review("F-001", "F-001-U-1")

        md = cycle_log.render_cycle_log("F-001-U-1", pr_info={}, review_threads=[])
        assert "ultrareview: fix cycle 1" in md, (
            "ultrareview_fix_cycle_1 must render as a cycle-history heading — "
            "without it, the committed log silently drops the audit trail "
            "(the U-3 H1 pattern, recurring for the new event type)"
        )
        # Summary text must reach the rendered markdown too (not just the
        # heading) so a reader sees how many findings the cycle addressed.
        assert "coder fix cycle 1 for 1 ultrareview finding(s)" in md
