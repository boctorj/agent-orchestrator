"""Cost telemetry derived from unit_events timestamps.

This is an approximation, not exact billing. Computes per-unit and
per-feature wall-clock session time from event timestamps and multiplies
by the published session-hour rate. Token costs are NOT included
(would require Anthropic's billing API).

Use the numbers for relative comparisons ("U-3 cost 2x U-1") and rough
totals ("this feature was ~$0.50"), not for invoicing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from orchestrator import state

SESSION_HOURLY_RATE_USD = 0.08
"""Published Anthropic Managed Agents session-hour billing rate (May 2026)."""


def _parse_iso(ts: str) -> datetime:
    # state writes ISO-8601 with timezone; fromisoformat handles it in Py 3.11+
    return datetime.fromisoformat(ts)


def compute_unit_cost(unit_id: str) -> dict:
    """Approximate cost for one unit based on event timestamps.

    Per session, wall-clock seconds = (last event ts) - (first event ts).
    Sum across all sessions touching this unit. Multiply by hourly rate.

    Returns:
        {
            unit_id,
            wall_clock_seconds,          # total elapsed for the unit
            total_session_seconds,       # sum of per-session durations
            session_count,
            sessions: [{session_id, wall_seconds}, ...],
            est_session_cost_usd,
            note,
        }
    """
    events = state.list_events(unit_id)
    if not events:
        return {"unit_id": unit_id, "error": "no events"}

    # Wall clock for the unit
    first_ts = events[0]["ts"]
    last_ts = events[-1]["ts"]
    wall_clock_seconds = round((_parse_iso(last_ts) - _parse_iso(first_ts)).total_seconds(), 1)

    # Per-session durations
    by_session: dict[str, list[str]] = defaultdict(list)
    for e in events:
        sid = e.get("session_id", "")
        if sid:
            by_session[sid].append(e["ts"])

    sessions = []
    total_session_seconds = 0.0
    for sid, ts_list in by_session.items():
        ts_list.sort()
        dur = (_parse_iso(ts_list[-1]) - _parse_iso(ts_list[0])).total_seconds()
        sessions.append({"session_id": sid, "wall_seconds": round(dur, 1)})
        total_session_seconds += dur

    est_cost = total_session_seconds / 3600.0 * SESSION_HOURLY_RATE_USD

    return {
        "unit_id": unit_id,
        "wall_clock_seconds": wall_clock_seconds,
        "total_session_seconds": round(total_session_seconds, 1),
        "session_count": len(sessions),
        "sessions": sessions,
        "est_session_cost_usd": round(est_cost, 4),
        "note": "session-hour estimate only; token costs not included",
    }


def compute_feature_cost(feature_id: str) -> dict:
    """Aggregate cost across all units of a feature.

    Returns:
        {
            feature_id,
            unit_count,
            per_unit: [...],
            total_session_seconds,
            est_total_cost_usd,
            note,
        }
    """
    units = state.list_unit_states(feature_id)
    per_unit = [compute_unit_cost(u.unit_id) for u in units]

    total_seconds = sum(u.get("total_session_seconds", 0) for u in per_unit)
    total_cost = sum(u.get("est_session_cost_usd", 0) for u in per_unit)

    return {
        "feature_id": feature_id,
        "unit_count": len(units),
        "per_unit": per_unit,
        "total_session_seconds": round(total_seconds, 1),
        "est_total_cost_usd": round(total_cost, 4),
        "note": "session-hour estimate only; token costs not included",
    }
