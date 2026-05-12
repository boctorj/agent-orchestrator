# Troubleshooting

Common issues and fixes. Run `orchestrator doctor` first — it diagnoses
most of these automatically.

---

## Setup

### `orchestrator: command not found`

You haven't installed the package:

```bash
cd ~/repos/agent-orchestrator
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

The `orchestrator` command becomes available after `pip install -e .`
while the venv is active.

### `orchestrator init` hangs on the API key prompt

The hidden input doesn't echo characters as you type — paste your key,
press Enter. Paste should work even though you can't see what you typed.

---

## Auth & API

### `Auth conflict: Both a token and an API key are set`

Claude Code is reading both your claude.ai login token AND
`ANTHROPIC_API_KEY` from the shell environment. Result: Claude Code
uses the API key (and bills your API account) instead of your Team
subscription.

**Fix:** ensure `ANTHROPIC_API_KEY` is only in `.env`, never exported
to the shell. The `orchestrator run` command handles this automatically.
If you launch Claude Code another way, run `unset ANTHROPIC_API_KEY`
first.

### GITHUB_TOKEN returns 401

Token is invalid (expired, revoked, or never had `Metadata: Read`).

```bash
# verify directly:
TOKEN=$(grep '^GITHUB_TOKEN=' .env | cut -d= -f2-)
curl -i -H "Authorization: Bearer $TOKEN" https://api.github.com/user
```

If 401: regenerate at
[github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens)
with the permissions listed in README.

### GITHUB_TOKEN returns 404 on the sandbox repo

Token is valid but doesn't have the sandbox repo in its allowlist
(fine-grained PATs are repo-scoped at creation time).

**Fix:** regenerate the PAT with the sandbox repo selected, OR set
"Repository access: All repositories" (less secure but simpler).

### Anthropic API: "no credits" / 401

Your Claude.ai Team subscription does NOT include API credits — those
are billed separately through console.anthropic.com. Add a payment
method or check with whoever manages your org's API console access.

---

## Claude Code session

### MCP server not loading (`hello_world_test` tool unavailable)

Cause: Claude Code didn't pick up `.mcp.json`.

**Fixes (in order):**

1. **Restart Claude Code in the project directory.** Quit (`/quit`) and
   relaunch from `~/repos/agent-orchestrator` (not from elsewhere).

2. **Verify `.mcp.json` exists in the project root** (not in `.claude/`):
   ```bash
   ls -la .mcp.json
   ```

3. **Approve the workspace trust dialog.** First time Claude Code sees
   a `.mcp.json`, it asks "trust this project's MCP servers?" — approve.

4. **Check the MCP server starts standalone:**
   ```bash
   ./.venv/bin/python -m orchestrator.mcp_server
   # should hang waiting for stdio input (Ctrl+C to exit)
   ```

   If it errors: missing dep, import error, etc.

### `claude --remote-control` flag not recognized

Recent Claude Code feature, exact flag may have shifted versions. Check:

```bash
claude --help | grep -i remote
```

Update `orchestrator run`'s flag accordingly or run claude directly:

```bash
claude --remote-control
# or:
claude rc
# or:
claude remote-control
```

---

## Repo verification

### Spawn returns `ERROR: target repo ... is not verified`

The orchestrator gates every spawn behind a 24h-cached verification of
the target repo. If a repo has never been verified — or was verified
more than 24 hours ago — spawns are blocked.

**Fix:**

```bash
# From the laptop, before launching the orchestrator:
orchestrator verify-repo https://github.com/owner/repo

