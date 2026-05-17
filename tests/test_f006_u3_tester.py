"""Independent tester-written spec tests for F-006-U-3.

These tests are written by an independent tester pass over the same
unit description as ``tests/test_f006_u3_spec.py`` (coder's own
self-checks). They cover orthogonal angles — the goal is to catch
implementation drift the coder's tests would miss:

  * **base_dir anchor invariant** — cycle-log writes anchor to
    ``state.STATE_DB.parent``, not ``Path.cwd()``. The lead's working
    directory is unrelated to where ``features/`` should live.

  * **Per-cycle append fires on reviewer-loop fixes too**, not only on
    tester-bug fixes. The unit description says "append per-cycle entries
    during the loop" — both fix loops must hook the writer.

  * **CI-timeout escalation** writes a cycle log (the description lists
    "escalation" as a finalize trigger, and CI-timeout is a distinct
    escalation path the coder's spec-tests don't exercise).

  * **`check_unit_pr` backfill commit message** mentions "backfill" — the
    proposal § "Per-unit cycle log" calls out the post-merge edit as
    distinct from the pre-merge finalize so the git journal is auditable.

  * **`check_unit_pr` does not re-backfill** when the unit is already
    ``done`` (the merge SHA is the *only* post-finalization edit; a
    second poll must not redo it).

  * **`check_unit_pr` does not run the writer when the PR is not merged
    yet** — the writer's domain is finalize + backfill, not polling
    chatter.

  * **`_cycle_log_base_dir()` helper** in execution.py returns
    ``Path(state.STATE_DB).parent`` (locks the invariant the implementer
    documented inline).

  * **Pre-merge terminal write uses the default commit message** (no
    "backfill" token) so the backfill is distinguishable in `git log`.

  * **Manual kill via direct state mutation** is a "terminal" outcome too —
    the cycle log captures the escalated status on disk regardless of
    which escalation branch we hit (here: CI timeout before tester).

All ``ManagedAgentWorker`` / ``github.*`` / ``subprocess.run`` / ``ntfy``
calls are mocked. No real network, git, or shell.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import cycle_log, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, ops

# --------------------------- shared fakes ---------------------------


@dataclass
class _Proc:
    """Stand-in for ``subprocess.CompletedProcess``."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _Runner:
    """Recording subprocess.run shim — distinct from coder's runner so a
    rename in their fixtures doesn't break our coverage.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _Proc:
        self.calls.append(list(argv))
        # ``git diff --cached --quiet`` rc=1 == "there are staged changes"
        # which is the path that triggers the commit. Everything else is
        # benign zero-exit so the writer keeps going.
        if argv[:3] == ["git", "diff", "--cached"]:
            return _Proc(returncode=1)
        if argv[:3] == ["gh", "pr", "view"]:
            return _Proc(stdout=json.dumps({"title": "T", "body": "B", "headRefOid": "headsha"}))
        if argv[:3] == ["gh", "api", "graphql"]:
            return _Proc(stdout="{}")
        return _Proc()


def _seed(
    *,
    unit_id: str = "F-700-U-2",
    feature_id: str = "F-700",
    repo: str = "https://github.com/o/r",
    pr_number: int | None = 99,
    status: str = "in_ci",
) -> None:
    """Seed a feature+plan+unit so cycle_review/check_unit_pr have something
    to chew on.
    """
    state.save_feature(
        Feature(id=feature_id, title="t", description="d", repo_path=repo, status="approved")
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title="some unit", description="d")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="feat/b",
            pr_number=pr_number,
            coder_session_id="sesn-c",
        )
    )


@pytest.fixture(autouse=True)
def _silence_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every network/ntfy surface cycle_review or check_unit_pr can
    hit so a failing test reveals cycle-log behavior, not a missing
    monkeypatch.
    """
    monkeypatch.setattr(execution.ntfy, "push_escalation", lambda *a, **k: True)
    monkeypatch.setattr(execution.ntfy, "push_ready_to_merge", lambda *a, **k: True)
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.request_copilot_review",
        lambda *a, **k: {"requested": False},
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.wait_for_copilot_review",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("o", "r"),
    )


@pytest.fixture
def _ci_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        execution.ci_wait,
        "wait_for_ci",
        lambda *a, **k: CIWaitResult(status="green", elapsed_seconds=0.0, total_checks=1),
    )


