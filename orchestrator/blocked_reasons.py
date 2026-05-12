"""Structured BLOCKED outcome taxonomy + parser.

Worker agents (coder/tester/reviewer) signal a non-recoverable failure with a
final-line marker:

    BLOCKED: <one-line reason>

This module formalises that marker. A modern worker is asked (via prompt
guidance) to emit the structured form:

    BLOCKED: reason=<slug> [k=v]... | <free text>

…where `<slug>` is one of :class:`BlockedReason` and the optional
``key=value`` tokens carry domain-specific context (e.g. ``branch``,
``rule_type``, ``api_used`` for a branch-protection denial).

For backwards compatibility with workers that pre-date this convention — or
that simply forget the ``reason=`` field — a small set of pattern recognisers
runs against the prose to retroactively classify common failures. The first
three recognisers cover the three known GitHub branch-protection error
strings, which is the immediate motivation for this module (see feature
F-005).

If neither the structured tag nor any recogniser fires, the reason falls back
to :attr:`BlockedReason.UNKNOWN` and the prose is preserved verbatim so the
escalation summary still carries the worker's own explanation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class BlockedReason(StrEnum):
    """Canonical taxonomy of BLOCKED outcome reasons.

    Slug values are deliberately ``snake_case`` strings so they are easy to
    type into a prompt-emitted ``reason=<slug>`` field, easy to scan in event
    logs, and safe to surface in URLs / push titles without escaping.
    """

    BRANCH_PROTECTION_BLOCKED_PUSH = "branch_protection_blocked_push"
    AUTH_FAILURE = "auth_failure"
    NETWORK_ERROR = "network_error"
    DEPENDENCY_INSTALL_FAILED = "dependency_install_failed"
    DISK_FULL = "disk_full"
    RATE_LIMITED = "rate_limited"
    CI_TOOL_MISSING = "ci_tool_missing"
    MERGE_CONFLICT_UNRESOLVED = "merge_conflict_unresolved"
    UNKNOWN = "unknown"


VALID_REASONS: frozenset[str] = frozenset(r.value for r in BlockedReason)
"""All slugs in the taxonomy. Used to validate prompt-emitted ``reason=``."""


# --------------------------- structured payload ---------------------------


@dataclass(frozen=True)
class BlockedPayload:
    """Parsed representation of a BLOCKED marker.

    Attributes:
        reason: One of :data:`VALID_REASONS`. Falls back to ``"unknown"`` if
            neither the prompt-emitted tag nor any recogniser matched.
        prose: The free-text portion of the marker (after the ``|`` separator,
            or the full marker body when no separator was supplied). Never
            empty — the worker's own message is preserved here even when the
            classification is ``unknown``.
        fields: Optional ``key=value`` pairs the worker emitted alongside
            ``reason=`` (e.g. ``{"branch": "feat/foo",
            "rule_type": "required_pull_request_reviews",
            "api_used": "git_push"}``). Empty when none were present.
        recognized_by: Name of the recogniser that classified a free-form
            marker. ``None`` when the worker emitted ``reason=<slug>``
            directly, or when no recogniser matched (in which case
            :attr:`reason` is ``"unknown"``).
    """

    reason: str
    prose: str
    fields: dict[str, str] = field(default_factory=dict)
    recognized_by: str | None = None

    def to_event_payload(self) -> dict[str, object]:
        """Return a dict suitable for embedding in a unit_event details column.

        The orchestrator stores this JSON-encoded so the dashboard /
        escalation push can surface ``reason`` + ``fields`` without re-parsing
        the prose.
        """
        out: dict[str, object] = {"reason": self.reason, "prose": self.prose}
        if self.fields:
            out["fields"] = dict(self.fields)
        if self.recognized_by:
            out["recognized_by"] = self.recognized_by
        return out


# --------------------------- recognisers (fallback) ---------------------------
#
# Each entry is (name, compiled regex, slug). Order matters: the first match
# wins. New recognisers can be appended (or `register_recognizer` called) as
# we learn about new failure modes.
#
# The three branch-protection strings below are the original motivation —
# two real escalations (F-001-U-1, F-004-U-1 on 2026-05-12) bottomed out in
# pushes rejected by branch protection, and the existing BLOCKED handler
# only carried the prose tail through.

_Recognizer = tuple[str, "re.Pattern[str]", str]

_RECOGNIZERS: list[_Recognizer] = [
    (
        "branch_protection_pr_required",
        re.compile(r"Changes must be made through a pull request", re.IGNORECASE),
        BlockedReason.BRANCH_PROTECTION_BLOCKED_PUSH.value,
    ),
    (
        "branch_protection_required_reviews",
        re.compile(r"required_pull_request_reviews", re.IGNORECASE),
        BlockedReason.BRANCH_PROTECTION_BLOCKED_PUSH.value,
    ),
    (
        "branch_protection_enforce_admins",
        re.compile(r"enforce_admins", re.IGNORECASE),
        BlockedReason.BRANCH_PROTECTION_BLOCKED_PUSH.value,
    ),
]


def register_recognizer(name: str, pattern: str | re.Pattern[str], slug: str) -> None:
    """Append a new pattern recogniser.

    Tests / future callers can use this to register more recognisers without
    touching the module-level list directly. ``slug`` must be a member of
    :data:`VALID_REASONS`.
    """
    if slug not in VALID_REASONS:
        raise ValueError(f"unknown reason slug: {slug!r} (must be one of {sorted(VALID_REASONS)})")
    compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, re.IGNORECASE)
    _RECOGNIZERS.append((name, compiled, slug))


def builtin_recognizer_names() -> list[str]:
    """Names of the built-in recognisers. Useful for introspection / tests."""
    return [name for name, _, _ in _RECOGNIZERS]


def classify_prose(text: str) -> tuple[str, str | None]:
    """Run the recogniser list against ``text``.

    Returns ``(slug, recognizer_name)``. If nothing matches, returns
    ``("unknown", None)``.
    """
    for name, pattern, slug in _RECOGNIZERS:
        if pattern.search(text):
            return slug, name
    return BlockedReason.UNKNOWN.value, None


# --------------------------- parser ---------------------------


_BLOCKED_LINE_RE = re.compile(r"^BLOCKED:\s*(.+)$", re.MULTILINE)
"""Captures the body of the last-line BLOCKED marker.

