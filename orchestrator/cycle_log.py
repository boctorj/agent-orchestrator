"""Per-unit cycle log writer.

Renders ``features/F-XXX/U-N.md`` from ``unit_events`` rows plus GitHub
data mirrored via ``gh pr view`` (PR description + ``headRefOid``) and a
``reviewThreads`` GraphQL query. Writes atomically (tmp file + rename)
and auto-commits the result locally — never pushes.

This module is a pure library: no MCP tool registers it, no caller in
``orchestrator/tools/`` invokes it yet. F-006-U-N follow-ups will wire
``write_cycle_log`` into ``cycle_review``'s terminal branches and expose
``regenerate_cycle_log`` as an MCP tool.

See ``docs/PROPOSAL-feature-spec-and-headless-daemon.md`` § "Per-unit
cycle log" for the schema and the "Persistence and commit strategy"
section for the local-only commit policy.
"""

from __future__ import annotations

import contextlib
import json
import subprocess  # nosec B404 — invoking `gh` / `git` is the whole point of this module
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator import state
from orchestrator.github import parse_repo_url
from orchestrator.models import WorkUnit

# Subprocess runner shape callers can swap in for tests. Real
# ``subprocess.run`` has an overloaded signature; for our purposes
# anything that accepts argv + kwargs and returns an object with
# ``.returncode`` / ``.stdout`` / ``.stderr`` qualifies.
SubprocessRunner = Callable[..., Any]

# Auto-commit identity for cycle-log writes. Matches the per-command
# pattern used by coder/tester worker commits — distinct from the human
# operator's identity so the project journal is auditable.
COMMIT_USER_NAME = "orchestrator-bot"
COMMIT_USER_EMAIL = "agent@orchestrator"

# GraphQL for review-thread mirroring. Mirrors the snippet documented in
# ``orchestrator/prompts/coder.md`` so coder agents and the cycle-log
# writer query the same shape.
_REVIEW_THREADS_QUERY = """\
query($owner:String!, $repo:String!, $pr:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$pr) {
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first:1) {
            nodes {
              databaseId
              path
              line
              body
              url
              author { login }
            }
          }
        }
      }
    }
  }
}
"""


# --------------------------- paths ---------------------------


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


def feature_dir(feature_id: str, *, base_dir: Path | None = None) -> Path:
    """Return ``<base>/features/<feature_id>/``, creating it if missing.

    Idempotent. Both ``write_cycle_log`` and ``regenerate_cycle_log``
    funnel through here before any file write so the call works whether
    F-006-U-1's ``load_feature`` spec-bootstrap has landed and whether
    the target feature pre-dates the ``features/`` directory entirely.
    """
    root = base_dir if base_dir is not None else Path.cwd()
    path = root / "features" / feature_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def cycle_log_path(
    unit_id: str,
    *,
    feature_id: str | None = None,
    base_dir: Path | None = None,
) -> Path:
    """Return ``<base>/features/<F-XXX>/<U-N>.md``.

    ``feature_id`` is inferred from ``state.work_units`` when omitted; if
    the row is missing (orphan / pre-state case), the prefix of
    ``unit_id`` up to ``-U-`` is used as a fallback.
    """
    if feature_id is None:
        unit_state = state.get_unit_state(unit_id)
        feature_id = unit_state.feature_id if unit_state else _feature_id_from_unit_id(unit_id)
    fdir = feature_dir(feature_id, base_dir=base_dir)
    return fdir / f"{_unit_basename(unit_id)}.md"


# --------------------------- GitHub mirroring ---------------------------


