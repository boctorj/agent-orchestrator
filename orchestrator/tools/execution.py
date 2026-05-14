"""Execution MCP tools: spawn coder/tester/reviewer, address feedback, run full cycle.

The `cycle_review` orchestration is broken into three private helper functions
— `_tester_phase`, `_copilot_phase`, `_reviewer_phase` — to keep the main flow
linear and each phase independently testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from orchestrator import ci_wait, github, ntfy, state
from orchestrator.agents import ManagedAgentWorker
from orchestrator.blocked_reasons import parse_blocked_marker
from orchestrator.models import WorkUnitState
from orchestrator.tools import (
    BUG_FOUND_RE,
    CAP_3,
    FIX_PUSHED_RE,
    PR_URL_RE,
    REVIEW_APPROVED_RE,
    REVIEW_CHANGES_RE,
    REVIEW_COMMENT_RE,
    REVIEW_RECOMMEND_MERGE_RE,
    TESTS_PASS_RE,
    blocked_event_details,
    branch_for,
    compose_coder_task,
    compose_fix_task,
    compose_reviewer_task,
    compose_tester_task,
    ensure_verified_for_feature,
    ensure_verified_for_unit,
    format_blocked_last_error,
    get_agent_token,
    mcp,
    need_github_token,
    safe_amend_pr_body,
    safe_comment_pr,
    tail,
)

# --------------------------- spawn_unit (coder) ---------------------------


@mcp.tool()
def spawn_unit(feature_id: str, unit_id: str) -> str:
    """Spawn a coder Managed Agent for one work unit. BLOCKS for minutes.

    Preconditions: feature loaded, plan saved + approved, GITHUB_TOKEN set,
    feature.repo_path is a GitHub URL, and the repo passed verification
    within the last 24h (call `verify_repo(<url>)` first if not).
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    feature = state.get_feature(feature_id)
    if not feature:
        return f"ERROR: feature {feature_id} not found"
    if feature.status != "approved":
        return f"ERROR: feature {feature_id} status is '{feature.status}' — must be 'approved'."
    if not feature.repo_path:
        return f"ERROR: feature {feature_id} has no repo_path."

    plan = state.get_plan(feature_id)
    if not plan:
        return f"ERROR: no plan for {feature_id}"

    unit = next((u for u in plan.units if u.id == unit_id), None)
    if not unit:
        return f"ERROR: unit {unit_id} not in plan for {feature_id}"

    existing = state.get_unit_state(unit_id)
    if existing and existing.coder_session_id:
        return (
            f"ERROR: unit {unit_id} already has coder session {existing.coder_session_id}. "
            f"Use cycle_review or address_review to advance it."
        )

    if err := need_github_token():
        return err
    github_token = get_agent_token()

    branch = branch_for(feature, unit)
    task = compose_coder_task(feature, unit, branch, github_token)

    state.upsert_unit_state(
        WorkUnitState(unit_id=unit_id, feature_id=feature_id, status="coding", branch=branch)
    )
    state.record_event(
        unit_id,
        feature_id,
        "spawn_coder",
        source="orchestrator",
        cycle_number=0,
        summary=f"Spawning coder for {unit_id}",
    )

    try:
        worker = ManagedAgentWorker(role="coder")
        session_id, response = worker.spawn(task, title=f"{unit_id}: {unit.title}")
    except Exception as e:  # noqa: BLE001 — surface as orchestrator error
        state.touch_unit(unit_id, status="escalated", error=str(e))
        state.record_event(
            unit_id,
            feature_id,
            "coder_error",
            source="orchestrator",
            cycle_number=0,
            summary=str(e),
        )
        return f"ERROR spawning coder for {unit_id}: {e}"

    pr_match = PR_URL_RE.search(response)
    blocked_payload = parse_blocked_marker(response)

    if pr_match:
        pr_url = pr_match.group(1)
        pr_number = int(pr_match.group(2))
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=unit_id,
                feature_id=feature_id,
                status="in_ci",
                branch=branch,
                pr_number=pr_number,
                coder_session_id=session_id,
            )
        )
        amend_warn = safe_amend_pr_body(
            feature.repo_path,
            pr_number,
            f"_Coder session: `{session_id}` · unit {unit_id}_",
        )
        state.record_event(
            unit_id,
            feature_id,
            "pr_opened",
            source="coder",
            cycle_number=0,
            summary=f"PR #{pr_number} opened",
            session_id=session_id,
            details=pr_url,
        )
        result: dict[str, Any] = {
            "unit_id": unit_id,
            "session_id": session_id,
            "branch": branch,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "summary": tail(response),
        }
        if amend_warn:
            result["amend_warning"] = amend_warn
        return json.dumps(result, indent=2)

    if blocked_payload is not None:
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=unit_id,
                feature_id=feature_id,
                status="escalated",
                branch=branch,
                coder_session_id=session_id,
                last_error=format_blocked_last_error(blocked_payload),
            )
        )
        state.record_event(
            unit_id,
            feature_id,
            "coder_blocked",
            source="coder",
            cycle_number=0,
            summary=blocked_payload.prose,
            session_id=session_id,
            details=blocked_event_details(blocked_payload, tail(response)),
        )
        ntfy.push_escalation(
            unit_id, f"coder blocked [{blocked_payload.reason}]: {blocked_payload.prose}"
        )
        return (
            f"BLOCKED — unit {unit_id} escalated.\n"
            f"Reason code: {blocked_payload.reason}\n"
            f"Reason: {blocked_payload.prose}\n"
            f"Session: {session_id}\nLast output:\n{tail(response)}"
        )

    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="escalated",
            branch=branch,
            coder_session_id=session_id,
            last_error="Coder finished without PR_URL or BLOCKED marker",
        )
    )
    state.record_event(
        unit_id,
        feature_id,
        "coder_no_marker",
        source="orchestrator",
        cycle_number=0,
        summary="No PR_URL or BLOCKED",
        session_id=session_id,
        details=tail(response),
    )
    ntfy.push_escalation(unit_id, "coder emitted no PR_URL or BLOCKED marker")
    return (
        f"ESCALATED — coder did not emit a clear marker.\n"
        f"Session: {session_id}\nLast output:\n{tail(response)}"
    )


