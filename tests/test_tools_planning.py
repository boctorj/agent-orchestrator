"""Tests for orchestrator/tools/planning.py — feature + plan MCP tools."""

from __future__ import annotations

import json

from orchestrator import state
from orchestrator.models import Feature
from orchestrator.tools import planning

# --------------------------- load_feature ---------------------------


def test_load_feature_returns_summary(tmp_state_db):
    msg = planning.load_feature(title="hello", description="d", repo_path="https://github.com/o/r")
    assert "Loaded feature" in msg
    assert "F-001" in msg
    assert "hello" in msg


def test_load_feature_auto_allocates_id(tmp_state_db):
    planning.load_feature(title="A", description="d")
    msg = planning.load_feature(title="B", description="d")
    assert "F-002" in msg


def test_load_feature_respects_explicit_id(tmp_state_db):
    msg = planning.load_feature(title="t", description="d", id="F-999")
    assert "F-999" in msg
    assert state.get_feature("F-999") is not None


def test_load_feature_persists_all_fields(tmp_state_db):
    planning.load_feature(
        title="t",
        description="d",
        repo_path="https://github.com/joe/repo",
        branch_prefix="feat/F-001-x",
    )
    f = state.get_feature("F-001")
    assert f.repo_path == "https://github.com/joe/repo"
    assert f.branch_prefix == "feat/F-001-x"


def test_load_feature_warns_on_unverified_repo(tmp_state_db):
    """`load_feature` is a warn-not-block gate — feature still created but the
    response surfaces a ⚠ pointing the lead at `verify_repo()`."""
    msg = planning.load_feature(
        title="t",
        description="d",
        repo_path="https://github.com/unseen/repo",  # NOT in pre-seeded fixture
    )
    assert "Loaded feature" in msg
    assert state.get_feature("F-001") is not None
    assert "⚠" in msg
    assert "not verified" in msg
    assert "verify_repo" in msg


def test_load_feature_no_warning_when_repo_verified(tmp_state_db):
    """If the repo's already fresh-verified, no warning appears."""
    msg = planning.load_feature(
        title="t",
        description="d",
        repo_path="https://github.com/o/r",  # auto-verified by fixture
    )
    assert "⚠" not in msg
    assert "not verified" not in msg


def test_load_feature_warns_on_malformed_repo_path(tmp_state_db):
    msg = planning.load_feature(title="t", description="d", repo_path="not a url")
    assert "⚠" in msg
    assert "malformed" in msg


# --------- load_feature update semantics (F-004: preserve approval) ---------


def test_load_feature_new_creation_uses_loaded_message(tmp_state_db):
    """A brand-new feature surfaces 'Loaded feature' — existing behavior intact."""
    msg = planning.load_feature(title="hello", description="d")
    assert "Loaded feature" in msg
    assert "Updated feature" not in msg


def test_load_feature_metadata_only_on_approved_preserves_approval(tmp_state_db):
    """Calling load_feature on an approved feature with new metadata (e.g.
    fixing a wrong repo_path) MUST preserve status='approved'."""
    planning.load_feature(title="t", description="d", repo_path="https://github.com/o/r")
    planning.save_plan(
        "F-001",
        [{"id": "U1", "title": "u", "description": "d", "depends_on": []}],
    )
    planning.approve_plan("F-001")
    assert state.get_feature("F-001").status == "approved"

    msg = planning.load_feature(
        title="new title",
        description="new desc",
        id="F-001",
        repo_path="https://github.com/o/r",
    )

    feat = state.get_feature("F-001")
    assert feat.status == "approved"
    assert feat.title == "new title"
    assert feat.description == "new desc"
    assert "Updated feature F-001" in msg
    assert "metadata-only" in msg
    assert "approval preserved" in msg


