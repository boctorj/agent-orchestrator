"""DAG scheduling + parallel execution MCP tools."""

from __future__ import annotations

import json

from orchestrator import state
from orchestrator.models import CANCELLED_UNIT_STATUSES, READY_TO_MERGE_STATUSES
from orchestrator.tools import ensure_verified_for_feature, mcp
from orchestrator.tools.execution import cycle_review, spawn_unit


@mcp.tool()
def next_ready_units(feature_id: str) -> str:
    """Return work units ready to spawn: not yet started AND all deps are 'done'.

    A unit is 'ready' when:
      - It has no row in work_units yet (never spawned), AND
      - Every unit in its `depends_on` list has work_units.status = 'done'
        (i.e. its PR has been merged and reconcile_unit_pr has flipped it).

    A dep in ``approved_awaiting_merge`` keeps downstream units blocked —
    the cycle finished but the human's merge click hasn't landed yet, so
    the dep can still be re-opened with changes. The
    ``approved_awaiting_merge`` unit itself is reported under
    ``awaiting_merge`` (a bucket distinct from ``in_flight``: no agent is
    running, but the lead should know the human still owes a merge click).
    See :data:`READY_TO_MERGE_STATUSES`. F-009-U-4, audit Gap H.

    Call this whenever a unit transitions to 'done' (typically after the
    user merges and `reconcile_unit_pr` is run). For each unit returned,
    spawn it (spawn_unit → cycle_review) one at a time, or batch via
    parallel_units.
    """
    plan = state.get_plan(feature_id)
    if not plan:
        return json.dumps({"error": f"no plan for {feature_id}"})

    unit_states = {s.unit_id: s for s in state.list_unit_states(feature_id)}
    # Only 'done' counts as a satisfied dep — ``approved_awaiting_merge``
    # is explicitly excluded (F-009-U-4): a PR awaiting merge can still be
    # closed unmerged or rebased, so downstream work shouldn't start until
    # the human's merge click has landed. ``cancelled`` (F-016 Phase 2.5)
    # also doesn't satisfy a dep — per spec, "downstream dep-evaluation
    # treats cancelled as not-done".
    done_ids = {uid for uid, s in unit_states.items() if s.status == "done"}

    ready, blocked, in_flight, awaiting_merge, cancelled = [], [], [], [], []

    for unit in plan.units:
        if unit.id in unit_states:
            s = unit_states[unit.id]
            if s.status == "done":
                continue
            elif s.status == "escalated":
                blocked.append({"unit_id": unit.id, "reason": s.last_error or "escalated"})
            elif s.status in READY_TO_MERGE_STATUSES:
                awaiting_merge.append({"unit_id": unit.id, "status": s.status})
            elif s.status in CANCELLED_UNIT_STATUSES:
                cancelled.append({"unit_id": unit.id, "cancelled_at": s.cancelled_at})
            else:
                in_flight.append({"unit_id": unit.id, "status": s.status})
            continue

        if any(dep not in done_ids for dep in unit.depends_on):
            continue  # waiting on deps
        ready.append(
            {
                "unit_id": unit.id,
                "title": unit.title,
                "depends_on": unit.depends_on,
            }
        )

    return json.dumps(
        {
            "feature_id": feature_id,
            "ready_to_spawn": ready,
            "in_flight": in_flight,
            "awaiting_merge": awaiting_merge,
            "escalated": blocked,
            "cancelled": cancelled,
            "total_ready": len(ready),
        },
        indent=2,
    )


@mcp.tool()
def next_ready_units_all() -> str:
    """Return work units ready to spawn across ALL features, not just one.

    Aggregates `next_ready_units(feature_id)` across every approved or
    in-progress feature. Each entry is annotated with its `feature_id`.

    Use this at the start of a multi-feature session and after merges.
    When `ready_to_spawn` spans multiple features, prefer
    `parallel_units_global(...)` over per-feature loops.
    """
    aggregated: dict[str, list] = {
        "ready_to_spawn": [],
        "in_flight": [],
        "awaiting_merge": [],
        "escalated": [],
        "cancelled": [],
    }
    for f in state.list_features():
        if f.status not in ("approved", "in_progress"):
            continue
        try:
            per_feat = json.loads(next_ready_units(f.id))
        except json.JSONDecodeError:
            continue
        if "error" in per_feat:
            continue
        for key in aggregated:
            for entry in per_feat.get(key, []):
                entry = dict(entry)
                entry.setdefault("feature_id", f.id)
                aggregated[key].append(entry)

    return json.dumps(
        {
            "total_ready": len(aggregated["ready_to_spawn"]),
            "total_in_flight": len(aggregated["in_flight"]),
            "total_awaiting_merge": len(aggregated["awaiting_merge"]),
            "total_escalated": len(aggregated["escalated"]),
            "total_cancelled": len(aggregated["cancelled"]),
            **aggregated,
        },
        indent=2,
    )


