"""Tests for orchestrator/tools/ops.py — operational MCP tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import ops


def _seed_unit(feature_id="F", unit_id="U1", status="coding", **kwargs):
    state.save_feature(
        Feature(id=feature_id, title="t", description="d", repo_path="https://github.com/o/r"),
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="b",
            **kwargs,
        ),
    )


# --------------------------- hello_world_test ---------------------------


def test_hello_world_test_calls_managed_agent(tmp_state_db, monkeypatch):
    """Verifies the smoke test spawns a worker and archives — no real API calls."""
    fake_worker = MagicMock()
    fake_worker.spawn.return_value = ("sesn_fake", "hello from a managed agent")
    fake_worker.archive.return_value = None

    monkeypatch.setattr("orchestrator.tools.ops.ManagedAgentWorker", lambda role: fake_worker)

    out = ops.hello_world_test()
    assert "session_id=sesn_fake" in out
    assert "hello from a managed agent" in out
    fake_worker.spawn.assert_called_once()
    fake_worker.archive.assert_called_once_with("sesn_fake")


# --------------------------- check_unit_pr ---------------------------


def test_check_unit_pr_no_pr(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description=""))
    state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
    assert "ERROR" in ops.check_unit_pr("U1")


def test_check_unit_pr_unknown_unit(tmp_state_db):
    assert "no PR" in ops.check_unit_pr("nope")


def test_check_unit_pr_missing_token(tmp_state_db, no_github_token):
    _seed_unit(pr_number=5)
    msg = ops.check_unit_pr("U1")
    assert "no GitHub auth" in msg


def _stub_pr_merged(monkeypatch, merged_at: str = "2026-05-11T19:00:00Z"):
    """Stub get_pr_state/get_pr_check_runs to report a merged PR."""
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {
            "state": "closed",
            "merged": True,
            "merged_at": merged_at,
            "head_sha": "abc",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


def _stub_pr_open(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


def _stub_pr_closed_unmerged(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "closed", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


def test_check_unit_pr_is_readonly_on_unmerged(tmp_state_db, with_github_token, monkeypatch):
    """Unmerged PR: state unchanged, zero new events recorded."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)

    pre = state.get_unit_state("U1")
    pre_events = state.list_events("U1")

    out = ops.check_unit_pr("U1")
    parsed = json.loads(out)
    assert parsed["orchestrator_status"] == "in_ci"

    post = state.get_unit_state("U1")
    assert post.status == pre.status
    assert post.last_error == pre.last_error
    assert state.list_events("U1") == pre_events


