"""Tests for ``orchestrator.markers`` — pure marker parsing (F-016 Phase 0).

The module owns the regex grammar that ``_record_terminal_marker`` (lead
path) and the F-016 watcher daemon both consume. Determinism is the
contract: same ``(role, text)`` -> equal :class:`MarkerSpec` -> stable
:func:`dedupe_key` -> ``INSERT OR IGNORE`` no-ops on re-scan.
"""

from __future__ import annotations

import pytest

from orchestrator import markers
from orchestrator.blocked_reasons import BlockedPayload
from orchestrator.markers import MarkerSpec, dedupe_key, scan_response

# --------------------------- scan_response ---------------------------


class TestScanResponseReviewer:
    def test_recommend_merge_returns_recommend_event_with_reason(self):
        spec = scan_response("reviewer", "looks good\nREVIEW_RECOMMEND_MERGE: clean")

        assert spec is not None
        assert spec.role == "reviewer"
        assert spec.marker == "REVIEW_RECOMMEND_MERGE"
        assert spec.event_type == "reviewer_recommend_merge"
        assert spec.target_status == "approved_awaiting_merge"
        assert spec.extras == {"reason": "clean"}
        assert "clean" in spec.summary

    def test_request_changes_does_not_set_target_status(self):
        spec = scan_response("reviewer", "REVIEW_REQUEST_CHANGES: rename it")

        assert spec is not None
        assert spec.marker == "REVIEW_REQUEST_CHANGES"
        assert spec.event_type == "reviewer_request_changes"
        # caller's fix-loop owns the next status transition
        assert spec.target_status is None
        assert spec.extras == {"issue": "rename it"}

    def test_comment_only_flips_to_in_ci(self):
        spec = scan_response("reviewer", "REVIEW_COMMENT")

        assert spec is not None
        assert spec.marker == "REVIEW_COMMENT"
        assert spec.event_type == "reviewer_comment"
        assert spec.target_status == "in_ci"


class TestScanResponseTester:
    def test_tests_pass_flips_to_in_ci(self):
        spec = scan_response("tester", "TESTS_PASS\n")

        assert spec is not None
        assert spec.marker == "TESTS_PASS"
        assert spec.event_type == "tests_pass"
        assert spec.target_status == "in_ci"

    def test_bug_found_carries_bug_text(self):
        spec = scan_response("tester", "BUG_FOUND: off-by-one in loop")

        assert spec is not None
        assert spec.marker == "BUG_FOUND"
        assert spec.event_type == "tester_bug_found"
        assert spec.target_status is None
        assert spec.extras == {"bug": "off-by-one in loop"}


class TestScanResponseCoder:
    def test_pr_url_captures_url_and_number(self):
        spec = scan_response("coder", "PR_URL: https://github.com/o/r/pull/42")

        assert spec is not None
        assert spec.marker == "PR_URL"
        assert spec.event_type == "pr_opened"
        assert spec.target_status == "in_ci"
        assert spec.extras == {
            "pr_url": "https://github.com/o/r/pull/42",
            "pr_number": 42,
        }

    def test_fix_pushed_flips_to_in_ci(self):
        spec = scan_response("coder", "patch applied\nFIX_PUSHED\n")

        assert spec is not None
        assert spec.marker == "FIX_PUSHED"
        assert spec.event_type == "fix_pushed"
        assert spec.target_status == "in_ci"


class TestScanResponseBlockedUniversal:
    @pytest.mark.parametrize("role", ["coder", "tester", "reviewer"])
    def test_blocked_recognised_for_every_role(self, role):
        spec = scan_response(role, "ran into a wall\nBLOCKED: reason=auth_failure | 401")

        assert spec is not None
        assert spec.marker == "BLOCKED"
        assert spec.event_type == f"{role}_blocked"
        assert spec.target_status == "escalated"
        assert isinstance(spec.blocked_payload, BlockedPayload)
        assert spec.blocked_payload.reason == "auth_failure"
        assert "auth_failure" in spec.last_error

    def test_unstructured_blocked_classified_via_recognisers(self):
        spec = scan_response(
            "coder",
            "BLOCKED: Changes must be made through a pull request",
        )

        assert spec is not None
        assert spec.blocked_payload is not None
        assert spec.blocked_payload.reason == "branch_protection_blocked_push"


class TestScanResponseCrossRoleIgnored:
    def test_tester_response_with_reviewer_marker_returns_none(self):
        # cross-role markers are not recognised
        assert scan_response("tester", "REVIEW_RECOMMEND_MERGE: not mine") is None

    def test_coder_response_with_tester_marker_returns_none(self):
        assert scan_response("coder", "TESTS_PASS") is None

    def test_reviewer_response_with_coder_marker_returns_none(self):
        assert scan_response("reviewer", "FIX_PUSHED") is None

    def test_no_marker_returns_none(self):
        assert scan_response("reviewer", "just thinking out loud") is None


