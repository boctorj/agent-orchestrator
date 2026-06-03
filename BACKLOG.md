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

### Cross-feature dependencies 🆕
F-008 depends on F-007 landing is a planning concern today. Could
become a `state.db features.depends_on` column so the daemon respects
ordering across features (don't start F-008 units before F-007's
PRs are merged). Mentioned in PR #19's open-questions section.
**Effort:** ~3-4 hrs (schema migration + scheduler update + tests).

### Cross-feature memory / preferences 🆕
A user-level `~/.orchestrator/preferences.md` (your preferred libraries,
test conventions, team norms, code-style notes) auto-injected into
every new spec.md's Constraints section at `load_feature` time. Stops
you from re-typing the same context across 20 features.
**Effort:** ~3-4 hrs. **Depends on:** spec.md infrastructure (PR #19
Phase 1).

### Plan revision tracking 🆕
Re-planning a feature today wipes the old plan. Could track plan
versions so escalations that trigger a re-plan don't lose the prior
decomposition. Useful for postmortems and learning from the planning
mistakes that led to the re-plan.
**Effort:** ~half day (schema migration + UI updates).

### Replay / what-if 🆕
Re-run a failed unit with different prompts / strategy without losing
the existing history. Useful for debugging meta-prompt changes —
"would this prompt edit have prevented yesterday's escalation?" —
without polluting state.db.
**Effort:** ~1 day. Medium complexity; needs careful state-machine
work so the original cycle history stays intact.

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

### Ultrareview as a terminal gate 🆕
After our reviewer emits `REVIEW_RECOMMEND_MERGE`, fire `/ultrareview`
as an optional final pass; only emit ready-to-merge if it passes.
Catches final-mile issues our reviewer + Copilot miss; costs measurably
per cycle, so likely opt-in via a feature-level flag.
**Effort:** ~half day. **Depends on:** PR #19 Phase 1 (so ultrareview
has spec.md as intent to compare against).

