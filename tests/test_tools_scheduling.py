"""Tests for orchestrator/tools/scheduling.py — DAG scheduling + parallel execution."""

from __future__ import annotations

import json
import threading
import time

from orchestrator import state
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import scheduling


def _setup_feature_with_plan(feature_id="F-001", n_units=3, status="approved"):
    state.save_feature(Feature(id=feature_id, title="t", description="d", status=status))
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=f"{feature_id}-U-{i}",
                feature_id=feature_id,
                title=f"u{i}",
                description="",
                depends_on=[] if i == 1 else [f"{feature_id}-U-1"],
            )
            for i in range(1, n_units + 1)
        ],
    )


# --------------------------- next_ready_units ---------------------------


def test_next_ready_units_no_plan(tmp_state_db):
    out = scheduling.next_ready_units("F-XXX")
    parsed = json.loads(out)
    assert "error" in parsed


def test_next_ready_units_initial_dag(tmp_state_db):
    _setup_feature_with_plan(n_units=3)
    out = scheduling.next_ready_units("F-001")
    parsed = json.loads(out)
    # Only U-1 has no deps
    ready_ids = [u["unit_id"] for u in parsed["ready_to_spawn"]]
    assert ready_ids == ["F-001-U-1"]
    assert parsed["total_ready"] == 1


def test_next_ready_units_unblocks_after_done(tmp_state_db):
    _setup_feature_with_plan(n_units=3)
    state.upsert_unit_state(WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="done"))
    out = scheduling.next_ready_units("F-001")
    parsed = json.loads(out)
    ready_ids = sorted(u["unit_id"] for u in parsed["ready_to_spawn"])
    assert ready_ids == ["F-001-U-2", "F-001-U-3"]


def test_next_ready_units_reports_in_flight(tmp_state_db):
    _setup_feature_with_plan(n_units=2)
    state.upsert_unit_state(WorkUnitState(unit_id="F-001-U-1", feature_id="F-001", status="coding"))
    out = scheduling.next_ready_units("F-001")
    parsed = json.loads(out)
    assert parsed["ready_to_spawn"] == []
    in_flight_ids = [u["unit_id"] for u in parsed["in_flight"]]
    assert "F-001-U-1" in in_flight_ids


def test_next_ready_units_reports_escalated(tmp_state_db):
    _setup_feature_with_plan(n_units=2)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="F-001-U-1",
            feature_id="F-001",
            status="escalated",
            last_error="cap-3",
        )
    )
    out = scheduling.next_ready_units("F-001")
    parsed = json.loads(out)
    escalated_ids = [u["unit_id"] for u in parsed["escalated"]]
    assert "F-001-U-1" in escalated_ids


# --------------------------- next_ready_units_all ---------------------------


def test_next_ready_units_all_aggregates(tmp_state_db):
    _setup_feature_with_plan("F-001", n_units=2)
    _setup_feature_with_plan("F-002", n_units=2)
    out = scheduling.next_ready_units_all()
    parsed = json.loads(out)
    # U-1 of each feature is ready (no deps)
    assert parsed["total_ready"] == 2
    feature_ids = {u["feature_id"] for u in parsed["ready_to_spawn"]}
    assert feature_ids == {"F-001", "F-002"}


def test_next_ready_units_all_excludes_draft_features(tmp_state_db):
    _setup_feature_with_plan("F-001", n_units=2, status="approved")
    _setup_feature_with_plan("F-002", n_units=2, status="draft")
    out = scheduling.next_ready_units_all()
    parsed = json.loads(out)
    feature_ids = {u["feature_id"] for u in parsed["ready_to_spawn"]}
    assert feature_ids == {"F-001"}


def test_next_ready_units_all_empty(tmp_state_db):
    out = scheduling.next_ready_units_all()
    parsed = json.loads(out)
    assert parsed["total_ready"] == 0


# --------------------------- parallel_units + parallel_units_global ---------------------------


def _make_fake_one(call_log, log_lock):
    """Create a fake ``_run_one`` that records each call.

    F-016 Phase 5 retired the thread-pool fan-out (``parallel_units`` /
    ``parallel_units_global`` no longer use ``ThreadPoolExecutor`` —
    daemon-driven concurrency handles the fan-out via ``cycle_review``'s
    Phase 4 async dispatch). This fake records each ``_run_one`` call so
    tests can assert (a) every unit was dispatched and (b) the calls
    happened on the caller's thread (no pool was spun up).
    """

    def fake(*args, **kwargs):
        with log_lock:
            call_log.append((threading.get_ident(), time.time()))
        return {
            "feature_id": args[0] if args else kwargs.get("feature_id", "F"),
            "unit_id": args[1] if len(args) > 1 else kwargs.get("unit_id", "U"),
            "pr_url": "http://example/pull/1",
            "pr_number": 1,
            "cycle_outcome": "approved_awaiting_merge",
            "cycle_message": "ok",
        }

    return fake


def test_parallel_units_empty(tmp_state_db):
    out = scheduling.parallel_units("F", [])
    parsed = json.loads(out)
    assert "error" in parsed