class TestScanResponseAllowedNarrowing:
    def test_pr_url_excluded_when_only_fix_pushed_blocked_allowed(self):
        # mirrors the address_review narrowing — a coder resume that
        # carries a stray PR_URL must not match (the unit already has a PR)
        spec = scan_response(
            "coder",
            "PR_URL: https://github.com/o/r/pull/9\nFIX_PUSHED",
            allowed=frozenset({"FIX_PUSHED", "BLOCKED"}),
        )
        assert spec is not None
        assert spec.marker == "FIX_PUSHED"

    def test_blocked_excluded_when_not_in_allowed(self):
        spec = scan_response(
            "coder",
            "BLOCKED: nothing works",
            allowed=frozenset({"FIX_PUSHED"}),
        )
        assert spec is None


# --------------------------- determinism / equality ---------------------------


class TestScanResponseDeterminism:
    """The watcher daemon and the lead must produce equal MarkerSpecs from
    the same response so the dedupe key collides on re-scan."""

    def test_same_input_returns_equal_spec(self):
        response = "passed everything\nTESTS_PASS\n"
        a = scan_response("tester", response)
        b = scan_response("tester", response)
        assert a is not None
        assert a == b

    def test_different_payloads_produce_different_specs(self):
        a = scan_response("tester", "BUG_FOUND: a")
        b = scan_response("tester", "BUG_FOUND: b")
        assert a != b


# --------------------------- dedupe_key ---------------------------


class TestDedupeKey:
    def test_returns_full_sha256_hexdigest(self):
        key = dedupe_key(
            session_id="sesn-1",
            cycle_number=0,
            event_type="fix_pushed",
            marker_payload="FIX_PUSHED",
        )
        assert len(key) == 64
        # sha256 hex output is lowercase 0-9a-f
        assert all(c in "0123456789abcdef" for c in key)

    def test_same_inputs_produce_same_key(self):
        kw = dict(
            session_id="sesn-1",
            cycle_number=2,
            event_type="fix_pushed",
            marker_payload="FIX_PUSHED",
        )
        assert dedupe_key(**kw) == dedupe_key(**kw)

    def test_cycle_number_changes_key(self):
        """The proposal flags this as load-bearing: a re-emit of the same
        marker in a later cycle must NOT dedupe against the earlier one."""
        a = dedupe_key(
            session_id="s",
            cycle_number=1,
            event_type="fix_pushed",
            marker_payload="FIX_PUSHED",
        )
        b = dedupe_key(
            session_id="s",
            cycle_number=3,
            event_type="fix_pushed",
            marker_payload="FIX_PUSHED",
        )
        assert a != b

    def test_event_type_changes_key(self):
        """``coder_blocked`` vs ``coder_blocked_on_fix`` must not collide."""
        a = dedupe_key(
            session_id="s",
            cycle_number=0,
            event_type="coder_blocked",
            marker_payload="auth_failure|401",
        )
        b = dedupe_key(
            session_id="s",
            cycle_number=0,
            event_type="coder_blocked_on_fix",
            marker_payload="auth_failure|401",
        )
        assert a != b

    def test_session_id_changes_key(self):
        a = dedupe_key(
            session_id="s-a", cycle_number=0, event_type="tests_pass", marker_payload="x"
        )
        b = dedupe_key(
            session_id="s-b", cycle_number=0, event_type="tests_pass", marker_payload="x"
        )
        assert a != b

    def test_none_cycle_number_encoded_explicitly(self):
        # No exceptions, distinct from cycle_number=0
        a = dedupe_key(session_id="s", cycle_number=None, event_type="t", marker_payload="x")
        b = dedupe_key(session_id="s", cycle_number=0, event_type="t", marker_payload="x")
        assert isinstance(a, str)
        assert a != b


# --------------------------- module surface ---------------------------


class TestModuleSurface:
    def test_marker_spec_is_a_dataclass(self):
        # the proposal acceptance test asserts the return type's name
        assert MarkerSpec.__name__ == "MarkerSpec"
        spec = scan_response("tester", "TESTS_PASS")
        assert isinstance(spec, MarkerSpec)

    def test_regex_constants_exported(self):
        # the daemon and other readers want the regex constants live in
        # this module too; the legacy ``orchestrator.tools`` re-export
        # is what existing call sites consume
        for name in (
            "PR_URL_RE",
            "TESTS_PASS_RE",
            "BUG_FOUND_RE",
            "REVIEW_CHANGES_RE",
            "REVIEW_COMMENT_RE",
            "REVIEW_RECOMMEND_MERGE_RE",
            "FIX_PUSHED_RE",
        ):
            assert hasattr(markers, name), f"markers.{name} missing"
