"""Operational MCP tools: diagnostics, CI/merge polling, restart recovery, cache reset."""

from __future__ import annotations

import contextlib
import json
import sqlite3

from orchestrator import blocked_hints, cycle_log, github, github_app, repo_verify, state
from orchestrator.agents import ManagedAgentWorker
from orchestrator.models import ACTIVE_UNIT_STATUSES
from orchestrator.tools import mcp, need_github_token

# Active statuses that should not legally observe a merged PR — coding/
# testing/reviewing/fixing/opening_pr units have an agent mid-flight, so
# a merged PR would mean the agent is racing with a human merge. Treat as
# unreachable in practice; reconcile_unit_pr emits 'reconcile_refused' to
# document the policy rather than silently flipping to done.
_RECONCILE_REFUSED_STATUSES: frozenset[str] = ACTIVE_UNIT_STATUSES - {"in_ci"}


@mcp.tool()
def hello_world_test() -> str:
    """Smoke test. Spawns a fresh Managed Agent, asks it to say hello.

    Use to confirm the Claude Code + MCP + Managed Agents chain is healthy.
    Costs ~one second of session-hour billing; safe to call any time.
    """
    worker = ManagedAgentWorker(role="coder")
    session_id, response = worker.spawn(
        "Reply with exactly the string: hello from a managed agent",
        title="smoke-test",
    )
    worker.archive(session_id)
    return f"session_id={session_id}\nresponse={response!r}"


@mcp.tool()
def check_unit_pr(unit_id: str) -> str:
    """Poll GitHub for the PR's state + check_runs. **Read-only.**

    Returns the observed PR state and CI checks alongside the orchestrator's
    current ``status`` for the unit. Does NOT mutate state — safe to call
    from dashboards, diagnostics, or monitors as often as you like.

    To advance a unit's state when its PR has been merged, call
    ``reconcile_unit_pr(unit_id)`` instead. That tool reads via this one
    and then applies the (PR-state, unit-status) transitions.
    """
    unit_state = state.get_unit_state(unit_id)
    if not unit_state or not unit_state.pr_number:
        return f"ERROR: unit {unit_id} has no PR"

    feature = state.get_feature(unit_state.feature_id)
    if not feature:
        return f"ERROR: feature for unit {unit_id} not found"

    if err := need_github_token():
        return err

    try:
        pr_state = github.get_pr_state(feature.repo_path, unit_state.pr_number)
        checks = github.get_pr_check_runs(feature.repo_path, unit_state.pr_number)
    except Exception as e:  # noqa: BLE001
        return f"ERROR querying GitHub: {e}"

    return json.dumps(
        {
            "unit_id": unit_id,
            "pr_number": unit_state.pr_number,
            "pr_state": pr_state,
            "checks": checks,
            "orchestrator_status": unit_state.status,
        },
        indent=2,
    )


