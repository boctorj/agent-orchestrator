# F-014: Unit health probe — comprehensive PR/reviews/conflicts/CI reconciliation

## Intent

Today the orchestrator inspects a unit's external reality through two
narrow tools: `check_unit_pr` (read-only poll of PR merged-state + CI
conclusions) and `reconcile_unit_pr` (apply the `merged → done`
transitions). They share most of their read logic, they only look at a
thin slice of the truth (merged? CI green?), and they are two tools
where there should be one canonical health surface.

They miss almost everything that actually strands a unit:
- merge **conflicts** (CONFLICTING / DIRTY mergeable_state + which files),
- **required-check drift** (a branch-protection-required check that isn't
  present at all, vs. one that ran and failed),
- **review-thread** state (unresolved threads, dismissed reviews,
  changes-requested counts, codeowner / Copilot review presence),
- worker **session liveness** (idle / running / terminated per role),
- **cycle-cap proximity** and `last_activity` age.

This feature introduces one comprehensive, **pure** health probe plus a
**decision table**, exposes them as a single canonical MCP tool
`inspect_unit_health`, and demotes `check_unit_pr` / `reconcile_unit_pr`
to thin, deprecated aliases over it.

It is deliberately the **foundation** for the next two features:
- **F-015** promotes this probe's *shadow decisions* (predicted-but-not-
  applied transitions) into real automated transitions.
- **F-016** (the non-blocking reconciler daemon, see
  [`docs/PROPOSAL-async-orchestrator.md`](../../docs/PROPOSAL-async-orchestrator.md))
  runs `decide_transitions` in a level-triggered loop. The daemon's
  `derive_next_action(unit) = f(unit_state, observations)` **is** this
  feature's `decide_transitions`. Building the pure engine here is what
  lets F-016 have "one engine, two callers (lead + daemon)" rather than a
  parallel state machine.

### Units

**F-014-U-1: `health.py` probe + decision table (with shadow output)**
Add `orchestrator/health.py` with two **pure** functions (no I/O beyond
the `gh` / `anthropic` clients passed in).

`probe_unit_health(unit_id, gh_client, anthropic_client) -> HealthReport`
queries:
- **PR**: state (open/closed/merged), mergeable, mergeable_state,
  conflict file list (when CONFLICTING / DIRTY).
- **Git**: commits ahead/behind base, head_sha + age, last force-push
  timestamp.
- **CI**: all check_runs (name, status, conclusion, details_url incl. log
  URLs for failing jobs); required-vs-actual delta against branch
  protection; pending runs.
- **Reviews**: GH approval count, changes-requested count, dismissed
  reviews, unresolved review-thread count, codeowner-requested reviewers,
  Copilot review presence + state.
- **Worker sessions** (per coder/tester/reviewer role): session_status
  from Anthropic (idle / running / terminated).
- **Orchestrator**: cycle count vs. cap-3 (proximity), last_activity age,
  downstream-blocked count.

`decide_transitions(local_state, report) -> Decision` returns a dataclass
with two fields:
- `actions_to_apply: list[Action]` covering:
  - existing reconcile cells: `merged + in_ci → done` (+ `merged` event);
    `merged + escalated → done` (+ `merged` AND
    `recovered_from_escalated`, clears `last_error`); cycle-log writer
    side effect preserved.
  - new event-only signals: `pr_conflict_detected` (with file list),
    `required_check_missing`, `ci_drift_detected` (sets `last_error`, no
    status change).
- `shadow_decisions: list[ShadowDecision]` covering deferred rules:
  - `escalated_to_in_ci_reset` (escalated + CI green + GH-approved + no
    open threads),
  - `merge_reverted_flag` (done + merge_commit no longer reachable from
    main),
  - any additional risky cells identified during implementation.
  Each `ShadowDecision` carries `{rule_name, predicted_action,
  trigger_inputs (structured), rationale (str)}`.

Comprehensive table tests covering each
`(status × pr_state × ci × reviews × conflicts)` cell for both
`actions_to_apply` and `shadow_decisions`. No deps.

**F-014-U-2: Collapse `check_unit_pr` + `reconcile_unit_pr` into canonical
`inspect_unit_health` with full event persistence**
Wires U-1 into MCP.

New canonical tool `inspect_unit_health(unit_id, dry_run=False)` in
`orchestrator/tools/health.py` (new file). On non-dry-run: calls
`probe_unit_health`, runs `decide_transitions`, applies `actions_to_apply`
via the **existing** `state.touch_unit` / event-append code paths (reuse,
do not duplicate). For each `ShadowDecision`, writes a
`shadow_transition_proposed` event with the full payload (rule_name,
predicted_action, trigger_inputs, rationale) in the event's `details`
JSON. Also stores a `health_report_snapshot` event (full serialized
HealthReport in `details`) on the first probe per unit per UTC day for
forensics retention; tunable via env var.

