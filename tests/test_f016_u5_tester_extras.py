"""F-016-U-5 — Phase 3: watcher daemon (extra tester tests).

Independent of ``tests/test_f016_u5_tester.py`` and the coder's own
``tests/test_daemon.py`` + ``tests/test_state.py``. This file pins
spec-acceptance corners those files don't explicitly assert against
``features/F-016/spec.md`` § Phase 3 and the
``docs/PROPOSAL-async-orchestrator.md`` proposal:

  * **The daemon's own docstring contract: "Applying ApplyTesterMarker
    (TESTS_PASS) when the unit is already reviewing is a no-op."**
    (``orchestrator/daemon.py`` lines 21-22, verbatim.) A stale
    terminal marker in a role's session tail must NOT bounce the unit
    backwards on a subsequent tick.

    The Kubernetes-controller pattern the daemon spec hangs on
    (``features/F-016/spec.md`` § "Constraints": "Idempotent
    transitions are load-bearing. The daemon is stateless between
    ticks; duplicate work is deduped by Phase 0's ``dedupe_key``") only
    holds if a marker that has already moved the unit forward in an
    earlier tick is a no-op on every subsequent tick. The current
    implementation's source-status guard is
    ``ACTIVE_UNIT_STATUSES``-wide — it does NOT restrict to the
    per-marker source status the docstring claims — so a tester
    ``TESTS_PASS`` parsed off a tail at status=``reviewing`` flips the
    unit backwards to ``in_ci``. Same shape with the coder's persistent
    ``PR_URL`` / ``FIX_PUSHED`` markers (each tail keeps its history
    indefinitely once the role's session has been touched).

    These tests reproduce the regression both in isolation
    (``_apply_marker_transition`` called with a fresh ``MarkerSpec``)
    and end-to-end (``reconcile_unit`` running over a mid-cycle unit
    with persistent role sessions).

  * **Heartbeat-before-reconcile ordering.** Per the
    ``DaemonLoop.tick`` docstring: "Heartbeats first so a single hung
    ``reconcile_unit`` doesn't starve the lock and provoke a spurious
    takeover." If a future refactor inverts the order, a stuck reconcile
    could lose the lock to a stale-window takeover. Lock the ordering
    in a test.

  * **Falsy ``ORCH_DAEMON_DRIVE`` keeps the daemon out.** Per
    ``daemon.py`` § ``is_drive_enabled`` docstring: "Everything else
    (including absence and the empty string) is False." The unit
    description's "Opt-in via ORCH_DAEMON_DRIVE=true" makes this the
    safety hinge: a typo'd value must NOT silently enable drive.

  * **No-target markers (``BUG_FOUND`` / ``REVIEW_REQUEST_CHANGES``)
    don't even acquire the daemon CAS.** The early-out at
    ``spec.target_status is None`` is BEFORE the
    ``claim_unit_owner`` call — a wasted owner claim on a non-flipping
    marker would briefly clobber a lead's pending advance-lock attempt.

  * **A scope check on the marker grammar.** The unit's spec § "Out of
    scope" says "Replacing the cap-3 contract or the marker grammar."
    The daemon must consume the published markers, not silently shadow
    them with its own table. Assert the daemon's ``ROLES`` matches the
    grammar's known roles 1-1.

  * **Recheck guard between roles drops a tick mid-loop.** Spec § per-
    unit reconcile: "The cancel and lock guards are re-checked between
    stages so a user cancellation mid-tick stops further work
    immediately rather than completing the F-014 probe on a unit the
    user just stopped." A regression that lifts the recheck out of the
    inter-role loop would let a cancelled unit still hit the F-014
    probe phase.

The fixtures borrow the shape of ``test_f016_u5_tester.py``'s helpers
but stay self-contained so a future refactor of either file doesn't
ripple.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from orchestrator import daemon, markers, state
from orchestrator.models import Feature, WorkUnitState

# --------------------------- shared fixtures ---------------------------


@dataclass
class _TailResult:
    """``TailResult`` look-alike acceptable both via ``[]`` and ``.get``."""

    status: str = "idle"
    messages: list[dict] = field(default_factory=list)
    reason: str | None = None

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


def _stub_no_probe(monkeypatch) -> None:
    monkeypatch.setattr(daemon, "_probe_and_decide_unit", lambda _u: [])


def _seed_unit(
    *,
    unit_id: str = "U-X",
    feature_id: str = "F-X",
    status: str = "coding",
    sessions: dict[str, str] | None = None,
    pr_number: int | None = None,
) -> WorkUnitState:
    state.save_feature(
        Feature(
            id=feature_id,
            title="t",
            description="d",
            repo_path="https://github.com/o/r",
        )
    )
    s = sessions or {}
    unit = WorkUnitState(
        unit_id=unit_id,
        feature_id=feature_id,
        status=status,
        coder_session_id=s.get("coder", ""),
        tester_session_id=s.get("tester", ""),
        reviewer_session_id=s.get("reviewer", ""),
        pr_number=pr_number,
    )
    state.upsert_unit_state(unit)
    return unit


# --------------------------- bug-reproduction: stale-marker backflip ---------------------------


class TestStaleMarkerNoBackflip:
    """The daemon's own docstring example (lines 21-22 of ``daemon.py``):

        "Applying ApplyTesterMarker(TESTS_PASS) when the unit is already
        reviewing is a no-op."

    Spec § "Constraints": "Idempotent transitions are load-bearing.
    The daemon is stateless between ticks (Kubernetes-controller
    pattern); duplicate work is deduped by Phase 0's ``dedupe_key`` and
    guarded by the ``owner`` CAS."

    Because role sessions live across the whole unit lifetime, a tester
    that emitted ``TESTS_PASS`` on round 0 leaves the marker in its tail
    permanently. When the unit later progresses to ``reviewing``, the
    daemon's per-tick re-scan of the tester tail must NOT re-apply the
    ``TESTS_PASS`` target_status (``in_ci``) and bounce the unit back.

    Same shape with the coder's ``PR_URL`` / ``FIX_PUSHED`` — both
    persist in the coder's tail for the unit's lifetime.
    """

    def test_tests_pass_marker_on_reviewing_unit_is_noop(self, tmp_state_db):
        """Direct ``_apply_marker_transition`` repro — the documented no-op
        the daemon's docstring promises."""
        unit = _seed_unit(status="reviewing", sessions={"tester": "sess_t"})
        spec = markers.scan_response("tester", "TESTS_PASS")
        assert spec is not None
        assert spec.target_status == "in_ci"
        # Per docstring: applying TESTS_PASS to a reviewing unit is a no-op.
        applied = daemon._apply_marker_transition(unit.unit_id, spec)
        assert applied is False, (
            "stale TESTS_PASS bounced reviewing → in_ci; the daemon's own "
            "docstring guarantees this is a no-op (lines 21-22 of daemon.py)"
        )
        assert state.get_unit_state(unit.unit_id).status == "reviewing"

    def test_pr_url_marker_on_reviewing_unit_is_noop(self, tmp_state_db):
        """The coder's PR_URL is the canonical "always in the tail" marker
        because the coder session is touched first and never archived. A
        unit at ``reviewing`` must not be backflipped to ``in_ci`` because
        the daemon re-scanned a persistent PR_URL on the coder tail."""
        unit = _seed_unit(status="reviewing", sessions={"coder": "sess_c"})
        spec = markers.scan_response("coder", "PR_URL: https://github.com/o/r/pull/3")
        assert spec is not None
        assert spec.target_status == "in_ci"
        applied = daemon._apply_marker_transition(unit.unit_id, spec)
        assert applied is False, (
            "stale PR_URL bounced reviewing → in_ci; spec § 'Idempotent "
            "transitions are load-bearing' — a marker already-acted-on in "
            "an earlier tick must be a no-op on every subsequent tick."
        )
        assert state.get_unit_state(unit.unit_id).status == "reviewing"

    def test_reconcile_unit_does_not_backflip_reviewing_to_in_ci(self, tmp_state_db, monkeypatch):
        """End-to-end: a unit mid-``reviewing`` with PR_URL still in the
        coder tail and TESTS_PASS still in the tester tail must stay at
        ``reviewing`` across reconcile ticks. This is the realistic
        production state (sessions are not archived between phases on
        the success path)."""
        unit = _seed_unit(
            status="reviewing",
            sessions={"coder": "sess_c", "tester": "sess_t", "reviewer": "sess_r"},
        )
        canned = {
            "coder": _TailResult(
                messages=[
                    {
                        "ts": "t",
                        "role": "agent",
                        "text": "PR_URL: https://github.com/o/r/pull/3",
                    }
                ]
            ),
            "tester": _TailResult(messages=[{"ts": "t", "role": "agent", "text": "TESTS_PASS"}]),
            # reviewer mid-review; no terminal marker yet.
            "reviewer": _TailResult(messages=[]),
        }

        class _PerRoleWorker:
            def __init__(self, role: str) -> None:
                self.role = role

            def tail_messages(self, _sid: str, *, limit: int = 50):  # noqa: ARG002
                return canned[self.role]

        monkeypatch.setattr("orchestrator.daemon.make_worker", lambda role: _PerRoleWorker(role))
        _stub_no_probe(monkeypatch)
        # Two ticks — the second should still find the unit at "reviewing".
        daemon.reconcile_unit(unit.unit_id)
        daemon.reconcile_unit(unit.unit_id)
        latest = state.get_unit_state(unit.unit_id)
        assert latest.status == "reviewing", (
            f"daemon backflipped reviewing → {latest.status}; "
            "spec acceptance: 'Idempotent transitions are load-bearing' — "
            "a stale role tail must not bounce a downstream-status unit."
        )

    def test_review_recommend_merge_marker_on_in_ci_unit_is_noop(self, tmp_state_db):
        """Mirror of the same regression in the reviewer slot: the
        reviewer's ``REVIEW_RECOMMEND_MERGE`` targets
        ``approved_awaiting_merge``; applying it to a unit that briefly
        regressed to ``in_ci`` (e.g. the same backflip bug or any other
        round-trip) must be a no-op when the reviewer marker is stale.

        This test is currently expected to PASS — the case is included
        as a contract on the source-status restriction the fix needs to
        respect symmetrically across the three roles."""
        unit = _seed_unit(status="in_ci", sessions={"reviewer": "sess_r"})
        spec = markers.scan_response("reviewer", "REVIEW_RECOMMEND_MERGE: clean")
        assert spec is not None
        assert spec.target_status == "approved_awaiting_merge"
        # Reviewer markers should only flip from "reviewing", not from
        # "in_ci" — applying a stale endorsement to a unit that's now
        # in CI re-run would land the unit in approved_awaiting_merge
        # without the reviewer actually having endorsed the new SHA.
        applied = daemon._apply_marker_transition(unit.unit_id, spec)
        assert applied is False, (
            "stale REVIEW_RECOMMEND_MERGE bounced in_ci → "
            "approved_awaiting_merge; the source-status restriction must "
            "be per-marker, not 'any active status'."
        )
        assert state.get_unit_state(unit.unit_id).status == "in_ci"


