"""Wait for a PR's CI check_runs to settle.

Used by `cycle_review` and the standalone spawn_* tools to enforce a
"CI must be green" gate at each hand-off:

  coder push    → wait_for_ci → tester
  tester push   → wait_for_ci → Copilot + reviewer
  coder fix     → wait_for_ci → next phase
  reviewer approves → wait_for_ci (final) → approved_awaiting_merge

When CI fails the orchestrator loops back to the coder with
`address_review(source='ci', feedback=…)` and re-waits after FIX_PUSHED.

Pure module — `wait_for_ci` accepts `get_checks`, `sleep`, `now`
callables so tests drive deterministic state without real time.

Conclusion semantics
--------------------

A check_run is *passing* if its conclusion is in {success, skipped, neutral}.
(`skipped` covers path-filtered checks that GitHub never actually ran.
`neutral` covers checks that decided to no-op without failure intent.)

A check_run is *failing* if its conclusion is in {failure, cancelled,
timed_out, action_required, stale}. Any one failure short-circuits the
wait into "failed" status.

A check_run is *pending* if its status is anything other than "completed".
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from orchestrator import github

# --------------------------- defaults ---------------------------

# How long to wait for CI before giving up and escalating. Tunable for the
# slowest expected CI matrix. The orchestrator's own CI takes ~3 minutes;
# target repos with Linux + macOS + Windows matrices commonly take longer.
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("CI_WAIT_TIMEOUT_SECONDS", "600"))  # 10 min

# Poll the check_runs API every N seconds. Long enough to avoid rate-
# limiting on long waits, short enough that we react promptly when CI
# settles.
DEFAULT_POLL_INTERVAL_SECONDS = int(os.getenv("CI_WAIT_POLL_INTERVAL", "15"))

# If a PR has zero check_runs immediately after a push, GitHub Actions
# may simply not have started the workflow yet. Wait briefly before
# declaring "no CI configured" and proceeding without the gate.
DEFAULT_NO_CI_GRACE_SECONDS = int(os.getenv("CI_WAIT_NO_CI_GRACE", "30"))


# --------------------------- result type ---------------------------

WaitStatus = Literal["green", "failed", "timeout", "no_ci"]

FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "stale"}
)
PASSING_CONCLUSIONS: frozenset[str] = frozenset({"success", "skipped", "neutral"})


@dataclass
class CIWaitResult:
    status: WaitStatus
    elapsed_seconds: float
    total_checks: int = 0
    failing_runs: list[dict] = field(default_factory=list)
    last_snapshot: dict | None = None

    @property
    def summary(self) -> str:
        if self.status == "green":
            return f"✓ CI green ({self.total_checks} check(s), {self.elapsed_seconds:.0f}s)"
        if self.status == "failed":
            names = ", ".join(r.get("name", "?") for r in self.failing_runs[:5])
            n = len(self.failing_runs)
            return f"✗ CI failed: {n} failing check(s) — {names}"
        if self.status == "timeout":
            return (
                f"⏱ CI timeout after {self.elapsed_seconds:.0f}s "
                f"({self.total_checks} check(s) still running)"
            )
        return f"⚠ no CI configured (proceeded after {self.elapsed_seconds:.0f}s grace)"


# --------------------------- main entry point ---------------------------


def wait_for_ci(
    repo_url: str,
    pr_number: int,
    *,
    timeout_seconds: int | None = None,
    poll_interval_seconds: int | None = None,
    no_ci_grace_seconds: int | None = None,
    get_checks: Callable[[str, int], dict] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> CIWaitResult:
    """Poll GitHub check_runs until CI settles, fails, times out, or grace-period passes.

    Args:
        repo_url: the target repo (passed through to `get_checks`).
        pr_number: the PR number.
        timeout_seconds: maximum total wait. Defaults to env / 600s.
        poll_interval_seconds: sleep between polls. Defaults to env / 15s.
        no_ci_grace_seconds: how long to wait while total==0 before declaring no-CI.
        get_checks: injection point — defaults to `github.get_pr_check_runs`.
        sleep: injection point for tests (replace with no-op).
        now: injection point — defaults to `time.monotonic` for elapsed time.

    Returns:
        CIWaitResult with status one of:
          - 'green'   — every check completed with a passing conclusion
          - 'failed'  — at least one check completed with a failing conclusion
          - 'timeout' — timeout_seconds elapsed with checks still pending
          - 'no_ci'   — no checks appeared within no_ci_grace_seconds
    """
    timeout = DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    poll = DEFAULT_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
    no_ci_grace = (
        DEFAULT_NO_CI_GRACE_SECONDS if no_ci_grace_seconds is None else no_ci_grace_seconds
    )
    fetch = get_checks if get_checks is not None else github.get_pr_check_runs

    start = now()

    while True:
        snapshot = fetch(repo_url, pr_number)
        total = snapshot.get("total", 0)
        runs = snapshot.get("runs", [])
        elapsed = now() - start

        # No checks visible — either CI hasn't started, or repo has no CI.
        if total == 0:
            if elapsed >= no_ci_grace:
                return CIWaitResult(
                    status="no_ci",
                    elapsed_seconds=elapsed,
                    last_snapshot=snapshot,
                )
            sleep(poll)
            continue

        failing = [
            r
            for r in runs
            if r.get("status") == "completed" and r.get("conclusion") in FAILURE_CONCLUSIONS
        ]
        if failing:
            return CIWaitResult(
                status="failed",
                elapsed_seconds=elapsed,
                total_checks=total,
                failing_runs=failing,
                last_snapshot=snapshot,
            )

        pending = [r for r in runs if r.get("status") != "completed"]
        if not pending:
            return CIWaitResult(
                status="green",
                elapsed_seconds=elapsed,
                total_checks=total,
                last_snapshot=snapshot,
            )

        # Still settling — check timeout
        if elapsed >= timeout:
            return CIWaitResult(
                status="timeout",
                elapsed_seconds=elapsed,
                total_checks=total,
                last_snapshot=snapshot,
            )

        sleep(poll)


__all__ = [
    "DEFAULT_NO_CI_GRACE_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAILURE_CONCLUSIONS",
    "PASSING_CONCLUSIONS",
    "CIWaitResult",
    "WaitStatus",
    "wait_for_ci",
]
