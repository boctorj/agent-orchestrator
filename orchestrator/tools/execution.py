"""Execution MCP tools: spawn coder/tester/reviewer, address feedback, run full cycle.

The `cycle_review` orchestration is broken into private helper functions —
`_tester_phase`, `_copilot_phase`, `_reviewer_phase`, and the F-007 opt-in
`_ultrareview_phase` — to keep the main flow linear and each phase
independently testable.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from orchestrator import ci_wait, cycle_log, feature_spec, github, ntfy, state, ultrareview
from orchestrator import markers as markers_module
from orchestrator.agents import ManagedAgentWorker
from orchestrator.blocked_reasons import parse_blocked_marker
from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    CANCELLED_UNIT_STATUSES,
    READY_TO_MERGE_STATUSES,
    TERMINAL_UNIT_STATUSES,
    Feature,
    WorkUnit,
    WorkUnitState,
)
from orchestrator.tools import (
    CAP_3,
    PR_URL_RE,
    blocked_event_details,
    branch_for,
    compose_coder_task,
    compose_fix_task,
    compose_reviewer_delta_task,
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
from orchestrator.workers import make_worker

# --------------------------- task-message context helpers ---------------------------


def _predecessor_summaries(unit: WorkUnit) -> list[tuple[str, str]]:
    """``(unit_id, cycle-log summary)`` for every declared predecessor.

    Empty summaries (missing cycle log on disk) are preserved here so callers
    can see which deps were declared regardless of materialization state;
    :func:`_render_context_blocks` drops the empties when rendering. This
    keeps the "graceful missing-file" contract from F-006-U-4 explicit at
    both layers.
    """
    return [(dep_id, cycle_log.cycle_log_summary(dep_id)) for dep_id in unit.depends_on]


def _task_context_kwargs(feature: Feature, unit: WorkUnit) -> dict[str, Any]:
    """Common ``compose_*_task`` injection kwargs: spec text + predecessor logs.

    Centralizes the two reads coder/tester/reviewer share so a future change
    to either source (additional fields, alternative summarization) lands in
    one place. Missing files yield empty values — each context block then
    drops out individually at the renderer (see
    :func:`orchestrator.tools._render_context_blocks`). That's the F-006-U-4
    "read-side gracefully handles missing files" guarantee: e.g. a feature
    with no merged predecessors still injects ``## FEATURE SPEC`` once its
    spec.md exists, but ``## PREDECESSOR UNITS`` stays absent until the
    first dep's cycle log lands on disk.
    """
    return {
        "feature_spec_text": feature_spec.read_spec(feature.id),
        "predecessor_logs": _predecessor_summaries(unit),
    }


# Reviewer retry threshold for injecting the unit's own cycle log
# (PROPOSAL § "Role prompt changes" — reviewer gets ``## THIS UNIT'S CYCLE LOG``
# only on retry cycle >= 2; the first reviewer turn has no prior cycle to
# read). Pulled out as a constant so the proposal-spec semantics are
# greppable from anywhere.
REVIEWER_OWN_LOG_MIN_ROUND = 2


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
    task = compose_coder_task(
        feature, unit, branch, github_token, **_task_context_kwargs(feature, unit)
    )

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


# --------------------------- spawn_unit_async (F-016 Phase 1) ---------------------------


@mcp.tool()
def spawn_unit_async(feature_id: str, unit_id: str) -> str:
    """Dispatch a coder for one unit; return in ~2s without waiting.

    Phase 1 of the dispatcher/watcher split (F-016). Persists
    ``coder_session_id`` to ``state.db`` BEFORE ``spawn_unit_async``
    returns — a lead killed between this tool's return and the next
    ``wait_unit`` / daemon tick leaves a recoverable row rather than the
    ghost-row failure mode (status=coding with empty session_id) the
    F-014-U-1 escalation surfaced. Pair with
    ``wait_unit(unit_id, 'coder')`` when the caller wants the marker, or
    let the F-016-U-5 watcher daemon advance the unit asynchronously.

    The mid-spawn window — between ``worker.spawn_async`` accepting the
    dispatch and us writing the session id — is the only state where the
    row exists with ``coder_session_id=""``; a fresh lead can still
    recover via ``scan_unit_session`` since the row carries
    ``status=coding`` and the worker session is live on the backend.

    Same preconditions as ``spawn_unit``: feature loaded + approved,
    GITHUB_TOKEN set, target repo fresh-verified.

    Backend selection follows ``ORCH_WORKER_BACKEND`` via
    :func:`orchestrator.workers.make_worker`. The ``docker`` backend's
    ``spawn_async`` raises ``NotImplementedError`` until its async split
    lands; the lead sees an actionable error rather than a silent block.
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
            f"Use wait_unit, cycle_review, or address_review to advance it."
        )

    if err := need_github_token():
        return err
    github_token = get_agent_token()

    branch = branch_for(feature, unit)
    task = compose_coder_task(
        feature, unit, branch, github_token, **_task_context_kwargs(feature, unit)
    )

    # Seed the row BEFORE the worker call so a kill between the
    # ``spawn_async`` submit and our session-id write still leaves a
    # ``status=coding`` row the next restart can recover (worst case
    # via ``scan_unit_session`` once the lead manually correlates the
    # Anthropic-side session). Order matters: row first, then submit,
    # then session_id write.
    state.upsert_unit_state(
        WorkUnitState(unit_id=unit_id, feature_id=feature_id, status="coding", branch=branch)
    )
    state.record_event(
        unit_id,
        feature_id,
        "spawn_coder_async",
        source="orchestrator",
        cycle_number=0,
        summary=f"Dispatching coder for {unit_id} (non-blocking)",
    )

    try:
        worker = make_worker("coder")
        session_id = worker.spawn_async(task, title=f"{unit_id}: {unit.title}")
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

    # Persist session_id immediately. A kill from here on still leaves
    # ``coder_session_id`` recorded — ``wait_unit`` / the daemon can pick
    # up the still-running worker. ``last_activity`` advances as part of
    # the upsert and surfaces in the JSON below.
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="coding",
            branch=branch,
            coder_session_id=session_id,
        )
    )
    refreshed = state.get_unit_state(unit_id)

    result: dict[str, Any] = {
        "unit_id": unit_id,
        "feature_id": feature_id,
        "session_id": session_id,
        "branch": branch,
        "status": "coding",
        "submitted_at": refreshed.last_activity if refreshed else "",
    }
    return json.dumps(result, indent=2)


# --------------------------- wait_unit (F-016 Phase 1) ---------------------------


