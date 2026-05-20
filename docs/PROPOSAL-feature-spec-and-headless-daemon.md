# PROPOSAL: feature spec + cycle logs + headless daemon

Status: draft · Date: 2026-05-14

## TL;DR

Three changes that let the orchestrator run 20+ features simultaneously
without the lead's chat context bloating into hallucination territory:

1. **Per-feature `spec.md`** — durable, version-controlled feature design
   doc. Replaces chat history as the source of truth for intent,
   decisions, and acceptance criteria.
2. **Per-unit cycle log (`features/F-XXX/U-N.md`)** — auto-generated
   markdown summary of what each unit shipped: final PR description +
   findings + fixes + spec deviations. Loaded by downstream units.
3. **Headless daemon** — background Python process that drives
   `cycle_review` autonomously without the lead's chat in the loop.
   Worker agents (coder/tester/reviewer) still run as LLM-backed Managed
   Agents — what changes is that the lead session no longer absorbs
   their JSON returns. Removes ~80% of today's chat-resident tool-call
   traffic.

Together: the lead's role collapses to **plan + approve + escalate +
merge**. State.db + spec.md + cycle logs are the durable substrate; the
chat becomes ephemeral working memory.

## Motivation

At 20 simultaneous features under the current architecture, a realistic
week of work pushes 400–700K tokens through one lead session:

- ~80 units × 1–3 cycles each = 80–240 `cycle_review` invocations,
  500–2000 tokens of JSON returned to chat per call.
- Planning conversations for 20 features add ~200K tokens.
- Status polling (`show_dashboard`, `unit_history`) adds another 100K+.
- Cross-feature reasoning lookups add 20–50K.

This approaches Opus 4.7's 1M ceiling well before quality degrades from
auto-compaction. Symptoms: lead mis-attributes decisions across features,
"hallucinates" prior conversations, forgets why scope was set 3 weeks ago.

The root cause is **load-bearing context living in volatile chat
history**. Two paths that don't fix it: session hygiene (still bleeds
when user asks the lead for status) and ephemeral sessions (loses
continuity entirely). The fix is to **externalize load-bearing context
into durable files** so any session can re-load it, and to **stop
absorbing execution chatter into the chat at all**.

## Goals and non-goals

**Goals:** run 20+ features without context bloat; preserve decision
continuity across sessions and weeks; keep the phone / Remote Control
workflow unchanged; phase the rollout so each piece ships standalone.

**Non-goals:** replacing the lead with a fully autonomous agent (lead
still handles planning, escalation triage, cross-feature decisions);
exceeding Anthropic's existing concurrency limits (daemon respects
`max_concurrent=5`); cross-repo or cross-account work.

## Architecture

```
Persistent layer (no LLM, survives restarts)
─────────────────────────────────────────────
features/F-XXX/spec.md        ← intent, decisions, acceptance criteria
                                git history = decision log
features/F-XXX/U-N.md         ← per-unit cycle log, auto-written on
                                terminal state
state.db                      ← plans, units, events, costs
GitHub PRs                    ← canonical PR convo; mirrored into cycle
                                logs on terminal state

Execution layer (background, no lead chat)
─────────────────────────────────────────────
orchestrator daemon           ← poll state.db, atomic-claim ready units,
                                drive cycle_review, write cycle log,
                                push ntfy on escalation / ready-to-merge
                                (worker agents — coder/tester/reviewer —
                                 are still LLM-backed; only the daemon
                                 process itself is plain Python)

LLM layer (lead chat)
─────────────────────────────────────────────
Lead session
  - load_feature / save_plan / approve_plan      ← writes state.db + spec.md
  - feature_memory(F-X)                          ← session bootstrap
  - handle escalation (read spec + cycle log)
  - status (read state.db)
```

The lead never absorbs cycle_review JSON. State queries return compact
summaries. The chat carries only what's needed for the current decision.

## Components

### 1. Per-feature spec.md

Path: `features/F-XXX/spec.md`. Schema:

```markdown
# F-XXX: <title>

## Intent
<feature description, pre-filled from load_feature>

## Acceptance
_TBD — concrete, testable criteria for "done"._

## Out of scope
_TBD — hard boundary against scope creep._

## Approach
_TBD — high-level design choices, library / framework decisions._

## Constraints
_TBD — non-functional requirements (perf, security, compatibility)._

## Decisions
_TBD — non-obvious choices with reasoning. Grows over time._

## Open questions
_TBD — things still undecided. Resolved questions move to Decisions._
```

Every section other than Intent starts as a `_TBD — <hint>._` placeholder
so the lead can see at a glance what still needs filling in. See
[`docs/SPEC-FORMAT.md`](SPEC-FORMAT.md) for the canonical reference;
`orchestrator/feature_spec.py::render_template()` is the source of truth
for the literal bytes.

**Rules:**

- `load_feature` writes the starter template (title + description
  pre-filled). Lead edits it during planning.
- Lead edits spec.md when planning reaches a non-obvious decision, scope
  shifts, or an escalation triages to a design change.
- Every commit message MUST have a `Why:` line. The commit log IS the
  decision log; no separate `decisions.md`.
- Plan stays in state.db, not in spec.md. Plan is structured data the
  scheduler queries; mirroring it would risk drift.

### 2. Per-unit cycle log

Path: `features/F-XXX/U-N.md`. Schema:

```markdown
# F-007-U-2 — OAuth callback route

## PR
#42 · https://github.com/owner/repo/pull/42
Status: merged (2026-05-15 14:32 UTC)
PR head SHA: <headRefOid at terminal state>
Merge commit SHA: <mergeCommit.oid, captured when reconcile_unit_pr confirms merged>

## Coder's PR description (verbatim, as of merge)
[full PR body text captured at terminal state]

## Cycle history
3 cycles · cap-3 not hit · total cost $X.XX

### Cycle 1 — tester: BUG_FOUND
- callback returned 500 on invalid state param (oauth.py:42 · #r3239501818)
### Cycle 1 — coder fix: FIX_PUSHED
- added validate_state() before token exchange (commit <sha>)
### Cycle 2 — reviewer: REVIEW_REQUEST_CHANGES
- 🔴 token storage skipped Fernet wrapping (oauth.py:127 · #r3239501822)
- 🟠 missing test for refresh-token path (#r3239501823)
### Cycle 2 — coder fix: FIX_PUSHED
- wrapped via _wrap_token() before insert; added test_refresh_token
### Cycle 3 — reviewer: REVIEW_RECOMMEND_MERGE

## Spec deviations during this unit
None.  (or: "see spec.md commit <sha> — Fernet scope clarified")

## Links
- PR conversation · final commit · state.db unit_events
```

**Rules:**

- Auto-generated by the orchestrator on terminal state
  (`REVIEW_RECOMMEND_MERGE`, `REVIEW_COMMENT`, escalation, manual kill).
  Workers and the lead don't write cycle logs.
- Appended per-cycle, finalized at terminal state. **Immutable on the
  normal path** — the lead and workers never overwrite a finalized log.
  Two narrow exceptions: (a) post-merge SHA backfill (see below); (b)
  recovery via `regenerate_cycle_log(unit_id)` for orphaned logs or
  user-edited PR descriptions on GitHub — see Risks §4–5.
- Mirrored from GitHub at write time: `gh pr view --json title,body,headRefOid`
  for description and PR head SHA; GraphQL `reviewThreads` for findings;
  comment URLs preserved as deep links.
- **Two SHAs captured at different points:**
  - `headRefOid` (PR head at terminal state) — captured when the cycle
    log is first finalized (`REVIEW_RECOMMEND_MERGE` / `REVIEW_COMMENT` /
    escalation).
  - `mergeCommit.oid` (the actual commit on main) — captured later when
    `reconcile_unit_pr` confirms the PR has been merged (or the read-only
    `check_unit_pr` on a diagnostic call). Diverges from `headRefOid`
    for squash and rebase merges. The cycle log is amended once to add
    this field; that's the only post-finalization edit.

**Storage decision:** markdown files, NOT state.db. PR descriptions can
be 5–10KB; markdown is human-readable; git history of the file is a
free audit trail; external tools can grep `features/` without speaking
the state.db schema.

**Spec-deviation signaling:** if the diff diverges from spec, either
spec was updated mid-cycle (cycle log links the commit) or the
deviation is undocumented (reviewer flags as 🔴 per scope rules). Cycle
log captures the finding regardless.