Kept in this module so the structured parser and the simple line-scan
both agree on what counts as a BLOCKED marker. ``orchestrator.tools``
re-exports the same compiled regex as ``BLOCKED_RE`` for legacy callers.
"""

# Allowed key=value field token: bare key, '=', then a non-whitespace value.
# Pipe terminates the structured head, so it must not appear inside a value.
_FIELD_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s|]+)")


def parse_blocked_body(body: str) -> BlockedPayload:
    """Parse the text after ``BLOCKED:`` into a :class:`BlockedPayload`.

    Accepts both the modern structured form
    ``reason=<slug> [k=v]... | <free text>`` and the legacy bare-prose form
    ``<one-line reason>``. The presence of a ``|`` separator is the signal
    that the worker intended structured emission, but a ``reason=`` token
    anywhere in the head is also accepted as a fallback.

    Classification priority:
      1. ``reason=<slug>`` in the structured head, when ``<slug>`` is a known
         taxonomy member.
      2. Recogniser match against the prose (or the entire body when no
         separator was used).
      3. :attr:`BlockedReason.UNKNOWN` — prose is still preserved.
    """
    body = body.strip()
    if "|" in body:
        head, _, prose = body.partition("|")
        head = head.strip()
        prose = prose.strip() or body
    else:
        head = body
        prose = body

    fields: dict[str, str] = {}
    for m in _FIELD_TOKEN_RE.finditer(head):
        fields[m.group(1)] = m.group(2)

    reason_tag = fields.pop("reason", "")
    recognized_by: str | None = None

    if reason_tag and reason_tag in VALID_REASONS:
        reason = reason_tag
    else:
        # Either no reason= tag, or it carried a slug we don't recognise.
        # Run recognisers against the prose; fall back to "unknown".
        # If the worker supplied an off-list slug, surface that fact via the
        # fields dict so a human can see what they emitted.
        if reason_tag:
            fields.setdefault("unrecognized_reason_tag", reason_tag)
        reason, recognized_by = classify_prose(prose)

    return BlockedPayload(
        reason=reason,
        prose=prose,
        fields=fields,
        recognized_by=recognized_by,
    )


def parse_blocked_marker(response: str) -> BlockedPayload | None:
    """Scan ``response`` for a ``BLOCKED:`` marker; return a parsed payload.

    Returns ``None`` when no marker is present, so callers can distinguish
    "not blocked" from "blocked with empty body". When multiple markers are
    present, the **last** one wins — agents sometimes mention BLOCKED in
    earlier prose ("if X happens you'd see BLOCKED: …") and only the
    terminal line is canonical.
    """
    matches = list(_BLOCKED_LINE_RE.finditer(response))
    if not matches:
        return None
    return parse_blocked_body(matches[-1].group(1))
