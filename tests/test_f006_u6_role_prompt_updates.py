"""Spec tests for F-006-U-6 — role-prompt + CLAUDE.md updates.

The unit description in F-006-U-6 requires four prompt-level edits per
the proposal's "Role prompt changes" + "CLAUDE.md updates" sections of
`docs/PROPOSAL-feature-spec-and-headless-daemon.md`:

  coder.md:
    - read `## FEATURE SPEC` FIRST (before implementation)
    - emit a `## Spec satisfaction` section in the PR description
    - align with predecessor decisions
    - re-read the spec on every fix-loop resume

  tester.md:
    - test against the spec's Acceptance criteria, not just the unit
      description
    - treat scope violations as `BUG_FOUND`

  reviewer.md:
    - perform a mandatory spec-vs-PR-description comparison
    - read `## THIS UNIT'S CYCLE LOG` first on retry cycles
    - run the predecessor consistency check
    - Method renumbered (was 7 steps, now 8)

  CLAUDE.md (lead persona):
    - call `feature_memory(F-X)` at session start before discussing a
      feature
    - edit and commit `spec.md` with a `Why:` line on non-obvious
      decisions
    - cycle logs are read-only history (revise `spec.md`, never the
      cycle log)

Mirrors the style of `tests/test_coder_prompt.py` and
`tests/test_reviewer_prompt.py` — pin the load-bearing structure of the
prompt files so accidental edits don't silently regress the discipline.
The agents themselves aren't unit-tested (Managed Agents are external);
what we lock in here is the prompt invariants the unit description
called out verbatim.

Pure markdown / regex assertions only — no orchestrator runtime is
exercised. These tests are safe to run without a state.db fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = REPO_ROOT / "orchestrator" / "prompts"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


# --------------------------- fixtures ---------------------------


@pytest.fixture(scope="module")
def coder_prompt() -> str:
    return (PROMPT_DIR / "coder.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tester_prompt() -> str:
    return (PROMPT_DIR / "tester.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def reviewer_prompt() -> str:
    return (PROMPT_DIR / "reviewer.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def claude_md() -> str:
    return CLAUDE_MD.read_text(encoding="utf-8")


# ============================================================================
# CODER.MD
# ============================================================================


class TestCoderReadsSpecFirst:
    """Coder must read `## FEATURE SPEC` BEFORE implementing.

    Order matters — the unit description says coder "reads spec FIRST".
    A workflow that mentions the spec block only after the implementation
    step has the rule but not the intent.
    """

    def test_feature_spec_block_documented_in_task_section(self, coder_prompt):
        """The `## Your task` section must enumerate the FEATURE SPEC
        context block — that's the contract that tells the coder what
        the orchestrator injects."""
        # The "## Your task" section is bounded by the next top-level "## " header.
        task_start = coder_prompt.find("## Your task")
        assert task_start != -1, "missing '## Your task' section"
        rest = coder_prompt[task_start + len("## Your task") :]
        next_hdr = re.search(r"\n## ", rest)
        task_section = rest[: next_hdr.start()] if next_hdr else rest
        assert "## FEATURE SPEC" in task_section, (
            "coder.md doesn't document `## FEATURE SPEC` as an injected "
            "context block in the task section"
        )

    def test_predecessor_units_block_documented_in_task_section(self, coder_prompt):
        task_start = coder_prompt.find("## Your task")
        rest = coder_prompt[task_start + len("## Your task") :]
        next_hdr = re.search(r"\n## ", rest)
        task_section = rest[: next_hdr.start()] if next_hdr else rest
        assert "## PREDECESSOR UNITS" in task_section, (
            "coder.md doesn't document `## PREDECESSOR UNITS` as an "
            "injected context block in the task section"
        )

    def test_read_spec_step_appears_before_implement_step(self, coder_prompt):
        """The step that says 'read the spec' must come before the
        'implement' step in the numbered workflow. If 'implement' shows
        up first, the coder will code blind and only check the spec
        afterward — the opposite of what the unit description requires."""
        workflow, _, _ = coder_prompt.partition("## When resumed with feedback")
        # The actual prompt phrase is on its own bullet line with backticks
        # around the block name, e.g. "Read `## FEATURE SPEC` FIRST".
        spec_read_match = re.search(
            r"(?i)Read.{0,20}`?## FEATURE SPEC`?.{0,20}FIRST",
            workflow,
        )
        # The implementation step says "Implement the unit. Make the SMALLEST"
        implement_idx = workflow.find("Implement the unit")
        assert spec_read_match is not None, (
            "coder.md doesn't tell the agent to read `## FEATURE SPEC` FIRST"
        )
        assert implement_idx != -1, "coder.md is missing the 'Implement the unit' step"
        assert spec_read_match.start() < implement_idx, (
            "coder.md instructs 'Implement' before 'read FEATURE SPEC FIRST' — "
            "spec-first discipline is broken"
        )

    def test_spec_wins_on_conflict_with_unit_description(self, coder_prompt):
        """If the unit description and the spec disagree, the spec is
        authoritative. The unit description calls this out as 'spec
        wins on conflict'; pin the rule so a future edit doesn't
        accidentally drop it."""
        # The phrasing in the prompt: "If unit description and spec conflict, spec wins."
        assert re.search(
            r"(?i)spec wins|spec is authoritative|spec takes precedence",
            coder_prompt,
        ), "coder.md doesn't tell the agent which source wins on spec-vs-unit-description conflict"


class TestCoderEmitsSpecSatisfactionSection:
    """The PR description must include a `## Spec satisfaction` section
    when a spec exists. This is the surface the reviewer agent reads to
    verify spec compliance — silent absence defeats the discipline."""

    def test_spec_satisfaction_section_name_present(self, coder_prompt):
        # Must appear at least twice — once introducing the rule, once
        # showing the template inside the PR-body checklist.
        assert coder_prompt.count("## Spec satisfaction") >= 2, (
            f"coder.md should reference `## Spec satisfaction` at least twice "
            f"(rule + template); found {coder_prompt.count('## Spec satisfaction')}"
        )

    def test_spec_satisfaction_section_is_mandatory_when_spec_exists(self, coder_prompt):
        """The unit description says the section is required when the
        spec is injected — not optional, not 'preferred'. Pin the
        mandatory wording. The rule must appear in the same paragraph
        as the section name (one of the multiple mentions), not in a
        wandering sentence far away."""
        # Look for any of: "mandatory", "required", or the negative form
        # "omit only when no spec was provided" which the diff used.
        # Scan ALL occurrences of `## Spec satisfaction` and pass if at
        # least one is paired with a mandatory marker in a 700-char
        # window (forward — the template is below the section name).
        positions = [m.start() for m in re.finditer(r"## Spec satisfaction", coder_prompt)]
        assert positions, "no `## Spec satisfaction` mentions to mandatory-check"
        found = False
        for p in positions:
            window = coder_prompt[max(p - 100, 0) : p + 700]
            if re.search(
                r"(?i)mandatory|required|must include|omit only when no spec",
                window,
            ):
                found = True
                break
        assert found, (
            "coder.md doesn't make the `## Spec satisfaction` section mandatory "
            "when a spec exists — no occurrence of the section name is paired "
            "with a 'mandatory' / 'required' / 'omit only when no spec' marker"
        )

    def test_spec_satisfaction_template_lists_three_subsections(self, coder_prompt):
        """The PR-body template for `## Spec satisfaction` must enumerate
        the three sub-fields the reviewer expects: satisfied acceptance
        criteria, deviations, predecessor alignment. Dropping any one
        means the reviewer can't audit it.

        The template lives in a ```markdown ... ``` fence under the
        PR-body checklist (step 11). Find the longest forward window
        beyond a `## Spec satisfaction` occurrence and verify all three
        sub-sections appear in it.
        """
        positions = [m.start() for m in re.finditer(r"## Spec satisfaction", coder_prompt)]
        assert positions, "no `## Spec satisfaction` mention to template-check"

        # The template window is the one that contains the three
        # sub-headings. Find an occurrence whose forward 1.5KB window
        # contains all three markers; that's the template fence. If no
        # single occurrence works, the template is broken (or split).
        success = False
        for p in positions:
            window = coder_prompt[p : p + 1500]
            has_accept = bool(
                re.search(
                    r"(?i)satisfies these acceptance criteria|acceptance criteria from features",
                    window,
                )
            )
            has_dev = bool(re.search(r"(?i)deviation", window))
            has_pred = bool(re.search(r"(?i)predecessor alignment", window))
            if has_accept and has_dev and has_pred:
                success = True
                break
        assert success, (
            "no `## Spec satisfaction` occurrence is followed by a template "
            "block listing all three sub-sections (acceptance criteria, "
            "deviations, predecessor alignment) — template is broken or split"
        )


class TestCoderAlignsWithPredecessors:
    """The coder must align with predecessor decisions (validator
    choices, naming, interfaces locked in by merged dependency units),
    or surface the divergence explicitly."""

    def test_predecessor_alignment_rule_present(self, coder_prompt):
        # Look for the rule in coder.md — wording: "align with predecessor"
        # or "Align with predecessor".
        assert re.search(r"(?i)align with predecessor", coder_prompt), (
            "coder.md doesn't tell the agent to align with predecessor decisions"
        )

    def test_predecessor_divergence_must_be_surfaced(self, coder_prompt):
        """Silent divergence from a predecessor's choice is exactly the
        scope-creep failure mode this rule defends against. The prompt
        must require the coder to surface the divergence (not just
        silently use a different validator)."""
        # Look for any phrasing along: "If you have a reason to diverge,
        # surface it in `## Spec satisfaction`" / "flag the divergence"
        # / "note the deviation"...
        assert re.search(
            r"(?i)(?:surface|flag|note|document)\s+(?:it|the|this|any)?\s*\w*\s*"
            r"(?:divergence|diverge|deviation|in `## Spec satisfaction`)",
            coder_prompt,
        ) or re.search(
            r"(?i)surface it in `## Spec satisfaction`",
            coder_prompt,
        ), (
            "coder.md doesn't require the agent to surface predecessor "
            "divergences in `## Spec satisfaction`"
        )


class TestCoderRereadsSpecOnResume:
    """On every fix-loop resume the spec must be re-read — the
    orchestrator re-injects it fresh each turn, and if the spec was
    edited mid-cycle (escalation → design change), the new version is
    what the coder should build against."""

    def test_resume_section_has_reread_spec_rule(self, coder_prompt):
        """The 'When resumed with feedback' section must contain the
        re-read instruction. Putting it in the cold-start section
        only doesn't cover the resume path."""
        resume_idx = coder_prompt.find("## When resumed with feedback")
        assert resume_idx != -1, "missing '## When resumed with feedback' section"
        resume_section = coder_prompt[resume_idx:]
        # Look for any form of "re-read" + "FEATURE SPEC" near each other.
        # Phrasing in the prompt: "Re-read `## FEATURE SPEC` on every resume."
        assert re.search(
            r"(?i)re-?read.{0,80}FEATURE SPEC|FEATURE SPEC.{0,80}re-?read",
            resume_section,
            re.DOTALL,
        ), (
            "resume section doesn't tell the agent to re-read `## FEATURE SPEC` "
            "on every fix-loop turn — spec edits mid-cycle won't take effect"
        )

    def test_resume_reread_mentions_orchestrator_reinjection(self, coder_prompt):
        """The instruction makes more sense when paired with the
        rationale: the orchestrator re-injects the block on each resume.
        Without the 'why', the agent might skip the re-read assuming
        the block is stale. Pin the rationale."""
        resume_idx = coder_prompt.find("## When resumed with feedback")
        resume_section = coder_prompt[resume_idx:]
        # Look for any of: "re-inject", "fresh on each", "living source",
        # "spec is your living source of truth", etc.
        assert re.search(
            r"(?i)re-?inject|fresh on each|living source|new version is what",
            resume_section,
        ), (
            "resume section doesn't explain that the orchestrator re-injects "
            "the spec fresh on every turn"
        )


