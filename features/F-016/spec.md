# F-016: Defense-in-depth — server-side enforcement and prompt versioning

## Intent

The orchestrator's hard rules ("NEVER merge a PR", "NEVER touch `.github/workflows/*`",
"NEVER push outside `tests/`") are today enforced solely by prose in the agent
prompts. A model that ignores, misreads, or hallucinates past these rules has no
mechanical backstop. The security and correctness literature is clear: prompt-level
constraints are aspirational; server-side and infrastructure-level enforcement is
defense-in-depth.

Three specific gaps this feature closes:

1. **Marker protocol violations are silent.** If an agent emits zero terminal
   markers (or two), `execution.py` falls through to `_escalate_no_marker` with a
   generic "no marker found" message. After F-015 lands, tool-call dispatch makes
   the count precise — this unit adds a structured error for protocol violations.

2. **"NEVER touch `.github/workflows/*`" and "NEVER push outside `tests/`" are
   prose rules.** A pre-push git hook in the worker sandbox can enforce both
   mechanically. This is the same defense-in-depth principle that makes branch
   protection meaningful: don't rely on the committer to self-enforce.

3. **Prompts have no version metadata.** When a prompt file changes, there is no
   visible signal in the PR diff beyond "lines changed." There is no record of
   which version of a prompt was active when a particular unit ran, making it hard
   to correlate agent behavior regressions with prompt edits. Versioned headers and
   a CI gate address both.

This feature is positioned last in the sequence because:
- Unit D-1 (marker validation) requires F-015's tool-call dispatch to have precise
  marker counts.
- Units D-2 and D-3 are independently useful but lower-risk than A–C and can wait
  for the structural foundation to be in place.

D-4 (spawning self-review as a separate agent rather than a same-session compose)
is deliberately deferred: it requires cycle-level metrics on whether F-014's
same-session self-review (`coder-selfreview.md`) catches sufficient bugs to justify
the added cost and complexity.

### Units

**F-016-D-1: Structured marker-emission validation**
After `dispatch_marker` (F-015-U-3) processes an agent response, validate:
- Exactly one terminal marker tool call was made (no more, no fewer).
- The marker is appropriate for the role (e.g., a tester must not emit
  `emit_review_request_changes`; a reviewer must not emit `emit_pr_url`).
On violation, produce a structured `BLOCKED: reason=marker_protocol_violation`
event (add the slug to `blocked_reasons.py`) with fields: `role`, `marker_count`,
`markers_seen: list[str]`. Route to the existing escalation path. Add tests:
- Agent emits zero markers → `marker_protocol_violation` escalation.
- Agent emits two markers → `marker_protocol_violation` escalation.
- Agent (tester role) emits `emit_review_request_changes` → `marker_protocol_violation`.
This unit **depends on F-015-U-3** (tool-call dispatch must be live).

**F-016-D-2: Worker-side pre-push hook for scope enforcement**
Add a `pre-push` git hook to the worker sandbox image that enforces two rules:
1. **Workflows guard**: if any file in `git diff --name-only origin/main..HEAD`
   matches `.github/workflows/*`, the push is aborted unless the environment
   variable `ORCH_ALLOW_WORKFLOW_CHANGES=1` is set. The orchestrator sets this
   variable only when the unit description carried the `[WORKFLOW]` annotation (per
   the CLAUDE.md convention). This converts the prose rule into a mechanical
   backstop — the agent can try to push a workflow change, the hook aborts it, the
   agent gets a non-zero exit from `git push`, and emits `BLOCKED`.
2. **Tester scope guard**: if the worker role is `tester`, abort the push if any
   file outside `tests/` (or the project's equivalent test directory, configurable
   via `ORCH_TEST_DIR`) is included in the commits/refs being pushed. Prevents
   tester agents from accidentally pushing implementation fixes instead of
   reporting them.
