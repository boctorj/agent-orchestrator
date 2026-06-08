"""Tests for orchestrator/cycle_log.py — per-unit cycle-log writer.

All ``gh`` and ``git`` invocations are stubbed via an injectable
``subprocess.run``-shaped callable; no real GitHub or shell is touched.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import cycle_log, state
from orchestrator.models import Feature, WorkUnit, WorkUnitState

# --------------------------- fakes ---------------------------


@dataclass
class FakeProc:
    """Minimal ``subprocess.CompletedProcess`` look-alike."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    """Records every argv it's called with; returns canned responses by
    command shape.

    Tests register responses for prefixes like ``("gh", "pr", "view")``
    or ``("git", "diff", "--cached")``; the first matching registration
    wins. Unregistered calls return a zero-exit empty response so the
    happy-path code never accidentally hits the real shell.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: list[tuple[tuple[str, ...], FakeProc]] = []

    def register(self, prefix: tuple[str, ...], proc: FakeProc) -> None:
        self._responses.append((prefix, proc))

    def __call__(self, argv, **kwargs) -> FakeProc:  # noqa: D401 — match subprocess.run signature
        self.calls.append(list(argv))
        for prefix, proc in self._responses:
            if tuple(argv[: len(prefix)]) == prefix:
                return proc
        return FakeProc()


def _seed(
    unit_id: str = "F-007-U-2",
    feature_id: str = "F-007",
    repo_path: str = "https://github.com/o/r",
    pr_number: int | None = 42,
    status: str = "in_ci",
    unit_title: str = "OAuth callback route",
    events: list[dict[str, Any]] | None = None,
) -> None:
    """Persist a feature + plan + unit row + event stream for tests."""
    state.save_feature(Feature(id=feature_id, title="OAuth", description="", repo_path=repo_path))
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title=unit_title, description="")],
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
            details=ev.get("details", ""),
        )


# --------------------------- path helpers ---------------------------


class TestUnitBasename:
    @pytest.mark.parametrize(
        "unit_id,expected",
        [
            ("F-007-U-2", "U-2"),
            ("F-001-U-12", "U-12"),
            ("F-100-U-1", "U-1"),
        ],
    )
    def test_well_formed(self, unit_id: str, expected: str) -> None:
        assert cycle_log._unit_basename(unit_id) == expected

    def test_falls_back_to_raw_id(self) -> None:
        assert cycle_log._unit_basename("not-shaped-like-a-unit") == "not-shaped-like-a-unit"


class TestFeatureDir:
    def test_creates_directory(self, tmp_path: Path) -> None:
        d = cycle_log.feature_dir("F-007", base_dir=tmp_path)
        assert d == tmp_path / "features" / "F-007"
        assert d.is_dir()

    def test_idempotent_when_exists(self, tmp_path: Path) -> None:
        (tmp_path / "features" / "F-007").mkdir(parents=True)
        d = cycle_log.feature_dir("F-007", base_dir=tmp_path)
        assert d.is_dir()

    def test_works_without_pre_existing_features_root(self, tmp_path: Path) -> None:
        # Mirrors the "feature pre-dates spec.md infrastructure" case from
        # the F-006-U-2 unit description: no `features/` exists at all.
        assert not (tmp_path / "features").exists()
        cycle_log.feature_dir("F-099", base_dir=tmp_path)
        assert (tmp_path / "features" / "F-099").is_dir()


