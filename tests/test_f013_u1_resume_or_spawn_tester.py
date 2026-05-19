"""Independent tester-agent tests for F-013-U-1.

The unit adds a ``_resume_or_spawn_tester(feature_id, unit_id)`` helper to
``orchestrator/tools/execution.py`` and rewires the **initial** call site
inside ``_tester_phase`` from ``spawn_tester(...)`` to
``_resume_or_spawn_tester(...)``. Behaviour:

  * If ``unit_state.tester_session_id`` is empty → delegate to
    ``spawn_tester(feature_id, unit_id)`` and return its raw response.
  * If set → call ``_resume_role_session("tester", sid, recovery_prompt)``
    with a prompt instructing the tester to re-emit its verdict marker,
    scan via ``_record_terminal_marker(role="tester", ...)`` and return
    JSON in the same shape ``_record_step`` consumes from a fresh
    ``spawn_tester`` output (TESTS_PASS / BUG_FOUND / BLOCKED).

Wire change: ``_tester_phase`` initial call uses the helper.
The interior **retry** site (clears ``tester_session_id`` first then
calls ``spawn_tester`` directly) is intentionally untouched and exercised
here too.

Regression bug this unit fixes: ``cycle_review`` re-entering a unit with
a non-empty ``tester_session_id`` (network-timeout orphan) previously
escalated as ``outcome="escalated"`` / message ``"tester ended with
unexpected outcome: RAW"`` because the first ``spawn_tester`` errored
with "tester session already exists" and ``_record_step`` parsed the
error string as a RAW outcome.

These tests are independent of the coder's tests in
``tests/test_tools_execution.py::TestResumeOrSpawnTester`` /
``TestCycleReviewRecoversOrphanedTesterSession`` — divergences should
flag drift in either direction.
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


def _seed_feature_and_unit(
    *,
    feature_id: str = "F-013",
    unit_id: str = "F-013-U-1",
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
            review_round=review_round,
        )
    )


# =========================================================================
# Behaviour 1 — helper delegates to spawn_tester when no orphan session
# =========================================================================


class TestDelegatesWhenNoSession:
    """With ``tester_session_id`` empty the helper must call ``spawn_tester``
    and return that response verbatim — no resume call, no extra parsing."""

    def test_empty_session_calls_spawn_tester(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(tester_session_id="")

        captured_args: list[tuple[str, str]] = []
        sentinel = json.dumps({"unit_id": "F-013-U-1", "outcome": "TESTS_PASS"})

        def fake_spawn(fid: str, uid: str) -> str:
            captured_args.append((fid, uid))
            return sentinel

        monkeypatch.setattr(execution, "spawn_tester", fake_spawn)

        # ManagedAgentWorker should NOT be instantiated — the helper has
        # nothing to resume so it should hand straight to spawn_tester.
        def boom_worker(*a, **k):
            raise AssertionError(
                "ManagedAgentWorker constructed in delegate path — helper must "
                "not attempt a resume when tester_session_id is empty"
            )

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", boom_worker)

        out = execution._resume_or_spawn_tester("F-013", "F-013-U-1")

        assert captured_args == [("F-013", "F-013-U-1")]
        # Returns whatever spawn_tester returned, verbatim
        assert out == sentinel

    def test_no_unit_state_falls_through_to_spawn_tester(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """An unspawned unit (no row in work_units) shouldn't break the helper —
        it should just defer to spawn_tester (which will surface its own
        precondition error). Mirrors the implementation's `unit_state is None`
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
            [WorkUnit(id="F-013-U-1", feature_id="F-013", title="u", description="d")],
        )
        state.approve_plan("F-013")

        calls: list[tuple[str, str]] = []

        def fake_spawn(fid: str, uid: str) -> str:
            calls.append((fid, uid))
            return "DELEGATED"

        monkeypatch.setattr(execution, "spawn_tester", fake_spawn)

        out = execution._resume_or_spawn_tester("F-013", "F-013-U-1")
        assert calls == [("F-013", "F-013-U-1")]
        assert out == "DELEGATED"


