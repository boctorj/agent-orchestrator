"""Spec-compliance tests for F-006-U-4 — compose_*_task injections.

Locks in the contract from the unit description verbatim:

  > Update compose_coder_task / compose_tester_task / compose_reviewer_task
  > signatures to accept the feature spec text, predecessor cycle-log
  > summaries, and this unit's own cycle log. Render three context blocks
  > per the proposal's table: ## FEATURE SPEC always; ## PREDECESSOR UNITS
  > when deps exist; ## THIS UNIT'S CYCLE LOG for the reviewer on retry
  > cycle >= 2. Read-side gracefully handles missing files (so this is a
  > no-op for this feature's own in-flight units — that's expected).
  > Update every call site in orchestrator/tools/execution.py and every
  > test that constructs a task message.

Coverage by section below:

  1. compose_*_task signatures — kwargs exist and default to no-injection.
  2. ## FEATURE SPEC block — rendered when feature_spec_text is non-empty;
     omitted when empty; appears in coder + tester + reviewer.
  3. ## PREDECESSOR UNITS block — rendered per declared dep with a non-
     empty summary; omitted entirely when no deps OR all summaries empty;
     individual empty summaries are dropped.
  4. ## THIS UNIT'S CYCLE LOG block — reviewer only; rendered when
     own_cycle_log is non-empty; absent from coder / tester signatures.
  5. Read-side null-safety — execution.py call sites read spec.md +
     predecessor cycle logs from disk and gracefully handle missing files
     (F-006's own in-flight units inject nothing, not raise).
  6. Execution.py wiring — every call site passes the new kwargs;
     reviewer gates own_cycle_log on review_round >= 2.
  7. cycle_log_summary — drops the verbatim "## Coder's PR description"
     block to honour the proposal's ~500-token-per-predecessor budget.
"""

from __future__ import annotations

import pytest

from orchestrator import cycle_log, feature_spec, state
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import (
    _render_context_blocks,  # noqa: PLC2701 — exercised below as a helper
    compose_coder_task,
    compose_reviewer_task,
    compose_tester_task,
    execution,
)

# Convenient fixtures reused across multiple tests below. Kept local to
# this module — the U-1 file's fixtures don't apply here (they assume the
# pre-wiring world).


@pytest.fixture
def feature() -> Feature:
    return Feature(
        id="F-006",
        title="spec + cycle logs",
        description="Phase 1 of the proposal.",
        repo_path="https://github.com/o/r",
        branch_prefix="feat/F-006-spec-cycle-logs",
    )


@pytest.fixture
def unit() -> WorkUnit:
    return WorkUnit(
        id="F-006-U-4",
        feature_id="F-006",
        title="compose_*_task injections",
        description="Wire FEATURE SPEC + PREDECESSOR UNITS into worker task messages.",
        depends_on=["F-006-U-1", "F-006-U-2"],
    )


# --------------------------- signature / default no-op ---------------------------


class TestSignatureDefaults:
    """All three compose_*_task helpers must accept the new kwargs and
    must produce empty-context output when called with their defaults.
    Backwards compatibility for any caller that hasn't been updated yet,
    plus the graceful-missing-file path."""

    def test_coder_no_kwargs_omits_all_blocks(self, feature, unit):
        body = compose_coder_task(feature, unit, "branch", "tok")
        assert "## FEATURE SPEC" not in body
        assert "## PREDECESSOR UNITS" not in body
        assert "## THIS UNIT'S CYCLE LOG" not in body

    def test_tester_no_kwargs_omits_all_blocks(self, feature, unit):
        body = compose_tester_task(feature, unit, "branch", 1, "tok")
        assert "## FEATURE SPEC" not in body
        assert "## PREDECESSOR UNITS" not in body
        assert "## THIS UNIT'S CYCLE LOG" not in body

    def test_reviewer_no_kwargs_omits_all_blocks(self, feature, unit):
        body = compose_reviewer_task(feature, unit, 1, "tok")
        assert "## FEATURE SPEC" not in body
        assert "## PREDECESSOR UNITS" not in body
        assert "## THIS UNIT'S CYCLE LOG" not in body

    def test_kwargs_are_keyword_only(self, feature, unit):
        """The new kwargs must be keyword-only — preserves positional
        backward compatibility for existing call sites (e.g. test code
        that passes branch/token positionally)."""
        with pytest.raises(TypeError):
            compose_coder_task(feature, unit, "b", "tok", "spec text")  # type: ignore[misc]

    def test_reviewer_accepts_own_cycle_log_kwarg(self, feature, unit):
        body = compose_reviewer_task(feature, unit, 1, "tok", own_cycle_log="LOG")
        assert "## THIS UNIT'S CYCLE LOG" in body
        assert "LOG" in body

    def test_coder_rejects_own_cycle_log_kwarg(self, feature, unit):
        """own_cycle_log is reviewer-only per the proposal table."""
        with pytest.raises(TypeError):
            compose_coder_task(feature, unit, "b", "tok", own_cycle_log="x")  # type: ignore[call-arg]

    def test_tester_rejects_own_cycle_log_kwarg(self, feature, unit):
        with pytest.raises(TypeError):
            compose_tester_task(feature, unit, "b", 1, "tok", own_cycle_log="x")  # type: ignore[call-arg]