# --------------------------- spawn_tester ---------------------------


@mcp.tool()
def spawn_tester(feature_id: str, unit_id: str) -> str:
    """Spawn a tester Managed Agent for a unit whose coder has opened a PR.

    Tester writes tests on the coder's branch, runs them, signals
    TESTS_PASS / BUG_FOUND / BLOCKED. BLOCKS for minutes.

    Refuses to spawn if CI is currently failing on the PR — testing a
    red PR is wasted work. Use `cycle_review` for the automated CI-fix
    loop, or `send_to_unit` as an escape hatch.

    Repo must be fresh-verified (call `verify_repo(<url>)` if blocked).
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    if err := _check_ci_or_refuse(feature_id, unit_id, label="tester"):
        return err

    feature = state.get_feature(feature_id)
    if not feature:
        return f"ERROR: feature {feature_id} not found"
    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for unit {unit_id} — spawn coder first"
    if not unit_state.branch or not unit_state.pr_number:
        return f"ERROR: unit {unit_id} has no branch/PR yet"
    if unit_state.tester_session_id:
        return f"ERROR: tester session already exists for {unit_id}. Use address_review to send follow-up."

    plan = state.get_plan(feature_id)
    unit = next((u for u in plan.units if u.id == unit_id), None) if plan else None
    if not unit:
        return f"ERROR: unit {unit_id} not in plan"

    if err := need_github_token():
        return err
    github_token = get_agent_token()

    task = compose_tester_task(feature, unit, unit_state.branch, unit_state.pr_number, github_token)
    state.touch_unit(unit_id, status="testing")
    state.record_event(
        unit_id,
        feature_id,
        "spawn_tester",
        source="orchestrator",
        cycle_number=unit_state.review_round,
        summary=f"Spawning tester for {unit_id}",
    )

    try:
        worker = ManagedAgentWorker(role="tester")
        session_id, response = worker.spawn(task, title=f"tester {unit_id}")
    except Exception as e:  # noqa: BLE001
        state.touch_unit(unit_id, status="escalated", error=str(e))
        state.record_event(
            unit_id,
            feature_id,
            "tester_error",
            source="orchestrator",
            cycle_number=unit_state.review_round,
            summary=str(e),
        )
        return f"ERROR spawning tester: {e}"

    unit_state.tester_session_id = session_id
    state.upsert_unit_state(unit_state)

    if TESTS_PASS_RE.search(response):
        state.touch_unit(unit_id, status="in_ci")
        state.record_event(
            unit_id,
            feature_id,
            "tests_pass",
            source="tester",
            cycle_number=unit_state.review_round,
            summary="All tests pass",
            session_id=session_id,
        )
        safe_comment_pr(
            feature.repo_path,
            unit_state.pr_number,
            f"🤖 **Tester:** all tests pass. _Session: `{session_id}`_",
        )
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "TESTS_PASS",
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    bug = BUG_FOUND_RE.search(response)
    if bug:
        reason = bug.group(1).strip()
        state.record_event(
            unit_id,
            feature_id,
            "tester_bug_found",
            source="tester",
            cycle_number=unit_state.review_round,
            summary=reason,
            session_id=session_id,
            details=tail(response),
        )
        safe_comment_pr(
            feature.repo_path,
            unit_state.pr_number,
            f"🤖 **Tester found a bug:** {reason}\n\n"
            f"_Failing tests committed. Orchestrator will resume coder._\n"
            f"_Session: `{session_id}`_",
        )
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "BUG_FOUND",
                "bug": reason,
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    blocked_payload = parse_blocked_marker(response)
    if blocked_payload is not None:
        state.touch_unit(
            unit_id, status="escalated", error=format_blocked_last_error(blocked_payload)
        )
        state.record_event(
            unit_id,
            feature_id,
            "tester_blocked",
            source="tester",
            cycle_number=unit_state.review_round,
            summary=blocked_payload.prose,
            session_id=session_id,
            details=blocked_event_details(blocked_payload, tail(response)),
        )
        safe_comment_pr(
            feature.repo_path,
            unit_state.pr_number,
            f"🚨 **Tester BLOCKED [{blocked_payload.reason}]:** {blocked_payload.prose}\n"
            f"_Escalated to human._",
        )
        return f"BLOCKED — tester for {unit_id} [{blocked_payload.reason}]: {blocked_payload.prose}"

    return _escalate_no_marker(
        unit_id=unit_id,
        feature_id=feature_id,
        role="tester",
        cycle_number=unit_state.review_round,
        session_id=session_id,
        response=response,
    )


# --------------------------- spawn_reviewer ---------------------------


@mcp.tool()
def spawn_reviewer(feature_id: str, unit_id: str) -> str:
    """Spawn a reviewer Managed Agent for a unit's PR.

    Reviewer is read-only. Posts review via `gh pr review`. Signals
    REVIEW_APPROVED / REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES /
    REVIEW_COMMENT / BLOCKED. BLOCKS for minutes.

    Refuses to spawn if CI is currently failing — reviewing a red PR
    duplicates effort the reviewer would otherwise spend critiquing the
    same failures. Use `cycle_review` for the automated CI-fix loop.

    Repo must be fresh-verified (call `verify_repo(<url>)` if blocked).
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    if err := _check_ci_or_refuse(feature_id, unit_id, label="reviewer"):
        return err

    feature = state.get_feature(feature_id)
    if not feature:
        return f"ERROR: feature {feature_id} not found"
    unit_state = state.get_unit_state(unit_id)
    if not unit_state or not unit_state.pr_number:
        return f"ERROR: unit {unit_id} has no PR yet"
    if unit_state.reviewer_session_id:
        return f"ERROR: reviewer session already exists for {unit_id}"

    plan = state.get_plan(feature_id)
    unit = next((u for u in plan.units if u.id == unit_id), None) if plan else None
    if not unit:
        return f"ERROR: unit {unit_id} not in plan"

    if err := need_github_token():
        return err
    github_token = get_agent_token()

    task = compose_reviewer_task(feature, unit, unit_state.pr_number, github_token)
    state.touch_unit(unit_id, status="reviewing")
    state.record_event(
        unit_id,
        feature_id,
        "spawn_reviewer",
        source="orchestrator",
        cycle_number=unit_state.review_round,
        summary=f"Spawning reviewer for {unit_id}",
    )

    try:
        worker = ManagedAgentWorker(role="reviewer")
        session_id, response = worker.spawn(task, title=f"reviewer {unit_id}")
    except Exception as e:  # noqa: BLE001
        state.touch_unit(unit_id, status="escalated", error=str(e))
        state.record_event(
            unit_id,
            feature_id,
            "reviewer_error",
            source="orchestrator",
            cycle_number=unit_state.review_round,
            summary=str(e),
        )
        return f"ERROR spawning reviewer: {e}"

    unit_state.reviewer_session_id = session_id
    state.upsert_unit_state(unit_state)

    if REVIEW_APPROVED_RE.search(response):
        state.touch_unit(unit_id, status="in_ci")
        state.record_event(
            unit_id,
            feature_id,
            "reviewer_approved",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary="Approved",
            session_id=session_id,
        )
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "REVIEW_APPROVED",
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    recommend = REVIEW_RECOMMEND_MERGE_RE.search(response)
    if recommend:
        reason = recommend.group(1).strip()
        state.touch_unit(unit_id, status="in_ci")
        state.record_event(
            unit_id,
            feature_id,
            "reviewer_recommend_merge",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary=f"Endorsed (self-approval blocked): {reason}",
            session_id=session_id,
        )
        safe_comment_pr(
            feature.repo_path,
            unit_state.pr_number,
            f"🤖 **Reviewer endorsed for merge** (self-approval blocked, posted as comment).\n\n"
            f"_{reason}_\n_Session: `{session_id}`_",
        )
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "REVIEW_RECOMMEND_MERGE",
                "reason": reason,
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    changes = REVIEW_CHANGES_RE.search(response)
    if changes:
        reason = changes.group(1).strip()
        state.record_event(
            unit_id,
            feature_id,
            "reviewer_request_changes",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary=reason,
            session_id=session_id,
            details=tail(response),
        )
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "REVIEW_REQUEST_CHANGES",
                "issue": reason,
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    if REVIEW_COMMENT_RE.search(response):
        state.touch_unit(unit_id, status="in_ci")
        state.record_event(
            unit_id,
            feature_id,
            "reviewer_comment",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary="Comment-only review",
            session_id=session_id,
        )
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "REVIEW_COMMENT",
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    blocked_payload = parse_blocked_marker(response)
    if blocked_payload is not None:
        state.touch_unit(
            unit_id, status="escalated", error=format_blocked_last_error(blocked_payload)
        )
        state.record_event(
            unit_id,
            feature_id,
            "reviewer_blocked",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary=blocked_payload.prose,
            session_id=session_id,
            details=blocked_event_details(blocked_payload, tail(response)),
        )
        safe_comment_pr(
            feature.repo_path,
            unit_state.pr_number,
            f"🚨 **Reviewer BLOCKED [{blocked_payload.reason}]:** {blocked_payload.prose}\n"
            f"_Escalated to human._",
        )
        return (
            f"BLOCKED — reviewer for {unit_id} [{blocked_payload.reason}]: {blocked_payload.prose}"
        )

    return _escalate_no_marker(
        unit_id=unit_id,
        feature_id=feature_id,
        role="reviewer",
        cycle_number=unit_state.review_round,
        session_id=session_id,
        response=response,
    )