# --------------------------- heartbeat-before-reconcile ordering ---------------------------


class TestHeartbeatOrdering:
    """Per ``DaemonLoop.tick`` docstring: 'Heartbeats first so a single
    hung reconcile_unit doesn't starve the lock and provoke a spurious
    takeover.'"""

    def test_heartbeat_is_called_before_reconcile_once(self, tmp_state_db, monkeypatch):
        order: list[str] = []

        def _spy_heartbeat(_handle) -> bool:
            order.append("heartbeat")
            return True

        def _spy_reconcile() -> int:
            order.append("reconcile")
            return 0

        monkeypatch.setattr(daemon, "heartbeat", _spy_heartbeat)
        monkeypatch.setattr(daemon, "reconcile_once", _spy_reconcile)
        loop = daemon.DaemonLoop()
        loop.handle = daemon.claim_singleton(holder_id="us")
        assert loop.handle is not None
        loop.tick()
        assert order == ["heartbeat", "reconcile"], (
            f"expected heartbeat before reconcile_once; got {order}"
        )

    def test_failed_heartbeat_skips_reconcile_entirely(self, tmp_state_db, monkeypatch):
        """If the heartbeat fails (lock taken over), reconcile_once must
        NOT fire — driving state on a unit you no longer own is a
        spec violation of 'owner CAS picks a winner; loser bails'."""
        reconcile_calls = {"n": 0}

        def _bump(*_a, **_k) -> int:
            reconcile_calls["n"] += 1
            return 5

        monkeypatch.setattr(daemon, "heartbeat", lambda _h: False)
        monkeypatch.setattr(daemon, "reconcile_once", _bump)
        loop = daemon.DaemonLoop()
        loop.handle = daemon.claim_singleton(holder_id="us")
        result = loop.tick()
        assert result == 0
        assert reconcile_calls["n"] == 0, (
            "reconcile_once ran after a failed heartbeat — the loser of "
            "the lock takeover must NOT drive state."
        )
        assert loop.is_stopping()


