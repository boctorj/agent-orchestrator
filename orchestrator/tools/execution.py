"""Execution MCP tools: spawn coder/tester/reviewer, address feedback, run full cycle.

The `cycle_review` orchestration is broken into three private helper functions
— `_tester_phase`, `_copilot_phase`, `_reviewer_phase` — to keep the main flow
linear and each phase independently testable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator import ci_wait, github, ntfy, state
from orchestrator.agents import ManagedAgentWorker
from orchestrator.blocked_reasons import parse_blocked_marker
from orchestrator.models import ACTIVE_UNIT_STATUSES, WorkUnitState
from orchestrator.tools import (
    BUG_FOUND_RE,
    CAP_3,
    FIX_PUSHED_RE,
    PR_URL_RE,
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
    safe_dismiss_own_change_requests,
    safe_submit_pr_review,
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

    marker = _record_terminal_marker(
        unit_id=unit_id,
        feature_id=feature_id,
        role="tester",
        response=response,
        session_id=session_id,
        cycle_number=unit_state.review_round,
    )

    if marker is None:
        return _escalate_no_marker(
            unit_id=unit_id,
            feature_id=feature_id,
            role="tester",
            cycle_number=unit_state.review_round,
            session_id=session_id,
            response=response,
        )

    if marker["marker"] == "TESTS_PASS":
        # Supersede any prior REQUEST_CHANGES review by this bot identity
        # (from an earlier BUG_FOUND cycle). A same-user COMMENT review
        # resets the effective review state on most repos; dismissal
        # handles the strict "Require resolution of changes requested"
        # branch-protection setting.
        safe_dismiss_own_change_requests(
            feature.repo_path,
            unit_state.pr_number,
            "Tests pass on retry — superseding prior tester review.",
        )
        safe_submit_pr_review(
            feature.repo_path,
            unit_state.pr_number,
            f"🤖 **Tester:** all tests pass. _Session: `{session_id}`_",
            event="COMMENT",
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

    if marker["marker"] == "BUG_FOUND":
        # NB: tester posts its own inline REQUEST_CHANGES review with the
        # per-bug detail (see tester.md "Posting the BUG_FOUND review").
        # We don't add a top-level comment here — that would duplicate the
        # inline review. The session-id breadcrumb lives in the event log.
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "BUG_FOUND",
                "bug": marker["bug"],
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    # marker["marker"] == "BLOCKED"
    payload = marker["payload"]
    safe_comment_pr(
        feature.repo_path,
        unit_state.pr_number,
        f"🚨 **Tester BLOCKED [{payload.reason}]:** {payload.prose}\n_Escalated to human._",
    )
    return f"BLOCKED — tester for {unit_id} [{payload.reason}]: {payload.prose}"


# --------------------------- spawn_reviewer ---------------------------


@mcp.tool()
def spawn_reviewer(feature_id: str, unit_id: str) -> str:
    """Spawn a reviewer Managed Agent for a unit's PR.

    Reviewer is read-only. Posts review via the Reviews API. Signals
    REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES / REVIEW_COMMENT /
    BLOCKED. BLOCKS for minutes. (`REVIEW_APPROVED` is deprecated — the
    orchestrator never uses GitHub's `--approve`; a reviewer that emits
    it falls through to the no-marker escalation path so prompt drift
    is visible rather than silently accepted.)

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

    marker = _record_terminal_marker(
        unit_id=unit_id,
        feature_id=feature_id,
        role="reviewer",
        response=response,
        session_id=session_id,
        cycle_number=unit_state.review_round,
    )

    if marker is None:
        return _escalate_no_marker(
            unit_id=unit_id,
            feature_id=feature_id,
            role="reviewer",
            cycle_number=unit_state.review_round,
            session_id=session_id,
            response=response,
        )

    if marker["marker"] == "REVIEW_RECOMMEND_MERGE":
        reason = marker["reason"]
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

    if marker["marker"] == "REVIEW_REQUEST_CHANGES":
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "REVIEW_REQUEST_CHANGES",
                "issue": marker["issue"],
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    if marker["marker"] == "REVIEW_COMMENT":
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "REVIEW_COMMENT",
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    # marker["marker"] == "BLOCKED"
    payload = marker["payload"]
    safe_comment_pr(
        feature.repo_path,
        unit_state.pr_number,
        f"🚨 **Reviewer BLOCKED [{payload.reason}]:** {payload.prose}\n_Escalated to human._",
    )
    return f"BLOCKED — reviewer for {unit_id} [{payload.reason}]: {payload.prose}"


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

    marker = _record_terminal_marker(
        unit_id=unit_id,
        feature_id=unit_state.feature_id,
        role="coder",
        response=response,
        session_id=unit_state.coder_session_id,
        cycle_number=round_num,
        blocked_event="coder_blocked_on_fix",
        # PR_URL on a coder resume is anomalous — the unit already has a PR
        # from spawn_unit. Excluding it here makes a stray PR_URL response
        # fall through to _escalate_no_marker (matching pre-helper behaviour)
        # instead of writing a spurious pr_opened event + flipping to in_ci.
        markers=frozenset({"FIX_PUSHED", "BLOCKED"}),
    )

    if marker is None:
        return _escalate_no_marker(
            unit_id=unit_id,
            feature_id=unit_state.feature_id,
            role="fix",
            cycle_number=round_num,
            session_id=unit_state.coder_session_id,
            response=response,
        )

    if marker["marker"] == "FIX_PUSHED":
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

    # marker["marker"] == "BLOCKED" — the only other option given the markers subset.
    payload = marker["payload"]
    return f"BLOCKED — coder couldn't apply fix [{payload.reason}]: {payload.prose}"


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