# Or from the lead's chat:
verify_repo("https://github.com/owner/repo")
```

The command runs ~5 GitHub API calls. On success it caches a row in
`verified_repos`. On failure it prints which policy check failed and
links to the exact fix (usually setting up branch protection at
github.com/owner/repo/settings/branches).

### `verify_repo` says "branch protection rule on `main` is missing"

The orchestrator REQUIRES branch protection on the default branch with:

- Require PR before merging
- Require ≥1 approving review
- Block force pushes (`allow_force_pushes = false`)
- Block deletion (`allow_deletions = false`)
- Block admin bypass (`enforce_admins = true`)

This is the same checklist the README lists under "Required before
Stage 3" — the orchestrator now enforces it programmatically before
allowing any spawn. Set the rules in repo Settings → Branches and
re-run `verify_repo`.

### `verify_repo` says "App installation covers this repo: not in installation's repo list"

You're using App auth (good — bot identity) but the App isn't installed
on this specific repo. Visit the App's installation settings on GitHub
and add the target repo to its allowed list.

If you need to work across multiple orgs from one orchestrator, see
BACKLOG.md → "Multi-installation App support" (deferred).

### Verification warns about CODEOWNERS / required signatures / status checks

These are non-blocking warnings — the repo IS verified and spawns will
proceed. They flag situations where the merge step may still get
blocked at GitHub layer:

- **CODEOWNERS**: a required-reviewer rule may not be satisfied by the
  reviewer agent's endorsement. Expect to merge manually.
- **required_signatures**: bot commits aren't GPG-signed by default;
  GitHub will block merge.
- **required status checks**: CI must pass before the PR can merge
  (informational — usually fine).

---

## Agent execution

### Coder agent reports "permission denied" cloning the repo

The fine-grained PAT doesn't include `Contents: write` on the target
repo. Re-issue with that permission added.

### Reviewer always says `REVIEW_RECOMMEND_MERGE`, never `REVIEW_APPROVED`

This is **expected** when all agents authenticate with one PAT — GitHub
blocks you from approving your own PR. The orchestrator handles this
gracefully (treats RECOMMEND_MERGE as terminal success).

Proper fix: register a GitHub App for the orchestrator (separate bot
identity). Documented in README under "Known limitation".

### `spawn_tester` / `spawn_reviewer` returns `refusing to spawn — CI is failing`

The standalone gate refuses to spawn a tester or reviewer against a PR
with red CI — testing or reviewing a broken-build PR is wasted work.

**Fix options:**

1. Run `cycle_review(feature_id, unit_id)` instead — the cycle's
   embedded fix loop will `address_review(source='ci', ...)` the
   coder and re-wait, automatically.
2. Open the PR on GitHub, inspect the failing check's logs, and
   manually `address_review(unit_id, 'ci', 'fix-it instructions')`
   yourself, then retry.
3. As an escape hatch (rarely correct): `send_to_unit(unit_id,
   'coder', '<instruction>')` to bypass the gate. Documented under
   "low-level" use only.

### `cycle_review` escalates with `CI did not settle within Ns`

The CI-wait timeout (default 10 min) elapsed while check_runs were
still pending. Common causes:

- A self-hosted runner is offline / queue is backlogged
- The workflow file changed and the new workflow takes longer than the
  default
- A workflow_dispatch / external workflow is required but never fired

**Fix:** confirm CI eventually completes on the PR, then retry
`cycle_review`. To raise the timeout for slow matrices, set
`CI_WAIT_TIMEOUT_SECONDS=1800` (30 min) in `.env` before launching
the orchestrator.

### `cycle_review` escalates with `cap of 3 cycles hit while fixing CI failure`

The orchestrator tried to fix CI 3 times in the same unit and CI still
failed. Either the failures aren't fixable by simple code changes
(infra flake, flaky test) or the coder can't diagnose them.

**Fix:** open `unit_history(unit_id)` to see what fixes were attempted,
then take over manually via `address_review` or close the PR and
re-spawn.

### Cycle hits cap-3 on a unit

ntfy push fires (if NTFY_TOPIC set). The full cycle history is on the
PR conversation and in `unit_history(unit_id)`. Options:

- **Take over manually**: `send_to_unit(unit_id, 'coder', 'do X')` to
  resume the coder session with your direction
- **Close the PR and re-spawn**: nuke the unit's row from `state.db`,
  re-spawn fresh
- **Update the unit description and retry**: the unit might have been
  too ambiguous; revise and `save_plan` + `approve_plan` again

### Agent reports network failure (`curl: (6) Could not resolve host`)

The Managed Agent's container has `limited` networking with a specific
allowlist. If the agent tries to reach a host not in
`ALLOWED_NETWORK_HOSTS` (defined in `orchestrator/agents.py`), it
silently fails.

**Fix:** add the host to `ALLOWED_NETWORK_HOSTS`, then:

```bash
./scripts/reset_cache.sh    # invalidate cached env config
# next spawn creates a fresh environment with the new allowlist
```

---

## State & dashboard

### Dashboard says "state.db not found"

Run the orchestrator at least once first to create state.db
(or `orchestrator init` to initialize it).

### `state.db` corrupted / locked

```bash
# Restore from the latest snapshot:
ls snapshots/state.db.*  # find most recent
cp snapshots/state.db.<latest> state.db
```

If you don't have a snapshot, you've lost state. Features themselves
(PRs, branches) still exist on GitHub — but the orchestrator won't
know about them. Start fresh with `rm state.db && orchestrator init`.

### Unit stuck in `coding` / `testing` / `in_ci` after Claude Code restart

The session is still running on Anthropic's side; local state didn't
get final-state updates. Use:

```
list_in_flight
resume_unit('F-001-U-X', 'coder')   # or 'tester' / 'reviewer'
```

The MCP tools return the session's current Anthropic-side status so
you can decide what to do (mark done, escalate, etc.).

---

## Cost

### Higher than expected charges

`feature_cost(feature_id)` shows session-hour estimates from event
timestamps. Token costs are NOT included. If charges seem high:

1. Check the Anthropic console for actual usage
2. Look for runaway agents (cap-3 cycles fired? agents still alive in
   the console?)
3. Run `reset_cached_resources` and check the Anthropic console for
   orphaned agents to delete manually

---

## When in doubt

```bash
orchestrator doctor
```

Runs ~10 checks and tells you what's broken.
