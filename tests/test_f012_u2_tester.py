"""Tester-written contracts for F-012-U-2 (reviewer session resume on retry).

These tests are independent of the coder's own ``tests/test_tools_execution.py``
and ``tests/test_reviewer_prompt.py``. They lock the behaviours the coder's
suite leaves under-pinned:

  * ``compose_reviewer_delta_task`` has a *direct* contract — the coder's
    tests only exercise it transitively through ``_resume_reviewer_for_delta``.
    If a future refactor changes the composer signature or drops a section
    of the delta message, the transitive tests can still pass while the
    agent contract silently drifts (e.g. a missing "RESOLVED/NOT_RESOLVED/
    N/A" vocabulary line would make the agent emit different statuses, and
    the eval fixture grep wouldn't match).
  * **Multi-retry session reuse.** The unit description's central claim is
    "stop clearing reviewer_session_id at execution.py:1030" — the coder's
    suite proves that *one* retry reuses the session, but the pre-F-012 bug
    re-cleared the id between every retry. A two-retry cycle (REQUEST_CHANGES
    → fix → REQUEST_CHANGES → fix → RECOMMEND_MERGE) is the canonical
    regression scenario; if a future refactor accidentally re-introduces
    the clear inside the loop, single-retry tests stay green.
  * **``_capture_reviewed_sha`` has a direct contract.** It's the bridge
    between the reviewer phase and the delta range — exceptions from the
    gh API must NOT propagate (the next retry's prompt has a documented
    fallback for unknown PRIOR_SHA), and the previous value must be
    preserved on failure.
  * **github.get_pr_state failure inside _resume_reviewer_for_delta.** The
    coder's stub always returns a SHA; the prompt's fallback string
    (``"(unknown — fetch via gh pr view --json headRefOid)"``) is the
    contract that makes "best-effort current_sha capture" safe. If the
    impl ever blew up on a gh failure instead of falling back, the entire
    delta retry would crash mid-cycle.
  * **Delta-scenario fixture self-consistency.** The coder pins axis
    coverage + valid markers, but does not check that
    ``expected_marker`` is *consistent* with ``diff_signal`` — e.g. a
    scenario flagged ``new_critical`` while emitting
    ``REVIEW_RECOMMEND_MERGE`` would be a fixture bug that quietly turns
    every eval green. The eval is only meaningful if expected verdicts
    follow the decision rule the prompt encodes.
  * **The reconciliation-table example in the prompt demonstrates all
    three statuses.** RESOLVED, NOT_RESOLVED, and N/A are the contract;
    showing only two would leave the agent without a worked example of
    the third (most-skippable) status.

All ``ManagedAgentWorker`` interactions are stubbed via the same
``FakeWorker`` pattern the coder's suite uses; no live Anthropic / GitHub
network calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import compose_reviewer_delta_task, execution

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEWER_PROMPT = REPO_ROOT / "orchestrator" / "prompts" / "reviewer.md"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "reviewer_delta_scenarios.json"


# --------------------------- autouse: pretend CI is green ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """CI is irrelevant to this unit's contracts — pretend it's green."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


# --------------------------- fakes (mirrors coder's test_tools_execution) ---------------------------


class _FakeWorker:
    """Stub for ManagedAgentWorker that returns a queue of canned strings.

    Differs from the coder's ``FakeWorker`` in only one respect: the
    response queue may hold a *list* of strings, popped FIFO. The coder's
    fake returns the same canned response every time, which is fine for
    one-spawn-one-resume tests; this tester needs multi-retry behaviour
    where each ``resume`` answers differently."""

    def __init__(self, role: str, spawn_responses=None, resume_responses=None):
        self.role = role
        self._spawn_responses: list[str] = list(spawn_responses or [""])
        self._resume_responses: list[str] = list(resume_responses or [""])
        self.spawn_calls: list[tuple[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        sid = f"sesn-{self.role}-{len(self.spawn_calls)}"
        self.spawn_calls.append((task, title))
        resp = self._spawn_responses.pop(0) if self._spawn_responses else ""
        return sid, resp

    def resume(self, session_id: str, msg: str) -> str:
        self.resume_calls.append((session_id, msg))
        return self._resume_responses.pop(0) if self._resume_responses else ""

    def archive(self, session_id: str) -> None:
        pass


def _install_fake_worker(monkeypatch, *, spawn_responses=None, resume_responses=None):
    """Install a per-role ``_FakeWorker`` factory. Returns the instance map."""
    instances: dict[str, _FakeWorker] = {}

    def factory(role: str) -> _FakeWorker:
        if role not in instances:
            instances[role] = _FakeWorker(role, spawn_responses, resume_responses)
        return instances[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return instances


def _stub_github(monkeypatch, head_sha="deadbeefcafe", copilot_review=None):
    """Patch github.* helpers so no network call leaks."""
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
        lambda *a, **k: {"head_sha": head_sha, "state": "open", "merged": False},
    )


def _seed_unit_ready_for_reviewer(reviewer_session_id="sesn-reviewer-existing"):
    """Set up F-001 + a unit that already has a coder PR and an existing
    reviewer session ready for a delta resume."""
    state.save_feature(
        Feature(
            id="F-001",
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            branch_prefix="feat/F-001",
            status="approved",
        )
    )
    state.save_plan(
        "F-001",
        [WorkUnit(id="F-001-U-1", feature_id="F-001", title="u1", description="impl this")],
    )
    state.approve_plan("F-001")
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-001-U-1",
            feature_id="F-001",
            status="reviewing",
            branch="feat/F-001-u-1",
            pr_number=42,
            coder_session_id="sesn-coder-existing",
            reviewer_session_id=reviewer_session_id,
        )
    )