**Persistence and commit strategy** (applies to both `spec.md` and cycle
logs in `features/`):

- The orchestrator workdir is a git repo. spec.md and cycle logs live
  inside it at `features/F-XXX/`. **Files are committed locally by the
  orchestrator/lead, never left as uncommitted working-tree state.**
  Uncommitted markdown in `features/` is a bug.
- **Who commits what:**
  - **spec.md** — committed by the **lead** with a `Why:` message it
    composes from the surrounding conversation. The lead's CLAUDE.md
    rule requires a commit per non-obvious decision.
  - **Cycle logs** — committed by the **orchestrator** automatically on
    each write (per-cycle append + terminal-state finalize + post-merge
    SHA backfill). Commit identity:
    `user.email=agent@orchestrator user.name=orchestrator-bot`,
    matching the per-command pattern workers already use.
- **Push policy:** auto-commit is **local only**; push is manual or via
  a periodic operator-run sweep. This keeps history without spamming the
  remote on every cycle. (`orchestrator daemon` does not push.)
- **Partial-write protection:** standard write-to-tmp-then-rename. The
  orchestrator writes to `features/F-XXX/U-N.md.tmp` then `mv`s to the
  final name before staging. A crash during write leaves the prior
  finalized version intact (or no file, for first-time writes).
- **Conflicts:** filenames don't collide (each unit owns its own file
  path; spec.md is per-feature). Concurrent writers on the same file
  are not expected. If the daemon and the lead ever race on `spec.md`,
  the loser observes the file changed and re-reads before retrying its
  commit (standard `git add` + `commit` flow handles this).
- **Branches:** all commits go to whatever branch the orchestrator
  workdir is currently on. Recommended operator practice: run the
  orchestrator on a long-lived `main` checkout; treat the auto-commits
  like a project journal that gets pushed at session end.

### 3. Atomic ready-unit claim

**Schema change required.** Today, `next_ready_units` returns units that
have no row in `work_units` yet — the "row exists" sentinel marks
spawned units. That model doesn't support atomic claiming because there's
nothing to UPDATE.

The fix is to **pre-create `work_units` rows at `approve_plan` time**
with a new `pending` status. After this change:

- `approve_plan(feature_id)` inserts one row per planned unit with
  `status='pending'`.
- `next_ready_units` returns `pending` rows whose deps are all `done`.
- `claimed` joins the status enum as a brief transitional state.

Atomic claim (uses Python-generated timestamp to match the existing
`orchestrator/state.py:_now()` convention):

```sql
UPDATE work_units
SET status = 'claimed', claimed_by = ?, claimed_at = ?
WHERE unit_id = ? AND status = 'pending'
RETURNING unit_id;
```

If the UPDATE returns a row, caller owns the unit. Otherwise someone
else already claimed it; caller moves on. `atomic_claim()` is a
primitive callable by either the daemon or the lead (via `spawn_unit`).
On the daemon path, the daemon claims first then orchestrates spawn +
cycle. On the lead path, `spawn_unit`'s entry performs the claim.
Either way, a claimed unit transitions to `coding` / `in_ci` / etc. via
the existing state machine; `claimed` is a brief transitional state only.

SQLite WAL mode handles concurrent readers/writers fine at this scale.

### 4. Headless daemon

New CLI: `orchestrator daemon`.

```python
# orchestrator/daemon.py — sketch
POLL_INTERVAL = 30

def main():
    setup_signal_handlers()  # SIGTERM → drain in-flight, exit
    while not shutdown_requested:
        for entry in state.next_ready_units_all():
            if state.atomic_claim(entry):
                executor.submit(_drive_unit, entry)
        sleep(POLL_INTERVAL)

def _drive_unit(entry):
    try:
        # Pending units need a PR opened first; in-flight units skip
        # straight to cycle_review.
        if not state.has_coder_session(entry.unit_id):
            execution.spawn_unit(entry.feature_id, entry.unit_id)
        result = execution.cycle_review(entry.feature_id, entry.unit_id)
        if result.outcome == 'escalated':
            ntfy.push_escalation(entry.unit_id, result.summary)
        elif result.outcome == 'approved_awaiting_merge':
            ntfy.push_ready_to_merge(entry.unit_id, result.pr_url)
    except Exception as e:
        state.release_claim(entry.unit_id, error=str(e))
        ntfy.push_escalation(entry.unit_id, f"daemon error: {e}")
```

