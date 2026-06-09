"""Gap-coverage tests for F-018-U-1.

Complements ``tests/test_f018_u1_tester.py`` (the primary acceptance pin).
Those tests cover the seven Acceptance bullets at one representative
point each; these add the predicates the primary file omits:

  * **Pre-reviewer gate dispatch.** Spec § Acceptance 1 says the
    mergeable check fires at BOTH gates (pre-tester AND pre-reviewer);
    the primary file pins the pre-tester gate's dispatch and counts
    probes at both gates, but does not pin "conflict at pre-reviewer
    gate dispatches a merge fix and then the cycle terminates cleanly".
  * **Repeated conflict loop iteration.** Spec § Acceptance 2 says
    "Mergeable check re-runs; if still conflicted, loop." The primary
    file pins one rebase round; here we pin the iteration — a second
    conflict surfaces after the first rebase, and the loop runs again
    without escalating until either main is clean or cap-3.
  * **Daemon audit trail.** Spec § Acceptance 5's transition
    ``DispatchConflictFix(coder, files)`` is observable via a
    ``coder_resumed`` event with ``source='merge'`` — pinned here so a
    refactor that drops the audit event is caught (the dashboard /
    ``unit_history`` digest relies on it).
  * **Daemon dispatch preconditions.** Same Acceptance 5 — the dispatch
    is implicit on having a ``coder_session_id`` + a ``pr_number``. A
    unit missing either MUST NOT silently transition to ``fixing`` (the
    coder thread couldn't be reached, and a phantom ``fixing`` would
    confuse the dashboard's in-flight count). Pinned here.
  * **WorkUnitState DB round-trip.** Spec § Acceptance 3 — the
    counter has to survive a process restart so the cycle_review gate
    and the daemon agree on the same value. The primary file
    exercises in-process get/increment; here we pin the
    ``upsert``→``get`` round trip.

Spec ref: ``features/F-018/spec.md`` § Acceptance.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from orchestrator import daemon, state
from orchestrator.ci_wait import CIWaitResult
from orchestrator.health import Action, Decision
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import CAP_3, execution

# --------------------------- shared fixtures / helpers ---------------------------


@pytest.fixture(autouse=True)
def _ci_green(monkeypatch):
    """Pretend CI is green so cycle_review tests don't need a CI fixture."""

    def fake_wait(*args, **kwargs):
        return CIWaitResult(status="green", elapsed_seconds=0.0, total_checks=1)

    monkeypatch.setattr("orchestrator.tools.execution.ci_wait.wait_for_ci", fake_wait)


def _seed_unit(
    *,
    unit_id="F-018-U-1",
    feature_id="F-018",
    status="in_ci",
    coder_session="sesn-c",
    pr_number=42,
    branch="f-018-u-1",
    tester_session="",
    reviewer_session="",
):
    """Seed a feature + plan + unit row; mirror of the primary tester's helper."""
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title="t", description="d")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch=branch,
            pr_number=pr_number,
            coder_session_id=coder_session,
            tester_session_id=tester_session,
            reviewer_session_id=reviewer_session,
        )
    )
    return state.get_unit_state(unit_id)


def _conflict_action(files, mergeable_state="dirty"):
    """Shape F-014's decision table emits for ``pr_conflict_detected``."""
    return Action.event(
        "pr_conflict_detected",
        f"PR has conflicts ({mergeable_state})",
        details=f"files: {', '.join(files)}",
        payload={"conflict_files": list(files), "mergeable_state": mergeable_state},
    )


def _stub_probe_sequence(monkeypatch, sequence):
    """Patch the F-014 probe path with a per-call sequence of actions.

    ``sequence`` is a list of ``list[Action]`` — one entry per probe
    call; later calls re-use the last entry.

    Returns the call-counter dict so callers can assert on probe count.
    """
    calls = {"n": 0}

    def fake_load_context(uid):
        return state.get_unit_state(uid), "https://github.com/o/r"

    def fake_probe_and_decide(unit_state, repo_url):  # noqa: ARG001
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        actions = sequence[idx]
        report = type("HR", (), {})()
        return report, Decision(actions_to_apply=list(actions), shadow_decisions=[])

    monkeypatch.setattr("orchestrator.tools.health._load_context", fake_load_context)
    monkeypatch.setattr("orchestrator.tools.health._probe_and_decide", fake_probe_and_decide)
    return calls


