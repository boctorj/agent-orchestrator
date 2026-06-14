"""F-016-U-8 — tester-authored tests for uniform non-blocking dispatch.

Complements ``tests/test_f016_u8_spec.py`` (coder-authored spec tests) by
covering the acceptance gaps the spec file leaves unchecked:

* **Acceptance #1 (timing) across ALL seven surfaces.** The spec file
  pins ≤3 s for ``spawn_unit`` / ``address_review_async`` /
  ``spawn_tester_async`` / ``spawn_reviewer_async`` — these tests fill in
  ``cycle_review`` / ``send_to_unit`` / ``parallel_units`` /
  ``parallel_units_global`` so every surface spec § Acceptance 1 lists is
  exercised by a timer.

* **Acceptance #2 (killed-lead doesn't strand)** for the *spawn* surfaces.
  ``test_f016_u8_spec.py`` covers ``address_review_async`` only; here we
  cover ``spawn_unit_async`` / ``spawn_tester_async`` /
  ``spawn_reviewer_async`` — the session_id MUST be on disk before each
  surface returns, so a fresh lead (or the daemon) can pick up the
  in-flight worker.

* **Dispatcher gate — invalid_heartbeat nudge variant.** Predecessor U-7
  shipped a distinct nudge for the corrupted-heartbeat case (`state.py`
  refuses takeover on unparseable rows). The U-8 ``_dispatch_or_block``
  helper MUST surface that variant via ``_daemon_down_nudge_for``; spec
  tests only check the generic "daemon down" wording.

* **``parallel_units_global``** — the spec file covers per-unit async
  routing for ``parallel_units`` but not ``parallel_units_global``,
  which the spec lists separately in Acceptance #1.

* **``_run_one`` chains async-handoff to ``cycle_review``** — the spec
  test asserts ``phase != 'spawn' or outcome != 'no_pr'`` (negation), but
  doesn't verify ``cycle_review`` is actually invoked. Here we wire a
  counting fake so the chain is observable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from orchestrator import state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution

# --------------------------- shared fixtures (mirrors spec file) ---------------------------


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


@dataclass
class _FakeAsyncWorker:
    """Bookkeeping double for the async worker primitives.

    Mirrors the spec test file's helper so this module is self-contained.
    """

    role: str = "coder"
    next_session_id: str = "sess-new"
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.submissions.append({"op": "spawn_async", "task": task, "title": title})
        return self.next_session_id

    def resume_async(self, session_id: str, msg: str) -> None:
        self.submissions.append({"op": "resume_async", "session_id": session_id, "msg": msg})


def _install_fake_async_worker(monkeypatch) -> dict[str, _FakeAsyncWorker]:
    workers: dict[str, _FakeAsyncWorker] = {
        "coder": _FakeAsyncWorker(role="coder", next_session_id="sess-coder"),
        "tester": _FakeAsyncWorker(role="tester", next_session_id="sess-tester"),
        "reviewer": _FakeAsyncWorker(role="reviewer", next_session_id="sess-reviewer"),
    }
    monkeypatch.setattr("orchestrator.tools.execution.make_worker", lambda role: workers[role])
    return workers


# --------------------------- ≤3 s timing for the remaining surfaces ---------------------------


class TestSurfaceTimingUnderNtfyPlusDaemon:
    """Spec § Acceptance 1: ALL of ``spawn_unit`` / ``address_review`` /
    ``spawn_tester`` / ``spawn_reviewer`` / ``send_to_unit`` /
    ``cycle_review`` / ``parallel_units`` / ``parallel_units_global``
    return in ≤3 s under NTFY+daemon.

    The spec-test file covers ``spawn_unit``, ``address_review_async``,
    ``spawn_tester_async`` and ``spawn_reviewer_async``. The four
    surfaces here close out the acceptance list.
    """

    def test_cycle_review_returns_under_3s(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        start = time.monotonic()
        out = execution.cycle_review("F-001", "F-016-U-8-T")
        elapsed = time.monotonic() - start
        # Under NTFY+daemon, cycle_review hands off via the async impl
        # which returns ``delivered: true`` without blocking on a tick.
        parsed = json.loads(out)
        assert parsed.get("delivered") is True, (
            f"cycle_review should have handed off async under NTFY+daemon; got {out[:200]}"
        )
        assert elapsed < 3.0, f"cycle_review took {elapsed:.2f}s; acceptance ≤3 s"

    def test_send_to_unit_returns_under_3s(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        start = time.monotonic()
        out = execution.send_to_unit("F-016-U-8-T", "coder", "ping")
        elapsed = time.monotonic() - start
        parsed = json.loads(out)
        # Async path returns ``delivered: true``.
        assert parsed.get("delivered") is True, (
            f"send_to_unit should have routed to send_to_unit_async; got {out[:200]}"
        )
        assert elapsed < 3.0, f"send_to_unit took {elapsed:.2f}s; acceptance ≤3 s"

    def test_parallel_units_returns_under_3s_for_three_units(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        """``parallel_units`` over N units must still return ≤3 s when
        each unit's ``spawn_unit`` + ``cycle_review`` hands off async."""
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
                WorkUnit(id=f"F-016-U-8-{tag}", feature_id="F-001", title=tag, description="")
                for tag in ("A", "B", "C")
            ],
        )
        state.approve_plan("F-001")
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)

        from orchestrator.tools import scheduling

        start = time.monotonic()
        out = scheduling.parallel_units("F-001", ["F-016-U-8-A", "F-016-U-8-B", "F-016-U-8-C"])
        elapsed = time.monotonic() - start
        parsed = json.loads(out)
        assert parsed["unit_count"] == 3
        assert elapsed < 3.0, (
            f"parallel_units(3 units) took {elapsed:.2f}s; acceptance ≤3 s under NTFY+daemon"
        )

    def test_parallel_units_global_returns_under_3s_for_three_units(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        """``parallel_units_global`` — the cross-feature surface — is
        listed alongside ``parallel_units`` in Acceptance #1. Same
        timing bound."""
        # Two features, two units / one unit.
        for fid in ("F-001", "F-002"):
            state.save_feature(
                Feature(
                    id=fid,
                    title="t",
                    description="d",
                    repo_path="https://github.com/o/r",
                    status="approved",
                )
            )
        state.save_plan(
            "F-001",
            [
                WorkUnit(id=f"{fid}-A", feature_id=fid, title="a", description="")
                for fid in ("F-001",)
            ]
            + [
                WorkUnit(id=f"{fid}-B", feature_id=fid, title="b", description="")
                for fid in ("F-001",)
            ],
        )
        state.save_plan(
            "F-002",
            [WorkUnit(id="F-002-A", feature_id="F-002", title="a", description="")],
        )
        state.approve_plan("F-001")
        state.approve_plan("F-002")
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)

        from orchestrator.tools import scheduling

        refs = [
            {"feature_id": "F-001", "unit_id": "F-001-A"},
            {"feature_id": "F-001", "unit_id": "F-001-B"},
            {"feature_id": "F-002", "unit_id": "F-002-A"},
        ]
        start = time.monotonic()
        out = scheduling.parallel_units_global(refs)
        elapsed = time.monotonic() - start
        parsed = json.loads(out)
        assert parsed["unit_count"] == 3
        assert elapsed < 3.0, f"parallel_units_global(3 units) took {elapsed:.2f}s; acceptance ≤3 s"


