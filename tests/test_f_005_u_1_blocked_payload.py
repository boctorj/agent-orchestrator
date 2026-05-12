"""Independent tester-agent tests for F-005-U-1.

Reason taxonomy + structured BLOCKED payload.

Verifies the contract from the unit description directly:

1. The reason taxonomy enumerates exactly the nine specified slugs.
2. Coder / tester / reviewer prompt files instruct workers to emit the
   structured ``BLOCKED: reason=<slug> [k=v]... | <prose>`` form.
3. The orchestrator's BLOCKED marker parser populates event payload from
   the structured fields.
4. The three branch-protection phrases ship as built-in recognizers
   (in that exact order) so legacy / un-tagged workers still classify.
5. End-to-end through ``spawn_unit`` / ``spawn_tester`` /
   ``spawn_reviewer`` / ``address_review``:
   (a) prompt-emitted structured reasons reach the event payload,
   (b) free-form output gets classified by a recognizer,
   (c) unrecognized output falls back to ``reason='unknown'`` without
       losing the worker's own prose.

These tests are deliberately written from scratch (not copied from the
coder's tests/test_blocked_reasons.py or tests/test_tools_execution.py)
so they act as an independent verification of the intended behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import state
from orchestrator.blocked_reasons import (
    VALID_REASONS,
    BlockedPayload,
    BlockedReason,
    builtin_recognizer_names,
    classify_prose,
    parse_blocked_body,
    parse_blocked_marker,
)
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution


# --------------------------- shared helpers / fakes ---------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = REPO_ROOT / "orchestrator" / "prompts"


@pytest.fixture(autouse=True)
def _force_ci_green(monkeypatch):
    """Force CI gate to green so it doesn't block our spawn calls."""

    def _green(*a, **kw):
        return CIWaitResult(status="green", elapsed_seconds=0.1, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", _green)


class _CannedWorker:
    """Minimal stand-in for ManagedAgentWorker.

    Captures spawn() / resume() calls. Different from FakeWorker in
    tests/test_tools_execution.py — we hand back a fresh session_id per
    role so address_review's resume() path has a valid coder session
    pre-stored.
    """

    _counter = 0

    def __init__(self, role: str, spawn_response: str = "", resume_response: str = ""):
        self.role = role
        self._spawn_response = spawn_response
        self._resume_response = resume_response

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        _CannedWorker._counter += 1
        return f"sess-{self.role}-{_CannedWorker._counter}", self._spawn_response

    def resume(self, session_id: str, msg: str) -> str:
        return self._resume_response

    def archive(self, session_id: str) -> None:  # pragma: no cover - never called in these tests
        pass


def _install_worker(monkeypatch, *, spawn_response="", resume_response=""):
    """Patch ManagedAgentWorker constructor to return _CannedWorker."""
    cache: dict[str, _CannedWorker] = {}

    def factory(role: str) -> _CannedWorker:
        if role not in cache:
            cache[role] = _CannedWorker(
                role, spawn_response=spawn_response, resume_response=resume_response
            )
        return cache[role]

    monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", factory)
    return cache


def _silence_side_effects(monkeypatch):
    """Mute github / ntfy I/O so tests stay hermetic."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_escalation",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge",
        lambda *a, **k: True,
    )


def _seed_feature(feature_id="F-005", repo="https://github.com/o/r") -> None:
    """Save a feature + plan + approve, using the pre-verified repo URL."""
    state.save_feature(
        Feature(
            id=feature_id,
            title="reason taxonomy",
            description="d",
            repo_path=repo,
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=f"{feature_id}-U-1",
                feature_id=feature_id,
                title="reason taxonomy",
                description="emit structured BLOCKED reasons",
            )
        ],
    )
    state.approve_plan(feature_id)


def _seed_with_pr(feature_id="F-005", unit_id="F-005-U-1") -> None:
    """As _seed_feature, but also pre-populate a unit state that already has a PR."""
    _seed_feature(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="in_ci",
            branch="feat/reason-taxonomy",
            pr_number=7,
            coder_session_id="sesn-c-seed",
        )
    )


def _last_blocked_event(unit_id: str) -> dict:
    """Return the JSON-decoded ``details`` of the most recent *_blocked event."""
    events = state.list_events(unit_id)
    blocked = [e for e in events if "blocked" in e["event_type"]]
    assert blocked, f"no blocked event recorded for {unit_id}"
    return json.loads(blocked[-1]["details"])


# --------------------------- (1) taxonomy ---------------------------


class TestReasonTaxonomy:
    """BlockedReason enum + VALID_REASONS frozenset spec coverage."""

    EXPECTED = {
        "branch_protection_blocked_push",
        "auth_failure",
        "network_error",
        "dependency_install_failed",
        "disk_full",
        "rate_limited",
        "ci_tool_missing",
        "merge_conflict_unresolved",
        "unknown",
    }

    def test_valid_reasons_exact_set(self):
        """VALID_REASONS is exactly the spec'd nine slugs — no more, no less."""
        assert set(VALID_REASONS) == self.EXPECTED

    def test_nine_slugs_total(self):
        """Lock cardinality to nine so a stray addition is caught."""
        assert len(VALID_REASONS) == 9

    @pytest.mark.parametrize("slug", sorted(EXPECTED))
    def test_every_slug_has_enum_member(self, slug):
        """Each slug has a corresponding StrEnum member with matching .value."""
        members = {r.value for r in BlockedReason}
        assert slug in members

    def test_enum_value_str_equality(self):
        """StrEnum: every member compares equal to its string value."""
        assert BlockedReason.UNKNOWN == "unknown"
        assert BlockedReason.AUTH_FAILURE == "auth_failure"
        assert BlockedReason.BRANCH_PROTECTION_BLOCKED_PUSH == "branch_protection_blocked_push"


# --------------------------- (2) prompts emit structured form ---------------------------


class TestPromptsTeachStructuredFormat:
    """The coder/tester/reviewer prompts must instruct workers to emit the
    structured BLOCKED form and must enumerate the nine reason slugs.

    The unit description is explicit: "Update tester + coder + reviewer
    prompts to emit BLOCKED in the form 'BLOCKED: reason=<slug> | <free text>'."
    Read the prompt files from disk so a regression that silently strips the
    guidance from one role's prompt fails this test.
    """

    @pytest.fixture(scope="class")
    def prompts(self) -> dict[str, str]:
        return {
            "coder": (PROMPT_DIR / "coder.md").read_text(),
            "tester": (PROMPT_DIR / "tester.md").read_text(),
            "reviewer": (PROMPT_DIR / "reviewer.md").read_text(),
        }

    @pytest.mark.parametrize("role", ["coder", "tester", "reviewer"])
    def test_prompt_describes_structured_marker(self, role, prompts):
        body = prompts[role]
        # Must show the literal template — "reason=<slug>" — so the agent
        # knows the syntax exactly.
        assert "reason=<slug>" in body
        # And the pipe separator that splits structured head from prose.
        assert "|" in body  # trivially true, but pairs with the next:
        assert "<free text>" in body or "<one-line free text>" in body

    @pytest.mark.parametrize("role", ["coder", "tester", "reviewer"])
    @pytest.mark.parametrize("slug", sorted(TestReasonTaxonomy.EXPECTED))
    def test_each_role_prompt_lists_every_slug(self, role, slug, prompts):
        """All nine slugs must be documented in each role's prompt so the
        agent has a complete menu to pick from."""
        assert slug in prompts[role], (
            f"prompt {role}.md is missing slug {slug!r} from the taxonomy"
        )


# --------------------------- (3) parser + payload ---------------------------


class TestParseBlockedBody:
    """Pure parser behavior."""

    def test_returns_blocked_payload(self):
        p = parse_blocked_body("reason=disk_full | df shows 100%")
        assert isinstance(p, BlockedPayload)

    def test_structured_minimal(self):
        p = parse_blocked_body("reason=auth_failure | gh returned 401")
        assert p.reason == "auth_failure"
        assert p.prose == "gh returned 401"
        assert p.fields == {}
        assert p.recognized_by is None

    def test_structured_with_fields(self):
        p = parse_blocked_body(
            "reason=branch_protection_blocked_push "
            "branch=feat/F-005-foo-u-1 "
            "rule_type=enforce_admins "
            "api_used=git_push "
            "| push rejected; ask user to bypass admins or scope rule to main"
        )
        assert p.reason == "branch_protection_blocked_push"
        # Every field token captured, in any order
        assert p.fields["branch"] == "feat/F-005-foo-u-1"
        assert p.fields["rule_type"] == "enforce_admins"
        assert p.fields["api_used"] == "git_push"
        # Prose preserved verbatim (after the pipe, trimmed)
        assert "bypass admins" in p.prose
        assert "scope rule to main" in p.prose

    def test_unrecognized_slug_falls_through_to_recognizer(self):
        """A bogus slug must not be silently accepted; the parser should
        run the recognizer chain against the prose and preserve the bogus
        slug in fields so a human can see what the worker emitted."""
        p = parse_blocked_body(
            "reason=foo_bar_baz | required_pull_request_reviews blocked the push"
        )
        # Recognizer fires on "required_pull_request_reviews" → branch_protection
        assert p.reason == "branch_protection_blocked_push"
        assert p.fields.get("unrecognized_reason_tag") == "foo_bar_baz"

    def test_legacy_bare_prose_with_recognizer_match(self):
        """Pre-F-005-U-1 workers emit ``BLOCKED: <prose>`` (no pipe).
        Parser must still classify via recognizer."""
        p = parse_blocked_body(
            "remote: error: GH013: Changes must be made through a pull request"
        )
        assert p.reason == "branch_protection_blocked_push"
        assert p.recognized_by == "branch_protection_pr_required"
        assert "Changes must be made through a pull request" in p.prose
        assert p.fields == {}

    def test_legacy_bare_prose_no_recognizer_match_is_unknown(self):
        """No structured tag, no recognizer match → reason='unknown',
        prose preserved verbatim."""
        p = parse_blocked_body("we just couldn't figure this one out")
        assert p.reason == "unknown"
        assert p.prose == "we just couldn't figure this one out"
        assert p.recognized_by is None
        assert p.fields == {}

    def test_payload_is_immutable(self):
        """BlockedPayload is a frozen dataclass so events stored in
        state.db can't be mutated mid-flight."""
        p = BlockedPayload(reason="unknown", prose="hi")
        with pytest.raises((AttributeError, TypeError)):
            p.reason = "auth_failure"  # type: ignore[misc]

    def test_to_event_payload_includes_reason_and_prose(self):
        """to_event_payload() is the canonical JSON shape stored on a
        unit_event row. reason + prose are mandatory."""
        p = parse_blocked_body("reason=network_error host=api.github.com | DNS timed out")
        out = p.to_event_payload()
        assert out["reason"] == "network_error"
        assert out["prose"] == "DNS timed out"
        assert out["fields"] == {"host": "api.github.com"}
        # recognized_by absent — slug came from the structured tag, not a recognizer
        assert "recognized_by" not in out

    def test_to_event_payload_omits_empty_fields(self):
        """When no field tokens were emitted, the dict should not carry an
        empty ``fields`` key (keeps event_details JSON compact)."""
        p = parse_blocked_body("reason=auth_failure | token rejected")
        out = p.to_event_payload()
        assert "fields" not in out
        # Same goes for recognized_by when none fired
        assert "recognized_by" not in out


# --------------------------- recognizer chain ---------------------------


class TestRecognizers:
    """The three branch-protection phrases must ship as the first three
    built-in recognizers in the prescribed order — this is verbatim from
    the unit description.
    """

    def test_first_three_recognizers_are_branch_protection(self):
        names = builtin_recognizer_names()
        # Must have at least the three, in order, at the head of the list.
        assert names[0] == "branch_protection_pr_required"
        assert names[1] == "branch_protection_required_reviews"
        assert names[2] == "branch_protection_enforce_admins"

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            (
                "remote: error: GH013: Changes must be made through a pull request.",
                "branch_protection_pr_required",
            ),
            (
                'API: { "rule_type": "required_pull_request_reviews" }',
                "branch_protection_required_reviews",
            ),
            (
                "branch protection includes enforce_admins=true",
                "branch_protection_enforce_admins",
            ),
        ],
    )
    def test_classify_prose_matches_branch_protection_strings(self, text, expected_name):
        slug, name = classify_prose(text)
        assert slug == "branch_protection_blocked_push"
        assert name == expected_name

    def test_classify_prose_is_case_insensitive(self):
        slug, _ = classify_prose("CHANGES MUST BE MADE THROUGH A PULL REQUEST")
        assert slug == "branch_protection_blocked_push"

    def test_classify_prose_no_match(self):
        slug, name = classify_prose("the toaster caught fire")
        assert slug == "unknown"
        assert name is None