def test_check_unit_pr_is_readonly_on_merged(tmp_state_db, with_github_token, monkeypatch):
    """Merged PR: check_unit_pr observes merge but does NOT flip status or
    emit 'merged'. Advancing state is reconcile_unit_pr's job."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_merged(monkeypatch)

    out = ops.check_unit_pr("U1")
    parsed = json.loads(out)
    assert parsed["pr_state"]["merged"] is True
    # Read-only: status stays in_ci until reconcile_unit_pr runs.
    assert parsed["orchestrator_status"] == "in_ci"
    assert state.get_unit_state("U1").status == "in_ci"

    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert "merged" not in event_types


def test_check_unit_pr_handles_github_error(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("orchestrator.tools.ops.github.get_pr_state", boom)

    msg = ops.check_unit_pr("U1")
    assert "ERROR querying GitHub" in msg


# --------------------------- reconcile_unit_pr ---------------------------


def test_reconcile_in_ci_plus_merged_flips_to_done(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_merged(monkeypatch)

    out = ops.reconcile_unit_pr("U1")
    parsed = json.loads(out)
    assert parsed["reconciled"] is True
    assert parsed["action"] == "merged-from-in_ci"
    assert parsed["orchestrator_status"] == "done"
    assert state.get_unit_state("U1").status == "done"

    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert event_types.count("merged") == 1


def test_reconcile_escalated_plus_merged_flips_to_done_clearing_error(
    tmp_state_db, with_github_token, monkeypatch
):
    _seed_unit(pr_number=5, status="escalated")
    state.touch_unit("U1", error="BLOCKED [auth_failure]: 401 from gh")
    _stub_pr_merged(monkeypatch)

    out = ops.reconcile_unit_pr("U1")
    parsed = json.loads(out)
    assert parsed["reconciled"] is True
    assert parsed["action"] == "merged-from-escalated"
    assert parsed["orchestrator_status"] == "done"

    refreshed = state.get_unit_state("U1")
    assert refreshed.status == "done"
    assert refreshed.last_error == ""

    events = state.list_events("U1")
    event_types = [e["event_type"] for e in events]
    assert event_types.count("merged") == 1
    assert event_types.count("recovered_from_escalated") == 1
    recovery = next(e for e in events if e["event_type"] == "recovered_from_escalated")
    # Prior last_error preserved in details for audit.
    assert "401 from gh" in recovery["details"]


def test_reconcile_escalated_plus_open_pr_is_noop(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5, status="escalated")
    state.touch_unit("U1", error="BLOCKED: something")
    _stub_pr_open(monkeypatch)

    pre_events = state.list_events("U1")
    out = ops.reconcile_unit_pr("U1")
    parsed = json.loads(out)

    assert parsed["reconciled"] is False
    assert parsed["action"] == "no-op-pr-not-merged"
    refreshed = state.get_unit_state("U1")
    assert refreshed.status == "escalated"
    assert refreshed.last_error == "BLOCKED: something"
    assert state.list_events("U1") == pre_events


def test_reconcile_active_status_plus_merged_refuses(tmp_state_db, with_github_token, monkeypatch):
    """coding/testing/reviewing/fixing/opening_pr + merged = racy; refuse +
    emit 'reconcile_refused'. Unreachable in practice but explicit.

    ``reconciled`` stays False because the unit's status row is NOT
    transitioned — the refusal only records an audit event.
    """
    _stub_pr_merged(monkeypatch)
    for status in ("coding", "testing", "reviewing", "fixing", "opening_pr"):
        unit_id = f"U-{status}"
        _seed_unit(unit_id=unit_id, pr_number=5, status=status)

        out = ops.reconcile_unit_pr(unit_id)
        parsed = json.loads(out)
        assert parsed["reconciled"] is False
        assert parsed["action"] == f"refused-from-{status}"

        refreshed = state.get_unit_state(unit_id)
        assert refreshed.status == status  # unchanged

        event_types = [e["event_type"] for e in state.list_events(unit_id)]
        assert "merged" not in event_types
        assert event_types.count("reconcile_refused") == 1


def test_reconcile_closed_unmerged_is_noop_any_status(tmp_state_db, with_github_token, monkeypatch):
    """Closed-but-not-merged is a human decision; orchestrator stays out."""
    _stub_pr_closed_unmerged(monkeypatch)
    for status in ("in_ci", "escalated", "coding"):
        unit_id = f"U-{status}"
        _seed_unit(unit_id=unit_id, pr_number=5, status=status)

        out = ops.reconcile_unit_pr(unit_id)
        parsed = json.loads(out)
        assert parsed["reconciled"] is False
        assert parsed["action"] == "no-op-pr-not-merged"
        assert state.get_unit_state(unit_id).status == status
        assert state.list_events(unit_id) == []


def test_reconcile_idempotent_on_merged_pr(tmp_state_db, with_github_token, monkeypatch):
    """Calling twice on the same merged PR emits 'merged' exactly once."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_merged(monkeypatch)

    out1 = ops.reconcile_unit_pr("U1")
    assert json.loads(out1)["action"] == "merged-from-in_ci"

    out2 = ops.reconcile_unit_pr("U1")
    parsed2 = json.loads(out2)
    assert parsed2["reconciled"] is False
    assert parsed2["action"] == "no-op-already-done"

    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert event_types.count("merged") == 1