# --------------------------- address_review (coder resume) ---------------------------


@mcp.tool()
def address_review(unit_id: str, source: str, feedback: str) -> str:
    """Resume the coder session to address feedback (from tester/reviewer/ci/human).

    Increments review_round. BLOCKS for minutes.
    Returns coder's response — should end with FIX_PUSHED or BLOCKED.

    Repo must be fresh-verified (call `verify_repo(<url>)` if blocked).
    """
    if source not in ("tester", "reviewer", "ci", "human"):
        return f"ERROR: source must be tester|reviewer|ci|human, got {source!r}"

    if err := ensure_verified_for_unit(unit_id):
        return err

    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for unit {unit_id}"
    if not unit_state.coder_session_id:
        return f"ERROR: no coder session for {unit_id}"
    if not unit_state.pr_number:
        return f"ERROR: no PR for unit {unit_id} — spawn coder first"

    feature = state.get_feature(unit_state.feature_id)
    plan = state.get_plan(unit_state.feature_id)
    unit = next((u for u in plan.units if u.id == unit_id), None) if plan else None
    if not feature or not unit:
        return f"ERROR: feature/unit lookup failed for {unit_id}"

    round_num = state.increment_review_round(unit_id)
    state.touch_unit(unit_id, status="fixing")
    state.record_event(
        unit_id,
        unit_state.feature_id,
        "coder_resumed",
        source=source,
        cycle_number=round_num,
        summary=f"Address feedback from {source}",
        details=feedback[:1000],
    )

    fix_msg = compose_fix_task(
        feature, unit, unit_state.branch, unit_state.pr_number, source, feedback
    )
    try:
        worker = ManagedAgentWorker(role="coder")
        response = worker.resume(unit_state.coder_session_id, fix_msg)
    except Exception as e:  # noqa: BLE001
        state.touch_unit(unit_id, status="escalated", error=str(e))
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "coder_resume_error",
            source="orchestrator",
            cycle_number=round_num,
            summary=str(e),
        )
        return f"ERROR resuming coder: {e}"

    if FIX_PUSHED_RE.search(response):
        state.touch_unit(unit_id, status="in_ci")
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "fix_pushed",
            source="coder",
            cycle_number=round_num,
            summary="Fix committed and pushed",
            session_id=unit_state.coder_session_id,
            details=tail(response),
        )
        if unit_state.pr_number:
            safe_comment_pr(
                feature.repo_path,
                unit_state.pr_number,
                f"🤖 **Coder pushed fix** (cycle {round_num}) addressing {source} feedback.",
            )
        return json.dumps(
            {
                "unit_id": unit_id,
                "cycle": round_num,
                "outcome": "FIX_PUSHED",
                "summary": tail(response),
            },
            indent=2,
        )

    blocked_payload = parse_blocked_marker(response)
    if blocked_payload is not None:
        state.touch_unit(
            unit_id,
            status="escalated",
            error=f"Coder BLOCKED on fix [{blocked_payload.reason}]: {blocked_payload.prose}",
        )
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "coder_blocked_on_fix",
            source="coder",
            cycle_number=round_num,
            summary=blocked_payload.prose,
            session_id=unit_state.coder_session_id,
            details=blocked_event_details(blocked_payload, tail(response)),
        )
        return (
            f"BLOCKED — coder couldn't apply fix [{blocked_payload.reason}]: "
            f"{blocked_payload.prose}"
        )

    return _escalate_no_marker(
        unit_id=unit_id,
        feature_id=unit_state.feature_id,
        role="fix",
        cycle_number=round_num,
        session_id=unit_state.coder_session_id,
        response=response,
    )


