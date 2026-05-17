"""GitHub mirroring helpers for the cycle-log writer.

Split out from ``orchestrator/cycle_log.py`` so that the rendering and
write paths can be exercised without touching ``subprocess`` or stubbing
``gh`` — see review on PR #26 (\"this file is way too long and hard to
test\"). Public surface (``fetch_pr_info`` / ``fetch_review_threads``) is
re-exported from ``orchestrator.cycle_log`` for back-compat with existing
callers and tests.

Best-effort by design: every helper returns an empty value on transport,
parse, or repo-URL error rather than raising. The cycle log is a
post-hoc summary — a transient GitHub outage or a malformed
``feature.repo_path`` must never block the state-derived history we have
on disk from being written.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 — invoking `gh` is the whole point of this module
from collections.abc import Callable
from typing import Any

from orchestrator.github import parse_repo_url

# Subprocess runner shape callers can swap in for tests. Real
# ``subprocess.run`` has an overloaded signature; for our purposes
# anything that accepts argv + kwargs and returns an object with
# ``.returncode`` / ``.stdout`` / ``.stderr`` qualifies.
SubprocessRunner = Callable[..., Any]

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


def _run_gh(
    argv: list[str],
    *,
    run: SubprocessRunner,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Invoke ``gh`` and return ``(returncode, stdout, stderr)``.

    Why this exists (was: ``_run_gh`` L127 review comment — see PR #26):
    callers need three things that ``subprocess.run`` doesn't give them
    directly: (1) silent handling of ``FileNotFoundError`` when ``gh``
    isn't installed (returned as rc=127 to match shell convention so the
    callers' ``code != 0`` branch fires), (2) safe attribute fallback
    when a stubbed runner returns a non-``CompletedProcess`` shape, and
    (3) a uniform tuple so both call sites (``fetch_pr_info`` /
    ``fetch_review_threads``) stay branch-free. Each call site reading
    this directly would duplicate ~8 lines of boilerplate — keeping the
    helper avoids that.
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
    dict on any error (malformed ``repo_url``, ``gh`` transport failure,
    non-JSON output, JSON in an unexpected shape). Pass ``run`` from
    tests to avoid invoking the real CLI.
    """
    runner = run if run is not None else subprocess.run
    try:
        owner, repo = parse_repo_url(repo_url)
    except ValueError:
        # Malformed ``feature.repo_path`` must not block the cycle-log
        # write. Caller still gets a state-driven render with
        # ``_unavailable_`` placeholders for the PR section.
        return {}
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
    the original finding). Empty list on transport / parse / repo-URL /
    shape error.
    """
    runner = run if run is not None else subprocess.run
    try:
        owner, repo = parse_repo_url(repo_url)
    except ValueError:
        return []
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
    # Shape-defensive walk: ``gh api graphql`` is *supposed* to return
    # ``{"data": {"repository": {"pullRequest": {"reviewThreads":
    # {"nodes": [...]}}}}}`` but a non-dict root or a ``nodes: null``
    # would crash the chained ``.get()`` calls. Coerce at every step.
    if not isinstance(payload, dict):
        return []
    cursor: Any = payload
    for key in ("data", "repository", "pullRequest", "reviewThreads"):
        cursor = cursor.get(key) if isinstance(cursor, dict) else None
        if cursor is None:
            return []
    nodes = cursor.get("nodes") if isinstance(cursor, dict) else None
    if not isinstance(nodes, list):
        return []

    threads: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        comments_node = node.get("comments") if isinstance(node.get("comments"), dict) else {}
        comments = comments_node.get("nodes") if isinstance(comments_node, dict) else []
        first = comments[0] if isinstance(comments, list) and comments else {}
        if not isinstance(first, dict):
            first = {}
        author = first.get("author") if isinstance(first.get("author"), dict) else {}
        threads.append(
            {
                "id": node.get("id"),
                "isResolved": bool(node.get("isResolved")),
                "isOutdated": bool(node.get("isOutdated")),
                "path": first.get("path"),
                "line": first.get("line"),
                "body": first.get("body", ""),
                "url": first.get("url", ""),
                "author": author.get("login", "") if isinstance(author, dict) else "",
            }
        )
    return threads


__all__ = [
    "SubprocessRunner",
    "fetch_pr_info",
    "fetch_review_threads",
]
