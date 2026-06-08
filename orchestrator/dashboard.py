"""TUI dashboard for the orchestrator state, plus a markdown renderer
that the lead can surface inside a Claude Code chat (or mobile via
Remote Control).

Run the TUI from the project root:
    python -m orchestrator.dashboard       # or ./scripts/dashboard.sh
Refreshes every 2s. Ctrl+C to quit.

The same data backs `render_markdown()`, which the MCP tool
`show_dashboard()` returns to the lead for in-chat display.

Reads-only — never writes to state.db. Safe to run alongside the MCP server.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import time
from datetime import datetime

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orchestrator import blocked_hints, costs, state

REFRESH_SECONDS = 2.0

# --- status → (color, emoji) ---
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "draft": ("dim", ""),
    "planned": ("blue", "📋"),
    "approved": ("cyan", "✓"),
    "pending": ("dim", ""),
    "coding": ("yellow", "🔧"),
    "testing": ("yellow", "🧪"),
    "opening_pr": ("yellow", "📤"),
    "in_ci": ("magenta", "🔵"),
    "reviewing": ("yellow", "👀"),
    "fixing": ("yellow", "🔧"),
    "approved_awaiting_merge": ("green", "🟢"),
    "done": ("green", "✅"),
    "escalated": ("bold red", "🚨"),
    # F-016 Phase 2.5: sticky-cancel terminal. Distinct from `escalated`
    # (a triage-required failure) — `cancelled` means the user
    # explicitly pulled the unit, so it renders dim rather than red.
    "cancelled": ("dim", "🚫"),
    "in_progress": ("yellow", "🔄"),
}

ACTIVE_STATUSES = {"coding", "testing", "opening_pr", "in_ci", "reviewing", "fixing"}

TERMINAL_REVIEW_EVENTS = {
    "reviewer_recommend_merge",
    "reviewer_approved",
    "reviewer_comment",
}

CODER_RESTART_EVENTS = {"coder_resumed", "spawn_coder"}


def _style_status(status: str) -> Text:
    style, emoji = STATUS_STYLE.get(status, ("", ""))
    label = f"{emoji} {status}" if emoji else status
    return Text(label.strip(), style=style)


def _status_md(status: str) -> str:
    """Markdown-friendly emoji + label, no rich styling."""
    _, emoji = STATUS_STYLE.get(status, ("", ""))
    return f"{emoji} {status}" if emoji else status


def _hh_mm_ss(iso_ts: str) -> str:
    try:
        return datetime.fromisoformat(iso_ts).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return iso_ts or "?"


def _is_awaiting_merge(unit_id: str) -> bool:
    events = state.list_events(unit_id)
    for e in reversed(events):
        etype = e["event_type"]
        if etype in CODER_RESTART_EVENTS:
            return False
        if etype in TERMINAL_REVIEW_EVENTS:
            return True
    return False


# --------------------------- data fetchers (renderer-agnostic) ---------------------------


def _features_data() -> list[dict]:
    rows = []
    for f in state.list_features():
        units = state.list_unit_states(f.id)
        plan = state.get_plan(f.id)
        plan_total = len(plan.units) if plan else len(units)
        done = sum(1 for u in units if u.status == "done")
        try:
            cost = costs.compute_feature_cost(f.id).get("est_total_cost_usd", 0.0)
            cost_str = f"${cost:.2f}" if cost else "—"
        except Exception:
            cost_str = "—"
        rows.append(
            {
                "id": f.id,
                "title": f.title or "(no title)",
                "status": f.status,
                "cost": cost_str,
                "units": f"{done}/{plan_total}" if plan_total else "—",
            }
        )
    return rows


def _open_db_ro() -> sqlite3.Connection:
    """Open the state DB with Row factory. Caller wraps in contextlib.closing."""
    conn = sqlite3.connect(state.STATE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _in_flight_data() -> list[dict]:
    with contextlib.closing(_open_db_ro()) as conn:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        # `placeholders` is "?,?,?" built from a fixed-size tuple of literal
        # statuses; the actual values bind through the params tuple.
        rows = conn.execute(
            f"SELECT * FROM work_units WHERE status IN ({placeholders}) ORDER BY last_activity DESC",  # noqa: S608  # nosec B608
            tuple(ACTIVE_STATUSES),
        ).fetchall()
    # ``approved_awaiting_merge`` is the dedicated bucket for endorsed-but-
    # not-merged units (F-009-U-4) and is NOT in ``ACTIVE_STATUSES``, so the
    # SQL filter already excludes it. The old post-filter against
    # ``_is_awaiting_merge`` (event-driven) was redundant for the new design
    # and additionally hid transitional rows (``in_ci`` + stale
    # ``reviewer_recommend_merge`` event from the pre-F-009-U-4 send_to_unit
    # path) from both panels — drop it.
    return [
        {
            "unit_id": r["unit_id"],
            "feature_id": r["feature_id"],
            "status": r["status"],
            "branch": r["branch"] or "—",
            "pr": f"#{r['pr_number']}" if r["pr_number"] else "—",
            "cycle": r["review_round"],
        }
        for r in rows
    ]


def _awaiting_merge_data() -> list[dict]:
    """Units whose reviewer endorsed the PR but the human hasn't merged yet.

    Authoritative source: ``work_units.status == 'approved_awaiting_merge'``
    (written by ``cycle_review._emit_terminal`` and by ``send_to_unit`` via
    ``_record_terminal_marker``). Closes audit Gap H (F-009-U-4) — the
    pre-existing event-driven heuristic (:func:`_is_awaiting_merge`) is
    no longer consulted by either bucket query and remains only as a
    diagnostic helper.
    """
    with contextlib.closing(_open_db_ro()) as conn:
        rows = conn.execute(
            "SELECT * FROM work_units WHERE status = 'approved_awaiting_merge' "
            "ORDER BY last_activity DESC"
        ).fetchall()
    return [
        {
            "pr": f"#{r['pr_number']}" if r["pr_number"] else "—",
            "unit_id": r["unit_id"],
            "feature_id": r["feature_id"],
            "branch": r["branch"] or "—",
        }
        for r in rows
    ]


def _escalated_data() -> list[dict]:
    """Return rows for the dashboard's escalated panel.

    For units whose most-recent BLOCKED-style event carries a structured
    reason (see :mod:`orchestrator.blocked_hints`), the ``last_error``
    field is rendered as the full ``reason -> hint -> prose`` summary
    with no truncation — the remediation tail is exactly the part the
    lead needs to read. For unclassified failures (``reason="unknown"``)
    the legacy 120-char truncation is preserved so the panel stays
    compact on noisy/longer messages.
    """
    with contextlib.closing(_open_db_ro()) as conn:
        rows = conn.execute(
            "SELECT * FROM work_units WHERE status = 'escalated' ORDER BY last_activity DESC"
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        raw_error = r["last_error"] or "(no error message)"
        reason, prose = blocked_hints.latest_blocked_reason(r["unit_id"])
        if reason != blocked_hints.UNKNOWN_REASON:
            # Build a structured rendering; combine prose from the event
            # with the unit-state's last_error so both are visible.
            display_prose = prose or raw_error
            display = blocked_hints.format_escalation_summary(reason, display_prose)
        else:
            display = raw_error[:120]
        out.append(
            {
                "unit_id": r["unit_id"],
                "feature_id": r["feature_id"],
                "last_error": display,
                "reason": reason,
                "pr": f"#{r['pr_number']}" if r["pr_number"] else "—",
            }
        )
    return out


def _events_data(limit: int = 10) -> list[dict]:
    with contextlib.closing(_open_db_ro()) as conn:
        rows = conn.execute(
            "SELECT * FROM unit_events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {
            "time": _hh_mm_ss(r["ts"]),
            "unit_id": r["unit_id"],
            "event_type": r["event_type"],
            "source": r["source"],
            "summary": (r["summary"] or "")[:80],
        }
        for r in rows
    ]


# --------------------------- rich (TUI) panels ---------------------------


def _features_panel() -> Panel:
    table = Table(expand=True, show_edge=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Cost", justify="right", no_wrap=True)
    table.add_column("Units", justify="right", no_wrap=True)

    data = _features_data()
    if not data:
        table.add_row("—", "[dim]no features loaded[/dim]", "", "", "")
    for r in data:
        table.add_row(r["id"], r["title"], _style_status(r["status"]), r["cost"], r["units"])
    return Panel(table, title="📊 Features", border_style="cyan")


def _in_flight_panel() -> Panel:
    table = Table(expand=True, show_edge=False)
    table.add_column("Unit", style="cyan", no_wrap=True)
    table.add_column("Feature", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Branch", overflow="fold")
    table.add_column("PR", no_wrap=True)
    table.add_column("Cycle", justify="right", no_wrap=True)

    data = _in_flight_data()
    if not data:
        table.add_row("—", "", "[dim]nothing in flight[/dim]", "", "", "")
    for r in data:
        table.add_row(
            r["unit_id"],
            r["feature_id"],
            _style_status(r["status"]),
            r["branch"],
            r["pr"],
            str(r["cycle"]),
        )
    return Panel(table, title="🔧 In flight", border_style="yellow")


def _awaiting_merge_panel() -> Panel:
    table = Table(expand=True, show_edge=False)
    table.add_column("PR", style="bold green", no_wrap=True)
    table.add_column("Unit", style="cyan", no_wrap=True)
    table.add_column("Feature", no_wrap=True)
    table.add_column("Branch", overflow="fold")

    data = _awaiting_merge_data()
    if not data:
        table.add_row("—", "", "[dim]none awaiting merge[/dim]", "")
    for r in data:
        table.add_row(r["pr"], r["unit_id"], r["feature_id"], r["branch"])
    return Panel(table, title="🟢 Awaiting your merge", border_style="green")


def _escalated_panel() -> Panel:
    table = Table(expand=True, show_edge=False)
    table.add_column("Unit", style="cyan", no_wrap=True)
    table.add_column("Feature", no_wrap=True)
    table.add_column("Last error", overflow="fold")
    table.add_column("PR", no_wrap=True)

    data = _escalated_data()
    if not data:
        table.add_row("—", "", "[dim]none[/dim]", "")
    for r in data:
        table.add_row(r["unit_id"], r["feature_id"], r["last_error"], r["pr"])
    return Panel(table, title="🚨 Escalated", border_style="red" if data else "dim")


def _events_panel(limit: int = 10) -> Panel:
    table = Table(expand=True, show_edge=False)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Unit", style="cyan", no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("Source", style="dim", no_wrap=True)
    table.add_column("Summary", overflow="fold")

    data = _events_data(limit)
    if not data:
        table.add_row("—", "", "[dim]no events yet[/dim]", "", "")
    for r in data:
        table.add_row(r["time"], r["unit_id"], r["event_type"], r["source"], r["summary"])
    return Panel(table, title="📜 Recent events", border_style="blue")


# --------------------------- markdown renderer (for chat display) ---------------------------


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Tiny markdown-table renderer. Escapes pipes."""

    def esc(cell: str) -> str:
        return str(cell).replace("|", "\\|")

    lines = ["| " + " | ".join(esc(h) for h in headers) + " |"]
    lines.append("|" + "|".join(" --- " for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)


def render_markdown() -> str:
    """Render the dashboard as a markdown string suitable for chat display.

    Sections: Features, In flight, Awaiting your merge, Escalated, Recent events.
    Used by the show_dashboard MCP tool so the lead can surface state in chat.
    """
    out: list[str] = []
    ts = datetime.now().strftime("%H:%M:%S")

    # Features
    out.append("## 📊 Features")
    fd = _features_data()
    if not fd:
        out.append("_no features loaded_")
    else:
        out.append(
            _md_table(
                ["ID", "Title", "Status", "Cost", "Units"],
                [[r["id"], r["title"], _status_md(r["status"]), r["cost"], r["units"]] for r in fd],
            )
        )

    # In flight
    out.append("\n## 🔧 In flight")
    inf = _in_flight_data()
    if not inf:
        out.append("_nothing in flight_")
    else:
        out.append(
            _md_table(
                ["Unit", "Feature", "Status", "Branch", "PR", "Cycle"],
                [
                    [
                        r["unit_id"],
                        r["feature_id"],
                        _status_md(r["status"]),
                        r["branch"],
                        r["pr"],
                        str(r["cycle"]),
                    ]
                    for r in inf
                ],
            )
        )

    # Awaiting merge
    out.append("\n## 🟢 Awaiting your merge")
    aw = _awaiting_merge_data()
    if not aw:
        out.append("_none awaiting merge_")
    else:
        out.append(
            _md_table(
                ["PR", "Unit", "Feature", "Branch"],
                [[r["pr"], r["unit_id"], r["feature_id"], r["branch"]] for r in aw],
            )
        )

    # Escalated
    out.append("\n## 🚨 Escalated")
    esc = _escalated_data()
    if not esc:
        out.append("_none_")
    else:
        out.append(
            _md_table(
                ["Unit", "Feature", "Last error", "PR"],
                [[r["unit_id"], r["feature_id"], r["last_error"], r["pr"]] for r in esc],
            )
        )

    # Recent events
    out.append("\n## 📜 Recent events")
    ev = _events_data(limit=10)
    if not ev:
        out.append("_no events yet_")
    else:
        out.append(
            _md_table(
                ["Time", "Unit", "Event", "Source", "Summary"],
                [[r["time"], r["unit_id"], r["event_type"], r["source"], r["summary"]] for r in ev],
            )
        )

    out.append(f"\n_snapshot @ {ts}_")
    return "\n".join(out)


# --------------------------- main TUI loop ---------------------------


def render() -> Group:
    return Group(
        _features_panel(),
        _in_flight_panel(),
        _awaiting_merge_panel(),
        _escalated_panel(),
        _events_panel(),
        Text(
            f"refreshing every {REFRESH_SECONDS:.0f}s · Ctrl+C to quit · {datetime.now().strftime('%H:%M:%S')}",
            style="dim italic",
        ),
    )


def render_once(console: Console | None = None) -> None:
    """One-shot render — useful for non-interactive smoke tests."""
    console = console or Console()
    console.print(render())


def main() -> int:
    console = Console()
    if not state.STATE_DB.exists():
        console.print(
            "[yellow]state.db not found.[/yellow] Run the orchestrator at least once first."
        )
        return 1

    try:
        with Live(render(), refresh_per_second=4, screen=True, console=console) as live:
            while True:
                time.sleep(REFRESH_SECONDS)
                live.update(render())
    except KeyboardInterrupt:
        console.print("\n[dim]dashboard stopped[/dim]")
        return 0


if __name__ == "__main__":
    sys.exit(main())
