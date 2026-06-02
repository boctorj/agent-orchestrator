"""Tests for ``orchestrator/tools/health.py`` — canonical ``inspect_unit_health``.

Covers F-014-U-2 contracts not directly exercised by the table tests for
the pure decision engine in ``orchestrator/health.py``:

* Action application reuses ``state.touch_unit`` / ``state.record_event`` /
  ``cycle_log.write_cycle_log`` (no duplicate writes).
* Each shadow decision is persisted as a ``shadow_transition_proposed``
  event with the full ``ShadowDecision`` payload in ``details`` JSON.
* ``health_report_snapshot`` events are at most one per unit per UTC day
  and are gated by the ``ORCH_HEALTH_SNAPSHOT_DAILY`` env var.
* ``dry_run=True`` skips every write (no transitions, no events).
* The markdown digest lists applied actions and shadow decisions.
* The tool is registered in the FastMCP registry under the spec name.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import health as health_tool

# --------------------------- fake clients ---------------------------


@dataclass
class FakeGH:
    """Reuses the ``test_health.FakeGH`` shape but kept local to avoid a
    cross-test-module import dependency. The protocol surface is small
    enough that duplication is cheaper than coupling."""

    pr: dict | None = None
    check_runs: list[dict] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    compare: dict = field(default_factory=lambda: {"ahead_by": 0, "behind_by": 0})
    reviews: list[dict] = field(default_factory=list)
    review_threads: list[dict] = field(default_factory=list)
    requested_reviewers: dict = field(default_factory=lambda: {"users": [], "teams": []})
    copilot_review: dict | None = None
    last_force_push_at: str | None = None
    head_commit: dict = field(default_factory=dict)
    merge_commit_on_main: bool = True

    def get_pr(self, unit_id: str) -> dict | None:
        return self.pr

    def get_check_runs(self, unit_id: str) -> list[dict]:
        return list(self.check_runs)

    def get_required_checks(self, unit_id: str) -> list[str]:
        return list(self.required_checks)

    def get_compare_to_base(self, unit_id: str) -> dict:
        return dict(self.compare)

    def get_reviews(self, unit_id: str) -> list[dict]:
        return list(self.reviews)

    def get_review_threads(self, unit_id: str) -> list[dict]:
        return list(self.review_threads)

    def get_requested_reviewers(self, unit_id: str) -> dict:
        return dict(self.requested_reviewers)

    def get_copilot_review(self, unit_id: str) -> dict | None:
        return self.copilot_review

    def get_last_force_push_at(self, unit_id: str) -> str | None:
        return self.last_force_push_at

    def get_head_commit(self, unit_id: str) -> dict:
        return dict(self.head_commit)

    def is_merge_commit_on_main(self, unit_id: str) -> bool:
        return self.merge_commit_on_main


@dataclass
class FakeAnthropic:
    statuses: dict[str, str] = field(default_factory=dict)

    def get_session_status(self, session_id: str) -> str | None:
        return self.statuses.get(session_id)


# --------------------------- helpers ---------------------------


def _seed_unit(
    *,
    unit_id: str = "U1",
    feature_id: str = "F",
    status: str = "in_ci",
    pr_number: int | None = 42,
    review_round: int = 0,
    last_error: str = "",
    coder_session_id: str = "",
    tester_session_id: str = "",
    reviewer_session_id: str = "",
) -> None:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
        )
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="b",
            pr_number=pr_number,
            coder_session_id=coder_session_id,
            tester_session_id=tester_session_id,
            reviewer_session_id=reviewer_session_id,
            review_round=review_round,
            last_error=last_error,
        )
    )


def _install_clients(
    monkeypatch: pytest.MonkeyPatch,
    gh: FakeGH,
    anthropic: FakeAnthropic | None = None,
) -> None:
    """Route the production client factories to fakes."""
    monkeypatch.setattr(health_tool, "_make_gh_client", lambda repo, pr: gh)
    monkeypatch.setattr(
        health_tool,
        "_make_anthropic_client",
        lambda: anthropic or FakeAnthropic(),
    )


def _pr(
    *,
    state_: str = "open",
    merged: bool = False,
    mergeable: bool | None = True,
    mergeable_state: str = "clean",
    head_sha: str = "deadbeef",
    merge_commit_sha: str | None = None,
    merged_at: str | None = None,
    base: str = "main",
) -> dict:
    return {
        "state": state_,
        "merged": merged,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "head_sha": head_sha,
        "merge_commit_sha": merge_commit_sha,
        "merged_at": merged_at,
        "base": base,
    }


# ============================================================================
# error paths
# ============================================================================


def test_inspect_unit_health_missing_pr(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description=""))
    state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
    assert "ERROR" in health_tool.inspect_unit_health("U1")


def test_inspect_unit_health_no_state(tmp_state_db):
    out = health_tool.inspect_unit_health("nope")
    assert "ERROR" in out
    assert "no PR" in out


def test_inspect_unit_health_missing_token(tmp_state_db, no_github_token):
    _seed_unit()
    out = health_tool.inspect_unit_health("U1")
    assert "no GitHub auth" in out


def test_inspect_unit_health_surfaces_probe_errors(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit()

    class BoomGH(FakeGH):
        def get_pr(self, unit_id: str) -> dict | None:
            raise RuntimeError("network melted")

    _install_clients(monkeypatch, BoomGH(pr=_pr()))
    out = health_tool.inspect_unit_health("U1")
    assert "ERROR probing unit health" in out
    assert "network melted" in out


# ============================================================================
# action application — reuses state primitives
# ============================================================================


class TestActionApplication:
    """``inspect_unit_health`` advances units identically to ``reconcile_unit_pr``
    because both route writes through ``state.touch_unit`` /
    ``state.record_event``."""

    def test_merged_in_ci_flips_to_done_and_emits_merged_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(
                state_="closed",
                merged=True,
                merged_at="2026-05-21T11:00:00Z",
                merge_commit_sha="abc123",
            )
        )
        _install_clients(monkeypatch, gh)

        out = health_tool.inspect_unit_health("U1")
        assert "Applied actions" in out

        refreshed = state.get_unit_state("U1")
        assert refreshed.status == "done"
        events = state.list_events("U1")
        types = [e["event_type"] for e in events]
        assert types.count("merged") == 1

    def test_merged_escalated_clears_last_error_and_emits_recovered(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated", last_error="BLOCKED [auth]: 401")
        gh = FakeGH(
            pr=_pr(
                state_="closed",
                merged=True,
                merged_at="2026-05-21T11:00:00Z",
                merge_commit_sha="abc123",
            )
        )
        _install_clients(monkeypatch, gh)

        health_tool.inspect_unit_health("U1")
        refreshed = state.get_unit_state("U1")
        assert refreshed.status == "done"
        assert refreshed.last_error == ""

        events = state.list_events("U1")
        types = [e["event_type"] for e in events]
        assert types.count("merged") == 1
        assert types.count("recovered_from_escalated") == 1
        recovery = next(e for e in events if e["event_type"] == "recovered_from_escalated")
        # Prior last_error preserved in details for audit.
        assert "401" in recovery["details"]

    def test_open_pr_no_transitions(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr(state_="open", merged=False)))

        pre_events = state.list_events("U1")
        health_tool.inspect_unit_health("U1")

        assert state.get_unit_state("U1").status == "in_ci"
        post_events = state.list_events("U1")
        new_types = [
            e["event_type"]
            for e in post_events
            if e not in pre_events and e["event_type"] != health_tool.SNAPSHOT_EVENT_TYPE
        ]
        # No transition events on an open PR.
        assert "merged" not in new_types
        assert "recovered_from_escalated" not in new_types

    def test_ci_drift_event_sets_last_error_without_status_change(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": "u",
                }
            ],
        )
        _install_clients(monkeypatch, gh)

        health_tool.inspect_unit_health("U1")
        refreshed = state.get_unit_state("U1")
        # Status unchanged (only the last_error column is set).
        assert refreshed.status == "in_ci"
        assert "CI drift" in refreshed.last_error
        events = state.list_events("U1")
        assert any(e["event_type"] == "ci_drift_detected" for e in events)


# ============================================================================
# shadow-decision persistence — the new audit channel
# ============================================================================


class TestShadowDecisionRecording:
    """For each ``ShadowDecision`` returned by ``decide_transitions``, the
    canonical tool persists one ``shadow_transition_proposed`` event with
    the full structured payload in ``details`` JSON."""

    def _build_escalated_reset_setup(self) -> FakeGH:
        return FakeGH(
            pr=_pr(),
            check_runs=[
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "u",
                }
            ],
            reviews=[{"state": "APPROVED"}],
        )

    def test_shadow_decisions_are_recorded_as_events(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated")
        _install_clients(monkeypatch, self._build_escalated_reset_setup())

        health_tool.inspect_unit_health("U1")
        shadow_events = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SHADOW_EVENT_TYPE
        ]
        assert len(shadow_events) == 1
        assert "escalated_to_in_ci_reset" in shadow_events[0]["summary"]

    def test_shadow_details_carry_full_structured_payload(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated", review_round=2)
        _install_clients(monkeypatch, self._build_escalated_reset_setup())

        health_tool.inspect_unit_health("U1")
        shadow = next(
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SHADOW_EVENT_TYPE
        )
        details = json.loads(shadow["details"])
        assert details["rule_name"] == "escalated_to_in_ci_reset"
        # predicted_action is the asdict() of the Action dataclass.
        assert details["predicted_action"]["kind"] == "transition"
        assert details["predicted_action"]["target_status"] == "in_ci"
        # trigger_inputs records the predicate inputs.
        assert details["trigger_inputs"]["status"] == "escalated"
        assert details["trigger_inputs"]["approvals"] == 1
        # Rationale is a non-empty string.
        assert isinstance(details["rationale"], str)
        assert details["rationale"]

    def test_shadow_event_metadata_uses_orchestrator_source_and_cycle_number(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated", review_round=3)
        _install_clients(monkeypatch, self._build_escalated_reset_setup())

        health_tool.inspect_unit_health("U1")
        shadow = next(
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SHADOW_EVENT_TYPE
        )
        assert shadow["source"] == "orchestrator"
        assert shadow["cycle_number"] == 3

    def test_dry_run_does_not_record_shadow_events(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="escalated")
        _install_clients(monkeypatch, self._build_escalated_reset_setup())

        out = health_tool.inspect_unit_health("U1", dry_run=True)
        # Digest still lists the shadow decision so the lead sees it.
        assert "escalated_to_in_ci_reset" in out

        shadow_events = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SHADOW_EVENT_TYPE
        ]
        assert shadow_events == []


# ============================================================================
# health_report_snapshot retention — at most one per unit per UTC day
# ============================================================================


class TestHealthReportSnapshot:
    def test_first_probe_today_records_snapshot(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))

        health_tool.inspect_unit_health("U1")
        snapshots = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SNAPSHOT_EVENT_TYPE
        ]
        assert len(snapshots) == 1

    def test_snapshot_details_contain_serialized_health_report(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr(head_sha="abc123")))

        health_tool.inspect_unit_health("U1")
        snapshot = next(
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SNAPSHOT_EVENT_TYPE
        )
        details = json.loads(snapshot["details"])
        # Carries the full HealthReport dataclass shape.
        assert details["unit_id"] == "U1"
        assert details["pr"]["head_sha"] == "abc123"
        assert "ci" in details
        assert "reviews" in details

    def test_second_probe_same_day_does_not_record_extra_snapshot(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))

        health_tool.inspect_unit_health("U1")
        health_tool.inspect_unit_health("U1")
        health_tool.inspect_unit_health("U1")

        snapshots = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SNAPSHOT_EVENT_TYPE
        ]
        assert len(snapshots) == 1

    def test_dry_run_does_not_record_snapshot(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))

        health_tool.inspect_unit_health("U1", dry_run=True)
        snapshots = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SNAPSHOT_EVENT_TYPE
        ]
        assert snapshots == []

    def test_env_var_disables_snapshot_recording(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))
        monkeypatch.setenv(health_tool.SNAPSHOT_RETENTION_ENV, "0")

        health_tool.inspect_unit_health("U1")
        snapshots = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SNAPSHOT_EVENT_TYPE
        ]
        assert snapshots == []

    @pytest.mark.parametrize("disabled_value", ["0", "false", "False", "no", "off"])
    def test_env_var_accepts_common_false_strings(
        self, disabled_value, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))
        monkeypatch.setenv(health_tool.SNAPSHOT_RETENTION_ENV, disabled_value)

        health_tool.inspect_unit_health("U1")
        snapshots = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SNAPSHOT_EVENT_TYPE
        ]
        assert snapshots == [], f"{disabled_value!r} should disable snapshot recording"

    def test_env_var_unset_or_truthy_records_snapshot(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))
        monkeypatch.delenv(health_tool.SNAPSHOT_RETENTION_ENV, raising=False)

        health_tool.inspect_unit_health("U1")
        snapshots = [
            e for e in state.list_events("U1") if e["event_type"] == health_tool.SNAPSHOT_EVENT_TYPE
        ]
        assert len(snapshots) == 1


# ============================================================================
# dry-run semantics — nothing persists
# ============================================================================


class TestDryRun:
    def test_dry_run_does_not_apply_merged_transition(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(state_="closed", merged=True, merged_at="2026-05-21T11:00:00Z"),
        )
        _install_clients(monkeypatch, gh)

        out = health_tool.inspect_unit_health("U1", dry_run=True)
        assert "dry-run" in out
        # Status unchanged — dry-run is observation only.
        assert state.get_unit_state("U1").status == "in_ci"
        types = [e["event_type"] for e in state.list_events("U1")]
        assert "merged" not in types

    def test_dry_run_skips_every_write(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_unit(status="escalated")
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "u",
                }
            ],
            reviews=[{"state": "APPROVED"}],
        )
        _install_clients(monkeypatch, gh)

        pre_state = state.get_unit_state("U1")
        pre_events = state.list_events("U1")
        health_tool.inspect_unit_health("U1", dry_run=True)
        post_state = state.get_unit_state("U1")
        post_events = state.list_events("U1")

        # Only last_activity differs (it doesn't — touch_unit isn't called).
        assert post_state.status == pre_state.status
        assert post_state.last_error == pre_state.last_error
        assert post_events == pre_events


# ============================================================================
# markdown digest contents
# ============================================================================


class TestDigest:
    def test_digest_includes_health_summary_lines(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))

        out = health_tool.inspect_unit_health("U1")
        assert "PR:" in out
        assert "CI:" in out
        assert "Reviews:" in out
        assert "Orchestrator:" in out

    def test_digest_lists_applied_actions_when_present(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(state_="closed", merged=True, merged_at="2026-05-21T11:00:00Z"),
        )
        _install_clients(monkeypatch, gh)

        out = health_tool.inspect_unit_health("U1")
        assert "Applied actions" in out
        assert "transition" in out  # the done transition shows up
        assert "merged" in out

    def test_digest_lists_shadow_decisions(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_unit(status="escalated")
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "u",
                }
            ],
            reviews=[{"state": "APPROVED"}],
        )
        _install_clients(monkeypatch, gh)

        out = health_tool.inspect_unit_health("U1")
        assert "Shadow decisions" in out
        assert "escalated_to_in_ci_reset" in out
        assert "rationale" in out

    def test_digest_dry_run_marker(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr()))

        out = health_tool.inspect_unit_health("U1", dry_run=True)
        assert "dry-run" in out
        # Dry-run digest does not promise applied actions.
        assert "## Applied actions" not in out


# ============================================================================
# MCP registry integration + CLAUDE.md doc
# ============================================================================


def test_inspect_unit_health_is_registered_as_mcp_tool():
    """The canonical tool must appear in the FastMCP registry under the
    spec name — a refactor that drops the ``@mcp.tool()`` decorator
    would leave behavioral tests green while the LLM lost the tool."""
    from orchestrator.tools import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "inspect_unit_health" in names, (
        f"inspect_unit_health missing from MCP tool registry — found: {sorted(names)}"
    )


def test_inspect_unit_health_mcp_signature_advertises_unit_id_and_dry_run():
    from orchestrator.tools import mcp

    tools = asyncio.run(mcp.list_tools())
    inspect = next(t for t in tools if t.name == "inspect_unit_health")
    schema = inspect.inputSchema or {}
    properties = schema.get("properties", {})
    assert "unit_id" in properties
    assert "dry_run" in properties


def _claude_md_text() -> str:
    root = Path(__file__).resolve().parent.parent
    return (root / "CLAUDE.md").read_text(encoding="utf-8")


def test_claude_md_documents_inspect_unit_health_as_canonical():
    text = _claude_md_text()
    assert "inspect_unit_health" in text
    assert "canonical" in text.lower()


def test_claude_md_marks_aliases_deprecated():
    text = _claude_md_text()
    # Both aliases must show up flagged as deprecated.
    assert "check_unit_pr" in text
    assert "reconcile_unit_pr" in text
    lowered = text.lower()
    assert "deprecated" in lowered
