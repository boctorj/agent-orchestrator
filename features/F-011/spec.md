# F-011: F-011: State-machine correctness — counter + status hygiene (P2)

## Intent
Per docs/STATE-MACHINE-AUDIT.md gaps E, F, G. Three independent correctness bugs in the existing transition logic. None are urgent, but each one nudges the state machine in subtle wrong directions that compound when something else fails.

Three units:

U-1 (Gap F): In address_review (execution.py:602), state.increment_review_round(unit_id) currently runs BEFORE worker.resume(...). On a coder_resume_error (resume raises an exception — e.g. flaky network), the unit goes escalated with the cycle counter advanced, even though no actual fix attempt was made. The CAP_3 budget is meant to count fix-attempts, not resume-attempts. Move the increment to happen only after worker.resume returns successfully (or even better, only after FIX_PUSHED is parsed). Update the coder_resumed event's cycle_number to use the pre-increment value if increment moves after. Tests: a resume that raises must NOT bump review_round; a resume that returns BLOCKED bumps once; a resume returning FIX_PUSHED bumps once. The CAP_3 gate at the head of cycle_review's fix loop still fires on the third successful fix.

U-2 (Gap G): In spawn_tester (execution.py:326-352, the BUG_FOUND branch) and spawn_reviewer (:490-512, the REQUEST_CHANGES branch), add state.touch_unit(unit_id, status="in_ci") after the event is recorded. Currently the status stays "testing"/"reviewing" even though the tester/reviewer session has ended — the dashboard mis-labels these units as "agent active." Tests: after BUG_FOUND, unit_state.status == "in_ci"; after REQUEST_CHANGES, unit_state.status == "in_ci"; the tester_session_id / reviewer_session_id stays set (needed by cycle_review for the address_review hand-off).

U-3 (Gap E): In spawn_unit (execution.py:123 and :138), the PR_URL parse currently does state.upsert_unit_state(...) then safe_amend_pr_body then state.record_event("pr_opened") as three separate _connect() contexts. Process death between the upsert and the record_event leaves the unit row with status=in_ci and pr_number=N but no pr_opened event in unit_events — unit_history looks like the PR never opened. Add a state.record_pr_opened_atomic(unit, pr_url, session_id) helper that runs the upsert and the event-record inside a single transaction. Use it from spawn_unit. Tests: simulate a failure between the two writes (mock conn.commit or similar) — either both writes land or neither does, never just the upsert.

These three are independent — they can be parallelized.

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