# --------------------------- ## FEATURE SPEC block ---------------------------


class TestFeatureSpecBlock:
    def test_renders_in_coder_task(self, feature, unit):
        body = compose_coder_task(
            feature,
            unit,
            "b",
            "tok",
            feature_spec_text="# F-006: spec\n\n## Intent\nFoo.\n",
        )
        assert "## FEATURE SPEC" in body
        assert "## Intent" in body
        assert "Foo." in body

    def test_renders_in_tester_task(self, feature, unit):
        body = compose_tester_task(
            feature, unit, "b", 1, "tok", feature_spec_text="SPEC TEXT MARKER"
        )
        assert "## FEATURE SPEC" in body
        assert "SPEC TEXT MARKER" in body

    def test_renders_in_reviewer_task(self, feature, unit):
        body = compose_reviewer_task(feature, unit, 1, "tok", feature_spec_text="SPEC TEXT MARKER")
        assert "## FEATURE SPEC" in body
        assert "SPEC TEXT MARKER" in body

    def test_blank_text_omits_block(self, feature, unit):
        body = compose_coder_task(feature, unit, "b", "tok", feature_spec_text="   \n\n  ")
        assert "## FEATURE SPEC" not in body

    def test_appears_after_feature_context(self, feature, unit):
        """Blocks render AFTER the "FEATURE CONTEXT:" stanza so the
        agent has the basic task framing before the richer context."""
        body = compose_coder_task(feature, unit, "b", "tok", feature_spec_text="SPEC TEXT MARKER")
        assert body.index("FEATURE CONTEXT") < body.index("## FEATURE SPEC")
        # ...and before the closing instructions.
        assert body.index("## FEATURE SPEC") < body.index("Follow your standard workflow")


# --------------------------- ## PREDECESSOR UNITS block ---------------------------


class TestPredecessorUnitsBlock:
    def test_renders_each_dep_under_sub_heading(self, feature, unit):
        body = compose_coder_task(
            feature,
            unit,
            "b",
            "tok",
            predecessor_logs=[("F-006-U-1", "U-1 summary"), ("F-006-U-2", "U-2 summary")],
        )
        assert "## PREDECESSOR UNITS" in body
        assert "### F-006-U-1" in body
        assert "U-1 summary" in body
        assert "### F-006-U-2" in body
        assert "U-2 summary" in body

    def test_omits_when_no_deps(self, feature, unit):
        body = compose_coder_task(feature, unit, "b", "tok", predecessor_logs=[])
        assert "## PREDECESSOR UNITS" not in body

    def test_omits_when_all_summaries_empty(self, feature, unit):
        """An in-flight predecessor whose cycle log doesn't exist yet
        contributes an empty summary; if every declared dep is in that
        state, no PREDECESSOR UNITS block should render at all."""
        body = compose_coder_task(
            feature,
            unit,
            "b",
            "tok",
            predecessor_logs=[("F-006-U-1", ""), ("F-006-U-2", "")],
        )
        assert "## PREDECESSOR UNITS" not in body
        assert "F-006-U-1" not in body  # not even as a subheading

    def test_drops_individual_empty_summary(self, feature, unit):
        body = compose_coder_task(
            feature,
            unit,
            "b",
            "tok",
            predecessor_logs=[
                ("F-006-U-1", "real summary"),
                ("F-006-U-2", ""),  # in-flight predecessor, no log yet
            ],
        )
        assert "## PREDECESSOR UNITS" in body
        assert "### F-006-U-1" in body
        assert "real summary" in body
        assert "### F-006-U-2" not in body

    def test_renders_in_tester_task(self, feature, unit):
        body = compose_tester_task(
            feature,
            unit,
            "b",
            1,
            "tok",
            predecessor_logs=[("F-006-U-1", "summary")],
        )
        assert "## PREDECESSOR UNITS" in body

    def test_renders_in_reviewer_task(self, feature, unit):
        body = compose_reviewer_task(
            feature, unit, 1, "tok", predecessor_logs=[("F-006-U-1", "summary")]
        )
        assert "## PREDECESSOR UNITS" in body