### Reviewer / tester learning from past escalations 🆕
If a class of bug recurs across units (race condition, validation gap,
prop drift), future units in similar territory get an automated hint
in their task message. Cross-unit memory beyond the spec — possibly a
`patterns.md` per repo that accumulates "things we have learned to
watch for here."
**Effort:** ~1 day. **Depends on:** cycle logs accumulated (PR #19 Phase 1).

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

### Worker observability — `tail_worker(unit_id, role)` 🆕
New MCP tool that streams a worker's recent agent.message output
mid-cycle. Today you can't see what a hung worker is doing until it
times out at 30 min. Particularly load-bearing once the headless
daemon ships (Phase 3 of PR #19) since synchronous chat visibility
goes away.
**Effort:** ~3-4 hrs. **Required for:** debugging the daemon era.

### Cost guardrails — per-feature budget caps + alerts 🆕
Two-layer policy on top of the existing `feature_cost` / `unit_cost`
telemetry: (a) per-feature daily $ cap that pauses spawns when hit;
(b) ntfy alert when feature spend crosses a threshold (e.g., 50% of
cap); (c) daemon-level kill-switch on global spend rate. Today nothing
prevents an autonomous daemon from burning unattended over a weekend.
**Effort:** ~1 day. **Required for:** safely running PR #19 Phase 3
(daemon) at scale.

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

### DockerAiderWorker (multi-model aider inside the Docker worker)
Implement the `Worker` protocol against `aider` instead of `claude` —
multi-model support out of the box (Claude, GPT, Gemini, Llama). The
Docker worker abstraction shipped in F-001-U-1 is the seam: a new
`orchestrator/workers/docker_aider.py` module mirrors the shape of
`docker_claude_code.py`, plus an aider invocation in
`docker/aider.Dockerfile` (or extend `worker.Dockerfile` behind a build
arg) and a factory branch in `orchestrator/workers/__init__.py` keyed
on `ORCH_WORKER_BACKEND=docker_aider`.

Reuses verbatim: the cred-boundary plumbing (whitelist env, never-mount
list, registry passthrough), the DNS allowlist (`network/allowlist.
dnsmasq.conf`), the `doctor` audit shape, the timeout handling.

New work: aider CLI argv builder (different flag set), output parsing
for aider's session/result formats, prompt re-tuning (the agent prompts
in `orchestrator/prompts/` reference Claude's terminal markers — aider
needs a parallel set or a model-agnostic emit convention), provider-key
forwarding (`OPENAI_API_KEY`, `GEMINI_API_KEY`, etc. added to the env
whitelist only when the user explicitly opts in via
`ORCH_AIDER_PROVIDERS=openai,gemini`).

**Effort:** ~150 LOC + tests on top of F-001-U-1's abstraction. Add the
provider-key forwarding to the cred-audit so the doctor command shows
exactly which keys cross the boundary, mirroring the
`ANTHROPIC_API_KEY` flag already there.

### DockerOpenAICodexWorker (GPT-5 / Codex via OpenAI Assistants)
Implement the `Worker` protocol against the OpenAI Assistants API. Same
container-isolation story as `DockerClaudeCodeWorker` — read-only
rootfs, `--cap-drop=ALL`, dnsmasq allowlist — but the in-container
binary is the openai CLI (or a thin Python wrapper around the
Assistants SDK) rather than `claude`. Factory keys on
`ORCH_WORKER_BACKEND=docker_openai_codex`.

Reuses: container hardening, mounts, network allowlist, timeouts,
cred-audit receipts.

New work: Assistants thread/run lifecycle (different shape than
`claude --resume <uuid>` — `thread_id` is the equivalent), tool-call
forwarding (Assistants doesn't bundle a Bash tool by default; either
mount `bash` as a function tool or use the Code Interpreter feature),
prompt re-tuning for GPT-5's calibration. `OPENAI_API_KEY` gets a
container-side `--env` entry in API-key mode (no OAuth equivalent on
the OpenAI side today).

**Effort:** ~150 LOC + tests on top of F-001-U-1's abstraction. The
prompt re-tuning is the largest single unknown — Claude's terminal
markers (`PR_URL:`, `TESTS_PASS`) need to survive a model swap.

### BedrockClaudeWorker (managed Claude on AWS Bedrock)
Same Claude as `ManagedAgentWorker`, hosted in AWS Bedrock instead of
Anthropic's Managed Agents. Compliance-friendly for shops that need
their Claude calls inside an AWS account; cheaper at volume on
committed-throughput contracts. Factory keys on
`ORCH_WORKER_BACKEND=bedrock`.

Reuses: the Worker protocol surface, the agent prompts (verbatim —
Bedrock serves the same Claude model family), the `_resource_signature`
caching scheme (Bedrock model IDs slot into the signature).

New work: AWS auth (IAM role / `AWS_PROFILE` / SSO via boto3), region
selection (`AWS_REGION` env), Bedrock's request/response shape
(different from `client.beta.agents` — uses `bedrock-runtime.InvokeModel`
or the Converse API), session continuity (Bedrock has no first-class
session object today — fall back to "Path 2" from the PROPOSAL doc:
orchestrator-managed transcript prepended on each invocation). Bedrock
doesn't give us a gVisor sandbox — the orchestrator-side guarantees
shift to "AWS account boundary" instead.

**Effort:** ~150 LOC + tests on top of F-001-U-1's abstraction. The
session-continuity fallback is the biggest implementation departure
from the Claude Code worker; estimate could grow if Bedrock ships
first-class sessions in the meantime.

### LocalWorker (self-driven loop on user infra)
No Managed Agents dependency.
**Effort:** ~2 days.

The naïve "Claude Code subprocess on the host" version. Better isolated
variant — Docker containers + claude.ai OAuth + DNS allowlist — shipped
in F-001 (see
[`docs/PROPOSAL-docker-workers.md`](docs/PROPOSAL-docker-workers.md) for
the proposal that drove the design, and `orchestrator/workers/docker_claude_code.py`
for the implementation). This entry stays as the unisolated-subprocess
variant for hosts where Docker isn't available; revisit only if a real
demand surfaces.

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

## Superseded / renumbered specs

Three speculative feature specs were committed together in `f8f9015`
("Add future features (#39)") under numbers F-014/F-015/F-016. Those
numbers were later repurposed in state.db for a different trilogy
(unit-health-probe / promote-shadow-decisions / non-blocking-daemon),
so the original specs were displaced. The ideas are kept here; full
text is recoverable at git ref `f8f9015`.

### Role / prompt decomposition (was F-014) 🆕
Split monolithic `coder.md` (~347 lines) into focused role prompts —
`coder-open.md`, `coder-fix.md`, `coder-fix-ci.md`,
`coder-selfreview.md` — and `reviewer.md` into `reviewer-stance.md` +
`reviewer-method.md`, composed via `{{include}}`. Goal: resumed agents
stop carrying opener context (token + attention savings); each prompt
has exactly one path. Depended on the F-013 composer.
**Recover:** `git show f8f9015:features/F-014/spec.md`.

### Structured tool-call markers (was F-015) 🆕
Replace terminal marker *strings* (`PR_URL:`, `TESTS_PASS`,
`REVIEW_RECOMMEND_MERGE`, …) with structured tool calls
(`emit_pr_url`, `emit_tests_pass`, …) so marker counts are exact rather
than regex-matched anywhere in a long response. Prerequisite for precise
marker-protocol validation.
**Recover:** `git show f8f9015:features/F-015/spec.md`.

### Defense-in-depth: server-side enforcement (was F-016) 🆕
Mechanical backstops for the prose hard rules:
- **Marker-protocol validation** — exactly-one terminal marker, correct
  for the role; structured `marker_protocol_violation` BLOCKED slug.
- **Worker-side pre-push hook** — abort pushes touching
  `.github/workflows/*` unless `ORCH_ALLOW_WORKFLOW_CHANGES=1`, and abort
  tester pushes outside `tests/`. Converts prose rules into a git-hook
  gate. **Still worth doing independently of the daemon work.**
- **Prompt version headers + CI drift gate** — `version: N` frontmatter
  per role prompt; CI fails if a prompt's composed output changes without
  a version bump.
**Recover:** `git show f8f9015:features/F-016/spec.md`.

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
