"""F-012-U-1: CI poll tuning + drop GATE-3 defensive recheck.

Contract this unit shipped (per `features/F-012/spec.md` and the unit
description):

  1. `orchestrator.ci_wait.DEFAULT_POLL_INTERVAL_SECONDS` defaults to **5s**
     (was 15s). Reduces poll-interval-rounded loss at each of the ~2 CI
     gates per `cycle_review`.

  2. `poll_interval_seconds=0` is honored as a **true busy-poll** — no
     lower-bound clamp, no upward rounding. Valid for tests and very fast
     CI matrices. Both via the `CI_WAIT_POLL_INTERVAL` env var (read at
     import time) and via the explicit kwarg on `wait_for_ci`.

  3. `cycle_review` no longer runs the GATE-3 defensive final pre-merge
     `wait_for_ci`. A clean cycle hits exactly **two** CI gates:
        - GATE 1: coder PR push
        - GATE 2: tester test push
     The reviewer phase's own embedded fix-loop already gates on green
     after any reviewer-driven push, so a final re-check would only
     re-pay the rounded wait without adding signal.

  4. `docs/ARCHITECTURE.md` documents the new defaults (5s default,
     `0` honored as busy-poll) and the dropped GATE-3 box.

These tests are intentionally complementary to (not a replacement for)
the assertions added in `tests/test_ci_wait.py` and
`tests/test_tools_execution.py::TestCycleReview::test_clean_run_makes_exactly_two_ci_waits`.
They probe the contract from independent angles so a partial regression
(e.g. only the env var clamp creeps back, or only the docs revert) still
trips at least one test.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

from orchestrator import ci_wait, state
from orchestrator.ci_wait import CIWaitResult, wait_for_ci
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------- shared deterministic stubs ---------------------------


def _check(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "details_url": f"https://example.com/{name}",
    }


def _snapshot(*runs: dict) -> dict:
    return {
        "total": len(runs),
        "runs": list(runs),
        "conclusion_counts": {},
        "head_sha": "abc123",
    }


class _Clock:
    """Monotonic time + sleep recorder. `now()` ticks by `step` per call."""

    def __init__(self, step: float = 1.0) -> None:
        self.step = step
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        t = self.t
        self.t += self.step
        return t

    def sleep(self, n: float) -> None:
        self.slept.append(n)


class _ScriptedChecks:
    """Returns the next scripted snapshot per call; final snapshot repeats."""

    def __init__(self, *snapshots: dict) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0

    def __call__(self, repo_url: str, pr_number: int) -> dict:
        idx = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[idx]


# =====================================================================
# Contract 1: default poll interval is 5s
# =====================================================================


class TestDefaultPollIntervalIsFiveSeconds:
    """`DEFAULT_POLL_INTERVAL_SECONDS == 5` under a clean environment."""

    def test_module_constant_with_no_env_override(self, monkeypatch):
        """No env var → default literal in source must be 5."""
        monkeypatch.delenv("CI_WAIT_POLL_INTERVAL", raising=False)
        importlib.reload(ci_wait)
        try:
            assert ci_wait.DEFAULT_POLL_INTERVAL_SECONDS == 5, (
                f"expected default poll interval 5s, got "
                f"{ci_wait.DEFAULT_POLL_INTERVAL_SECONDS}s — F-012-U-1 must drop "
                "the historical 15s default"
            )
        finally:
            importlib.reload(ci_wait)


# =====================================================================
# Contract 2: poll_interval = 0 is honored (no clamp), both via env + kwarg
# =====================================================================


class TestPollIntervalZeroBusyPoll:
    """`0` is honored as a true busy-poll — no lower-bound clamp anywhere."""

    def test_env_var_zero_honored(self, monkeypatch):
        """`CI_WAIT_POLL_INTERVAL=0` produces a module default of 0, not clamped up."""
        monkeypatch.setenv("CI_WAIT_POLL_INTERVAL", "0")
        importlib.reload(ci_wait)
        try:
            assert ci_wait.DEFAULT_POLL_INTERVAL_SECONDS == 0, (
                "env CI_WAIT_POLL_INTERVAL=0 must produce DEFAULT_POLL_INTERVAL_SECONDS=0 "
                "(no lower-bound clamp — busy-poll opt-in for tests / fast CI)"
            )
        finally:
            monkeypatch.delenv("CI_WAIT_POLL_INTERVAL", raising=False)
            importlib.reload(ci_wait)

    def test_kwarg_zero_passes_zero_to_sleep_until_green(self):
        """End-to-end: `poll_interval_seconds=0` on a pending→green sequence
        must complete with `status='green'` and every recorded sleep == 0.

        The implementation must not clamp the 0 up to (say) 1s before
        passing to `sleep()`.
        """
        clock = _Clock(step=2.0)
        fetch = _ScriptedChecks(
            _snapshot(_check("tests", status="in_progress", conclusion=None)),
            _snapshot(_check("tests", status="in_progress", conclusion=None)),
            _snapshot(_check("tests", status="in_progress", conclusion=None)),
            _snapshot(_check("tests", status="completed", conclusion="success")),
        )
        r = wait_for_ci(
            "https://github.com/o/r",
            1,
            timeout_seconds=120,
            poll_interval_seconds=0,
            no_ci_grace_seconds=60,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "green"
        # 4 polls: 3 pending + 1 green → 3 sleeps between them.
        assert len(clock.slept) == 3, (
            f"expected 3 sleeps (one between each of 4 polls), got {clock.slept!r}"
        )
        # Every sleep was exactly 0 — no upward clamp.
        assert all(s == 0 for s in clock.slept), (
            f"poll_interval_seconds=0 must produce 0-valued sleeps, got {clock.slept!r}"
        )

    def test_kwarg_zero_passes_zero_during_no_ci_grace(self):
        """The same no-clamp rule applies during the no-CI grace polling.

        We pick `step=2.0`, `no_ci_grace=5`: a few empty polls fire before
        elapsed crosses the grace threshold and `wait_for_ci` returns
        `no_ci`. Every grace-window sleep must be 0 — no upward clamp.
        """
        clock = _Clock(step=2.0)
        fetch = _ScriptedChecks(_snapshot())  # always empty
        r = wait_for_ci(
            "https://github.com/o/r",
            1,
            timeout_seconds=120,
            poll_interval_seconds=0,
            no_ci_grace_seconds=5,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "no_ci"
        assert clock.slept and all(s == 0 for s in clock.slept), (
            "no-CI grace polling must also honor poll_interval=0 with zero-valued sleeps; "
            f"got {clock.slept!r}"
        )


# =====================================================================
# Contract 3: cycle_review makes exactly 2 CI waits on a clean run
# =====================================================================


def _seed_ready_for_cycle(unit_id: str = "F-001-U-1", feature_id: str = "F-001") -> None:
    """Insert a feature + plan + WorkUnitState ready for cycle_review."""
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
        [WorkUnit(id=unit_id, feature_id=feature_id, title="u1", description="impl this")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="in_ci",
            branch="feat/branch",
            pr_number=5,
            coder_session_id="sesn-c",
        )
    )


def _stub_github_for_cycle(monkeypatch) -> None:
    """Patch every github.* call cycle_review reaches into."""
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
        lambda *a, **k: None,  # timeout = best-effort no-op
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("owner", "repo"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
    )


def _stub_clean_phases(monkeypatch) -> None:
    """tester says TESTS_PASS, reviewer says REVIEW_RECOMMEND_MERGE."""
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


class TestCycleReviewTwoCIWaitsOnly:
    """A clean `cycle_review` runs exactly GATE 1 + GATE 2 (no GATE 3)."""

    def test_clean_run_calls_wait_for_ci_exactly_twice(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Count `ci_wait.wait_for_ci` invocations during a clean cycle.

        Pre-F-012-U-1 the count was 3 (coder push, tester push, final pre-merge).
        Post-F-012-U-1 it must be 2.
        """
        _seed_ready_for_cycle()
        _stub_clean_phases(monkeypatch)
        _stub_github_for_cycle(monkeypatch)

        calls: list[tuple[tuple, dict]] = []

        def counting_wait(*args, **kwargs):
            calls.append((args, kwargs))
            return CIWaitResult(status="green", elapsed_seconds=0.1, total_checks=1)

        monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", counting_wait)

        out = execution.cycle_review("F-001", "F-001-U-1")
        parsed = json.loads(out)

        assert parsed["outcome"] == "approved_awaiting_merge", parsed
        assert len(calls) == 2, (
            f"clean cycle_review must call wait_for_ci exactly twice "
            f"(GATE 1 coder push + GATE 2 tester push); got {len(calls)}. "
            f"A 3rd call indicates the GATE-3 defensive recheck was re-added."
        )


