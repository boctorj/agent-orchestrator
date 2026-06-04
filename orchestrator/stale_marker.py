"""Stale-marker delta-review rule (F-016 Phase 2.5).

The lead can ``send_to_unit_async`` the coder while a reviewer marker
is already on the books for an earlier PR head. By the time the
reviewer's verdict was emitted, the coder may have pushed new commits
in response to the lead's message — the marker is now describing
*pre-push* code. The Phase 3 daemon's reconcile tick must catch this
on re-scan after the advance-lock window expires; otherwise
``advance_to_terminal`` would land on a stale endorsement.

Per ``docs/PROPOSAL-async-orchestrator.md`` § "Stale-marker handling
(reviewer specifically)"::

    case A — reviewer.reviewed_sha == pr.head_sha:
        valid — record as terminal normally
    case B — reviewer.reviewed_sha < pr.head_sha (coder pushed):
        stale — record ``reviewer_stale_marker_pending_delta`` event;
        resume reviewer session with a delta-review prompt
    case C — reviewer hasn't emitted yet:
        no-op; next tick catches the marker

This module ships the pure classifier (:func:`classify_marker_freshness`)
and the audit-event helper (:func:`record_stale_marker_pending_delta`)
the Phase 3 daemon will compose into its reconcile loop. The delta
prompt itself reuses the existing F-013 machinery in
:func:`orchestrator.tools.execution._resume_reviewer_for_delta` — the
daemon calls that helper rather than reimplementing the resume here.
"""

from __future__ import annotations

from typing import Literal

from orchestrator import state

MarkerFreshness = Literal["valid", "stale", "not_emitted"]

# Reviewer events that ``classify_marker_freshness`` recognises as
# "marker emitted". RECOMMEND_MERGE and COMMENT are the endorsement /
# comment-only terminals; REQUEST_CHANGES is intentionally absent —
# a request-changes marker leaves the unit in ``fixing`` and the
# delta-review machinery already handles that path via the coder loop.
REVIEWER_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"reviewer_recommend_merge", "reviewer_comment"}
)

# Coder events that, when timestamped after a reviewer marker, indicate
# the PR head has moved since the reviewer reviewed. ``fix_pushed`` is
# the dominant signal (every ``address_review`` follow-up records one);
# ``pr_opened`` covers the rare race where the coder re-opens during
# the reviewing window.
CODER_PUSH_EVENT_TYPES: frozenset[str] = frozenset({"fix_pushed", "pr_opened"})


def classify_marker_freshness(
    *,
    reviewer_marker_sha: str | None,
    current_pr_head_sha: str | None,
    marker_emitted: bool,
) -> MarkerFreshness:
    """Return the spec's case A/B/C label for a reviewer marker.

    Pure function — no I/O, fully deterministic given inputs:

      * ``not_emitted`` when ``marker_emitted`` is False, regardless of
        SHAs.
      * ``valid`` when both SHAs are known and equal — case A.
      * ``stale`` when both SHAs are known and differ — case B.
      * ``valid`` (conservative) when either SHA is unavailable — we
        can't prove staleness without both anchors, and a false-stale
        triggers an unnecessary delta resume. The freshness rule is a
        defense against a known race; absence of evidence is not
        evidence of staleness.

    Callers that lack a SHA but have stronger ordering evidence (e.g.
    event timestamps from ``unit_events``) should use
    :func:`detect_stale_reviewer_marker` instead, which composes
    multiple signals.
    """
    if not marker_emitted:
        return "not_emitted"
    if not reviewer_marker_sha or not current_pr_head_sha:
        # Conservative — see docstring. The daemon's next tick re-derives
        # the action; the false-valid here just delays the (correct)
        # transition by one poll cycle if the SHAs become known later.
        return "valid"
    if reviewer_marker_sha == current_pr_head_sha:
        return "valid"
    return "stale"


