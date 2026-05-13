# Security policy + threat model

This document describes the orchestrator's trust boundaries, what's
defended, what isn't, and how to report issues.

## Reporting a vulnerability

Open a private security advisory on this repository, or email the maintainer
directly. Don't open public issues for unpatched vulnerabilities.

---

## Threat model (v1, single-user)

The orchestrator is a **single-user developer tool**. It is NOT a
multi-tenant service. The threat model reflects that.

### Trust boundaries

```
┌──────────────────────────────────────────────────────────────┐
│ Trusted (assumed friendly):                                  │
│   - The user's laptop                                        │
│   - The user's claude.ai login (Claude Code)                 │
│   - The user's API key (ANTHROPIC_API_KEY)                   │
│   - The user's GitHub PAT (GITHUB_TOKEN)                     │
│   - The orchestrator MCP server (Python code we wrote)       │
│   - The state.db on the user's laptop                        │
│                                                              │
│ Anthropic infrastructure (trust by contract):                │
│   - Managed Agent containers (gVisor-isolated by Anthropic)  │
│   - Session storage + event history                          │
│                                                              │
│ Semi-trusted (could go rogue):                               │
│   - Spawned agent sessions (LLM + tool execution)            │
│   - Their PR titles / bodies / commit messages               │
│   - PR comments from Copilot or other reviewers              │
│                                                              │
│ Untrusted:                                                   │
│   - External URLs the agent might be tricked into hitting    │
│   - Open-source dependencies the agent installs              │
│   - Anything an attacker controls                            │
└──────────────────────────────────────────────────────────────┘
```

### What the orchestrator defends against

| Threat | Defense |
|---|---|
| Agent makes unintended commits to main | GitHub branch protection (configured per-repo, required 1 approval) |
| Agent merges its own PR | No `merge_pr` MCP tool exists, by design |
| Agent force-pushes / deletes branches | PAT scope excludes admin; agent prompts have hard rules; GitHub blocks via branch protection |
| Agent commits secrets | Pre-commit hooks (repo-level, recommended); coder prompt explicit "NEVER commit secrets" |
| Agent goes rogue and exfiltrates code/tokens | Managed Agent `limited` network — only github.com + pypi/npm + anthropic.com reachable. Most exfil endpoints unreachable. |
| Compromised dependency runs in agent container | Container is gVisor-sandboxed by Anthropic + limited network — blast radius capped |
| Agent modifies CI workflows | Coder prompt hard rule, PAT does NOT grant `Workflows` permission |
| Cost runaway | Cap-3 review cycle limit; cost telemetry (`feature_cost`) |
| Stale agent sessions accumulate | TTL on cached resources (30 days) + manual `reset_cached_resources` |
| State.db corruption / loss | `./scripts/snapshot_state.sh` daily backups (user-driven) |

### Human review as the final gate

Every PR the orchestrator opens lands in front of a human before merge.
There are two paths:

1. **Repos with CODEOWNERS** (recommended for production). GitHub
   auto-requests review from the owning team on every bot PR. The
   orchestrator's reviewer agent emits `REVIEW_RECOMMEND_MERGE` as an
   informational endorsement; humans formally approve and merge.
2. **Repos without CODEOWNERS** (sandbox / single-developer). The user
   is the only reviewer. Same `REVIEW_RECOMMEND_MERGE` terminal state;
   user merges from chat or the GitHub UI.

The orchestrator deliberately has no `merge_pr` tool. Branch protection
on the default branch (verified by `verify_repo`) ensures the only path
from "PR open" to "merged into main" goes through a human clicking
"merge".

The previously-documented "same-account self-approval limitation" is
**not a limitation — it's the design.** The bot SHOULD NOT approve its
own work. Two-identity setups (App-for-coder + PAT-for-reviewer) are
still tracked in `BACKLOG.md` as an option for teams that explicitly
want bot-only approval flows, but they are not the recommended path.

### Non-defenses — what the orchestrator does NOT defend against (known limitations)

