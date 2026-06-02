"""Tester-added tests for F-014-U-2: ``inspect_unit_health`` MCP tool.

Complements ``tests/test_tools_health.py`` (the coder's own test file) by
covering the contracts the spec calls out that aren't already exercised
there:

* ``merged + approved_awaiting_merge`` → ``done`` + ``merged`` event
  (the F-009-U-4 cell — spec calls out that the canonical tool must
  cover the same reconcile cells as ``reconcile_unit_pr``).
* ``merged + active-role`` status (``coding``/``fixing``/etc.) emits a
  ``reconcile_refused`` event and does NOT advance status — matches
  ``ops._RECONCILE_REFUSED_STATUSES`` policy.
* ``write_cycle_log`` side effect fires on merged polls when
  ``merge_commit_sha`` is populated (spec constraint: "preserving the
  cycle-log writer side effect on merged polls"). And does *not* fire
  when the SHA is still null (race-tolerant).
* ``pr_conflict_detected`` event emitted when ``mergeable_state`` is
  ``dirty``/``conflicting`` — new event signal added by F-014-U-2.
* ``required_check_missing`` event emitted when a required check is
  absent — new event signal added by F-014-U-2.
* Deprecation warnings on ``check_unit_pr`` and ``reconcile_unit_pr``
  ("Log both aliases as deprecated" — spec wording).
* ``inspect_unit_health`` is idempotent on an already-``done`` unit
  (the "second poll" race case).
* MCP signature: ``dry_run`` has a default value (``False``) so the
  tool can be invoked with just a ``unit_id`` from chat.
* CLAUDE.md updates: scheduling rule + restart recovery sections
  mention ``inspect_unit_health`` while preserving existing
  ``reconcile_unit_pr`` references (spec wording).
* Behavioral parity: ``reconcile_unit_pr`` and ``inspect_unit_health``,
  observing the same merged PR, both advance the unit to ``done`` and
  emit a single ``merged`` event each.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import health as health_tool
from orchestrator.tools import ops

# --------------------------- fake clients ---------------------------


@dataclass
class FakeGH:
    """Minimal :class:`GitHubHealthClient` for these tests.

    Mirrors the shape used in ``tests/test_tools_health.py`` so the
    fixtures are interchangeable — kept local to avoid cross-test-module
    import coupling.
    """

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
    conflict_files: list[str] | None = None,
) -> dict:
    payload: dict = {
        "state": state_,
        "merged": merged,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "head_sha": head_sha,
        "merge_commit_sha": merge_commit_sha,
        "merged_at": merged_at,
        "base": base,
    }
    if conflict_files is not None:
        payload["conflict_files"] = list(conflict_files)
    return payload


# ============================================================================
# Reconcile-cell coverage: merged + approved_awaiting_merge (F-009-U-4)
# ============================================================================


class TestApprovedAwaitingMergeCell:
    """``merged + approved_awaiting_merge`` is one of the three reconcile
    cells the canonical tool must cover (F-009-U-4 added the status; the
    spec for U-2 requires ``inspect_unit_health`` to apply the *same*
    merged-transitions ``reconcile_unit_pr`` does)."""

    def test_merged_approved_awaiting_merge_flips_to_done(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="approved_awaiting_merge")
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
        # Not the escalated path — no recovered_from_escalated event.
        assert "recovered_from_escalated" not in types

    def test_merged_approved_awaiting_merge_emits_merged_event_with_source_human(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="approved_awaiting_merge")
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
        merged_evt = next(
            e for e in state.list_events("U1") if e["event_type"] == "merged"
        )
        # ``reconcile_unit_pr`` records ``source='human'`` because the
        # merge click is a human action; the canonical tool must agree
        # so dashboards filtering on source=human keep working.
        assert merged_evt["source"] == "human"


# ============================================================================
# Reconcile-cell coverage: merged + active-role status → reconcile_refused
# ============================================================================


class TestMergeFromActiveRoleRefused:
    """When a PR is merged while a worker role is mid-flight (coding /
    fixing / testing / reviewing / opening_pr), the policy is "refuse to
    advance" — emit ``reconcile_refused`` and leave the unit row
    unchanged. The canonical tool must match ``reconcile_unit_pr``'s
    policy because both go through the same shared health-action table."""

    @pytest.mark.parametrize(
        "active_status", ["coding", "fixing", "testing", "reviewing", "opening_pr"]
    )
    def test_active_status_merged_emits_reconcile_refused(
        self, active_status, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status=active_status)
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

        # Status unchanged.
        assert state.get_unit_state("U1").status == active_status
        # reconcile_refused emitted, merged NOT emitted (no transition).
        types = [e["event_type"] for e in state.list_events("U1")]
        assert "reconcile_refused" in types
        assert "merged" not in types


# ============================================================================
# Cycle-log writer side effect (spec: "preserving the cycle-log writer
# side effect on merged polls")
# ============================================================================


class TestCycleLogWriterSideEffect:
    """The spec for ``reconcile_unit_pr`` (preserved as alias in U-2)
    says the cycle-log writer must fire on every merged poll with a
    populated ``merge_commit_sha``. The canonical tool must preserve
    this side effect so the two paths stay behavior-identical."""

    def test_merged_with_sha_invokes_write_cycle_log(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(
                state_="closed",
                merged=True,
                merged_at="2026-05-21T11:00:00Z",
                merge_commit_sha="merged-sha-123",
            )
        )
        _install_clients(monkeypatch, gh)

        with patch.object(
            health_tool.cycle_log, "write_cycle_log", autospec=True
        ) as mock_write:
            health_tool.inspect_unit_health("U1")

        assert mock_write.called, "write_cycle_log should fire on merged poll with SHA"
        # Validate the call's structured args — unit_id + merge_commit_sha
        # must propagate so the cycle log records the correct commit on main.
        call_args = mock_write.call_args
        assert call_args.args[0] == "U1"
        assert call_args.kwargs.get("merge_commit_sha") == "merged-sha-123"

    def test_merged_without_sha_skips_write_cycle_log(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """SHA is asynchronously populated by GitHub — the first poll
        after a merge may return ``merged=True`` with ``merge_commit_sha=None``.
        The writer must skip rather than crash; a later poll catches up
        once the SHA arrives."""
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(
                state_="closed",
                merged=True,
                merged_at="2026-05-21T11:00:00Z",
                merge_commit_sha=None,
            )
        )
        _install_clients(monkeypatch, gh)

        with patch.object(
            health_tool.cycle_log, "write_cycle_log", autospec=True
        ) as mock_write:
            health_tool.inspect_unit_health("U1")

        assert not mock_write.called

    def test_open_pr_does_not_invoke_write_cycle_log(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(pr=_pr(state_="open", merged=False))
        _install_clients(monkeypatch, gh)

        with patch.object(
            health_tool.cycle_log, "write_cycle_log", autospec=True
        ) as mock_write:
            health_tool.inspect_unit_health("U1")

        assert not mock_write.called

    def test_write_cycle_log_error_does_not_break_response(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Cycle-log writer failures are best-effort — a missing ``gh``
        binary or a non-repo workdir must not abort the inspect_unit_health
        response the lead is waiting on."""
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(
                state_="closed",
                merged=True,
                merged_at="2026-05-21T11:00:00Z",
                merge_commit_sha="merged-sha-123",
            )
        )
        _install_clients(monkeypatch, gh)

        def _boom(*a, **kw):
            raise RuntimeError("git missing")

        monkeypatch.setattr(health_tool.cycle_log, "write_cycle_log", _boom)

        # Must complete normally despite the writer raising — the
        # caller sees a digest, not an exception.
        out = health_tool.inspect_unit_health("U1")
        assert "Applied actions" in out
        assert state.get_unit_state("U1").status == "done"