# --------------------------- Acceptance #2: killed-lead doesn't strand ---------------------------


class TestKilledLeadDoesNotStrandUnit:
    """Spec § Acceptance 2 + unit description: "a lead killed right after
    any async dispatch does not strand the unit".

    The post-dispatch session_id MUST be written to ``state.db`` before
    each ``*_async`` surface returns — otherwise a kill in the
    return-path window leaves a ghost row the daemon can't recover.

    ``test_f016_u8_spec.py`` covers this for ``address_review_async``;
    these tests cover the three spawn surfaces.
    """

    def test_spawn_unit_async_persists_coder_session_before_return(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _setup_feature()
        _seed_daemon_lock()
        workers = _install_fake_async_worker(monkeypatch)
        out = execution.spawn_unit_async("F-001", "F-016-U-8-T")
        parsed = json.loads(out)
        assert parsed["delivered"] is True
        assert parsed["session_id"] == "sess-coder"
        # Re-read cold: simulate a fresh-lead process opening the same
        # state.db. The row MUST carry the session_id.
        recovered = state.get_unit_state("F-016-U-8-T")
        assert recovered is not None
        assert recovered.coder_session_id == "sess-coder", (
            "spawn_unit_async returned without persisting coder_session_id — "
            "a killed lead would strand the in-flight worker (ghost row)."
        )
        assert recovered.status == "coding"
        # Sanity: the worker was actually submitted.
        assert any(s["op"] == "spawn_async" for s in workers["coder"].submissions)

    def test_spawn_tester_async_persists_tester_session_before_return(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        execution.spawn_tester_async("F-001", "F-016-U-8-T")
        # Cold re-read.
        recovered = state.get_unit_state("F-016-U-8-T")
        assert recovered is not None
        assert recovered.tester_session_id == "sess-tester", (
            "spawn_tester_async returned without persisting tester_session_id — "
            "a killed lead would strand the in-flight tester."
        )
        assert recovered.status == "testing"

    def test_spawn_reviewer_async_persists_reviewer_session_before_return(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _seed_coded_unit()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)
        execution.spawn_reviewer_async("F-001", "F-016-U-8-T")
        recovered = state.get_unit_state("F-016-U-8-T")
        assert recovered is not None
        assert recovered.reviewer_session_id == "sess-reviewer", (
            "spawn_reviewer_async returned without persisting reviewer_session_id."
        )
        assert recovered.status == "reviewing"


# --------------------------- _dispatch_or_block invalid_heartbeat nudge ---------------------------


class TestDispatchOrBlockInvalidHeartbeatNudge:
    """Predecessor F-016-U-7 / U-6 shipped a distinct nudge for the
    ``invalid_heartbeat`` daemon-health reason: a workspace whose
    ``daemon_locks.heartbeat_at`` is unparseable can't be claimed by
    ``orchestrator daemon start`` (``state.claim_daemon_lock`` refuses
    takeover on corrupted rows), so the generic "start the daemon"
    nudge would silently fail. The Phase 6 ``_dispatch_or_block`` MUST
    surface the variant via ``_daemon_down_nudge_for`` — otherwise the
    seven surfaces drift on that edge case.
    """

    def test_invalid_heartbeat_nudge_attached_to_blocking_result(
        self, tmp_state_db, with_ntfy_topic, with_github_token
    ):
        # Seed a daemon_locks row with a malformed heartbeat_at — same
        # technique tests/test_f016_u6_tester.py uses for its
        # ``test_invalid_heartbeat_treated_as_dead``.
        # PR #70 review M2: use ``state._connect`` rather than
        # ``sqlite3.connect`` so the connection is closed on context
        # exit (CONTRIBUTING.md § "Common pitfalls" — ``with
        # sqlite3.Connection as conn`` commits but does not close).
        path = str(Path(state.STATE_DB).resolve())
        with state._connect() as conn:
            conn.execute(
                "INSERT INTO daemon_locks "
                "(state_db_path, holder_id, heartbeat_at, started_at) "
                "VALUES (?, ?, ?, ?)",
                (path, "holder", "not-an-iso-string", "2024-01-01T00:00:00"),
            )

        # Sanity: _daemon_health classifies this as invalid_heartbeat.
        info = execution._daemon_health()
        assert info["reason"] == "invalid_heartbeat", (
            f"prereq: expected invalid_heartbeat, got {info!r}"
        )

        result = execution._dispatch_or_block(
            async_fn=lambda d: "ASYNC",
            blocking_fn=lambda: json.dumps({"outcome": "approved_awaiting_merge"}),
        )
        parsed = json.loads(result)
        # The invalid_heartbeat nudge calls out the manual-recovery
        # step (delete the corrupted row) — distinct from the generic
        # "start the daemon" wording. Both forms agree it's the
        # ``daemon_locks`` row that's broken, not ntfy.
        assert "nudge" in parsed
        nudge = parsed["nudge"]
        # The invalid_heartbeat nudge mentions deleting the row / the
        # heartbeat_at malformation; the generic nudge does not.
        assert (
            "delete" in nudge.lower()
            or "heartbeat_at" in nudge.lower()
            or "corrupted" in nudge.lower()
        ), (
            f"_dispatch_or_block did not surface the invalid_heartbeat-specific "
            f"recovery instruction; nudge text was: {nudge!r}"
        )
        # And it MUST NOT be the bare daemon-down nudge.
        assert nudge != execution._CYCLE_REVIEW_DAEMON_DOWN_NUDGE, (
            "_dispatch_or_block fell back to the generic daemon-down nudge "
            "on the invalid_heartbeat reason — operator would follow advice "
            "that state.claim_daemon_lock then silently rejects."
        )

    def test_invalid_heartbeat_does_not_route_to_async(
        self, tmp_state_db, with_ntfy_topic, with_github_token
    ):
        """Belt-and-braces: a daemon row with a malformed heartbeat is
        ``running=False`` per :func:`_daemon_health`; the dispatcher
        MUST stay on the blocking branch."""
        # PR #70 review M2: use ``state._connect`` rather than
        # ``sqlite3.connect`` — see the sibling test above for the
        # CONTRIBUTING.md citation.
        path = str(Path(state.STATE_DB).resolve())
        with state._connect() as conn:
            conn.execute(
                "INSERT INTO daemon_locks "
                "(state_db_path, holder_id, heartbeat_at, started_at) "
                "VALUES (?, ?, ?, ?)",
                (path, "holder", "garbage", "2024-01-01T00:00:00"),
            )

        calls: list[str] = []
        execution._dispatch_or_block(
            async_fn=lambda d: (calls.append("async"), "ASYNC")[1],
            blocking_fn=lambda: (calls.append("blocking"), json.dumps({"x": 1}))[1],
        )
        assert calls == ["blocking"], (
            f"invalid_heartbeat MUST short-circuit to blocking; got call sequence {calls}"
        )


# --------------------------- parallel_units_global async routing ---------------------------


class TestParallelUnitsGlobalRoutesPerUnitThroughAsync:
    """The spec-test file covers ``parallel_units`` only; spec
    Acceptance #1 names ``parallel_units_global`` separately.

    Each per-unit dispatch must walk ``spawn_unit`` → async handoff →
    ``cycle_review`` → async handoff. Pre-U-8, ``_run_one`` bailed on
    "no_pr" for async-handoff spawns; the post-U-8 contract chains
    through.
    """

    def test_each_unit_dispatched_async_under_ntfy_plus_daemon(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        # Two features, one unit each.
        for fid in ("F-100", "F-101"):
            state.save_feature(
                Feature(
                    id=fid,
                    title="t",
                    description="d",
                    repo_path="https://github.com/o/r",
                    status="approved",
                )
            )
            state.save_plan(
                fid,
                [WorkUnit(id=f"{fid}-A", feature_id=fid, title="a", description="")],
            )
            state.approve_plan(fid)
        _seed_daemon_lock()

        # Shared coder fake captures both spawns.
        coder = _FakeAsyncWorker(role="coder", next_session_id="sess-coder")

        def _fac(role: str):
            return {
                "coder": coder,
                "tester": _FakeAsyncWorker(role="tester", next_session_id="sess-tester"),
                "reviewer": _FakeAsyncWorker(role="reviewer", next_session_id="sess-reviewer"),
            }[role]

        monkeypatch.setattr("orchestrator.tools.execution.make_worker", _fac)

        from orchestrator.tools import scheduling

        refs = [
            {"feature_id": "F-100", "unit_id": "F-100-A"},
            {"feature_id": "F-101", "unit_id": "F-101-A"},
        ]
        out = scheduling.parallel_units_global(refs)
        parsed = json.loads(out)
        assert parsed["unit_count"] == 2
        # Pre-U-8 fingerprint: ``phase: "spawn", outcome: "no_pr"`` would
        # show up here for an async handoff. Reject that explicitly.
        for r in parsed["results"]:
            assert not (r.get("phase") == "spawn" and r.get("outcome") == "no_pr"), (
                f"unit {r['unit_id']} stranded on no_pr early-bail in "
                f"parallel_units_global; got {r!r}"
            )
            # Every unit's spawn was async-mode.
            assert r.get("spawn_mode") == "async_daemon", (
                f"unit {r['unit_id']} spawn_mode != async_daemon: {r!r}"
            )
            assert r.get("spawn_delivered") is True
        # Two coder spawn_async submissions total — one per unit.
        spawn_count = sum(1 for s in coder.submissions if s["op"] == "spawn_async")
        assert spawn_count == 2, (
            f"expected 2 coder spawn_async calls (one per unit), got {spawn_count}"
        )


# --------------------------- _run_one chains async-handoff to cycle_review ---------------------------


class TestRunOneChainsAsyncSpawnIntoCycleReview:
    """The spec test file asserts ``_run_one`` does NOT bail on "no_pr"
    for async-handoff spawns (negation). Verify the positive form: the
    async spawn handoff MUST flow into ``cycle_review`` so the daemon
    takes ownership of the full pipeline.
    """

    def test_async_spawn_handoff_invokes_cycle_review(
        self, tmp_state_db, with_github_token, with_ntfy_topic, monkeypatch
    ):
        _setup_feature()
        _seed_daemon_lock()
        _install_fake_async_worker(monkeypatch)

        # Spy on cycle_review at the scheduling import site.
        cycle_calls: list[tuple[str, str]] = []
        original_cycle = execution.cycle_review

        def counting_cycle(fid: str, uid: str) -> str:
            cycle_calls.append((fid, uid))
            return original_cycle(fid, uid)

        monkeypatch.setattr("orchestrator.tools.scheduling.cycle_review", counting_cycle)

        from orchestrator.tools import scheduling

        result = scheduling._run_one("F-001", "F-016-U-8-T")
        assert cycle_calls == [("F-001", "F-016-U-8-T")], (
            f"async spawn handoff did NOT chain into cycle_review; "
            f"cycle_calls={cycle_calls}, result={result}"
        )
        # And the result carries cycle_mode/cycle_delivered (the U-7
        # contract preserved across U-8) showing the cycle_review call
        # also handed off async.
        assert result.get("spawn_mode") == "async_daemon"
        assert result.get("spawn_delivered") is True
        assert result.get("cycle_delivered") is True or result.get("cycle_mode") == "async_daemon"


# --------------------------- explicit _blocking gating preserved ---------------------------


class TestExplicitBlockingPreservesVerifyGate:
    """U-8 description: "Each surface keeps explicit ``_blocking`` and
    ``_async`` variants". The verify gate is supposed to stay on the
    explicit blocking variant (the engine impl helpers drop it because
    the dispatcher already ran it).

    Tests an explicit ``_blocking`` call against an unverified repo to
    confirm the public wrapper still gates.
    """

    @pytest.mark.parametrize(
        "surface",
        [
            "spawn_unit_blocking",
            "spawn_tester_blocking",
            "spawn_reviewer_blocking",
        ],
    )
    def test_blocking_variant_still_gates_on_verify(
        self, tmp_state_db, with_github_token, no_ntfy_topic, surface
    ):
        # Use a repo the tmp_state_db fixture did NOT pre-verify so the
        # gate fires.
        state.save_feature(
            Feature(
                id="F-XX",
                title="t",
                description="d",
                repo_path="https://github.com/notverified/repo",
                status="approved",
            )
        )
        state.save_plan(
            "F-XX",
            [WorkUnit(id="U-XX", feature_id="F-XX", title="u", description="")],
        )
        state.approve_plan("F-XX")
        fn = getattr(execution, surface)
        out = fn("F-XX", "U-XX")
        # The verify gate returns a bare ``ERROR:`` string — not JSON.
        assert isinstance(out, str)
        assert out.startswith("ERROR") or "not verified" in out.lower(), (
            f"{surface} on unverified repo should hit verify gate; got {out[:200]}"
        )

    def test_address_review_blocking_still_gates_on_verify(
        self, tmp_state_db, with_github_token, no_ntfy_topic
    ):
        # ``address_review_blocking`` takes (unit_id, source, feedback);
        # the verify gate looks up the unit's feature, so we need an
        # unverified-feature unit row.
        state.save_feature(
            Feature(
                id="F-YY",
                title="t",
                description="d",
                repo_path="https://github.com/notverified/repo",
                status="approved",
            )
        )
        state.save_plan(
            "F-YY",
            [WorkUnit(id="U-YY", feature_id="F-YY", title="u", description="")],
        )
        state.approve_plan("F-YY")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U-YY",
                feature_id="F-YY",
                status="in_ci",
                branch="b",
                pr_number=1,
                coder_session_id="sesn-x",
            )
        )
        out = execution.address_review_blocking("U-YY", "tester", "fix")
        assert out.startswith("ERROR") or "not verified" in out.lower(), (
            f"address_review_blocking on unverified repo skipped verify gate: {out[:200]}"
        )
