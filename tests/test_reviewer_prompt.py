"""Pin the reviewer prompt's load-bearing structure + delta-scenario fixtures.

Mirrors ``tests/test_coder_prompt.py``: the agent itself isn't unit-tested
(Managed Agents are external), but the prompt invariants — section
presence, terminal-marker contract, reconciliation vocabulary — are. If
anyone edits ``orchestrator/prompts/reviewer.md`` in a way that drops
these, CI catches it.

The F-012-U-2 fixtures (``tests/fixtures/reviewer_delta_scenarios.json``)
encode 10 retry scenarios spanning the (fix_coverage × new_findings) matrix.
The structural assertions here keep the fixture file honest; the actual
eval is run out-of-band against a real Managed Agents session (no
deterministic local equivalent).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = REPO_ROOT / "orchestrator" / "prompts"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "reviewer_delta_scenarios.json"


@pytest.fixture(scope="module")
def reviewer_prompt() -> str:
    return (PROMPT_DIR / "reviewer.md").read_text()


@pytest.fixture(scope="module")
def delta_scenarios() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


# --------------------------- On-delta-re-review section ---------------------------


class TestOnDeltaReReviewSection:
    """F-012-U-2 adds an 'On delta re-review' section to reviewer.md that
    instructs the agent how to handle a resumed session on retry. The
    section must cover four contracts so the delta path is safe to default
    on: skip-inventory, re-diff-only, reconcile-each, fresh-marker."""

    def test_section_heading_present(self, reviewer_prompt):
        assert "On delta re-review" in reviewer_prompt, (
            "missing 'On delta re-review' section — delta resume path has no agent guidance"
        )

    def test_skip_full_inventory_called_out(self, reviewer_prompt):
        section = _delta_section(reviewer_prompt)
        # The agent must be told to skip the Method's step 1 (clone+inventory).
        # Match either phrasing — section heading reference or git-fetch hint.
        assert re.search(r"Skip Method step 1|clone/inventory|just `git fetch`", section), (
            "delta section must tell the agent to skip the cold-start inventory step"
        )

    def test_diff_only_prior_to_current(self, reviewer_prompt):
        section = _delta_section(reviewer_prompt)
        # The diff-range guidance is what enables the latency win.
        assert "PRIOR_SHA" in section
        assert "CURRENT_SHA" in section
        assert re.search(r"PRIOR_SHA\.\.CURRENT_SHA|git diff PRIOR_SHA\.\.CURRENT_SHA", section), (
            "delta section must scope the diff to PRIOR_SHA..CURRENT_SHA"
        )

    def test_reconciliation_vocabulary_present(self, reviewer_prompt):
        """Each prior finding must be reconciled as RESOLVED / NOT_RESOLVED / N/A.
        These exact tokens are the contract — the reconciliation table in the
        prompt uses them, and the fixtures encode them as `diff_signal` values.
        """
        section = _delta_section(reviewer_prompt)
        assert "RESOLVED" in section
        assert "NOT_RESOLVED" in section
        assert "N/A" in section

    def test_anti_anchoring_both_directions(self, reviewer_prompt):
        """The section must warn against both over-anchoring (false
        REQUEST_CHANGES) AND capitulation (false RECOMMEND_MERGE). Naming
        only one direction is worse than naming neither — the agent will
        drift the *other* way to compensate."""
        section = _delta_section(reviewer_prompt)
        # Cover both failure modes verbatim or via clearly named alternatives.
        assert re.search(r"over-anchoring|Over-anchoring", section), (
            "missing over-anchoring warning (false REQUEST_CHANGES)"
        )
        assert re.search(r"[Cc]apitulation|false `RECOMMEND_MERGE`", section), (
            "missing capitulation warning (false RECOMMEND_MERGE)"
        )

    def test_fresh_terminal_marker_required(self, reviewer_prompt):
        """The orchestrator treats the *new* marker as the verdict; the
        prior one is history. Silence locks the cap-3 loop. The prompt
        must say so."""
        section = _delta_section(reviewer_prompt)
        assert re.search(r"fresh terminal marker|fresh marker|new verdict", section), (
            "delta section must require a fresh terminal marker for the current state"
        )

    def test_delta_section_lists_all_terminal_markers(self, reviewer_prompt):
        """All four valid markers must appear so the agent doesn't drop
        one (in particular: a REVIEW_COMMENT path for delta-introduced 🟡-only
        cases)."""
        section = _delta_section(reviewer_prompt)
        assert "REVIEW_RECOMMEND_MERGE" in section
        assert "REVIEW_REQUEST_CHANGES" in section
        assert "REVIEW_COMMENT" in section


# --------------------------- delta-scenario fixtures ---------------------------


class TestDeltaScenarioFixtures:
    """Structural assertions on the 10-scenario eval set. The eval itself
    runs out-of-band; CI just keeps the file honest (count, required
    fields, marker values in the valid set, no duplicate ids)."""

    EXPECTED_COUNT = 10
    REQUIRED_KEYS = {
        "id",
        "fix_coverage",
        "new_findings",
        "description",
        "prior_findings",
        "coder_fix_summary",
        "diff_signal",
        "expected_marker",
        "expected_reason_hint",
    }
    VALID_MARKERS = {"REVIEW_RECOMMEND_MERGE", "REVIEW_REQUEST_CHANGES", "REVIEW_COMMENT"}
    VALID_FIX_COVERAGE = {"all", "some", "none"}
    VALID_NEW_FINDINGS = {"none", "blocking", "nit"}

    def test_count_is_ten(self, delta_scenarios):
        scenarios = delta_scenarios["scenarios"]
        assert len(scenarios) == self.EXPECTED_COUNT, (
            f"fixtures must hold exactly {self.EXPECTED_COUNT} scenarios "
            f"(coder fixed all/some/none × {{none, blocking, nit}} new findings); "
            f"got {len(scenarios)}"
        )

    def test_required_keys_present(self, delta_scenarios):
        for s in delta_scenarios["scenarios"]:
            missing = self.REQUIRED_KEYS - set(s.keys())
            assert not missing, f"scenario {s.get('id', '?')} missing keys: {missing}"

    def test_ids_unique(self, delta_scenarios):
        ids = [s["id"] for s in delta_scenarios["scenarios"]]
        assert len(set(ids)) == len(ids), f"duplicate scenario ids: {ids}"

    def test_markers_are_valid(self, delta_scenarios):
        for s in delta_scenarios["scenarios"]:
            assert s["expected_marker"] in self.VALID_MARKERS, (
                f"scenario {s['id']} has invalid marker {s['expected_marker']!r}; "
                f"must be one of {self.VALID_MARKERS}"
            )

    def test_fix_coverage_axis_covered(self, delta_scenarios):
        """All three fix_coverage values must appear at least once across
        the 10 scenarios — that's the axis the unit description called
        out explicitly (`coder fixed all/some/none of prior findings`)."""
        seen = {s["fix_coverage"] for s in delta_scenarios["scenarios"]}
        assert seen == self.VALID_FIX_COVERAGE, (
            f"fix_coverage axis incomplete — saw {seen}, want {self.VALID_FIX_COVERAGE}"
        )

    def test_new_findings_axis_covered(self, delta_scenarios):
        seen = {s["new_findings"] for s in delta_scenarios["scenarios"]}
        assert seen == self.VALID_NEW_FINDINGS, (
            f"new_findings axis incomplete — saw {seen}, want {self.VALID_NEW_FINDINGS}"
        )

    def test_prior_findings_nonempty(self, delta_scenarios):
        """A retry implies the prior turn emitted REVIEW_REQUEST_CHANGES,
        which implies at least one prior finding. An empty list breaks
        the reconciliation scenario."""
        for s in delta_scenarios["scenarios"]:
            assert s["prior_findings"], (
                f"scenario {s['id']} has no prior_findings — retry without prior verdict makes no sense"
            )

    def test_diff_signal_reconciles_each_prior_finding(self, delta_scenarios):
        """diff_signal must reconcile every prior finding (F1..Fn) — the
        reconciliation contract is per-finding, not aggregate. Extra
        keys (new_critical, new_nit, etc.) are allowed for delta-
        introduced issues."""
        for s in delta_scenarios["scenarios"]:
            prior_keys = {f"F{i + 1}" for i in range(len(s["prior_findings"]))}
            signal_keys = set(s["diff_signal"].keys())
            missing = prior_keys - signal_keys
            assert not missing, (
                f"scenario {s['id']} diff_signal doesn't reconcile prior findings {missing}"
            )

    def test_reconciliation_statuses_use_vocabulary(self, delta_scenarios):
        """Per-finding statuses in diff_signal must start with one of
        RESOLVED / NOT_RESOLVED / N/A so the eval-runner can grep verdicts
        deterministically."""
        valid_prefixes = ("RESOLVED", "NOT_RESOLVED", "N/A")
        for s in delta_scenarios["scenarios"]:
            for i in range(len(s["prior_findings"])):
                value = s["diff_signal"][f"F{i + 1}"]
                assert value.startswith(valid_prefixes), (
                    f"scenario {s['id']} finding F{i + 1} status {value!r} doesn't start with one of {valid_prefixes}"
                )


# --------------------------- helpers ---------------------------


def _delta_section(prompt: str) -> str:
    """Return the body of the 'On delta re-review' section.

    Bounded by the next top-level header to avoid pulling in unrelated
    sections (Red Flags / Hard rules / etc.).
    """
    start = prompt.find("## On delta re-review")
    assert start != -1, "section header missing"
    # Look for the next ## section
    rest = prompt[start + len("## On delta re-review") :]
    next_header = re.search(r"\n## ", rest)
    end = next_header.start() if next_header else len(rest)
    return rest[:end]
