"""Spec-compliance tests for F-006-U-2 (cycle-log writer + regenerate).

Independent tester-written tests complementing ``tests/test_cycle_log.py``
by locking in the EXACT semantics of the F-006-U-2 unit description and
the proposal's "Per-unit cycle log" / "Persistence and commit strategy"
sections. Verbatim guarantees these tests pin down:

  1. ``mkdir(parents=True, exist_ok=True)`` is called on the
     ``features/F-XXX/`` directory **before any file write**, both from
     ``write_cycle_log`` and from ``regenerate_cycle_log``. Must work
     when the ``features/`` root does not exist (feature pre-dates U-1)
     and when the target dir already exists (idempotent).
  2. PR description + ``headRefOid`` are mirrored via
     ``gh pr view --json title,body,headRefOid`` (exactly those three
     fields, by the spec).
  3. Review threads are mirrored via the GraphQL ``reviewThreads`` query.
  4. Writes are atomic: temp file in the same directory + rename. A
     crash mid-rename leaves the prior finalized file intact, with no
     dangling ``.tmp``.
  5. Auto-commit identity is ``user.email=agent@orchestrator`` +
     ``user.name=orchestrator-bot`` (verbatim from the unit description
     and the proposal § "Persistence and commit strategy").
  6. Auto-commit is **local only — never pushes**.
  7. ``regenerate_cycle_log`` covers both recovery scenarios:
     (a) orphan recovery — state says terminal, no file on disk; and
     (b) PR-description repair — file exists but is stale.
  8. Pure library: no module under ``orchestrator/`` (outside
     ``cycle_log.py`` itself) imports it yet. This unit must not change
     runtime behavior.

All ``gh``/``git`` invocations are stubbed via an injectable
``subprocess.run``-shaped callable; no real GitHub, git, or shell is
touched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import cycle_log, state
from orchestrator.models import Feature, WorkUnit, WorkUnitState

# --------------------------- fakes ---------------------------


@dataclass
class _Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _Runner:
    """Records argv per invocation; serves canned responses by argv prefix.

    Behavior matches the test-runner pattern already in use in
    ``tests/test_cycle_log.py``: the first registered prefix that
    matches wins; an unregistered call returns a benign zero-exit empty
    response so the code under test never accidentally hits the real
    shell.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: list[tuple[tuple[str, ...], _Proc]] = []

    def register(self, prefix: tuple[str, ...], proc: _Proc) -> None:
        self._responses.append((prefix, proc))

    def __call__(self, argv: list[str], **kwargs: Any) -> _Proc:
        self.calls.append(list(argv))
        for prefix, proc in self._responses:
            if tuple(argv[: len(prefix)]) == prefix:
                return proc
        return _Proc()


def _seed_unit(
    *,
    unit_id: str = "F-042-U-1",
    feature_id: str = "F-042",
    repo_path: str = "https://github.com/o/r",
    pr_number: int | None = 7,
    status: str = "in_ci",
    title: str = "Demo unit",
    events: list[dict[str, Any]] | None = None,
) -> None:
    state.save_feature(Feature(id=feature_id, title="Demo", description="d", repo_path=repo_path))
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title=title, description="")],
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            pr_number=pr_number,
        )
    )
    for ev in events or []:
        state.record_event(
            unit_id,
            feature_id,
            ev["event_type"],
            source=ev.get("source", "orchestrator"),
            cycle_number=ev.get("cycle_number", 0),
            summary=ev.get("summary", ""),
        )


# =============================================================================
# 1. mkdir-before-write guarantee — both writers
# =============================================================================