# --------------------------- cycle_review (refactored) ---------------------------


@dataclass
class CycleContext:
    """Carrier object for cycle_review phase helpers."""

    feature_id: str
    unit_id: str
    history: list[dict]


def _record_step(ctx: CycleContext, name: str, result_json_str: str) -> dict:
    """Parse a tool's JSON result and append to history. Returns the parsed dict."""
    try:
        r = json.loads(result_json_str)
    except (json.JSONDecodeError, TypeError):
        r = {"outcome": "RAW", "raw": result_json_str}
    ctx.history.append({"step": name, "result": r})
    return r


def _pr_url_for(feature_id: str, unit_state: WorkUnitState | None) -> str | None:
    """Reconstruct a PR URL from feature.repo_path + unit_state.pr_number."""
    if not unit_state or not unit_state.pr_number:
        return None
    feature = state.get_feature(feature_id)
    if not feature:
        return None
    try:
        owner, repo = github.parse_repo_url(feature.repo_path)
    except ValueError:
        return None
    return f"https://github.com/{owner}/{repo}/pull/{unit_state.pr_number}"


def _emit_terminal(ctx: CycleContext, outcome: str, msg: str) -> str:
    """Final return value of cycle_review. Fires ntfy push as side effect."""
    unit_state = state.get_unit_state(ctx.unit_id)
    pr_url = _pr_url_for(ctx.feature_id, unit_state)

    if outcome == "escalated":
        ntfy.push_escalation(ctx.unit_id, msg, pr_url=pr_url)
    elif outcome == "approved_awaiting_merge":
        ntfy.push_ready_to_merge(ctx.unit_id, pr_url or "(no PR url)", summary=msg)

    final_state_json = get_unit_status(ctx.unit_id)
    try:
        final_state = json.loads(final_state_json)
    except json.JSONDecodeError:
        final_state = {"raw": final_state_json}

    return json.dumps(
        {
            "unit_id": ctx.unit_id,
            "outcome": outcome,
            "message": msg,
            "history": ctx.history,
            "final_state": final_state,
        },
        indent=2,
    )


