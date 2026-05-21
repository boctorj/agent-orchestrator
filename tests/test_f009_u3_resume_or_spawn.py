"""F-009-U-3: spawn_tester / spawn_reviewer become resume-or-spawn.

Closes audit Gaps C (post-escalation recovery) and D (transient-retry).
Pre-F-009-U-3, ``spawn_tester`` / ``spawn_reviewer`` refused (returned
``ERROR: ... already exists``) when the unit row already had a
``{role}_session_id``. That blocked legitimate recovery flows: the
worker backend still held accumulated context (PR diff, prior findings,
test scaffolding) but no surface could re-engage it. Spawning fresh
would discard the context and re-pay clone+inventory cost.

New contract:
  * **Initial spawn (no session set)** — unchanged. ``worker.spawn`` is
    called with the role-specific initial task.
  * **Resume (session set)** — ``worker.resume`` is called against the
    persisted session id with the role+reason-appropriate stock prompt
    from :func:`build_recovery_prompt`. The marker chain
    (:func:`_record_terminal_marker`) runs identically — status flips
    per marker; events recorded the same way.

The two contracts converge on the same return shape so callers
(``cycle_review``'s ``_record_step``, the MCP tool's JSON output) don't
need to know which branch ran.

Tests in this module:
  * build_recovery_prompt covers every (role, reason) pair and rejects
    unknown keys.
  * spawn_tester / spawn_reviewer: resume-on-prior-session for both the
    escalated and active-status starting states; initial-spawn path is
    unchanged when no session.
  * address_review: reaches an escalated unit without being rejected
    (no guard on status='escalated').
  * cycle_review: a retry after a simulated transient failure recovers
    cleanly on the second invocation.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green so the CI gate doesn't intercept."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


@pytest.fixture(autouse=True)
def _silence_ntfy(monkeypatch):
    """Stub ntfy so missing NTFY_TOPIC doesn't influence behaviour."""
    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True)
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
    )