# --------------------------- opt-in safety ---------------------------


class TestOptInSafety:
    """Falsy values for ``ORCH_DAEMON_DRIVE`` must NOT enable drive.

    The unit description makes opt-in the safety hinge: "Opt-in via
    ``ORCH_DAEMON_DRIVE=true``." A typo'd value silently enabling drive
    would re-introduce the very race the lock + opt-in is meant to
    prevent."""

    @pytest.mark.parametrize(
        "val",
        ["false", "0", "no", "off", "", "maybe", "TRue ", " true", "yes\n"],
    )
    def test_falsy_or_quirky_values(self, monkeypatch, val):
        monkeypatch.setenv(daemon.DAEMON_DRIVE_ENV, val)
        expected = val.strip().lower() in ("1", "true", "yes", "on")
        assert daemon.is_drive_enabled() is expected, (
            f"{val!r} parsed to {daemon.is_drive_enabled()}; expected {expected}"
        )


# --------------------------- non-flipping markers don't claim owner ---------------------------


class TestNoTargetMarkerSkipsCAS:
    """``BUG_FOUND`` / ``REVIEW_REQUEST_CHANGES`` carry
    ``target_status=None`` and must short-circuit BEFORE
    ``claim_unit_owner`` runs — otherwise a daemon mid-tick would
    briefly own the unit row and could race a lead's
    ``send_to_unit_async`` advance-lock claim."""

    def test_bug_found_does_not_claim_owner(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="testing")
        spec = markers.scan_response("tester", "BUG_FOUND: divide by zero")
        assert spec is not None and spec.target_status is None
        claim_calls: list[tuple] = []
        orig_claim = state.claim_unit_owner

        def _spy(unit_id, owner, *, expected_owner=""):
            claim_calls.append((unit_id, owner, expected_owner))
            return orig_claim(unit_id, owner, expected_owner=expected_owner)

        monkeypatch.setattr(state, "claim_unit_owner", _spy)
        monkeypatch.setattr(daemon.state, "claim_unit_owner", _spy)
        result = daemon._apply_marker_transition(unit.unit_id, spec)
        assert result is False
        assert claim_calls == [], (
            "BUG_FOUND triggered a no-op owner claim — the target_status "
            "early-out must run BEFORE claim_unit_owner so a lead's "
            "advance-lock isn't briefly clobbered."
        )

    def test_review_request_changes_does_not_claim_owner(self, tmp_state_db, monkeypatch):
        unit = _seed_unit(status="reviewing")
        spec = markers.scan_response("reviewer", "REVIEW_REQUEST_CHANGES: nits")
        assert spec is not None and spec.target_status is None
        claim_calls: list[tuple] = []

        def _spy(*args, **kwargs):
            claim_calls.append((args, kwargs))
            return True

        monkeypatch.setattr(daemon.state, "claim_unit_owner", _spy)
        result = daemon._apply_marker_transition(unit.unit_id, spec)
        assert result is False
        assert claim_calls == []