@pytest.fixture
def _runner(monkeypatch: pytest.MonkeyPatch) -> _Runner:
    """Route both cycle_log module's subprocess.run and the gh helper
    module's through the recorder. Cycle-log's commit + gh mirror both
    funnel here.
    """
    runner = _Runner()
    monkeypatch.setattr("orchestrator.cycle_log.subprocess.run", runner)
    monkeypatch.setattr("orchestrator.cycle_log_gh.subprocess.run", runner)
    return runner


# =============================================================================
# A. base_dir anchor — STATE_DB.parent, not cwd
# =============================================================================


class TestBaseDirAnchor:
    """The implementer's inline doc says ``state.STATE_DB.parent`` rather
    than ``Path.cwd()``. A future refactor that flips the anchor back to
    cwd would silently break tmp-isolated tests and break operators who
    run the orchestrator from a different working directory than the
    state.db lives.
    """

    def test_cycle_log_lives_under_state_db_parent_not_cwd(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _ci_green: None,
        _runner: _Runner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Set cwd to a different tmp dir than the state.db parent. If the
        # implementation anchors on cwd, the log lands here; if it
        # anchors on STATE_DB.parent (correct), it lands in tmp_state_db's
        # parent.
        unrelated_cwd = tmp_path / "unrelated_workdir"
        unrelated_cwd.mkdir()
        monkeypatch.chdir(unrelated_cwd)

        _seed()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )

        execution.cycle_review("F-700", "F-700-U-2")

        correct = tmp_state_db.parent / "features" / "F-700" / "U-2.md"
        wrong = unrelated_cwd / "features" / "F-700" / "U-2.md"
        assert correct.is_file(), (
            f"cycle log must be anchored to STATE_DB.parent ({tmp_state_db.parent}); "
            f"file not found at {correct}"
        )
        assert not wrong.exists(), (
            f"cycle log must NOT land relative to cwd ({unrelated_cwd}); unexpected file at {wrong}"
        )

    def test_helper_returns_state_db_parent(self, tmp_state_db: Path) -> None:
        """The private helper that materializes the anchor must point at
        STATE_DB.parent verbatim. Pinning the helper directly catches a
        refactor that swaps it for ``Path.cwd()`` even if no visible
        cycle_review call would surface the regression in CI.
        """
        # _cycle_log_base_dir is a private helper but it's load-bearing for
        # cycle_log isolation in tests + for production correctness.
        assert execution._cycle_log_base_dir() == Path(state.STATE_DB).parent


# =============================================================================
# B. Per-cycle append fires in the REVIEWER fix loop (not only tester)
# =============================================================================