# --------------------------- escalation helpers ---------------------------


def _escalate_no_marker(
    *,
    unit_id: str,
    feature_id: str,
    role: str,
    cycle_number: int,
    session_id: str,
    response: str,
) -> str:
    """Terminal fallback when a worker's response doesn't match any expected marker.

    Used by `spawn_tester` / `spawn_reviewer` / `address_review`. The
    `spawn_unit` (coder) variant differs structurally — it stores its
    session_id in the WorkUnitState here for the first time, and pushes
    a phone notification — so it stays inline.

    Marks the unit escalated, records a `<role>_no_marker` event with
    the truncated agent response, and returns an ERROR string the
    calling MCP tool propagates to the lead.
    """
    state.touch_unit(unit_id, status="escalated", error=f"{role.title()} emitted no marker")
    state.record_event(
        unit_id,
        feature_id,
        f"{role}_no_marker",
        source="orchestrator",
        cycle_number=cycle_number,
        summary="No marker emitted",
        session_id=session_id,
        details=tail(response),
    )
    return f"ESCALATED — {role} for {unit_id} emitted no marker.\nLast output:\n{tail(response)}"


# --------------------------- CI wait helpers ---------------------------


def _check_ci_or_refuse(feature_id: str, unit_id: str, *, label: str) -> str | None:
    """Standalone (non-cycle_review) CI gate.

    For `spawn_tester` / `spawn_reviewer` called directly: confirm CI is
    green RIGHT NOW. No fix loop — if CI is red or timed-out, return a
    structured ERROR string so the lead surfaces it to the user. The
    user can then either fix CI manually, run `cycle_review` for
    automated handling, or use `send_to_unit` as an escape hatch.

    Returns None on green (or no_ci pass-through); ERROR string otherwise.
    """
    unit_state = state.get_unit_state(unit_id)
    feature = state.get_feature(feature_id)
    if not (unit_state and feature and unit_state.pr_number and feature.repo_path):
        return None  # nothing to check yet; let the caller's own validation fire

    result = ci_wait.wait_for_ci(feature.repo_path, unit_state.pr_number)
    if result.status in ("green", "no_ci"):
        return None
    if result.status == "failed":
        names = ", ".join(r.get("name", "?") for r in result.failing_runs[:5])
        return (
            f"ERROR: refusing to spawn {label} — CI is failing on PR "
            f"#{unit_state.pr_number} ({names}). Fix CI first, or use "
            f"cycle_review() for the automated fix loop."
        )
    # timeout
    return (
        f"ERROR: refusing to spawn {label} — CI did not settle within "
        f"{result.elapsed_seconds:.0f}s on PR #{unit_state.pr_number}. "
        f"Retry once CI completes."
    )


