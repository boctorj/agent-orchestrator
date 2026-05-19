# F-012: Cycle-review transition latency — adaptive Copilot wait, reviewer session resume, CI poll tuning

## Intent
Reduce orchestrator-side gate latency between agent transitions in `cycle_review`. Measured on the F-009 parallel run, ~22-24% of every unit's wall-clock today is spent in CI + Copilot gates (not agent work), and reviewer retries pay a full cold-start session each cycle (e.g. F-009-U-1 had two reviewer spawns of 957s and 999s for what could be a delta review).

Three independent fixes, ordered by impact-per-effort:

(1) Adaptive Copilot review wait. Today `_copilot_phase` in `orchestrator/tools/execution.py` unconditionally waits up to 300s for a Copilot review every cycle. Event-table evidence: 11 `copilot_review_timeout` vs 1 `copilot_review_received` across 5 days — Copilot is almost never enabled / responsive on these repos, so the 5-minute wait is paid for nothing. Fix: track per-repo Copilot responsiveness (column on `verified_repos`, or per-repo memo), and skip the wait when prior attempts have consistently timed out. Keep current behavior as a fallback on first encounter / on repos where Copilot has produced reviews. Estimated savings: ~5 min per review round.

(2) Reviewer + tester session resume on retry. `orchestrator/tools/execution.py:935` (tester) and `:1030` (reviewer) explicitly clear `*_session_id` on retry, forcing a fresh sandbox + cold context that re-clones the PR and re-runs analysis. Replace that with `worker.resume(reviewer_session_id, delta_msg)` and a new "delta review" prompt path that says "the coder pushed commits X..Y in response to your prior feedback; reassess and emit a terminal marker." Risk: the existing reviewer prompt isn't tuned to potentially reverse its own verdict — needs a separate retry-prompt and prompt eval before flipping on. Coder session already follows this pattern (`worker.resume(coder_session_id, fix_msg)`) so the worker plumbing is the same. Estimated savings: ~10 min per retry cycle (the second reviewer pass on F-009-U-1 took 999s for a delta review on a single commit).

(3) CI poll tuning + drop final defensive CI re-check. `CI_WAIT_POLL_INTERVAL` is 15s (`orchestrator/ci_wait.py:51`); dropping to 5s cuts the rounding-up loss at each of the ~3 CI gates per cycle. Also, `cycle_review`'s "GATE 3 defensive" final pre-merge CI check at `orchestrator/tools/execution.py:1088` re-pays the wait when the reviewer's fix-loop already gated on green — mostly safe to delete. Estimated savings: ~2 min per unit.

Combined: ~17 min recoverable on a single-retry unit, more on multi-retry units (F-007-U-2 had 3 reviewer cycles).

References:
- F-009-U-1 breakdown: 5027s wall-clock = 3942s agent work + 1084s gates + 1s instant transitions
- All three fixes are orthogonal — ship in any order; (1) and (3) are pure infra, (2) touches the reviewer prompt contract.

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