def _stub_github(monkeypatch):
    """No-op the github.* helpers cycle_review touches."""
    monkeypatch.setattr("orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_comment_pr", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.tools.execution.safe_submit_pr_review", lambda *a, **k: "")
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_dismiss_own_change_requests", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.request_copilot_review",
        lambda *a, **k: {"requested": True, "status_code": 201, "note": ""},
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.wait_for_copilot_review", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.parse_repo_url",
        lambda url: ("owner", "repo"),
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.github.get_pr_state",
        lambda *a, **k: {"head_sha": "deadbeef", "state": "open", "merged": False},
    )


# ---------------------------------------------------------------------------
# Gap A — pre-reviewer gate dispatch
# ---------------------------------------------------------------------------


class TestPreReviewerGateDispatch:
    """Spec § Acceptance 1 — 'after each CI-green gate (pre-tester AND
    pre-reviewer)'. The primary tester pins the pre-tester gate; this
    pins the pre-reviewer gate.

    Why this matters: a sibling unit can merge between the tester's
    test push and the reviewer phase spawn. If only the pre-tester gate
    runs the probe, that race wouldn't be caught — the reviewer would
    review an unmergeable PR.
    """

    def test_conflict_at_pre_reviewer_gate_dispatches_merge_fix(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit()
        # Probe sequence:
        #   call 1: pre-tester gate (after coder PR push CI green) — clean
        #   call 2: pre-reviewer gate (after tester test push CI green) — CONFLICT
        #   call 3: pre-reviewer gate after rebase + CI re-run — clean
        _stub_probe_sequence(
            monkeypatch,
            [
                [],  # pre-tester gate: clean
                [_conflict_action(["pre_reviewer_sibling.py"])],  # pre-reviewer gate: conflict
                [],  # after rebase: clean
                [],  # subsequent probes: clean
            ],
        )

        dispatched: list[tuple[str, str, str]] = []

        def fake_address_review(uid, src, fb):
            dispatched.append((uid, src, fb))
            return json.dumps({"outcome": "FIX_PUSHED", "cycle": 1, "summary": "rebased"})

        monkeypatch.setattr(execution, "address_review", fake_address_review)
        monkeypatch.setattr(
            execution,
            "spawn_tester",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "TESTS_PASS"}),
        )
        monkeypatch.setattr(
            execution,
            "spawn_reviewer",
            lambda f, u: json.dumps({"unit_id": u, "outcome": "REVIEW_RECOMMEND_MERGE"}),
        )
        _stub_github(monkeypatch)
        monkeypatch.setattr(
            "orchestrator.tools.execution.ntfy.push_ready_to_merge",
            lambda *a, **k: True,
        )

        out = execution.cycle_review("F-018", "F-018-U-1")
        parsed = json.loads(out)
        assert parsed["outcome"] == "approved_awaiting_merge"

        # Exactly one merge-source dispatch (the pre-reviewer gate's
        # conflict triggered it; the pre-tester gate was clean).
        merge_dispatches = [d for d in dispatched if d[1] == "merge"]
        assert len(merge_dispatches) == 1, (
            f"spec § Acceptance 1 — exactly one merge-source dispatch must "
            f"fire (pre-tester clean, pre-reviewer conflict); got "
            f"{len(merge_dispatches)}: {merge_dispatches}"
        )
        # The conflict file list from the pre-reviewer gate reaches the dispatch.
        assert "pre_reviewer_sibling.py" in merge_dispatches[0][2]

        # Counter bumped exactly once (one rebase round happened).
        got = state.get_unit_state("F-018-U-1")
        assert got.conflict_fix_attempts == 1, (
            f"one rebase round (pre-reviewer gate) bumps counter to 1; got "
            f"conflict_fix_attempts={got.conflict_fix_attempts}"
        )


# ---------------------------------------------------------------------------
# Gap B — repeated conflict loop iteration (Acceptance 2 — "loop")
# ---------------------------------------------------------------------------