Returns a markdown digest: HealthReport summary, `applied_actions` list,
`shadow_decisions` list (so the lead sees what was deferred and why).

Make `check_unit_pr(unit_id)` a thin alias for
`inspect_unit_health(unit_id, dry_run=True)` (same shape minus
`applied_actions`). Make `reconcile_unit_pr(unit_id)` a thin alias for
`inspect_unit_health(unit_id, dry_run=False)`, preserving the cycle-log
writer side effect on merged polls. Log both aliases as **deprecated**.

Update CLAUDE.md to document `inspect_unit_health` as the canonical
surface (aliases retained for backward compatibility but deprecated);
update the scheduling rule and restart-recovery flow to recommend
`inspect_unit_health` in new flows while preserving existing
`reconcile_unit_pr` references.

All existing `check_unit_pr` / `reconcile_unit_pr` tests must pass
unchanged through the aliases. Add new tests for the canonical tool's
shadow-decision recording and HealthReport snapshot retention.
Depends on F-014-U-1.

## Acceptance

- `orchestrator/health.py` exposes pure `probe_unit_health` and
  `decide_transitions`; no network I/O except via injected clients.
- `decide_transitions` table tests cover every
  `(status × pr_state × ci × reviews × conflicts)` cell for both
  `actions_to_apply` and `shadow_decisions`.
- `inspect_unit_health(unit_id)` applies the same `merged → done`
  transitions `reconcile_unit_pr` does today, plus emits
  `pr_conflict_detected` / `required_check_missing` / `ci_drift_detected`
  events where applicable.
- Each deferred rule emits a `shadow_transition_proposed` event with the
  full structured payload; a `health_report_snapshot` is stored at most
  once per unit per UTC day.
- `check_unit_pr` and `reconcile_unit_pr` are aliases over
  `inspect_unit_health` (dry-run / non-dry-run) and all their existing
  tests pass unchanged.
- CLAUDE.md documents `inspect_unit_health` as canonical and the two
  aliases as deprecated.

## Out of scope

- **Promoting shadow decisions to applied transitions** — that is F-015.
  This feature only *records* shadow decisions; it never acts on them.
- The non-blocking daemon / dispatcher-watcher split — F-016. This feature
  ships the pure engine the daemon will call, not the daemon.
- Removing `check_unit_pr` / `reconcile_unit_pr` — they stay as deprecated
  aliases; deletion (if ever) is a later cleanup once callers migrate.
- Any change to the cap-3 contract, marker grammar, or worker prompts.

## Constraints

- **Reuse, don't duplicate state writes.** `actions_to_apply` must run
  through the existing `state.touch_unit` / event-append paths so the
  alias-vs-canonical behavior cannot diverge.
- **Backward compatible.** `check_unit_pr` / `reconcile_unit_pr` keep
  their exact return shapes through the aliases; the cycle-log writer side
  effect on merged polls is preserved.
- **Purity.** `probe_unit_health` and `decide_transitions` take clients as
  arguments and perform no other I/O, so F-015 and F-016 can call them
  from a daemon loop and unit-test them without mocks-of-mocks.

## Decisions

- **One canonical tool over two narrow ones**: `check_unit_pr` /
  `reconcile_unit_pr` already share read logic and differ only in
  dry-run vs. apply. Collapsing them removes the read/advance drift and
  gives F-015/F-016 a single seam.
- **Shadow decisions instead of immediately acting on the risky cells**:
  rules like `escalated_to_in_ci_reset` and `merge_reverted_flag` are
  high-value but high-blast-radius. Emitting them as
  `shadow_transition_proposed` events lets F-016's first-week rollout run
  them side-by-side with the daemon's actual transitions and flag
  divergence before promotion (F-015).
- **Per-day HealthReport snapshot, not per-poll**: full report retention
  is for forensics; one snapshot per unit per UTC day bounds storage while
  preserving an audit trail.

## Open questions

- Should `required_check_missing` ever auto-escalate, or stay event-only
  until F-015? Default: event-only here.
- Snapshot retention env var name + default TTL — settle during U-2.
- Does `decide_transitions` need the worker session_status at all for the
  *applied* actions, or only for shadow decisions? If only shadow,
  consider splitting the probe so the cheap (PR/CI) path can run more
  often than the session-status path.

## History

This file previously described a different, never-built feature
("Role decomposition — coder split and self-review extraction"),
committed speculatively in `f8f9015` ("Add future features (#39)").
That scope was renumbered/superseded; it is preserved in `BACKLOG.md`
and recoverable at git `f8f9015:features/F-014/spec.md`.