# ============================================================================
# pr_conflict_detected event
# ============================================================================


class TestPRConflictDetected:
    """New event signal in U-2 — applied to events bucket (not shadow)."""

    @pytest.mark.parametrize("conflict_state", ["dirty", "conflicting"])
    def test_dirty_or_conflicting_mergeable_state_emits_event(
        self, conflict_state, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(
                state_="open",
                merged=False,
                mergeable=False,
                mergeable_state=conflict_state,
            )
        )
        _install_clients(monkeypatch, gh)

        health_tool.inspect_unit_health("U1")
        types = [e["event_type"] for e in state.list_events("U1")]
        assert "pr_conflict_detected" in types

    def test_clean_mergeable_state_does_not_emit_conflict_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=_pr(mergeable_state="clean")))

        health_tool.inspect_unit_health("U1")
        types = [e["event_type"] for e in state.list_events("U1")]
        assert "pr_conflict_detected" not in types

    def test_merged_pr_does_not_emit_conflict_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """A merged PR cannot meaningfully be 'in conflict' anymore —
        the event is silent post-merge so the digest reads as a clean
        merge."""
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(
                state_="closed",
                merged=True,
                merged_at="2026-05-21T11:00:00Z",
                mergeable_state="dirty",
                merge_commit_sha="abc123",
            )
        )
        _install_clients(monkeypatch, gh)

        health_tool.inspect_unit_health("U1")
        types = [e["event_type"] for e in state.list_events("U1")]
        assert "pr_conflict_detected" not in types