# --------------------------- ## THIS UNIT'S CYCLE LOG block ---------------------------


class TestOwnCycleLogBlock:
    def test_reviewer_renders_when_log_provided(self, feature, unit):
        body = compose_reviewer_task(feature, unit, 1, "tok", own_cycle_log="OWN-LOG-MARKER")
        assert "## THIS UNIT'S CYCLE LOG" in body
        assert "OWN-LOG-MARKER" in body

    def test_reviewer_omits_when_log_blank(self, feature, unit):
        body = compose_reviewer_task(feature, unit, 1, "tok", own_cycle_log="   ")
        assert "## THIS UNIT'S CYCLE LOG" not in body

    def test_reviewer_block_after_predecessors(self, feature, unit):
        """Ordering: FEATURE SPEC → PREDECESSOR UNITS → THIS UNIT'S CYCLE LOG."""
        body = compose_reviewer_task(
            feature,
            unit,
            1,
            "tok",
            feature_spec_text="SPEC",
            predecessor_logs=[("F-006-U-1", "PRED")],
            own_cycle_log="OWN",
        )
        i_spec = body.index("## FEATURE SPEC")
        i_pred = body.index("## PREDECESSOR UNITS")
        i_own = body.index("## THIS UNIT'S CYCLE LOG")
        assert i_spec < i_pred < i_own


# --------------------------- _render_context_blocks (helper) ---------------------------


class TestRenderContextBlocksHelper:
    def test_empty_inputs_yields_empty_string(self):
        assert _render_context_blocks() == ""

    def test_only_spec(self):
        out = _render_context_blocks(feature_spec_text="X")
        assert "## FEATURE SPEC" in out
        assert "## PREDECESSOR UNITS" not in out
        assert "## THIS UNIT'S CYCLE LOG" not in out

    def test_only_predecessor(self):
        out = _render_context_blocks(predecessor_logs=[("U-1", "summary")])
        assert "## FEATURE SPEC" not in out
        assert "## PREDECESSOR UNITS" in out
        assert "### U-1" in out

    def test_only_own_log(self):
        out = _render_context_blocks(own_cycle_log="X")
        assert "## THIS UNIT'S CYCLE LOG" in out

    def test_all_three_blocks_separated_by_blank_line(self):
        out = _render_context_blocks(
            feature_spec_text="SPEC",
            predecessor_logs=[("U-1", "PRED")],
            own_cycle_log="OWN",
        )
        # blocks are separated by "\n\n" so the rendered string parses
        # as three distinct markdown sections, not one giant block.
        assert "\n\n## PREDECESSOR UNITS" in out
        assert "\n\n## THIS UNIT'S CYCLE LOG" in out

    def test_predecessor_h1_stripped_to_avoid_outranking_wrapper(self):
        """PR #44 N2 finding: a predecessor summary that starts with
        ``# F-XXX-U-N — title`` (h1) would out-rank the ``### <uid>``
        wrapper (h3). The renderer strips the leading H1 before wrapping
        so the embedded headings sit cleanly under the wrapper."""
        summary_with_h1 = "# F-006-U-1 — title\n\n## Cycle history\nfoo\n"
        out = _render_context_blocks(predecessor_logs=[("F-006-U-1", summary_with_h1)])
        assert "### F-006-U-1" in out
        # The H1 line itself must be gone — the content nests under the
        # wrapper instead of competing with it at the document level.
        assert "# F-006-U-1 — title" not in out

    def test_own_log_h1_stripped_to_avoid_outranking_wrapper(self):
        """Same H1-strip rule applies to the reviewer's own-cycle-log
        block: the H1 in a freshly-read cycle log out-ranks the
        ``## THIS UNIT'S CYCLE LOG`` h2 wrapper."""
        own_with_h1 = "# F-006-U-X — title\n\n## Cycle history\nfoo\n"
        out = _render_context_blocks(own_cycle_log=own_with_h1)
        assert "## THIS UNIT'S CYCLE LOG" in out
        assert "# F-006-U-X — title" not in out

    def test_ends_with_exactly_one_blank_line_after_content(self):
        """The trailing newlines must produce *one* blank line between
        the last block and the next thing the caller appends (typically
        the closing "Follow your standard workflow..." instruction).
        A double-blank-line indicates the renderer's per-block trailing
        ``\\n`` is doubling up with the join's ``\\n\\n`` — PR #44 Copilot
        finding."""
        out = _render_context_blocks(feature_spec_text="spec body")
        # The string ends with exactly two newlines (one blank line) —
        # not three (would be a double-blank glitch).
        assert out.endswith("\n\n")
        assert not out.endswith("\n\n\n")

    def test_no_extra_blank_line_in_compose_output(self):
        """Composing with a non-empty context produces a single blank line
        between the inserted block and the closing instruction, matching
        the empty-context case."""
        from orchestrator.models import Feature, WorkUnit

        f = Feature(id="F-006", title="t", description="d")
        u = WorkUnit(id="F-006-U-1", feature_id="F-006", title="u", description="ud")
        with_ctx = compose_coder_task(f, u, "b", "tok", feature_spec_text="spec body")
        # Exactly one blank line ("spec body" + "\n\n" + "Follow ...") —
        # NOT two ("spec body" + "\n\n\n" + "Follow ...").
        assert "spec body\n\nFollow your standard workflow" in with_ctx
        assert "spec body\n\n\nFollow your standard workflow" not in with_ctx