# --------------------------- response-level marker scan ---------------------------


class TestParseBlockedMarker:
    def test_no_marker_returns_none(self):
        assert parse_blocked_marker("just narrative, no marker") is None
        assert parse_blocked_marker("PR_URL: https://x/pull/1") is None

    def test_finds_terminal_marker(self):
        resp = (
            "first I tried foo, then bar.\n"
            "BLOCKED: reason=disk_full | /workspace is full\n"
        )
        p = parse_blocked_marker(resp)
        assert p is not None
        assert p.reason == "disk_full"
        assert "/workspace is full" in p.prose

    def test_last_marker_wins_when_multiple_present(self):
        """Agents sometimes quote the BLOCKED template in narrative ("if X
        happens, you'd see BLOCKED: ...") before emitting the real one.
        Only the final occurrence is canonical."""
        resp = (
            "narrative: I might write 'BLOCKED: reason=unknown | hypothetical'\n"
            "...\n"
            "BLOCKED: reason=rate_limited | secondary 429 from api.github.com\n"
        )
        p = parse_blocked_marker(resp)
        assert p is not None
        assert p.reason == "rate_limited"
        assert "secondary 429" in p.prose


# --------------------------- (5a) prompt-emitted structured reasons E2E ---------------------------


class TestStructuredReasonEndToEnd:
    """Scenario (a) from the unit description: a worker emits
    ``BLOCKED: reason=<slug> ...``, the orchestrator parses it, and the
    structured payload reaches the unit_event details + last_error."""

    def test_coder_emits_branch_protection_payload(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "tried git push, got 403.\n"
                "BLOCKED: reason=branch_protection_blocked_push "
                "branch=feat/reason-taxonomy "
                "rule_type=required_pull_request_reviews "
                "api_used=git_push "
                "| push rejected; scope rule to main only or grant bypass"
            ),
        )
        _silence_side_effects(monkeypatch)

        out = execution.spawn_unit("F-005", "F-005-U-1")

        # 1. CLI return string surfaces both slug and prose
        assert "branch_protection_blocked_push" in out
        assert "scope rule to main only" in out

        # 2. WorkUnitState is escalated and last_error carries the slug tag
        s = state.get_unit_state("F-005-U-1")
        assert s is not None
        assert s.status == "escalated"
        assert "[branch_protection_blocked_push]" in (s.last_error or "")

        # 3. Recorded event details (JSON) contain structured fields
        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["fields"]["branch"] == "feat/reason-taxonomy"
        assert details["fields"]["rule_type"] == "required_pull_request_reviews"
        assert details["fields"]["api_used"] == "git_push"
        # prompt-emitted: no recognizer fired
        assert "recognized_by" not in details

    def test_tester_emits_dependency_install_failed_payload(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_with_pr()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "pip install pytest-asyncio exited 1\n"
                "BLOCKED: reason=dependency_install_failed pkg=pytest-asyncio | "
                "wheel build failed; sandbox missing libffi-dev"
            ),
        )
        _silence_side_effects(monkeypatch)

        msg = execution.spawn_tester("F-005", "F-005-U-1")

        assert "dependency_install_failed" in msg
        s = state.get_unit_state("F-005-U-1")
        assert s.status == "escalated"
        assert "[dependency_install_failed]" in (s.last_error or "")

        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "dependency_install_failed"
        assert details["fields"] == {"pkg": "pytest-asyncio"}
        assert "libffi-dev" in details["prose"]

    def test_reviewer_emits_auth_failure_payload(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_with_pr()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "gh pr view returned 401\n"
                "BLOCKED: reason=auth_failure | token rejected; need fresh PAT"
            ),
        )
        _silence_side_effects(monkeypatch)

        msg = execution.spawn_reviewer("F-005", "F-005-U-1")

        assert "auth_failure" in msg
        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "auth_failure"
        assert details["prose"] == "token rejected; need fresh PAT"

    def test_address_review_coder_emits_merge_conflict_payload(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_with_pr()
        _install_worker(
            monkeypatch,
            resume_response=(
                "rebase against main conflicted on 3 files\n"
                "BLOCKED: reason=merge_conflict_unresolved | three-way conflicts in "
                "prompts/coder.md need human guidance"
            ),
        )
        _silence_side_effects(monkeypatch)

        msg = execution.address_review("F-005-U-1", "tester", "fix the divide-by-zero")

        assert "merge_conflict_unresolved" in msg
        s = state.get_unit_state("F-005-U-1")
        assert s.status == "escalated"
        assert "[merge_conflict_unresolved]" in (s.last_error or "")

        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "merge_conflict_unresolved"
        assert "three-way conflicts" in details["prose"]


# --------------------------- (5b) recognizer fallback E2E ---------------------------


class TestRecognizerFallbackEndToEnd:
    """Scenario (b) from the unit description: a worker (often pre-dating
    F-005-U-1) emits bare prose; the orchestrator's recognizer chain still
    classifies it. The three branch-protection phrases must work."""

    def test_coder_blocked_pr_required_phrase_classified(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "git push origin feat/...\n"
                "remote: error: GH013: Changes must be made through a pull request\n"
                "BLOCKED: push rejected by remote — "
                "Changes must be made through a pull request"
            ),
        )
        _silence_side_effects(monkeypatch)

        execution.spawn_unit("F-005", "F-005-U-1")

        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["recognized_by"] == "branch_protection_pr_required"
        # Prose preserved (we don't lose what the worker actually said)
        assert "Changes must be made through a pull request" in details["prose"]
        # fields dict either absent or empty (no key=value tokens supplied)
        assert details.get("fields", {}) == {}

        # last_error includes the structured reason tag
        s = state.get_unit_state("F-005-U-1")
        assert "[branch_protection_blocked_push]" in (s.last_error or "")

    def test_tester_blocked_required_reviews_token_classified(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_with_pr()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "BLOCKED: API replied 422 with rule_type=required_pull_request_reviews"
            ),
        )
        _silence_side_effects(monkeypatch)

        execution.spawn_tester("F-005", "F-005-U-1")

        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["recognized_by"] == "branch_protection_required_reviews"

    def test_reviewer_blocked_enforce_admins_token_classified(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_with_pr()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "BLOCKED: protection rule with enforce_admins=true blocks even admins"
            ),
        )
        _silence_side_effects(monkeypatch)

        execution.spawn_reviewer("F-005", "F-005-U-1")

        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "branch_protection_blocked_push"
        assert details["recognized_by"] == "branch_protection_enforce_admins"


