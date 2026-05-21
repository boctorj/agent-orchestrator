"""Tester-written contracts for F-007-U-3 (ultrareview gate wiring in cycle_review).

These tests are independent of the coder's own ``TestCycleReviewUltrareviewGate``
in ``tests/test_tools_execution.py``. They lock down the behaviours the unit
description promises that the coder's suite doesn't already pin verbatim:

  * **ntfy semantics.** On PASS the cycle terminates ``approved_awaiting_merge``
    and fires ``ntfy.push_ready_to_merge`` (the user gets a phone push that the
    PR is ready). On FAIL it fires ``ntfy.push_escalation`` (the user needs to
    look) and does NOT fire ``push_ready_to_merge``. The unit description names
    "ntfy ready-to-merge" explicitly — without verifying the call, we'd ship a
    silent gate that catches bugs but never tells the user.

  * **trigger → wait ordering.** ``ultrareview.trigger`` must complete before
    ``wait_for_result`` is invoked (the wrapper's split is fire-then-harvest).
    A swapped order would harvest a stale verdict or raise the
    "no ultrareview run registered" RuntimeError baked into the U-2 wrapper.

  * **Fail-closed on wait_for_result raising.** The coder's existing suite covers
    the ``trigger`` raise path; this pins the symmetric ``wait_for_result`` raise
    path. Both must escalate, not crash and not silently endorse.

  * **PR URL canonicalization.** The gate must pass the canonical PR URL to the
    wrapper (``https://github.com/owner/repo/pull/N`` from ``parse_repo_url`` +
    ``unit_state.pr_number``), not e.g. the repo_path. The wrapper keys its
    registry on the canonical URL — a mismatch here would make ``wait_for_result``
    fail to find the run.

  * **History breadcrumbs.** The JSON ``cycle_review`` returns includes
    ``ultrareview_started`` and ``ultrareview`` step entries (with the findings
    list on FAIL) so the lead can show the user *why* a unit escalated.

  * **Escalation skips the gate.** When the reviewer phase ends in escalation
    (e.g. cap-3 hit on REVIEW_REQUEST_CHANGES), the ultrareview gate does NOT
    fire — the spec says "after our reviewer emits REVIEW_RECOMMEND_MERGE",
    not "after our reviewer phase ends." Spending ultrareview tokens on a unit
    we already know is escalated wastes the cost-sensitivity rationale of the
    opt-in flag.

  * **Event source attribution.** The ``ultrareview_started`` /
    ``ultrareview_passed`` / ``ultrareview_failed`` events are recorded with
    ``source='ultrareview'`` (not 'reviewer' or 'orchestrator') so the cost
    attribution pipeline F-007 plans can separate ultrareview spend from other
    agent spend (the open question in spec.md is precisely this break-out).

All worker / GitHub / ntfy / ultrareview surfaces are stubbed via monkeypatch.
No live network, no real ``claude`` CLI, no real GitHub calls.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared scaffolding ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green — focus tests on the ultrareview gate."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _stub_github_helpers(monkeypatch, copilot_review=None):
    """No-op all github.* surfaces cycle_review touches."""
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
        lambda *a, **k: copilot_review,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("owner", "repo"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.get_pr_state",
        lambda *a, **k: {"head_sha": "deadbeef", "state": "open", "merged": False},
    )


def _seed_feature(feature_id="F-001", *, ultrareview_enabled=False):
    """Seed a feature + plan + already-PR'd unit (skips coder + tester phases)."""
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


def _stub_phases_for_endorsement(monkeypatch):
    """Make tester pass + reviewer endorse — leaves only the ultrareview branch under test."""
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


def _record_ntfy(monkeypatch):
    """Capture every ntfy call into a list of ('ready' | 'escalate', args, kwargs)."""
    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge",
        lambda *a, **k: calls.append(("ready", a, k)),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda *a, **k: calls.append(("escalate", a, k)),
    )
    return calls


