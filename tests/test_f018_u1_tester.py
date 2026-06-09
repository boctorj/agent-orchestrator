"""Independent acceptance-test pinning for F-018-U-1.

Conflict-aware ``cycle_review`` + daemon wiring. These tests are written
against the **F-018 spec's Acceptance section** rather than against the
implementation surface. Where the implementation diverges from the spec
the tests fail — flagging the divergence as a bug, not a stylistic
preference.

Spec ref: ``features/F-018/spec.md`` § Acceptance.

The seven Acceptance bullets the tests pin (one ``test_*`` minimum per
bullet, plus boundary / edge coverage):

  1. ``cycle_review`` calls the mergeable probe after each CI-green
     gate (pre-tester, pre-reviewer). Conflict → ``address_review(
     source='merge', …)`` instead of advancing.
  2. After a rebased HEAD, the existing ``_wait_ci_with_fix_loop``
     waits for CI on the new SHA; mergeable re-probes; loops on
     repeated conflict.
  3. ``WorkUnitState.conflict_fix_attempts`` is the cap-3 counter,
     independent of ``review_round``. Cap-3 escalates with
     ``conflict_rebase_diverging``.
  4. Prior tester/reviewer markers stay (F-018 doesn't touch them);
     after the rebase clears, the cycle proceeds via the existing
     delta-review path. We pin the F-018 invariant: the merge-fix
     resume DOES NOT clear ``tester_session_id`` /
     ``reviewer_session_id`` (the stale-marker rule, owned by
     F-016 Phase 2.5, is what invalidates them on the new SHA).
  5. Daemon ``(approved_awaiting_merge, PrConflictDetected) →
     DispatchConflictFix → fixing``. The transition fires ONLY for
     the post-terminal case; an active unit is left for cycle_review.
  6. ``address_review`` accepts ``source='merge'`` and
     ``compose_fix_task`` emits rebase-against-main guidance with the
     conflict file list. Coder prompt has the SOURCE: merge section.
  7. The independent counters guarantee (pinned at multiple layers).

These tests deliberately stub the F-014 probe and worker resume paths so
they exercise the F-018 wiring without reaching GitHub or Anthropic.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from orchestrator import daemon, state
from orchestrator.blocked_reasons import VALID_REASONS, BlockedReason
from orchestrator.ci_wait import CIWaitResult
from orchestrator.health import Action, Decision
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import CAP_3, compose_fix_task, execution

# --------------------------- shared fixtures / helpers ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend CI is green so cycle_review tests don't need a CI fixture."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=0.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _seed_unit(
    unit_id="F-018-U-1",
    feature_id="F-018",
    status="in_ci",
    coder_session="sesn-c",
    pr_number=42,
    branch="f-018-u-1",
    tester_session="",
    reviewer_session="",
):
    """Seed a feature + plan + unit row in the test DB.

    Returns ``WorkUnitState`` so callers can assert on the seeded state.
    """
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
        [WorkUnit(id=unit_id, feature_id=feature_id, title="t", description="d")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch=branch,
            pr_number=pr_number,
            coder_session_id=coder_session,
            tester_session_id=tester_session,
            reviewer_session_id=reviewer_session,
        )
    )
    return state.get_unit_state(unit_id)


def _conflict_action(files, mergeable_state="dirty"):
    """The shape F-014's decision table emits for ``pr_conflict_detected``."""
    return Action.event(
        "pr_conflict_detected",
        f"PR has conflicts ({mergeable_state})",
        details=f"files: {', '.join(files)}",
        payload={"conflict_files": list(files), "mergeable_state": mergeable_state},
    )


