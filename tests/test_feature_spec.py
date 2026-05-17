"""Tests for orchestrator/feature_spec.py — spec.md template + writer."""

from __future__ import annotations

from orchestrator import feature_spec, state


def test_features_root_is_sibling_of_state_db(tmp_state_db):
    assert feature_spec.features_root() == state.STATE_DB.parent / "features"


def test_spec_path_layout(tmp_state_db):
    path = feature_spec.spec_path("F-007")
    assert path.parent == feature_spec.features_root() / "F-007"
    assert path.name == "spec.md"


def test_render_template_includes_all_required_sections(tmp_state_db):
    body = feature_spec.render_template("F-007", "OAuth", "Add Google OAuth login.")
    # Per docs/PROPOSAL-feature-spec-and-headless-daemon.md §1.
    for header in (
        "# F-007: OAuth",
        "## Intent",
        "## Acceptance",
        "## Out of scope",
        "## Approach",
        "## Constraints",
        "## Decisions",
        "## Open questions",
    ):
        assert header in body, f"missing section: {header}"


def test_render_template_seeds_intent_with_description(tmp_state_db):
    body = feature_spec.render_template("F-007", "OAuth", "Add Google OAuth login.")
    # The description must land in the Intent section, not anywhere else.
    intent_block = body.split("## Intent", 1)[1].split("##", 1)[0]
    assert "Add Google OAuth login." in intent_block


def test_render_template_seeds_title_in_heading(tmp_state_db):
    body = feature_spec.render_template("F-007", "OAuth", "d")
    assert body.startswith("# F-007: OAuth\n")


def test_write_spec_if_missing_creates_file_and_parent_dir(tmp_state_db):
    path = feature_spec.write_spec_if_missing("F-007", "OAuth", "Add Google OAuth login.")
    assert path.exists()
    assert path.parent.is_dir()
    content = path.read_text(encoding="utf-8")
    assert "# F-007: OAuth" in content
    assert "Add Google OAuth login." in content


def test_write_spec_if_missing_preserves_existing_file(tmp_state_db):
    """Manual edits the lead made during planning must NOT be clobbered when
    load_feature is re-invoked (e.g. to fix a wrong repo_path)."""
    path = feature_spec.write_spec_if_missing("F-007", "OAuth", "first description")
    path.write_text("# F-007: OAuth\n\n## Intent\nhand-edited\n", encoding="utf-8")

    returned = feature_spec.write_spec_if_missing("F-007", "OAuth (renamed)", "new description")
    assert returned == path
    assert path.read_text(encoding="utf-8") == ("# F-007: OAuth\n\n## Intent\nhand-edited\n"), (
        "existing spec.md was overwritten"
    )


def test_write_spec_if_missing_returns_path_even_when_skipping(tmp_state_db):
    first = feature_spec.write_spec_if_missing("F-007", "t", "d")
    second = feature_spec.write_spec_if_missing("F-007", "t", "d")
    assert first == second


def test_write_spec_if_missing_does_not_leave_tmp_artifact(tmp_state_db):
    """write-to-tmp + rename should leave no `.tmp` file behind on success."""
    path = feature_spec.write_spec_if_missing("F-007", "t", "d")
    tmp_artifact = path.with_suffix(path.suffix + ".tmp")
    assert not tmp_artifact.exists()


def test_multiple_features_get_independent_directories(tmp_state_db):
    p1 = feature_spec.write_spec_if_missing("F-001", "A", "desc A")
    p2 = feature_spec.write_spec_if_missing("F-002", "B", "desc B")
    assert p1.parent != p2.parent
    assert "desc A" in p1.read_text(encoding="utf-8")
    assert "desc B" in p2.read_text(encoding="utf-8")