The hook is a shell script committed to `docker/hooks/pre-push` and copied into
`.git/hooks/` during worker sandbox initialization. Add tests: push a workflow file
without the flag, assert non-zero exit and structured BLOCKED emitted by the agent.
**Note: this unit modifies `docker/` and potentially `.github/workflows/*` if the
sandbox image build pipeline is CI-managed.** Add `[WORKFLOW]` to unit description
when spawning.

**F-016-D-3: Prompt version headers and CI drift gate**
Add a version header to every composed role prompt at load time:
```
<!-- Role: coder-open · Version: 7 · markers: markers.py@<sha> · composed: <timestamp> -->
```
The `Version` integer is stored as a `version: N` frontmatter field at the top of
each role prompt file (e.g., `prompts/roles/coder-open.md`). It must be manually
incremented (by the PR author) whenever the role prompt's composed output changes
materially. A CI test fails if:
- A role prompt file or any of its `{{include}}`d dependencies changed in this PR,
  AND
- The `version:` field did not change.
This creates a forcing function: prompt edits are visible and intentional. The
header also appears in agent-session audit logs, enabling correlation of behavior
regressions with specific prompt versions.
Separate from `Version`: a `markers-sha` field in the header records the git SHA of
`markers.py` at compose time, making it possible to audit which marker schema was
active for any given agent run.
**This unit depends on F-013-U-1** (the composer inserts the header) and on
**F-015-U-1** (`markers.py` must exist to supply the SHA).

## Acceptance

- **D-1**: An agent response with zero terminal tool calls produces a
  `marker_protocol_violation` BLOCKED event (not a generic `no_marker` escalation).
  An agent response with two terminal tool calls produces the same. A tester
  emitting `emit_review_request_changes` produces the same. All three cases are
  covered by automated tests.
- **D-2**: Attempting to push a file matching `.github/workflows/*` in the worker
  sandbox without `ORCH_ALLOW_WORKFLOW_CHANGES=1` exits non-zero and the agent
  emits `BLOCKED: reason=branch_protection_blocked_push` (or a new
  `workflow_push_blocked` slug). Verified in a sandbox repo test. The tester scope
  guard is separately verified by attempting to push an implementation file from
  the tester role.
- **D-3**: Every composed role prompt contains a version header with Role, Version,
  markers-sha, and composed timestamp. The CI gate fails when a role prompt's
  composed output changes without a `version:` bump. Confirmed by a test that
  artificially edits `common/auth.md` and asserts CI fails without a version bump
  on the affected role prompts.
- D-4 is explicitly **not** in scope for this feature's acceptance criteria.

## Out of scope

- **D-4 (separate self-review spawn)**: deferred until F-014's same-session self-
  review (`coder-selfreview.md`) has run on ≥ 5 real units and cycle logs show
  whether it catches material bugs. Revisit in BACKLOG.md when metrics are
  available.
- PAT scope restriction to individual file paths — GitHub's PAT scopes are coarse
  (repo-level, not path-level). This cannot be enforced via token configuration
  and requires the hook approach in D-2 instead.
- Restricting the reviewer's token from writing PR reviews beyond the assigned PR —
  the reviewer is already read-only by prompt; mechanical enforcement would require
  per-PR token scoping (not available via PAT or App installations).
- Any change to the cycle-counter logic, escalation caps, or state machine — purely
  structural enforcement, not policy.

## Approach

**D-1** is trivial once F-015-U-3 lands: `dispatch_marker` already has the full
list of `tool_use` blocks in the response. Count them; check role appropriateness
against a `ROLE_ALLOWED_MARKERS` dict in `markers.py`.