# --------------------------- (5c) unknown fallback preserves prose ---------------------------


class TestUnknownFallback:
    """Scenario (c) from the unit description: when nothing matches, the
    reason slug is ``unknown`` and the worker's prose is preserved verbatim
    so the escalation summary still carries their explanation."""

    def test_spawn_unit_unknown_preserves_prose(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature()
        _install_worker(
            monkeypatch,
            spawn_response="BLOCKED: spec says 'make it faster' with no measurable target",
        )
        _silence_side_effects(monkeypatch)

        out = execution.spawn_unit("F-005", "F-005-U-1")

        assert "unknown" in out
        # Worker's own words must appear verbatim somewhere accessible
        assert "make it faster" in out
        s = state.get_unit_state("F-005-U-1")
        assert "[unknown]" in (s.last_error or "")
        assert "make it faster" in (s.last_error or "")

        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "unknown"
        assert "make it faster" in details["prose"]
        # No recognizer fired → key omitted from compact event payload
        assert "recognized_by" not in details

    def test_spawn_tester_unknown_preserves_prose(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_with_pr()
        _install_worker(
            monkeypatch,
            spawn_response="BLOCKED: unit description is so vague I genuinely can't test it",
        )
        _silence_side_effects(monkeypatch)

        msg = execution.spawn_tester("F-005", "F-005-U-1")

        assert "unknown" in msg
        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "unknown"
        assert "vague" in details["prose"]

    def test_address_review_unknown_preserves_prose(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_with_pr()
        _install_worker(
            monkeypatch,
            resume_response="BLOCKED: hit an internal sandbox error I've never seen before",
        )
        _silence_side_effects(monkeypatch)

        msg = execution.address_review("F-005-U-1", "reviewer", "rename x to y")

        assert "unknown" in msg
        s = state.get_unit_state("F-005-U-1")
        assert "[unknown]" in (s.last_error or "")
        details = _last_blocked_event("F-005-U-1")
        assert details["reason"] == "unknown"
        assert "internal sandbox error" in details["prose"]


# --------------------------- last_error / ntfy formatting ---------------------------


class TestEscalationSurfaceFormatting:
    """The slug must surface on the lead's screen + the phone push body.

    Goal (b) from the feature context: "surface that structured reason in
    escalation summaries and ntfy push bodies."
    """

    def test_last_error_carries_slug_in_brackets(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_feature()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "BLOCKED: reason=ci_tool_missing tool=pytest | pytest not on PATH in sandbox"
            ),
        )
        _silence_side_effects(monkeypatch)

        execution.spawn_unit("F-005", "F-005-U-1")
        s = state.get_unit_state("F-005-U-1")
        # Bracketed slug is the documented format used by
        # format_blocked_last_error so the dashboard can scan reason at a glance
        assert (s.last_error or "").startswith("BLOCKED [ci_tool_missing]")
        assert "pytest not on PATH" in s.last_error

    def test_ntfy_push_includes_slug(self, tmp_state_db, with_github_token, monkeypatch):
        _seed_feature()
        _install_worker(
            monkeypatch,
            spawn_response=(
                "BLOCKED: reason=rate_limited host=api.github.com | secondary rate limit"
            ),
        )
        monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
        monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )
        captured: list[tuple] = []
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_escalation",
            lambda *args, **kw: captured.append((args, kw)) or True,
        )

        execution.spawn_unit("F-005", "F-005-U-1")

        assert captured, "expected an escalation push"
        # The push body (positional arg 1) carries the reason slug for the phone
        args, _ = captured[0]
        body = " ".join(str(a) for a in args)
        assert "rate_limited" in body
        assert "secondary rate limit" in body