class TestCoderWorkflowStillIntegral:
    """Sanity guards: the F-006-U-6 edits must not break the pre-existing
    workflow contract (consecutive step numbering, BLOCKED escape on
    rebase conflict, etc.). These overlap test_coder_prompt.py but are
    re-asserted here so this file is self-contained — if someone deletes
    the older file the F-006-U-6 coverage doesn't get a stealth gap."""

    def test_workflow_numbering_is_consecutive(self, coder_prompt):
        workflow, _, _ = coder_prompt.partition("## When resumed with feedback")
        nums = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", workflow, re.MULTILINE)]
        assert nums, "no numbered workflow steps found"
        assert nums == list(range(1, len(nums) + 1)), (
            f"workflow numbering is broken after F-006-U-6 insertion: got {nums}"
        )

    def test_workflow_step_count_grew_to_accommodate_spec_step(self, coder_prompt):
        """The unit description says coder reads spec FIRST — implementing
        that necessarily adds at least one step to the workflow (the
        old prompt was 10 steps; adding the spec-read step makes it
        ≥ 11). Allow ≥ 11 to leave headroom for future additions."""
        workflow, _, _ = coder_prompt.partition("## When resumed with feedback")
        count = len(re.findall(r"^\d+\. ", workflow, re.MULTILINE))
        assert count >= 11, (
            f"F-006-U-6 should have added a 'read spec FIRST' step but "
            f"workflow has only {count} steps (was 10 before this unit)"
        )


