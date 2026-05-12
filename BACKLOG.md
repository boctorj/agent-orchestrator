# Backlog

Future work, categorized. Items here are **deferred ideas**, not
commitments. Promote to actual issues / sprints when there's a reason
to ship them. Effort estimates are rough order-of-magnitude.

---

## Task source integrations

### Jira: pull tasks (feature source) 🆕
Read JQL queries (e.g., "assignee = me AND status = 'To Do'"); convert
matching issues into orchestrator features. Each issue → one
`load_feature` call:
- Issue summary → feature title
- Issue description → feature description
- Acceptance criteria field → hints for unit breakdown
- Linked repo (from custom field or convention) → repo_path

CLI: `orchestrator jira pull` runs the import. Or a daemon mode for
continuous polling.

**Effort:** ~2 days (Atlassian SDK auth, polling, conversion logic).
**Blocked by:** nothing functional, but multi-user makes more sense
after persistence abstraction.

### Jira: push progress (status sync) 🆕
As units transition `coding → testing → in_ci → done`, sync to the
linked Jira issue: move status, add PR URL as comment, add "completed
by orchestrator" label.
**Effort:** ~1 day after Jira auth is wired.

### Linear / GitHub Projects / Notion / Asana
Same pattern as Jira, different APIs.
**Effort:** ~1-2 days each.

---

## Identity & auth

### ~~GitHub App identity for agents (vs PAT)~~ — SHIPPED
Agents now authenticate via GitHub App installation tokens
(`orchestrator/github_app.py`). Commits + PRs attributed to
`<your-app>[bot]`, 1-hour-lived tokens, scoped per-installation.
PAT fallback remains for single-user sandbox setups.
**Setup**: `orchestrator init` walks through it; pick `a` for App.

