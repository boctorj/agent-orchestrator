"""F-009-U-2: split check_unit_pr (read-only) and reconcile_unit_pr (state-advancing).

This test module pins the *contract* promises from the unit description that
aren't directly asserted by the existing per-tool tests in
``tests/test_tools_ops.py``:

  * `check_unit_pr` return shape is unchanged across the refactor — same set
    of top-level JSON keys.
  * `check_unit_pr` is genuinely read-only — every status path leaves
    `work_units.last_activity` and the events log untouched (not just the
    happy-path merged/unmerged pair).
  * `reconcile_unit_pr` actually *delegates* to `check_unit_pr` rather than
    re-implementing the poll inline (single source of truth for the GitHub
    read).
  * `reconcile_unit_pr`'s emitted `merged` and `recovered_from_escalated`
    events carry the correct payload (source, cycle_number, summary,
    details). The unit description's recovered_from_escalated event must
    preserve the prior `last_error` in `details`.
  * `reconcile_unit_pr` on the not-yet-mapped statuses (`pending`,
    `approved_awaiting_merge`) refuses with a `reconcile_refused` event
    rather than silently flipping to done. The active-status refusal is
    already covered; this exercises the *fallthrough* branch.
  * Documentation (CLAUDE.md, check_unit_pr docstring) reflects the new
    split — the unit description explicitly calls this out.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import ops

# --------------------------- helpers ---------------------------


def _seed(unit_id="U1", feature_id="F", status="in_ci", pr_number=5, **kwargs):
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
            **kwargs,
        )
    )


def _stub_merged(monkeypatch, merged_at="2026-05-18T22:00:00Z", head_sha="deadbeef"):
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {
            "state": "closed",
            "merged": True,
            "merged_at": merged_at,
            "head_sha": head_sha,
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 1, "conclusion_counts": {"success": 1}, "runs": []},
    )


def _stub_open(monkeypatch, head_sha="cafef00d"):
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": head_sha},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )


# --------------------------- return shape pinning ---------------------------


def test_check_unit_pr_return_shape_is_stable_unmerged(
    tmp_state_db, with_github_token, monkeypatch
):
    """Description: 'Return shape unchanged: pr_state, checks, head_sha,
    orchestrator_status.' Pin every top-level key so a future refactor
    can't silently drop one."""
    _seed(status="in_ci", pr_number=42)
    _stub_open(monkeypatch, head_sha="cafef00d")

    parsed = json.loads(ops.check_unit_pr("U1"))
    # head_sha lives inside pr_state (matches the pre-refactor shape).
    assert set(parsed.keys()) == {
        "unit_id",
        "pr_number",
        "pr_state",
        "checks",
        "orchestrator_status",
    }
    assert parsed["unit_id"] == "U1"
    assert parsed["pr_number"] == 42
    assert parsed["pr_state"]["head_sha"] == "cafef00d"
    assert parsed["orchestrator_status"] == "in_ci"
    assert "total" in parsed["checks"]  # CI checks block surfaced


def test_check_unit_pr_return_shape_is_stable_merged(tmp_state_db, with_github_token, monkeypatch):
    """Same key set whether or not the PR is merged — the read shape is
    a stable contract for dashboards/diagnostics."""
    _seed(status="in_ci", pr_number=42)
    _stub_merged(monkeypatch)

    parsed = json.loads(ops.check_unit_pr("U1"))
    assert set(parsed.keys()) == {
        "unit_id",
        "pr_number",
        "pr_state",
        "checks",
        "orchestrator_status",
    }
    assert parsed["pr_state"]["merged"] is True
    # Read-only: orchestrator_status still reports the pre-merge state.
    assert parsed["orchestrator_status"] == "in_ci"


# --------------------------- read-only-across-every-status ---------------------------


def test_check_unit_pr_does_not_touch_last_activity_on_unmerged(
    tmp_state_db, with_github_token, monkeypatch
):
    """Read-only means no DB writes — including the last_activity bump
    that touch_unit would do. Verifies the timestamp hasn't moved."""
    _seed(status="in_ci")
    _stub_open(monkeypatch)
    pre_activity = state.get_unit_state("U1").last_activity

    ops.check_unit_pr("U1")
    ops.check_unit_pr("U1")
    ops.check_unit_pr("U1")

    post_activity = state.get_unit_state("U1").last_activity
    assert post_activity == pre_activity