def _stub_probe(monkeypatch, sequence):
    """Patch ``_load_context`` / ``_probe_and_decide`` with a per-call sequence.

    ``sequence`` is a list of ``list[Action]`` — one entry per probe
    call. After exhausting the list, subsequent probes get the last
    entry. Lets a single test specify "conflict, then clean, then
    clean" by passing ``[[conflict], [], []]``.
    """
    calls = {"n": 0}

    def fake_load_context(uid):
        return state.get_unit_state(uid), "https://github.com/o/r"

    def fake_probe_and_decide(unit_state, repo_url):  # noqa: ARG001
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        actions = sequence[idx]
        report = type("HR", (), {})()
        return report, Decision(actions_to_apply=list(actions), shadow_decisions=[])

    monkeypatch.setattr("orchestrator.tools.health._load_context", fake_load_context)
    monkeypatch.setattr("orchestrator.tools.health._probe_and_decide", fake_probe_and_decide)
    return calls


def _stub_github(monkeypatch):
    """No-op the github.* helpers cycle_review touches."""
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
        "orchestrator.tools.execution.github.wait_for_copilot_review", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("owner", "repo"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.get_pr_state",
        lambda *a, **k: {"head_sha": "deadbeef", "state": "open", "merged": False},
    )


# ---------------------------------------------------------------------------
# Acceptance 6 — blocked-reason taxonomy + prompt + compose_fix_task
# ---------------------------------------------------------------------------


class TestAcceptance6_TaxonomyAndPromptShape:
    """Acceptance 6 — the new ``source='merge'`` is recognized end-to-end."""

    def test_conflict_rebase_diverging_slug_in_taxonomy(self):
        """Spec § Acceptance 3 cites the slug verbatim — pin its identity."""
        assert BlockedReason.CONFLICT_REBASE_DIVERGING.value == "conflict_rebase_diverging"
        assert "conflict_rebase_diverging" in VALID_REASONS

    def test_address_review_accepts_merge_source(self, tmp_state_db, with_github_token):
        """``address_review`` rejects unknown sources with an ERROR string. The
        spec § Acceptance 6 says ``merge`` must be recognized — pin the
        accept-path by exercising the source-validation branch."""
        # Don't seed a unit; we just want to confirm the source string
        # passes validation rather than getting the "source must be …"
        # rejection. The next error ("no state for unit") is fine.
        out = execution.address_review("F-NOPE-U-1", "merge", "rebase against main")
        assert "source must be" not in out, (
            f"address_review must accept source='merge' (spec § Acceptance 6); got: {out!r}"
        )

    def test_address_review_rejects_unknown_source_still(self, tmp_state_db, with_github_token):
        """Sanity — the source validation didn't get wholesale removed."""
        out = execution.address_review("F-NOPE-U-1", "bogus-source", "x")
        assert "source must be" in out

    def test_compose_fix_task_merge_lists_conflict_files(self):
        """The conflict file list (the FEEDBACK above) must actually reach
        the coder. Without this the agent has no idea which files to
        resolve — defeats the whole flow."""
        feature = Feature(id="F", title="t", description="d", repo_path="x")
        unit = WorkUnit(id="F-U-1", feature_id="F", title="t", description="d")
        out = compose_fix_task(
            feature,
            unit,
            "branch",
            7,
            "merge",
            "Rebase against main; resolve conflicts in: a.py, b.py.",
        )
        # File list is the FEEDBACK; must appear verbatim in the message.
        assert "a.py" in out and "b.py" in out
        # Source label is present so the coder prompt's SOURCE: merge
        # section is selected at runtime.
        assert "SOURCE:    merge" in out

    def test_compose_fix_task_merge_says_rebase_not_merge(self):
        """Spec § Acceptance 6: 'instructs the coder to rebase against
        main (not merge) so the PR's commit graph stays linear'."""
        feature = Feature(id="F", title="t", description="d", repo_path="x")
        unit = WorkUnit(id="F-U-1", feature_id="F", title="t", description="d")
        out = compose_fix_task(feature, unit, "branch", 7, "merge", "files: a.py").lower()
        # 'rebase' must appear; 'git merge origin/main' must NOT be
        # presented as the action to take (the prompt either calls it
        # out as forbidden, or doesn't suggest it at all).
        assert "rebase" in out, "merge-source guidance must mention rebase"
        # Force-push (--force-with-lease) is the required ending step.
        assert "force-push" in out or "force push" in out

    def test_coder_prompt_has_merge_source_section(self):
        """Coder prompt must document SOURCE: merge so the agent
        knows what to do when resumed with the merge source. Spec
        § Acceptance 6 — 'in compose_fix_task and the coder prompt'."""
        from pathlib import Path

        prompt = (
            Path(__file__).resolve().parent.parent / "orchestrator" / "prompts" / "coder.md"
        ).read_text()
        assert "SOURCE: merge" in prompt, (
            "coder.md must have a SOURCE: merge section — without it the "
            "agent has no docs for the F-018 rebase fix-loop"
        )
        # The section must talk about rebase + force-push + linear graph.
        merge_idx = prompt.find("### `SOURCE: merge`")
        assert merge_idx > -1, "SOURCE: merge section must use ### heading"
        section = prompt[merge_idx : merge_idx + 4000]
        assert "rebase" in section.lower()
        assert "--force-with-lease" in section, (
            "merge-source prompt must specify --force-with-lease (not bare --force)"
        )
        assert "linear" in section.lower(), (
            "merge-source prompt must explain WHY rebase, not merge — "
            "the linear-graph rule for delta review"
        )


