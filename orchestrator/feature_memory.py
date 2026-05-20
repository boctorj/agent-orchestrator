"""Session-bootstrap memory blob for the lead persona.

Phase 1 of the feature-spec + cycle-logs work (see
``docs/PROPOSAL-feature-spec-and-headless-daemon.md`` §5 "Feature memory
bootstrap"). ``build_feature_memory(feature_id)`` returns a single
markdown string the lead reads at session start so a fresh chat can
re-orient without scrolling prior conversations.

Content (each section degrades to a placeholder if its data is absent):

  * ``spec.md`` — the lead-owned design doc.
  * ``git log -10 -- features/F-XXX/spec.md`` — the decision-log timeline
    (``Why:`` commit bodies are the timeline; see ``docs/SPEC-FORMAT.md``).
  * Per-unit ``unit_summary`` digest — status + cycle counts + last error.
  * Cycle-log "Final" sections (PR + last cycle subsection) for every
    unit whose cycle log exists on disk.
  * Recent escalation events across the feature.

Read-side null-safety is mandatory: every step degrades to a placeholder
rather than raising. The unit description names this explicitly
("Read-side must handle missing features/ directory gracefully"), and
the orchestrator-bot writer in ``cycle_log.py`` follows the same
best-effort discipline.
"""

from __future__ import annotations

import subprocess  # nosec B404 — `git log` is the whole point of one helper
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator import blocked_hints, state
from orchestrator.cycle_log_render import _unit_basename
from orchestrator.models import WorkUnitState

# ``subprocess.run``-shaped callable; tests swap in a fake. Matches the
# convention from ``orchestrator.cycle_log_gh.SubprocessRunner``.
SubprocessRunner = Callable[..., Any]

# Event types worth surfacing in "Recent escalations". Drawn from the
# ``record_event`` call sites in ``orchestrator/tools/execution.py`` —
# everything that flips a unit to ``escalated`` or carries a structured
# BLOCKED payload. Scheduling chatter (``spawn_*``, ``pr_opened``,
# ``fix_pushed``) is intentionally excluded.
_ESCALATION_EVENT_SUFFIXES = (
    "_blocked",
    "_blocked_on_fix",
    "_no_marker",
    "_error",
)

# How many escalation events to surface in the blob. Mirrors the
# ``git log -10`` size — enough to triage, small enough to keep the
# total blob under the proposal's ~7K-token soft cap.
_MAX_ESCALATION_EVENTS = 10

# How many spec.md commits to include in the decision-log section.
# Matches the unit description's "``git log -10 -- spec.md``" verbatim.
_SPEC_GIT_LOG_DEPTH = 10


# --------------------------- public API ---------------------------


def build_feature_memory(
    feature_id: str,
    *,
    base_dir: Path | None = None,
    run: SubprocessRunner | None = None,
) -> str:
    """Return the session-bootstrap markdown blob for ``feature_id``.

    ``base_dir`` defaults to ``state.STATE_DB.parent`` so production calls
    pick up the orchestrator workdir while tests using ``tmp_state_db``
    land in a temp tree. ``run`` defaults to ``subprocess.run``; tests
    swap in a recording fake.

    Returns an ``ERROR: ...`` string when the feature doesn't exist in
    state. Otherwise renders all six sections, each with a placeholder
    when its data source is empty.
    """
    feature = state.get_feature(feature_id)
    if feature is None:
        return f"ERROR: feature {feature_id} not found"

    root = base_dir if base_dir is not None else Path(state.STATE_DB).parent
    runner = run if run is not None else subprocess.run

    units = state.list_unit_states(feature_id)

    sections = [
        _render_header(feature_id, feature.title, feature.status, feature.repo_path),
        _render_spec(root, feature_id),
        _render_spec_git_log(root, feature_id, runner),
        _render_units(units),
        _render_cycle_log_finals(root, feature_id, units),
        _render_recent_escalations(units),
    ]
    return "\n\n".join(sections).rstrip() + "\n"


# --------------------------- section renderers ---------------------------


def _render_header(feature_id: str, title: str, status: str, repo_path: str) -> str:
    lines = [f"# Feature memory: {feature_id} — {title}", ""]
    lines.append(f"Status: {status}")
    lines.append(f"Repo: {repo_path or '_unset_'}")
    return "\n".join(lines)


