"""Tests for orchestrator/tools/health.py — canonical inspect_unit_health surface.

Focuses on the wiring contract from F-014-U-2:

  * ``inspect_unit_health(unit_id, dry_run=False)`` applies the same
    ``merged → done`` transitions ``reconcile_unit_pr`` does and routes
    them through ``state.touch_unit`` / ``state.record_event`` (no
    duplicated write paths).
  * Each :class:`~orchestrator.health.ShadowDecision` becomes a
    ``shadow_transition_proposed`` event with the full structured
    payload in ``details``.
  * A ``health_report_snapshot`` event is recorded at most once per
    interval (``ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS``) per unit.
  * ``dry_run=True`` mutates nothing — no actions applied, no shadow
    events, no snapshot.
  * Deprecated aliases (``check_unit_pr`` / ``reconcile_unit_pr``)
    keep their legacy shape and log a deprecation warning.
  * ``inspect_unit_health`` is registered in the MCP tool registry.
"""

from __future__ import annotations

import asyncio
import json
import logging

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import health, ops

# --------------------------- helpers ---------------------------


def _seed_unit(unit_id="U1", feature_id="F", status="in_ci", pr_number=5, **kwargs):
    state.save_feature(
        Feature(id=feature_id, title="t", description="d", repo_path="https://github.com/o/r")
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="b",
            pr_number=pr_number,
            **kwargs,
        )
    )


