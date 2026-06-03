"""Pure marker parsing — the stateless half of the F-016 watcher loop.

Worker agents (coder / tester / reviewer) end their responses with a
terminal-marker sentinel:

  * coder:    ``PR_URL: <url>`` | ``FIX_PUSHED`` | ``BLOCKED: …``
  * tester:   ``TESTS_PASS`` | ``BUG_FOUND: <reason>`` | ``BLOCKED: …``
  * reviewer: ``REVIEW_RECOMMEND_MERGE: <reason>`` |
              ``REVIEW_REQUEST_CHANGES: <issue>`` | ``REVIEW_COMMENT`` |
              ``BLOCKED: …``

This module isolates the regex application from the side-effects of
recording the result. Two callers consume the parsed
:class:`MarkerSpec`:

  1. :func:`orchestrator.tools.execution._record_terminal_marker` — the
     blocking lead path. Writes the ``unit_events`` row + status flip.
  2. The watcher daemon (F-016-U-5) — re-scans an idle session's tail.
     Routes the same :class:`MarkerSpec` through the same recorder, but
     the recorder's ``INSERT OR IGNORE`` (keyed by
     :func:`dedupe_key`) makes a duplicate scan a no-op.

The parser is intentionally pure: no DB, no logging, no I/O. The same
``(role, text)`` always produces the same :class:`MarkerSpec` — that
determinism is what makes the dedupe key stable across the lead and
the daemon scanning the same response.

BLOCKED markers are universal across roles and carry a structured
payload (see :mod:`orchestrator.blocked_reasons`); they sit alongside
the role-scoped marker table in :func:`scan_response`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.blocked_reasons import BlockedPayload, parse_blocked_marker

# --------------------------- regexes ---------------------------
#
# Kept here so the pure parser owns its grammar. ``orchestrator.tools``
# re-exports the same compiled objects for legacy import sites (the
# names predate this module).

PR_URL_RE = re.compile(r"PR_URL:\s*(https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+))", re.IGNORECASE)
TESTS_PASS_RE = re.compile(r"^TESTS_PASS\s*$", re.MULTILINE)
BUG_FOUND_RE = re.compile(r"^BUG_FOUND:\s*(.+)$", re.MULTILINE)
REVIEW_CHANGES_RE = re.compile(r"^REVIEW_REQUEST_CHANGES:\s*(.+)$", re.MULTILINE)
REVIEW_COMMENT_RE = re.compile(r"^REVIEW_COMMENT\s*$", re.MULTILINE)
REVIEW_RECOMMEND_MERGE_RE = re.compile(r"^REVIEW_RECOMMEND_MERGE:\s*(.+)$", re.MULTILINE)
FIX_PUSHED_RE = re.compile(r"\bFIX_PUSHED\b")


# --------------------------- result type ---------------------------


@dataclass(frozen=True)
class MarkerSpec:
    """One parsed terminal-marker result — everything a recorder needs.

    Returned by :func:`scan_response`. Carries the role-scoped marker
    identity, the ``unit_events`` shape it would record, and the per-
    marker extras the blocking caller surfaces in its MCP return JSON
    (e.g. ``pr_url`` / ``pr_number`` for PR_URL, ``payload`` for
    BLOCKED).

    Fields:
        role: ``coder`` / ``tester`` / ``reviewer`` — who emitted it.
        marker: Short marker name (``PR_URL`` / ``FIX_PUSHED`` /
            ``TESTS_PASS`` / ``BUG_FOUND`` / ``REVIEW_RECOMMEND_MERGE`` /
            ``REVIEW_REQUEST_CHANGES`` / ``REVIEW_COMMENT`` /
            ``BLOCKED``).
        event_type: The ``unit_events.event_type`` slug to record
            (``pr_opened`` / ``fix_pushed`` / ``tests_pass`` /
            ``tester_bug_found`` / ``reviewer_recommend_merge`` /
            ``reviewer_request_changes`` / ``reviewer_comment`` /
            ``<role>_blocked``). The BLOCKED slug uses the role-default
            here; callers (notably ``address_review``) may override to
            ``coder_blocked_on_fix`` before recording.
        target_status: New ``work_units.status`` value when the unit is
            currently active. ``None`` for BUG_FOUND and
            REVIEW_REQUEST_CHANGES (those leave status to the caller's
            fix-loop). ``escalated`` for BLOCKED.
        summary: Suggested ``unit_events.summary`` value.
        details: Suggested ``unit_events.details`` value (already
            truncated where applicable).
        payload: The stable hash source for :func:`dedupe_key`. For most
            markers this is the regex's captured reason text; for
            markerless variants (``TESTS_PASS`` / ``REVIEW_COMMENT``)
            it's the marker name itself; for BLOCKED it's the structured
            reason slug + prose.
        extras: Per-marker keys merged into the MCP-return dict by
            :func:`orchestrator.tools.execution._record_terminal_marker`
            (e.g. ``{"pr_url": ..., "pr_number": ...}``). For BLOCKED
            this carries ``{"payload": BlockedPayload}`` so the caller
            can stringify the structured fields.
        last_error: Pre-formatted ``WorkUnitState.last_error`` value to
            set alongside the status flip. Non-empty only for BLOCKED.
        blocked_payload: The structured :class:`BlockedPayload` when
            ``marker == "BLOCKED"``, else ``None``. Hoisted out of
            ``extras`` so type-aware callers don't need to cast.
    """

    role: str
    marker: str
    event_type: str
    target_status: str | None
    summary: str
    details: str
    payload: str
    extras: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    blocked_payload: BlockedPayload | None = None


# --------------------------- dedupe key ---------------------------


def dedupe_key(
    *,
    session_id: str,
    cycle_number: int | None,
    event_type: str,
    marker_payload: str,
) -> str:
    """Stable hash for the F-016 Phase 0 idempotent recorder.

    Identity = ``(session_id, cycle_number, event_type, marker_payload)``.

    ``cycle_number`` is load-bearing — the same role can legitimately
    emit the same marker payload in different cycles (coder pushes
    ``FIX_PUSHED`` in cycle 1, then again in cycle 3 after a
    regression). Without it the second emit would dedupe against the
    first. ``None`` is encoded explicitly so a missing cycle still
    contributes to the hash domain.

    ``event_type`` rather than ``marker`` so the
    ``coder_blocked`` / ``coder_blocked_on_fix`` split documented in
    :class:`MarkerSpec` stays distinguishable — a fix-loop BLOCKED must
    not dedupe against an earlier spawn-time BLOCKED on the same
    session.

    The hash is the full sha256 hexdigest. Storing the truncated form
    would risk collisions on a long-running DB; the column is TEXT so
    the 64-char string costs nothing observable.
    """
    raw = f"{session_id}|{cycle_number}|{event_type}|{marker_payload}"
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------- internal parse rules ---------------------------


@dataclass(frozen=True)
class _Rule:
    """One (role, marker) row of the dispatch table.

    Internal to this module; the public surface is
    :func:`scan_response`. ``build`` converts the regex match plus the
    full response into the variable parts of :class:`MarkerSpec`
    (``summary`` / ``details`` / ``extras`` / ``payload``).
    """

    role: str
    marker: str
    pattern: re.Pattern[str]
    event_type: str
    target_status: str | None
    build: Callable[[re.Match[str], str], tuple[str, str, dict[str, Any], str]]


def _tail(text: str, n: int = 800) -> str:
    """Last ``n`` chars of ``text``, with an ellipsis prefix if truncated.

    Mirrors :func:`orchestrator.tools.tail` — duplicated here so the
    pure parser keeps zero dependencies on ``orchestrator.tools``.
    """
    return text if len(text) <= n else "...\n" + text[-n:]


def _build_pr_url(m: re.Match[str], _response: str) -> tuple[str, str, dict[str, Any], str]:
    pr_url, pr_number = m.group(1), int(m.group(2))
    return (
        f"PR #{pr_number} opened",
        pr_url,
        {"pr_url": pr_url, "pr_number": pr_number},
        pr_url,
    )


def _build_fix_pushed(_m: re.Match[str], response: str) -> tuple[str, str, dict[str, Any], str]:
    return ("Fix committed and pushed", _tail(response), {}, "FIX_PUSHED")


def _build_tests_pass(_m: re.Match[str], _response: str) -> tuple[str, str, dict[str, Any], str]:
    return ("All tests pass", "", {}, "TESTS_PASS")


def _build_bug_found(m: re.Match[str], response: str) -> tuple[str, str, dict[str, Any], str]:
    reason = m.group(1).strip()
    return (reason, _tail(response), {"bug": reason}, reason)


def _build_recommend_merge(
    m: re.Match[str], _response: str
) -> tuple[str, str, dict[str, Any], str]:
    reason = m.group(1).strip()
    return (
        f"Endorsed (self-approval blocked): {reason}",
        "",
        {"reason": reason},
        reason,
    )


def _build_request_changes(m: re.Match[str], response: str) -> tuple[str, str, dict[str, Any], str]:
    reason = m.group(1).strip()
    return (reason, _tail(response), {"issue": reason}, reason)


def _build_review_comment(
    _m: re.Match[str], _response: str
) -> tuple[str, str, dict[str, Any], str]:
    return ("Comment-only review", "", {}, "REVIEW_COMMENT")


# Ordered per role: ``PR_URL`` precedes ``FIX_PUSHED`` so a fresh
# spawn_unit response carrying both (rare but possible — agent quoted
# its own marker) matches the canonical first marker.
_RULES: tuple[_Rule, ...] = (
    _Rule("coder", "PR_URL", PR_URL_RE, "pr_opened", "in_ci", _build_pr_url),
    _Rule("coder", "FIX_PUSHED", FIX_PUSHED_RE, "fix_pushed", "in_ci", _build_fix_pushed),
    _Rule("tester", "TESTS_PASS", TESTS_PASS_RE, "tests_pass", "in_ci", _build_tests_pass),
    _Rule("tester", "BUG_FOUND", BUG_FOUND_RE, "tester_bug_found", None, _build_bug_found),
    _Rule(
        "reviewer",
        "REVIEW_RECOMMEND_MERGE",
        REVIEW_RECOMMEND_MERGE_RE,
        "reviewer_recommend_merge",
        # Reviewer endorsement is terminal for the cycle — land the
        # unit in the awaiting-merge bucket so send_to_unit(reviewer)
        # endorsements match cycle_review's terminal state. F-009-U-4.
        "approved_awaiting_merge",
        _build_recommend_merge,
    ),
    _Rule(
        "reviewer",
        "REVIEW_REQUEST_CHANGES",
        REVIEW_CHANGES_RE,
        "reviewer_request_changes",
        None,
        _build_request_changes,
    ),
    _Rule(
        "reviewer",
        "REVIEW_COMMENT",
        REVIEW_COMMENT_RE,
        "reviewer_comment",
        "in_ci",
        _build_review_comment,
    ),
)


def _format_blocked_last_error(payload: BlockedPayload) -> str:
    """One-line human-readable form for ``WorkUnitState.last_error``.

    Mirrors :func:`orchestrator.tools.format_blocked_last_error` —
    duplicated here so the pure parser keeps zero dependencies on
    ``orchestrator.tools``.
    """
    return f"BLOCKED [{payload.reason}]: {payload.prose}"


# --------------------------- public scan entry point ---------------------------


def scan_response(
    role: str,
    text: str,
    *,
    allowed: frozenset[str] | None = None,
) -> MarkerSpec | None:
    """Return the parsed :class:`MarkerSpec` for a worker response, or ``None``.

    Pure function — no DB, no logging, no I/O. The same ``(role, text)``
    always returns an equal :class:`MarkerSpec` so the dedupe key derived
    from its ``event_type`` + ``payload`` is stable across the lead and
    the watcher daemon scanning the same response.

    Role-scoped markers: a ``tester`` response carrying
    ``REVIEW_RECOMMEND_MERGE`` returns ``None`` (not a tester marker).
    ``BLOCKED`` is universal across roles.

    Args:
        role: ``coder`` / ``tester`` / ``reviewer``. Unknown roles
            silently match nothing (return ``None``).
        text: The agent's full response text. The marker can sit
            anywhere; per-marker regexes anchor to line boundaries
            where appropriate.
        allowed: Optional whitelist of marker names. ``None`` means
            "every role-appropriate marker". Callers narrow this when
            certain markers are invalid in their context (e.g. a coder
            resume excludes ``PR_URL`` because the unit already has a
            PR from ``spawn_unit``).
    """
    permit = (lambda _name: True) if allowed is None else (lambda name: name in allowed)

    for rule in _RULES:
        if rule.role != role or not permit(rule.marker):
            continue
        match = rule.pattern.search(text)
        if not match:
            continue
        summary, details, extras, payload = rule.build(match, text)
        return MarkerSpec(
            role=role,
            marker=rule.marker,
            event_type=rule.event_type,
            target_status=rule.target_status,
            summary=summary,
            details=details,
            payload=payload,
            extras=extras,
        )

    if not permit("BLOCKED"):
        return None
    blocked = parse_blocked_marker(text)
    if blocked is None:
        return None
    return MarkerSpec(
        role=role,
        marker="BLOCKED",
        event_type=f"{role}_blocked",
        target_status="escalated",
        summary=blocked.prose,
        # details are domain-specific (the caller composes the JSON
        # blob from the structured payload + the truncated response
        # tail); the pure scan leaves it empty so the recorder owns it.
        details="",
        payload=f"{blocked.reason}|{blocked.prose}",
        extras={"payload": blocked},
        last_error=_format_blocked_last_error(blocked),
        blocked_payload=blocked,
    )
