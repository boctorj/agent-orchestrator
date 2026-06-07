"""F-017-U-1 tester tests — acceptance criteria + "no changes to" pins.

Supplementary to ``tests/test_f017_u1_tldr.py`` (the coder's own
verification suite). This module focuses on the parts of the spec the
coder's tests don't pin explicitly:

  1. **Allow-list / regex pins** — the unit description says "No
     changes to ``cycle_log.py``'s ``_CYCLE_LOG_SECTION_AFTER_PR_RE``
     allow-list; no changes to ``_render_context_blocks`` or
     ``execution.py:57``." If a later refactor silently moves the
     TL;DR heading into the strip range or renames the predecessor-
     summary call site, the coder-side format pins still pass but
     production injection breaks. Pin them here.
  2. **TBD placeholder exact string** — spec § Acceptance #4 fixes the
     wording (``"_TBD — coder did not fill in this sub-section._"``
     including the em-dash and italic markers). Coder tests check
     "placeholder appears" but not "exact spelling matches the spec."
  3. **Strict heading match** — spec § Decisions / § Open questions
     ("Leaning strict for now — it forces coder-prompt discipline").
     Variant sub-headings (``### What changed``, ``### Downstream
     interface``) must NOT be auto-mirrored; the cycle log shows TBD
     so the coder discovers the mistake at PR-review time, not when
     a downstream worker silently inherits the wrong information.
  4. **End-to-end via writer** — ``write_cycle_log`` → ``read_cycle_log``
     returns the full file with TL;DR present; ``cycle_log_summary``
     returns the slice with TL;DR present and the PR-description block
     dropped. This is the actual production data flow the spec § Intent
     ¶3 promises ("predecessor injection automatically gets the new
     content with zero changes to execution.py or _render_context_blocks").
  5. **Insertion-point structural pin** — no ``## `` heading sits
     between ``## PR`` and ``## TL;DR``, and none between ``## TL;DR``
     and ``## Coder's PR description``. The format ordering needs the
     TL;DR to be *immediately* adjacent to the PR-description anchor
     so the existing strip semantics don't sweep it up.
  6. **Coder prompt mirrors the TBD wording** — so the coder reading
     the prompt sees what shows up in their cycle log when they skip
     a sub-section. Without this, the coder may not realize the
     auto-fill writer produced placeholders.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
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
from orchestrator.tools import _render_context_blocks, execution


# --------------------------- shared fakes ---------------------------


@dataclass
class _FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _FakeRunner:
    """Records argv; returns canned responses by prefix.

    Mirrors ``tests/test_cycle_log.py::FakeRunner`` so the production
    writer code path (``gh pr view`` + ``gh api graphql`` mirrors,
    ``git add`` / ``git commit``) runs without touching the real shell.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: list[tuple[tuple[str, ...], _FakeProc]] = []

    def register(self, prefix: tuple[str, ...], proc: _FakeProc) -> None:
        self._responses.append((prefix, proc))

    def __call__(self, argv, **_kwargs) -> _FakeProc:
        self.calls.append(list(argv))
        for prefix, proc in self._responses:
            if tuple(argv[: len(prefix)]) == prefix:
                return proc
        return _FakeProc()


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


# --------------------------- 1. allow-list / "no changes to" pins -------------


