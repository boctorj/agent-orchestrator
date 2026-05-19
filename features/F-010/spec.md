# F-010: F-010: State-machine recovery from escalation (P1)

## Intent
Per docs/STATE-MACHINE-AUDIT.md gaps A/C/D. Once a unit hits escalated, cycle_review and the spawn surfaces refuse to re-engage because the session_id is still set on the unit row (the duplicate-spawn guards at execution.py:249 and :420). Manual recovery via send_to_unit hits Gap B. check_unit_pr also refuses to flip escalated units to done even when the user merges the PR. Result: legitimately-shippable units stay stuck in escalated forever; the dashboard counts them as failures and downstream units never unblock.

Two units:

U-1: In spawn_tester (execution.py:222) and spawn_reviewer (:391), add a session-reset preamble: if unit_state.status == "escalated" AND the role's session_id is set, clear *_session_id, log a {role}_session_reset event (source=orchestrator), then proceed with the normal spawn path. Mirror the same logic in cycle_review's _tester_phase / _reviewer_phase guards. Same applies to address_review's coder-session lookup. Tests: spawn_tester on an escalated unit with prior tester_session_id clears it and runs cleanly; cycle_review retry after a transient failure picks up where it left off; the existing duplicate-spawn-while-running guard still fires when status is not "escalated".

U-2: In check_unit_pr (ops.py:32-78), widen the merge-flip gate to include escalated units. When pr_state.merged is True AND unit_state.status in ("in_ci", "escalated", "approved_awaiting_merge") AND status != "done", flip to done. Record a recovered_from_escalated event (in addition to merged) when prior status was escalated. Clear last_error. Tests: escalated unit with a merged PR flips to done via check_unit_pr and emits both events; in_ci path still records only "merged"; non-merged PR on escalated unit stays escalated.

U-2 doesn't depend on U-1 but should ship after — U-1 is the higher-impact fix.

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
