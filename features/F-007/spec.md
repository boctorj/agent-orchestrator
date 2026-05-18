# F-007: Ultrareview as terminal gate

> Per-feature opt-in for an extra pre-merge review pass that fires Anthropic's `/ultrareview` after our reviewer agent endorses. Backfilled retrospectively on 2026-05-18 — F-007-U-1 (PR #27, merged 2026-05-18 04:56 UTC) and F-007-U-2 (PR #28, merged 2026-05-18 05:04 UTC) shipped before this spec.md existed.

## Intent

Add an optional final pre-merge review pass to `cycle_review` that fires Anthropic's `/ultrareview` (multi-agent cloud bug-hunter) after our reviewer agent emits `REVIEW_RECOMMEND_MERGE`. Only emit `approved_awaiting_merge` if ultrareview also passes; otherwise route findings back through the existing fix loop via `address_review(source='ultrareview', ...)`.

This catches final-mile issues that the line-level reviewers (Copilot + our reviewer) miss: subtle spec drift, hidden coupling, edge cases the attention budget skipped, "is this actually done?" reviews against the full feature spec.

Ultrareview is **opt-in per feature** (default off) because each run costs measurable tokens — not justified for every feature, but valuable on load-bearing changes.

**Who benefits:** the user (catches issues before merge that would otherwise show up in production); the orchestrator (a third independent verifier reduces the chance of merging shippable-looking-but-actually-broken code).

## Acceptance