class _StubWorker:
    """Minimal `ManagedAgentWorker` stand-in capturing every spawn/resume call."""

    def __init__(self, role: str, *, spawn_response: str = "", resume_response: str = ""):
        self.role = role
        self._spawn_response = spawn_response
        self._resume_response = resume_response
        self.spawn_calls: list[tuple[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        sid = f"sesn-{self.role}-{len(self.spawn_calls)}"
        self.spawn_calls.append((task, title))
        return sid, self._spawn_response

    def resume(self, session_id: str, msg: str) -> str:
        self.resume_calls.append((session_id, msg))
        return self._resume_response

    def archive(self, session_id: str) -> None:  # pragma: no cover - unused
        pass


def _install_workers(monkeypatch, *, per_role: dict[str, dict]) -> dict[str, _StubWorker]:
    instances: dict[str, _StubWorker] = {}

    def factory(role: str) -> _StubWorker:
        if role not in instances:
            cfg = per_role.get(role, {})
            instances[role] = _StubWorker(role, **cfg)
        return instances[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return instances


def _stub_github(monkeypatch, copilot_review=None):
    """Patch the github/safe_* helpers `execution.py` touches into no-ops."""
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


def _seed_unit(
    *,
    status: str = "in_ci",
    tester_session_id: str = "",
    reviewer_session_id: str = "",
    last_error: str = "",
    repo: str = "https://github.com/o/r",
) -> None:
    state.save_feature(
        Feature(id="F-001", title="t", description="d", repo_path=repo, status="approved")
    )
    state.save_plan(
        "F-001",
        [WorkUnit(id="F-001-U-1", feature_id="F-001", title="u", description="d")],
    )
    state.approve_plan("F-001")
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-001-U-1",
            feature_id="F-001",
            status=status,
            branch="feat/branch",
            pr_number=5,
            coder_session_id="sesn-coder",
            tester_session_id=tester_session_id,
            reviewer_session_id=reviewer_session_id,
            last_error=last_error,
        )
    )


# =========================================================================
# build_recovery_prompt — pinned per (role, reason) stock messages
# =========================================================================


class TestBuildRecoveryPrompt:
    """Each ``(role, reason)`` pair returns the canonical stock message; any
    unknown pair raises ValueError so prompt drift is loud."""

    @pytest.mark.parametrize("role", ["coder", "tester", "reviewer"])
    @pytest.mark.parametrize("reason", ["post-escalation", "transient-retry", "ci-fix"])
    def test_known_pair_returns_nonempty_marker_aware_string(self, role, reason):
        msg = execution.build_recovery_prompt(role, reason, last_error="x", details="y")
        assert isinstance(msg, str)
        assert msg.strip(), "stock message must be non-empty"
        # Every prompt names the marker vocabulary for its role so the
        # agent knows what to emit on the resume.
        per_role_markers = {
            "coder": ("FIX_PUSHED", "BLOCKED"),
            "tester": ("TESTS_PASS", "BUG_FOUND", "BLOCKED"),
            "reviewer": (
                "REVIEW_RECOMMEND_MERGE",
                "REVIEW_REQUEST_CHANGES",
                "REVIEW_COMMENT",
                "BLOCKED",
            ),
        }
        for marker in per_role_markers[role]:
            assert marker in msg, f"({role}, {reason}) prompt must mention {marker}; got: {msg!r}"

    def test_post_escalation_interpolates_last_error(self):
        msg = execution.build_recovery_prompt(
            "tester", "post-escalation", last_error="git auth expired"
        )
        assert "git auth expired" in msg
        assert "previously escalated" in msg.lower() or "escalated" in msg.lower()

    def test_post_escalation_missing_last_error_uses_unspecified(self):
        msg = execution.build_recovery_prompt("coder", "post-escalation", last_error="")
        # Templates always render — empty input becomes a visible sentinel
        # rather than a confusing literal "{last_error}".
        assert "{last_error}" not in msg
        assert "(unspecified)" in msg

    def test_transient_retry_for_tester_has_F013_load_bearing_phrases(self):
        """The tester transient-retry prompt is the one ``_resume_or_spawn_tester``
        sends; the F-013-U-1 contract pins the load-bearing phrases that make
        it actionable. We re-pin them here so a refactor that drifts the
        wording can't silently regress F-013."""
        msg = execution.build_recovery_prompt("tester", "transient-retry")
        assert "network timeout" in msg.lower()
        assert "re-emit" in msg.lower()
        for marker in ("TESTS_PASS", "BUG_FOUND", "BLOCKED"):
            assert marker in msg
        # "Don't redo work" instruction
        assert "do not redo" in msg.lower() or "not redo" in msg.lower()

    def test_ci_fix_interpolates_details(self):
        msg = execution.build_recovery_prompt(
            "coder", "ci-fix", details="ruff failed on orchestrator/tools/execution.py"
        )
        assert "ruff failed" in msg

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            execution.build_recovery_prompt("wizard", "transient-retry")  # type: ignore[arg-type]

    def test_unknown_reason_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            execution.build_recovery_prompt("tester", "vibes")  # type: ignore[arg-type]


# =========================================================================
# spawn_tester resume-on-prior-session
# =========================================================================


class TestSpawnTesterResumesOnPriorSession:
    """spawn_tester with ``tester_session_id`` set MUST call worker.resume
    against that session id (not worker.spawn). The marker chain still runs
    via _record_terminal_marker; status flips per marker."""

    def test_escalated_unit_resumes_with_post_escalation_prompt(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Gap-C: a unit that was escalated keeps its session_id. The next
        spawn_tester call must resume that session with the post-escalation
        prompt (last_error interpolated) — not refuse, not cold-start."""
        _seed_unit(
            status="escalated",
            tester_session_id="orphaned-tester-sid",
            last_error="cap-3 hit on tester bug 3",
        )
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)

        # The orphaned session was resumed, not replaced
        assert parsed["outcome"] == "TESTS_PASS"
        assert parsed["session_id"] == "orphaned-tester-sid"

        # worker.resume called once with the orphan session id, worker.spawn
        # never called at all (no fresh thread).
        tester = instances["tester"]
        assert tester.spawn_calls == [], (
            "must NOT cold-start a fresh tester session when one already exists"
        )
        assert len(tester.resume_calls) == 1
        sid, msg = tester.resume_calls[0]
        assert sid == "orphaned-tester-sid"

        # Recovery prompt is the post-escalation variant — references the
        # prior last_error and the verdict marker vocabulary.
        assert "cap-3 hit on tester bug 3" in msg
        assert "TESTS_PASS" in msg
        assert "BUG_FOUND" in msg

        # Marker chain (from U-1) ran: status flips to in_ci on TESTS_PASS,
        # and last_error is cleared since the escalation has been recovered.
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci"
        assert s.last_error == ""
        assert s.tester_session_id == "orphaned-tester-sid"

    def test_active_unit_resumes_with_transient_retry_prompt(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Gap-D: a unit mid-cycle (status='in_ci' with a tester session id
        from a prior network-blip-killed call) resumes with the
        transient-retry prompt."""
        _seed_unit(status="in_ci", tester_session_id="orphaned-tester-sid")
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "BUG_FOUND: off-by-one"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "BUG_FOUND"
        assert parsed["bug"] == "off-by-one"

        tester = instances["tester"]
        assert tester.spawn_calls == []
        sid, msg = tester.resume_calls[0]
        assert sid == "orphaned-tester-sid"
        # transient-retry variant
        assert "network timeout" in msg.lower()

    def test_no_session_falls_through_to_spawn(self, tmp_state_db, with_github_token, monkeypatch):
        """The initial-spawn path is unchanged when no prior session — fresh
        worker.spawn with the composed tester task, no worker.resume."""
        _seed_unit(status="in_ci", tester_session_id="")
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"spawn_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "TESTS_PASS"

        tester = instances["tester"]
        assert len(tester.spawn_calls) == 1
        assert tester.resume_calls == []

        s = state.get_unit_state("F-001-U-1")
        assert s.tester_session_id.startswith("sesn-tester-")

    def test_resume_records_tester_resume_audit_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Operators need a breadcrumb that the resume branch ran (vs a
        fresh spawn). ``tester_resume`` is recorded before the resume call."""
        _seed_unit(status="escalated", tester_session_id="orphan-sid", last_error="boom")
        _install_workers(monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}})
        _stub_github(monkeypatch)

        execution.spawn_tester("F-001", "F-001-U-1")

        events = state.list_events("F-001-U-1")
        types = [e["event_type"] for e in events]
        assert "tester_resume" in types
        # The structured marker event lands too (from _record_terminal_marker)
        assert "tests_pass" in types
        # tester_resume precedes the marker event (chronological breadcrumb)
        assert types.index("tester_resume") < types.index("tests_pass")
        resume_evt = next(e for e in events if e["event_type"] == "tester_resume")
        assert resume_evt["session_id"] == "orphan-sid"
        assert "post-escalation" in resume_evt["summary"]


# =========================================================================
# spawn_reviewer resume-on-prior-session
# =========================================================================


class TestSpawnReviewerResumesOnPriorSession:
    """Symmetric to TestSpawnTesterResumesOnPriorSession."""

    def test_escalated_unit_resumes_with_post_escalation_prompt(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(
            status="escalated",
            reviewer_session_id="orphan-reviewer",
            last_error="reviewer cap-3 on spec compliance",
        )
        instances = _install_workers(
            monkeypatch,
            per_role={
                "reviewer": {"resume_response": "endorsed\nREVIEW_RECOMMEND_MERGE: spec satisfied"}
            },
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"
        assert parsed["session_id"] == "orphan-reviewer"

        reviewer = instances["reviewer"]
        assert reviewer.spawn_calls == []
        sid, msg = reviewer.resume_calls[0]
        assert sid == "orphan-reviewer"
        assert "reviewer cap-3 on spec compliance" in msg
        assert "REVIEW_RECOMMEND_MERGE" in msg
        assert "REVIEW_REQUEST_CHANGES" in msg

        s = state.get_unit_state("F-001-U-1")
        # F-009-U-4 retargeted REVIEW_RECOMMEND_MERGE from in_ci to
        # approved_awaiting_merge (the marker is terminal for the cycle —
        # land directly in the awaiting-merge bucket cycle_review's
        # _emit_terminal would otherwise set). The escalated → resume →
        # endorse path goes through the same marker helper.
        assert s.status == "approved_awaiting_merge"
        assert s.last_error == ""

    def test_active_unit_resumes_with_transient_retry_prompt(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="reviewing", reviewer_session_id="orphan-reviewer")
        instances = _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_REQUEST_CHANGES: missing edge case"}},
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_REQUEST_CHANGES"
        assert "missing edge case" in parsed["issue"]

        reviewer = instances["reviewer"]
        assert reviewer.spawn_calls == []
        sid, msg = reviewer.resume_calls[0]
        assert sid == "orphan-reviewer"
        assert "network timeout" in msg.lower()
        # REVIEW_REQUEST_CHANGES is not a terminal flip-to-in_ci marker — status
        # stays 'reviewing' for the caller's address_review cycle.
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "reviewing"

    def test_no_session_falls_through_to_spawn(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_unit(status="in_ci", reviewer_session_id="")
        instances = _install_workers(
            monkeypatch,
            per_role={"reviewer": {"spawn_response": "REVIEW_RECOMMEND_MERGE: clean"}},
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"

        reviewer = instances["reviewer"]
        assert len(reviewer.spawn_calls) == 1
        assert reviewer.resume_calls == []

    def test_resume_records_reviewer_resume_audit_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated", reviewer_session_id="orphan-r", last_error="boom")
        _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_RECOMMEND_MERGE: ok"}},
        )
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-001", "F-001-U-1")

        events = state.list_events("F-001-U-1")
        types = [e["event_type"] for e in events]
        assert "reviewer_resume" in types
        assert "reviewer_recommend_merge" in types
        assert types.index("reviewer_resume") < types.index("reviewer_recommend_merge")
        resume_evt = next(e for e in events if e["event_type"] == "reviewer_resume")
        assert resume_evt["session_id"] == "orphan-r"
        assert "post-escalation" in resume_evt["summary"]


# =========================================================================
# Resume-on-worker-exception path
# =========================================================================


class TestResumeWorkerExceptionPath:
    """When ``worker.resume`` raises during the recovery attempt, the helper
    must surface an ERROR string (not propagate) and re-escalate the unit."""

    def test_tester_resume_raises_surfaces_error(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated", tester_session_id="orphan", last_error="prior")

        class BoomWorker:
            def __init__(self, role: str):
                self.role = role

            def resume(self, sid, msg):
                raise RuntimeError("anthropic 503")

            def spawn(self, *a, **k):  # pragma: no cover
                raise AssertionError("must not spawn fresh during resume")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", BoomWorker)
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")
        assert out.startswith("ERROR resuming tester")
        assert "anthropic 503" in out

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"
        assert "anthropic 503" in (s.last_error or "")

    def test_reviewer_resume_raises_surfaces_error(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated", reviewer_session_id="orphan-r", last_error="prior")

        class BoomWorker:
            def __init__(self, role: str):
                self.role = role

            def resume(self, sid, msg):
                raise RuntimeError("network timeout")

            def spawn(self, *a, **k):  # pragma: no cover
                raise AssertionError("must not spawn fresh during resume")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", BoomWorker)
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")
        assert out.startswith("ERROR resuming reviewer")

        s = state.get_unit_state("F-001-U-1")
        assert s.status == "escalated"


# =========================================================================
# address_review reaches escalated units (no guard rejects)
# =========================================================================


class TestAddressReviewOnEscalatedUnit:
    """Gap-C through the coder surface: address_review must NOT refuse a
    unit just because its status is 'escalated'. The session_id is the
    source of truth; an escalated unit still has its coder_session_id, and
    the human calling address_review is the recovery path."""

    def test_escalated_unit_with_coder_session_resumes(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated", last_error="prior coder cap-3")

        instances = _install_workers(
            monkeypatch, per_role={"coder": {"resume_response": "ok\nFIX_PUSHED\ndone"}}
        )
        _stub_github(monkeypatch)

        out = execution.address_review("F-001-U-1", "human", "try a different approach")
        parsed = json.loads(out)
        assert parsed["outcome"] == "FIX_PUSHED"

        # The coder thread was resumed (against its persisted session id),
        # not respawned.
        coder = instances["coder"]
        assert len(coder.resume_calls) == 1
        sid, _ = coder.resume_calls[0]
        assert sid == "sesn-coder"
        assert coder.spawn_calls == []

        # Status flipped through fixing → in_ci (via _record_terminal_marker
        # on FIX_PUSHED).
        s = state.get_unit_state("F-001-U-1")
        assert s.status == "in_ci"


# =========================================================================
# cycle_review retry after transient failure
# =========================================================================


class TestCycleReviewTransientRetry:
    """Re-running ``cycle_review`` after a transient failure that left
    ``tester_session_id`` populated must converge on the happy terminal
    instead of escalating — the F-013-U-1 path, plus the F-009-U-3
    spawn_tester native resume, both feed the same recovery."""

    def test_cycle_review_recovers_on_second_call(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        # Simulate first cycle_review died mid-tester: session id persisted,
        # no terminal marker recorded yet, status back to in_ci from GATE 1.
        _seed_unit(status="in_ci", tester_session_id="orphan-tester-sid")

        # On retry: tester resume returns TESTS_PASS; reviewer spawns fresh
        # and recommends merge.
        _install_workers(
            monkeypatch,
            per_role={
                "tester": {"resume_response": "TESTS_PASS"},
                "reviewer": {"spawn_response": "REVIEW_RECOMMEND_MERGE: clean"},
            },
        )
        _stub_github(monkeypatch)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        # Should reach happy terminal, NOT escalate with a stale RAW outcome
        assert parsed["outcome"] == "approved_awaiting_merge", (
            f"second cycle_review must recover from orphaned tester session; got {parsed!r}"
        )
        assert "unexpected outcome: RAW" not in parsed.get("message", "")


# =========================================================================
# `done` status guard — refuses resume on merged units
# =========================================================================


class TestResumeRefusesOnDone:
    """Reviewer finding H1 / Copilot #1: the resume path dropped the
    duplicate-spawn-guard wall that incidentally protected merged units
    from being silently re-opened. The contract restores the protection
    on the ``done`` status while preserving recovery for escalated/active.

    A merged unit with a still-populated ``{role}_session_id`` (orphaned
    from the pre-merge cycle) must refuse resume on all four surfaces:
    ``spawn_tester``, ``spawn_reviewer``, ``address_review``, and the
    interior ``_resume_or_spawn_tester`` helper that ``cycle_review`` uses.
    """

    def test_spawn_tester_refuses_resume_on_done(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="done", tester_session_id="orphan-from-before-merge")
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-001", "F-001-U-1")

        assert "ERROR" in out, f"must refuse on done; got {out!r}"
        assert "already done" in out
        assert "reconcile_unit_pr" in out, "error must point at the right next step"

        # No worker constructed at all — refusal happens before any factory
        # call, so the role entry never materialises in `instances`.
        assert "tester" not in instances, (
            "ManagedAgentWorker must not even be instantiated on a done unit"
        )

        # Status is unchanged — the unit stays done.
        assert state.get_unit_state("F-001-U-1").status == "done"

    def test_spawn_reviewer_refuses_resume_on_done(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="done", reviewer_session_id="orphan-reviewer")
        instances = _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_RECOMMEND_MERGE: clean"}},
        )
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-001", "F-001-U-1")

        assert "ERROR" in out, f"must refuse on done; got {out!r}"
        assert "already done" in out
        assert "reconcile_unit_pr" in out
        assert "reviewer" not in instances
        assert state.get_unit_state("F-001-U-1").status == "done"

    def test_address_review_refuses_on_done(self, tmp_state_db, with_github_token, monkeypatch):
        """Reviewer finding M2: ``address_review`` previously had no guard
        against ``status='done'`` either — calling it on a merged unit
        would flip status to ``fixing``. The docstring's "non-terminal"
        promise now matches behaviour."""
        _seed_unit(status="done")
        instances = _install_workers(
            monkeypatch, per_role={"coder": {"resume_response": "FIX_PUSHED"}}
        )
        _stub_github(monkeypatch)

        out = execution.address_review("F-001-U-1", "human", "try again")

        assert "ERROR" in out, f"must refuse on done; got {out!r}"
        assert "already done" in out
        assert "reconcile_unit_pr" in out
        assert "coder" not in instances
        assert state.get_unit_state("F-001-U-1").status == "done"

    def test_resume_or_spawn_tester_refuses_on_done(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The interior helper that cycle_review's _tester_phase calls must
        also refuse — otherwise a stray cycle_review on a merged unit would
        re-open it via the F-013-U-1 path even though the F-009-U-3 surface
        protects it."""
        _seed_unit(status="done", tester_session_id="orphan-tester")
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        out = execution._resume_or_spawn_tester("F-001", "F-001-U-1")

        assert "ERROR" in out
        assert "already done" in out
        assert "tester" not in instances
        assert state.get_unit_state("F-001-U-1").status == "done"


# =========================================================================
# `_resume_or_spawn_tester` — derive reason + clear last_error (M1)
# =========================================================================


class TestResumeOrSpawnTesterPostEscalationContract:
    """Reviewer finding M1 / Copilot #2: ``_resume_or_spawn_tester`` used to
    hardcode the transient-retry recovery prompt and never cleared
    ``last_error``. On an escalated unit with an orphaned tester session,
    that meant: (a) the worker got a "network timeout" prompt for a hard
    failure, and (b) a successful TESTS_PASS recovery left the stale
    ``last_error`` populated on the dashboard.

    The fix derives the reason from ``unit_state.status`` (matching
    ``_resume_role_for_recovery``) and clears ``last_error`` on entry.
    """

    def test_escalated_unit_gets_post_escalation_prompt_with_last_error(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(
            status="escalated",
            tester_session_id="orphan-tester",
            last_error="cap-3 hit on tester bug",
        )
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        execution._resume_or_spawn_tester("F-001", "F-001-U-1")

        tw = instances["tester"]
        assert len(tw.resume_calls) == 1
        _, msg = tw.resume_calls[0]
        # The post-escalation template references "escalated" and interpolates
        # the unit's last_error. The transient-retry template says "lost to a
        # network timeout" — must NOT appear here.
        assert "escalated" in msg.lower(), (
            f"post-escalation prompt must reference the prior escalation; got msg={msg!r}"
        )
        assert "cap-3 hit on tester bug" in msg, "last_error must be interpolated"
        assert "network timeout" not in msg.lower(), (
            "transient-retry prompt sent on an escalated unit — wrong reason derived"
        )

    def test_in_ci_unit_still_gets_transient_retry_prompt(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Sanity: the in_ci path (Gap-D — F-013-U-1's original scope) still
        gets the transient-retry script. The "network timeout" phrasing is
        load-bearing for the F-013 contract assertion."""
        _seed_unit(status="in_ci", tester_session_id="orphan-tester")
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        execution._resume_or_spawn_tester("F-001", "F-001-U-1")

        tw = instances["tester"]
        _, msg = tw.resume_calls[0]
        assert "network timeout" in msg.lower(), (
            f"in_ci → transient-retry; the F-013 contract requires this phrase; got msg={msg!r}"
        )

    def test_successful_recovery_clears_stale_last_error(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """After a TESTS_PASS recovery on an escalated unit, ``last_error``
        must be empty — otherwise the dashboard shows a stale escalation
        reason for a now-passing unit."""
        _seed_unit(
            status="escalated",
            tester_session_id="orphan-tester",
            last_error="cap-3 hit on tester bug",
        )
        _install_workers(monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}})
        _stub_github(monkeypatch)

        execution._resume_or_spawn_tester("F-001", "F-001-U-1")

        s = state.get_unit_state("F-001-U-1")
        assert s.last_error == "", (
            f"last_error must be cleared on successful recovery; got {s.last_error!r}"
        )
