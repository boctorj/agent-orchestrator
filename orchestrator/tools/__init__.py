"""Shared infrastructure for MCP tool modules.

Holds the FastMCP instance, the marker regexes (parsed from agent responses),
task-composition templates, and small helpers.

Tools register themselves by decorating with `@mcp.tool()`. The entry point
in `orchestrator.mcp_server` imports every submodule under `tools/` to trigger
those registrations before calling `mcp.run()`.
"""

from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from orchestrator import github, github_app, repo_verify, state
from orchestrator.models import Feature, WorkUnit

# --- the FastMCP instance every tool module imports ---
mcp = FastMCP("orchestrator")

# --- caps ---
CAP_3 = 3
"""Max shared cycles (tester-bug + reviewer-change combined) per unit."""

# --- marker regexes (agents emit these as final-line sentinels) ---
PR_URL_RE = re.compile(r"PR_URL:\s*(https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+))", re.IGNORECASE)
BLOCKED_RE = re.compile(r"^BLOCKED:\s*(.+)$", re.MULTILINE)
TESTS_PASS_RE = re.compile(r"^TESTS_PASS\s*$", re.MULTILINE)
BUG_FOUND_RE = re.compile(r"^BUG_FOUND:\s*(.+)$", re.MULTILINE)
REVIEW_APPROVED_RE = re.compile(r"^REVIEW_APPROVED\s*$", re.MULTILINE)
REVIEW_CHANGES_RE = re.compile(r"^REVIEW_REQUEST_CHANGES:\s*(.+)$", re.MULTILINE)
REVIEW_COMMENT_RE = re.compile(r"^REVIEW_COMMENT\s*$", re.MULTILINE)
REVIEW_RECOMMEND_MERGE_RE = re.compile(r"^REVIEW_RECOMMEND_MERGE:\s*(.+)$", re.MULTILINE)
FIX_PUSHED_RE = re.compile(r"\bFIX_PUSHED\b")


# --- pure helpers ---


def branch_for(feature: Feature, unit: WorkUnit) -> str:
    """Branch name from feature.branch_prefix + unit_id suffix."""
    if feature.branch_prefix:
        tail = unit.id.rsplit("-", 1)[-1]
        return f"{feature.branch_prefix}-u-{tail}"
    return unit.id.lower()


def tail(text: str, n: int = 800) -> str:
    """Last n chars of `text`, with an ellipsis prefix if truncated."""
    return text if len(text) <= n else "...\n" + text[-n:]


def need_github_token() -> str | None:
    """Return an ERROR string if no GitHub auth is configured, else None.

    Accepts either GitHub App credentials (preferred) or a GITHUB_TOKEN PAT.
    Name kept for backward compatibility with existing call sites; despite
    the name it now covers App auth too.
    """
    if github_app.is_app_configured():
        return None
    import os

    if not os.getenv("GITHUB_TOKEN", "").strip():
        return (
            "ERROR: no GitHub auth configured — set GITHUB_APP_ID + "
            "GITHUB_APP_INSTALLATION_ID + GITHUB_APP_PRIVATE_KEY_PATH "
            "(recommended), or GITHUB_TOKEN (PAT fallback)."
        )
    return None


def get_agent_token() -> str:
    """Return the token to inject into a worker agent's task message.

    App installation token (1-hour) if configured; PAT fallback otherwise.
    """
    return github_app.get_agent_token()


# --- repo-verification gate ---
#
# Every spawn against a target repo must pass through one of these gates.
# If the repo isn't fresh-verified (cache hit within state.VERIFY_TTL_HOURS),
# the gate returns an ERROR string and the MCP tool must NOT proceed.
#
# Rationale: defense-in-depth alongside branch protection + the
# no-merge-tool guarantee. Verification can drift (someone disables branch
# protection mid-feature), so we re-check at each spawn rather than
# trusting a one-time setup check.


def ensure_verified_for_feature(feature_id: str) -> str | None:
    """Return None if the feature's target repo is fresh-verified, else ERROR str.

    Allows when:
      - Feature has no repo_path set (smoke tests, dev features)
      - Repo has a fresh row in state.verified_repos (within TTL)

    Blocks (returns ERROR str) when:
      - Feature not found
      - Feature has a repo_path but it's malformed
      - Repo has never been verified
      - Repo's last verification is stale (older than VERIFY_TTL_HOURS)

    The returned string is meant to be surfaced verbatim by the calling
    MCP tool; it includes the exact `verify_repo(...)` call the user should
    run to unblock.
    """
    feature = state.get_feature(feature_id)
    if not feature:
        # Let the spawn surface "feature not found" with its own canonical
        # error message — verification gate concerns itself only with repos.
        return None
    if not feature.repo_path:
        return None  # no repo configured → smoke test / dev mode; allow
    try:
        normalized = repo_verify.normalize_repo_url(feature.repo_path)
    except ValueError as e:
        return f"ERROR: feature {feature_id} has invalid repo_path {feature.repo_path!r}: {e}"
    fresh = state.get_fresh_verified_repo(normalized)
    if fresh is not None:
        return None  # all clear
    return (
        f"ERROR: target repo {normalized} is not verified "
        f"(or its verification has expired; TTL is {state.VERIFY_TTL_HOURS}h).\n"
        f"Spawning blocked to enforce the orchestrator's branch-protection policy.\n"
        f"\n"
        f"Fix:\n"
        f"  1. Ensure branch protection is set on {normalized}'s default branch.\n"
        f"  2. Call `verify_repo({normalized!r})` to refresh the cache.\n"
        f"  3. Retry this spawn."
    )


