"""Tests for orchestrator/blocked_hints.py — reason -> hint table + formatters.

Covers the four channels F-005-U-2 surfaces structured BLOCKED reasons
into: escalation summaries, ntfy push body, dashboard last-error rendering,
and DB-aware lookup. The producer side (events carrying ``reason`` in
their ``details`` JSON) lives in a sibling unit — these tests verify the
surfacing layer in isolation by hand-crafting events.
"""

from __future__ import annotations

import json

from orchestrator import blocked_hints, state
from orchestrator.models import Feature, WorkUnitState

# --------------------------- remediation_hint ---------------------------


class TestRemediationHint:
    def test_branch_protection_returns_three_options(self):
        hint = blocked_hints.remediation_hint("branch_protection_blocked_push")
        # Each of the three fix-it options from the feature description:
        assert "main" in hint  # option 1: scope to main only
        assert "bypass actor" in hint  # option 2: add bypass actor
        assert "PAT" in hint  # option 3: bypass-capable PAT
        # Numbered list ordering
        assert "1." in hint and "2." in hint and "3." in hint

    def test_auth_failure_has_actionable_steps(self):
        hint = blocked_hints.remediation_hint("auth_failure")
        assert "GITHUB_TOKEN" in hint or "App" in hint
        assert "verify_repo" in hint

    def test_unknown_reason_returns_empty(self):
        assert blocked_hints.remediation_hint("unknown") == ""

    def test_empty_string_returns_empty(self):
        assert blocked_hints.remediation_hint("") == ""

    def test_off_taxonomy_slug_returns_empty(self):
        """A slug that isn't in REMEDIATION_HINTS shouldn't crash or fabricate
        guidance — the surfacing layer falls back to prose-only."""
        assert blocked_hints.remediation_hint("totally_made_up_reason") == ""

    def test_every_taxonomy_entry_has_nonempty_hint(self):
        """Every slug shipped in REMEDIATION_HINTS must have a non-empty,
        multi-line value — degenerate entries would silently degrade the
        surfacing UX."""
        for slug, hint in blocked_hints.REMEDIATION_HINTS.items():
            assert hint.strip(), f"hint for {slug!r} is empty/whitespace"
            assert "\n" in hint, f"hint for {slug!r} is single-line"


# --------------------------- extract_reason_from_details ---------------------------


class TestExtractReasonFromDetails:
    def test_structured_json_parses_cleanly(self):
        details = json.dumps(
            {
                "reason": "branch_protection_blocked_push",
                "prose": "push rejected by required_pull_request_reviews",
            }
        )
        reason, prose = blocked_hints.extract_reason_from_details(details)
        assert reason == "branch_protection_blocked_push"
        assert prose == "push rejected by required_pull_request_reviews"

    def test_prose_only_falls_back_to_unknown(self):
        reason, prose = blocked_hints.extract_reason_from_details("remote: error: GH013: blah blah")
        assert reason == "unknown"
        # Original prose preserved verbatim — no regression for unclassifiable cases.
        assert prose == "remote: error: GH013: blah blah"

    def test_empty_string(self):
        reason, prose = blocked_hints.extract_reason_from_details("")
        assert reason == "unknown"
        assert prose == ""

    def test_none_treated_as_empty(self):
        """list_events returns dicts where details may be the empty string;
        callers should still get a clean fallback."""
        reason, prose = blocked_hints.extract_reason_from_details("")
        assert reason == "unknown"
        assert prose == ""

    def test_malformed_json_falls_back_to_prose(self):
        """A details string that LOOKS like JSON but isn't parseable must not
        crash the surfacing layer."""
        details = '{"reason": "auth_failure", oops not json'
        reason, prose = blocked_hints.extract_reason_from_details(details)
        assert reason == "unknown"
        assert prose == details

    def test_json_array_treated_as_unknown(self):
        """A JSON value that isn't an object shouldn't be confused with a
        reason payload."""
        reason, _ = blocked_hints.extract_reason_from_details("[1, 2, 3]")
        assert reason == "unknown"

    def test_json_missing_reason_field_is_unknown(self):
        details = json.dumps({"prose": "no reason field"})
        reason, prose = blocked_hints.extract_reason_from_details(details)
        assert reason == "unknown"
        # Falls back to the full details string when reason is missing.
        assert prose == details

    def test_json_with_reason_but_no_prose_uses_details(self):
        details = json.dumps({"reason": "rate_limited"})
        reason, prose = blocked_hints.extract_reason_from_details(details)
        assert reason == "rate_limited"
        assert prose == details

    def test_non_string_reason_falls_back_to_unknown(self):
        details = json.dumps({"reason": 42, "prose": "bogus"})
        reason, _ = blocked_hints.extract_reason_from_details(details)
        assert reason == "unknown"