def _wait_ci_with_fix_loop(ctx: CycleContext, label: str) -> tuple[bool, str | None]:
    """cycle_review-flavor CI gate with embedded fix loop.

    Wait for CI on the current PR. Behavior by outcome:
      - green / no_ci  → return (True, None); cycle_review proceeds.
      - failed         → invoke `address_review(source="ci", ...)`. On
                         FIX_PUSHED, loop back and re-wait on the new
                         head_sha. Counts toward CAP_3 each time.
      - timeout        → return (False, msg) — cycle_review escalates.
      - cap hit during CI-fix loop → return (False, msg).

    ``label`` describes which push we're gating on (e.g. "coder push",
    "tester push", "final pre-merge") for ctx.history breadcrumbs.
    """
    unit_state = state.get_unit_state(ctx.unit_id)
    feature = state.get_feature(ctx.feature_id)
    if not (unit_state and feature and unit_state.pr_number and feature.repo_path):
        # No PR to gate on — pass through. cycle_review will hit its own
        # "no PR" error inside the next phase.
        return True, None

    while True:
        result = ci_wait.wait_for_ci(feature.repo_path, unit_state.pr_number)
        ctx.history.append(
            {
                "step": f"ci_wait ({label})",
                "status": result.status,
                "summary": result.summary,
                "elapsed_seconds": result.elapsed_seconds,
            }
        )

        if result.status in ("green", "no_ci"):
            return True, None

        if result.status == "timeout":
            return False, f"CI timeout after {label}: {result.summary}"

        # status == "failed" — engage the fix loop
        current = state.get_unit_state(ctx.unit_id)
        if current is None or current.review_round >= CAP_3:
            return False, (
                f"cap of {CAP_3} cycles hit while fixing CI failure after {label}: {result.summary}"
            )

        details_urls = [
            r.get("details_url") for r in result.failing_runs[:3] if r.get("details_url")
        ]
        failing_names = [r.get("name", "?") for r in result.failing_runs[:5]]
        feedback = (
            f"CI is failing on the PR. Failing checks: {', '.join(failing_names)}.\n"
            f"Inspect each failing job's logs (`gh pr checks` or these URLs: "
            f"{details_urls}) and push a focused fix to the same branch."
        )

        fix_out = _record_step(
            ctx,
            f"address_review (ci) after {label}",
            address_review(ctx.unit_id, "ci", feedback),
        )
        if fix_out.get("outcome") != "FIX_PUSHED":
            return False, (
                f"coder fix for CI failure after {label} did not succeed: "
                f"{fix_out.get('outcome', 'unknown')}"
            )
        # Re-read PR state in case the fix updated head_sha; loop to re-poll CI
        unit_state = state.get_unit_state(ctx.unit_id) or unit_state