def _flip_status_if_active(unit_id: str, *, target: str, error: str = "") -> None:
    """Conditional `touch_unit(status=target)` — applies only on an active from-state.

    The helper-driven status flips (success → ``in_ci``, BLOCKED → ``escalated``)
    are safe for the spawn_*/address_review surfaces because each does its own
    ``state.touch_unit(unit_id, status=<active>)`` first — the unit is always in
    ``coding`` / ``testing`` / ``reviewing`` / ``fixing`` / ``in_ci`` when the
    marker chain runs.

    ``send_to_unit`` has no such guarantee: it's a low-level escape hatch the
    user can fire at any session — including a ``done`` or ``escalated`` unit
    for a clarification chat. Without this gate, a reviewer agent (prompted to
    end every reply with a marker) emitting ``REVIEW_RECOMMEND_MERGE`` after the
    PR has already merged would silently flip the unit back to ``in_ci``.

    Audit events still fire from the helper unconditionally — only the status
    transition is gated, so the manual_message audit trail and structured
    marker event still land regardless of the unit's terminal state.
    """
    cur = state.get_unit_state(unit_id)
    if cur and cur.status in ACTIVE_UNIT_STATUSES:
        state.touch_unit(unit_id, status=target, error=error)


@dataclass(frozen=True)
class _MarkerSpec:
    """One (role, marker) pair: regex + the event-recording shape it produces.

    Drives `_record_terminal_marker`'s dispatch loop. Each spec captures
    everything that varies per marker so the loop body stays a four-liner:
    regex match → build event payload → optional status flip → return.

    ``build`` receives the regex match plus the worker response and produces
    ``(extras, event_kwargs)``:
      - ``extras`` is merged into the helper's return dict alongside
        ``{"marker": <name>}``.
      - ``event_kwargs`` is the keyword payload for ``state.record_event``
        (``summary`` / ``details`` — the rest is filled by the loop).
    """

    role: str
    marker: str
    pattern: re.Pattern[str]
    event_type: str
    flips_in_ci: bool
    build: Callable[[re.Match[str], str], tuple[dict[str, Any], dict[str, str]]]


def _build_pr_url(m: re.Match[str], _response: str) -> tuple[dict[str, Any], dict[str, str]]:
    pr_url, pr_number = m.group(1), int(m.group(2))
    return (
        {"pr_url": pr_url, "pr_number": pr_number},
        {"summary": f"PR #{pr_number} opened", "details": pr_url},
    )


def _build_fix_pushed(_m: re.Match[str], response: str) -> tuple[dict[str, Any], dict[str, str]]:
    return ({}, {"summary": "Fix committed and pushed", "details": tail(response)})


def _build_tests_pass(_m: re.Match[str], _response: str) -> tuple[dict[str, Any], dict[str, str]]:
    return ({}, {"summary": "All tests pass", "details": ""})


def _build_bug_found(m: re.Match[str], response: str) -> tuple[dict[str, Any], dict[str, str]]:
    reason = m.group(1).strip()
    return ({"bug": reason}, {"summary": reason, "details": tail(response)})


def _build_recommend_merge(
    m: re.Match[str], _response: str
) -> tuple[dict[str, Any], dict[str, str]]:
    reason = m.group(1).strip()
    return (
        {"reason": reason},
        {"summary": f"Endorsed (self-approval blocked): {reason}", "details": ""},
    )


def _build_request_changes(
    m: re.Match[str], response: str
) -> tuple[dict[str, Any], dict[str, str]]:
    reason = m.group(1).strip()
    return ({"issue": reason}, {"summary": reason, "details": tail(response)})