def test_check_unit_pr_does_not_touch_last_activity_on_merged(
    tmp_state_db, with_github_token, monkeypatch
):
    """Same invariant for the merged path — observing a merged PR must
    not leave a fingerprint on the unit row."""
    _seed(status="in_ci")
    _stub_merged(monkeypatch)
    pre_activity = state.get_unit_state("U1").last_activity

    ops.check_unit_pr("U1")
    post_activity = state.get_unit_state("U1").last_activity
    assert post_activity == pre_activity


def test_check_unit_pr_records_zero_events_across_every_pr_state(
    tmp_state_db, with_github_token, monkeypatch
):
    """No event types — `merged`, `reconcile_refused`, anything — may be
    emitted by check_unit_pr. Cycle through open / closed-unmerged /
    merged to prove the read path is uniformly silent."""
    _seed(status="in_ci")

    pr_stub_chain = [
        lambda u, p: {"state": "open", "merged": False, "head_sha": "a"},
        lambda u, p: {"state": "closed", "merged": False, "head_sha": "b"},
        lambda u, p: {"state": "closed", "merged": True, "merged_at": "x", "head_sha": "c"},
    ]
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda u, p: {"total": 0, "conclusion_counts": {}, "runs": []},
    )
    for stub in pr_stub_chain:
        monkeypatch.setattr("orchestrator.tools.ops.github.get_pr_state", stub)
        ops.check_unit_pr("U1")

    assert state.list_events("U1") == []


# --------------------------- delegation contract ---------------------------


def test_reconcile_delegates_to_check_unit_pr(tmp_state_db, with_github_token, monkeypatch):
    """reconcile_unit_pr's docstring promises it 'reads via check_unit_pr'.
    Patch check_unit_pr itself and assert reconcile honours the indirection
    — otherwise a refactor that re-inlines the poll would silently drift
    the two readers apart."""
    _seed(status="in_ci", pr_number=7)

    called: dict = {"count": 0, "unit_id": None}

    poll_payload = {
        "unit_id": "U1",
        "pr_number": 7,
        "pr_state": {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-18T22:00:00Z",
            "head_sha": "deadbeef",
        },
        "checks": {"total": 0, "conclusion_counts": {}, "runs": []},
        "orchestrator_status": "in_ci",
    }

    def fake_check(uid):
        called["count"] += 1
        called["unit_id"] = uid
        return json.dumps(poll_payload, indent=2)

    monkeypatch.setattr("orchestrator.tools.ops.check_unit_pr", fake_check)

    out = ops.reconcile_unit_pr("U1")
    parsed = json.loads(out)

    assert called["count"] == 1
    assert called["unit_id"] == "U1"
    # And the read payload's fields flowed through to the reconcile response.
    assert parsed["pr_number"] == 7
    assert parsed["pr_state"]["head_sha"] == "deadbeef"
    assert parsed["reconciled"] is True
    assert parsed["action"] == "merged-from-in_ci"


# --------------------------- event-payload pinning ---------------------------


def test_reconcile_merged_event_payload_is_complete(tmp_state_db, with_github_token, monkeypatch):
    """The emitted `merged` event must carry source='human', the unit's
    review_round as cycle_number, and a summary referencing the PR number
    plus merged_at timestamp. Dashboards depend on this for the
    'Recent merges' surface."""
    _seed(status="in_ci", pr_number=42, review_round=2)
    _stub_merged(monkeypatch, merged_at="2026-05-18T22:00:00Z")

    ops.reconcile_unit_pr("U1")
    events = state.list_events("U1")
    merged = [e for e in events if e["event_type"] == "merged"]
    assert len(merged) == 1
    ev = merged[0]
    assert ev["source"] == "human"
    assert ev["cycle_number"] == 2
    assert "#42" in ev["summary"]
    assert "2026-05-18T22:00:00Z" in ev["summary"]