@mcp.tool()
def reconcile_unit_pr(unit_id: str) -> str:
    """Reconcile orchestrator state with the PR's actual status on GitHub.

    Reads via ``check_unit_pr`` (which never mutates), then applies state
    transitions based on the (PR state, orchestrator status) pair:

      merged + in_ci      → status='done'; emit 'merged'.
      merged + escalated  → status='done'; emit 'merged' AND
                            'recovered_from_escalated' (details = prior
                            ``last_error``). ``last_error`` is cleared.
      merged + coding/testing/opening_pr/reviewing/fixing
                          → no-op + emit 'reconcile_refused' (unreachable
                            in practice; explicit refusal documents policy).
      open PR + any state → no-op, no events.
      closed-unmerged + any state → no-op (human decision; orchestrator
                            stays out).

    Idempotent: a second call after the unit has already flipped to ``done``
    re-reads the PR but emits no further events.

    Return shape matches ``check_unit_pr`` plus an ``action`` slug naming
    the branch taken and a ``reconciled`` flag that is ``True`` **only**
    when the unit's ``status`` row was actually transitioned to ``done``
    on this call. The ``no-op-*`` and ``refused-from-*`` branches all
    return ``reconciled=False`` (status unchanged); consult ``action``
    for the precise sub-case.
    """
    poll_result = check_unit_pr(unit_id)
    # Surface upstream errors verbatim (no PR, no feature, no token, GH error).
    if poll_result.startswith("ERROR"):
        return poll_result

    try:
        poll = json.loads(poll_result)
    except json.JSONDecodeError:
        return poll_result  # defensive — check_unit_pr returns JSON on success

    # Re-read state after the poll so any race with another caller is reflected.
    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: unit {unit_id} disappeared between poll and reconcile"

    pr_state = poll.get("pr_state", {})
    merged = bool(pr_state.get("merged"))
    status = unit_state.status
    cycle = unit_state.review_round
    reconciled = False  # only the two status-flipping branches below set True

    if not merged:
        # Open PR or closed-unmerged: orchestrator stays out.
        action = "no-op-pr-not-merged"
    elif status == "done":
        # Idempotency guard: a prior reconcile already flipped to done.
        action = "no-op-already-done"
    elif status == "in_ci":
        summary = f"PR #{unit_state.pr_number} merged at {pr_state.get('merged_at')}"
        state.touch_unit(unit_id, status="done")
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "merged",
            source="human",
            cycle_number=cycle,
            summary=summary,
        )
        action = "merged-from-in_ci"
        reconciled = True
    elif status == "escalated":
        summary = f"PR #{unit_state.pr_number} merged at {pr_state.get('merged_at')}"
        prior_error = unit_state.last_error
        state.touch_unit(unit_id, status="done", clear_error=True)
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "merged",
            source="human",
            cycle_number=cycle,
            summary=summary,
        )
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "recovered_from_escalated",
            source="human",
            cycle_number=cycle,
            summary="merged after escalation; last_error cleared",
            details=prior_error,
        )
        action = "merged-from-escalated"
        reconciled = True
    elif status in _RECONCILE_REFUSED_STATUSES:
        # Merged while an agent role is mid-flight is racy enough that the
        # right policy is "refuse and let the human investigate" — never
        # silently advance a unit whose coder/tester/reviewer is still live.
        # Unit status stays unchanged; reconciled=False matches the no-op
        # branches' "row not transitioned" semantic.
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "reconcile_refused",
            source="human",
            cycle_number=cycle,
            summary=f"refusing to advance unit in active status {status!r} to done",
        )
        action = f"refused-from-{status}"
    else:
        # 'pending', 'approved_awaiting_merge' (F-009-U-4 will add this
        # branch), or any future status we haven't taught reconcile about.
        state.record_event(
            unit_id,
            unit_state.feature_id,
            "reconcile_refused",
            source="human",
            cycle_number=cycle,
            summary=f"refusing to advance unit in status {status!r} to done",
        )
        action = f"refused-from-{status}"

    if action.startswith("merged-from-"):
        merge_sha = pr_state.get("merge_commit_sha")
        if merge_sha:
            with contextlib.suppress(Exception):
                cycle_log.write_cycle_log(
                    unit_id,
                    base_dir=cycle_log.cycle_log_base_dir(),
                    merge_commit_sha=merge_sha,
                    commit_message=f"cycle-log: backfill merge SHA for {unit_id}",
                )

    # Single return path through a fresh re-read so every branch reflects
    # the post-transition row — including the no-op-* / refused-* branches
    # where a concurrent caller may have advanced the unit between
    # check_unit_pr's read and ours.
    refreshed = state.get_unit_state(unit_id)
    return json.dumps(
        {
            **poll,
            "orchestrator_status": refreshed.status if refreshed else "unknown",
            "reconciled": reconciled,
            "action": action,
        },
        indent=2,
    )