# ============================================================================
# TESTER.MD
# ============================================================================


class TestTesterTestsAgainstSpecAcceptance:
    """Tester must test against the spec's Acceptance criteria, not just
    the unit description. The unit description is one slice; the spec
    is the contract."""

    def test_feature_spec_block_documented(self, tester_prompt):
        assert "## FEATURE SPEC" in tester_prompt, (
            "tester.md doesn't document the `## FEATURE SPEC` context block"
        )

    def test_predecessor_units_block_documented(self, tester_prompt):
        assert "## PREDECESSOR UNITS" in tester_prompt, (
            "tester.md doesn't document the `## PREDECESSOR UNITS` context block"
        )

    def test_acceptance_criteria_named_as_test_target(self, tester_prompt):
        """The phrase 'Acceptance' (with capital A — the spec section
        name) should appear in the workflow's test-writing step. Plain
        'acceptance' in a generic sentence isn't enough — we need the
        word used as the spec section name."""
        # Find step 4 — "Write tests..." — and assert the spec's
        # Acceptance section is named.
        # We can't anchor on "step 4" because the numbering could change;
        # find the relevant phrasing instead.
        assert re.search(
            r"(?i)spec'?s? Acceptance|Acceptance criteria|Acceptance section",
            tester_prompt,
        ), (
            "tester.md doesn't name the spec's Acceptance section as a "
            "test target — testing against the unit description alone is the "
            "pre-F-006-U-6 behavior"
        )

    def test_spec_acceptance_omits_unit_description_still_in_scope(self, tester_prompt):
        """The unit description says criteria the unit description
        omits but the spec includes are STILL in scope. This is the
        most-likely-skipped rule, since testers tend to mirror the
        explicit task. Pin the rule."""
        # Wording in the prompt: "Spec criteria the unit description
        # omits are still in scope."
        assert re.search(
            r"(?i)criteria the unit description omits|spec criteria.{0,80}still in scope|"
            r"still in scope|acceptance criteria are the contract",
            tester_prompt,
        ), (
            "tester.md doesn't tell the tester that spec criteria omitted "
            "from the unit description are still in scope"
        )

    def test_predecessor_consistency_check_mentioned(self, tester_prompt):
        """The cross-check against predecessor decisions must be
        instructed — silent divergence from U-2's interface in U-3's
        code is a real bug."""
        assert re.search(
            r"(?i)cross-?check.{0,80}predecessor|predecessor.{0,80}(?:decision|interface)",
            tester_prompt,
            re.DOTALL,
        ), "tester.md doesn't tell the tester to cross-check predecessor decisions"


