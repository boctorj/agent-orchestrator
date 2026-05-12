"""Tests for orchestrator/costs.py — session-hour cost estimation."""

from __future__ import annotations

from orchestrator import costs, state
from orchestrator.models import Feature, WorkUnitState


def _seed_unit(feature_id: str = "F", unit_id: str = "U1"):
    state.save_feature(Feature(id=feature_id, title="t", description=""))
    state.upsert_unit_state(WorkUnitState(unit_id=unit_id, feature_id=feature_id, status="done"))


def _inject_event(db_path, unit_id, feature_id, ts, session_id=""):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO unit_events (unit_id, feature_id, ts, event_type, source, summary, details, session_id) "
        "VALUES (?, ?, ?, 'evt', 'orch', '', '', ?)",
        (unit_id, feature_id, ts, session_id),
    )
    conn.commit()
    conn.close()


class TestComputeUnitCost:
    def test_no_events_returns_error(self, tmp_state_db):
        _seed_unit()
        r = costs.compute_unit_cost("U1")
        assert "error" in r

    def test_wall_clock_spans_events(self, tmp_state_db):
        _seed_unit()
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:00:00+00:00")
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:05:00+00:00")
        r = costs.compute_unit_cost("U1")
        assert r["wall_clock_seconds"] == 300.0

    def test_per_session_durations_summed(self, tmp_state_db):
        _seed_unit()
        # session "coder" runs for 180s
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:00:00+00:00", session_id="coder")
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:03:00+00:00", session_id="coder")
        # session "tester" runs for 60s
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:03:30+00:00", session_id="tester")
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:04:30+00:00", session_id="tester")
        r = costs.compute_unit_cost("U1")
        assert r["session_count"] == 2
        assert r["total_session_seconds"] == 240.0  # 180 + 60
        # Cost = 240/3600 * 0.08 = 0.00533...
        assert r["est_session_cost_usd"] == round(240.0 / 3600.0 * 0.08, 4)

    def test_events_without_session_id_excluded_from_session_totals(self, tmp_state_db):
        _seed_unit()
        # Two events without session_id, one with
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:00:00+00:00", session_id="")
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:05:00+00:00", session_id="")
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:01:00+00:00", session_id="X")
        _inject_event(tmp_state_db, "U1", "F", "2026-05-11T00:04:00+00:00", session_id="X")
        r = costs.compute_unit_cost("U1")
        assert r["session_count"] == 1
        # Per-session: 4:00 - 1:00 = 180s
        assert r["total_session_seconds"] == 180.0
        # Wall-clock still spans all events: 5min = 300s
        assert r["wall_clock_seconds"] == 300.0


class TestComputeFeatureCost:
    def test_no_units_returns_zero(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        r = costs.compute_feature_cost("F")
        assert r["feature_id"] == "F"
        assert r["unit_count"] == 0
        assert r["est_total_cost_usd"] == 0.0

    def test_aggregates_across_multiple_units(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        for uid in ("U1", "U2"):
            state.upsert_unit_state(WorkUnitState(unit_id=uid, feature_id="F", status="done"))
            _inject_event(tmp_state_db, uid, "F", "2026-05-11T00:00:00+00:00", session_id="s")
            _inject_event(tmp_state_db, uid, "F", "2026-05-11T00:01:00+00:00", session_id="s")
        r = costs.compute_feature_cost("F")
        assert r["unit_count"] == 2
        # Each unit = 60s of session time → total 120s
        assert r["total_session_seconds"] == 120.0


def test_session_hourly_rate_is_0_08():
    assert costs.SESSION_HOURLY_RATE_USD == 0.08
