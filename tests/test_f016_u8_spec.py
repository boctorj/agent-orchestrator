"""F-016-U-8 — Phase 6: uniform non-blocking dispatch (spec tests).

Pins the spec-acceptance behavior from ``features/F-016/spec.md`` §
Phase 6 and the proposal's per-phase acceptance:

  * **Every long-running lead-facing command flips by default under
    NTFY+daemon, not just ``cycle_review``.** Acceptance 1 lists seven
    surfaces — ``spawn_unit`` / ``address_review`` / ``spawn_tester``
    / ``spawn_reviewer`` / ``send_to_unit`` / ``cycle_review`` /
    ``parallel_units(_global)``. Each must return in ≤3 s under
    NTFY+daemon and retain an explicit ``_blocking`` variant.

  * **The dispatcher gate is shared.** A single ``_dispatch_or_block``
    helper owns the routing decision so the seven surfaces read
    identically — U-6 had one gate inline in ``cycle_review``; U-8
    factors it.

  * **New ``_async`` variants land for the surfaces that were
    blocking-only**: ``address_review_async`` (via
    ``worker.resume_async``), ``spawn_tester_async`` /
    ``spawn_reviewer_async`` (via ``worker.spawn_async`` or
    ``worker.resume_async`` for resumes).

  * **Daemon pickup from a freshly-seeded row.** Acceptance 1's
    "drives each async-dispatched phase to terminal" — for each
    ``_async`` dispatch, the daemon's marker observation closes the
    loop on the SINGLE phase's terminal flip
    (``coding/opening_pr/fixing → in_ci`` via PR_URL / FIX_PUSHED,
    ``testing → in_ci`` via TESTS_PASS,
    ``reviewing → approved_awaiting_merge`` via REVIEW_RECOMMEND_MERGE).
    BUG_FOUND / REVIEW_REQUEST_CHANGES leave status alone for the
    lead's fix-loop — out of U-8 scope (U-9 / future work).

  * **A lead killed right after any async dispatch does not strand
    the unit.** Acceptance 2. The session_id MUST be persisted before
    the async surface returns, so a reattaching lead (or the daemon)
    can resume the in-flight worker.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from orchestrator import daemon, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared fixtures ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend every PR's CI is green for tests in this module."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=1.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _setup_feature(
    feature_id: str = "F-001",
    unit_id: str = "F-016-U-8-T",
    repo: str = "https://github.com/o/r",
) -> None:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path=repo,
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [
            WorkUnit(
                id=unit_id,
                feature_id=feature_id,
                title="u1",
                description="impl this",
            )
        ],
    )
    state.approve_plan(feature_id)


def _seed_coded_unit(unit_id: str = "F-016-U-8-T", feature_id: str = "F-001") -> None:
    """Feature + unit with a coder session and a PR already open."""
    _setup_feature(feature_id=feature_id, unit_id=unit_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="in_ci",
            branch="feat/branch",
            pr_number=5,
            coder_session_id="sesn-c",
        )
    )


def _seed_daemon_lock(holder_id: str = "test-holder") -> None:
    from pathlib import Path

    path = str(Path(state.STATE_DB).resolve())
    state.claim_daemon_lock(path, holder_id)


def _stub_github(monkeypatch):
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests", lambda *a, **k: 0
    )
    monkeypatch.setattr("orchestrator.tools.execution.ntfy.push_escalation", lambda *a, **k: True)
    monkeypatch.setattr(
        "orchestrator.tools.execution.ntfy.push_ready_to_merge", lambda *a, **k: True
    )


# A worker double that captures spawn_async / resume_async submissions
# WITHOUT touching the Anthropic SDK. Each ``async_submit`` is the
# bookkeeping surface tests assert on; the canned ``next_session_id``
# is what ``spawn_async`` hands back to the dispatcher.


@dataclass
class _FakeAsyncWorker:
    role: str = "coder"
    next_session_id: str = "sess-new"
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.submissions.append({"op": "spawn_async", "task": task, "title": title})
        return self.next_session_id

    def resume_async(self, session_id: str, msg: str) -> None:
        self.submissions.append({"op": "resume_async", "session_id": session_id, "msg": msg})


def _install_fake_async_worker(monkeypatch) -> dict[str, _FakeAsyncWorker]:
    """Patch ``make_worker`` to return per-role fakes; return the dict.

    Patches ``make_worker`` at the import site used by the new Phase 6
    helpers (``orchestrator.tools.execution.make_worker``) AND the
    daemon's cached-worker entry so a daemon tick sees a deterministic
    tail.
    """
    workers: dict[str, _FakeAsyncWorker] = {
        "coder": _FakeAsyncWorker(role="coder", next_session_id="sess-coder"),
        "tester": _FakeAsyncWorker(role="tester", next_session_id="sess-tester"),
        "reviewer": _FakeAsyncWorker(role="reviewer", next_session_id="sess-reviewer"),
    }
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda role: workers[role])
    return workers


# --------------------------- _dispatch_or_block helper ---------------------------


class TestDispatchOrBlockHelper:
    """The factored peer of U-6's ``cycle_review`` dispatcher.

    Spec § Phase 6: "Factor U-6's gate into a shared
    ``_dispatch_or_block`` helper". Every Phase 6 surface MUST go
    through this helper — otherwise the seven surfaces drift on the
    NTFY-vs-daemon gate's edge cases (PR #64 reviewer M1 fence).
    """

    def test_async_branch_under_ntfy_plus_daemon(
        self, tmp_state_db, with_ntfy_topic, with_github_token
    ):
        calls: list[str] = []
        result = execution._dispatch_or_block(
            async_fn=lambda d: (calls.append(f"async/{d['running']}"), "ASYNC")[1],
            blocking_fn=lambda: (calls.append("blocking"), "BLOCKING")[1],
        )
        # No daemon seeded — falls through to blocking.
        assert calls == ["blocking"]
        assert "BLOCKING" in result

    def test_async_branch_runs_when_daemon_alive(
        self, tmp_state_db, with_ntfy_topic, with_github_token
    ):
        _seed_daemon_lock()
        calls: list[str] = []
        result = execution._dispatch_or_block(
            async_fn=lambda d: (calls.append(f"async/{d['running']}"), "ASYNC")[1],
            blocking_fn=lambda: (calls.append("blocking"), "BLOCKING")[1],
        )
        assert calls == ["async/True"]
        assert result == "ASYNC"

    def test_blocking_branch_runs_when_ntfy_unset(
        self, tmp_state_db, no_ntfy_topic, with_github_token
    ):
        _seed_daemon_lock()  # daemon is alive but ntfy isn't configured
        calls: list[str] = []
        execution._dispatch_or_block(
            async_fn=lambda d: (calls.append("async"), "ASYNC")[1],
            blocking_fn=lambda: (calls.append("blocking"), "BLOCKING")[1],
        )
        # Spec § "Decisions": "Default-flip gated on NTFY_TOPIC".
        assert calls == ["blocking"]

    def test_blocking_branch_runs_when_daemon_down(
        self, tmp_state_db, with_ntfy_topic, with_github_token
    ):
        # No daemon_locks row.
        calls: list[str] = []
        execution._dispatch_or_block(
            async_fn=lambda d: (calls.append("async"), "ASYNC")[1],
            blocking_fn=lambda: (calls.append("blocking"), "BLOCKING")[1],
        )
        assert calls == ["blocking"]

    def test_blocking_path_attaches_ntfy_nudge_when_ntfy_unset(
        self, tmp_state_db, no_ntfy_topic, with_github_token
    ):
        result = execution._dispatch_or_block(
            async_fn=lambda d: "ASYNC",
            blocking_fn=lambda: json.dumps({"outcome": "approved_awaiting_merge"}),
        )
        parsed = json.loads(result)
        # The NTFY nudge MUST land in ``nudge`` so the lead persona
        # surfaces it once per session.
        assert "ntfy_topic" in parsed["nudge"].lower()

    def test_blocking_path_attaches_daemon_nudge_when_daemon_down(
        self, tmp_state_db, with_ntfy_topic, with_github_token
    ):
        result = execution._dispatch_or_block(
            async_fn=lambda d: "ASYNC",
            blocking_fn=lambda: json.dumps({"outcome": "approved_awaiting_merge"}),
        )
        parsed = json.loads(result)
        assert "daemon" in parsed["nudge"].lower()

    def test_bare_string_blocking_result_unwrapped(
        self, tmp_state_db, no_ntfy_topic, with_github_token
    ):
        """An ERROR / BLOCKED bare-string response must pass through
        without the nudge being prepended — the M2 regression class.
        """
        result = execution._dispatch_or_block(
            async_fn=lambda d: "ASYNC",
            blocking_fn=lambda: "ERROR: target repo not verified",
        )
        # Bare string is returned verbatim; no nudge attachment.
        assert result == "ERROR: target repo not verified"
        assert "ntfy" not in result.lower()

    def test_daemon_health_called_exactly_once_on_async_path(
        self, tmp_state_db, with_ntfy_topic, with_github_token, monkeypatch
    ):
        """PR #64 reviewer M1: one ``_daemon_health`` call per dispatcher
        entry, not two."""
        _seed_daemon_lock()
        calls: list[int] = []
        original = execution._daemon_health

        def counting_health():
            calls.append(1)
            return original()

        monkeypatch.setattr(execution, "_daemon_health", counting_health)
        execution._dispatch_or_block(
            async_fn=lambda d: "ASYNC",
            blocking_fn=lambda: "B",
        )
        assert sum(calls) == 1


# --------------------------- spawn_unit (dispatcher) ---------------------------


class TestSpawnUnitDispatcher:
    """``spawn_unit`` is now a thin dispatcher; the old behaviour lives
    on as ``spawn_unit_blocking``."""

    def test_ntfy_plus_daemon_routes_to_async(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _setup_feature()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        out = execution.spawn_unit("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["session_id"] == "sess-coder"
        assert parsed["status"] == "coding"
        # coder spawn_async submission landed.
        assert any(s["op"] == "spawn_async" for s in workers["coder"].submissions)

    def test_ntfy_unset_routes_to_blocking(
        self, tmp_state_db, with_github_token, no_ntfy_topic, monkeypatch
    ):
        _setup_feature()
        # Blocking path uses ManagedAgentWorker(role='coder').spawn — patch.
        fake_spawn_calls: list[Any] = []

        class _BlockingCoder:
            def __init__(self, *a, **k):
                pass

            def spawn(self, task: str, *, title: str | None = None):
                fake_spawn_calls.append(task)
                return ("sess-block", "PR_URL: https://github.com/o/r/pull/8")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _BlockingCoder)
        _stub_github(monkeypatch)
        out = execution.spawn_unit("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["pr_number"] == 8
        assert fake_spawn_calls, "blocking spawn was not called when daemon is down"

    def test_async_returns_under_3s(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        """Spec § Acceptance 1: ``spawn_unit`` returns in ≤3 s under
        NTFY+daemon."""
        _setup_feature()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        start = time.monotonic()
        execution.spawn_unit("F-001", "F-016-U-8-T")
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"spawn_unit took {elapsed:.2f}s; acceptance: ≤3 s under NTFY+daemon"

    def test_blocking_variant_still_callable_explicitly(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        """``spawn_unit_blocking`` MUST stay reachable even when the
        default would have flipped to async — operator opt-out."""
        _setup_feature()
        _seed_daemon_lock()

        class _BlockingCoder:
            def __init__(self, *a, **k):
                pass

            def spawn(self, task: str, *, title: str | None = None):
                return ("sess-block", "PR_URL: https://github.com/o/r/pull/9")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _BlockingCoder)
        _stub_github(monkeypatch)
        out = execution.spawn_unit_blocking("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["pr_number"] == 9


# --------------------------- send_to_unit (dispatcher) ---------------------------


class TestSendToUnitDispatcher:
    def test_ntfy_plus_daemon_routes_to_async(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        out = execution.send_to_unit("F-016-U-8-T", "coder", "say hi")
        parsed = json.loads(out)
        # send_to_unit_async return shape.
        assert parsed["delivered"] is True
        assert parsed["role"] == "coder"
        assert any(s["op"] == "resume_async" for s in workers["coder"].submissions)

    def test_blocking_branch_used_when_ntfy_unset(
        self, tmp_state_db, with_github_token, no_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()

        # Blocking calls ManagedAgentWorker(role=...).resume(...).
        class _Blocking:
            def __init__(self, *a, **k):
                pass

            def resume(self, session_id: str, msg: str):
                return "FIX_PUSHED"

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _Blocking)
        _stub_github(monkeypatch)
        out = execution.send_to_unit("F-016-U-8-T", "coder", "say hi")
        # Blocking returns the worker's response string verbatim.
        assert "FIX_PUSHED" in out

    def test_blocking_variant_still_callable(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()

        class _Blocking:
            def __init__(self, *a, **k):
                pass

            def resume(self, session_id: str, msg: str):
                return "FIX_PUSHED"

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _Blocking)
        _stub_github(monkeypatch)
        out = execution.send_to_unit_blocking("F-016-U-8-T", "coder", "say hi")
        assert "FIX_PUSHED" in out


# --------------------------- address_review ---------------------------


class TestAddressReviewAsync:
    """New address_review_async via worker.resume_async."""

    def test_returns_fast(self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch):
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        start = time.monotonic()
        execution.address_review_async("F-016-U-8-T", "tester", "fix the test")
        elapsed = time.monotonic() - start
        assert elapsed < 3.0

    def test_persists_status_flip_and_session(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        out = execution.address_review_async("F-016-U-8-T", "tester", "fix the test")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["session_id"] == "sesn-c"
        # The fix prompt was submitted via resume_async on the coder
        # session — that's the spec's "via worker.resume_async" wording.
        assert any(
            s["op"] == "resume_async" and s["session_id"] == "sesn-c"
            for s in workers["coder"].submissions
        )
        # Status flipped to ``fixing`` and review_round bumped — the
        # lead can be killed at any instant and the state-machine row
        # reflects the in-flight fix.
        latest = state.get_unit_state("F-016-U-8-T")
        assert latest.status == "fixing"
        assert latest.review_round == 1

    def test_merge_source_does_not_bump_review_round(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        execution.address_review_async("F-016-U-8-T", "merge", "rebase against main")
        latest = state.get_unit_state("F-016-U-8-T")
        # F-018: ``merge`` source uses conflict_fix_attempts, not cap-3.
        assert latest.review_round == 0

    def test_unknown_source_is_structured_error(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        out = execution.address_review_async("F-016-U-8-T", "bogus", "fix")
        assert "source must be" in out

    def test_killed_lead_does_not_strand_unit(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        """Acceptance 2: a lead killed right after the async dispatch
        does not strand the unit. The session_id + status flip MUST be
        on disk before the tool returns so a fresh lead / the daemon
        can pick up exactly where the dispatch left off.
        """
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        out = execution.address_review_async("F-016-U-8-T", "tester", "fix")
        parsed = json.loads(out)
        # Simulate "lead killed" by re-reading state cold.
        del parsed
        recovered = state.get_unit_state("F-016-U-8-T")
        # Row is on disk; the in-flight coder is reachable via
        # ``coder_session_id`` and the recovery flow.
        assert recovered.coder_session_id == "sesn-c"
        assert recovered.status == "fixing"


class TestAddressReviewDispatcher:
    def test_ntfy_plus_daemon_routes_to_async(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        # blocking path uses ManagedAgentWorker so failure would surface
        out = execution.address_review("F-016-U-8-T", "tester", "fix")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert any(s["op"] == "resume_async" for s in workers["coder"].submissions)

    def test_unset_ntfy_routes_to_blocking(
        self, tmp_state_db, with_github_token, no_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()

        class _Block:
            def __init__(self, *a, **k):
                pass

            def resume(self, session_id: str, msg: str):
                return "FIX_PUSHED"

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _Block)
        _stub_github(monkeypatch)
        out = execution.address_review("F-016-U-8-T", "tester", "fix")
        parsed = json.loads(out)
        assert parsed["outcome"] == "FIX_PUSHED"


# --------------------------- spawn_tester ---------------------------


class TestSpawnTesterAsync:
    def test_returns_fast_and_persists_session(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        start = time.monotonic()
        out = execution.spawn_tester_async("F-001", "F-016-U-8-T")
        elapsed = time.monotonic() - start
        assert elapsed < 3.0
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["role"] == "tester"
        assert parsed["session_id"] == "sess-tester"
        # spawn_async was the chosen op (no prior tester_session_id).
        assert any(s["op"] == "spawn_async" for s in workers["tester"].submissions)
        # Persisted to state.db so a killed lead can recover.
        latest = state.get_unit_state("F-016-U-8-T")
        assert latest.tester_session_id == "sess-tester"
        assert latest.status == "testing"

    def test_existing_tester_session_uses_resume_async(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        s = state.get_unit_state("F-016-U-8-T")
        s.tester_session_id = "sesn-t-prior"
        state.upsert_unit_state(s)
        workers = _install_fake_async_worker(monkeypatch)
        out = execution.spawn_tester_async("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["session_id"] == "sesn-t-prior"
        # Resume — not spawn — was the op.
        assert any(s["op"] == "resume_async" for s in workers["tester"].submissions)

    def test_blocking_variant_still_works(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        """``spawn_tester_blocking`` MUST still walk the synchronous
        path with CI check."""
        _seed_coded_unit()
        _seed_daemon_lock()

        class _Block:
            def __init__(self, *a, **k):
                pass

            def spawn(self, task: str, *, title: str | None = None):
                return ("sess-block-t", "TESTS_PASS")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _Block)
        _stub_github(monkeypatch)
        out = execution.spawn_tester_blocking("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["outcome"] == "TESTS_PASS"


class TestSpawnTesterDispatcher:
    def test_ntfy_plus_daemon_routes_to_async(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        out = execution.spawn_tester("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert any(s["op"] == "spawn_async" for s in workers["tester"].submissions)


# --------------------------- spawn_reviewer ---------------------------


class TestSpawnReviewerAsync:
    def test_returns_fast_and_persists_session(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        start = time.monotonic()
        out = execution.spawn_reviewer_async("F-001", "F-016-U-8-T")
        elapsed = time.monotonic() - start
        assert elapsed < 3.0
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["role"] == "reviewer"
        assert parsed["session_id"] == "sess-reviewer"
        assert any(s["op"] == "spawn_async" for s in workers["reviewer"].submissions)
        latest = state.get_unit_state("F-016-U-8-T")
        assert latest.reviewer_session_id == "sess-reviewer"
        assert latest.status == "reviewing"

    def test_blocking_variant_still_works(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()

        class _Block:
            def __init__(self, *a, **k):
                pass

            def spawn(self, task: str, *, title: str | None = None):
                return ("sess-block-r", "REVIEW_RECOMMEND_MERGE: looks good")

        monkeypatch.setattr("orchestrator.tools.execution.ManagedAgentWorker", _Block)
        _stub_github(monkeypatch)
        out = execution.spawn_reviewer_blocking("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["outcome"] == "REVIEW_RECOMMEND_MERGE"


class TestSpawnReviewerDispatcher:
    def test_ntfy_plus_daemon_routes_to_async(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        out = execution.spawn_reviewer("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert any(s["op"] == "spawn_async" for s in workers["reviewer"].submissions)


# --------------------------- registration ---------------------------


class TestExplicitVariantsAlwaysAvailable:
    """Spec § Phase 6: "Each surface keeps explicit _blocking and
    _async; only the default flips." All seven async/blocking pairs
    must exist on the module."""

    @pytest.mark.parametrize(
        "name",
        [
            "spawn_unit",
            "spawn_unit_async",
            "spawn_unit_blocking",
            "send_to_unit",
            "send_to_unit_async",
            "send_to_unit_blocking",
            "address_review",
            "address_review_async",
            "address_review_blocking",
            "spawn_tester",
            "spawn_tester_async",
            "spawn_tester_blocking",
            "spawn_reviewer",
            "spawn_reviewer_async",
            "spawn_reviewer_blocking",
            "cycle_review",
            "cycle_review_async",
            "cycle_review_blocking",
        ],
    )
    def test_name_is_callable(self, name):
        assert callable(getattr(execution, name)), (
            f"{name} must be importable from orchestrator.tools.execution"
        )


# --------------------------- daemon pickup ---------------------------


@dataclass
class _FakeTailResult:
    status: str = "idle"
    messages: list[dict] = field(default_factory=list)
    reason: str | None = None

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


@dataclass
class _FakeTailWorker:
    role: str = "coder"
    canned: dict[str, _FakeTailResult] = field(default_factory=dict)

    def tail_messages(self, session_id: str, *, limit: int = 50):  # noqa: ARG002
        return self.canned.get(session_id, _FakeTailResult())


class TestDaemonDrivesAsyncDispatchedPhasesToTerminal:
    """Acceptance 1: "Verify the daemon drives each async-dispatched
    phase to terminal from a freshly-seeded row."

    For each ``_async`` surface, the daemon's ``reconcile_unit`` MUST
    observe the resulting marker in the worker's tail and apply the
    per-marker terminal flip — that's the "closes the loop" property
    the spec relies on.

    Out of U-8 scope: cycle-wide drive (tester-pass triggering
    reviewer spawn). The async dispatch closes the SINGLE phase's
    terminal flip; cycle-wide drive ships later.
    """

    def _drive_with_marker(
        self, monkeypatch, unit_id: str, role: str, session_id: str, marker_text: str
    ) -> WorkUnitState | None:
        """Wire a fake tail returning ``marker_text`` for ``session_id`` and tick the daemon."""
        worker = _FakeTailWorker(
            role=role,
            canned={
                session_id: _FakeTailResult(
                    status="idle",
                    messages=[
                        {
                            "ts": "2024-01-01T00:00:00",
                            "role": "agent",
                            "text": marker_text,
                        }
                    ],
                )
            },
        )
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _r: worker)
        # No PR-side F-014 probe — keep marker-only assertion scope.
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [])
        daemon.reconcile_unit(unit_id)
        return state.get_unit_state(unit_id)

    def test_coder_pr_url_after_spawn_unit_async_flips_to_in_ci(self, tmp_state_db, monkeypatch):
        """``spawn_unit_async`` seeds (status=coding, coder_session_id);
        the coder emits PR_URL on the tail → daemon flips ``coding →
        in_ci``."""
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-8-T",
                feature_id="F-001",
                status="coding",
                branch="b",
                coder_session_id="sess-c",
            )
        )
        latest = self._drive_with_marker(
            monkeypatch,
            "F-016-U-8-T",
            "coder",
            "sess-c",
            "PR_URL: https://github.com/o/r/pull/42",
        )
        assert latest is not None
        assert latest.status == "in_ci"

    def test_coder_fix_pushed_after_address_review_async_flips_to_in_ci(
        self, tmp_state_db, monkeypatch
    ):
        """``address_review_async`` seeds (status=fixing); the coder
        emits FIX_PUSHED on the tail → daemon flips ``fixing → in_ci``.
        """
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-8-T",
                feature_id="F-001",
                status="fixing",
                branch="b",
                pr_number=5,
                coder_session_id="sess-c",
                review_round=1,
            )
        )
        latest = self._drive_with_marker(
            monkeypatch, "F-016-U-8-T", "coder", "sess-c", "FIX_PUSHED"
        )
        assert latest is not None
        assert latest.status == "in_ci"

    def test_tester_tests_pass_after_spawn_tester_async_flips_to_in_ci(
        self, tmp_state_db, monkeypatch
    ):
        """``spawn_tester_async`` seeds (status=testing,
        tester_session_id); the tester emits TESTS_PASS on the tail →
        daemon flips ``testing → in_ci``."""
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-8-T",
                feature_id="F-001",
                status="testing",
                branch="b",
                pr_number=5,
                coder_session_id="sess-c",
                tester_session_id="sess-t",
            )
        )
        latest = self._drive_with_marker(
            monkeypatch, "F-016-U-8-T", "tester", "sess-t", "TESTS_PASS"
        )
        assert latest is not None
        assert latest.status == "in_ci"

    def test_reviewer_endorsement_after_spawn_reviewer_async_flips_to_awaiting_merge(
        self, tmp_state_db, monkeypatch
    ):
        """``spawn_reviewer_async`` seeds (status=reviewing,
        reviewer_session_id); the reviewer emits REVIEW_RECOMMEND_MERGE
        on the tail → daemon flips ``reviewing →
        approved_awaiting_merge``."""
        _setup_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="F-016-U-8-T",
                feature_id="F-001",
                status="reviewing",
                branch="b",
                pr_number=5,
                coder_session_id="sess-c",
                reviewer_session_id="sess-r",
            )
        )
        latest = self._drive_with_marker(
            monkeypatch,
            "F-016-U-8-T",
            "reviewer",
            "sess-r",
            "REVIEW_RECOMMEND_MERGE: ship it",
        )
        assert latest is not None
        assert latest.status == "approved_awaiting_merge"


# --------------------------- parallel_units routing ---------------------------


class TestParallelUnitsRoutesPerUnitThroughAsync:
    """Spec § Phase 6 unit description: "parallel_units /
    parallel_units_global (route per-unit through async)".

    Under NTFY+daemon, each unit in the batch must be dispatched via
    the async spawn (≤2 s) — the pre-U-8 ``_run_one`` bailed on "no
    pr_url" which broke that contract. The post-U-8 behavior:
    successful async handoff is a valid intermediate state.
    """

    def test_each_unit_dispatched_async_under_ntfy_plus_daemon(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        # Two units in one feature.
        state.save_feature(
            Feature(
                id="F-001",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan(
            "F-001",
            [
                WorkUnit(id="F-016-U-8-A", feature_id="F-001", title="a", description=""),
                WorkUnit(id="F-016-U-8-B", feature_id="F-001", title="b", description=""),
            ],
        )
        state.approve_plan("F-001")
        _seed_daemon_lock()

        # All async-handoff workers — spawn_async returns session ids.
        coder = _FakeAsyncWorker(role="coder", next_session_id="sess-coder")

        def _fac(role: str):
            return {
                "coder": coder,
                "tester": _FakeAsyncWorker(role="tester", next_session_id="sess-tester"),
                "reviewer": _FakeAsyncWorker(role="reviewer", next_session_id="sess-reviewer"),
            }[role]

        monkeypatch.setattr("orchestrator.tools.execution.make_worker", _fac)

        from orchestrator.tools import scheduling

        out = scheduling.parallel_units("F-001", ["F-016-U-8-A", "F-016-U-8-B"])
        parsed = json.loads(out)
        assert parsed["unit_count"] == 2
        # Both units got past the spawn step (no "no_pr" early-bail).
        # The pre-U-8 _run_one would have returned outcome="no_pr"
        # for both — the post-U-8 contract chains through cycle_review.
        for r in parsed["results"]:
            assert r.get("phase") != "spawn" or r.get("outcome") != "no_pr", (
                f"unit {r['unit_id']} stranded on no_pr early-bail; "
                "U-8 parallel_units must route async handoff through cycle_review"
            )
        # Both coder spawn_async submits landed.
        assert sum(1 for s in coder.submissions if s["op"] == "spawn_async") == 2