# --------------------------- marker-grammar scope check ---------------------------


class TestMarkerGrammarScope:
    """Spec § "Out of scope": "Replacing the cap-3 contract or the marker
    grammar." The daemon must consume the published grammar
    (``orchestrator.markers`` known roles), not silently shadow it."""

    def test_daemon_roles_match_marker_known_roles(self):
        # The daemon scans these three roles per tick. If a future
        # marker grammar adds a role and the daemon doesn't, the daemon
        # silently drops the new role's tail — a scope-creep regression
        # the test surfaces immediately.
        from orchestrator.markers import _KNOWN_ROLES

        assert set(daemon.ROLES) == set(_KNOWN_ROLES), (
            f"daemon.ROLES {set(daemon.ROLES)} drifted from "
            f"markers._KNOWN_ROLES {set(_KNOWN_ROLES)}"
        )


# --------------------------- recheck-between-roles guard ---------------------------


class TestRecheckBetweenRoles:
    """Spec § "Pre-conditions checked before any state mutation, per
    tick AND per action": "The cancel and lock guards are re-checked
    between stages so a user cancellation mid-tick stops further work
    immediately rather than completing the F-014 probe on a unit the
    user just stopped." A regression that hoists the recheck out of the
    inter-role loop would let a cancelled unit hit the F-014 probe
    phase anyway."""

    def test_cancel_observed_after_coder_scan_stops_probe(self, tmp_state_db, monkeypatch):
        """Coder scan runs first; if the user's cancel lands between
        coder and tester scans (modeled here by stubbing the coder
        worker to ``state.cancel_unit`` mid-tail), the tester scan AND
        the F-014 probe must both be skipped."""
        unit = _seed_unit(
            status="coding",
            sessions={"coder": "sess_c", "tester": "sess_t"},
            pr_number=42,
        )

        seen_roles: list[str] = []
        probe_calls = {"n": 0}

        def _cancelling_make_worker(role: str):
            seen_roles.append(role)

            class _W:
                def tail_messages(self, _sid: str, *, limit: int = 50):  # noqa: ARG002
                    # On the coder role, simulate a concurrent
                    # ``cancel_unit`` from the lead before we get to the
                    # next role.
                    if role == "coder":
                        state.cancel_unit(unit.unit_id)
                    return _TailResult()

            return _W()

        def _spy_probe(_unit):
            probe_calls["n"] += 1
            return []

        monkeypatch.setattr("orchestrator.daemon.make_worker", _cancelling_make_worker)
        monkeypatch.setattr(daemon, "_probe_and_decide_unit", _spy_probe)
        daemon.reconcile_unit(unit.unit_id)
        # Tester role NOT scanned, F-014 probe NOT run.
        assert "tester" not in seen_roles, (
            f"tester scanned after cancel landed; seen_roles={seen_roles}"
        )
        assert "reviewer" not in seen_roles, (
            f"reviewer scanned after cancel landed; seen_roles={seen_roles}"
        )
        assert probe_calls["n"] == 0, (
            "F-014 probe ran on a cancelled unit; spec § Phase 2.5 sticky cancel violated."
        )