def _render_spec(root: Path, feature_id: str) -> str:
    spec = root / "features" / feature_id / "spec.md"
    body = _safe_read_text(spec)
    if body is None:
        return "## spec.md\n\n_no spec.md on disk_"
    return "## spec.md\n\n" + body.rstrip()


def _render_spec_git_log(root: Path, feature_id: str, run: SubprocessRunner) -> str:
    """`git log -10 -- features/F-XXX/spec.md` as the decision timeline."""
    rel = f"features/{feature_id}/spec.md"
    try:
        proc = run(
            ["git", "log", f"-{_SPEC_GIT_LOG_DEPTH}", "--", rel],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return "## Recent spec.md edits (git log -10)\n\n_git not available_"
    if getattr(proc, "returncode", 1) != 0:
        return "## Recent spec.md edits (git log -10)\n\n_no commits / not a git repo_"
    stdout = (getattr(proc, "stdout", "") or "").rstrip()
    if not stdout:
        return "## Recent spec.md edits (git log -10)\n\n_no commits for this spec.md yet_"
    return "## Recent spec.md edits (git log -10)\n\n" + stdout


def _render_units(units: list[WorkUnitState]) -> str:
    if not units:
        return "## Units\n\n_no units spawned yet_"
    lines = ["## Units", ""]
    for u in units:
        summary = state.summarize_unit(u.unit_id)
        cycle_count = u.review_round
        # ``event_counts_by_type`` is a Counter dump; surface the keys
        # the lead cares about in a compact form.
        type_counts = summary.get("event_counts_by_type") or {}
        terminal_counts = _terminal_event_counts(type_counts)
        bits = [f"status={u.status}", f"cycles={cycle_count}"]
        if u.pr_number:
            bits.append(f"pr=#{u.pr_number}")
        if terminal_counts:
            bits.append(terminal_counts)
        if u.last_error:
            bits.append(f"error={_truncate(u.last_error, 80)}")
        lines.append(f"- {u.unit_id} — " + " · ".join(bits))
    return "\n".join(lines)


def _render_cycle_log_finals(root: Path, feature_id: str, units: list[WorkUnitState]) -> str:
    """Per-unit cycle-log 'Final' digest — PR section + last cycle subsection.

    Reads the on-disk markdown rather than re-rendering from state so
    any post-merge SHA backfill (the one allowed post-finalization edit)
    shows through. Units without a cycle log on disk (still in-flight,
    or pre-F-006-U-2) are silently skipped — the units list above
    already calls out their status.
    """
    feature_dir = root / "features" / feature_id
    if not feature_dir.is_dir():
        return "## Cycle log finals\n\n_no cycle logs on disk_"

    blocks: list[str] = []
    for u in units:
        log_path = feature_dir / f"{_unit_basename(u.unit_id)}.md"
        body = _safe_read_text(log_path)
        if body is None:
            continue
        digest = _extract_final_sections(body)
        if digest.strip():
            blocks.append(digest)

    if not blocks:
        return "## Cycle log finals\n\n_no cycle logs on disk_"

    return "## Cycle log finals\n\n" + "\n\n".join(blocks)


def _render_recent_escalations(units: list[WorkUnitState]) -> str:
    """Newest-first list of escalation-class events across the feature.

    Walks every unit's event log, keeps the rows whose ``event_type``
    matches an escalation suffix, and limits to ``_MAX_ESCALATION_EVENTS``.
    Each line is tagged with the unit_id so the lead can jump straight to
    the offender without cross-referencing.
    """
    rows: list[dict[str, Any]] = []
    for u in units:
        for ev in state.list_events(u.unit_id):
            if _is_escalation_event(ev.get("event_type", "")):
                ev = dict(ev)
                ev.setdefault("unit_id", u.unit_id)
                rows.append(ev)

    if not rows:
        return "## Recent escalations\n\n_no escalation events_"

    # Newest first; ts is ISO-8601 UTC so lexicographic sort = chronological.
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    rows = rows[:_MAX_ESCALATION_EVENTS]

    lines = ["## Recent escalations", ""]
    for r in rows:
        summary = (r.get("summary") or "").strip() or _reason_from_details(r.get("details", ""))
        cycle = r.get("cycle_number")
        cycle_bit = f" cycle={cycle}" if cycle is not None else ""
        ts = r.get("ts") or ""
        lines.append(
            f"- {ts} · {r.get('unit_id')} · {r.get('event_type')}{cycle_bit}"
            + (f" — {summary}" if summary else "")
        )
    return "\n".join(lines)


# --------------------------- helpers ---------------------------


def _safe_read_text(path: Path) -> str | None:
    """Read ``path`` as utf-8 text; return None on any I/O error.

    Matches the "best effort, never raise" convention used throughout
    the cycle-log writer — a missing file or a permission error must
    degrade to a placeholder, not blow up the whole blob.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return None


def _extract_final_sections(cycle_log_text: str) -> str:
    """Pull the H1, the ``## PR`` section, and the last ``### Cycle`` subsection.

    Used by ``_render_cycle_log_finals`` to keep each unit's contribution
    to the blob bounded (~500 tokens, per the proposal). Tolerates
    cycle-log files that don't match the full schema — a half-written
    log without a ``## PR`` heading still produces a usable header line.
    """
    lines = cycle_log_text.splitlines()
    out: list[str] = []

    # H1 — the unit identifier line.
    for line in lines:
        if line.startswith("# "):
            out.append(line)
            break

    # ## PR section — verbatim until the next "## " sibling heading.
    pr_block = _extract_section(lines, "## PR")
    if pr_block:
        if out:
            out.append("")
        out.extend(["## PR", *pr_block])

    # Final cycle subsection — the cap-3 summary line plus the last
    # "### Cycle N — heading" entry.
    cycle_block = _extract_section(lines, "## Cycle history")
    if cycle_block:
        # Find the count summary (first non-empty line) and the last
        # "### Cycle " subsection.
        summary_line = next((ln for ln in cycle_block if ln.strip()), "")
        sub_idxs = [i for i, ln in enumerate(cycle_block) if ln.startswith("### Cycle ")]
        if out:
            out.append("")
        out.append("## Cycle history (final)")
        if summary_line:
            out.append(summary_line)
        if sub_idxs:
            out.append("")
            out.extend(cycle_block[sub_idxs[-1] :])

    return "\n".join(out)


def _extract_section(lines: list[str], heading: str) -> list[str]:
    """Return the body of one ``## Heading`` section, exclusive of the heading.

    Stops at the next ``## `` sibling heading or end-of-file. Returns
    an empty list if the heading isn't found.
    """
    body: list[str] = []
    in_section = False
    for line in lines:
        if line.strip() == heading:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            body.append(line)
    # Trim trailing blank lines so callers can join without doubled gaps.
    while body and not body[-1].strip():
        body.pop()
    return body


def _is_escalation_event(event_type: str) -> bool:
    return any(event_type.endswith(suffix) for suffix in _ESCALATION_EVENT_SUFFIXES)


def _terminal_event_counts(type_counts: dict[str, int]) -> str:
    """Compact one-liner of which terminal markers fired for the unit.

    Pulls the counts the lead actually wants to see ("did the tester
    pass? did the reviewer recommend merge?") rather than dumping the
    whole Counter. Empty string when none of the tracked types fired.
    """
    interesting = (
        ("tests_pass", "tests_pass"),
        ("tester_bug_found", "bugs"),
        ("reviewer_recommend_merge", "recommend_merge"),
        ("reviewer_request_changes", "request_changes"),
        ("reviewer_comment", "review_comment"),
        ("fix_pushed", "fixes"),
    )
    bits = [f"{label}={type_counts[key]}" for key, label in interesting if type_counts.get(key)]
    return " ".join(bits)


def _reason_from_details(details: str) -> str:
    """One-line label for an escalation event with a structured details blob.

    Thin wrapper over :func:`blocked_hints.extract_reason_from_details`
    so the two surfaces stay in sync — if the BLOCKED-payload schema ever
    grows a new field, ``blocked_hints`` is the one place we update. We
    drop the prose half of the tuple when there's no recognized reason
    (it just echoes the raw details, which the caller already showed).
    """
    reason, prose = blocked_hints.extract_reason_from_details(details)
    if reason == blocked_hints.UNKNOWN_REASON:
        return ""
    return f"[{reason}] {prose}" if prose else f"[{reason}]"


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


__all__ = ["build_feature_memory"]