def test_load_feature_metadata_only_on_draft_stays_draft(tmp_state_db):
    """Metadata-only update on a never-approved feature keeps status non-approved
    and message doesn't falsely claim 'approval preserved'."""
    planning.load_feature(title="t", description="d")
    before = state.get_feature("F-001").status
    assert before != "approved"

    msg = planning.load_feature(title="renamed", description="new desc", id="F-001")

    feat = state.get_feature("F-001")
    assert feat.status != "approved"
    assert feat.status == before  # status unchanged
    assert feat.title == "renamed"
    assert "Updated feature F-001" in msg
    assert "approval preserved" not in msg
    assert "reset to draft" not in msg


def test_load_feature_units_changed_on_approved_resets_to_draft(tmp_state_db):
    """If save_plan was called between approval and this load_feature (i.e.
    units list materially changed), the feature drops back to draft."""
    planning.load_feature(title="t", description="d", repo_path="https://github.com/o/r")
    planning.save_plan(
        "F-001",
        [{"id": "U1", "title": "u", "description": "d", "depends_on": []}],
    )
    planning.approve_plan("F-001")
    assert state.get_feature("F-001").status == "approved"

    # Re-save the plan with a different unit list. save_plan resets plan
    # status to 'draft' but does NOT reset feature.status (only bumps
    # draft -> planned). After this step, feature is still 'approved'
    # while plan is 'draft' — the inconsistency this unit detects.
    planning.save_plan(
        "F-001",
        [
            {"id": "U1", "title": "u", "description": "d", "depends_on": []},
            {"id": "U2", "title": "u2", "description": "d2", "depends_on": []},
        ],
    )
    assert state.get_plan("F-001").status == "draft"
    assert state.get_feature("F-001").status == "approved"

    msg = planning.load_feature(
        title="t",
        description="d",
        id="F-001",
        repo_path="https://github.com/o/r",
    )

    feat = state.get_feature("F-001")
    assert feat.status == "draft"
    assert "Updated feature F-001" in msg
    assert "units changed" in msg
    assert "reset to draft" in msg


def test_load_feature_units_changed_on_draft_does_not_claim_approval_reset(tmp_state_db):
    """Same flow as the approved-reset case but starting from a never-approved
    feature: result must NOT be 'approved' and message must NOT claim it was
    reset from approval (there was nothing to reset)."""
    planning.load_feature(title="t", description="d")
    planning.save_plan(
        "F-001",
        [{"id": "U1", "title": "u", "description": "d", "depends_on": []}],
    )
    # No approve_plan call — feature is 'planned', plan is 'draft'.
    planning.save_plan(
        "F-001",
        [
            {"id": "U1", "title": "u", "description": "d", "depends_on": []},
            {"id": "U2", "title": "u2", "description": "d2", "depends_on": []},
        ],
    )
    assert state.get_feature("F-001").status != "approved"

    msg = planning.load_feature(title="t", description="d", id="F-001")

    feat = state.get_feature("F-001")
    assert feat.status != "approved"
    assert "Updated feature F-001" in msg
    assert "approval preserved" not in msg
    assert "reset to draft" not in msg


# --------- F-004-U-1 update semantics: additional independent coverage ---------
#
# These tests are written from the unit description ("preserve approval on
# metadata-only updates") rather than from the implementation. They cover
# scenarios that aren't directly exercised by the tests above: the F-001
# onboarding bug (fixing a wrong repo_path), each metadata field changing
# in turn, created_at preservation, message-shape contrast between the
# two paths, an explicit-but-nonexistent id falling through to creation,
# and a re-approve round trip.


def _seed_approved_feature(repo_path: str = "https://github.com/o/r") -> None:
    """Helper: create + plan + approve F-001 with a single unit."""
    planning.load_feature(title="orig", description="orig desc", repo_path=repo_path)
    planning.save_plan(
        "F-001",
        [{"id": "U1", "title": "u", "description": "d", "depends_on": []}],
    )
    planning.approve_plan("F-001")
    assert state.get_feature("F-001").status == "approved"


