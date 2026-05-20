"""Per-unit cycle log writer — public API.

Renders ``features/F-XXX/U-N.md`` from ``unit_events`` rows plus GitHub
data mirrored via ``gh pr view`` (PR description + ``headRefOid``) and a
``reviewThreads`` GraphQL query. Writes atomically (tmp file + rename)
and auto-commits the result locally — never pushes.

This module is a pure library: no MCP tool registers it, no caller in
``orchestrator/tools/`` invokes it yet. F-006-U-N follow-ups will wire
``write_cycle_log`` into ``cycle_review``'s terminal branches and expose
``regenerate_cycle_log`` as an MCP tool.

The gh-mirroring (`cycle_log_gh.py`) and markdown rendering
(`cycle_log_render.py`) live in sibling modules so the I/O and the
formatting can be exercised independently. This module owns paths,
atomic write, local commit, and the orchestrating ``write_cycle_log`` /
``regenerate_cycle_log`` entry points. Public names from the siblings
are re-exported here so callers and tests can keep using
``from orchestrator import cycle_log`` followed by ``cycle_log.X``.

See ``docs/PROPOSAL-feature-spec-and-headless-daemon.md`` § "Per-unit
cycle log" for the schema and the "Persistence and commit strategy"
section for the local-only commit policy.
"""

from __future__ import annotations

import contextlib
import re
import subprocess  # nosec B404 — invoking `git` is the whole point of the commit step
from pathlib import Path
from typing import Any

from orchestrator import state
from orchestrator.cycle_log_gh import (
    SubprocessRunner,
    fetch_pr_info,
    fetch_review_threads,
)
from orchestrator.cycle_log_render import (
    _feature_id_from_unit_id,
    _unit_basename,
    render_cycle_log,
)

# Auto-commit identity for cycle-log writes. Matches the per-command
# pattern used by coder/tester worker commits — distinct from the human
# operator's identity so the project journal is auditable.
COMMIT_USER_NAME = "orchestrator-bot"
COMMIT_USER_EMAIL = "agent@orchestrator"

# Reverse of the renderer's ``Merge commit SHA: <sha>`` line. Used by
# ``regenerate_cycle_log`` to preserve a previously-backfilled SHA when
# re-rendering offline (so the recovery tool doesn't silently strip the
# only post-finalization edit allowed by the proposal).
_MERGE_SHA_RE = re.compile(r"^Merge commit SHA:\s*(\S+)\s*$", re.MULTILINE)


# --------------------------- paths ---------------------------


def cycle_log_base_dir() -> Path:
    """Return the on-disk anchor for ``features/`` cycle-log storage.

    Anchored to ``state.STATE_DB.parent`` rather than ``Path.cwd()`` so
    tests (which monkeypatch ``state.STATE_DB`` to a tmp file) get an
    isolated tmp tree, and so runtime callers don't depend on whatever
    the caller's CWD happens to be. Matches the
    ``orchestrator.feature_spec.features_root`` anchor — a future
    re-anchoring of cycle-log storage lands here in one place.
    """
    return Path(state.STATE_DB).parent


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

    # ``git diff --cached --quiet -- <file>`` exit codes:
    #   0   nothing staged (idempotent regenerate — return False)
    #   1   staged changes present (proceed to commit)
    #   128 git error (non-repo, corrupt index, ...) — treat as no-op.
    # Treat *only* rc==1 as "has changes"; anything else (including the
    # historical "treat non-zero as has-changes" bug this comment
    # replaces) is a fail-safe no-op.
    diff = run(
        ["git", "diff", "--cached", "--quiet", "--", str(target)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(diff, "returncode", -1) != 1:
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
    merge_commit_sha: str | None = None,
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

    ``merge_commit_sha`` is the post-merge backfill knob: ``reconcile_unit_pr``
    re-invokes this writer once it confirms the PR has merged, supplying
    ``mergeCommit.oid`` so the finalized log records the commit on main.
    This is the only post-finalization edit allowed (proposal § "Per-unit
    cycle log" rule on immutability).

    Returns the resolved absolute path to the written file.
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
    markdown = render_cycle_log(
        unit_id,
        pr_info=pr_info,
        review_threads=review_threads,
        merge_commit_sha=merge_commit_sha,
    )

    # 4. atomic write
    _atomic_write(target, markdown)

    # 5. commit local only
    cwd = base_dir if base_dir is not None else Path.cwd()
    message = commit_message or f"cycle-log: {unit_id}"
    _git_commit_local(target, cwd=cwd, run=runner, message=message)

    return target.resolve()


def _extract_merge_sha(markdown: str) -> str | None:
    """Return the ``Merge commit SHA`` value from an existing cycle log.

    The renderer emits ``Merge commit SHA: <sha>`` only when ``reconcile_unit_pr``
    has backfilled the post-merge SHA. ``regenerate_cycle_log`` calls
    this to preserve that field across an offline re-render — otherwise
    re-running the recovery tool on a merged unit silently strips the
    one and only post-finalization edit the proposal permits.
    """
    m = _MERGE_SHA_RE.search(markdown)
    if not m:
        return None
    return m.group(1)


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

    Preserves any previously-backfilled ``Merge commit SHA`` line in
    the existing on-disk file — the proposal makes the merge-SHA edit
    the only post-finalization mutation; the recovery tool must not
    silently undo it. Read the SHA from disk rather than re-fetching
    from ``gh`` so the recovery path stays usable offline.
    """
    target = cycle_log_path(unit_id, base_dir=base_dir)
    preserved_sha: str | None = None
    if target.exists():
        with contextlib.suppress(OSError):
            preserved_sha = _extract_merge_sha(target.read_text(encoding="utf-8"))

    return write_cycle_log(
        unit_id,
        base_dir=base_dir,
        run=run,
        merge_commit_sha=preserved_sha,
        commit_message=f"cycle-log: regenerate {unit_id}",
    )


__all__ = [
    "COMMIT_USER_EMAIL",
    "COMMIT_USER_NAME",
    "cycle_log_base_dir",
    "cycle_log_path",
    "feature_dir",
    "fetch_pr_info",
    "fetch_review_threads",
    "regenerate_cycle_log",
    "render_cycle_log",
    "write_cycle_log",
]