# =============================================================================
# Composer contract — direct tests of compose_reviewer_delta_task
# =============================================================================


@pytest.fixture
def feat_and_unit():
    f = Feature(
        id="F-001",
        title="t",
        description="d",
        repo_path="https://github.com/o/r",
        branch_prefix="feat/F-001",
    )
    u = WorkUnit(id="F-001-U-1", feature_id="F-001", title="u", description="d")
    return f, u


class TestComposeReviewerDeltaTask:
    """The delta message is the *only* thing the resumed reviewer agent sees
    on retry — everything else is in its session memory. The composer must
    carry every field the prompt's 'On delta re-review' section references,
    otherwise the agent has guidance but no inputs (or vice-versa).
    """

    def test_returns_nonempty_string(self, feat_and_unit):
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaa1234",
            current_sha="bbb5678",
            prior_findings="missing edge case",
            fix_summary="added empty-input guard",
        )
        assert isinstance(out, str)
        assert out.strip(), "delta task must be a nonempty message"

    def test_announces_delta_re_review_at_top(self, feat_and_unit):
        """The agent's session-resume hook in reviewer.md is keyed on the
        opening phrase 'DELTA RE-REVIEW' — a future rename here breaks the
        prompt's branching."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary="y",
        )
        assert "DELTA RE-REVIEW" in out
        assert "PR #42" in out

    def test_includes_unit_id_and_pr_number(self, feat_and_unit):
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary="y",
        )
        assert "F-001-U-1" in out
        assert "42" in out

    def test_carries_both_shas_verbatim(self, feat_and_unit):
        """The agent runs ``git diff PRIOR_SHA..CURRENT_SHA`` against these
        exact strings; substring drift (e.g. only first 8 chars) would
        produce an empty diff in some cases."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaaaaaa1234567890",
            current_sha="bbbbbbb9876543210",
            prior_findings="x",
            fix_summary="y",
        )
        assert "aaaaaaa1234567890" in out
        assert "bbbbbbb9876543210" in out

    def test_carries_prior_findings_verbatim(self, feat_and_unit):
        """Reviewer must reconcile against the exact prior summary, not a
        paraphrase. A truncated/stripped version would let the agent
        reconcile against the wrong list."""
        f, u = feat_and_unit
        findings = "F1 race condition on shared map; F2 log includes raw token"
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings=findings,
            fix_summary="y",
        )
        assert findings in out

    def test_carries_fix_summary_verbatim(self, feat_and_unit):
        """The coder's claim about what they fixed must round-trip. The
        anti-capitulation contract in the prompt depends on the reviewer
        seeing the claim *and* checking the code — they can't check the
        claim if it's been re-summarised."""
        f, u = feat_and_unit
        fix_summary = "Wrapped map access in a lock. Will redact tokens in a follow-up."
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary=fix_summary,
        )
        assert fix_summary in out

    def test_skip_inventory_instruction_present(self, feat_and_unit):
        """The latency win comes from skipping the cold-start inventory.
        If this instruction goes missing, the agent will re-do step 1
        of The Method and the resume's wall-clock collapses back to
        cold-spawn levels."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary="y",
        )
        # Either explicit "SKIP" wording or the Method-step-1 reference.
        assert re.search(r"SKIP|skip.*clone|inventory", out, re.IGNORECASE), (
            "delta task must instruct the agent to skip the clone/inventory step"
        )

    def test_diff_range_scoping_present(self, feat_and_unit):
        """``PRIOR_SHA..CURRENT_SHA`` is the literal range the prompt section
        and the composer must agree on. The two-dot range notation is
        meaningful — three-dots would include merge-base divergence which
        the delta path doesn't want."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaa",
            current_sha="bbb",
            prior_findings="x",
            fix_summary="y",
        )
        # Either the literal PRIOR_SHA..CURRENT_SHA placeholder or the
        # interpolated aaa..bbb form is acceptable; the composer interpolates
        # so we expect the latter.
        assert "aaa..bbb" in out or "PRIOR_SHA..CURRENT_SHA" in out, (
            "delta task must scope the diff to PRIOR_SHA..CURRENT_SHA"
        )

    def test_reconciliation_vocabulary_present(self, feat_and_unit):
        """RESOLVED / NOT_RESOLVED / N/A is the controlled vocabulary the
        eval-runner greps for. The fixture's ``diff_signal`` values use
        these exact tokens; if the composer drops them from the agent
        instructions, the agent will improvise its own status names and
        the eval grep will miss them."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary="y",
        )
        assert "RESOLVED" in out
        assert "NOT_RESOLVED" in out
        assert "N/A" in out

    def test_lists_all_four_terminal_markers(self, feat_and_unit):
        """The agent must know every valid terminal marker on a delta turn.
        Dropping BLOCKED would leave the agent without an escape hatch on
        retry; dropping REVIEW_COMMENT would force REQUEST_CHANGES /
        RECOMMEND_MERGE binarisation."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary="y",
        )
        assert "REVIEW_RECOMMEND_MERGE" in out
        assert "REVIEW_REQUEST_CHANGES" in out
        assert "REVIEW_COMMENT" in out
        assert "BLOCKED" in out

    def test_anchoring_warning_present(self, feat_and_unit):
        """The retry path is the *one* turn where prior verdict bias is
        guaranteed to be present. The composer's body — not just the
        system prompt — must remind the agent to watch for it, because
        the system prompt section is a long way back in context after
        a multi-cycle resume."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary="y",
        )
        assert re.search(r"anchor|ANCHOR|Anchor", out), (
            "delta task body must reference the anti-anchoring contract; "
            "system-prompt reference alone is not enough on a long resume"
        )

    def test_falls_back_when_prior_sha_empty(self, feat_and_unit):
        """Empty prior_sha is the common case on the *first* retry — the
        orchestrator captures the SHA after each reviewer turn, but a
        transient gh API failure during capture leaves it empty. The
        composer must emit a sensible fallback the agent can act on,
        not an empty value next to the label."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="",  # capture failed
            current_sha="bbbb",
            prior_findings="x",
            fix_summary="y",
        )
        # The PRIOR_SHA line must NOT be left as an empty value.
        # Match the label followed by something more than whitespace.
        match = re.search(r"PRIOR_SHA:\s*(\S+.*)", out)
        assert match is not None, "PRIOR_SHA line missing entirely"
        assert match.group(1).strip(), (
            f"PRIOR_SHA fallback string is empty/whitespace: {match.group(1)!r}"
        )

    def test_falls_back_when_current_sha_empty(self, feat_and_unit):
        """Same contract for current_sha — the orchestrator's
        get_pr_state failure path inside _resume_reviewer_for_delta
        passes ``""`` rather than crashing."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaaa",
            current_sha="",
            prior_findings="x",
            fix_summary="y",
        )
        match = re.search(r"CURRENT_SHA:\s*(\S+.*)", out)
        assert match is not None, "CURRENT_SHA line missing entirely"
        assert match.group(1).strip(), (
            f"CURRENT_SHA fallback string is empty/whitespace: {match.group(1)!r}"
        )

    def test_falls_back_when_prior_findings_empty(self, feat_and_unit):
        """A retry without recorded prior findings is anomalous (the
        prior REQUEST_CHANGES *should* have stored an issue summary) —
        but recoverable: the agent can re-read inline comments on the PR.
        The composer must include guidance, not a blank label."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaaa",
            current_sha="bbbb",
            prior_findings="",
            fix_summary="y",
        )
        # No "(empty)" header — must surface a usable fallback string.
        match = re.search(r"PRIOR_FINDINGS[^\n]*\n([^\n]+)", out)
        assert match is not None
        first_value_line = match.group(1).strip()
        assert first_value_line, "prior_findings fallback must be non-empty"

    def test_falls_back_when_fix_summary_empty(self, feat_and_unit):
        """address_review's response may not include a structured
        ``summary`` field on the FIX_PUSHED dict (older coder responses,
        regression scenarios). The composer must accept ``""`` without
        leaving a blank line where the agent would expect context."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaaa",
            current_sha="bbbb",
            prior_findings="x",
            fix_summary="",
        )
        match = re.search(r"FIX SUMMARY[^\n]*\n([^\n]+)", out, re.IGNORECASE)
        assert match is not None, "fix-summary section missing"
        assert match.group(1).strip(), "fix_summary fallback must be non-empty"

    def test_renders_feature_spec_block_when_provided(self, feat_and_unit):
        """F-006-U-6 review feedback (M1): the reviewer.md "Read this
        FIRST on retry" rule for ``## FEATURE SPEC`` / ``## THIS UNIT'S
        CYCLE LOG`` must be backed by composer support on the delta-resume
        path — features/F-006/spec.md § Constraints requires fresh
        re-injection on every resume, including the steady-state delta
        retry."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaaa",
            current_sha="bbbb",
            prior_findings="x",
            fix_summary="y",
            feature_spec_text="# F-001\n\n## Acceptance\n- supports refresh",
        )
        assert "## FEATURE SPEC" in out
        assert "supports refresh" in out

    def test_renders_predecessor_units_block_when_provided(self, feat_and_unit):
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaaa",
            current_sha="bbbb",
            prior_findings="x",
            fix_summary="y",
            predecessor_logs=[("F-001-U-0", "Picked validator Y.")],
        )
        assert "## PREDECESSOR UNITS" in out
        assert "F-001-U-0" in out
        assert "validator Y" in out

    def test_renders_own_cycle_log_block_when_provided(self, feat_and_unit):
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="aaaa",
            current_sha="bbbb",
            prior_findings="x",
            fix_summary="y",
            own_cycle_log="# F-001-U-1\n\n### Cycle 1 — reviewer: REVIEW_REQUEST_CHANGES",
        )
        assert "## THIS UNIT'S CYCLE LOG" in out
        assert "Cycle 1" in out

    def test_omits_context_blocks_when_kwargs_empty(self, feat_and_unit):
        """Pre-F-006 call sites pass no context kwargs and must see the
        original message unchanged — no stray block headers."""
        f, u = feat_and_unit
        out = compose_reviewer_delta_task(
            feature=f,
            unit=u,
            pr_number=42,
            prior_sha="a",
            current_sha="b",
            prior_findings="x",
            fix_summary="y",
        )
        assert "## FEATURE SPEC" not in out
        assert "## PREDECESSOR UNITS" not in out
        assert "## THIS UNIT'S CYCLE LOG" not in out