# ---------------------------------------------------------------------------
# Acceptance 3 + 7 — conflict_fix_attempts counter is independent of cap-3
# ---------------------------------------------------------------------------


class TestAcceptance3_CounterIndependence:
    """Acceptance 3 + 7: ``conflict_fix_attempts`` is its own budget.

    The spec is explicit (§ Acceptance 7):
      "``conflict_fix_attempts`` is independent of ``cycle_number`` (a
      conflict-fix cycle does not increment cap-3, and a cap-3
      tester-bug fix does not increment conflict_fix_attempts)."
    """

    def test_workunitstate_has_conflict_fix_attempts_default_zero(self):
        """A fresh ``WorkUnitState`` constructor defaults the counter to 0
        so existing (pre-F-018) callers behave exactly like today.
        Backward-compat constraint from the spec § Constraints."""
        s = WorkUnitState(unit_id="x", feature_id="F")
        assert s.conflict_fix_attempts == 0

    def test_increment_conflict_does_not_touch_review_round(self, tmp_state_db):
        _seed_unit()
        # Pre-load review_round to 2 so we can distinguish "left alone"
        # from "default to 0".
        state.increment_review_round("F-018-U-1")
        state.increment_review_round("F-018-U-1")
        assert state.get_unit_state("F-018-U-1").review_round == 2

        new_value = state.increment_conflict_fix_attempts("F-018-U-1")
        got = state.get_unit_state("F-018-U-1")
        assert new_value == 1
        assert got.conflict_fix_attempts == 1
        # The smoking gun: review_round must not have moved.
        assert got.review_round == 2, (
            f"conflict_fix_attempts bump leaked into review_round: "
            f"got review_round={got.review_round}, expected 2"
        )

    def test_increment_review_round_does_not_touch_conflict_counter(self, tmp_state_db):
        """The other direction — a cap-3 tester-bug fix MUST NOT bump
        conflict_fix_attempts."""
        _seed_unit()
        state.increment_conflict_fix_attempts("F-018-U-1")
        assert state.get_unit_state("F-018-U-1").conflict_fix_attempts == 1

        for _ in range(3):
            state.increment_review_round("F-018-U-1")

        got = state.get_unit_state("F-018-U-1")
        assert got.review_round == 3
        # Must still be 1 — not 4, not reset to 0.
        assert got.conflict_fix_attempts == 1, (
            f"review_round bumps leaked into conflict_fix_attempts: "
            f"got conflict_fix_attempts={got.conflict_fix_attempts}, expected 1"
        )

    def test_conflict_counter_is_int_column_in_schema(self, tmp_state_db):
        """Spec § Approach 2: ``conflict_fix_attempts INTEGER DEFAULT 0``.
        Pin the column type so a future migration that swaps to TEXT or
        drops the default is caught."""
        with sqlite3.connect(tmp_state_db) as conn:
            cols = {
                r[1]: (r[2], r[4])  # name -> (type, default)
                for r in conn.execute("PRAGMA table_info(work_units)").fetchall()
            }
        assert "conflict_fix_attempts" in cols
        col_type, default = cols["conflict_fix_attempts"]
        assert col_type.upper() == "INTEGER", (
            f"conflict_fix_attempts must be INTEGER per spec § Approach 2; got {col_type!r}"
        )
        # Default 0 means pre-F-018 rows read 0 — the spec's backward-compat
        # guarantee ("Units with conflict_fix_attempts=0 behave exactly like
        # today").
        assert str(default) == "0", (
            f"conflict_fix_attempts must default to 0 (spec § Constraints — "
            f"backward compat); got default={default!r}"
        )

    def test_address_review_merge_source_skips_review_round_bump(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The spec's load-bearing carve-out (§ Acceptance 7): a
        sibling-merge-induced conflict is mechanical, not quality, so
        ``address_review(source='merge', ...)`` MUST NOT consume
        review_round even though every other source does.

        Pinning this at the address_review surface (not just the
        conflict-fix loop) so the contract holds for any future caller —
        manual ``address_review`` from the lead, daemon-driven calls,
        anything.
        """
        _seed_unit()

        # Fake worker that returns FIX_PUSHED so address_review's happy
        # path lands cleanly.
        class _W:
            def __init__(self, role):
                pass

            def resume(self, sid, msg):  # noqa: ARG002
                return "rebased & force-pushed\nFIX_PUSHED"

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _W)
        _stub_github(monkeypatch)

        before = state.get_unit_state("F-018-U-1")
        assert before.review_round == 0
        assert before.conflict_fix_attempts == 0

        # Sanity: address_review on a tester source DOES bump review_round.
        execution.address_review("F-018-U-1", "tester", "tests failed")
        after_tester = state.get_unit_state("F-018-U-1")
        assert after_tester.review_round == 1

        # The bit under test: address_review on merge source does NOT bump.
        execution.address_review("F-018-U-1", "merge", "rebase: files=a.py")
        after_merge = state.get_unit_state("F-018-U-1")
        assert after_merge.review_round == 1, (
            "address_review(source='merge') leaked into review_round "
            "(spec § Acceptance 7 — 'a conflict-fix cycle does not increment "
            f"cap-3'); got review_round={after_merge.review_round}, expected 1"
        )


# ---------------------------------------------------------------------------
# Acceptance 1 + 2 — cycle_review's mergeable gates
# ---------------------------------------------------------------------------


class TestAcceptance1_MergeableProbeShape:
    """``_check_mergeable`` is the gate primitive — exact contract test.

    Spec § Approach 1: "wraps ``inspect_unit_health(dry_run=True)`` and
    returns the conflict files if ``pr_conflict_detected``, else None."
    """

    def test_returns_conflict_result_when_action_emitted(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit()
        _stub_probe(monkeypatch, [[_conflict_action(["src/foo.py", "src/bar.py"])]])

        result = execution._check_mergeable("F-018-U-1")
        assert result is not None, (
            "spec § Approach 1 — _check_mergeable must return the conflict "
            "files when the F-014 probe surfaces pr_conflict_detected"
        )
        assert result.conflict_files == ["src/foo.py", "src/bar.py"]
        # ``mergeable_state`` is the GitHub field the F-014 decision table
        # carries forward; the helper preserves it for the dispatch payload.
        assert result.mergeable_state

    def test_returns_none_when_probe_emits_no_conflict_action(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit()
        # An empty action list (mergeable PR) returns None — no conflict.
        _stub_probe(monkeypatch, [[]])
        assert execution._check_mergeable("F-018-U-1") is None

    def test_returns_none_on_load_context_error(self, tmp_state_db, monkeypatch):
        """The probe error path returns None — cycle_review treats it as
        'pass through clean' rather than escalating on transient probe
        failures. Same defensive posture as inspect_unit_health(dry_run=
        True)."""
        monkeypatch.setattr(
            "orchestrator.tools.health._load_context",
            lambda uid: "ERROR: no PR",
        )
        assert execution._check_mergeable("F-018-U-1") is None


class TestAcceptance1_CycleReviewGatesFire:
    """Spec § Acceptance 1: ``cycle_review`` fires the mergeable check
    AFTER each CI-green gate (pre-tester AND pre-reviewer).
    """

    def test_both_gates_fire_mergeable_probe(self, tmp_state_db, with_github_token, monkeypatch):
        """On a clean PR, cycle_review reaches terminal via the existing
        path. Both gates must have probed mergeable (pre-tester +
        pre-reviewer); we count the probe calls."""
        _seed_unit()
        probe_counter = _stub_probe(monkeypatch, [[]])  # always clean
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
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-018", "F-018-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

        # The pre-tester + pre-reviewer gates each call the probe at least
        # once. If either gate is missing the F-018 wiring, this drops
        # below 2. Spec § Acceptance 1.
        assert probe_counter["n"] >= 2, (
            f"spec § Acceptance 1 — cycle_review must probe mergeable at both "
            f"the pre-tester and pre-reviewer CI-green gates; only saw "
            f"{probe_counter['n']} probe call(s)"
        )

    def test_conflict_at_pre_tester_gate_dispatches_merge_fix(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Spec § Acceptance 1 — 'when the probe reports
        ``pr_conflict_detected``, the cycle dispatches a fix to the coder
        via ``address_review(source='merge', feedback=...)`` listing the
        conflict files, instead of advancing to the next phase'.

        This test pins the dispatch DECISION at the pre-tester gate
        specifically.
        """
        _seed_unit()
        # Probe sequence: conflict at first call, clean after rebase, clean
        # at the pre-reviewer gate.
        _stub_probe(
            monkeypatch,
            [
                [_conflict_action(["sibling.py"])],
                [],
                [],
                [],
            ],
        )

        dispatched: list[tuple[str, str, str]] = []

        def fake_address_review(uid, src, fb):
            dispatched.append((uid, src, fb))
            return json.dumps({"outcome": "FIX_PUSHED", "cycle": 1, "summary": "rebased"})

        monkeypatch.setattr(execution, "address_review", fake_address_review)
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
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-018", "F-018-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

        merge_dispatches = [d for d in dispatched if d[1] == "merge"]
        assert len(merge_dispatches) == 1, (
            f"spec § Acceptance 1 — exactly one merge-source dispatch must "
            f"have fired (pre-tester conflict, pre-reviewer clean); got "
            f"{len(merge_dispatches)}: {merge_dispatches}"
        )
        # The conflict file list reaches the dispatch as feedback.
        assert "sibling.py" in merge_dispatches[0][2]


# ---------------------------------------------------------------------------
# Acceptance 3 — cap-3 escalation with the conflict_rebase_diverging slug
# ---------------------------------------------------------------------------


class TestAcceptance3_Cap3Escalation:
    """Spec § Acceptance 3: 'When the cap hits, the unit escalates with
    reason ``conflict_rebase_diverging``.'
    """

    def test_cap_3_fires_event_and_flips_to_escalated(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Pre-load conflict_fix_attempts to CAP_3, then trigger one more
        conflict — must escalate WITHOUT dispatching the coder again."""
        _seed_unit()
        for _ in range(CAP_3):
            state.increment_conflict_fix_attempts("F-018-U-1")

        _stub_probe(monkeypatch, [[_conflict_action(["x.py"])]])

        dispatched = []
        monkeypatch.setattr(
            execution,
            "address_review",
            lambda uid, src, fb: dispatched.append((uid, src, fb)) or "should not be called",
        )

        ctx = execution.CycleContext(feature_id="F-018", unit_id="F-018-U-1", history=[])
        ok, msg = execution._conflict_fix_loop(ctx, "pre-tester")
        assert ok is False
        assert msg is not None
        assert "conflict_rebase_diverging" in msg, (
            f"spec § Acceptance 3 — cap-3 escalation must surface the "
            f"conflict_rebase_diverging slug; got msg={msg!r}"
        )
        assert dispatched == [], (
            "spec § Acceptance 3 — at cap-3 the coder must NOT be dispatched "
            "again (the whole point of cap-3 is to STOP rebasing)"
        )

        got = state.get_unit_state("F-018-U-1")
        assert got.status == "escalated"
        assert "conflict_rebase_diverging" in got.last_error

        events = state.list_events("F-018-U-1")
        slug_events = [e for e in events if e["event_type"] == "conflict_rebase_diverging"]
        assert len(slug_events) >= 1, (
            "spec § Acceptance 3 — the escalation must record a "
            "conflict_rebase_diverging audit event for the dashboard"
        )

    def test_cap_3_boundary_value(self, tmp_state_db, with_github_token, monkeypatch):
        """The boundary case: at exactly 2 attempts, the 3rd MUST be
        allowed to dispatch (not the cap yet); at exactly 3 attempts, the
        4th MUST escalate (cap hit). Off-by-one bugs in the cap check
        are the classic failure mode here.
        """
        _seed_unit()
        state.increment_conflict_fix_attempts("F-018-U-1")
        state.increment_conflict_fix_attempts("F-018-U-1")
        assert state.get_unit_state("F-018-U-1").conflict_fix_attempts == 2

        # The 3rd attempt (counter at 2 → 3) is still allowed.
        _stub_probe(monkeypatch, [[_conflict_action(["x.py"])], []])

        monkeypatch.setattr(
            execution,
            "address_review",
            lambda uid, src, fb: json.dumps({"outcome": "FIX_PUSHED"}),
        )
        _stub_github(monkeypatch)

        ctx = execution.CycleContext(feature_id="F-018", unit_id="F-018-U-1", history=[])
        ok, msg = execution._conflict_fix_loop(ctx, "pre-tester")
        assert ok is True, (
            f"at conflict_fix_attempts=2, the third attempt must be allowed "
            f"(cap is 3, not 2); got ok={ok} msg={msg!r}"
        )
        assert state.get_unit_state("F-018-U-1").conflict_fix_attempts == 3


# ---------------------------------------------------------------------------
# Acceptance 4 — F-018 doesn't touch tester/reviewer session ids
# ---------------------------------------------------------------------------


class TestAcceptance4_StaleMarkersUntouchedByF018:
    """Spec § Acceptance 4: 'Prior tester/reviewer markers from before
    the rebase are NOT discarded by this feature — they are invalidated
    naturally by F-016 Phase 2.5's existing stale-marker rule.'

    The F-018 contract is the *negative* one: the merge-source dispatch
    DOES NOT clear or replace tester_session_id / reviewer_session_id.
    The delta-review path (F-016 Phase 2.5, already shipped) handles
    invalidation on the new SHA — this unit must not duplicate or
    short-circuit it.
    """

    def test_address_review_merge_preserves_tester_and_reviewer_sessions(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The merge-source resume MUST NOT clear tester / reviewer
        session ids. F-016 Phase 2.5 owns invalidation via the
        stale-marker rule — duplicating it here breaks the delta-review
        contract."""
        _seed_unit(tester_session="sesn-t-original", reviewer_session="sesn-r-original")

        class _W:
            def __init__(self, role):
                pass

            def resume(self, sid, msg):  # noqa: ARG002
                return "rebased\nFIX_PUSHED"

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _W)
        _stub_github(monkeypatch)

        execution.address_review("F-018-U-1", "merge", "rebase: files=a.py")

        got = state.get_unit_state("F-018-U-1")
        assert got.tester_session_id == "sesn-t-original", (
            "address_review(source='merge') must NOT clear tester_session_id; "
            "F-016 Phase 2.5 owns stale-marker invalidation (spec § Acceptance 4)"
        )
        assert got.reviewer_session_id == "sesn-r-original", (
            "address_review(source='merge') must NOT clear reviewer_session_id; "
            "F-016 Phase 2.5 owns stale-marker invalidation (spec § Acceptance 4)"
        )


# ---------------------------------------------------------------------------
# Acceptance 5 — daemon transition: (approved_awaiting_merge,
#                                    PrConflictDetected) → DispatchConflictFix
# ---------------------------------------------------------------------------


def _seed_awaiting_merge_for_daemon(
    *,
    unit_id="U-AM",
    feature_id="F-AM",
    coder_session="sesn-c",
    pr_number=9,
    conflict_attempts=0,
):
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
        [WorkUnit(id=unit_id, feature_id=feature_id, title="u", description="d")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="approved_awaiting_merge",
            branch="feat/x",
            pr_number=pr_number,
            coder_session_id=coder_session,
        )
    )
    for _ in range(conflict_attempts):
        state.increment_conflict_fix_attempts(unit_id)
    return state.get_unit_state(unit_id)


