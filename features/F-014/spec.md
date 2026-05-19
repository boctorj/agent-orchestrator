# F-014: Role decomposition — coder split and self-review extraction

## Intent

`coder.md` (347 lines) currently violates the Single Responsibility Principle by
serving three distinct roles in one prompt:

1. **Opener** — clone repo, implement unit, open PR (steps 1–11).
2. **Fixer** — resume session, fetch unresolved threads, address feedback, push,
   reply inline (the fix-loop section).
3. **Self-reviewer** — Simplify pass + Arch-drift pass before push (step 8, ~35
   lines of its own decision logic and review criteria embedded in the opener).

Additionally, the fixer contains a structural branch on `SOURCE:` (reviewer/tester/
human vs. ci) that are fundamentally different flows conflated in a single section.

The cost:
- When the orchestrator resumes a coder for fix-loop cycle 3, it ships the entire
  opener prompt (~150 lines of irrelevant instructions) as live context. Token cost
  compounds per cycle. Model attention is split.
- The self-review persona (Simplify / Arch-drift) is buried in a numbered list and
  cannot be tuned, tested, or evaluated in isolation.
- `reviewer.md` similarly mixes philosophical framing ("Three Promises", adversarial
  stance, "Red Flags") with procedural steps (7-step method), two concerns that
  evolve at different rates and have different editors.

This feature decomposes the prompts into focused units:
- `prompts/roles/coder-open.md` — opener only
- `prompts/roles/coder-fix.md` — fix-loop (inline-source: reviewer/tester/human)
- `prompts/roles/coder-fix-ci.md` — CI-failure variant (no GraphQL fetch, no inline
  replies)
- `prompts/roles/coder-selfreview.md` — the Simplify + Arch-drift persona, composed
  into the opener after commit but kept separately editable
- `prompts/roles/reviewer-stance.md` — adversarial framing, Three Promises, Red
  Flags
- `prompts/roles/reviewer-method.md` — 7-step procedure + severity rubric + posting
  recipe

The worker (`managed_agent.py`, `docker_claude_code.py`) selects the role prompt
based on call type. Resumed agents no longer carry opener context.

### Units

**F-014-U-1: Split `coder.md` → `coder-open.md` + `coder-fix.md`**
Create `prompts/roles/coder-open.md` (steps 1–11, the opener flow) and
`prompts/roles/coder-fix.md` (the fix-loop for reviewer/tester/human sources).
Both use `{{include common/...}}` for shared content (depends on F-013-U-1).
Update the worker's spawn path to load `coder-open.md` for initial spawns and
`coder-fix.md` for resumes where source ∈ {reviewer, tester, human}. Add a test
asserting that a resumed coder session does not include opener-specific text
(e.g., "Clone and branch", "Open the PR", step-number markers from the opener
flow).

**F-014-U-2: Extract `coder-fix-ci.md` for CI-source resumes**
The `SOURCE: ci` fix path is structurally distinct: no GraphQL thread fetch, no
per-thread decision loop, no inline replies. Currently it shares a section with the
inline-source path under an `if SOURCE == ci` conditional that the model must
mentally parse. Create `prompts/roles/coder-fix-ci.md` with only the CI-failure
flow. The worker selects it when `address_review(source='ci', ...)` is called.
Remove the `SOURCE: ci` branch from `coder-fix.md`. Each prompt now has exactly
one path.

**F-014-U-3: Extract self-review into `coder-selfreview.md`, compose into opener**
Lift step 8 ("Simplify pass" + "Arch-drift pass") from `coder-open.md` into
`prompts/roles/coder-selfreview.md`. Compose it back into `coder-open.md` via
`{{include roles/coder-selfreview.md}}` at the correct position in the workflow.
The net runtime prompt is identical; what changes is that the self-review persona
is now a separately versioned, separately testable artifact. Add a test that
loads `coder-selfreview.md` standalone and asserts it contains both "Simplify"
and "Arch-drift" pass instructions.