def test_f004_fixing_wrong_repo_path_keeps_approval(tmp_state_db):
    """The exact F-001 onboarding scenario: user approved a plan, then realised
    repo_path was wrong. Re-calling load_feature with the corrected repo_path
    must NOT silently revert the feature to draft."""
    _seed_approved_feature(repo_path="https://github.com/wrong/repo")
    # repo not pre-verified — load_feature warns but doesn't block. We don't
    # care about the warning for this test, only the status preservation.

    msg = planning.load_feature(
        title="orig",
        description="orig desc",
        id="F-001",
        repo_path="https://github.com/o/r",  # corrected
    )

    feat = state.get_feature("F-001")
    assert feat.status == "approved", "approval was silently dropped on a metadata fix"
    assert feat.repo_path == "https://github.com/o/r"
    assert "approval preserved" in msg


def test_f004_changing_only_title_preserves_approval(tmp_state_db):
    _seed_approved_feature()
    planning.load_feature(
        title="new title only",
        description="orig desc",
        id="F-001",
        repo_path="https://github.com/o/r",
    )
    feat = state.get_feature("F-001")
    assert feat.status == "approved"
    assert feat.title == "new title only"


def test_f004_changing_only_description_preserves_approval(tmp_state_db):
    _seed_approved_feature()
    planning.load_feature(
        title="orig",
        description="new description only",
        id="F-001",
        repo_path="https://github.com/o/r",
    )
    feat = state.get_feature("F-001")
    assert feat.status == "approved"
    assert feat.description == "new description only"


def test_f004_changing_only_branch_prefix_preserves_approval(tmp_state_db):
    _seed_approved_feature()
    planning.load_feature(
        title="orig",
        description="orig desc",
        id="F-001",
        repo_path="https://github.com/o/r",
        branch_prefix="feat/F-001-renamed",
    )
    feat = state.get_feature("F-001")
    assert feat.status == "approved"
    assert feat.branch_prefix == "feat/F-001-renamed"


def test_f004_created_at_preserved_across_metadata_update(tmp_state_db):
    """The feature's created_at must NOT be regenerated when load_feature is
    used as a metadata-update path — only updates should not look like fresh
    creations in any audit field."""
    _seed_approved_feature()
    before = state.get_feature("F-001").created_at
    assert before  # sanity: created_at was set on initial save

    planning.load_feature(
        title="orig",
        description="orig desc",
        id="F-001",
        repo_path="https://github.com/o/r",
    )
    after = state.get_feature("F-001").created_at
    assert after == before


def test_f004_approved_path_message_distinct_from_units_changed_path(tmp_state_db):
    """The unit description requires the returned message to make it
    UNAMBIGUOUS which path was taken. Both messages mention 'Updated feature
    F-001' but the suffix MUST differ between the two cases."""
    # Path A: metadata-only update on approved.
    _seed_approved_feature()
    msg_a = planning.load_feature(
        title="x",
        description="y",
        id="F-001",
        repo_path="https://github.com/o/r",
    )

    # Path B: units changed on approved (re-save then load_feature).
    planning.save_plan(
        "F-001",
        [
            {"id": "U1", "title": "u", "description": "d", "depends_on": []},
            {"id": "U2", "title": "u2", "description": "d2", "depends_on": []},
        ],
    )
    msg_b = planning.load_feature(
        title="x",
        description="y",
        id="F-001",
        repo_path="https://github.com/o/r",
    )

    assert "Updated feature F-001" in msg_a
    assert "Updated feature F-001" in msg_b
    # The two suffixes must be different so the user can tell which path
    # ran. Strip the common prefix; what's left must not match.
    suffix_a = msg_a.split("Updated feature F-001", 1)[1].splitlines()[0]
    suffix_b = msg_b.split("Updated feature F-001", 1)[1].splitlines()[0]
    assert suffix_a != suffix_b
    # And specifically, only path B should say 'draft' / 'units changed'.
    assert "reset to draft" not in msg_a
    assert "reset to draft" in msg_b


