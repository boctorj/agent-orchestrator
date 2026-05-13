"""Read-only state inspection + cost reporting + dashboard MCP tools."""

from __future__ import annotations

import json
from dataclasses import asdict

from orchestrator import blocked_hints, costs, state
from orchestrator.tools import mcp


@mcp.tool()
def get_unit_status(unit_id: str) -> str:
    """Read the persisted state of one work unit."""
    s = state.get_unit_state(unit_id)
    if not s:
        return f"No state for unit {unit_id} (not yet spawned)"
    return json.dumps(asdict(s), indent=2)


@mcp.tool()
def list_units(feature_id: str) -> str:
    """List all units for a feature with their state."""
    units = state.list_unit_states(feature_id)
    if not units:
        return f"No units spawned yet for {feature_id}"
    return json.dumps([asdict(u) for u in units], indent=2)


@mcp.tool()
def unit_history(unit_id: str, reason: str = "") -> str:
    """Return the full event timeline for a unit (oldest first).

    ``reason``: when non-empty, restrict the result to events whose
    ``details`` JSON carries a matching ``reason`` slug (see
    :mod:`orchestrator.blocked_hints`). Useful for chat queries like
    "show me everything blocked on auth for U-1" — the lead passes
    ``reason="auth_failure"`` and gets only those events. Empty string
    (the default) preserves the original "all events" behaviour.
    """
    events = state.list_events(unit_id)
    if not events:
        return f"No events for {unit_id}"
    if reason:
        events = [
            e
            for e in events
            if blocked_hints.extract_reason_from_details(e.get("details") or "")[0] == reason
        ]
        if not events:
            return f"No events for {unit_id} matching reason={reason!r}"
    return json.dumps(events, indent=2)


@mcp.tool()
def unit_summary(unit_id: str) -> str:
    """Human-readable digest of a unit's lifecycle."""
    return json.dumps(state.summarize_unit(unit_id), indent=2)


@mcp.tool()
def unit_cost(unit_id: str) -> str:
    """Approximate cost for one unit based on session wall-clock time.

    Returns JSON with per-session breakdown + total estimated USD cost.
    Note: only session-hour billing is estimated; token costs not included.
    """
    return json.dumps(costs.compute_unit_cost(unit_id), indent=2)


@mcp.tool()
def feature_cost(feature_id: str) -> str:
    """Aggregate cost across all units in a feature.

    Returns JSON with per-unit breakdown + feature total. Same caveats as
    unit_cost (session-hour estimate only).
    """
    return json.dumps(costs.compute_feature_cost(feature_id), indent=2)


@mcp.tool()
def show_dashboard() -> str:
    """Return a markdown snapshot of the orchestrator state for chat display.

    Same data as the TUI dashboard, formatted for chat readability. Includes:
      - All features with status, cost, progress
      - Units in flight
      - PRs awaiting the user's merge
      - Escalated units
      - 10 most-recent events across all features

    Call when:
      - User asks "what's the status / state / dashboard"
      - Starting a fresh conversation (orientation after restart)
      - After a parallel_units_global batch completes (cross-feature digest)

    Output is point-in-time. For real-time, run `orchestrator dashboard`
    in a separate terminal.
    """
    from orchestrator import dashboard

    return dashboard.render_markdown()