def _build_review_comment(
    _m: re.Match[str], _response: str
) -> tuple[dict[str, Any], dict[str, str]]:
    return ({}, {"summary": "Comment-only review", "details": ""})


# Ordered per role: PR_URL before FIX_PUSHED for the coder branch matches the
# pre-refactor precedence (the spawn_unit path checks PR_URL first too).
_MARKER_SPECS: tuple[_MarkerSpec, ...] = (
    _MarkerSpec("coder", "PR_URL", PR_URL_RE, "pr_opened", True, _build_pr_url),
    _MarkerSpec("coder", "FIX_PUSHED", FIX_PUSHED_RE, "fix_pushed", True, _build_fix_pushed),
    _MarkerSpec("tester", "TESTS_PASS", TESTS_PASS_RE, "tests_pass", True, _build_tests_pass),
    _MarkerSpec("tester", "BUG_FOUND", BUG_FOUND_RE, "tester_bug_found", False, _build_bug_found),
    _MarkerSpec(
        "reviewer",
        "REVIEW_RECOMMEND_MERGE",
        REVIEW_RECOMMEND_MERGE_RE,
        "reviewer_recommend_merge",
        True,
        _build_recommend_merge,
    ),
    _MarkerSpec(
        "reviewer",
        "REVIEW_REQUEST_CHANGES",
        REVIEW_CHANGES_RE,
        "reviewer_request_changes",
        False,
        _build_request_changes,
    ),
    _MarkerSpec(
        "reviewer",
        "REVIEW_COMMENT",
        REVIEW_COMMENT_RE,
        "reviewer_comment",
        True,
        _build_review_comment,
    ),
)


