"""Markdown rendering for per-unit cycle logs.

Split out from ``orchestrator/cycle_log.py`` so the rendering can be
tested in isolation from gh / subprocess / filesystem (see PR #26
review on file length and test ergonomics). Pure function: given the
state-derived events + mirrored GitHub data, produces the markdown
string. ``orchestrator.cycle_log`` re-exports ``render_cycle_log`` for
back-compat with existing callers and tests.

Schema reference: ``docs/PROPOSAL-feature-spec-and-headless-daemon.md``
§ "Per-unit cycle log".
"""

from __future__ import annotations

import re
from typing import Any

from orchestrator import state
from orchestrator.github import parse_repo_url
from orchestrator.models import WorkUnit

# Single source of truth for the "Coder's PR description" block heading.
# Exported so ``orchestrator.cycle_log.cycle_log_summary`` can locate the
# block by exact-match rather than by prefix — if this string is ever
# reworded, the import in ``cycle_log.py`` fails loudly instead of
# silently degrading to a no-op stripper.
PR_DESCRIPTION_HEADING = "## Coder's PR description (verbatim, as of last capture)"

# F-017 § Acceptance: every new cycle log carries a ``## TL;DR`` block
# between ``## PR`` and ``## Coder's PR description``, with three
# required H3 sub-headings (always present — empty sub-sections get a
# TBD placeholder so downstream prompts remain grep-stable).
TLDR_HEADING = "## TL;DR"
TLDR_SUBHEADINGS: tuple[str, ...] = (
    "### What shipped",
    "### Downstream contract",
    "### Decisions worth knowing",
)
# F-017 § Decisions: "Empty sub-sections still emit the heading with a
# TBD placeholder — callers and tests can rely on heading presence."
TLDR_PLACEHOLDER = "_TBD — coder did not fill in this sub-section._"


def _unit_basename(unit_id: str) -> str:
    """Return ``U-N`` from ``F-XXX-U-N``. Falls back to the raw id."""
    parts = unit_id.rsplit("-", 2)
    if len(parts) >= 2 and parts[-2] == "U":
        return f"U-{parts[-1]}"
    return unit_id


def _feature_id_from_unit_id(unit_id: str) -> str:
    """Best-effort: derive ``F-XXX`` from ``F-XXX-U-N``."""
    if "-U-" in unit_id:
        return unit_id.split("-U-", 1)[0]
    return unit_id


# Map state-event types to the worker / outcome label used in the cycle
# history headings. Events not in this map are skipped (e.g. ``spawn_*``
# events are scheduler chatter, not cycle outcomes).
_EVENT_HEADINGS: dict[str, str] = {
    "pr_opened": "coder: PR opened",
    "coder_blocked": "coder: BLOCKED",
    "coder_no_marker": "coder: NO_MARKER",
    "coder_resume_error": "coder: ERROR",
    "tests_pass": "tester: TESTS_PASS",  # nosec B105 — heading label, not a credential
    "tester_bug_found": "tester: BUG_FOUND",
    "tester_blocked": "tester: BLOCKED",
    "tester_error": "tester: ERROR",
    "reviewer_recommend_merge": "reviewer: REVIEW_RECOMMEND_MERGE",
    "reviewer_request_changes": "reviewer: REVIEW_REQUEST_CHANGES",
    "reviewer_comment": "reviewer: REVIEW_COMMENT",
    "reviewer_blocked": "reviewer: BLOCKED",
    "reviewer_error": "reviewer: ERROR",
    "fix_pushed": "coder fix: FIX_PUSHED",
    "coder_blocked_on_fix": "coder fix: BLOCKED",
    # F-007 ultrareview gate (opt-in terminal pass after reviewer endorses).
    # Without these, a unit that escalated *because* ultrareview failed shows
    # the reviewer endorsement and nothing else — the persistent on-disk
    # cycle log silently drops the entire reason for escalation.
    "ultrareview_started": "ultrareview: STARTED",
    "ultrareview_passed": "ultrareview: PASSED",
    "ultrareview_failed": "ultrareview: FAILED",
}

# F-007-U-4 fix-loop events carry the cycle number in the type suffix
# (``ultrareview_fix_cycle_1``, ``_2``, ...) so they can't live in the static
# ``_EVENT_HEADINGS`` table. The renderer routes through ``_heading_for``
# instead of a direct ``_EVENT_HEADINGS.get`` so the dynamic prefix is
# resolved alongside the static keys — without this, the U-3 H1 pattern
# (events recorded but dropped by the renderer) silently recurs for every
# ultrareview-driven fix cycle.
_ULTRAREVIEW_FIX_CYCLE_PREFIX = "ultrareview_fix_cycle_"


