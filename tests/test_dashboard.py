"""Tests for orchestrator/dashboard.py — data fetchers and markdown renderer."""

from __future__ import annotations

from orchestrator import dashboard, state
from orchestrator.models import Feature, WorkUnit, WorkUnitState


class TestIsAwaitingMerge:
    def test_returns_false_when_no_events(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="in_ci"))
        assert dashboard._is_awaiting_merge("U1") is False

    def test_true_after_terminal_review_event(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="in_ci"))
        state.record_event("U1", "F", "spawn_coder")
        state.record_event("U1", "F", "pr_opened", source="coder")
        state.record_event("U1", "F", "reviewer_recommend_merge", source="reviewer")
        assert dashboard._is_awaiting_merge("U1") is True

    def test_false_after_coder_resumed_invalidates_review(self, tmp_state_db):
        """If a reviewer endorsed, but then coder restarted (new round),
        we're no longer 'awaiting merge' — that approval is stale."""
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="in_ci"))
        state.record_event("U1", "F", "reviewer_approved", source="reviewer")
        state.record_event("U1", "F", "coder_resumed", source="reviewer")
        assert dashboard._is_awaiting_merge("U1") is False

    def test_true_for_reviewer_approved(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="in_ci"))
        state.record_event("U1", "F", "reviewer_approved", source="reviewer")
        assert dashboard._is_awaiting_merge("U1") is True

    def test_true_for_reviewer_comment(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="in_ci"))
        state.record_event("U1", "F", "reviewer_comment", source="reviewer")
        assert dashboard._is_awaiting_merge("U1") is True


class TestDataFetchers:
    def test_features_data_empty(self, tmp_state_db):
        assert dashboard._features_data() == []

    def test_features_data_shows_progress(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="hello", description="", status="approved"))
        state.save_plan(
            "F",
            [
                WorkUnit(id=f"F-U-{i}", feature_id="F", title=f"u{i}", description="")
                for i in range(1, 4)
            ],
        )
        # 1 done, 1 in flight, 1 not yet spawned
        state.upsert_unit_state(WorkUnitState(unit_id="F-U-1", feature_id="F", status="done"))
        state.upsert_unit_state(WorkUnitState(unit_id="F-U-2", feature_id="F", status="coding"))

        rows = dashboard._features_data()
        assert len(rows) == 1
        assert rows[0]["id"] == "F"
        # Plan has 3 units; 1 done → "1/3"
        assert rows[0]["units"] == "1/3"

    def test_in_flight_excludes_done_and_escalated(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        state.upsert_unit_state(WorkUnitState(unit_id="U2", feature_id="F", status="done"))
        state.upsert_unit_state(WorkUnitState(unit_id="U3", feature_id="F", status="escalated"))

        rows = dashboard._in_flight_data()
        unit_ids = {r["unit_id"] for r in rows}
        assert unit_ids == {"U1"}

    def test_awaiting_merge_picks_up_endorsed_units(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(
            WorkUnitState(unit_id="U1", feature_id="F", status="in_ci", pr_number=5)
        )
        state.record_event("U1", "F", "reviewer_recommend_merge", source="reviewer")

        rows = dashboard._awaiting_merge_data()
        assert len(rows) == 1
        assert rows[0]["pr"] == "#5"
        assert rows[0]["unit_id"] == "U1"

    def test_escalated_data_orders_by_recency(self, tmp_state_db):
        import time

        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(
            WorkUnitState(unit_id="U1", feature_id="F", status="escalated", last_error="first")
        )
        time.sleep(0.01)
        state.upsert_unit_state(
            WorkUnitState(unit_id="U2", feature_id="F", status="escalated", last_error="second")
        )
        rows = dashboard._escalated_data()
        # Order is by last_activity DESC
        assert rows[0]["unit_id"] == "U2"
        assert rows[1]["unit_id"] == "U1"

    def test_escalated_data_truncates_when_reason_unknown(self, tmp_state_db):
        """Legacy path: no structured reason → keep the 120-char truncation
        on `last_error` so the panel stays compact for unclassified
        failures (no regression from pre-F-005-U-2 behaviour)."""
        import json as _json

        state.save_feature(Feature(id="F", title="t", description=""))
        long_error = "x" * 500
        state.upsert_unit_state(
            WorkUnitState(unit_id="U1", feature_id="F", status="escalated", last_error=long_error)
        )
        # An event WITHOUT structured reason should leave the row in legacy mode.
        state.record_event("U1", "F", "coder_blocked", details="plain prose")

        rows = dashboard._escalated_data()
        assert len(rows[0]["last_error"]) == 120
        assert rows[0]["reason"] == "unknown"

        # And conversely: when there's no event at all, still truncated.
        _ = _json  # keep import grouped; used in companion test below

    def test_escalated_data_full_remediation_when_reason_known(self, tmp_state_db):
        """F-005-U-2 contract: when the most recent BLOCKED-style event
        carries a structured reason, the dashboard surfaces the FULL
        `reason -> hint -> prose` tail with no 120-char truncation."""
        import json as _json

        state.save_feature(Feature(id="F", title="t", description=""))
        long_prose = (
            "remote: error: GH013: Repository rule violations found for refs/heads/"
            "f-005-u-2. " + ("x" * 400)
        )
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U1", feature_id="F", status="escalated", last_error="short header"
            )
        )
        state.record_event(
            "U1",
            "F",
            "coder_blocked",
            details=_json.dumps({"reason": "branch_protection_blocked_push", "prose": long_prose}),
        )
        rows = dashboard._escalated_data()
        row = rows[0]
        # The reason and the multi-line hint are present
        assert row["reason"] == "branch_protection_blocked_push"
        assert "Reason: branch_protection_blocked_push" in row["last_error"]
        # Full prose tail preserved — no truncation
        assert long_prose in row["last_error"]
        # The full rendering must exceed the legacy 120-char cap to actually
        # be a regression-free improvement.
        assert len(row["last_error"]) > 120

    def test_events_data_returns_newest_first(self, tmp_state_db):
        import time

        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        for i in range(3):
            state.record_event("U1", "F", f"step_{i}")
            time.sleep(0.01)

        rows = dashboard._events_data(limit=10)
        # newest first
        assert rows[0]["event_type"] == "step_2"
        assert rows[-1]["event_type"] == "step_0"

    def test_events_data_respects_limit(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        for i in range(15):
            state.record_event("U1", "F", f"step_{i}")
        assert len(dashboard._events_data(limit=5)) == 5


class TestRenderMarkdown:
    def test_renders_empty_state(self, tmp_state_db):
        md = dashboard.render_markdown()
        # Should have all 5 sections even when empty
        assert "## 📊 Features" in md
        assert "## 🔧 In flight" in md
        assert "## 🟢 Awaiting your merge" in md
        assert "## 🚨 Escalated" in md
        assert "## 📜 Recent events" in md
        # Empty-state placeholders
        assert "no features loaded" in md
        assert "nothing in flight" in md

    def test_renders_mixed_state_with_tables(self, tmp_state_db):
        state.save_feature(Feature(id="F-001", title="math", description="", status="approved"))
        state.save_plan(
            "F-001", [WorkUnit(id="F-001-U-1", feature_id="F-001", title="u1", description="")]
        )
        state.upsert_unit_state(
            WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="in_ci", pr_number=5)
        )
        state.record_event("F-001-U-1", "F-001", "reviewer_recommend_merge", source="reviewer")

        md = dashboard.render_markdown()
        assert "F-001" in md
        assert "math" in md
        assert "#5" in md  # PR number in awaiting-merge

    def test_escapes_pipes_in_cells(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="x | y", description=""))
        md = dashboard.render_markdown()
        # The pipe in title should be escaped to not break the table
        assert "x \\| y" in md