def _record_terminal_marker(
    *,
    unit_id: str,
    feature_id: str,
    role: str,
    response: str,
    session_id: str,
    cycle_number: int,
    blocked_event: str | None = None,
    markers: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Parse a worker response for role-appropriate terminal markers.

    Single source of truth for the marker → (event, status) mapping shared by
    ``spawn_tester``, ``spawn_reviewer``, ``address_review``, and
    ``send_to_unit``. Cross-role markers are ignored — a tester response
    containing ``REVIEW_RECOMMEND_MERGE`` is NOT a recognised marker.

    Per-role marker scope::

        coder    -> PR_URL | FIX_PUSHED | BLOCKED
        tester   -> TESTS_PASS | BUG_FOUND | BLOCKED
        reviewer -> REVIEW_RECOMMEND_MERGE | REVIEW_REQUEST_CHANGES |
                    REVIEW_COMMENT | BLOCKED

    Each (role, marker) pair is encoded once in ``_MARKER_SPECS``; the body
    is a single dispatch loop. Callers that know certain role-appropriate
    markers are invalid in their context can narrow the search via
    ``markers`` (e.g. ``address_review`` and ``send_to_unit(role='coder')``
    pass ``{"FIX_PUSHED", "BLOCKED"}`` — a coder resume returning ``PR_URL``
    is anomalous since the unit already has a PR from ``spawn_unit``, and
    matching it here would write a spurious ``pr_opened`` event without
    persisting ``pr_number`` to the unit row).

    Side effects on match:
      - Appends one ``unit_event`` row (``pr_opened`` / ``fix_pushed`` /
        ``tests_pass`` / ``tester_bug_found`` / ``reviewer_recommend_merge`` /
        ``reviewer_request_changes`` / ``reviewer_comment`` / ``{role}_blocked``
        — or the ``blocked_event`` override, used by ``address_review`` to
        keep the historical ``coder_blocked_on_fix`` distinction).
      - Updates ``work_units.status`` *only when the unit is currently in an
        active state* (see :func:`_flip_status_if_active`): success markers →
        ``in_ci``; ``BLOCKED`` → ``escalated`` (and populates ``last_error``);
        ``BUG_FOUND`` / ``REVIEW_REQUEST_CHANGES`` leave status unchanged
        (the caller's loop holds the unit in ``testing`` / ``reviewing``
        until the next ``address_review`` cycle).

    PR comments, ntfy pushes, and JSON return-value composition stay in the
    calling tool — those vary too much per surface to belong here.

    Returns ``None`` if no marker matched (caller should escalate as no-marker),
    or a dict ``{"marker": <name>, ...extras}`` describing the match.
    """
    allowed = (lambda _name: True) if markers is None else (lambda name: name in markers)

    for spec in _MARKER_SPECS:
        if spec.role != role or not allowed(spec.marker):
            continue
        match = spec.pattern.search(response)
        if not match:
            continue
        extras, event_kwargs = spec.build(match, response)
        if spec.flips_in_ci:
            _flip_status_if_active(unit_id, target="in_ci")
        state.record_event(
            unit_id,
            feature_id,
            spec.event_type,
            source=role,
            cycle_number=cycle_number,
            session_id=session_id,
            **event_kwargs,
        )
        return {"marker": spec.marker, **extras}

    # BLOCKED is universal across roles and uses parse_blocked_marker (not a
    # plain regex), so it sits outside the spec table.
    if allowed("BLOCKED"):
        payload = parse_blocked_marker(response)
        if payload is not None:
            _flip_status_if_active(
                unit_id,
                target="escalated",
                error=format_blocked_last_error(payload),
            )
            state.record_event(
                unit_id,
                feature_id,
                blocked_event or f"{role}_blocked",
                source=role,
                cycle_number=cycle_number,
                summary=payload.prose,
                session_id=session_id,
                details=blocked_event_details(payload, tail(response)),
            )
            return {"marker": "BLOCKED", "payload": payload}

    return None


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

    if outcome in ("REVIEW_COMMENT", "REVIEW_RECOMMEND_MERGE"):
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

    return _emit_terminal(
        ctx,
        "approved_awaiting_merge",
        "Review terminal (approved/comment/recommend_merge), CI green. PR awaits human merge.",
    )


# --------------------------- send_to_unit (low-level) ---------------------------


def _resolve_session_id(unit_state: WorkUnitState, role: str) -> str:
    """Return the persisted session_id for ``role`` on ``unit_state``.

    Empty string if the role has not been spawned yet (caller decides whether
    that is an error in context).
    """
    return {
        "coder": unit_state.coder_session_id,
        "tester": unit_state.tester_session_id,
        "reviewer": unit_state.reviewer_session_id,
    }[role]


def _resume_role_session(role: str, session_id: str, message: str) -> str:
    """Resume the role's worker session and return the worker's response.

    Thin wrapper over ``ManagedAgentWorker(role).resume()`` — exists so the
    ``send_to_unit`` MCP tool can stay declarative about *what* it does
    (resolve session → resume → record) rather than instantiate workers
    inline alongside everything else.
    """
    worker = ManagedAgentWorker(role=role)
    return worker.resume(session_id, message)


def _record_manual_message(
    *,
    unit_id: str,
    feature_id: str,
    role: str,
    session_id: str,
    message: str,
    cycle_number: int,
) -> None:
    """Append the ``{role}_manual_message`` audit row that pairs with every
    ``send_to_unit`` invocation.

    Always written, regardless of whether the worker's response carried a
    structured marker — the audit trail captures *that the human sent
    something*, distinct from *what the agent emitted in reply* (the latter
    lives in the marker event recorded just before this one).
    """
    state.touch_unit(unit_id)
    state.record_event(
        unit_id,
        feature_id,
        f"{role}_manual_message",
        source="human",
        cycle_number=cycle_number,
        summary="Manual send_to_unit",
        session_id=session_id,
        details=message[:500],
    )


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

    sid = _resolve_session_id(unit_state, role)
    if not sid:
        return f"ERROR: no {role} session for {unit_id}"

    try:
        response = _resume_role_session(role, sid, message)
    except Exception as e:  # noqa: BLE001
        state.touch_unit(unit_id, error=str(e))
        return f"ERROR resuming {role}: {e}"

    # Per-role marker scan FIRST so any structured outcome lands chronologically
    # before the human-issued audit row that elicited it. For role='coder',
    # narrow to {FIX_PUSHED, BLOCKED}: a coder resume returning PR_URL is
    # anomalous (the unit already has a PR from spawn_unit) and matching it
    # would drift the audit log from the unit row's pr_number — symmetric to
    # address_review's marker narrowing.
    _record_terminal_marker(
        unit_id=unit_id,
        feature_id=unit_state.feature_id,
        role=role,
        response=response,
        session_id=sid,
        cycle_number=unit_state.review_round,
        markers=frozenset({"FIX_PUSHED", "BLOCKED"}) if role == "coder" else None,
    )
    _record_manual_message(
        unit_id=unit_id,
        feature_id=unit_state.feature_id,
        role=role,
        session_id=sid,
        message=message,
        cycle_number=unit_state.review_round,
    )
    return response


# Re-export for cycle_review's _emit_terminal (avoids circular import via observability)
def get_unit_status(unit_id: str) -> str:
    """Persisted state of one unit as JSON. Mirrors observability.get_unit_status."""
    from dataclasses import asdict

    s = state.get_unit_state(unit_id)
    if not s:
        return f"No state for unit {unit_id} (not yet spawned)"
    return json.dumps(asdict(s), indent=2)