**F-014-U-4: Split `reviewer.md` → `reviewer-stance.md` + `reviewer-method.md`**
`reviewer-stance.md`: the adversarial stance rationale, Three Promises, Red Flags,
tone rules, and "The Bottom Line". Changes rarely; owned by whoever sets review
philosophy.
`reviewer-method.md`: the 7-step procedure (Inventory → Read conventions → Diff
top-to-bottom → Cross-check interfaces → Trace data flow → Diff deletions →
Sanity-check tests), severity tiers, posting recipe, and terminal-marker table.
Changes with workflow or GitHub API updates.
Both are composed into the runtime reviewer prompt via `{{include}}`. The Golden
snapshot for `reviewer.md` is updated to reflect the composed result (byte-
identical to today's text). Add tests asserting each standalone file parses without
unresolved `{{include}}` directives.

**U-1 depends on F-013-U-1 (composer). U-2 depends on U-1. U-3 depends on U-1.**
**U-4 depends on F-013-U-1. U-1 and U-4 are independent of each other.**

## Acceptance

- A resumed coder agent (fix-loop) receives a prompt that does NOT contain the
  opener steps 1–11 text. Verified by asserting absence of "Clone and branch" /
  "Open the PR" / "PR_URL:" instruction text in the rendered fix-loop prompt.
- A CI-source resume uses `coder-fix-ci.md` and does not include GraphQL-fetch
  instructions or the inline-reply flow.
- `coder-selfreview.md` exists as a standalone file containing both Simplify-pass
  and Arch-drift-pass instructions. It is `{{include}}`d in `coder-open.md` at the
  correct workflow position.
- `reviewer-stance.md` and `reviewer-method.md` exist. The composed reviewer prompt
  is byte-identical to today's `reviewer.md` (golden-snapshot passes).
- No opener instructions appear in any fix-loop prompt. No fix-loop instructions
  appear in the opener prompt. Verified by unit tests.
- The combined token count of the fix-loop prompt (coder-fix.md composed) is at
  least 100 tokens fewer than the current coder.md.

## Out of scope

- Changing any agent behavior or cycle logic.
- Converting self-review into a separate spawned agent role — that is D-4 in F-016
  and is deferred pending metrics on whether same-session self-review catches
  sufficient bugs.
- Structured output / tool-call replacement of marker strings — F-015.
- Prompt content improvements (rewriting, strengthening) — these specs cover
  *structure*, not *substance*. Substance improvements can be done as standalone
  commits once the structure is stable.
- Any change to the `address_review` MCP tool signature.

## Approach

The worker's role-selection logic lives in `managed_agent.py` (and its Docker
equivalent). The selection is a simple conditional:

```
if call_type == "spawn":
    prompt = compose("roles/coder-open.md")
elif call_type == "resume" and source == "ci":
    prompt = compose("roles/coder-fix-ci.md")
elif call_type == "resume":
    prompt = compose("roles/coder-fix.md")
```

The `source` is already available at the `address_review` call site in
`execution.py`; it needs to be plumbed into the worker's resume call as a prompt
selector parameter.

Self-review (U-3) stays in the same coder session: `coder-selfreview.md` is
composed inline into `coder-open.md` via `{{include}}`, not a separate spawn.
This preserves the coder's full context (the diff it just created, the CLAUDE.md it
read, the repo structure it explored) which is essential for a meaningful
Arch-drift pass.

## Constraints

- **No token increase.** Each composed prompt must have fewer or equal tokens vs.
  the current monolithic `coder.md`. The fix-loop prompt should be meaningfully
  shorter (target: ≥ 30% reduction vs. current coder.md).
- **Backward compatible.** Existing MCP tool signatures (`spawn_unit`,
  `address_review`) are unchanged. The role-selection is internal to the worker.
- **Golden snapshots must pass.** The `coder-open.md` + `coder-fix.md` +
  `coder-fix-ci.md` composed outputs together cover all the text in the current
  `coder.md` (with duplication eliminated by F-013).

## Decisions

- **Same-session self-review (compose into opener) over separate spawn**: a separate
  spawn for self-review would lose the in-session context (explored file tree,
  understood CLAUDE.md conventions, the actual diff just written). A same-session
  prompt-include preserves all of that. Cost is zero (no extra API call). Revisit
  if D-4 metrics show separate-spawn produces materially sharper findings.
- **`coder-fix-ci.md` as a separate file over a conditional block**: a conditional
  inside `coder-fix.md` ("if SOURCE is ci, do this instead") still requires the
  model to branch — the whole point of this split is to eliminate model-side
  branching. Two small files with one path each is cleaner.

## Open questions

- Should `source` be passed as an explicit parameter to the worker's `resume()`
  method, or should the worker infer it from the task message content? Explicit
  parameter is cleaner; check whether `docker_claude_code.py`'s resume signature
  needs the same change.
- The "Edge cases" section in current `coder.md` (lines 279–301) covers three
  dispatcher-level cases the orchestrator could handle instead of the agent.
  Evaluate during U-1/U-2 implementation: can any of these be eliminated by the
  orchestrator sending clearer task messages, rather than being documented in the
  prompt?