# --------------------------- read-side: feature_spec.read_spec ---------------------------


class TestReadSpec:
    def test_returns_empty_when_file_missing(self, tmp_state_db):
        assert feature_spec.read_spec("F-001") == ""

    def test_returns_content_when_present(self, tmp_state_db):
        feature_spec.write_spec_if_missing("F-001", "t", "d")
        out = feature_spec.read_spec("F-001")
        assert out
        assert "# F-001: t" in out


# --------------------------- read-side: cycle_log readers ---------------------------


class TestCycleLogReaders:
    def _seed(self, tmp_state_db, *, with_log: bool = False, body: str = "") -> None:
        state.save_feature(
            Feature(id="F-006", title="t", description="d", repo_path="https://github.com/o/r")
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-006-U-1", feature_id="F-006", status="done")
        )
        if with_log:
            # Anchor fixture writes on the same root the readers default
            # to (cycle_log_base_dir, which the tmp_state_db fixture
            # monkeypatches via state.STATE_DB.parent).
            path = cycle_log.cycle_log_path("F-006-U-1", base_dir=cycle_log.cycle_log_base_dir())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def test_read_cycle_log_empty_when_missing(self, tmp_state_db):
        self._seed(tmp_state_db)
        assert cycle_log.read_cycle_log("F-006-U-1") == ""

    def test_read_cycle_log_returns_full_text(self, tmp_state_db):
        body = "# F-006-U-1\n\n## PR\n#1\n"
        self._seed(tmp_state_db, with_log=True, body=body)
        assert cycle_log.read_cycle_log("F-006-U-1") == body

    def test_cycle_log_summary_empty_when_missing(self, tmp_state_db):
        self._seed(tmp_state_db)
        assert cycle_log.cycle_log_summary("F-006-U-1") == ""

    def test_cycle_log_summary_drops_pr_description_block(self, tmp_state_db):
        # Renderer emits "## Coder's PR description (verbatim, ...)" — the
        # summary path must strip that block (it's the largest section and
        # not actionable for downstream-unit task messages).
        body = (
            "# F-006-U-1 — title\n\n"
            "## PR\n#1\n\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "Long PR body with lots of tokens.\n\n"
            "## Cycle history\n"
            "1 cycles · cap-3 not hit\n\n"
            "## Review threads\n"
            "_no review threads_\n"
        )
        self._seed(tmp_state_db, with_log=True, body=body)
        summary = cycle_log.cycle_log_summary("F-006-U-1")
        assert summary
        assert "## PR" in summary
        assert "## Cycle history" in summary
        assert "## Review threads" in summary
        # The verbatim PR body must NOT survive.
        assert "Long PR body with lots of tokens." not in summary
        assert "## Coder's PR description" not in summary

    def test_cycle_log_summary_passthrough_when_no_pr_description(self, tmp_state_db):
        """If a log already lacks a PR-description block (renderer drift /
        truncated regenerate), the summary returns the body as-is."""
        body = "# F-006-U-1\n\n## PR\n#1\n\n## Cycle history\n1 cycles\n"
        self._seed(tmp_state_db, with_log=True, body=body)
        assert cycle_log.cycle_log_summary("F-006-U-1") == body

    def test_cycle_log_summary_strips_pr_body_with_inner_h2_headings(self, tmp_state_db):
        """PR #44 H1 regression: the coder prompt mandates ``## What this
        change does`` / ``## Manual verification needed`` / ``## Decisions``
        inside the PR body, so the verbatim PR description block legitimately
        contains its own ``## `` headings. A naive ``find('\\n## ')`` stripper
        would stop at the first inner heading and leak the rest of the PR
        body into the summary, blowing the proposal's ~500-token-per-
        predecessor budget. cycle_log_summary must anchor on the next
        cycle-log SECTION heading (Cycle history / Review threads / ...),
        not just any ``## `` line."""
        body = (
            "# F-006-U-1 — title\n\n"
            "## PR\n#1\n\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "**Unit ID:** F-006-U-1\n\n"
            "## What this change does\n"
            "PR-BODY-WHAT-MARKER-MUST-BE-STRIPPED\n\n"
            "## Manual verification needed\n"
            "PR-BODY-MANUAL-MARKER-MUST-BE-STRIPPED\n\n"
            "## Decisions/deviations from the unit description\n"
            "PR-BODY-DECISIONS-MARKER-MUST-BE-STRIPPED\n\n"
            "## Cycle history\nKEEP-CYCLE-HISTORY\n\n"
            "## Review threads\nKEEP-REVIEW-THREADS\n"
        )
        self._seed(tmp_state_db, with_log=True, body=body)
        summary = cycle_log.cycle_log_summary("F-006-U-1")
        # Every PR-body marker MUST be absent — the stripper has to skip past
        # all inner `## ` headings to land on the real cycle-log section.
        assert "PR-BODY-WHAT-MARKER-MUST-BE-STRIPPED" not in summary
        assert "PR-BODY-MANUAL-MARKER-MUST-BE-STRIPPED" not in summary
        assert "PR-BODY-DECISIONS-MARKER-MUST-BE-STRIPPED" not in summary
        assert "## What this change does" not in summary
        assert "## Manual verification needed" not in summary
        # Cycle-log sections after the PR body must survive.
        assert "## Cycle history" in summary
        assert "KEEP-CYCLE-HISTORY" in summary
        assert "## Review threads" in summary
        assert "KEEP-REVIEW-THREADS" in summary

    def test_cycle_log_summary_uses_exported_renderer_heading_constant(self):
        """The summary stripper must reference the renderer's exported
        ``PR_DESCRIPTION_HEADING`` constant rather than re-deriving it from
        a prefix. If the renderer ever changes the heading wording, the
        import breaks loudly instead of degrading to a no-op stripper."""
        from orchestrator.cycle_log_render import PR_DESCRIPTION_HEADING

        # Must be the literal heading the renderer emits — full string,
        # not a prefix.
        assert PR_DESCRIPTION_HEADING == "## Coder's PR description (verbatim, as of last capture)"


