# F-018: Conflict-aware cycle_review + daemon wiring

## Intent
Today's `cycle_review` flow does not check PR mergeable state. The reviewer can emit `REVIEW_RECOMMEND_MERGE` on code that has merge conflicts (because another unit landed mid-cycle, or because main has moved since the PR opened). cycle_review fires `_emit_terminal("approved_awaiting_merge", ...)` and returns silently. The conflict only surfaces when the user clicks "Merge" on GitHub and the UI rejects it — or when someone manually calls `inspect_unit_health(unit_id)`.

Worse: a unit that hits `approved_awaiting_merge` cleanly can develop a conflict LATER, as sibling units merge while this one sits awaiting the human. The orchestrator today has no post-terminal monitoring.

F-014 already has the detection logic in `health.py` — `pr_conflict_detected` is one of the new event-only signals — but cycle_review and the cycle-flow never call it. F-016 Phase 3's daemon will call `inspect_unit_health` on every reconcile tick, so detection comes for free in daemon mode; what's missing is the **recovery flow** wiring.

This feature adds conflict awareness at two trigger sites with one shared recovery flow:

**Trigger 1 — cycle_review's CI-green gates (pre-tester, pre-reviewer):** after CI passes and before the next phase spawn, call `inspect_unit_health` to check mergeable state. If `pr_conflict_detected`, dispatch a fix message to the coder with the conflict file list. Wait for the coder's rebased HEAD, re-check mergeable, loop. Conflict-fix retries use a SEPARATE counter (`conflict_fix_attempts`, cap 3) so they don't burn the cap-3 quality budget — a merge conflict is mechanical, not a quality failure.

**Trigger 2 — daemon-side post-terminal monitoring (F-016 Phase 3):** when a unit in `approved_awaiting_merge` develops a conflict (because a sibling merged), the daemon's `inspect_unit_health` poll catches it, emits `pr_conflict_detected`, and dispatches the SAME conflict-fix flow back to the coder. The unit transitions out of `approved_awaiting_merge` back into `fixing` until conflicts clear and the reviewer re-endorses on the new SHA.

**Recovery flow (shared by both triggers):**
1. `address_review(unit_id, source="merge", feedback="Rebase against main; resolve conflicts in <files>.")`
2. Coder rebases, force-pushes new HEAD.
3. Wait for CI on rebased HEAD via existing `_wait_ci_with_fix_loop`.
4. Re-check mergeable; loop until clean or `conflict_fix_attempts` hits cap 3.
5. **Prior tester/reviewer markers are stale w.r.t. the new SHA** — F-016 Phase 2.5's stale-marker rule already handles this via `worker.resume(session_id, delta_review_prompt)`. Delta-review emits a fresh marker on the new SHA. No new logic needed here.
6. If reviewer endorses the rebased code → fresh `approved_awaiting_merge`.
7. If `conflict_fix_attempts` hits cap 3 → escalate to user with "rebase loop diverging; main is too volatile."

**Why one unit and not two:** the trigger sites differ but the recovery flow is identical. Implementing the recovery once and wiring both triggers to it is cleaner than scaling the work to artificial unit boundaries.

**Dependency on F-016:** the daemon trigger requires F-016-U-5 (Phase 3 watcher daemon) to land. The cycle_review-gate trigger does not.

## Acceptance

When the feature ships:

1. `cycle_review` calls `inspect_unit_health(unit_id, dry_run=True)`
   after each CI-green gate (pre-tester and pre-reviewer). When the
   probe reports `pr_conflict_detected`, the cycle dispatches a fix
   to the coder via `address_review(source="merge", feedback=...)`
   listing the conflict files, instead of advancing to the next
   phase.
2. After the coder pushes a rebased HEAD, `_wait_ci_with_fix_loop`
   waits for CI on the new SHA (existing path; no change). The
   mergeable check then re-runs. If still conflicted, loop.
3. A new counter `WorkUnitState.conflict_fix_attempts` tracks
   conflict-fix retries separately from `cycle_number`. Cap is 3.
   When the cap hits, the unit escalates with reason
   `conflict_rebase_diverging`.
4. Prior tester/reviewer markers from before the rebase are NOT
   discarded by this feature — they are invalidated naturally by
   F-016 Phase 2.5's existing stale-marker rule
   (`reviewer.reviewed_sha < pr.head_sha` → delta re-review). After
   the rebase clears conflicts, the reviewer phase resumes via the
   existing delta-review path on the new SHA.
5. (Daemon trigger, requires F-016 Phase 3) The daemon's reconcile
   loop's `inspect_unit_health` call already runs for every active
   unit. The transition table gains one row:
   `(approved_awaiting_merge, PrConflictDetected(files)) →
   DispatchConflictFix(coder, files)`. The unit transitions back to
   `fixing`. Same recovery flow as cycle_review's gates.