def test_f004_units_changed_reset_then_reapprove_round_trip(tmp_state_db):
    """After reset-to-draft, re-approving the new plan must work, and a
    subsequent metadata-only load_feature must again preserve approval.
    This proves the reset isn't a one-way trap."""
    _seed_approved_feature()
    planning.save_plan(
        "F-001",
        [
            {"id": "U1", "title": "u", "description": "d", "depends_on": []},
            {"id": "U2", "title": "u2", "description": "d2", "depends_on": []},
        ],
    )
    # First load_feature call: detects units changed, resets to draft.
    planning.load_feature(
        title="orig",
        description="orig desc",
        id="F-001",
        repo_path="https://github.com/o/r",
    )
    assert state.get_feature("F-001").status == "draft"

    # Re-approve.
    planning.approve_plan("F-001")
    assert state.get_feature("F-001").status == "approved"

    # Second load_feature call (metadata-only this time) must preserve.
    msg = planning.load_feature(
        title="orig2",
        description="orig desc",
        id="F-001",
        repo_path="https://github.com/o/r",
    )
    assert state.get_feature("F-001").status == "approved"
    assert "approval preserved" in msg


def test_f004_explicit_id_with_no_existing_row_creates_new(tmp_state_db):
    """If an id is passed but no feature with that id exists, the call
    must behave as creation — not raise, not pretend to 'update' a ghost."""
    msg = planning.load_feature(title="t", description="d", id="F-777")
    assert "Loaded feature" in msg
    assert "F-777" in msg
    assert state.get_feature("F-777") is not None
    assert state.get_feature("F-777").status == "draft"


def test_f004_metadata_update_does_not_touch_plan_units(tmp_state_db):
    """A metadata-only load_feature must not mutate the saved plan.units
    (no accidental clear-on-write)."""
    _seed_approved_feature()
    plan_before = state.get_plan("F-001")
    assert len(plan_before.units) == 1
    assert plan_before.status == "approved"

    planning.load_feature(
        title="renamed",
        description="new",
        id="F-001",
        repo_path="https://github.com/o/r",
    )

    plan_after = state.get_plan("F-001")
    assert len(plan_after.units) == 1
    assert plan_after.units[0].id == "U1"
    assert plan_after.status == "approved"


def test_f004_new_creation_status_and_message_unchanged(tmp_state_db):
    """The 'existing behavior unchanged' clause: a first-time load_feature
    (no id supplied) still allocates a fresh F-NNN, sets status='draft',
    and surfaces 'Loaded feature' (not 'Updated feature')."""
    msg = planning.load_feature(title="brand new", description="d")
    assert "Loaded feature" in msg
    assert "Updated feature" not in msg
    assert "F-001" in msg
    feat = state.get_feature("F-001")
    assert feat is not None
    assert feat.status == "draft"
    assert feat.title == "brand new"


# --------------------------- list_features ---------------------------


def test_list_features_empty(tmp_state_db):
    assert "No features loaded" in planning.list_features()


def test_list_features_returns_json_with_status(tmp_state_db):
    planning.load_feature(title="a", description="d")
    out = planning.list_features()
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["id"] == "F-001"
    assert parsed[0]["status"] == "draft"


# --------------------------- ultrareview_enabled (F-007-U-1) ---------------------------


def test_load_feature_default_ultrareview_disabled(tmp_state_db):
    """Opt-in flag must default to False — ultrareview costs measurably."""
    planning.load_feature(title="t", description="d")
    assert state.get_feature("F-001").ultrareview_enabled is False


def test_load_feature_accepts_ultrareview_enabled_true(tmp_state_db):
    planning.load_feature(title="t", description="d", ultrareview_enabled=True)
    assert state.get_feature("F-001").ultrareview_enabled is True


def test_load_feature_toggles_ultrareview_on_existing_feature(tmp_state_db):
    """Toggling the flag on an existing approved feature is metadata-only —
    it must NOT drop the feature back to draft (mirrors fixing repo_path)."""
    planning.load_feature(title="t", description="d", repo_path="https://github.com/o/r")
    planning.save_plan(
        "F-001",
        [{"id": "U1", "title": "u", "description": "d", "depends_on": []}],
    )
    planning.approve_plan("F-001")

    planning.load_feature(
        title="t",
        description="d",
        id="F-001",
        repo_path="https://github.com/o/r",
        ultrareview_enabled=True,
    )
    feat = state.get_feature("F-001")
    assert feat.status == "approved"
    assert feat.ultrareview_enabled is True