@mcp.tool()
def list_in_flight(reason: str = "") -> str:
    """List units in active states across all features.

    Use after an MCP server restart (laptop sleep, crash, /quit + relaunch)
    to find units whose sessions may still be running on Anthropic's side
    but whose local state never got finalized. For each, you can call
    `resume_unit(unit_id, role)` to query the session's current status.

    Returns units with status in: coding, testing, opening_pr, in_ci,
    reviewing, fixing.

    ``reason``: when non-empty (e.g. ``"auth_failure"``,
    ``"branch_protection_blocked_push"``) the active-status set is
    widened to also include ``escalated`` and the rows are filtered to
    those whose most-recent BLOCKED-style event carries a matching
    structured reason (see :mod:`orchestrator.blocked_hints`). This lets
    the lead ask "show me everything blocked on auth" in one call. Each
    matching row gets an extra ``reason`` field so the lead can see
    which slug matched. Empty string (the default) preserves the
    original active-only behaviour.
    """
    statuses: tuple[str, ...] = tuple(ACTIVE_UNIT_STATUSES)
    if reason:
        statuses = statuses + ("escalated",)
    placeholders = ",".join("?" * len(statuses))
    with contextlib.closing(sqlite3.connect(state.STATE_DB)) as conn:
        conn.row_factory = sqlite3.Row
        # `placeholders` is "?,?,?,..." sized to the fixed status tuple
        # above; the values themselves bind via the `statuses` parameter.
        rows = conn.execute(
            f"SELECT * FROM work_units WHERE status IN ({placeholders}) ORDER BY last_activity DESC",  # noqa: S608  # nosec B608
            statuses,
        ).fetchall()

    result: list[dict] = []
    for r in rows:
        row_reason, _ = blocked_hints.latest_blocked_reason(r["unit_id"])
        if reason and row_reason != reason:
            continue
        entry: dict = {
            "unit_id": r["unit_id"],
            "feature_id": r["feature_id"],
            "status": r["status"],
            "branch": r["branch"],
            "pr_number": r["pr_number"],
            "has_coder_session": bool(r["coder_session_id"]),
            "has_tester_session": bool(r["tester_session_id"]),
            "has_reviewer_session": bool(r["reviewer_session_id"]),
            "review_round": r["review_round"],
            "last_activity": r["last_activity"],
            "last_error": r["last_error"],
        }
        if reason:
            entry["reason"] = row_reason
        result.append(entry)
    return json.dumps(result, indent=2)


@mcp.tool()
def resume_unit(unit_id: str, role: str = "coder") -> str:
    """Check status of a unit's saved session after an MCP server restart.

    Queries Anthropic for the session's current status (idle/running/
    rescheduling/terminated) and returns it along with local state.
    Does NOT auto-advance the unit's status — interpret the result and
    call the appropriate next-step tool manually.
    """
    if role not in ("coder", "tester", "reviewer"):
        return f"ERROR: role must be coder|tester|reviewer, got {role!r}"

    unit_state = state.get_unit_state(unit_id)
    if not unit_state:
        return f"ERROR: no state for {unit_id}"

    sid_map = {
        "coder": unit_state.coder_session_id,
        "tester": unit_state.tester_session_id,
        "reviewer": unit_state.reviewer_session_id,
    }
    sid = sid_map[role]
    if not sid:
        return f"ERROR: no {role} session_id stored for {unit_id}"

    worker = ManagedAgentWorker(role=role)
    try:
        session = worker.client.beta.sessions.retrieve(sid)
    except Exception as e:  # noqa: BLE001
        return f"ERROR retrieving session {sid}: {e}"

    return json.dumps(
        {
            "unit_id": unit_id,
            "role": role,
            "session_id": sid,
            "session_status": getattr(session, "status", "unknown"),
            "session_title": getattr(session, "title", None),
            "local_status": unit_state.status,
            "branch": unit_state.branch,
            "pr_number": unit_state.pr_number,
            "review_round": unit_state.review_round,
            "last_activity": unit_state.last_activity,
        },
        indent=2,
    )


# --------------------------- repo verification ---------------------------