def ensure_verified_for_unit(unit_id: str) -> str | None:
    """Like ensure_verified_for_feature but takes a unit_id (looks up feature)."""
    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for unit {unit_id}"
    return ensure_verified_for_feature(unit_state.feature_id)


# --- task-composition templates ---


def compose_coder_task(feature: Feature, unit: WorkUnit, branch: str, github_token: str) -> str:
    return f"""Implement work unit {unit.id} for feature {feature.id}.

REPO_URL: {feature.repo_path}
BRANCH:   {branch}
GH_TOKEN: {github_token}

UNIT TITLE: {unit.title}

UNIT DESCRIPTION:
{unit.description}

FEATURE CONTEXT (parent feature this unit belongs to):
{feature.description}

Follow your standard workflow (see system prompt). End with `PR_URL: <url>`
or `BLOCKED: <reason>` on the last line.
"""


def compose_tester_task(feature: Feature, unit: WorkUnit, branch: str, github_token: str) -> str:
    return f"""Write tests for work unit {unit.id} which the coder has already
implemented and pushed to branch `{branch}`.

REPO_URL: {feature.repo_path}
BRANCH:   {branch}
GH_TOKEN: {github_token}

UNIT TITLE: {unit.title}

UNIT DESCRIPTION (what the implementation SHOULD do):
{unit.description}

FEATURE CONTEXT:
{feature.description}

Follow your standard workflow (see system prompt). End with EXACTLY ONE of:
- `TESTS_PASS` (tests written + pushed, all green)
- `BUG_FOUND: <one-line bug summary>` (tests reveal an implementation bug;
  include the failing assertion + expected vs actual above this line)
- `BLOCKED: <one-line reason>` (can't even write/run tests)
"""


def compose_reviewer_task(
    feature: Feature, unit: WorkUnit, pr_number: int, github_token: str
) -> str:
    return f"""Review PR #{pr_number} for work unit {unit.id}.

REPO_URL:  {feature.repo_path}
PR_NUMBER: {pr_number}
GH_TOKEN:  {github_token}

UNIT TITLE: {unit.title}

UNIT DESCRIPTION (intended behavior to validate against):
{unit.description}

FEATURE CONTEXT:
{feature.description}

Follow your standard workflow (see system prompt). Post the review via
`gh pr review` and end with EXACTLY ONE of:
- `REVIEW_APPROVED`
- `REVIEW_RECOMMEND_MERGE: <one-line reason>` (when self-approval blocked)
- `REVIEW_REQUEST_CHANGES: <one-line main issue>`
- `REVIEW_COMMENT`
- `BLOCKED: <one-line reason>`
"""


def compose_fix_task(
    feature: Feature, unit: WorkUnit, branch: str, source: str, feedback: str
) -> str:
    return f"""You have feedback to address on your existing work for unit {unit.id}.
Source of feedback: {source}.

REPO_URL: {feature.repo_path}
BRANCH:   {branch} (your branch, already checked out from your previous turn)

FEEDBACK:
{feedback}

Make the smallest fix that addresses the feedback. Don't refactor unrelated
code. Run the existing tests (committed by tester) and confirm they pass
locally before pushing. Then `git add` only the files you changed, commit
with a one-line message referencing what was fixed, and push to the same
branch.

End your response with `FIX_PUSHED` on its own line, OR `BLOCKED: <reason>`
if you couldn't apply the fix.
"""


# --- side-effect helpers (best-effort: never raise) ---


def safe_amend_pr_body(repo_url: str, pr_number: int, text: str) -> str:
    try:
        github.amend_pr_body(repo_url, pr_number, text)
        return ""
    except Exception as e:  # noqa: BLE001 — best-effort, log via return
        return f"WARN: amend PR body failed: {e}"


def safe_comment_pr(repo_url: str, pr_number: int, body: str) -> str:
    try:
        github.post_pr_comment(repo_url, pr_number, body)
        return ""
    except Exception as e:  # noqa: BLE001 — best-effort
        return f"WARN: post PR comment failed: {e}"