def test_reconcile_surfaces_check_unit_pr_errors(tmp_state_db, with_github_token, monkeypatch):
    """reconcile delegates to check_unit_pr; upstream errors propagate verbatim."""
    _seed_unit(pr_number=5)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("orchestrator.tools.ops.github.get_pr_state", boom)

    msg = ops.reconcile_unit_pr("U1")
    assert "ERROR querying GitHub" in msg


def test_reconcile_missing_pr(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description=""))
    state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
    assert "ERROR" in ops.reconcile_unit_pr("U1")


def test_reconcile_missing_token(tmp_state_db, no_github_token):
    _seed_unit(pr_number=5)
    msg = ops.reconcile_unit_pr("U1")
    assert "no GitHub auth" in msg


def test_reconcile_flag_matrix_pins_per_branch_semantic(
    tmp_state_db, with_github_token, monkeypatch
):
    """Single-shot pin for the ``reconciled`` flag across every branch.

    ``reconciled`` means "this call transitioned the unit's status row to
    done". True ONLY on the two merging branches; False on every no-op
    and every refusal (which records an audit event but leaves status
    untouched). Reviewer M1 on PR #33 caught the prior inconsistency
    where refused-from-* paths returned True alongside an unchanged
    status — guard against regressing that.
    """
    cases = [
        # (status, pr-stub, expected reconciled, expected action)
        ("in_ci", _stub_pr_merged, True, "merged-from-in_ci"),
        ("escalated", _stub_pr_merged, True, "merged-from-escalated"),
        ("in_ci", _stub_pr_open, False, "no-op-pr-not-merged"),
        ("in_ci", _stub_pr_closed_unmerged, False, "no-op-pr-not-merged"),
        ("done", _stub_pr_merged, False, "no-op-already-done"),
        ("coding", _stub_pr_merged, False, "refused-from-coding"),
        ("testing", _stub_pr_merged, False, "refused-from-testing"),
        ("opening_pr", _stub_pr_merged, False, "refused-from-opening_pr"),
        ("reviewing", _stub_pr_merged, False, "refused-from-reviewing"),
        ("fixing", _stub_pr_merged, False, "refused-from-fixing"),
        ("pending", _stub_pr_merged, False, "refused-from-pending"),
    ]
    for i, (status, stub, expected_reconciled, expected_action) in enumerate(cases):
        unit_id = f"U-mx-{i}"
        _seed_unit(unit_id=unit_id, pr_number=5, status=status)
        stub(monkeypatch)
        parsed = json.loads(ops.reconcile_unit_pr(unit_id))
        assert parsed["reconciled"] is expected_reconciled, (
            f"{status!r}/{expected_action!r}: reconciled flag wrong"
        )
        assert parsed["action"] == expected_action


def test_reconcile_no_op_branches_return_fresh_orchestrator_status(
    tmp_state_db, with_github_token, monkeypatch
):
    """Copilot finding on PR #33: every return path — including the no-op-*
    early-return branches — must surface the *re-read* orchestrator_status,
    not the stale value baked into ``check_unit_pr``'s response.

    Simulate the race: ``check_unit_pr`` reports status=in_ci, then a
    concurrent caller flips the unit to ``done`` before ``reconcile_unit_pr``
    re-reads. The returned ``orchestrator_status`` must reflect the
    post-race row.
    """
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)

    # Patch check_unit_pr to return a stale orchestrator_status. We don't
    # also race state.touch_unit because the early-return path doesn't
    # exercise the post-read transition; the contract is that the final
    # re-read wins regardless of what the poll observed.
    stale_payload = json.dumps(
        {
            "unit_id": "U1",
            "pr_number": 5,
            "pr_state": {"state": "open", "merged": False, "head_sha": "abc"},
            "checks": {"total": 0, "conclusion_counts": {}, "runs": []},
            "orchestrator_status": "in_ci",  # stale: the row will be 'done' below
        },
        indent=2,
    )
    monkeypatch.setattr("orchestrator.tools.ops.check_unit_pr", lambda uid: stale_payload)
    # Simulate concurrent advance between the poll and reconcile's re-read.
    state.touch_unit("U1", status="done")

    parsed = json.loads(ops.reconcile_unit_pr("U1"))
    assert parsed["action"] == "no-op-pr-not-merged"
    assert parsed["orchestrator_status"] == "done"  # re-read wins, not stale 'in_ci'