# =====================================================================
# Contract 4: documentation reflects the new defaults
# =====================================================================


class TestArchitectureDocumentsNewDefaults:
    """`docs/ARCHITECTURE.md` mentions the new 5s default and 0-as-busy-poll."""

    def test_architecture_md_states_5s_default(self):
        text = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        # The env-knobs paragraph must reference CI_WAIT_POLL_INTERVAL with
        # a `5s` default (not `15s`). Regex pinned to the same line family
        # used in the existing prose so a future re-word still trips if the
        # number reverts.
        assert "CI_WAIT_POLL_INTERVAL" in text, (
            "ARCHITECTURE.md no longer mentions CI_WAIT_POLL_INTERVAL"
        )
        # The `(default Ns` annotation can wrap onto the next line in the
        # rendered prose — match across the small window after the env-var
        # name (DOTALL).
        m = re.search(
            r"CI_WAIT_POLL_INTERVAL.{0,80}?\(default\s+(\d+)s",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        assert m, (
            "ARCHITECTURE.md must document CI_WAIT_POLL_INTERVAL with a `(default Ns` annotation"
        )
        assert m.group(1) == "5", (
            f"ARCHITECTURE.md documents CI_WAIT_POLL_INTERVAL default as {m.group(1)}s, "
            "must be 5s per F-012-U-1"
        )
        # 15s default must no longer be advertised anywhere in the env-knobs prose.
        assert "default 15s" not in text, (
            "ARCHITECTURE.md still advertises the old `default 15s` somewhere"
        )

    def test_architecture_md_describes_zero_as_busy_poll(self):
        text = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        assert "busy-poll" in text.lower(), (
            "ARCHITECTURE.md must describe the `0` poll-interval semantics "
            "(busy-poll / no clamp) per F-012-U-1"
        )

    def test_architecture_md_drops_gate3_box(self):
        """The ASCII gate diagram must no longer include a GATE-3 final box.

        Pre-F-012-U-1 the diagram had a `[GATE 3: wait_for_ci final pre-merge
        confirmation]` line. Removing the block was an explicit part of the
        unit's scope.
        """
        text = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        assert "GATE 3:" not in text, (
            "ARCHITECTURE.md still references `GATE 3:` in the cycle-review diagram"
        )
        assert "final pre-merge confirmation" not in text.lower(), (
            "ARCHITECTURE.md still describes the dropped `final pre-merge confirmation` gate"
        )
