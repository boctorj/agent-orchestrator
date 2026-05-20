"""Shared infrastructure for MCP tool modules.

Holds the FastMCP instance, the marker regexes (parsed from agent responses),
task-composition templates, and small helpers.

Tools register themselves by decorating with `@mcp.tool()`. The entry point
in `orchestrator.mcp_server` imports every submodule under `tools/` to trigger
those registrations before calling `mcp.run()`.
"""

from __future__ import annotations

import json
import re

from mcp.server.fastmcp import FastMCP

from orchestrator import github, github_app, repo_verify, state
from orchestrator.blocked_reasons import BlockedPayload
from orchestrator.models import Feature, WorkUnit

# --- the FastMCP instance every tool module imports ---
mcp = FastMCP("orchestrator")

# --- caps ---
CAP_3 = 3
"""Max shared cycles (tester-bug + reviewer-change combined) per unit."""

# --- marker regexes (agents emit these as final-line sentinels) ---
PR_URL_RE = re.compile(r"PR_URL:\s*(https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+))", re.IGNORECASE)
BLOCKED_RE = re.compile(r"^BLOCKED:\s*(.+)$", re.MULTILINE)
"""Legacy line-only matcher; structured parsing lives in
:func:`orchestrator.blocked_reasons.parse_blocked_marker`. Kept here so existing
import sites (and tests) continue to work."""
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
#
# The compose_*_task helpers build the initial worker task message for the
# coder / tester / reviewer roles. Each accepts optional context blocks
# defined in docs/PROPOSAL-feature-spec-and-headless-daemon.md
# § "Role prompt changes":
#
#   | block                        | when                                   |
#   | ## FEATURE SPEC              | always (when spec.md exists)           |
#   | ## PREDECESSOR UNITS         | when deps exist AND their summaries    |
#   |                              |   are non-empty                        |
#   | ## THIS UNIT'S CYCLE LOG     | reviewer, retry cycle >= 2             |
#
# Read-side is graceful: missing files yield empty strings here and the
# block silently drops out. Callers in execution.py read the files via
# orchestrator.feature_spec.read_spec / orchestrator.cycle_log.cycle_log_summary
# / orchestrator.cycle_log.read_cycle_log; this module just renders.


# Strip the leading ``# F-XXX-U-N — title`` H1 line from a cycle-log body
# before nesting it under a ``### <uid>`` predecessor wrapper or a
# ``## THIS UNIT'S CYCLE LOG`` own-log wrapper. The H1 duplicates the unit
# identifier the wrapper already carries, and as h1 it outranks the
# wrapper in the markdown outline (cosmetic but worth fixing — PR #44 N2
# finding). Idempotent: a body without a leading H1 passes through.
_LEADING_H1_RE = re.compile(r"\A#\s[^\n]*\n+")


def _strip_leading_h1(body: str) -> str:
    return _LEADING_H1_RE.sub("", body, count=1)


def _render_context_blocks(
    *,
    feature_spec_text: str = "",
    predecessor_logs: list[tuple[str, str]] | None = None,
    own_cycle_log: str = "",
) -> str:
    """Render the proposal's three optional context blocks as a single string.

    Returns ``""`` when nothing to render — the splice site can append the
    return value unconditionally. Each non-empty input becomes one ``##``
    block; predecessor logs nest under ``### <unit_id>`` sub-headings.
    Empty predecessor summaries are dropped individually, so the
    ``## PREDECESSOR UNITS`` heading itself disappears if every dep's log
    happens to be missing.

    Predecessor summaries and the own-cycle-log body have their leading
    ``# `` H1 stripped before wrapping so the embedded headings don't
    outrank the wrappers above them (see ``_strip_leading_h1``).

    The return value ends with exactly ``\\n\\n`` (one blank line) so the
    template's ``{feature.description}\\n\\n{context}Follow ...`` spacing
    produces a single blank line between the inserted blocks and the
    closing instruction — both when the context is empty and when it isn't.
    """
    blocks: list[str] = []
    if feature_spec_text.strip():
        blocks.append(f"## FEATURE SPEC\n\n{feature_spec_text.rstrip()}")
    if predecessor_logs:
        kept = [(uid, _strip_leading_h1(s).rstrip()) for uid, s in predecessor_logs if s.strip()]
        kept = [(uid, body) for uid, body in kept if body]
        if kept:
            parts = ["## PREDECESSOR UNITS"]
            for uid, body in kept:
                parts.append(f"\n\n### {uid}\n\n{body}")
            blocks.append("".join(parts))
    if own_cycle_log.strip():
        own_body = _strip_leading_h1(own_cycle_log).rstrip()
        if own_body:
            blocks.append(f"## THIS UNIT'S CYCLE LOG\n\n{own_body}")
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n\n"