class TestMkdirBeforeAnyWrite:
    """The unit description's CRITICAL invariant.

    > both the writer and regenerate_cycle_log MUST call
    > Path(features/F-XXX).mkdir(parents=True, exist_ok=True) before any
    > write so it works whether U-1 has landed yet AND for features that
    > pre-date the spec.md infrastructure.
    """

    def test_write_creates_features_root_when_absent(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_unit(pr_number=None)
        assert not (tmp_path / "features").exists()

        target = cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())

        assert (tmp_path / "features").is_dir()
        assert (tmp_path / "features" / "F-042").is_dir()
        assert target.is_file()

    def test_regenerate_creates_features_root_when_absent(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_unit(pr_number=None)
        assert not (tmp_path / "features").exists()

        target = cycle_log.regenerate_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())

        assert (tmp_path / "features" / "F-042").is_dir()
        assert target.is_file()

    def test_write_is_idempotent_when_target_dir_already_exists(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_unit(pr_number=None)
        (tmp_path / "features" / "F-042").mkdir(parents=True)

        # Must not raise FileExistsError.
        cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())
        cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())

        assert (tmp_path / "features" / "F-042" / "U-1.md").is_file()

    def test_feature_dir_uses_parents_true_exist_ok_true_pattern(self, tmp_path: Path) -> None:
        """Locks in the exact pattern from the unit description.

        Calling ``feature_dir`` twice on a nested path must succeed
        (parents=True semantics) and must not raise on the second call
        (exist_ok=True semantics).
        """
        nested = tmp_path / "deeper" / "still_nested"
        d1 = cycle_log.feature_dir("F-042", base_dir=nested)
        d2 = cycle_log.feature_dir("F-042", base_dir=nested)
        assert d1 == d2 == nested / "features" / "F-042"
        assert d1.is_dir()


# =============================================================================
# 2. gh pr view --json title,body,headRefOid  (exact command shape)
# =============================================================================


class TestPrViewMirroring:
    """Spec: mirror PR via ``gh pr view --json title,body,headRefOid``."""

    def test_argv_is_gh_pr_view_with_correct_json_fields(self) -> None:
        runner = _Runner()
        runner.register(
            ("gh", "pr", "view"),
            _Proc(stdout=json.dumps({"title": "t", "body": "b", "headRefOid": "abc"})),
        )

        cycle_log.fetch_pr_info("https://github.com/o/r", 7, run=runner)

        gh_calls = [c for c in runner.calls if c[:3] == ["gh", "pr", "view"]]
        assert gh_calls, "expected a `gh pr view` invocation"
        argv = gh_calls[0]
        # The exact three fields the spec requires.
        assert "--json" in argv
        json_fields = argv[argv.index("--json") + 1]
        fields = set(json_fields.split(","))
        assert fields == {"title", "body", "headRefOid"}, (
            f"spec requires exactly title,body,headRefOid; got {json_fields!r}"
        )
        # PR number passes through.
        assert "7" in argv

    def test_uses_owner_repo_from_url(self) -> None:
        runner = _Runner()
        runner.register(("gh", "pr", "view"), _Proc(stdout="{}"))
        cycle_log.fetch_pr_info("https://github.com/owner/repo", 99, run=runner)

        argv = next(c for c in runner.calls if c[:3] == ["gh", "pr", "view"])
        assert "owner/repo" in argv

    def test_returns_dict_with_title_body_headrefoid_keys(self) -> None:
        runner = _Runner()
        runner.register(
            ("gh", "pr", "view"),
            _Proc(stdout=json.dumps({"title": "T", "body": "B", "headRefOid": "deadbeef"})),
        )
        info = cycle_log.fetch_pr_info("https://github.com/o/r", 1, run=runner)
        assert info["title"] == "T"
        assert info["body"] == "B"
        assert info["headRefOid"] == "deadbeef"


# =============================================================================
# 3. GraphQL reviewThreads  (correct query name + variables)
# =============================================================================


class TestReviewThreadsMirroring:
    def test_invokes_gh_api_graphql_with_reviewthreads_query(self) -> None:
        runner = _Runner()
        runner.register(("gh", "api", "graphql"), _Proc(stdout="{}"))
        cycle_log.fetch_review_threads("https://github.com/owner/repo", 7, run=runner)

        graphql_calls = [c for c in runner.calls if c[:3] == ["gh", "api", "graphql"]]
        assert graphql_calls, "expected a `gh api graphql` invocation"
        joined = " ".join(graphql_calls[0])
        assert "reviewThreads" in joined, "GraphQL query must hit the reviewThreads connection"
        # The query needs all three variables to address a single PR.
        assert "owner=owner" in graphql_calls[0]
        assert "repo=repo" in graphql_calls[0]
        assert "pr=7" in graphql_calls[0]

    def test_extracts_tier_marker_and_url_per_thread(self) -> None:
        runner = _Runner()
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "PRT_X",
                                    "isResolved": False,
                                    "isOutdated": True,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "databaseId": 1,
                                                "path": "a.py",
                                                "line": 9,
                                                "body": "🔴 critical",
                                                "url": "https://github.com/o/r/pull/7#r1",
                                                "author": {"login": "rev"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }
        runner.register(("gh", "api", "graphql"), _Proc(stdout=json.dumps(payload)))

        threads = cycle_log.fetch_review_threads("https://github.com/o/r", 7, run=runner)

        assert len(threads) == 1
        t = threads[0]
        assert t["path"] == "a.py"
        assert t["line"] == 9
        assert t["isOutdated"] is True
        assert t["isResolved"] is False
        # The deep link is preserved so cycle logs can jump to the comment.
        assert t["url"].endswith("#r1")