class TestRepeatedConflictLoopIteration:
    """Spec § Acceptance 2: 'Mergeable check re-runs; if still
    conflicted, loop.' The primary test pins a single round. This pins
    the loop body: two consecutive conflicts (sibling A merges → rebase
    → sibling B merges before CI clears → rebase again) both get
    dispatched without escalating, and the counter ends at 2.

    The whole point of cap-3 is to allow 3 mechanical rebases; the loop
    must actually iterate them.
    """

    def test_two_consecutive_conflicts_both_dispatch(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        _seed_unit()
        # Probe sequence — inside ``_conflict_fix_loop`` for the
        # pre-tester gate:
        #   probe 1: conflict (first sibling lands) → dispatch round 1
        #   probe 2: conflict (second sibling raced the rebase) → dispatch round 2
        #   probe 3: clean → exit loop
        _stub_probe_sequence(
            monkeypatch,
            [
                [_conflict_action(["A.py"])],
                [_conflict_action(["B.py"])],
                [],
            ],
        )

        dispatched_files: list[str] = []

        def fake_address_review(uid, src, fb):  # noqa: ARG001
            assert src == "merge", f"loop must dispatch source='merge', got {src!r}"
            dispatched_files.append(fb)
            return json.dumps({"outcome": "FIX_PUSHED"})

        monkeypatch.setattr(execution, "address_review", fake_address_review)
        _stub_github(monkeypatch)

        ctx = execution.CycleContext(feature_id="F-018", unit_id="F-018-U-1", history=[])
        ok, msg = execution._conflict_fix_loop(ctx, "pre-tester")
        assert ok is True, (
            f"loop must succeed once probe finally returns clean; got ok={ok} msg={msg!r}"
        )

        # Two dispatches happened — one per conflict round.
        assert len(dispatched_files) == 2, (
            f"spec § Acceptance 2 — loop must iterate on repeated "
            f"conflict; got {len(dispatched_files)} dispatch(es)"
        )
        # The two rounds carry the two distinct conflict file lists —
        # round-1 mentions A.py, round-2 mentions B.py. If the impl
        # cached the first conflict result and re-used it, B.py wouldn't
        # appear.
        assert "A.py" in dispatched_files[0]
        assert "B.py" in dispatched_files[1], (
            "second dispatch must carry the SECOND probe's file list, "
            "not the first (no caching of the prior conflict signal)"
        )

        # Counter ended at exactly 2 (two rebase rounds, no cap hit).
        got = state.get_unit_state("F-018-U-1")
        assert got.conflict_fix_attempts == 2, (
            f"counter must reflect two rebase rounds; got "
            f"conflict_fix_attempts={got.conflict_fix_attempts}"
        )
        # Cap was not breached; status is still in_ci (loop succeeded).
        assert got.status != "escalated"

    def test_loop_exits_clean_after_one_successful_rebase(
        self, tmp_state_db, with_github_token, monkeypatch
    ):
        """Lower-bound iteration: conflict → rebase → clean. Pins the
        common case shape (counter == 1, dispatched once)."""
        _seed_unit()
        _stub_probe_sequence(
            monkeypatch,
            [
                [_conflict_action(["only.py"])],
                [],  # clean after rebase
            ],
        )
        dispatched: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            execution,
            "address_review",
            lambda uid, src, fb: (
                dispatched.append((uid, src, fb)) or json.dumps({"outcome": "FIX_PUSHED"})
            ),
        )
        _stub_github(monkeypatch)

        ctx = execution.CycleContext(feature_id="F-018", unit_id="F-018-U-1", history=[])
        ok, msg = execution._conflict_fix_loop(ctx, "pre-tester")
        assert ok is True, f"happy path must succeed; got ok={ok} msg={msg!r}"
        assert len(dispatched) == 1
        assert state.get_unit_state("F-018-U-1").conflict_fix_attempts == 1


# ---------------------------------------------------------------------------
# Gap C + D — daemon dispatch: audit event + missing-precondition refusal
# ---------------------------------------------------------------------------


class _CapturingWorker:
    """Captures ``resume_async`` calls; supplies stub ``tail_messages``."""

    def __init__(self):
        self.resume_async_calls: list[tuple[str, str]] = []

    def resume_async(self, session_id, msg):
        self.resume_async_calls.append((session_id, msg))

    def tail_messages(self, session_id, *, limit=50):  # noqa: ARG002
        return {"status": "idle", "messages": []}


def _seed_awaiting_merge(
    *,
    unit_id="U-AM",
    feature_id="F-AM",
    coder_session="sesn-c",
    pr_number=9,
    conflict_attempts=0,
):
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
            status="approved",
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title="u", description="d")],
    )
    state.approve_plan(feature_id)
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status="approved_awaiting_merge",
            branch="feat/x",
            pr_number=pr_number,
            coder_session_id=coder_session,
        )
    )
    for _ in range(conflict_attempts):
        state.increment_conflict_fix_attempts(unit_id)
    return state.get_unit_state(unit_id)


