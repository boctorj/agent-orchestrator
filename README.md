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

## Status: v1 complete (Stages 1-8)

The orchestrator can take a feature description, break it into a work-unit
DAG with the user's approval, spawn coder/tester/reviewer Managed Agents
per unit, run an automated fix loop (cap = 3 cycles), open PRs, post bug
findings as PR comments, escalate to your phone via ntfy.sh when needed,
and schedule next-ready units as merges complete. Stage 6 added parallel
execution, cost telemetry, and restart resilience; Stage 7 added
cross-feature scheduling; Stage 8 added the `verify_repo` gate that
refuses to spawn against repos without branch protection.

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
| `orchestrator verify-repo <url>` | Run + cache the branch-protection / approvals / no-bypass policy check for a target repo (24h TTL); required before any spawn |
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

## Worker backends

Coder / tester / reviewer agents run inside one of two backends. The choice
is made at startup via `ORCH_WORKER_BACKEND` in `.env` — no code edits
needed; the factory in
[`orchestrator/workers/__init__.py`](orchestrator/workers/__init__.py)
reads the env var at spawn time.

| Backend | Default? | Where it runs | `.env` setting |
|---|---|---|---|
| **Managed Agents** | ✅ yes | Anthropic's managed infra (gVisor sandboxes) | unset, or `ORCH_WORKER_BACKEND=managed_agents` |
| **Docker** | opt-in | A local container (`orchestrator/worker:latest`) on the host machine | `ORCH_WORKER_BACKEND=docker` |

**Default — Managed Agents.** New users who follow `orchestrator init` get
this path with no extra setup beyond an Anthropic API key. Workloads run on
Anthropic infra with `limited` outbound networking — see the
"Network allowlist (Managed Agent containers)" section below for the host
allowlist applied to those sandboxes.

**Opt-in — Docker.** Runs the same coder/tester/reviewer prompts inside a
locally-managed container image you build and own. Use this when you want
the worker on hardware you control — air-gapped environments,
internal-registry access (private PyPI / npm / container registries), or
custom toolchains the Managed Agent sandbox doesn't include. See
[`docs/PROPOSAL-docker-workers.md`](docs/PROPOSAL-docker-workers.md) for
the threat model, build instructions, and credential-boundary audit.

`orchestrator doctor` prints which backend is currently selected and, on
docker, runs four pre-flight probes (1: docker CLI on PATH · 2: daemon
reachable · 3: `claude --version` succeeds inside the worker image ·
4: `orch-net` bridge network exists) and renders the credential audit
described below.

`orchestrator init` asks **"Managed Agents or Docker workers?"** during
setup and writes `ORCH_WORKER_BACKEND` into `.env` accordingly. Picking
docker also runs a one-shot `docker version` probe; if the daemon
isn't reachable yet, the wizard prints a one-line warning and continues
— you can start Docker later and `orchestrator doctor` will re-check.

## Choosing a worker backend

The two-backend choice is the most consequential decision at install
time. Trade-offs, distilled from
[`docs/PROPOSAL-docker-workers.md`](docs/PROPOSAL-docker-workers.md):