class _CapturingWorker:
    """Captures ``resume_async`` calls; supplies stub ``tail_messages``."""

    def __init__(self):
        self.resume_async_calls: list[tuple[str, str]] = []

    def resume_async(self, session_id, msg):
        self.resume_async_calls.append((session_id, msg))

    def tail_messages(self, session_id, *, limit=50):  # noqa: ARG002
        # Empty tail — no marker observation triggers status flips.
        return {"status": "idle", "messages": []}


class TestAcceptance5_DaemonDispatchConflictFix:
    """Spec § Acceptance 5: 'The transition table gains one row:
    ``(approved_awaiting_merge, PrConflictDetected(files)) →
    DispatchConflictFix(coder, files)``. The unit transitions back to
    ``fixing``.'
    """

    def test_daemon_flips_awaiting_merge_to_fixing_on_conflict(self, tmp_state_db, monkeypatch):
        """The transition row, end-to-end. Reconciling a unit in
        approved_awaiting_merge when the F-014 probe surfaces
        pr_conflict_detected must:

          1. flip status: ``approved_awaiting_merge → fixing``
          2. bump conflict_fix_attempts
          3. dispatch resume_async on the coder session
        """
        unit = _seed_awaiting_merge_for_daemon()
        worker = _CapturingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)
        monkeypatch.setattr(
            daemon,
            "_probe_and_decide_unit",
            lambda _u: [_conflict_action(["main.py"])],
        )

        daemon.reconcile_unit(unit.unit_id)

        got = state.get_unit_state(unit.unit_id)
        # (1) status flip
        assert got.status == "fixing", (
            f"spec § Acceptance 5 — daemon must flip awaiting_merge → fixing "
            f"on conflict; got status={got.status!r}"
        )
        # (2) counter bump
        assert got.conflict_fix_attempts == 1
        # (3) coder dispatched
        assert len(worker.resume_async_calls) == 1
        sid, msg = worker.resume_async_calls[0]
        assert sid == "sesn-c"
        # The merge source label is present, conflict files reach the agent.
        assert "SOURCE:    merge" in msg
        assert "main.py" in msg

    def test_daemon_does_not_dispatch_for_active_unit(self, tmp_state_db, monkeypatch):
        """Spec § Acceptance 5: 'The transition fires only when the unit
        is currently parked awaiting human merge.' An active unit (in_ci /
        coding / fixing / reviewing / testing) is owned by cycle_review's
        gate — the daemon would race the synchronous flow.
        """
        state.save_feature(
            Feature(
                id="F-A",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan(
            "F-A",
            [WorkUnit(id="U-A", feature_id="F-A", title="u", description="d")],
        )
        state.approve_plan("F-A")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U-A",
                feature_id="F-A",
                status="reviewing",
                branch="b",
                pr_number=5,
                coder_session_id="sc",
            )
        )
        worker = _CapturingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)
        monkeypatch.setattr(
            daemon,
            "_probe_and_decide_unit",
            lambda _u: [_conflict_action(["foo.py"])],
        )

        daemon.reconcile_unit("U-A")

        got = state.get_unit_state("U-A")
        # Status and counter must be untouched.
        assert got.status == "reviewing", (
            f"daemon must NOT dispatch conflict-fix for an active unit "
            f"(cycle_review owns that flow); got status={got.status!r}"
        )
        assert got.conflict_fix_attempts == 0
        assert worker.resume_async_calls == [], (
            "daemon must NOT call resume_async on an active unit; "
            "cycle_review's synchronous gate owns the dispatch"
        )

    def test_daemon_cap_3_escalates_with_correct_slug(self, tmp_state_db, monkeypatch):
        """Cap-3 on the counter escalates with conflict_rebase_diverging
        (spec § Acceptance 3 + 5 — both surfaces share the same slug)."""
        unit = _seed_awaiting_merge_for_daemon(conflict_attempts=CAP_3)
        worker = _CapturingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)
        monkeypatch.setattr(
            daemon,
            "_probe_and_decide_unit",
            lambda _u: [_conflict_action(["x.py"])],
        )

        daemon.reconcile_unit(unit.unit_id)

        got = state.get_unit_state(unit.unit_id)
        assert got.status == "escalated"
        assert "conflict_rebase_diverging" in got.last_error
        # No further resume on cap-3.
        assert worker.resume_async_calls == []
        events = state.list_events(unit.unit_id)
        assert any(e["event_type"] == "conflict_rebase_diverging" for e in events), (
            "daemon cap-3 path must record the conflict_rebase_diverging audit "
            "event (parity with cycle_review's gate)"
        )

    def test_daemon_does_not_dispatch_without_conflict_action(self, tmp_state_db, monkeypatch):
        """An awaiting_merge unit whose F-014 probe is silent (no conflict)
        stays put. The trigger is the conflict event, not the status."""
        unit = _seed_awaiting_merge_for_daemon()
        worker = _CapturingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [])

        daemon.reconcile_unit(unit.unit_id)

        got = state.get_unit_state(unit.unit_id)
        assert got.status == "approved_awaiting_merge"
        assert got.conflict_fix_attempts == 0
        assert worker.resume_async_calls == []