| Threat | Why deferred | Mitigation today |
|---|---|---|
| **A malicious or hijacked Anthropic API key holder** | If your `ANTHROPIC_API_KEY` is stolen, attacker can spawn arbitrary agents on your account | Out of scope — same threat model as any API key. Keep `.env` permissions tight; rotate periodically |
| **Compromise of the user's laptop** | Full local trust assumption | Out of scope — same as any local-dev tool |
| **Multi-user data isolation** | v1 is single-user; state.db has no per-user rows | If you deploy to multiple users, you need the Postgres+auth work in BACKLOG.md first |

### Token exposure in session history — improved by GitHub App

Worker agents need a token to clone + push. The orchestrator embeds it in
the initial task message; the token ends up in Anthropic's session storage
for the lifetime of the session.

**Before**: GITHUB_TOKEN PAT (long-lived, your identity, broad-ish scope).
**Now**: GitHub App installation token (1-hour lifetime, bot identity, per-installation scope).

Installation tokens expire automatically. Even if a session's storage were
somehow exposed past the 1-hour window, the token wouldn't work anymore.
This is the most important security improvement in the orchestrator.

---

## Security controls in the codebase

### Agent execution sandbox

- **Container isolation**: Managed Agents run inside gVisor (kernel
  syscall sandbox), per Anthropic's documented infrastructure.
- **Network allowlist**: `ALLOWED_NETWORK_HOSTS` in
  `orchestrator/agents.py` restricts outbound traffic. `limited` mode
  blocks any destination not on the list.
- **Per-role least privilege** (in prompts):
  - `coder`: can write any file, commit, push, open PRs
  - `tester`: only writes to `tests/`
  - `reviewer`: READ-ONLY, posts review via `gh pr review --comment`

### Token handling

- API keys + PAT live in `.env`, NEVER committed (in `.gitignore`)
- `orchestrator run` explicitly clears `ANTHROPIC_API_KEY` from the
  shell before launching `claude` (so Claude Code uses the Team-plan
  token instead of API billing)
- The MCP server reads from `.env` via `python-dotenv` — never from
  the parent shell environment
- **GitHub auth: App preferred over PAT.** `orchestrator/github_app.py`
  mints 1-hour-lived installation tokens from the App's private key.
  Tokens are cached in-process for 50 min, then re-minted. PAT
  fallback remains for single-user setups.
- The MCP launcher (`orchestrator/mcp_launcher.py`) strips all env
  vars except a small allowlist before exec'ing the server — secrets
  inherited from the parent shell don't reach the subprocess.
- Tokens passed to agents only in the initial task message; agent
  prompts say "NEVER echo the token"
- App private key (`.pem`) should be `chmod 600` on the user's disk

### Code hygiene

- **Linting**: `ruff check` runs in CI on every push/PR
- **Type checking**: `mypy` runs in CI (currently non-strict; tightening
  over time)
- **Security scan**: `bandit -r orchestrator` runs in CI
- **Dependency audit**: `pip-audit` runs in CI (non-blocking warning)
- **Tests**: 110+ pytest tests, all required to pass before merge

### What the user must do themselves

These are NOT enforced by the orchestrator:

1. **Configure branch protection** on every target repo (1 approval
   required, no force-push, no deletion) — see README "Required before
   Stage 3"
2. **Use a fine-grained PAT** scoped to specific repos with minimum
   permissions — see README "GitHub PAT"
3. **Rotate the PAT every 90 days** (default fine-grained PAT lifetime)
4. **Snapshot state.db** periodically (`./scripts/snapshot_state.sh`)
5. **Review every PR** before merging — the orchestrator does NOT
   self-merge; agents do not have `merge_pr` tool

---

## Known issues (not yet fixed)

None at this time. Issues that have been identified and patched are in
git history.

## Reporting + disclosure

For security-sensitive issues: private security advisory on this repo,
or email the maintainer. **Do not** post tokens, exploit details, or
unpatched vulnerabilities in public issues / PRs.