# --------------------------- execution.py call sites ---------------------------


class TestExecutionTaskContextKwargs:
    """The _task_context_kwargs helper reads spec.md + predecessor cycle
    logs and produces the kwargs every compose_*_task call site passes."""

    def test_reads_spec_when_present(self, tmp_state_db):
        feature_spec.write_spec_if_missing("F-006", "t", "d")
        f = Feature(id="F-006", title="t", description="d")
        u = WorkUnit(id="F-006-U-1", feature_id="F-006", title="u", description="ud")
        kw = execution._task_context_kwargs(f, u)
        assert "## Intent" in kw["feature_spec_text"]
        assert kw["predecessor_logs"] == []

    def test_empty_spec_when_missing(self, tmp_state_db):
        f = Feature(id="F-006", title="t", description="d")
        u = WorkUnit(id="F-006-U-1", feature_id="F-006", title="u", description="ud")
        kw = execution._task_context_kwargs(f, u)
        assert kw["feature_spec_text"] == ""
        assert kw["predecessor_logs"] == []

    def test_predecessor_summaries_for_declared_deps(self, tmp_state_db):
        state.save_feature(
            Feature(id="F-006", title="t", description="d", repo_path="https://github.com/o/r")
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-006-U-1", feature_id="F-006", status="done")
        )
        body = (
            "# F-006-U-1\n\n## PR\n#1\n\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "verbose body\n\n"
            "## Cycle history\n1 cycles · cap-3 not hit\n"
        )
        path = cycle_log.cycle_log_path("F-006-U-1", base_dir=cycle_log.cycle_log_base_dir())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

        f = Feature(id="F-006", title="t", description="d")
        u = WorkUnit(
            id="F-006-U-4",
            feature_id="F-006",
            title="u",
            description="ud",
            depends_on=["F-006-U-1"],
        )
        kw = execution._task_context_kwargs(f, u)
        assert kw["predecessor_logs"]
        dep_id, summary = kw["predecessor_logs"][0]
        assert dep_id == "F-006-U-1"
        assert "## Cycle history" in summary
        assert "verbose body" not in summary  # PR description stripped

    def test_in_flight_predecessor_yields_empty_summary(self, tmp_state_db):
        """F-006's own units have no cycle logs yet during their flight —
        the unit description explicitly calls this out as expected."""
        f = Feature(id="F-006", title="t", description="d")
        u = WorkUnit(
            id="F-006-U-4",
            feature_id="F-006",
            title="u",
            description="ud",
            depends_on=["F-006-U-1", "F-006-U-2"],
        )
        kw = execution._task_context_kwargs(f, u)
        assert kw["predecessor_logs"] == [("F-006-U-1", ""), ("F-006-U-2", "")]


