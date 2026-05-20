# State-machine audit

Living notes on the orchestrator's cycle / unit-status state machine: edge
cases that have bitten us, the recovery paths added to handle them, and the
invariants worth preserving when touching `orchestrator/tools/execution.py`.

Companion to [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) § 7b
("Cycle_review state machine in detail") — that doc covers the happy-path
diagram; this one covers the failure modes.

## Stale-session recovery at tester/reviewer re-entry

**Failure mode (pre-F-013).** When `cycle_review` dies mid-phase — typically
a network timeout against the Anthropic Managed Agents API while a tester
or reviewer worker is running — the unit row in `state.db` is left with a
non-empty `tester_session_id` or `reviewer_session_id` but no terminal
marker (`tests_pass`, `tester_bug_found`, `reviewer_recommend_merge`, …)
event recorded. The PR is still open and (often) CI is still green; the
unit just needs the in-flight worker to issue its verdict.

On the next `cycle_review` call, however, the initial `spawn_tester` /
`spawn_reviewer` invocation hit the "session already exists" precondition
guard:

```python
# orchestrator/tools/execution.py
if unit_state.tester_session_id:
    return f"ERROR: tester session already exists for {unit_id}"
if unit_state.reviewer_session_id:
    return f"ERROR: reviewer session already exists for {unit_id}"
```

`_record_step` couldn't parse the bare error string as JSON, so it fell
back to `{"outcome": "RAW", "raw": "<error string>"}`. The phase helper's
`outcome.startswith("BLOCKED")` short-circuit checks the parsed
`outcome` field, not the raw blob, so the BLOCKED branch missed and the
phase reached its trailing line:

```python
return False, f"tester ended with unexpected outcome: {outcome}"
return False, f"reviewer ended with unexpected outcome: {outcome}"
```

`cycle_review` then escalated with the misleading
`message="tester ended with unexpected outcome: RAW"` (or `reviewer …`)
— even though CI was green and the unit just needed the worker to emit
a marker.

Observed instances on 2026-05-19: F-012-U-2 (round=0), F-008-U-1
(round=3), F-009-U-2 (round=3). F-009-U-2 had already cleared a full
CI fix-loop (FIX_PUSHED + 356s of waiting for CI green) before dying
at the tester wall.

**Recovery (F-013-U-1 + F-013-U-2).** The initial-call sites in
`_tester_phase` and `_reviewer_phase` route through
`_resume_or_spawn_tester(feature_id, unit_id)` and
`_resume_or_spawn_reviewer(feature_id, unit_id)` respectively. The
helpers branch:

- **No session on the unit** → delegate to `spawn_tester` /
  `spawn_reviewer` and return its raw response. Same shape, no behaviour
  change from the pre-F-013 path.
- **Session set (orphan condition)** → flip status to `testing` /
  `reviewing`, record a `tester_resume` / `reviewer_resume` audit
  breadcrumb, then call
  `_resume_role_session(<role>, sid, recovery_prompt)`. The recovery
  prompt explains that the previous response was lost to a network
  timeout, that the PR is still open and CI is green, and asks the agent
  to re-emit its verdict marker on its own line — without redoing work
  it already completed.

The resume response is scanned via the same
`_record_terminal_marker(role=…, …)` dispatch that `spawn_tester` /
`spawn_reviewer` use, so all marker → audit-event mappings, the
"flips_in_ci" status transitions, and the `BLOCKED → escalated` flip
fire identically to the cold-start path.

The helpers return JSON in the same shape `_record_step` consumes from
a fresh spawn — including the BLOCKED branch, which returns
`outcome="BLOCKED"` rather than the bare `"BLOCKED — …"` string that
`spawn_tester` and `spawn_reviewer` still emit. This keeps the phase
helpers' `outcome.startswith("BLOCKED")` short-circuit firing on the
resume path; without it, a BLOCKED resume would fall through to the
very "unexpected outcome: RAW" branch the unit fixes.

**Where each path lives.**

| Site | Function | Behaviour |
|---|---|---|
| `_tester_phase` initial call | `_resume_or_spawn_tester` | Resume if `tester_session_id` set; else `spawn_tester`. |
| `_tester_phase` retry (post-BUG_FOUND fix-loop) | `spawn_tester` directly | The retry path already clears `tester_session_id` before calling, so the orphan condition cannot arise. |
| `_reviewer_phase` initial call | `_resume_or_spawn_reviewer` | Resume if `reviewer_session_id` set; else `spawn_reviewer`. |
| `_reviewer_phase` retry (post-REVIEW_REQUEST_CHANGES fix-loop) | `_resume_reviewer_for_delta` | F-012-U-2 — keeps the existing reviewer session for a delta re-review. Untouched by F-013. |

**Operator-visible breadcrumbs.** A successful resume looks like this in
`unit_history`:

```
… (prior cycle events …)
tester_resume      orchestrator   "Resuming orphaned tester session"
tests_pass         tester         "All tests pass"
```

The `tester_resume` / `reviewer_resume` event between the prior cycle's
events and the verdict event is the signal that the orchestrator chose
the recovery branch over a fresh spawn. Without that breadcrumb a
`tests_pass` against an orphan `session_id` with no preceding
`spawn_tester` event would be unexplained.

**Invariants for future edits.**

- A new initial-call site for `spawn_tester` or `spawn_reviewer` inside
  `cycle_review` (or any other surface that runs on a possibly-resumed
  unit row) must go through the resume-or-spawn helper, not call
  `spawn_*` directly.
- The helpers must return JSON for every marker, including BLOCKED.
  `_record_step`'s RAW fallback is for genuinely malformed responses,
  not for a marker we already parsed structurally.
- The `tester_resume` / `reviewer_resume` audit event must precede any
  marker event from the same resume call. Tests assert the chronology
  so an operator can reconstruct "this verdict came from a resumed
  orphan, not a fresh spawn" from `unit_history`.
