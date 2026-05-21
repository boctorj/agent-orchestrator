# Architecture

This document describes how `agent-orchestrator` is built, why it's built that
way, and where to extend it. It is the canonical engineering reference for the
project; everything else (README, SECURITY, BACKLOG) is downstream of it.

---

## Contents

- [1. What this is, in one paragraph](#1-what-this-is-in-one-paragraph)
- [2. Design goals + non-goals](#2-design-goals--non-goals)
- [3. High-level architecture](#3-high-level-architecture)
- [4. Process model + lifetimes](#4-process-model--lifetimes)
- [5. Component map](#5-component-map)
- [6. Data model](#6-data-model)
- [7. Control flows](#7-control-flows)
- [8. External integrations](#8-external-integrations)
- [9. Security model](#9-security-model)
- [10. Quality engineering](#10-quality-engineering)
- [11. Extension points](#11-extension-points)
- [12. Key decisions + rejected alternatives](#12-key-decisions--rejected-alternatives)
- [13. Glossary](#13-glossary)

---

## 1. What this is, in one paragraph

A single-developer tool that automates the full SDLC for a software feature
end-to-end: you chat with a **project lead agent** (a Claude Code session) on
your laptop or phone; it breaks the feature into a dependency DAG of work
units; for each unit it spawns isolated **coder / tester / reviewer agents**
on Anthropic's Managed Agents infrastructure; those agents clone your repo,
implement the change, write tests, open a PR, address tester bugs, then a
reviewer agent posts a review; you push the merge button. The orchestrator
sits between you and the worker agents, holds state, enforces guardrails,
and surfaces what's happening through a TUI dashboard and chat-based status.

---

## 2. Design goals + non-goals

### Goals

| Goal | Why |
|---|---|
| **Defense-in-depth** | Agents have real powers (filesystem, git, gh); multiple layers must prevent a rogue or buggy agent from doing irreversible damage |
| **Mobile-first interaction** | Chat with the lead from phone via Claude Code Remote Control; push notifications for "ready to merge" / escalations |
| **Sessions persist across iterations** | Coder/tester/reviewer keep memory across review-fix cycles; only the system thread can resume them |
| **Cheap to run** | Single-developer use; runs on your laptop; ~pennies per feature |
| **Easy for one more person to try** | `pip install -e .` + `orchestrator init` → working in ~5 minutes |
| **Eat our own dogfood** | The orchestrator itself uses ruff + mypy + bandit + pytest + pre-commit + CI on every commit |

### Non-goals (v1)

| Non-goal | Reason |
|---|---|
| Multi-tenant SaaS | Single-user is enough; multi-tenant needs Postgres + auth + ops (BACKLOG) |
| Truly async agent execution | MCP tools are synchronous; lead blocks for minutes during `cycle_review`. Acceptable when chatting from a phone |
| Non-Claude model backends | `Worker` protocol exists so backends can be swapped; concrete impls deferred until pain demands |
| Repo conflict resolution across features | Two features touching the same file is an inherent merge problem, not solvable by the orchestrator |
| Automatic merging | Always a human merge button. By design — never built a `merge_pr` MCP tool |

---

## 3. High-level architecture

Three planes — chat, orchestration, execution — separated by clear protocol
boundaries.

```
┌────────────────────────────────────────────────────────────────────────┐
│                          USER (you)                                    │
│  - laptop terminal (Claude Code TUI)                                   │
│  - mobile (Claude app via Remote Control)                              │
└────────────────────────────────────────────────────────────────────────┘
                            │  chat / Remote Control
                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  LEAD (Claude Code session)                            │
│                                                                        │
│  - System prompt: CLAUDE.md at project root                            │
│  - Has access to MCP tools registered in .mcp.json                     │
│  - Holds conversation, plans features, decides which tool to call      │
└────────────────────────────────────────────────────────────────────────┘
                            │  MCP (stdio JSON-RPC)
                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│            ORCHESTRATOR MCP SERVER (Python process)                    │
│                                                                        │
│   Entry: orchestrator/mcp_launcher.py → orchestrator/mcp_server.py     │
│                                                                        │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │  tools/__init__.py  — FastMCP instance + shared helpers      │    │
│   │  tools/planning.py  — load/save/approve features + plans     │    │
│   │  tools/execution.py — spawn_unit, spawn_tester, ...,         │    │
│   │                       cycle_review (3-phase state machine)   │    │
│   │  tools/scheduling.py — DAG: next_ready_units(_all),          │    │
│   │                        parallel_units(_global)               │    │
│   │  tools/observability.py — get_unit_status, unit_history,     │    │
│   │                            unit_cost, show_dashboard         │    │
│   │  tools/ops.py — check_unit_pr, reconcile_unit_pr,            │    │
│   │                 list_in_flight, resume_unit, hello_world_test,│    │
│   │                 reset_cached_resources, verify_repo,         │    │
│   │                 list_verified_repos, forget_repo             │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                                                                        │
│   state.py    — SQLite (state.db)                                      │
│   github.py   — REST helpers (PR comments, check polling, ...)         │
│   github_app.py — App JWT + installation token minting                 │
│   ntfy.py     — phone push notifications                               │
│   costs.py    — session-hour cost estimates from event timestamps      │
│   dashboard.py — rich (TUI) + markdown (in-chat) renderers             │
│   workers/   — Worker protocol (base.py) + ManagedAgentWorker          │
│                (managed_agent.py) + DockerClaudeCodeWorker             │
│                (docker_claude_code.py) — F-001                         │
└────────────────────────────────────────────────────────────────────────┘
       │ Anthropic SDK            │ GitHub REST          │ ntfy.sh
       │ (mints sessions)         │ (PRs, checks,        │ (POST topic)
       │                          │  reviews, comments)  │
       ▼                          ▼                      ▼
┌──────────────────┐    ┌────────────────────┐   ┌────────────────────┐
│ Anthropic        │    │ GitHub             │   │ ntfy.sh            │
│ Managed Agents   │    │                    │   │                    │
│                  │    │ - Sandbox repo /   │   │ - Push to phone    │
│ gVisor sandboxes │    │   target repo      │   │   via ntfy app     │
│ for coder,       │    │ - Copilot Reviewer │   │ - "ready to merge" │
│ tester, reviewer │    │ - Branch           │   │ - "escalated"      │
│ (1 per role per  │    │   protection       │   │                    │
│ unit per cycle)  │    │ - CI checks        │   │                    │
└──────────────────┘    └────────────────────┘   └────────────────────┘
```

### Why these three planes?

1. **Lead (chat plane)** is where humans want to be. It runs in Claude Code,
   which gives us Remote Control for free (mobile chat to a local session).
   It does not directly access state or call APIs — it calls MCP tools.

2. **Orchestrator MCP server (control plane)** is the only place that holds
   state, decides next actions, mints tokens, and talks to external APIs.
   It is pure Python, fully testable, no LLM in the hot path.

3. **Managed Agents (execution plane)** is where work actually happens. Each
   agent runs in a gVisor sandbox provisioned by Anthropic. The orchestrator
   never executes shell commands itself; it tells agents what to do via
   structured task messages.

This separation means:
- You can test the orchestrator (the most complex layer) without spending a
  cent on Anthropic compute. 289 tests pass in 5 seconds against fake workers.
- A compromised worker can't escalate to the orchestrator process; the only
  channel back is the agent's text response, which is parsed for known markers.
- The lead's tool surface is finite (23 MCP tools, all human-readable JSON
  contracts). It can't "improvise" a new operation.

---

## 4. Process model + lifetimes

```
TIME →
─────────────────────────────────────────────────────────────────────────
laptop terminal #1:  claude --remote-control  ────────────────────►  (you /quit)
                          │
                          ├─ spawns MCP subprocess via .mcp.json
                          ▼
MCP server process:  python -m orchestrator.mcp_launcher  ─────────►  (dies with claude)
                          │
                          │ on tool call, may create:
                          ▼
Per-spawn threads:   spawn_unit / cycle_review / parallel_units
                     (ThreadPoolExecutor for parallel_*)

Anthropic side:      Managed Agent sessions (1h container TTL)
                     - one per (unit, role, retry-cycle)
                     - lives independently of orchestrator
                     - resumable via session_id even after restart

laptop terminal #2:  orchestrator dashboard  ─────────────────────►  (Ctrl+C)
                     reads state.db read-only every 2s

GitHub side:         PR branches + open PRs persist independently
                     of everything else
```

**Crash recovery model**: state.db is the source of truth. If `claude` dies
mid-cycle, the Anthropic-side session keeps running. On relaunch, the lead
calls `list_in_flight()` to find units with non-terminal status, then
`resume_unit(unit_id, role)` to check each session. Recovery is manual but
the data is preserved.

---

## 5. Component map

```
orchestrator/
├── __init__.py                  empty
├── mcp_launcher.py              env-minimization wrapper (strips secrets
│                                from parent shell before exec'ing server)
├── mcp_server.py                imports all tools/, runs FastMCP stdio
├── agents.py                    Backwards-compat shim re-exporting from
│                                workers/ (the real code lives there since
│                                F-001-U-1 split the Worker abstraction)
├── workers/                     Worker abstraction subpackage
│   ├── __init__.py              make_worker(role) factory keyed by
│   │                            ORCH_WORKER_BACKEND (managed_agents | docker)
│   ├── base.py                  Worker protocol (spawn/resume/archive)
│   ├── managed_agent.py         ManagedAgentWorker (Anthropic Managed Agents)
│   └── docker_claude_code.py    DockerClaudeCodeWorker — F-001:
│                                hybrid OAuth/API-key auth, hardened
│                                docker run, cred audit, doctor probes,
│                                internal-registry passthrough
├── network/                     DNS allowlist for Docker workers (F-001-U-3)
│   ├── __init__.py              allowlist_config_path(), package-manager hosts
│   └── allowlist.dnsmasq.conf   dnsmasq config served by run-worker-dns.sh
├── state.py                     SQLite layer: features, plans, work_units,
│                                unit_events, cached_resources
├── models.py                    Feature, Plan, WorkUnit, WorkUnitState
│                                + UnitStatus Literal + ACTIVE_/
│                                READY_TO_MERGE_/TERMINAL_UNIT_STATUSES
│                                frozensets
├── github.py                    REST helpers (PR comments / reviews /
│                                check_runs / Copilot review fetch+wait)
├── github_app.py                GitHub App: JWT signing, installation
│                                token mint+cache, get_agent_token()
│                                with App-or-PAT preference
├── ntfy.py                      Phone push notifications (no-op if
│                                NTFY_TOPIC unset)
├── costs.py                     Session-hour cost estimate from
│                                unit_events timestamps
├── dashboard.py                 Rich TUI + markdown renderers
│                                (5 panels: features / in flight /
│                                awaiting merge / escalated / events)
├── cli.py                       Click CLI: init, doctor, run, dashboard,
│                                version
├── tools/                       MCP tool subpackage — see below
│   ├── __init__.py              FastMCP instance, marker regexes
│   │                            (PR_URL_RE, BLOCKED_RE, etc.),
│   │                            task-composition templates,
│   │                            get_agent_token() + need_github_token()
│   ├── planning.py              5 tools: load_feature, list_features,
│   │                            save_plan, get_plan, approve_plan
│   ├── execution.py             6 tools + cycle_review state machine:
│   │                            spawn_unit, spawn_tester, spawn_reviewer,
│   │                            address_review, cycle_review, send_to_unit
│   │                            with _tester_phase / _copilot_phase /
│   │                            _reviewer_phase / _emit_terminal helpers
│   ├── scheduling.py            4 tools: next_ready_units,
│   │                            next_ready_units_all, parallel_units,
│   │                            parallel_units_global (thread-pool based)
│   ├── observability.py         7 tools: get_unit_status, list_units,
│   │                            unit_history, unit_summary, unit_cost,
│   │                            feature_cost, show_dashboard
│   └── ops.py                   9 tools: hello_world_test, check_unit_pr,
│                                reconcile_unit_pr, list_in_flight,
│                                resume_unit, reset_cached_resources,
│                                verify_repo, list_verified_repos, forget_repo
└── prompts/
    ├── coder.md                 system prompt for coder Managed Agent
    ├── tester.md                system prompt for tester Managed Agent
    └── reviewer.md              system prompt for reviewer Managed Agent

scripts/
├── dashboard.sh                 Launch TUI dashboard
├── snapshot_state.sh            Daily backup of state.db (keeps last 30)
└── reset_cache.sh               Force re-create cached agents

CLAUDE.md                        Lead's system prompt (auto-loaded by claude)
.mcp.json                        Tells claude to spawn mcp_launcher
.pre-commit-config.yaml          11 hooks: ruff + ruff-format + hygiene
.github/workflows/ci.yml         pre-commit + 6-job test matrix + audit
pyproject.toml                   Package metadata + ruff + mypy + bandit +
                                 pytest + coverage configs
```

**Total: 31 MCP tools across 5 modules; 17 Python modules; ~3000 LOC.**

---

## 6. Data model

```
                    ┌───────────────────┐
                    │     features      │
                    │───────────────────│
                    │ id          PK    │
                    │ title             │
                    │ description       │
                    │ repo_path         │
                    │ branch_prefix     │
                    │ status            │ ◄── 'draft' → 'planned' →
                    │ created_at        │     'approved' → 'in_progress'
                    └───────────────────┘     → 'done'
                            │ 1
                            │
                            │ N
                    ┌───────────────────┐         ┌───────────────────┐
                    │      plans        │         │   work_units      │
                    │───────────────────│         │───────────────────│
                    │ feature_id  FK PK │ ◄──┐    │ unit_id     PK    │
                    │ units_json        │    │    │ feature_id  FK    │
                    │   (WorkUnit list) │    │    │ status            │ ◄── 9 states
                    │ status            │    │    │ branch            │
                    │   'draft'|        │    │    │ pr_number         │
                    │   'approved'      │    │    │ coder_session_id  │
                    │ approved_at       │    │    │ tester_session_id │
                    └───────────────────┘    │    │ reviewer_sess_id  │
                                             │    │ review_round      │
                                             │    │ last_activity     │
                                             │    │ last_error        │
                                             │    └───────────────────┘
                                             │              │ 1
                                             │              │
                                             │              │ N
                                             │    ┌───────────────────┐
                                             └────│   unit_events     │
                                                  │───────────────────│
                                                  │ id          PK    │
                                                  │ unit_id     FK    │
                                                  │ feature_id  FK    │
                                                  │ ts                │
                                                  │ event_type        │ ◄── append-only
                                                  │ source            │     audit log
                                                  │ cycle_number      │
                                                  │ summary           │
                                                  │ details           │
                                                  │ session_id        │
                                                  └───────────────────┘

                    ┌───────────────────┐
                    │ cached_resources  │ ◄── (role, prompt_hash) PK
                    │───────────────────│     30-day TTL via created_at check
                    │ role        PK    │
                    │ prompt_hash PK    │
                    │ agent_id          │
                    │ environment_id    │
                    │ created_at        │
                    └───────────────────┘

                    ┌───────────────────┐
                    │  verified_repos   │ ◄── repo_url PK
                    │───────────────────│     24h TTL — see VERIFY_TTL_HOURS
                    │ repo_url     PK   │     in orchestrator/state.py
                    │ default_branch    │
                    │ auth_mode         │     'pat' | 'app'
                    │ auth_identity     │     'user:login' or
                    │ verified_at       │     'app:installation:<id>'
                    │ has_branch_       │
                    │   protection      │
                    │ required_approvals│     ≥1 enforced
                    │ blocks_force_push │
                    │ blocks_deletion   │
                    │ blocks_bypass     │     enforce_admins=true
                    │ has_codeowners    │     warning flag
                    │ requires_signed_  │
                    │   commits         │     warning flag
                    │ warnings_json     │
                    └───────────────────┘
                            ▲
                            │  spawn-time gate (ensure_verified_*)
                            │  reads this table before allowing any
                            │  worker agent to act on a target repo
```

**Filesystem-resident state (not in SQLite).** Two surfaces live in the
orchestrator workdir's git repo rather than `state.db`:

* `features/F-XXX/spec.md` — durable, version-controlled feature design
  doc seeded by `load_feature` from `orchestrator/feature_spec.py`.
  Intent / Acceptance / Out-of-scope / Approach / Constraints /
  Decisions / Open questions. Edited by the lead during planning and
  preserved across re-invocations (`write_spec_if_missing` is
  idempotent — see `docs/SPEC-FORMAT.md`). Committed by the lead with
  `Why:` messages so the git log is the decision history.
* `features/F-XXX/U-N.md` — per-unit cycle log (F-006 Phase 1, separate
  unit). Mirrors `unit_events` rows into a human-readable summary
  alongside the PR description, so post-mortem context survives
  state.db loss / restore.

Schema reference for both: [`docs/SPEC-FORMAT.md`](SPEC-FORMAT.md).

### State transitions for `work_units.status`

```
              ┌─────────┐
              │ pending │  (plan approved, no spawn yet)
              └────┬────┘
                   │  spawn_unit
                   ▼
              ┌─────────┐
              │ coding  │  coder Managed Agent session active
              └────┬────┘
                   │  (coder pushes branch + opens PR)
                   ▼
              ┌─────────┐
       ┌─────►│ in_ci   │◄────┐
       │      └────┬────┘     │ FIX_PUSHED from address_review,
       │           │          │ or after every other state below
       │           │ spawn_tester
       │           ▼
       │      ┌─────────┐
       │      │ testing │
       │      └────┬────┘
       │           │  TESTS_PASS or BUG_FOUND
       │  TESTS_   ├──────────────────────────► (BUG_FOUND: increment
       │  PASS     │                              review_round, fix, retry)
       │           │  spawn_reviewer
       │           ▼
       │      ┌──────────┐
       │      │reviewing │
       │      └────┬─────┘
       │           │  REVIEW_APPROVED / REVIEW_RECOMMEND_MERGE /
       │           │  REVIEW_COMMENT or REVIEW_REQUEST_CHANGES
       └───────────┤  (changes: increment review_round, fix, retry)
                   │
                   │  human merges on github.com + reconcile_unit_pr called
                   ▼
              ┌─────────┐
              │  done   │   (terminal success)
              └─────────┘

ANY active state can transition to:
              ┌──────────┐
              │escalated │   (cap-3 hit, BLOCKED marker, no-marker emit,
              └──────────┘    or unexpected error). ntfy push fires.
```

Where `cycle_review` runs `_tester_phase()` then `_copilot_phase()` then
`_reviewer_phase()` as nested loops with `CAP_3 = 3` total fix cycles.

---

## 7. Control flows

### 7a. Feature → PR (the happy path)

```
sequenceDiagram (mental model — not actual UML):

You → Lead:           "ship feature X: description ..."
Lead → orchestrator:  load_feature(...)
                      → returns F-001
Lead → You:           [posts proposed work-unit breakdown]
You → Lead:           "looks good, approve"
Lead → orchestrator:  save_plan(F-001, [units...])
Lead → orchestrator:  approve_plan(F-001)
Lead → orchestrator:  next_ready_units_all()
                      → [F-001-U-1]
Lead → orchestrator:  spawn_unit(F-001, F-001-U-1)
   orchestrator → Anthropic: agents.create + environments.create
                              (cached if signature matches)
   orchestrator → Anthropic: sessions.create + events.send(task)
   orchestrator ← Anthropic: streamed agent.message events
                              → response text, terminate on
                                session.status_idle
   orchestrator → state.db:  status='in_ci', pr_number=N, coder_sid
   orchestrator → github:    PATCH PR body (append session id)
Lead → orchestrator:  cycle_review(F-001, F-001-U-1)
   _tester_phase:
       spawn_tester → tester Managed Agent → TESTS_PASS or BUG_FOUND loop
   _copilot_phase:
       github.request_copilot_review → wait_for_copilot_review (5min)
   _reviewer_phase:
       spawn_reviewer → reviewer Managed Agent →
           REVIEW_APPROVED / RECOMMEND_MERGE / COMMENT / REQUEST_CHANGES
       loop on REQUEST_CHANGES via address_review (cap 3)
   _emit_terminal('approved_awaiting_merge', ...)
                      → unit status flips to 'approved_awaiting_merge'
                      → ntfy push to your phone
Lead → You:           "✅ F-001-U-1 → PR #N, awaiting your merge"
You → github.com:     click merge
You → Lead:           "merged"
Lead → orchestrator:  reconcile_unit_pr(F-001-U-1)
                      → state flips to 'done' (check_unit_pr remains
                        a side-effect-free poll)
Lead → orchestrator:  next_ready_units_all()
                      → [F-001-U-2, F-001-U-3]  (if 1 unblocked them)
Lead → orchestrator:  parallel_units_global([{F-001, U-2}, {F-001, U-3}])
                      → thread pool, each runs spawn_unit + cycle_review
... continues until all units done.
```

### 7b. Cycle_review state machine in detail

```
                     ┌────────────────┐
                     │ cycle_review() │
                     └────────┬───────┘
                              │
                              ▼
                     ┌────────────────┐
                     │ _tester_phase  │
                     │                │
                     │  spawn_tester  │──BLOCKED──► _emit_terminal('escalated')
                     └────────┬───────┘
                              │ TESTS_PASS or BUG_FOUND
                              │
                ┌─────────────┴──────────────┐
                │                            │
            BUG_FOUND                    TESTS_PASS
                │                            │
                ▼                            ▼
        ┌──────────────┐              ┌────────────────┐
        │review_round  │              │ _copilot_phase │
        │  >= CAP_3?   │              │ (best-effort)  │
        └──────┬───────┘              └────────┬───────┘
            yes│  no                            │
               │  │                             ▼
   ┌───────────┘  │                     ┌──────────────────┐
   ▼              ▼                     │ _reviewer_phase  │
_emit_terminal  address_review          │                  │
('escalated')   (FIX_PUSHED?)           │ spawn_reviewer   │──BLOCKED─┐
                 yes│  no                └────────┬─────────┘          │
                    │  │                          │                    │
                    ▼  └──► _emit_terminal      APPROVED/             │
                clear tester_   ('escalated')   RECOMMEND_MERGE/      │
                  session_id                    COMMENT  → terminal   │
                    │                                                  │
                    └──► back to spawn_tester  REQUEST_CHANGES         │
                                                  │                    │
                                                  ▼                    ▼
                                          ┌──────────────┐    _emit_terminal
                                          │review_round  │    ('escalated')
                                          │  >= CAP_3?   │
                                          └──────┬───────┘
                                              yes│  no
                                                 │  │
                                       ┌─────────┘  │
                                       ▼            ▼
                          _emit_terminal       address_review
                          ('escalated')        (FIX_PUSHED?)
                                                yes│  no
                                                   │  │
                                                   ▼  └──► escalate
                                            clear reviewer_
                                              session_id
                                                   │
                                                   └──► back to spawn_reviewer

  _emit_terminal also:
    - On 'escalated' → ntfy.push_escalation
    - On 'approved_awaiting_merge' → ntfy.push_ready_to_merge
    - Always: returns JSON {outcome, message, history, final_state}
```

**CI-green gate between phases.** Every push to the PR (coder's initial
push, tester's test commit, coder's fix push) triggers a new CI run on
GitHub. `cycle_review` waits for that run to settle green BEFORE moving
to the next phase:

```
spawn_unit returns PR_URL
   │
   ▼
[GATE 1: wait_for_ci on coder PR push]
   │
   ├─ green / no_ci  →  proceed to _tester_phase
   ├─ failed         →  address_review(source='ci') →  FIX_PUSHED  →  loop GATE 1
   │                                                ↘  not FIX_PUSHED → escalate
   │                                                ↘  cap-3 hit       → escalate
   └─ timeout        →  escalate ("CI did not settle after Ns")

_tester_phase ends with TESTS_PASS
   │
   ▼
[GATE 2: wait_for_ci on tester's test push] — same red→fix loop

_copilot_phase + _reviewer_phase
   │
   ▼
_emit_terminal('approved_awaiting_merge')
```

A clean cycle makes exactly two `wait_for_ci` calls (GATE 1 + GATE 2).
The reviewer phase's own embedded fix-loop already gates on green after
any reviewer-driven push, so a final pre-merge re-check would only
re-pay a poll-interval-rounded wait without adding signal.

Inside `_tester_phase` and `_reviewer_phase`, every `address_review`
that returns FIX_PUSHED is followed by another `wait_for_ci` BEFORE the
tester or reviewer is re-spawned — a coder fix could break CI even if
it satisfies the previous reviewer/tester feedback.

CI failures count toward the same `CAP_3` shared cycle counter as
tester-bug and reviewer-change cycles. A pathologically flaky CI can
exhaust the cap before any tester or reviewer work happens; that's
intended — flaky CI is a real escalation signal.

The gate is implemented in `orchestrator/ci_wait.py` (pure polling
helper) + `_wait_ci_with_fix_loop()` in `orchestrator/tools/execution.py`
(cycle_review-flavor gate with embedded fix loop). The standalone
`spawn_tester` / `spawn_reviewer` MCP tools use a simpler gate
(`_check_ci_or_refuse`) that returns an ERROR rather than running a fix
loop — those are for direct lead-driven use; the lead can fix CI
manually or fall back to `cycle_review` for automation.

Timeouts and grace periods are configurable via env:
`CI_WAIT_TIMEOUT_SECONDS` (default 600s), `CI_WAIT_POLL_INTERVAL`
(default 5s — `0` is honored as a busy-poll for tests / very fast CI;
no lower-bound clamp), `CI_WAIT_NO_CI_GRACE` (default 30s). "No CI
configured" (zero check_runs after the grace period) is a pass-through
— sandbox repos without GitHub Actions still complete a cycle.

### 7c. Multi-feature parallel scheduling

```
                  ┌─────────────────────────┐
                  │ next_ready_units_all()  │
                  └────────────┬────────────┘
                               │
              for each approved feature in state:
                  for each unit in plan:
                      - if no row in work_units AND
                        all deps have status='done':
                          → emit as "ready"
                      - elif row exists with status='escalated':
                          → emit as "escalated"
                      - elif row exists with active status:
                          → emit as "in_flight"
                               │
                               ▼
              ┌──────────────────────────────┐
              │ Lead decides: 1 ready? Many? │
              └──────────────┬───────────────┘
                             │
            ┌────────────────┼────────────────┐
            │                │                │
       0 ready          1 ready          2+ ready
            │                │                │
            ▼                ▼                ▼
       "nothing to     spawn_unit +    parallel_units_global([
         do; awaiting    cycle_review     {feature_id, unit_id},
         merges"         (sequential)     ...
                                         ], max_concurrent=3)
                                              │
                                              ▼
                                         ThreadPoolExecutor
                                         - each thread runs
                                           spawn_unit + cycle_review
                                           independently
                                         - cap 5 concurrent (Anthropic
                                           rate limit safety)
                                         - returns when all done
                                         - failures isolated
                                           per-thread
```

---

## 8. External integrations

### 8a. Anthropic Managed Agents

| What | Where | Why |
|---|---|---|
| Worker abstraction | `workers/base.py` `Worker` Protocol + `workers/managed_agent.py` `ManagedAgentWorker` | Allows swapping backends without touching orchestrator logic; second impl `workers/docker_claude_code.py` shipped via F-001 |
| Session lifecycle | Anthropic-managed gVisor containers, 1-hour TTL | Avoids us running a sandbox. Sessions resumable by ID. |
| Tool surface inside agent | `agent_toolset_20260401` (Bash, file ops, web fetch) | Standard Anthropic preset; agents bring `gh` via the container's pre-installed apt packages |
| Network policy | `limited` mode with `ALLOWED_NETWORK_HOSTS` allowlist | Blocks data exfiltration to non-allowlisted hosts |
| Agent + env caching | `(role, prompt_hash)` → `(agent_id, env_id)` in `cached_resources` table; 30-day TTL | Avoid creating new Anthropic agents on every spawn. Prompt edit auto-busts cache. |

### 8b. GitHub

| What | Where | Why |
|---|---|---|
| Branch protection | Configured on `main` of target repo | Mechanically prevents agents from merging; agents can't approve their own PRs |
| Token: GitHub App | `github_app.py` mints installation tokens; tokens cached 50min | Bot identity (`<app>[bot]`), 1-hour lifetime, easy revocation |
| Token: PAT fallback | `GITHUB_TOKEN` env var | Single-user sandbox path; remains supported |
| PR operations | `github.py`: `amend_pr_body`, `post_pr_comment`, `get_pr_state`, `get_pr_check_runs` | Used by orchestrator to surface agent activity on PRs as the human-visible UI layer |
| Copilot integration | `github.request_copilot_review`, `wait_for_copilot_review` | Two-reviewer model: Copilot for line-level, our reviewer for spec/scope |
| Self-approval | NOT fixed; reviewer detects same-identity case and emits `REVIEW_RECOMMEND_MERGE` | Single-App identity means coder and reviewer share login; the marker is the orchestrator's signal that human merger should treat the comment as a passing review |

### 8c. ntfy.sh

| What | Where | Why |
|---|---|---|
| Push module | `ntfy.py` | Phone-friendly real-time notifications |
| Triggers | `_emit_terminal` in cycle_review: escalation + approved_awaiting_merge; `spawn_unit` on direct BLOCKED / no-marker | "Real" attention events vs noise |
| No-op when unconfigured | `is_configured()` check at top of `push()` | Optional; system works fine without |
| Threat model | Topic is hard-to-guess string; treated as password | ntfy.sh public server; topic = effective auth |

### 8d. Anthropic API auth flow

```
1. orchestrator/mcp_launcher.py spawns mcp_server with minimum env
   (ANTHROPIC_API_KEY NOT in the parent env)
2. mcp_server.py: load_dotenv() reads .env → puts ANTHROPIC_API_KEY in os.environ
3. workers/managed_agent.py: Anthropic() client picks up ANTHROPIC_API_KEY from os.environ
4. Worker.spawn() calls client.beta.agents.create + sessions.create
```

This means `claude` itself (the Claude Code session) does NOT see
`ANTHROPIC_API_KEY` and therefore uses your claude.ai Team-plan token
instead of API billing. Saves money on the lead's tokens.

---

## 9. Security model

### Trust boundaries

```
┌─────────────────────────────────────────────────────────────────┐
│ TRUSTED (assumed friendly):                                     │
│   - Your laptop                                                 │
│   - Your claude.ai login (used by Claude Code)                  │
│   - Your ANTHROPIC_API_KEY                                      │
│   - The orchestrator Python code                                │
│   - state.db on disk                                            │
├─────────────────────────────────────────────────────────────────┤
│ TRUSTED BY CONTRACT (Anthropic infrastructure):                 │
│   - Managed Agent gVisor containers                             │
│   - Session storage + event history                             │
│   - JWT-based authentication                                    │
├─────────────────────────────────────────────────────────────────┤
│ SEMI-TRUSTED (could go rogue):                                  │
│   - Spawned coder/tester/reviewer agents                        │
│   - Their PR titles + bodies + commit messages                  │
│   - Any external service the agent reaches                      │
├─────────────────────────────────────────────────────────────────┤
│ UNTRUSTED:                                                      │
│   - External URLs an agent might be tricked into hitting        │
│   - Open-source dependencies the agent installs                 │
│   - Anything an attacker controls                               │
└─────────────────────────────────────────────────────────────────┘
```

### Defenses in depth

| Threat | Defense |
|---|---|
| Agent merges its own PR | No `merge_pr` MCP tool exists, by design |
| Agent force-pushes or deletes | PAT/App scope excludes admin; prompt hard rules; branch protection blocks at GitHub layer |
| Spawning against a repo with no branch protection (lost the no-merge guarantee) | **Verification gate** — `verified_repos` cache + `ensure_verified_*` helpers refuse to spawn until the repo passes policy (read/write/protection/approvals/no-bypass). 24h TTL re-verifies. |
| Agent commits secrets | Pre-commit hook in target repo recommended; coder prompt explicit "NEVER commit secrets" |
| Agent exfiltrates code/tokens to outside URL | Managed Agent `limited` network mode + `ALLOWED_NETWORK_HOSTS` allowlist |
| Compromised dependency runs in container | gVisor sandbox + limited network → blast radius capped |
| Agent modifies `.github/workflows/*` | PAT/App scope excludes Workflows write; coder prompt hard rule |
| Cost runaway | `CAP_3` shared cycle limit + cost telemetry (`feature_cost`) |
| Bot self-approves its own PR | **Human review as the final gate** — on repos with CODEOWNERS, GitHub auto-requests review from the owning team; on sandbox repos, the user reviews. The reviewer agent is a *pre-screener* and emits `REVIEW_RECOMMEND_MERGE` (never `--approve`). No `merge_pr` MCP tool exists. Branch protection enforces "human approval before merge" at GitHub layer. |

The verification gate is implemented as `ensure_verified_for_feature()` /
`ensure_verified_for_unit()` in `orchestrator/tools/__init__.py`. Every
spawn surface (`spawn_unit`, `spawn_tester`, `spawn_reviewer`,
`address_review`, `cycle_review`, `send_to_unit`, `parallel_units`,
`parallel_units_global`) calls one of them at the top and refuses to
proceed if the target repo is missing from the cache or its row has aged
past `VERIFY_TTL_HOURS`. `load_feature` only warns (planning is free);
spawns hard-block. The cache is populated by the `verify_repo()` MCP
tool or the `orchestrator verify-repo <url>` CLI subcommand.
| Stale cached agents | `cached_resources` 30-day TTL + manual `reset_cached_resources` |
| state.db loss | `scripts/snapshot_state.sh` (user-driven daily backup) |
| Token in agent session history | GitHub App installation tokens are 1-hour-lived; PAT tokens are not (PAT path is for sandbox use only) |
| Secrets leak from parent shell to MCP subprocess | `mcp_launcher.py` env allowlist strips all but ~15 vars; `ANTHROPIC_API_KEY` etc. are read fresh from `.env` |
| Self-approval bypass (reviewer rubber-stamps coder's PR) | Reviewer prompt's pre-flight `gh api /user` check + `REVIEW_RECOMMEND_MERGE` fallback; branch protection requires human approval; full fix needs two-identity model (BACKLOG) |

### What we explicitly DON'T defend against (and why)

| Threat | Why not | Workaround |
|---|---|---|
| Stolen `ANTHROPIC_API_KEY` | Out of scope — same as any API key | Rotate; `chmod 600 .env` |
| Compromised laptop | Out of scope — same as any local-dev tool | Standard OS hygiene |
| Multi-user data isolation | v1 is single-user | Multi-tenant work in BACKLOG |
| Reviewer agent rubber-stamping | LLM judgment limitation | Hybrid review with GitHub Copilot reduces this; human still merges |

See `SECURITY.md` for the full threat model + reporting policy.

---

## 10. Quality engineering

### Test pyramid

```
                         ┌─────────────┐
                         │ Integration │     ~50 tests (cycle_review with
                         │ tests       │      mocked workers; CLI subcommands
                         │             │      with CliRunner)
                         └─────────────┘
                       ┌─────────────────┐
                       │  Component tests │   ~150 tests (state, github,
                       │                  │    ntfy, costs, dashboard,
                       │                  │    tools/* — mocked externals)
                       └──────────────────┘
                  ┌──────────────────────────┐
                  │      Unit tests          │  ~90 tests (regexes, helpers,
                  │      (pure functions)    │   parse_repo_url, JWT shape,
                  │                          │   markdown table render, etc.)
                  └──────────────────────────┘

   289 tests total · 5-second runtime · 85.83% line + branch coverage
```

### What runs where

| Layer | When | Tools |
|---|---|---|
| Local pre-commit | every `git commit` | ruff lint (`--fix`), ruff format, file hygiene (trailing whitespace, EOF, YAML/TOML check, large-file blocker, merge-conflict markers, line-ending norm, case-conflict, detect-private-key) |
| Local pytest | every `pytest` | all 289 tests + coverage report (no `--cov-fail-under` locally) |
| CI test matrix | every push/PR | ubuntu + macOS + Windows × Python 3.11 + 3.12 = **6 jobs**. Each runs: `pre-commit run --all-files` (same hooks as local), mypy, bandit, pytest with `--cov-fail-under=80` |
| CI dependency audit | every push/PR | pip-audit (warning-only) |

### Coverage by module

| Module | Coverage | Notes |
|---|---|---|
| `models.py` | 100% | dataclasses |
| `costs.py` | 100% | pure logic |
| `state.py` | 99% | SQLite CRUD + TTL + events |
| `ntfy.py` | 96% | push helpers + no-op path |
| `tools/observability.py` | 100% | thin wrappers |
| `tools/planning.py` | 100% | feature/plan tools |
| `tools/ops.py` | 94% | hello_world, check_unit_pr, reconcile_unit_pr, list_in_flight, resume_unit |
| `tools/scheduling.py` | 97% | DAG + parallel |
| `tools/execution.py` | 91% | cycle_review state machine |
| `dashboard.py` | 93% | rich panels + markdown |
| `cli.py` | 75% | Click subcommands |
| `github.py` | 71% | REST helpers (polling loop + some error paths uncovered) |
| `tools/__init__.py` | 80% | shared infra |
| `github_app.py` | **100%** | JWT mint + token cache + fallback |
| `workers/managed_agent.py` | 25% | `_resource_signature` pure-fn tested; `ManagedAgentWorker` SDK calls intentionally not mocked |
| `workers/docker_claude_code.py` | 86% | Argv construction, cred audit, doctor probes, internal-registry passthrough all unit-tested; E2E suite (`tests/e2e/`) gated on `ORCH_RUN_E2E=1` covers real Docker daemon path |
| `workers/base.py`, `workers/__init__.py` | 100% / 100% | Protocol shape + factory branching |

**`workers/managed_agent.py` is the deliberate gap.** Fully mocking the
Anthropic SDK is substantial; would need to inject a fake `Anthropic`
client and stub `beta.agents.create`, `beta.sessions.create`,
`beta.sessions.events.stream`, etc. Tracked in BACKLOG; we're at 86%
overall coverage and the gate is 80%, so this is below the urgency
threshold. The Docker backend (`workers/docker_claude_code.py`) does
not have this gap — subprocess is dependency-injectable and every
hardening flag is asserted on in unit tests.

### Lint / type / security gates

| Check | Tool | Setting |
|---|---|---|
| Style + idioms | ruff (E/F/W/I/B/UP/SIM/TID252) | 100 char lines, py311 target |
| Format | ruff format | enforced in CI |
| Types | mypy | non-strict (`check_untyped_defs=true`) — start permissive, ratchet later |
| Security scan | bandit | skip B101 (assert in tests); other plugins enforce |
| Dep CVE | pip-audit | warning-only |

---

## 11. Extension points

### 11a. Adding a new MCP tool

1. Add a function decorated with `@mcp.tool()` in the appropriate `tools/*.py` file
2. Update `CLAUDE.md` to teach the lead when to call it
3. Add tests under `tests/test_tools_<module>.py`
4. That's it — the entry point auto-imports all `tools/*.py` modules

### 11b. Adding a new Worker backend (e.g. Bedrock, Modal, local)

1. Add a new module `orchestrator/workers/<your_backend>.py` implementing
   the `Worker` Protocol from `workers/base.py`:
   ```python
   class MyWorker:
       role: str
       def spawn(self, task: str, *, title: str | None) -> tuple[str, str]: ...
       def resume(self, session_id: str, msg: str) -> str: ...
       def archive(self, session_id: str) -> None: ...
   ```
2. Register the backend in `workers/__init__.py`'s `make_worker(role)`
   factory under a new value of `ORCH_WORKER_BACKEND`. The factory is the
   single switchboard; nothing in `tools/` or the lead persona changes.
3. Add the backend to `KNOWN_BACKENDS` so the factory rejects typos clearly.
4. Add tests under `tests/test_<your_backend>_worker_*.py` — patterns to
   reuse from F-001: argv construction (`build_*_argv`), credential
   boundary (`build_cred_audit().render()` snapshot), doctor probes.

The orchestrator's tool layer never assumes the worker is Anthropic-specific.
The biggest practical gotcha is the agent's tool surface (`agent_toolset_20260401`
gives Anthropic agents Bash/file ops/etc. for free; other backends need their
own way to provide that — `DockerClaudeCodeWorker` does this via a baked-in
container image with `git`, `gh`, `claude`, Python, and Node; Modal/E2B
sandboxes work similarly).

### 11b-bis. Choosing a worker backend (Managed Agents vs Docker)

Two `Worker` implementations ship today, both pluggable via the
`orchestrator/workers/__init__.py` factory keyed by
`ORCH_WORKER_BACKEND`. The trade-offs are summarized in
[`README.md` § "Choosing a worker backend"](../README.md#choosing-a-worker-backend);
the architectural points worth pinning here:

* **Auth boundary.** Managed Agents authenticate by the API key; the
  Docker backend chooses at spawn time via
  `select_auth_mode(host_env)` — `ANTHROPIC_API_KEY` set ⇒ API-key
  mode (forwarded into the container, no `~/.claude` mount);
  unset ⇒ OAuth mode (claude.ai credentials bind-mounted read-only
  from `~/.claude`, sessions sub-mount bound rw separately so
  `claude --resume` can persist state without writing through the ro
  creds mount). No fallback identity in OAuth mode — a missing
  claude.ai login surfaces as a spawn failure, not a silent demote
  to API billing.
* **Credential audit as receipts.** `build_cred_audit()` returns a
  `CredAudit` dataclass (`workers/docker_claude_code.py`) the doctor
  command renders as a fixed-format text block: every env var passed
  / dropped, every mount, every never-mounted host path, every
  internal-registry host added to the DNS allowlist. The shape is
  pinned by snapshot tests (`tests/test_doctor_cred_audit.py`).
  Adding a third backend should follow the same receipt pattern
  rather than inventing a new audit surface.
* **Network policy.** Managed Agents run on Anthropic's kernel-side
  egress allowlist; Docker workers run on a DNS-level allowlist
  (`network/allowlist.dnsmasq.conf`, served by
  `scripts/run-worker-dns.sh`). The DNS layer is documented as a
  **soft boundary** — raw-IP egress is still possible — alongside
  the hard layers (read-only rootfs, `--cap-drop=ALL`, `--user
  1000:1000`, `--memory`/`--cpus`/`--pids-limit`).
* **OAuth lifecycle.** The host's Claude Code refreshes its OAuth
  token in-place inside `~/.claude`; since we mount that directory
  read-only the worker sees the refreshed token transparently for the
  lifetime of its container. Per-session state (transcripts, the
  session UUIDs `claude --resume` consumes) lives under the writable
  `~/.claude/sessions` sub-mount.
* **Concurrency model.** Managed Agents parallelism is bounded by
  Anthropic's quotas. Docker workers are bounded by the user's
  claude.ai plan concurrency (~1–2 on Pro, more on Team/Max), so
  `parallel_units(_global)` can briefly drive 9 live sessions (3
  units × 3 roles) which serialize against the plan cap. F-001-U-6's
  E2E suite stress-tests this against a real daemon.

The `Worker` Protocol in `workers/base.py` is the seam — adding
`DockerAiderWorker`, `DockerOpenAICodexWorker`, or a `BedrockClaudeWorker`
on top of U-1's abstraction is ~150 LOC + tests per backend and changes
nothing in `tools/` or the lead persona. See `BACKLOG.md` for sized
entries.

### 11c. Adding a new state backend (Postgres / etc.)

1. Define a `StorageBackend` Protocol with the public functions of `state.py`
2. Refactor `state.py` to dispatch through the backend
3. Add `PostgresBackend` (or whatever) impl
4. Config: `STORAGE_BACKEND=postgres` + connection string env var

Major work — not done yet. Required for multi-tenant deployment. Tracked in
BACKLOG.

### 11d. Customizing prompts

`orchestrator/prompts/*.md` are loaded fresh on every Worker creation. Edit
them; the prompt hash changes; the next spawn creates a new cached agent
automatically. No manual reset needed (unless you also change the model or
network config — those aren't in the hash; use `reset_cached_resources`).

### 11e. Per-feature spec.md and per-unit cycle logs

Two filesystem persistence surfaces sit alongside `state.db` in the
orchestrator workdir (see §6 for the data-model summary):

* **`features/F-XXX/spec.md`** — feature-level design doc. Seeded on
  `load_feature` from `orchestrator/feature_spec.py::render_template()`
  and never overwritten. Schema and `Why:` commit pattern in
  [`docs/SPEC-FORMAT.md`](SPEC-FORMAT.md). To customize the starter
  template, edit `render_template()`; existing files on disk are
  preserved.
* **`features/F-XXX/U-N.md`** — per-unit cycle log written by
  `orchestrator/cycle_log.py` (F-006-U-2). Idempotent; can be
  regenerated from `state.unit_events` + `gh pr view`. The renderer is
  a pure library — call sites (cycle_review terminal hand-off, manual
  `regenerate_cycle_log`) are wired in later Phase 1 units.

Adding a new persistent-on-disk artifact follows the same shape:
write-to-tmp + `Path.replace`, idempotent re-renderer, prefix-rooted
under `features/F-XXX/`, no escape outside the orchestrator workdir.

---

## 12. Key decisions + rejected alternatives

### Why MCP between lead and orchestrator (vs. inline Python tool in Claude Code)

- **Chose MCP**: lets the lead be a regular Claude Code session, not a
  bespoke wrapper. Mobile chat via Remote Control "just works." The
  Python orchestrator is testable independently.
- **Rejected**: a Claude Code subagent that runs Python directly. Subagents
  in Claude Code are fresh per invocation; we needed persistent sessions
  across review-fix cycles.

### Why Managed Agents (vs. local executor)

- **Chose Managed Agents**: Anthropic provides gVisor sandboxing, network
  policy, and tool execution out of the box. No infrastructure to maintain.
- **Rejected**: running the agent loop ourselves with Modal/E2B sandboxes.
  Would have added ~2 weeks of work for marginally better cost control.

### Why SQLite for state (vs. Postgres / Redis / files)

- **Chose SQLite**: zero setup, single file, sufficient for single-user.
  Tests run against temp DBs with no fixtures beyond a path.
- **Rejected**: Postgres. Real DB needed for multi-tenant, but we're
  single-user. Adding it speculatively would burn ~1.5 days for no v1
  benefit.

### Why GitHub App + PAT fallback (vs. App-only or PAT-only)

- **Chose dual**: App is the production-grade path (bot identity, short
  tokens, easier audit/revoke); PAT path is one env var for sandbox users.
- **Rejected App-only**: would have made "try this in 5 min" significantly
  harder. App setup requires GitHub admin access, key generation, etc.
- **Rejected PAT-only**: tokens lived 90 days; commits attributed to your
  user; revocation harder.

### Why two-reviewer model (Copilot + our reviewer)

- **Chose hybrid**: Copilot is calibrated for line-level review (anti-patterns,
  idiomatic violations); our reviewer is task-aware (knows the unit spec,
  scope, feature context). They cover complementary failure modes.
- **Rejected our-reviewer-only**: as observed live, our reviewer missed
  things Copilot caught. Calibration drift over time also a concern.
- **Rejected Copilot-only**: Copilot doesn't have the unit description; can't
  judge spec compliance.

### Why cycle_review is synchronous (vs. async/poll model)

- **Chose synchronous**: MCP tool calls are inherently synchronous from
  Claude Code's perspective; making this async would require redesigning
  the entire lead-orchestrator contract.
- **Cost**: lead is blocked for 5-20+ min during cycle_review. Acceptable
  because the user is on their phone and can do other things.
- **Tracked in BACKLOG**: True async cycle_review is a real-engineering item
  for if/when this becomes painful.

### Why a thin MCP launcher (vs. direct python -m orchestrator.mcp_server)

- **Chose launcher**: strips parent env to a minimum allowlist before
  exec'ing. If the MCP subprocess is ever compromised, it inherits HOME/PATH
  but not ANTHROPIC_API_KEY / GITHUB_TOKEN / SSH agent / AWS creds / etc.
- **Cost**: one extra Python process startup (~50ms). Negligible.
- **Rejected**: passing secrets through. Defense in depth says minimize
  the attack surface even when the surface is "merely our own code."

### Why TUI dashboard + chat markdown (vs. one or the other)

- **Chose both**: TUI for ambient awareness at the laptop; chat markdown
  for on-demand status from anywhere (including phone). Same data layer
  (`_*_data()` functions feed both renderers).
- **Rejected web dashboard**: would require a running HTTP server,
  authentication, networking story. For single-user, terminal + chat
  is enough.

### Why a Worker protocol from day one (vs. concrete `ManagedAgentWorker` only)

- **Chose protocol**: cost is 30 minutes; lets future backends slot in
  without touching the tool layer. Already saved us when we tested
  parallel execution with mock workers (the entire `test_tools_execution.py`
  test suite uses a `FakeWorker`).
- **Rejected concrete-only**: would have required a real Anthropic call
  on every test run, ~30 seconds per test, $$$.

---

## 13. Glossary

| Term | Meaning |
|---|---|
| **Lead** | The Claude Code session that the user chats with. Has access to MCP tools, no direct API access. |
| **MCP** | Model Context Protocol. The JSON-RPC-over-stdio protocol Claude Code uses to talk to tool servers. |
| **MCP server** | Our Python process implementing the orchestrator's tool surface. |
| **Worker** | Abstract role for a Managed Agent that does work (coder/tester/reviewer). |
| **Managed Agent** | An Anthropic-hosted sandbox container running a model + tools, addressable by `session_id`. |
| **Session** | A specific running instance of a Managed Agent. Has its own conversation history, persistable, resumable by ID. |
| **Feature** | A user-described unit of work, broken down into Work Units. |
| **Work Unit** | A single PR's worth of code change. Has a unit_id like `F-001-U-3`, depends on zero or more other units. |
| **Plan** | The DAG of work units for a feature, plus its approval status. |
| **Cycle** | One iteration of `coder → fix` driven by tester or reviewer feedback. CAP_3 = max 3 per unit. |
| **review_round** | Counter on a unit. Increments on each `address_review` call. Hitting CAP_3 escalates. |
| **Marker** | Sentinel string at the end of an agent's response (`PR_URL:`, `TESTS_PASS`, `BLOCKED:`, etc.) parsed by the orchestrator. |
| **Escalation** | A terminal failure state. Unit goes to `status=escalated`. ntfy push fires. Human takes over. |
| **Self-approval** | When the same GitHub identity opens and reviews a PR. GitHub blocks `--approve`; reviewer falls back to `REVIEW_RECOMMEND_MERGE`. |
| **TUI** | Terminal user interface (the `rich`-based dashboard). |
| **Remote Control** | Claude Code feature that lets you join a laptop session from a mobile device via QR. |
| **Resource signature** | sha256 of (role, prompt, model). Cache key for Anthropic agent + environment. Changes when any input changes, auto-busting the cache. |

---

## Where to look when

- **Adding a feature to the orchestrator itself** → start at the relevant
  `tools/*.py` module + write tests under `tests/test_tools_<module>.py`
- **Debugging a stuck unit** → `unit_history(unit_id)` MCP tool, the dashboard,
  or `sqlite3 state.db` directly
- **Understanding why a decision was made** → §12 of this doc, or `BACKLOG.md`
- **Setting up the project** → `README.md`
- **Security review** → `SECURITY.md`
- **Operational issues** → `TROUBLESHOOTING.md`
- **Future work** → `BACKLOG.md`
- **The agent prompts themselves** → `orchestrator/prompts/*.md`
- **The lead's behavior contract** → `CLAUDE.md`