def _latest_event_of_types(
    events: list[dict],
    event_types: frozenset[str],
) -> dict | None:
    """Most-recent event whose ``event_type`` is in ``event_types``.

    ``events`` is oldest-first (per :func:`state.list_events`), so we
    iterate in reverse and return the first hit. Returns ``None`` if no
    event of those types exists.
    """
    for event in reversed(events):
        if event.get("event_type") in event_types:
            return event
    return None


def detect_stale_reviewer_marker(unit_id: str) -> dict:
    """Classify the unit's most-recent reviewer marker against later coder pushes.

    Composes two signals:

      * **SHA comparison** (preferred): when the reviewer marker carried
        the head_sha it saw, and the unit's current PR head_sha is
        known, compare via :func:`classify_marker_freshness`.
      * **Timestamp ordering** (fallback): when the SHAs aren't recorded,
        the helper inspects ``unit_events`` for any
        ``CODER_PUSH_EVENT_TYPES`` row whose ``ts`` is later than the
        reviewer marker's ``ts``. A later coder push implies the PR
        head moved, so the marker is stale.

    Returns a dict shape::

        {
            "case": "valid" | "stale" | "not_emitted",
            "reviewer_marker_event_type": str | None,
            "reviewer_marker_ts": str | None,
            "later_coder_push_ts": str | None,
            "later_coder_push_event_type": str | None,
        }

    Phase 2.5 ships this as the daemon's discoverability surface; the
    Phase 3 daemon's reconcile loop calls this once per poll on units in
    ``reviewing`` / ``approved_awaiting_merge``-pre-emit.
    """
    events = state.list_events(unit_id)
    reviewer_event = _latest_event_of_types(events, REVIEWER_TERMINAL_EVENT_TYPES)
    if reviewer_event is None:
        return {
            "case": "not_emitted",
            "reviewer_marker_event_type": None,
            "reviewer_marker_ts": None,
            "later_coder_push_ts": None,
            "later_coder_push_event_type": None,
        }

    reviewer_ts = reviewer_event.get("ts", "")
    # Look for any coder push event after the reviewer's marker timestamp.
    later_push: dict | None = None
    for event in reversed(events):
        if event.get("event_type") in CODER_PUSH_EVENT_TYPES and event.get("ts", "") > reviewer_ts:
            later_push = event
            break

    case: MarkerFreshness = "stale" if later_push is not None else "valid"
    return {
        "case": case,
        "reviewer_marker_event_type": reviewer_event.get("event_type"),
        "reviewer_marker_ts": reviewer_ts,
        "later_coder_push_ts": later_push.get("ts") if later_push else None,
        "later_coder_push_event_type": later_push.get("event_type") if later_push else None,
    }


def record_stale_marker_pending_delta(
    unit_id: str,
    feature_id: str,
    *,
    reviewer_event_type: str,
    later_push_event_type: str,
) -> bool:
    """Append the ``reviewer_stale_marker_pending_delta`` audit row.

    The Phase 3 daemon calls this when :func:`detect_stale_reviewer_marker`
    returns ``case == "stale"``, *before* it submits the delta-review
    prompt to the reviewer session. The event row is the audit anchor
    the lead and the dashboard can surface ("daemon detected stale
    marker, requested delta re-review") and the Phase 0 dedupe contract
    keys it per ``(unit, reviewer marker, later push)`` so a poll loop
    that hits the same staleness twice writes one row.

    Returns the boolean :func:`state.record_event` returns — ``True``
    if a row landed, ``False`` if the dedupe-keyed row already existed.
    """
    dedupe_key = f"{unit_id}|{reviewer_event_type}|{later_push_event_type}|pending_delta"
    return state.record_event(
        unit_id,
        feature_id,
        "reviewer_stale_marker_pending_delta",
        source="orchestrator",
        summary=(
            f"reviewer {reviewer_event_type} stale vs later {later_push_event_type}; "
            f"delta re-review pending"
        ),
        dedupe_key=dedupe_key,
    )