Thread pool capped at `max_concurrent=5` (existing Anthropic-rate-limit
cap). Stateless across restarts — state.db is the source of truth.
Idempotent: re-running on a non-`ready` unit is a no-op (claim fails).
Logs to `~/.orchestrator/daemon.log` for operator debugging.

**Lead-facing MCP tools:** `daemon_status()` → `{running, pid,
last_tick_at}`, `daemon_pause()` (drain in-flight, stop claiming),
`daemon_resume()`. `orchestrator doctor` learns to probe daemon
liveness.

### 5. Feature memory bootstrap

New MCP tool: `feature_memory(feature_id) → str`. Returns a ~3–7K-token
blob (scales with completed-unit count): spec.md content + `git log -10`
of spec.md + `unit_summary` + cycle log "Final" sections (~500 tokens
per merged unit) + recent escalation events.

Lead calls this once at session start when discussing a specific
feature. Replaces "scroll through chat history" — fresh sessions
re-bootstrap from durable state, not from prior chats.

### 6. CLAUDE.md updates

New rules for the lead persona:

- Before discussing F-X, call `feature_memory(F-X)`. Don't rely on chat
  history.
- When making a non-obvious design decision, edit `features/F-XXX/spec.md`
  and commit with a `Why:` line.
- When daemon is running, don't call `cycle_review` yourself. Plan,
  approve, escalate, merge.
- Cycle logs are read-only history. To revise a past decision, edit
  spec.md (not the cycle log).

## Role prompt changes

All three role prompts (coder.md, tester.md, reviewer.md) receive new
context blocks in their task message:

| Block | When | Source |
|---|---|---|
| `## FEATURE SPEC` | always | `features/F-XXX/spec.md` (~1.5K tokens) |
| `## PREDECESSOR UNITS` | when deps exist | cycle log summaries (~500 each) |
| `## THIS UNIT'S CYCLE LOG` | reviewer, retry cycle ≥ 2 | own cycle log (~1.5K) |

Total worst-case task-message size: ~5K tokens (reviewer, 3 merged
predecessors, cycle 3). One-time at spawn/resume; doesn't accumulate.

### Coder

- Read spec FIRST. Unit description says what; spec says why and what
  success looks like. Build against both. If they conflict, spec wins
  and the coder flags the conflict in the PR description.