def _tester_phase(ctx: CycleContext) -> tuple[bool, str | None]:
    """Run tester until TESTS_PASS or escalation. Returns (passed, escalation_msg).

    Iterates the tester→address_review→retest loop, respecting CAP_3.
    After every coder fix push, waits for CI green before re-spawning the
    tester (the fix might break CI even if the tester would re-pass).
    """
    tester_out = _record_step(ctx, "tester", spawn_tester(ctx.feature_id, ctx.unit_id))
    outcome = tester_out.get("outcome")

    if isinstance(outcome, str) and outcome.startswith("BLOCKED"):
        return False, "tester blocked"

    while outcome == "BUG_FOUND":
        unit_state = state.get_unit_state(ctx.unit_id)
        if unit_state is None or unit_state.review_round >= CAP_3:
            return False, f"cap of {CAP_3} cycles hit while addressing tester bug"

        fix_out = _record_step(
            ctx,
            "address_review (tester bug)",
            address_review(ctx.unit_id, "tester", tester_out.get("bug", "")),
        )
        if fix_out.get("outcome") != "FIX_PUSHED":
            return False, "coder fix did not succeed"

        # Wait for CI on the fix push before re-running tester. If CI fails,
        # the helper loops on its own (more CI-fix cycles, all sharing CAP_3).
        ok, msg = _wait_ci_with_fix_loop(ctx, "tester-bug fix push")
        if not ok:
            return False, msg

        # Clear tester session so the retry creates a fresh one
        s = state.get_unit_state(ctx.unit_id)
        if s is None:
            return False, "unit state vanished mid-cycle"
        s.tester_session_id = ""
        state.upsert_unit_state(s)

        tester_out = _record_step(ctx, "tester (retry)", spawn_tester(ctx.feature_id, ctx.unit_id))
        outcome = tester_out.get("outcome")
        if isinstance(outcome, str) and outcome.startswith("BLOCKED"):
            return False, "tester blocked on retry"

    if outcome != "TESTS_PASS":
        return False, f"tester ended with unexpected outcome: {outcome}"

    return True, None


def _copilot_phase(ctx: CycleContext) -> None:
    """Request a Copilot review + wait up to 5 min. Best-effort, never fails the cycle."""
    unit_state = state.get_unit_state(ctx.unit_id)
    feature = state.get_feature(ctx.feature_id)
    if not (unit_state and unit_state.pr_number and feature):
        return

    req = github.request_copilot_review(feature.repo_path, unit_state.pr_number)
    ctx.history.append({"step": "copilot_request", "result": req})

    copilot_review = github.wait_for_copilot_review(
        feature.repo_path,
        unit_state.pr_number,
        timeout_seconds=300,
    )
    if copilot_review:
        state.record_event(
            ctx.unit_id,
            ctx.feature_id,
            "copilot_review_received",
            source="copilot",
            cycle_number=unit_state.review_round,
            summary=f"Copilot {copilot_review.get('state')} with "
            f"{copilot_review.get('inline_count', 0)} inline comments",
            details=(copilot_review.get("body") or "")[:1500],
        )
        ctx.history.append(
            {
                "step": "copilot_review",
                "outcome": "received",
                "state": copilot_review.get("state"),
                "inline_count": copilot_review.get("inline_count"),
            }
        )
    else:
        state.record_event(
            ctx.unit_id,
            ctx.feature_id,
            "copilot_review_timeout",
            source="copilot",
            cycle_number=unit_state.review_round,
            summary="No Copilot review within timeout — proceeding without it",
        )
        ctx.history.append({"step": "copilot_review", "outcome": "timeout"})


def _reviewer_phase(ctx: CycleContext) -> tuple[bool, str | None]:
    """Run reviewer until approved/recommend-merge/comment or escalation.

    Returns (approved, escalation_msg). Iterates address_review on
    REVIEW_REQUEST_CHANGES, respecting CAP_3. Each coder fix push waits
    for CI green before re-spawning the reviewer.
    """
    reviewer_out = _record_step(ctx, "reviewer", spawn_reviewer(ctx.feature_id, ctx.unit_id))
    outcome = reviewer_out.get("outcome")

    if isinstance(outcome, str) and outcome.startswith("BLOCKED"):
        return False, "reviewer blocked"

    while outcome == "REVIEW_REQUEST_CHANGES":
        unit_state = state.get_unit_state(ctx.unit_id)
        if unit_state is None or unit_state.review_round >= CAP_3:
            return False, f"cap of {CAP_3} cycles hit while addressing reviewer"

        fix_out = _record_step(
            ctx,
            "address_review (reviewer changes)",
            address_review(ctx.unit_id, "reviewer", reviewer_out.get("issue", "")),
        )
        if fix_out.get("outcome") != "FIX_PUSHED":
            return False, "coder fix (for review) did not succeed"

        # Wait for CI on the fix push before re-running reviewer
        ok, msg = _wait_ci_with_fix_loop(ctx, "reviewer-changes fix push")
        if not ok:
            return False, msg

        # Clear reviewer session so retry creates a fresh one
        s = state.get_unit_state(ctx.unit_id)
        if s is None:
            return False, "unit state vanished mid-cycle"
        s.reviewer_session_id = ""
        state.upsert_unit_state(s)

        reviewer_out = _record_step(
            ctx, "reviewer (retry)", spawn_reviewer(ctx.feature_id, ctx.unit_id)
        )
        outcome = reviewer_out.get("outcome")
        if isinstance(outcome, str) and outcome.startswith("BLOCKED"):
            return False, "reviewer blocked on retry"

    if outcome in ("REVIEW_APPROVED", "REVIEW_COMMENT", "REVIEW_RECOMMEND_MERGE"):
        return True, None

    return False, f"reviewer ended with unexpected outcome: {outcome}"


