# Feature spec format + `Why:` commit-message pattern

Reference for the per-feature `spec.md` artifact and the
`Why: <reason>` commit-message convention that together act as the
orchestrator's durable decision log.

For the design rationale (why files instead of state.db rows, why git
log instead of a separate `decisions.md`, why this lives next to
`state.db`), see
[`docs/PROPOSAL-feature-spec-and-headless-daemon.md`](PROPOSAL-feature-spec-and-headless-daemon.md)
§1 and §"Decisions".

## What it is

Every feature loaded via `load_feature()` gets a
`features/<feature_id>/spec.md` seeded next to `state.db`. The file is
the durable, version-controlled source of truth for the feature's
intent, acceptance criteria, scope, design choices, and open questions.
It outlives any one lead chat session — fresh sessions re-bootstrap from
spec.md rather than scrolling chat history.

## Where it lives

```
<orchestrator workdir>/
  state.db
  features/
    F-001/
      spec.md          ← this file
    F-002/
      spec.md
    ...
```

`features/` is a sibling of `state.db`. Both belong to the orchestrator
workdir's git repo so `git log -- features/F-XXX/spec.md` IS the
feature's decision timeline.

## Template

```markdown
# F-XXX: <title>

## Intent
What we're building. Why. Who benefits.

## Acceptance
Concrete, testable criteria for "done".

## Out of scope
Hard boundary against scope creep.

## Approach
High-level design choices, library / framework decisions.

## Constraints
Non-functional requirements (perf, security, compatibility).

## Decisions
Non-obvious choices with reasoning. Grows over time.

## Open questions
Things still undecided. Resolved questions move to Decisions.
```

`load_feature` seeds this template with the feature's title and
description (description lands in **Intent**). Every other section
starts as a `_TBD_` placeholder so the lead can see at a glance what
still needs filling in.

## Lifecycle

| Phase | Who writes | What changes |
|---|---|---|
| `load_feature` | orchestrator | Creates `features/<id>/spec.md` from template. Idempotent — never overwrites an existing file. |
| Planning | lead | Fills in Acceptance / Out of scope / Approach / Constraints. Commits with `Why:`. |
| Execution | lead | Appends to Decisions / Open questions as non-obvious choices surface or escalations trigger design changes. |
| Post-merge | (no one) | spec.md is not a changelog — historical decisions stay in git log, not appended in-place. |

**`load_feature` is a one-shot seeder.** Re-calling it on an existing
feature (e.g. to fix a wrong `repo_path`) will *not* overwrite an
existing `spec.md`. Manual edits are safe.

If `spec.md` is missing on disk for a feature row that exists in
`state.db` (e.g. created before this format was introduced), the next
`load_feature` call back-fills it from the template.

## The `Why:` commit-message pattern

Every commit that edits `spec.md` MUST include a `Why:` line in its
message body. The commit log IS the decision log; we deliberately do
*not* maintain a separate `decisions.md`.

### Format

```
spec(F-007): <one-line subject>

Why: <one paragraph explaining the reason. What changed in the world,
what alternatives were considered, why this option won.>
```

Subject line: `spec(F-XXX): <imperative phrase>` — keeps spec edits
grep-able. Body: one or more `Why:` lines, each capturing the reason
for a discrete change.

### Examples

```
spec(F-007): keep U-2 monolithic, no DB-schema split

Why: a separate units split was tempting but the OAuth callback
needs atomic write of (state, token, user_id). Splitting introduces
a partial-write window we'd need locks to close — simpler to keep one
unit.
```

```
spec(F-007): Fernet wraps stored tokens only

Why: U-3 escalation showed Approach was ambiguous on whether
in-flight tokens should be wrapped. Clarifying to "at-rest only"
because in-flight is already TLS-protected and wrapping mid-call
breaks the refresh path.
```

### When to commit

- After every non-obvious planning decision.
- When an escalation triages to a spec change rather than a code fix.
- When scope shifts (item moves from "Out of scope" → "Acceptance" or
  vice versa).

Trivial edits (typos, formatting) don't need a `Why:` — squash them
into the next substantive commit or stage with a plain
`docs: fix typo in F-007 spec` message.

## Why this design

Captured here briefly for the sceptic at the keyboard; full reasoning
in the proposal's `## Decisions` section:

- **spec.md, not state.db rows** — markdown is human-readable, greppable,
  and survives any future state.db migration.
- **git log, not `decisions.md`** — redundant pair: the file's own
  history is already the timeline. A second file invites drift.
- **`Why:` discipline, not auto-summarized chat** — chat-to-spec
  summarization is lossy; an enforced one-line `Why:` produces sharper
  artifacts.
- **Lead-owned spec.md, orchestrator-owned cycle logs** —
  `features/<id>/U-N.md` cycle logs (added in a later unit of this
  feature) are auto-generated immutable transcripts. spec.md is the
  human-editable design doc. They never collide.
