"""Extended F-013-U-2 coverage for ``_resume_or_spawn_reviewer``.

Symmetric to ``tests/test_f013_u1_resume_or_spawn_tester.py``: supplements
the spec-shaped tests in
``tests/test_tools_execution.py::TestResumeOrSpawnReviewer`` /
``TestCycleReviewRecoversOrphanedReviewerSession`` with a per-behaviour
matrix (one test class per observable property). The split exists because
the spec-shaped trio is short by design — the matrix here covers edge
cases the spec doesn't mention (recovery-prompt content, retry-site
wire-up, audit-event chronology, JSON round-trip through ``_record_step``).

The unit adds a ``_resume_or_spawn_reviewer(feature_id, unit_id)`` helper
to ``orchestrator/tools/execution.py`` and rewires the **initial** call
site inside ``_reviewer_phase`` from ``spawn_reviewer(...)`` to
``_resume_or_spawn_reviewer(...)``. Behaviour:

  * If ``unit_state.reviewer_session_id`` is empty → delegate to
    ``spawn_reviewer(feature_id, unit_id)`` and return its raw response.
  * If set → call ``_resume_role_session("reviewer", sid, recovery_prompt)``
    with a prompt instructing the reviewer to re-emit its verdict marker,
    scan via ``_record_terminal_marker(role="reviewer", ...)`` and return
    JSON in the same shape ``_record_step`` consumes from a fresh
    ``spawn_reviewer`` output (REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES /
    REVIEW_COMMENT / BLOCKED).

Wire change: ``_reviewer_phase`` initial call uses the helper.
The interior **retry** site (post-REVIEW_REQUEST_CHANGES fix-loop) uses
``_resume_reviewer_for_delta`` (F-012-U-2), which keeps the existing
reviewer session for a delta re-review — NOT a bare ``spawn_reviewer``.
That retry-site wire-up is asserted here too.

Regression bug this unit fixes: ``cycle_review`` re-entering a unit with
a non-empty ``reviewer_session_id`` (network-timeout orphan) previously
escalated as ``outcome="escalated"`` / message ``"reviewer ended with
unexpected outcome: RAW"`` because the first ``spawn_reviewer`` errored
with "reviewer session already exists" and ``_record_step`` parsed the
error string as a RAW outcome.
"""

from __future__ import annotations

import inspect
import json

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared fixtures / helpers ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green so the gate doesn't get in the way."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


@pytest.fixture(autouse=True)
def _silence_ntfy(monkeypatch):
    """Stub ntfy pushes so a missing NTFY_TOPIC env doesn't matter."""
    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True)
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
    )


class _StubWorker:
    """Minimal ManagedAgentWorker stand-in for spawn + resume.

    A factory wraps this so every role gets its own instance; tests pull
    the instance back out of ``instances[role]`` to inspect calls.
    """

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
    """Wire `ManagedAgentWorker` so each role yields a unique `_StubWorker`.

    `per_role[role]` is a dict of kwargs forwarded to the stub
    (``spawn_response`` / ``resume_response``). Roles not listed get an
    empty-responder.
    """
    instances: dict[str, _StubWorker] = {}

    def factory(role: str) -> _StubWorker:
        if role not in instances:
            cfg = per_role.get(role, {})
            instances[role] = _StubWorker(role, **cfg)
        return instances[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return instances


def _stub_github(monkeypatch, copilot_review=None):
    """Patch every github / safe_* helper execution.py touches into a no-op."""
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
        lambda *a, **k: {"head_sha": "deadbeefcafe", "state": "open", "merged": False},
    )


def _seed_feature_and_unit(
    *,
    feature_id: str = "F-013",
    unit_id: str = "F-013-U-2",
    reviewer_session_id: str = "",
    tester_session_id: str = "",
    review_round: int = 0,
    repo: str = "https://github.com/o/r",
) -> None:
    """Save a feature with one approved unit and a coded unit_state."""
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=repo,
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=unit_id,
                feature_id=feature_id,
                title="u",
                description="impl this",
            )
        ],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="in_ci",
            branch="feat/branch",
            pr_number=7,
            coder_session_id="sesn-coder",
            tester_session_id=tester_session_id,
            reviewer_session_id=reviewer_session_id,
            review_round=review_round,
        )
    )


