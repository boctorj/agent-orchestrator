# Proposal: Ultrareview as a terminal gate (opt-in per feature)

**Status:** draft · Date: 2026-05-15

**Scope of this doc:** the design and opt-in semantics. Schema +
`load_feature` plumbing ships as **F-007-U-1**; the cycle_review wiring
that actually fires `/ultrareview` is later work in the F-007 series.

## TL;DR

After our reviewer agent emits `REVIEW_RECOMMEND_MERGE`, optionally fire
`/ultrareview` as a final pass. Only emit `approved_awaiting_merge` if
ultrareview also passes; otherwise route findings back through
`address_review` like any other review feedback.

This catches final-mile issues our reviewer + Copilot miss (subtle spec
drift, hidden coupling, edge cases the line-level review didn't surface).
But ultrareview costs measurably per cycle — enough that it should be
**opt-in per feature**, not on by default.

## Motivation

Today's cycle_review terminates after our reviewer endorses:

```
coder PR → CI green → tester → CI green → Copilot review → CI green
  → reviewer agent (spec/scope/intent) → REVIEW_RECOMMEND_MERGE
  → notify lead, await human merge
```

Three things review at line-level granularity (Copilot, our reviewer's
spec compliance pass, the human merger). None of them looks at the PR
holistically the way `/ultrareview` does: re-reading the diff against
the full feature spec, considering side effects across modules,
challenging the design choices, asking "is this actually done?"

In practice that catches:
- spec acceptance criteria that look met but actually aren't,
- subtle scope creep / unrelated drive-by edits,
- hidden coupling introduced by the change,
- edges the line-level reviewers' attention budget skipped.

The cost: ultrareview burns measurable tokens — non-trivial per cycle,
and cycle_review already runs Copilot + our reviewer (and re-runs them
after every fix). Always-on isn't justified for every feature; **opt-in
per feature** is.

## Opt-in semantics

A single feature-level boolean: `ultrareview_enabled`. Default `False`.

### Where the flag lives

- **Schema:** new column `features.ultrareview_enabled INTEGER NOT NULL
  DEFAULT 0` (SQLite booleans are ints). Backfilled to 0 on existing
  rows via an idempotent migration in `init_db()` (SQLite has no
  `ADD COLUMN IF NOT EXISTS`, so `PRAGMA table_info(features)` gates
  the `ALTER TABLE`).
- **Dataclass:** `Feature.ultrareview_enabled: bool = False`.
- **State helpers:** `save_feature` / `get_feature` / `list_features`
  round-trip the flag; `_feature_from_row` coerces SQLite's INTEGER
  back to `bool` so callers can rely on `is True` / `is False`
  (mirrors the existing `_verified_repo_from_row` pattern).

### How the flag gets set

- **Lead → `load_feature(title, description, ..., ultrareview_enabled=False)`.**
  The lead asks the user during feature breakdown: "this feature looks
  load-bearing — turn on ultrareview for it?" and passes the answer to
  `load_feature`.
- **Toggling later** is a metadata-only update — calling `load_feature`
  with the same `id` and a different `ultrareview_enabled` value
  preserves plan approval (same rule as fixing a wrong `repo_path`).
- **No env-level kill switch in F-007-U-1.** Per-feature granularity is
  the whole point. A later unit may add a `cycle_review(..., force_no_ultrareview=True)`
  override for emergencies.

### Where the flag surfaces

- `list_features()` JSON now includes `"ultrareview_enabled": bool` per
  feature so the lead can see at-a-glance which features have the gate
  on.
- `get_plan(feature_id)` JSON adds the same field at the top level
  (it's a feature attribute, not a plan attribute, but get_plan is
  what the lead reads when reviewing a feature pre-execution — so the
  flag is most useful here).
- `verify_repo` / dashboard / cost telemetry are unchanged. The flag is
  consumed by `cycle_review` (next unit, not this one) and is otherwise
  inert.

## What the next unit will do (not this PR)

This unit ships the schema + plumbing only. The behaviour change lives
in the next F-007 unit, which will:

1. Read `feature.ultrareview_enabled` inside `cycle_review`.
2. After our reviewer emits `REVIEW_RECOMMEND_MERGE`, if the flag is
   off, terminate as `approved_awaiting_merge` (today's behaviour).
3. If the flag is on, fire `/ultrareview` against the PR (mechanism
   TBD — slash-command on the coder's session, or a separate worker
   role). Wait for its verdict.
4. If ultrareview passes → terminate as `approved_awaiting_merge`.
   If it requests changes → `address_review(source='ultrareview', ...)`
   and re-enter the fix loop. Counts toward the cap-3 budget.
5. Emit a new event type `ultrareview_passed` / `ultrareview_changes_requested`
   into `unit_events` so cost telemetry and the cycle log can attribute
   the extra spend.

Acceptance criteria for the follow-up unit live in
`features/F-007/spec.md` (per F-006 Phase 1 convention).

## Why a feature-level flag (and not a repo-level one)

Considered and rejected:

- **Repo-level** (in `verified_repos`): too coarse. Mixed-criticality
  repos are common — auth-handling features want ultrareview, README
  tweaks don't.
- **Unit-level**: too fine — the lead would have to set it on every
  unit, and the natural unit of "is this load-bearing?" is the feature.
- **Env var (`ORCH_ULTRAREVIEW_DEFAULT`)**: hides intent. The flag's
  state should be visible in `list_features` / `get_plan`, not inferred
  from a process environment.

Feature-level wins on three counts: it's the natural granularity of the
intent ("is THIS feature load-bearing?"), it's visible in state.db, and
it's already the unit at which planning + approval happens.

## Risks

1. **Flag stays off and we ship a broken feature.** Mitigation:
   ultrareview is gravy on top of Copilot + our reviewer; both already
   run unconditionally. The flag controls a *third* check, not the only
   check.
2. **Cost spike on a heavy fix-loop feature.** Mitigation: ultrareview
   findings count toward the cap-3 budget like any other review source,
   so a runaway loop escalates to the human instead of burning forever.
3. **Lead forgets to set the flag.** Mitigation: load_feature output
   surfaces the value (via `list_features` / `get_plan`), so a quick
   `list_features` shows which features have ultrareview on. The lead
   persona can be updated to ask the user during breakdown — but that
   prompt-level change is out of scope for this unit (it ships with
   the cycle_review wiring in the follow-up).

## Out of scope for F-007-U-1

- The cycle_review wiring that consumes the flag.
- The `/ultrareview` worker role / slash-command implementation.
- Cost attribution / event types for ultrareview spend.
- Lead-persona prompt changes asking the user about the flag.
- Documenting the flag in `README.md` and `docs/ARCHITECTURE.md` —
  deferred until the user-visible behaviour ships in the follow-up
  unit. (Per CONTRIBUTING.md: "Update docs if user-visible behavior
  changed.")

## References

- `BACKLOG.md` "Ultrareview as a terminal gate" — original feature
  request and effort estimate.
- `docs/PROPOSAL-feature-spec-and-headless-daemon.md` — F-006 Phase 1
  introduces `features/F-XXX/spec.md`, which ultrareview will use as
  the intent comparator in the follow-up unit.
- `orchestrator/state.py` `_migrate_features_ultrareview` — the
  idempotent column-add for pre-F-007 state.db files.