class TestRichPanels:
    """Rich panel functions should render without crashing on any state.

    Doesn't assert pixel-perfect output — just that the functions return
    a Panel and don't blow up on empty / populated / mixed inputs.
    """

    def test_all_panels_render_on_empty(self, tmp_state_db):
        from rich.panel import Panel

        for fn in (
            dashboard._features_panel,
            dashboard._in_flight_panel,
            dashboard._awaiting_merge_panel,
            dashboard._escalated_panel,
            dashboard._events_panel,
        ):
            result = fn()
            assert isinstance(result, Panel)

    def test_all_panels_render_on_populated(self, tmp_state_db):
        from rich.panel import Panel

        state.save_feature(Feature(id="F", title="t", description="d", status="approved"))
        state.upsert_unit_state(
            WorkUnitState(unit_id="U1", feature_id="F", status="coding", branch="b")
        )
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U2",
                feature_id="F",
                status="escalated",
                last_error="cap-3 hit",
                pr_number=5,
            )
        )
        state.record_event("U1", "F", "spawn_coder")
        # All five panel-builders should still return a Panel
        for fn in (
            dashboard._features_panel,
            dashboard._in_flight_panel,
            dashboard._awaiting_merge_panel,
            dashboard._escalated_panel,
            dashboard._events_panel,
        ):
            assert isinstance(fn(), Panel)

    def test_render_group_includes_all_sections(self, tmp_state_db):
        from rich.console import Group

        group = dashboard.render()
        assert isinstance(group, Group)
        # The group contains 5 panels + 1 hint text = 6 renderables
        assert len(group.renderables) == 6

    def test_render_once_does_not_crash(self, tmp_state_db, capsys):
        dashboard.render_once()
        out = capsys.readouterr().out
        assert "Features" in out  # title from a panel border

    def test_main_returns_1_when_state_db_missing(self, tmp_path, monkeypatch):
        """The TUI main entry point should bail cleanly if state.db doesn't exist."""
        monkeypatch.setattr("orchestrator.state.STATE_DB", tmp_path / "nope.db")
        assert dashboard.main() == 1


class TestStyleStatus:
    def test_known_statuses_get_emoji_label(self):
        from orchestrator.dashboard import _style_status

        result = _style_status("done")
        text = str(result)
        assert "done" in text

    def test_unknown_status_returns_plain(self):
        from orchestrator.dashboard import _style_status

        result = _style_status("weird_unknown_status")
        assert "weird_unknown_status" in str(result)

    def test_status_md_known(self):
        from orchestrator.dashboard import _status_md

        assert "done" in _status_md("done")
        assert "✅" in _status_md("done")

    def test_status_md_unknown_no_emoji(self):
        from orchestrator.dashboard import _status_md

        assert _status_md("zzz") == "zzz"


class TestHhMmSs:
    def test_parses_iso_timestamp(self):
        from orchestrator.dashboard import _hh_mm_ss

        result = _hh_mm_ss("2026-05-11T14:23:01+00:00")
        assert result == "14:23:01"

    def test_returns_input_on_malformed(self):
        from orchestrator.dashboard import _hh_mm_ss

        assert _hh_mm_ss("not-a-timestamp") == "not-a-timestamp"

    def test_returns_question_mark_on_empty(self):
        from orchestrator.dashboard import _hh_mm_ss

        assert _hh_mm_ss("") == "?"