# --------------------------- list_in_flight ---------------------------


def test_list_in_flight_empty(tmp_state_db):
    out = ops.list_in_flight()
    parsed = json.loads(out)
    assert parsed == []


def test_list_in_flight_returns_only_active_statuses(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description="d"))
    for uid, status in [
        ("U1", "coding"),
        ("U2", "done"),  # not active
        ("U3", "in_ci"),
        ("U4", "escalated"),  # not active
        ("U5", "reviewing"),
    ]:
        state.upsert_unit_state(WorkUnitState(unit_id=uid, feature_id="F", status=status))

    out = ops.list_in_flight()
    parsed = json.loads(out)
    unit_ids = sorted(r["unit_id"] for r in parsed)
    assert unit_ids == ["U1", "U3", "U5"]


def test_list_in_flight_includes_session_flags(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description="d"))
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="U1",
            feature_id="F",
            status="reviewing",
            coder_session_id="sesn_c",
            tester_session_id="sesn_t",
        ),
    )
    out = ops.list_in_flight()
    parsed = json.loads(out)
    row = parsed[0]
    assert row["has_coder_session"] is True
    assert row["has_tester_session"] is True
    assert row["has_reviewer_session"] is False


def test_list_in_flight_no_reason_filter_excludes_escalated(tmp_state_db):
    """Default call (no reason= arg) preserves the original active-only
    semantics — escalated units stay out of the result."""
    state.save_feature(Feature(id="F", title="t", description="d"))
    state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
    state.upsert_unit_state(WorkUnitState(unit_id="U2", feature_id="F", status="escalated"))
    parsed = json.loads(ops.list_in_flight())
    ids = sorted(r["unit_id"] for r in parsed)
    assert ids == ["U1"]


def test_list_in_flight_reason_filter_returns_only_matching_units(tmp_state_db):
    """F-005-U-2 query path: lead asks 'show me everything blocked on auth'
    via list_in_flight(reason='auth_failure'). The escalated-status gate
    opens up, and only units whose latest blocked event slug matches
    come back."""
    state.save_feature(Feature(id="F", title="t", description="d"))
    # Three units, each escalated with a different (or no) reason
    state.upsert_unit_state(WorkUnitState(unit_id="U-auth", feature_id="F", status="escalated"))
    state.record_event(
        "U-auth",
        "F",
        "coder_blocked",
        details=json.dumps({"reason": "auth_failure", "prose": "401"}),
    )
    state.upsert_unit_state(WorkUnitState(unit_id="U-bp", feature_id="F", status="escalated"))
    state.record_event(
        "U-bp",
        "F",
        "coder_blocked",
        details=json.dumps({"reason": "branch_protection_blocked_push", "prose": "denied"}),
    )
    state.upsert_unit_state(WorkUnitState(unit_id="U-active", feature_id="F", status="coding"))

    parsed = json.loads(ops.list_in_flight(reason="auth_failure"))
    ids = [r["unit_id"] for r in parsed]
    assert ids == ["U-auth"]
    # Filtered results expose the slug match for downstream display.
    assert parsed[0]["reason"] == "auth_failure"


def test_list_in_flight_reason_filter_with_no_matches_returns_empty(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description="d"))
    state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="escalated"))
    state.record_event("U1", "F", "coder_blocked", details="plain prose")
    parsed = json.loads(ops.list_in_flight(reason="auth_failure"))
    assert parsed == []


# --------------------------- resume_unit ---------------------------