@mcp.tool()
def cycle_review(feature_id: str, unit_id: str) -> str:
    """Full automated post-spawn loop:
      tester → (if BUG: address_review → tester) → Copilot review →
      our reviewer → (if CHANGES: address_review → reviewer) → terminal.

    Cap = CAP_3 shared cycles across tester-bugs and reviewer-changes.
    On cap hit or any BLOCKED: marks escalated, returns summary.
    BLOCKS until terminal (success or escalation). Typically 5-20+ minutes.

    Repo must be fresh-verified (call `verify_repo(<url>)` if blocked).
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    ctx = CycleContext(feature_id=feature_id, unit_id=unit_id, history=[])

    # GATE 1: wait for CI on the coder's initial PR push before testing.
    # If CI is red, the helper runs an embedded fix loop (counts toward CAP_3).
    ok, msg = _wait_ci_with_fix_loop(ctx, "coder PR push")
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "CI gate failed before tester")

    passed, msg = _tester_phase(ctx)
    if not passed:
        return _emit_terminal(ctx, "escalated", msg or "tester phase failed")

    # GATE 2: tester pushed its tests; wait for CI green before Copilot + reviewer.
    ok, msg = _wait_ci_with_fix_loop(ctx, "tester test push")
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "CI gate failed before reviewer")

    _copilot_phase(ctx)

    approved, msg = _reviewer_phase(ctx)
    if not approved:
        return _emit_terminal(ctx, "escalated", msg or "reviewer phase failed")

    # GATE 3 (defensive): final CI check before declaring ready-to-merge.
    # If reviewer's loop already pushed fixes, _reviewer_phase has waited; this
    # is a belt-and-suspenders confirmation. A red here typically means a race
    # with a re-running workflow.
    ok, msg = _wait_ci_with_fix_loop(ctx, "final pre-merge check")
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "CI red at final pre-merge confirmation")

    return _emit_terminal(
        ctx,
        "approved_awaiting_merge",
        "Review terminal (approved/comment/recommend_merge), CI green. PR awaits human merge.",
    )


# --------------------------- send_to_unit (low-level) ---------------------------


@mcp.tool()
def send_to_unit(unit_id: str, role: str, message: str) -> str:
    """Low-level: resume a role's session with an arbitrary message.

    Prefer address_review / cycle_review for normal flow.

    Repo must be fresh-verified (call `verify_repo(<url>)` if blocked).
    """
    if role not in ("coder", "tester", "reviewer"):
        return f"ERROR: role must be coder|tester|reviewer, got {role!r}"

    if err := ensure_verified_for_unit(unit_id):
        return err

    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for unit {unit_id}"

    sid = {
        "coder": unit_state.coder_session_id,
        "tester": unit_state.tester_session_id,
        "reviewer": unit_state.reviewer_session_id,
    }[role]
    if not sid:
        return f"ERROR: no {role} session for {unit_id}"

    try:
        worker = ManagedAgentWorker(role=role)
        response = worker.resume(sid, message)
        state.touch_unit(unit_id)
        state.record_event(
            unit_id,
            unit_state.feature_id,
            f"{role}_manual_message",
            source="human",
            cycle_number=unit_state.review_round,
            summary="Manual send_to_unit",
            session_id=sid,
            details=message[:500],
        )
        return response
    except Exception as e:  # noqa: BLE001
        state.touch_unit(unit_id, error=str(e))
        return f"ERROR resuming {role}: {e}"


# Re-export for cycle_review's _emit_terminal (avoids circular import via observability)
def get_unit_status(unit_id: str) -> str:
    """Persisted state of one unit as JSON. Mirrors observability.get_unit_status."""
    from dataclasses import asdict

    s = state.get_unit_state(unit_id)
    if not s:
        return f"No state for unit {unit_id} (not yet spawned)"
    return json.dumps(asdict(s), indent=2)
