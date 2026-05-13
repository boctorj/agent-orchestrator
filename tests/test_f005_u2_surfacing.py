"""End-to-end tests for F-005-U-2: structured BLOCKED reasons surface
through the four channels (escalation summary, ntfy push, dashboard,
chat filters) as a coherent whole.

The individual modules (``blocked_hints``, ``dashboard``, ``ntfy``,
``tools.observability``, ``tools.ops``) each have their own focused
test files. This file checks the *contract from the unit description*:

* When an event carries a structured reason, escalation summaries render
  as ``reason -> remediation hint -> raw prose`` (in that order).
* ``branch_protection_blocked_push`` exposes the three F-005 fix-it
  options (scope to main / add bypass actor / re-issue bypass-capable
  PAT) at every surface that consumes the hint.
* The slug + hint reach the ntfy push body so the user can action from
  phone.
* The dashboard's "Last error" column stops truncating when a reason is
  known, but keeps the legacy 120-char cap for ``reason="unknown"``.
* ``unit_history`` and ``list_in_flight`` both accept a ``reason=``
  filter so the lead can query "show me everything blocked on auth".
* ``reason="unknown"`` collapses every surface back to today's prose-only
  rendering so nothing regresses for un-classifiable failures.
"""

from __future__ import annotations

import json

from orchestrator import blocked_hints, dashboard, ntfy, state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import observability, ops

BRANCH_PROTECTION_SLUG = "branch_protection_blocked_push"


def _seed_escalated(unit_id: str, feature_id: str, reason_slug: str, prose: str) -> None:
    """Helper: persist a feature + escalated unit + structured event."""
    if not state.get_feature(feature_id):
        state.save_feature(Feature(id=feature_id, title="t", description=""))
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="escalated",
            last_error="seed",
            pr_number=42,
        )
    )
    state.record_event(
        unit_id,
        feature_id,
        "coder_blocked",
        details=json.dumps({"reason": reason_slug, "prose": prose}),
    )


# --------------------------- 1. branch-protection slug + three options ---------------------------


class TestBranchProtectionThreeOptions:
    """The unit description names three specific fix-it options for the
    ``branch_protection_blocked_push`` reason. All three must be present
    in the hint (and therefore at every surfacing channel)."""

    def test_slug_exists_in_taxonomy(self):
        # The exact slug F-005-U-1 produces — sibling-unit contract.
        assert BRANCH_PROTECTION_SLUG in blocked_hints.REMEDIATION_HINTS

    def test_hint_mentions_scope_to_main_option(self):
        hint = blocked_hints.remediation_hint(BRANCH_PROTECTION_SLUG)
        # Option 1: scope the protection rule to `main` only.
        assert "main" in hint, "fix-it option 'scope to main only' missing from hint"

    def test_hint_mentions_bypass_actor_option(self):
        hint = blocked_hints.remediation_hint(BRANCH_PROTECTION_SLUG)
        # Option 2: add the orchestrator identity as a bypass actor.
        assert "bypass actor" in hint, "fix-it option 'add bypass actor' missing from hint"

    def test_hint_mentions_bypass_capable_pat_option(self):
        hint = blocked_hints.remediation_hint(BRANCH_PROTECTION_SLUG)
        # Option 3: re-issue a bypass-capable PAT.
        assert "PAT" in hint, "fix-it option 'bypass-capable PAT' missing from hint"

    def test_hint_is_numbered_list_with_three_entries(self):
        """The three options need to be discoverable as distinct list items,
        not just incidentally present substrings. Numbered list ordering
        protects against accidental concatenation/merge."""
        hint = blocked_hints.remediation_hint(BRANCH_PROTECTION_SLUG)
        assert "1." in hint
        assert "2." in hint
        assert "3." in hint
        # Make sure there isn't a stray fourth option (the spec is exactly three).
        assert "4." not in hint


# --------------------------- 2. escalation summary ordering ---------------------------


class TestEscalationSummaryOrdering:
    """Spec literally says ``reason -> remediation hint -> raw prose``.
    The three blocks must appear in that order, not some other permutation."""

    def test_reason_appears_before_hint_appears_before_prose(self):
        prose = "PROSE_MARKER_TAIL"
        rendered = blocked_hints.format_escalation_summary(BRANCH_PROTECTION_SLUG, prose)
        reason_idx = rendered.find(f"Reason: {BRANCH_PROTECTION_SLUG}")
        # Use one of the hint markers — "Branch protection rejected" starts the hint.
        hint_idx = rendered.find("Branch protection rejected")
        prose_idx = rendered.find(prose)
        assert reason_idx != -1, "reason header missing"
        assert hint_idx != -1, "hint body missing"
        assert prose_idx != -1, "prose tail missing"
        assert reason_idx < hint_idx < prose_idx, (
            f"order should be reason({reason_idx}) -> hint({hint_idx}) -> "
            f"prose({prose_idx}), got: {rendered!r}"
        )

    def test_unknown_reason_emits_only_prose(self):
        """Regression guard: unclassifiable failures keep today's behavior."""
        prose = "free-form failure tail"
        rendered = blocked_hints.format_escalation_summary("unknown", prose)
        assert rendered == prose
        assert "Reason:" not in rendered

    def test_offtaxonomy_slug_collapses_to_prose_only(self):
        """Unknown-to-the-taxonomy slug behaves like 'unknown' — we don't
        fabricate guidance for slugs the hint table doesn't recognize."""
        prose = "something weird happened"
        rendered = blocked_hints.format_escalation_summary("totally_made_up_slug", prose)
        assert rendered == prose