@mcp.tool()
def wait_unit(unit_id: str, role: str = "coder", timeout_s: int = 600) -> str:
    """Explicit-wait counterpart to ``spawn_unit_async`` / ``resume_async``.

    Streams the worker session via the backend's ``wait_idle`` and:

      * on a recognised terminal marker → records the event idempotently
        (Phase-0 dedupe), advances the unit row, returns the parsed
        marker plus its extras (``pr_url`` / ``pr_number`` / etc.) as
        JSON.
      * on ``TimeoutError`` after ``timeout_s`` → returns
        ``{"status": "still_running", "reason": "timeout"}`` WITHOUT
        flipping the unit row. The caller (lead or daemon) decides
        whether to retry, escalate, or hand off.
      * on idle-with-no-marker → returns ``{"status": "still_running",
        "reason": "no_marker"}`` (status unchanged). Phase-1 wait is a
        read surface; ``cycle_review`` keeps the "no marker → escalate"
        policy.

    Backend selection follows ``ORCH_WORKER_BACKEND`` via
    :func:`orchestrator.workers.make_worker` — the ``docker`` backend's
    ``wait_idle`` raises ``NotImplementedError`` until its async split
    lands.

    Args:
        unit_id: The work unit to wait on.
        role: ``coder`` / ``tester`` / ``reviewer``.
        timeout_s: Max seconds before returning ``still_running``.
    """
    if role not in ("coder", "tester", "reviewer"):
        return f"ERROR: role must be coder|tester|reviewer, got {role!r}"

    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for unit {unit_id}"

    session_id = _resolve_session_id(unit_state, role)
    if not session_id:
        return f"ERROR: no {role} session for {unit_id} — likely never spawned"

    try:
        worker = make_worker(role)
        response = worker.wait_idle(session_id, timeout_seconds=timeout_s)
    except TimeoutError:
        return json.dumps(
            {
                "unit_id": unit_id,
                "role": role,
                "session_id": session_id,
                "status": "still_running",
                "reason": "timeout",
                "timeout_s": timeout_s,
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001
        return f"ERROR waiting on {role} session {session_id}: {e}"

    marker = _record_terminal_marker(
        unit_id=unit_id,
        feature_id=unit_state.feature_id,
        role=role,
        response=response,
        session_id=session_id,
        cycle_number=unit_state.review_round,
    )
    if marker is None:
        # Idled without a recognised marker. Don't escalate here — the
        # cycle_review and daemon paths own that policy. Phase-1 wait
        # reports observations; the caller decides next step.
        return json.dumps(
            {
                "unit_id": unit_id,
                "role": role,
                "session_id": session_id,
                "status": "still_running",
                "reason": "no_marker",
                "marker": None,
                "response_tail": tail(response),
            },
            indent=2,
        )

    # ``_record_terminal_marker`` returns ``{"marker": <name>, **extras}``
    # — for PR_URL that includes ``pr_url`` + ``pr_number``; for BLOCKED
    # the structured payload. Spread it into the response so callers
    # don't have to call ``unit_history`` to recover the URL the worker
    # just emitted.
    refreshed = state.get_unit_state(unit_id)
    return json.dumps(
        {
            "unit_id": unit_id,
            "role": role,
            "session_id": session_id,
            **marker,
            "status": refreshed.status if refreshed else unit_state.status,
            "response_tail": tail(response),
        },
        indent=2,
        default=str,
    )


# --------------------------- spawn_tester ---------------------------


@mcp.tool()
def spawn_tester(feature_id: str, unit_id: str) -> str:
    """Spawn OR resume a tester Managed Agent for a unit whose coder has opened a PR.

    **Idempotent on units with a prior ``tester_session_id``** — instead of
    refusing (the pre-F-009-U-3 contract), the existing worker session is
    resumed with a role-aware recovery prompt (see :func:`build_recovery_prompt`).
    Resuming preserves the worker backend's accumulated context (PR diff,
    prior test scaffolding, prior findings); spawning fresh would discard
    all of that and re-pay the clone + inventory cost. This is the common
    case for Gap-C (post-escalation recovery) and Gap-D (transient-retry)
    in docs/STATE-MACHINE-AUDIT.md.

    Tester signals TESTS_PASS / BUG_FOUND / BLOCKED. BLOCKS for minutes.

    Refuses to act if CI is currently failing on the PR — testing a
    red PR is wasted work. Use ``cycle_review`` for the automated CI-fix
    loop, or ``send_to_unit`` as an escape hatch.

    Repo must be fresh-verified (call ``verify_repo(<url>)`` if blocked).
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

    plan = state.get_plan(feature_id)
    unit = next((u for u in plan.units if u.id == unit_id), None) if plan else None
    if not unit:
        return f"ERROR: unit {unit_id} not in plan"

    if err := need_github_token():
        return err

    # Resume-or-spawn: a prior session_id means the worker holds accumulated
    # context for this unit — resume it instead of cold-starting. Covers
    # Gap-C (post-escalation recovery) and Gap-D (transient-retry) without
    # a schema change. See ``_resume_role_for_recovery`` for the contract.
    if unit_state.tester_session_id:
        return _resume_role_for_recovery(role="tester", feature=feature, unit_state=unit_state)

    github_token = get_agent_token()

    task = compose_tester_task(
        feature,
        unit,
        unit_state.branch,
        unit_state.pr_number,
        github_token,
        **_task_context_kwargs(feature, unit),
    )
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

    return _format_tester_marker_response(
        unit_id=unit_id,
        repo_path=feature.repo_path,
        pr_number=unit_state.pr_number,
        session_id=session_id,
        response=response,
        marker=marker,
    )


def _format_tester_marker_response(
    *,
    unit_id: str,
    repo_path: str,
    pr_number: int,
    session_id: str,
    response: str,
    marker: dict[str, Any],
) -> str:
    """Tester marker → MCP return JSON (or BLOCKED string), shared by initial spawn + resume.

    Mirrors :func:`_format_reviewer_marker_response`: both ``spawn_tester``
    (initial spawn) and ``_resume_role_for_recovery`` (resume on prior
    ``tester_session_id``) emit the same shape so consumers (cycle_review's
    ``_record_step``, the MCP tool's JSON return contract) don't need to
    special-case the resume path.

    Side effects (run from this helper, not the caller):
      - **TESTS_PASS**: dismiss any prior REQUEST_CHANGES review by this
        bot identity (so branch protection's "Require resolution of changes
        requested" doesn't block the eventual merge) and post a same-user
        COMMENT breadcrumb anchored to the session id.
      - **BLOCKED**: post the escalation comment on the PR.
      - **BUG_FOUND**: no PR-side action here — the tester agent posts its
        own inline REQUEST_CHANGES review per ``prompts/tester.md`` ("Posting
        the BUG_FOUND review"); a top-level comment would duplicate it.
    """
    if marker["marker"] == "TESTS_PASS":
        safe_dismiss_own_change_requests(
            repo_path,
            pr_number,
            "Tests pass on retry — superseding prior tester review.",
        )
        safe_submit_pr_review(
            repo_path,
            pr_number,
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
        repo_path,
        pr_number,
        f"🚨 **Tester BLOCKED [{payload.reason}]:** {payload.prose}\n_Escalated to human._",
    )
    return f"BLOCKED — tester for {unit_id} [{payload.reason}]: {payload.prose}"


# --------------------------- spawn_reviewer ---------------------------


@mcp.tool()
def spawn_reviewer(feature_id: str, unit_id: str) -> str:
    """Spawn OR resume a reviewer Managed Agent for a unit's PR.

    **Idempotent on units with a prior ``reviewer_session_id``** — instead
    of refusing (the pre-F-009-U-3 contract), the existing worker session
    is resumed with a role-aware recovery prompt (see
    :func:`build_recovery_prompt`). Resuming preserves the worker backend's
    accumulated context (PR inventory, prior findings); spawning fresh
    would discard it. Covers Gap-C (post-escalation recovery) and Gap-D
    (transient-retry) in docs/STATE-MACHINE-AUDIT.md.

    Reviewer is read-only. Posts review via the Reviews API. Signals
    REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES / REVIEW_COMMENT /
    BLOCKED. BLOCKS for minutes. (``REVIEW_APPROVED`` is deprecated — the
    orchestrator never uses GitHub's ``--approve``; a reviewer that emits
    it falls through to the no-marker escalation path so prompt drift
    is visible rather than silently accepted.)

    Refuses to act if CI is currently failing — reviewing a red PR
    duplicates effort the reviewer would otherwise spend critiquing the
    same failures. Use ``cycle_review`` for the automated CI-fix loop.

    Repo must be fresh-verified (call ``verify_repo(<url>)`` if blocked).
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

    plan = state.get_plan(feature_id)
    unit = next((u for u in plan.units if u.id == unit_id), None) if plan else None
    if not unit:
        return f"ERROR: unit {unit_id} not in plan"

    if err := need_github_token():
        return err

    # Resume-or-spawn: prior session_id means the worker holds accumulated
    # context (PR inventory, prior verdict) — resume rather than cold-start.
    # Symmetric to the spawn_tester branch above; see ``_resume_role_for_recovery``.
    if unit_state.reviewer_session_id:
        return _resume_role_for_recovery(role="reviewer", feature=feature, unit_state=unit_state)

    github_token = get_agent_token()

    reviewer_kwargs = _task_context_kwargs(feature, unit)
    if unit_state.review_round >= REVIEWER_OWN_LOG_MIN_ROUND:
        reviewer_kwargs["own_cycle_log"] = cycle_log.read_cycle_log(unit_id)
    task = compose_reviewer_task(
        feature, unit, unit_state.pr_number, github_token, **reviewer_kwargs
    )
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

    return _format_reviewer_marker_response(
        unit_id=unit_id,
        repo_path=feature.repo_path,
        pr_number=unit_state.pr_number,
        session_id=session_id,
        response=response,
        marker=marker,
    )


def _format_reviewer_marker_response(
    *,
    unit_id: str,
    repo_path: str | None,
    pr_number: int | None,
    session_id: str,
    response: str,
    marker: dict[str, Any],
) -> str:
    """Marker → MCP return JSON (or BLOCKED string), shared by initial spawn + delta resume.

    Both ``spawn_reviewer`` and ``_resume_reviewer_for_delta`` must emit the
    same shape so ``cycle_review`` consumers (``_record_step``, history
    entries) don't need to special-case the retry path.

    Side effect: posts the recommendation / BLOCKED comment to the PR when
    ``repo_path`` and ``pr_number`` are both set. The degenerate case where
    ``_resume_or_spawn_reviewer`` resumes on a row whose ``pr_number`` /
    feature record was lost gracefully skips the PR-side write rather than
    falling through to ``_escalate_no_marker`` for a marker that already
    parsed cleanly.
    """
    if marker["marker"] == "REVIEW_RECOMMEND_MERGE":
        reason = marker["reason"]
        if repo_path and pr_number is not None:
            safe_comment_pr(
                repo_path,
                pr_number,
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
    if repo_path and pr_number is not None:
        safe_comment_pr(
            repo_path,
            pr_number,
            f"🚨 **Reviewer BLOCKED [{payload.reason}]:** {payload.prose}\n_Escalated to human._",
        )
    return f"BLOCKED — reviewer for {unit_id} [{payload.reason}]: {payload.prose}"


# --------------------------- address_review (coder resume) ---------------------------


@mcp.tool()
def address_review(unit_id: str, source: str, feedback: str) -> str:
    """Resume the coder session to address feedback (from tester/reviewer/ci/human).

    **Idempotent on any status with a ``coder_session_id``, EXCEPT ``done``.**
    The session_id is the source of truth — as long as ``coder_session_id``
    is set and the unit is not merged, the coder thread is resumed and the
    unit transitions back to ``fixing``. This is the standard recovery path
    for Gap-C (cleared blocker after escalation): the human surfaces
    feedback via this call and the coder picks up exactly where it left
    off. No guard rejects an escalated unit — the prior ``last_error`` is
    the *reason* the human is calling now.

    Refuses on ``status='done'`` to avoid silently re-opening a merged unit.
    NB: ``escalated`` is listed in ``TERMINAL_UNIT_STATUSES`` alongside
    ``done`` but is deliberately *not* refused here — that's the audit
    Gap-C contract.

    Increments review_round. BLOCKS for minutes.
    Returns coder's response — should end with FIX_PUSHED or BLOCKED.

    Repo must be fresh-verified (call ``verify_repo(<url>)`` if blocked).
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
    # Done units have a merged PR — re-opening via a coder resume would
    # silently flip status away from terminal. Recovery for merged units
    # goes through reconcile_unit_pr (which is idempotent and read-only
    # against the worker session). Symmetric to the `done` guard in
    # `_resume_role_for_recovery`.
    if unit_state.status == "done":
        return (
            f"ERROR: unit {unit_id} is already done (PR merged). "
            f"Refusing to resume coder — use reconcile_unit_pr to refresh "
            f"state if needed."
        )

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
        feature,
        unit,
        unit_state.branch,
        unit_state.pr_number,
        source,
        feedback,
        **_task_context_kwargs(feature, unit),
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
    """Carrier object for cycle_review phase helpers.

    ``last_reviewed_sha`` tracks the PR head SHA the reviewer most recently
    looked at — captured after every reviewer turn (initial spawn + each
    delta resume). On retry, ``_resume_reviewer_for_delta`` passes it as the
    prior anchor in the delta range; the reviewer agent diffs only
    ``prior_sha..current_sha`` instead of re-reading the whole PR.
    """

    feature_id: str
    unit_id: str
    history: list[dict]
    last_reviewed_sha: str = ""


def _record_step(ctx: CycleContext, name: str, result_json_str: str) -> dict:
    """Parse a tool's JSON result and append to history. Returns the parsed dict."""
    try:
        r = json.loads(result_json_str)
    except (json.JSONDecodeError, TypeError):
        r = {"outcome": "RAW", "raw": result_json_str}
    ctx.history.append({"step": name, "result": r})
    return r


def _write_cycle_log_safe(unit_id: str) -> None:
    """Render + commit the per-unit cycle log; swallow every failure.

    Cycle logs are post-hoc summaries — a gh outage, a non-repo workdir,
    or a missing ``git`` must never abort ``cycle_review``. Errors are
    intentionally silent here; recovery lives in
    ``cycle_log.regenerate_cycle_log``. The post-merge SHA backfill is
    handled in ``reconcile_unit_pr`` (ops.py), not here — cycle_review
    runs strictly before any merge.
    """
    with contextlib.suppress(Exception):
        cycle_log.write_cycle_log(unit_id, base_dir=cycle_log.cycle_log_base_dir())


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
    """Final return value of cycle_review. Fires ntfy push as side effect.

    The ``approved_awaiting_merge`` ntfy push is dedupe-keyed per unit via
    the ``cycle_terminal_emitted`` event row: ``state.record_event`` with a
    constant ``cycle_terminal_emitted:<unit_id>`` key uses
    ``INSERT OR IGNORE`` (Phase 0's primitive) so the second call against
    the same unit returns ``rowcount == 0`` and the status flip + ntfy push
    skip. Closes the F-016-U-5 daemon's notification-storm window: a unit
    sitting in ``approved_awaiting_merge`` for hours waiting on the human
    merge collects exactly one push, not one per poll interval (PR #58 H1).
    The escalation branch stays unconditional — re-escalation is rare and
    the human needs the signal each time.
    """
    # Finalize the cycle log before the ntfy push so an operator who taps
    # the notification and looks at features/F-XXX/U-N.md sees the
    # current terminal state captured on disk. Covers all cycle_review
    # terminal branches: REVIEW_RECOMMEND_MERGE, REVIEW_COMMENT, and
    # every escalation path (tester blocked, reviewer blocked, cap-3,
    # CI timeout, ...).
    _write_cycle_log_safe(ctx.unit_id)

    unit_state = state.get_unit_state(ctx.unit_id)
    pr_url = _pr_url_for(ctx.feature_id, unit_state)

    if outcome == "escalated":
        ntfy.push_escalation(ctx.unit_id, msg, pr_url=pr_url)
    elif outcome == "approved_awaiting_merge":
        # Dedupe via cycle_terminal_emitted event. INSERT OR IGNORE on the
        # per-unit dedupe_key means a re-call (daemon tick, accidental
        # double cycle_review, etc.) returns False here and skips the
        # status flip + ntfy push (PR #58 H1). Without this, the F-016-U-5
        # daemon would re-fire the ntfy push on every tick against a unit
        # sitting in approved_awaiting_merge — a notification storm for
        # any unit waiting hours/days for the human merge.
        inserted = state.record_event(
            ctx.unit_id,
            ctx.feature_id,
            "cycle_terminal_emitted",
            source="orchestrator",
            cycle_number=unit_state.review_round if unit_state else None,
            summary="approved_awaiting_merge terminal fired",
            details=msg,
            dedupe_key=f"cycle_terminal_emitted:{ctx.unit_id}",
        )
        if inserted:
            # Persist the awaiting-merge status before pushing — the ntfy
            # listener follows the URL to the dashboard, which reads this
            # row. The flip is gated on an active from-state so a stray
            # re-entry can't drift a `done` unit back to
            # `approved_awaiting_merge` (F-009-U-4).
            _flip_status_if_active(ctx.unit_id, target="approved_awaiting_merge")
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
    ``send_to_unit``. The grammar lives in
    :mod:`orchestrator.markers`; this helper is the recorder half — it
    takes the pure :class:`markers.MarkerSpec` and applies the audit-row
    write + status flip side-effects.

    Cross-role markers are ignored — a tester response containing
    ``REVIEW_RECOMMEND_MERGE`` is NOT a recognised marker. Per-role
    marker scope::

        coder    -> PR_URL | FIX_PUSHED | BLOCKED
        tester   -> TESTS_PASS | BUG_FOUND | BLOCKED
        reviewer -> REVIEW_RECOMMEND_MERGE | REVIEW_REQUEST_CHANGES |
                    REVIEW_COMMENT | BLOCKED

    Callers that know certain role-appropriate markers are invalid in
    their context can narrow the search via ``markers`` (e.g.
    ``address_review`` and ``send_to_unit(role='coder')`` pass
    ``{"FIX_PUSHED", "BLOCKED"}`` — a coder resume returning ``PR_URL``
    is anomalous since the unit already has a PR from ``spawn_unit``,
    and matching it here would write a spurious ``pr_opened`` event
    without persisting ``pr_number`` to the unit row).

    Side effects on match:
      - Appends one ``unit_event`` row keyed by
        :func:`orchestrator.markers.dedupe_key` — a second call on the
        same response (e.g. the F-016 watcher daemon re-scanning an idle
        session) is a no-op via ``INSERT OR IGNORE``. Event types:
        ``pr_opened`` / ``fix_pushed`` / ``tests_pass`` /
        ``tester_bug_found`` / ``reviewer_recommend_merge`` /
        ``reviewer_request_changes`` / ``reviewer_comment`` /
        ``{role}_blocked`` — or the ``blocked_event`` override, used by
        ``address_review`` to keep the historical
        ``coder_blocked_on_fix`` distinction.
      - Updates ``work_units.status`` *only when the unit is currently in an
        active state* (see :func:`_flip_status_if_active`) to the
        per-marker ``target_status``: most success markers target
        ``in_ci``; ``REVIEW_RECOMMEND_MERGE`` targets
        ``approved_awaiting_merge`` (F-009-U-4 — matches cycle_review's
        terminal); ``BLOCKED`` flips to ``escalated`` and populates
        ``last_error``; ``BUG_FOUND`` / ``REVIEW_REQUEST_CHANGES`` leave
        status unchanged (the caller's loop holds the unit in ``testing``
        / ``reviewing`` until the next ``address_review`` cycle).

    PR comments, ntfy pushes, and JSON return-value composition stay in the
    calling tool — those vary too much per surface to belong here.

    Returns ``None`` if no marker matched (caller should escalate as no-marker),
    or a dict ``{"marker": <name>, ...extras}`` describing the match.
    """
    spec = markers_module.scan_response(role, response, allowed=markers)
    if spec is None:
        return None

    # ``address_review`` keeps the historical ``coder_blocked_on_fix``
    # discriminator. The override lands BEFORE the dedupe key is hashed so
    # a fix-loop BLOCKED cannot collide with a spawn-time BLOCKED on the
    # same coder session.
    event_type = spec.event_type
    if spec.marker == "BLOCKED" and blocked_event:
        event_type = blocked_event

    # BLOCKED's details JSON-encodes the structured payload + response
    # tail; the pure scan leaves details empty so the recorder owns the
    # response-truncation side. Other markers use scan_response's details.
    details = spec.details
    if spec.blocked_payload is not None:
        details = blocked_event_details(spec.blocked_payload, tail(response))

    if spec.target_status is not None:
        _flip_status_if_active(unit_id, target=spec.target_status, error=spec.last_error)

    key = markers_module.dedupe_key(
        session_id=session_id,
        cycle_number=cycle_number,
        event_type=event_type,
        marker_payload=spec.payload,
    )
    state.record_event(
        unit_id,
        feature_id,
        event_type,
        source=role,
        cycle_number=cycle_number,
        session_id=session_id,
        summary=spec.summary,
        details=details,
        dedupe_key=key,
    )
    return {"marker": spec.marker, **spec.extras}


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

    ``label`` describes which push we're gating on (e.g. "coder PR push",
    "tester test push", "tester-bug fix push", "reviewer-changes fix push")
    for ctx.history breadcrumbs.
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


# --------------------------- resume-or-spawn / recovery prompts ---------------------------
#
# F-009-U-3: ``spawn_tester`` / ``spawn_reviewer`` are idempotent on units
# with a prior ``{role}_session_id``. Instead of refusing (the pre-F-009-U-3
# contract), the existing worker session is resumed with a role-aware
# recovery prompt. Closes audit Gaps C (post-escalation recovery) and D
# (transient-retry) without a schema change — the session id on the unit
# row is already the source of truth for "this worker holds context".

RecoveryReason = Literal["post-escalation", "transient-retry", "ci-fix"]
RecoveryRole = Literal["coder", "tester", "reviewer"]

# Stock recovery prompts keyed by (role, reason). Each template may
# interpolate ``{last_error}`` and ``{details}`` via ``str.format`` —
# placeholders are optional, templates that don't reference them just
# pass through unchanged. Keep these short and marker-explicit; the
# load-bearing content is the marker vocabulary, not prose.
_RECOVERY_PROMPTS: dict[tuple[str, str], str] = {
    ("coder", "post-escalation"): (
        "You were previously escalated (last error: {last_error}). The PR is "
        "still open and CI is now green — continue addressing the most recent "
        "feedback and end your reply with FIX_PUSHED (or BLOCKED on a hard "
        "failure)."
    ),
    ("coder", "transient-retry"): (
        "Picking up your coder session after a transient orchestrator failure. "
        "Re-emit your last marker (FIX_PUSHED or BLOCKED) on its own line — "
        "do not redo work you already completed."
    ),
    ("coder", "ci-fix"): (
        "CI has failed: {details}. Inspect the failing checks, push a focused "
        "fix to the same branch, and end your reply with FIX_PUSHED or BLOCKED."
    ),
    ("tester", "post-escalation"): (
        "You were previously escalated (last error: {last_error}). CI is now "
        "green and the PR is open — re-evaluate the current PR head and emit "
        "your verdict marker (TESTS_PASS / BUG_FOUND / BLOCKED). Do not redo "
        "test work you already completed."
    ),
    ("tester", "transient-retry"): (
        "Your previous response was lost to a network timeout in the orchestrator. "
        "The PR is still open and CI is currently green. Please re-evaluate the "
        "current PR head and re-emit your verdict marker (TESTS_PASS / BUG_FOUND / "
        "BLOCKED) — do not redo test work you already completed. Output only the "
        "marker on its own line."
    ),
    ("tester", "ci-fix"): (
        "CI has failed: {details}. Wait for the coder's fix push, then re-run "
        "tests and emit TESTS_PASS / BUG_FOUND / BLOCKED."
    ),
    ("reviewer", "post-escalation"): (
        "You were previously escalated (last error: {last_error}). CI is now "
        "green — re-evaluate the PR and emit your verdict marker "
        "(REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES / REVIEW_COMMENT / "
        "BLOCKED). Do not redo work you already completed."
    ),
    ("reviewer", "transient-retry"): (
        "Your previous response was lost to a network timeout in the orchestrator. "
        "CI is still green and the PR is open. Re-emit your last verdict marker "
        "(REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES / REVIEW_COMMENT / "
        "BLOCKED) on its own line — do not redo work you already completed."
    ),
    ("reviewer", "ci-fix"): (
        "CI has failed: {details}. Wait for the coder's fix push and CI to go "
        "green, then re-evaluate and emit REVIEW_RECOMMEND_MERGE / "
        "REVIEW_REQUEST_CHANGES / REVIEW_COMMENT / BLOCKED."
    ),
}


def build_recovery_prompt(
    role: RecoveryRole,
    reason: RecoveryReason,
    *,
    last_error: str = "",
    details: str = "",
) -> str:
    """Return the canonical recovery message for ``(role, reason)``.

    Used by ``spawn_tester`` / ``spawn_reviewer`` (and the internal
    ``_resume_or_spawn_tester`` shim) when resuming a session that already
    has accumulated worker-side context. The prompt is short and
    marker-explicit: the agent's job on a resume is to *re-emit* its
    terminal marker, not to redo the work that produced it.

    Recognised reasons:
      * ``post-escalation`` — unit hit ``escalated`` previously and the
        human cleared the blocker; verify/continue. ``last_error`` is
        interpolated into the template.
      * ``transient-retry`` — orchestrator-side network blip or restart;
        the agent's prior reply was lost. Re-emit the marker.
      * ``ci-fix`` — CI failed; coder pushes a focused fix, tester/reviewer
        wait then re-evaluate. ``details`` carries the failing-checks summary.

    Unknown ``(role, reason)`` pairs raise ``ValueError`` so prompt drift
    is loud rather than silent. Tests in
    ``tests/test_f009_u3_resume_or_spawn.py`` pin each stock message.
    """
    key = (role, reason)
    if key not in _RECOVERY_PROMPTS:
        raise ValueError(
            f"unknown (role, reason) for build_recovery_prompt: {key!r}. "
            f"Valid roles: coder|tester|reviewer; "
            f"valid reasons: post-escalation|transient-retry|ci-fix."
        )
    return _RECOVERY_PROMPTS[key].format(
        last_error=last_error or "(unspecified)",
        details=details or "(unspecified)",
    )


def _derive_recovery_reason(unit_state: WorkUnitState) -> RecoveryReason:
    """Map unit status → which recovery template to use.

    Escalated unit means we're recovering from a prior hard failure (Gap-C);
    any other status (typically the active role, or ``in_ci`` mid-cycle)
    means the prior worker reply was lost in transit (Gap-D).
    """
    return "post-escalation" if unit_state.status == "escalated" else "transient-retry"


def _resume_role_for_recovery(
    *,
    role: Literal["tester", "reviewer"],
    feature: Feature,
    unit_state: WorkUnitState,
) -> str:
    """Resume an existing tester/reviewer session for context-preserving recovery.

    Called from ``spawn_tester`` / ``spawn_reviewer`` when the unit has a
    prior ``{role}_session_id``. Both spawn surfaces converge on the same
    response shape (``_format_tester_marker_response`` /
    ``_format_reviewer_marker_response``) so callers don't need to
    special-case the resume path.

    **Refuses on ``status='done'``** — the old duplicate-spawn-guard wall
    incidentally protected merged units from being silently re-opened; the
    resume contract preserves that. Recovery is for the escalated/active
    states (Gap-C / Gap-D); a merged unit needs ``reconcile_unit_pr``, not
    resume. (``escalated`` is recoverable — it's *terminal* per
    ``TERMINAL_UNIT_STATUSES`` but the audit gap C is exactly the
    post-escalation recovery path, so we deliberately allow it here.)

    Status handling on the recoverable branches:
      - flips to ``testing`` / ``reviewing`` while the worker runs;
      - clears ``last_error`` on entry — we're starting a fresh attempt and
        the marker chain will repopulate it if the worker BLOCKS again;
      - records a ``{role}_resume`` audit row (with the derived reason as
        the summary suffix) before the resume call so operators see the
        recovery branch was chosen.
    """
    # `done` units have a merged PR — re-engaging the worker session would
    # silently flip status away from terminal, re-trigger ntfy pushes, and
    # pollute the lead's scheduling view. The pre-F-009-U-3 refusal wall
    # incidentally caught this; the resume contract has to as well.
    if unit_state.status == "done":
        return (
            f"ERROR: unit {unit_state.unit_id} is already done (PR merged). "
            f"Refusing to resume {role} — use reconcile_unit_pr to refresh "
            f"state if needed, or clear the session id manually if you "
            f"intend to re-open."
        )

    # Callers (spawn_tester / spawn_reviewer) gate on pr_number before
    # routing here; assert keeps mypy honest about the narrowing.
    pr_number = unit_state.pr_number
    assert pr_number is not None, (
        f"_resume_role_for_recovery called on {unit_state.unit_id} without a PR — "
        "spawn_tester / spawn_reviewer must gate on pr_number first"
    )
    session_id = (
        unit_state.tester_session_id if role == "tester" else unit_state.reviewer_session_id
    )
    cycle_number = unit_state.review_round
    reason = _derive_recovery_reason(unit_state)
    recovery_msg = build_recovery_prompt(role, reason, last_error=unit_state.last_error)
    active_status: Literal["testing", "reviewing"] = "testing" if role == "tester" else "reviewing"

    # Clear any prior last_error on entry — escalated → active is a fresh
    # attempt; if the worker re-emits BLOCKED, _record_terminal_marker
    # populates the new error.
    state.touch_unit(unit_state.unit_id, status=active_status, clear_error=True)
    state.record_event(
        unit_state.unit_id,
        unit_state.feature_id,
        f"{role}_resume",
        source="orchestrator",
        cycle_number=cycle_number,
        session_id=session_id,
        summary=f"Resuming {role} session ({reason})",
    )

    try:
        response = _resume_role_session(role, session_id, recovery_msg)
    except Exception as e:  # noqa: BLE001 — surface as orchestrator error
        state.touch_unit(unit_state.unit_id, status="escalated", error=str(e))
        state.record_event(
            unit_state.unit_id,
            unit_state.feature_id,
            f"{role}_resume_error",
            source="orchestrator",
            cycle_number=cycle_number,
            summary=str(e),
        )
        return f"ERROR resuming {role}: {e}"

    marker = _record_terminal_marker(
        unit_id=unit_state.unit_id,
        feature_id=unit_state.feature_id,
        role=role,
        response=response,
        session_id=session_id,
        cycle_number=cycle_number,
    )

    if marker is None:
        return _escalate_no_marker(
            unit_id=unit_state.unit_id,
            feature_id=unit_state.feature_id,
            role=role,
            cycle_number=cycle_number,
            session_id=session_id,
            response=response,
        )

    if role == "tester":
        return _format_tester_marker_response(
            unit_id=unit_state.unit_id,
            repo_path=feature.repo_path,
            pr_number=pr_number,
            session_id=session_id,
            response=response,
            marker=marker,
        )
    return _format_reviewer_marker_response(
        unit_id=unit_state.unit_id,
        repo_path=feature.repo_path,
        pr_number=pr_number,
        session_id=session_id,
        response=response,
        marker=marker,
    )


def _resume_or_spawn_tester(feature_id: str, unit_id: str) -> str:
    """Resume an existing tester session, or delegate to ``spawn_tester``.

    Predates F-009-U-3, when ``spawn_tester`` was *refusal-on-duplicate*
    rather than resume-or-spawn. Kept because ``_tester_phase``'s initial
    call site routes through this name (and the F-013-U-1 behavioural
    suite asserts the resume branch runs *without* calling ``spawn_tester``
    when the session is set — a contract this helper enforces directly).

    Behaviour parity with ``spawn_tester``'s native resume branch
    (F-009-U-3): the recovery prompt is built via :func:`build_recovery_prompt`
    rather than a per-call literal, so prompt drift between this helper
    and ``_resume_role_for_recovery`` is impossible.

    The interior retry site clears ``tester_session_id`` before calling
    ``spawn_tester`` directly, so the orphan condition cannot arise there.
    """
    unit_state = state.get_unit_state(unit_id)
    if unit_state is None or not unit_state.tester_session_id:
        return spawn_tester(feature_id, unit_id)

    # Symmetric to the `done` guard in ``_resume_role_for_recovery`` — a
    # merged unit's tester session must not be silently re-engaged, even
    # from cycle_review's internal retry path.
    if unit_state.status == "done":
        return (
            f"ERROR: unit {unit_id} is already done (PR merged). "
            f"Refusing to resume tester — use reconcile_unit_pr to refresh "
            f"state if needed, or clear tester_session_id manually if you "
            f"intend to re-open."
        )

    session_id = unit_state.tester_session_id
    cycle_number = unit_state.review_round
    feature = state.get_feature(feature_id)

    # Derive the reason from current status so an escalated unit gets the
    # post-escalation prompt (with interpolated last_error) instead of the
    # "your previous response was lost" transient-retry script. Mirrors
    # ``_resume_role_for_recovery``. Clearing last_error on entry matches
    # the same helper — touch_unit's `error=""` default is a no-op, so the
    # explicit ``clear_error=True`` is what actually wipes the column.
    reason = _derive_recovery_reason(unit_state)
    state.touch_unit(unit_id, status="testing", clear_error=True)
    state.record_event(
        unit_id,
        feature_id,
        "tester_resume",
        source="orchestrator",
        cycle_number=cycle_number,
        session_id=session_id,
        summary=f"Resuming orphaned tester session ({reason})",
    )

    try:
        response = _resume_role_session(
            "tester",
            session_id,
            build_recovery_prompt("tester", reason, last_error=unit_state.last_error),
        )
    except Exception as e:  # noqa: BLE001 — surface as orchestrator error
        state.touch_unit(unit_id, status="escalated", error=str(e))
        state.record_event(
            unit_id,
            feature_id,
            "tester_resume_error",
            source="orchestrator",
            cycle_number=cycle_number,
            summary=str(e),
        )
        return f"ERROR resuming tester: {e}"

    marker = _record_terminal_marker(
        unit_id=unit_id,
        feature_id=feature_id,
        role="tester",
        response=response,
        session_id=session_id,
        cycle_number=cycle_number,
    )

    if marker is None:
        return _escalate_no_marker(
            unit_id=unit_id,
            feature_id=feature_id,
            role="tester",
            cycle_number=cycle_number,
            session_id=session_id,
            response=response,
        )

    if marker["marker"] == "TESTS_PASS":
        if feature and unit_state.pr_number:
            # Mirrors spawn_tester:317-327 — supersede any prior
            # REQUEST_CHANGES from a BUG_FOUND cycle so branch protection's
            # "Require resolution of changes requested" rule doesn't block
            # the eventual merge, and post the operator-visible breadcrumb.
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
    if feature and unit_state.pr_number:
        safe_comment_pr(
            feature.repo_path,
            unit_state.pr_number,
            f"🚨 **Tester BLOCKED [{payload.reason}]:** {payload.prose}\n_Escalated to human._",
        )
    # JSON (not bare string) so _record_step parses outcome="BLOCKED" — keeps
    # _tester_phase's startswith("BLOCKED") short-circuit from missing into
    # the "unexpected outcome: RAW" branch.
    return json.dumps(
        {
            "unit_id": unit_id,
            "outcome": "BLOCKED",
            "reason": payload.reason,
            "prose": payload.prose,
            "session_id": session_id,
            "summary": tail(response),
        },
        indent=2,
    )


def _tester_phase(ctx: CycleContext) -> tuple[bool, str | None]:
    """Run tester until TESTS_PASS or escalation. Returns (passed, escalation_msg).

    Iterates the tester→address_review→retest loop, respecting CAP_3.
    After every coder fix push, waits for CI green before re-spawning the
    tester (the fix might break CI even if the tester would re-pass).

    On entry, an orphaned ``tester_session_id`` (typical after a previous
    cycle died on a network timeout) is resumed via
    ``_resume_or_spawn_tester`` for a cheap verdict re-emission instead of
    a fresh spawn. Both the helper and ``spawn_tester`` itself are now
    natively resume-or-spawn (F-009-U-3); the helper is retained here for
    its JSON-on-BLOCKED contract — bare ``"BLOCKED — ..."`` strings from
    ``spawn_tester`` fall through ``_record_step``'s RAW fallback, which
    would re-surface the very ``"unexpected outcome: RAW"`` regression
    F-013 fixed. The interior retry site clears the session id first and
    calls ``spawn_tester`` directly.
    """
    tester_out = _record_step(ctx, "tester", _resume_or_spawn_tester(ctx.feature_id, ctx.unit_id))
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

        # Append this cycle's events to the on-disk log before the next
        # iteration. The renderer reads state.list_events, so each call
        # picks up the FIX_PUSHED / CI / re-tester events recorded since
        # the previous write — that's the "per-cycle append" the proposal
        # specifies, materialized as a fresh atomic write.
        _write_cycle_log_safe(ctx.unit_id)

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


def _capture_reviewed_sha(ctx: CycleContext) -> None:
    """Stamp ``ctx.last_reviewed_sha`` with the current PR head SHA.

    Called right after each reviewer turn so the *next* retry's delta range
    (``prior_sha..current_sha``) anchors on what the reviewer actually saw.
    Best-effort: a transient gh API failure leaves the prior value in place
    and the delta message falls back to "(unknown — fetch via gh)".
    """
    unit_state = state.get_unit_state(ctx.unit_id)
    feature = state.get_feature(ctx.feature_id)
    if not (unit_state and feature and unit_state.pr_number and feature.repo_path):
        return
    try:
        pr_state = github.get_pr_state(feature.repo_path, unit_state.pr_number)
    except Exception:  # noqa: BLE001 — best-effort
        return
    sha = pr_state.get("head_sha")
    if sha:
        ctx.last_reviewed_sha = sha


def _resume_reviewer_for_delta(
    ctx: CycleContext,
    unit_state: WorkUnitState,
    feature: Feature,
    unit: WorkUnit,
    prior_findings: str,
    fix_summary: str,
) -> str:
    """Resume the existing reviewer session for a delta re-review.

    Replaces the pre-F-012 retry path that cleared ``reviewer_session_id``
    and cold-started a fresh sandbox (957s + 999s for back-to-back reviews on
    F-009-U-1). Keeps the session, sends a delta-scoped message, and reuses
    ``_format_reviewer_marker_response`` so the return shape matches
    ``spawn_reviewer``.

    Returns the same JSON shape as ``spawn_reviewer`` (or a ``BLOCKED — …``
    string on BLOCKED) so ``_record_step`` consumers don't need to special-
    case the retry path.
    """
    session_id = unit_state.reviewer_session_id
    if not session_id:
        return f"ERROR: no reviewer session to resume for {ctx.unit_id} — call spawn_reviewer first"
    pr_number = unit_state.pr_number
    if pr_number is None:
        return f"ERROR: no PR for {ctx.unit_id} — can't delta-review without a PR"

    try:
        pr_state = github.get_pr_state(feature.repo_path, pr_number)
        current_sha = pr_state.get("head_sha") or ""
    except Exception:  # noqa: BLE001 — best-effort; prompt has a fallback
        current_sha = ""

    delta_kwargs = _task_context_kwargs(feature, unit)
    if unit_state.review_round >= REVIEWER_OWN_LOG_MIN_ROUND:
        delta_kwargs["own_cycle_log"] = cycle_log.read_cycle_log(ctx.unit_id)
    delta_msg = compose_reviewer_delta_task(
        feature=feature,
        unit=unit,
        pr_number=pr_number,
        prior_sha=ctx.last_reviewed_sha,
        current_sha=current_sha,
        prior_findings=prior_findings,
        fix_summary=fix_summary,
        **delta_kwargs,
    )

    state.touch_unit(ctx.unit_id, status="reviewing")
    state.record_event(
        ctx.unit_id,
        ctx.feature_id,
        "reviewer_resumed_for_delta",
        source="orchestrator",
        cycle_number=unit_state.review_round,
        summary=(f"Delta review {ctx.last_reviewed_sha[:8] or '?'}..{current_sha[:8] or '?'}"),
        session_id=session_id,
    )

    try:
        worker = ManagedAgentWorker(role="reviewer")
        response = worker.resume(session_id, delta_msg)
    except Exception as e:  # noqa: BLE001 — surface as orchestrator error
        state.touch_unit(ctx.unit_id, status="escalated", error=str(e))
        state.record_event(
            ctx.unit_id,
            ctx.feature_id,
            "reviewer_resume_error",
            source="orchestrator",
            cycle_number=unit_state.review_round,
            summary=str(e),
        )
        return f"ERROR resuming reviewer: {e}"

    marker = _record_terminal_marker(
        unit_id=ctx.unit_id,
        feature_id=ctx.feature_id,
        role="reviewer",
        response=response,
        session_id=session_id,
        cycle_number=unit_state.review_round,
    )

    if marker is None:
        return _escalate_no_marker(
            unit_id=ctx.unit_id,
            feature_id=ctx.feature_id,
            role="reviewer",
            cycle_number=unit_state.review_round,
            session_id=session_id,
            response=response,
        )

    return _format_reviewer_marker_response(
        unit_id=ctx.unit_id,
        repo_path=feature.repo_path,
        pr_number=pr_number,
        session_id=session_id,
        response=response,
        marker=marker,
    )


_REVIEWER_RESUME_RECOVERY_PROMPT = (
    "Your previous response was lost to a network timeout in the orchestrator. "
    "The PR is still open and CI is currently green. Please re-evaluate the "
    "current PR head and re-emit your verdict marker (REVIEW_RECOMMEND_MERGE / "
    "REVIEW_REQUEST_CHANGES / REVIEW_COMMENT / BLOCKED) — do not redo review "
    "work you already completed. Output only the marker on its own line "
    "(with its one-line reason where applicable)."
)


def _resume_or_spawn_reviewer(feature_id: str, unit_id: str) -> str:
    """Resume an existing reviewer session, or delegate to ``spawn_reviewer``.

    Symmetric to ``_resume_or_spawn_tester`` (F-013-U-1). ``cycle_review``
    re-entering a unit whose previous ``_reviewer_phase`` died on a network
    timeout previously caused ``spawn_reviewer`` to error with "reviewer
    session already exists" — surfacing as the misleading "unexpected
    outcome: RAW" escalation via ``_record_step``'s JSON fallback. This
    helper detects the orphaned session, resumes it for a cheap verdict
    re-emission, and returns a result in the same JSON shape ``_record_step``
    expects from a fresh ``spawn_reviewer`` call (with the BLOCKED branch
    extended to JSON — see below).

    Behaviour parity with ``spawn_reviewer`` on a resume hit:
      - status flips to ``reviewing`` while the worker runs (so dashboards
        don't show the stale ``in_ci`` from cycle_review's GATE 2);
      - a ``reviewer_resume`` audit row is written before the resume call
        so operators can tell the recovery branch was chosen over a fresh
        spawn (matching the ``tester_resume`` breadcrumb from U-1);
      - REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES / REVIEW_COMMENT
        use the shared ``_format_reviewer_marker_response`` formatter so
        the PR-side endorsement comment matches ``spawn_reviewer``;
      - the BLOCKED branch posts the same PR comment as
        ``_format_reviewer_marker_response`` but returns JSON
        (``outcome="BLOCKED"``) rather than the bare ``"BLOCKED — …"``
        string. ``_reviewer_phase``'s ``outcome.startswith("BLOCKED")``
        short-circuit checks the parsed ``outcome`` field from
        ``_record_step``; a bare string falls through as ``outcome="RAW"``,
        which re-creates the very ``"unexpected outcome: RAW"`` regression
        this PR exists to prevent. The pre-existing ``spawn_reviewer`` /
        ``_resume_reviewer_for_delta`` BLOCKED bare-string return is left
        alone — fixing it is out of scope for F-013-U-2.

    Only the ``_reviewer_phase`` initial-call site uses this. The retry
    site goes through ``_resume_reviewer_for_delta`` (F-012-U-2) which
    keeps the existing session for a delta re-review.
    """
    unit_state = state.get_unit_state(unit_id)
    if unit_state is None or not unit_state.reviewer_session_id:
        return spawn_reviewer(feature_id, unit_id)

    session_id = unit_state.reviewer_session_id
    cycle_number = unit_state.review_round
    feature = state.get_feature(feature_id)

    state.touch_unit(unit_id, status="reviewing")
    state.record_event(
        unit_id,
        feature_id,
        "reviewer_resume",
        source="orchestrator",
        cycle_number=cycle_number,
        session_id=session_id,
        summary="Resuming orphaned reviewer session",
    )

    try:
        response = _resume_role_session("reviewer", session_id, _REVIEWER_RESUME_RECOVERY_PROMPT)
    except Exception as e:  # noqa: BLE001 — surface as orchestrator error
        state.touch_unit(unit_id, status="escalated", error=str(e))
        state.record_event(
            unit_id,
            feature_id,
            "reviewer_resume_error",
            source="orchestrator",
            cycle_number=cycle_number,
            summary=str(e),
        )
        return f"ERROR resuming reviewer: {e}"

    marker = _record_terminal_marker(
        unit_id=unit_id,
        feature_id=feature_id,
        role="reviewer",
        response=response,
        session_id=session_id,
        cycle_number=cycle_number,
    )

    if marker is None:
        return _escalate_no_marker(
            unit_id=unit_id,
            feature_id=feature_id,
            role="reviewer",
            cycle_number=cycle_number,
            session_id=session_id,
            response=response,
        )

    if marker["marker"] == "BLOCKED":
        payload = marker["payload"]
        if feature and unit_state.pr_number:
            safe_comment_pr(
                feature.repo_path,
                unit_state.pr_number,
                f"🚨 **Reviewer BLOCKED [{payload.reason}]:** {payload.prose}\n"
                f"_Escalated to human._",
            )
        # JSON (not bare string like spawn_reviewer) so _record_step parses
        # outcome="BLOCKED" — keeps _reviewer_phase's startswith("BLOCKED")
        # short-circuit from missing into the "unexpected outcome: RAW" branch.
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "BLOCKED",
                "reason": payload.reason,
                "prose": payload.prose,
                "session_id": session_id,
                "summary": tail(response),
            },
            indent=2,
        )

    # REVIEW_RECOMMEND_MERGE / REVIEW_REQUEST_CHANGES / REVIEW_COMMENT — the
    # marker is already parsed and recorded; degrade gracefully when feature
    # or pr_number is missing rather than escalating a clean verdict via
    # _escalate_no_marker. The formatter guards its PR-side writes on the
    # Optional repo_path/pr_number, so a None pair skips the PR comment but
    # still returns the JSON outcome _record_step expects.
    return _format_reviewer_marker_response(
        unit_id=unit_id,
        repo_path=feature.repo_path if feature else None,
        pr_number=unit_state.pr_number,
        session_id=session_id,
        response=response,
        marker=marker,
    )


def _reviewer_phase(ctx: CycleContext) -> tuple[bool, str | None, str | None]:
    """Run reviewer until approved/recommend-merge/comment or escalation.

    Returns ``(approved, escalation_msg, final_outcome)``. ``final_outcome``
    is the last reviewer marker string (``REVIEW_RECOMMEND_MERGE`` /
    ``REVIEW_COMMENT`` / ``REVIEW_REQUEST_CHANGES`` / ``BLOCKED…``) so the
    caller can branch (e.g. ``cycle_review`` fires the ultrareview gate only
    on ``REVIEW_RECOMMEND_MERGE``).

    Iterates the fix-loop on REVIEW_REQUEST_CHANGES, respecting CAP_3. The
    *first* reviewer turn is a spawn-or-resume via
    ``_resume_or_spawn_reviewer`` — a cold-start ``spawn_reviewer`` when no
    ``reviewer_session_id`` is on the unit, or a cheap verdict re-emission
    when an orphaned session is present (typical after a previous cycle died
    on a network timeout; symmetric to the ``_tester_phase`` recovery added
    in F-013-U-1). Every subsequent retry inside the REVIEW_REQUEST_CHANGES
    fix-loop is a session resume via ``_resume_reviewer_for_delta``
    (F-012-U-2 — avoids re-paying the ~900s clone+inventory on every cycle).

    Each coder fix push waits for CI green before re-running the reviewer.
    """
    reviewer_out = _record_step(
        ctx, "reviewer", _resume_or_spawn_reviewer(ctx.feature_id, ctx.unit_id)
    )
    outcome = reviewer_out.get("outcome")

    if isinstance(outcome, str) and outcome.startswith("BLOCKED"):
        return False, "reviewer blocked", outcome

    while outcome == "REVIEW_REQUEST_CHANGES":
        # Anchor the next delta range on what the reviewer just saw. Done
        # inside the loop (not after the initial spawn) so a clean first
        # review skips the gh GET — the anchor is only consulted on retry.
        _capture_reviewed_sha(ctx)

        unit_state = state.get_unit_state(ctx.unit_id)
        if unit_state is None or unit_state.review_round >= CAP_3:
            return False, f"cap of {CAP_3} cycles hit while addressing reviewer", outcome

        prior_findings = reviewer_out.get("issue", "")
        fix_out = _record_step(
            ctx,
            "address_review (reviewer changes)",
            address_review(ctx.unit_id, "reviewer", prior_findings),
        )
        if fix_out.get("outcome") != "FIX_PUSHED":
            return False, "coder fix (for review) did not succeed", outcome

        ok, msg = _wait_ci_with_fix_loop(ctx, "reviewer-changes fix push")
        if not ok:
            return False, msg, outcome

        # Per-cycle append (see matching call in _tester_phase).
        _write_cycle_log_safe(ctx.unit_id)

        # Resume the existing reviewer session for a delta re-review rather
        # than clearing reviewer_session_id + cold-starting. The session
        # already holds the PR inventory + prior verdict; we just send a
        # delta-scoped message (see compose_reviewer_delta_task).
        unit_state = state.get_unit_state(ctx.unit_id)
        feature = state.get_feature(ctx.feature_id)
        plan = state.get_plan(ctx.feature_id)
        unit = next((u for u in plan.units if u.id == ctx.unit_id), None) if plan else None
        if unit_state is None or feature is None or unit is None:
            return False, "unit state vanished mid-cycle", outcome

        reviewer_out = _record_step(
            ctx,
            "reviewer (delta resume)",
            _resume_reviewer_for_delta(
                ctx,
                unit_state,
                feature,
                unit,
                prior_findings=prior_findings,
                fix_summary=fix_out.get("summary", ""),
            ),
        )
        outcome = reviewer_out.get("outcome")
        if isinstance(outcome, str) and outcome.startswith("BLOCKED"):
            return False, "reviewer blocked on retry", outcome

    if outcome in ("REVIEW_COMMENT", "REVIEW_RECOMMEND_MERGE"):
        return True, None, outcome

    return False, f"reviewer ended with unexpected outcome: {outcome}", outcome


def _truncate_findings_for_details(findings: list[str], budget: int = 1500) -> str:
    """Join ``findings`` newline-separated, keeping whole entries up to ``budget`` chars.

    Char-slicing the joined blob (``"\\n".join(findings)[:budget]``) cuts mid-
    finding for long lists, leaving event-log readers with a corrupted final
    entry. Slicing per-finding instead preserves complete entries and appends
    an explicit ``... (N more findings truncated)`` marker so the cost-
    attribution / postmortem queries that read ``unit_events.details`` see
    that the list was capped rather than mistaking the missing items for
    "ultrareview only found this many".
    """
    if not findings:
        return "(no findings reported)"
    kept: list[str] = []
    used = 0
    for f in findings:
        # +1 for the newline between this entry and the previous one (only
        # charged when there's already at least one entry — matches the
        # char count of "\n".join).
        sep = 1 if kept else 0
        if used + sep + len(f) > budget and kept:
            kept.append(f"... ({len(findings) - len(kept)} more findings truncated)")
            break
        kept.append(f)
        used += sep + len(f)
    return "\n".join(kept)


def _ultrareview_phase(ctx: CycleContext) -> tuple[bool, str | None]:
    """Fire ``/ultrareview`` as the terminal pre-merge gate (F-007).

    Called only when ``feature.ultrareview_enabled`` is on AND the reviewer
    endorsed via ``REVIEW_RECOMMEND_MERGE``. Triggers the subprocess wrapper,
    blocks on the verdict, and surfaces findings on FAIL.

    Returns ``(passed, escalation_msg)``. On PASS the caller terminates as
    ``approved_awaiting_merge``. On FAIL this initial impl escalates so the
    user can decide manually — the full FAIL fix-loop (with cap-3
    accounting and ``address_review(source='ultrareview', ...)``) ships in
    F-007-U-4.

    Events recorded for cost attribution + cycle-log entries:
      * ``ultrareview_started`` — always, just before trigger.
      * ``ultrareview_passed`` — on PASS verdict.
      * ``ultrareview_failed`` — on every False-return path (verdict FAIL,
        wrapper exception, defensive missing-PR-URL), with findings (or the
        exception message) in ``details``. Every escalation has an event
        trail explaining why.

    Fail-closed on wrapper exceptions: a missing CLI, parse error, or any
    other ``ultrareview`` raise is treated as a failed gate, never as a
    silent endorsement.
    """
    unit_state = state.get_unit_state(ctx.unit_id)
    cycle_number = unit_state.review_round if unit_state else 0
    pr_url = _pr_url_for(ctx.feature_id, unit_state)
    if pr_url is None:
        # Defensive: a reviewer that endorsed has a PR, so this branch is
        # nearly unreachable. Still record an ultrareview_failed event so
        # every False-return path leaves a telemetry trail — the resulting
        # escalation should never land in the user's notification with no
        # event log explaining why.
        state.record_event(
            ctx.unit_id,
            ctx.feature_id,
            "ultrareview_failed",
            source="ultrareview",
            cycle_number=cycle_number,
            summary="ultrareview gate: no PR URL available",
        )
        ctx.history.append({"step": "ultrareview", "outcome": "error", "error": "no PR URL"})
        return False, "ultrareview gate: no PR URL available"

    state.record_event(
        ctx.unit_id,
        ctx.feature_id,
        "ultrareview_started",
        source="ultrareview",
        cycle_number=cycle_number,
        summary="firing /ultrareview",
        details=pr_url,
    )
    ctx.history.append({"step": "ultrareview_started", "pr_url": pr_url})

    try:
        ultrareview.trigger(pr_url)
        result = ultrareview.wait_for_result(pr_url)
    except Exception as e:  # noqa: BLE001 — fail-closed: any wrapper raise = gate fails
        state.record_event(
            ctx.unit_id,
            ctx.feature_id,
            "ultrareview_failed",
            source="ultrareview",
            cycle_number=cycle_number,
            summary=f"ultrareview wrapper error: {e}",
        )
        ctx.history.append({"step": "ultrareview", "outcome": "error", "error": str(e)})
        return False, f"ultrareview wrapper error: {e}"

    findings = list(result.get("findings", []))
    if result.get("passed"):
        state.record_event(
            ctx.unit_id,
            ctx.feature_id,
            "ultrareview_passed",
            source="ultrareview",
            cycle_number=cycle_number,
            summary="ultrareview passed",
        )
        ctx.history.append({"step": "ultrareview", "outcome": "passed"})
        return True, None

    findings_text = "\n".join(findings) if findings else "(no findings reported)"
    state.record_event(
        ctx.unit_id,
        ctx.feature_id,
        "ultrareview_failed",
        source="ultrareview",
        cycle_number=cycle_number,
        summary=f"ultrareview failed with {len(findings)} findings",
        details=_truncate_findings_for_details(findings),
    )
    ctx.history.append({"step": "ultrareview", "outcome": "failed", "findings": findings})
    return False, f"ultrareview failed:\n{findings_text}"


# --------------------------- F-016 Phase 2: phase commands ---------------------------
#
# Status sets define each phase's idempotence boundary: a unit whose status
# falls in the bucket has already moved past that phase, so the corresponding
# ``advance_to_X`` returns an ``already_past`` no-op. Single source of truth
# for the daemon (F-016-U-5) and the lead.
#
# Why ``approved_awaiting_merge`` is NOT in ``_PAST_TERMINAL_STATUSES``: the
# REVIEW_RECOMMEND_MERGE marker flips status to ``approved_awaiting_merge``
# BEFORE ``advance_to_terminal`` runs; treating it as already-past on the
# status side alone would skip the ntfy push + cycle-log finalize in
# ``_emit_terminal``. Re-call dedupe is layered instead via
# ``_terminal_already_emitted`` (the ``cycle_terminal_emitted`` event row
# ``_emit_terminal`` records on its first success); a daemon re-tick during
# the human-merge waiting window reads that event and short-circuits to
# ``already_past`` without re-pushing ntfy (PR #58 H1). The narrow status
# bucket + the event-based dedupe together fence the firing window from
# both sides.
_PAST_TESTER_STATUSES: frozenset[str] = frozenset(
    {"reviewing", "fixing", "approved_awaiting_merge", "done"}
)
_PAST_REVIEWER_STATUSES: frozenset[str] = frozenset({"approved_awaiting_merge", "done"})
_PAST_TERMINAL_STATUSES: frozenset[str] = frozenset({"done"})


def _not_ready_response(unit_id: str) -> str:
    """JSON when ``advance_to_X`` is called before ``spawn_unit`` ran.

    The unit row doesn't exist yet — no PR, no worker session. Distinct
    from ``escalated`` (a terminal failure with last_error context) so a
    Phase-3 daemon can branch on it: ``not_ready`` means "wait" or
    "spawn first", not "human triage needed".
    """
    return json.dumps(
        {
            "unit_id": unit_id,
            "outcome": "not_ready",
            "message": f"no state for unit {unit_id} — call spawn_unit first",
        },
        indent=2,
    )


def _already_past_response(unit_id: str, unit_state: WorkUnitState, next_action: str | None) -> str:
    """JSON for a no-op ``advance_to_X`` call.

    Shape mirrors the success-advance JSON so callers (lead chat + daemon)
    don't need to special-case the idempotent path: same ``unit_id`` /
    ``status`` / ``next_action`` keys, distinguished only by
    ``outcome == "already_past"``.
    """
    return json.dumps(
        {
            "unit_id": unit_id,
            "outcome": "already_past",
            "status": unit_state.status,
            "next_action": next_action,
        },
        indent=2,
    )


def _escalated_response(unit_id: str, unit_state: WorkUnitState) -> str:
    """JSON for an ``advance_to_X`` call on an already-escalated unit.

    Surfaces ``last_error`` so the lead can see why the unit was escalated
    without an extra ``unit_history`` round-trip. Distinct outcome from
    ``already_past`` so a Phase-3 daemon can branch on it: escalated units
    need a human decision, not the next advance.
    """
    return json.dumps(
        {
            "unit_id": unit_id,
            "outcome": "escalated",
            "status": "escalated",
            "message": unit_state.last_error or "unit already escalated",
        },
        indent=2,
    )


def _advance_response(unit_id: str, next_action: str, ctx: CycleContext, **extras: Any) -> str:
    """JSON for a successful ``advance_to_X`` mid-pipeline advance.

    Intermediate phases (tester, reviewer) don't emit the terminal — that's
    ``advance_to_terminal``'s job. This helper packages the post-phase
    status + accumulated history into a uniform shape for the chat and the
    daemon's reconcile loop.
    """
    refreshed = state.get_unit_state(unit_id)
    body: dict[str, Any] = {
        "unit_id": unit_id,
        "outcome": "advanced",
        "status": refreshed.status if refreshed else "unknown",
        "next_action": next_action,
        "history": ctx.history,
    }
    body.update(extras)
    return json.dumps(body, indent=2)


def _last_reviewer_outcome(unit_id: str) -> str | None:
    """Most-recent reviewer terminal marker for the unit, as a marker name.

    Returns ``"REVIEW_RECOMMEND_MERGE"`` / ``"REVIEW_COMMENT"`` /
    ``"REVIEW_REQUEST_CHANGES"`` or ``None`` if no reviewer marker has
    landed yet. Used by ``advance_to_terminal`` to decide whether to fire
    the F-007 ultrareview gate — only endorsements (RECOMMEND_MERGE)
    trigger it. Reads events rather than threading the outcome through a
    separate state column so the lead+daemon callers don't need to share
    in-memory context across MCP calls.
    """
    events = state.list_events(unit_id)
    # Events are oldest-first; we want the most recent reviewer marker.
    marker_for_event = {
        "reviewer_recommend_merge": "REVIEW_RECOMMEND_MERGE",
        "reviewer_comment": "REVIEW_COMMENT",
        "reviewer_request_changes": "REVIEW_REQUEST_CHANGES",
    }
    for event in reversed(events):
        marker = marker_for_event.get(event.get("event_type", ""))
        if marker is not None:
            return marker
    return None


def _terminal_already_emitted(unit_id: str) -> bool:
    """True iff ``_emit_terminal`` has already fired the
    ``approved_awaiting_merge`` ntfy push for this unit.

    Checks for the ``cycle_terminal_emitted`` event ``_emit_terminal``
    records via the per-unit dedupe_key. The single-row read lets
    ``advance_to_terminal`` short-circuit a daemon re-tick (PR #58 H1)
    against a unit waiting hours/days for the human merge: status sits
    in ``approved_awaiting_merge`` for the duration but the dedupe event
    pins "the terminal already fired", so the second call returns
    ``already_past`` rather than re-pushing the ntfy.
    """
    return any(e.get("event_type") == "cycle_terminal_emitted" for e in state.list_events(unit_id))


def _run_tester_advance(ctx: CycleContext) -> tuple[bool, str | None]:
    """Tester-phase work: GATE 1 (CI on coder push) → tester → GATE 2.

    Returns ``(success, escalation_msg)``. Shared by ``advance_to_tester``
    and the ``cycle_review`` wrapper so both paths walk the same engine
    (spec § "No parallel state machine").
    """
    ok, msg = _wait_ci_with_fix_loop(ctx, "coder PR push")
    if not ok:
        return False, msg or "CI gate failed before tester"

    passed, msg = _tester_phase(ctx)
    if not passed:
        return False, msg or "tester phase failed"

    ok, msg = _wait_ci_with_fix_loop(ctx, "tester test push")
    if not ok:
        return False, msg or "CI gate failed before reviewer"

    return True, None


def _run_reviewer_advance(ctx: CycleContext) -> tuple[bool, str | None, str | None]:
    """Reviewer-phase work: copilot best-effort + reviewer fix-loop.

    Returns ``(success, escalation_msg, reviewer_outcome)`` where
    ``reviewer_outcome`` is the final reviewer marker name
    (``"REVIEW_RECOMMEND_MERGE"`` / ``"REVIEW_COMMENT"`` / ``"BLOCKED…"``)
    so callers can branch on endorsement vs. comment-only.
    """
    _copilot_phase(ctx)
    approved, msg, reviewer_outcome = _reviewer_phase(ctx)
    if not approved:
        return False, msg or "reviewer phase failed", reviewer_outcome
    return True, None, reviewer_outcome


def _run_terminal_advance(
    ctx: CycleContext, reviewer_outcome: str | None
) -> tuple[bool, str | None]:
    """Terminal-phase work: optional ultrareview gate per F-007.

    Doesn't emit the terminal itself — the caller does, so the wrapper can
    share one ``_emit_terminal`` call (one ntfy push, one cycle-log write)
    across the three phases. ``reviewer_outcome`` gates the ultrareview
    fire: only ``REVIEW_RECOMMEND_MERGE`` triggers the audit (comment-only
    terminals aren't endorsements; the gate stays quiet).
    """
    feature = state.get_feature(ctx.feature_id)
    if (
        reviewer_outcome == "REVIEW_RECOMMEND_MERGE"
        and feature is not None
        and feature.ultrareview_enabled
    ):
        ur_passed, ur_msg = _ultrareview_phase(ctx)
        if not ur_passed:
            return False, ur_msg or "ultrareview phase failed"
    return True, None


_TERMINAL_SUCCESS_MSG = (
    "Review terminal (approved/comment/recommend_merge), CI green. PR awaits human merge."
)


@mcp.tool()
def advance_to_tester(feature_id: str, unit_id: str) -> str:
    """Phase 1 of the cycle pipeline: wait for CI green → tester → CI green.

    Idempotent on current ``WorkUnitState.status``: returns ``already_past``
    when the unit has moved past the tester boundary (status in
    ``{reviewing, fixing, approved_awaiting_merge, done}``). On escalation,
    fires the same terminal handler as ``cycle_review`` so the cycle log +
    ntfy push land.

    Returns JSON ``{unit_id, outcome, status, next_action, ...}`` where
    ``outcome`` is one of ``advanced`` / ``already_past`` / ``escalated`` /
    ``not_ready``. F-016 Phase 2 — pair with ``advance_to_reviewer`` and
    ``advance_to_terminal`` to drive the pipeline phase-by-phase.
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    unit_state = state.get_unit_state(unit_id)
    if unit_state is None:
        return _not_ready_response(unit_id)

    if unit_state.status == "escalated":
        return _escalated_response(unit_id, unit_state)

    if unit_state.status in _PAST_TESTER_STATUSES:
        return _already_past_response(unit_id, unit_state, next_action="advance_to_reviewer")

    ctx = CycleContext(feature_id=feature_id, unit_id=unit_id, history=[])
    ok, msg = _run_tester_advance(ctx)
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "tester phase failed")

    return _advance_response(unit_id, next_action="advance_to_reviewer", ctx=ctx)


@mcp.tool()
def advance_to_reviewer(feature_id: str, unit_id: str) -> str:
    """Phase 2 of the cycle pipeline: Copilot review (best-effort) + reviewer.

    Idempotent on current ``WorkUnitState.status``: returns ``already_past``
    when status is in ``{approved_awaiting_merge, done}``. On reviewer
    BLOCKED or cap-3 hit, fires the terminal handler. Mid-flight restart
    (lead killed between tester and reviewer phases) is supported via
    ``_resume_or_spawn_reviewer`` — the existing reviewer session is reused
    for a cheap verdict re-emission rather than a cold spawn (F-013-U-2).
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    unit_state = state.get_unit_state(unit_id)
    if unit_state is None:
        return _not_ready_response(unit_id)

    if unit_state.status == "escalated":
        return _escalated_response(unit_id, unit_state)

    if unit_state.status in _PAST_REVIEWER_STATUSES:
        return _already_past_response(unit_id, unit_state, next_action="advance_to_terminal")

    ctx = CycleContext(feature_id=feature_id, unit_id=unit_id, history=[])
    ok, msg, reviewer_outcome = _run_reviewer_advance(ctx)
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "reviewer phase failed")

    return _advance_response(
        unit_id,
        next_action="advance_to_terminal",
        ctx=ctx,
        reviewer_outcome=reviewer_outcome,
    )


@mcp.tool()
def advance_to_terminal(feature_id: str, unit_id: str) -> str:
    """Phase 3 of the cycle pipeline: optional F-007 ultrareview gate, then
    emit the terminal (ntfy push + cycle-log finalize + status flip).

    **Reviewer-marker precondition (PR #58 C1).** The success path requires
    a prior ``reviewer_recommend_merge`` or ``reviewer_comment`` event for
    this unit. Without one, the call returns ``not_ready`` and does NOT
    flip status, fire ntfy, or finalize cycle-log — exactly the false
    "ready to merge" notification the daemon (F-016-U-5) would otherwise
    push on every poll of a unit still in ``coding`` / ``testing`` /
    ``in_ci`` / ``reviewing`` / ``fixing``. ``reviewer_request_changes``
    and the absence of a marker both fall under "no terminal possible
    yet"; the caller's right next step is ``advance_to_reviewer``.

    **Idempotence on terminal emit (PR #58 H1).** The first successful
    terminal-emit records a ``cycle_terminal_emitted`` event keyed on the
    unit; subsequent calls (daemon re-ticks, accidental double
    ``cycle_review``) read the event in the early already-emitted check
    and return ``already_past`` without re-firing the ntfy push. The
    ``status='done'`` short-circuit handles the post-merge path; the
    event check handles the ``approved_awaiting_merge`` waiting window
    where a unit can sit for hours/days before a human merges.

    Reads the latest reviewer marker from ``unit_events`` to decide
    whether to fire ultrareview — only ``REVIEW_RECOMMEND_MERGE``
    endorsements trigger it (REVIEW_COMMENT terminals are comment-only,
    not endorsements).
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    unit_state = state.get_unit_state(unit_id)
    if unit_state is None:
        return _not_ready_response(unit_id)

    if unit_state.status == "escalated":
        return _escalated_response(unit_id, unit_state)

    if unit_state.status in _PAST_TERMINAL_STATUSES:
        return _already_past_response(unit_id, unit_state, next_action=None)

    # H1 dedupe: if a previous _emit_terminal already fired the ntfy push
    # and flipped status, the dedupe-keyed cycle_terminal_emitted event
    # lives in unit_events. Treat the unit as already_past so the daemon's
    # repeated calls during the human-merge waiting window are no-ops.
    if _terminal_already_emitted(unit_id):
        return _already_past_response(unit_id, unit_state, next_action=None)

    reviewer_outcome = _last_reviewer_outcome(unit_id)

    # C1 precondition: no reviewer endorsement / comment means the
    # terminal phase is not the next action. Returning not_ready (rather
    # than silently emitting a false-positive "ready to merge") is the
    # contract every misuse route (lead typo, daemon speculative tick,
    # test forgetting to seed events) needs.
    if reviewer_outcome not in ("REVIEW_RECOMMEND_MERGE", "REVIEW_COMMENT"):
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "not_ready",
                "status": unit_state.status,
                "message": (
                    "no reviewer marker yet — run advance_to_reviewer first "
                    "(latest reviewer event: " + (reviewer_outcome or "none") + ")"
                ),
            },
            indent=2,
        )

    ctx = CycleContext(feature_id=feature_id, unit_id=unit_id, history=[])
    ok, msg = _run_terminal_advance(ctx, reviewer_outcome)
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "ultrareview phase failed")

    return _emit_terminal(ctx, "approved_awaiting_merge", _TERMINAL_SUCCESS_MSG)


@mcp.tool()
def cycle_review(feature_id: str, unit_id: str) -> str:
    """Convenience wrapper: ``advance_to_tester`` → ``advance_to_reviewer``
    → ``advance_to_terminal``, sharing one ``CycleContext`` so history,
    ntfy push, and cycle-log finalize happen exactly once.

    The post-spawn loop:
      tester → (if BUG: address_review → tester) → Copilot review →
      our reviewer → (if CHANGES: address_review → reviewer) → optional
      ultrareview gate (if feature.ultrareview_enabled) → terminal.

    Cap = CAP_3 shared cycles across tester-bugs and reviewer-changes.
    On cap hit or any BLOCKED: marks escalated, returns summary.
    BLOCKS until terminal (success or escalation). Typically 5-20+ minutes.

    Repo must be fresh-verified (call `verify_repo(<url>)` if blocked).

    Re-entry recovery: if a previous ``cycle_review`` call died mid-phase
    (typically a network timeout) leaving the unit with a non-empty
    ``tester_session_id`` or ``reviewer_session_id`` but no terminal marker
    recorded, the initial ``_tester_phase`` / ``_reviewer_phase`` call on
    re-entry resumes the existing session (asking it to re-emit its
    verdict) rather than spawning a fresh one. See
    ``docs/STATE-MACHINE-AUDIT.md``
    § "Stale-session recovery at tester/reviewer re-entry".

    Why a shared ``CycleContext`` rather than three nested MCP calls: the
    three ``_run_*_advance`` helpers are the engine, the MCP tools are
    thin wrappers around them with their own idempotence checks. Sharing
    one ctx keeps history contiguous and lets ``_emit_terminal`` fire
    exactly once with the full timeline — spec § "No parallel state
    machine" makes the engine the contract, not the MCP-call topology.
    """
    if err := ensure_verified_for_feature(feature_id):
        return err

    ctx = CycleContext(feature_id=feature_id, unit_id=unit_id, history=[])

    ok, msg = _run_tester_advance(ctx)
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "tester phase failed")

    ok, msg, reviewer_outcome = _run_reviewer_advance(ctx)
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "reviewer phase failed")

    ok, msg = _run_terminal_advance(ctx, reviewer_outcome)
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "ultrareview phase failed")

    return _emit_terminal(ctx, "approved_awaiting_merge", _TERMINAL_SUCCESS_MSG)


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


# --------------------------- F-016 Phase 2.5: lead/daemon contract ---------------------------
#
# Three primitives the lead has at runtime to influence a unit while the
# (Phase 3) daemon drives it:
#
#   * ``send_to_unit_async`` — submit-only message; holds the per-unit
#     advance-lock for the ~1s submit window so the daemon's tick
#     doesn't race ``advance_state_machine`` against an in-flight send.
#   * ``cancel_unit`` — sticky cancel; archives every role's session and
#     marks the unit ``cancelled`` so the daemon's next tick stops
#     driving it.
#
# The graph-mutation primitive ``update_unit_deps`` lives in
# ``orchestrator/tools/planning.py`` because it edits the plan, not the
# work-unit row.


# Per the spec § "Routing rule for send_to_unit(unit_id, message, role=None)":
# default role by current ``WorkUnitState.status``. Statuses where no
# role is actionable map to ``None`` so :func:`_resolve_default_role`
# returns ``None`` and :func:`send_to_unit_async` surfaces a structured
# error rather than picking a wrong role.
_DEFAULT_ROLE_BY_STATUS: dict[str, str | None] = {
    "coding": "coder",
    "opening_pr": "coder",
    "in_ci": "coder",
    "fixing": "coder",
    "testing": "tester",
    "reviewing": "reviewer",
    "escalated": "coder",
    "approved_awaiting_merge": None,
    "done": None,
    "cancelled": None,
}


def _role_session_status(unit_state: WorkUnitState, role: str) -> dict[str, Any]:
    """Per-role actionability digest for :func:`send_to_unit_async`.

    Returns ``{"status": str, "actionable": bool}`` per the spec's
    "Not-actionable delivery responses" section. Phase 2.5 ships a
    *coarse* signal — non-empty ``session_id`` resolves to
    ``actionable: True`` regardless of whether the worker session is
    actually idle, terminated, or archived. The F-016-U-5 daemon will
    refine this via the F-014 unit-health probe; this deferral is
    documented in the PR description's ``## Spec satisfaction →
    Deviations`` so a caller relying on the spec's four-case shape
    knows the gap.

    The return shape stays narrow on purpose: ``{status, actionable}``
    only — no ``session_id`` field, because the diagnostics payload is
    surfaced to the lead and leaking the worker session identifier
    through an error-response surface is more than the docstring
    contract promises.
    """
    sid = _resolve_session_id(unit_state, role)
    if not sid:
        return {"status": "no_session", "actionable": False}
    # Phase 2.5 deferral: terminated / archived collapse to "idle" with
    # actionable=True. The spec's four-case shape requires probing the
    # backend's session-status surface; that lands in F-016-U-5.
    return {"status": "idle", "actionable": True}


def _role_diagnostics_payload(unit_state: WorkUnitState) -> dict[str, dict[str, Any]]:
    """``{role: {status, actionable, ...}}`` for every worker role."""
    return {
        role: _role_session_status(unit_state, role) for role in ("coder", "tester", "reviewer")
    }


def _resolve_default_role(unit_state: WorkUnitState) -> str | None:
    """Pick the default role for an unspecified ``send_to_unit_async`` call.

    Returns one of ``"coder"`` / ``"tester"`` / ``"reviewer"`` per the
    spec's routing table, or ``None`` when the current status has no
    actionable role (terminal: ``approved_awaiting_merge`` / ``done`` /
    ``cancelled``). The caller surfaces ``None`` as a structured error.
    """
    return _DEFAULT_ROLE_BY_STATUS.get(unit_state.status)


@mcp.tool()
def send_to_unit_async(unit_id: str, message: str, role: str = "") -> str:
    """Submit a follow-up message to a worker session WITHOUT waiting for the reply.

    Async counterpart to :func:`send_to_unit`. Per the spec, ``send_to_unit``
    becomes a ~1s submit window once ``worker.resume_async`` is available:
    the user-message event lands on the worker's queue, the per-unit
    advance-lock is held during the submit so a Phase 3 daemon doesn't
    race ``advance_state_machine`` on the same tick, and the worker's
    response arrives later via the daemon's normal poll (or via
    :func:`wait_unit` if the lead chooses to block).

    Routing — when ``role`` is empty, the default is picked from the
    unit's current ``WorkUnitState.status`` per the spec's routing table::

        coding | in_ci | fixing | escalated -> coder
        testing                              -> tester
        reviewing                            -> reviewer
        approved_awaiting_merge | done | cancelled -> ERROR (terminal)

    The lead overrides explicitly when its intent diverges from the
    current phase. No content heuristics, no auto-fallback to a
    different role on delivery failure — silently sending to the wrong
    role would corrupt intent worse than a structured error.

    Returns JSON. Successful submit::

        {
          "delivered": true,
          "unit_id": "...",
          "role": "coder",
          "session_id": "sess_abc...",
          "advance_lock": "released"
        }

    Not-actionable delivery (per spec §"Not-actionable delivery responses")::

        {
          "delivered": false,
          "reason": "<unit_terminal|unit_cancelled|no_session|no_default_role|...>",
          "role_diagnostics": {
              "coder":    {"status": "...", "actionable": <bool>},
              "tester":   {"status": "...", "actionable": <bool>},
              "reviewer": {"status": "...", "actionable": <bool>}
          },
          "next_steps": ["...", "..."]
        }

    Repo must be fresh-verified (call ``verify_repo(<url>)`` if blocked).
    """
    if err := ensure_verified_for_unit(unit_id):
        return err

    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for unit {unit_id}"

    # Terminal-status refusal (PR #59 Copilot finding 1). A unit in
    # ``done`` / ``approved_awaiting_merge`` / ``cancelled`` must NOT
    # receive new messages regardless of whether the caller passed an
    # explicit ``role`` — burning an API call on a terminal unit is
    # the exact failure mode the spec's routing table ("done | cancelled
    # → error — unit is terminal", "approved_awaiting_merge → error —
    # PR is done") guards against. The role-resolution short-circuit
    # below only fires when ``role==""``, so without this gate an
    # explicit ``role="coder"`` would slip past. ``escalated`` is
    # intentionally NOT in this set — the spec routes escalated → coder
    # so the lead can hand-hold the coder through triage.
    if state.is_cancelled(unit_id):
        return json.dumps(
            {
                "delivered": False,
                "reason": "unit_cancelled",
                "unit_id": unit_id,
                "role_diagnostics": _role_diagnostics_payload(unit_state),
                "next_steps": [
                    "unit was cancelled via cancel_unit; spawn fresh or pick a different unit",
                ],
            },
            indent=2,
        )

    if unit_state.status in ("done", "approved_awaiting_merge"):
        return json.dumps(
            {
                "delivered": False,
                "reason": "unit_terminal",
                "unit_id": unit_id,
                "status": unit_state.status,
                "role_diagnostics": _role_diagnostics_payload(unit_state),
                "next_steps": [
                    f"unit is {unit_state.status!r}; no role can receive a message",
                ],
            },
            indent=2,
        )

    if role == "":
        default_role = _resolve_default_role(unit_state)
        if default_role is None:
            return json.dumps(
                {
                    "delivered": False,
                    "reason": "no_default_role",
                    "unit_id": unit_id,
                    "status": unit_state.status,
                    "role_diagnostics": _role_diagnostics_payload(unit_state),
                    "next_steps": [
                        "unit is in a terminal status; no role can receive a message",
                    ],
                },
                indent=2,
            )
        role = default_role

    if role not in ("coder", "tester", "reviewer"):
        return f"ERROR: role must be coder|tester|reviewer, got {role!r}"

    session_id = _resolve_session_id(unit_state, role)
    if not session_id:
        return json.dumps(
            {
                "delivered": False,
                "reason": f"no_{role}_session",
                "unit_id": unit_id,
                "role": role,
                "role_diagnostics": _role_diagnostics_payload(unit_state),
                "next_steps": [
                    f"{role} has not been spawned for {unit_id}; "
                    "use spawn_unit / spawn_tester / spawn_reviewer first",
                ],
            },
            indent=2,
        )

    # Hold the advance-lock for the ~1s submit window. Per spec the lock
    # is unit-wide (not per-role) so a same-tick daemon doesn't advance
    # the reviewer while the coder is mid-receive. The DB-side
    # ``owner='lead'`` write makes the claim visible to the Phase 3
    # daemon (separate process); the in-process RLock serializes
    # concurrent leads in the same MCP server.
    with state.lead_advance_lock(unit_id):
        try:
            worker = make_worker(role)
            worker.resume_async(session_id, message)
        except Exception as e:  # noqa: BLE001 — surface as orchestrator error
            state.touch_unit(unit_id, error=str(e))
            return json.dumps(
                {
                    "delivered": False,
                    "reason": f"{role}_resume_async_error",
                    "unit_id": unit_id,
                    "role": role,
                    "session_id": session_id,
                    "error": str(e),
                    "role_diagnostics": _role_diagnostics_payload(unit_state),
                    "next_steps": [
                        f"{role} session rejected the resume — check unit_history "
                        f"for the worker's last messages, then retry or escalate",
                    ],
                },
                indent=2,
            )

        # Audit row mirrors the synchronous ``send_to_unit`` path so the
        # event log shows lead-issued messages whether the lead waited
        # for the reply or not.
        _record_manual_message(
            unit_id=unit_id,
            feature_id=unit_state.feature_id,
            role=role,
            session_id=session_id,
            message=message,
            cycle_number=unit_state.review_round,
        )

    return json.dumps(
        {
            "delivered": True,
            "unit_id": unit_id,
            "role": role,
            "session_id": session_id,
            "advance_lock": "released",
        },
        indent=2,
    )


# Statuses ``cancel_unit`` refuses to mutate. A merged (``done``) /
# endorsed (``approved_awaiting_merge``) / escalated unit has a
# meaningful terminal record the user does NOT want silently rewritten
# to ``cancelled`` — flipping ``done → cancelled`` erases the merge
# evidence the scheduler reads to satisfy downstream deps, flipping
# ``approved_awaiting_merge → cancelled`` removes the pending-merge
# indicator the user is waiting on, and flipping ``escalated``
# overwrites the ``last_error`` triage anchor. The ``CANCELLED_UNIT_STATUSES``
# member is the idempotent re-call path handled separately below.
_CANCEL_REFUSED_STATUSES: frozenset[str] = TERMINAL_UNIT_STATUSES | READY_TO_MERGE_STATUSES


def _archive_unit_sessions(unit_state: WorkUnitState) -> dict[str, str]:
    """Best-effort archive every worker session associated with the unit.

    Returns ``{role: outcome}`` where outcome is ``"archived"``,
    ``"no_session"``, or ``"error: <exception>"``. Cancellation must
    proceed even when a backend call raises (transient gh / Anthropic
    outage shouldn't strand the unit in ``coding``); the caller decides
    whether to surface partial failures to the user.
    """
    outcomes: dict[str, str] = {}
    for role in ("coder", "tester", "reviewer"):
        sid = _resolve_session_id(unit_state, role)
        if not sid:
            outcomes[role] = "no_session"
            continue
        try:
            worker = make_worker(role)
            worker.archive(sid)
            outcomes[role] = "archived"
        except Exception as e:  # noqa: BLE001 — proceed with cancel
            outcomes[role] = f"error: {e}"
    return outcomes


@mcp.tool()
def cancel_unit(unit_id: str) -> str:
    """Sticky-cancel a unit: archive every worker session, mark ``cancelled``.

    F-016 Phase 2.5 primitive. The unit's status is flipped to
    ``cancelled`` and ``cancelled_at`` is stamped — both sticky: the
    Phase 3 daemon reads ``cancelled_at`` on every tick and stops
    driving the unit. Every role's worker session is best-effort
    archived (a backend error does not block the cancel).

    **Refuses terminal statuses** (PR #59 C1). A merged (``done``) /
    endorsed (``approved_awaiting_merge``) / escalated unit is not
    cancellable — ``cancel_unit`` is for halting *in-flight* work, and
    silently rewriting a terminal status would erase the merge record
    that downstream dep evaluation depends on, drop the
    pending-merge indicator the user is waiting on, or overwrite the
    ``last_error`` triage anchor on an escalation. Returns
    ``outcome: refused`` without touching the unit or archiving
    sessions; the caller surfaces this to the user.

    Idempotent: re-calling on an already-cancelled unit returns
    ``outcome: already_cancelled`` without re-archiving sessions.

    **Serialized via** :func:`state.lead_advance_lock` (PR #59 M3).
    The lock window covers the archive + status flip + audit row so
    two concurrent ``cancel_unit`` calls double-archive only their
    in-process serialization paths, and a Phase 3 daemon tick during
    the window sees ``owner='lead'`` and defers. The audit event row
    is dedupe-keyed per unit so an MCP retry that bypasses the lock
    (different process) still writes one ``unit_cancelled`` row.

    Downstream dep-evaluation treats a ``cancelled`` unit as not-done —
    units depending on it stay blocked until the lead reshapes the
    graph via :func:`update_unit_deps`. Spec § "cancel_unit archives
    the worker session and marks the unit cancelled".

    Repo must be fresh-verified (call ``verify_repo(<url>)`` if blocked).
    """
    if err := ensure_verified_for_unit(unit_id):
        return err

    # First-pass read for the early-refusal short-circuits. We re-read
    # inside the lock below to close the TOCTOU on the status check —
    # but the read outside is cheap and lets us skip locking entirely
    # when the unit is already terminal / cancelled.
    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for unit {unit_id}"

    if unit_state.status in _CANCEL_REFUSED_STATUSES:
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "refused",
                "status": unit_state.status,
                "reason": (
                    f"unit is {unit_state.status!r}; cancel_unit refuses terminal "
                    "statuses (merge record / endorsement / escalation must not "
                    "be silently rewritten)"
                ),
            },
            indent=2,
        )

    if unit_state.status in CANCELLED_UNIT_STATUSES:
        return json.dumps(
            {
                "unit_id": unit_id,
                "outcome": "already_cancelled",
                "cancelled_at": unit_state.cancelled_at,
            },
            indent=2,
        )

    # Serialize against concurrent cancels + send_to_unit_async + the
    # future Phase 3 daemon tick. Re-read the row inside the lock so
    # the terminal-refuse + already-cancelled checks see whatever
    # landed while we were waiting on the lock.
    with state.lead_advance_lock(unit_id):
        unit_state = state.get_unit_state(unit_id)
        if not unit_state:
            return f"ERROR: no state for unit {unit_id} (row vanished under lock)"
        if unit_state.status in _CANCEL_REFUSED_STATUSES:
            return json.dumps(
                {
                    "unit_id": unit_id,
                    "outcome": "refused",
                    "status": unit_state.status,
                    "reason": (
                        f"unit is {unit_state.status!r}; cancel_unit refuses terminal statuses"
                    ),
                },
                indent=2,
            )
        if unit_state.status in CANCELLED_UNIT_STATUSES:
            return json.dumps(
                {
                    "unit_id": unit_id,
                    "outcome": "already_cancelled",
                    "cancelled_at": unit_state.cancelled_at,
                },
                indent=2,
            )

        archive_outcomes = _archive_unit_sessions(unit_state)

        if not state.cancel_unit(unit_id):
            return f"ERROR: cancel_unit failed for {unit_id} (row missing)"

        state.record_event(
            unit_id,
            unit_state.feature_id,
            "unit_cancelled",
            source="human",
            cycle_number=unit_state.review_round,
            summary="Unit cancelled via cancel_unit",
            details=json.dumps({"archive_outcomes": archive_outcomes}),
            # Dedupe per unit — a same-process retry that re-enters
            # under the same lock would already short-circuit on
            # ``status == cancelled``; this key catches the
            # cross-process race (two MCP servers, MCP retry storm)
            # where the lock can't serialize.
            dedupe_key=f"{unit_id}|unit_cancelled",
        )

    refreshed = state.get_unit_state(unit_id)
    return json.dumps(
        {
            "unit_id": unit_id,
            "outcome": "cancelled",
            "status": refreshed.status if refreshed else "cancelled",
            "cancelled_at": refreshed.cancelled_at if refreshed else None,
            "archive_outcomes": archive_outcomes,
        },
        indent=2,
    )


# Re-export for cycle_review's _emit_terminal (avoids circular import via observability)
def get_unit_status(unit_id: str) -> str:
    """Persisted state of one unit as JSON. Mirrors observability.get_unit_status."""
    from dataclasses import asdict

    s = state.get_unit_state(unit_id)
    if not s:
        return f"No state for unit {unit_id} (not yet spawned)"
    return json.dumps(asdict(s), indent=2)