def _heading_for(event_type: str) -> str | None:
    """Resolve the cycle-history heading for ``event_type``, or ``None`` to skip.

    Handles both the static ``_EVENT_HEADINGS`` map and the dynamic
    ``ultrareview_fix_cycle_N`` family introduced in F-007-U-4 (where ``N``
    is the shared CAP_3 cycle number address_review writes for the coder
    resume). Routing both lookups through one function keeps the renderer's
    loop a single line — and ensures any future dynamic-suffix event type
    only needs to extend this helper, not every call site.
    """
    if event_type.startswith(_ULTRAREVIEW_FIX_CYCLE_PREFIX):
        n = event_type[len(_ULTRAREVIEW_FIX_CYCLE_PREFIX) :]
        return f"ultrareview: fix cycle {n}"
    return _EVENT_HEADINGS.get(event_type)


_REVIEW_TIER_PREFIX = ("🔴", "🟠", "🟡", "🔵")


def _lookup_unit(unit_id: str, feature_id: str) -> WorkUnit | None:
    plan = state.get_plan(feature_id)
    if plan is None:
        return None
    return next((u for u in plan.units if u.id == unit_id), None)


def _render_pr_section(
    pr_info: dict[str, Any],
    pr_number: int | None,
    repo_url: str,
    unit_status: str,
    status_ts: str = "",
    merge_commit_sha: str | None = None,
) -> list[str]:
    lines = ["## PR"]
    if pr_number and repo_url:
        try:
            owner, repo = parse_repo_url(repo_url)
            lines.append(f"#{pr_number} · https://github.com/{owner}/{repo}/pull/{pr_number}")
        except ValueError:
            lines.append(f"#{pr_number}")
    elif pr_number:
        lines.append(f"#{pr_number}")
    else:
        lines.append("_no PR opened_")
    status_line = f"Status: {unit_status}"
    if status_ts:
        # Match the proposal § "Per-unit cycle log" example: `Status: merged
        # (2026-05-15 14:32 UTC)`. ``last_activity`` is already UTC ISO-8601
        # so we surface it verbatim and tag the timezone.
        status_line += f" ({status_ts} UTC)"
    lines.append(status_line)
    head_sha = pr_info.get("headRefOid") or ""
    lines.append(f"PR head SHA: {head_sha or '_unknown_'}")
    # `mergeCommit.oid` only exists once the PR is merged; the backfill
    # path (F-006-U-3: ``reconcile_unit_pr``) re-renders the log with the
    # SHA supplied so the finalized cycle log records the commit on main.
    # Pre-merge writes omit the line entirely — see the proposal
    # § "Per-unit cycle log" "Two SHAs captured at different points".
    if merge_commit_sha:
        lines.append(f"Merge commit SHA: {merge_commit_sha}")
    return lines


def _render_pr_description(pr_info: dict[str, Any]) -> list[str]:
    body = (pr_info.get("body") or "").rstrip()
    lines = [PR_DESCRIPTION_HEADING]
    lines.append(body if body else "_unavailable_")
    return lines


def _extract_tldr_subsections(pr_body: str) -> dict[str, str]:
    """Mirror the coder PR body's three canonical ``### `` sub-sections.

    Returns ``{sub_heading: content}`` keyed by the canonical names in
    :data:`TLDR_SUBHEADINGS`. Missing sub-sections map to ``""``; the
    renderer then substitutes :data:`TLDR_PLACEHOLDER` per F-017
    § Decisions ("Empty sub-sections still emit the heading with a TBD
    placeholder").

    Matching is *strict* on the canonical heading text (F-017 § Open
    questions — revisit only if reviewers report frequent mismatches).
    A sub-section's body runs from the heading line to the next markdown
    heading at any level, or EOF.
    """
    found: dict[str, str] = dict.fromkeys(TLDR_SUBHEADINGS, "")
    if not pr_body:
        return found
    for name in TLDR_SUBHEADINGS:
        # Strict canonical-heading match: only spaces/tabs may follow the
        # canonical text on the heading line. Without that anchor, the
        # extractor would silently match prefix collisions like
        # ``### What shippedness`` and mirror the wrong content
        # (Copilot PR #63 review). Sub-section body then runs to the next
        # markdown heading at any level, or EOF.
        pattern = rf"^{re.escape(name)}[ \t]*\n(?P<content>.*?)(?=^#{{1,}}\s|\Z)"
        m = re.search(pattern, pr_body, re.MULTILINE | re.DOTALL)
        if m:
            found[name] = m.group("content").strip()
    return found


def _render_tldr(pr_info: dict[str, Any]) -> list[str]:
    """Render the ``## TL;DR`` block from the coder's PR body.

    Always emits the H2 heading and all three H3 sub-headings — missing
    sub-sections fall through to :data:`TLDR_PLACEHOLDER` so the format
    stays grep-stable (F-017 § Acceptance #1, #4) and the downstream
    worker prompt content is predictable (F-017 § Decisions).
    """
    sections = _extract_tldr_subsections(pr_info.get("body") or "")
    lines = [TLDR_HEADING]
    for name in TLDR_SUBHEADINGS:
        lines.append("")
        lines.append(name)
        lines.append(sections[name] or TLDR_PLACEHOLDER)
    return lines


