# F-009: F-009: Close state-machine recording gaps (P0)

## Intent
Per docs/STATE-MACHINE-AUDIT.md gaps B/I and H. These are the two highest-impact drift cases — every endorsement that went through send_to_unit in the F-006/F-007/F-008 sessions was invisible to the orchestrator's state, and the dashboard's "Awaiting your merge" bucket is permanently empty because nothing writes the status it queries on.

Two units:

U-1: Extract a _record_terminal_marker(unit_id, feature_id, role, response, session_id, cycle_number) helper next to _escalate_no_marker in execution.py. Helper runs the same regex chain spawn_tester/spawn_reviewer/address_review use today (TESTS_PASS_RE / BUG_FOUND_RE / REVIEW_RECOMMEND_MERGE_RE / REVIEW_CHANGES_RE / REVIEW_COMMENT_RE / FIX_PUSHED_RE / parse_blocked_marker) and writes the corresponding event + state transition. Call it from send_to_unit (execution.py:1131) after worker.resume succeeds, before recording {role}_manual_message. Keep the manual_message event for audit; add the structured one for state. Refactor the existing call sites to use the helper so the parsing logic has one source of truth. Add tests covering: send_to_unit(reviewer, msg) where reviewer's response contains REVIEW_RECOMMEND_MERGE writes both reviewer_manual_message AND reviewer_recommend_merge, and flips status from reviewing to in_ci. Same for TESTS_PASS / FIX_PUSHED / BLOCKED variants.

U-2: Add "approved_awaiting_merge" to the UnitStatus Literal in models.py. In cycle_review's _emit_terminal (execution.py:728), when outcome=="approved_awaiting_merge" call state.touch_unit(ctx.unit_id, status="approved_awaiting_merge"). Add the new status to the "Awaiting your merge" bucket logic used by show_dashboard and to a new READY_TO_MERGE_STATUSES set (or update TERMINAL_UNIT_STATUSES) so next_ready_units treats it as "not in flight, not blocked, but not 'done' until merge." Add a check_unit_pr branch so it flips approved_awaiting_merge → done on observed merge. Tests: dashboard shows the unit in the right bucket; check_unit_pr advances on merge; next_ready_units treats it as inactive.

U-2 depends on U-1 (the recording helper is what writes the new status from the send_to_unit path; without U-1 the status would only be reachable via cycle_review's terminal).

## Acceptance
_TBD — concrete, testable criteria for "done"._

## Out of scope
_TBD — hard boundary against scope creep._

## Approach
_TBD — high-level design choices, library / framework decisions._

## Constraints
_TBD — non-functional requirements (perf, security, compatibility)._

## Decisions
_None yet. Non-obvious choices land here as planning and execution progress; the commit message for each edit carries the `Why:` line (see `docs/SPEC-FORMAT.md`)._

## Open questions
_None yet. Resolved questions move to Decisions._
