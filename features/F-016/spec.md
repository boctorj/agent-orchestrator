# F-016: Non-blocking orchestrator — dispatcher/watcher split + reconciliation daemon

> **Design proposal (draft):** [`docs/PROPOSAL-async-orchestrator.md`](../../docs/PROPOSAL-async-orchestrator.md).
> This spec is the feature-level summary; the proposal carries the full
> phase-by-phase API surface, migration sequence, risks, and acceptance.

## Intent

Every chat session blocks for 5–30 minutes on each `spawn_unit` /
`cycle_review` call, because those MCP tools do the dispatching, the
waiting, the marker parsing, AND the state writes in one synchronous
function. Workers already run remotely (Managed Agents / Docker) — the
orchestrator is merely polling — but the lead session can't accept user
input until the poll loop finishes. At 5–10 simultaneous features this
blocking is the dominant pain point, and a killed lead process can strand
work mid-flight (the ghost-row failure mode).

This feature separates the **dispatcher** (fast: submit + record
session_id, returns ≤2s) from the **watcher** (slow: poll + parse +
advance), and moves the watcher into a long-lived background **daemon**
that owns the state machine. MCP tools become RPC; the lead's role
collapses to *plan → approve → react to push notifications*.

It is the natural continuation of
[`PROPOSAL-feature-spec-and-headless-daemon.md`](../../docs/PROPOSAL-feature-spec-and-headless-daemon.md)
§ Phase 3, and it is **built directly on F-014**: the daemon's
level-triggered `derive_next_action(unit) = f(unit_state, observations)`
is F-014's pure `decide_transitions`. F-014 ships the engine; F-016 ships
the loop that calls it — "one engine, two callers (lead + daemon)", no
parallel state machine.

