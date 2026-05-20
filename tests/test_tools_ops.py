"""Tests for orchestrator/tools/ops.py — operational MCP tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
#
# This section consolidates the F-008-U-2 contracts for the tail_worker
# MCP tool. Coder-authored coverage of the four status branches +
# validation + read-only contract is interleaved with the tester's
# additional pins on:
#
#   * MCP-registry integration (the @mcp.tool() decorator actually fires)
#   * CLAUDE.md persona doc updated for the new tool
#   * Role → session_id resolution is per-role (no silent fall-through)
#   * make_worker is called with the requested role (backend selection
#     plumbs through correctly)
#   * Status-aware formatting strings match the spec verbatim
#
# Originally landed in two files; folded here to honour the
# tests/test_<module>.py convention from CONTRIBUTING.md.


def _fake_worker_returning(status: str, messages: list[dict], reason: str | None = None):
    """Build a MagicMock worker whose tail_messages returns a fixed TailResult."""
    fake = MagicMock()
    fake.tail_messages.return_value = {
        "status": status,
        "messages": messages,
        "reason": reason,
    }
    return fake


# --- validation + missing-state paths -------------------------------------


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
    assert "no session for U1/coder — likely never spawned" in msg


def test_tail_worker_invalid_role_returns_actionable_error(tmp_state_db):
    """The role-validation error must list every accepted role so the
    lead can correct the call without grepping the source."""
    msg = ops.tail_worker("U1", role="ceo")
    assert msg.startswith("ERROR")
    for valid in ("coder", "tester", "reviewer"):
        assert valid in msg


# --- status-aware formatting (substring + verbatim spec phrase) ----------


def test_tail_worker_running_status_formatting(tmp_state_db, monkeypatch):
    """`running` → 'worker active, last N messages' header + messages."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = _fake_worker_returning(
        "running",
        [
            {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "opening branch"},
            {"ts": "2025-01-01T12:00:01Z", "role": "agent", "text": "running tests"},
        ],
    )
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


def test_tail_worker_running_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Pin the spec phrasing verbatim — comma, plural noun, N substituted —
    so a regression that dropped the comma or pluralised differently
    surfaces immediately."""
    _seed_unit(coder_session_id="sesn_xyz")
    msgs = [
        {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "one"},
        {"ts": "2025-01-01T12:00:01Z", "role": "agent", "text": "two"},
        {"ts": "2025-01-01T12:00:02Z", "role": "agent", "text": "three"},
    ]
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning("running", msgs),
    )

    out = ops.tail_worker("U1", role="coder")
    assert "worker active, last 3 messages" in out


def test_tail_worker_idle_status_formatting(tmp_state_db, monkeypatch):
    """`idle` → 'worker completed, final messages' header."""
    _seed_unit(tester_session_id="sesn_t")

    fake_worker = _fake_worker_returning(
        "idle",
        [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "TESTS_PASS"}],
    )
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="tester")
    assert "worker completed" in out
    assert "final messages" in out
    assert "TESTS_PASS" in out


def test_tail_worker_idle_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Spec verbatim: `worker completed, final messages`."""
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "idle",
            [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "TESTS_PASS"}],
        ),
    )

    out = ops.tail_worker("U1", role="coder")
    assert "worker completed, final messages" in out


def test_tail_worker_terminated_status_formatting(tmp_state_db, monkeypatch):
    """`terminated` → 'worker dead (reason); last messages before death'."""
    _seed_unit(reviewer_session_id="sesn_r")

    fake_worker = _fake_worker_returning(
        "terminated",
        [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "about to crash"}],
        reason="session.status=terminated",
    )
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="reviewer")
    assert "worker dead" in out
    assert "session.status=terminated" in out
    assert "before death" in out
    assert "about to crash" in out