class TestTesterScopeViolationsAreBugs:
    """A diff that touches code the spec's `Out of scope` excludes is a
    bug, not a stylistic preference. The tester must emit BUG_FOUND on
    scope violations, not silently let them through."""

    def test_out_of_scope_named_as_bug_trigger(self, tester_prompt):
        """The prompt must reference 'Out of scope' (the spec section
        name) as a bug trigger so the agent knows which spec section
        bounds the scope check."""
        assert re.search(r"(?i)Out of scope", tester_prompt), (
            "tester.md doesn't reference the spec's 'Out of scope' section"
        )

    def test_scope_violation_triggers_bug_found(self, tester_prompt):
        """The unit description says 'treats scope violations as
        BUG_FOUND'. Pin that the BUG_FOUND marker is named in the
        scope-violation paragraph (not in a generic 'how to emit
        BUG_FOUND' section elsewhere)."""
        # Find the paragraph that discusses scope violations.
        scope_idx = tester_prompt.find("Scope violations")
        # Fallback to a softer match.
        if scope_idx == -1:
            scope_idx = tester_prompt.lower().find("scope violation")
        assert scope_idx != -1, (
            "tester.md has no paragraph about scope violations — the unit "
            "description's 'treats scope violations as BUG_FOUND' rule is missing"
        )
        # Window: the paragraph itself, ~700 chars
        window = tester_prompt[scope_idx : scope_idx + 700]
        assert "BUG_FOUND" in window, (
            "scope-violation paragraph doesn't direct the tester to emit "
            "BUG_FOUND — silent scope creep would pass"
        )

    def test_step_6_interpretation_includes_scope_violation_path(self, tester_prompt):
        """Step 6 (Interpret results) routes outcomes to TESTS_PASS /
        fix-test / BUG_FOUND. The F-006-U-6 edit added a parenthetical
        clarifying that 'including a spec scope violation per step 4'
        counts as IMPLEMENTATION wrong. Pin the linkage so the rule
        isn't orphaned in step 4."""
        # Find the IMPLEMENTATION-wrong bullet.
        m = re.search(
            r"(?:IMPLEMENTATION is wrong)(.{0,250})",
            tester_prompt,
            re.DOTALL,
        )
        assert m is not None, "tester.md is missing the 'IMPLEMENTATION wrong' bullet"
        bullet_text = m.group(0)
        assert re.search(r"(?i)scope violation|scope.{0,30}step 4", bullet_text), (
            "step 6's IMPLEMENTATION-wrong bullet doesn't route scope "
            "violations into BUG_FOUND — they'd be misinterpreted as "
            "'test itself is wrong'"
        )