**F-015 is absorbed.** F-015 ("promote unit-health probe shadow decisions
to automated transitions") is the same state-machine work as the daemon's
reconciler. F-014's shadow-decision telemetry becomes F-016's validation
harness during rollout. F-015 should be marked `obsoleted-by F-016`.

### Units (one per migration phase)

- **F-016-U-1 — Phase 0: idempotent marker recording.** `unit_events.dedupe_key`
  + `INSERT OR IGNORE`; pure `markers.scan_response(role, text)`;
  read-only `scan_unit_session`. Zero behavior change; unblocks every
  later phase.
- **F-016-U-2 — Phase 1: non-blocking spawn primitives.** Wire the
  existing `spawn_async` / `wait_idle` into MCP; add `resume_async`.
  `spawn_unit_async` returns in ≤2s and persists `session_id` before
  returning (kills the ghost-row class).
- **F-016-U-3 — Phase 2: decompose `cycle_review` into phase commands.**
  `advance_to_tester` / `advance_to_reviewer` / `advance_to_terminal`,
  each idempotent on current status. `cycle_review` becomes a convenience
  wrapper over them.
- **F-016-U-4 — Phase 2.5: lead/daemon interaction contract.** Per-unit
  advance-lock (~1s submit window), `cancel_unit` (sticky), async
  `send_to_unit`, `update_unit_deps`; stale-marker delta-review rule.
- **F-016-U-5 — Phase 3: watcher daemon.** Level-triggered reconciler
  (`orchestrator/daemon.py`), idempotent transitions, SQLite-backed
  singleton (`daemon_locks`) keyed by absolute state.db path (one daemon
  per workspace), `owner` CAS, automatic crash recovery. Opt-in via
  `ORCH_DAEMON_DRIVE=true`.
- **F-016-U-6 — Phase 4: lead becomes pure dispatcher.** `cycle_review`
  defaults to daemon mode **when `NTFY_TOPIC` is set** (else stays
  blocking + nudges); explicit `cycle_review_async` / `cycle_review_blocking`
  always available. `cycle_review_blocking` calls the daemon's engine
  in-process — no duplicate transition table.
- **F-016-U-7 — Phase 5: cleanup + unified bootstrap + credential hardening.**
  Move blocking phase helpers into
  `orchestrator/cycle/phases.py` (shared by daemon + blocking caller),
  retire thread-pool internals; `orchestrator run` auto-starts a
  detached daemon when `ORCH_DAEMON_DRIVE=true`; add `orchestrator
  daemon stop`; strip/validate `ANTHROPIC_*` to kill shell-rc
  shadowing; update CLAUDE.md/README/`docs/DAEMON.md`.
- **F-016-U-8 — Phase 6: uniform non-blocking dispatch.** U-6 made
  only `cycle_review` a non-blocking dispatcher, but `spawn_unit`,
  `address_review`, `spawn_tester`, `spawn_reviewer`, `send_to_unit`,
  and `parallel_units` / `parallel_units_global` still block minutes — so the lead
  foot-guns itself by calling a blocking sibling (the post-F-016
  blocking still observed in practice). Factor U-6's gate into a
  shared `_dispatch_or_block` helper and apply it to every
  long-running command; add the missing `_async` variants
  (`address_review_async`, `spawn_tester_async`, `spawn_reviewer_async`).
  Each surface keeps an explicit `_blocking`/`_async` variant; only the
  default flips. Depends on U-7.
- **F-016-U-9 — Spawn ghost-row guard + retry cap (anti-loop hardening).**
  Born from a live incident (2026-06-10): U-7 was
  re-spawned ~6× over 12 h, each blocking spawn dying on a
  managed-agents network read-timeout after 45–60 min and never
  persisting a `session_id`, so the row stayed re-spawnable forever.
  `spawn_unit`'s only idempotency check is non-empty
  `coder_session_id` ([execution.py:128]), which a failed spawn never
  sets. Fix: refuse re-spawn on a row already in
  `{coding,opening_pr,in_ci,testing,reviewing,fixing,escalated}`
  (point caller at `inspect_unit_health`/`cancel_unit`), plus a
  3-attempt cap that escalates + ntfy instead of looping. Applies to
  `spawn_unit` + `spawn_unit_async`. Safety/idempotency fix, NOT the
  transport cure (async in U-7/U-8 fixes the timeout itself). No deps;
  ships ahead of U-7/U-8 (U-8 rebases its dispatcher onto this guard).
  Incident stopgap until merged: U-7's `coder_session_id` set to a
  `GHOST-LOOP-HALTED-*` sentinel so the existing guard refuses spawns.

## Acceptance

When all nine units merge:
1. `spawn_unit`, `address_review`, `spawn_tester`, `spawn_reviewer`,
   `send_to_unit`, `cycle_review`, `parallel_units`, `parallel_units_global`
   all return in ≤3s under NTFY+daemon (U-8 — not just `cycle_review`).
2. A lead session killed mid-cycle does not strand the unit; the daemon
   completes the cycle and ntfy fires.
3. `list_in_flight` / `resume_unit` / `tail_worker` continue to work
   (read-side, unchanged).
4. All existing tests pass; new tests cover the daemon's claim, polling,
   and recovery paths.
5. CLAUDE.md describes the new mental model: "react to state, don't drive
   execution."

(Per-phase acceptance lives in the proposal doc.)

## Out of scope

- Replacing the cap-3 contract or the marker grammar.
- Eliminating ntfy (it remains the human-attention channel).
- Multi-tenancy / cross-machine daemons (single user, single host; one
  daemon per workspace).
- Pushing stream updates *into* the chat — chat is the command surface;
  updates land in state.db + ntfy + dashboard.
- Swapping the worker backend (orthogonal — see
  [`PROPOSAL-docker-workers.md`](../../docs/PROPOSAL-docker-workers.md)).

## Constraints

- **Additive at every step.** Existing markers, prompts, state.db schema,
  and both worker backends keep working; every phase ships independently
  with no regression.
- **Idempotent transitions are load-bearing.** The daemon is stateless
  between ticks (Kubernetes-controller pattern); duplicate work is deduped
  by Phase 0's `dedupe_key` and guarded by the `owner` CAS.
- **No parallel state machine.** `cycle_review_blocking` and the daemon
  call the *same* `derive_next_action` + `execute` engine.

## Decisions

- **Separate process, not an MCP subprocess** — decoupled lifetimes so a
  killed lead doesn't kill the watcher.
- **SQLite `daemon_locks` singleton over a pidfile** — pidfiles are fragile
  (force-quit orphans, launchd races); the lock table + heartbeat handles
  takeover and keys per workspace.
- **Default-flip gated on `NTFY_TOPIC`** — without a feedback channel,
  a ≤1s `cycle_review` that goes quiet is worse UX than today's blocking.
- **U-9's `ci_drift` tail-dedupe fix shipped as a follow-up PR, not a new
  unit.** PR #69 was merged (2026-06-14 02:35) at U-9's round-1 fix, before
  the reviewer's re-verify closed the loop. The re-review then found the
  dedupe still tail-unsafe past 200 events (`_should_emit_ci_drift` /
  `_consecutive_failed_spawns` walked `list_events`'s *oldest*-N) and a
  regression test that passed whether or not the dedupe held. The completed
  fix — bounded `last_event_of_type` / `tail_events` queries, a regression
  test that fails on pre-fix code, comma-safe failing-set parsing, and the
  M1 docstring correction — was stranded on the orphaned branch by the
  squash merge, so it lands as a direct cherry-pick PR onto `main`. Why: the
  code was already written and reviewed against; spinning a fresh unit would
  re-run the full plan/cycle machinery for a two-commit graft. Lesson:
  merging before the reviewer re-verify closes can strand the very fixes the
  re-review demands — the human stays the merge authority, but the loop
  should close first.

## Open questions

- Daemon poll interval: constant 5s vs. per-session adaptive backoff
  (defer to Phase 3).
- GitHub webhook receiver as a polling alternative (possible Phase 6).
- Separate vs. subprocess daemon already decided (separate); revisit only
  if secrets/auth plumbing proves painful.

## History

This file previously described a different, never-built feature
("Defense-in-depth — server-side enforcement and prompt versioning":
marker-protocol validation, a worker-side pre-push scope hook, and prompt
version headers). That scope depended on the old, also-renumbered
F-015 ("structured tool-call markers") and is preserved in `BACKLOG.md`,
recoverable at git `f8f9015:features/F-016/spec.md`. The pre-push scope
hook in particular remains worth revisiting as defense-in-depth.