- Align with predecessor decisions (e.g., if U-2 picked validator Y,
  U-3 doesn't silently use X).
- PR description MUST include a `## Spec satisfaction` section:
  ```markdown
  ## Spec satisfaction
  Satisfies these acceptance criteria from features/F-XXX/spec.md:
  - [x] <criterion 1>
  - [x] <criterion 2>

  Deviations from spec:
  - <deviation>: <reason>   (or "None")

  Predecessor alignment:
  - Followed U-2's choice of validator Y (per F-XXX/U-2.md cycle 2)
  ```
- Re-read spec on every fix-loop resume — it may have changed mid-cycle.

### Tester

- Test against spec's acceptance criteria, not just the unit
  description. If spec says "Token refresh works without re-prompting"
  and the unit description doesn't mention refresh, the tester still
  writes the test.
- Scope violation → `BUG_FOUND`. If the diff touches code marked out of
  scope, file a failing test asserting the unrelated code shouldn't
  change.
- Cross-check predecessor decisions: tests must use the same validators
  / patterns / interfaces predecessor units adopted.
- BUG_FOUND inline review structure unchanged.

### Reviewer

- **Mandatory spec-vs-PR-description comparison.** For every claimed
  satisfied criterion → verify the diff actually satisfies it. For every
  claimed deviation → check it's reasonable, flag unreasonable ones 🔴.
  Undocumented deviations (diff diverges but PR description silent) →
  🔴. Spec criteria unaddressed AND not listed as deviations → 🟠.
- On retry cycles, read `## THIS UNIT'S CYCLE LOG` FIRST. Don't re-flag
  resolved findings.
- Predecessor consistency check: if U-2 decided X and U-3's PR silently
  does NOT-X, flag as 🟠 (spec needs update OR U-3 should align).
- Inline review structure unchanged (single `POST /reviews` with
  `comments[]`; tiers 🔴/🟠/🟡/🔵; any 🔴 or 🟠 → REVIEW_REQUEST_CHANGES).

## Lifecycles

### Planning + autonomous execution

```
User → Lead: "Add OAuth support with Google."

Lead:
  load_feature(...)                  → creates features/F-007/spec.md
  drafts plan, discusses with user
  edits spec.md, commits             → "spec: keep U-2 monolithic.
                                        Why: avoids DB-schema seam."
  save_plan(...) → approve_plan()

Session can end.

Daemon (next poll):
  - claims F-007-U-1, calls spawn_unit (opens PR), then cycle_review
  - on terminal: writes features/F-007/U-1.md, ntfy "ready to merge"
  - continues with U-2, U-3, U-4 as deps clear
  - when U-3 spawns: task message includes U-1 and U-2's cycle logs
    in PREDECESSOR UNITS block
```

### Escalation triage (fresh session)

```
Phone: "🚨 F-007-U-3 escalated: cap-3 on tester."

Lead (fresh chat):
  feature_memory("F-007")            → ~5K tokens: spec + cycle logs +
                                        unit_summary
  reads features/F-007/U-3.md        → all 3 cycles' findings
  diagnoses: skipped Fernet wrapping (U-2 did it correctly; spec
    mandates it; coder dropped it)
  proposes fix to user.

User: "Yes. Also clarify spec on wrapping scope."

Lead:
  edits spec.md, commits             → "spec: Fernet wraps stored
                                        tokens only. Why: U-3 escalation
                                        showed ambiguity."
  send_to_unit(F-007-U-3, "coder", "<pointer>")

Daemon resumes U-3 on next poll.
```

### Status check

```
"What's in flight?" → show_dashboard()      (read-only, <2K tokens)
"I merged U-2."     → reconcile_unit_pr()   (flips to done, daemon picks
                                             up U-4 on next poll)
```

## Decisions

Resolved during design discussion:

- **spec.md location:** in the orchestrator workdir's repo
  (`features/` inside agent-orchestrator). Git history works out of
  the box. Migrating to a separate repo later is straightforward.
- **Cycle logs are markdown files, not state.db rows.** See §2.
- **No separate `decisions.md`.** Git history of spec.md commit
  messages is the decision log.
- **Reviewer surfaces deviations, doesn't dictate which side fixes
  them.** Either coder revises code or lead updates spec. Cycle log
  captures whichever happened.
- **Cycle log token budget:** full text on disk (cheap, greppable);
  summaries (~500 tokens) injected into worker prompts.

## Open questions

1. **Cross-feature dependencies.** If F-008 depends on F-007 landing
   first, where does that live? `state.db features` could gain a
   `depends_on` column. For v1, leave as a planning concern; structured
   field can come later.

2. **Daemon lifecycle.** Manual start initially; document
   launchd/systemd unit as a Phase 4 follow-up.

3. **Cost telemetry under daemon.** Should the daemon push periodic
   cost summaries via ntfy? Add `cost_threshold_alert` later, not v1.

4. **Hung-session recovery.** If a worker session never idles, daemon
   loses a concurrency slot. Wire the existing 1800s worker timeout
   into the daemon's exception handler; release claim and ntfy-escalate
   on timeout.

5. **`unit_events.details` usage.** Some operational decisions
   ("retried with X") aren't spec-worthy but should be discoverable.
   Document writing structured `details` JSON when calling
   `address_review` / `send_to_unit`.

## Phases

### Phase 1 — spec.md + cycle logs + role prompts (no daemon)

Smallest piece that delivers value standalone.

- `load_feature` writes `features/F-XXX/spec.md`
- Cycle-log writer hooks into `cycle_review`'s terminal branches in
  `orchestrator/tools/execution.py`
- `compose_coder_task` / `compose_tester_task` / `compose_reviewer_task`
  inject `FEATURE SPEC`, `PREDECESSOR UNITS`, `THIS UNIT'S CYCLE LOG`
  blocks
- `feature_memory(feature_id)` MCP tool
- Role prompts (coder.md / tester.md / reviewer.md) updated per
  §"Role prompt changes"
- CLAUDE.md updates
- Documentation: spec format, commit-message-as-decision-log pattern,
  cycle log format

**Size:** 8–10 units · ~900 LOC + ~250 lines of prompt edits + tests.

**Ships:** decision continuity across sessions; downstream units see
predecessor decisions; reviewer enforces spec compliance.

### Phase 2 — atomic claim

Daemon prereq. Independently useful for multi-instance safety.

- Add `claimed` status to work_units enum
- Atomic claim primitive in state.py
- Update `spawn_unit` entry path
- Race tests

**Size:** 2–3 units · ~150 LOC + tests.

**Ships:** safe to run N orchestrator instances against one state.db.

### Phase 3 — daemon

- `orchestrator daemon` CLI subcommand
- `orchestrator/daemon.py`: poll loop, thread pool, signal handlers,
  pidfile, log rotation
- `daemon_status` / `daemon_pause` / `daemon_resume` MCP tools
- `orchestrator doctor` daemon-liveness probe
- CLAUDE.md updates: when NOT to call cycle_review
- Documentation: running, stopping, debugging

**Size:** 5–7 units · ~600 LOC + tests.

**Ships:** the actual scaling capability.

### Phase 4 — operational polish (later)

launchd/systemd auto-start; cost-threshold ntfy alerts; daemon log
rotation; cross-feature dependency field; auto-rendered plan.md if
demand exists.

Phases ship in 1 → 2 → 3 order. Each is independently useful.

## Risks

1. **Daemon as black box.** Mitigation: rich `daemon_status` output,
   log file, doctor probe.
2. **spec.md drift from reality.** Mitigation: CLAUDE.md rule; later, a
   stale-spec detector (compare last spec commit to last unit_event on
   the feature).
3. **Daemon crashes mid-cycle.** Unit stays `claimed` with no progress.
   Mitigation: claim expiry (release if `claimed_at > 1h` without an
   event).
4. **Cycle-log write failures.** State.db says done, no log on disk.
   Mitigation: idempotent `regenerate_cycle_log(unit_id)`; a periodic
   orphan-log sweeper (a separate housekeeping job, unrelated to the
   rejected cron-driven cycle scheduler) can patch missing logs.
5. **PR description drift after capture.** User edits the PR description
   on GitHub after the cycle log was written. Mitigation: capture
   timestamp + final SHA in the log; re-capture on `reconcile_unit_pr`
   merged-state confirmation.

## Rejected alternatives

1. **Separate `decisions.md` per feature.** Redundant — git log of
   spec.md captures the timeline.
2. **Auto-summarize chat history into spec.md.** Lossy; explicit
   `commit -m "Why: …"` discipline produces better artifacts.
3. **Per-feature lead session (20 chat windows).** Overwhelming.
   Available as opt-in, not default.
4. **Cron-driven scheduler instead of daemon.** Coarse granularity
   (1-minute minimum) is poor for reactive scheduling.
5. **Cap features at 5–8 simultaneously.** Goal is 20+; ducks the
   problem.
6. **Fetch PR conversation from GitHub on every spawn.** N×M API calls
   per spawn under daemon throughput; rate-limit prone; awkward
   post-merge. Mirror once at terminal state instead.
7. **Use the PR description itself as the cycle log.** PR descriptions
   are user-facing artifacts; cluttering them with cycle transcripts
   degrades them. Keep separate.

## Architecture impact

When implemented, `docs/ARCHITECTURE.md` needs updates to:

- §3 High-level architecture (add daemon to diagram)
- §4 Process model + lifetimes (daemon is a new long-lived process)
- §5 Component map (`orchestrator/daemon.py`, `features/` directory)
- §6 Data model (`work_units.claimed_by`, `claimed_at`, `claimed` status)
- §7 Control flows (daemon-driven cycle_review)
- §11 Extension points (spec.md and cycle logs as new persistent surfaces)
- §12 Key decisions (why spec.md+git over decisions.md; why cycle logs
  are files not rows; why we mirror PR convos to disk)