def compose_coder_task(
    feature: Feature,
    unit: WorkUnit,
    branch: str,
    github_token: str,
    *,
    feature_spec_text: str = "",
    predecessor_logs: list[tuple[str, str]] | None = None,
) -> str:
    context = _render_context_blocks(
        feature_spec_text=feature_spec_text, predecessor_logs=predecessor_logs
    )
    return f"""Implement work unit {unit.id} for feature {feature.id}.

REPO_URL: {feature.repo_path}
BRANCH:   {branch}
GH_TOKEN: {github_token}

UNIT TITLE: {unit.title}

UNIT DESCRIPTION:
{unit.description}

FEATURE CONTEXT (parent feature this unit belongs to):
{feature.description}

{context}Follow your standard workflow (see system prompt). End with `PR_URL: <url>`
or `BLOCKED: <reason>` on the last line.
"""


def compose_tester_task(
    feature: Feature,
    unit: WorkUnit,
    branch: str,
    pr_number: int,
    github_token: str,
    *,
    feature_spec_text: str = "",
    predecessor_logs: list[tuple[str, str]] | None = None,
) -> str:
    context = _render_context_blocks(
        feature_spec_text=feature_spec_text, predecessor_logs=predecessor_logs
    )
    return f"""Write tests for work unit {unit.id} which the coder has already
implemented and pushed to branch `{branch}`.

REPO_URL:  {feature.repo_path}
BRANCH:    {branch}
PR_NUMBER: {pr_number}
GH_TOKEN:  {github_token}

UNIT TITLE: {unit.title}

UNIT DESCRIPTION (what the implementation SHOULD do):
{unit.description}

FEATURE CONTEXT:
{feature.description}

{context}Follow your standard workflow (see system prompt). End with EXACTLY ONE of:
- `TESTS_PASS` (tests written + pushed, all green)
- `BUG_FOUND: <one-line bug summary>` (failing tests committed + inline
  review posted per system prompt; include the failing assertion + expected
  vs actual above this line)
- `BLOCKED: <one-line reason>` (can't even write/run tests)
"""


def compose_reviewer_task(
    feature: Feature,
    unit: WorkUnit,
    pr_number: int,
    github_token: str,
    *,
    feature_spec_text: str = "",
    predecessor_logs: list[tuple[str, str]] | None = None,
    own_cycle_log: str = "",
) -> str:
    context = _render_context_blocks(
        feature_spec_text=feature_spec_text,
        predecessor_logs=predecessor_logs,
        own_cycle_log=own_cycle_log,
    )
    return f"""Review PR #{pr_number} for work unit {unit.id}.

REPO_URL:  {feature.repo_path}
PR_NUMBER: {pr_number}
GH_TOKEN:  {github_token}

UNIT TITLE: {unit.title}

UNIT DESCRIPTION (intended behavior to validate against):
{unit.description}

FEATURE CONTEXT:
{feature.description}

{context}Follow your standard workflow (see system prompt). Post the review as
inline comments via `gh api .../pulls/N/reviews` (one call with
`comments[]`) and end with EXACTLY ONE of:
- `REVIEW_RECOMMEND_MERGE: <one-line reason>` (endorsing — clean PR, human merges)
- `REVIEW_REQUEST_CHANGES: <one-line main issue>` (any 🔴 or 🟠 finding — triggers fix-loop)
- `REVIEW_COMMENT` (only 🟡 / 🔵 nits/observations, not endorsing)
- `BLOCKED: <one-line reason>` (couldn't review)
"""