def test_tail_worker_terminated_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Spec verbatim: ``worker dead (reason); last messages before death`` —
    semicolon + 'before death' tail must be preserved."""
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "terminated",
            [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "oom"}],
            reason="container exit 137",
        ),
    )

    out = ops.tail_worker("U1", role="coder")
    assert "worker dead (container exit 137); last messages before death" in out


def test_tail_worker_terminated_without_reason_still_formats(tmp_state_db, monkeypatch):
    """If `reason` is somehow None on terminated, fall back to the bare
    status so the header still parses."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = _fake_worker_returning("terminated", [], reason=None)
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "worker dead" in out
    # Empty-messages branch drops "last messages before death" in favour
    # of the clearer "no messages before death" tail (see C3 below).
    assert "no messages before death" in out


def test_tail_worker_not_found_status_formatting(tmp_state_db, monkeypatch):
    """`not_found` from the backend (session_id stored but provider doesn't
    know it — e.g. expired managed-agent session) maps to the same 'no
    session for ...' phrase as the unstored-session branch."""
    _seed_unit(coder_session_id="sesn_dead")

    fake_worker = _fake_worker_returning("not_found", [], reason="session not found")
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "no session for U1/coder — likely never spawned" in out


def test_tail_worker_not_found_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Spec verbatim with em-dash: ``no session for unit_id/role — likely
    never spawned``. A regression that dropped the em-dash or fused the
    segments would slip past the looser substring checks above."""
    _seed_unit(coder_session_id="sesn_dead")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning("not_found", []),
    )

    out = ops.tail_worker("U1", role="coder")
    assert "no session for U1/coder — likely never spawned" in out


# --- empty-messages branches — header tail must reflect zero -------------


def test_tail_worker_running_with_no_messages_swaps_header_phrasing(tmp_state_db, monkeypatch):
    """C3: empty messages and a 'last N messages' header contradict each
    other. The running branch swaps to '— no messages yet' so the lead
    never sees 'last 0 messages' + an empty body."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "worker active" in out
    assert "no messages yet" in out
    assert "last 0 messages" not in out


def test_tail_worker_idle_with_no_messages_swaps_header_phrasing(tmp_state_db, monkeypatch):
    """Symmetric C3 fix for idle — 'final messages' header is replaced
    when the backend returned none, so a session that exited cleanly
    without emitting agent.message reads as 'worker completed — no
    messages emitted' rather than 'final messages' + empty body."""
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning("idle", []),
    )

    out = ops.tail_worker("U1", role="coder")
    assert "worker completed" in out
    assert "no messages" in out
    assert "final messages" not in out


# --- limit propagation ---------------------------------------------------


def test_tail_worker_default_limit_is_20(tmp_state_db, monkeypatch):
    """Default limit at the MCP layer is 20 (per F-008 spec). Higher
    backend defaults remain available to power-callers via the kwarg."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    ops.tail_worker("U1", role="coder")
    fake_worker.tail_messages.assert_called_once_with("sesn_xyz", limit=20)


def test_tail_worker_propagates_custom_limit(tmp_state_db, monkeypatch):
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    ops.tail_worker("U1", role="coder", limit=5)
    fake_worker.tail_messages.assert_called_once_with("sesn_xyz", limit=5)


# --- error handling: ValueError + generic exception ----------------------


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


def test_tail_worker_make_worker_value_error_surfaces_as_error(tmp_state_db, monkeypatch):
    """``make_worker`` raises ValueError for an unknown ORCH_WORKER_BACKEND.
    The MCP tool must catch it the same way it catches a backend ValueError —
    a misconfigured env shouldn't crash the loop when the lead asks to tail.
    """
    _seed_unit(coder_session_id="sesn_xyz")

    def bad_factory(role: str):
        raise ValueError("Unknown ORCH_WORKER_BACKEND value: 'bogus'.")

    monkeypatch.setattr("orchestrator.tools.ops.make_worker", bad_factory)

    msg = ops.tail_worker("U1", role="coder")
    assert msg.startswith("ERROR")
    assert "ORCH_WORKER_BACKEND" in msg


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


# --- pluralisation + message line shape ----------------------------------


def test_tail_worker_running_pluralizes_correctly_for_one_message(tmp_state_db, monkeypatch):
    """`last N message` (singular) when N=1; the header reads naturally
    instead of "last 1 messages"."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake_worker = _fake_worker_returning(
        "running",
        [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "x"}],
    )
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake_worker)

    out = ops.tail_worker("U1", role="coder")
    assert "last 1 message" in out
    assert "last 1 messages" not in out


