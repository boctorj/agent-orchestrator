"""Tests for orchestrator/ci_wait.py — polling logic for CI check_runs.

We inject `get_checks`, `sleep`, and `now` into `wait_for_ci` so each test
walks the polling loop through a deterministic sequence of snapshots
without burning wall-clock time.
"""

from __future__ import annotations

import pytest

from orchestrator import ci_wait
from orchestrator.ci_wait import CIWaitResult, wait_for_ci

# --------------------------- helpers ---------------------------


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


class FakeClock:
    """Monotonic clock + sleep counter.

    `now()` increments by `step` seconds every call (default 5s/poll) so a
    "10s timeout, 5s poll" test sees the timeout trip after 2 polls
    without any actual sleep.
    """

    def __init__(self, step: float = 5.0):
        self.step = step
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        # Capture the current time then advance — first call returns 0.
        out = self.t
        self.t += self.step
        return out

    def sleep(self, n: float) -> None:
        self.slept.append(n)


class ScriptedGetChecks:
    """Returns the next scripted snapshot each call. Last snapshot repeats."""

    def __init__(self, *snapshots: dict):
        self.snapshots = list(snapshots)
        self.calls = 0

    def __call__(self, repo_url: str, pr_number: int) -> dict:
        idx = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[idx]


# --------------------------- happy path ---------------------------


class TestWaitForCIGreen:
    def test_all_success_on_first_poll(self):
        clock = FakeClock(step=0)
        fetch = ScriptedGetChecks(_snapshot(_check("lint"), _check("tests")))
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=60,
            poll_interval_seconds=5,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "green"
        assert r.total_checks == 2
        assert clock.slept == []  # didn't poll twice

    def test_skipped_and_neutral_count_as_passing(self):
        """`success` is most common but `skipped` / `neutral` are valid pass."""
        clock = FakeClock(step=0)
        fetch = ScriptedGetChecks(
            _snapshot(
                _check("lint", conclusion="success"),
                _check("tests-windows", conclusion="skipped"),  # path filter
                _check("benchmark", conclusion="neutral"),
            )
        )
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=60,
            poll_interval_seconds=5,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "green"
        assert r.total_checks == 3

    def test_pending_then_green_after_a_few_polls(self):
        clock = FakeClock(step=5)  # each poll advances 5 wallclock seconds
        fetch = ScriptedGetChecks(
            _snapshot(_check("tests", status="in_progress", conclusion=None)),
            _snapshot(_check("tests", status="in_progress", conclusion=None)),
            _snapshot(_check("tests", status="completed", conclusion="success")),
        )
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=60,
            poll_interval_seconds=5,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "green"
        # 2 sleeps before the third poll returned green
        assert len(clock.slept) == 2


# --------------------------- failure paths ---------------------------


class TestWaitForCIFailed:
    def test_single_failure_short_circuits(self):
        clock = FakeClock(step=0)
        fetch = ScriptedGetChecks(
            _snapshot(
                _check("lint", conclusion="success"),
                _check("tests", conclusion="failure"),
            )
        )
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=60,
            poll_interval_seconds=5,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "failed"
        assert len(r.failing_runs) == 1
        assert r.failing_runs[0]["name"] == "tests"

    @pytest.mark.parametrize(
        "conclusion",
        ["failure", "cancelled", "timed_out", "action_required", "stale"],
    )
    def test_failure_conclusions_all_treated_as_failed(self, conclusion):
        clock = FakeClock(step=0)
        fetch = ScriptedGetChecks(_snapshot(_check("bad", conclusion=conclusion)))
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=60,
            poll_interval_seconds=5,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "failed", f"{conclusion} should be a failure"

    def test_failure_among_pending_still_fails_fast(self):
        """One completed-failure is enough — don't wait for the others."""
        clock = FakeClock(step=0)
        fetch = ScriptedGetChecks(
            _snapshot(
                _check("lint", status="in_progress", conclusion=None),
                _check("tests", conclusion="failure"),
            )
        )
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=60,
            poll_interval_seconds=5,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "failed"


# --------------------------- timeout ---------------------------


class TestWaitForCITimeout:
    def test_pending_until_timeout(self):
        clock = FakeClock(step=5)
        # Always returns "still running"
        fetch = ScriptedGetChecks(_snapshot(_check("tests", status="in_progress", conclusion=None)))
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=15,
            poll_interval_seconds=5,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "timeout"
        assert r.total_checks == 1
        # Elapsed should be >= timeout
        assert r.elapsed_seconds >= 15


# --------------------------- no-CI sandbox ---------------------------


class TestWaitForCINoCi:
    def test_zero_checks_returns_no_ci_after_grace(self):
        clock = FakeClock(step=10)
        fetch = ScriptedGetChecks(_snapshot())  # empty
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=120,
            poll_interval_seconds=5,
            no_ci_grace_seconds=15,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "no_ci"

    def test_no_checks_initially_then_checks_appear(self):
        """GH Actions sometimes takes a few seconds to register the workflow.

        The grace period gives it a window to show up before we declare 'no CI'.
        """
        clock = FakeClock(step=5)
        fetch = ScriptedGetChecks(
            _snapshot(),  # empty on first poll
            _snapshot(_check("tests", conclusion="success")),  # appears second poll
        )
        r = wait_for_ci(
            "u",
            1,
            timeout_seconds=120,
            poll_interval_seconds=5,
            no_ci_grace_seconds=15,  # 15s grace
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        # First poll: elapsed=0, empty → wait. Second poll: elapsed=5,
        # check exists → green.
        assert r.status == "green"


# --------------------------- formatting helpers ---------------------------


class TestSummary:
    def test_green_summary(self):
        r = CIWaitResult(status="green", elapsed_seconds=8.0, total_checks=4)
        assert "✓ CI green" in r.summary
        assert "4 check" in r.summary

    def test_failed_summary_includes_check_names(self):
        r = CIWaitResult(
            status="failed",
            elapsed_seconds=12.0,
            total_checks=3,
            failing_runs=[{"name": "lint"}, {"name": "tests"}],
        )
        assert "✗ CI failed" in r.summary
        assert "lint" in r.summary and "tests" in r.summary

    def test_timeout_summary(self):
        r = CIWaitResult(status="timeout", elapsed_seconds=600.0, total_checks=2)
        assert "timeout" in r.summary
        assert "2 check" in r.summary

    def test_no_ci_summary(self):
        r = CIWaitResult(status="no_ci", elapsed_seconds=30.0)
        assert "no CI configured" in r.summary


# --------------------------- env defaults ---------------------------


def test_env_defaults_override_module_defaults(monkeypatch):
    """Re-import to pick up env at module init."""
    monkeypatch.setenv("CI_WAIT_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("CI_WAIT_POLL_INTERVAL", "7")
    monkeypatch.setenv("CI_WAIT_NO_CI_GRACE", "3")

    import importlib

    importlib.reload(ci_wait)
    try:
        assert ci_wait.DEFAULT_TIMEOUT_SECONDS == 42
        assert ci_wait.DEFAULT_POLL_INTERVAL_SECONDS == 7
        assert ci_wait.DEFAULT_NO_CI_GRACE_SECONDS == 3
    finally:
        # Reset for subsequent tests in this session
        monkeypatch.delenv("CI_WAIT_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("CI_WAIT_POLL_INTERVAL", raising=False)
        monkeypatch.delenv("CI_WAIT_NO_CI_GRACE", raising=False)
        importlib.reload(ci_wait)