def _run_gh(
    argv: list[str],
    *,
    run: SubprocessRunner,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Invoke ``gh`` and return ``(returncode, stdout, stderr)``.

    Errors are returned to the caller (never raised) — the cycle log is
    written best-effort so a transient GitHub outage doesn't lose the
    state-driven cycle history we have on disk.
    """
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    return (
        getattr(proc, "returncode", 1),
        getattr(proc, "stdout", "") or "",
        getattr(proc, "stderr", "") or "",
    )


def fetch_pr_info(
    repo_url: str,
    pr_number: int,
    *,
    run: SubprocessRunner | None = None,
) -> dict[str, Any]:
    """Mirror the PR description + head SHA via ``gh pr view``.

    Returns a dict with ``title``, ``body``, ``headRefOid`` — or an empty
    dict if ``gh`` fails / returns non-JSON. Pass ``run`` from tests to
    avoid invoking the real CLI.
    """
    runner = run if run is not None else subprocess.run
    owner, repo = parse_repo_url(repo_url)
    code, out, _ = _run_gh(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "title,body,headRefOid",
        ],
        run=runner,
    )
    if code != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def fetch_review_threads(
    repo_url: str,
    pr_number: int,
    *,
    run: SubprocessRunner | None = None,
) -> list[dict[str, Any]]:
    """Mirror review threads via the GraphQL ``reviewThreads`` query.

    Returns a list of ``{id, isResolved, isOutdated, path, line, body,
    url, author}`` dicts (the first comment of each thread, which carries
    the original finding). Empty list on transport / parse error.
    """
    runner = run if run is not None else subprocess.run
    owner, repo = parse_repo_url(repo_url)
    code, out, _ = _run_gh(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_REVIEW_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"pr={pr_number}",
        ],
        run=runner,
    )
    if code != 0 or not out.strip():
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return []

    nodes = (
        payload.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    threads: list[dict[str, Any]] = []
    for node in nodes:
        comments = (node.get("comments") or {}).get("nodes") or []
        first = comments[0] if comments else {}
        threads.append(
            {
                "id": node.get("id"),
                "isResolved": bool(node.get("isResolved")),
                "isOutdated": bool(node.get("isOutdated")),
                "path": first.get("path"),
                "line": first.get("line"),
                "body": first.get("body", ""),
                "url": first.get("url", ""),
                "author": (first.get("author") or {}).get("login", ""),
            }
        )
    return threads


# --------------------------- rendering ---------------------------


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
}


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
    lines.append(f"Status: {unit_status}")
    head_sha = pr_info.get("headRefOid") or ""
    lines.append(f"PR head SHA: {head_sha or '_unknown_'}")
    return lines


def _render_pr_description(pr_info: dict[str, Any]) -> list[str]:
    body = (pr_info.get("body") or "").rstrip()
    lines = ["## Coder's PR description (verbatim, as of last capture)"]
    lines.append(body if body else "_unavailable_")
    return lines


def _render_cycle_history(events: list[dict[str, Any]]) -> list[str]:
    rendered: list[dict[str, Any]] = []
    for ev in events:
        heading = _EVENT_HEADINGS.get(ev["event_type"])
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
    cap_hit = cycle_count >= 3
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


_REVIEW_TIER_PREFIX = ("🔴", "🟠", "🟡", "🔵")


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
) -> str:
    """Render the cycle-log markdown for ``unit_id`` from current state.

    ``pr_info`` and ``review_threads`` come from ``fetch_pr_info`` /
    ``fetch_review_threads`` in normal operation; pass them in directly
    for tests or for an offline regenerate.
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
        ),
        _render_pr_description(pr_info),
        _render_cycle_history(state.list_events(unit_id) if unit_state else []),
        _render_review_threads(review_threads),
    ]
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


# --------------------------- writing + committing ---------------------------