# ============================================================================
# REVIEWER.MD
# ============================================================================


class TestReviewerSpecVsPrDescriptionComparison:
    """The reviewer must perform a mandatory spec-vs-PR-description
    comparison. The Method has a dedicated step for it; pin the step's
    existence and the load-bearing severity rules."""

    def test_method_has_eight_steps_not_seven(self, reviewer_prompt):
        """Old Method was 7 steps; F-006-U-6 inserted a new step 3
        (spec-vs-PR comparison), pushing the count to 8. Pin the header
        so a future trim that drops the step doesn't silently regress."""
        assert re.search(r"## The Method.{0,30}8 steps", reviewer_prompt), (
            "reviewer.md's Method header should say '8 steps' after F-006-U-6 "
            "(was 7 before this unit)"
        )

    def test_spec_vs_pr_step_exists_and_is_mandatory(self, reviewer_prompt):
        """A dedicated Method step must perform the spec-vs-PR
        comparison. Look for the heading 'Spec-vs-PR-description
        comparison' and the word 'mandatory' nearby — the rule must be
        non-optional."""
        idx = reviewer_prompt.find("Spec-vs-PR-description comparison")
        assert idx != -1, (
            "reviewer.md is missing the 'Spec-vs-PR-description comparison' "
            "Method step required by F-006-U-6"
        )
        # window: the heading line and the immediate explanation
        window = reviewer_prompt[idx : idx + 200]
        assert re.search(r"(?i)mandatory", window), (
            "spec-vs-PR-description comparison step is not marked mandatory"
        )

    def test_undocumented_deviation_is_red_finding(self, reviewer_prompt):
        """The unit description says undocumented deviations from the
        spec are 🔴. Pin the severity assignment so a relaxation can't
        slip in silently."""
        # The relevant text: "diff changes that diverge from the spec but
        # the PR description is silent about → 🔴 (undocumented deviation)"
        # Use a loose multi-line match.
        m = re.search(
            r"undocumented deviation.{0,80}🔴|🔴.{0,80}undocumented deviation|"
            r"PR description is silent.{0,200}🔴|🔴.{0,200}undocumented",
            reviewer_prompt,
            re.DOTALL,
        )
        assert m is not None, "reviewer.md doesn't classify undocumented spec deviations as 🔴"

    def test_missing_spec_satisfaction_when_spec_present_is_red(self, reviewer_prompt):
        """If the spec was injected but the PR description omits the
        `## Spec satisfaction` section entirely, that's a 🔴 — the
        coder skipped the alignment step. Pin the severity."""
        # Look for the rule in any phrasing.
        # Diff text: "Missing `## Spec satisfaction` when `## FEATURE
        # SPEC` was provided → 🔴"
        assert re.search(
            r"Missing `## Spec satisfaction`.{0,80}🔴|🔴.{0,80}Missing `## Spec satisfaction`|"
            r"empty PR description on a spec'd feature.{0,200}🔴",
            reviewer_prompt,
            re.DOTALL,
        ), (
            "reviewer.md doesn't classify a missing `## Spec satisfaction` "
            "section as 🔴 when a spec was provided"
        )


