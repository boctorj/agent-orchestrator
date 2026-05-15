"""Spec-compliance tests for F-006-U-1.

The coder's `test_feature_spec.py` and the F-006 additions to
`test_tools_planning.py` exercise the template renderer and the
`load_feature` wiring. This file is independent tester coverage that
locks in the EXACT contract from the unit description verbatim:

  > Create features/F-XXX/ on load_feature and write spec.md from a
  > template (Intent / Acceptance / Out of scope / Approach / Constraints
  > / Decisions / Open questions) seeded with the feature title and
  > description. Add a spec-format reference doc under docs/ plus the
  > 'Why:' commit-message-as-decision-log pattern. Pure additive:
  > planning produces spec.md; no consumers yet.

Concretely the assertions below cover:

  1. Directory creation: `features/<feature_id>/` exists after
     `load_feature` (not just the file).
  2. Section presence + EXACT ORDER, matching the parenthesised list in
     the unit description.
  3. Title appears in the H1 heading; description lands in the Intent
     block (and only there).
  4. The spec-format reference doc `docs/SPEC-FORMAT.md` exists, names
     every template section, and documents the `Why:` commit-message
     pattern as the decision log.
  5. "Pure additive" guard: no MCP tool named `feature_memory` is
     registered yet, and the `compose_*_task` templates do not inject
     spec.md content yet — both are intentionally deferred to later
     units of F-006.
  6. spec.md is seeded regardless of whether `repo_path` is supplied
     (planning is free; the verification gate is orthogonal).
"""

from __future__ import annotations

from pathlib import Path