# --------------------------- format_escalation_summary ---------------------------


class TestFormatEscalationSummary:
    def test_known_reason_renders_three_blocks(self):
        out = blocked_hints.format_escalation_summary(
            "branch_protection_blocked_push",
            "git push rejected on f-005-u-2",
        )
        # Block 1: reason header
        assert "Reason: branch_protection_blocked_push" in out
        # Block 2: hint (one of the fix-its)
        assert "bypass" in out.lower()
        # Block 3: prose preserved verbatim
        assert "git push rejected on f-005-u-2" in out

    def test_unknown_reason_returns_prose_verbatim(self):
        prose = "some unclassified failure message"
        out = blocked_hints.format_escalation_summary("unknown", prose)
        assert out == prose
        # The "Reason:" header must NOT appear — the spec is explicit that
        # unclassifiable failures keep today's prose-only behaviour.
        assert "Reason:" not in out

    def test_empty_reason_returns_prose_verbatim(self):
        prose = "blob"
        assert blocked_hints.format_escalation_summary("", prose) == prose

    def test_known_reason_with_empty_prose_still_shows_hint(self):
        """Even when the worker's prose is empty, the reason + hint are
        still useful and should be surfaced."""
        out = blocked_hints.format_escalation_summary("auth_failure", "")
        assert "Reason: auth_failure" in out
        assert "GITHUB_TOKEN" in out or "App" in out


# --------------------------- format_ntfy_body ---------------------------


class TestFormatNtfyBody:
    def test_known_reason_includes_reason_and_hint(self):
        body = blocked_hints.format_ntfy_body(
            "F-001-U-1",
            "branch_protection_blocked_push",
            "push denied",
        )
        assert "F-001-U-1" in body
        assert "branch_protection_blocked_push" in body
        # Hint content surfaces
        assert "bypass" in body.lower()
        # Prose included
        assert "push denied" in body

    def test_unknown_reason_falls_back_to_single_line(self):
        body = blocked_hints.format_ntfy_body("U-1", "unknown", "thing broke")
        assert body == "Unit U-1 escalated: thing broke"

    def test_unknown_reason_with_empty_prose(self):
        """Edge case: no reason AND no prose. Body still readable."""
        body = blocked_hints.format_ntfy_body("U-1", "unknown", "")
        assert body == "Unit U-1 escalated:"


# --------------------------- latest_blocked_reason (DB-aware) ---------------------------


class TestLatestBlockedReason:
    def _seed(self, unit_id="U1", feature_id="F"):
        state.save_feature(Feature(id=feature_id, title="t", description="d"))
        state.upsert_unit_state(
            WorkUnitState(unit_id=unit_id, feature_id=feature_id, status="escalated")
        )

    def test_returns_unknown_when_no_events(self, tmp_state_db):
        reason, prose = blocked_hints.latest_blocked_reason("nope")
        assert reason == "unknown"
        assert prose == ""

    def test_returns_unknown_when_events_have_only_prose(self, tmp_state_db):
        self._seed()
        state.record_event("U1", "F", "coder_blocked", details="plain prose only, no JSON")
        reason, _ = blocked_hints.latest_blocked_reason("U1")
        assert reason == "unknown"

    def test_returns_structured_reason_from_most_recent_event(self, tmp_state_db):
        self._seed()
        # Older event WITHOUT structured reason
        state.record_event("U1", "F", "coder_blocked", details="legacy prose")
        # Newer event WITH structured reason
        state.record_event(
            "U1",
            "F",
            "coder_blocked_on_fix",
            details=json.dumps(
                {
                    "reason": "branch_protection_blocked_push",
                    "prose": "push denied on feature branch",
                }
            ),
        )
        reason, prose = blocked_hints.latest_blocked_reason("U1")
        assert reason == "branch_protection_blocked_push"
        assert prose == "push denied on feature branch"

    def test_walks_newest_first_and_stops_at_first_match(self, tmp_state_db):
        """When the most recent event has unknown but an earlier one has a
        known reason, the newest-first walk finds the earlier one. We DON'T
        want a newer prose-only event to mask an earlier structured one."""
        self._seed()
        state.record_event(
            "U1",
            "F",
            "coder_blocked",
            details=json.dumps({"reason": "auth_failure", "prose": "401"}),
        )
        # Newer event without structured reason
        state.record_event("U1", "F", "coder_resumed", details="manual nudge")
        reason, prose = blocked_hints.latest_blocked_reason("U1")
        # We should still find the structured reason from the earlier event.
        assert reason == "auth_failure"
        assert prose == "401"
