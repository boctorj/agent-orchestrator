# PROPOSAL: non-blocking orchestrator (F-016)

Status: draft · Date: 2026-05-21

## TL;DR

The lead chat blocks for 5-30 minutes on every `spawn_unit` and
`cycle_review` call because those MCP tools **do the dispatching, the
waiting, the marker parsing, AND the state writes** in a single
synchronous function. Workers run remotely on Anthropic's Managed
Agents — the orchestrator is just polling — but the lead session can't
accept user input until the poll loop finishes.

This proposal separates the **dispatcher** (fast: submit + record) from
the **watcher** (slow: poll + parse + advance) and moves the watcher
into a long-lived background daemon. MCP tools become RPC: every call
returns in ≤2 seconds. The daemon owns the state machine. The lead's
role collapses to *plan → approve → react to push notifications*.

This is the natural continuation of the headless-daemon work scoped in
[`PROPOSAL-feature-spec-and-headless-daemon.md`](PROPOSAL-feature-spec-and-headless-daemon.md)
§ Phase 3 — F-006/F-007/F-009 (merged) laid the foundations; this
proposal completes Phases 2 and 3 of that doc and adds concrete API
surface, migration steps, and acceptance criteria.

**F-015 is absorbed into this proposal.** F-015 ("promote unit-health
probe shadow decisions to automated transitions") is the same
state-machine work as Phase 3's reconciler. The F-014 unit-health
probe stays — its shadow-decision telemetry becomes F-016's
validation harness: during the first week of Phase 3 rollout, run
F-014's shadow decisions side-by-side with the daemon's actual
transitions and flag any divergence. F-015 should be marked
obsoleted-by F-016.

## Motivation — the user-visible flaw

Every chat session has the same shape:

```
User: spawn F-009-U-3
Lead: calling spawn_unit(...)
[15 minutes elapse — chat is frozen, user cannot interject]
Lead: coder opened PR #46. Running cycle_review...
[30 more minutes elapse — chat still frozen]
Lead: reviewer approved. PR awaits your merge.
```

During those 45 minutes, the lead can't answer "what's F-007 doing?",
can't plan another feature, can't react to a question. The system is
effectively single-threaded against a human's attention.

Worse, that frozen turn is fragile:

- If the lead's Claude Code process is killed, the MCP server's
  blocking thread dies too. The orchestrator may have submitted work
  to a Managed Agent but never persisted the session_id ([`tools/execution.py:153`](../orchestrator/tools/execution.py#L153) returns `(session_id, response)` only after the agent finishes; partial failures leave state.db with `status="coding"` and `coder_session_id=""` — see the F-014-U-1 ghost row).
- Restart recovery (`list_in_flight` → `resume_unit` → `tail_worker` →
  manual decisions, [`tools/ops.py:269-450`](../orchestrator/tools/ops.py)) exists *entirely* to repair this hole.
  Every failure mode F-009/F-013 fixed traces back to the same root
  cause: there is no persistent watcher.
- `parallel_units` ([`tools/scheduling.py:181`](../orchestrator/tools/scheduling.py#L181)) is a workaround that
  fans the blocking pattern into a thread pool — but the lead still
  blocks until every unit finishes.
- The cap-3 retry loop, CI wait, Copilot wait — all live inside
  `cycle_review` ([`tools/execution.py:2258`](../orchestrator/tools/execution.py#L2258)). All block.

The blocking is a design choice from when the orchestrator was a
single-user toy. At the current scale (5-10 simultaneous features),
it's the dominant pain point.

## Goals and non-goals

**Goals:**
- Every MCP tool call returns in ≤2 seconds.
- The lead chat can interleave planning, approval, and status queries
  while units are mid-cycle.
- A killed lead session does not strand work; the daemon keeps
  driving units to terminal state.
- Existing markers, prompts, state.db schema, and worker backends
  remain unchanged (additive only).
- Migration is gradual — each phase ships independently, every
  existing MCP tool keeps working at every step.

**Non-goals:**
- Replacing the cap-3 retry contract or the marker grammar.
- Eliminating ntfy push (it remains the human-attention channel).
- Multi-tenancy / cross-machine daemons (single user, single host).
- Stream-driven updates pushed *into* the chat. The chat is the
  command surface; updates land in state.db + ntfy + the dashboard.
- Replacing Managed Agents with a different worker backend
  (orthogonal — see [`PROPOSAL-docker-workers.md`](PROPOSAL-docker-workers.md)).

## Architecture

Today (blocking):

```
┌──────────────────────────┐
│ Lead chat (Claude Code)  │
│                          │
│ spawn_unit() ┐           │
│              ▼           │
│       MCP tool blocks    │
│       for 15-30 min      │
│         │                │
│         │ worker.spawn() │  ← blocks waiting for marker
│         │                │
│         ▼                │
│       parse marker       │
│       update state.db    │
│       return to chat     │
└──────────────────────────┘
```

Proposed (non-blocking):

```
┌──────────────────────────┐    ┌──────────────────────────────┐
│ Lead chat (Claude Code)  │    │ orchestrator daemon (host)   │
│                          │    │                              │
│ spawn_unit() ─┐          │    │ for unit in active_units:    │
│               │ ≤2s      │    │   tail_messages(session_id)  │
│       MCP tool dispatch  │    │   if terminal marker:        │
│         ▼                │    │     record event             │
│       worker.spawn()──┐  │    │     advance state            │
│       record session  │  │    │     trigger next phase       │
│       return job_id   │  │    │   if no marker yet: continue │
│                       │  │    │                              │
│ <─────────────────────┘  │    │ on terminal:                 │
│                          │    │   ntfy push to user          │
│ (chat free for next msg) │    │   write cycle log            │
└──────────────────────────┘    └──────────────────────────────┘
        ▲                                     │
        │                                     │
        └─── reads state.db ──────────────────┘
             (show_dashboard, list_in_flight,
              unit_history, tail_worker)
```

The MCP tools become the **lead's view** of the daemon. Lead never
calls worker APIs directly for waiting.

## Phase 0 — Idempotent marker recording (foundation)

**Goal:** Make the marker parser safe to re-apply to the same session
multiple times. Without this, the daemon's polling loop would write
duplicate events every time it re-scans a session.

Currently [`_record_terminal_marker`](../orchestrator/tools/execution.py) inserts a fresh
`unit_events` row on every call ([`state.py:620+`](../orchestrator/state.py)). The
parser is deterministic but the storage is not.

**Changes:**

1. Add `unit_events.dedupe_key TEXT` column (nullable; migration is
   additive).
2. Compute `dedupe_key = sha256(f"{session_id}|{cycle_number}|{event_type}|{marker_payload}")`
   for terminal-marker events. **`cycle_number` is critical** — the
   same role can legitimately emit the same marker payload in
   different cycles (e.g., coder pushes `FIX_PUSHED: added validate_state()`
   in cycle 1, then again in cycle 3 after a regression). Without
   `cycle_number` in the hash, the second emit gets silently dropped.
3. `INSERT OR IGNORE` on the row with a UNIQUE index on `dedupe_key`.
4. Add a pure function `orchestrator.markers.scan_response(role, text)`
   that returns the parsed `MarkerSpec` (or `None`) — moves the marker
   regex application out of execution.py and into a stateless module
   the daemon can also call.
5. Add MCP tool `scan_unit_session(unit_id, role)` — read-only:
   fetches `worker.tail_messages(session_id)`, runs `scan_response`,
   returns the parsed marker (does NOT write — caller decides). Useful
   for manual triage before Phase 3 lands.

**Acceptance:**
- Calling `_record_terminal_marker` twice on the same response writes
  exactly one event row.
- Existing tests in `tests/test_state.py` and the F-009 recording
  tests pass unmodified.
- New test: `scan_response("reviewer", "...REVIEW_RECOMMEND_MERGE: clean...")`
  returns `MarkerSpec(event_type="reviewer_recommend_merge", ...)`.

**Why this first:** Phase 0 has zero behavioral change for current
users. It unblocks every subsequent phase. Ships in ~1 day.

## Phase 1 — Non-blocking spawn primitives

**Goal:** Lead can dispatch a coder, then immediately answer a user
question without waiting for the PR to open.

**Good news — half the work already exists.** The worker protocol's
managed-agent implementation already has
[`spawn_async`](../orchestrator/workers/managed_agent.py#L188)
(returns `session_id` in ~1-2s) and
[`wait_idle`](../orchestrator/workers/managed_agent.py#L207)
(blocks for the response, with timeout). Phase 1 is mostly MCP wiring
+ adding a mirror `resume_async()` for the resume path.

**API surface (new MCP tools):**

```python
spawn_unit_async(feature_id, unit_id) -> {
    "unit_id": "F-016-U-1",
    "session_id": "sess_abc...",
    "status": "coding",
    "submitted_at": "2026-05-21T07:11:08Z"
}
# Returns in ~2 seconds. Persists session_id immediately.

wait_unit(unit_id, role, timeout_s=600) -> {marker?, status, reason?}
# Optional explicit wait. Returns on terminal marker OR timeout.
# On timeout: returns {status: "still_running", reason: "timeout"}
# — caller decides whether to retry, escalate, or hand off to daemon.

scan_unit_session(unit_id, role) -> {marker, written_event_id, status}
# Phase 0 read tool; optionally writes the event if a fresh terminal
# marker is found.
```

**Implementation:**

1. Add `resume_async(session_id, msg) -> None` to
   [`ManagedAgentWorker`](../orchestrator/workers/managed_agent.py#L241):
   sends the user-message event without waiting for the response.
   Same shape as `spawn_async`'s send half, ~6 lines.
2. Add the matching `Worker` protocol methods in
   [`workers/base.py`](../orchestrator/workers/base.py).
3. `spawn_unit_async` calls `worker.spawn_async` (already exists);
   persists `coder_session_id` before returning.
4. `wait_unit` calls `worker.wait_idle` + Phase 0 `scan_response` +
   `_record_terminal_marker`.
5. Existing `spawn_unit` retained unchanged for compat.

The Docker backend ([`docker_claude_code.py`](../orchestrator/workers/docker_claude_code.py))
may need a parallel `spawn_async`/`wait_idle`/`resume_async` trio if
it doesn't already split them — verify before scoping.

**Acceptance:**
- `spawn_unit_async` p95 latency < 3 seconds on Managed Agents backend.
- After `spawn_unit_async` returns, `get_unit_status` shows
  `status="coding"` and a non-empty `coder_session_id`.
- Killing the lead between `spawn_unit_async` and `wait_unit` leaves
  the unit in `coding` state with a live session_id; later
  `wait_unit` from a fresh lead session resumes correctly.
- F-014-U-1-style ghost rows (status=coding, session_id="") become
  structurally impossible because session_id is persisted before
  the worker call returns.

**Migration:** lead's standard flow gains an option:

```
# Old (still works)
spawn_unit(F-016, U-1)        # blocks 15 min
cycle_review(F-016, U-1)       # blocks 30 min

# New (non-blocking)
spawn_unit_async(F-016, U-1)   # ≤2s — answer user's other question now
# ...later, when ready...
wait_unit(F-016-U-1, "coder")  # block here if lead wants
```

Ships in ~3 days.

## Phase 2 — Decompose cycle_review into phase commands

**Goal:** Lead can drive the post-PR pipeline (tester → CI → reviewer
→ optional ultrareview) one phase at a time, interleaving user
interaction between phases.

The current 70-line `cycle_review` ([`tools/execution.py:2258`](../orchestrator/tools/execution.py#L2258))
sequentially calls `_wait_ci_with_fix_loop`, `_tester_phase`,
`_wait_ci_with_fix_loop`, `_copilot_phase`, `_reviewer_phase`,
optionally `_ultrareview_phase`, then `_emit_terminal`. Each
underscore-prefixed helper is already a complete phase boundary.

**New MCP tools (one per phase):**

```python
advance_to_tester(unit_id)    -> {status, next_action}
advance_to_reviewer(unit_id)  -> {status, next_action}
advance_to_terminal(unit_id)  -> {status, next_action}
```

Each is **idempotent** based on current `WorkUnitState.status`:
- Calling `advance_to_tester` when status is `in_ci` triggers CI-wait
  + tester spawn, returns when the tester phase completes or escalates.
- Calling it when status is already `reviewing` returns
  `{status: "already_past", next_action: "advance_to_terminal"}`.

**Lead drives the pipeline:**

```python
# Synchronous chain (lead chooses cadence)
advance_to_tester(F-016-U-1)
# ... lead handles other things ...
advance_to_reviewer(F-016-U-1)
# ... lead handles other things ...
advance_to_terminal(F-016-U-1)
```

Or one-shot:

```python
cycle_review(F-016-U-1)  # remains as a convenience wrapper that
                          # calls all three in sequence. Same outcome,
                          # same blocking, but now composed of public
                          # primitives.
```

**Acceptance:**
- All existing `cycle_review` integration tests pass when rewritten
  as three-step calls.
- `advance_to_*` is safe to call from a daemon (Phase 3 dependency).
- Restart mid-phase: after lead kill, calling `advance_to_X` from a
  fresh session resumes correctly (uses F-013's "resume or spawn"
  pattern at [`tools/execution.py:1517,1914`](../orchestrator/tools/execution.py#L1517)).

Ships in ~3-4 days. Has the side benefit of shrinking execution.py
by extracting phase logic into smaller modules.

## Phase 2.5 — Lead/daemon interaction contract

**Goal:** Define how the lead chat can influence a unit while the
daemon is driving it, without racing on state writes or worker
sessions.

**Guiding principle:** the orchestrator delivers messages and
respects locks; it does **not** decide whether a lead's message
overrides or extends prior context. That judgment belongs to the
worker agent — LLMs are precisely the thing that handles "is this
additive or replacing?" reliably. Building a structured
premise-replacement mechanism in the orchestrator would be strictly
more work for strictly less flexibility.

**Three primitives, three behaviors:**

| Primitive | Effect | Concurrency |
|---|---|---|
| Observe (`show_dashboard`, `tail_worker`, `unit_history`) | Read-only snapshot from state.db / worker tail | None — SQLite WAL handles concurrent R/W |
| Send a message (`send_to_unit`, `address_review(source="human")`) | Calls `worker.resume_async(session_id, msg)`; worker decides additive vs override | Lead-lock held during ~1s submit window only |
| Cancel (`cancel_unit`) | Archives worker sessions; marks unit `cancelled` | Sticky flag on `work_units`; daemon checks each tick |
| Graph mutation (`update_unit_deps`) | Re-shapes the DAG for future scheduling | Orthogonal — doesn't touch in-flight workers |

**Why no `pause`/`resume_unit_drive`:** every realistic pause use case
is better served by another primitive — "hold off committing" is a
`send_to_unit` message, "drain before downtime" is a daemon kill
switch, "halt this unit while I think" is either `cancel_unit` or
leaving the daemon alone for a few minutes. A first-class pause
introduces a halfway state where the worker keeps burning credits
producing output nobody will act on. Dropped.

**State.db additions:**

- `work_units.cancelled_at DATETIME` — terminal-cancel. Daemon
  archives sessions on next tick, then stops driving.
- `work_units.owner TEXT` (also in Phase 3) — CAS for terminal
  advances; prevents lead/daemon double-write.

(The earlier draft had `lead_lock_until` and `paused_at` columns;
both are gone — see below.)

**The lock collapses because `worker.resume` becomes async:**

The original concern was `worker.resume` racing `worker.tail_messages`.
But `worker.resume` is being split into submit/wait too (Phase 1
adds `resume_async`). `send_to_unit` now sends the user-message
event in ~1s and returns; the worker's response comes back later
via the daemon's normal poll. There's no long blocking window to
race against.

```python
def send_to_unit(unit_id, role, message):
    # ~1 second total. No 60s TTL, no heartbeats.
    with state.lead_advance_lock(unit_id):     # in-process or short DB lock
        # claim the unit briefly so daemon's same-tick read sees us
        worker.resume_async(session_id, message)
    return {"delivered": True, "session_id": session_id}
    # Daemon's next poll picks up worker's response naturally —
    # Phase 0 idempotency ensures any new marker is recorded once.
```

The brief lock window (the `with` block) just ensures the daemon's
poll loop doesn't fire `advance_state_machine` on a unit whose
worker is mid-message-send. After the with block exits, the daemon
is free to do whatever — it'll re-derive the right action from
`(unit_status, latest_marker)` on its next tick.

**Lock granularity — per-unit, not per-role:**

The lead-advance-lock applies to the **entire unit's state machine**,
not just the role being messaged. If it were per-role, the daemon
could advance the reviewer while the coder is still receiving the
lead's submit. Per-unit ensures a consistent snapshot. Since the
lock is held for ~1s (just the submit window), the cost is
negligible.

**Routing rule for `send_to_unit(unit_id, message, role=None)`:**

When `role` is omitted, default by current `WorkUnitState.status`:

| Unit status | Default role |
|---|---|
| `coding`, `in_ci`, `fixing` | coder |
| `testing` | tester |
| `reviewing` | reviewer |
| `escalated` | coder |
| `approved_awaiting_merge` | error — PR is done |
| `done`, `cancelled` | error — unit is terminal |

Lead overrides explicitly when intent diverges from current phase.
No heuristics on message content; no auto-fallback to a different
role on delivery failure.

**Not-actionable delivery responses:**

A targeted role can be non-actionable in four cases: `session_id`
empty, session `terminated`, session archived, or unit is in a
terminal status. `send_to_unit` returns a **structured error** with
per-role diagnostics so the lead can surface options to the user:

```python
{
  "delivered": False,
  "reason": "reviewer_session_terminated",
  "session_id": "sess_abc...",
  "role_diagnostics": {
    "coder":    {"status": "idle", "actionable": true},
    "tester":   {"status": "idle", "actionable": true},
    "reviewer": {"status": "terminated", "actionable": false}
  },
  "next_steps": [
    "send to coder or tester (both idle)",
    "re-spawn reviewer via cycle_review",
    "cancel the unit"
  ]
}
```

No silent fallback — sending the lead's message to a role they
didn't pick would corrupt intent worse than failing.

**Stale-marker handling (reviewer specifically):**

If the lead messages the coder during `reviewing` and the coder
pushes a new commit, the reviewer's marker (if already emitted)
will be on the **pre-push** code — stale relative to the new PR
state. The daemon's re-scan after lock expiry must detect this:

```
case A — reviewer.reviewed_sha == pr.head_sha:
    reviewer's marker is valid; record as terminal normally

case B — reviewer.reviewed_sha < pr.head_sha (coder pushed):
    reviewer's marker is stale
    record as `reviewer_stale_marker_pending_delta` event (audit)
    worker.resume(reviewer_session_id, delta-review prompt)
    reviewer reassesses on SHA_new; emits fresh marker
    daemon picks up the fresh marker on next tick

case C — reviewer hasn't emitted yet (session still running):
    no-op; next tick catches the marker
```

This reuses the existing F-013 delta-review machinery in
[`tools/execution.py:1806`](../orchestrator/tools/execution.py#L1806) —
the daemon just calls it from the reconcile loop instead of inline
in `_reviewer_phase`. Review **does not halt**; the reviewer
session is reused, its scope shifts from "review the original" to
"reassess given the coder's new commits."

**Primitive selection — when each applies:**

- "Switch from OAuth to SAML" → send a message. Worker decides what
  to do with its in-flight code.
- "Stop U-3, we're scrapping this approach" → `cancel_unit`.
- "U-3 also depends on U-2 now" → `update_unit_deps`. Scheduling
  graph, not worker state.

**Edge cases:**

1. **Lead crashes mid-`send_to_unit`.** Submit is ~1s; worst case
   the user-message event isn't sent and the lead's reply to user
   is missing. No state corruption. User retries the send.
2. **Two leads send to U-3 concurrently.** Both submits race;
   Anthropic's session-event queue serializes them. Worker receives
   both messages in some order and reasons about them. Lead-advance
   lock just ensures the daemon doesn't fire `advance_state_machine`
   between the two submits.
3. **Lead and daemon both reach terminal marker simultaneously.**
   CAS on `owner` picks a winner; Phase 0 dedupe_key makes the
   loser's write a no-op.
4. **Lead messages coder during `reviewing`, coder pushes.**
   Stale-marker rule (next section) triggers delta re-review.
   Review continues on the reviewer's same session; no halt.

**Acceptance:**
- `send_to_unit` returns in ≤2s. No duplicate events. Daemon picks
  up worker's response on next tick.
- `cancel_unit` archives the worker session and marks the unit
  `cancelled`; downstream dep-evaluation treats it as not-done.
- CLAUDE.md describes the three primitives: "Observe any time.
  Send-message is ~1s. Cancel is sticky. Graph edits are orthogonal."

Ships in ~2 days. Prerequisite for Phase 3 (daemon must honor
`cancelled_at` and the advance-lock from day one).

## Phase 3 — Watcher daemon

**Goal:** A background process tails all active sessions, applies
marker grammar, advances state, triggers next phases — **without
any chat in the loop**.

**Process model:**

```
orchestrator daemon start    # starts background process, writes pid file
orchestrator daemon stop     # graceful shutdown
orchestrator daemon status   # alive? lag? in-flight count?
orchestrator daemon logs     # tail recent decisions
```

The daemon is a plain Python event loop ([`orchestrator/daemon.py`](../orchestrator/daemon.py),
new). One process, one host, owns the entire state machine.

**Main loop (level-triggered reconciliation, pseudocode):**

```python
while not shutdown:
    units = state.list_in_flight()
    for unit in units:
        if unit.cancelled_at or state.has_active_advance_lock(unit):
            continue
        action = derive_next_action(unit)   # pure: (unit_state, latest_markers) → action
        if action:
            execute(action)                  # idempotent
    sleep(POLL_INTERVAL_S)   # default 5s, configurable
```

**Idempotent transitions — the load-bearing rule:**

The daemon does NOT track "what events have I already processed."
Instead, on every tick, it re-derives the correct next action from
the unit's current state and the latest observed markers:

```python
def derive_next_action(unit) -> Action | None:
    role_states = {
        role: read_latest_marker(unit, role)   # from unit_events, Phase 0
        for role in ("coder", "tester", "reviewer")
    }
    match unit.status:
        case "coding":          # PR not yet open
            if marker := role_states["coder"]:
                return ApplyCoderMarker(marker)  # PR_URL or BLOCKED
        case "in_ci":            # CI not yet green
            return WaitCi(unit)
        case "testing":
            if marker := role_states["tester"]:
                return ApplyTesterMarker(marker)
        case "reviewing":
            if marker := role_states["reviewer"]:
                return ApplyReviewerMarker(marker)
        # ...
    return None  # no action needed this tick
```

Every `Apply*` action is a function of `(current_status, marker)`.
Calling `ApplyTesterMarker(TESTS_PASS)` when unit is already in
`reviewing` (because that transition already happened) is a no-op —
the action's first check is "is the status what I expect?" If not,
return without doing anything.

This is the Kubernetes-controller pattern: the daemon is
**stateless between ticks**. Restart it, kill it, run it concurrently
with a stale instance — the worst case is duplicate work that gets
deduped by Phase 0's `dedupe_key`. There's no "last_observed_event_id"
column to drift out of sync with reality.

**Crash recovery is automatic:** on daemon startup, the loop just
runs. Every unit's next action is re-derived from current state.
If a transition was partially applied (event written but
`work_units.status` not yet updated), the next tick completes it.
No special recovery path.

**Concurrency / claiming:**

The proposed claim mechanism from [`PROPOSAL-feature-spec-and-headless-daemon.md`](PROPOSAL-feature-spec-and-headless-daemon.md)
§ Phase 2 lands here:

1. Add `work_units.owner TEXT` (nullable) — `"daemon"` or `"lead"`.
2. Daemon `INSERT ... ON CONFLICT` to atomically claim a unit before
   advancing it. Lease expires after N minutes if daemon dies; lead
   can manually reclaim.
3. Lead-driven `cycle_review` sets `owner="lead"` for the duration —
   daemon skips lead-owned units to prevent double-driving.

**Crash recovery — falls out of the level-triggered design:**

There is no special startup path. The daemon's main loop is its
own recovery: each tick, every in-flight unit's next action is
re-derived from state.db. If a marker arrived during the outage,
the next `tail_messages` call sees it, records it via Phase 0
`INSERT OR IGNORE`, and the following tick's `derive_next_action`
triggers the right transition.

Worst case after a crash: one full poll interval (5s default) of
recovery latency per unit. No manual `resume_unit` required.

**Singleton enforcement — SQLite-backed, not pidfile:**

Pidfiles are fragile (force-quit orphans, NFS, launchd races).
Use a SQLite lock table instead:

```sql
CREATE TABLE daemon_locks (
    state_db_path TEXT PRIMARY KEY,
    holder_id     TEXT NOT NULL,   -- random uuid per daemon start
    heartbeat_at  DATETIME NOT NULL,
    started_at    DATETIME NOT NULL
);
```

Daemon heartbeats every 5s; takeover is allowed if `heartbeat_at`
is older than 30s. A PID file is still written for the CLI's
`status`/`stop` commands but is a hint, not authoritative.

**Daemon per workspace, not per host:**

The user may have multiple workspaces with their own `state.db`
files (per `memory/user_multi_orchestrator.md`). Daemon singleton
is keyed by the **absolute `state.db` path** in the lock table
above, not by hostname. Two workspaces = two daemons,
transparently, each owning its own state.db. The CLI's
`orchestrator daemon start` resolves the right state.db from the
current working directory, the same way the MCP server does today.

**macOS auto-start:** ship a launchd plist template in
[`docs/`](../docs/) so the daemon survives reboots. Optional — running
manually via `orchestrator daemon start` is the default. The plist
must be parameterized by workspace path to allow multiple daemons
across multiple workspaces.

**Acceptance:**
- Killing the lead mid-cycle: daemon continues driving the unit;
  state advances to `approved_awaiting_merge`; ntfy push fires.
- Fresh lead session sees the unit's final state via
  `show_dashboard` without any manual `resume_unit` ceremony.
- Daemon survives MCP server restart (it's a separate process).
- Two daemons cannot run simultaneously for the same state.db
  (SQLite `daemon_locks` row + heartbeat takeover).
- Two daemons for *different* workspaces (different state.db paths)
  coexist without contention.
- Daemon crash → restart → no duplicate events written (Phase 0
  idempotency).

Ships in ~1-2 weeks. Biggest single chunk of work in the proposal.

## Phase 4 — Lead becomes pure dispatcher

**Goal:** Lead's standard flow stops blocking entirely. `cycle_review`
becomes "enqueue this unit for the daemon."

**Default-flip is conditional on `NTFY_TOPIC` being set:**

Without ntfy, the lead has no feedback channel when the daemon
finishes work — `cycle_review` would return in 1s and the chat
goes quiet. That's a worse UX than today's blocking flow.

So the default is:
- **`NTFY_TOPIC` set →** `cycle_review` defaults to non-blocking
  (daemon mode). Lead returns in ≤1s; user gets push notification
  on terminal.
- **`NTFY_TOPIC` unset →** `cycle_review` stays blocking (today's
  behavior). The lead emits a one-time setup nudge in chat:
  "Set NTFY_TOPIC to enable non-blocking mode — see README."

Users can override with explicit `cycle_review_async` / `cycle_review_blocking`
variants regardless of env config.

**Tool semantics change (additive — old behavior reachable via
new explicit names):**

```python
# Default behavior — depends on NTFY_TOPIC
cycle_review(feature_id, unit_id) -> str | dict
# With NTFY: returns in ≤1s, daemon drives, ntfy push on terminal.
# Without NTFY: blocks like today's cycle_review; nudges user.

# Explicit variants (always available)
cycle_review_async(feature_id, unit_id) -> dict     # ≤1s, daemon-driven
cycle_review_blocking(feature_id, unit_id) -> str   # today's behavior
```

**No parallel state machine — `cycle_review_blocking` calls the
daemon's engine in-process:**

The blocking path does NOT duplicate the transition table. Instead,
it calls the same `derive_next_action` + `execute` engine as the
daemon, in a tight loop:

```python
def cycle_review_blocking(feature_id, unit_id):
    while True:
        action = derive_next_action(get_unit(unit_id))
        if action is None:
            return _emit_terminal(...)
        execute(action)         # idempotent; may call worker.wait_idle
        if is_terminal(action.next_status):
            return action.summary
```

Same transition table, same `Apply*` actions, just driven from the
calling thread instead of the daemon's poll loop. Two callers of one
engine. If both run concurrently (lead + daemon both think they own
the unit), the `owner` CAS picks a winner per-tick and the loser's
`execute` no-ops — same protection that lets the daemon survive
restart.

**Lead's standard flow simplifies:**

```python
# Today (blocking, 30+ minutes per unit on the lead's clock):
spawn_unit(F-016, U-1)
cycle_review(F-016, U-1)
# user must wait

# After Phase 4:
spawn_unit_async(F-016, U-1)
cycle_review(F-016, U-1)
# lead returns to chat in ~3s. Daemon takes over. User gets ntfy push
# on terminal state. Lead's role: plan → approve → react.
```

`parallel_units` and `parallel_units_global` become **thin loops**
that call `spawn_unit_async` + `cycle_review` (queue variant) for
each ref. Daemon already handles concurrency naturally — every active
session is tailed independently. The cap from the explicit thread
pool becomes a daemon-side `MAX_CONCURRENT_DRIVES` knob.

**Acceptance:**
- `cycle_review` p95 latency < 2 seconds.
- `parallel_units_global([10 refs])` returns in < 5 seconds total
  (vs. today's 30+ minutes).
- ntfy push fires correctly for daemon-driven terminal transitions.
- Existing chat-driven UX patterns (planning, approval, escalation
  triage) continue to work unchanged.

Ships in ~3 days (mostly wiring + tests; the hard work is in Phase 3).

## Phase 5 — Cleanup

**Goal:** Retire the blocking implementations that Phases 1-4 made
redundant.

- Remove the `wait` half of the old `spawn`-as-monolith from
  `execution.py`. The blocking helpers (`_wait_ci_with_fix_loop`,
  `_tester_phase`, `_reviewer_phase`, `_ultrareview_phase`) move to
  [`orchestrator/cycle/phases.py`](../orchestrator/cycle/phases.py) (new)
  where both the daemon and the explicit `cycle_review_blocking`
  call them.
- Delete `parallel_units` / `parallel_units_global` thread-pool
  internals once daemon-driven concurrency proves itself in
  production.
- Update [`CLAUDE.md`](../CLAUDE.md) lead persona to the new mental
  model — daemon drives execution, lead reacts.
- Document daemon operations in [`README.md`](../README.md) and a new
  `docs/DAEMON.md`.

execution.py: 2445 lines → estimated ~700 lines after Phase 5.

Ships in ~2 days.

## Risks

**R1: Daemon as a single point of failure.** If the daemon dies, units
freeze until it restarts. Mitigation: launchd plist (auto-restart),
the daemon's first action on startup is to reconcile via Phase 0
idempotent re-scan, and `list_in_flight` continues to work from
state.db without the daemon. Worst case: user runs
`cycle_review_blocking` as a fallback.

**R2: Stale-session marker re-processing.** Solved by the
level-triggered design — `derive_next_action` is a pure function of
current state; duplicate event writes are deduped via Phase 0
`dedupe_key`; the `owner` CAS is the second line of defense for
transitions, not events.

**R3: Worker API rate limits.** Tailing N active sessions every 5s
multiplies API calls. Mitigation: exponential backoff on idle
sessions (a session that's been "running" for 10 min gets polled
every 30s, not 5s). Cap on concurrent polled sessions.

**R4: Lead and daemon both editing state.db.** SQLite handles
concurrent readers + a single writer. With WAL mode (already set
in `state.init_db`), this is fine. The `owner` column prevents
double-drives.

**R5: User confusion over async semantics.** The user may run
`spawn_unit_async` then close their laptop, missing that the daemon
continues. Mitigation: ntfy push is the primary feedback channel
(already exists, F-005); the dashboard script is the secondary
channel. Document clearly that the daemon must be running for async
flow to drive units to completion.

**R6: Phase rollout breaks the lead's mental model mid-migration.**
Each phase is additive — old MCP tools keep working. Users opt into
the new flow when ready. CLAUDE.md updates happen in Phase 5, not
piecemeal.

## Migration sequence

1. **Phase 0 (1 day, no behavior change):** dedupe_key, scan_response,
   scan_unit_session. F-016-U-1.
2. **Phase 1 (2 days, opt-in async):** wire `spawn_async`/`wait_idle`
   into MCP; add `resume_async`. F-016-U-2.
3. **Phase 2 (4 days, opt-in async):** phase commands exist alongside
   `cycle_review`. F-016-U-3.
4. **Phase 2.5 (2 days, interaction contract):** advance-lock,
   `cancel_unit`, `update_unit_deps`. Prerequisite for the daemon.
   F-016-U-4.
5. **Phase 3 (1-2 weeks, daemon ships):** level-triggered reconciler
   with idempotent transitions. Daemon-driven mode opt-in via env var
   `ORCH_DAEMON_DRIVE=true`. F-016-U-5.
6. **Phase 4 (3 days, default flip):** `cycle_review` defaults to
   daemon mode; `cycle_review_blocking` is the opt-out. F-016-U-6.
7. **Phase 5 (2 days, cleanup):** remove redundant blocking code.
   F-016-U-7.

Total: ~4-5 weeks of work. No phase ships a regression — every
intermediate state is fully usable.

## Open questions

- Should the daemon be a separate process or an MCP server subprocess
  that survives the lead? Separate process is the safer answer
  (decoupled lifetimes). MCP subprocess would simplify auth/secrets
  but couples lifetimes — rejected.
- Daemon's polling interval — 5s default, but should it be
  per-session adaptive based on observed phase duration? Defer to
  Phase 3 implementation; constant 5s is the simplest start.
- Webhook receiver for GitHub events (PR merge, CI complete) as an
  alternative to polling? Possible Phase 6 follow-up; out of scope
  here. Polling works and is simpler to ship.
- Multi-host daemon (one daemon per machine) — out of scope; the
  current single-user model assumes one daemon per workspace.

## Acceptance for F-016 overall

When all seven units are merged:

1. `spawn_unit`, `cycle_review`, `parallel_units_global` all return
   in ≤3 seconds.
2. A lead session killed mid-cycle does not strand the unit; daemon
   completes the cycle and ntfy fires.
3. `list_in_flight` + `resume_unit` + `tail_worker` continue to
   work (they're read-side and unchanged).
4. All existing tests pass; new tests cover the daemon's claim,
   polling, and recovery paths.
5. CLAUDE.md describes the new mental model; the lead's persona is
   "react to state, don't drive execution."