class TestDaemonAuditTrail:
    """Spec § Acceptance 5 + standard event-trail discipline — the
    daemon's ``DispatchConflictFix`` is observable via a ``coder_resumed``
    event with ``source='merge'``. The dashboard / ``unit_history``
    digest reads these events; dropping the audit trail blinds them.
    """

    def test_daemon_dispatch_emits_coder_resumed_with_merge_source(self, tmp_state_db, monkeypatch):
        unit = _seed_awaiting_merge()
        worker = _CapturingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)
        monkeypatch.setattr(
            daemon,
            "_probe_and_decide_unit",
            lambda _u: [_conflict_action(["audit.py", "trail.py"])],
        )

        daemon.reconcile_unit(unit.unit_id)

        events = state.list_events(unit.unit_id)
        coder_resumed = [e for e in events if e["event_type"] == "coder_resumed"]
        assert len(coder_resumed) == 1, (
            f"daemon dispatch must record exactly one coder_resumed event "
            f"(audit trail for the conflict-fix); got {len(coder_resumed)} "
            f"matching events"
        )
        assert coder_resumed[0]["source"] == "merge", (
            f"spec § Acceptance 5 — the daemon's dispatch is the F-018 "
            f"'merge' source; got source={coder_resumed[0]['source']!r}"
        )
        # The conflict file list is in the details payload so the
        # dashboard / unit_history digest can surface it.
        details = json.loads(coder_resumed[0]["details"])
        assert details.get("conflict_files") == ["audit.py", "trail.py"], (
            f"daemon coder_resumed event must carry the conflict file list "
            f"in details; got {details!r}"
        )


class TestDaemonDispatchPreconditions:
    """Defensive: the daemon's ``_dispatch_conflict_fix`` requires a
    reachable coder session + a PR. A unit missing either MUST NOT
    transition to ``fixing`` — that would silently break the dashboard's
    "in-flight" count and leave a phantom unit nobody is working on.
    """

    def test_missing_coder_session_does_not_flip_status(self, tmp_state_db, monkeypatch):
        """No ``coder_session_id`` → no thread to resume → no transition.
        The unit stays put for human triage.
        """
        unit = _seed_awaiting_merge(coder_session="")
        worker = _CapturingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)
        monkeypatch.setattr(
            daemon,
            "_probe_and_decide_unit",
            lambda _u: [_conflict_action(["x.py"])],
        )

        daemon.reconcile_unit(unit.unit_id)

        got = state.get_unit_state(unit.unit_id)
        # Status must stay at approved_awaiting_merge — no phantom 'fixing'.
        assert got.status == "approved_awaiting_merge", (
            f"daemon must not flip awaiting_merge → fixing when the coder "
            f"session is missing; got status={got.status!r}"
        )
        # Counter must not bump — there was no actual dispatch.
        assert got.conflict_fix_attempts == 0, (
            f"counter must not advance when dispatch refused; got "
            f"conflict_fix_attempts={got.conflict_fix_attempts}"
        )
        # No worker call.
        assert worker.resume_async_calls == []

    def test_missing_pr_number_does_not_flip_status(self, tmp_state_db, monkeypatch):
        """No ``pr_number`` → ``compose_fix_task`` has no PR to reference
        and the GitHub side of the rebase wouldn't surface anywhere. Same
        refusal posture as missing coder session."""
        # Seed an awaiting_merge unit with pr_number explicitly None.
        state.save_feature(
            Feature(
                id="F-NOPR",
                title="t",
                description="d",
                repo_path="https://github.com/o/r",
                status="approved",
            )
        )
        state.save_plan(
            "F-NOPR",
            [WorkUnit(id="U-NOPR", feature_id="F-NOPR", title="u", description="d")],
        )
        state.approve_plan("F-NOPR")
        state.upsert_unit_state(
            WorkUnitState(
                unit_id="U-NOPR",
                feature_id="F-NOPR",
                status="approved_awaiting_merge",
                branch="feat/x",
                pr_number=None,
                coder_session_id="sesn-c",
            )
        )
        worker = _CapturingWorker()
        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda _role: worker)
        monkeypatch.setattr(
            daemon,
            "_probe_and_decide_unit",
            lambda _u: [_conflict_action(["foo.py"])],
        )

        daemon.reconcile_unit("U-NOPR")

        got = state.get_unit_state("U-NOPR")
        assert got.status == "approved_awaiting_merge"
        assert got.conflict_fix_attempts == 0
        assert worker.resume_async_calls == []


# ---------------------------------------------------------------------------
# Gap E — WorkUnitState DB round-trip
# ---------------------------------------------------------------------------


