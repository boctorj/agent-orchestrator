"""F-017-U-1: TL;DR section in cycle-log writer + coder prompt.

Three pin-tests, mapping to the unit description's verification points:

  1. Format pinning — ``render_cycle_log`` emits ``## PR`` →
     ``## TL;DR`` → ``## Coder's PR description`` in that order, and
     the TL;DR block carries the three canonical sub-headings spelled
     exactly. Missing sub-sections in the coder PR body fall back to
     the ``_TBD — coder did not fill in this sub-section._`` placeholder.
  2. ``cycle_log_summary`` semantics preserved — because ``## TL;DR``
     sits *above* the existing strip boundary, the returned summary
     contains the TL;DR block verbatim with no caller-side change.
  3. End-to-end — ``compose_coder_task`` (and the sibling tester /
     reviewer composers) renders a ``## PREDECESSOR UNITS`` block that
     contains the TL;DR sub-section bodies copied through from the
     predecessor's cycle log.

The coder-prompt half of this unit (the PR-description template
update) is pinned in :mod:`tests.test_coder_prompt`; this module
covers the writer + injection wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import cycle_log, state
from orchestrator.cycle_log_render import (
    PR_DESCRIPTION_HEADING,
    TLDR_HEADING,
    TLDR_PLACEHOLDER,
    TLDR_SUBHEADINGS,
    render_cycle_log,
)
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import compose_coder_task, compose_reviewer_task, compose_tester_task

# --------------------------- shared fixtures ---------------------------


def _seed_unit(
    unit_id: str = "F-017-U-1",
    feature_id: str = "F-017",
    *,
    pr_number: int | None = 42,
    status: str = "in_ci",
) -> None:
    state.save_feature(
        Feature(
            id=feature_id,
            title="cycle log tightening",
            description="d",
            repo_path="https://github.com/o/r",
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title="TL;DR writer", description="")],
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            pr_number=pr_number,
        )
    )


def _pr_body_with_tldr(
    *,
    what_shipped: str | None = "Mirrored the PR's three TL;DR H3 sub-sections into the cycle log.",
    downstream_contract: str | None = (
        "render_cycle_log emits `## TL;DR` between `## PR` and `## Coder's PR description`."
    ),
    decisions_worth_knowing: str | None = (
        "Strict heading match; missing sub-sections fall back to a TBD placeholder."
    ),
) -> str:
    """Build a coder PR body with the three TL;DR sub-sections optionally filled."""
    parts = ["**Unit ID:** F-017-U-1", "", "## TL;DR", ""]
    pairs = [
        ("### What shipped", what_shipped),
        ("### Downstream contract", downstream_contract),
        ("### Decisions worth knowing", decisions_worth_knowing),
    ]
    for heading, content in pairs:
        if content is None:
            continue
        parts.append(heading)
        parts.append(content)
        parts.append("")
    parts.extend(
        [
            "## What this change does",
            "Implements F-017-U-1.",
            "",
            "## Spec satisfaction",
            "- [x] new TL;DR section between `## PR` and `## Coder's PR description`",
        ]
    )
    return "\n".join(parts)


# --------------------------- 1. format pinning ---------------------------


class TestTldrFormatPinning:
    """Heading order + sub-heading spelling are the structural contract."""

    def test_canonical_subheadings_are_exact_strings(self) -> None:
        # If anyone "improves" the spelling, the auto-fill extractor will
        # silently miss the coder's sub-sections — pin the canonical form.
        assert TLDR_SUBHEADINGS == (
            "### What shipped",
            "### Downstream contract",
            "### Decisions worth knowing",
        )
        assert TLDR_HEADING == "## TL;DR"

    def test_heading_order_pr_then_tldr_then_pr_description(self, tmp_state_db: Path) -> None:
        _seed_unit()
        md = render_cycle_log(
            "F-017-U-1",
            pr_info={"body": _pr_body_with_tldr(), "headRefOid": "abc123"},
            review_threads=[],
        )
        idx_pr = md.find("## PR")
        idx_tldr = md.find(TLDR_HEADING)
        idx_desc = md.find(PR_DESCRIPTION_HEADING)
        assert idx_pr != -1, "## PR section missing"
        assert idx_tldr != -1, "## TL;DR section missing"
        assert idx_desc != -1, "## Coder's PR description section missing"
        assert idx_pr < idx_tldr < idx_desc, (
            f"heading order wrong: PR={idx_pr}, TL;DR={idx_tldr}, PR-desc={idx_desc}"
        )

    def test_all_three_subheadings_emitted_when_pr_body_supplies_them(
        self, tmp_state_db: Path
    ) -> None:
        _seed_unit()
        md = render_cycle_log(
            "F-017-U-1",
            pr_info={"body": _pr_body_with_tldr(), "headRefOid": "abc123"},
            review_threads=[],
        )
        for sub in TLDR_SUBHEADINGS:
            assert sub in md, f"sub-heading {sub!r} missing from rendered cycle log"
        # And the actual content the test PR body supplied surfaces:
        assert "Mirrored the PR's three TL;DR H3 sub-sections" in md
        assert "render_cycle_log emits `## TL;DR`" in md
        assert "Strict heading match" in md

    def test_missing_subsection_emits_tbd_placeholder(self, tmp_state_db: Path) -> None:
        _seed_unit()
        # Coder forgot to fill "Decisions worth knowing".
        body = _pr_body_with_tldr(decisions_worth_knowing=None)
        md = render_cycle_log(
            "F-017-U-1",
            pr_info={"body": body, "headRefOid": "abc"},
            review_threads=[],
        )
        # Heading still present (grep-stability invariant from spec § Acceptance #1).
        assert "### Decisions worth knowing" in md
        # And the placeholder shows under it.
        assert TLDR_PLACEHOLDER in md
        # Filled sub-sections didn't get the placeholder.
        assert "Mirrored the PR's three TL;DR H3 sub-sections" in md

    def test_entirely_missing_tldr_block_in_pr_body_still_emits_full_skeleton(
        self, tmp_state_db: Path
    ) -> None:
        _seed_unit()
        # Coder PR body has no TL;DR section at all (legacy / lazy coder).
        md = render_cycle_log(
            "F-017-U-1",
            pr_info={"body": "## What this change does\nNo TL;DR here.", "headRefOid": "abc"},
            review_threads=[],
        )
        assert TLDR_HEADING in md
        for sub in TLDR_SUBHEADINGS:
            assert sub in md
        # Every sub-section gets a placeholder.
        assert md.count(TLDR_PLACEHOLDER) == len(TLDR_SUBHEADINGS)

    def test_tldr_block_emitted_even_when_pr_info_empty(self, tmp_state_db: Path) -> None:
        """No PR body at all → still emit the TL;DR skeleton with placeholders.

        Worker prompt content must be predictable per spec § Decisions —
        an absent block would otherwise signal a writer bug to callers."""
        _seed_unit()
        md = render_cycle_log("F-017-U-1", pr_info={}, review_threads=[])
        assert TLDR_HEADING in md
        for sub in TLDR_SUBHEADINGS:
            assert sub in md
        assert md.count(TLDR_PLACEHOLDER) == len(TLDR_SUBHEADINGS)


# --------------------------- 2. cycle_log_summary preserved ---------------------------


class TestCycleLogSummaryPreservesTldr:
    """Because TL;DR sits *above* the strip boundary, the existing
    summary semantics already preserve it — verify, don't re-implement."""

    def _write_log(self, tmp_state_db: Path, body: str) -> None:
        _seed_unit("F-017-U-1", "F-017")
        path = cycle_log.cycle_log_path("F-017-U-1", base_dir=cycle_log.cycle_log_base_dir())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_summary_contains_tldr_block_verbatim(self, tmp_state_db: Path) -> None:
        log_body = (
            "# F-017-U-1 — t\n\n"
            "## PR\n#1\nStatus: done\n\n"
            "## TL;DR\n\n"
            "### What shipped\nWHAT-SHIPPED-MARKER\n\n"
            "### Downstream contract\nDOWNSTREAM-MARKER\n\n"
            "### Decisions worth knowing\nDECISIONS-MARKER\n\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "Long PR body that gets stripped.\n\n"
            "## Cycle history\n1 cycles · cap-3 not hit\n\n"
            "## Review threads\n_no review threads_\n"
        )
        self._write_log(tmp_state_db, log_body)
        summary = cycle_log.cycle_log_summary("F-017-U-1")
        # Strip boundary still works (existing semantics preserved).
        assert "Long PR body that gets stripped." not in summary
        assert "## Coder's PR description" not in summary
        # TL;DR block survives verbatim — heading AND sub-headings AND content.
        assert TLDR_HEADING in summary
        for sub in TLDR_SUBHEADINGS:
            assert sub in summary
        assert "WHAT-SHIPPED-MARKER" in summary
        assert "DOWNSTREAM-MARKER" in summary
        assert "DECISIONS-MARKER" in summary
        # Cycle history + Review threads still survive — `_CYCLE_LOG_SECTION_AFTER_PR_RE`
        # allow-list is unchanged.
        assert "## Cycle history" in summary
        assert "## Review threads" in summary

    def test_summary_function_name_unchanged(self) -> None:
        """No rename allowed per unit description ('no rename, no caller change')."""
        assert callable(cycle_log.cycle_log_summary)