def test_tail_worker_message_lines_render_ts_role_text(tmp_state_db, monkeypatch):
    """Each rendered message line should carry the ts, role, and text
    so the lead can correlate against ``unit_history`` timestamps."""
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "running",
            [
                {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "alpha"},
                {"ts": "2025-01-01T12:00:01Z", "role": "agent", "text": "beta"},
            ],
        ),
    )

    out = ops.tail_worker("U1", role="coder")
    lines = out.splitlines()
    msg_lines = [ln for ln in lines if "alpha" in ln or "beta" in ln]
    assert len(msg_lines) == 2
    for ln in msg_lines:
        assert "2025-01-01T12:00:0" in ln
        assert "agent" in ln


# --- role → session_id resolution (per-role, no fall-through) -----------


def test_tail_worker_role_coder_uses_coder_session_id(tmp_state_db, monkeypatch):
    """Each role must read its own session_id field — a regression that
    routed every role through ``coder_session_id`` would still pass
    single-role tests because the seeded id happens to match the
    field the bug reads."""
    _seed_unit(
        coder_session_id="sesn_coder",
        tester_session_id="sesn_tester",
        reviewer_session_id="sesn_reviewer",
    )
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    ops.tail_worker("U1", role="coder")
    fake.tail_messages.assert_called_once_with("sesn_coder", limit=20)


def test_tail_worker_role_tester_uses_tester_session_id(tmp_state_db, monkeypatch):
    _seed_unit(
        coder_session_id="sesn_coder",
        tester_session_id="sesn_tester",
        reviewer_session_id="sesn_reviewer",
    )
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    ops.tail_worker("U1", role="tester")
    fake.tail_messages.assert_called_once_with("sesn_tester", limit=20)


def test_tail_worker_role_reviewer_uses_reviewer_session_id(tmp_state_db, monkeypatch):
    _seed_unit(
        coder_session_id="sesn_coder",
        tester_session_id="sesn_tester",
        reviewer_session_id="sesn_reviewer",
    )
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    ops.tail_worker("U1", role="reviewer")
    fake.tail_messages.assert_called_once_with("sesn_reviewer", limit=20)


def test_tail_worker_role_isolation_missing_role_session_does_not_fall_back(
    tmp_state_db, monkeypatch
):
    """If the role's own session_id is empty but other roles' ids ARE
    set, ``tail_worker`` must report "no session for unit_id/role" —
    NOT silently fall back to whichever role has a stored id.
    """
    _seed_unit(coder_session_id="", tester_session_id="sesn_t", reviewer_session_id="sesn_r")
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    msg = ops.tail_worker("U1", role="coder")
    assert "no session for U1/coder" in msg
    assert "never spawned" in msg
    # And critically: make_worker must NOT have been invoked, because
    # the no-session branch should short-circuit before backend lookup.
    fake.tail_messages.assert_not_called()


def test_tail_worker_factory_called_with_requested_role(tmp_state_db, monkeypatch):
    """The factory dispatches to managed-agents / docker; the role
    argument is what selects the correct prompt + identity per backend.
    A regression where ``make_worker`` was always called with
    ``role='coder'`` (or with the unit_id) would silently break
    tester/reviewer worker semantics."""
    _seed_unit(tester_session_id="sesn_t")
    captured: dict[str, str] = {}

    def fake_factory(role: str):
        captured["role"] = role
        return _fake_worker_returning("running", [])

    monkeypatch.setattr("orchestrator.tools.ops.make_worker", fake_factory)
    ops.tail_worker("U1", role="tester")

    assert captured["role"] == "tester"


# --- read-only contract --------------------------------------------------