# =============================================================================
# _resume_reviewer_for_delta — behaviours not covered by the coder's suite
# =============================================================================


def _build_ctx(prior_sha="cafe1234"):
    return execution.CycleContext(
        feature_id="F-001",
        unit_id="F-001-U-1",
        history=[],
        last_reviewed_sha=prior_sha,
    )


def _resolve_unit_feature():
    feature = state.get_feature("F-001")
    plan = state.get_plan("F-001")
    unit = next(u for u in plan.units if u.id == "F-001-U-1")
    return feature, unit


class TestResumeReviewerDeltaGhFailure:
    """The capture of ``current_sha`` inside ``_resume_reviewer_for_delta``
    is best-effort; the prompt is explicit that the agent has a fallback
    for unknown SHAs. The helper must *not* propagate gh errors.
    """

    def test_get_pr_state_exception_falls_through(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit_ready_for_reviewer()
        workers = _install_fake_worker(
            monkeypatch,
            resume_responses=["addressed\nREVIEW_RECOMMEND_MERGE: ok"],
        )
        _stub_github(monkeypatch)

        # Override get_pr_state to raise — _resume_reviewer_for_delta must
        # NOT propagate this and must still call worker.resume.
        def boom(*a, **k):
            raise RuntimeError("gh API 503")

        monkeypatch.setattr("orchestrator.tools.execution.github.get_pr_state", boom)

        ctx = _build_ctx()
        feature, unit = _resolve_unit_feature()
        unit_state = state.get_unit_state("F-001-U-1")
        out = execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )

        # Must succeed end-to-end despite the gh failure
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"

        # And resume was actually called (proving we didn't bail before
        # invoking the agent worker)
        reviewer_worker = workers["reviewer"]
        assert len(reviewer_worker.resume_calls) == 1
        _, sent_msg = reviewer_worker.resume_calls[0]
        # The agent message uses the fallback string for current_sha — NOT
        # an exception trace, NOT an empty line.
        # Look at the CURRENT_SHA line.
        m = re.search(r"CURRENT_SHA:\s*(\S+.*)", sent_msg)
        assert m is not None
        # current_sha was "" → fallback string used; "deadbeefcafe" must NOT
        # appear (that would mean the stub leaked through)
        assert "deadbeefcafe" not in sent_msg, (
            "current_sha fallback expected; got a real SHA somehow"
        )
        assert m.group(1).strip(), "CURRENT_SHA fallback must be non-empty"

    def test_does_not_crash_when_pr_number_none(self, tmp_state_db, with_github_token, monkeypatch):
        """Defensive: a reviewer-session-with-no-PR is a state-shape bug,
        but ``_resume_reviewer_for_delta`` must surface it as an ERROR
        return value rather than crashing inside compose_reviewer_delta_task
        or worker.resume."""
        _seed_unit_ready_for_reviewer()
        # Strip pr_number
        s = state.get_unit_state("F-001-U-1")
        s.pr_number = None
        state.upsert_unit_state(s)

        _install_fake_worker(monkeypatch)
        _stub_github(monkeypatch)

        ctx = _build_ctx()
        feature, unit = _resolve_unit_feature()
        unit_state = state.get_unit_state("F-001-U-1")
        out = execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )

        assert "ERROR" in out
        assert "PR" in out or "pr" in out

    def test_resume_call_carries_prior_sha_from_ctx(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """``ctx.last_reviewed_sha`` is the contract between
        ``_capture_reviewed_sha`` and the next retry's delta range. If
        the helper ever stops reading it from ctx (e.g. re-fetches and
        uses head_sha as prior_sha, which is the *current* sha and so
        always produces an empty diff), the latency win evaporates and
        the agent re-reviews the entire PR."""
        _seed_unit_ready_for_reviewer()
        workers = _install_fake_worker(
            monkeypatch,
            resume_responses=["REVIEW_RECOMMEND_MERGE: ok"],
        )
        _stub_github(monkeypatch, head_sha="newhead00000")

        ctx = _build_ctx(prior_sha="priorhead000")
        feature, unit = _resolve_unit_feature()
        unit_state = state.get_unit_state("F-001-U-1")
        execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )

        _, sent_msg = workers["reviewer"].resume_calls[0]
        assert "priorhead000" in sent_msg, "prior_sha from ctx must reach the agent msg"
        assert "newhead00000" in sent_msg, "current_sha from gh must reach the agent msg"
        # And the diff range must use them (not the bare placeholders)
        assert "priorhead000..newhead00000" in sent_msg

    def test_response_with_no_marker_escalates(self, tmp_state_db, with_github_token, monkeypatch):
        """A resumed reviewer that wanders off without emitting a fresh
        terminal marker must escalate — same contract as ``spawn_reviewer``.
        Silence is what the unit description calls "locks the cap-3 loop";
        the no-marker path is the defence against that."""
        _seed_unit_ready_for_reviewer()
        _install_fake_worker(
            monkeypatch,
            resume_responses=["I thought about it but reached no conclusion."],
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *a, **k: True,
        )

        ctx = _build_ctx()
        feature, unit = _resolve_unit_feature()
        unit_state = state.get_unit_state("F-001-U-1")
        out = execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )
        # Pre-helper behaviour: no-marker → ESCALATED return string
        assert "ESCALATED" in out

    def test_does_not_re_spawn_when_session_exists(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The whole point: zero spawn calls on a delta resume.
        A regression that secretly calls ``worker.spawn`` would silently
        cold-start (the FakeWorker assigns a new session id) but every
        other assertion in the coder's suite would still pass because
        the *new* id is what gets returned."""
        _seed_unit_ready_for_reviewer()
        workers = _install_fake_worker(
            monkeypatch,
            spawn_responses=["UNEXPECTED SPAWN\nREVIEW_RECOMMEND_MERGE: ok"],
            resume_responses=["REVIEW_RECOMMEND_MERGE: ok"],
        )
        _stub_github(monkeypatch)

        ctx = _build_ctx()
        feature, unit = _resolve_unit_feature()
        unit_state = state.get_unit_state("F-001-U-1")
        execution._resume_reviewer_for_delta(
            ctx, unit_state, feature, unit, prior_findings="x", fix_summary="y"
        )

        rw = workers["reviewer"]
        assert rw.spawn_calls == [], (
            f"delta resume must never cold-spawn; got {len(rw.spawn_calls)} spawn(s)"
        )
        assert len(rw.resume_calls) == 1


# =============================================================================
# _reviewer_phase — multi-retry session reuse (the regression scenario)
# =============================================================================


class TestReviewerPhaseMultiRetry:
    """The pre-F-012 bug at execution.py:1030 cleared reviewer_session_id
    inside the retry loop, so EVERY retry was a cold spawn. A one-retry
    test still passes against the buggy code because the loop only fires
    the clear *after* the first retry's spawn lands. The canonical
    regression scenario is two retries — and on the buggy code, the
    second reviewer turn would re-cold-spawn (new session id, new sandbox).
    """

    def test_two_retries_share_one_session_id(self, tmp_state_db, with_github_token, monkeypatch):
        # Coder-side: every fix returns FIX_PUSHED
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "address_review",
            lambda u, src, fb: json.dumps({"outcome": "FIX_PUSHED", "cycle": 1, "summary": "fix"}),
        )
        # Reviewer cycle: spawn=REQUEST_CHANGES, resume1=REQUEST_CHANGES,
        # resume2=RECOMMEND_MERGE
        _install_fake_worker(
            monkeypatch,
            spawn_responses=["REVIEW_REQUEST_CHANGES: first round of issues"],
            resume_responses=[
                "REVIEW_REQUEST_CHANGES: more issues",
                "REVIEW_RECOMMEND_MERGE: all clear now",
            ],
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        # Seed feature/plan/unit so cycle_review can find them
        state.save_feature(
            Feature(
                id="F-001",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                branch_prefix="feat/F-001",
                status="approved",
            )
        )
        state.save_plan(
            "F-001",
            [WorkUnit(id="F-001-U-1", feature_id="F-001", title="u1", description="d")],
        )
        state.approve_plan("F-001")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                branch="feat/F-001-u-1",
                pr_number=5,
                coder_session_id="sesn-c",
            )
        )

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        # Verify approved
        assert parsed["outcome"] == "approved_awaiting_merge", out

        # Two retries means two delta-resume steps appear in history
        steps = [s.get("step") for s in parsed["history"]]
        delta_steps = [s for s in steps if s == "reviewer (delta resume)"]
        assert len(delta_steps) == 2, (
            f"expected 2 delta-resume steps for 2-retry cycle; got {len(delta_steps)} "
            f"(steps={steps}). On the pre-F-012 bug the second retry was a cold spawn, "
            f"surfacing as 'reviewer (retry)' or a fresh 'reviewer' step here."
        )

        # The reviewer session id on state must still be the original spawn's
        # session — both retries reused it.
        s = state.get_unit_state("F-001-U-1")
        assert s.reviewer_session_id == "sesn-reviewer-0", (
            f"after two retries the session should still be the initial spawn's "
            f"sesn-reviewer-0; got {s.reviewer_session_id!r}. "
            f"A non-zero index means a retry cold-started a new session."
        )

    def test_reviewer_session_id_never_cleared_between_retries(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Independent of the FakeWorker session-id convention: at no
        point during the cycle should ``unit_state.reviewer_session_id``
        be observed as the empty string.

        We sample it by intercepting ``state.upsert_unit_state`` and
        recording every reviewer_session_id the cycle ever writes.
        If the pre-F-012 ``s.reviewer_session_id = ""`` + upsert line ever
        comes back, the empty-string write will show up in the trace.
        """
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "address_review",
            lambda u, src, fb: json.dumps({"outcome": "FIX_PUSHED", "cycle": 1, "summary": "fix"}),
        )
        _install_fake_worker(
            monkeypatch,
            spawn_responses=["REVIEW_REQUEST_CHANGES: round 1"],
            resume_responses=[
                "REVIEW_REQUEST_CHANGES: round 2",
                "REVIEW_RECOMMEND_MERGE: clean",
            ],
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        state.save_feature(
            Feature(
                id="F-001",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                branch_prefix="feat/F-001",
                status="approved",
            )
        )
        state.save_plan(
            "F-001",
            [WorkUnit(id="F-001-U-1", feature_id="F-001", title="u1", description="d")],
        )
        state.approve_plan("F-001")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-001-U-1",
                feature_id="F-001",
                status="in_ci",
                branch="feat/F-001-u-1",
                pr_number=5,
                coder_session_id="sesn-c",
            )
        )

        seen_session_ids: list[str | None] = []
        real_upsert = state.upsert_unit_state

        def trace_upsert(unit_state: WorkUnitState) -> None:
            if unit_state.unit_id == "F-001-U-1":
                seen_session_ids.append(unit_state.reviewer_session_id)
            real_upsert(unit_state)

        monkeypatch.setattr("orchestrator.state.upsert_unit_state", trace_upsert)
        # Also patch the imported reference inside execution
        monkeypatch.setattr("orchestrator.tools.execution.state.upsert_unit_state", trace_upsert)

        execution.cycle_review("F-001", "F-001-U-1")

        # The session id is set non-empty when spawn_reviewer first runs;
        # from that moment on, no observed write should record the empty
        # string for reviewer_session_id.
        sid_seen_after_first_nonempty = False
        for sid in seen_session_ids:
            if sid:
                sid_seen_after_first_nonempty = True
                continue
            if sid_seen_after_first_nonempty:
                pytest.fail(
                    f"reviewer_session_id was cleared mid-cycle: trace={seen_session_ids!r}"
                )


