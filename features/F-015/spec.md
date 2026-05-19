# F-015: Structured output contracts — replace marker-string parsing with tool calls

## Intent

The orchestrator currently communicates with worker agents via **terminal marker
strings**: the agent emits a line like `PR_URL: https://...` or `REVIEW_RECOMMEND_MERGE: clean`
at the end of its response, and `execution.py` regex-matches it to decide what
happens next. This is a 2023-era pattern with three compounding problems:

1. **Four independent sources of truth.** The marker strings are defined in:
   - Regex constants in `orchestrator/tools/execution.py` (`TESTS_PASS_RE`,
     `BUG_FOUND_RE`, `REVIEW_RECOMMEND_MERGE_RE`, …)
   - Prose instructions in `coder.md`, `tester.md`, `reviewer.md` ("always end
     your response with exactly one of…")
   - Comments in `cycle_log_gh.py` (the cycle-log renderer references marker
     semantics)
   - Implicitly in any test that asserts specific outcome strings
   Changing a marker name or adding a new one requires edits in ≥ 4 places; drift
   is silent (a prose update that doesn't match the regex is a runtime bug).

2. **No structural enforcement.** The current design *hopes* the agent emits
   exactly one marker per response. Zero markers → `_escalate_no_marker`. Two
   markers → undefined behavior (whichever the regex matches first). Malformed
   markers (typo, extra whitespace, wrong case) silently fall through to
   `_escalate_no_marker` with no actionable diagnostic.

3. **The field has moved.** MetaGPT uses Pydantic-typed inter-agent I/O. SWE-agent
   validates command grammar from a YAML schema. AutoGen and LangGraph dispatch on
   function/tool calls. Text-marker parsing over LLM prose is the pattern to
   retire; native tool-use is the replacement.

This feature introduces:
1. **`orchestrator/markers.py`** — a single Python module defining every terminal
   marker as a typed schema (name, fields, semantic meaning, GitHub API `event`
   mapping). This becomes the only source of truth.
2. **Anthropic tool schemas** generated from `markers.py` and wired into the Managed
   Agent spawn as `tools=[…]`. Agents call `emit_pr_url(url=...)` instead of
   printing `PR_URL: ...`.
3. **`execution.py` becomes a tool-call dispatcher** — no more regex over text.
   The parser reads the agent's `tool_use` blocks and routes accordingly.
4. **`common/markers.md`** generated from `markers.py` — the prompt section
   "always end with exactly one of…" is rendered from the schema, so prose can
   never drift from the parser.

A **feature flag** (`ORCH_MARKERS_TOOL_USE=1`) keeps the old regex path alive for
one release window, allowing rollback without a revert.

### Units

**F-015-U-1: Define `orchestrator/markers.py` schema**
Create `orchestrator/markers.py` with a `TerminalMarker` TypedDict (or dataclass):
`name`, `description`, `fields` (list of typed field names), `github_event`
(for reviewer markers: `"REQUEST_CHANGES"` / `"COMMENT"` / `None`), `severity`
(maps to the coder/tester/reviewer role that emits it).
Define all current markers:
- `emit_pr_url` (fields: `url`)
- `emit_fix_pushed` (fields: none)
- `emit_tests_pass` (fields: none)
- `emit_bug_found` (fields: `summary`)
- `emit_blocked` (fields: `reason`, `key_values: dict`, `detail`)
- `emit_review_request_changes` (fields: `summary`)
- `emit_review_recommend_merge` (fields: `verdict`)
- `emit_review_comment` (fields: none)
Add a `to_anthropic_tool_schema() -> dict` method per marker that produces the
Anthropic `tool` JSON schema. Add a module-level `ALL_MARKERS` list.
Tests: round-trip — `to_anthropic_tool_schema()` for each marker produces valid
JSON; the `emit_blocked` schema includes `reason` as an enum of the slugs from
`blocked_reasons.py`.

**F-015-U-2: Wire tool schemas into worker spawn**
Update `managed_agent.py` (and `docker_claude_code.py` if applicable) to include
`tools=[m.to_anthropic_tool_schema() for m in ALL_MARKERS]` in the agent spawn
call. Gate on `ORCH_MARKERS_TOOL_USE` env flag — when unset, the old path (no
tools wired) remains active. Add an integration smoke-test: spawn a no-op agent
with the tools wired and assert the API call includes the tool schemas.

**F-015-U-3: Parser becomes a tool-call dispatcher in `execution.py`**
When `ORCH_MARKERS_TOOL_USE=1`, after each agent response the orchestrator reads
`response.content` for `tool_use` blocks instead of regex-matching `response.text`.
Each `tool_use.name` maps to a handler (previously the regex branch). Build a
`dispatch_marker(tool_use_block) -> MarkerOutcome` function that replaces the
`if TESTS_PASS_RE.search(text): ...` chain. The old regex chain becomes the
fallback (else branch) when the flag is off. Tests: mock an agent response with a
`tool_use` block for each marker, assert the correct orchestrator state transition
fires. Mock a response with two `tool_use` blocks, assert a structured
`marker_protocol_violation` error (anticipates D-1 from F-016).

**F-015-U-4: Render `common/markers.md` from `markers.py`**
Add `render_markers_prose() -> str` to `markers.py` that produces the markdown
section currently written by hand in the role prompts ("Always end your response
with EXACTLY ONE of… Decision rule… Terminal marker table"). The
`{{include common/markers.md}}` directive in each role prompt resolves this at
load time. Remove the hand-written terminal-marker sections from the role prompts.
CI check: any change to `markers.py` that changes `render_markers_prose()` output
must regenerate the markers golden snapshot or the CI fails.

**U-1 has no deps. U-2 and U-4 depend on U-1. U-3 depends on U-2.**
**U-4 depends on F-013-U-1 (the composer).** All units depend on F-013 landing first
(so the composer is in place for U-4's `{{include}}`).

## Acceptance

- `orchestrator/markers.py` exists. `ALL_MARKERS` covers all 8 current marker names.
  `to_anthropic_tool_schema()` produces valid Anthropic tool JSON for each.
- With `ORCH_MARKERS_TOOL_USE=1`, the agent spawn API call includes `tools=[…]`
  matching `ALL_MARKERS`. Confirmed by integration test against the Managed Agent
  client.
- With `ORCH_MARKERS_TOOL_USE=1`, `execution.py` dispatches on `tool_use` blocks,
  not on regex. No `*_RE.search(response.text)` call is executed in the hot path.
- With `ORCH_MARKERS_TOOL_USE=0` (default, unset), the old regex path is active
  and all existing tests pass unchanged.
- `common/markers.md` is generated from `markers.py`. No hand-written terminal-
  marker prose exists in any role prompt. Golden snapshot passes.
- CI fails if `markers.py` is edited and the markers golden snapshot is not updated.
- The `emit_blocked` tool schema constrains `reason` to the enum from
  `blocked_reasons.py`. Drift between the two modules is caught by CI.

## Out of scope

- Server-side validation of exactly-one-marker-per-response — that is F-016-D-1
  and depends on this feature landing first.
- Changing the marker semantics or adding new markers — this feature is a
  structural replacement, not a semantic redesign.
- Migrating Docker worker to tool-use (if the Docker worker uses a different API
  path) — flag it as an open question; handle in the same feature if trivial, split
  if not.
- Removing the regex fallback path — the fallback stays for one release window
  (see Constraints). Removal is a follow-on cleanup PR after rollout confidence is
  established.

## Approach

`markers.py` is a pure Python module with no runtime dependencies beyond stdlib.
The Anthropic tool schema format is a dict with `name`, `description`,
`input_schema` (JSON Schema). `emit_blocked.input_schema.properties.reason` is an
`enum` array populated from `[s.value for s in BlockedReason]` (or equivalent from
`blocked_reasons.py`).

The feature flag `ORCH_MARKERS_TOOL_USE` is read once at server startup and stored
in a module-level constant. No per-call branching overhead.

The dispatch function signature:
```python
def dispatch_marker(
    tool_use: ToolUseBlock,          # from Anthropic SDK
    ctx: UnitContext,
) -> MarkerOutcome:
    ...
```
`MarkerOutcome` is a sum type / discriminated union of the current outcomes
(`PrOpened`, `FixPushed`, `TestsPass`, `BugFound`, `Blocked`, `ReviewOutcome`).
The existing state-machine transitions are unchanged; only the extraction point
(tool call vs. regex) changes.

## Constraints

- **Feature flag for rollback.** `ORCH_MARKERS_TOOL_USE` defaults to unset (old
  path). Set to `1` to enable. This must work in the same release as the tool
  schemas are wired — the flag allows a production rollback in under 60 seconds
  without a deploy.
- **No marker semantics changes.** This is a structural replacement. The state-
  machine transitions, cycle-counter increments, ntfy payloads, and dashboard
  updates are identical before and after.
- **Billing impact acknowledged.** Tool schemas add a small payload to each spawn
  call (~200–400 tokens for 8 tool schemas). Acceptable. Document in release notes.
- **All existing tests must pass with flag off.** The old path must be fully intact
  until the flag is flipped globally.

## Decisions

- **TypedDict over dataclass**: TypedDict serializes to JSON natively (no
  `asdict()` call needed); the Anthropic SDK expects dicts. Simpler for
  `to_anthropic_tool_schema()`.
- **`emit_` prefix on tool names**: distinguishes orchestrator terminal-marker
  tools from any repo-side tools the agent might call. Reduces collision risk.
- **One release window for the regex fallback**: two weeks is enough to gain
  confidence; longer delays the cleanup. A deliberate sunset date should be set
  when this feature is approved.

## Open questions

- Does the Anthropic Managed Agent API support `tool_choice: "none"` to prevent
  the agent from calling tools mid-session (only at the terminal)? If not, agents
  could call `emit_pr_url` mid-stream and the dispatcher would fire prematurely.
  Investigate: if `tool_choice` isn't available, add a wrapper that ignores
  non-terminal tool calls (e.g., only dispatch the *last* `tool_use` block in the
  response).
- The Docker worker (`docker_claude_code.py`) uses a different invocation path
  (subprocess `claude` CLI vs. SDK). Does the CLI expose `--tool` flags? If not,
  the Docker path may need to keep regex matching longer. Assess during U-2.
- Should `emit_blocked` call the existing `_escalate` path, or should the
  dispatcher have a separate `_escalate_structured` path that surfaces the typed
  fields (reason enum, key-values) to the dashboard and ntfy payload more richly
  than the current regex extraction does? This is a cheap improvement if done here.