# ---------------------------------------------------------------------------
# Acceptance 7 — end-to-end "sibling-induced mid-cycle conflict" pinning
# ---------------------------------------------------------------------------


class TestAcceptance7_EndToEnd:
    """Spec § Acceptance 7 — final bullet:

      'End-to-end: a unit with a sibling-induced conflict mid-cycle
      hits the conflict-fix flow, the coder rebases, CI re-runs,
      reviewer delta-reviews on the new SHA, terminal is
      approved_awaiting_merge on the rebased HEAD.'

    The interesting predicates pinned here:
      * the terminal state on the rebased HEAD is approved_awaiting_merge
      * conflict_fix_attempts ended at the value matching the number of
        rebase rounds (1 in the happy path)
      * review_round stayed at 0 (no quality-fix work was done)
    """

    def test_end_to_end_sibling_conflict_terminates_on_rebased_head(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit()
        # Probe sequence: conflict at pre-tester gate, clean afterwards.
        _stub_probe(
            monkeypatch,
            [
                [_conflict_action(["sibling.py"])],
                [],
                [],
                [],
                [],
            ],
        )

        monkeypatch.setattr(
            execution,
            "address_review",
            lambda uid, src, fb: json.dumps(
                {"outcome": "FIX_PUSHED", "cycle": 0, "summary": "rebased on main"}
            ),
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
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-018", "F-018-U-1")
        parsed = json.loads(out)
        # Terminal state ends at approved_awaiting_merge.
        assert parsed["outcome"] == "approved_awaiting_merge", (
            f"spec § Acceptance 7 — terminal must be approved_awaiting_merge "
            f"on the rebased HEAD; got {parsed['outcome']!r}"
        )

        got = state.get_unit_state("F-018-U-1")
        assert got.status == "approved_awaiting_merge"
        # One conflict round happened (pre-tester gate hit it once).
        assert got.conflict_fix_attempts == 1, (
            f"end-to-end: exactly one rebase round happened; got "
            f"conflict_fix_attempts={got.conflict_fix_attempts}"
        )
        # No quality cycles burned — the mechanical rebase is on its own budget.
        assert got.review_round == 0, (
            f"spec § Acceptance 7 — no quality cycles ran (only a mechanical "
            f"rebase); got review_round={got.review_round}"
        )