class TestReviewerReadsCycleLogFirstOnRetry:
    """Reviewer must read `## THIS UNIT'S CYCLE LOG` first on retry
    cycles. Without this, the reviewer would re-flag findings the coder
    already resolved (the over-anchoring failure mode F-012-U-2 already
    warned about — F-006-U-6's cycle-log read is the structural fix)."""

    def test_this_units_cycle_log_block_documented(self, reviewer_prompt):
        assert "## THIS UNIT'S CYCLE LOG" in reviewer_prompt, (
            "reviewer.md doesn't document the `## THIS UNIT'S CYCLE LOG` context block"
        )

    def test_cycle_log_marked_retry_only(self, reviewer_prompt):
        """The block is injected only on retry cycles (review_round ≥ 2).
        Pin the condition so a future edit doesn't accidentally promise
        the block on the first cycle, confusing the agent when it's
        absent."""
        idx = reviewer_prompt.find("## THIS UNIT'S CYCLE LOG")
        # Window around the bullet describing the block
        window = reviewer_prompt[idx : idx + 400]
        assert re.search(r"(?i)retry cycle|review_round\s*[≥>]=?\s*2", window), (
            "cycle-log block description doesn't mark it as retry-only"
        )

    def test_read_first_on_retry_instruction_present(self, reviewer_prompt):
        """The instruction "Read this FIRST on retry" must appear near
        the cycle-log block description so the reviewer knows the
        ordering — predecessor-aware reviewing requires the prior cycle's
        findings to be inspected before the new diff is read."""
        idx = reviewer_prompt.find("## THIS UNIT'S CYCLE LOG")
        window = reviewer_prompt[idx : idx + 500]
        assert re.search(r"(?i)read.{0,30}FIRST|FIRST.{0,30}retry", window), (
            "reviewer.md doesn't instruct 'read this FIRST on retry' for "
            "the cycle-log block — re-flagging resolved findings is the "
            "regression this rule prevents"
        )


class TestReviewerPredecessorConsistencyCheck:
    """The reviewer must run the predecessor consistency check —
    silent divergence from a merged predecessor's choice is at least 🟠
    (the coder needs to either align or document the divergence)."""

    def test_predecessor_consistency_check_step_exists(self, reviewer_prompt):
        assert re.search(
            r"(?i)predecessor consistency check|Predecessor consistency",
            reviewer_prompt,
        ), "reviewer.md doesn't define a 'Predecessor consistency check' as required by F-006-U-6"

    def test_predecessor_consistency_check_severity_is_orange(self, reviewer_prompt):
        """Per the proposal: silent predecessor divergence is 🟠 (not 🔴 —
        the coder may have a good reason; the fix is to surface it).
        Pin the severity."""
        # Find the section.
        idx = reviewer_prompt.lower().find("predecessor consistency")
        assert idx != -1
        # Window: the paragraph that explains the severity assignment
        window = reviewer_prompt[idx : idx + 600]
        assert "🟠" in window, (
            "predecessor consistency check should assign 🟠 (not 🔴 / 🟡) "
            "when divergence is silent — wrong severity changes which "
            "fix-loop the orchestrator runs"
        )

    def test_method_step_renumbering_is_consistent(self, reviewer_prompt):
        """When step 3 was inserted, the old steps 3-7 shifted to 4-8.
        The Red Flags section references those steps by number — if
        renumbering missed a reference, the agent ends up pointed at the
        wrong step.
        """
        # Find the Red Flags section.
        rf_idx = reviewer_prompt.find("Red Flags")
        assert rf_idx != -1, "Red Flags section missing"
        rf_section = reviewer_prompt[rf_idx : rf_idx + 2000]
        # Each Red Flag mentions a step number; collect them all.
        step_refs = re.findall(r"\(step (\d+)\)", rf_section)
        assert step_refs, "Red Flags section doesn't reference any Method steps"
        for n in step_refs:
            n_int = int(n)
            # Steps must be in range 1..8 after the renumber.
            assert 1 <= n_int <= 8, (
                f"Red Flags references 'step {n_int}' but the Method has 8 "
                f"steps after F-006-U-6 — stale step reference detected"
            )
            # And no reference should be to step 7 (the old final
            # "Sanity-check tests" step) unless it now means the new
            # step-7 (Diff the deletions). The simplest invariant: every
            # step number that appears must point to an existing step
            # in the Method.
        # Cross-check: every cited step is actually present as a heading
        # in the Method.
        method_idx = reviewer_prompt.find("## The Method")
        next_top = reviewer_prompt.find("\n## ", method_idx + 1)
        method_section = reviewer_prompt[method_idx:next_top]
        for n in step_refs:
            assert re.search(rf"^### {n}\. ", method_section, re.MULTILINE), (
                f"Red Flags references 'step {n}' but no '### {n}.' heading "
                f"exists in the Method section — step renumbering is incomplete"
            )