@mcp.tool()
def verify_repo(repo_url: str) -> str:
    """Verify a target repo against the orchestrator's policy and cache the result.

    Runs ~5 GitHub API calls (read access, default branch, branch protection
    rule, App installation membership for App auth, CODEOWNERS sniff).
    Stores a row in `verified_repos` if every blocking check passes; that
    row is trusted by spawn-side gates for 24h before re-verifying.

    Use cases:
      - First-time onboarding of a target repo (call before `load_feature`)
      - Re-check after the user fixes a missing branch protection rule
      - Force a fresh check before resuming a long-paused feature

    Returns a human-readable pass/fail report. The lead should surface it
    verbatim to the user — it includes the exact fix-it instructions when
    a check fails.
    """
    if err := need_github_token():
        return err
    try:
        token = github_app.get_agent_token()
    except RuntimeError as e:
        return f"ERROR: {e}"
    auth_mode = github_app.auth_mode()
    try:
        result = repo_verify.verify(repo_url, token, auth_mode=auth_mode)
    except ValueError as e:
        return f"ERROR: {e}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR contacting GitHub: {e}"

    lines = repo_verify.format_result_lines(result)
    if result.passed:
        state.save_verified_repo(result)
        lines.append("")
        lines.append("Cached — spawns against this repo are now allowed for 24h.")
    else:
        lines.append("")
        lines.append("Verification FAILED — spawns against this repo will be blocked until fixed.")

    # Diagnostic prepend: if `.env` on disk has a different GITHUB_TOKEN
    # than the value loaded in-process, the MCP server is using a stale
    # token (user rotated it without restarting). Surface that BEFORE the
    # report so it frames downstream auth/permission failures. Silent
    # when .env is missing or matches — non-blocking on its own.
    stale_warning = repo_verify.detect_stale_env()
    if stale_warning:
        lines = [stale_warning, ""] + lines
    return "\n".join(lines)


@mcp.tool()
def list_verified_repos() -> str:
    """List every repo currently in the verification cache.

    Output includes the verified-at timestamp, default branch, and auth
    identity that did the verification. Use to audit what the orchestrator
    will allow spawns against right now.
    """
    repos = state.list_verified_repos()
    if not repos:
        return "No repos have been verified. Call `verify_repo(<url>)` to add one."
    return json.dumps(
        [
            {
                "repo_url": r.repo_url,
                "default_branch": r.default_branch,
                "auth_mode": r.auth_mode,
                "auth_identity": r.auth_identity,
                "verified_at": r.verified_at,
                "required_approvals": r.required_approvals,
                "has_codeowners": r.has_codeowners,
                "requires_signed_commits": r.requires_signed_commits,
            }
            for r in repos
        ],
        indent=2,
    )


@mcp.tool()
def forget_repo(repo_url: str) -> str:
    """Remove a repo from the verification cache. Forces re-verify on next use.

    Use after:
      - You changed branch protection rules and want the cache to refresh
      - You revoked the App's access and want to confirm the gate kicks in
      - General debugging
    """
    try:
        normalized = repo_verify.normalize_repo_url(repo_url)
    except ValueError as e:
        return f"ERROR: {e}"
    deleted = state.forget_verified_repo(normalized)
    if not deleted:
        return f"{normalized} was not in the cache (already forgotten or never verified)."
    return f"Forgot {normalized}. Next spawn against it will trigger re-verification."


@mcp.tool()
def reset_cached_resources() -> str:
    """Drop the cached agent + environment ids. Next spawn creates fresh ones.

    Routine changes (prompt edits, model swap) AUTOMATICALLY invalidate the
    cache via the resource signature. Use this tool only for edge cases:
      - Anthropic deprecates a model/feature that breaks existing agents
      - You suspect cached agent_ids are stale and want a clean slate
      - Debugging cache-related weirdness

    Old agents/environments on Anthropic's side become orphaned but cost
    nothing while idle. Returns the count of cache rows deleted.
    """
    n = state.clear_cached_resources()
    return f"Cleared {n} cached resource row(s). Next spawn will create fresh agent + environment."