def test_list_features_includes_ultrareview_enabled(tmp_state_db):
    planning.load_feature(title="a", description="d", ultrareview_enabled=True)
    parsed = json.loads(planning.list_features())
    assert parsed[0]["ultrareview_enabled"] is True


def test_list_features_defaults_ultrareview_to_false_in_output(tmp_state_db):
    planning.load_feature(title="a", description="d")
    parsed = json.loads(planning.list_features())
    assert parsed[0]["ultrareview_enabled"] is False


def test_get_plan_surfaces_ultrareview_enabled(tmp_state_db):
    planning.load_feature(title="a", description="d", ultrareview_enabled=True)
    planning.save_plan(
        "F-001",
        [{"id": "U1", "title": "t", "description": "d", "depends_on": []}],
    )
    parsed = json.loads(planning.get_plan("F-001"))
    assert parsed["ultrareview_enabled"] is True


def test_get_plan_defaults_ultrareview_to_false(tmp_state_db):
    planning.load_feature(title="a", description="d")
    planning.save_plan(
        "F-001",
        [{"id": "U1", "title": "t", "description": "d", "depends_on": []}],
    )
    parsed = json.loads(planning.get_plan("F-001"))
    assert parsed["ultrareview_enabled"] is False


# --------------------------- save_plan ---------------------------


def test_save_plan_requires_existing_feature(tmp_state_db):
    msg = planning.save_plan(
        "F-999", [{"id": "X", "title": "t", "description": "d", "depends_on": []}]
    )
    assert "ERROR" in msg
    assert "not found" in msg


def test_save_plan_rejects_missing_field(tmp_state_db):
    planning.load_feature(title="a", description="d")
    msg = planning.save_plan("F-001", [{"id": "U1"}])  # missing title/description
    assert "ERROR" in msg
    assert "missing required field" in msg


def test_save_plan_rejects_unknown_dep(tmp_state_db):
    planning.load_feature(title="a", description="d")
    msg = planning.save_plan(
        "F-001",
        [
            {"id": "U1", "title": "t", "description": "d", "depends_on": []},
            {"id": "U2", "title": "t", "description": "d", "depends_on": ["NOPE"]},
        ],
    )
    assert "ERROR" in msg
    assert "depends on unknown unit" in msg


def test_save_plan_persists(tmp_state_db):
    planning.load_feature(title="a", description="d")
    msg = planning.save_plan(
        "F-001",
        [
            {"id": "U1", "title": "t", "description": "d", "depends_on": []},
            {"id": "U2", "title": "t", "description": "d", "depends_on": ["U1"]},
        ],
    )
    assert "Saved plan" in msg
    plan = state.get_plan("F-001")
    assert len(plan.units) == 2
    assert plan.units[1].depends_on == ["U1"]


# --------------------------- get_plan ---------------------------


def test_get_plan_when_none(tmp_state_db):
    assert "No plan" in planning.get_plan("F-001")


def test_get_plan_returns_json(tmp_state_db):
    planning.load_feature(title="a", description="d")
    planning.save_plan("F-001", [{"id": "U1", "title": "t", "description": "d", "depends_on": []}])
    out = planning.get_plan("F-001")
    parsed = json.loads(out)
    assert parsed["feature_id"] == "F-001"
    assert parsed["status"] == "draft"
    assert len(parsed["units"]) == 1


# --------------------------- approve_plan ---------------------------


def test_approve_plan_missing_plan_returns_error(tmp_state_db):
    state.save_feature(Feature(id="F-001", title="t", description="d"))
    msg = planning.approve_plan("F-001")
    assert "ERROR" in msg


def test_approve_plan_succeeds(tmp_state_db):
    planning.load_feature(title="a", description="d")
    planning.save_plan("F-001", [{"id": "U1", "title": "t", "description": "d", "depends_on": []}])
    msg = planning.approve_plan("F-001")
    assert "APPROVED" in msg
    assert state.get_plan("F-001").status == "approved"