# ============================================================================
# CLAUDE.MD
# ============================================================================


class TestClaudeMdFeatureMemoryRule:
    """The lead persona must call `feature_memory(F-X)` at session start
    before discussing a feature. Without this rule, a fresh session
    re-reads stale chat history instead of the durable spec.md + cycle
    logs — the entire F-006 premise."""

    def test_feature_memory_session_start_rule_present(self, claude_md):
        """Pin both halves of the rule: the call (`feature_memory(F-X)`)
        and the trigger ("at session start" / "before discussing")."""
        assert "feature_memory(F" in claude_md, (
            "CLAUDE.md doesn't mention the `feature_memory(F-X)` call"
        )
        # And it must be framed as session-start / before-discussing.
        # Look near the call site.
        # Scan all occurrences and check at least one has the trigger
        # nearby (in either direction).
        found = False
        for m in re.finditer(r"feature_memory\(F", claude_md):
            window = claude_md[max(m.start() - 200, 0) : m.start() + 400]
            if re.search(
                r"(?i)session start|before discussing|fresh session|"
                r"start of a fresh conversation|re-bootstrap",
                window,
            ):
                found = True
                break
        assert found, (
            "CLAUDE.md mentions `feature_memory(F-X)` but never frames it "
            "as a session-start / pre-discussion call — the trigger is the "
            "load-bearing half of the rule"
        )

    def test_feature_memory_rule_lives_in_discipline_section(self, claude_md):
        """The rule should be grouped with the other F-006 disciplines
        (spec-edit + cycle-log read-only). A wandering bullet in the
        MCP-tool catalog wouldn't be visible to the lead at session
        start. Pin that a dedicated section exists.
        """
        # Look for a header that scopes the F-006 rules. The diff added
        # "### Feature spec + cycle log discipline (F-006)".
        assert re.search(
            r"###?\s+.*(?:Feature spec|cycle log|F-006).*(?:discipline|rules|notes)",
            claude_md,
            re.IGNORECASE,
        ), (
            "CLAUDE.md is missing a grouped discipline section for the F-006 "
            "lead-persona rules — feature_memory / spec edits / cycle-log "
            "read-only should live together so the lead reads them as a unit"
        )


class TestClaudeMdSpecEditDiscipline:
    """Lead must edit `spec.md` on non-obvious decisions and commit with
    a `Why:` line. The git log of spec.md IS the decision log; no
    separate decisions.md."""

    def test_spec_md_edit_rule_present(self, claude_md):
        """The instruction to edit `features/F-XXX/spec.md` on
        non-obvious decisions must be explicit."""
        # Look for "Edit ... spec.md" or "spec.md whenever you make a ..."
        assert re.search(
            r"(?i)edit\s+`?features/F-[X\d]+/spec\.md`?|spec\.md whenever|"
            r"edit\s+`?spec\.md`?",
            claude_md,
        ), (
            "CLAUDE.md doesn't tell the lead to edit `features/F-XXX/spec.md` "
            "on non-obvious decisions"
        )

    def test_why_commit_line_required_on_spec_edits(self, claude_md):
        """The `Why:` line is the actual decision log. Pin the rule."""
        assert "Why:" in claude_md, (
            "CLAUDE.md doesn't reference the `Why:` commit-message line required on spec.md edits"
        )
        # And it must be in the context of spec.md / commit message.
        why_idx = claude_md.find("Why:")
        window = claude_md[max(why_idx - 400, 0) : why_idx + 400]
        assert re.search(r"(?i)spec\.md|commit", window), (
            "`Why:` is mentioned in CLAUDE.md but not in the context of spec.md commit messages"
        )

    def test_no_separate_decisions_md_rule(self, claude_md):
        """Pin the explicit rejection of a separate `decisions.md` —
        otherwise a future contributor might re-introduce the redundancy
        the proposal explicitly rejected."""
        # Look for any phrasing: "no separate `decisions.md`",
        # "no decisions.md", "git log of spec.md IS the decision log".
        assert re.search(
            r"(?i)no separate\s+`?decisions\.md`?|git log of `?spec\.md`?\s+(?:IS|is)|"
            r"decision log",
            claude_md,
        ), (
            "CLAUDE.md doesn't reject the separate `decisions.md` pattern — "
            "lead may re-introduce the redundancy the proposal rejected"
        )


