# agent-orchestrator

Multi-agent SDLC orchestrator. A **project lead** Claude Code session you
chat with from your phone (via Remote Control) breaks features into work
units and spawns **coder / tester / reviewer** agents on Anthropic Managed
Agents to do the work. PRs land on GitHub; you push the merge button.

**Engineering reference**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is
the canonical doc for how this is built and why. Read that before making
non-trivial changes.

**Contributing**: [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the dev
workflow, repo layout, quality gates, and commit conventions for changes
to this repo.

## Status: v1 complete (Stages 1-5)

The orchestrator can take a feature description, break it into a work-unit
DAG with the user's approval, spawn coder/tester/reviewer Managed Agents
per unit, run an automated fix loop (cap = 3 cycles), open PRs, post bug
findings as PR comments, escalate to your phone via ntfy.sh when needed,
and schedule next-ready units as merges complete.

You merge PRs. Everything else is automated.

## Quick start

```bash
# 1. Clone + install (requires Python 3.11+ — any of python3.11 / 3.12 / 3.13 works)
git clone https://github.com/joeboctor/agent-orchestrator.git
cd agent-orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Interactive setup (writes .env, initializes state.db)
orchestrator init

# 3. Verify everything is ready
orchestrator doctor

# 4. Launch Claude Code with the lead
orchestrator run

# 5. In another terminal — live dashboard
orchestrator dashboard
```

The `orchestrator init` wizard prompts for:

- **Anthropic API key** — from [console.anthropic.com](https://console.anthropic.com)
  (separate billing from your claude.ai subscription)
- **GitHub auth** — choose:
  - **App** (recommended for teams / real repos) — bot identity,
    1-hour-lived tokens, easier to revoke. You'll provide App ID,
    Installation ID, and a path to the `.pem` private key.
  - **PAT** (fast single-user setup) — fine-grained PAT, your user
    identity. See "GitHub PAT" section below for required scopes.
- **ntfy.sh topic** (optional) — for phone push notifications

## CLI commands

| Command | What it does |
|---|---|
| `orchestrator init` | Interactive setup wizard (writes `.env`, initializes `state.db`) |
| `orchestrator doctor` | Health check — 10 checks across env, tokens, claude CLI, package |
| `orchestrator run` | Launches Claude Code with `--remote-control` and clean env |
| `orchestrator dashboard` | Live TUI dashboard (~2s refresh, Ctrl+C to quit) |
| `orchestrator version` | Print installed version |


## GitHub PAT

If you chose **PAT** in `orchestrator init`, create a **fine-grained
personal access token** at
[github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens).

**Repository access:** select the specific target repo(s) the orchestrator
will open PRs against. ("All repositories" works but grants broader scope
than necessary — fine-grained PATs are repo-scoped at creation time and
re-issuing later requires regenerating the token.)

**Repository permissions:**

| Permission | Access | Used for |
|---|---|---|
| Metadata | Read | Required on every fine-grained PAT (auto-included) |
| Contents | Read & write | Clone, branch, commit, push (coder + tester); CODEOWNERS lookup in `verify_repo` |
| Pull requests | Read & write | `gh pr create`, `gh pr review --comment / --request-changes`, PR conversation comments, Copilot review requests, PR state polling |
| Issues | Read & write | PR conversation comments use the issues API (`/issues/<n>/comments`) |
| Actions | Read | CI-green gate polling — `check_runs` produced by GitHub Actions workflows |
| Commit statuses | Read | Status-API check results (non-Actions CI) |
| Administration | Read | `verify_repo` reads branch-protection rules at `/branches/<default>/protection` |

**Do NOT grant `Workflows`.** Its absence is a deliberate defense layer
preventing agents from modifying `.github/workflows/*` even if the coder
prompt's hard rule fails — see
[SECURITY.md](SECURITY.md#what-the-orchestrator-defends-against) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

After issuing, paste the token as `GITHUB_TOKEN` when `orchestrator init`
prompts, or write it directly into `.env`. Fine-grained PATs default to a
90-day lifetime — rotate before expiry.

**Classic (non-fine-grained) PATs are not recommended:** they grant
org-wide scopes and can't be repo-restricted. If you must use one, the
closest equivalent is `repo` (which is broader than what's listed above).

**Alternative — GitHub App** (recommended for teams): bot identity,
1-hour-lived tokens, easier rotation and audit. Pick this path in
`orchestrator init` instead of PAT.

## Smoke test

After `orchestrator run`, paste into chat:

> Run the hello world smoke test.

The lead calls `hello_world_test()` — spawns a Managed Agent on Anthropic's
infra, gets a response, archives. Successful output:

```
session_id=session_abc123...
response='hello from a managed agent'
```

If this works, the full chain is wired up. See `examples/` for real
multi-unit features to run next.

## Optional: ntfy.sh phone push setup

For escalations and ready-to-merge notifications to reach your phone:

1. Pick a hard-to-guess topic name. Anyone with this string can send/read
   messages on the public ntfy.sh server — treat it like a password.
   ```bash
   python -c "import secrets; print('agent-orch-' + secrets.token_urlsafe(12))"
   # → agent-orch-Xj9k_aB2c-LpN3qR  (use this as your topic)
   ```
2. Set `NTFY_TOPIC=<that-string>` in `.env`.
3. Install the [ntfy mobile app](https://ntfy.sh/) (iOS or Android).
4. In the app: **Subscribe to topic** → paste the same string.
5. Test from the laptop:
   ```bash
   curl -d "test push" "ntfy.sh/$(grep '^NTFY_TOPIC=' .env | cut -d= -f2)"
   ```
   You should get a push within seconds.

The orchestrator pushes on:
- Escalations (`🚨` — cap-3 hit, BLOCKED, agent error)
- Ready-to-merge (`✅` — reviewer endorsed, awaits your merge)

Both include the PR URL as a click action. Leave `NTFY_TOPIC` empty to
disable pushes entirely.

## Live TUI dashboard

Single-pane real-time view of orchestrator state. Run in a separate
terminal (or tmux split) while Claude Code runs in the main one:

```bash
./scripts/dashboard.sh
```

Shows five panels:

- **Features** — every feature with status, cost estimate, and progress (`done/total`)
- **In flight** — units actively being worked on (coding/testing/in_ci/reviewing/fixing)
- **Awaiting your merge** — PRs the orchestrator has shipped and is waiting for you to merge
- **Escalated** — units that hit cap-3 or BLOCKED, need your intervention
- **Recent events** — last 10 entries from `unit_events` across all features

Refreshes every 2s, Ctrl+C to quit. Read-only — never writes to `state.db`,
safe to run alongside the MCP server.

## Developer setup (contributing)

If you'll be editing the code, install the pre-commit hook so style +
hygiene checks run on every `git commit`:

```bash
pip install -e ".[dev]"     # if you haven't already
pre-commit install          # wires .git/hooks/pre-commit
```

Manual run on the whole repo:

```bash
pre-commit run --all-files
```

The pre-commit hooks (defined in `.pre-commit-config.yaml`) run:
- File hygiene: trailing whitespace, EOF newlines, YAML/TOML validation,
  large-file blocker, merge-conflict markers, line-ending normalization,
  private-key detector
- Ruff lint (with `--fix` auto-applied)
- Ruff format

Slower checks (`mypy`, `bandit`, `pytest`) run in CI on every push/PR,
not locally per-commit. See `.github/workflows/ci.yml` for the full CI
matrix (Linux + macOS + Windows × Python 3.11 + 3.12).

**Coverage gate**: PRs are blocked if test coverage drops below **80%**.
Current: **87%** (260+ tests). Run locally with `pytest` — the coverage
report prints automatically. The gate is in `pyproject.toml`
(`addopts = "... --cov=orchestrator ..."`) and CI adds
`--cov-fail-under=80`.

## State backup

`state.db` is the source of truth. Snapshot it periodically:

```bash
./scripts/snapshot_state.sh    # creates snapshots/state.db.YYYYMMDD-HHMMSS
                                # keeps the 30 most recent
```

Wire into cron or launchd if you want automatic snapshots. Or just rely
on Time Machine / your normal backup setup.

## Network allowlist (Managed Agent containers)

Coder/tester/reviewer containers run with **`limited` outbound networking**
— they can only reach the hosts in `ALLOWED_NETWORK_HOSTS`
(`orchestrator/agents.py`), plus public package registries (PyPI, npm, etc.)
via the `allow_package_managers: True` flag.

This is defense against:
- Token exfiltration if the agent goes rogue or runs poisoned dep code
- Code/data exfiltration to attacker-controlled endpoints
- Most dependency-confusion attacks

Default allowed hosts:

```python
ALLOWED_NETWORK_HOSTS = [
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "api.anthropic.com",
]
```

**To add hosts** (e.g. your org's private package registry, internal docs):
edit `ALLOWED_NETWORK_HOSTS` in `orchestrator/agents.py`. The change auto-
invalidates the cache (resource signature includes env_config), so the
next spawn creates a fresh environment with the new allowlist.

**If an agent reports a network failure** (curl/git/pip hanging or 403),
check whether the URL is in the allowlist. The agent will silently fail
on blocked hosts — there's no friendly error message.

## Cache management

The orchestrator caches (agent_id, environment_id) per role to avoid
re-creating them on every spawn. Cache key = sha256 of `role + prompt + model`.

**Three ways the cache invalidates:**

1. **Prompt edit** — editing `coder.md` / `tester.md` / `reviewer.md` changes
   the signature → next spawn creates a fresh agent with the new prompt.
2. **Model change** — bumping `DEFAULT_MODEL` in `agents.py` changes the
   signature → next spawn uses the new model.
3. **TTL (time-based)** — cached entries older than `MAX_CACHE_AGE_DAYS`
   (default 30) are treated as cache misses on lookup. This lets Anthropic's
   underlying improvements roll in over time without manual refreshes.

**Not in the cache key** (intentionally):
- **Networking config** (`ALLOWED_NETWORK_HOSTS`) — static; doesn't change
  in practice. If you ever change it, run `./scripts/reset_cache.sh`.
- **Tools list** (`DEFAULT_TOOLS`) — same reason.

**Manual reset** (for tools change, network change, debugging, or after an
Anthropic API change that breaks existing agents):

```bash
./scripts/reset_cache.sh
# or from the lead chat: "reset cached resources"
# (lead will call the reset_cached_resources MCP tool)
```

Old/aged-out agents get orphaned on Anthropic's side but cost nothing
while idle.

## Required before Stage 3 (target-repo setup)

Before agents start opening PRs against your target repo, configure
**branch protection on `main`**:

1. Repo → Settings → Branches → Add branch protection rule for `main`
2. Require a pull request before merging: ✅
3. Require approvals: **1**
4. Dismiss stale pull request approvals when new commits are pushed: ✅
5. Require approval from someone other than the last pusher: ✅
6. Restrict who can dismiss reviews: ✅ (just you)
7. Do not allow bypassing the above settings: ✅
8. Allow force pushes: ❌ (disabled)
9. Allow deletions: ❌ (disabled)

This is the **only enforcement layer** that mechanically prevents agents
from merging. The orchestrator's lack of a `merge_pr` MCP tool plus the
role-prompt rules ("NEVER merge") are belt-and-suspenders; branch
protection is the bedrock.

## Human review as the merge gate

Every PR the orchestrator opens lands in front of a human before merge.
Two paths:

1. **Repos with CODEOWNERS** (recommended for production). GitHub
   auto-requests review from the owning team on every bot PR. The
   orchestrator's reviewer agent emits `REVIEW_RECOMMEND_MERGE` as a
   pre-screen endorsement; humans formally approve and merge.
2. **Repos without CODEOWNERS** (sandbox / single-developer). The user
   is the only reviewer. Same `REVIEW_RECOMMEND_MERGE` terminal state;
   user merges from chat or the GitHub UI.

The orchestrator deliberately has no `merge_pr` tool. Branch protection
on the default branch (verified by `verify_repo` before any spawn)
ensures the only path from "PR open" to "merged into main" goes through
a human clicking "merge".

`REVIEW_RECOMMEND_MERGE` is the standard terminal outcome — not a
workaround. The bot SHOULD NOT approve its own work. Two-identity
setups (App-for-coder + PAT-for-reviewer) are tracked in `BACKLOG.md`
as an option for teams that explicitly want bot-only approval flows,
but they are not the recommended path.

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for a thorough list of
common issues with fixes. The quickest first step is:

```bash
orchestrator doctor
```

which runs ~10 checks (Python version, env file, token formats, live
GitHub auth, claude CLI installed, etc.) and tells you exactly what's
wrong.

## Architecture (where we're heading)

```
You (mobile, Claude app via Remote Control)
   ↕
Lead = Claude Code session in this directory
   ↓ MCP tool calls
Orchestrator MCP server (this Python package)
   ↓ ManagedAgentWorker → Anthropic Managed Agents API
Coder / Tester / Reviewer sessions (gVisor sandboxes on Anthropic infra)
   ↓ git push, gh pr create
GitHub (your repo)
```

The `Worker` protocol in `orchestrator/agents.py` is a portability seam —
v1 is `ManagedAgentWorker`, future impls (local executor, Modal, E2B,
non-Claude models) plug in without touching the lead or state machine.
