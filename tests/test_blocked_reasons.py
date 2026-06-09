"""Tests for orchestrator/blocked_reasons.py — taxonomy + parser + recognisers."""

from __future__ import annotations

import re

import pytest

from orchestrator.blocked_reasons import (
    VALID_REASONS,
    BlockedPayload,
    BlockedReason,
    builtin_recognizer_names,
    classify_prose,
    parse_blocked_body,
    parse_blocked_marker,
    register_recognizer,
)

# --------------------------- taxonomy ---------------------------


class TestTaxonomy:
    def test_expected_slugs_present(self):
        # The unit spec enumerates exactly these slugs; lock that
        # contract so accidental renames are caught.
        assert {
            "branch_protection_blocked_push",
            "auth_failure",
            "network_error",
            "dependency_install_failed",
            "disk_full",
            "rate_limited",
            "ci_tool_missing",
            "merge_conflict_unresolved",
            # F-018: orchestrator-side escalation when ``conflict_fix_attempts``
            # hits the cap (3 rebase rounds). Distinct from
            # ``merge_conflict_unresolved`` (single rebase the coder couldn't
            # resolve mechanically) — diverging means main is too volatile.
            "conflict_rebase_diverging",
            "unknown",
        } == VALID_REASONS

    def test_enum_values_match_strings(self):
        # Every enum member's value is exactly its slug — supports
        # `BlockedReason.AUTH_FAILURE == "auth_failure"` direct comparison.
        for r in BlockedReason:
            assert r.value in VALID_REASONS
            assert r == r.value


# --------------------------- structured form ---------------------------


class TestParseBlockedBodyStructured:
    def test_minimal_reason_only(self):
        p = parse_blocked_body("reason=auth_failure | token rejected")
        assert p.reason == "auth_failure"
        assert p.prose == "token rejected"
        assert p.fields == {}
        assert p.recognized_by is None

    def test_full_branch_protection_payload(self):
        p = parse_blocked_body(
            "reason=branch_protection_blocked_push "
            "branch=feat/F-001-foo "
            "rule_type=required_pull_request_reviews "
            "api_used=git_push "
            "| push rejected; ask user to scope rule to main only"
        )
        assert p.reason == "branch_protection_blocked_push"
        assert p.fields == {
            "branch": "feat/F-001-foo",
            "rule_type": "required_pull_request_reviews",
            "api_used": "git_push",
        }
        assert "scope rule to main only" in p.prose

    def test_unknown_slug_falls_through_to_classifier(self):
        # A bogus `reason=` tag is treated as if the worker forgot to tag,
        # but preserved in fields["unrecognized_reason_tag"] for the human.
        p = parse_blocked_body(
            "reason=cosmic_rays | required_pull_request_reviews flagged the push"
        )
        # The recogniser for required_pull_request_reviews fires
        assert p.reason == "branch_protection_blocked_push"
        assert p.recognized_by == "branch_protection_required_reviews"
        assert p.fields["unrecognized_reason_tag"] == "cosmic_rays"

    def test_empty_reason_tag_falls_through(self):
        p = parse_blocked_body("reason= | totally unrecognisable failure")
        assert p.reason == "unknown"
        assert p.prose == "totally unrecognisable failure"

    def test_prose_empty_after_pipe_uses_body(self):
        # If the worker writes a structured head but forgets prose, we
        # keep the head as fallback prose so the human still sees it.
        p = parse_blocked_body("reason=auth_failure |")
        assert p.reason == "auth_failure"
        assert p.prose  # non-empty
        assert "auth_failure" in p.prose

    def test_to_event_payload_shape(self):
        p = parse_blocked_body("reason=disk_full host=sandbox | df -h shows 100% on /workspace")
        d = p.to_event_payload()
        assert d == {
            "reason": "disk_full",
            "prose": "df -h shows 100% on /workspace",
            "fields": {"host": "sandbox"},
        }


# --------------------------- recogniser fallback ---------------------------