# --------------------------- F-014 engine reuse ---------------------------


class TestF014EngineNotDuplicated:
    """Spec § "Constraints" (3): "No parallel state machine.
    ``cycle_review_blocking`` and the daemon call the *same*
    derive_next_action + execute engine." Locks down that the daemon
    really does delegate, not silently reimplement."""

    def test_daemon_does_not_import_health_decide_directly_in_apply(
        self, tmp_state_db, monkeypatch
    ):
        """The daemon's ``_apply_health_action`` must route through
        ``tools.health._apply_action`` — patching that helper to a
        sentinel proves the daemon actually delegates rather than
        composing its own write path."""
        unit = _seed_unit(status="in_ci", pr_number=42)
        from orchestrator.health import Action
        from orchestrator.tools import health as tools_health

        # Patch the canonical applier with a sentinel; if the daemon
        # implements its own write path, the sentinel never runs and
        # the assertion below fails.
        called: list[tuple] = []
        monkeypatch.setattr(
            tools_health,
            "_apply_action",
            lambda u, a: called.append((u.unit_id, a.kind, a.target_status)),
        )
        result = daemon._apply_health_action(unit, Action.transition("done", "merged sentinel"))
        assert result is True
        assert called == [(unit.unit_id, "transition", "done")], (
            "daemon._apply_health_action did NOT route through "
            "tools.health._apply_action; spec § 'No parallel state machine' "
            "violated."
        )
