"""Reason -> remediation hint table + escalation-summary formatters.

A separate sibling unit (F-005-U-1) introduces a structured ``reason`` slug
on BLOCKED outcomes so the same prose tail can be classified into a
machine-readable taxonomy (``branch_protection_blocked_push``,
``auth_failure``, …). This module is the *surfacing* layer for that
taxonomy: it answers the question "given a reason slug, what should the
user do?" and renders that answer into the four channels that already
exist (escalation summaries, ntfy push body, dashboard "Last error"
column, and chat-level filtering on ``unit_history`` / ``list_in_flight``).

Design contract:

* Events MAY carry a structured reason inside their ``details`` column as
  a JSON object ``{"reason": "<slug>", "prose": "<free text>", ...}``. The
  producer of that JSON lives in the BLOCKED handler (a sibling unit).
* Events MAY also carry the legacy bare-prose ``details`` string. Nothing
  here regresses that case — ``extract_reason_from_details`` returns
  ``("unknown", details)`` for any input it can't parse.
* When the reason is anything *other* than ``"unknown"`` the surfacing
  layer SHOULD render "reason -> remediation hint -> prose" so the lead
  (and the user reading a phone push) sees the actionable fix-it options
  next to the raw failure tail. When it's ``"unknown"``, behaviour
  collapses back to today's prose-only rendering.

Nothing in this module touches GitHub or the network. The functions are
pure transformations on strings + JSON-encoded dicts.
"""

from __future__ import annotations

import json
from typing import Any

UNKNOWN_REASON = "unknown"

# --------------------------- reason -> hint table ---------------------------

# Multi-line remediation hints keyed by canonical reason slug. Strings are
# wrapped to ~78 chars so they read cleanly in both terminal and phone-push
# contexts. The branch-protection entry is the original motivation: the
# three fix-it options on F-005 surface explicitly so the lead can pick.
REMEDIATION_HINTS: dict[str, str] = {
    "branch_protection_blocked_push": (
        "Branch protection rejected the push. Fix options:\n"
        "  1. Scope the protection rule to `main` only "
        "(leave feature branches unprotected).\n"
        "  2. Add the orchestrator identity as a bypass actor on the rule.\n"
        "  3. Re-issue a bypass-capable PAT and update GITHUB_TOKEN."
    ),
    "auth_failure": (
        "Authentication failed against GitHub. Fix options:\n"
        "  1. Confirm GITHUB_TOKEN (or the GitHub App installation token) "
        "is non-empty and unexpired.\n"
        "  2. Re-run `verify_repo(<url>)` to refresh the auth cache.\n"
        "  3. Check that the App / PAT identity still has write access on "
        "the target repo."
    ),
    "network_error": (
        "Transient network failure. Fix options:\n"
        "  1. Retry the spawn — the upstream may have recovered.\n"
        "  2. If repeated: check the agent sandbox's outbound allowlist "
        "in `orchestrator/agents.py`."
    ),
    "dependency_install_failed": (
        "A dependency install step failed in the agent sandbox. Fix options:\n"
        "  1. Inspect the prose tail for the failing package + error.\n"
        "  2. Pin the dep in the repo's lock file and retry.\n"
        "  3. Confirm the sandbox can reach the package index "
        "(PyPI, npm, etc.)."
    ),
    "disk_full": (
        "Agent sandbox ran out of disk. Fix options:\n"
        "  1. Reset the agent + environment cache via "
        "`reset_cached_resources()`.\n"
        "  2. Confirm no large artifacts are being persisted "
        "outside the sandbox's scratch dir."
    ),
    "rate_limited": (
        "GitHub (or Anthropic) returned a rate-limit error. Fix options:\n"
        "  1. Wait until the limit window resets.\n"
        "  2. If using a PAT: switch to a GitHub App identity to lift "
        "REST limits to 5000 req/hr."
    ),
    "ci_tool_missing": (
        "A required CI tool wasn't present in the sandbox. Fix options:\n"
        "  1. Add the tool to the agent base image / network allowlist.\n"
        "  2. Or pin the workflow to a runner that ships with it."
    ),
    "merge_conflict_unresolved": (
        "Rebase produced conflicts the coder couldn't auto-resolve. Fix options:\n"
        "  1. Resume the coder with explicit conflict-resolution guidance.\n"
        "  2. Or take over locally, push the resolved branch, and re-run "
        "the cycle."
    ),
}