# =========================================================================
# Behaviour 2 — orphan session: resume with the spec's recovery prompt
# =========================================================================


class TestRecoveryPromptContract:
    """The resume prompt must match the unit description: explain it was a
    network timeout, that the PR is open + CI green, instruct re-emit of
    TESTS_PASS / BUG_FOUND / BLOCKED on its own line. Asserting the
    exact wording would be brittle; instead we assert the load-bearing
    phrases that make the prompt actionable for the resumed agent."""

    def test_resume_prompt_carries_required_signals(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(tester_session_id="stale-tester-sid")
        instances = _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}}
        )
        _stub_github(monkeypatch)

        # Helper must resume — never call spawn_tester in this branch
        def boom(*a, **k):
            raise AssertionError("must resume — not spawn — when session_id is set")

        monkeypatch.setattr(execution, "spawn_tester", boom)

        execution._resume_or_spawn_tester("F-013", "F-013-U-1")

        worker = instances["tester"]
        assert len(worker.resume_calls) == 1
        sid, msg = worker.resume_calls[0]
        assert sid == "stale-tester-sid", "must resume the orphaned session, not a new one"

        # Description-mandated content beats verbatim string comparison.
        # The agent needs to know (a) why it's being re-prompted, (b) what
        # marker vocabulary to use, (c) not to redo work.
        assert "network timeout" in msg.lower()
        for marker_name in ("TESTS_PASS", "BUG_FOUND", "BLOCKED"):
            assert marker_name in msg, f"resume prompt must list {marker_name}"
        # The "don't redo work" instruction (description: "do not redo test
        # work you already completed")
        assert "do not redo" in msg.lower() or "not redo" in msg.lower()


# =========================================================================
# Behaviour 3 — JSON shape parity with fresh spawn_tester
# =========================================================================


