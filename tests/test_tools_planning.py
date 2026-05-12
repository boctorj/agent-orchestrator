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