def test_resume_unit_bad_role(tmp_state_db):
    msg = ops.resume_unit("U1", role="hacker")
    assert "ERROR" in msg
    assert "role must be" in msg


def test_resume_unit_no_state(tmp_state_db):
    msg = ops.resume_unit("nope", role="coder")
    assert "no state" in msg


def test_resume_unit_no_session_id(tmp_state_db):
    _seed_unit()  # no session ids set
    msg = ops.resume_unit("U1", role="coder")
    assert "no coder session_id stored" in msg


def test_resume_unit_returns_session_status(tmp_state_db, monkeypatch):
    _seed_unit(coder_session_id="sesn_xyz")

    fake_session = MagicMock(status="idle", title="my session")
    fake_worker = MagicMock()
    fake_worker.client.beta.sessions.retrieve.return_value = fake_session
    monkeypatch.setattr("orchestrator.tools.ops.ManagedAgentWorker", lambda role: fake_worker)

    out = ops.resume_unit("U1", role="coder")
    parsed = json.loads(out)
    assert parsed["session_id"] == "sesn_xyz"
    assert parsed["session_status"] == "idle"
    assert parsed["role"] == "coder"


def test_resume_unit_handles_retrieve_error(tmp_state_db, monkeypatch):
    _seed_unit(coder_session_id="sesn_xyz")
    fake_worker = MagicMock()
    fake_worker.client.beta.sessions.retrieve.side_effect = RuntimeError("expired")
    monkeypatch.setattr("orchestrator.tools.ops.ManagedAgentWorker", lambda role: fake_worker)

    msg = ops.resume_unit("U1", role="coder")
    assert "ERROR retrieving session" in msg


# --------------------------- tail_worker ---------------------------


def test_tail_worker_bad_role(tmp_state_db):
    msg = ops.tail_worker("U1", role="hacker")
    assert "ERROR" in msg
    assert "role must be" in msg


def test_tail_worker_no_state(tmp_state_db):
    msg = ops.tail_worker("nope", role="coder")
    assert "no state" in msg


def test_tail_worker_no_session_id(tmp_state_db):
    """No session_id stored for the role maps to the 'not_found' format —
    the lead's mental model is "no session for unit_id/role" regardless of
    whether the backend never knew or local state never recorded one."""
    _seed_unit()  # no session ids set
    msg = ops.tail_worker("U1", role="coder")
    assert "no session for U1/coder" in msg
    assert "never spawned" in msg


def test_tail_worker_running_status_formatting(tmp_state_db, monkeypatch):
    """`running` → 'worker active, last N messages' header + messages."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "running",
        "messages": [
            {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "opening branch"},
            {"ts": "2025-01-01T12:00:01Z", "role": "agent", "text": "running tests"},
        ],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder", limit=20)
    assert "worker active" in out
    assert "last 2 messages" in out
    assert "opening branch" in out
    assert "running tests" in out
    # ts + role render in each line
    assert "2025-01-01T12:00:00Z" in out
    assert "agent" in out
    fake_worker.tail_messages.assert_called_once_with("sesn_xyz", limit=20)


def test_tail_worker_idle_status_formatting(tmp_state_db, monkeypatch):
    """`idle` → 'worker completed, final messages' header."""
    _seed_unit(tester_session_id="sesn_t")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "idle",
        "messages": [
            {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "TESTS_PASS"},
        ],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="tester")
    assert "worker completed" in out
    assert "final messages" in out
    assert "TESTS_PASS" in out


def test_tail_worker_terminated_status_formatting(tmp_state_db, monkeypatch):
    """`terminated` → 'worker dead (reason); last messages before death'."""
    _seed_unit(reviewer_session_id="sesn_r")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "terminated",
        "messages": [
            {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "about to crash"},
        ],
        "reason": "session.status=terminated",
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="reviewer")
    assert "worker dead" in out
    assert "session.status=terminated" in out
    assert "before death" in out
    assert "about to crash" in out


def test_tail_worker_terminated_without_reason_still_formats(tmp_state_db, monkeypatch):
    """If `reason` is somehow None on terminated, fall back to the bare
    status so the header still parses."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "terminated",
        "messages": [],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "worker dead" in out
    assert "before death" in out