- `load_feature(..., ultrareview_enabled=True)` flips a feature's terminal review pass to require ultrareview success.
- Toggling the flag on an approved feature is a metadata-only update — does NOT reset plan approval (same rule as the `repo_path` repair pattern; matched via a sentinel default).
- After our reviewer emits `REVIEW_RECOMMEND_MERGE` on a flag-enabled feature, `cycle_review` fires `/ultrareview` and waits for the verdict before terminating.
- On ultrareview PASS → terminal status `approved_awaiting_merge` (today's behaviour preserved).
- On ultrareview FAIL → findings route through `address_review(source='ultrareview', ...)` into the shared cap-3 fix loop.
- Ultrareview findings count toward the **shared cap-3 budget** alongside tester-bugs and reviewer-changes.
- `list_features` and `get_plan` JSON surface the per-feature flag.
- Events emitted: `ultrareview_passed`, `ultrareview_changes_requested`, `ultrareview_fix_cycle_N` (for cost attribution + cycle-log entries).

## Out of scope

- Repo-level or unit-level granularity for the flag (feature-level only).
- Env-var defaults (e.g. `ORCH_ULTRAREVIEW_DEFAULT`) — hides intent; flag state must be visible in `list_features` / `get_plan`.
- An emergency `cycle_review(..., force_no_ultrareview=True)` override — deferred to a later unit if real demand surfaces.
- README + `docs/ARCHITECTURE.md` updates for U-1 (schema-only, no user-visible behaviour change); deferred until U-3's cycle_review wiring ships the behaviour change.
- A long-lived `/ultrareview` worker role — U-2 ships a thin wrapper (`orchestrator/ultrareview.py`) invoking `claude ultrareview` as a one-shot CLI subprocess. Not a persistent worker session.

## Approach

**Per-feature opt-in flag (U-1).** New column `features.ultrareview_enabled INTEGER NOT NULL DEFAULT 0` with an idempotent ALTER-TABLE migration in `init_db()` (SQLite has no `ADD COLUMN IF NOT EXISTS`, so `PRAGMA table_info(features)` gates the migration). Round-tripped through `save_feature` / `get_feature` / `list_features` via a new `_feature_from_row` helper (mirrors `_verified_repo_from_row`).

**Invocation primitive (U-2).** Standalone `orchestrator/ultrareview.py` with `trigger(pr_url, *, timeout_seconds=None, spawn=None)` and `wait_for_result(pr_url, timeout=600)` returning `{passed: bool, findings: list[str]}`. Pinned transport: the `claude ultrareview <N> --json --timeout <m>` CLI subcommand. Rejected alternatives: interactive slash command (needs a TTY); PR-comment trigger (not a documented invocation surface). Fails CLOSED on `rc==0` with unparseable / schema-drifted `bugs.json` so schema drift can't silently endorse buggy PRs.

**Gate wiring (U-3, in flight).** Branch on `feature.ultrareview_enabled` after `REVIEW_RECOMMEND_MERGE`. On PASS, terminate as `approved_awaiting_merge`. On FAIL, initial impl escalates with findings; full FAIL loop arrives in U-4.

**FAIL loop (U-4, blocked).** Extend `address_review` with `source='ultrareview'` and a coder prompt variant ("reviewer already endorsed, but ultrareview caught these final-mile issues — fix without scope creep"). After coder fix + CI green, re-run ultrareview (not the reviewer agent) until PASS or cap-3.

## Constraints

- **Cost-sensitive.** Ultrareview is measurably more expensive per run than line-level review. Opt-in default-off ensures we don't accidentally light it up on every feature.
- **Fail-closed gate.** Schema drift / unparseable output must surface as a sentinel finding that flips `passed` to False. Endorsing a merge based on output we couldn't read is the failure mode this module exists to prevent.
- **Wrapper-side and cloud-side timer alignment.** `trigger`'s `timeout_seconds` and `wait_for_result`'s `timeout` default to the same value (`SPEC_WAIT_TIMEOUT_SECONDS=600`) so a wrapper SIGKILL also stops the cloud session — no zombie cloud-billing past the wrapper's cap.
- **MCP stdio isolation.** Subprocess uses `stdin=subprocess.DEVNULL` to prevent the child from intercepting the parent's JSON-RPC transport bytes when the orchestrator runs as an MCP stdio server.
- **No process leaks on retry.** Repeated `trigger()` on the same PR kills the prior subprocess + drains its stdout PIPE before storing the new handle.

## Decisions

**Feature-level granularity, not repo / unit / env.**
**Why:** repo-level is too coarse (mixed-criticality repos), unit-level too fine (lead would set it on every unit; natural unit of "is this load-bearing?" is the feature), env-var hides intent. Feature-level is the natural granularity of the design question, visible in state.db, already the unit at which planning + approval happens.

**Sentinel default on `load_feature(..., ultrareview_enabled=None)`.**
**Why:** the canonical "fix wrong repo_path" path re-calls `load_feature` with the same ID and omits unrelated args. A non-sentinel default would silently clobber the flag — breaking the metadata-update preservation rule established for the `repo_path` repair pattern. (Cycle-0 reviewer H1 finding on PR #27 surfaced this; sentinel was the cycle-1 fix.)

**Ultrareview findings count toward the shared cap-3 budget.**
**Why:** prevents runaway loops on a feature where ultrareview never accepts. Same escalation path as tester-bug or reviewer-changes loops — keeps the user as the final escalation target instead of burning budget indefinitely.

**Standalone Python module instead of a worker-role session.**
**Why:** `claude ultrareview` is a self-contained CLI invocation that runs to completion and emits structured output. A persistent worker session would add lifecycle management we don't need; the subprocess primitive is simpler and matches the CLI's one-shot semantics.

**`--json` flag + fail-closed `_parse_bugs` over heuristic-parse.**
**Why:** without `--json` the CLI emits a human-formatted report; the gate would have to distinguish banners from findings via fragile regex. `--json` is documented as the machine-readable channel. Fail-closed on schema drift ensures a buggy PR can't slip through if Anthropic renames `bugs` → `findings` in a future release.

**Aligned trigger/wait timeouts (both default 600s).**
**Why:** when the two defaults diverged (1800s trigger / 600s wait), every caller using defaults left a ~20-minute cloud-billing gap after the wrapper SIGKILLed. Aligning by construction closes the gap; explicit-timeout callers can still override. (Cycle-3 reviewer M4 finding on PR #28; fixed in cycle 4.)

**`_parse_bugs` is schema-scoped to ultrareview JSON findings only.**
**Why:** `_parse_bugs` does not parse reviewer/tester markers; it only turns `claude ultrareview --json` stdout into findings for the gate. Keeping the parser limited to the documented structured payload, and failing closed on missing or reshaped findings fields, preserves the unambiguous failure mode "ultrareview reported findings" without implying any broader role-specific marker parsing in `orchestrator/ultrareview.py`.

## Open questions

- **Lead-persona prompt update:** should the lead ask the user during `load_feature` breakdown whether the feature is load-bearing enough to enable ultrareview? Deferred to a later unit (or a separate feature) — out of scope for U-1 through U-4.
- **Cost attribution:** ultrareview-specific events are recorded but `feature_cost` / `unit_cost` don't separate ultrareview spend from other agent spend. Should we break it out?
- **Ultrareview vs reviewer agent overlap:** the reviewer agent already does spec compliance. How much of ultrareview's catch rate is genuinely additive vs duplicating reviewer work? Open until we have a few features ship with the flag enabled and can compare findings.
- **CLI docs URL stability:** `https://code.claude.com/docs/en/ultrareview` is the authoritative reference for the exit-code contract, `--json` flag, `--timeout` default, etc. Non-validatable from sandbox at review time. If Anthropic restructures the docs, the module's design rationale anchors lose.

## References

- `docs/PROPOSAL-ultrareview-gate.md` — full design rationale, opt-in semantics, what-the-next-unit-does, risks.
- `orchestrator/state.py:_migrate_features_ultrareview` — idempotent column-add for pre-F-007 state.db files.
- `orchestrator/ultrareview.py` — invocation primitive (U-2).
- PR #27 (F-007-U-1, merged 2026-05-18) — schema + plumbing.
- PR #28 (F-007-U-2, merged 2026-05-18) — invocation primitive.
