"""Pin the coder prompt's load-bearing structure.

These are not behavior tests — Managed Agent workers aren't unit-tested.
What we pin here are the prompt-level invariants: the workflow's step
numbering, the pre-commit self-review section, and the BLOCKED escape
path on unresolvable rebase conflicts. If anyone edits coder.md in a way
that drops or breaks these, CI catches it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPT_DIR = Path(__file__).resolve().parent.parent / "orchestrator" / "prompts"


@pytest.fixture(scope="module")
def coder_prompt() -> str:
    return (PROMPT_DIR / "coder.md").read_text()


# --------------------------- workflow structure ---------------------------


class TestWorkflowStepNumbering:
    """The numbered workflow at the top of coder.md must stay consecutive.

    Off-by-one mistakes (two step-8s, or 7 jumping to 9) are easy to
    introduce when inserting a new step. This test catches them.
    """

    def test_workflow_steps_are_1_through_n_with_no_gaps_or_duplicates(self, coder_prompt):
        # Only count the "## Workflow" section's numbered steps — not the
        # numbered lists inside the "When resumed with feedback" section
        # (which restarts at 1 deliberately).
        workflow, _, _ = coder_prompt.partition("## When resumed with feedback")
        step_numbers = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", workflow, re.MULTILINE)]
        assert step_numbers, "no numbered workflow steps found in coder.md"
        expected = list(range(1, len(step_numbers) + 1))
        assert step_numbers == expected, (
            f"workflow step numbering is broken: got {step_numbers}, expected {expected}"
        )

    def test_workflow_has_at_least_8_steps(self, coder_prompt):
        """Sanity bound — the workflow has setup, clone, read, implement,
        test, rebase, self-review, commit, PR-open, PR_URL-marker. Less
        than 8 means a section was deleted; flag for review."""
        workflow, _, _ = coder_prompt.partition("## When resumed with feedback")
        step_count = len(re.findall(r"^\d+\. ", workflow, re.MULTILINE))
        assert step_count >= 8, f"workflow shrunk to {step_count} steps; was 10"


# --------------------------- pre-commit self-review ---------------------------


class TestPrecommitSelfReview:
    """The pre-commit self-review pass (Simplify + Arch-drift) is the
    coder's chance to catch quality issues before the reviewer sees them.
    Pin the load-bearing pieces so a future trim doesn't silently remove
    them."""

    def test_self_review_section_exists(self, coder_prompt):
        assert "Pre-commit self-review" in coder_prompt, (
            "the pre-commit self-review section is missing"
        )

    def test_both_simplify_and_arch_drift_passes_referenced(self, coder_prompt):
        # The agent runs two passes; both names need to appear so the
        # prompt instructs both checks. Match case-insensitively because
        # the prompt may format these as "(a) Simplify" / "(b) Arch-drift".
        assert re.search(r"\bSimplify\b", coder_prompt), "Simplify pass missing"
        assert re.search(r"\bArch-drift\b", coder_prompt), "Arch-drift pass missing"

    def test_what_vs_why_comment_rule_present(self, coder_prompt):
        """The WHAT-comments-are-noise / WHY-comments-matter distinction
        is the most actionable single rule in the simplify pass. Pin it."""
        # Loose match — the prompt might phrase it different ways.
        assert "WHY" in coder_prompt
        assert "WHAT" in coder_prompt

    def test_self_review_runs_against_project_conventions(self, coder_prompt):
        """Arch-drift compares against CLAUDE.md / AGENTS.md / CONTRIBUTING.md.
        These three filenames are the canonical project-convention surfaces;
        if the prompt drops the reference, arch-drift loses its anchors."""
        section_idx = coder_prompt.find("Pre-commit self-review")
        # Look at the ~3KB after the heading
        section_window = coder_prompt[section_idx : section_idx + 3000]
        assert "CLAUDE.md" in section_window
        assert "CONTRIBUTING.md" in section_window


# --------------------------- BLOCKED rebase-conflict escape ---------------------------


class TestRebaseConflictBlockedEscape:
    """When rebase produces an unresolvable conflict, the coder must
    `git rebase --abort` and emit a structured BLOCKED line — not guess,
    not silently fail. Pin both halves of that contract."""

    def test_rebase_abort_instruction_present(self, coder_prompt):
        assert "git rebase --abort" in coder_prompt, (
            "coder prompt is missing the rebase --abort escape; agent will "
            "guess at conflicts it can't resolve"
        )

    def test_rebase_section_emits_blocked_on_unresolvable_conflict(self, coder_prompt):
        """Pin the contract inside the rebase step specifically: the
        instruction to abort + emit BLOCKED must live in the rebase step
        itself, not just in a generic BLOCKED-taxonomy section elsewhere.
        Otherwise the agent might not know to apply it during rebase."""
        # Find the rebase step's window — bounded so we don't accidentally
        # overrun into the BLOCKED-taxonomy section at the end of the file.
        rebase_idx = coder_prompt.find("Rebase against latest main")
        assert rebase_idx > -1, "Rebase step header missing"
        # Bound the section by the next numbered workflow step. We don't
        # know the step number (could be 7 today, 8 tomorrow), so look for
        # the generic pattern: blank-line + digit + dot + space at column 0.
        post_rebase = coder_prompt[rebase_idx:]
        next_step = re.search(r"\n\n\d+\. ", post_rebase)
        assert next_step is not None, "no next workflow step found after rebase"
        rebase_section = post_rebase[: next_step.start()]
        # The rebase section MUST contain both halves of the escape contract:
        # the abort command + the structured BLOCKED line with the right slug.
        assert "git rebase --abort" in rebase_section, (
            "rebase section doesn't tell the agent to abort on unresolvable conflict"
        )
        assert "merge_conflict_unresolved" in rebase_section, (
            "rebase section doesn't reference the merge_conflict_unresolved BLOCKED slug"
        )
        assert "BLOCKED: reason=" in rebase_section, (
            "rebase section doesn't show the structured BLOCKED format"
        )


# --------------------------- F-018 merge-source guidance ---------------------------


class TestMergeSourceFixGuidance:
    """F-018 — the ``SOURCE: merge`` fix-loop section walks the coder
    through a rebase-not-merge + force-push against the file list. Pin
    the load-bearing pieces so a future trim doesn't silently drop them.
    """

    def test_merge_source_section_exists(self, coder_prompt):
        assert "SOURCE: merge" in coder_prompt, (
            "coder prompt is missing the F-018 merge-source section; "
            "address_review(source='merge', ...) has no docs for the agent"
        )

    def _merge_section(self, coder_prompt: str) -> str:
        idx = coder_prompt.find("### `SOURCE: merge`")
        assert idx > -1, "merge-source heading missing"
        # Bound by the next ### heading.
        after = coder_prompt[idx + 1 :]
        next_idx = after.find("\n### ")
        return after if next_idx == -1 else after[:next_idx]

    def test_rebase_not_merge_explicit(self, coder_prompt):
        """The whole point is the rebase keeps the PR's commit graph linear;
        a `git merge origin/main` would smash main's history into the PR
        and defeat delta review."""
        section = self._merge_section(coder_prompt)
        assert "rebase" in section.lower()
        # The "don't merge" / linear-graph rule must be explicit so the
        # agent doesn't reach for `git merge` as the obvious tool.
        assert "linear" in section.lower()
        assert "git rebase origin/main" in section

    def test_force_push_with_lease_present(self, coder_prompt):
        """Rebase rewrites SHAs → force-push is the only way to land them.
        `--force-with-lease` (not bare `--force`) so an intervening push
        aborts the force instead of being silently overwritten."""
        section = self._merge_section(coder_prompt)
        assert "--force-with-lease" in section
        # `git push --force` (without --with-lease) is what the prompt
        # is calling OUT, not endorsing — accept either presence as long
        # as `--force-with-lease` is also there. The lease semantics
        # paragraph is the load-bearing thing.
        assert "lease" in section.lower()

    def test_blocked_escape_for_unresolvable_conflict(self, coder_prompt):
        """Same escape valve as the rebase step in the main workflow —
        if the conflict requires guessing, abort and emit BLOCKED with
        the merge_conflict_unresolved slug."""
        section = self._merge_section(coder_prompt)
        assert "git rebase --abort" in section
        assert "merge_conflict_unresolved" in section

    def test_scope_is_rebase_only(self, coder_prompt):
        """The reviewer already endorsed the unrebased content; the
        coder must not opportunistically refactor. Same "no scope creep"
        rule the ultrareview section uses."""
        section = self._merge_section(coder_prompt)
        assert "scope" in section.lower()

    def test_pr_out_of_date_edge_case_carves_out_merge_source(self, coder_prompt):
        """PR #66 M3: the ``PR out-of-date with base branch`` edge case
        in ``### Edge cases`` says "don't try to rebase" — which would
        flatly contradict the SOURCE: merge section's 50-line rebase
        recipe if it didn't carry an explicit carve-out pointing back
        at SOURCE: merge for the true-conflict case.
        """
        # Anchor on the edge-case bullet's heading.
        idx = coder_prompt.find("PR out-of-date with base branch")
        assert idx > -1, "PR out-of-date edge case bullet missing"
        # The bullet's body must say "no conflict" — the carve-out
        # rule that scopes "don't rebase" to the non-conflict case — and
        # reference SOURCE: merge so an agent reading both sections
        # knows which one to follow.
        bullet = coder_prompt[idx : idx + 600]
        assert "no conflict" in bullet.lower(), (
            "the edge case bullet must scope itself to the non-conflict "
            "case so it doesn't contradict the SOURCE: merge rebase recipe"
        )
        assert "SOURCE: merge" in bullet, (
            "the edge case bullet must point at the SOURCE: merge section "
            "for the true-conflict case"
        )
