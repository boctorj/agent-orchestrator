"""DAG scheduling + parallel execution MCP tools."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from orchestrator import state
from orchestrator.tools import ensure_verified_for_feature, mcp
from orchestrator.tools.execution import cycle_review, spawn_unit


@mcp.tool()
def next_ready_units(feature_id: str) -> str:
    """Return work units ready to spawn: not yet started AND all deps are 'done'.

    A unit is 'ready' when:
      - It has no row in work_units yet (never spawned), AND
      - Every unit in its `depends_on` list has work_units.status = 'done'
        (i.e. its PR has been merged and reconcile_unit_pr has flipped it).

    Call this whenever a unit transitions to 'done' (typically after the
    user merges and `reconcile_unit_pr` is run). For each unit returned,
    spawn it (spawn_unit → cycle_review) one at a time, or batch via
    parallel_units.
    """
    plan = state.get_plan(feature_id)
    if not plan:
        return json.dumps({"error": f"no plan for {feature_id}"})

    unit_states = {s.unit_id: s for s in state.list_unit_states(feature_id)}
    done_ids = {uid for uid, s in unit_states.items() if s.status == "done"}

    ready, blocked, in_flight = [], [], []

    for unit in plan.units:
        if unit.id in unit_states:
            s = unit_states[unit.id]
            if s.status == "done":
                continue
            elif s.status == "escalated":
                blocked.append({"unit_id": unit.id, "reason": s.last_error or "escalated"})
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
            "escalated": blocked,
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
        "escalated": [],
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
        for key in ("ready_to_spawn", "in_flight", "escalated"):
            for entry in per_feat.get(key, []):
                entry = dict(entry)
                entry.setdefault("feature_id", f.id)
                aggregated[key].append(entry)

    return json.dumps(
        {
            "total_ready": len(aggregated["ready_to_spawn"]),
            "total_in_flight": len(aggregated["in_flight"]),
            "total_escalated": len(aggregated["escalated"]),
            **aggregated,
        },
        indent=2,
    )


def _run_one(feature_id: str, unit_id: str) -> dict:
    """Spawn + cycle-review one unit. Used by both parallel tools.

    Returns a result dict whether the unit succeeded, failed, or errored.
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

    if "pr_url" not in sp:
        return {
            "feature_id": feature_id,
            "unit_id": unit_id,
            "phase": "spawn",
            "outcome": "no_pr",
            "result": sp,
        }

    try:
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
        "pr_url": sp.get("pr_url"),
        "pr_number": sp.get("pr_number"),
        "cycle_outcome": cy.get("outcome"),
        "cycle_message": cy.get("message"),
    }


@mcp.tool()
def parallel_units(feature_id: str, unit_ids: list[str], max_concurrent: int = 3) -> str:
    """Spawn + cycle-review multiple units in parallel within ONE feature.

    BLOCKS until all done. Up to `max_concurrent` (default 3, hard cap 5)
    run simultaneously via a thread pool.

    Use after `next_ready_units` returns 2+ ready units in a single feature.
    Significantly faster than serial spawn_unit + cycle_review.

    Caveats:
    - Cap of 5 to stay within Anthropic rate limits
    - Does NOT validate dependencies — call next_ready_units first
    - Independent unit failures don't affect siblings
    """
    if not unit_ids:
        return json.dumps({"error": "no unit_ids provided"})

    # Single feature → gate once up front to fail fast instead of in N workers
    if err := ensure_verified_for_feature(feature_id):
        return json.dumps({"error": err})

    max_workers = min(max_concurrent, len(unit_ids), 5)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_one, feature_id, uid): uid for uid in unit_ids}
        for fut in as_completed(futures):
            results.append(fut.result())

    return json.dumps(
        {
            "feature_id": feature_id,
            "unit_count": len(unit_ids),
            "max_concurrent": max_workers,
            "results": results,
        },
        indent=2,
    )


@mcp.tool()
def parallel_units_global(unit_refs: list[dict], max_concurrent: int = 3) -> str:
    """Run units from MULTIPLE features in parallel. BLOCKS until all done.

    unit_refs: list of {"feature_id": ..., "unit_id": ...} dicts.

    Use after `next_ready_units_all` when the ready list spans multiple
    features. Saturates the concurrency budget evenly across features
    rather than blocking on one at a time.

    Cap of 5 max_concurrent. Independent unit failures don't affect siblings.
    """
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

    max_workers = min(max_concurrent, len(unit_refs), 5)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_one, ref["feature_id"], ref["unit_id"]): ref for ref in unit_refs}
        for fut in as_completed(futures):
            results.append(fut.result())

    return json.dumps(
        {
            "unit_count": len(unit_refs),
            "max_concurrent": max_workers,
            "results": results,
        },
        indent=2,
    )