class TestUnchangedAllowListAndCallSite:
    """The unit description forbids changes to three specific touch-points.

    These tests guard against silent regressions: if a future refactor
    bundles a "tidy up the regex" or "rename _predecessor_summaries"
    change into an F-017 follow-up, the strip semantics break or the
    injection path stops calling ``cycle_log_summary``. Both would
    silently degrade the predecessor block; neither would be caught
    by the coder-side format pins."""

    def test_cycle_log_section_after_pr_regex_pattern_is_pinned(self) -> None:
        """Allow-list unchanged: ``Cycle history|Review threads|Spec deviations|Links``.

        Per the unit description: "No changes to ``cycle_log.py``'s
        ``_CYCLE_LOG_SECTION_AFTER_PR_RE`` allow-list". The TL;DR
        section MUST NOT be added to the regex — that would move the
        TL;DR back into the strip range and silently drop it from
        predecessor injection."""
        pattern = cycle_log._CYCLE_LOG_SECTION_AFTER_PR_RE.pattern
        assert pattern == r"^## (Cycle history|Review threads|Spec deviations|Links)\b", (
            "regex allow-list mutated; expected unchanged per F-017 § Approach "
            "and the unit description's 'no changes to ... allow-list' constraint"
        )
        # And specifically NOT TL;DR — that would re-include TL;DR in
        # the strip range and break the whole feature.
        assert "TL;DR" not in pattern, "TL;DR must NOT join the strip allow-list"

    def test_render_context_blocks_signature_unchanged(self) -> None:
        """``_render_context_blocks`` keeps the three keyword-only params it had pre-F-017.

        The unit description says "no changes to ``_render_context_blocks``";
        F-017 lives in the writer, not the injection helper. If the
        helper grows a new param to "handle TL;DR specially", that's a
        scope creep that breaks the spec's "zero changes to execution.py
        or _render_context_blocks" promise (spec § Intent ¶3)."""
        sig = inspect.signature(_render_context_blocks)
        params = list(sig.parameters.values())
        # All keyword-only per the existing call sites.
        names = [p.name for p in params]
        assert names == ["feature_spec_text", "predecessor_logs", "own_cycle_log"], (
            f"_render_context_blocks signature changed: got params {names}; "
            "unit description forbids touching this helper"
        )
        for p in params:
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"param {p.name} no longer keyword-only; existing call sites "
                "rely on the kwargs form"
            )

    def test_predecessor_summaries_still_calls_cycle_log_summary(
        self, monkeypatch
    ) -> None:
        """``execution._predecessor_summaries`` must invoke ``cycle_log.cycle_log_summary``.

        Unit description: "no changes to ... ``execution.py:57``." The
        line number may have drifted across refactors (currently
        ``_predecessor_summaries`` at line 56-65), but the call shape
        is the contract — it builds ``[(dep_id, cycle_log_summary(dep_id))]``.
        If a future change swaps ``cycle_log_summary`` for
        ``read_cycle_log`` or a new helper, predecessor injection
        re-grows ~75% and the feature is undone."""
        calls: list[str] = []

        def _spy(unit_id: str, *, base_dir=None) -> str:
            calls.append(unit_id)
            return f"summary-of-{unit_id}"

        monkeypatch.setattr(cycle_log, "cycle_log_summary", _spy)
        # And monkeypatch the symbol on the execution module too — the
        # call site does ``cycle_log.cycle_log_summary(...)`` so patching
        # on ``cycle_log`` is sufficient, but be defensive.

        unit = WorkUnit(
            id="F-017-U-1",
            feature_id="F-017",
            title="t",
            description="d",
            depends_on=["F-017-U-0", "F-016-U-5"],
        )
        result = execution._predecessor_summaries(unit)
        assert calls == ["F-017-U-0", "F-016-U-5"], (
            f"expected cycle_log_summary called once per dep in declared order; got {calls}"
        )
        assert result == [
            ("F-017-U-0", "summary-of-F-017-U-0"),
            ("F-016-U-5", "summary-of-F-016-U-5"),
        ]


# --------------------------- 2. TBD placeholder exact wording -------------


