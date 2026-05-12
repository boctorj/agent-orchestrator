"""Planning-phase MCP tools: features + plans."""

from __future__ import annotations

import json
from dataclasses import asdict

from orchestrator import repo_verify, state
from orchestrator.models import Feature, WorkUnit
from orchestrator.tools import mcp


@mcp.tool()
def load_feature(
    title: str,
    description: str,
    id: str = "",
    repo_path: str = "",
    branch_prefix: str = "",
) -> str:
    """Record a feature. `repo_path` should be a GitHub URL for Stage 3+.

    If `repo_path` is set, this also checks the verification cache and
    appends a warning if the repo has never been verified or its
    verification is stale (>24h). Planning continues regardless — but
    any subsequent spawn against the feature WILL be blocked until the
    user runs `verify_repo(<url>)`.
    """
    feature_id = id or state.next_feature_id()
    feature = Feature(
        id=feature_id,
        title=title,
        description=description,
        repo_path=repo_path,
        branch_prefix=branch_prefix,
    )
    state.save_feature(feature)
    msg = f"Loaded feature {feature.id}: {feature.title}"
    # Surface verification status as a warning so the lead can prompt the
    # user to verify before spawning (rather than discovering at spawn time).
    if repo_path:
        try:
            normalized = repo_verify.normalize_repo_url(repo_path)
        except ValueError as e:
            msg += f"\n\n⚠ repo_path is malformed: {e}"
        else:
            fresh = state.get_fresh_verified_repo(normalized)
            if fresh is None:
                msg += (
                    f"\n\n⚠ Target repo {normalized} is not verified — "
                    f"spawns will be blocked until you call "
                    f"`verify_repo({normalized!r})`."
                )
    return msg


@mcp.tool()
def list_features() -> str:
    features = state.list_features()
    if not features:
        return "No features loaded. Use load_feature() to start."
    return json.dumps(
        [{"id": f.id, "title": f.title, "status": f.status} for f in features],
        indent=2,
    )


@mcp.tool()
def save_plan(feature_id: str, units: list[dict]) -> str:
    if not state.get_feature(feature_id):
        return f"ERROR: feature {feature_id} not found — call load_feature first"

    try:
        work_units = [
            WorkUnit(
                id=u["id"],
                feature_id=feature_id,
                title=u["title"],
                description=u["description"],
                depends_on=u.get("depends_on", []),
            )
            for u in units
        ]
    except KeyError as e:
        return f"ERROR: unit missing required field {e}"

    unit_ids = {u.id for u in work_units}
    for u in work_units:
        for dep in u.depends_on:
            if dep not in unit_ids:
                return f"ERROR: unit {u.id} depends on unknown unit {dep}"

    state.save_plan(feature_id, work_units)
    return f"Saved plan for {feature_id}: {len(work_units)} unit(s). Status: draft."


@mcp.tool()
def get_plan(feature_id: str) -> str:
    plan = state.get_plan(feature_id)
    if not plan:
        return f"No plan exists for {feature_id} yet."
    return json.dumps(
        {
            "feature_id": plan.feature_id,
            "status": plan.status,
            "approved_at": plan.approved_at,
            "units": [asdict(u) for u in plan.units],
        },
        indent=2,
    )


@mcp.tool()
def approve_plan(feature_id: str) -> str:
    try:
        ts = state.approve_plan(feature_id)
    except ValueError as e:
        return f"ERROR: {e}"
    return f"Plan for {feature_id} APPROVED at {ts}."