def test_tail_worker_not_found_status_formatting(tmp_state_db, monkeypatch):
    """`not_found` from the backend (session_id stored but provider doesn't
    know it — e.g. expired managed-agent session) maps to the same 'no
    session for ...' phrase as the unstored-session branch."""
    _seed_unit(coder_session_id="sesn_dead")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "not_found",
        "messages": [],
        "reason": "session not found",
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "no session for U1/coder" in out
    assert "never spawned" in out


def test_tail_worker_default_limit_is_20(tmp_state_db, monkeypatch):
    """Default limit at the MCP layer is 20 (per F-008 spec). Higher
    backend defaults remain available to power-callers via the kwarg."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "running",
        "messages": [],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    ops.tail_worker("U1", role="coder")
    fake_worker.tail_messages.assert_called_once_with("sesn_xyz", limit=20)


def test_tail_worker_propagates_custom_limit(tmp_state_db, monkeypatch):
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "running",
        "messages": [],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    ops.tail_worker("U1", role="coder", limit=5)
    fake_worker.tail_messages.assert_called_once_with("sesn_xyz", limit=5)


def test_tail_worker_invalid_limit_surfaces_error(tmp_state_db, monkeypatch):
    """Backend raises ValueError on limit < 1; surface as ERROR string so
    the lead sees the problem rather than the tool blowing up."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.side_effect = ValueError(
        "tail_messages limit must be an int >= 1, got 0"
    )
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    msg = ops.tail_worker("U1", role="coder", limit=0)
    assert "ERROR" in msg
    assert "limit" in msg


def test_tail_worker_backend_error_surfaces_as_error(tmp_state_db, monkeypatch):
    """Unexpected backend failure (e.g. transport error retrieving the
    session) surfaces as ERROR rather than raising into the MCP loop."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.side_effect = RuntimeError("connection reset")
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    msg = ops.tail_worker("U1", role="coder")
    assert "ERROR" in msg
    assert "connection reset" in msg


def test_tail_worker_is_read_only(tmp_state_db, monkeypatch):
    """No state.db mutations, no events — `tail_worker` is poll-on-demand
    observability. Compare to `resume_unit` which is also read-only."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "running",
        "messages": [
            {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "working"},
        ],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    pre = state.get_unit_state("U1")
    pre_events = state.list_events("U1")

    ops.tail_worker("U1", role="coder")

    post = state.get_unit_state("U1")
    assert post.status == pre.status
    assert post.last_activity == pre.last_activity
    assert state.list_events("U1") == pre_events


def test_tail_worker_running_pluralizes_correctly_for_one_message(tmp_state_db, monkeypatch):
    """`last N message` (singular) when N=1; the header reads naturally
    instead of "last 1 messages"."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "running",
        "messages": [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "x"}],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "last 1 message" in out
    assert "last 1 messages" not in out


def test_tail_worker_running_with_no_messages_uses_zero(tmp_state_db, monkeypatch):
    """An in-flight session that hasn't emitted any agent.message yet
    still tails cleanly — the lead sees the 'worker active' state without
    a phantom messages section."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = MagicMock()
    fake_worker.tail_messages.return_value = {
        "status": "running",
        "messages": [],
        "reason": None,
    }
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "worker active" in out
    # Either "no messages yet" or just an empty messages section is fine;
    # what we must NOT do is print a phantom "last 0 messages" + nothing.
    assert "no messages" in out.lower() or "last 0 messages" not in out


# --------------------------- reset_cached_resources ---------------------------


def test_reset_cached_resources_empty(tmp_state_db):
    msg = ops.reset_cached_resources()
    assert "Cleared 0" in msg