# ============================================================================
# required_check_missing event
# ============================================================================


class TestRequiredCheckMissing:
    """New event signal in U-2 for branch-protection drift."""

    def test_required_check_absent_emits_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                {
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "u",
                }
            ],
            required_checks=["lint", "tests-required"],
        )
        _install_clients(monkeypatch, gh)

        health_tool.inspect_unit_health("U1")
        events = state.list_events("U1")
        types = [e["event_type"] for e in events]
        assert "required_check_missing" in types
        # Details should mention the actually-missing check name so the
        # digest gives the lead something to act on.
        missing_evt = next(e for e in events if e["event_type"] == "required_check_missing")
        assert "tests-required" in (missing_evt["details"] or "")

    def test_all_required_present_no_event(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        gh = FakeGH(
            pr=_pr(),
            check_runs=[
                {
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "u",
                }
            ],
            required_checks=["lint"],
        )
        _install_clients(monkeypatch, gh)

        health_tool.inspect_unit_health("U1")
        types = [e["event_type"] for e in state.list_events("U1")]
        assert "required_check_missing" not in types


# ============================================================================
# Deprecation warnings on aliases
# ============================================================================


class TestAliasDeprecationWarnings:
    """Spec: "Log both aliases as deprecated."

    Both ``check_unit_pr`` and ``reconcile_unit_pr`` must emit a
    ``DeprecationWarning`` so downstream callers can migrate."""

    def test_check_unit_pr_emits_deprecation_warning(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        # Stub the real GitHub helpers so check_unit_pr returns without I/O.
        monkeypatch.setattr(
            ops.github, "get_pr_state", lambda *a, **k: _pr(state_="open")
        )
        monkeypatch.setattr(
            ops.github, "get_pr_check_runs", lambda *a, **k: {"runs": []}
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ops.check_unit_pr("U1")

        deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecation, "check_unit_pr must emit a DeprecationWarning"
        assert any("inspect_unit_health" in str(w.message) for w in deprecation), (
            "deprecation warning should point users at inspect_unit_health"
        )

    def test_reconcile_unit_pr_emits_deprecation_warning(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        monkeypatch.setattr(
            ops.github, "get_pr_state", lambda *a, **k: _pr(state_="open")
        )
        monkeypatch.setattr(
            ops.github, "get_pr_check_runs", lambda *a, **k: {"runs": []}
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ops.reconcile_unit_pr("U1")

        deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecation, "reconcile_unit_pr must emit a DeprecationWarning"
        assert any("inspect_unit_health" in str(w.message) for w in deprecation), (
            "deprecation warning should point users at inspect_unit_health"
        )


# ============================================================================
# Idempotency
# ============================================================================


class TestIdempotency:
    """A second ``inspect_unit_health`` call after the unit has already
    flipped to ``done`` must not re-emit the ``merged`` event — matches
    ``reconcile_unit_pr``'s ``no-op-already-done`` guard."""

    def test_second_call_does_not_re_emit_merged_event(
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

        health_tool.inspect_unit_health("U1")  # poll 1: flip to done
        health_tool.inspect_unit_health("U1")  # poll 2: no-op
        health_tool.inspect_unit_health("U1")  # poll 3: no-op

        types = [e["event_type"] for e in state.list_events("U1")]
        # Exactly one merged event across three polls.
        assert types.count("merged") == 1
        # Status sticky at done.
        assert state.get_unit_state("U1").status == "done"


# ============================================================================
# MCP signature: dry_run default
# ============================================================================


def test_inspect_unit_health_mcp_dry_run_default_is_false():
    """Lead invokes ``inspect_unit_health(unit_id)`` without ``dry_run``;
    the MCP schema must default it to ``False`` so the no-arg call
    applies actions."""
    from orchestrator.tools import mcp

    tools = asyncio.run(mcp.list_tools())
    inspect = next(t for t in tools if t.name == "inspect_unit_health")
    schema = inspect.inputSchema or {}
    properties = schema.get("properties", {})
    dry_run_prop = properties.get("dry_run", {})
    # FastMCP renders Python defaults as JSON-Schema ``default`` keys.
    assert dry_run_prop.get("default") is False
    # And dry_run must NOT be in the required list (the lead must be
    # able to call ``inspect_unit_health(unit_id)`` with no second arg).
    required = schema.get("required", [])
    assert "dry_run" not in required
    assert "unit_id" in required


# ============================================================================
# CLAUDE.md updates per spec
# ============================================================================


def _claude_md_text() -> str:
    return (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text(
        encoding="utf-8"
    )


def test_claude_md_scheduling_rule_mentions_inspect_unit_health():
    """Spec wording: "Update the scheduling rule and restart-recovery
    flow sections to recommend ``inspect_unit_health`` in new flows
    while preserving the existing ``reconcile_unit_pr`` references." """
    text = _claude_md_text()
    # The scheduling-rule section is the heading we anchor on.
    sched_marker = "### Scheduling rule"
    assert sched_marker in text
    # Both names must appear after that heading (preserved + recommended).
    after_sched = text[text.index(sched_marker):]
    # Cut at the next H3 so we look only at the scheduling section.
    next_h3 = after_sched.find("\n### ", 1)
    section = after_sched if next_h3 == -1 else after_sched[:next_h3]
    assert "inspect_unit_health" in section, (
        "scheduling rule must recommend inspect_unit_health"
    )
    assert "reconcile_unit_pr" in section, (
        "scheduling rule must preserve the existing reconcile_unit_pr reference"
    )


def test_claude_md_restart_recovery_mentions_inspect_unit_health():
    text = _claude_md_text()
    restart_marker = "### Restart recovery flow"
    assert restart_marker in text
    after = text[text.index(restart_marker):]
    next_h3 = after.find("\n### ", 1)
    section = after if next_h3 == -1 else after[:next_h3]
    assert "inspect_unit_health" in section, (
        "restart-recovery flow must recommend inspect_unit_health"
    )
    assert "reconcile_unit_pr" in section, (
        "restart-recovery flow must preserve the existing reconcile_unit_pr reference"
    )


def test_claude_md_snapshot_env_var_is_documented():
    """The spec note: "tunable via env var" — the env var name should
    appear in the tool description so the lead can tell the user how to
    disable snapshot retention."""
    text = _claude_md_text()
    assert health_tool.SNAPSHOT_RETENTION_ENV in text


# ============================================================================
# Behavioral parity between canonical tool and the alias
# ============================================================================


class TestAliasBehavioralParity:
    """The spec constraint: "Reuse, don't duplicate state writes.
    ``actions_to_apply`` must run through the existing ``state.touch_unit``
    / event-append paths so the alias-vs-canonical behavior cannot
    diverge."

    Calling either ``reconcile_unit_pr(U)`` or
    ``inspect_unit_health(U)`` on the same merged-PR fixture must
    advance the unit to ``done`` and emit exactly one ``merged`` event
    via ``source='human'``."""

    def _merged_pr(self) -> dict:
        return _pr(
            state_="closed",
            merged=True,
            merged_at="2026-05-21T11:00:00Z",
            merge_commit_sha="merged-sha-xyz",
        )

    def test_inspect_unit_health_path(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit(status="in_ci")
        _install_clients(monkeypatch, FakeGH(pr=self._merged_pr()))

        health_tool.inspect_unit_health("U1")
        assert state.get_unit_state("U1").status == "done"
        merged = [e for e in state.list_events("U1") if e["event_type"] == "merged"]
        assert len(merged) == 1
        assert merged[0]["source"] == "human"

    def test_reconcile_unit_pr_path(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Same fixture, alias path. Must end in the same observable
        state — ``done`` + one ``merged`` event with ``source='human'``."""
        _seed_unit(status="in_ci")

        merged = self._merged_pr()
        monkeypatch.setattr(ops.github, "get_pr_state", lambda *a, **k: merged)
        monkeypatch.setattr(
            ops.github, "get_pr_check_runs", lambda *a, **k: {"runs": []}
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ops.reconcile_unit_pr("U1")

        assert state.get_unit_state("U1").status == "done"
        merged_events = [
            e for e in state.list_events("U1") if e["event_type"] == "merged"
        ]
        assert len(merged_events) == 1
        assert merged_events[0]["source"] == "human"