class TestPerCycleAppendInReviewerLoop:
    """The coder's tests cover per-cycle append for the tester-bug fix
    loop. The unit description says "append per-cycle entries during
    the loop" — both the tester loop *and* the reviewer loop must hook
    the writer between iterations.
    """

    def test_writer_invoked_between_reviewer_iterations(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _ci_green: None,
        _runner: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        # Reviewer flips: first call requests changes, second approves.
        reviewer_seq = iter(
            [
                json.dumps(
                    {"unit_id": "U", "outcome": "REVIEW_REQUEST_CHANGES", "issue": "needs tweak"}
                ),
                json.dumps({"unit_id": "U", "outcome": "REVIEW_RECOMMEND_MERGE"}),
            ]
        )
        monkeypatch.setattr(execution, "spawn_reviewer", lambda f, u: next(reviewer_seq))

        def fake_address_review(uid: str, src: str, fb: str) -> str:
            state.increment_review_round(uid)
            return json.dumps({"outcome": "FIX_PUSHED", "cycle": 1})

        monkeypatch.setattr(execution, "address_review", fake_address_review)

        write_calls: list[str | None] = []
        real_write = cycle_log.write_cycle_log

        def spy(unit_id: str, **kwargs: Any) -> Path:
            write_calls.append(kwargs.get("merge_commit_sha"))
            return real_write(unit_id, **kwargs)

        monkeypatch.setattr("orchestrator.tools.execution.cycle_log.write_cycle_log", spy)

        execution.cycle_review("F-700", "F-700-U-2")

        # We expect AT LEAST one mid-cycle write (after the reviewer-fix
        # push, before the reviewer retry) plus the terminal finalize.
        assert len(write_calls) >= 2, (
            f"expected reviewer-loop per-cycle append + terminal write; got {len(write_calls)} "
            f"call(s) with merge_commit_sha={write_calls!r}"
        )
        # Neither cycle_review write may carry a merge_commit_sha — that's
        # reserved for check_unit_pr backfill.
        assert all(sha is None for sha in write_calls), (
            f"cycle_review writes must not pass merge_commit_sha; saw {write_calls!r}"
        )


# =============================================================================
# C. CI-timeout escalation writes a cycle log
# =============================================================================


class TestCITimeoutEscalation:
    """The unit description lists "escalation" as a finalize trigger.
    cycle_review has several escalation entry points; CI timeout is one
    the coder's tests don't exercise.
    """

    def test_ci_timeout_before_tester_writes_cycle_log(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _runner: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed()
        # CI never goes green — wait_for_ci returns a timeout result.
        monkeypatch.setattr(
            execution.ci_wait,
            "wait_for_ci",
            lambda *a, **k: CIWaitResult(status="timeout", elapsed_seconds=600.0, total_checks=1),
        )

        out = execution.cycle_review("F-700", "F-700-U-2")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated", parsed

        log_path = tmp_state_db.parent / "features" / "F-700" / "U-2.md"
        assert log_path.is_file(), (
            "CI-timeout escalation is a terminal of cycle_review; cycle log must be finalized"
        )


# =============================================================================
# D. check_unit_pr — commit message + once-only backfill + non-merge skip
# =============================================================================


class TestCheckUnitPrBackfillSemantics:
    def _seed_merged_responder(self, monkeypatch: pytest.MonkeyPatch, *, merge_sha: str) -> None:
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_state",
            lambda url, pr: {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-05-15T14:32:00Z",
                "head_sha": "headsha",
                "merge_commit_sha": merge_sha,
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_check_runs",
            lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
        )

    def test_backfill_commit_message_mentions_backfill(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proposal: the post-merge backfill is the only post-finalization
        edit. To keep `git log features/F-XXX/U-N.md` auditable, the
        commit message must be distinguishable from the pre-merge
        finalize commit.
        """
        _seed(status="in_ci")
        self._seed_merged_responder(monkeypatch, merge_sha="cafef00d")

        captured: dict[str, Any] = {}

        def spy_write(unit_id: str, **kwargs: Any) -> Path:
            captured.update({"unit_id": unit_id, **kwargs})
            return Path("/tmp/ignored.md")  # nosec B108 — test stub; never written

        monkeypatch.setattr("orchestrator.tools.ops.cycle_log.write_cycle_log", spy_write)

        ops.check_unit_pr("F-700-U-2")

        assert captured.get("merge_commit_sha") == "cafef00d"
        msg = captured.get("commit_message", "")
        assert "backfill" in msg.lower(), (
            f"backfill commit message must say 'backfill' (got: {msg!r}); "
            "it's the proposal-spec'd marker that distinguishes the post-merge edit "
            "from the pre-merge finalize commit in git log."
        )
        # Same unit id passed through verbatim.
        assert captured["unit_id"] == "F-700-U-2"

    def test_no_writer_call_when_pr_not_merged(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A poll on an open PR must not touch the writer. The backfill is
        gated on ``merged == True``.
        """
        _seed(status="in_ci")
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_state",
            lambda url, pr: {
                "state": "open",
                "merged": False,
                "merged_at": None,
                "head_sha": "headsha",
                "merge_commit_sha": None,
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_check_runs",
            lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
        )

        write_calls: list[Any] = []

        def spy(*a: Any, **k: Any) -> Path:
            write_calls.append((a, k))
            return Path("/dev/null")

        monkeypatch.setattr("orchestrator.tools.ops.cycle_log.write_cycle_log", spy)

        out = ops.check_unit_pr("F-700-U-2")
        parsed = json.loads(out)
        # Status stays whatever it was — NOT done.
        assert parsed["orchestrator_status"] != "done"
        assert write_calls == [], (
            f"writer must not be invoked while PR is still open; got {write_calls!r}"
        )

    def test_backfill_runs_only_once_across_repeated_polls(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The proposal calls out the backfill as ``the only
        post-finalization edit`` — i.e. exactly one. A second
        ``check_unit_pr`` after the unit has already been flipped to
        ``done`` must not redo it (the unit_status != 'done' guard
        rides on the first-transition gate).
        """
        _seed(status="in_ci")
        self._seed_merged_responder(monkeypatch, merge_sha="d00ddeeb")

        write_count: dict[str, int] = {"n": 0}

        def spy(*a: Any, **k: Any) -> Path:
            write_count["n"] += 1
            return Path("/dev/null")

        monkeypatch.setattr("orchestrator.tools.ops.cycle_log.write_cycle_log", spy)

        ops.check_unit_pr("F-700-U-2")
        assert write_count["n"] == 1, "first poll must trigger the backfill exactly once"

        # Second poll on the now-merged unit: writer should NOT run again.
        ops.check_unit_pr("F-700-U-2")
        assert write_count["n"] == 1, (
            f"second poll on an already-done unit must not re-invoke the backfill writer; "
            f"got {write_count['n']} total invocations"
        )


# =============================================================================
# E. Pre-merge terminal write uses the default commit message
# =============================================================================


class TestPreMergeCommitMessage:
    """The pre-merge finalize uses the writer's default
    ``cycle-log: <unit_id>``; backfill uses ``cycle-log: backfill merge
    SHA for <unit_id>``. Both must be greppable separately in git log.
    """

    def test_pre_merge_commit_message_does_not_say_backfill(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _ci_green: None,
        _runner: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )

        execution.cycle_review("F-700", "F-700-U-2")

        commit_calls = [
            c for c in _runner.calls if c[:1] == ["git"] and "commit" in c and "-m" in c
        ]
        assert commit_calls, "expected at least one git commit invocation"
        # Extract every -m message and confirm none say "backfill".
        for argv in commit_calls:
            msg = argv[argv.index("-m") + 1]
            assert "backfill" not in msg.lower(), (
                f"pre-merge commit message must not mention 'backfill' "
                f"(reserved for the post-merge edit); got: {msg!r}"
            )


# =============================================================================
# F. Pre-merge cycle log has no Merge commit SHA; backfill adds one and ONLY one
# =============================================================================


class TestMergeShaLineAppearsExactlyOnce:
    """Defensive — after the full lifecycle (finalize + backfill) the
    rendered markdown contains exactly one ``Merge commit SHA: <sha>``
    line. A second backfill (if the gate misfired) would either
    duplicate or omit it.
    """

    def test_lifecycle_renders_exactly_one_merge_sha_line(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _ci_green: None,
        _runner: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed()
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )

        execution.cycle_review("F-700", "F-700-U-2")
        log_path = tmp_state_db.parent / "features" / "F-700" / "U-2.md"
        body_pre = log_path.read_text(encoding="utf-8")
        assert body_pre.count("Merge commit SHA") == 0

        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_state",
            lambda url, pr: {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-05-15T14:32:00Z",
                "head_sha": "headsha",
                "merge_commit_sha": "1234abcd5678",
            },
        )
        monkeypatch.setattr(
            "orchestrator.tools.ops.github.get_pr_check_runs",
            lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
        )
        ops.check_unit_pr("F-700-U-2")

        body_post = log_path.read_text(encoding="utf-8")
        assert body_post.count("Merge commit SHA: 1234abcd5678") == 1, (
            f"expected exactly one merge-SHA line post-backfill, got\n{body_post}"
        )
        # No leftover line from a missing/null SHA.
        assert "_unknown_" not in body_post.split("Merge commit SHA:")[1].splitlines()[0]


# =============================================================================
# G. cycle_review with no PR yet does not crash the writer hook
# =============================================================================


class TestNoPRYetStillTerminates:
    """If the cycle_review fast-path bails before there's a PR (e.g.
    unit_state has no pr_number), the cycle-log writer still gets called
    on the terminal branch. ``_write_cycle_log_safe`` swallows the
    inevitable error (cycle_log.write_cycle_log will not be able to
    mirror gh data, but the file must still be writable from
    state.list_events alone). The contract is: never crash cycle_review.
    """

    def test_terminal_emit_with_no_pr_does_not_raise(
        self,
        tmp_state_db: Path,
        with_github_token: None,
        _runner: _Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Set up a unit WITHOUT a pr_number; force escalation via tester
        # blocked outcome to exercise _emit_terminal.
        _seed(pr_number=None)
        monkeypatch.setattr(
            execution.ci_wait,
            "wait_for_ci",
            lambda *a, **k: CIWaitResult(status="no_ci", elapsed_seconds=0.0, total_checks=0),
        )
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: "BLOCKED — tester for X: no PR to inspect",
        )

        # No assertion needed beyond "doesn't raise" — cycle_review must
        # return cleanly even though _write_cycle_log_safe can't reach gh.
        out = execution.cycle_review("F-700", "F-700-U-2")
        parsed = json.loads(out)
        assert parsed["outcome"] == "escalated", parsed