class TestClaudeMdCycleLogsAreReadOnly:
    """Cycle logs (features/F-XXX/U-N.md) are read-only history. To
    revise a past decision, the lead edits spec.md (the canonical
    source) — never the cycle log."""

    def test_cycle_log_read_only_rule_present(self, claude_md):
        """Look for the phrase 'read-only' near 'cycle log' — the rule
        is short and unambiguous; either it's there or it isn't."""
        # Locate every "cycle log" mention and look for read-only nearby.
        found = False
        for m in re.finditer(r"(?i)cycle log", claude_md):
            window = claude_md[max(m.start() - 100, 0) : m.start() + 400]
            if re.search(r"(?i)read-only|immutable", window):
                found = True
                break
        assert found, "CLAUDE.md doesn't mark cycle logs as read-only history"

    def test_revise_spec_not_cycle_log_instruction(self, claude_md):
        """The 'revise spec.md, never the cycle log' instruction is what
        makes the rule actionable. Without it, the lead might edit a
        cycle log to 'fix' a past decision."""
        # Phrasing: "revise spec.md, never the cycle log" or similar.
        assert re.search(
            r"(?i)edit\s+`?spec\.md`?[^\n]{0,80}never the cycle log|"
            r"revise\s+`?spec\.md`?|edit `?spec\.md`?.*not.*cycle log|"
            r"never the cycle log",
            claude_md,
            re.DOTALL,
        ), (
            "CLAUDE.md doesn't tell the lead to revise spec.md instead of "
            "editing the cycle log when correcting a past decision"
        )


# ============================================================================
# Cross-cutting integration sanity
# ============================================================================


class TestCrossPromptConsistency:
    """The three context-block names — `## FEATURE SPEC`, `## PREDECESSOR
    UNITS`, `## THIS UNIT'S CYCLE LOG` — are the orchestrator's contract.
    Drift between prompts (e.g. coder calls it `## SPEC` while reviewer
    calls it `## FEATURE SPEC`) breaks injection. Pin the canonical
    spelling across all three files."""

    @pytest.mark.parametrize(
        "block_name, who_reads_it",
        [
            ("## FEATURE SPEC", ("coder", "tester", "reviewer")),
            ("## PREDECESSOR UNITS", ("coder", "tester", "reviewer")),
        ],
    )
    def test_context_block_spelled_identically_across_roles(
        self, coder_prompt, tester_prompt, reviewer_prompt, block_name, who_reads_it
    ):
        prompts = {"coder": coder_prompt, "tester": tester_prompt, "reviewer": reviewer_prompt}
        for role in who_reads_it:
            assert block_name in prompts[role], (
                f"{role}.md is missing the `{block_name}` context block — "
                f"the orchestrator's compose_*_task injection would land "
                f"unread"
            )

    def test_cycle_log_block_only_in_reviewer(self, coder_prompt, tester_prompt, reviewer_prompt):
        """Per the proposal, `## THIS UNIT'S CYCLE LOG` is reviewer-only
        (injected on retry cycles for the delta-review path). Coder and
        tester get the spec + predecessor blocks but not their own
        cycle log — otherwise the message bloats and the agents would
        try to act on findings they shouldn't see (the coder's own
        prior cycle is in their session memory; re-injecting it would
        bias the fix)."""
        assert "## THIS UNIT'S CYCLE LOG" in reviewer_prompt
        assert "## THIS UNIT'S CYCLE LOG" not in coder_prompt, (
            "coder.md references `## THIS UNIT'S CYCLE LOG` — that block is "
            "reviewer-only per the proposal's role-prompt table"
        )
        assert "## THIS UNIT'S CYCLE LOG" not in tester_prompt, (
            "tester.md references `## THIS UNIT'S CYCLE LOG` — that block is "
            "reviewer-only per the proposal's role-prompt table"
        )

    def test_spec_satisfaction_section_referenced_by_both_coder_and_reviewer(
        self, coder_prompt, reviewer_prompt
    ):
        """`## Spec satisfaction` is the contract surface between coder
        and reviewer — the coder writes it in the PR body, the reviewer
        reads it during step 3. Both prompts must name the section
        identically."""
        assert "## Spec satisfaction" in coder_prompt, (
            "coder.md doesn't reference the `## Spec satisfaction` section (producer side)"
        )
        assert "## Spec satisfaction" in reviewer_prompt, (
            "reviewer.md doesn't reference the `## Spec satisfaction` section (consumer side)"
        )
