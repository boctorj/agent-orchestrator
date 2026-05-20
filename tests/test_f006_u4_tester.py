"""Tester-authored tests for F-006-U-4 — compose_*_task injections + call sites.

Independent of `tests/test_f006_u4_spec.py` (coder's tests). Focuses on the
behaviors the unit description calls out that the coder's tests under-cover
or skip entirely:

  1. **Every call site in execution.py** passes the new kwargs — not just
     `spawn_unit`. Exercises `spawn_tester` and `spawn_reviewer` end-to-end
     and asserts the rendered task string actually carries the context.
  2. **Reviewer own_cycle_log gating on retry cycle >= 2** — coder asserted
     only the constant value. Here we drive the gate by varying
     `unit_state.review_round` (0, 1, 2, 3) and inspect the task string
     that reaches the worker.
  3. **Predecessor cycle-log integration** — a real `features/F-XXX/U-N.md`
     file on disk flows through the call site, the summary stripper, and
     the renderer into the `## PREDECESSOR UNITS` block.
  4. **Read-side null-safety end-to-end** — spawn_tester / spawn_reviewer
     succeed (no exception, no escalation) when spec.md and all
     predecessor cycle logs are missing. This is the "no-op for this
     feature's own in-flight units" path called out in the unit description.
  5. **Ordering and block delimiters** — context blocks land after
     FEATURE CONTEXT and before the closing instruction; multiple blocks
     are separated by a blank line so each parses as its own markdown
     section.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import cycle_log, feature_spec, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import (
    compose_coder_task,
    compose_reviewer_task,
    compose_tester_task,
    execution,
)

# --------------------------- shared CI/github stubs ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend CI is green so spawn_tester / spawn_reviewer don't refuse."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=0.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _stub_github(monkeypatch):
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests", lambda *a, **k: 0
    )


# --------------------------- worker fake ---------------------------


class _CapturingWorker:
    """Captures every spawn task message so we can assert injection content."""

    instances: list[_CapturingWorker] = []

    def __init__(self, role: str, response: str = "noop\nTESTS_PASS"):
        self.role = role
        self._response = response
        self.spawn_tasks: list[str] = []
        self.spawn_titles: list[str | None] = []
        _CapturingWorker.instances.append(self)

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        self.spawn_tasks.append(task)
        self.spawn_titles.append(title)
        return f"sesn-{self.role}-{len(self.spawn_tasks)}", self._response

    def resume(self, sid: str, msg: str) -> str:  # pragma: no cover - not exercised
        return ""

    def archive(self, sid: str) -> None:  # pragma: no cover
        pass


def _install_capturing_worker(monkeypatch, *, response_by_role: dict[str, str] | None = None):
    _CapturingWorker.instances = []
    canned = response_by_role or {}

    def factory(role: str) -> _CapturingWorker:
        return _CapturingWorker(role, response=canned.get(role, "noop\nTESTS_PASS"))

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)


# --------------------------- DB seeding helpers ---------------------------


def _seed_feature_with_unit(
    *,
    unit_id: str = "F-006-U-X",
    feature_id: str = "F-006",
    depends_on: list[str] | None = None,
    pr_number: int | None = 7,
    review_round: int = 0,
    repo_path: str = "https://github.com/o/r",
) -> None:
    """Create a feature + plan + post-coder unit state ready for tester/reviewer."""
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=repo_path,
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=unit_id,
                feature_id=feature_id,
                title="u",
                description="impl this",
                depends_on=depends_on or [],
            )
        ],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="in_ci",
            branch="feat/branch",
            pr_number=pr_number,
            coder_session_id="sesn-c",
            review_round=review_round,
        )
    )


def _write_predecessor_log(unit_id: str, body: str) -> Path:
    base = cycle_log.cycle_log_base_dir()
    path = cycle_log.cycle_log_path(unit_id, base_dir=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_own_log(unit_id: str, body: str) -> Path:
    return _write_predecessor_log(unit_id, body)


# --------------------------- 1. spawn_tester carries context ---------------------------


class TestSpawnTesterCallSiteWired:
    """The unit description: "Update every call site in
    orchestrator/tools/execution.py". spawn_tester is one of those call
    sites — it must thread `feature_spec_text` + `predecessor_logs` into
    `compose_tester_task` so the worker actually sees the new blocks."""

    def test_spec_block_appears_in_tester_task(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_with_unit(unit_id="F-006-U-X", feature_id="F-006")
        feature_spec.write_spec_if_missing("F-006", "spec title", "spec body intent")
        _install_capturing_worker(monkeypatch)
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-006", "F-006-U-X")

        assert "TESTS_PASS" in out
        assert _CapturingWorker.instances
        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## FEATURE SPEC" in task, "spawn_tester didn't inject ## FEATURE SPEC block"
        assert "spec body intent" in task

    def test_predecessor_block_appears_in_tester_task(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_with_unit(
            unit_id="F-006-U-X",
            feature_id="F-006",
            depends_on=["F-006-U-1"],
        )
        # A real cycle log on disk; the predecessor summarizer must read +
        # forward it into the task.
        _write_predecessor_log(
            "F-006-U-1",
            "# F-006-U-1 — title\n\n## PR\n#1\n\n## Cycle history\nPREDECESSOR-MARKER-XYZ\n",
        )
        _install_capturing_worker(monkeypatch)
        _stub_github(monkeypatch)

        execution.spawn_tester("F-006", "F-006-U-X")

        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## PREDECESSOR UNITS" in task
        assert "### F-006-U-1" in task
        assert "PREDECESSOR-MARKER-XYZ" in task

    def test_no_own_cycle_log_in_tester_task(self, tmp_state_db, with_github_token, monkeypatch):
        """Tester never gets `## THIS UNIT'S CYCLE LOG` (reviewer-only per
        proposal). Even if an own log exists on disk, the tester task
        must not include the block."""
        _seed_feature_with_unit(unit_id="F-006-U-X", feature_id="F-006")
        _write_own_log("F-006-U-X", "OWN-LOG-SHOULD-NOT-LEAK")
        _install_capturing_worker(monkeypatch)
        _stub_github(monkeypatch)

        execution.spawn_tester("F-006", "F-006-U-X")

        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## THIS UNIT'S CYCLE LOG" not in task
        assert "OWN-LOG-SHOULD-NOT-LEAK" not in task


# --------------------------- 2. spawn_reviewer review_round gate ---------------------------


class TestSpawnReviewerOwnLogGate:
    """The proposal: "## THIS UNIT'S CYCLE LOG — reviewer, retry cycle >= 2".

    Drives the gate by varying `unit_state.review_round` and inspects the
    actual task string. The coder's tests only checked the constant value
    of `REVIEWER_OWN_LOG_MIN_ROUND`; this checks observable behavior.
    """

    @pytest.mark.parametrize("review_round", [0, 1])
    def test_own_log_omitted_when_round_below_threshold(
        self, tmp_state_db, with_github_token, monkeypatch, review_round
    ):
        _seed_feature_with_unit(unit_id="F-006-U-X", feature_id="F-006", review_round=review_round)
        _write_own_log(
            "F-006-U-X",
            "# F-006-U-X\n\n## Cycle history\nFIRST-CYCLE-NOTES-MARKER\n",
        )
        _install_capturing_worker(monkeypatch, response_by_role={"reviewer": "REVIEW_COMMENT"})
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-006", "F-006-U-X")

        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## THIS UNIT'S CYCLE LOG" not in task, (
            f"reviewer at review_round={review_round} got own-log block before "
            "retry cycle >= 2 threshold"
        )
        assert "FIRST-CYCLE-NOTES-MARKER" not in task

    @pytest.mark.parametrize("review_round", [2, 3])
    def test_own_log_included_when_round_at_or_above_threshold(
        self, tmp_state_db, with_github_token, monkeypatch, review_round
    ):
        _seed_feature_with_unit(unit_id="F-006-U-X", feature_id="F-006", review_round=review_round)
        _write_own_log(
            "F-006-U-X",
            "# F-006-U-X\n\n## Cycle history\nFIRST-CYCLE-NOTES-MARKER\n",
        )
        _install_capturing_worker(monkeypatch, response_by_role={"reviewer": "REVIEW_COMMENT"})
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-006", "F-006-U-X")

        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## THIS UNIT'S CYCLE LOG" in task, (
            f"reviewer at review_round={review_round} missed own-log block at/above "
            "retry cycle >= 2 threshold"
        )
        assert "FIRST-CYCLE-NOTES-MARKER" in task

    def test_own_log_block_absent_when_round_meets_threshold_but_no_file(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Even past the threshold, if no own cycle log exists on disk
        (read-side graceful), the block is omitted rather than producing
        an empty heading. F-006's own in-flight units are exactly this
        case — the unit description calls it out."""
        _seed_feature_with_unit(unit_id="F-006-U-X", feature_id="F-006", review_round=3)
        # No _write_own_log call.
        _install_capturing_worker(monkeypatch, response_by_role={"reviewer": "REVIEW_COMMENT"})
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-006", "F-006-U-X")

        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## THIS UNIT'S CYCLE LOG" not in task