# =========================================================================
# Behaviour 1 — helper delegates to spawn_reviewer when no orphan session
# =========================================================================


class TestDelegatesWhenNoSession:
    """With ``reviewer_session_id`` empty the helper must call ``spawn_reviewer``
    and return that response verbatim — no resume call, no extra parsing."""

    def test_empty_session_calls_spawn_reviewer(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(reviewer_session_id="")

        captured_args: list[tuple[str, str]] = []
        sentinel = json.dumps({"unit_id": "F-013-U-2", "outcome": "REVIEW_RECOMMEND_MERGE"})

        def fake_spawn(fid: str, uid: str) -> str:
            captured_args.append((fid, uid))
            return sentinel

        monkeypatch.setattr(execution, "spawn_reviewer", fake_spawn)

        # ManagedAgentWorker should NOT be instantiated — the helper has
        # nothing to resume so it should hand straight to spawn_reviewer.
        def boom_worker(*a, **k):
            raise AssertionError(
                "ManagedAgentWorker constructed in delegate path — helper must "
                "not attempt a resume when reviewer_session_id is empty"
            )

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", boom_worker)

        out = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")

        assert captured_args == [("F-013", "F-013-U-2")]
        # Returns whatever spawn_reviewer returned, verbatim
        assert out == sentinel

    def test_no_unit_state_falls_through_to_spawn_reviewer(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """An unspawned unit (no row in work_units) shouldn't break the helper —
        it should just defer to spawn_reviewer (which will surface its own
        precondition error). Mirrors the implementation's ``unit_state is None``
        early-return."""
        # Feature + plan but NO unit_state row at all
        state.save_feature(
            Feature(
                id="F-013",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan(
            "F-013",
            [WorkUnit(id="F-013-U-2", feature_id="F-013", title="u", description="d")],
        )
        state.approve_plan("F-013")

        calls: list[tuple[str, str]] = []

        def fake_spawn(fid: str, uid: str) -> str:
            calls.append((fid, uid))
            return "DELEGATED"

        monkeypatch.setattr(execution, "spawn_reviewer", fake_spawn)

        out = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")
        assert calls == [("F-013", "F-013-U-2")]
        assert out == "DELEGATED"


# =========================================================================
# Behaviour 2 — orphan session: resume with the spec's recovery prompt
# =========================================================================


class TestRecoveryPromptContract:
    """The resume prompt must match the unit description: explain it was a
    network timeout, that the PR is open + CI green, instruct re-emit of
    REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES / REVIEW_COMMENT /
    BLOCKED on its own line. Asserting the exact wording would be brittle;
    instead we assert the load-bearing phrases that make the prompt
    actionable for the resumed agent."""

    def test_resume_prompt_carries_required_signals(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(reviewer_session_id="stale-reviewer-sid")
        instances = _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_RECOMMEND_MERGE: clean diff"}},
        )
        _stub_github(monkeypatch)

        # Helper must resume — never call spawn_reviewer in this branch
        def boom(*a, **k):
            raise AssertionError("must resume — not spawn — when session_id is set")

        monkeypatch.setattr(execution, "spawn_reviewer", boom)

        execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")

        worker = instances["reviewer"]
        assert len(worker.resume_calls) == 1
        sid, msg = worker.resume_calls[0]
        assert sid == "stale-reviewer-sid", "must resume the orphaned session, not a new one"

        # Description-mandated content beats verbatim string comparison.
        # The agent needs to know (a) why it's being re-prompted, (b) what
        # marker vocabulary to use, (c) not to redo work.
        assert "network timeout" in msg.lower()
        for marker_name in (
            "REVIEW_RECOMMEND_MERGE",
            "REVIEW_REQUEST_CHANGES",
            "REVIEW_COMMENT",
            "BLOCKED",
        ):
            assert marker_name in msg, f"resume prompt must list {marker_name}"
        # The "don't redo work" instruction
        assert "do not redo" in msg.lower() or "not redo" in msg.lower()


# =========================================================================
# Behaviour 3 — JSON shape parity with fresh spawn_reviewer
# =========================================================================


class TestResumeReturnsShapeRecordStepConsumes:
    """``_record_step`` calls ``json.loads`` and pulls fields out of the
    parsed dict (``outcome``, ``reason``, ...). The helper's resume-path
    return must therefore be valid JSON with the same field names as
    ``spawn_reviewer`` returns. Cross-check by feeding the helper's output
    back through the same parser ``_reviewer_phase`` uses.
    """

    def test_resume_recommend_merge_round_trips_through_record_step(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(reviewer_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={
                "reviewer": {
                    "resume_response": "verdict\nREVIEW_RECOMMEND_MERGE: tests cover everything\n"
                }
            },
        )
        _stub_github(monkeypatch)

        raw = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")
        parsed = json.loads(raw)

        # Shape matches spawn_reviewer's REVIEW_RECOMMEND_MERGE branch
        assert parsed["unit_id"] == "F-013-U-2"
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"
        assert parsed["session_id"] == "stale-sid"
        assert "tests cover everything" in parsed["reason"]
        assert "summary" in parsed

        # And ``_record_step`` (the function ``_reviewer_phase`` wraps the
        # helper's output with) consumes it cleanly — no RAW fallback.
        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-2", history=[])
        out = execution._record_step(ctx, "reviewer", raw)
        assert out.get("outcome") == "REVIEW_RECOMMEND_MERGE"
        assert out.get("outcome") != "RAW"
        # `_record_step` should have appended one entry
        assert ctx.history[-1]["step"] == "reviewer"
        assert ctx.history[-1]["result"]["outcome"] == "REVIEW_RECOMMEND_MERGE"

    def test_resume_request_changes_round_trips_through_record_step(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(reviewer_session_id="stale-sid")
        issue_text = "missing null-guard on the new branch"
        _install_workers(
            monkeypatch,
            per_role={
                "reviewer": {
                    "resume_response": f"on inspection:\nREVIEW_REQUEST_CHANGES: {issue_text}\nfix"
                }
            },
        )
        _stub_github(monkeypatch)

        raw = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")
        parsed = json.loads(raw)
        assert parsed["outcome"] == "REVIEW_REQUEST_CHANGES"
        assert parsed["issue"] == issue_text
        assert parsed["session_id"] == "stale-sid"
        assert parsed["unit_id"] == "F-013-U-2"

        # `_record_step` must surface the issue text untouched so
        # `_reviewer_phase` can forward it to `address_review`
        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-2", history=[])
        out = execution._record_step(ctx, "reviewer", raw)
        assert out["outcome"] == "REVIEW_REQUEST_CHANGES"
        assert out.get("issue") == issue_text

    def test_resume_review_comment_round_trips_through_record_step(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The REVIEW_COMMENT marker — a passive review with no merge
        recommendation, no requested changes — must also round-trip as
        a parsed outcome. ``_reviewer_phase`` treats it as a terminal
        success branch alongside REVIEW_RECOMMEND_MERGE, so falling
        back to RAW here would also bork the cycle."""
        _seed_feature_and_unit(reviewer_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "looked it over\nREVIEW_COMMENT\n"}},
        )
        _stub_github(monkeypatch)

        raw = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")
        parsed = json.loads(raw)
        assert parsed["outcome"] == "REVIEW_COMMENT"
        assert parsed["session_id"] == "stale-sid"

        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-2", history=[])
        out = execution._record_step(ctx, "reviewer", raw)
        assert out["outcome"] == "REVIEW_COMMENT"
        assert out["outcome"] != "RAW"

    def test_resume_blocked_propagates_marker(self, tmp_state_db, with_github_token, monkeypatch):
        """BLOCKED on resume must round-trip through ``_record_step`` as a
        parsed JSON outcome of ``"BLOCKED"`` — NOT a RAW fallback whose
        outcome field is the literal string ``"RAW"``.

        ``_reviewer_phase``'s ``outcome.startswith("BLOCKED")`` short-circuit
        checks the parsed ``outcome`` field, not the raw blob, so a RAW
        outcome would fall through to the "unexpected outcome: RAW" branch —
        the exact bug this PR exists to fix. The bare-string BLOCKED return
        that ``spawn_reviewer`` / ``_resume_reviewer_for_delta`` still emit
        is deliberately NOT what the helper does.
        """
        _seed_feature_and_unit(reviewer_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={
                "reviewer": {
                    "resume_response": (
                        "tried again\nBLOCKED: reason=auth_failure | gh token expired"
                    )
                }
            },
        )
        _stub_github(monkeypatch)

        raw = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")

        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-2", history=[])
        out = execution._record_step(ctx, "reviewer", raw)
        outcome = out.get("outcome")
        # Strict assertion (no RAW fallback): the resume helper must return
        # parseable JSON so _reviewer_phase's startswith("BLOCKED") fires.
        assert outcome != "RAW", (
            "resume BLOCKED must not fall through _record_step's RAW fallback "
            "— that re-creates the 'unexpected outcome: RAW' bug for reviewer too"
        )
        assert isinstance(outcome, str) and outcome.startswith("BLOCKED"), (
            f"resume-path BLOCKED outcome must be the literal string 'BLOCKED'; got: {outcome!r}"
        )

        # And the unit status is now escalated, per _record_terminal_marker
        s = state.get_unit_state("F-013-U-2")
        assert s.status == "escalated"


# =========================================================================
# Behaviour 4 — audit trail
# =========================================================================


class TestRecordsMarkerEvent:
    """Per the description: scan response via ``_record_terminal_marker``.
    That helper writes a ``reviewer_recommend_merge`` /
    ``reviewer_request_changes`` / ``reviewer_comment`` / ``reviewer_blocked``
    event row to the audit log — those events must be present after a
    resume."""

    def test_recommend_merge_writes_audit_event(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(reviewer_session_id="stale-sid", review_round=2)
        _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_RECOMMEND_MERGE: clean"}},
        )
        _stub_github(monkeypatch)

        execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")

        events = state.list_events("F-013-U-2")
        types = [e["event_type"] for e in events]
        assert "reviewer_recommend_merge" in types, (
            f"resume must record reviewer_recommend_merge event; got {types}"
        )

        # The event is anchored to the orphaned session (NOT a fresh one)
        # and inherits the unit's current review_round.
        rm = next(e for e in events if e["event_type"] == "reviewer_recommend_merge")
        assert rm["session_id"] == "stale-sid"
        assert rm["cycle_number"] == 2

    def test_request_changes_writes_audit_event(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(reviewer_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "REVIEW_REQUEST_CHANGES: missing tests"}},
        )
        _stub_github(monkeypatch)

        execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")

        types = [e["event_type"] for e in state.list_events("F-013-U-2")]
        assert "reviewer_request_changes" in types


# =========================================================================
# Behaviour 5 — no marker on resume → escalation (don't silently swallow)
# =========================================================================


class TestNoMarkerOnResumeEscalates:
    """If the resumed reviewer comes back with prose lacking any marker we
    can recognise, the helper should escalate rather than return an
    ambiguous OK — matching the pre-existing ``_escalate_no_marker``
    behaviour the description references when it says "scan via
    ``_record_terminal_marker``".
    """

    def test_no_marker_response_returns_escalation(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(reviewer_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={"reviewer": {"resume_response": "uhh I have nothing to say"}},
        )
        _stub_github(monkeypatch)

        out = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")
        assert "ESCALATED" in out
        assert state.get_unit_state("F-013-U-2").status == "escalated"

        # And an audit event was recorded
        types = [e["event_type"] for e in state.list_events("F-013-U-2")]
        assert "reviewer_no_marker" in types


# =========================================================================
# Behaviour 6 — resume worker exception is surfaced as ERROR (not raised)
# =========================================================================


class TestResumeExceptionPath:
    """If ``ManagedAgentWorker.resume`` raises (e.g. network blip on the
    recovery prompt itself), the helper must not propagate the exception
    out of cycle_review — it should mark the unit escalated and return
    an ERROR string."""

    def test_resume_exception_surfaces_as_error(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(reviewer_session_id="stale-sid")

        class BoomWorker:
            def __init__(self, role: str):
                self.role = role

            def resume(self, sid: str, msg: str) -> str:
                raise RuntimeError("anthropic 503")

            def spawn(self, *a, **k):  # pragma: no cover
                raise AssertionError("should not spawn")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", BoomWorker)

        out = execution._resume_or_spawn_reviewer("F-013", "F-013-U-2")
        assert out.startswith("ERROR")
        assert "anthropic 503" in out
        s = state.get_unit_state("F-013-U-2")
        assert s.status == "escalated"
        assert "anthropic 503" in (s.last_error or "")


# =========================================================================
# Behaviour 7 — wire change: _reviewer_phase uses the helper at the initial
# call site, the retry site remains _resume_reviewer_for_delta (NOT
# spawn_reviewer)
# =========================================================================


class TestReviewerPhaseWireUp:
    """The unit description's wire change must hold: initial call uses
    ``_resume_or_spawn_reviewer``; retry call after a REVIEW_REQUEST_CHANGES →
    FIX_PUSHED loop uses ``_resume_reviewer_for_delta`` (F-012-U-2) — NOT a
    bare ``spawn_reviewer``. The U-2 description explicitly says: "Leave
    the retry site at line 1217 untouched.".
    """

    def test_initial_call_uses_resume_helper(self, tmp_state_db, with_github_token, monkeypatch):
        """Source-level assertion: scanning the body of `_reviewer_phase` reveals
        ``_resume_or_spawn_reviewer`` is called and the *first* spawn-or-resume
        reference is the helper. Belt-and-braces on the behaviour test below.
        """
        src = inspect.getsource(execution._reviewer_phase)
        assert "_resume_or_spawn_reviewer" in src, (
            "_reviewer_phase must invoke _resume_or_spawn_reviewer"
        )

        # The initial call to a reviewer-spawning function should be the
        # helper. Find the first occurrences.
        first_helper = src.find("_resume_or_spawn_reviewer")
        assert first_helper >= 0

        # `spawn_reviewer(` appears as a substring of `_resume_or_spawn_reviewer(`,
        # so we need to skip those when looking for a "bare" call. The retry
        # path uses `_resume_reviewer_for_delta` instead — there should be
        # NO bare `spawn_reviewer(` at all inside `_reviewer_phase`.
        idx = 0
        bare_spawns: list[int] = []
        while True:
            idx = src.find("spawn_reviewer(", idx)
            if idx == -1:
                break
            prefix = src[max(0, idx - 20) : idx]
            if "_resume_or_" not in prefix:
                bare_spawns.append(idx)
            idx += 1

        assert bare_spawns == [], (
            f"_reviewer_phase must not call spawn_reviewer directly — the "
            f"initial site routes via _resume_or_spawn_reviewer and the "
            f"retry via _resume_reviewer_for_delta; found bare spawn_reviewer "
            f"call at offset(s) {bare_spawns}"
        )

    def test_retry_path_uses_resume_reviewer_for_delta(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The retry path: reviewer requests changes → coder pushes fix → CI
        green → reviewer is RESUMED for a delta re-review (F-012-U-2),
        NOT cold-spawned. Asserting via call sequence:
          1. _resume_or_spawn_reviewer called once at the top of the phase
             (here: no session yet, so it delegates to spawn_reviewer #1)
          2. address_review spawns coder fix (FIX_PUSHED)
          3. _resume_reviewer_for_delta resumes the reviewer session — and
             yields REVIEW_RECOMMEND_MERGE on the second turn.

        The cleanest behavioural assertion: after `_reviewer_phase` runs to
        completion on changes→fix→merge, ManagedAgentWorker("reviewer").spawn
        was called ONCE (the initial cold start) and .resume was called
        ONCE (the delta re-review). NO second spawn.
        """
        _seed_feature_and_unit(reviewer_session_id="")  # no orphan; clean state

        reviewer_states = iter(
            [
                "REVIEW_REQUEST_CHANGES: fix tab vs spaces",  # initial spawn → changes
                "REVIEW_RECOMMEND_MERGE: thanks",  # delta resume → merge
            ]
        )
        coder_responses = iter(["fix on the way\nFIX_PUSHED\nshipped"])

        # Per-role worker that pulls from its own response queue
        class SeqWorker:
            def __init__(self, role: str):
                self.role = role
                self.spawn_calls: list = []
                self.resume_calls: list = []

            def spawn(self, task, *, title=None):
                self.spawn_calls.append((task, title))
                if self.role == "reviewer":
                    resp = next(reviewer_states)
                elif self.role == "coder":
                    resp = next(coder_responses)
                else:
                    resp = ""
                sid = f"sesn-{self.role}-{len(self.spawn_calls)}"
                return sid, resp

            def resume(self, sid, msg):
                self.resume_calls.append((sid, msg))
                if self.role == "reviewer":
                    return next(reviewer_states)
                if self.role == "coder":
                    return next(coder_responses)
                return ""

            def archive(self, sid):  # pragma: no cover
                pass

        instances: dict[str, SeqWorker] = {}

        def factory(role: str) -> SeqWorker:
            if role not in instances:
                instances[role] = SeqWorker(role)
            return instances[role]

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
        _stub_github(monkeypatch)

        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-2", history=[])
        approved, msg, _outcome = execution._reviewer_phase(ctx)
        assert approved, (
            f"reviewer phase should pass after REQUEST_CHANGES→FIX_PUSHED→RECOMMEND_MERGE "
            f"retry; got approved={approved}, msg={msg!r}, history={ctx.history}"
        )

        # Exactly one cold-spawn (initial), exactly one resume (delta retry).
        rw = instances.get("reviewer")
        assert rw is not None
        assert len(rw.spawn_calls) == 1, (
            f"expected ONE initial reviewer spawn; got {len(rw.spawn_calls)} spawn calls"
        )
        assert len(rw.resume_calls) == 1, (
            f"expected ONE delta-resume (via _resume_reviewer_for_delta) on retry; "
            f"got {len(rw.resume_calls)} resume calls"
        )


# =========================================================================
# Behaviour 8 — Regression test for the F-013 root-cause bug
# =========================================================================


class TestRegressionOrphanReviewerSession:
    """Pre-fix behaviour: ``cycle_review`` on a unit with
    ``status='in_ci'``, ``reviewer_session_id`` set, and CI green hit
    "reviewer session already exists" from ``spawn_reviewer``, which
    ``_record_step`` interpreted as a RAW outcome, which ``_reviewer_phase``
    surfaced as the escalation message ``"reviewer ended with unexpected
    outcome: RAW"``.

    Post-fix expectation: the helper resumes the orphan, gets back
    REVIEW_RECOMMEND_MERGE, and the terminal outcome is
    ``approved_awaiting_merge``.
    """

    def test_cycle_review_recovers_from_orphan_reviewer_session(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        # Orphan ONLY on the reviewer side — tester gets a fresh spawn that
        # immediately returns TESTS_PASS, so the cycle reaches the reviewer
        # phase.
        _seed_feature_and_unit(
            tester_session_id="",
            reviewer_session_id="orphan-reviewer-sid",
        )

        _install_workers(
            monkeypatch,
            per_role={
                # Fresh tester spawn returns TESTS_PASS
                "tester": {"spawn_response": "all green\nTESTS_PASS"},
                # Orphan reviewer resume emits the merge endorsement
                "reviewer": {"resume_response": "looks clean\nREVIEW_RECOMMEND_MERGE: ship it"},
            },
        )
        _stub_github(monkeypatch)

        out_str = execution.cycle_review("F-013", "F-013-U-2")
        out = json.loads(out_str)

        # Specifically these two pre-fix failure signatures must NOT appear:
        assert out["outcome"] != "escalated", (
            f"orphan reviewer session should be recovered, not escalated; "
            f"got outcome={out['outcome']!r}, message={out.get('message')!r}"
        )
        assert "unexpected outcome: RAW" not in out.get("message", ""), (
            f"the 'reviewer ended with unexpected outcome: RAW' regression has "
            f"returned; message={out.get('message')!r}"
        )
        # Positive: the cycle reaches the happy terminal
        assert out["outcome"] == "approved_awaiting_merge"