def _stub_pr_merged(monkeypatch, *, merge_commit_sha: str | None = None):
    """Stub ``github.get_pr_state`` / ``get_pr_check_runs`` for a merged PR."""
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_state",
        lambda url, pr: {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-20T10:00:00Z",
            "head_sha": "abc",
            "merge_commit_sha": merge_commit_sha,
            "mergeable": None,
            "mergeable_state": "clean",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


def _stub_pr_open(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_state",
        lambda url, pr: {
            "state": "open",
            "merged": False,
            "head_sha": "abc",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


def _stub_pr_open_with_conflict(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_state",
        lambda url, pr: {
            "state": "open",
            "merged": False,
            "head_sha": "abc",
            "mergeable": False,
            "mergeable_state": "dirty",
            "conflict_files": ["src/a.py"],
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


def _suppress_cycle_log(monkeypatch):
    """Stub ``cycle_log.write_cycle_log`` so tests don't shell out to git."""
    monkeypatch.setattr("orchestrator.tools.health.cycle_log.write_cycle_log", lambda *a, **k: None)


def _disable_snapshot(monkeypatch):
    """Disable snapshot retention so tests can isolate other event types."""
    monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "0")


# --------------------------- validation paths ---------------------------


def test_inspect_unit_health_missing_pr(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description=""))
    state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
    msg = health.inspect_unit_health("U1")
    assert "ERROR" in msg
    assert "no PR" in msg


def test_inspect_unit_health_missing_unit(tmp_state_db):
    assert "no PR" in health.inspect_unit_health("nope")


def test_inspect_unit_health_missing_token(tmp_state_db, no_github_token):
    _seed_unit(pr_number=5)
    msg = health.inspect_unit_health("U1")
    assert "no GitHub auth" in msg


def test_inspect_unit_health_handles_github_error(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("orchestrator.tools.health.github.get_pr_state", boom)
    msg = health.inspect_unit_health("U1")
    assert "ERROR querying GitHub" in msg
    assert "network down" in msg


# --------------------------- apply actions (non-dry-run) ---------------------------


def test_inspect_unit_health_flips_in_ci_to_done_on_merged(
    tmp_state_db, with_github_token, monkeypatch
):
    """Non-dry-run on a merged PR with status=in_ci must transition to done
    and emit a 'merged' event — same observable effect as the deprecated
    ``reconcile_unit_pr`` alias."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_merged(monkeypatch)
    _suppress_cycle_log(monkeypatch)
    _disable_snapshot(monkeypatch)

    out = health.inspect_unit_health("U1")
    assert "Applied actions" in out
    assert "transition → done" in out

    refreshed = state.get_unit_state("U1")
    assert refreshed.status == "done"
    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert event_types.count("merged") == 1


def test_inspect_unit_health_clears_last_error_on_escalated_merge(
    tmp_state_db, with_github_token, monkeypatch
):
    _seed_unit(pr_number=5, status="escalated")
    state.touch_unit("U1", error="cap-3 cycle hit")
    _stub_pr_merged(monkeypatch)
    _suppress_cycle_log(monkeypatch)
    _disable_snapshot(monkeypatch)

    health.inspect_unit_health("U1")
    refreshed = state.get_unit_state("U1")
    assert refreshed.status == "done"
    assert refreshed.last_error == ""

    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert "merged" in event_types
    assert "recovered_from_escalated" in event_types
    recovered = next(
        e for e in state.list_events("U1") if e["event_type"] == "recovered_from_escalated"
    )
    # Prior last_error preserved as the recovered event's details for audit.
    assert "cap-3 cycle hit" in recovered["details"]


def test_inspect_unit_health_open_pr_no_transition(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)
    _disable_snapshot(monkeypatch)

    out = health.inspect_unit_health("U1")
    assert "## Applied actions (0)" in out
    assert state.get_unit_state("U1").status == "in_ci"
    # No state-mutating events emitted by the empty decision.
    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert "merged" not in event_types
    assert "reconcile_refused" not in event_types


def test_inspect_unit_health_conflict_emits_event_only(
    tmp_state_db, with_github_token, monkeypatch
):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open_with_conflict(monkeypatch)
    _disable_snapshot(monkeypatch)

    health.inspect_unit_health("U1")
    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert "pr_conflict_detected" in event_types
    # Event-only — status unchanged.
    assert state.get_unit_state("U1").status == "in_ci"


def test_inspect_unit_health_ci_drift_sets_last_error(
    tmp_state_db, with_github_token, monkeypatch
):
    """``ci_drift_detected`` carries ``set_last_error`` on its Action; the
    apply layer must write it to ``last_error`` without changing the
    status — pinning the "events can populate last_error" contract.
    """
    _seed_unit(pr_number=5, status="approved_awaiting_merge")
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_state",
        lambda url, pr: {
            "state": "open",
            "merged": False,
            "head_sha": "abc",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_check_runs",
        lambda url, pr: {
            "total": 1,
            "conclusion_counts": {"failure": 1},
            "runs": [
                {"name": "ci", "status": "completed", "conclusion": "failure", "details_url": ""}
            ],
        },
    )
    _disable_snapshot(monkeypatch)

    health.inspect_unit_health("U1")
    refreshed = state.get_unit_state("U1")
    # Status unchanged — drift is observe-and-log, not auto-repair.
    assert refreshed.status == "approved_awaiting_merge"
    assert "CI drift" in refreshed.last_error
    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert "ci_drift_detected" in event_types


def test_inspect_unit_health_writes_via_state_primitives(
    tmp_state_db, with_github_token, monkeypatch
):
    """The spec's reuse contract — actions flow through ``state.touch_unit`` /
    ``state.record_event``. A spy on each call verifies the wiring rather
    than re-checking the downstream effect (already covered above)."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_merged(monkeypatch)
    _suppress_cycle_log(monkeypatch)
    _disable_snapshot(monkeypatch)

    touch_calls: list = []
    record_calls: list = []
    real_touch = state.touch_unit
    real_record = state.record_event

    def spy_touch(*args, **kwargs):
        touch_calls.append((args, kwargs))
        return real_touch(*args, **kwargs)

    def spy_record(*args, **kwargs):
        record_calls.append((args, kwargs))
        return real_record(*args, **kwargs)

    monkeypatch.setattr("orchestrator.tools.health.state.touch_unit", spy_touch)
    monkeypatch.setattr("orchestrator.tools.health.state.record_event", spy_record)

    health.inspect_unit_health("U1")
    # transition done routed through touch_unit; merged event through record_event.
    assert any(call_kwargs.get("status") == "done" for _, call_kwargs in touch_calls)
    record_types = [args[2] for args, _ in record_calls]
    assert "merged" in record_types


def test_inspect_unit_health_dry_run_does_not_mutate(tmp_state_db, with_github_token, monkeypatch):
    """dry_run=True is read-only: no transitions, no events, no shadows
    persisted, no snapshot. The decision IS computed (and surfaced in
    the markdown digest) but never applied."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_merged(monkeypatch)

    pre = state.get_unit_state("U1")
    pre_events = state.list_events("U1")

    out = health.inspect_unit_health("U1", dry_run=True)

    assert "dry_run" in out
    # Decision was computed (predicted action present in the digest)…
    assert "transition → done" in out
    # …but no state changes.
    post = state.get_unit_state("U1")
    assert post.status == pre.status
    assert post.last_error == pre.last_error
    assert state.list_events("U1") == pre_events


# --------------------------- shadow-decision recording ---------------------------


def test_inspect_unit_health_records_shadow_transition_proposed(
    tmp_state_db, with_github_token, monkeypatch
):
    """A real shadow decision: escalated unit with a merged PR observes
    ``merged → done`` as a live action but the ``merge_reverted_flag`` /
    ``escalated_to_in_ci_reset`` rules are evaluated against the same
    snapshot. We engineer the escalated-reset trigger by stubbing the
    GH client to report CI green + an approval."""
    _seed_unit(pr_number=5, status="escalated")
    _disable_snapshot(monkeypatch)

    # PR open + CI green + (synthesised) approval triggers the shadow
    # rule. We patch the production GH client's no-data defaults via
    # subclassing so the review-list reports one approval — exercising
    # the shadow-record path end-to-end without needing F-015's full
    # branch-protection probe.
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_state",
        lambda url, pr: {
            "state": "open",
            "merged": False,
            "head_sha": "abc",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_check_runs",
        lambda url, pr: {
            "total": 1,
            "conclusion_counts": {"success": 1},
            "runs": [
                {"name": "ci", "status": "completed", "conclusion": "success", "details_url": ""}
            ],
        },
    )

    real_client = health._ProductionGitHubClient

    class _GhWithApproval(real_client):
        def get_reviews(self, unit_id):  # noqa: ARG002
            return [{"state": "APPROVED", "user": {"login": "alice"}, "dismissed": False}]

    monkeypatch.setattr("orchestrator.tools.health._ProductionGitHubClient", _GhWithApproval)

    health.inspect_unit_health("U1")
    shadow_events = [
        e for e in state.list_events("U1") if e["event_type"] == "shadow_transition_proposed"
    ]
    assert len(shadow_events) >= 1
    ev = shadow_events[0]
    payload = json.loads(ev["details"])
    # Full structured payload per the spec.
    assert payload["rule_name"] == "escalated_to_in_ci_reset"
    assert payload["predicted_action"]["kind"] == "transition"
    assert payload["predicted_action"]["target_status"] == "in_ci"
    assert payload["predicted_action"]["clear_error"] is True
    assert isinstance(payload["trigger_inputs"], dict)
    assert payload["trigger_inputs"]["status"] == "escalated"
    assert payload["trigger_inputs"]["ci_green"] is True
    assert isinstance(payload["rationale"], str)
    assert payload["rationale"].strip()


def test_inspect_unit_health_no_shadow_means_no_shadow_event(
    tmp_state_db, with_github_token, monkeypatch
):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)
    _disable_snapshot(monkeypatch)

    health.inspect_unit_health("U1")
    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert "shadow_transition_proposed" not in event_types


def test_inspect_unit_health_dry_run_does_not_record_shadow_events(
    tmp_state_db, with_github_token, monkeypatch
):
    """dry_run=True must surface shadow decisions in the digest but not
    persist them — the read-only contract holds even for the shadow
    channel."""
    _seed_unit(pr_number=5, status="escalated")
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_state",
        lambda url, pr: {
            "state": "open",
            "merged": False,
            "head_sha": "abc",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.health.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )

    real_client = health._ProductionGitHubClient

    class _GhWithApproval(real_client):
        def get_reviews(self, unit_id):  # noqa: ARG002
            return [{"state": "APPROVED", "user": {"login": "a"}, "dismissed": False}]

    monkeypatch.setattr("orchestrator.tools.health._ProductionGitHubClient", _GhWithApproval)

    pre_events = state.list_events("U1")
    out = health.inspect_unit_health("U1", dry_run=True)

    # Shadow decision IS surfaced to the lead in the digest.
    assert "escalated_to_in_ci_reset" in out
    # But no event written to state.db.
    assert state.list_events("U1") == pre_events


# --------------------------- snapshot retention ---------------------------


def test_inspect_unit_health_records_snapshot_on_first_probe(
    tmp_state_db, with_github_token, monkeypatch
):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)
    monkeypatch.delenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", raising=False)

    health.inspect_unit_health("U1")
    snaps = [e for e in state.list_events("U1") if e["event_type"] == "health_report_snapshot"]
    assert len(snaps) == 1
    # Details column carries the serialised HealthReport for forensics.
    payload = json.loads(snaps[0]["details"])
    assert payload["unit_id"] == "U1"
    assert "pr" in payload
    assert "ci" in payload
    assert "orchestrator" in payload


def test_inspect_unit_health_snapshot_deduped_within_interval(
    tmp_state_db, with_github_token, monkeypatch
):
    """Second probe within the interval window writes no second snapshot —
    the dedupe is the whole point of 'first probe per unit per UTC day'."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)
    monkeypatch.delenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", raising=False)

    health.inspect_unit_health("U1")
    health.inspect_unit_health("U1")
    health.inspect_unit_health("U1")

    snaps = [e for e in state.list_events("U1") if e["event_type"] == "health_report_snapshot"]
    assert len(snaps) == 1


def test_inspect_unit_health_snapshot_disabled_when_env_var_zero(
    tmp_state_db, with_github_token, monkeypatch
):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)
    monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "0")

    health.inspect_unit_health("U1")
    snaps = [e for e in state.list_events("U1") if e["event_type"] == "health_report_snapshot"]
    assert snaps == []


def test_inspect_unit_health_snapshot_invalid_env_var_falls_back_to_default(
    tmp_state_db, with_github_token, monkeypatch
):
    """Garbage in the env var must not crash the probe — fall back to the
    24h default so forensics retention stays on."""
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)
    monkeypatch.setenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", "not-a-number")

    health.inspect_unit_health("U1")
    snaps = [e for e in state.list_events("U1") if e["event_type"] == "health_report_snapshot"]
    assert len(snaps) == 1


def test_inspect_unit_health_dry_run_does_not_snapshot(
    tmp_state_db, with_github_token, monkeypatch
):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_open(monkeypatch)
    monkeypatch.delenv("ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS", raising=False)

    health.inspect_unit_health("U1", dry_run=True)
    snaps = [e for e in state.list_events("U1") if e["event_type"] == "health_report_snapshot"]
    assert snaps == []


# --------------------------- markdown digest shape ---------------------------


def test_inspect_unit_health_returns_markdown_digest(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5, status="in_ci")
    _stub_pr_merged(monkeypatch, merge_commit_sha="abc123")
    _suppress_cycle_log(monkeypatch)
    _disable_snapshot(monkeypatch)

    out = health.inspect_unit_health("U1")
    # Heading + the three required sections per the spec.
    assert out.startswith("# inspect_unit_health: U1")
    assert "## HealthReport summary" in out
    assert "## Applied actions" in out
    assert "## Shadow decisions" in out


# --------------------------- deprecated aliases ---------------------------


def test_check_unit_pr_alias_still_returns_legacy_json_shape(
    tmp_state_db, with_github_token, monkeypatch
):
    """Backward compatibility — the deprecated alias keeps the exact JSON
    shape so existing dashboards / scripts don't break."""
    _seed_unit(pr_number=5, status="in_ci")
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )
    parsed = json.loads(ops.check_unit_pr("U1"))
    assert set(parsed.keys()) == {
        "unit_id",
        "pr_number",
        "pr_state",
        "checks",
        "orchestrator_status",
    }