class TestTbdPlaceholderExactWording:
    """The spec fixes the placeholder string verbatim — pin it.

    Spec § Acceptance #4 (verbatim): "the writer emits the sub-heading
    with a ``_TBD — coder did not fill in this sub-section._``
    placeholder." If anyone retypes the dash as a hyphen, drops the
    italic underscores, or rewords ("not provided" / "missing"), the
    coder-side test `test_missing_subsection_emits_tbd_placeholder`
    still passes because it checks against the constant. This test
    pins the constant *against the spec wording* so the contract
    survives a "let's reword for clarity" change."""

    def test_placeholder_string_matches_spec_wording_verbatim(self) -> None:
        assert TLDR_PLACEHOLDER == "_TBD — coder did not fill in this sub-section._", (
            f"TBD placeholder no longer matches F-017 § Acceptance #4 wording; "
            f"got {TLDR_PLACEHOLDER!r}"
        )

    def test_placeholder_uses_em_dash_not_hyphen(self) -> None:
        """Em-dash (U+2014) — not a plain hyphen — is what the spec wrote."""
        assert "—" in TLDR_PLACEHOLDER, "expected em-dash (U+2014) in placeholder"
        assert " - " not in TLDR_PLACEHOLDER, "plain hyphen-space-hyphen is not the spec wording"

    def test_placeholder_is_italic(self) -> None:
        """Markdown italic markers (``_..._``) surround the whole placeholder."""
        assert TLDR_PLACEHOLDER.startswith("_") and TLDR_PLACEHOLDER.endswith("_"), (
            "spec wording wraps the placeholder in single-underscore italics"
        )


# --------------------------- 3. strict heading match ---------------------


class TestStrictHeadingMatch:
    """Variant sub-heading names must NOT be auto-mirrored.

    Per spec § Open questions: "Leaning strict for now — it forces
    coder-prompt discipline. Revisit if reviewers report frequent
    mismatches." A lax match (e.g. fuzzy / synonym-aware) would let
    the coder ship ``### What changed`` and have it silently copied
    into the cycle log under ``### What shipped``, which would
    mis-name the contract for every downstream worker that reads it."""

    def test_variant_subheading_names_yield_placeholder(self, tmp_state_db: Path) -> None:
        _seed_unit()
        body = (
            "## TL;DR\n\n"
            "### What changed\n"  # variant — must not match "### What shipped"
            "VARIANT-WHAT-MARKER\n\n"
            "### Downstream interface\n"  # variant — must not match
            "VARIANT-CONTRACT-MARKER\n\n"
            "### Notable decisions\n"  # variant
            "VARIANT-DECISIONS-MARKER\n\n"
        )
        md = render_cycle_log(
            "F-017-U-1",
            pr_info={"body": body, "headRefOid": "abc"},
            review_threads=[],
        )
        # All three CANONICAL sub-headings still present (skeleton).
        for sub in TLDR_SUBHEADINGS:
            assert sub in md, f"canonical sub-heading {sub!r} missing"
        # All three CANONICAL sub-headings show TBD placeholder.
        assert md.count(TLDR_PLACEHOLDER) == len(TLDR_SUBHEADINGS), (
            "variant heading names must NOT be mirrored as canonical content"
        )
        # And the variant content text MUST NOT appear in the cycle log
        # TL;DR block — only inside the verbatim PR description copy
        # (which sits below the TL;DR section). Specifically, the
        # markers must not appear *before* the PR-description heading.
        idx_desc = md.find(PR_DESCRIPTION_HEADING)
        prefix = md[:idx_desc]
        assert "VARIANT-WHAT-MARKER" not in prefix, (
            "variant heading content leaked into TL;DR section"
        )
        assert "VARIANT-CONTRACT-MARKER" not in prefix, (
            "variant heading content leaked into TL;DR section"
        )
        assert "VARIANT-DECISIONS-MARKER" not in prefix, (
            "variant heading content leaked into TL;DR section"
        )


# --------------------------- 4. end-to-end via writer ---------------------


