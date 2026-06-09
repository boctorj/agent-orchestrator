"""Dataclasses for orchestrator state.

Plans and work units are persisted to SQLite via state.py. These are the
in-memory representations the lead and MCP tools work with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

FeatureStatus = Literal["draft", "planned", "approved", "in_progress", "done"]
PlanStatus = Literal["draft", "approved"]
UnitStatus = Literal[
    "pending",  # plan approved but no spawn yet
    "coding",  # coder session active, implementing
    "testing",  # tester session active, writing/running tests
    "opening_pr",  # coder is opening the PR
    "in_ci",  # PR open, CI running, no review yet
    "reviewing",  # reviewer session active
    "fixing",  # coder addressing review comments
    "approved_awaiting_merge",  # reviewer endorsed; awaits human merge
    "done",  # merged (by human) or otherwise complete
    "escalated",  # cap-3 hit or hard error; awaits human
    "cancelled",  # user-cancelled via cancel_unit (F-016 Phase 2.5)
]

# Single source of truth for status-set membership.
# Reference these rather than open-coding status string lists across modules.
ACTIVE_UNIT_STATUSES: frozenset[str] = frozenset(
    {
        "coding",
        "testing",
        "opening_pr",
        "in_ci",
        "reviewing",
        "fixing",
    }
)
# Endorsed by the reviewer, CI green, but not yet merged by a human. Neither
# active (no agent running) nor terminal (the dep chain stays blocked until
# the merge flips it to ``done``) — sits in its own bucket. F-009-U-4.
READY_TO_MERGE_STATUSES: frozenset[str] = frozenset({"approved_awaiting_merge"})
TERMINAL_UNIT_STATUSES: frozenset[str] = frozenset({"done", "escalated"})
# Sticky-cancel terminal (F-016 Phase 2.5): the user pulled the unit; no
# agent is running, no merge will land, and downstream dep-evaluation
# treats it as not-done (a dep on a ``cancelled`` unit stays blocked
# forever unless the lead reshapes the graph via ``update_unit_deps``).
CANCELLED_UNIT_STATUSES: frozenset[str] = frozenset({"cancelled"})


@dataclass
class Feature:
    id: str
    title: str
    description: str
    repo_path: str = ""  # URL like https://github.com/owner/repo for Managed Agents
    branch_prefix: str = ""
    status: FeatureStatus = "draft"
    created_at: str = ""
    # Opt-in flag for the ultrareview terminal gate (F-007). When True,
    # cycle_review fires /ultrareview after our reviewer endorses and only
    # emits ready-to-merge if ultrareview passes too. Off by default —
    # ultrareview costs measurably per cycle. See
    # docs/PROPOSAL-ultrareview-gate.md for the opt-in semantics.
    ultrareview_enabled: bool = False


@dataclass
class WorkUnit:
    id: str
    feature_id: str
    title: str
    description: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    feature_id: str
    units: list[WorkUnit]
    status: PlanStatus = "draft"
    approved_at: str | None = None


@dataclass
class WorkUnitState:
    """Per-unit runtime state, persisted to the work_units table.

    ``cancelled_at`` and ``owner`` are the F-016 Phase 2.5 additions:

      * ``cancelled_at`` — ISO timestamp when ``cancel_unit`` ran. Sticky:
        once set, the daemon (F-016-U-5) reads it on every tick and stops
        driving the unit; the state machine never leaves ``cancelled``.
      * ``owner`` — short-lived claim string (``"lead"`` while the
        lead-advance-lock is held; ``""`` otherwise). The Phase 3 daemon
        will use this column as a CAS target for terminal advances; for
        Phase 2.5 it just makes the lock visible across processes so the
        daemon doesn't race the lead's ~1s ``send_to_unit_async`` submit
        window.
    """

    unit_id: str
    feature_id: str
    status: UnitStatus = "pending"
    branch: str = ""
    pr_number: int | None = None
    coder_session_id: str = ""
    tester_session_id: str = ""
    reviewer_session_id: str = ""
    review_round: int = 0
    last_activity: str = ""
    last_error: str = ""
    cancelled_at: str | None = None
    owner: str = ""
    # F-018: mechanical rebase retries kept separate from ``review_round`` so a
    # sibling-merge-induced conflict does not consume the quality cap-3 budget.
    # Capped independently at :data:`orchestrator.tools.CAP_3`.
    conflict_fix_attempts: int = 0


# --------------------------- repo verification ---------------------------


@dataclass
class CheckResult:
    """One pass/fail check inside a VerificationResult."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerificationResult:
    """Outcome of verifying a target repo against the orchestrator's policy.

    Built by `repo_verify.verify()`; persisted as a `VerifiedRepo` row on
    success. `passed` is True iff every check in `checks` passed.

    Three signal channels, deliberately separate:
      - ``checks``  — pass/fail policy items. Any failure blocks.
      - ``warnings`` — things to fix (CI gating with no required reviewers,
                       required signed commits, etc.). Non-blocking.
      - ``notes``    — POSITIVE informational items about a repo's setup
                       (e.g., "CODEOWNERS present — humans will gate merges,
                       reviewer agent pre-screens"). Non-blocking, not a
                       warning — these describe how the orchestrator will
                       interact with the repo, not problems with it.
    """

    repo_url: str
    default_branch: str = ""
    auth_mode: str = ""  # 'pat' | 'app'
    auth_identity: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failure_summary(self) -> str:
        failed = [c for c in self.checks if not c.passed]
        if not failed:
            return ""
        lines = [f"  ✗ {c.name}" + (f": {c.detail}" if c.detail else "") for c in failed]
        return "\n".join(lines)


@dataclass
class VerifiedRepo:
    """A cached verification snapshot, persisted in state.verified_repos.

    Considered fresh while `verified_at` is within `state.VERIFY_TTL_HOURS`
    of now. After that, the orchestrator re-verifies on next access.
    """

    repo_url: str
    default_branch: str
    auth_mode: str
    auth_identity: str
    verified_at: str
    has_branch_protection: bool = False
    required_approvals: int = 0
    blocks_force_push: bool = False
    blocks_deletion: bool = False
    blocks_bypass: bool = False
    has_codeowners: bool = False
    requires_signed_commits: bool = False
    warnings_json: str = "[]"