def _record_ultrareview(monkeypatch, *, passed: bool, findings=None, order_log=None):
    """Stub ultrareview.trigger + wait_for_result; record calls into a shared list.

    ``order_log``: if provided, each call appends ('trigger', pr_url) /
    ('wait', pr_url) so the test can assert ordering.
    """
    findings = list(findings or [])
    calls: dict[str, list] = {"trigger": [], "wait": []}

    def fake_trigger(pr_url, **kw):
        calls["trigger"].append((pr_url, kw))
        if order_log is not None:
            order_log.append(("trigger", pr_url))

    def fake_wait(pr_url, **kw):
        calls["wait"].append((pr_url, kw))
        if order_log is not None:
            order_log.append(("wait", pr_url))
        return {"passed": passed, "findings": findings}

    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.trigger", fake_trigger)
    monkeypatch.setattr("orchestrator.tools.execution.ultrareview.wait_for_result", fake_wait)
    return calls


# --------------------------- ntfy contract tests ---------------------------


class TestUltrareviewNtfySemantics:
    """The unit description literally says "PASS -> approved_awaiting_merge
    (ntfy ready-to-merge)". On FAIL the cycle escalates — escalations have
    their own ntfy path. These tests pin BOTH calls (and their negative
    counterparts) so a silent regression in the phone-push surface fails CI.
    """

    def test_pass_fires_ready_to_merge_push(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        ntfy_calls = _record_ntfy(monkeypatch)
        _record_ultrareview(monkeypatch, passed=True)

        execution.cycle_review("F-001", "F-001-U-1")

        kinds = [c[0] for c in ntfy_calls]
        assert "ready" in kinds, "PASS path must fire ntfy.push_ready_to_merge"
        assert "escalate" not in kinds, "PASS path must NOT escalate"
        # The PR URL must be in the ready-to-merge push so phone tap → PR.
        ready_call = next(c for c in ntfy_calls if c[0] == "ready")
        _kind, args, _kwargs = ready_call
        assert "https://github.com/owner/repo/pull/5" in args, (
            "ready-to-merge push must carry the PR URL (phone tap target)"
        )

    def test_fail_fires_escalation_and_no_ready_to_merge(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        ntfy_calls = _record_ntfy(monkeypatch)
        findings = ["src/a.py:1 — leak", "src/b.py:2 — race"]
        _record_ultrareview(monkeypatch, passed=False, findings=findings)

        execution.cycle_review("F-001", "F-001-U-1")

        kinds = [c[0] for c in ntfy_calls]
        assert "escalate" in kinds, "FAIL path must fire ntfy.push_escalation"
        assert "ready" not in kinds, (
            "FAIL must NOT fire ready-to-merge — that would lie to the user about merge readiness"
        )

    def test_flag_off_endorsement_still_fires_ready_to_merge(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """With the flag off, ultrareview is bypassed but today's
        ready-to-merge ntfy push must still fire on REVIEW_RECOMMEND_MERGE.
        Regression guard against the gate accidentally swallowing the push.
        """
        _seed_feature(ultrareview_enabled=False)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        ntfy_calls = _record_ntfy(monkeypatch)
        _record_ultrareview(monkeypatch, passed=True)  # would fire if it were checked

        execution.cycle_review("F-001", "F-001-U-1")

        kinds = [c[0] for c in ntfy_calls]
        assert "ready" in kinds, "flag-off path must preserve today's ready-to-merge phone push"


# --------------------------- invocation contract tests ---------------------------


class TestUltrareviewInvocationContract:
    """Pin the wire-protocol with the U-2 wrapper.

    The wrapper's registry keys on the canonical PR URL; a typo in the URL
    the gate passes would mean ``wait_for_result`` raises "no ultrareview run
    registered". The ordering check guards against an editor reordering the
    two calls (the wrapper supports the split deliberately).
    """

    def test_trigger_is_called_before_wait_for_result(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)

        order_log: list[tuple[str, str]] = []
        _record_ultrareview(monkeypatch, passed=True, order_log=order_log)

        execution.cycle_review("F-001", "F-001-U-1")

        # Exactly one of each, in this order.
        kinds = [k for k, _ in order_log]
        assert kinds == ["trigger", "wait"], f"trigger must precede wait_for_result; got {kinds}"

    def test_canonical_pr_url_passed_to_wrapper(self, tmp_state_db, with_github_token, monkeypatch):
        """The gate must hand the wrapper a canonical
        ``https://github.com/<owner>/<repo>/pull/<N>`` URL built from
        ``feature.repo_path`` + ``unit_state.pr_number``, not e.g. the
        repo_path alone or the branch URL — those would never match the
        wrapper's registry key.
        """
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)
        calls = _record_ultrareview(monkeypatch, passed=True)

        execution.cycle_review("F-001", "F-001-U-1")

        assert len(calls["trigger"]) == 1
        trigger_url = calls["trigger"][0][0]
        assert trigger_url == "https://github.com/owner/repo/pull/5", (
            f"gate passed non-canonical URL to ultrareview.trigger: {trigger_url!r}"
        )
        # And wait_for_result gets the SAME URL so the registry hit works.
        assert calls["wait"][0][0] == trigger_url, (
            "trigger + wait_for_result must agree on the PR URL (registry key)"
        )

    def test_wait_for_result_raise_escalates_fail_closed(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Symmetric to the coder's ``trigger`` raise test: a raise from
        ``wait_for_result`` (e.g. wrapper-internal assertion error) must
        also escalate. The wrapper's contract treats unparseable output as
        a sentinel finding, but unexpected Python-level exceptions must
        still fail closed — not crash cycle_review nor silently endorse.
        """
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)

        def fake_trigger(pr_url, **kw):
            pass

        def boom_wait(pr_url, **kw):
            raise RuntimeError("wrapper internal: subprocess vanished")

        monkeypatch.setattr("orchestrator.tools.execution.ultrareview.trigger", fake_trigger)
        monkeypatch.setattr("orchestrator.tools.execution.ultrareview.wait_for_result", boom_wait)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated", (
            "wait_for_result raise must escalate (fail-closed), not silently endorse"
        )
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "ultrareview_failed" in types, "wrapper raise must record ultrareview_failed"
        assert "ultrareview_passed" not in types


# --------------------------- gate-firing precondition tests ---------------------------


class TestUltrareviewGateOnlyAfterEndorsement:
    """The spec says the gate fires after ``REVIEW_RECOMMEND_MERGE``. Any
    other reviewer terminal — escalation, BLOCKED, REVIEW_REQUEST_CHANGES
    that hits cap-3 — must NOT spend ultrareview tokens (the whole opt-in
    flag exists because each run has measurable cost).
    """

    def test_reviewer_cap_3_escalation_skips_ultrareview(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """If the reviewer phase ends in cap-3 escalation, ultrareview must NOT
        fire — we already know the unit is going to the human, no point burning
        tokens on a pre-merge gate for a PR that isn't ready to merge.
        """
        _seed_feature(ultrareview_enabled=True)
        # Tester passes — reviewer requests changes — coder "fixes" — reviewer
        # requests changes again — etc. until cap-3 hits. We don't need the
        # full loop; just make the reviewer always return REVIEW_REQUEST_CHANGES
        # and the coder always FIX_PUSHED. _reviewer_phase exits when
        # review_round >= CAP_3.
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )

        # Crank the unit's review_round up to CAP_3 already so the first
        # REVIEW_REQUEST_CHANGES bounces out immediately.
        from orchestrator.tools import CAP_3

        s = state.get_unit_state("F-001-U-1")
        s.review_round = CAP_3
        state.upsert_unit_state(s)

        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps(
                {"unit_id": u, "outcome": "REVIEW_REQUEST_CHANGES", "issue": "x"}
            ),
        )
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)
        calls = _record_ultrareview(monkeypatch, passed=True)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated"
        assert calls["trigger"] == [], (
            "ultrareview must NOT fire on a reviewer escalation — spec says "
            "only after REVIEW_RECOMMEND_MERGE"
        )
        assert calls["wait"] == []
        types = [e["event_type"] for e in state.list_events("F-001-U-1")]
        assert "ultrareview_started" not in types


# --------------------------- event attribution + history tests ---------------------------


class TestUltrareviewEventAttribution:
    """Event source + event-types pinned for cost attribution + cycle-log entries.

    spec.md U-3: "Emit ultrareview_started / ultrareview_passed /
    ultrareview_failed events." The OPEN-QUESTION in spec.md explicitly calls
    out that these events drive future cost break-out. If the source string
    drifts from 'ultrareview' (e.g. to 'orchestrator'), the cost pipeline can't
    distinguish ultrareview spend.
    """

    def test_pass_events_have_source_ultrareview(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)
        _record_ultrareview(monkeypatch, passed=True)

        execution.cycle_review("F-001", "F-001-U-1")

        events = state.list_events("F-001-U-1")
        started = [e for e in events if e["event_type"] == "ultrareview_started"]
        passed = [e for e in events if e["event_type"] == "ultrareview_passed"]
        assert len(started) == 1 and len(passed) == 1
        assert started[0]["source"] == "ultrareview", (
            f"ultrareview_started.source must be 'ultrareview', got {started[0]['source']!r}"
        )
        assert passed[0]["source"] == "ultrareview", (
            f"ultrareview_passed.source must be 'ultrareview', got {passed[0]['source']!r}"
        )
        # The PR URL should be discoverable on the started event so the cycle
        # log can link to it without a join.
        assert "/pull/5" in (started[0].get("details") or ""), (
            "ultrareview_started.details should carry the PR URL "
            "(cycle-log surfaces it without re-fetching)"
        )

    def test_fail_event_carries_findings_in_details(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Unit description: "On FAIL, initial impl escalates with findings."
        The findings must be persisted on the ``ultrareview_failed`` event so
        the cycle log + post-mortem dashboard can surface them without
        re-running the wrapper.
        """
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)
        findings = [
            "src/leak.py:42 — file descriptor leaked on retry",
            "src/race.py:7 — TOCTOU on cache invalidation",
        ]
        _record_ultrareview(monkeypatch, passed=False, findings=findings)

        execution.cycle_review("F-001", "F-001-U-1")

        events = state.list_events("F-001-U-1")
        failed = [e for e in events if e["event_type"] == "ultrareview_failed"]
        assert len(failed) == 1, "exactly one ultrareview_failed event per cycle"
        details = failed[0].get("details") or ""
        for f in findings:
            assert f in details, (
                f"finding {f!r} must appear in ultrareview_failed.details (cycle-log surface)"
            )
        # The summary should reference the failure (a human reading the event
        # log shouldn't need to parse the details JSON to see what happened).
        summary = (failed[0].get("summary") or "").lower()
        assert "fail" in summary or "ultrareview" in summary, (
            f"ultrareview_failed.summary should describe the failure, got {summary!r}"
        )


# --------------------------- history payload tests ---------------------------


class TestUltrareviewHistoryBreadcrumbs:
    """The JSON ``cycle_review`` returns surfaces a ``history`` list the lead
    shows the user on escalation. The ultrareview gate must leave a trail
    there too — without it, the user sees an "escalated" outcome with no
    clue ultrareview was even consulted.
    """

    def test_history_includes_ultrareview_started_on_pass(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)
        _record_ultrareview(monkeypatch, passed=True)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        steps = [h.get("step") for h in parsed["history"]]
        assert "ultrareview_started" in steps, (
            "history must record that ultrareview was triggered (for the "
            "lead's user-facing summary)"
        )
        # And there must be an entry reporting the verdict.
        verdict_entries = [h for h in parsed["history"] if h.get("step") == "ultrareview"]
        assert len(verdict_entries) >= 1, "history must record the ultrareview verdict step"
        outcomes = [e.get("outcome") for e in verdict_entries]
        assert "passed" in outcomes, f"expected 'passed' in verdict outcomes, got {outcomes!r}"

    def test_history_carries_findings_on_fail(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature(ultrareview_enabled=True)
        _stub_phases_for_endorsement(monkeypatch)
        _stub_github_helpers(monkeypatch)
        _record_ntfy(monkeypatch)
        findings = ["x:1 — bug A", "y:2 — bug B"]
        _record_ultrareview(monkeypatch, passed=False, findings=findings)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        verdict_entries = [h for h in parsed["history"] if h.get("step") == "ultrareview"]
        assert len(verdict_entries) >= 1
        # The failing verdict entry must carry the findings list (so the lead
        # can surface them to the user without going to the event log).
        failing = [e for e in verdict_entries if e.get("outcome") == "failed"]
        assert len(failing) == 1, f"expected one failed verdict, got {verdict_entries!r}"
        recorded = failing[0].get("findings") or []
        for f in findings:
            assert f in recorded, f"finding {f!r} must appear in history[ultrareview].findings"