def test_reconcile_recovered_event_preserves_prior_last_error(
    tmp_state_db, with_github_token, monkeypatch
):
    """recovered_from_escalated must stash the prior last_error verbatim
    in `details` — that's the audit trail explaining why the unit was
    in escalated. Without this, merging an escalated unit erases the
    forensic record."""
    _seed(status="escalated", pr_number=42, review_round=3)
    state.touch_unit(
        "U1",
        error="BLOCKED [auth_failure]: 401 from github api during push",
    )
    _stub_merged(monkeypatch)

    ops.reconcile_unit_pr("U1")
    events = state.list_events("U1")
    recovery = [e for e in events if e["event_type"] == "recovered_from_escalated"]
    assert len(recovery) == 1
    ev = recovery[0]
    assert "401 from github api during push" in ev["details"]
    assert "auth_failure" in ev["details"]
    assert ev["cycle_number"] == 3
    # last_error wiped on the row itself.
    assert state.get_unit_state("U1").last_error == ""


def test_reconcile_in_ci_does_not_emit_recovered_event(
    tmp_state_db, with_github_token, monkeypatch
):
    """`recovered_from_escalated` is *exclusive* to the escalated→done
    branch — the normal in_ci→done merge must not emit it. Pinning this
    so a future regression can't blur the two transitions together."""
    _seed(status="in_ci", pr_number=42)
    _stub_merged(monkeypatch)

    ops.reconcile_unit_pr("U1")
    types = [e["event_type"] for e in state.list_events("U1")]
    assert "merged" in types
    assert "recovered_from_escalated" not in types


# --------------------------- "pending" / unknown fallthrough ---------------------------


def test_reconcile_pending_plus_merged_refuses(tmp_state_db, with_github_token, monkeypatch):
    """`pending` isn't an active role-status but it's also not in_ci/
    escalated/done. The reconciler must not silently flip a pending unit
    to done just because its PR happens to be merged (would mask a serious
    state-machine bug elsewhere). Refuse with reconcile_refused."""
    _seed(status="pending", pr_number=5)
    _stub_merged(monkeypatch)

    out = ops.reconcile_unit_pr("U1")
    parsed = json.loads(out)
    assert parsed["action"] == "refused-from-pending"
    # Refusal is not a state transition; reconciled mirrors the no-op branches.
    assert parsed["reconciled"] is False
    # Status unchanged.
    assert state.get_unit_state("U1").status == "pending"
    event_types = [e["event_type"] for e in state.list_events("U1")]
    assert event_types.count("reconcile_refused") == 1
    assert "merged" not in event_types


# --------------------------- documentation contract ---------------------------


def test_check_unit_pr_docstring_advertises_read_only(tmp_state_db):
    """Unit description explicitly says 'Update CLAUDE.md: check_unit_pr
    is read-only; reconcile_unit_pr is the state-advancing call.' Pin the
    docstring promise here so a copy-edit can't silently weaken it."""
    doc = (ops.check_unit_pr.__doc__ or "").lower()
    # F-009-U-2 contract: read-only + cross-reference to reconcile_unit_pr.
    assert "read-only" in doc or "read only" in doc
    assert "reconcile_unit_pr" in doc


def test_reconcile_unit_pr_docstring_describes_transitions(tmp_state_db):
    """reconcile's docstring must cover the four documented (PR-state,
    status) branches — proves the policy is captured next to the code."""
    doc = ops.reconcile_unit_pr.__doc__ or ""
    assert "in_ci" in doc
    assert "escalated" in doc
    assert "merged" in doc
    # No-op branches surfaced in the docstring so the lead can read the
    # policy from MCP-tool inspection alone.
    assert "no-op" in doc.lower() or "stays out" in doc.lower()


def test_claude_md_documents_split(tmp_state_db):
    """The repo's runtime persona must mention BOTH tools and identify
    reconcile_unit_pr as the state-advancing one. Future doc drift on
    this becomes a test failure rather than silent confusion."""
    claude_md = Path(__file__).parent.parent / "CLAUDE.md"
    assert claude_md.exists(), "CLAUDE.md missing from repo root"
    text = claude_md.read_text()
    assert "check_unit_pr" in text
    assert "reconcile_unit_pr" in text
    # check_unit_pr advertised as read-only.
    assert "read-only" in text.lower() or "read only" in text.lower()