class TestResumeReturnsShapeRecordStepConsumes:
    """``_record_step`` calls ``json.loads`` and pulls fields out of the
    parsed dict (``outcome``, ``bug``, ...). The helper's resume-path
    return must therefore be valid JSON with the same field names as
    ``spawn_tester`` returns. Cross-check by feeding the helper's output
    back through the same parser ``_tester_phase`` uses.
    """

    def test_resume_tests_pass_round_trips_through_record_step(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(tester_session_id="stale-sid")
        _install_workers(
            monkeypatch, per_role={"tester": {"resume_response": "verdict\nTESTS_PASS\n"}}
        )
        _stub_github(monkeypatch)

        raw = execution._resume_or_spawn_tester("F-013", "F-013-U-1")
        parsed = json.loads(raw)

        # Shape matches spawn_tester's TESTS_PASS branch
        assert parsed["unit_id"] == "F-013-U-1"
        assert parsed["outcome"] == "TESTS_PASS"
        assert parsed["session_id"] == "stale-sid"
        assert "summary" in parsed

        # And ``_record_step`` (the function ``_tester_phase`` wraps the
        # helper's output with) consumes it cleanly — no RAW fallback.
        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-1", history=[])
        out = execution._record_step(ctx, "tester", raw)
        assert out.get("outcome") == "TESTS_PASS"
        assert out.get("outcome") != "RAW"
        # `_record_step` should have appended one entry
        assert ctx.history[-1]["step"] == "tester"
        assert ctx.history[-1]["result"]["outcome"] == "TESTS_PASS"

    def test_resume_bug_found_round_trips_through_record_step(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(tester_session_id="stale-sid")
        bug_text = "off-by-one in pagination"
        _install_workers(
            monkeypatch,
            per_role={"tester": {"resume_response": f"i found:\nBUG_FOUND: {bug_text}\nsee tests"}},
        )
        _stub_github(monkeypatch)

        raw = execution._resume_or_spawn_tester("F-013", "F-013-U-1")
        parsed = json.loads(raw)
        assert parsed["outcome"] == "BUG_FOUND"
        assert parsed["bug"] == bug_text
        assert parsed["session_id"] == "stale-sid"
        assert parsed["unit_id"] == "F-013-U-1"

        # `_record_step` must surface the bug text untouched so
        # `_tester_phase` can forward it to `address_review`
        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-1", history=[])
        out = execution._record_step(ctx, "tester", raw)
        assert out["outcome"] == "BUG_FOUND"
        assert out.get("bug") == bug_text

    def test_resume_blocked_propagates_marker(self, tmp_state_db, with_github_token, monkeypatch):
        """BLOCKED on resume must be passed through in a form
        ``_record_step`` parses as ``outcome.startswith("BLOCKED")``
        (matching how ``_tester_phase`` short-circuits on tester blocks)."""
        _seed_feature_and_unit(tester_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={
                "tester": {
                    "resume_response": (
                        "tried again\nBLOCKED: reason=ci_tool_missing tool=pytest "
                        "| pytest not installed"
                    )
                }
            },
        )
        _stub_github(monkeypatch)

        raw = execution._resume_or_spawn_tester("F-013", "F-013-U-1")

        # The wire path: `_record_step` → outcome string startswith "BLOCKED"
        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-1", history=[])
        out = execution._record_step(ctx, "tester", raw)
        outcome = out.get("outcome")
        # Either a parsed JSON outcome that starts with BLOCKED, or the RAW
        # fallback whose raw string starts with BLOCKED — `_tester_phase`'s
        # ``isinstance(outcome, str) and outcome.startswith("BLOCKED")``
        # branch covers the RAW-string case.
        if outcome == "RAW":
            raw_blob = out.get("raw", "")
            assert raw_blob.startswith("BLOCKED"), (
                f"_record_step RAW fallback must preserve BLOCKED prefix; got: {raw_blob!r}"
            )
        else:
            assert isinstance(outcome, str) and outcome.startswith("BLOCKED"), (
                f"resume-path BLOCKED outcome must start with 'BLOCKED'; got: {outcome!r}"
            )

        # And the unit status is now escalated, per _record_terminal_marker
        s = state.get_unit_state("F-013-U-1")
        assert s.status == "escalated"


# =========================================================================
# Behaviour 4 — audit trail
# =========================================================================


class TestRecordsMarkerEvent:
    """Per the description: scan response via ``_record_terminal_marker``.
    That helper writes a ``tests_pass`` / ``tester_bug_found`` / etc. event
    row to the audit log — those events must be present after a resume."""

    def test_tests_pass_writes_audit_event(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(tester_session_id="stale-sid", review_round=2)
        _install_workers(monkeypatch, per_role={"tester": {"resume_response": "TESTS_PASS"}})
        _stub_github(monkeypatch)

        execution._resume_or_spawn_tester("F-013", "F-013-U-1")

        events = state.list_events("F-013-U-1")
        types = [e["event_type"] for e in events]
        assert "tests_pass" in types, f"resume must record tests_pass event; got {types}"

        # The event is anchored to the orphaned session (NOT a fresh one)
        # and inherits the unit's current review_round.
        tp = next(e for e in events if e["event_type"] == "tests_pass")
        assert tp["session_id"] == "stale-sid"
        assert tp["cycle_number"] == 2

    def test_bug_found_writes_audit_event(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(tester_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={"tester": {"resume_response": "BUG_FOUND: forgot null guard"}},
        )
        _stub_github(monkeypatch)

        execution._resume_or_spawn_tester("F-013", "F-013-U-1")

        types = [e["event_type"] for e in state.list_events("F-013-U-1")]
        assert "tester_bug_found" in types


# =========================================================================
# Behaviour 5 — no marker on resume → escalation (don't silently swallow)
# =========================================================================


class TestNoMarkerOnResumeEscalates:
    """If the resumed tester comes back with prose lacking any marker we
    can recognise, the helper should escalate rather than return an
    ambiguous OK — matching the pre-existing ``_escalate_no_marker``
    behaviour the description references when it says "scan via
    ``_record_terminal_marker``".
    """

    def test_no_marker_response_returns_escalation(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(tester_session_id="stale-sid")
        _install_workers(
            monkeypatch,
            per_role={"tester": {"resume_response": "uhh I have nothing to say"}},
        )
        _stub_github(monkeypatch)

        out = execution._resume_or_spawn_tester("F-013", "F-013-U-1")
        assert "ESCALATED" in out
        assert state.get_unit_state("F-013-U-1").status == "escalated"

        # And an audit event was recorded
        types = [e["event_type"] for e in state.list_events("F-013-U-1")]
        assert "tester_no_marker" in types


# =========================================================================
# Behaviour 6 — resume worker exception is surfaced as ERROR (not raised)
# =========================================================================


class TestResumeExceptionPath:
    """If ``ManagedAgentWorker.resume`` raises (e.g. network blip on the
    recovery prompt itself), the helper must not propagate the exception
    out of cycle_review — it should mark the unit escalated and return
    an ERROR string."""

    def test_resume_exception_surfaces_as_error(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_and_unit(tester_session_id="stale-sid")

        class BoomWorker:
            def __init__(self, role: str):
                self.role = role

            def resume(self, sid: str, msg: str) -> str:
                raise RuntimeError("anthropic 503")

            def spawn(self, *a, **k):  # pragma: no cover
                raise AssertionError("should not spawn")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", BoomWorker)

        out = execution._resume_or_spawn_tester("F-013", "F-013-U-1")
        assert out.startswith("ERROR")
        assert "anthropic 503" in out
        s = state.get_unit_state("F-013-U-1")
        assert s.status == "escalated"
        assert "anthropic 503" in (s.last_error or "")


# =========================================================================
# Behaviour 7 — wire change: _tester_phase uses the helper at the initial
# call site, the retry site remains spawn_tester
# =========================================================================


class TestTesterPhaseWireUp:
    """The unit description's wire change must hold: initial call uses
    ``_resume_or_spawn_tester``; retry call after a BUG_FOUND → FIX_PUSHED
    loop still uses ``spawn_tester`` directly (the retry path clears the
    session id first)."""

    def test_initial_call_uses_resume_helper(self, tmp_state_db, with_github_token, monkeypatch):
        """Source-level assertion: scanning the body of `_tester_phase` reveals
        ``_resume_or_spawn_tester`` is called and the *first* spawn-or-resume
        reference is the helper. Belt-and-braces on the behaviour test below.
        """
        src = inspect.getsource(execution._tester_phase)
        assert "_resume_or_spawn_tester" in src, "_tester_phase must invoke _resume_or_spawn_tester"

        # The initial call to a tester-spawning function should be the
        # helper. Find the line numbers of each reference.
        first_helper = src.find("_resume_or_spawn_tester")
        assert first_helper >= 0
        # `spawn_tester(` appears in `_resume_or_spawn_tester(...)` too. Find
        # the first standalone-looking call (preceded by whitespace, not by
        # `_resume_or_`):
        idx = 0
        first_bare_spawn = -1
        while True:
            idx = src.find("spawn_tester(", idx)
            if idx == -1:
                break
            prefix = src[max(0, idx - 20) : idx]
            if "_resume_or_" not in prefix:
                first_bare_spawn = idx
                break
            idx += 1

        assert first_bare_spawn > first_helper, (
            "the FIRST tester-spawn call inside _tester_phase must be the "
            "_resume_or_spawn_tester helper; bare spawn_tester is allowed only "
            "at the retry site below"
        )

    def test_retry_after_bug_found_uses_spawn_tester_directly(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """The retry path: tester finds bug → coder pushes fix → CI green →
        cleared tester_session_id → spawn fresh tester. Asserting via call
        sequence:
          1. _resume_or_spawn_tester called once at the top of the phase
             (here: no session yet, so it delegates to spawn_tester #1)
          2. address_review spawns coder fix (FIX_PUSHED)
          3. After clearing tester_session_id, the retry calls spawn_tester
             AGAIN — and this time directly, not through the helper

        The cleanest behavioural assertion: after `_tester_phase` runs to
        completion on bug→fix→pass, ManagedAgentWorker("tester").spawn was
        called TWICE (once initial, once retry) and no resume call was
        made (because the unit started with no orphaned session).
        """
        _seed_feature_and_unit(tester_session_id="")  # NO orphan; clean state

        tester_states = iter(
            [
                "BUG_FOUND: divide by zero",  # first spawn → bug
                "TESTS_PASS",  # retry spawn → pass
            ]
        )
        coder_responses = iter(["fix on the way\nFIX_PUSHED\nshipped"])

        # Make the per-role worker remember every response in sequence
        class SeqWorker:
            def __init__(self, role: str):
                self.role = role
                self.spawn_calls: list = []
                self.resume_calls: list = []

            def spawn(self, task, *, title=None):
                self.spawn_calls.append((task, title))
                if self.role == "tester":
                    resp = next(tester_states)
                elif self.role == "coder":
                    resp = next(coder_responses)
                else:
                    resp = ""
                sid = f"sesn-{self.role}-{len(self.spawn_calls)}"
                return sid, resp

            def resume(self, sid, msg):
                self.resume_calls.append((sid, msg))
                if self.role == "coder":
                    return next(coder_responses)
                if self.role == "tester":
                    return next(tester_states)
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

        ctx = execution.CycleContext(feature_id="F-013", unit_id="F-013-U-1", history=[])
        passed, msg = execution._tester_phase(ctx)
        assert passed, (
            f"tester phase should pass after BUG_FOUND→FIX_PUSHED→TESTS_PASS retry; "
            f"got passed={passed}, msg={msg!r}, history={ctx.history}"
        )

        # Two tester spawns (initial + retry); zero resumes (no orphan).
        tw = instances.get("tester")
        assert tw is not None
        assert len(tw.spawn_calls) == 2, (
            f"expected initial spawn + one retry spawn; got {len(tw.spawn_calls)} spawn calls"
        )
        assert tw.resume_calls == [], "no orphan → no resume call expected"


# =========================================================================
# Behaviour 8 — Regression test for the F-013 root-cause bug
# =========================================================================


class TestRegressionOrphanTesterSession:
    """Pre-fix behaviour: ``cycle_review`` on a unit with
    ``status='in_ci'``, ``tester_session_id`` set, and CI green hit
    "tester session already exists" from ``spawn_tester``, which
    ``_record_step`` interpreted as a RAW outcome, which ``_tester_phase``
    surfaced as the escalation message ``"tester ended with unexpected
    outcome: RAW"``.

    Post-fix expectation: the helper resumes the orphan, gets back
    TESTS_PASS, cycle continues to the reviewer phase, and the terminal
    outcome is ``approved_awaiting_merge``.
    """

    def test_cycle_review_recovers_from_orphan_tester_session(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_and_unit(tester_session_id="orphan-tester-sid")

        _install_workers(
            monkeypatch,
            per_role={
                "tester": {"resume_response": "TESTS_PASS"},
                # Reviewer is fresh (no session yet) so it'll be spawned;
                # any happy-path marker terminates the cycle approved.
                "reviewer": {"spawn_response": "looks clean\nREVIEW_RECOMMEND_MERGE: ship it"},
            },
        )
        _stub_github(monkeypatch)

        out_str = execution.cycle_review("F-013", "F-013-U-1")
        out = json.loads(out_str)

        # Specifically these two pre-fix failure signatures must NOT appear:
        assert out["outcome"] != "escalated", (
            f"orphan tester session should be recovered, not escalated; "
            f"got outcome={out['outcome']!r}, message={out.get('message')!r}"
        )
        assert "unexpected outcome: RAW" not in out.get("message", ""), (
            f"the 'tester ended with unexpected outcome: RAW' regression has "
            f"returned; message={out.get('message')!r}"
        )
        # Positive: the cycle reaches the happy terminal
        assert out["outcome"] == "approved_awaiting_merge"