# --------------------------- 3. ntfy push body carries slug + hint ---------------------------


class TestNtfyPushBodySurfacesHint:
    """Spec: 'Include reason + hint in the ntfy push body so the user can
    action from phone.' Verified against the actual push_escalation
    callable (not just format_ntfy_body in isolation)."""

    def _patch_httpx(self, monkeypatch):
        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                pass

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def post(self, url, content=None, headers=None):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = dict(headers or {})
                return FakeResponse()

        monkeypatch.setattr("orchestrator.ntfy.httpx.Client", FakeClient)
        return captured

    def test_known_reason_body_carries_slug_and_three_options(self, monkeypatch, with_ntfy_topic):
        captured = self._patch_httpx(monkeypatch)
        ok = ntfy.push_escalation(
            "F-005-U-2",
            "push rejected by branch protection",
            pr_url="https://github.com/o/r/pull/9",
            reason_slug=BRANCH_PROTECTION_SLUG,
        )
        assert ok is True
        body = captured["content"]
        assert isinstance(body, str)
        # Slug
        assert BRANCH_PROTECTION_SLUG in body
        # All three fix-it options reachable from the phone push.
        assert "main" in body
        assert "bypass actor" in body
        assert "PAT" in body
        # Prose tail still present.
        assert "push rejected by branch protection" in body
        # PR URL also wired up as click target.
        assert captured["headers"]["Click"] == "https://github.com/o/r/pull/9"

    def test_default_reason_slug_is_unknown_regression(self, monkeypatch, with_ntfy_topic):
        """Default behavior (caller omits ``reason_slug``) must match
        today's single-line body — no regression for callers that haven't
        been updated to pass the slug."""
        captured = self._patch_httpx(monkeypatch)
        ntfy.push_escalation("U-1", "thing broke")
        body = captured["content"]
        assert "Reason:" not in body
        assert "Unit U-1 escalated: thing broke" in body


# --------------------------- 4. dashboard renderer ---------------------------


class TestDashboardSurfacing:
    """Spec: 'Stop truncating the remediation tail in the dashboard's
    Last error column for events with a known reason.'"""

    def test_full_remediation_visible_in_escalated_data(self, tmp_state_db):
        long_prose = "remote: error: GH013: " + "y" * 500
        _seed_escalated("U-bp", "F", BRANCH_PROTECTION_SLUG, long_prose)

        rows = dashboard._escalated_data()
        assert rows, "expected one escalated row"
        row = next(r for r in rows if r["unit_id"] == "U-bp")
        assert row["reason"] == BRANCH_PROTECTION_SLUG
        # No 120-char cap when the reason is known.
        assert len(row["last_error"]) > 120
        # Full prose tail preserved.
        assert long_prose in row["last_error"]
        # Hint surfaced too.
        assert "bypass actor" in row["last_error"]

    def test_unknown_reason_dashboard_keeps_120_char_truncation(self, tmp_state_db):
        long_error = "z" * 500
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U-leg", feature_id="F", status="escalated", last_error=long_error
            )
        )
        # Plain-prose event (no JSON) → reason resolves to "unknown".
        state.record_event("U-leg", "F", "coder_blocked", details="plain prose tail")

        rows = dashboard._escalated_data()
        row = next(r for r in rows if r["unit_id"] == "U-leg")
        assert row["reason"] == "unknown"
        assert len(row["last_error"]) == 120

    def test_show_dashboard_markdown_includes_full_remediation(self, tmp_state_db):
        long_prose = "RAW_PROSE_MARKER_" + "q" * 200
        _seed_escalated("U-md", "F", BRANCH_PROTECTION_SLUG, long_prose)

        md = observability.show_dashboard()
        assert "RAW_PROSE_MARKER_" in md, "full prose should reach the chat-rendered markdown"
        # The slug surfaces verbatim in the rendered markdown table.
        assert BRANCH_PROTECTION_SLUG in md
        # The "Reason:" header is part of the structured rendering.
        assert "Reason: " + BRANCH_PROTECTION_SLUG in md