# =============================================================================
# _capture_reviewed_sha — direct contract tests
# =============================================================================


class TestCaptureReviewedSha:
    """The bridge from reviewer turn → next retry's delta range. Errors here
    are silent (best-effort) but contractually so — the prompt's
    'When PRIOR_SHA is unknown' fallback only works if the empty case is
    reachable, which means the helper must not crash and must not
    overwrite a previous value with junk on failure.
    """

    def test_updates_last_reviewed_sha_from_pr_state(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit_ready_for_reviewer()
        _stub_github(monkeypatch, head_sha="freshhead12345")

        ctx = _build_ctx(prior_sha="oldhead0")
        execution._capture_reviewed_sha(ctx)

        assert ctx.last_reviewed_sha == "freshhead12345"

    def test_preserves_previous_value_on_github_failure(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit_ready_for_reviewer()
        _stub_github(monkeypatch)

        # Override gh to raise
        def boom(*a, **k):
            raise RuntimeError("transient gh 503")

        monkeypatch.setattr("orchestrator.tools.execution.github.get_pr_state", boom)

        ctx = _build_ctx(prior_sha="oldhead0")
        execution._capture_reviewed_sha(ctx)

        # Previous value preserved — the next retry's delta range still has
        # *something* to anchor on (this anchor turn's value, even if stale)
        # rather than crashing or being silently zeroed.
        assert ctx.last_reviewed_sha == "oldhead0"

    def test_no_op_when_no_pr_number(self, tmp_state_db, with_github_token, monkeypatch):
        """If the unit has no PR yet, there's nothing to capture. The
        helper must not raise and must not flip last_reviewed_sha to
        a bogus value (it should remain whatever the caller put there)."""
        _seed_unit_ready_for_reviewer()
        s = state.get_unit_state("F-001-U-1")
        s.pr_number = None
        state.upsert_unit_state(s)
        _stub_github(monkeypatch)

        ctx = _build_ctx(prior_sha="initial")
        execution._capture_reviewed_sha(ctx)
        assert ctx.last_reviewed_sha == "initial"

    def test_no_op_when_unit_state_missing(self, tmp_state_db, with_github_token, monkeypatch):
        """Defensive: don't crash if the unit's state row vanished
        between turns (e.g. external delete during a mid-cycle restart)."""
        # No seeded unit
        state.save_feature(
            Feature(
                id="F-001",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        _stub_github(monkeypatch)

        ctx = _build_ctx(prior_sha="initial")
        execution._capture_reviewed_sha(ctx)  # must not raise
        assert ctx.last_reviewed_sha == "initial"


# =============================================================================
# Delta-scenario fixture: expected verdict must follow the prompt's decision rule
# =============================================================================


class TestDeltaScenarioVerdictConsistency:
    """The coder's ``test_reviewer_prompt.py`` pins axis coverage and valid
    marker values, but does NOT check that ``expected_marker`` follows the
    decision rule the prompt encodes. A scenario flagged with a
    ``new_critical`` but marked ``REVIEW_RECOMMEND_MERGE`` would be
    self-contradictory — and the eval grep would silently agree with the
    fixture, defeating the eval's purpose.

    Decision rule (transcribed from the 'How to work the delta turn'
    table in reviewer.md):

      | Reconciliation result                          | Marker                  |
      |------------------------------------------------|-------------------------|
      | All RESOLVED/N/A AND no new 🔴/🟠              | REVIEW_RECOMMEND_MERGE  |
      | Any NOT_RESOLVED 🔴/🟠 OR any new 🔴/🟠         | REVIEW_REQUEST_CHANGES  |
      | All RESOLVED/N/A AND only new 🟡/🔵            | REVIEW_COMMENT          |
    """

    @pytest.fixture
    def scenarios(self):
        return json.loads(FIXTURE_PATH.read_text())["scenarios"]

    @staticmethod
    def _derive_expected_marker(scenario: dict) -> str:
        """Apply the prompt's decision rule to derive the marker.

        Returns the marker name OR raises if the scenario is internally
        inconsistent (e.g. a finding status that's neither RESOLVED nor
        NOT_RESOLVED nor N/A).
        """
        diff_signal = scenario["diff_signal"]
        prior_findings = scenario["prior_findings"]

        # Per-finding reconciliation
        any_not_resolved = False
        for i in range(len(prior_findings)):
            value = diff_signal[f"F{i + 1}"]
            if value.startswith("NOT_RESOLVED"):
                any_not_resolved = True
            elif value.startswith(("RESOLVED", "N/A")):
                pass
            else:
                raise AssertionError(f"scenario {scenario['id']} F{i + 1} status {value!r} unknown")

        # New findings: keys other than F1..Fn
        prior_keys = {f"F{i + 1}" for i in range(len(prior_findings))}
        extra_keys = set(diff_signal.keys()) - prior_keys
        any_new_blocking = any(k.startswith(("new_critical", "new_high")) for k in extra_keys)
        any_new_nit = any(k.startswith(("new_nit", "new_medium", "new_low")) for k in extra_keys)

        if any_not_resolved or any_new_blocking:
            return "REVIEW_REQUEST_CHANGES"
        if any_new_nit:
            return "REVIEW_COMMENT"
        return "REVIEW_RECOMMEND_MERGE"

    def test_each_expected_marker_follows_decision_rule(self, scenarios):
        for s in scenarios:
            derived = self._derive_expected_marker(s)
            assert derived == s["expected_marker"], (
                f"scenario {s['id']!r} ({s['description']}): "
                f"expected_marker={s['expected_marker']!r} but the decision rule "
                f"applied to its diff_signal yields {derived!r}. "
                f"Either the fixture's reconciliation contradicts the verdict, "
                f"or the decision rule's encoding has drifted."
            )

    def test_new_findings_axis_value_matches_diff_signal(self, scenarios):
        """``new_findings`` declared at the scenario top level must
        actually appear in ``diff_signal`` (and vice-versa). If they
        disagree, the axis coverage assertion the coder pins is
        meaningless because the per-scenario inputs lie about which
        cell they sit in."""
        for s in scenarios:
            prior_keys = {f"F{i + 1}" for i in range(len(s["prior_findings"]))}
            extras = set(s["diff_signal"].keys()) - prior_keys
            has_blocking = any(k.startswith(("new_critical", "new_high")) for k in extras)
            has_nit = any(k.startswith(("new_nit", "new_medium", "new_low")) for k in extras)

            if s["new_findings"] == "none":
                assert not extras, (
                    f"scenario {s['id']} claims new_findings='none' but diff_signal "
                    f"carries delta-introduced keys {extras}"
                )
            elif s["new_findings"] == "blocking":
                assert has_blocking, (
                    f"scenario {s['id']} claims new_findings='blocking' but diff_signal "
                    f"has no new_critical/new_high entry (extras={extras})"
                )
            elif s["new_findings"] == "nit":
                assert has_nit, (
                    f"scenario {s['id']} claims new_findings='nit' but diff_signal "
                    f"has no new_nit/new_medium entry (extras={extras})"
                )


# =============================================================================
# Prompt: the reconciliation-table EXAMPLE demonstrates all three statuses
# =============================================================================


class TestReviewerPromptTableExample:
    """The coder pins that the vocabulary words appear *somewhere* in the
    delta section. That's not the same thing as showing the agent a worked
    example using all three. Without an example row for N/A specifically,
    the agent is likely to skip N/A and binarise on RESOLVED/NOT_RESOLVED —
    which then makes scenario S6 (the only N/A case in the fixtures) less
    likely to land the correct verdict on a real eval run.
    """

    def test_table_example_includes_all_three_statuses(self):
        prompt = REVIEWER_PROMPT.read_text()

        # Bound the search to the delta-review section so we're inspecting
        # the worked-example table, not some other table elsewhere in the
        # prompt that uses the same vocabulary.
        start = prompt.find("## On delta re-review")
        assert start != -1, "On delta re-review section missing"
        rest = prompt[start:]
        end = rest.find("\n## ", 1)
        section = rest[: end if end != -1 else len(rest)]

        # The "reconcile each prior finding" example table sits below a
        # "Prior finding | Status | Evidence" header. Pull the contiguous
        # block of table rows (optionally indented) starting at that
        # header, stopping at the first non-table line.
        header_match = re.search(r"^[ \t]*\|\s*Prior finding\s*\|.*$", section, re.MULTILINE)
        assert header_match is not None, (
            "the 'reconcile each prior finding' example table header is missing — "
            "without a worked example the agent has vocabulary but no template"
        )
        # Collect the header + all following lines that look like table rows
        # ("|...|" possibly indented). Stop at the first blank / prose line.
        tail_lines: list[str] = [header_match.group(0)]
        after_header = section[header_match.end() + 1 :]
        for line in after_header.splitlines():
            if re.match(r"^[ \t]*\|", line):
                tail_lines.append(line)
            else:
                break
        table = "\n".join(tail_lines)

        # All three statuses must appear as a value in at least one row.
        for status in ("RESOLVED", "NOT_RESOLVED", "N/A"):
            assert re.search(rf"\|\s*{re.escape(status)}\s*\|", table), (
                f"reconciliation example table doesn't demonstrate status {status!r}; "
                f"without a row showing it, agents under-use the third status. "
                f"Table: {table!r}"
            )