def _git_commit_local(
    target: Path,
    *,
    cwd: Path,
    run: SubprocessRunner,
    message: str,
) -> bool:
    """Stage + commit ``target`` under the orchestrator-bot identity.

    Local only — never pushes. Returns True if a commit was created,
    False if there was nothing to commit (idempotent re-render) or the
    workdir isn't a git repo. Failures are swallowed: a missing ``git``
    or a non-repo workdir must not block the file write.
    """
    try:
        add = run(
            ["git", "add", "--", str(target)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    if getattr(add, "returncode", 1) != 0:
        return False

    # `git diff --cached --quiet -- <file>` exits 0 when nothing to
    # commit, 1 when there are staged changes. Avoid a no-op commit
    # (regenerate on unchanged input is allowed and must not pollute
    # history).
    diff = run(
        ["git", "diff", "--cached", "--quiet", "--", str(target)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(diff, "returncode", 0) == 0:
        return False

    commit = run(
        [
            "git",
            "-c",
            f"user.email={COMMIT_USER_EMAIL}",
            "-c",
            f"user.name={COMMIT_USER_NAME}",
            "commit",
            "--only",
            "--",
            str(target),
            "-m",
            message,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return getattr(commit, "returncode", 1) == 0


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` via tmp file + rename.

    A crash mid-write leaves either the prior finalized file intact
    (rename never ran) or no file at all (first-time write). The tmp
    name lives in the same directory as the target so the rename is
    same-filesystem atomic via ``os.replace``.
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
    except Exception:
        # Best-effort cleanup; we don't want a dangling .tmp on failure.
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise


def write_cycle_log(
    unit_id: str,
    *,
    base_dir: Path | None = None,
    run: SubprocessRunner | None = None,
    pr_info: dict[str, Any] | None = None,
    review_threads: list[dict[str, Any]] | None = None,
    commit_message: str | None = None,
) -> Path:
    """Render + persist ``features/F-XXX/U-N.md`` and auto-commit locally.

    Steps:
      1. ``mkdir -p features/F-XXX`` (works regardless of whether U-1's
         ``load_feature`` change has landed and regardless of whether
         the feature pre-dates the ``features/`` directory).
      2. Mirror the PR description + head SHA via ``gh pr view`` and the
         review threads via the GraphQL ``reviewThreads`` query, unless
         the caller supplied them.
      3. Render markdown from ``state.list_events`` + the mirrored data.
      4. Write atomically (``.md.tmp`` then rename).
      5. ``git add`` + commit under ``orchestrator-bot`` identity. Never
         pushes — push policy is operator-driven (see proposal §
         "Persistence and commit strategy").

    Returns the absolute path to the written file.
    """
    runner = run if run is not None else subprocess.run

    unit_state = state.get_unit_state(unit_id)
    if unit_state is None:
        raise ValueError(f"no work_units row for {unit_id}")
    feature_id = unit_state.feature_id
    feature = state.get_feature(feature_id)

    # 1. mkdir features/F-XXX  (idempotent; precondition for atomic write)
    target = cycle_log_path(unit_id, feature_id=feature_id, base_dir=base_dir)

    # 2. mirror from GitHub when we have a PR + repo and the caller
    #    didn't pre-supply the data (test path).
    if pr_info is None and feature and feature.repo_path and unit_state.pr_number:
        pr_info = fetch_pr_info(feature.repo_path, unit_state.pr_number, run=runner)
    if review_threads is None and feature and feature.repo_path and unit_state.pr_number:
        review_threads = fetch_review_threads(feature.repo_path, unit_state.pr_number, run=runner)

    # 3. render
    markdown = render_cycle_log(unit_id, pr_info=pr_info, review_threads=review_threads)

    # 4. atomic write
    _atomic_write(target, markdown)

    # 5. commit local only
    cwd = base_dir if base_dir is not None else Path.cwd()
    message = commit_message or f"cycle-log: {unit_id}"
    _git_commit_local(target, cwd=cwd, run=runner, message=message)

    return target


def regenerate_cycle_log(
    unit_id: str,
    *,
    base_dir: Path | None = None,
    run: SubprocessRunner | None = None,
) -> Path:
    """Re-render the cycle log from scratch. Idempotent.

    Two recovery scenarios this is built for:

    * **Orphan recovery** — ``state.work_units`` shows a terminal
      state but no ``features/F-XXX/U-N.md`` exists (the writer
      crashed between the state update and the file write). Re-render
      from ``state.list_events`` + GitHub.
    * **PR-description repair** — the human edited the PR description
      on GitHub after the cycle log was finalized. Re-mirror via
      ``gh pr view`` so the on-disk copy matches reality again.

    Same mkdir-before-write guarantee as :func:`write_cycle_log`, so the
    function works for features that pre-date the ``features/``
    directory (i.e. before F-006-U-1's ``load_feature`` change has
    landed).
    """
    return write_cycle_log(
        unit_id,
        base_dir=base_dir,
        run=run,
        commit_message=f"cycle-log: regenerate {unit_id}",
    )


__all__ = [
    "COMMIT_USER_EMAIL",
    "COMMIT_USER_NAME",
    "cycle_log_path",
    "feature_dir",
    "fetch_pr_info",
    "fetch_review_threads",
    "regenerate_cycle_log",
    "render_cycle_log",
    "write_cycle_log",
]
