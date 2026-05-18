# F-006: Feature spec + cycle logs + role prompts (Phase 1 of PR #19)

> Phase 1 of the "feature spec + cycle logs + headless daemon" proposal (`docs/PROPOSAL-feature-spec-and-headless-daemon.md`, merged as PR #19). Establishes the durable per-feature artifacts and role-prompt context blocks that the proposal's later phases (atomic claim, headless daemon) depend on. Backfilled retrospectively on 2026-05-18 — F-006-U-1 (PR #25, merged 2026-05-17 02:48 UTC) and F-006-U-2 (PR #26, merged 2026-05-17 02:52 UTC) shipped before this spec.md existed; F-006-U-3 (PR #31) in flight; F-006-U-4, U-5, U-6 not yet started. This is the meta-feature whose own first unit (`load_feature` → seed `spec.md`) created the convention every other backfilled spec.md follows.

## Intent

Externalize the orchestrator's load-bearing context — feature intent, decisions, acceptance criteria, per-cycle execution history — from volatile chat history into durable, version-controlled markdown files next to `state.db`. Wire those artifacts into the worker task message via three context blocks (`## FEATURE SPEC`, `## PREDECESSOR UNITS`, `## THIS UNIT'S CYCLE LOG`) so coder / tester / reviewer agents read the same durable substrate the lead reads. Update CLAUDE.md and role prompts so the discipline is enforced.

Phase 1 is the smallest piece of the proposal that ships value standalone — decision continuity across lead chat sessions, predecessor-aware downstream units, and reviewer-enforced spec compliance. Phases 2 (atomic claim) and 3 (headless daemon) are explicitly out of scope for this feature and land as their own features later.

**Who benefits:** the lead persona (fresh sessions re-bootstrap from `feature_memory(F-X)` instead of scrolling chat history); downstream worker agents (see merged predecessors' decisions and findings); the reviewer agent (mandatory spec-vs-PR-description comparison catches scope drift); the user (escalations have full historical context attached, no chat-history dependence).

## Acceptance

- `load_feature(...)` creates `features/<feature_id>/spec.md` from a starter template (Intent / Acceptance / Out of scope / Approach / Constraints / Decisions / Open questions), pre-filled with the feature title and description into Intent. Idempotent — never overwrites an existing file; safe to re-call (e.g. fixing a wrong `repo_path` doesn't clobber lead edits). Back-fills features that pre-date F-006.
- `docs/SPEC-FORMAT.md` documents the spec template + the `Why:` commit-message-as-decision-log pattern.
- `orchestrator/cycle_log.py` exposes a pure library that renders `features/F-XXX/U-N.md` from `state.unit_events` + GitHub-mirrored PR data, writes atomically (`.tmp` then rename), and auto-commits locally as `agent@orchestrator / orchestrator-bot`. Includes `regenerate_cycle_log(unit_id)` for the orphan-recovery and user-edited-PR-description repair paths (proposal Risks §4–5).
- Both writers call `mkdir(parents=True, exist_ok=True)` before writing so they work whether U-1 has landed and for features that pre-date the `features/` directory entirely.
- `cycle_review`'s terminal branches in `orchestrator/tools/execution.py` write per-cycle entries during the loop and finalize on `REVIEW_RECOMMEND_MERGE` / `REVIEW_COMMENT` / escalation / manual kill (U-3).
- `check_unit_pr` captures `mergeCommit.oid` once when the PR confirms merged and amends the cycle log; this is the only post-finalization edit (U-3).
- `compose_coder_task` / `compose_tester_task` / `compose_reviewer_task` inject `## FEATURE SPEC` (always), `## PREDECESSOR UNITS` (when deps exist), and `## THIS UNIT'S CYCLE LOG` (reviewer, retry cycle ≥ 2). Read-side gracefully handles missing files (U-4).
- New MCP tool `feature_memory(feature_id) → str` returns a ~3–7K-token session-bootstrap blob: spec.md + `git log -10 -- features/F-XXX/spec.md` + aggregated `unit_summary` + cycle-log "Final" sections + recent escalation events (U-5).
- Role prompts (`orchestrator/prompts/coder.md`, `tester.md`, `reviewer.md`) updated per the proposal's "Role prompt changes" section: coder emits `## Spec satisfaction` in PR description, tester tests against spec acceptance criteria, reviewer performs mandatory spec-vs-PR-description comparison (U-6).
- CLAUDE.md updated with lead-persona rules: call `feature_memory(F-X)` at session start before discussing a feature; commit spec edits with a `Why:` line; cycle logs are read-only (revise `spec.md`, never the cycle log) (U-6).
- Every commit that edits `spec.md` includes a `Why: <reason>` line. The git log of `spec.md` IS the decision log; no separate `decisions.md`.
- Files in `features/` are always committed locally — uncommitted markdown in `features/` is a bug.
- Tests pass commit-by-commit (each unit's PR is independently green).

## Out of scope

- **Phase 2 of the proposal — atomic ready-unit claim** (`work_units.claimed_by` / `claimed_at`, `pending` and `claimed` statuses, race tests). Ships as its own feature.
- **Phase 3 of the proposal — headless daemon** (`orchestrator daemon` CLI, poll loop, `daemon_status` / `daemon_pause` / `daemon_resume` MCP tools). Ships as its own feature after Phase 2.
- **Phase 4 of the proposal — operational polish** (launchd/systemd auto-start, cost-threshold ntfy alerts, daemon log rotation, cross-feature dependency field, auto-rendered plan.md).
- **A separate `decisions.md` per feature.** Rejected: git log of `spec.md` already captures the timeline; a second file invites drift.
- **Auto-summarization of chat history into spec.md.** Rejected: lossy; explicit `Why:` discipline produces sharper artifacts.
- **Push policy for the orchestrator's auto-commits.** Local-only by design; push is manual or via a separate operator-run sweep. The orchestrator never pushes.
- **State.db schema for cycle logs.** Cycle logs are markdown files; storing them as rows would lose human-readability and `grep`-ability of `features/`.
- **`ARCHITECTURE.md` / `CONTRIBUTING.md` updates beyond what's required for the Phase 1 surface.** Larger architecture rewrites belong with Phase 2 / Phase 3 when more substrate changes.
- **Backfilling specs and cycle logs for features that shipped before F-006 (F-001 through F-005).** U-1's idempotent seeder produces a template-only `spec.md` for older features on the next `load_feature` call; manual backfill of historical decisions is left to the operator.

## Approach

Six units in dependency order:

**U-1 — `load_feature` writes `spec.md`** (no deps). New module `orchestrator/feature_spec.py`: `spec_path()` / `render_template()` / `write_spec_if_missing()`, rooted at `state.STATE_DB.parent / "features"` so the `tmp_state_db` fixture's monkeypatch lands correctly. `load_feature` calls `write_spec_if_missing()` on **both** the creation and metadata-update paths so features that pre-date F-006 get back-filled on the next call (decision detailed below). Ships `docs/SPEC-FORMAT.md` as a single doc covering the template + the `Why:` discipline.

**U-2 — cycle-log writer library + `regenerate_cycle_log`** (no deps; runs in parallel with U-1). New module `orchestrator/cycle_log.py`: `feature_dir()` for `mkdir -p`, `cycle_log_path()`, `fetch_pr_info()` (wraps `gh pr view --json title,body,headRefOid`), `fetch_review_threads()` (wraps the GraphQL `reviewThreads` query), `render_cycle_log()`, `write_cycle_log()` (fetch → render → atomic write → local commit), and `regenerate_cycle_log()` for orphan recovery. Library-only — no call sites wired in this unit, so it changes no runtime behavior. Both writers `mkdir(parents=True, exist_ok=True)` before any write.

**U-3 — wire cycle-log writer into `cycle_review` + post-merge SHA backfill** (depends on U-2; in flight). Hooks U-2's writer into `cycle_review`'s terminal branches in `orchestrator/tools/execution.py`. Adds the `check_unit_pr` amendment that captures `mergeCommit.oid` on confirmed merge. Two SHAs are captured at different points: `headRefOid` (PR head at terminal state) at first finalize, `mergeCommit.oid` (the actual commit on main, which diverges for squash and rebase merges) on confirmed merge.

**U-4 — `compose_*_task` injections** (depends on U-1, U-2). Updates `compose_coder_task` / `compose_tester_task` / `compose_reviewer_task` signatures to accept the feature spec text + predecessor cycle-log summaries + this unit's own cycle log. Renders three context blocks per the proposal's table. Read-side gracefully handles missing files (so F-006's own in-flight units are a no-op — that's expected). Updates every call site in `execution.py` and every test constructing a task message.

**U-5 — `feature_memory(feature_id)` MCP tool** (depends on U-1, U-2). Returns a ~3–7K-token blob: `spec.md` + `git log -10 -- features/F-XXX/spec.md` + aggregated `unit_summary` + cycle-log Final / terminal sections + recent escalation events. Handles empty state (no `spec.md`, no cycle logs) and populated state (mixed merged / in-flight / escalated).

**U-6 — role-prompt + CLAUDE.md updates** (depends on U-3, U-4, U-5). Lands LAST so every worker spawned during U-1..U-5 still runs the pre-Phase-1 prompts. The coder spawned on U-6 itself is explicitly told: "the new spec-vs-PR-description / spec-satisfaction rules take effect only after this PR merges and the next worker spawns — do NOT behaviorally apply them to this PR itself."

**Worst-case task-message size:** ~5K tokens (reviewer, 3 merged predecessors, cycle 3). One-time at spawn/resume; doesn't accumulate.

## Constraints

- **`load_feature` is a one-shot seeder, never an overwriter.** Re-calling on an existing feature (e.g. to fix a wrong `repo_path`) MUST preserve manual edits. Missing `spec.md` for an existing feature row triggers back-fill from template; existing files are left untouched.
- **Cycle logs are immutable on the normal path.** Only two writes after a terminal state are allowed: (a) post-merge `mergeCommit.oid` backfill (one-shot), (b) `regenerate_cycle_log(unit_id)` for orphan or user-edited-PR-description recovery. Workers and the lead never overwrite a finalized cycle log.
- **Partial-write safety: write-to-tmp-then-rename for every file in `features/`.** A crash during write leaves the prior finalized version intact (or no file, for first-time writes).
- **Local commits only; no push.** Auto-commits use `user.email=agent@orchestrator user.name=orchestrator-bot`. Push is manual or via a separate operator-run sweep. The orchestrator never executes `git push`.
- **Uncommitted markdown in `features/` is a bug.** Both spec.md (lead-committed) and cycle logs (orchestrator-committed) must be checked in.
- **`mkdir(parents=True, exist_ok=True)` before every write** in both U-1 and U-2 — so the writers work regardless of unit landing order and for features predating the `features/` directory.
- **Worker-prompt context blocks are one-shot per spawn/resume.** ~5K-token worst case must not accumulate across cycles; re-injected fresh on every resume to pick up mid-cycle spec edits.
- **U-3 and U-4 both modify `orchestrator/tools/execution.py`.** They run in parallel; whichever merges second needs a rebase. CI fail in the reviewer cycle surfaces the conflict; this is an accepted sequencing risk, not a flaw.
- **Read-side null-safety.** Every consumer (`feature_memory`, `compose_*_task`, role prompts) must handle the case where `spec.md` or a cycle log doesn't yet exist — F-006's own in-flight units have no predecessor cycle logs, and pre-F-006 features may have only template specs.

## Decisions

**`spec.md` lives in the orchestrator workdir's git repo, sibling of `state.db`.**
**Why:** git history of `spec.md` works out of the box as the decision timeline; no separate decision-storage subsystem needed. Migrating to an external repo later is straightforward. Considered a separate `decisions/` repo or a state.db table; rejected as more complexity for less utility.

**Markdown files, not state.db rows, for both `spec.md` and cycle logs.**
**Why:** PR descriptions can be 5–10KB and markdown is human-readable + greppable. `git log -- features/F-XXX/` IS the audit trail — free. External tools can read `features/` without speaking the state.db schema. State.db is for structured operational data the scheduler queries.

**No separate `decisions.md`; git log of `spec.md` IS the decision log.**
**Why:** the file's own history is already the timeline. A second file invites drift between "what the spec says now" and "why we changed it." Enforced via the `Why:` discipline on every spec edit.

**`Why:` line required on every `spec.md` commit; no auto-summarization of chat.**
**Why:** chat-to-spec auto-summarization is lossy and produces softer artifacts than enforced one-line `Why:` discipline. The format pattern in `docs/SPEC-FORMAT.md` keeps spec edits grep-able (`spec(F-XXX): <imperative>` subject + `Why: ...` body).

**U-1 seeds `spec.md` on BOTH `load_feature` paths — creation AND metadata-update.**
**Why:** the unit description says "Create `features/F-XXX/` on `load_feature`"; the natural read is "ensure a `spec.md` exists after every `load_feature` call." The update path is what back-fills features that pre-date F-006 (e.g. F-001 through F-005) — without this, those features would never get a `spec.md`. `write_spec_if_missing` is the no-op for the common case where it already exists.

**One doc (`docs/SPEC-FORMAT.md`) covering both the template and the `Why:` pattern, not two.**
**Why:** the `Why:` discipline only makes sense in the context of "what is this file we're committing edits to." Co-locating is natural; matches `CONTRIBUTING.md`'s "do not create new doc files unless explicitly asked" guidance.

**Cycle logs are orchestrator-owned and immutable; `spec.md` is lead-owned and editable.**
**Why:** the cycle log is an auto-generated transcript — a historical artifact. The lead and workers never re-write it. spec.md is the human-editable design doc. They never collide because they own different files. To revise a past decision, the lead edits `spec.md` (the canonical source), not the cycle log.

**Two SHAs captured at different points: `headRefOid` at terminal finalize, `mergeCommit.oid` at confirmed merge.**
**Why:** GitHub squash + rebase merges produce a `mergeCommit.oid` that diverges from `headRefOid`. The merge commit is the authoritative artifact on `main`; the PR head SHA is the artifact the reviewer endorsed. Both matter; capturing them at the right moments gives a complete provenance chain. The post-merge backfill is the only post-finalization edit allowed.

**Post-merge SHA backfill is decoupled from cycle-log regeneration; the writer reads existing on-disk SHA to preserve it across re-renders.**
**Why:** GitHub's REST API can return `merge_commit_sha` as `null` immediately after a merge — the field is populated asynchronously over a window of seconds. The initial implementation gated cycle-log regeneration on `unit_state.status == 'done'` AND emitted exactly once: if the first `check_unit_pr` poll after merge saw `status=done` with `merge_commit_sha=null`, the log was regenerated WITHOUT the SHA, and the "exactly once" lock-out prevented any subsequent backfill once the SHA became available. That was fail-OPEN behavior dressed as success. The fix splits the status flip from the SHA backfill in `ops.py`, and `regenerate_cycle_log` reads the existing `Merge commit SHA: <sha>` line from disk so a populated SHA survives any re-render (offline-capable). (PR #31 cycle-1 reviewer H1 finding; fixed in `047e879`.)

**Cycle-log writer's local-commit step swallows `FileNotFoundError` (missing `git`) and non-zero `git add` exits.**
**Why:** a missing-git or non-repo workdir shouldn't block the file write — the file is the primary artifact, the commit is a nicety on top. "Files committed locally ... never left as uncommitted working-tree state" is the intent, but best effort: the file always lands. Operator can `git add` later in a non-repo workdir. (U-2 coder decision.)

**U-6 lands LAST and explicitly tells its own coder NOT to apply the new rules to its own PR.**
**Why:** the role-prompt + CLAUDE.md changes are rules the reviewer agent will enforce on the NEXT feature after merge. If U-6's coder applied them to its own PR (e.g. emitting `## Spec satisfaction` for a prompt-edits PR), the meta-reasoning would be circular and the PR would be confusing. The new rules take effect only after U-6 merges and the next worker spawns. This is a one-time bootstrap quirk specific to this unit.

**U-3 and U-4 run in parallel despite touching the same file (`execution.py`).**
**Why:** the alternative (sequence them) wastes a parallelism slot. The cost of a rebase on whichever merges second is small (CI fail surfaces the conflict; the coder rebases and re-runs). Explicitly called out in both unit descriptions ("KNOWN: ... whichever merges second needs a rebase").

**Auto-commit is local-only; push is manual.**
**Why:** auto-pushing on every cycle would spam the remote with `orchestrator-bot` commits. Keeping push manual lets the operator decide when to publish the journal — at session end, after a feature ships, on a periodic sweep. The orchestrator never executes `git push`.

## Open questions

- **Stale-spec detector.** Proposal Risks §2 calls out the risk that `spec.md` drifts from reality if the lead forgets to edit it. No enforcement in Phase 1; the CLAUDE.md rule is policy, not mechanism. Could later compare last `spec.md` commit timestamp to last `unit_event` on the feature and warn. Open until we have real drift incidents.
- **Daemon-side cycle-log writing.** Phase 1 wires writes from `cycle_review` in the lead's chat. Phase 3's daemon will write the same logs from background threads. Concurrent writes on the same `features/F-XXX/U-N.md` aren't expected (each unit owns its own file path), but the assumption needs re-validation when the daemon ships.
- **`feature_memory` token budget under heavy use.** Proposal says ~3–7K tokens scaling with completed-unit count. A feature with 20+ merged units could push ~10K+. No hard cap in U-5; deferred to when we observe real usage.
- **Cross-feature dependencies.** Proposal §"Open questions" #1 — if F-008 depends on F-007 landing first, where does that live? Left as a planning concern for v1; `features.depends_on` column can be added later if real demand surfaces.
- **`unit_events.details` usage for non-spec-worthy operational decisions.** Proposal §"Open questions" #5 — some choices ("retried with X") aren't spec-worthy but should be discoverable. Open: structured-JSON convention vs free-form vs ignore.

## References

- `docs/PROPOSAL-feature-spec-and-headless-daemon.md` (PR #19, merged 2026-05-14) — authoritative design rationale, all three phases, rejected alternatives, role-prompt changes table.
- `docs/SPEC-FORMAT.md` — spec template + `Why:` commit-message-as-decision-log pattern (shipped in U-1).
- `orchestrator/feature_spec.py` — `spec_path()`, `render_template()`, `write_spec_if_missing()` (U-1).
- `orchestrator/tools/planning.py` — `load_feature` invokes `write_spec_if_missing()` on creation + metadata-update (U-1).
- `orchestrator/cycle_log.py` — cycle-log writer library + `regenerate_cycle_log()` (U-2).
- `orchestrator/tools/ops.py` — `check_unit_pr` post-merge SHA backfill (U-3, in flight on PR #31).
- `orchestrator/tools/execution.py` — `cycle_review` terminal-branch wiring (U-3) and `compose_*_task` injections (U-4, not yet started).
- `orchestrator/prompts/coder.md`, `tester.md`, `reviewer.md` — role-prompt updates (U-6, not yet started).
- `CLAUDE.md` — lead-persona rules (U-6, not yet started).
- PR #25 (F-006-U-1, merged 2026-05-17) — `spec.md` template + `load_feature` seeding.
- PR #26 (F-006-U-2, merged 2026-05-17) — cycle-log writer library + `regenerate_cycle_log`.
- PR #31 (F-006-U-3, in flight) — cycle-log wiring into `cycle_review` + post-merge `mergeCommit.oid` backfill.