from orchestrator import feature_spec
from orchestrator.models import Feature, WorkUnit
from orchestrator.tools import (
    compose_coder_task,
    compose_fix_task,
    compose_reviewer_task,
    compose_tester_task,
    mcp,
    planning,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_FORMAT_DOC = REPO_ROOT / "docs" / "SPEC-FORMAT.md"

# The seven section headings, in the EXACT order the unit description
# lists them.
SECTION_ORDER = (
    "## Intent",
    "## Acceptance",
    "## Out of scope",
    "## Approach",
    "## Constraints",
    "## Decisions",
    "## Open questions",
)


# --------------------------- directory creation ---------------------------


def test_load_feature_creates_features_subdirectory(tmp_state_db):
    """`load_feature` must create `features/<feature_id>/` (not just the
    file). Tests in test_tools_planning.py assert the file exists; this
    one asserts the directory itself, which is what the unit description
    names explicitly ("Create features/F-XXX/ on load_feature")."""
    feature_dir = feature_spec.features_root() / "F-001"
    assert not feature_dir.exists(), "precondition: tmp DB starts clean"

    planning.load_feature(title="t", description="d")

    assert feature_dir.is_dir(), "features/F-001/ was not created"
    assert (feature_dir / "spec.md").is_file()


def test_load_feature_creates_features_root_when_absent(tmp_state_db):
    """The `features/` parent itself must be created on first call; we
    shouldn't rely on it pre-existing in the workdir."""
    features_root = feature_spec.features_root()
    assert not features_root.exists()

    planning.load_feature(title="t", description="d")

    assert features_root.is_dir()


def test_load_feature_writes_spec_with_explicit_id(tmp_state_db):
    """An explicit id="F-077" writes to features/F-077/spec.md, not into
    the auto-allocated F-001 slot."""
    planning.load_feature(title="t", description="d", id="F-077")

    explicit = feature_spec.spec_path("F-077")
    assert explicit.exists()
    assert "# F-077: t" in explicit.read_text(encoding="utf-8")
    # And F-001 must NOT have been created as a side effect.
    assert not feature_spec.spec_path("F-001").exists()


# --------------------------- template shape ---------------------------


def test_template_section_order_matches_unit_description(tmp_state_db):
    """The seven sections must appear in the order the unit description
    lists them: Intent, Acceptance, Out of scope, Approach, Constraints,
    Decisions, Open questions."""
    body = feature_spec.render_template("F-007", "OAuth", "d")
    positions = [body.index(h) for h in SECTION_ORDER]
    assert positions == sorted(positions), (
        f"template section order is wrong: got positions {positions} for sections {SECTION_ORDER}"
    )


def test_template_first_line_is_h1_with_id_and_title(tmp_state_db):
    """The H1 must be `# <feature_id>: <title>` so a glance at the file
    identifies the feature."""
    body = feature_spec.render_template("F-042", "Some Title", "desc")
    first_line = body.splitlines()[0]
    assert first_line == "# F-042: Some Title"


def test_template_description_only_in_intent_section(tmp_state_db):
    """The unique description string must appear ONCE, inside the Intent
    block — not duplicated into Decisions/Open questions/etc."""
    needle = "QQQ-unique-description-marker-ZZZ"
    body = feature_spec.render_template("F-007", "OAuth", needle)

    assert body.count(needle) == 1, (
        f"description '{needle}' should appear exactly once in the "
        f"template, found {body.count(needle)}"
    )

    intent_block = body.split("## Intent", 1)[1].split("\n## ", 1)[0]
    assert needle in intent_block


def test_template_title_only_in_h1_heading(tmp_state_db):
    """A distinctive title should appear only in the H1 — not leaked
    into Intent or any other section as a stray duplicate."""
    needle = "QQQ-unique-title-marker-ZZZ"
    body = feature_spec.render_template("F-007", needle, "d")
    assert body.count(needle) == 1


def test_template_has_blank_line_between_heading_and_intent(tmp_state_db):
    """Standard markdown convention: blank line between H1 and the next
    section heading. Makes the file render correctly on GitHub."""
    body = feature_spec.render_template("F-007", "t", "d")
    lines = body.splitlines()
    # Line 0 is `# F-007: t`; line 1 should be blank; line 2 starts the
    # first section.
    assert lines[0].startswith("# F-007"), lines[0]
    assert lines[1] == "", f"expected blank line after H1, got {lines[1]!r}"


def test_template_renders_for_all_features_in_db(tmp_state_db):
    """Smoke: every feature_id and title round-trips into a parseable
    template. Guards against any embedded f-string brace mismatch."""
    for fid, title, desc in (
        ("F-001", "alpha", "first"),
        ("F-002", "beta with spaces", "second"),
        ("F-100", "γ-unicode-Δ", "third"),
    ):
        body = feature_spec.render_template(fid, title, desc)
        assert body.startswith(f"# {fid}: {title}\n")
        for header in SECTION_ORDER:
            assert header in body


# --------------------------- spec-format doc ---------------------------


def test_spec_format_doc_exists():
    """The unit description requires 'a spec-format reference doc under
    docs/'. Path is `docs/SPEC-FORMAT.md` — referenced from
    `orchestrator/feature_spec.py:render_template`."""
    assert SPEC_FORMAT_DOC.exists(), (
        f"expected reference doc at {SPEC_FORMAT_DOC.relative_to(REPO_ROOT)}; "
        f"unit description requires 'spec-format reference doc under docs/'"
    )


def test_spec_format_doc_lists_all_template_sections():
    """Every section the implementation seeds must appear in the
    reference doc, otherwise the doc and the seeder will drift."""
    text = SPEC_FORMAT_DOC.read_text(encoding="utf-8")
    for header in SECTION_ORDER:
        # Strip the leading '## ' so we match the section name regardless
        # of whether the doc renders it as a sub-heading or as plain text.
        name = header[3:]
        assert name in text, f"docs/SPEC-FORMAT.md does not mention section {name!r}"


def test_spec_format_doc_documents_why_commit_pattern():
    """The unit description requires "the 'Why:' commit-message-as-
    decision-log pattern" be documented. We assert the doc mentions both
    the 'Why:' marker and that it is the decision log (any of the
    obvious phrasings)."""
    text = SPEC_FORMAT_DOC.read_text(encoding="utf-8")
    assert "Why:" in text, "doc does not name the `Why:` commit-message marker"

    # The pattern is the *decision log*. Accept any phrasing that links
    # the git commit log to the decision-log role.
    lowered = text.lower()
    decision_log_phrases = (
        "decision log",
        "commit log is the decision",
        "git log",
        "as decision log",
    )
    assert any(p in lowered for p in decision_log_phrases), (
        "doc names `Why:` but never frames the commit log as the "
        "decision log — required by unit description"
    )


def test_spec_format_doc_mentions_features_directory_layout():
    """The doc must describe the on-disk location so future contributors
    can find it without reading the code."""
    text = SPEC_FORMAT_DOC.read_text(encoding="utf-8")
    assert "features/" in text
    assert "spec.md" in text


# --------------------------- pure-additive guard ---------------------------


def test_no_feature_memory_mcp_tool_registered_yet():
    """The proposal defers the `feature_memory` MCP tool to a later unit.
    F-006-U-1 is "pure additive: planning produces spec.md; no consumers
    yet." — a feature_memory tool registered here would be premature."""
    # FastMCP tools register at import time. By the time this test runs,
    # every tool module has been imported through `orchestrator.tools.*`.
    # `mcp._tool_manager._tools` is the registry; if FastMCP changes the
    # private API we fall back to scanning loaded tool modules.
    try:
        registered = set(mcp._tool_manager._tools.keys())  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - defensive
        from orchestrator.tools import (
            execution,
            observability,
            ops,
            scheduling,
        )
        from orchestrator.tools import planning as p

        registered = set()
        for mod in (execution, observability, ops, p, scheduling):
            for name in dir(mod):
                obj = getattr(mod, name)
                if callable(obj) and getattr(obj, "__module__", "").startswith(
                    "orchestrator.tools"
                ):
                    registered.add(name)

    assert "feature_memory" not in registered, (
        "feature_memory MCP tool is registered but the F-006-U-1 unit "
        "description marks it deferred to a later unit (pure additive)"
    )


def test_compose_task_templates_do_not_inject_spec_content_yet(tmp_state_db):
    """compose_*_task templates fan FEATURE CONTEXT (description) into the
    coder/tester/reviewer task — but they MUST NOT yet inline `spec.md`
    contents or read the file. Wiring those injections is a later unit
    of F-006; doing it here would break the 'no consumers yet' clause."""
    feature = Feature(
        id="F-001",
        title="t",
        description="d",
        repo_path="https://github.com/o/r",
        branch_prefix="feat/F-001",
    )
    unit = WorkUnit(id="F-001-U-1", feature_id="F-001", title="u", description="ud")

    rendered = [
        compose_coder_task(feature, unit, branch="feat/F-001-x", github_token="tok"),
        compose_tester_task(feature, unit, branch="feat/F-001-x", pr_number=1, github_token="tok"),
        compose_reviewer_task(feature, unit, pr_number=1, github_token="tok"),
        compose_fix_task(
            feature, unit, branch="feat/F-001-x", pr_number=1, source="tester", feedback="f"
        ),
    ]
    for body in rendered:
        # None of the task templates should be injecting the durable
        # spec.md content yet. The proposal calls the injected block
        # `## FEATURE SPEC` (see PROPOSAL §"Role prompt changes"); the
        # absence of that header here means "no consumer wired".
        assert "## FEATURE SPEC" not in body, (
            "compose_*_task is injecting a `## FEATURE SPEC` block, "
            "but F-006-U-1 is the seeder-only unit (no consumers yet)"
        )
        # Reading the file would be a side-effect injection too.
        assert "spec.md" not in body, (
            "compose_*_task references `spec.md`, but the consumer "
            "wiring is deferred to a later unit of F-006"
        )


def test_planning_module_does_not_read_spec_md_on_import(tmp_state_db):
    """Importability invariant from CONTRIBUTING.md: module imports must
    be pure. Re-importing planning must NOT touch the filesystem under
    features/."""
    import importlib

    from orchestrator.tools import planning as planning_mod

    features_root = feature_spec.features_root()
    assert not features_root.exists(), "precondition: clean tmp state"

    importlib.reload(planning_mod)

    assert not features_root.exists(), (
        "re-importing orchestrator.tools.planning created files under "
        "features/; module imports must be side-effect-free"
    )


# --------------------------- load_feature integration ---------------------------


def test_load_feature_writes_spec_without_repo_path(tmp_state_db):
    """Planning is free (no verification gate triggers without a
    repo_path); spec.md must still be seeded so the lead has something
    to edit during the planning conversation."""
    planning.load_feature(title="t", description="d")
    assert feature_spec.spec_path("F-001").exists()


def test_load_feature_writes_spec_when_repo_unverified(tmp_state_db):
    """A warn-not-block repo gate must still produce a spec.md — the
    warning is orthogonal to the seeding."""
    msg = planning.load_feature(
        title="t",
        description="d",
        repo_path="https://github.com/never/verified",
    )
    assert "⚠" in msg
    assert feature_spec.spec_path("F-001").exists()


def test_load_feature_writes_spec_when_repo_path_malformed(tmp_state_db):
    """Even the malformed-URL path still produces a spec.md — the
    template seeding precedes URL normalisation."""
    msg = planning.load_feature(title="t", description="d", repo_path="not a url")
    assert "malformed" in msg
    assert feature_spec.spec_path("F-001").exists()


def test_load_feature_does_not_write_spec_for_other_features(tmp_state_db):
    """Per-feature isolation: load_feature for F-001 must not touch
    F-002's directory (regression guard against accidental fan-out)."""
    planning.load_feature(title="alpha", description="d")
    f2_dir = feature_spec.features_root() / "F-002"
    assert not f2_dir.exists()


def test_load_feature_seeded_template_includes_all_unit_description_sections(tmp_state_db):
    """End-to-end: the file on disk after `load_feature` contains every
    section named in the unit description, in the order listed."""
    planning.load_feature(title="OAuth", description="Add Google OAuth login.")
    body = feature_spec.spec_path("F-001").read_text(encoding="utf-8")

    positions = [body.index(h) for h in SECTION_ORDER]
    assert positions == sorted(positions)
    # Heading carries id + title.
    assert body.splitlines()[0] == "# F-001: OAuth"
    # Description landed in Intent.
    intent_block = body.split("## Intent", 1)[1].split("\n## ", 1)[0]
    assert "Add Google OAuth login." in intent_block