def test_reset_cached_resources_counts_rows(tmp_state_db):
    state.save_cached_resource("coder", "sig1", "agent_1", "env_1")
    state.save_cached_resource("tester", "sig2", "agent_2", "env_2")
    msg = ops.reset_cached_resources()
    assert "Cleared 2" in msg
    assert state.get_cached_resource("coder", "sig1") is None


# --------------------------- verify_repo / list_verified / forget_repo ---------------------------


def _stub_verify_pass(monkeypatch):
    """Patch repo_verify.verify to return a passing result without any HTTP."""
    from orchestrator.models import CheckResult, VerificationResult

    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/owner/repo",
            default_branch="main",
            auth_mode=auth_mode,
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
        )

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", fake_verify)


def _stub_verify_fail(monkeypatch, detail="no rule on main"):
    from orchestrator.models import CheckResult, VerificationResult

    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/owner/repo",
            default_branch="main",
            auth_mode=auth_mode,
            checks=[CheckResult("branch protection exists", False, detail)],
        )

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", fake_verify)


def _stub_token(monkeypatch):
    monkeypatch.setattr("orchestrator.tools.ops.github_app.get_agent_token", lambda: "ghp_fake")
    monkeypatch.setattr("orchestrator.tools.ops.github_app.auth_mode", lambda: "pat")


def test_verify_repo_caches_on_success(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)

    out = ops.verify_repo("github.com/owner/repo")
    assert "✓" in out
    assert "Cached" in out
    cached = state.get_verified_repo("https://github.com/owner/repo")
    assert cached is not None
    assert cached.default_branch == "main"


def test_verify_repo_does_not_cache_on_failure(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    _stub_verify_fail(monkeypatch)

    out = ops.verify_repo("github.com/owner/repo")
    assert "FAILED" in out
    assert state.get_verified_repo("https://github.com/owner/repo") is None


def test_verify_repo_missing_token(tmp_state_db, no_github_token):
    out = ops.verify_repo("github.com/owner/repo")
    assert "no GitHub auth" in out.lower() or "github_token" in out.lower()


def test_verify_repo_handles_value_error(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    # Bad URL form — normalize_repo_url raises before verify() is called
    out = ops.verify_repo("git@github.com:owner/repo")
    assert "ERROR" in out
    assert "SSH" in out


def test_verify_repo_surfaces_github_error(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)

    def boom(url, token, auth_mode="pat"):
        raise RuntimeError("network exploded")

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", boom)
    out = ops.verify_repo("github.com/owner/repo")
    assert "ERROR contacting GitHub" in out
    assert "network exploded" in out


def test_list_verified_repos_empty(tmp_state_db):
    # Clear the fixture's pre-seeded test repos so we exercise the empty path
    state.forget_verified_repo("https://github.com/o/r")
    state.forget_verified_repo("https://github.com/joe/repo")
    out = ops.list_verified_repos()
    assert "No repos have been verified" in out


def test_list_verified_repos_returns_json(tmp_state_db, with_github_token, monkeypatch):
    # Clear the fixture's pre-seeded entries to focus the assertion
    state.forget_verified_repo("https://github.com/o/r")
    state.forget_verified_repo("https://github.com/joe/repo")
    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)
    ops.verify_repo("github.com/owner/repo")

    out = ops.list_verified_repos()
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["repo_url"] == "https://github.com/owner/repo"
    assert parsed[0]["default_branch"] == "main"
    assert parsed[0]["auth_mode"] == "pat"


def test_forget_repo_removes_row(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)
    ops.verify_repo("github.com/owner/repo")
    assert state.get_verified_repo("https://github.com/owner/repo") is not None

    out = ops.forget_repo("github.com/owner/repo")
    assert "Forgot" in out
    assert state.get_verified_repo("https://github.com/owner/repo") is None


def test_forget_repo_missing_url(tmp_state_db):
    out = ops.forget_repo("github.com/never/seen")
    assert "was not in the cache" in out


def test_forget_repo_rejects_bad_url(tmp_state_db):
    out = ops.forget_repo("not a url at all")
    assert "ERROR" in out