class TestRecognizerFallback:
    """Workers that pre-date the structured format must still classify."""

    def test_pr_required_phrase(self):
        slug, name = classify_prose(
            "remote: error: GH013: Changes must be made through a pull request."
        )
        assert slug == "branch_protection_blocked_push"
        assert name == "branch_protection_pr_required"

    def test_required_pull_request_reviews_token(self):
        slug, name = classify_prose("API response: rule_type=required_pull_request_reviews")
        assert slug == "branch_protection_blocked_push"
        assert name == "branch_protection_required_reviews"

    def test_enforce_admins_token(self):
        slug, name = classify_prose('protection: { "enforce_admins": true, ... }')
        assert slug == "branch_protection_blocked_push"
        assert name == "branch_protection_enforce_admins"

    def test_case_insensitive(self):
        slug, _ = classify_prose("ENFORCE_ADMINS is set")
        assert slug == "branch_protection_blocked_push"

    def test_no_match_returns_unknown(self):
        slug, name = classify_prose("the unit description is too vague to test")
        assert slug == "unknown"
        assert name is None

    def test_builtin_recognizers_cover_three_branch_protection_strings(self):
        # The unit spec mandates "ship the three branch-protection strings
        # as the first recognisers" — assert order and identity.
        names = builtin_recognizer_names()
        assert names[:3] == [
            "branch_protection_pr_required",
            "branch_protection_required_reviews",
            "branch_protection_enforce_admins",
        ]


class TestRegisterRecognizer:
    def test_appends_at_end(self):
        before = builtin_recognizer_names()
        register_recognizer("ratelimit_secondary", r"secondary rate limit", "rate_limited")
        try:
            after = builtin_recognizer_names()
            assert after[-1] == "ratelimit_secondary"
            assert len(after) == len(before) + 1

            slug, name = classify_prose("GitHub returned secondary rate limit")
            assert slug == "rate_limited"
            assert name == "ratelimit_secondary"
        finally:
            # Best-effort cleanup so test ordering doesn't matter.
            from orchestrator import blocked_reasons

            blocked_reasons._RECOGNIZERS.pop()

    def test_rejects_unknown_slug(self):
        with pytest.raises(ValueError, match="unknown reason slug"):
            register_recognizer("bad", r"x", "not_in_taxonomy")

    def test_accepts_compiled_pattern(self):
        pat = re.compile(r"out of memory", re.IGNORECASE)
        register_recognizer("oom", pat, "unknown")
        try:
            slug, name = classify_prose("ERROR: Out Of Memory at offset 0x4000")
            assert slug == "unknown"
            assert name == "oom"
        finally:
            from orchestrator import blocked_reasons

            blocked_reasons._RECOGNIZERS.pop()


# --------------------------- bare-prose / legacy form ---------------------------


class TestParseBlockedBodyLegacy:
    """Workers that pre-date this change emit `BLOCKED: <prose>` with no `|`."""

    def test_legacy_form_classified_by_recognizer(self):
        p = parse_blocked_body("remote rejected: Changes must be made through a pull request")
        assert p.reason == "branch_protection_blocked_push"
        assert p.recognized_by == "branch_protection_pr_required"
        # Prose retains the full body
        assert "pull request" in p.prose
        assert p.fields == {}

    def test_legacy_form_unknown_preserves_prose(self):
        p = parse_blocked_body("spec is ambiguous")
        assert p.reason == "unknown"
        assert p.prose == "spec is ambiguous"
        assert p.recognized_by is None


# --------------------------- response-level marker scan ---------------------------


class TestParseBlockedMarker:
    def test_no_marker_returns_none(self):
        assert parse_blocked_marker("PR_URL: https://...") is None
        assert parse_blocked_marker("") is None

    def test_finds_marker_amid_prose(self):
        resp = """Started cloning the repo, ran tests...
something went sideways.

BLOCKED: reason=ci_tool_missing tool=pytest | pytest not installed in sandbox
"""
        p = parse_blocked_marker(resp)
        assert p is not None
        assert p.reason == "ci_tool_missing"
        assert p.fields == {"tool": "pytest"}
        assert "pytest not installed" in p.prose

    def test_last_marker_wins(self):
        # An agent might say "if X happens you'd see BLOCKED: ..." in
        # narration, then emit the real one at the end. Only the last
        # counts.
        resp = (
            "narrative: if it failed I'd write BLOCKED: reason=unknown | dummy\n"
            "<eventually>\n"
            "BLOCKED: reason=auth_failure | gh returned 401 Bad credentials\n"
        )
        p = parse_blocked_marker(resp)
        assert p is not None
        assert p.reason == "auth_failure"
        assert "401" in p.prose


# --------------------------- immutability ---------------------------


def test_payload_is_frozen():
    p = BlockedPayload(reason="unknown", prose="x")
    with pytest.raises(AttributeError):
        p.reason = "auth_failure"  # type: ignore[misc]