def _run_one(feature_id: str, unit_id: str) -> dict:
    """Spawn + cycle-review one unit. Used by both parallel tools.

    Returns a result dict whether the unit succeeded, failed, or errored.

    F-016 Phase 5: the thread pool that used to wrap this helper has been
    retired — daemon-driven concurrency (proposal § Phase 5: "Delete
    parallel_units / parallel_units_global thread-pool internals once
    daemon-driven concurrency proves itself in production") handles the
    fan-out now. ``cycle_review`` auto-routes async vs. blocking via the
    Phase 4 dispatcher (``NTFY_TOPIC`` + daemon health), so the parallel
    callers walk the same engine the chat persona does without a parallel
    blocking path.

    F-016 Phase 6 (U-8): ``spawn_unit`` is now ALSO a dispatcher (async
    handoff under NTFY+daemon, blocking otherwise). On the async branch
    the spawn return carries ``{"delivered": true, "status": "coding",
    "session_id": …}`` with no ``pr_url`` (the coder hasn't opened the
    PR yet — that lands on a later daemon tick). Pre-U-8 ``_run_one``
    bailed on "no pr_url" which short-circuited every async dispatch
    into a ``no_pr`` result; the post-U-8 contract treats a successful
    async handoff as a valid intermediate state and STILL chains
    through to ``cycle_review`` so the daemon takes ownership of the
    full pipeline.
    """
    try:
        spawn_result = spawn_unit(feature_id, unit_id)
    except Exception as e:  # noqa: BLE001 — surface as result, don't propagate
        return {"feature_id": feature_id, "unit_id": unit_id, "phase": "spawn", "error": str(e)}

    try:
        sp = json.loads(spawn_result)
    except json.JSONDecodeError:
        return {
            "feature_id": feature_id,
            "unit_id": unit_id,
            "phase": "spawn",
            "outcome": "non_json",
            "raw": spawn_result,
        }

    spawn_delivered_async = sp.get("delivered") is True and sp.get("mode") == "async_daemon"
    # Async handoff (Phase 6 U-8): the coder is dispatched, the PR will
    # land on a later tick. Skip the pre-U-8 "no pr_url → bail" guard so
    # ``cycle_review`` still routes through the daemon dispatcher. The
    # blocking path still requires ``pr_url`` because a synchronous
    # spawn that finished without one is a real error (coder emitted no
    # PR_URL marker — escalated).
    if not spawn_delivered_async and "pr_url" not in sp:
        return {
            "feature_id": feature_id,
            "unit_id": unit_id,
            "phase": "spawn",
            "outcome": "no_pr",
            "result": sp,
        }

    try:
        # F-016 Phase 4 dispatcher: ``cycle_review`` is async when
        # ``NTFY_TOPIC`` + the daemon are live, blocking otherwise.
        # Both branches return JSON ``_emit_terminal`` produces (success
        # or escalation) OR the async handoff envelope, so downstream
        # consumers see a parseable dict either way.
        cycle_result = cycle_review(feature_id, unit_id)
        cy = json.loads(cycle_result)
    except Exception as e:  # noqa: BLE001
        return {
            "feature_id": feature_id,
            "unit_id": unit_id,
            "phase": "cycle",
            "spawn_pr": sp.get("pr_url"),
            "error": str(e),
        }

    return {
        "feature_id": feature_id,
        "unit_id": unit_id,
        # ``pr_url`` is only populated on the blocking spawn branch;
        # async handoff returns no PR (the coder will open it on a
        # later daemon tick). ``spawn_mode`` lets a caller tell which
        # branch we walked without re-running ``ntfy.is_configured()``.
        "pr_url": sp.get("pr_url"),
        "pr_number": sp.get("pr_number"),
        "spawn_mode": sp.get("mode"),
        "spawn_delivered": sp.get("delivered"),
        # ``cy`` may be either the terminal-emit JSON (``outcome``
        # populated, ``approved_awaiting_merge`` / ``escalated`` on the
        # blocking path) OR the Phase 4 async-handoff envelope
        # (``delivered=True``, ``mode='async_daemon'``, ``outcome``
        # absent — the daemon will drive the unit to terminal and ntfy
        # will push on completion). Pass both through so the lead can
        # tell the difference; downstream callers that key on
        # ``cycle_outcome`` see ``None`` for the async branch, which is
        # the right signal that "the unit isn't done yet — wait for
        # the ntfy push".
        "cycle_outcome": cy.get("outcome"),
        "cycle_message": cy.get("message"),
        "cycle_mode": cy.get("mode"),
        "cycle_delivered": cy.get("delivered"),
    }