class TestExecutionReviewerOwnLogGate:
    """spawn_reviewer injects own_cycle_log only on retry cycle >= 2.

    Verifies the proposal's "reviewer, retry cycle >= 2" trigger — earlier
    cycles' compose_reviewer_task call sites must NOT pass own_cycle_log,
    even if a cycle log happens to exist on disk."""

    def test_constant_matches_proposal(self):
        # Defensive: if someone changes the threshold, this test fails
        # loud rather than silently regressing the proposal contract.
        assert execution.REVIEWER_OWN_LOG_MIN_ROUND == 2


# --------------------------- end-to-end through spawn_unit ---------------------------


class _FakeWorker:
    def __init__(self, role: str):
        self.role = role
        self.tasks: list[str] = []

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        self.tasks.append(task)
        return f"sesn-{self.role}", "PR_URL: https://github.com/o/r/pull/1"

    def resume(self, sid: str, msg: str) -> str:  # pragma: no cover - not exercised here
        return ""


class TestSpawnUnitTaskComposition:
    """spawn_unit's compose_coder_task call must include the new kwargs.

    Exercises the actual MCP entry point so a regression that drops the
    kwargs anywhere in the chain (helper, splat, signature) fails loudly."""

    def _setup_feature(self) -> None:
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
            [
                WorkUnit(
                    id="F-006-U-4",
                    feature_id="F-006",
                    title="u",
                    description="ud",
                    depends_on=["F-006-U-1"],
                )
            ],
        )
        state.approve_plan("F-006")

    def test_coder_task_includes_spec_when_present(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        self._setup_feature()
        feature_spec.write_spec_if_missing("F-006", "spec title", "spec body")

        captured: list[_FakeWorker] = []

        def factory(role: str) -> _FakeWorker:
            w = _FakeWorker(role)
            captured.append(w)
            return w

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
        monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")

        execution.spawn_unit("F-006", "F-006-U-4")

        assert captured
        task = captured[0].tasks[0]
        assert "## FEATURE SPEC" in task
        assert "spec body" in task

    def test_coder_task_no_spec_when_missing(self, tmp_state_db, with_github_token, monkeypatch):
        """Read-side null-safety — no spec.md on disk means no block,
        not an exception."""
        self._setup_feature()

        captured: list[_FakeWorker] = []

        def factory(role: str) -> _FakeWorker:
            w = _FakeWorker(role)
            captured.append(w)
            return w

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
        monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")

        execution.spawn_unit("F-006", "F-006-U-4")

        assert captured
        task = captured[0].tasks[0]
        assert "## FEATURE SPEC" not in task
        # And no predecessor block since the dep's cycle log is missing.
        assert "## PREDECESSOR UNITS" not in task