# --------------------------- 3. spawn_reviewer passes all kwargs ---------------------------


class TestSpawnReviewerCallSiteWired:
    """spawn_reviewer must inject feature spec + predecessor logs on EVERY
    review_round (the spec block is "always", not gated on retry). The
    own-cycle-log is the only retry-gated block."""

    def test_spec_block_on_first_review_round(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature_with_unit(unit_id="F-006-U-X", feature_id="F-006", review_round=0)
        feature_spec.write_spec_if_missing("F-006", "spec title", "REVIEWER-SPEC-CONTENT")
        _install_capturing_worker(monkeypatch, response_by_role={"reviewer": "REVIEW_COMMENT"})
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-006", "F-006-U-X")

        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## FEATURE SPEC" in task
        assert "REVIEWER-SPEC-CONTENT" in task

    def test_predecessor_block_on_first_review_round(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_with_unit(
            unit_id="F-006-U-X",
            feature_id="F-006",
            depends_on=["F-006-U-2"],
            review_round=0,
        )
        _write_predecessor_log(
            "F-006-U-2",
            "# F-006-U-2 — title\n\n## PR\n#2\n\n## Cycle history\nREVIEWER-PRED-MARKER\n",
        )
        _install_capturing_worker(monkeypatch, response_by_role={"reviewer": "REVIEW_COMMENT"})
        _stub_github(monkeypatch)

        execution.spawn_reviewer("F-006", "F-006-U-X")

        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## PREDECESSOR UNITS" in task
        assert "### F-006-U-2" in task
        assert "REVIEWER-PRED-MARKER" in task


# --------------------------- 4. read-side graceful: in-flight units ---------------------------


class TestInFlightUnitIsNoOp:
    """Unit description verbatim: "this is a no-op for this feature's own
    in-flight units — that's expected". Verify spawn_tester +
    spawn_reviewer complete normally with NO injection content when
    spec.md and predecessor cycle logs are missing."""

    def test_spawn_tester_no_injection_when_files_missing(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_with_unit(
            unit_id="F-006-U-4",
            feature_id="F-006",
            depends_on=["F-006-U-1", "F-006-U-2", "F-006-U-3"],
        )
        # Explicitly DO NOT write spec.md or any predecessor log.
        _install_capturing_worker(monkeypatch)
        _stub_github(monkeypatch)

        out = execution.spawn_tester("F-006", "F-006-U-4")

        # spawn_tester returns its normal result (no ERROR / no escalation).
        assert "ERROR" not in out
        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## FEATURE SPEC" not in task
        assert "## PREDECESSOR UNITS" not in task
        assert "## THIS UNIT'S CYCLE LOG" not in task
        # Sanity: unit_id still rendered (the bones of the task message).
        assert "F-006-U-4" in task

    def test_spawn_reviewer_no_injection_when_files_missing(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature_with_unit(
            unit_id="F-006-U-4",
            feature_id="F-006",
            depends_on=["F-006-U-1", "F-006-U-2"],
            review_round=3,  # past the own-log threshold too
        )
        _install_capturing_worker(monkeypatch, response_by_role={"reviewer": "REVIEW_COMMENT"})
        _stub_github(monkeypatch)

        out = execution.spawn_reviewer("F-006", "F-006-U-4")

        assert "ERROR" not in out
        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## FEATURE SPEC" not in task
        assert "## PREDECESSOR UNITS" not in task
        assert "## THIS UNIT'S CYCLE LOG" not in task


# --------------------------- 5. structure / ordering invariants ---------------------------


class TestRenderedStructure:
    """Granular ordering / delimiter checks not covered by the coder's
    structural tests."""

    def _feature(self) -> Feature:
        return Feature(
            id="F-006",
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            branch_prefix="feat/F-006-spec-cycle-logs",
        )

    def _unit(self) -> WorkUnit:
        return WorkUnit(
            id="F-006-U-4",
            feature_id="F-006",
            title="u",
            description="ud",
            depends_on=["F-006-U-1"],
        )

    def test_tester_context_lands_between_feature_context_and_marker_instruction(self):
        body = compose_tester_task(
            self._feature(),
            self._unit(),
            "branch",
            7,
            "tok",
            feature_spec_text="SPEC",
        )
        # ordering: FEATURE CONTEXT → ## FEATURE SPEC → "End with EXACTLY ONE of"
        i_ctx = body.index("FEATURE CONTEXT")
        i_spec = body.index("## FEATURE SPEC")
        i_end = body.index("End with EXACTLY ONE of")
        assert i_ctx < i_spec < i_end

    def test_reviewer_context_lands_between_feature_context_and_marker_instruction(self):
        body = compose_reviewer_task(
            self._feature(),
            self._unit(),
            7,
            "tok",
            feature_spec_text="SPEC",
        )
        i_ctx = body.index("FEATURE CONTEXT")
        i_spec = body.index("## FEATURE SPEC")
        i_end = body.index("Post the review as")
        assert i_ctx < i_spec < i_end

    def test_predecessor_subheadings_in_dependency_order(self):
        """Multiple predecessors render in the order the caller supplied
        (which is `unit.depends_on` order — preserving dep ordering keeps
        the worker's reading order deterministic)."""
        body = compose_coder_task(
            self._feature(),
            self._unit(),
            "b",
            "tok",
            predecessor_logs=[
                ("F-006-U-A", "first"),
                ("F-006-U-B", "second"),
                ("F-006-U-C", "third"),
            ],
        )
        i_a = body.index("### F-006-U-A")
        i_b = body.index("### F-006-U-B")
        i_c = body.index("### F-006-U-C")
        assert i_a < i_b < i_c

    def test_blocks_separated_by_blank_line(self):
        """Spec + predecessor + own_cycle_log render with `\\n\\n` between
        them so each is parseable as a standalone markdown section."""
        body = compose_reviewer_task(
            self._feature(),
            self._unit(),
            7,
            "tok",
            feature_spec_text="SPEC",
            predecessor_logs=[("F-006-U-1", "PRED")],
            own_cycle_log="OWN",
        )
        # Each successive top-level heading must be preceded by a blank line.
        assert "\n\n## PREDECESSOR UNITS" in body
        assert "\n\n## THIS UNIT'S CYCLE LOG" in body


# --------------------------- 6. _task_context_kwargs cross-feature isolation ---------------------------


class TestTaskContextKwargsIsolation:
    """Tightens the cross-feature isolation guarantee: a feature's
    _task_context_kwargs reads only THAT feature's spec.md, not a
    sibling's."""

    def test_reads_only_target_features_spec(self, tmp_state_db):
        feature_spec.write_spec_if_missing("F-006", "f6 title", "F006-SPEC-BODY")
        feature_spec.write_spec_if_missing("F-007", "f7 title", "F007-SPEC-BODY")
        f = Feature(id="F-006", title="t", description="d")
        u = WorkUnit(id="F-006-U-1", feature_id="F-006", title="u", description="ud")

        kw = execution._task_context_kwargs(f, u)
        assert "F006-SPEC-BODY" in kw["feature_spec_text"]
        assert "F007-SPEC-BODY" not in kw["feature_spec_text"]

    def test_only_declared_deps_appear_in_predecessor_logs(self, tmp_state_db):
        """A unit with no `depends_on` gets an empty predecessor list,
        even if other units in the same feature have cycle logs."""
        state.save_feature(
            Feature(id="F-006", title="t", description="d", repo_path="https://github.com/o/r")
        )
        _write_predecessor_log(
            "F-006-U-1",
            "# F-006-U-1\n\n## PR\n#1\n\n## Cycle history\nIRRELEVANT\n",
        )
        f = Feature(id="F-006", title="t", description="d")
        u_no_deps = WorkUnit(id="F-006-U-X", feature_id="F-006", title="u", description="ud")

        kw = execution._task_context_kwargs(f, u_no_deps)
        assert kw["predecessor_logs"] == []


# --------------------------- 7. summary stripping correctness ---------------------------


class TestCycleLogSummaryShape:
    """Verifies the proposal's ~500-token budget intent: cycle_log_summary
    drops the PR description (the largest block) but preserves the actual
    decision artefacts a downstream unit needs."""

    def test_pr_description_block_dropped_but_subsequent_blocks_preserved(self, tmp_state_db):
        state.save_feature(
            Feature(id="F-006", title="t", description="d", repo_path="https://github.com/o/r")
        )
        body = (
            "# F-006-U-1 — title\n"
            "\n"
            "## PR\n#1\n"
            "\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "LONG-PR-BODY-DO-NOT-LEAK\n"
            "more PR body text\n"
            "\n"
            "## Cycle history\nKEEP-THIS\n"
            "\n"
            "## Review threads\nALSO-KEEP-THIS\n"
        )
        _write_predecessor_log("F-006-U-1", body)

        summary = cycle_log.cycle_log_summary("F-006-U-1")
        assert "LONG-PR-BODY-DO-NOT-LEAK" not in summary
        assert "more PR body text" not in summary
        assert "KEEP-THIS" in summary
        assert "ALSO-KEEP-THIS" in summary
        # The two subsequent headings retain their `##` level (not folded).
        assert "## Cycle history" in summary
        assert "## Review threads" in summary

    def test_summary_used_by_predecessor_summaries_helper(self, tmp_state_db):
        """End-to-end: a verbose PR description on a predecessor log
        doesn't leak into the rendered ## PREDECESSOR UNITS block."""
        state.save_feature(
            Feature(id="F-006", title="t", description="d", repo_path="https://github.com/o/r")
        )
        body = (
            "# F-006-U-1\n\n"
            "## PR\n#1\n\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "VERBOSE-PR-BODY-MUST-NOT-APPEAR\n\n"
            "## Cycle history\nactual-decision-marker\n"
        )
        _write_predecessor_log("F-006-U-1", body)
        u = WorkUnit(
            id="F-006-U-X",
            feature_id="F-006",
            title="u",
            description="ud",
            depends_on=["F-006-U-1"],
        )
        pairs = execution._predecessor_summaries(u)
        assert len(pairs) == 1
        dep_id, summary = pairs[0]
        assert dep_id == "F-006-U-1"
        assert "VERBOSE-PR-BODY-MUST-NOT-APPEAR" not in summary
        assert "actual-decision-marker" in summary


# --------------------------- 8. backward-compat: old call shape still works ---------------------------


class TestBackwardsCompatNoKwargs:
    """Existing test code (e.g. tests/test_tools_shared.py) calls
    compose_*_task with the pre-U-4 positional signature. Those calls
    must still work and produce a body with the canonical structural
    markers — otherwise unrelated tests would have to be rewritten."""

    def test_coder_old_signature_still_renders_required_fields(self):
        f = Feature(
            id="F-001",
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            branch_prefix="feat/F-001",
        )
        u = WorkUnit(id="F-001-U-1", feature_id="F-001", title="u", description="ud")
        body = compose_coder_task(f, u, "feat/F-001-u-1", "tok")
        assert "F-001-U-1" in body
        assert "feat/F-001-u-1" in body
        assert "tok" in body
        assert "PR_URL: <url>" in body

    def test_tester_old_signature_still_renders_required_fields(self):
        f = Feature(id="F-001", title="t", description="d", repo_path="https://github.com/o/r")
        u = WorkUnit(id="F-001-U-1", feature_id="F-001", title="u", description="ud")
        body = compose_tester_task(f, u, "feat/branch", 99, "tok")
        assert "PR_NUMBER: 99" in body
        assert "TESTS_PASS" in body

    def test_reviewer_old_signature_still_renders_required_fields(self):
        f = Feature(id="F-001", title="t", description="d", repo_path="https://github.com/o/r")
        u = WorkUnit(id="F-001-U-1", feature_id="F-001", title="u", description="ud")
        body = compose_reviewer_task(f, u, 99, "tok")
        assert "PR #99" in body
        assert "REVIEW_RECOMMEND_MERGE" in body


# --------------------------- 9. captured task fields ---------------------------


class TestSpawnUnitDoesNotRequireSpec:
    """spawn_unit (coder) was tested by the coder's tests; this is a
    light double-check that the call site is wired symmetrically with
    spawn_tester / spawn_reviewer for the FEATURE SPEC block (the proposal
    says "always" — i.e. on the coder's first turn too, not just tester /
    reviewer)."""

    def test_spawn_unit_injects_feature_spec(self, tmp_state_db, with_github_token, monkeypatch):
        state.save_feature(
            Feature(
                id="F-006",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan(
            "F-006",
            [WorkUnit(id="F-006-U-X", feature_id="F-006", title="u", description="ud")],
        )
        state.approve_plan("F-006")
        feature_spec.write_spec_if_missing("F-006", "spec t", "CODER-SPEC-CONTENT")

        _install_capturing_worker(
            monkeypatch,
            response_by_role={"coder": "PR_URL: https://github.com/o/r/pull/1"},
        )
        _stub_github(monkeypatch)
        # spawn_unit looks up github state — make those benign.
        monkeypatch.setattr(
            "orchestrator.tools.execution.github.parse_repo_url",
            lambda url: ("o", "r"),
        )
        monkeypatch.setattr(
            "orchestrator.tools.execution.github.get_pr_state",
            lambda *a, **k: {"head_sha": "deadbeef", "state": "open", "merged": False},
        )

        out = execution.spawn_unit("F-006", "F-006-U-X")
        # Either JSON success or some non-error path — what matters is the
        # task message contains the FEATURE SPEC injection.
        assert _CapturingWorker.instances
        task = _CapturingWorker.instances[0].spawn_tasks[0]
        assert "## FEATURE SPEC" in task
        assert "CODER-SPEC-CONTENT" in task
        # spawn_unit's happy-path return is JSON; sanity-parse if so.
        try:
            parsed = json.loads(out)
            assert parsed.get("pr_number") == 1
        except (ValueError, TypeError):
            # Non-JSON returns happen on escalation; the injection still
            # mattered above, so don't fail this test on the return shape.
            pass