### Two-identity model for true self-approval
Even with the GitHub App, the reviewer agent still can't `--approve`
a PR opened by the coder if both use the same App identity. The fix
is to use the App for coder/tester and a separate identity (a second
App, or the user's PAT) for the reviewer.
**Effort:** ~1-2 hrs. Worth doing when you graduate from
RECOMMEND_MERGE to wanting GitHub's formal approval signal.

### Per-user API keys (multi-tenant prep)
Per-user vault, OAuth flows, encrypted storage.
**Effort:** ~3-5 days. **Blocked by:** Postgres backend.

### Multi-installation App support (cross-org target repos)
Today the orchestrator assumes a single `GITHUB_APP_INSTALLATION_ID` in
`.env`, which means an App can only operate on target repos belonging
to ONE org/account. Working across multiple orgs requires falling back
to PAT auth.

The fix: a `repo_auth_profiles` table keyed by `repo_url` with
per-installation `{app_id, installation_id, base_url}` records. The
verification cache (`verified_repos`) already stores
`auth_identity = "app:installation:<id>"` so this groundwork is in
place. `github_app.mint_installation_token()` would gain an
`installation_id` parameter and pick the right one based on the repo.

**Effort:** ~1 day. Defer until you have a real cross-org workflow
asking for it.

### GitHub Enterprise support
`agents.py` `ALLOWED_NETWORK_HOSTS` hardcodes github.com /
api.github.com / codeload / objects / raw.githubusercontent.com.
`repo_verify.normalize_repo_url` also rejects non-github.com hosts.

Parameterize via env (`GITHUB_API_BASE`, `GITHUB_HOSTS`) so Enterprise
deployments can point the orchestrator at their own GHE instance. The
verification flow + branch-protection checks all use the documented
GHE API and should work as-is once the host is configurable.

**Effort:** ~half-day. **Blocked by:** none, just demand.

---

## Workflow extensions

### Auto-poll merge status
Kill the manual "I merged X" step. Background poller hits GitHub.
**Effort:** ~2-3 hrs.

### True async cycle_review
Don't block the lead during cycle_review's 5-20 min runtime.
**Effort:** ~6-8 hrs.

### Multi-repo features
Feature spans backend + frontend + infra repos.
**Effort:** ~1 day.

### Rollback / revert workflow
If a merged unit causes regressions, spawn a revert unit automatically.
**Effort:** ~1 day.

### Pre-spawn validation (spec sanity)
A "spec critic" agent checks unit descriptions for ambiguity before spawn.
**Effort:** ~half day.

### Post-merge smoke test
After merge, run smoke test on main; if broken, spawn a fix unit.
**Effort:** ~half day.

### Branch garbage collection
Delete merged-unit branches after N days (orchestrator-side, not agent-side).
**Effort:** ~1 hr.

---

## Quality gates

### Required test coverage per unit
Reviewer blocks merge if coverage delta below threshold.
**Effort:** ~2-3 hrs.

### Required CI green before reviewer
Wait for CI completion (not just trigger) before spawning reviewer; CI
failures route back to coder via `address_review`.
**Effort:** ~2 hrs.

### Security scan integration
Pipe Trivy / Snyk / GitHub Code Scanning into reviewer context.
**Effort:** ~1-2 days.

---

## Observability & cost

### Real token cost tracking
`feature_cost` is session-hours only today. Add token counts.
**Effort:** ~1 day.

### Daily digest
Cron-scheduled activity summary via ntfy or email.
**Effort:** ~half day.

### Cost / time budgets with hard caps
Per-feature dollar + wall-clock limits; orchestrator halts on cap.
**Effort:** ~2-3 hrs.

### Structured logging (compliance-ready)
JSON logs, exportable, SOC 2-friendly.
**Effort:** ~half day.

---

## Persistence & deployment

### Postgres backend
StorageBackend Protocol + Postgres impl + migrations.
**Effort:** ~1.5 days. **Required for:** anything multi-user.

### Self-hostable Docker image
MCP server + Postgres + dashboard in one container.
**Effort:** ~1-2 weeks.

### Multi-tenant SaaS
Auth, isolation, billing, ops.
**Effort:** ~4-8 weeks.

### Hybrid: local MCP + cloud dashboard
Lead stays local, dashboard becomes hosted.
**Effort:** ~1 week.

---

## LLM backend abstraction

### Self-hosted Worker backend (enterprise enabler)
Implement the `Worker` protocol (see `docs/ARCHITECTURE.md` §11) against
a self-hosted execution environment instead of Anthropic's Managed
Agents.

**Why this matters**

The current `ManagedAgentWorker` spawns containers on Anthropic's gVisor
infrastructure. Those containers:
- Cannot reach hosts behind a corporate VPN
- Cannot resolve internal DNS (`artifactory.example.com`, etc.)
- Have no credentials for internal package registries
- Are constrained to `ALLOWED_NETWORK_HOSTS` + public package managers

For any target repo that depends on **internal artifacts** (private
PyPI, internal NuGet feeds, Maven internal repos, internal Docker
images, internal-only git submodules), the agent's `pip install` /
`npm install` / etc. fails with hung TCP connects or NXDOMAIN. The
coder can't install deps; the tester can't run the suite; the reviewer
can't run linters that depend on internal types.

This is the largest blocker to running the orchestrator against real
enterprise repos at scale. Repos with entirely public deps work today
— the blocker is exactly for the "production codebase with internal
artifacts" case.

**What it looks like**

A new `SelfHostedWorker` (or org-named variant) implementing the same
`Worker` protocol as `ManagedAgentWorker`, but executing the agent on:
- A K8s pod / ECS task / Modal function running INSIDE the
  enterprise's own network
- With its own gVisor-equivalent sandbox isolation
- With access to the corporate package mirror, internal DNS, and a
  secret manager for registry credentials
- Reachable from the user's laptop via mTLS or a signed JWT

The `Worker` protocol is the seam. The orchestrator's state machine,
MCP tools, prompts, observability, and cost tracking don't change.
Only the spawn backend differs.

**Estimated scope**

A full quarter of platform-engineering work for a competent engineer:
- Auth (how does the laptop prove identity to the enterprise runtime?)
- Secret management (registry creds, GitHub token injection)
- Scheduling (concurrency limits, retries, queueing)
- Observability (streaming agent stdout back to the lead)
- Cost tracking (replaces session-hour estimates with whatever the
  self-hosted runtime bills)
- Sandbox isolation (gVisor / firecracker / equivalent — don't lose
  what Anthropic's sandbox provides)
- Lifecycle (cold start, warm pool, eviction)

Not weekend hacking.

**Why deferred**

- Most users (single-developer sandboxes, open-source maintainers,
  small teams) don't need it. Their target repos use public deps.
- Adds substantial infra dependency (the orchestrator currently runs
  from a laptop with no other services).
- Best done AFTER:
  - The open-source surface stabilizes (no more API churn)
  - A real enterprise pilot identifies which workflows the self-hosted
    worker must support (auth model, secret store, runtime choice)
  - The CODEOWNERS-as-gate flow is battle-tested (so we know exactly
    what the human-review touchpoints are inside the worker)

**Workaround until this lands**

Pilot the orchestrator on target repos with entirely public
dependencies. Examples that often work even at large enterprises:
- Public open-source repos owned by the org (community SDKs, docs)
- Internal repos whose deps are vendored or all public-PyPI/npm
- Build configuration repos, infra-as-code repos with public providers
- Documentation-only repos

When you hit a repo with internal deps, that's the signal to either
vendor the deps, swap the repo for the pilot, or revisit this entry.

### Bedrock / Vertex AI Worker (managed Claude on AWS/GCP)
Same Claude, different infra. Cheaper at volume; compliance-friendly.
**Effort:** ~1 day each (most code reuses).

### LocalWorker (self-driven loop on user infra)
No Managed Agents dependency.
**Effort:** ~2 days.

The naïve "Claude Code subprocess on the host" version. Better isolated
variant — Docker containers + claude.ai OAuth + DNS allowlist — is spec'd
in [`docs/PROPOSAL-docker-workers.md`](docs/PROPOSAL-docker-workers.md).
That proposal supersedes this entry once anyone picks it up; ~5 days
total for a polished v1 including the network allowlist + internal-
registry support.

### OpenAI Assistants Worker
Different model family; real behavior-parity work.
**Effort:** ~3 days.

---

## Operational polish

### Publish to PyPI
`pip install agent-orchestrator` without git clone.
**Effort:** ~half day.

### Homebrew formula
For Mac users who don't want to manage Python deps.
**Effort:** ~half day.

### Per-project CLAUDE.md template generator
Drop a starter CLAUDE.md tuned for common stacks (Python/Django,
TypeScript/Next, Rust/Cargo, etc.).
**Effort:** ~1 day.

### Telemetry (opt-in)
Anonymous usage stats. Needs clear privacy story.
**Effort:** ~1 day.

---

## Custom workflows

### Skill: triage incoming bugs
Lead reads an issue tracker, decides: investigate / fix / punt.
**Effort:** ~half day on top of Jira integration.

### Skill: release engineer
Bump version, changelog, tag, push.
**Effort:** ~half day.

### Skill: refactor planner
Plan a refactor DAG considering risk; behavior tests before structural changes.
**Effort:** ~1 day plus prompt tuning.

---

## Long shots / research

- Multi-lead session (two leads, one project, lock-coordinated)
- Learned reviewer (fine-tune the rubric from past comment outcomes)
- Auto-formation of teams (lead picks roles based on feature shape)
- Cross-repo code search (vector embeddings, "has this bug been fixed before?")

---

## When to revisit

Low-fidelity by design. When you start one of these:
1. Promote to a proper plan / GitHub issue
2. Update effort estimate after real scoping
3. Remove from this file once shipped

If a category gets too long, split it into its own file.
