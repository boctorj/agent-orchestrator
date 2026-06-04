"""F-016-U-2 — supplemental tester coverage.

Complements ``tests/test_f016_u2_tester.py`` (and the coder's own
``tests/test_f016_u2_non_blocking_spawn.py``) by pinning spec / contract
invariants the existing tests skim past:

  * **Event provenance for ``spawn_coder_async``.** Existing tests assert
    the event is *present*; this file pins its ``source`` /
    ``cycle_number`` / ``feature_id`` columns. A regression that wrote
    ``source="coder"`` (the role) or ``cycle_number=None`` would silently
    poison the audit-trail and the future F-016-U-5 daemon's
    level-triggered reconciler.

  * **Worker call-site arguments.** The existing tests don't validate
    *what* ``worker.spawn_async`` receives. Two failures matter:

      1. ``title=`` must be ``f"{unit_id}: {unit.title}"`` — the
         tail_worker / scan_unit_session F-016-U-1 recovery surface
         relies on the human-readable title for triage.
      2. ``task`` must be the ``compose_coder_task`` output (the
         marker-grammar prompt). A regression that dropped the task
         arg or sent a bare ``unit_id`` would still ``spawn_async``
         a session but produce a coder with no instructions.

  * **``timeout_s`` propagation.** ``wait_unit(unit_id, role, timeout_s=N)``
    must pass ``N`` through to ``worker.wait_idle(..., timeout_seconds=N)``.
    Existing tests record the call but don't assert the number matches —
    a regression silently capping at the worker default (30 min) would
    hang the daemon's adaptive backoff (F-016 Phase 3).

  * **JSON response_tail field.** The PR description fixes the
    ``wait_unit`` JSON shape as carrying ``response_tail`` on both the
    marker-hit and no-marker paths; pin it so a future cleanup doesn't
    silently drop the field other tools (the dashboard) rely on.

  * **``spawn_unit_async`` error path persists ``coder_error``.** The
    impl writes a ``coder_error`` event on ``worker.spawn_async`` failure;
    the existing tests assert the row goes ``escalated`` but don't
    cover the audit-row half — that's what surfaces in
    ``unit_history`` / the dashboard when the user asks why a unit
    blew up.

  * **Predecessor F-016-U-1 ``_KNOWN_ROLES`` consistency.** U-1's cycle
    log records the bug + fix: ``scan_response`` must reject unknown
    roles BEFORE matching ``BLOCKED``-text into a spurious
    ``{role}_blocked`` MarkerSpec. ``wait_unit`` should likewise reject
    an unknown role at the doorstep — before constructing the worker
    OR reading the unit row from state.db — so a corrupted role
    parameter can't probe orchestrator internals.

  * **Out-of-scope guard.** F-016-U-2's spec ``Out of scope`` excludes
    "Replacing the cap-3 contract or the marker grammar". Pin the
    public surface of ``orchestrator.markers`` (``_KNOWN_ROLES``,
    ``scan_response`` signature, dedupe_key signature) as unchanged —
    a scope creep that silently extended the marker grammar would
    break F-016-U-3..U-7 downstream assumptions.

  * **MCP tool docstrings.** FastMCP exposes the python docstring to
    the lead as the tool description. An empty docstring degrades the
    lead's "what does this do?" surface.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from orchestrator import state
from orchestrator.models import Feature, WorkUnit, WorkUnitState
from orchestrator.tools import execution, mcp


# --------------------------- fixtures ---------------------------


@pytest.fixture(autouse=True)
def _bypass_verify_gate(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.execution.ensure_verified_for_feature", lambda _f: None
    )
    monkeypatch.setattr(
        "orchestrator.tools.execution.ensure_verified_for_unit", lambda _u: None
    )


@pytest.fixture(autouse=True)
def _no_github_writes(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.tools.execution.safe_amend_pr_body", lambda *a, **kw: None
    )


@pytest.fixture
def _fake_pat(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_extras")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# Pick distinct ids from the existing tester file so any cross-test
# state leak (shouldn't happen — tmp_state_db isolates the db file)
# surfaces as a clean mismatch rather than silent reuse.
_FID = "F-016-X"
_UID = "F-016-X-U-1"
_UTITLE = "phase-1 dispatcher primitives"


def _seed_feature(feature_id: str = _FID, unit_id: str = _UID, title: str = _UTITLE):
    state.save_feature(
        Feature(
            id=feature_id,
            title="phase-1 extras",
            description="extras",
            repo_path="https://github.com/o/r",
            status="approved",
            branch_prefix="extras",
        )
    )
    state.save_plan(
        feature_id,
        [WorkUnit(id=unit_id, feature_id=feature_id, title=title, description="impl phase-1")],
    )
    state.approve_plan(feature_id)
    return feature_id, unit_id


# --------------------------- worker fakes ---------------------------


class _RecordingWorker:
    """Records full args to every async call so tests can assert
    semantic plumbing — not just call-count."""

    def __init__(
        self,
        role: str,
        *,
        session_id: str = "sesn-extras",
        wait_response: str = "",
        wait_raises: BaseException | None = None,
    ):
        self.role = role
        self._session_id = session_id
        self._wait_response = wait_response
        self._wait_raises = wait_raises
        self.spawn_async_calls: list[dict] = []
        self.wait_idle_calls: list[dict] = []
        self.resume_async_calls: list[dict] = []

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        self.spawn_async_calls.append({"task": task, "title": title})
        return self._session_id

    def wait_idle(self, session_id: str, *, timeout_seconds: int = 1800) -> str:
        self.wait_idle_calls.append(
            {"session_id": session_id, "timeout_seconds": timeout_seconds}
        )
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._wait_response

    def resume_async(self, session_id: str, msg: str) -> None:
        self.resume_async_calls.append({"session_id": session_id, "msg": msg})

    # Blocking surfaces — must never be reached from the async primitives.
    def spawn(self, task, *, title=None):
        raise AssertionError("blocking spawn() must not be called")

    def resume(self, session_id, msg):
        raise AssertionError("blocking resume() must not be called")

    def archive(self, session_id):
        return None


def _install_worker(monkeypatch, worker: _RecordingWorker) -> None:
    monkeypatch.setattr(
        "orchestrator.tools.execution.ManagedAgentWorker", lambda role: worker
    )


# ===========================================================================
# spawn_unit_async — event provenance
# ===========================================================================


class TestSpawnUnitAsyncEventProvenance:
    """``spawn_coder_async`` is the audit row the F-016-U-5 watcher
    daemon's reconciler will key off when triaging an in-flight unit.
    Pin its column shape so a regression doesn't poison the daemon
    once it lands.
    """

    def test_event_recorded_with_source_orchestrator_and_cycle_zero(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        feature_id, unit_id = _seed_feature()
        _install_worker(monkeypatch, _RecordingWorker("coder", session_id="sesn-p1"))

        execution.spawn_unit_async(feature_id, unit_id)

        events = [
            e for e in state.list_events(unit_id) if e["event_type"] == "spawn_coder_async"
        ]
        assert len(events) == 1, f"expected exactly one spawn_coder_async event, got {events}"
        ev = events[0]
        # ``source`` must be "orchestrator" (not the role — the lead's
        # dispatch is the actor, not the worker). The daemon filters
        # orchestrator-originated rows from worker-originated rows.
        assert ev["source"] == "orchestrator", (
            f"spawn_coder_async source must be 'orchestrator', got {ev['source']!r}"
        )
        # Phase-0 dispatch is pre-cycle: the cycle counter advances inside
        # cycle_review / address_review, not on the spawn surface.
        assert ev["cycle_number"] == 0, (
            f"spawn_coder_async cycle_number must be 0, got {ev['cycle_number']!r}"
        )
        # Must cross-reference the feature_id for cross-feature queries.
        assert ev["feature_id"] == feature_id

    def test_failure_records_coder_error_event(self, tmp_state_db, monkeypatch, _fake_pat):
        """The impl writes a ``coder_error`` event on spawn failure so
        ``unit_history`` shows *why* a unit got escalated. Without that,
        the dashboard surfaces an escalated row with no breadcrumb.
        """
        feature_id, unit_id = _seed_feature()

        class _Boom(_RecordingWorker):
            def spawn_async(self, task, *, title=None):  # type: ignore[override]
                raise RuntimeError("anthropic 503")

        _install_worker(monkeypatch, _Boom("coder"))

        result = execution.spawn_unit_async(feature_id, unit_id)
        assert result.startswith("ERROR")

        events = [e for e in state.list_events(unit_id) if e["event_type"] == "coder_error"]
        assert len(events) == 1, f"expected one coder_error event, got {events}"
        assert "anthropic 503" in events[0]["summary"]


# ===========================================================================
# spawn_unit_async — what reaches the worker
# ===========================================================================


class TestSpawnUnitAsyncWorkerArguments:
    """The dispatch tool composes a coder prompt + a human-friendly
    session title, then submits both to ``worker.spawn_async``. Existing
    tests assert the call happened; these assert the *content* matches
    the spec contract.
    """

    def test_title_kwarg_has_unit_id_and_unit_title(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        """``scan_unit_session`` / ``tail_worker`` surface the Anthropic
        title to users triaging an in-flight session — it has to be
        human-recognisable. The contract is ``{unit_id}: {unit.title}``.
        """
        feature_id, unit_id = _seed_feature(title="wire spawn_async into MCP")
        worker = _RecordingWorker("coder", session_id="sesn-title")
        _install_worker(monkeypatch, worker)

        execution.spawn_unit_async(feature_id, unit_id)

        assert len(worker.spawn_async_calls) == 1
        title = worker.spawn_async_calls[0]["title"]
        assert title is not None, "title kwarg must not be None — used by triage tools"
        assert unit_id in title, f"unit_id {unit_id!r} missing from title {title!r}"
        assert "wire spawn_async into MCP" in title, (
            f"unit.title missing from worker title {title!r}"
        )

    def test_task_arg_is_compose_coder_task_output(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        """``worker.spawn_async(task, …)`` must receive the result of
        ``compose_coder_task`` — the marker-grammar coder prompt. A
        regression that passed the bare ``unit.description`` would still
        spawn a session but produce a coder with no marker contract,
        which would manifest as silent ``no_marker`` returns from
        ``wait_unit``.
        """
        feature_id, unit_id = _seed_feature()

        # Sentinel composed-task payload — we override the helper so the
        # test isn't entangled with the real prompt template.
        sentinel = "SENTINEL_CODER_PROMPT::do the work::marker grammar applies"
        monkeypatch.setattr(
            "orchestrator.tools.execution.compose_coder_task",
            lambda *a, **kw: sentinel,
        )
        worker = _RecordingWorker("coder", session_id="sesn-task")
        _install_worker(monkeypatch, worker)

        execution.spawn_unit_async(feature_id, unit_id)

        assert len(worker.spawn_async_calls) == 1
        task_arg = worker.spawn_async_calls[0]["task"]
        assert task_arg == sentinel, (
            f"worker.spawn_async did NOT receive compose_coder_task output: {task_arg!r}"
        )


# ===========================================================================
# spawn_unit_async — return shape
# ===========================================================================


class TestSpawnUnitAsyncReturnShape:
    """Pin the JSON shape ``spawn_unit_async`` returns. The lead /
    dashboard parses these fields; a silently-dropped column would
    break upstream callers without an obvious failure mode.
    """

    def test_response_carries_feature_id(self, tmp_state_db, monkeypatch, _fake_pat):
        feature_id, unit_id = _seed_feature()
        _install_worker(monkeypatch, _RecordingWorker("coder", session_id="sesn-fid"))

        payload = json.loads(execution.spawn_unit_async(feature_id, unit_id))

        assert payload["feature_id"] == feature_id, (
            f"feature_id missing/incorrect in response: {payload!r}"
        )

    def test_response_session_id_matches_persisted_row(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        """The returned ``session_id`` must equal the value persisted
        to ``work_units.coder_session_id``. A divergence here means
        the caller is reading one value while the daemon is reading
        another — a sneaky ghost-row variant.
        """
        feature_id, unit_id = _seed_feature()
        _install_worker(monkeypatch, _RecordingWorker("coder", session_id="sesn-roundtrip"))

        payload = json.loads(execution.spawn_unit_async(feature_id, unit_id))
        row = state.get_unit_state(unit_id)

        assert row is not None
        assert payload["session_id"] == row.coder_session_id == "sesn-roundtrip"

    def test_response_branch_matches_persisted_row(
        self, tmp_state_db, monkeypatch, _fake_pat
    ):
        feature_id, unit_id = _seed_feature()
        _install_worker(monkeypatch, _RecordingWorker("coder", session_id="sesn-branch"))

        payload = json.loads(execution.spawn_unit_async(feature_id, unit_id))
        row = state.get_unit_state(unit_id)

        assert row is not None
        assert payload["branch"] == row.branch
        assert payload["branch"]  # non-empty


# ===========================================================================
# wait_unit — argument plumbing
# ===========================================================================


class TestWaitUnitTimeoutPropagation:
    """``timeout_s`` is the only knob the lead / daemon have for tuning
    wait-budget. It must reach ``worker.wait_idle`` verbatim.
    """

    def _seed_coder_session(self, session_id: str = "sesn-coder") -> str:
        feature_id, unit_id = _seed_feature()
        state.upsert_unit_state(
            WorkUnitState(
                unit_id=unit_id,
                feature_id=feature_id,
                status="coding",
                branch="extras-u-1",
                coder_session_id=session_id,
            )
        )
        return unit_id

    def test_custom_timeout_reaches_wait_idle(self, tmp_state_db, monkeypatch):
        unit_id = self._seed_coder_session()
        worker = _RecordingWorker(
            "coder", session_id="sesn-coder", wait_response="PR_URL: https://x/y/pull/1"
        )
        _install_worker(monkeypatch, worker)

        execution.wait_unit(unit_id, "coder", timeout_s=42)

        assert len(worker.wait_idle_calls) == 1
        call = worker.wait_idle_calls[0]
        assert call["timeout_seconds"] == 42, (
            f"timeout_s not propagated to worker.wait_idle: got "
            f"timeout_seconds={call['timeout_seconds']}, expected 42"
        )
        assert call["session_id"] == "sesn-coder"

    def test_timeout_value_echoed_in_still_running_payload(
        self, tmp_state_db, monkeypatch
    ):
        """When ``worker.wait_idle`` raises TimeoutError, the JSON
        response must include the original ``timeout_s`` so the daemon /
        operator can see what budget was honoured.
        """
        unit_id = self._seed_coder_session()
        worker = _RecordingWorker(
            "coder", session_id="sesn-coder", wait_raises=TimeoutError("did not idle")
        )
        _install_worker(monkeypatch, worker)

        payload = json.loads(execution.wait_unit(unit_id, "coder", timeout_s=37))

        assert payload["status"] == "still_running"
        assert payload["reason"] == "timeout"
        assert payload["timeout_s"] == 37


# ===========================================================================
# wait_unit — JSON shape
# ===========================================================================


class TestWaitUnitResponseShape:
    """Pin the shape of the ``wait_unit`` JSON. The dashboard and the
    F-016-U-5 daemon both parse these fields. A regression that
    dropped or renamed a field would silently break read-side callers.
    """

    def _seed(self, role: str, session_id: str, status: str = "coding") -> str:
        feature_id, unit_id = _seed_feature()
        field = {
            "coder": "coder_session_id",
            "tester": "tester_session_id",
            "reviewer": "reviewer_session_id",
        }[role]
        state.upsert_unit_state(
            WorkUnitState(
                **{
                    "unit_id": unit_id,
                    "feature_id": feature_id,
                    "status": status,
                    "branch": "extras-u-1",
                    field: session_id,
                }
            )
        )
        return unit_id

    def test_marker_path_includes_response_tail_and_session_id(
        self, tmp_state_db, monkeypatch
    ):
        unit_id = self._seed("coder", "sesn-rt-1")
        worker = _RecordingWorker(
            "coder",
            session_id="sesn-rt-1",
            wait_response="some chatter\nPR_URL: https://github.com/o/r/pull/9",
        )
        _install_worker(monkeypatch, worker)

        payload = json.loads(execution.wait_unit(unit_id, "coder", timeout_s=10))

        # The contract surfaces both the parsed marker AND the response
        # tail — the latter is what dashboards show humans triaging a
        # noisy success path.
        assert payload["marker"] == "PR_URL"
        assert payload["session_id"] == "sesn-rt-1"
        assert "response_tail" in payload
        assert "PR_URL" in payload["response_tail"]
        assert payload["unit_id"] == unit_id
        assert payload["role"] == "coder"

    def test_no_marker_path_includes_response_tail(self, tmp_state_db, monkeypatch):
        unit_id = self._seed("coder", "sesn-rt-2")
        worker = _RecordingWorker(
            "coder",
            session_id="sesn-rt-2",
            wait_response="rambled but never emitted a marker",
        )
        _install_worker(monkeypatch, worker)

        payload = json.loads(execution.wait_unit(unit_id, "coder", timeout_s=10))

        assert payload["status"] == "still_running"
        assert payload["reason"] == "no_marker"
        assert "response_tail" in payload, (
            "no_marker path must surface response_tail so triage tools can show "
            "what the agent actually said"
        )
        assert "rambled" in payload["response_tail"]


# ===========================================================================
# Predecessor F-016-U-1 — _KNOWN_ROLES contract consistency
# ===========================================================================


class TestPredecessorRoleGateConsistency:
    """F-016-U-1 locked in a gate: ``markers.scan_response`` rejects
    unknown roles BEFORE matching ``BLOCKED`` text. ``wait_unit`` is
    the new caller; it must behave consistently — the role check has
    to land before any worker construction or state.db read so a
    corrupted role argument can't probe orchestrator internals.
    """

    def test_unknown_role_rejected_without_state_db_read(
        self, tmp_state_db, monkeypatch
    ):
        """The role guard must short-circuit BEFORE
        ``state.get_unit_state`` — otherwise an unknown-role wait_unit
        call could exfiltrate "does this unit exist" info to a corrupted
        caller. Same paranoia as U-1's `_KNOWN_ROLES` placement.
        """
        called = {"get_unit_state": False}
        real_get = state.get_unit_state

        def _spy(unit_id):
            called["get_unit_state"] = True
            return real_get(unit_id)

        monkeypatch.setattr(state, "get_unit_state", _spy)
        # Don't even need a worker stub — the path must reject before
        # ``ManagedAgentWorker`` is touched.
        monkeypatch.setattr(
            "orchestrator.tools.execution.ManagedAgentWorker",
            lambda role: (_ for _ in ()).throw(
                AssertionError("worker constructed despite unknown role")
            ),
        )

        result = execution.wait_unit("nonexistent-unit", "daemon", timeout_s=5)

        assert result.startswith("ERROR")
        assert not called["get_unit_state"], (
            "wait_unit read state.db before validating role — gate ordering bug"
        )

    def test_known_roles_match_predecessor_contract(self, tmp_state_db):
        """The set of accepted roles in ``wait_unit`` must equal U-1's
        ``markers._KNOWN_ROLES``. A divergence here would mean the
        recorder accepts a role wait_unit rejects (or vice versa) —
        a future-tense mismatch that bites Phase-3 daemon callers.
        """
        from orchestrator import markers

        # Extract the role set ``wait_unit`` enforces by trying every
        # canonical role + a couple of impostors against the function.
        # The function's behaviour IS the contract.
        for role in ("coder", "tester", "reviewer"):
            # Known roles must NOT trip the role-guard message.
            err = execution.wait_unit("does-not-exist", role, timeout_s=1)
            assert "role must be" not in err, (
                f"wait_unit rejected known role {role!r}: {err}"
            )

        # Plus assert the marker module's gate set matches what we
        # accept — predecessor contract.
        assert markers._KNOWN_ROLES == frozenset({"coder", "tester", "reviewer"})


# ===========================================================================
# Out-of-scope guard — marker grammar untouched
# ===========================================================================


class TestScopeInvariants:
    """F-016-U-2's ``Out of scope`` excludes "Replacing the cap-3
    contract or the marker grammar." Pin the public surface of
    ``orchestrator.markers`` so a scope-creep regression is loud.
    """

    def test_scan_response_signature_unchanged(self):
        from orchestrator import markers

        sig = inspect.signature(markers.scan_response)
        params = list(sig.parameters)
        # Predecessor F-016-U-1 contract: (role, text, *, allowed=None).
        assert params[:2] == ["role", "text"], (
            f"markers.scan_response signature drifted: {params}"
        )
        assert "allowed" in sig.parameters

    def test_dedupe_key_signature_unchanged(self):
        from orchestrator import markers

        sig = inspect.signature(markers.dedupe_key)
        # F-016-U-1 lock: dedupe_key(*, session_id, cycle_number,
        # event_type, marker_payload).
        kw_only = {
            name for name, p in sig.parameters.items() if p.kind == p.KEYWORD_ONLY
        }
        assert {"session_id", "cycle_number", "event_type", "marker_payload"} <= kw_only, (
            f"markers.dedupe_key kw-only set drifted: {sorted(kw_only)}"
        )

    def test_known_roles_set_unchanged(self):
        from orchestrator import markers

        assert markers._KNOWN_ROLES == frozenset({"coder", "tester", "reviewer"}), (
            f"_KNOWN_ROLES drifted: {markers._KNOWN_ROLES}"
        )


# ===========================================================================
# MCP tool docstrings
# ===========================================================================


class TestMcpToolDocstrings:
    """FastMCP surfaces the python docstring as the MCP tool description
    the lead sees. An empty docstring degrades the discoverability of
    the new primitives.
    """

    def test_async_tools_have_non_empty_descriptions(self):
        tools = asyncio.new_event_loop().run_until_complete(mcp.list_tools())
        by_name = {t.name: t for t in tools}

        for name in ("spawn_unit_async", "wait_unit"):
            assert name in by_name, f"{name} missing from MCP tool registry"
            desc = by_name[name].description or ""
            assert desc.strip(), f"{name} has empty MCP description"
            # The Phase-1 contract — the lead needs to know what these
            # tools do without reading source. A bare one-line title
            # wouldn't be enough; require a couple of sentences.
            assert len(desc) > 80, (
                f"{name} description is suspiciously short ({len(desc)} chars): "
                f"{desc!r}"
            )