def compose_reviewer_delta_task(
    feature: Feature,
    unit: WorkUnit,
    pr_number: int,
    prior_sha: str,
    current_sha: str,
    prior_findings: str,
    fix_summary: str,
) -> str:
    """Delta-review resume message for an existing reviewer session.

    Sent via ``worker.resume(reviewer_session_id, ...)`` on retry (instead of a
    cold-start ``spawn_reviewer``). The session already holds the full PR
    inventory + prior verdict from the first turn, so this message scopes the
    work to:

      1. Re-diff only ``prior_sha..current_sha`` — skip the clone/inventory
         step the agent ran on its first turn.
      2. Reconcile each prior finding as RESOLVED / NOT_RESOLVED / N/A.
      3. Emit a fresh terminal marker for the *current* PR state.

    The companion "On delta re-review" section in ``prompts/reviewer.md``
    expands the contract (anti-anchoring guidance, reconciliation table format,
    when N/A is appropriate).
    """
    return f"""DELTA RE-REVIEW — PR #{pr_number} ({feature.title}), unit {unit.id}.

The coder pushed a fix in response to your prior REVIEW_REQUEST_CHANGES.
Reassess the current PR state and emit a fresh terminal marker.

PRIOR_SHA:   {prior_sha or "(unknown — diff from your last reviewed state)"}
CURRENT_SHA: {current_sha or "(unknown — fetch via gh pr view --json headRefOid)"}

PRIOR_FINDINGS (one-line summary from your last verdict):
{prior_findings or "(none recorded — consult your inline review comments on the PR)"}

CODER'S FIX SUMMARY (their reply to the fix loop):
{fix_summary or "(no summary — read PR comments for what they changed)"}

Follow the "On delta re-review" section of your system prompt:
  - SKIP the full clone/inventory step (1 in The Method) — your session
    already has the PR loaded; just `git fetch` and diff the new range.
  - DIFF ONLY `{prior_sha or "PRIOR_SHA"}..{current_sha or "CURRENT_SHA"}`
    rather than the whole PR; the rest is the same code you already reviewed.
  - RECONCILE each prior finding as RESOLVED, NOT_RESOLVED, or N/A
    (with a one-line justification per item).
  - WATCH FOR ANCHORING: don't auto-endorse just because the coder pushed,
    and don't dig in just to preserve your prior verdict. Vote the code.
  - EMIT A FRESH terminal marker for the current state. The orchestrator
    treats *this* marker as the new verdict — silence on the marker line
    locks the cap-3 loop. End with EXACTLY ONE of:
      - `REVIEW_RECOMMEND_MERGE: <reason>` (all prior findings resolved, no new ones)
      - `REVIEW_REQUEST_CHANGES: <main issue>` (any prior 🔴/🟠 still open, or a new one)
      - `REVIEW_COMMENT` (only 🟡/🔵 left)
      - `BLOCKED: <reason>` (couldn't reassess)
"""


def compose_fix_task(
    feature: Feature,
    unit: WorkUnit,
    branch: str,
    pr_number: int,
    source: str,
    feedback: str,
) -> str:
    return f"""You have feedback to address on your existing work for unit {unit.id}.

REPO_URL:  {feature.repo_path}
BRANCH:    {branch} (your branch, already checked out from your previous turn)
PR_NUMBER: {pr_number}
SOURCE:    {source}

FEEDBACK (orchestrator summary — actionable detail lives in PR comments):
{feedback}

Follow the source-specific fix-loop flow in your system prompt
(`## When resumed with feedback`). For `reviewer` / `tester` / `human`
sources, the source of truth is the inline review comments on PR #{pr_number} —
fetch them, address each in code, then **reply inline** to each thread with
what you did. For `ci`, the FEEDBACK above is the full context (no inline
anchors possible).

End your response with `FIX_PUSHED` on its own line, OR a structured
`BLOCKED:` line if you couldn't apply the fix.
"""


# --- BLOCKED-marker helpers (see orchestrator.blocked_reasons) ---


def format_blocked_last_error(payload: BlockedPayload) -> str:
    """One-line human-readable form for ``WorkUnitState.last_error``.

    Includes the reason slug in square brackets so the dashboard /
    next_ready_units listing surfaces the classification immediately even
    when the prose is long.
    """
    return f"BLOCKED [{payload.reason}]: {payload.prose}"


def blocked_event_details(payload: BlockedPayload, response_tail: str) -> str:
    """JSON-encoded ``details`` value for a BLOCKED unit_event.

    The orchestrator stores BLOCKED events with the structured payload + the
    truncated worker response together. Downstream consumers (dashboard,
    escalation summaries, ntfy bodies) can ``json.loads`` this to lift the
    reason slug and structured fields without re-parsing the prose.
    """
    out = payload.to_event_payload()
    out["response_tail"] = response_tail
    return json.dumps(out)


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


def safe_submit_pr_review(repo_url: str, pr_number: int, body: str, event: str = "COMMENT") -> str:
    try:
        github.submit_pr_review(repo_url, pr_number, body, event=event)
        return ""
    except Exception as e:  # noqa: BLE001 — best-effort
        return f"WARN: submit PR review failed: {e}"


def safe_dismiss_own_change_requests(repo_url: str, pr_number: int, message: str) -> int:
    try:
        return github.dismiss_own_change_requests(repo_url, pr_number, message)
    except Exception:  # noqa: BLE001 — best-effort
        return 0