class TestConflictCounterRoundTrip:
    """Spec § Acceptance 3 — the counter must survive ``state.db``
    persistence so the cycle_review gate (sync, in-process) and the
    daemon (separate process) agree on the same value. The primary
    file's in-process tests share a connection; here we pin the
    "store → re-open → read" path the daemon relies on.
    """

    def test_increment_is_visible_via_fresh_get_unit_state(self, tmp_state_db):
        """The counter is written through to ``work_units`` and a fresh
        ``get_unit_state`` (fresh row read) returns the bumped value.
        Not just an in-memory mutation on the WorkUnitState object."""
        _seed_unit()
        n = state.increment_conflict_fix_attempts("F-018-U-1")
        assert n == 1

        # A fresh fetch must show the same value — proving the bump hit
        # the row, not just an in-process cache.
        got = state.get_unit_state("F-018-U-1")
        assert got.conflict_fix_attempts == 1

    def test_counter_survives_raw_sqlite_round_trip(self, tmp_state_db):
        """The strongest "persistent" check: read the raw column via
        sqlite3 directly. Catches a future regression where the column
        becomes a view / derived field rather than a persisted integer.
        """
        _seed_unit()
        state.increment_conflict_fix_attempts("F-018-U-1")
        state.increment_conflict_fix_attempts("F-018-U-1")

        with sqlite3.connect(tmp_state_db) as conn:
            row = conn.execute(
                "SELECT conflict_fix_attempts FROM work_units WHERE unit_id = ?",
                ("F-018-U-1",),
            ).fetchone()
        assert row is not None and row[0] == 2, (
            f"persisted column must reflect the two increments; raw row = {row}"
        )

    def test_workunitstate_dataclass_field_is_persisted_on_reads(self, tmp_state_db):
        """``WorkUnitState`` exposes a ``conflict_fix_attempts`` attribute
        that ``get_unit_state`` populates from the row. If the dataclass
        forgets the field, hydration would either raise (missing arg) or
        silently drop the value — pin both directions."""
        _seed_unit()
        state.increment_conflict_fix_attempts("F-018-U-1")
        state.increment_conflict_fix_attempts("F-018-U-1")
        state.increment_conflict_fix_attempts("F-018-U-1")

        got = state.get_unit_state("F-018-U-1")
        # The model exposes the field as a typed int.
        assert hasattr(got, "conflict_fix_attempts"), (
            "WorkUnitState must expose conflict_fix_attempts for the daemon "
            "to read the counter without raw SQL (spec § Acceptance 3)"
        )
        assert isinstance(got.conflict_fix_attempts, int)
        assert got.conflict_fix_attempts == 3

    def test_get_conflict_fix_attempts_helper_matches_field(self, tmp_state_db):
        """The dedicated ``get_conflict_fix_attempts`` helper returns
        the same value as the model field. Both surfaces are documented
        in the implementation; pinning the parity prevents a future
        split (one updated, the other stale)."""
        _seed_unit()
        state.increment_conflict_fix_attempts("F-018-U-1")
        helper_val = state.get_conflict_fix_attempts("F-018-U-1")
        field_val = state.get_unit_state("F-018-U-1").conflict_fix_attempts
        assert helper_val == field_val == 1


# ---------------------------------------------------------------------------
# Cap-3 + counter — interplay (extra pinning around boundary conditions)
# ---------------------------------------------------------------------------


class TestCounterBoundariesAndCapInterplay:
    """A few extra pin-the-boundary tests around CAP_3 + counter interplay
    that the primary file doesn't explicitly cover.
    """

    def test_get_conflict_fix_attempts_for_missing_unit_returns_zero(self, tmp_state_db):
        """Defensive — a daemon tick fetching a unit-id that was just
        deleted (race) must read ``0`` rather than crash. The
        implementation says so explicitly; pin it."""
        n = state.get_conflict_fix_attempts("U-DOES-NOT-EXIST")
        assert n == 0, f"missing unit must read 0; got {n}"

    def test_increment_returns_new_value_each_call(self, tmp_state_db):
        """The return value of ``increment_conflict_fix_attempts`` is what
        the caller branches on for cap-check. A regression that returns
        the prior value (off-by-one) would let the loop run one extra
        round."""
        _seed_unit()
        assert state.increment_conflict_fix_attempts("F-018-U-1") == 1
        assert state.increment_conflict_fix_attempts("F-018-U-1") == 2
        assert state.increment_conflict_fix_attempts("F-018-U-1") == 3
        # One past the cap; the cycle_review loop won't call increment
        # again (it short-circuits on the cap check), but the helper
        # itself doesn't enforce the cap — just keeps counting.
        assert state.increment_conflict_fix_attempts("F-018-U-1") == 4

    def test_cap_constant_is_three(self):
        """Sanity: F-018 says cap-3. The primary test references CAP_3 from
        the tools package; we pin the literal value here so a future refactor
        that bumps to CAP_5 in ``tools.__init__`` but forgets the F-018 spec
        is caught."""
        assert CAP_3 == 3