class TestEndToEndWriterReadSummary:
    """Spec § Intent ¶3 promises predecessor injection just works:
    write_cycle_log produces a file whose ``cycle_log_summary`` slice
    contains TL;DR (sans PR description), and whose ``read_cycle_log``
    return value contains both."""

    PR_BODY = (
        "**Unit ID:** F-017-U-1\n\n"
        "## TL;DR\n\n"
        "### What shipped\n"
        "E2E-WRITER-WHAT\n\n"
        "### Downstream contract\n"
        "E2E-WRITER-CONTRACT\n\n"
        "### Decisions worth knowing\n"
        "E2E-WRITER-DECISIONS\n\n"
        "## What this change does\n"
        "VERBATIM-PR-DESCRIPTION-LONG-PROSE\n"
    )

    def _make_runner_with_pr_body(self, pr_body: str) -> _FakeRunner:
        runner = _FakeRunner()
        runner.register(
            ("gh", "pr", "view"),
            _FakeProc(
                stdout=json.dumps(
                    {"title": "T", "body": pr_body, "headRefOid": "deadbeef"}
                )
            ),
        )
        runner.register(
            ("gh", "api", "graphql"),
            _FakeProc(
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {"reviewThreads": {"nodes": []}}
                            }
                        }
                    }
                )
            ),
        )
        return runner

    def test_write_then_read_returns_full_file_including_tldr(
        self, tmp_path: Path, tmp_state_db: Path, monkeypatch
    ) -> None:
        """``read_cycle_log`` returns the full audit file (spec § Acceptance #3)."""
        _seed_unit()
        # Override base dir so read_cycle_log and write_cycle_log agree.
        monkeypatch.setattr(cycle_log, "cycle_log_base_dir", lambda: tmp_path)
        runner = self._make_runner_with_pr_body(self.PR_BODY)

        target = cycle_log.write_cycle_log(
            "F-017-U-1", base_dir=tmp_path, run=runner
        )
        assert target.is_file(), "writer didn't materialize the cycle log"

        full = cycle_log.read_cycle_log("F-017-U-1", base_dir=tmp_path)
        # Full file has the TL;DR section AND the verbatim PR description.
        assert TLDR_HEADING in full
        for sub in TLDR_SUBHEADINGS:
            assert sub in full
        assert "E2E-WRITER-WHAT" in full
        assert "E2E-WRITER-CONTRACT" in full
        assert "E2E-WRITER-DECISIONS" in full
        # PR description block survives in the full file (only the
        # summary view strips it).
        assert PR_DESCRIPTION_HEADING in full
        assert "VERBATIM-PR-DESCRIPTION-LONG-PROSE" in full

    def test_write_then_summary_strips_pr_description_but_keeps_tldr(
        self, tmp_path: Path, tmp_state_db: Path, monkeypatch
    ) -> None:
        """``cycle_log_summary`` keeps TL;DR + cycle history; drops PR description.

        This is the actual production contract: the predecessor block
        carries the TL;DR sub-section bodies through to a downstream
        coder/tester/reviewer prompt. Verified end-to-end (write →
        summary) rather than against a hand-crafted log body."""
        _seed_unit()
        monkeypatch.setattr(cycle_log, "cycle_log_base_dir", lambda: tmp_path)
        runner = self._make_runner_with_pr_body(self.PR_BODY)
        cycle_log.write_cycle_log("F-017-U-1", base_dir=tmp_path, run=runner)

        summary = cycle_log.cycle_log_summary("F-017-U-1", base_dir=tmp_path)
        # TL;DR survives.
        assert TLDR_HEADING in summary
        for sub in TLDR_SUBHEADINGS:
            assert sub in summary
        assert "E2E-WRITER-WHAT" in summary
        assert "E2E-WRITER-CONTRACT" in summary
        assert "E2E-WRITER-DECISIONS" in summary
        # PR description block stripped — both heading and verbatim body.
        assert PR_DESCRIPTION_HEADING not in summary, (
            "PR description heading should be stripped from the summary"
        )
        assert "VERBATIM-PR-DESCRIPTION-LONG-PROSE" not in summary, (
            "verbatim PR description prose leaked into the summary slice"
        )
        # Cycle history / Review threads still present — allow-list
        # boundary intact.
        assert "## Cycle history" in summary
        assert "## Review threads" in summary


# --------------------------- 5. insertion-point structural pin -----------


