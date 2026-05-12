"""Tests for orchestrator/tools/observability.py — read-only state inspection."""

from __future__ import annotations

import json

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import observability


def _seed(feature_id="F", unit_id="U1", status="coding"):
    state.save_feature(Feature(id=feature_id, title="t", description="d"))
    state.upsert_unit_state(WorkUnitState(unit_id=unit_id, feature_id=feature_id, status=status))


# --------------------------- get_unit_status ---------------------------


def test_get_unit_status_no_state(tmp_state_db):
    assert "No state" in observability.get_unit_status("nope")


def test_get_unit_status_returns_json(tmp_state_db):
    _seed()
    out = observability.get_unit_status("U1")
    parsed = json.loads(out)
    assert parsed["unit_id"] == "U1"
    assert parsed["status"] == "coding"


# --------------------------- list_units ---------------------------


def test_list_units_empty(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description="d"))
    assert "No units" in observability.list_units("F")


def test_list_units_returns_json(tmp_state_db):
    _seed()
    out = observability.list_units("F")
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["unit_id"] == "U1"


# --------------------------- unit_history + unit_summary ---------------------------


def test_unit_history_empty(tmp_state_db):
    assert "No events" in observability.unit_history("nope")


def test_unit_history_returns_events(tmp_state_db):
    _seed()
    state.record_event("U1", "F", "spawn_coder", summary="start")
    state.record_event("U1", "F", "pr_opened", source="coder")
    out = observability.unit_history("U1")
    parsed = json.loads(out)
    assert len(parsed) == 2


def test_unit_summary_returns_digest(tmp_state_db):
    _seed()
    state.record_event("U1", "F", "spawn_coder")
    out = observability.unit_summary("U1")
    parsed = json.loads(out)
    assert parsed["unit_id"] == "U1"
    assert parsed["event_count"] == 1


# --------------------------- unit_cost + feature_cost ---------------------------


def test_unit_cost_no_events(tmp_state_db):
    _seed()
    out = observability.unit_cost("U1")
    parsed = json.loads(out)
    assert "error" in parsed


def test_feature_cost_empty(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description="d"))
    out = observability.feature_cost("F")
    parsed = json.loads(out)
    assert parsed["feature_id"] == "F"
    assert parsed["est_total_cost_usd"] == 0.0


# --------------------------- show_dashboard ---------------------------


def test_show_dashboard_returns_markdown(tmp_state_db):
    """Should render the same markdown as dashboard.render_markdown."""
    out = observability.show_dashboard()
    assert "## 📊 Features" in out
    assert "## 🔧 In flight" in out
    assert "## 🟢 Awaiting your merge" in out


def test_show_dashboard_includes_populated_state(tmp_state_db):
    _seed(status="coding")
    out = observability.show_dashboard()
    assert "F" in out
    assert "coding" in out
