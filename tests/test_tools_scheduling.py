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


def _make_fake_one(call_log, log_lock, sleep_s=0.1, barrier=None):
    """Create a fake do_one that mimics a successful spawn+cycle.

    When ``barrier`` is supplied, each invocation blocks on it before
    returning, so every pooled task is provably in-flight simultaneously.
    That makes the "distinct threads used" assertion deterministic:
    ``ThreadPoolExecutor(max_workers=N)`` treats N as a *ceiling*, so a
    task that finishes before the pool lazily spins up the Nth worker may
    legally reuse an idle thread — yielding <N distinct ids and a flake on
    slow runners (seen on windows-py3.11). Gating every task on the
    barrier forces one live thread per concurrent task. The timeout turns
    a genuine concurrency regression (serial execution) into a loud
    failure instead of a hang.
    """

    def fake(*args, **kwargs):
        with log_lock:
            call_log.append((threading.get_ident(), time.time()))
        if barrier is not None:
            barrier.wait(timeout=5.0)
        time.sleep(sleep_s)
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


def test_parallel_units_runs_concurrently(tmp_state_db, monkeypatch):
    call_log = []
    log_lock = threading.Lock()
    barrier = threading.Barrier(3)
    fake = _make_fake_one(call_log, log_lock, sleep_s=0.2, barrier=barrier)
    monkeypatch.setattr(scheduling, "_run_one", fake)

    start = time.time()
    out = scheduling.parallel_units("F-001", ["U1", "U2", "U3"], max_concurrent=3)
    elapsed = time.time() - start

    parsed = json.loads(out)
    assert parsed["unit_count"] == 3
    assert parsed["max_concurrent"] == 3
    # 3 units × 0.2s each = 0.6s serial; parallel should be ~0.2s. Threshold
    # sits below 0.6s (real serial regression) but above the per-thread
    # startup cost macOS/Windows CI runners pay on cold pools — 0.5s was
    # tight enough to flake on slower runners (seen on PR #47 cycle 4).
    assert elapsed < 0.55
    # Three distinct threads used
    thread_ids = {t for t, _ in call_log}
    assert len(thread_ids) == 3


def test_parallel_units_caps_concurrency_at_5(tmp_state_db, monkeypatch):
    fake = _make_fake_one([], threading.Lock(), sleep_s=0.0)
    monkeypatch.setattr(scheduling, "_run_one", fake)

    out = scheduling.parallel_units(
        "F-001", ["U1", "U2", "U3", "U4", "U5", "U6", "U7"], max_concurrent=100
    )
    parsed = json.loads(out)
    # Cap is min(max_concurrent, len(unit_ids), 5)
    assert parsed["max_concurrent"] == 5


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


def test_parallel_units_global_runs_cross_feature_concurrent(tmp_state_db, monkeypatch):
    call_log = []
    log_lock = threading.Lock()
    barrier = threading.Barrier(3)
    fake = _make_fake_one(call_log, log_lock, sleep_s=0.15, barrier=barrier)
    monkeypatch.setattr(scheduling, "_run_one", fake)

    refs = [
        {"feature_id": "F-001", "unit_id": "U-1"},
        {"feature_id": "F-002", "unit_id": "U-1"},
        {"feature_id": "F-003", "unit_id": "U-1"},
    ]
    start = time.time()
    out = scheduling.parallel_units_global(refs, max_concurrent=3)
    elapsed = time.time() - start

    parsed = json.loads(out)
    assert parsed["unit_count"] == 3
    # ~0.15s parallel vs 0.45s serial. Threshold stays below the serial
    # baseline (correct = strictly parallel) but loosened above 0.4s after
    # PR #47 cycle 4 surfaced flakes on slower macOS / Windows runners
    # where thread pool startup cost can land in the 0.4-0.5s window.
    assert elapsed < 0.55
    thread_ids = {t for t, _ in call_log}
    assert len(thread_ids) == 3


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

    # Thread-pool callers always block, so _run_one routes through the
    # explicit blocking variant (F-016-U-6). Patch THAT symbol — patching
    # the legacy dispatcher would no-op.
    monkeypatch.setattr(scheduling, "cycle_review_blocking", boom)
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
        "cycle_review_blocking",
        lambda f, u: json.dumps({"outcome": "approved_awaiting_merge", "message": "ok"}),
    )
    out = scheduling._run_one("F-001", "U-X")
    assert out["unit_id"] == "U-X"
    assert out["pr_number"] == 7
    assert out["cycle_outcome"] == "approved_awaiting_merge"