**D-2** is a shell script (`docker/hooks/pre-push`):
```sh
#!/bin/sh
# Block workflow changes unless explicitly authorized
changed=$(git diff --name-only origin/main..HEAD)
if echo "$changed" | grep -q "^\.github/workflows/"; then
  if [ "$ORCH_ALLOW_WORKFLOW_CHANGES" != "1" ]; then
    echo "HOOK BLOCKED: workflow changes require [WORKFLOW] flag in unit description" >&2
    exit 1
  fi
fi
# Tester scope guard
if [ "$ORCH_WORKER_ROLE" = "tester" ]; then
  test_dir="${ORCH_TEST_DIR:-tests}"
  outside=$(echo "$changed" | grep -v "^$test_dir/")
  if [ -n "$outside" ]; then
    echo "HOOK BLOCKED: tester may only push to $test_dir/" >&2
    exit 1
  fi
fi
```
The hook is installed during sandbox initialization
(`managed_agent.py` / worker entrypoint). `ORCH_WORKER_ROLE` and
`ORCH_ALLOW_WORKFLOW_CHANGES` are injected by the orchestrator's worker spawn call.

**D-3** version header is injected by `_compose_prompt` as the first line of the
composed output. The `version:` frontmatter field is stripped from the visible
prompt body (agents don't need to read it). The CI test is a pytest fixture that
collects all role prompt files, hashes their composed output, and compares against
a committed `tests/snapshots/prompt_versions.json` that maps
`{role_file: {version, composed_hash}}`. On change without version bump, the test
fails with a diff showing which files changed.

## Constraints

- **D-1 requires F-015.** Without tool-call dispatch, the marker count is
  approximate (regex can match anywhere in a long response). D-1 must land in the
  same release as or after F-015.
- **D-2 must not break existing Docker worker builds.** The hook is additive; the
  default env vars are unset (no guards active) so existing units run unchanged.
  Opt-in via `ORCH_WORKER_ROLE` being set.
- **D-3 version bumps are manual by convention, not enforced at the character
  level.** The CI gate detects *whether* a bump happened, not whether the bump is
  semantically correct. Rely on PR review for the latter.
- **D-4 is deferred.** Any PR that attempts to include D-4 scope should be rejected
  until the metrics threshold (5 real units with F-014 self-review) is met.

## Decisions

- **Pre-push hook over prompt-only for D-2**: the pre-push hook runs inside the
  agent's own git environment. It is a mechanical gate that fires regardless of
  what the model decides. A prompt-only rule requires the model to remember and
  self-enforce across a long session. Defense-in-depth demands both.
- **`marker_protocol_violation` as a new BLOCKED slug** (not `unknown`): the slug
  is structured, parseable, and can be surfaced to the dashboard distinctly from
  genuine failures. It also provides a diagnostic that points to the prompt/tooling
  contract, not the task.
- **Version header as a comment, not frontmatter**: agents must never see YAML
  frontmatter stripped by the composer — if the composer fails to strip it, the
  agent receives malformed instructions. HTML comments are safe (agents ignore
  them) and git-diff-readable.
- **D-4 deferred**: adding a separate self-review spawn before verifying that F-014's
  same-session approach is insufficient would be premature optimization. The cost
  of a separate spawn is real (extra API call, cold context, latency); the benefit
  is unproven at this point.

## Open questions

- For D-2: should the hook emit a structured `BLOCKED:` line to stdout for the
  orchestrator to parse, or rely on the agent to detect the non-zero exit from
  `git push` and emit its own BLOCKED? The latter is more consistent with existing
  coder behavior ("if push fails, emit BLOCKED") but requires the coder to interpret
  the hook's stderr message correctly. Decide during D-2 implementation.
- For D-3: what triggers a `version:` bump? Proposed convention: bump on any edit
  that changes the text an agent sees (content change); do not bump for pure
  reformatting or comment changes inside `{{include}}`d common files that don't
  alter prose. Codify this in `CONTRIBUTING.md` during D-3.
- D-4 revisit condition: after F-014 lands, define the metric explicitly. Proposed:
  "if coder-selfreview catches ≥ 1 material finding per 5 units in cycle logs,
  same-session is sufficient; if <1 per 10 units, revisit D-4." Add to BACKLOG.md
  when F-014 closes.