def remediation_hint(reason: str) -> str:
    """Return the multi-line hint for ``reason``.

    Returns the empty string when ``reason`` is ``"unknown"``, falsy, or not
    in the taxonomy — callers can ``if hint: ...`` to decide whether to
    surface the structured rendering.
    """
    if not reason or reason == UNKNOWN_REASON:
        return ""
    return REMEDIATION_HINTS.get(reason, "")


# --------------------------- extraction from event details ---------------------------


def extract_reason_from_details(details: str) -> tuple[str, str]:
    """Parse an event's ``details`` column into ``(reason_slug, prose)``.

    Accepts:

    * A JSON-encoded dict carrying at least ``reason`` (and typically
      ``prose``). Anything outside the dict is preserved as-is via the
      ``prose`` field; if ``prose`` is missing, falls back to the original
      ``details`` string.
    * Anything else — returns ``("unknown", details)``.

    The function never raises: malformed JSON, missing keys, and a None
    ``details`` (legacy events) all collapse cleanly to the unknown case.
    """
    if not details:
        return UNKNOWN_REASON, details or ""
    text = details.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return UNKNOWN_REASON, details
    try:
        parsed: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return UNKNOWN_REASON, details
    if not isinstance(parsed, dict):
        return UNKNOWN_REASON, details
    reason = parsed.get("reason")
    if not isinstance(reason, str) or not reason:
        return UNKNOWN_REASON, details
    prose = parsed.get("prose")
    if not isinstance(prose, str):
        prose = details
    return reason, prose


# --------------------------- escalation summary formatter ---------------------------


def format_escalation_summary(reason: str, prose: str) -> str:
    """Render the lead-facing escalation summary.

    When ``reason`` resolves to a known taxonomy entry the output is:

        Reason: <slug>
        <multi-line hint>

        <prose>

    Otherwise the output is just ``prose`` verbatim — preserving today's
    prose-only behaviour so unclassified failures do not regress.
    """
    hint = remediation_hint(reason)
    if not hint:
        return prose
    blocks = [f"Reason: {reason}", hint]
    if prose:
        blocks.append(prose)
    return "\n\n".join(blocks)


def format_ntfy_body(unit_id: str, reason: str, prose: str) -> str:
    """Render the body of an escalation push.

    Phone pushes have very little vertical real estate, so the body leads
    with the reason slug and an inline list of fix-it options (when
    known). Falls back to a single-line ``Unit <id> escalated: <prose>``
    when the reason is ``"unknown"``.
    """
    hint = remediation_hint(reason)
    if not hint:
        return f"Unit {unit_id} escalated: {prose}".rstrip()
    lines = [f"Unit {unit_id} escalated", f"Reason: {reason}", "", hint]
    if prose:
        lines.extend(["", prose])
    return "\n".join(lines)


# --------------------------- DB-aware helpers ---------------------------


def latest_blocked_reason(unit_id: str) -> tuple[str, str]:
    """Walk a unit's event log newest-first; return the first reason found.

    Returns ``("unknown", "")`` when the unit has no events at all or none
    of them carry a structured reason. Imports ``state`` lazily so this
    module stays cheap to import in contexts that never touch SQLite.
    """
    from orchestrator import state  # local import → no module-load side effects

    events = state.list_events(unit_id)
    for event in reversed(events):
        details = event.get("details") or ""
        reason, prose = extract_reason_from_details(details)
        if reason != UNKNOWN_REASON:
            return reason, prose
    return UNKNOWN_REASON, ""