6. New `address_review` source value `"merge"` is recognized in
   `compose_fix_task` and the coder prompt. The fix message lists
   exact conflict files and instructs the coder to rebase against
   main (not merge) so the PR's commit graph stays linear.
7. Tests pin:
   - Mergeable check fires at both cycle_review gates.
   - `conflict_fix_attempts` is independent of `cycle_number` (a
     conflict-fix cycle does not increment cap-3, and a cap-3
     tester-bug fix does not increment conflict_fix_attempts).
   - Cap 3 on `conflict_fix_attempts` escalates with the right
     reason slug.
   - After a successful rebase, the cycle proceeds to the next
     phase via the existing delta-review path (no separate retry
     logic for the reviewer).
   - End-to-end: a unit with a sibling-induced conflict mid-cycle
     hits the conflict-fix flow, the coder rebases, CI re-runs,
     reviewer delta-reviews on the new SHA, terminal is
     `approved_awaiting_merge` on the rebased HEAD.

## Out of scope

- **Auto-detecting which OTHER unit caused the conflict.** The
  feedback message lists conflict FILES; the user (or coder) can
  trace which sibling unit owns those files from git history if
  needed. We don't try to compute a dependency graph from conflict
  sets.
- **Auto-merge of trivial conflicts** (e.g., import-block conflicts).
  The coder agent decides how to resolve — same as today's
  `address_review` flow. If GitHub's "Update branch" is sufficient,
  the coder can use that; if a real merge is needed, the coder
  resolves.
- **Pre-existing conflict at PR open time** (i.e., coder opened a PR
  that was already conflicted). Today's flow doesn't catch this
  either; out of scope to fix here. The pre-tester gate would catch
  it on the first CI-green pass anyway.
- **`update_unit_deps` adjustments** based on observed conflicts.
  The graph mutation primitive from F-016 Phase 2.5 is for
  intentional re-shaping by the lead, not orchestrator inference.

## Approach

**Touch points (estimated):**

1. [`orchestrator/tools/execution.py`](../../orchestrator/tools/execution.py) —
   add a helper `_check_mergeable(unit_id) -> ConflictResult | None`
   that wraps `inspect_unit_health(dry_run=True)` and returns the
   conflict files if `pr_conflict_detected`, else None. Call it
   after each `_wait_ci_with_fix_loop` in `cycle_review`.
2. [`orchestrator/state.py`](../../orchestrator/state.py) — add a
   `conflict_fix_attempts INTEGER DEFAULT 0` column to `work_units`
   (additive migration). Helper methods to increment + read; cap
   check.
3. [`orchestrator/tools/__init__.py`](../../orchestrator/tools/__init__.py)'s
   `compose_fix_task` — accept `source="merge"` and emit
   appropriate rebase-against-main instructions. Add a paragraph to
   [`orchestrator/prompts/coder.md`](../../orchestrator/prompts/coder.md)
   for the `merge` source case: rebase, not merge; resolve conflicts
   by re-running the original change against new HEAD; force-push.
4. [`orchestrator/blocked_reasons.py`](../../orchestrator/blocked_reasons.py) —
   add `conflict_rebase_diverging` reason slug.
5. [`orchestrator/daemon.py`](../../orchestrator/daemon.py) (F-016
   Phase 3, new) — one row in the transition table mapping
   `(approved_awaiting_merge, PrConflictDetected)` to
   `DispatchConflictFix`. Drops in cleanly once F-016-U-5 ships.

**Recovery flow integration:** the conflict-fix uses
`address_review(source="merge", ...)` rather than a brand-new
spawn or a custom resume. This reuses the existing fix-loop
machinery (`_wait_ci_with_fix_loop`, FIX_PUSHED marker handling,
delta-review trigger on tester/reviewer sessions). The only new
behavior is the dispatch decision; the rebase + verify + delta
loop is unchanged.

## Constraints

- **Cap 3 conflict-fix attempts is hard.** Don't let the loop run
  forever on a volatile main branch; escalate to user with the
  diverging slug so they can decide whether to land a sibling and
  retry, or rebase manually.
- **Conflict-fix retries do NOT consume cap-3.** Cap-3 is the
  quality-failure circuit breaker. Merge conflicts are mechanical.
  Mixing the counters would surprise users when a unit that had ZERO
  quality issues escalates after three sibling merges.
- **Backward compatible.** Units with `conflict_fix_attempts=0`
  (new column, default 0) behave exactly like today. The check
  itself is additive at gates that already exist.
- **No new dependencies.** Implementation reuses F-014's
  `inspect_unit_health`, F-016 Phase 2.5's stale-marker delta
  review, and the existing `address_review` fix-loop pipeline.

## Decisions
_None yet. Non-obvious choices land here as planning and execution progress; the commit message for each edit carries the `Why:` line (see `docs/SPEC-FORMAT.md`)._

## Open questions
_None yet. Resolved questions move to Decisions._