# F-016 Phase 5: the ``max_concurrent`` parameter is retained on both
# parallel surfaces for callsite compatibility (CLAUDE.md's scheduling
# rule and the lead persona still pass it), but it's now a no-op — the
# daemon owns fan-out. Documented on each tool so a curious caller sees
# why the knob doesn't change behavior.


@mcp.tool()
def parallel_units(feature_id: str, unit_ids: list[str], max_concurrent: int = 3) -> str:
    """Spawn + cycle-review multiple units within ONE feature.

    Use after ``next_ready_units`` returns 2+ ready units in a single
    feature. Each unit walks ``spawn_unit`` + ``cycle_review`` in turn;
    when the F-016 watcher daemon is running and ``NTFY_TOPIC`` is set,
    ``cycle_review`` hands off to the daemon (≤2 s per dispatch) and
    actual fan-out is daemon-driven. Without those, each call falls back
    to blocking ``cycle_review_blocking`` semantics — slower, but
    correct.

    The ``max_concurrent`` argument is kept for callsite compatibility
    (proposal § Phase 5 retires the thread pool but the lead persona
    still passes the knob); concurrency is delegated to the daemon's
    poll loop now and the value is otherwise unused.

    Caveats:
    - Does NOT validate dependencies — call next_ready_units first
    - Independent unit failures don't affect siblings (each is a try/except)
    """
    del max_concurrent  # daemon owns fan-out; retained for callsite compat
    if not unit_ids:
        return json.dumps({"error": "no unit_ids provided"})

    # Single feature → gate once up front to fail fast instead of in N workers
    if err := ensure_verified_for_feature(feature_id):
        return json.dumps({"error": err})

    results = [_run_one(feature_id, uid) for uid in unit_ids]

    return json.dumps(
        {
            "feature_id": feature_id,
            "unit_count": len(unit_ids),
            "results": results,
        },
        indent=2,
    )


@mcp.tool()
def parallel_units_global(unit_refs: list[dict], max_concurrent: int = 3) -> str:
    """Run units from MULTIPLE features.

    ``unit_refs``: list of ``{"feature_id": ..., "unit_id": ...}`` dicts.

    Use after ``next_ready_units_all`` when the ready list spans multiple
    features. Each unit walks ``spawn_unit`` + ``cycle_review`` in turn;
    when the F-016 watcher daemon is running and ``NTFY_TOPIC`` is set,
    ``cycle_review`` hands off to the daemon (≤2 s per dispatch) and
    actual fan-out is daemon-driven. Without those, each call falls back
    to blocking ``cycle_review_blocking`` semantics.

    The ``max_concurrent`` argument is kept for callsite compatibility
    (proposal § Phase 5 retires the thread pool but the lead persona
    still passes the knob); concurrency is delegated to the daemon's
    poll loop now and the value is otherwise unused.

    Independent unit failures don't affect siblings.
    """
    del max_concurrent  # daemon owns fan-out; retained for callsite compat
    if not unit_refs:
        return json.dumps({"error": "no unit_refs provided"})

    for ref in unit_refs:
        if not isinstance(ref, dict) or "feature_id" not in ref or "unit_id" not in ref:
            return json.dumps({"error": f"invalid unit_ref (need feature_id + unit_id): {ref}"})

    # Gate each distinct feature once up front. Aggregating verification
    # failures lets the lead surface them together instead of as N spawn
    # errors that all say the same thing.
    seen_features: set[str] = set()
    verify_errors: list[dict] = []
    for ref in unit_refs:
        fid = ref["feature_id"]
        if fid in seen_features:
            continue
        seen_features.add(fid)
        if err := ensure_verified_for_feature(fid):
            verify_errors.append({"feature_id": fid, "error": err})
    if verify_errors:
        return json.dumps(
            {
                "error": "one or more target repos not verified — spawn batch refused",
                "details": verify_errors,
            },
            indent=2,
        )

    results = [_run_one(ref["feature_id"], ref["unit_id"]) for ref in unit_refs]

    return json.dumps(
        {
            "unit_count": len(unit_refs),
            "results": results,
        },
        indent=2,
    )