class TestCycleLogPath:
    def test_infers_feature_from_state(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed()
        p = cycle_log.cycle_log_path("F-007-U-2", base_dir=tmp_path)
        assert p == tmp_path / "features" / "F-007" / "U-2.md"
        # the parent dir is materialized so the subsequent write is safe
        assert p.parent.is_dir()

    def test_explicit_feature_id_overrides_state_lookup(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        p = cycle_log.cycle_log_path("F-007-U-2", feature_id="F-007", base_dir=tmp_path)
        assert p == tmp_path / "features" / "F-007" / "U-2.md"

    def test_orphan_unit_falls_back_to_id_prefix(self, tmp_path: Path, tmp_state_db: Path) -> None:
        # No state row, no explicit feature_id → derive from "F-099-U-3" prefix.
        p = cycle_log.cycle_log_path("F-099-U-3", base_dir=tmp_path)
        assert p == tmp_path / "features" / "F-099" / "U-3.md"


# --------------------------- GitHub mirroring ---------------------------


class TestFetchPrInfo:
    def test_invokes_gh_pr_view_with_json_fields(self) -> None:
        runner = FakeRunner()
        runner.register(
            ("gh", "pr", "view"),
            FakeProc(stdout=json.dumps({"title": "T", "body": "B", "headRefOid": "abc123"})),
        )

        info = cycle_log.fetch_pr_info("https://github.com/o/r", 42, run=runner)

        assert info == {"title": "T", "body": "B", "headRefOid": "abc123"}
        assert any(
            argv[:3] == ["gh", "pr", "view"] and "title,body,headRefOid" in argv
            for argv in runner.calls
        )

    def test_returns_empty_on_non_zero_exit(self) -> None:
        runner = FakeRunner()
        runner.register(("gh", "pr", "view"), FakeProc(returncode=1, stderr="boom"))
        assert cycle_log.fetch_pr_info("https://github.com/o/r", 42, run=runner) == {}

    def test_returns_empty_on_malformed_json(self) -> None:
        runner = FakeRunner()
        runner.register(("gh", "pr", "view"), FakeProc(stdout="not json"))
        assert cycle_log.fetch_pr_info("https://github.com/o/r", 42, run=runner) == {}

    def test_handles_missing_gh_binary(self) -> None:
        def boom(*a, **k):
            raise FileNotFoundError("gh")

        assert cycle_log.fetch_pr_info("https://github.com/o/r", 42, run=boom) == {}


class TestFetchReviewThreads:
    def test_invokes_graphql_review_threads_query(self) -> None:
        runner = FakeRunner()
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "PRT_1",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "databaseId": 12345,
                                                "path": "src/oauth.py",
                                                "line": 42,
                                                "body": "🔴 missing wrap",
                                                "url": "https://github.com/o/r/pull/42#r12345",
                                                "author": {"login": "reviewer"},
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
        runner.register(("gh", "api", "graphql"), FakeProc(stdout=json.dumps(payload)))

        threads = cycle_log.fetch_review_threads("https://github.com/o/r", 42, run=runner)

        assert len(threads) == 1
        t = threads[0]
        assert t["path"] == "src/oauth.py"
        assert t["line"] == 42
        assert t["author"] == "reviewer"
        assert t["url"].endswith("#r12345")
        # GraphQL call shape
        graphql_call = next(c for c in runner.calls if c[:3] == ["gh", "api", "graphql"])
        joined = " ".join(graphql_call)
        assert "reviewThreads" in joined
        assert "owner=o" in graphql_call
        assert "repo=r" in graphql_call
        assert "pr=42" in graphql_call

    def test_returns_empty_when_no_nodes(self) -> None:
        runner = FakeRunner()
        runner.register(
            ("gh", "api", "graphql"),
            FakeProc(
                stdout=json.dumps(
                    {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
                )
            ),
        )
        assert cycle_log.fetch_review_threads("https://github.com/o/r", 42, run=runner) == []

    def test_returns_empty_on_error(self) -> None:
        runner = FakeRunner()
        runner.register(("gh", "api", "graphql"), FakeProc(returncode=1, stderr="boom"))
        assert cycle_log.fetch_review_threads("https://github.com/o/r", 42, run=runner) == []


# --------------------------- rendering ---------------------------


class TestRenderCycleLog:
    def test_includes_required_sections(self, tmp_state_db: Path) -> None:
        _seed(
            events=[
                {
                    "event_type": "pr_opened",
                    "source": "coder",
                    "cycle_number": 0,
                    "summary": "PR #42 opened",
                },
                {
                    "event_type": "tester_bug_found",
                    "source": "tester",
                    "cycle_number": 1,
                    "summary": "callback 500 on invalid state",
                },
                {
                    "event_type": "fix_pushed",
                    "source": "coder",
                    "cycle_number": 1,
                    "summary": "added validate_state()",
                },
                {
                    "event_type": "reviewer_recommend_merge",
                    "source": "reviewer",
                    "cycle_number": 2,
                    "summary": "endorsed",
                },
            ]
        )

        md = cycle_log.render_cycle_log(
            "F-007-U-2",
            pr_info={
                "title": "OAuth callback route",
                "body": "## Summary\nadds the callback handler",
                "headRefOid": "deadbeef",
            },
            review_threads=[],
        )

        assert md.startswith("# F-007-U-2 — OAuth callback route")
        assert "## PR" in md
        assert "#42 · https://github.com/o/r/pull/42" in md
        assert "PR head SHA: deadbeef" in md
        assert "## Coder's PR description" in md
        assert "adds the callback handler" in md
        assert "## Cycle history" in md
        assert "### Cycle 0 — coder: PR opened" in md
        assert "### Cycle 1 — tester: BUG_FOUND" in md
        assert "callback 500 on invalid state" in md
        assert "### Cycle 1 — coder fix: FIX_PUSHED" in md
        assert "### Cycle 2 — reviewer: REVIEW_RECOMMEND_MERGE" in md
        assert "## Review threads" in md

    def test_unknown_head_sha_renders_placeholder(self, tmp_state_db: Path) -> None:
        _seed()
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert "PR head SHA: _unknown_" in md
        assert "_unavailable_" in md  # PR description placeholder

    def test_status_line_includes_last_activity_timestamp(self, tmp_state_db: Path) -> None:
        # The proposal example schema renders
        # `Status: merged (2026-05-15 14:32 UTC)`. The renderer reads
        # the timestamp out of ``unit_state.last_activity`` (already UTC
        # ISO-8601 from ``state._now()``).
        _seed()
        unit_state = state.get_unit_state("F-007-U-2")
        assert unit_state is not None and unit_state.last_activity, (
            "_seed should leave last_activity set so this test exercises the timestamp path"
        )
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert f"Status: in_ci ({unit_state.last_activity} UTC)" in md

    def test_renders_review_threads_with_tier_marker(self, tmp_state_db: Path) -> None:
        _seed()
        threads = [
            {
                "id": "PRT_1",
                "isResolved": False,
                "isOutdated": False,
                "path": "oauth.py",
                "line": 127,
                "body": "🔴 missing Fernet wrap before store",
                "url": "https://github.com/o/r/pull/42#r999",
                "author": "rev",
            },
            {
                "id": "PRT_2",
                "isResolved": True,
                "isOutdated": False,
                "path": "tests/test_oauth.py",
                "line": 12,
                "body": "🟠 missing refresh test",
                "url": "",
                "author": "rev",
            },
        ]
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=threads)
        assert "🔴 oauth.py:127" in md
        assert "🟠 tests/test_oauth.py:12 [resolved]" in md
        assert "(https://github.com/o/r/pull/42#r999)" in md

    def test_skips_scheduler_events(self, tmp_state_db: Path) -> None:
        _seed(
            events=[
                {"event_type": "spawn_coder", "cycle_number": 0, "summary": "kick off"},
                {"event_type": "spawn_tester", "cycle_number": 1, "summary": "kick off"},
                {
                    "event_type": "fix_pushed",
                    "source": "coder",
                    "cycle_number": 1,
                    "summary": "fix",
                },
            ]
        )
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert "spawn" not in md.lower()
        assert "### Cycle 1 — coder fix: FIX_PUSHED" in md

    def test_cap_3_marker_when_cap_actually_hit(self, tmp_state_db: Path) -> None:
        # Three fix cycles AND the unit ended in `escalated` status —
        # exactly the cap-hit scenario the execution layer produces when
        # `review_round >= CAP_3` blocks the next fix attempt.
        _seed(
            status="escalated",
            events=[
                {"event_type": "fix_pushed", "cycle_number": 1, "summary": "f1"},
                {"event_type": "fix_pushed", "cycle_number": 2, "summary": "f2"},
                {"event_type": "fix_pushed", "cycle_number": 3, "summary": "f3"},
            ],
        )
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert "3 cycles · cap-3 hit" in md

    def test_three_cycles_with_recommend_merge_renders_not_hit(self, tmp_state_db: Path) -> None:
        # Mirror the proposal § "Per-unit cycle log" example schema:
        # three cycles ending in REVIEW_RECOMMEND_MERGE must render as
        # `cap-3 not hit` — the cap was reached but the unit succeeded.
        _seed(
            status="in_ci",
            events=[
                {"event_type": "fix_pushed", "cycle_number": 1, "summary": "f1"},
                {"event_type": "fix_pushed", "cycle_number": 2, "summary": "f2"},
                {
                    "event_type": "reviewer_recommend_merge",
                    "cycle_number": 3,
                    "summary": "endorsed",
                },
            ],
        )
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert "3 cycles · cap-3 not hit" in md
        assert "### Cycle 3 — reviewer: REVIEW_RECOMMEND_MERGE" in md

    def test_ultrareview_events_render_in_cycle_history(self, tmp_state_db: Path) -> None:
        # F-007-U-3 regression: ``ultrareview_*`` events were recorded but the
        # renderer's ``_EVENT_HEADINGS`` allow-list dropped them, so a unit
        # that escalated *because* of ultrareview FAIL silently looked like
        # the reviewer endorsed and the unit then mysteriously escalated.
        # All three event names from the gate must surface, and the FAILED
        # summary must reach the rendered markdown so the committed log
        # explains the escalation.
        _seed(
            status="escalated",
            events=[
                {
                    "event_type": "reviewer_recommend_merge",
                    "cycle_number": 0,
                    "summary": "endorsed",
                },
                {
                    "event_type": "ultrareview_started",
                    "source": "ultrareview",
                    "cycle_number": 0,
                    "summary": "firing /ultrareview",
                },
                {
                    "event_type": "ultrareview_failed",
                    "source": "ultrareview",
                    "cycle_number": 0,
                    "summary": "ultrareview failed with 2 findings",
                },
            ],
        )
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert "### Cycle 0 — ultrareview: STARTED" in md
        assert "### Cycle 0 — ultrareview: FAILED" in md
        assert "ultrareview failed with 2 findings" in md, (
            "FAILED event summary must reach the rendered markdown — the "
            "committed cycle log is the only on-disk record of why the unit "
            "escalated"
        )

    def test_ultrareview_passed_event_renders(self, tmp_state_db: Path) -> None:
        # Symmetric to the FAILED test: PASS path must also surface so a
        # successful ultrareview gate run shows up in the committed log
        # (cost-attribution / postmortem queries read this).
        _seed(
            status="in_ci",
            events=[
                {
                    "event_type": "reviewer_recommend_merge",
                    "cycle_number": 0,
                    "summary": "endorsed",
                },
                {
                    "event_type": "ultrareview_started",
                    "cycle_number": 0,
                    "summary": "firing /ultrareview",
                },
                {
                    "event_type": "ultrareview_passed",
                    "cycle_number": 0,
                    "summary": "ultrareview passed",
                },
            ],
        )
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert "### Cycle 0 — ultrareview: STARTED" in md
        assert "### Cycle 0 — ultrareview: PASSED" in md

    def test_ultrareview_fix_cycle_events_render_with_dynamic_n(self, tmp_state_db: Path) -> None:
        # F-007-U-4 regression (recurrence of U-3's H1 pattern): the renderer
        # historically used a static ``_EVENT_HEADINGS.get(event_type)``
        # lookup that silently dropped every ``ultrareview_fix_cycle_N``
        # event, where N is the shared CAP_3 cycle number. The fix routes
        # the lookup through ``_heading_for`` to resolve the dynamic prefix.
        #
        # A committed cycle log for a unit that escalated after running
        # through ultrareview fix cycles must surface every fix-cycle event
        # (with its summary) so a reader can attribute the interleaved
        # `fix_pushed` events to ultrareview vs reviewer-changes vs CI-fix —
        # the distinction the event was added to make legible.
        _seed(
            status="escalated",
            events=[
                {
                    "event_type": "reviewer_recommend_merge",
                    "cycle_number": 0,
                    "summary": "endorsed",
                },
                {
                    "event_type": "ultrareview_started",
                    "source": "ultrareview",
                    "cycle_number": 0,
                    "summary": "firing /ultrareview",
                },
                {
                    "event_type": "ultrareview_failed",
                    "source": "ultrareview",
                    "cycle_number": 0,
                    "summary": "ultrareview failed with 2 findings",
                },
                {
                    "event_type": "ultrareview_fix_cycle_1",
                    "source": "ultrareview",
                    "cycle_number": 1,
                    "summary": "coder fix cycle 1 for 2 ultrareview finding(s)",
                },
                {
                    "event_type": "fix_pushed",
                    "cycle_number": 1,
                    "summary": "fix pushed",
                },
                {
                    "event_type": "ultrareview_fix_cycle_2",
                    "source": "ultrareview",
                    "cycle_number": 2,
                    "summary": "coder fix cycle 2 for 1 ultrareview finding(s)",
                },
            ],
        )
        md = cycle_log.render_cycle_log("F-007-U-2", pr_info={}, review_threads=[])
        assert "### Cycle 1 — ultrareview: fix cycle 1" in md, (
            "ultrareview_fix_cycle_1 must surface in the rendered markdown — "
            "without it, the committed log can't attribute the interleaved "
            "coder fix events to ultrareview vs reviewer-changes vs CI-fix"
        )
        assert "### Cycle 2 — ultrareview: fix cycle 2" in md
        assert "coder fix cycle 1 for 2 ultrareview finding(s)" in md, (
            "fix-cycle event summary must reach the rendered markdown — "
            "the cycle-log is the on-disk record of the audit trail"
        )


# --------------------------- writing ---------------------------


class TestWriteCycleLog:
    def test_creates_features_dir_before_write_for_unseen_feature(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        # No features/ at all on disk — mirrors the "feature pre-dates
        # spec.md infrastructure" scenario the F-006-U-2 unit description
        # calls out explicitly.
        assert not (tmp_path / "features").exists()
        _seed(pr_number=None)  # no PR -> skip github calls
        runner = FakeRunner()

        target = cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path, run=runner)

        assert target == tmp_path / "features" / "F-007" / "U-2.md"
        assert target.is_file()
        assert "# F-007-U-2" in target.read_text(encoding="utf-8")

    def test_atomic_write_leaves_no_tmp_file_on_success(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed(pr_number=None)
        cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path, run=FakeRunner())
        assert list((tmp_path / "features" / "F-007").glob("*.tmp")) == []

    def test_atomic_write_preserves_prior_file_on_failure(
        self, tmp_path: Path, tmp_state_db: Path, monkeypatch
    ) -> None:
        # Pre-seed an existing finalized log; simulate a crash during the
        # subsequent tmp.replace() call. The original file must survive.
        _seed(pr_number=None)
        target = tmp_path / "features" / "F-007" / "U-2.md"
        target.parent.mkdir(parents=True)
        target.write_text("PRIOR CONTENT", encoding="utf-8")

        def explode(self, target_path):  # noqa: ARG001
            raise OSError("disk full")

        monkeypatch.setattr(Path, "replace", explode)

        with pytest.raises(OSError, match="disk full"):
            cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path, run=FakeRunner())

        assert target.read_text(encoding="utf-8") == "PRIOR CONTENT"
        # No dangling .tmp either.
        assert list(target.parent.glob("*.tmp")) == []

    def test_mirrors_pr_description_and_threads_from_gh(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed()
        runner = FakeRunner()
        runner.register(
            ("gh", "pr", "view"),
            FakeProc(stdout=json.dumps({"title": "T", "body": "PR body", "headRefOid": "abc"})),
        )
        runner.register(
            ("gh", "api", "graphql"),
            FakeProc(
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": "PRT",
                                                "isResolved": False,
                                                "isOutdated": False,
                                                "comments": {
                                                    "nodes": [
                                                        {
                                                            "databaseId": 9,
                                                            "path": "x.py",
                                                            "line": 1,
                                                            "body": "🔴 thing",
                                                            "url": "https://example/r#9",
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
                )
            ),
        )

        target = cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path, run=runner)

        body = target.read_text(encoding="utf-8")
        assert "PR body" in body
        assert "PR head SHA: abc" in body
        assert "🔴 x.py:1" in body

    def test_auto_commits_locally_with_orchestrator_bot_identity(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed(pr_number=None)
        runner = FakeRunner()
        # Pretend `git diff --cached --quiet` reports staged changes
        # (returncode 1 → "go ahead and commit").
        runner.register(("git", "diff", "--cached"), FakeProc(returncode=1))

        cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path, run=runner)

        commit_calls = [c for c in runner.calls if c[:1] == ["git"] and "commit" in c]
        assert commit_calls, "expected one git commit invocation"
        argv = commit_calls[0]
        assert f"user.email={cycle_log.COMMIT_USER_EMAIL}" in argv
        assert f"user.name={cycle_log.COMMIT_USER_NAME}" in argv
        # Hard rule: never push.
        assert not any("push" in c for c in runner.calls)

    def test_skips_commit_when_nothing_changed(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed(pr_number=None)
        runner = FakeRunner()
        # `git diff --cached --quiet` exits 0 → no staged changes.
        runner.register(("git", "diff", "--cached"), FakeProc(returncode=0))

        cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path, run=runner)

        assert not any(c[:1] == ["git"] and "commit" in c for c in runner.calls), (
            "no-op commits must not pollute history"
        )

    def test_raises_when_unit_unknown(self, tmp_path: Path, tmp_state_db: Path) -> None:
        with pytest.raises(ValueError, match="no work_units row"):
            cycle_log.write_cycle_log("F-099-U-1", base_dir=tmp_path, run=FakeRunner())

    def test_returns_resolved_absolute_path(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """write_cycle_log must return an absolute path (Copilot review #3249520148)."""
        _seed(pr_number=None)
        rel_base = Path("./" + tmp_path.name)
        # Run from tmp_path.parent so the relative ``./<name>`` resolves to tmp_path.
        import os

        prev = Path.cwd()
        try:
            os.chdir(tmp_path.parent)
            target = cycle_log.write_cycle_log("F-007-U-2", base_dir=rel_base, run=FakeRunner())
        finally:
            os.chdir(prev)
        assert target.is_absolute()

    def test_does_not_commit_when_git_errors(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """git diff rc != 1 (e.g. 128 from a non-repo) must skip commit, not proceed.

        Copilot review #3249520130 — previously any non-zero rc was treated
        as "has staged changes" which would issue a spurious commit attempt.
        """
        _seed(pr_number=None)
        runner = FakeRunner()
        runner.register(("git", "diff", "--cached"), FakeProc(returncode=128, stderr="not a repo"))

        cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path, run=runner)

        assert not any(c[:1] == ["git"] and "commit" in c for c in runner.calls), (
            "rc=128 from git diff must not trigger a commit"
        )


class TestDefensiveFetch:
    """Defensive parsing for the GitHub mirroring (Copilot review on PR #26).

    parse_repo_url ValueError handling + GraphQL shape coercion.
    """

    def test_fetch_pr_info_returns_empty_on_malformed_repo_url(self) -> None:
        # ``parse_repo_url`` raises ValueError on a non-GitHub URL.
        info = cycle_log.fetch_pr_info("not-a-github-url", 42, run=FakeRunner())
        assert info == {}

    def test_fetch_review_threads_returns_empty_on_malformed_repo_url(self) -> None:
        threads = cycle_log.fetch_review_threads("not-a-github-url", 42, run=FakeRunner())
        assert threads == []

    def test_fetch_review_threads_handles_non_dict_json_root(self) -> None:
        runner = FakeRunner()
        runner.register(("gh", "api", "graphql"), FakeProc(returncode=0, stdout="[1, 2, 3]"))
        assert cycle_log.fetch_review_threads("https://github.com/o/r", 1, run=runner) == []

    def test_fetch_review_threads_handles_nodes_null(self) -> None:
        runner = FakeRunner()
        payload = json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": None}}}}}
        )
        runner.register(("gh", "api", "graphql"), FakeProc(returncode=0, stdout=payload))
        assert cycle_log.fetch_review_threads("https://github.com/o/r", 1, run=runner) == []

    def test_fetch_review_threads_handles_missing_pull_request_key(self) -> None:
        runner = FakeRunner()
        # data.repository present but no pullRequest key
        runner.register(
            ("gh", "api", "graphql"),
            FakeProc(returncode=0, stdout=json.dumps({"data": {"repository": {}}})),
        )
        assert cycle_log.fetch_review_threads("https://github.com/o/r", 1, run=runner) == []


class TestRegenerateCycleLog:
    def test_writes_when_file_missing(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed(pr_number=None)
        target = cycle_log.regenerate_cycle_log("F-007-U-2", base_dir=tmp_path, run=FakeRunner())
        assert target.is_file()

    def test_overwrites_existing_file(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed(pr_number=None)
        target = tmp_path / "features" / "F-007" / "U-2.md"
        target.parent.mkdir(parents=True)
        target.write_text("STALE STALE STALE", encoding="utf-8")

        cycle_log.regenerate_cycle_log("F-007-U-2", base_dir=tmp_path, run=FakeRunner())

        rewritten = target.read_text(encoding="utf-8")
        assert "STALE" not in rewritten
        assert "# F-007-U-2" in rewritten

    def test_creates_features_dir_for_orphan_feature(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        # Feature pre-dates spec.md infrastructure → no features/ dir.
        _seed(pr_number=None)
        assert not (tmp_path / "features").exists()

        cycle_log.regenerate_cycle_log("F-007-U-2", base_dir=tmp_path, run=FakeRunner())

        assert (tmp_path / "features" / "F-007" / "U-2.md").is_file()

    def test_uses_regenerate_commit_message(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed(pr_number=None)
        runner = FakeRunner()
        runner.register(("git", "diff", "--cached"), FakeProc(returncode=1))

        cycle_log.regenerate_cycle_log("F-007-U-2", base_dir=tmp_path, run=runner)

        commit = next(c for c in runner.calls if "commit" in c)
        # "-m" is followed by the message
        msg = commit[commit.index("-m") + 1]
        assert "regenerate" in msg
        assert "F-007-U-2" in msg


# --------------------------- defaults wire to real subprocess ---------------------------


class TestRunnerDefault:
    def test_write_cycle_log_uses_subprocess_run_by_default(
        self,
        tmp_path: Path,
        tmp_state_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity check: when ``run`` isn't passed, ``subprocess.run`` is used.

        Caller code that forgets to pass a runner must still get the
        right binding, not a misfire from a stray module-level capture.
        """
        _seed(pr_number=None)
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return FakeProc()

        monkeypatch.setattr(subprocess, "run", fake_run)

        cycle_log.write_cycle_log("F-007-U-2", base_dir=tmp_path)

        assert any(c[:1] == ["git"] for c in calls), "default runner must reach subprocess.run"