# --------------------------- 5. unit_history reason filter ---------------------------


class TestUnitHistoryFilter:
    def _seed_mixed_events(self):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="escalated"))
        # Two events with auth_failure, one with branch_protection, one plain.
        state.record_event(
            "U1",
            "F",
            "coder_blocked",
            details=json.dumps({"reason": "auth_failure", "prose": "401-1"}),
        )
        state.record_event("U1", "F", "spawn_tester", details="prose only")
        state.record_event(
            "U1",
            "F",
            "tester_blocked",
            details=json.dumps({"reason": "auth_failure", "prose": "401-2"}),
        )
        state.record_event(
            "U1",
            "F",
            "coder_blocked",
            details=json.dumps({"reason": BRANCH_PROTECTION_SLUG, "prose": "denied"}),
        )

    def test_filter_returns_only_matching_slug(self, tmp_state_db):
        self._seed_mixed_events()
        parsed = json.loads(observability.unit_history("U1", reason="auth_failure"))
        assert len(parsed) == 2
        for e in parsed:
            payload = json.loads(e["details"])
            assert payload["reason"] == "auth_failure"

    def test_filter_with_different_slug_returns_only_that_slug(self, tmp_state_db):
        self._seed_mixed_events()
        parsed = json.loads(observability.unit_history("U1", reason=BRANCH_PROTECTION_SLUG))
        assert len(parsed) == 1
        assert json.loads(parsed[0]["details"])["reason"] == BRANCH_PROTECTION_SLUG

    def test_empty_filter_is_no_op(self, tmp_state_db):
        """Spec: 'Empty string (the default) preserves the original
        all-events behaviour.'"""
        self._seed_mixed_events()
        all_events = json.loads(observability.unit_history("U1"))
        same_via_empty = json.loads(observability.unit_history("U1", reason=""))
        assert len(all_events) == 4
        assert len(same_via_empty) == 4

    def test_no_matches_returns_friendly_message(self, tmp_state_db):
        """Empty result for an unmatched filter is not an error — the lead
        should see an informative message rather than a bare `[]`."""
        self._seed_mixed_events()
        out = observability.unit_history("U1", reason="rate_limited")
        # Not JSON, not a stack trace — a message naming the slug.
        assert "rate_limited" in out
        assert "matching" in out.lower() or "no events" in out.lower()


# --------------------------- 6. list_in_flight reason filter ---------------------------


class TestListInFlightFilter:
    """End-to-end check for the chat query 'show me everything blocked on
    auth': lead calls list_in_flight(reason='auth_failure') and gets
    exactly the units blocked on that slug, including escalated ones."""

    def _seed_mixed_units(self):
        state.save_feature(Feature(id="F", title="t", description=""))
        # 1) Active unit, no event — should NOT match an auth filter.
        state.upsert_unit_state(WorkUnitState(unit_id="U-active", feature_id="F", status="coding"))
        # 2) Escalated unit blocked on auth — should match auth filter.
        state.upsert_unit_state(WorkUnitState(unit_id="U-auth", feature_id="F", status="escalated"))
        state.record_event(
            "U-auth",
            "F",
            "coder_blocked",
            details=json.dumps({"reason": "auth_failure", "prose": "401"}),
        )
        # 3) Escalated unit blocked on branch protection — should NOT match auth filter.
        state.upsert_unit_state(WorkUnitState(unit_id="U-bp", feature_id="F", status="escalated"))
        state.record_event(
            "U-bp",
            "F",
            "coder_blocked",
            details=json.dumps({"reason": BRANCH_PROTECTION_SLUG, "prose": "denied"}),
        )
        # 4) Escalated unit with plain-prose event — reason='unknown', no match.
        state.upsert_unit_state(
            WorkUnitState(unit_id="U-legacy", feature_id="F", status="escalated")
        )
        state.record_event("U-legacy", "F", "coder_blocked", details="plain prose only")

    def test_blocked_on_auth_returns_only_auth_unit(self, tmp_state_db):
        self._seed_mixed_units()
        parsed = json.loads(ops.list_in_flight(reason="auth_failure"))
        ids = [r["unit_id"] for r in parsed]
        assert ids == ["U-auth"]
        assert parsed[0]["reason"] == "auth_failure"

    def test_blocked_on_branch_protection_returns_only_bp_unit(self, tmp_state_db):
        self._seed_mixed_units()
        parsed = json.loads(ops.list_in_flight(reason=BRANCH_PROTECTION_SLUG))
        ids = [r["unit_id"] for r in parsed]
        assert ids == ["U-bp"]
        assert parsed[0]["reason"] == BRANCH_PROTECTION_SLUG

    def test_no_filter_excludes_escalated_default_behavior(self, tmp_state_db):
        """Spec: default call (no reason= arg) preserves the original
        active-only behavior — escalated units stay out."""
        self._seed_mixed_units()
        parsed = json.loads(ops.list_in_flight())
        ids = sorted(r["unit_id"] for r in parsed)
        assert ids == ["U-active"]
        # No row should carry a `reason` field when no filter is in effect.
        for r in parsed:
            assert "reason" not in r

    def test_empty_string_filter_is_default_behavior(self, tmp_state_db):
        """Caller passing the falsy empty string should not accidentally
        opt into the widened/escalated-included path."""
        self._seed_mixed_units()
        parsed = json.loads(ops.list_in_flight(reason=""))
        ids = sorted(r["unit_id"] for r in parsed)
        assert ids == ["U-active"]


