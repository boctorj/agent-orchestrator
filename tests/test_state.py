"""Tests for orchestrator/state.py — SQLite layer."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator import state
from orchestrator.models import CheckResult, Feature, VerificationResult, WorkUnit, WorkUnitState

# --------------------------- _connect() lifecycle ---------------------------


class TestConnectCloses:
    """`_connect()` must fully close the connection on context exit.

    A plain `with sqlite3.Connection as conn:` commits/rollbacks the
    transaction but does NOT close — the connection lingers until GC,
    leaking file descriptors and emitting ResourceWarning under pytest.
    Our helper wraps that so callers get auto-close.
    """

    def test_connection_is_closed_after_with_block(self, tmp_state_db):
        with state._connect() as conn:
            conn.execute("SELECT 1").fetchone()

        # Operating on a closed sqlite3 connection raises ProgrammingError
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_connection_commits_on_normal_exit(self, tmp_state_db):
        """Regression: closing must NOT skip the commit. Writes must persist."""
        with state._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
            conn.execute("INSERT INTO _probe VALUES (42)")

        # Re-open a fresh connection and verify the row persisted
        with state._connect() as conn:
            row = conn.execute("SELECT x FROM _probe").fetchone()
            assert row["x"] == 42

    def test_connection_rolls_back_on_exception(self, tmp_state_db):
        """An exception inside the `with` block must roll back AND close."""
        with state._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _probe2 (x INTEGER)")

        with pytest.raises(RuntimeError), state._connect() as conn:
            conn.execute("INSERT INTO _probe2 VALUES (1)")
            raise RuntimeError("simulate failure mid-transaction")

        # Conn is closed
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

        # Row was rolled back — table exists from prior block but empty
        with state._connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS c FROM _probe2").fetchone()["c"] == 0


# --------------------------- features ---------------------------


class TestFeatures:
    def test_save_and_get_round_trip(self, tmp_state_db):
        f = Feature(
            id="F-001",
            title="hello",
            description="desc",
            repo_path="https://github.com/o/r",
            branch_prefix="feat/F-001-x",
        )
        state.save_feature(f)
        got = state.get_feature("F-001")
        assert got is not None
        assert got.id == "F-001"
        assert got.title == "hello"
        assert got.repo_path == "https://github.com/o/r"
        assert got.created_at  # populated by save

    def test_get_missing_returns_none(self, tmp_state_db):
        assert state.get_feature("F-999") is None

    def test_list_features_empty(self, tmp_state_db):
        assert state.list_features() == []

    def test_list_features_ordered_by_created_at(self, tmp_state_db):
        state.save_feature(Feature(id="A", title="a", description=""))
        time.sleep(0.01)
        state.save_feature(Feature(id="B", title="b", description=""))
        ids = [f.id for f in state.list_features()]
        assert ids == ["A", "B"]

    def test_save_feature_upserts_on_conflict(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="first", description="d"))
        state.save_feature(Feature(id="F", title="second", description="d"))
        got = state.get_feature("F")
        assert got.title == "second"

    def test_next_feature_id_starts_at_001(self, tmp_state_db):
        assert state.next_feature_id() == "F-001"

    def test_next_feature_id_increments(self, tmp_state_db):
        state.save_feature(Feature(id="F-001", title="a", description=""))
        state.save_feature(Feature(id="F-002", title="b", description=""))
        assert state.next_feature_id() == "F-003"

    def test_next_feature_id_skips_non_numeric(self, tmp_state_db):
        state.save_feature(Feature(id="F-005", title="a", description=""))
        state.save_feature(Feature(id="weird-id", title="b", description=""))
        assert state.next_feature_id() == "F-006"


# --------------------------- plans ---------------------------


class TestPlans:
    def test_save_and_get_round_trip(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        units = [
            WorkUnit(id="F-U-1", feature_id="F", title="u1", description="", depends_on=[]),
            WorkUnit(id="F-U-2", feature_id="F", title="u2", description="", depends_on=["F-U-1"]),
        ]
        state.save_plan("F", units)
        plan = state.get_plan("F")
        assert plan is not None
        assert plan.feature_id == "F"
        assert plan.status == "draft"
        assert len(plan.units) == 2
        assert plan.units[1].depends_on == ["F-U-1"]

    def test_save_plan_resets_status_on_overwrite(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.save_plan("F", [WorkUnit(id="U1", feature_id="F", title="t", description="")])
        state.approve_plan("F")
        # Save again — should revert to draft
        state.save_plan("F", [WorkUnit(id="U1", feature_id="F", title="t", description="")])
        plan = state.get_plan("F")
        assert plan.status == "draft"
        assert plan.approved_at is None

    def test_approve_plan_sets_timestamp_and_feature_status(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.save_plan("F", [WorkUnit(id="U1", feature_id="F", title="t", description="")])
        ts = state.approve_plan("F")
        plan = state.get_plan("F")
        assert plan.status == "approved"
        assert plan.approved_at == ts
        feat = state.get_feature("F")
        assert feat.status == "approved"

    def test_approve_plan_raises_when_missing(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        with pytest.raises(ValueError, match="No plan exists"):
            state.approve_plan("F")


# --------------------------- work units ---------------------------


class TestWorkUnits:
    def test_upsert_and_get(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        u = WorkUnitState(unit_id="U1", feature_id="F", status="coding", branch="b1")
        state.upsert_unit_state(u)
        got = state.get_unit_state("U1")
        assert got is not None
        assert got.status == "coding"
        assert got.branch == "b1"
        assert got.last_activity  # auto-populated

    def test_upsert_overwrites(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="done"))
        assert state.get_unit_state("U1").status == "done"

    def test_get_missing_returns_none(self, tmp_state_db):
        assert state.get_unit_state("nope") is None

    def test_touch_unit_updates_status(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        state.touch_unit("U1", status="in_ci")
        assert state.get_unit_state("U1").status == "in_ci"

    def test_touch_unit_sets_error(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        state.touch_unit("U1", error="something broke")
        assert state.get_unit_state("U1").last_error == "something broke"

    def test_list_unit_states_filters_by_feature(self, tmp_state_db):
        state.save_feature(Feature(id="F1", title="a", description=""))
        state.save_feature(Feature(id="F2", title="b", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="F1-U1", feature_id="F1", status="done"))
        state.upsert_unit_state(WorkUnitState(unit_id="F2-U1", feature_id="F2", status="done"))
        f1_units = state.list_unit_states("F1")
        assert len(f1_units) == 1
        assert f1_units[0].unit_id == "F1-U1"

    def test_fk_constraint_rejects_orphan_unit(self, tmp_state_db):
        # No feature 'NOPE' exists → FK violation
        with pytest.raises(sqlite3.IntegrityError):
            state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="NOPE", status="coding"))

    def test_increment_review_round_returns_new_value(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        assert state.increment_review_round("U1") == 1
        assert state.increment_review_round("U1") == 2
        assert state.get_unit_state("U1").review_round == 2


# --------------------------- events ---------------------------


class TestEvents:
    def test_record_and_list_events(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        state.record_event("U1", "F", "spawn_coder", summary="start")
        state.record_event("U1", "F", "pr_opened", summary="PR #1", source="coder")
        events = state.list_events("U1")
        assert len(events) == 2
        assert events[0]["event_type"] == "spawn_coder"
        assert events[1]["event_type"] == "pr_opened"
        assert events[1]["source"] == "coder"

    def test_events_ordered_by_ts_oldest_first(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        for i in range(3):
            state.record_event("U1", "F", f"step_{i}", summary=f"s{i}")
            time.sleep(0.01)
        events = state.list_events("U1")
        assert [e["event_type"] for e in events] == ["step_0", "step_1", "step_2"]

    def test_summarize_counts_types(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
        for t in ["spawn_coder", "spawn_tester", "spawn_tester", "pr_opened"]:
            state.record_event("U1", "F", t)
        s = state.summarize_unit("U1")
        assert s["event_count"] == 4
        assert s["event_counts_by_type"]["spawn_tester"] == 2
        assert s["event_counts_by_type"]["spawn_coder"] == 1


# --------------------------- cached resources ---------------------------


class TestCachedResources:
    def test_save_and_get_round_trip(self, tmp_state_db):
        state.save_cached_resource("coder", "sig1", "agent_1", "env_1")
        got = state.get_cached_resource("coder", "sig1")
        assert got == ("agent_1", "env_1")

    def test_get_missing_returns_none(self, tmp_state_db):
        assert state.get_cached_resource("coder", "sigX") is None

    def test_save_upserts_same_key(self, tmp_state_db):
        state.save_cached_resource("coder", "sig1", "agent_old", "env_old")
        state.save_cached_resource("coder", "sig1", "agent_new", "env_new")
        assert state.get_cached_resource("coder", "sig1") == ("agent_new", "env_new")

    def test_clear_returns_count(self, tmp_state_db):
        state.save_cached_resource("coder", "sigA", "a1", "e1")
        state.save_cached_resource("tester", "sigB", "a2", "e2")
        assert state.clear_cached_resources() == 2
        assert state.get_cached_resource("coder", "sigA") is None

    def test_ttl_expires_old_rows(self, tmp_state_db):
        # Inject an aged-out row directly
        old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        import sqlite3

        with sqlite3.connect(tmp_state_db) as conn:
            conn.execute(
                "INSERT INTO cached_resources (role, prompt_hash, agent_id, environment_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("coder", "sig_old", "agent_old", "env_old", old_ts),
            )
        # Default TTL is 30 days
        assert state.get_cached_resource("coder", "sig_old") is None
        # Larger TTL retrieves it
        assert state.get_cached_resource("coder", "sig_old", max_age_days=60) == (
            "agent_old",
            "env_old",
        )

    def test_malformed_created_at_treated_as_miss(self, tmp_state_db):
        import sqlite3

        with sqlite3.connect(tmp_state_db) as conn:
            conn.execute(
                "INSERT INTO cached_resources (role, prompt_hash, agent_id, environment_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("coder", "sig_bad", "agent_bad", "env_bad", "not-a-timestamp"),
            )
        assert state.get_cached_resource("coder", "sig_bad") is None


# --------------------------- verified_repos ---------------------------


def _passing_result(url="https://github.com/owner/repo") -> VerificationResult:
    """Build a passing VerificationResult fixture matching what verify() emits."""
    return VerificationResult(
        repo_url=url,
        default_branch="main",
        auth_mode="pat",
        auth_identity="user:tester",
        checks=[
            CheckResult("read access", True),
            CheckResult("write access", True),
            CheckResult("branch protection exists", True),
            CheckResult(
                "≥1 approving review required",
                True,
                "required_approving_review_count = 1",
            ),
            CheckResult("force push blocked", True),
            CheckResult("deletion blocked", True),
            CheckResult("admin bypass blocked", True),
        ],
        warnings=[],
    )


class TestVerifiedReposCRUD:
    def test_save_and_get_round_trip(self, tmp_state_db):
        state.save_verified_repo(_passing_result())
        got = state.get_verified_repo("https://github.com/owner/repo")
        assert got is not None
        assert got.default_branch == "main"
        assert got.auth_mode == "pat"
        assert got.auth_identity == "user:tester"
        assert got.has_branch_protection is True
        assert got.required_approvals == 1
        assert got.blocks_force_push is True
        assert got.blocks_deletion is True
        assert got.blocks_bypass is True
        assert got.has_codeowners is False
        assert got.requires_signed_commits is False
        assert got.verified_at  # populated by save

    def test_save_refuses_failed_result(self, tmp_state_db):
        result = VerificationResult(
            repo_url="https://github.com/o/r",
            checks=[CheckResult("read access", False, "404")],
        )
        with pytest.raises(ValueError, match="failed verification"):
            state.save_verified_repo(result)

    def test_get_missing_returns_none(self, tmp_state_db):
        assert state.get_verified_repo("https://github.com/nope/none") is None

    def test_save_is_idempotent_replaces_row(self, tmp_state_db):
        state.save_verified_repo(_passing_result())
        first = state.get_verified_repo("https://github.com/owner/repo")
        time.sleep(0.01)
        # Re-save with the same URL
        state.save_verified_repo(_passing_result())
        second = state.get_verified_repo("https://github.com/owner/repo")
        assert first is not None and second is not None
        assert second.verified_at >= first.verified_at  # refreshed

    def test_codeowners_note_sets_has_codeowners_flag(self, tmp_state_db):
        """CODEOWNERS lives in `notes` (positive signal), not `warnings`.

        The persisted row still flips `has_codeowners=True` so spawn-time
        gates and the lead's chat output can detect it.
        """
        result = _passing_result()
        result.notes = ["CODEOWNERS present at .github/CODEOWNERS — by design"]
        state.save_verified_repo(result)
        got = state.get_verified_repo(result.repo_url)
        assert got is not None
        assert got.has_codeowners is True
        # warnings_json column is for the warnings list only (notes aren't
        # persisted — they're reproducible from re-verifying).
        assert got.warnings_json == "[]"

    def test_warnings_round_trip_through_json(self, tmp_state_db):
        """Independent `warnings_json` round-trip — kept after CODEOWNERS moved.

        Uses a warning that ISN'T CODEOWNERS so we exercise the column on
        its own (e.g., required_signatures).
        """
        result = _passing_result()
        result.warnings = ["required_signatures is on — agent commits aren't GPG-signed"]
        state.save_verified_repo(result)
        got = state.get_verified_repo(result.repo_url)
        assert got is not None
        assert got.has_codeowners is False  # no CODEOWNERS note
        assert "required_signatures" in got.warnings_json
        assert got.requires_signed_commits is True

    def test_list_verified_repos(self, tmp_state_db):
        # Clear the fixture's pre-seeded test repos for a clean assertion
        state.forget_verified_repo("https://github.com/o/r")
        state.forget_verified_repo("https://github.com/joe/repo")
        assert state.list_verified_repos() == []
        state.save_verified_repo(_passing_result("https://github.com/a/x"))
        time.sleep(0.01)
        state.save_verified_repo(_passing_result("https://github.com/b/y"))
        rows = state.list_verified_repos()
        urls = [r.repo_url for r in rows]
        # Ordered by verified_at — a/x verified first
        assert urls == ["https://github.com/a/x", "https://github.com/b/y"]

    def test_forget_verified_repo(self, tmp_state_db):
        state.save_verified_repo(_passing_result())
        assert state.forget_verified_repo("https://github.com/owner/repo") is True
        assert state.get_verified_repo("https://github.com/owner/repo") is None
        # Second call returns False (already gone)
        assert state.forget_verified_repo("https://github.com/owner/repo") is False


class TestVerifiedRepoTTL:
    def test_fresh_inside_ttl(self, tmp_state_db):
        state.save_verified_repo(_passing_result())
        fresh = state.get_fresh_verified_repo("https://github.com/owner/repo")
        assert fresh is not None

    def test_stale_after_ttl(self, tmp_state_db):
        state.save_verified_repo(_passing_result())
        # Hand-roll an old timestamp directly into the row
        import sqlite3

        old_ts = (datetime.now(UTC) - timedelta(hours=state.VERIFY_TTL_HOURS + 1)).isoformat()
        with sqlite3.connect(tmp_state_db) as conn:
            conn.execute(
                "UPDATE verified_repos SET verified_at = ? WHERE repo_url = ?",
                (old_ts, "https://github.com/owner/repo"),
            )
        assert state.get_fresh_verified_repo("https://github.com/owner/repo") is None
        # Raw get still returns the row (TTL is for the fresh check only)
        assert state.get_verified_repo("https://github.com/owner/repo") is not None

    def test_malformed_timestamp_treated_as_stale(self, tmp_state_db):
        state.save_verified_repo(_passing_result())
        import sqlite3

        with sqlite3.connect(tmp_state_db) as conn:
            conn.execute(
                "UPDATE verified_repos SET verified_at = ? WHERE repo_url = ?",
                ("not-iso-8601", "https://github.com/owner/repo"),
            )
        assert state.get_fresh_verified_repo("https://github.com/owner/repo") is None

    def test_custom_ttl_kwarg(self, tmp_state_db):
        state.save_verified_repo(_passing_result())
        # 0-hour TTL → anything is stale
        assert state.get_fresh_verified_repo("https://github.com/owner/repo", ttl_hours=0) is None
