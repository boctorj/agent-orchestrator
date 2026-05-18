"""Planning-phase MCP tools: features + plans."""

from __future__ import annotations

import json
from dataclasses import asdict

from orchestrator import feature_spec, repo_verify, state
from orchestrator.models import Feature, FeatureStatus, WorkUnit
from orchestrator.tools import mcp


@mcp.tool()
def load_feature(
    title: str,
    description: str,
    id: str = "",
    repo_path: str = "",
    branch_prefix: str = "",
    ultrareview_enabled: bool | None = None,
) -> str:
    """Record a feature. `repo_path` should be a GitHub URL for Stage 3+.

    If `repo_path` is set, this also checks the verification cache and
    appends a warning if the repo has never been verified or its
    verification is stale (>24h). Planning continues regardless — but
    any subsequent spawn against the feature WILL be blocked until the
    user runs `verify_repo(<url>)`.

    `ultrareview_enabled` is an opt-in feature-level flag for the
    `/ultrareview` terminal gate after our reviewer endorses. Off by
    default because ultrareview costs measurably per cycle. See
    docs/PROPOSAL-ultrareview-gate.md.

    The default is `None` (sentinel for "caller did not specify") rather
    than `False`, so a metadata-only update on an existing feature
    preserves the prior flag value when the caller omits the argument —
    fixing a wrong `repo_path` on an ultrareview-enabled feature won't
    silently disable the gate. On creation, an omitted flag means False.
    Pass `True` / `False` explicitly to set the flag; pass nothing to
    preserve.

    Update semantics: calling load_feature with an `id` that already
    exists is treated as a metadata update, not a re-creation. If the
    plan's units are unchanged since approval (plan.status == 'approved'),
    feature.status is preserved as 'approved' — fixing a wrong repo_path
    or toggling ultrareview_enabled does not require the user to re-call
    `approve_plan`. If save_plan was called between approval and now
    (plan.status == 'draft', meaning the units list materially changed),
    the feature drops back to 'draft' — material change requires
    re-approval.
    """
    existing = state.get_feature(id) if id else None

    if existing is None:
        # New feature creation — existing behavior.
        feature_id = id or state.next_feature_id()
        feature = Feature(
            id=feature_id,
            title=title,
            description=description,
            repo_path=repo_path,
            branch_prefix=branch_prefix,
            ultrareview_enabled=bool(ultrareview_enabled),
        )
        state.save_feature(feature)
        msg = f"Loaded feature {feature.id}: {feature.title}"
    else:
        # Existing id — decide whether to preserve approval.
        #
        # load_feature itself only carries metadata fields, so the units
        # list is unchanged by this call. The plan's own status tells us
        # whether they changed via a prior save_plan: that helper resets
        # plan.status to 'draft' whenever the units list is re-saved.
        # So plan.status == 'approved' here means "units unchanged since
        # the last approve_plan" — a true metadata-only update.
        plan = state.get_plan(existing.id)
        plan_still_approved = plan is not None and plan.status == "approved"

        new_status: FeatureStatus
        if existing.status == "approved" and plan_still_approved:
            new_status = "approved"
            path_desc = "metadata-only — approval preserved"
        elif existing.status == "approved":
            # plan was re-saved (units changed) between approval and now;
            # the feature row hadn't caught up. Drop to draft — material
            # change requires re-approval.
            new_status = "draft"
            path_desc = "units changed — reset to draft"
        else:
            # Feature was never approved; metadata update doesn't shift
            # its status one way or the other.
            new_status = existing.status
            path_desc = "metadata updated"

        # Sentinel-preserve: an omitted ultrareview_enabled means "leave it
        # as-is" rather than "set to False" — otherwise a metadata-only
        # update silently drops a previously-enabled flag (the canonical
        # "fix a wrong repo_path" path in the proposal).
        new_ultrareview = (
            existing.ultrareview_enabled if ultrareview_enabled is None else ultrareview_enabled
        )

        feature = Feature(
            id=existing.id,
            title=title,
            description=description,
            repo_path=repo_path,
            branch_prefix=branch_prefix,
            status=new_status,
            created_at=existing.created_at,
            ultrareview_enabled=new_ultrareview,
        )
        state.save_feature(feature)
        msg = f"Updated feature {feature.id} ({path_desc})"

    # Seed `features/<feature_id>/spec.md` from the template. Idempotent —
    # leaves an existing spec.md untouched, so lead-authored edits survive
    # repeated `load_feature` calls (e.g. fixing a wrong repo_path). Also
    # back-fills spec.md for features that pre-date F-006 the first time
    # they're touched.
    feature_spec.write_spec_if_missing(feature.id, feature.title, feature.description)

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
        [
            {
                "id": f.id,
                "title": f.title,
                "status": f.status,
                "ultrareview_enabled": f.ultrareview_enabled,
            }
            for f in features
        ],
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
    result = state.get_plan_with_ultrareview(feature_id)
    if result is None:
        return f"No plan exists for {feature_id} yet."
    plan, ultrareview_enabled = result
    return json.dumps(
        {
            "feature_id": plan.feature_id,
            "status": plan.status,
            "approved_at": plan.approved_at,
            "ultrareview_enabled": ultrareview_enabled,
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