# --------------------------- 3. end-to-end via compose_*_task ---------------------------


@pytest.fixture
def predecessor_log_body() -> str:
    """Cycle-log summary text for a hypothetical merged predecessor."""
    return (
        "# F-017-U-0 — predecessor\n\n"
        "## PR\n#9\nStatus: done\n\n"
        "## TL;DR\n\n"
        "### What shipped\nE2E-WHAT-MARKER\n\n"
        "### Downstream contract\nE2E-CONTRACT-MARKER\n\n"
        "### Decisions worth knowing\nE2E-DECISIONS-MARKER\n\n"
        "## Cycle history\n1 cycles · cap-3 not hit\n"
    )


class TestComposeTasksInjectTldr:
    """Worker-task messages carry the predecessor TL;DR through to the
    downstream coder / tester / reviewer prompt verbatim."""

    @pytest.fixture
    def feature_and_unit(self) -> tuple[Feature, WorkUnit]:
        f = Feature(id="F-017", title="t", description="d")
        u = WorkUnit(
            id="F-017-U-1",
            feature_id="F-017",
            title="u",
            description="ud",
            depends_on=["F-017-U-0"],
        )
        return f, u

    def test_coder_task_predecessor_block_contains_tldr(
        self, feature_and_unit, predecessor_log_body
    ) -> None:
        f, u = feature_and_unit
        body = compose_coder_task(
            f,
            u,
            "branch",
            "tok",
            predecessor_logs=[("F-017-U-0", predecessor_log_body)],
        )
        assert "## PREDECESSOR UNITS" in body
        assert "### F-017-U-0" in body
        # All three TL;DR sub-sections + their content survive the
        # predecessor render path (strip-H1 wrapper, etc.).
        for sub in TLDR_SUBHEADINGS:
            assert sub in body
        assert "E2E-WHAT-MARKER" in body
        assert "E2E-CONTRACT-MARKER" in body
        assert "E2E-DECISIONS-MARKER" in body

    def test_tester_task_predecessor_block_contains_tldr(
        self, feature_and_unit, predecessor_log_body
    ) -> None:
        f, u = feature_and_unit
        body = compose_tester_task(
            f,
            u,
            "branch",
            42,
            "tok",
            predecessor_logs=[("F-017-U-0", predecessor_log_body)],
        )
        assert "## PREDECESSOR UNITS" in body
        assert "E2E-WHAT-MARKER" in body
        assert "E2E-CONTRACT-MARKER" in body
        assert "E2E-DECISIONS-MARKER" in body

    def test_reviewer_task_predecessor_block_contains_tldr(
        self, feature_and_unit, predecessor_log_body
    ) -> None:
        f, u = feature_and_unit
        body = compose_reviewer_task(
            f,
            u,
            42,
            "tok",
            predecessor_logs=[("F-017-U-0", predecessor_log_body)],
        )
        assert "## PREDECESSOR UNITS" in body
        assert "E2E-WHAT-MARKER" in body
        assert "E2E-CONTRACT-MARKER" in body
        assert "E2E-DECISIONS-MARKER" in body


# --------------------------- coder prompt template pin ---------------------------


class TestCoderPromptRequiresTldr:
    """Coder prompt PR-description template must instruct the agent to
    include the three canonical sub-sections — otherwise the auto-fill
    extractor silently emits all placeholders."""

    @pytest.fixture(scope="class")
    def coder_prompt(self) -> str:
        prompt_path = (
            Path(__file__).resolve().parent.parent / "orchestrator" / "prompts" / "coder.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    def test_prompt_mentions_tldr_section(self, coder_prompt: str) -> None:
        assert "## TL;DR" in coder_prompt, (
            "coder prompt no longer mentions ## TL;DR; cycle-log auto-fill "
            "will produce all-TBD sub-sections"
        )

    def test_prompt_lists_all_three_canonical_subheadings(self, coder_prompt: str) -> None:
        for sub in TLDR_SUBHEADINGS:
            assert sub in coder_prompt, (
                f"coder prompt missing canonical sub-heading {sub!r}; "
                "auto-fill extractor will silently miss it"
            )