class TestTldrStructuralPlacement:
    """TL;DR must sit immediately between ``## PR`` and ``## Coder's PR description``.

    Spec § Decisions, "Insertion point": "the only structural slot
    that (a) sits above the strip boundary so ``cycle_log_summary``
    preserves it and (b) sits below the PR identity so humans /
    Claude reading top-down see metadata before commentary." If a
    future renderer inserts a ``## Cycle history`` or some other H2
    between ``## PR`` and ``## TL;DR``, the strip boundary semantics
    silently change (because the allow-list anchors on the FIRST
    matching heading after the PR-description block) and the TL;DR
    slice may shift."""

    def test_no_h2_between_pr_and_tldr_or_between_tldr_and_pr_description(
        self, tmp_state_db: Path
    ) -> None:
        _seed_unit()
        md = render_cycle_log(
            "F-017-U-1",
            pr_info={
                "body": (
                    "## TL;DR\n\n"
                    "### What shipped\nA\n\n"
                    "### Downstream contract\nB\n\n"
                    "### Decisions worth knowing\nC\n\n"
                ),
                "headRefOid": "x",
            },
            review_threads=[],
        )

        # Compute the byte offsets of the three anchor H2s.
        idx_pr = md.find("## PR\n")
        idx_tldr = md.find(f"{TLDR_HEADING}\n")
        idx_desc = md.find(PR_DESCRIPTION_HEADING)
        assert idx_pr != -1 and idx_tldr != -1 and idx_desc != -1
        assert idx_pr < idx_tldr < idx_desc

        # Find every H2 in the document; assert no H2 appears between
        # ``## PR`` and ``## TL;DR`` other than those two, and none
        # between ``## TL;DR`` and ``## Coder's PR description``.
        # H2 = a line starting with `## ` followed by a non-`#` char.
        import re as _re

        h2_iter = list(_re.finditer(r"^## [^#\n][^\n]*$", md, _re.MULTILINE))
        h2_positions = [(m.start(), m.group(0)) for m in h2_iter]

        between_pr_and_tldr = [
            (pos, head)
            for pos, head in h2_positions
            if idx_pr < pos < idx_tldr
        ]
        assert between_pr_and_tldr == [], (
            f"unexpected H2 between '## PR' and '## TL;DR': {between_pr_and_tldr}"
        )

        between_tldr_and_desc = [
            (pos, head)
            for pos, head in h2_positions
            if idx_tldr < pos < idx_desc
        ]
        assert between_tldr_and_desc == [], (
            f"unexpected H2 between '## TL;DR' and '## Coder's PR description': "
            f"{between_tldr_and_desc}"
        )


# --------------------------- 6. coder prompt mirrors TBD wording -----------


class TestCoderPromptMirrorsTbdWording:
    """The coder prompt should warn the coder what happens when they
    skip a sub-section. Without the explicit placeholder text in the
    prompt, the coder may not realize the auto-fill writer produces
    ``_TBD ..._`` instead of "missing"."""

    @pytest.fixture(scope="class")
    def coder_prompt(self) -> str:
        return (
            (Path(__file__).resolve().parent.parent / "orchestrator" / "prompts" / "coder.md")
            .read_text(encoding="utf-8")
        )

    def test_prompt_mentions_tbd_placeholder_wording(self, coder_prompt: str) -> None:
        """Coder prompt names the placeholder so the coder knows the consequence."""
        assert "TBD" in coder_prompt, (
            "coder prompt no longer mentions the TBD placeholder — coder won't "
            "know what skipping a sub-section produces in the cycle log"
        )

    def test_prompt_pr_template_keeps_required_h2_headings(self, coder_prompt: str) -> None:
        """Pre-existing PR-body sections must still be required.

        The unit description says the TL;DR section is *additive* to
        the existing 'Spec satisfaction' / 'Decisions / deviations'
        template; adding TL;DR must not delete the existing
        instructions. (Coder's tests check TL;DR is added; this test
        checks the additive nature.)"""
        # Spec satisfaction is the heaviest pre-F-017 requirement.
        assert "Spec satisfaction" in coder_prompt, (
            "## Spec satisfaction requirement removed from coder prompt; "
            "F-017 was meant to be additive (unit description)"
        )