# =============================================================================
# 4. Atomic write: tmp → rename, no dangling tmp on success, prior preserved on failure
# =============================================================================


class TestAtomicWrite:
    def test_uses_tmp_suffix_and_rename(
        self, tmp_path: Path, tmp_state_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The temp file lives in the same directory as the target so
        rename is same-filesystem atomic. Capture the source path of the
        rename to prove a `.tmp` cousin was created."""
        _seed_unit(pr_number=None)

        captured: dict[str, Path] = {}
        original_replace = Path.replace

        def spy_replace(self: Path, target: Path) -> Path:  # type: ignore[override]
            captured["src"] = Path(self)
            captured["dst"] = Path(target)
            return original_replace(self, target)

        monkeypatch.setattr(Path, "replace", spy_replace)

        cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())

        assert "src" in captured, "expected an atomic Path.replace() call during the write"
        src = captured["src"]
        dst = captured["dst"]
        # Same parent directory (atomic-on-same-fs guarantee).
        assert src.parent == dst.parent
        # Tmp suffix on the source side.
        assert src.name.endswith(".tmp"), f"expected tmp suffix, got {src.name}"
        # Final lands at U-1.md.
        assert dst.name == "U-1.md"

    def test_no_tmp_file_remains_on_success(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed_unit(pr_number=None)
        cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())
        assert list((tmp_path / "features" / "F-042").glob("*.tmp")) == []

    def test_prior_file_survives_failed_write(
        self, tmp_path: Path, tmp_state_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial-write-safety guarantee from the proposal.

        > A crash mid-write leaves the prior finalized version intact
        > (or no file, for first-time writes).
        """
        _seed_unit(pr_number=None)
        target = tmp_path / "features" / "F-042" / "U-1.md"
        target.parent.mkdir(parents=True)
        target.write_text("PRIOR FINALIZED", encoding="utf-8")

        def boom(self: Path, target_path: Path) -> Path:  # type: ignore[override]
            raise OSError("disk full mid-rename")

        monkeypatch.setattr(Path, "replace", boom)

        with pytest.raises(OSError, match="disk full"):
            cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())

        assert target.read_text(encoding="utf-8") == "PRIOR FINALIZED"
        assert list(target.parent.glob("*.tmp")) == []


# =============================================================================
# 5 + 6. Commit identity is fixed, local-only (never pushes)
# =============================================================================