def _render_cycle_history(
    events: list[dict[str, Any]],
    *,
    unit_status: str = "",
) -> list[str]:
    rendered: list[dict[str, Any]] = []
    for ev in events:
        heading = _heading_for(ev["event_type"])
        if heading is None:
            continue
        rendered.append(
            {
                "cycle": ev.get("cycle_number") if ev.get("cycle_number") is not None else 0,
                "heading": heading,
                "summary": (ev.get("summary") or "").strip(),
            }
        )

    cycle_count = max((r["cycle"] for r in rendered), default=0)
    # "cap-3 hit" ⇔ the unit was escalated *because* the cap was reached.
    # A unit that runs 3 cycles and is approved on cycle 3 has
    # ``cycle_count == 3`` but the cap was NOT hit — see the proposal §
    # "Per-unit cycle log" example, which renders that case as
    # `3 cycles · cap-3 not hit`. The execution-side enforcement at
    # ``review_round >= CAP_3`` only escalates when the *next* fix would
    # exceed the cap, so unit_status is the authoritative signal.
    cap_hit = unit_status == "escalated" and cycle_count >= 3
    lines = ["## Cycle history"]
    lines.append(f"{cycle_count} cycles · " + ("cap-3 hit" if cap_hit else "cap-3 not hit"))
    if not rendered:
        lines.append("")
        lines.append("_no cycle events recorded_")
        return lines

    for entry in rendered:
        lines.append("")
        lines.append(f"### Cycle {entry['cycle']} — {entry['heading']}")
        summary = entry["summary"] or "_no summary recorded_"
        lines.append(f"- {summary}")
    return lines


def _tier_marker(body: str) -> str:
    """Return the leading tier emoji if the comment starts with one."""
    stripped = body.lstrip()
    for marker in _REVIEW_TIER_PREFIX:
        if stripped.startswith(marker):
            return marker
    return ""


def _excerpt(body: str, limit: int = 160) -> str:
    """Single-line, length-capped excerpt for the threads index."""
    flat = " ".join(body.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _render_review_threads(threads: list[dict[str, Any]]) -> list[str]:
    lines = ["## Review threads"]
    if not threads:
        lines.append("_no review threads_")
        return lines
    for t in threads:
        marker = _tier_marker(t.get("body", ""))
        path = t.get("path") or "(general)"
        line_no = t.get("line")
        loc = f"{path}:{line_no}" if line_no else path
        url = t.get("url") or ""
        excerpt = _excerpt(t.get("body", ""))
        status_bits = []
        if t.get("isResolved"):
            status_bits.append("resolved")
        if t.get("isOutdated"):
            status_bits.append("outdated")
        status = f" [{', '.join(status_bits)}]" if status_bits else ""
        prefix = f"{marker} " if marker else ""
        bullet = f"- {prefix}{loc}{status} — {excerpt}"
        if url:
            bullet += f" ({url})"
        lines.append(bullet)
    return lines


def render_cycle_log(
    unit_id: str,
    *,
    pr_info: dict[str, Any] | None = None,
    review_threads: list[dict[str, Any]] | None = None,
    merge_commit_sha: str | None = None,
) -> str:
    """Render the cycle-log markdown for ``unit_id`` from current state.

    ``pr_info`` and ``review_threads`` come from
    ``orchestrator.cycle_log_gh.fetch_pr_info`` /
    ``fetch_review_threads`` in normal operation; pass them in directly
    for tests or for an offline regenerate.

    ``merge_commit_sha`` is captured separately by ``reconcile_unit_pr`` once
    the PR confirms merged (the post-merge backfill — the only edit
    allowed after the cycle log has been finalized). When omitted the
    log renders without a "Merge commit SHA" line.
    """
    pr_info = pr_info or {}
    review_threads = review_threads or []

    unit_state = state.get_unit_state(unit_id)
    feature_id = unit_state.feature_id if unit_state else _feature_id_from_unit_id(unit_id)
    feature = state.get_feature(feature_id)
    unit = _lookup_unit(unit_id, feature_id)

    title = unit.title if unit else (pr_info.get("title") or "")
    header = f"# {unit_id}" + (f" — {title}" if title else "")

    blocks: list[list[str]] = [
        [header],
        _render_pr_section(
            pr_info,
            unit_state.pr_number if unit_state else None,
            feature.repo_path if feature else "",
            unit_state.status if unit_state else "unknown",
            status_ts=unit_state.last_activity if unit_state else "",
            merge_commit_sha=merge_commit_sha,
        ),
        # F-017: TL;DR sits *above* the strip boundary used by
        # ``cycle_log.cycle_log_summary`` so predecessor injection naturally
        # picks it up — no caller change needed.
        _render_tldr(pr_info),
        _render_pr_description(pr_info),
        _render_cycle_history(
            state.list_events(unit_id) if unit_state else [],
            unit_status=unit_state.status if unit_state else "",
        ),
        _render_review_threads(review_threads),
    ]
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


__all__ = [
    "PR_DESCRIPTION_HEADING",
    "TLDR_HEADING",
    "TLDR_PLACEHOLDER",
    "TLDR_SUBHEADINGS",
    "render_cycle_log",
]