def test_check_unit_pr_alias_logs_deprecation_warning(
    tmp_state_db, with_github_token, monkeypatch, caplog
):
    _seed_unit(pr_number=5, status="in_ci")
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )

    with caplog.at_level(logging.WARNING, logger="orchestrator.tools.ops"):
        ops.check_unit_pr("U1")
    deprecation = [r for r in caplog.records if "deprecated" in r.getMessage()]
    assert deprecation, "check_unit_pr must log a deprecation warning"
    assert any("inspect_unit_health" in r.getMessage() for r in deprecation)


def test_reconcile_unit_pr_alias_logs_deprecation_warning(
    tmp_state_db, with_github_token, monkeypatch, caplog
):
    _seed_unit(pr_number=5, status="in_ci")
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )

    with caplog.at_level(logging.WARNING, logger="orchestrator.tools.ops"):
        ops.reconcile_unit_pr("U1")
    deprecation = [r for r in caplog.records if "deprecated" in r.getMessage()]
    # Two warnings expected (reconcile + its internal check_unit_pr delegation).
    assert any("reconcile_unit_pr" in r.getMessage() for r in deprecation)
    assert any("inspect_unit_health" in r.getMessage() for r in deprecation)


def test_reconcile_unit_pr_alias_still_flips_in_ci_to_done(
    tmp_state_db, with_github_token, monkeypatch
):
    """The whole point of the alias is behaviour-preservation — the
    happy-path merged transition must still fire end-to-end."""
    _seed_unit(pr_number=5, status="in_ci")
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-20T10:00:00Z",
            "head_sha": "abc",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )

    parsed = json.loads(ops.reconcile_unit_pr("U1"))
    assert parsed["reconciled"] is True
    assert parsed["action"] == "merged-from-in_ci"
    assert state.get_unit_state("U1").status == "done"


# --------------------------- MCP registry ---------------------------


def test_inspect_unit_health_is_registered_as_mcp_tool():
    """A future refactor that drops the ``@mcp.tool()`` decorator would
    leave every behavioural test green while the production MCP server
    silently lost the canonical surface."""
    from orchestrator.tools import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "inspect_unit_health" in names


def test_inspect_unit_health_mcp_signature_advertises_unit_id_and_dry_run():
    from orchestrator.tools import mcp

    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == "inspect_unit_health")
    schema = tool.inputSchema or {}
    properties = schema.get("properties", {})
    assert "unit_id" in properties
    assert "dry_run" in properties


# --------------------------- CLAUDE.md ---------------------------


def test_claude_md_documents_inspect_unit_health_as_canonical():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "inspect_unit_health" in text
    # The aliases are explicitly marked deprecated.
    assert "deprecated" in text.lower()
    # And recommended in new flows.
    assert "inspect_unit_health(unit_id)" in text or "inspect_unit_health(X)" in text