class TestAutoCommitPolicy:
    """The unit description and proposal pin both the identity and the
    local-only push policy verbatim.
    """

    def test_commit_user_email_and_name_constants_match_spec(self) -> None:
        # The proposal § "Persistence and commit strategy" says:
        #   user.email=agent@orchestrator user.name=orchestrator-bot
        assert cycle_log.COMMIT_USER_EMAIL == "agent@orchestrator"
        assert cycle_log.COMMIT_USER_NAME == "orchestrator-bot"

    def test_git_commit_uses_dash_c_identity_flags(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_unit(pr_number=None)
        runner = _Runner()
        # `git diff --cached --quiet` returns 1 when there ARE staged
        # changes — that's the path that proceeds to commit.
        runner.register(("git", "diff", "--cached"), _Proc(returncode=1))

        cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=runner)

        commit_argv = next(
            (c for c in runner.calls if c[:1] == ["git"] and "commit" in c),
            None,
        )
        assert commit_argv is not None, "expected a `git commit` invocation"
        # The `-c key=value` overrides must travel BEFORE the `commit`
        # subcommand (that's how `git -c` works). Verify both keys are
        # present and the identity values are the ones from the spec.
        flat = " ".join(commit_argv)
        assert "user.email=agent@orchestrator" in flat
        assert "user.name=orchestrator-bot" in flat

    def test_no_push_call_is_ever_emitted(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """Hard rule: ``auto-commit is local only``. `git push` is a bug."""
        _seed_unit(pr_number=None)
        runner = _Runner()
        runner.register(("git", "diff", "--cached"), _Proc(returncode=1))

        cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=runner)
        cycle_log.regenerate_cycle_log("F-042-U-1", base_dir=tmp_path, run=runner)

        for argv in runner.calls:
            assert "push" not in argv, f"local-only push policy violated: {argv}"

    def test_no_op_commit_is_skipped(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """Re-rendering on unchanged input must not pollute history."""
        _seed_unit(pr_number=None)
        runner = _Runner()
        # `git diff --cached --quiet` returns 0 → nothing to commit.
        runner.register(("git", "diff", "--cached"), _Proc(returncode=0))

        cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=runner)

        commit_attempts = [c for c in runner.calls if c[:1] == ["git"] and "commit" in c]
        assert commit_attempts == [], (
            f"no-op re-render must skip the commit; got attempts: {commit_attempts!r}"
        )


# =============================================================================
# 7. Regenerate covers BOTH recovery scenarios called out in the description
# =============================================================================