def test_tail_worker_is_read_only(tmp_state_db, monkeypatch):
    """No state.db mutations, no events — `tail_worker` is poll-on-demand
    observability. Compare to `resume_unit` which is also read-only.

    Calls multiple times to amplify any per-call drift (the
    headless-daemon phase will poll this surface repeatedly).
    """
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "running",
            [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "working"}],
        ),
    )

    pre = state.get_unit_state("U1")
    pre_events = state.list_events("U1")

    for _ in range(3):
        ops.tail_worker("U1", role="coder")

    post = state.get_unit_state("U1")
    assert post.status == pre.status
    assert post.last_activity == pre.last_activity
    assert post.last_error == pre.last_error
    assert state.list_events("U1") == pre_events


# --- MCP-registry integration --------------------------------------------


def test_tail_worker_is_registered_as_mcp_tool():
    """The unit description says "Register MCP tool" — assert it actually
    shows up in the FastMCP registry under the spec name. A future
    refactor that drops the ``@mcp.tool()`` decorator on ``tail_worker``
    would leave every behavioural test green while the production MCP
    server silently lost the tool.
    """
    from orchestrator.tools import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "tail_worker" in names, (
        f"tail_worker missing from MCP tool registry — found: {sorted(names)}"
    )


def test_tail_worker_mcp_signature_advertises_unit_id_role_limit():
    """The registered tool's parameter schema must include the three
    arguments named in the unit title: ``unit_id``, ``role``, ``limit``.
    Drift in the signature (e.g. accidentally dropping ``limit`` to a
    private default) would silently change the LLM's available
    surface."""
    from orchestrator.tools import mcp

    tools = asyncio.run(mcp.list_tools())
    tail = next(t for t in tools if t.name == "tail_worker")
    schema = tail.inputSchema or {}
    properties = schema.get("properties", {})
    assert "unit_id" in properties
    assert "role" in properties
    assert "limit" in properties


# --- CLAUDE.md persona doc updated ---------------------------------------


def _claude_md_text() -> str:
    """Read CLAUDE.md as a single string for substring assertions.

    Located via the repo root one parent above ``tests/``. The persona
    doc is checked in at the repo root by convention (see
    ``CONTRIBUTING.md`` § "Repo layout").
    """
    root = Path(__file__).resolve().parent.parent
    return (root / "CLAUDE.md").read_text(encoding="utf-8")


def test_claude_md_persona_documents_tail_worker_signature():
    """The persona must advertise the tool by name + signature so the
    lead knows when to call it. Catches the case where the impl ships
    but the persona never learns the new vocabulary.
    """
    text = _claude_md_text()
    assert "tail_worker" in text
    assert "tail_worker(unit_id, role" in text


def test_claude_md_persona_describes_all_four_statuses():
    """Each of the four ``TailStatus`` values must show up in the persona
    so the lead can interpret the tool's output. Missing one (e.g.
    ``not_found``) would make the lead confused about a real-world
    response from that branch.
    """
    text = _claude_md_text()
    for status in ("running", "idle", "terminated", "not_found"):
        assert status in text, f"persona doc is missing the {status!r} branch guidance"


def test_claude_md_persona_terminated_guidance_points_to_resume_unit_and_escalation():
    """The unit description explicitly says: "terminated should be
    followed up with resume_unit + escalation". The persona must
    encode that follow-up so the lead knows what to do when the
    backend reports a dead session.
    """
    text = _claude_md_text()
    assert "tail_worker" in text
    lower = text.lower()
    assert "resume_unit" in text, "persona must mention resume_unit as the terminated follow-up"
    assert "escalat" in lower, "persona must mention escalation as the terminated follow-up"


def test_claude_md_persona_describes_when_to_call():
    """Description says "usage guidance (when to call ...)". The persona
    needs concrete trigger conditions, not just a tool-signature blurb.
    """
    text = _claude_md_text().lower()
    cues = ["blocking", "what's the coder doing", "triag", "hung", "progress"]
    assert any(cue in text for cue in cues), (
        f"persona doc must include at least one 'when to call' usage cue out of {cues}"
    )


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
