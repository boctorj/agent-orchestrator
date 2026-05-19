# F-013: Prompt composition + deduplication foundation

## Intent

The three role prompts (`coder.md`, `tester.md`, `reviewer.md`) share 25–30% of
their content verbatim: the sandbox environment block, the GitHub auth bootstrap,
the hard-rules list, the BLOCKED taxonomy (8 slugs × ~10 lines each × 3 files = ~90
duplicate lines), the project-conventions discovery pattern, and the inline-review
posting recipe. Every cross-cutting change today requires three edits; it is easy
to forget one. The BLOCKED taxonomy has a fourth source of truth in
`orchestrator/blocked_reasons.py`, so the Python enum and the prose can silently
drift.

This feature introduces:
1. A **`{{include path}}`** composer in the prompt loader so role prompts can pull
   in shared fragments at runtime.
2. A **`prompts/common/`** layer holding all cross-cutting content.
3. **Codegen of `common/blocked-taxonomy.md`** from `blocked_reasons.py` so the
   enum is the only source of truth.
4. **Golden-snapshot tests** that prove the composed prompts are byte-identical to
   what agents receive today, making this a safe zero-behavior-change refactor.

No agent behavior changes. No role decomposition (that is F-014). No structured
outputs (that is F-015). Pure structural refactor with a mechanical safety net.

### Units

**F-013-U-1: `{{include}}` composer in prompt loader**
Add a `_compose_prompt(path)` function in `orchestrator/agents.py` (or the module
that currently loads `prompts/*.md`). It reads a file, resolves every
`{{include <relative-path>}}` directive recursively, and returns the fully-expanded
string. No third-party template engine — a single `re.sub` loop is sufficient.
The existing prompt-loading call sites are updated to go through `_compose_prompt`.
A golden-snapshot test captures the current composed text for each role and fails
if future edits change it without a deliberate snapshot update.

**F-013-U-2: Extract `common/environment.md`, `common/auth.md`,
`common/hard-rules.md`, `common/project-conventions.md`**
Move the four blocks duplicated verbatim across all three role prompts into
`prompts/common/`. Update each role prompt to `{{include}}` them. Verify via
golden-snapshot that the composed text is byte-identical to the pre-refactor file.
Approximate savings: ~60 lines removed from the combined prompt corpus.

**F-013-U-3: Codegen `common/blocked-taxonomy.md` from `blocked_reasons.py`**
`orchestrator/blocked_reasons.py` is the authoritative enum of BLOCKED slugs.
Add a `render_blocked_taxonomy() -> str` function there that produces the markdown
prose table. At prompt-load time, `_compose_prompt` resolves
`{{include common/blocked-taxonomy.md}}` by calling `render_blocked_taxonomy()`
rather than reading a static file. Add a CI test: import `blocked_reasons` and
assert the rendered output matches a committed golden snapshot; the snapshot must
be regenerated (and the test re-passes) whenever the enum changes. Remove the
three inline prose copies from the role prompts. Approximate savings: ~90 lines.

**F-013-U-4: Extract `common/review-posting.md`**
Both `tester.md` and `reviewer.md` carry the same ~30-line recipe: SHA extraction
via `gh pr view --json headRefOid`, the `python3 - <<'PY'` heredoc that builds the
JSON payload, and the `gh api -X POST .../reviews` call. Move this into
`prompts/common/review-posting.md`. Both role prompts `{{include}}` it. Add the
golden snapshot. Approximate savings: ~30 lines.

**F-013-U-1 has no deps. U-2, U-3, U-4 depend on U-1.**
U-2, U-3, U-4 are independent of each other once U-1 lands and can be parallelized.

## Acceptance

- `prompts/common/` directory exists with at minimum: `environment.md`, `auth.md`,
  `hard-rules.md`, `project-conventions.md`, `review-posting.md`.
- `common/blocked-taxonomy.md` is generated at prompt-load time from
  `blocked_reasons.py`; no static copy of the taxonomy prose exists in any role
  prompt.
- Golden-snapshot tests pass for all three role prompts: the text sent to the
  Managed Agent API is byte-identical before and after this refactor.
- CI fails if `blocked_reasons.py` is edited and the taxonomy golden snapshot is not
  updated to match.
- The combined line count of the three role prompts (excluding `common/` includes)
  is reduced by ≥ 150 lines.
- No role prompt contains the auth bootstrap, environment block, hard-rules list, or
  BLOCKED taxonomy as inline prose.

## Out of scope

- Role decomposition (coder-open vs coder-fix split) — F-014.
- Structured output / tool-call replacement of marker strings — F-015.
- Reviewer stance vs. method split — F-014.
- Any change to agent behavior, cycle logic, or state machine.
- Jinja2 or any other template engine dependency — the `{{include}}` resolver is a
  10-line `re.sub` loop; no new package dependency is acceptable.

## Approach

The `{{include <path>}}` directive is resolved relative to `prompts/`. Paths use
POSIX separators. Recursive includes are allowed (depth limit: 3) to support
future nesting without risk of cycles. The resolver is deterministic and stateless.

`render_blocked_taxonomy()` in `blocked_reasons.py` produces a markdown code block
(matching the current prose format) so the substitution is transparent to agents.

Golden snapshots live in `tests/snapshots/prompts/` as `.md` files committed to
git. The snapshot test loads each role prompt through `_compose_prompt`, diffs
against the committed snapshot, and fails with a unified diff on mismatch. A
`make update-snapshots` target regenerates them for intentional updates.

## Constraints

- **Zero behavior change.** Agents receive identically the same text as today.
  The golden-snapshot gate enforces this mechanically.
- **No new runtime dependencies.** The composer is pure-stdlib Python.
- **Load-time composition, not build-time.** Prompts are composed when the MCP
  server starts, not pre-baked into a dist artifact. This keeps the development
  loop fast (edit a `common/` file, restart the server, test immediately).
- **Backward compatible.** Role prompts that do not use `{{include}}` continue to
  load unchanged. The composer is strictly additive.

## Decisions

- **`{{include}}` over Jinja2**: Jinja2 is a compile-time dependency and introduces
  a new attack surface (template injection). A raw regex resolver is 10 lines, has
  no footgun, and covers everything we need. If variable substitution is needed
  later (F-015 renders marker prose from a schema), it can be added as a second
  pass without changing the include mechanism.
- **Load-time, not build-time codegen**: build-time codegen requires a CI step that
  produces committed artifacts, which creates "committed generated files" merge
  conflict patterns. Load-time keeps the source-of-truth in the Python file and the
  rendering in memory.
- **Golden snapshots over diff-on-deploy**: a deploy-time diff would catch drift
  too late (after a broken prompt reached the agent). CI-time snapshots catch it
  in the PR.

## Open questions

- Should `render_blocked_taxonomy()` be called at import time (module-level constant)
  or lazily on first prompt load? Import-time is simpler; lazy is safer if the
  module is ever imported in contexts where rendering isn't wanted (e.g. test stubs).
  Likely import-time; revisit if test isolation issues surface.