class TestRegenerateRecoveryScenarios:
    def test_orphan_recovery_creates_log_when_nothing_on_disk(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Risk #4: state.db says terminal, no file on disk."""
        _seed_unit(
            pr_number=None,
            events=[
                {"event_type": "pr_opened", "cycle_number": 0, "summary": "PR open"},
                {
                    "event_type": "reviewer_recommend_merge",
                    "cycle_number": 1,
                    "summary": "endorsed",
                },
            ],
        )
        assert not (tmp_path / "features").exists()

        target = cycle_log.regenerate_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())

        body = target.read_text(encoding="utf-8")
        assert "F-042-U-1" in body
        assert "REVIEW_RECOMMEND_MERGE" in body

    def test_pr_description_repair_overwrites_stale_body(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Risk #5: user edited the PR description on GitHub after the
        log was finalized. Regenerate must re-mirror via `gh pr view`
        and replace the on-disk body.
        """
        _seed_unit()
        target = tmp_path / "features" / "F-042" / "U-1.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            "# F-042-U-1\n\n## Coder's PR description\n\nSTALE BODY FROM YESTERDAY\n",
            encoding="utf-8",
        )

        runner = _Runner()
        runner.register(
            ("gh", "pr", "view"),
            _Proc(
                stdout=json.dumps(
                    {
                        "title": "T",
                        "body": "FRESH BODY USER JUST EDITED",
                        "headRefOid": "newsha",
                    }
                )
            ),
        )
        runner.register(("gh", "api", "graphql"), _Proc(stdout="{}"))

        cycle_log.regenerate_cycle_log("F-042-U-1", base_dir=tmp_path, run=runner)

        body = target.read_text(encoding="utf-8")
        assert "STALE BODY" not in body
        assert "FRESH BODY USER JUST EDITED" in body
        assert "PR head SHA: newsha" in body

    def test_regenerate_uses_distinct_commit_message(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Regenerate's commit message must distinguish it from a normal
        per-cycle write so the project journal is auditable.
        """
        _seed_unit(pr_number=None)
        runner = _Runner()
        runner.register(("git", "diff", "--cached"), _Proc(returncode=1))

        cycle_log.regenerate_cycle_log("F-042-U-1", base_dir=tmp_path, run=runner)

        commit_argv = next(c for c in runner.calls if "commit" in c)
        msg = commit_argv[commit_argv.index("-m") + 1]
        assert "regenerate" in msg.lower()
        assert "F-042-U-1" in msg

    def test_regenerate_idempotent_across_back_to_back_calls(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Running regenerate twice in a row on unchanged state must not
        keep appending git commits (the second call should hit the
        no-op-commit gate).
        """
        _seed_unit(pr_number=None)
        # First call: simulate "there are staged changes" so we commit.
        # Second call: simulate "nothing changed" so we skip.
        call_count: dict[str, int] = {"n": 0}

        def diff_response(argv: list[str], **_: Any) -> _Proc:
            call_count["n"] += 1
            return _Proc(returncode=1 if call_count["n"] == 1 else 0)

        # Custom runner that varies the diff exit code over time.
        class _VaryingRunner(_Runner):
            def __call__(self, argv: list[str], **kwargs: Any) -> _Proc:
                self.calls.append(list(argv))
                if argv[:3] == ["git", "diff", "--cached"]:
                    return diff_response(argv)
                return _Proc()

        vr = _VaryingRunner()
        cycle_log.regenerate_cycle_log("F-042-U-1", base_dir=tmp_path, run=vr)
        cycle_log.regenerate_cycle_log("F-042-U-1", base_dir=tmp_path, run=vr)

        commits = [c for c in vr.calls if c[:1] == ["git"] and "commit" in c]
        assert len(commits) == 1, (
            f"second regenerate on unchanged state must skip commit; got {len(commits)}"
        )


# =============================================================================
# 8. Rendering pulls from state.list_events (state-driven, not GitHub-driven)
# =============================================================================


class TestStateDrivenRendering:
    """The unit description says: "render cycle log markdown from state
    events". GitHub mirroring is for description+head SHA+threads; the
    cycle history itself must come from ``state.list_events``.
    """

    def test_cycle_history_reflects_unit_events_rows(self, tmp_state_db: Path) -> None:
        _seed_unit(
            events=[
                {
                    "event_type": "tester_bug_found",
                    "cycle_number": 1,
                    "summary": "race in oauth handler",
                },
                {
                    "event_type": "fix_pushed",
                    "cycle_number": 1,
                    "summary": "added mutex around token cache",
                },
                {
                    "event_type": "reviewer_request_changes",
                    "cycle_number": 2,
                    "summary": "needs Fernet wrap",
                },
            ]
        )
        md = cycle_log.render_cycle_log("F-042-U-1", pr_info={}, review_threads=[])

        assert "race in oauth handler" in md
        assert "added mutex around token cache" in md
        assert "needs Fernet wrap" in md
        # Cycle headings reflect the cycle_number column.
        assert re.search(r"### Cycle 1 — tester: BUG_FOUND", md)
        assert re.search(r"### Cycle 2 — reviewer: REVIEW_REQUEST_CHANGES", md)

    def test_no_cycle_events_renders_empty_marker(self, tmp_state_db: Path) -> None:
        _seed_unit(events=[])
        md = cycle_log.render_cycle_log("F-042-U-1", pr_info={}, review_threads=[])
        # The "## Cycle history" section must still exist (schema
        # consistency), with an explicit "no events" marker.
        assert "## Cycle history" in md
        assert "no cycle events" in md.lower()

    def test_header_includes_unit_id_and_title(self, tmp_state_db: Path) -> None:
        _seed_unit(title="My nice unit")
        md = cycle_log.render_cycle_log("F-042-U-1", pr_info={}, review_threads=[])
        first_line = md.splitlines()[0]
        assert first_line.startswith("# F-042-U-1")
        assert "My nice unit" in first_line


# =============================================================================
# 9. Write-cycle-log refuses orphan unit ids (defensive)
# =============================================================================


class TestWriteCycleLogValidation:
    def test_raises_when_unit_state_missing(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """write_cycle_log must surface a clear error when the unit row
        is absent. (``regenerate_cycle_log`` is a thin wrapper around
        ``write_cycle_log`` and surfaces the same error; the prefix
        fallback in ``cycle_log_path`` only applies when callers invoke
        ``cycle_log_path`` directly.)
        """
        with pytest.raises(ValueError):
            cycle_log.write_cycle_log("F-999-U-1", base_dir=tmp_path, run=_Runner())

    def test_write_returns_correct_path(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed_unit(pr_number=None)
        target = cycle_log.write_cycle_log("F-042-U-1", base_dir=tmp_path, run=_Runner())
        assert target == tmp_path / "features" / "F-042" / "U-1.md"