# --------------------------- 7. cross-tool consistency ---------------------------


class TestCrossToolConsistency:
    """Both filtering tools should agree on what slug a given event
    carries. This is the contract that lets the lead pivot between
    'show me everything blocked on X' (list_in_flight) and 'show me
    the auth-failure events on this unit' (unit_history)."""

    def test_history_and_in_flight_agree_on_slug(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U-x", feature_id="F", status="escalated"))
        state.record_event(
            "U-x",
            "F",
            "coder_blocked",
            details=json.dumps({"reason": "auth_failure", "prose": "401"}),
        )

        in_flight = json.loads(ops.list_in_flight(reason="auth_failure"))
        history = json.loads(observability.unit_history("U-x", reason="auth_failure"))
        assert len(in_flight) == 1
        assert in_flight[0]["unit_id"] == "U-x"
        assert in_flight[0]["reason"] == "auth_failure"
        assert len(history) == 1
        payload = json.loads(history[0]["details"])
        assert payload["reason"] == "auth_failure"


# --------------------------- 8. unknown-reason regression at every surface ---------------------------


class TestUnknownReasonRegression:
    """Spec: 'Reason=unknown falls back to today's prose-only behavior so
    nothing regresses for un-classifiable failures.' Exercise every
    surface with an unknown-reason event and assert no F-005-U-2-style
    structured rendering leaks through."""

    def test_format_escalation_summary_is_prose_only(self):
        prose = "legacy prose message"
        assert blocked_hints.format_escalation_summary("unknown", prose) == prose

    def test_format_ntfy_body_is_one_line(self):
        body = blocked_hints.format_ntfy_body("U-1", "unknown", "legacy prose")
        assert body == "Unit U-1 escalated: legacy prose"
        assert "\n" not in body  # truly single-line

    def test_dashboard_escalated_data_truncates(self, tmp_state_db):
        long_error = "L" * 500
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U-unk", feature_id="F", status="escalated", last_error=long_error
            )
        )
        state.record_event("U-unk", "F", "coder_blocked", details="plain prose")
        row = next(r for r in dashboard._escalated_data() if r["unit_id"] == "U-unk")
        assert row["reason"] == "unknown"
        # Legacy 120-char cap preserved.
        assert len(row["last_error"]) == 120

    def test_latest_blocked_reason_falls_back(self, tmp_state_db):
        state.save_feature(Feature(id="F", title="t", description=""))
        state.upsert_unit_state(WorkUnitState(unit_id="U-unk2", feature_id="F", status="escalated"))
        state.record_event("U-unk2", "F", "coder_blocked", details="no JSON here")
        reason, prose = blocked_hints.latest_blocked_reason("U-unk2")
        assert reason == "unknown"
        # Empty-prose fallback is the documented contract.
        assert prose == ""


# --------------------------- 9. taxonomy completeness ---------------------------


class TestTaxonomyHealth:
    """Sanity checks against the hint table to catch accidental
    regressions — empty entries, wrong types, off-by-one keys."""

    def test_all_slugs_are_non_empty_strings(self):
        for slug in blocked_hints.REMEDIATION_HINTS:
            assert isinstance(slug, str) and slug
            # Slugs use lowercase snake_case — no spaces, no uppercase.
            assert slug == slug.lower()
            assert " " not in slug

    def test_all_hints_are_non_empty_multiline_strings(self):
        for slug, hint in blocked_hints.REMEDIATION_HINTS.items():
            assert isinstance(hint, str)
            assert hint.strip(), f"hint for {slug!r} is empty"
            assert "\n" in hint, f"hint for {slug!r} is single-line"

    def test_unknown_is_not_in_the_table(self):
        """``unknown`` is the sentinel value, not a taxonomy entry — it
        must never have a hint, otherwise the fallback logic breaks."""
        assert "unknown" not in blocked_hints.REMEDIATION_HINTS
        assert blocked_hints.UNKNOWN_REASON == "unknown"