def test_parallel_units_dispatches_every_unit_serially(tmp_state_db, monkeypatch):
    """F-016 Phase 5: no thread pool. The dispatcher walks every unit on
    the caller's thread; daemon-driven concurrency owns fan-out."""
    call_log = []
    log_lock = threading.Lock()
    fake = _make_fake_one(call_log, log_lock)
    monkeypatch.setattr(scheduling, "_run_one", fake)

    out = scheduling.parallel_units("F-001", ["U1", "U2", "U3"], max_concurrent=3)
    parsed = json.loads(out)
    assert parsed["unit_count"] == 3
    assert len(parsed["results"]) == 3
    # All calls happen on the caller's thread — proof the thread pool is gone.
    thread_ids = {t for t, _ in call_log}
    assert thread_ids == {threading.get_ident()}


def test_parallel_units_max_concurrent_is_noop(tmp_state_db, monkeypatch):
    """``max_concurrent`` is retained for callsite compatibility (the
    lead persona still passes it) but the daemon owns fan-out now, so
    the value doesn't change behavior — the response intentionally
    omits the legacy ``max_concurrent`` key."""
    fake = _make_fake_one([], threading.Lock())
    monkeypatch.setattr(scheduling, "_run_one", fake)

    out = scheduling.parallel_units(
        "F-001", ["U1", "U2", "U3", "U4", "U5", "U6", "U7"], max_concurrent=100
    )
    parsed = json.loads(out)
    assert parsed["unit_count"] == 7
    assert len(parsed["results"]) == 7
    assert "max_concurrent" not in parsed


def test_parallel_units_global_empty(tmp_state_db):
    out = scheduling.parallel_units_global([])
    parsed = json.loads(out)
    assert "error" in parsed


def test_parallel_units_global_rejects_bad_refs(tmp_state_db):
    out = scheduling.parallel_units_global([{"feature_id": "F"}])  # missing unit_id
    parsed = json.loads(out)
    assert "error" in parsed
    assert "invalid unit_ref" in parsed["error"]

    out = scheduling.parallel_units_global([{"unit_id": "U"}])  # missing feature_id
    assert "error" in json.loads(out)

    out = scheduling.parallel_units_global([{"bogus": True}])
    assert "error" in json.loads(out)


def test_parallel_units_global_dispatches_cross_feature_serially(tmp_state_db, monkeypatch):
    """Same shape as the single-feature dispatcher — every ref is dispatched
    on the caller's thread, no thread pool, daemon owns fan-out."""
    call_log = []
    log_lock = threading.Lock()
    fake = _make_fake_one(call_log, log_lock)
    monkeypatch.setattr(scheduling, "_run_one", fake)

    refs = [
        {"feature_id": "F-001", "unit_id": "U-1"},
        {"feature_id": "F-002", "unit_id": "U-1"},
        {"feature_id": "F-003", "unit_id": "U-1"},
    ]
    out = scheduling.parallel_units_global(refs, max_concurrent=3)

    parsed = json.loads(out)
    assert parsed["unit_count"] == 3
    assert len(parsed["results"]) == 3
    thread_ids = {t for t, _ in call_log}
    assert thread_ids == {threading.get_ident()}


# --------------------------- _run_one internals ---------------------------


def test_run_one_non_json_spawn_result(tmp_state_db, monkeypatch):
    """If spawn_unit returns an error string (not JSON), _run_one classifies it."""
    monkeypatch.setattr(scheduling, "spawn_unit", lambda f, u: "ERROR: not found")
    out = scheduling._run_one("F-001", "U-X")
    assert out["phase"] == "spawn"
    assert out["outcome"] == "non_json"


def test_run_one_spawn_without_pr_url(tmp_state_db, monkeypatch):
    """If spawn_unit returns JSON without pr_url (escalation path), classify as no_pr."""
    monkeypatch.setattr(
        scheduling,
        "spawn_unit",
        lambda f, u: json.dumps({"unit_id": u, "outcome": "escalated"}),
    )
    out = scheduling._run_one("F-001", "U-X")
    assert out["phase"] == "spawn"
    assert out["outcome"] == "no_pr"


def test_run_one_spawn_raises(tmp_state_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduling, "spawn_unit", boom)
    out = scheduling._run_one("F-001", "U-X")
    assert out["phase"] == "spawn"
    assert "error" in out


def test_run_one_cycle_raises(tmp_state_db, monkeypatch):
    """spawn_unit succeeds, but cycle_review blows up."""
    monkeypatch.setattr(
        scheduling,
        "spawn_unit",
        lambda f, u: json.dumps({"unit_id": u, "pr_url": "http://x", "pr_number": 1}),
    )

    def boom(*a, **k):
        raise RuntimeError("cycle exploded")

    # F-016 Phase 5: ``_run_one`` calls the Phase 4 dispatcher
    # ``cycle_review`` (auto-routes async vs. blocking on ``NTFY_TOPIC`` +
    # daemon health) — patching it here covers both branches.
    monkeypatch.setattr(scheduling, "cycle_review", boom)
    out = scheduling._run_one("F-001", "U-X")
    assert out["phase"] == "cycle"
    assert "error" in out


def test_run_one_happy_path(tmp_state_db, monkeypatch):
    monkeypatch.setattr(
        scheduling,
        "spawn_unit",
        lambda f, u: json.dumps({"unit_id": u, "pr_url": "http://x", "pr_number": 7}),
    )
    monkeypatch.setattr(
        scheduling,
        "cycle_review",
        lambda f, u: json.dumps({"outcome": "approved_awaiting_merge", "message": "ok"}),
    )
    out = scheduling._run_one("F-001", "U-X")
    assert out["unit_id"] == "U-X"
    assert out["pr_number"] == 7
    assert out["cycle_outcome"] == "approved_awaiting_merge"