| Axis | Managed Agents (default) | Docker workers (opt-in) |
|---|---|---|
| **Auth** | `sk-ant-` API key (`ANTHROPIC_API_KEY`) | claude.ai OAuth (your subscription, mounted from `~/.claude`); API key still usable if you set `ANTHROPIC_API_KEY` |
| **Sandbox** | gVisor by default (Anthropic-managed) | Docker `--cap-drop=ALL --security-opt=no-new-privileges --read-only --user 1000:1000`, optionally `--runtime=runsc` for gVisor parity |
| **Network** | Kernel-side allowlist (`ALLOWED_NETWORK_HOSTS`) on the Managed Agents host | dnsmasq DNS allowlist on `127.0.0.1:5353` (slightly weaker — raw-IP egress remains possible) |
| **Concurrency** | High (Anthropic's quotas) | Bounded by your claude.ai plan's concurrent-session cap (~1–2 on Pro, more on Team/Max) |
| **Cost** | `$/session-hour` + token costs against your API key | Flat — your existing claude.ai subscription |
| **Internal package registries** | No (not reachable from Managed Agents sandbox) | Yes — auto-mounts `~/.npmrc`, `~/.pip/pip.conf`, `~/.docker/config.json` read-only into the worker |
| **Cost telemetry** | Per-session billing data | None (flat subscription) |
| **Setup** | API key in `.env` | Docker daemon, image build, dnsmasq sidecar |
| **Image distribution** | Anthropic-managed | You maintain `orchestrator/worker:latest`. Override the tag with `ORCH_DOCKER_WORKER_IMAGE=<tag>` to ship custom images per environment. |

### Credential boundary + `doctor` audit (Docker)

The docker backend enforces a **strict credential boundary** wired up
in [`orchestrator/workers/docker_claude_code.py`](orchestrator/workers/docker_claude_code.py).
Two invariants the `doctor` command renders as receipts:

1. **Only whitelisted env vars cross into the container.** `GITHUB_TOKEN`
   always; `ANTHROPIC_API_KEY` only in API-key mode. Random host
   vars (`AWS_*`, `SSH_*`, `GCP_*`, `KUBE*`, `DOCKER_*`, …) are
   dropped on the floor — the subprocess env handed to `docker run` is
   a curated dict, never `os.environ`.
2. **Only safe host paths get mounted.** `~/.ssh`, `~/.aws`, `~/.gitconfig`,
   `~/.git-credentials`, `~/.config/gcloud`, `~/.kube` are in the
   `NEVER_MOUNTED_HOST_PATHS` list and refused even if a user names
   one via `ORCH_WORKER_EXTRA_MOUNTS`. Workspace, claude.ai sessions
   dir (rw), and registry credentials (ro) are the only mounts.

`orchestrator doctor` with `ORCH_WORKER_BACKEND=docker` prints the full
audit — every env var passed/dropped, every mount, every NEVER path the
host has but the worker won't see — so you can verify what crossed the
boundary on each spawn.

### OAuth lifecycle (Docker, claude.ai mode)

When `ANTHROPIC_API_KEY` is unset, the docker worker mounts your
`~/.claude` directory **read-only** so Claude Code inside the container
finds the OAuth token issued for your claude.ai subscription. Two
properties to know:

- **Mid-job refresh.** Claude Code refreshes the OAuth token in-place;
  the writable `~/.claude/sessions` sub-mount (bound rw separately
  from the ro creds mount) is where session state lands across
  `claude --resume` invocations. A worker that's been running for
  hours stays authenticated.
- **No fallback identity.** If the user has not logged into claude.ai
  on the host, there is no claude.ai token to mount; the worker won't
  fall back to the API key automatically. Set `ANTHROPIC_API_KEY` to
  switch modes explicitly (auto-selected by
  `select_auth_mode(host_env)`), or `claude login` on the host first.

The OAuth token is a real bearer credential. A worker that's rogue
*within its sandbox* can read it (the same threat surface as you
running `claude` locally yourself). The mitigations are read-only
mounting + capability drop + the DNS allowlist below.

### DNS allowlist (Docker)

Worker containers launch with `--dns=127.0.0.1 --dns-search=.`,
pinning name resolution to a local dnsmasq sidecar on
`127.0.0.1:5353` started by
[`scripts/run-worker-dns.sh`](scripts/run-worker-dns.sh). The
config in
[`orchestrator/network/allowlist.dnsmasq.conf`](orchestrator/network/allowlist.dnsmasq.conf)
forwards `github.com`, `api.github.com`, `pypi.org`,
`registry.npmjs.org`, `api.anthropic.com`, etc. to Cloudflare DNS
(1.1.1.1) and resolves anything else to `0.0.0.0` (non-routable —
connect fails).

`ORCH_INTERNAL_REGISTRY_HOSTS=a.example,b.example` adds custom hosts
to the allowlist for internal artifactory / private PyPI deployments;
the `doctor` audit lists every host added this way under "Internal
registry hosts (added to DNS allowlist)".

**Soft boundary, not a guarantee.** This is name-level filtering —
a compromised worker that hardcodes an IP can still reach it. See
[SECURITY.md](SECURITY.md#non-defenses--what-the-orchestrator-does-not-defend-against-known-limitations)
for the full non-defense list and
[`docs/PROPOSAL-docker-workers.md`](docs/PROPOSAL-docker-workers.md)
for the threat-model rationale.

### Running the E2E suite locally

The end-to-end smoke suite added in F-001-U-6 stitches the Docker
worker against a real Docker daemon — build worker image, launch
dnsmasq, spawn a worker against
[`tests/fixtures/sandbox-repo/`](tests/fixtures/sandbox-repo/), and
exercise the auth/cred/network invariants the unit tests pin in
isolation.

```bash
# Requirements on host: Docker daemon running, ~/.claude with a real
# claude.ai session for the OAuth-path tests.
ORCH_RUN_E2E=1 pytest tests/e2e -q                                    # most coverage, skips claude itself
ORCH_RUN_E2E=1 ORCH_E2E_CLAUDE_AUTH=1 pytest tests/e2e -q             # also runs the spawn/resume round-trip
```

**Two gates:**

- **Suite-level: `ORCH_RUN_E2E=1`.** The `docker_available` autouse
  fixture in [`tests/e2e/conftest.py`](tests/e2e/conftest.py) skips the
  module unless this env var is set. The main CI matrix doesn't set it
  (E2E is opt-in to keep iteration fast); the dedicated
  `e2e-docker.yml` workflow sets it and runs the suite on every PR.
- **Test-level: `ORCH_E2E_CLAUDE_AUTH=1`.** Individual tests needing a
  real claude.ai login (notably the spawn/resume round-trip) gate on
  this so contributors without a session see a clear skip reason
  instead of a confusing auth failure.

If Docker isn't reachable, the autouse fixture skips cleanly even with
`ORCH_RUN_E2E=1` set, so failed-daemon doesn't fail the suite.

## Network allowlist (Managed Agent containers)

Coder/tester/reviewer containers run with **`limited` outbound networking**
— they can only reach the hosts in `ALLOWED_NETWORK_HOSTS`
(defined in `orchestrator/workers/managed_agent.py`; re-exported via the
`orchestrator/agents.py` back-compat shim), plus public package
registries (PyPI, npm, etc.) via the `allow_package_managers: True` flag.

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
edit `ALLOWED_NETWORK_HOSTS` in `orchestrator/workers/managed_agent.py`.
This change does **not** auto-invalidate the cache: the resource signature
does not include env/network config, so existing cached environments keep
the old allowlist until you reset cached resources (for example,
`./scripts/reset_cache.sh` or `reset_cached_resources`) and spawn again.

**If an agent reports a network failure** (curl/git/pip hanging or 403),
check whether the URL is in the allowlist. The agent will silently fail
on blocked hosts — there's no friendly error message.

## Cache management

The orchestrator caches (agent_id, environment_id) per role to avoid
re-creating them on every spawn. Cache key = sha256 of `role + prompt + model`.

**Three ways the cache invalidates:**

1. **Prompt edit** — editing `coder.md` / `tester.md` / `reviewer.md` changes
   the signature → next spawn creates a fresh agent with the new prompt.
2. **Model change** — bumping `DEFAULT_MODEL` in
   `orchestrator/workers/managed_agent.py` changes the signature → next
   spawn uses the new model.
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

### Optional ultrareview gate (F-007)

Load-bearing features can opt in to an extra pre-merge pass with
`load_feature(..., ultrareview_enabled=True)`. After the reviewer agent
emits `REVIEW_RECOMMEND_MERGE`, `cycle_review` fires Anthropic's
`/ultrareview` (a multi-agent cloud bug-hunter) as a final audit. The
flag is **per-feature opt-in** because each run has measurable token
cost — not worth it on every change, valuable on the risky ones.

- **Ultrareview PASS** → `approved_awaiting_merge` (today's behaviour).
- **Ultrareview FAIL** → the structured findings post as a PR comment so
  the human sees the meta-audit, then the coder addresses them via
  `address_review(source='ultrareview', ...)` ("reviewer already endorsed,
  fix the listed findings without scope creep"). After CI green, the gate
  re-runs ultrareview (not the reviewer — endorsement already happened).
- The loop shares the same **cap-3 budget** as tester-bug and reviewer-
  change fixes. Cap hit → escalation with the full ultrareview history.

The gate **fails closed** on wrapper errors / unparseable verdict output:
if we can't read what ultrareview emitted, the PR does not merge.

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

The `Worker` protocol in `orchestrator/workers/base.py` is a portability
seam — today's impls are `ManagedAgentWorker`
(`orchestrator/workers/managed_agent.py`) and `DockerClaudeCodeWorker`
(`orchestrator/workers/docker_claude_code.py`), selected at spawn time
by `make_worker(role)` (`orchestrator/workers/__init__.py`) keyed by
`ORCH_WORKER_BACKEND`. Future impls (Modal, E2B, non-Claude models) plug
in without touching the lead or state machine. The historical
`orchestrator/agents.py` module is a back-compat shim that re-exports
these symbols.
