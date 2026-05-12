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
    "done",  # merged (by human) or otherwise complete
    "escalated",  # cap-3 hit or hard error; awaits human
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
TERMINAL_UNIT_STATUSES: frozenset[str] = frozenset({"done", "escalated"})


@dataclass
class Feature:
    id: str
    title: str
    description: str
    repo_path: str = ""  # URL like https://github.com/owner/repo for Managed Agents
    branch_prefix: str = ""
    status: FeatureStatus = "draft"
    created_at: str = ""


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
    """Per-unit runtime state, persisted to the work_units table."""

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
