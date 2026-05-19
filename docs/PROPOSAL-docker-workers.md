# Proposal: Docker workers + claude.ai auth + LLM abstraction

**Status:** ✅ **delivered v1 on 2026-05-14** as feature F-001 (units U-1 through U-6, PRs [#1](https://github.com/boctorj/agent-orchestrator/pull/1) · [#11](https://github.com/boctorj/agent-orchestrator/pull/11) · [#13](https://github.com/boctorj/agent-orchestrator/pull/13) · [#14](https://github.com/boctorj/agent-orchestrator/pull/14) · [#16](https://github.com/boctorj/agent-orchestrator/pull/16) · [#18](https://github.com/boctorj/agent-orchestrator/pull/18)). This doc preserves the design narrative; the canonical reference for current state is [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) §11 (Extension points: Worker protocol) and the inline docstrings in `orchestrator/workers/docker_claude_code.py`. Drifts between the original proposal and the shipped implementation are marked **[Δ shipped]** in the relevant sections.

**Original revision:** 2026-05-12 (pre-Phase 1)
**Last updated:** 2026-05-14 (post-delivery sweep — arch-drift skill)

---

## Goal

Run the orchestrator end-to-end **without an Anthropic API key**, on
infrastructure the user controls (Docker), with a clean seam for swapping in
different LLMs later. Workers execute on the user's laptop inside locked-down
containers; auth piggybacks on the same claude.ai subscription the lead already
uses.

## The shape of the change

The orchestrator already had the right abstraction: the `Worker` protocol.
This proposal added a second implementation alongside `ManagedAgentWorker`
(Anthropic Managed Agents API) and split the protocol into its own module:

```
orchestrator/
  workers/
    __init__.py            ← make_worker(role) factory; picks backend from env
    base.py                ← Worker protocol (moved from agents.py in U-1)
    managed_agent.py       ← existing ManagedAgentWorker (relocated)
    docker_claude_code.py  ← Docker-backed Worker
  network/
    __init__.py            ← allowlist_config_path(), package-manager hosts
    allowlist.dnsmasq.conf ← outbound DNS allowlist for worker containers
docker/
  worker.Dockerfile        ← Python 3.12 + Node + git + gh + claude CLI
scripts/
  run-worker-dns.sh        ← starts dnsmasq sidecar on 127.0.0.1:5353,
                             ensures orch-net bridge exists
tests/
  e2e/test_docker_worker_smoke.py  ← E2E suite against real Docker
  fixtures/sandbox-repo/   ← minimal repo fixture for the workspace mount
.github/workflows/
  ci.yml                   ← matrix: ubuntu/macos/windows × Python 3.11+3.12
  e2e-docker.yml           ← opt-in E2E job, ORCH_RUN_E2E=1 gate
```

**[Δ shipped]** The proposal named the Worker protocol's home as
`orchestrator/agents.py`. U-1 relocated it to `orchestrator/workers/base.py`;
`orchestrator/agents.py` survives as a small shim module (docstring + `__all__`
+ re-exports of `Worker`, `ManagedAgentWorker`, and `make_worker`) so historical
callers don't break.

The factory reads `ORCH_WORKER_BACKEND=managed_agents|docker` from `.env`
(default: `managed_agents`). Everything downstream — `cycle_review`, the gate
logic, the prompts, the verification cache, the dashboard — sees an opaque
`Worker` interface and doesn't care which implementation answered.

## Auth: how claude.ai flows into containers

Claude Code stores OAuth credentials in `~/.claude/credentials.json` (and
related files). The Docker worker supports **two auth modes**, chosen at
spawn time by `select_auth_mode()`:

- **OAuth mode (default)** — when `ANTHROPIC_API_KEY` is unset on the host:
  mount `~/.claude` **read-only** into each container at
  `/home/agent/.claude`. Claude Code finds the creds and authenticates as
  the user. Writable sub-mount `~/.claude/sessions` is bound separately so
  `claude --resume` can persist session state without writing through the
  read-only credentials mount.
- **API-key mode** — when `ANTHROPIC_API_KEY` is set on the host: forward
  it via `--env ANTHROPIC_API_KEY` into the container; do **not** mount
  `~/.claude`. Useful for CI and for users who already have an API key.

**[Δ shipped]** The original proposal showed the mount target as
`/root/.claude`. Containers actually run as the non-root `agent` user
(UID 1000); the real target is `/home/agent/.claude`. `select_auth_mode()`
is logged at spawn time as `"Auth: claude.ai OAuth"` or `"Auth: API key"`.

**Threat caveat:** a mounted credentials file means a rogue worker can read
and exfiltrate the OAuth token. Different threat profile than gVisor +
API keys. Mitigations: read-only mount, capability drop, DNS allowlist,
NEVER_MOUNTED_HOST_PATHS guard (see "Internal-registry passthrough" below).

**Concurrency caveat:** claude.ai subscriptions limit concurrent sessions
(typically 1–2 on Pro, more on Team/Max). Parallel workers from
`cycle_review` will serialize. Real cap depends on plan. Documented as a
known limitation; mitigated by the cycle being naturally sequential most
of the time (one tester at a time, one reviewer at a time per unit). Still
open as a measurement question — see "Open questions" below.

## Sandbox model: Docker as the isolation boundary

A locked-down `docker run` invocation, applied to every worker container.
`DockerClaudeCodeWorker.build_docker_argv()` is the deterministic source of
truth; the unit-test suite asserts on its output. Shipped shape (OAuth mode
shown; API-key mode swaps the auth lines):

```bash
docker run --rm \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --read-only \
  --tmpfs=/tmp:size=512M,mode=1777 \
  --tmpfs=/home/agent/.cache:size=512M,mode=0700 \
  --user 1000:1000 \
  --memory=4g --cpus=2 --pids-limit=512 \
  --network=orch-net \
  --dns=127.0.0.1 --dns-search=. \
  --mount type=bind,source=${WORKDIR},target=/workspace \
  --mount type=bind,source=${HOME}/.claude/sessions,target=/home/agent/.claude/sessions \
  --mount type=bind,source=${HOME}/.claude,target=/home/agent/.claude,readonly \
  # auto-mounted registry config files (read-only) when present on host:
  --mount type=bind,source=${HOME}/.npmrc,target=/home/agent/.npmrc,readonly \
  --mount type=bind,source=${HOME}/.pip/pip.conf,target=/home/agent/.pip/pip.conf,readonly \
  --mount type=bind,source=${HOME}/.docker/config.json,target=/home/agent/.docker/config.json,readonly \
  # value-less --env form keeps secrets out of argv (docker reads from the
  # curated subprocess env handed to it by build_subprocess_env):
  --env GITHUB_TOKEN \
  orchestrator/worker:latest \
  claude --output-format json -p "<task>"
```

**[Δ shipped]** The proposal used `-v` shorthand throughout; the shipped
argv uses `--mount type=bind,...` with explicit `readonly` keyword. Both
work, but `--mount` is friendlier to paths with spaces and makes the
read-only intent unambiguous in argv inspection.

**[Δ shipped]** The proposal also listed `--runtime=runsc` as an optional
gVisor isolation layer. v1 does **not** expose this as a flag or env knob.
A user who wants gVisor parity can override Docker's default runtime
system-wide; surfacing it as an orchestrator-level knob is on the backlog.

This isn't *as good as* Managed Agents' default gVisor (host-kernel exploits
still apply without `runsc`), but it's much better than a `LocalWorker` running
unrestricted on the user's filesystem. The capability drop, read-only rootfs,
and tmpfs scratch space mean a compromised worker can scribble in `/workspace`
(intended) and `/tmp` (ephemeral) but can't touch the host.

### Credential boundary (the receipts surface)

Two env contexts to keep distinct — both `build_subprocess_env()` and
`build_cred_audit()` enforce strict whitelist boundaries, but at different
layers:

- **The host-side subprocess env** handed to `subprocess.run(["docker", "run", ...])`
  is a **curated dict**, NOT `os.environ`. It preserves the minimum the
  docker CLI itself needs to function: `PATH`, `HOME`, `LANG`, `LC_ALL`,
  and `DOCKER_HOST` (if set). Plus `GITHUB_TOKEN` always; plus
  `ANTHROPIC_API_KEY` in API-key mode. Everything else from `os.environ`
  is dropped on the floor.
- **The container env** (passed via the `--env <NAME>` flags in the
  docker argv) is a stricter whitelist on top of the subprocess env:
  only `GITHUB_TOKEN` always, plus `ANTHROPIC_API_KEY` in API-key mode,
  cross into the container. `PATH`/`HOME`/`DOCKER_HOST` etc. stay
  host-side; the container has its own minimal env baked into the
  Dockerfile.

The cred-audit reports the container-env boundary (what the worker actually
sees), since that's the trust-relevant surface. `SENSITIVE_ENV_PREFIXES`
(`AWS_`, `SSH_`, `GCP_`, `GOOGLE_`, `AZURE_`, `KUBE`, `DOCKER_`) and
`SENSITIVE_ENV_NAMES` (`OPENAI_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`GITHUB_APP_PRIVATE_KEY*`, `HOME`, `USER`, `PATH`) are the explicit
"audit me — was this on the host but blocked from the container?" list
shown in the audit's "Env vars dropped" section.

`CredAudit.render()` is what `orchestrator doctor` prints — five sections:
env vars passed, env vars dropped (sensitives present on host that did
**not** receive), mounts passed (labelled `(rw)` / `(ro)` / `(auto)` /
`(extra)`), paths **never** mounted (only those that actually exist on
host — receipt-accurate per PR #11 review SUGGESTION 3), and internal
registry hosts added to the DNS allowlist.

## Network policy: replicating the allowlist

Today's `ALLOWED_NETWORK_HOSTS` is enforced kernel-side by Anthropic's network
policy. For Docker workers, the most pragmatic equivalent is **DNS-level
filtering** via a dnsmasq sidecar on `127.0.0.1:5353`. Shipped config
(`orchestrator/network/allowlist.dnsmasq.conf`):

```
# forward each allowed host to a real upstream
server=/github.com/1.1.1.1
server=/api.github.com/1.1.1.1
server=/codeload.github.com/1.1.1.1
server=/objects.githubusercontent.com/1.1.1.1
server=/raw.githubusercontent.com/1.1.1.1
server=/api.anthropic.com/1.1.1.1
server=/pypi.org/1.1.1.1
server=/files.pythonhosted.org/1.1.1.1
server=/registry.npmjs.org/1.1.1.1
# default-deny everything else
address=/#/0.0.0.0
```

**[Δ shipped]** The original proposal showed `address=/host/<resolved-ip>`
with `no-resolv` and an empty `server=`. The shipped model is the inverse:
forward known-allowed hosts to a real upstream (1.1.1.1 by default), and
use `address=/#/0.0.0.0` as the default-deny wildcard. Behaviour is
equivalent at the policy layer (allow named hosts, deny everything else)
but the dnsmasq mechanics differ.

Worker containers launch with `--dns=127.0.0.1` and `--dns-search=.`. Any DNS
query for a non-allowlisted host resolves to `0.0.0.0` → outbound connect fails.

**Soft boundary disclaimer:** bypassable if a worker hits raw IPs. Every
standard agent action (`gh`, `git clone`, `pip install`, `docker pull`)
goes through DNS. For real exfiltration attempts (which typically use named
C2 servers), this works. Documented in `SECURITY.md` "Non-defenses".

### Internal-registry passthrough (the killer side-effect)

**[Δ shipped]** The original proposal framed this as "optional volume
mounts for `~/.npmrc`, `~/.pip/pip.conf`, etc., configurable via .env."
The shipped implementation goes further: `AUTO_MOUNT_REGISTRY_PATHS` —
`~/.npmrc`, `~/.pip/pip.conf`, `~/.docker/config.json` — auto-mounts read-only
**when present on the host**, no flag needed. For everything else, the
user opts in via `ORCH_WORKER_EXTRA_MOUNTS=path1,path2,...`.

Two safety guards:

- **NEVER_MOUNT validator** (`_violates_never_mount`, PR #14 review C4):
  `ORCH_WORKER_EXTRA_MOUNTS` entries are resolved via `Path.resolve()`
  (handles symlinks) and checked against `NEVER_MOUNTED_HOST_PATHS =
  ("~/.ssh", "~/.aws", "~/.config/gcloud", "~/.kube", "~/.gitconfig",
  "~/.git-credentials")`. Equality OR prefix-containment match → `ValueError`
  with a remediation message. A NEVER-list violation is a security failure,
  not a typo; silent-drop felt wrong.
- **Doctor heuristic** (`audit_registry_passthrough_for_repo`): inspects
  the cloned repo's `package.json` `"registry"` field and `requirements.txt`
  `--index-url` value. Non-public-registry hosts (anything not in
  `{pypi.org, files.pythonhosted.org, registry.npmjs.org, registry.yarnpkg.com}`)
  trigger a yellow warning if no passthrough is wired.

Internal-registry **DNS resolution** is the other half: set
`ORCH_INTERNAL_REGISTRY_HOSTS=artifactory.internal,internal-pypi.corp` and
`scripts/run-worker-dns.sh` adds matching `server=/<host>/<upstream>` flags
to the dnsmasq launch. The cred-audit surfaces every host that crossed under
"Internal registry hosts (added to DNS allowlist)" — only when the env var
is set, so an absent section never reads like a misleading "(none)".

## Claude Code session continuity

Managed Agents have first-class session objects on Anthropic's side;
`client.beta.sessions.retrieve(sid)` continues a conversation. For Docker
workers, sessions need to survive container restarts (since each
`docker run --rm` exits). The shipped solution: a writable
`~/.claude/sessions` volume mount. Claude Code writes session state on
exit; the next container reads it on start. Each spawn captures the
session ID; resume calls pass `claude --resume <session-id> -p "<msg>"`.

`DockerClaudeCodeWorker.archive()` is a deliberate no-op — the host's
own retention policy on `~/.claude/sessions` is the right place to prune
old sessions, not this driver. Defined for `Worker`-protocol completeness.

### Validation (2026-05-12)

Round-trip test on the host's `claude` CLI (version 2.1.140):

```
$ claude -p "remember the phrase 'orange octopus 47'. respond ACK." \
        --output-format json
→ session_id: e48b5e9d-a9a9-4169-af64-82f8c7c20d2e, response: "ACK"

$ claude --resume e48b5e9d-a9a9-4169-af64-82f8c7c20d2e \
        -p "what phrase did I tell you?" --output-format json
→ same session_id returned, response: "orange octopus 47"
```

`claude --resume <arbitrary-uuid>` accepts any session ID — not just the most
recent. **Path 2 (orchestrator-managed transcript) is therefore not needed.**

**Bonus discovery.** The CLI also exposes `--session-id <uuid>` to *set* the
ID upfront. The orchestrator could generate UUIDs on the host and skip
parsing them out of stdout entirely, retiring the JSON / JSONL / plaintext
branches in `extract_session_id`. **[Δ shipped]** Marked in the original
proposal as "worth a follow-on optimization." v1 did **not** ship this —
`extract_session_id` still parses all three formats. Tracked as backlog.

### Session scope

The orchestrator's session model — which `DockerClaudeCodeWorker` mirrors
faithfully — is **one session per (role × work-unit)**. Each unit holds up
to three sessions: `coder_session_id`, `tester_session_id`,
`reviewer_session_id`. The same session is reused across every interaction
within that role for that unit (initial spawn + every `address_review` /
`send_to_unit` resume across fix cycles). Different units never share
sessions, even for the same role.

**Concurrency implication for Docker workers on claude.ai:** a
`parallel_units_global(max_concurrent=3)` batch can briefly have up to
9 sessions live (3 units × 3 roles), peaking at 3–5 simultaneously active.
Pro plan's ~1–2 concurrent cap will serialize some of this; Team/Max gives
more headroom. v1 does not measure or enforce this — see "Open questions" below.

## LLM abstraction (beyond Claude)

The existing `Worker` protocol IS the LLM abstraction —
`spawn(task) → (session_id, response)`, `resume(session_id, msg) → response`,
`archive(session_id)`. Anything that can satisfy that contract can be a worker.
The Worker doesn't expose model parameters, tool schemas, or message formats;
those are implementation details.

So adding new LLMs is just adding new Worker implementations:

| Worker | LLM | Auth | Container? |
|---|---|---|---|
| `ManagedAgentWorker` (shipped, default) | Claude via Anthropic Managed Agents | API key | gVisor (Anthropic-managed) |
| `DockerClaudeCodeWorker` (shipped, F-001) | Claude via Claude Code CLI | claude.ai OAuth **or** API key | Docker (user-managed) |
| `DockerAiderWorker` (backlog) | Multi-model via aider (Claude, GPT, Gemini, Llama) | per-model API keys or local | Docker |
| `DockerOpenAICodexWorker` (backlog) | GPT-5/Codex via OpenAI Assistants | OpenAI key | Docker |
| `BedrockClaudeWorker` (backlog) | Claude via AWS Bedrock | AWS creds | AWS-managed |

The factory `make_worker(role)` reads `ORCH_WORKER_BACKEND` and instantiates
the right one. Adding a new LLM = ~150 lines + tests, no changes anywhere else
in the orchestrator. See `BACKLOG.md` entries for sized backlog tasks.

The **agent prompts** (`orchestrator/prompts/coder.md` etc.) are written for
Claude today. They reference Claude's tool-use conventions (`gh`, `bash`,
etc.) and Claude's terminal markers (`PR_URL`, `TESTS_PASS`). They'd need
light re-tuning for a different model family. That's a one-time cost per new
LLM, not a structural problem.

## Configuration knobs

Shipped env-var surface (see `orchestrator/workers/docker_claude_code.py`
for current values + docstrings):

| Env var | Default | Purpose |
|---|---|---|
| `ORCH_WORKER_BACKEND` | `managed_agents` | Which backend the factory instantiates (`managed_agents` or `docker`). |
| `ORCH_DOCKER_WORKER_IMAGE` | `orchestrator/worker:latest` | Override the container image tag (CLI passes through to `cli.py` for `init`/`doctor`). |
| `ORCH_WORKER_EXTRA_MOUNTS` | (unset) | Comma-separated host paths to bind-mount read-only into the container. Rejected if any resolves into `NEVER_MOUNTED_HOST_PATHS`. |
| `ORCH_INTERNAL_REGISTRY_HOSTS` | (unset) | Comma-separated hostnames to add to the dnsmasq allowlist (e.g. `artifactory.internal,internal-pypi.corp`). |
| `ORCH_INTERNAL_REGISTRY_UPSTREAM` | `1.1.1.1` | DNS upstream the sidecar forwards each `ORCH_INTERNAL_REGISTRY_HOSTS` entry to (read by `scripts/run-worker-dns.sh`). |
| `ORCH_WORKER_TIMEOUT_SECONDS` | `1800` (30 min) | Per-spawn / per-resume subprocess timeout. Added in U-6 review (PR #11 reviewer SUGGESTION 1). |
| `ORCH_RUN_E2E` | (unset) | Suite-level opt-in gate for the E2E smoke suite (`tests/e2e/`). CI workflow sets this; local dev runs require explicit opt-in. |
| `ORCH_E2E_CLAUDE_AUTH` | (unset) | Test-level gate within the E2E suite — individual tests needing a real `claude` login (e.g. spawn/resume round-trip) skip cleanly unless this is set. |
| `ORCH_DNSMASQ_BIND` | `127.0.0.1` | Where `scripts/run-worker-dns.sh` binds the sidecar. |
| `ORCH_DNSMASQ_CONFIG` | bundled allowlist path | Override the dnsmasq config file path. |
| `ORCH_DOCKER_NETWORK` | `orch-net` | Bridge network name; the script ensures it exists idempotently. |
| `ANTHROPIC_API_KEY` | (unset) | When set, switches the worker to API-key auth mode (no `~/.claude` mount). |
| `GITHUB_TOKEN` | required | Forwarded into every worker for `gh` / `git push`. |

**[Δ shipped]** The original proposal only named `ORCH_WORKER_BACKEND` and
hinted at an unspecified pip.conf override. The shipped config surface is
materially larger; this table is the authoritative list.

## Trade-offs vs Managed Agents

| Capability | Managed Agents | Docker + claude.ai |
|---|---|---|
| Auth | `sk-ant-` API key | claude.ai OAuth (your subscription) **or** API key |
| Cost | $0.08/session-hour + token costs | Flat (your subscription) — not measured against plan limits |
| Sandbox | gVisor by default | Docker hardened (`--cap-drop=ALL`, `--read-only`, `--user 1000`, `--security-opt=no-new-privileges`, `--memory/cpus/pids-limit` caps); `--runtime=runsc` NOT exposed as a v1 knob |
| Network | Kernel-side allowlist | DNS-level allowlist via dnsmasq sidecar (raw-IP bypassable; documented in SECURITY.md "Non-defenses") |
| Parallelism | High | Limited by claude.ai concurrency (1–2 on Pro, more on Team/Max) |
| Cross-platform | Linux/macOS/Windows | Docker required; CI matrix covers all three. dnsmasq sidecar on Windows not E2E-tested. |
| Cost telemetry | Per-session billing data | None (flat subscription) |
| **Internal package registries** | No | **Yes** — auto-mounted `~/.npmrc`/`~/.pip/pip.conf`/`~/.docker/config.json`, plus `ORCH_WORKER_EXTRA_MOUNTS` + `ORCH_INTERNAL_REGISTRY_HOSTS` |
| Setup | API key in `.env` | Docker daemon, image build, dnsmasq sidecar |
| Cold start per spawn | ~direct API latency (~100ms) | ~500ms–1s container start |
| Image distribution | Anthropic-managed | We maintain `orchestrator/worker` image (~2–3 GB monolithic; modular variant on backlog) |
| Timeout enforcement | Anthropic-side | `ORCH_WORKER_TIMEOUT_SECONDS` (default 30 min) — `subprocess.run(timeout=…)` on spawn/resume |

## What shipped (replaces "Implementation plan")

Original proposal sized this as **5 phases over ~5 days**. Shipped as
**6 units over 2 days** (2026-05-12 → 2026-05-14), total cost ~$2.08 in
session-hours (token costs not measured).

| Unit | PR | Title | Notes |
|---|---|---|---|
| F-001-U-1 | [#1](https://github.com/boctorj/agent-orchestrator/pull/1) | Worker abstraction cleanup | Relocated `ManagedAgentWorker` and extracted the `Worker` protocol. No behavior change. |
| F-001-U-2 | [#11](https://github.com/boctorj/agent-orchestrator/pull/11) | Docker worker MVP — hybrid auth, strict cred boundary | `docker/worker.Dockerfile`, `DockerClaudeCodeWorker`, `build_cred_audit`, `run_doctor_probes` (3 probes initially). |
| F-001-U-3 | [#13](https://github.com/boctorj/agent-orchestrator/pull/13) | dnsmasq sidecar + orch-net probe | `scripts/run-worker-dns.sh`, `allowlist.dnsmasq.conf`, 4th doctor probe (network exists). |
| F-001-U-4 | [#14](https://github.com/boctorj/agent-orchestrator/pull/14) | Internal-registry passthrough (default-on) | `AUTO_MOUNT_REGISTRY_PATHS`, `ORCH_WORKER_EXTRA_MOUNTS`, `ORCH_INTERNAL_REGISTRY_HOSTS`, NEVER_MOUNT guard (PR-review C4), `audit_registry_passthrough_for_repo`. |
| F-001-U-6 | [#16](https://github.com/boctorj/agent-orchestrator/pull/16) | End-to-end smoke test (real Docker) | `tests/e2e/test_docker_worker_smoke.py`, `tests/e2e/conftest.py` (autouse `docker_available` gate), `tests/fixtures/sandbox-repo/`, `ORCH_RUN_E2E=1` suite gate + `ORCH_E2E_CLAUDE_AUTH=1` test-level gate, `ORCH_WORKER_TIMEOUT_SECONDS`, `.github/workflows/e2e-docker.yml`. **Was NOT in the original 5-phase plan** — added during planning to cover what unit tests can't. |
| F-001-U-5 | [#18](https://github.com/boctorj/agent-orchestrator/pull/18) | Polish — init wizard + docs + BACKLOG | `orchestrator init` branches on backend choice, README "Choosing a worker backend" section, BACKLOG entries for sibling Worker implementations. |

Merge order matches dependency order, except U-5 shipped after U-6 because
the docs depend on the final config knob set landing.

## Open questions (post-v1)

1. ~~**Claude Code's `--resume` semantics**~~ **RESOLVED 2026-05-12.** Tested
   via round-trip; `claude --resume <arbitrary-uuid>` works on CLI version
   2.1.140. Path 2 (orchestrator-managed transcript) not needed. See
   "Validation (2026-05-12)" addendum in the session-continuity section above.
2. **claude.ai concurrency limit**: what's the actual cap per plan? **Still
   open.** v1 ships without measurement or runtime gating. Mitigation today
   is that fix-cycle traffic is naturally sequential.
3. **Image size / pull time**: Claude Code + Node + Python + common test deps
   is ~2–3 GB. **Still monolithic in v1**; a modular variant (slim base +
   per-role layers) is on the backlog.
4. **Apple Silicon / Linux / Windows parity**: CI matrix covers all three for
   the unit suite. The E2E smoke suite runs on `ubuntu-latest` only.
   `_expand_with_home` is OS-agnostic via `Path`, but `AUTO_MOUNT_REGISTRY_PATHS`
   encodes Unix conventions (`~/.npmrc` etc.). Windows users wire passthrough
   via `ORCH_WORKER_EXTRA_MOUNTS` pointing at `%APPDATA%/...` paths. dnsmasq
   sidecar on Windows: untested in CI.
5. **Cost of "flat subscription" framing**: claude.ai usage limits exist.
   Heavy orchestrator use might bump against them. **Not measured in v1.**
   README documents the limitation but doesn't enforce a budget.
6. **`--session-id <uuid>` optimization**: deferred from the original
   proposal. Would let the orchestrator generate UUIDs on the host and
   retire the JSON/JSONL/plaintext parsing in `extract_session_id`. On the
   backlog as a low-priority cleanup.
7. **`--runtime=runsc` knob**: a user can override Docker's default runtime
   system-wide today, but the orchestrator doesn't expose it as a per-spawn
   flag. Worth surfacing if a user asks.

## Why this was worth doing

Three things you get that Managed Agents can't deliver today (and the v1
shipped each of them):

1. **No API key billing surprise.** Hobbyists, students, and OSS maintainers
   who already have a Pro/Max plan can just use it. OAuth mode is the default
   when `ANTHROPIC_API_KEY` is unset — zero-config for the common case.
2. **Internal-network access.** The `SelfHostedWorker` BACKLOG entry exists
   exactly because Managed Agents can't reach corporate VPNs / internal
   artifactory. Docker workers solve this without a Kubernetes cluster —
   `AUTO_MOUNT_REGISTRY_PATHS` makes `npm install` / `pip install` /
   `docker pull` against internal registries "just work" out of the box.
3. **LLM portability.** Once the worker is "spawn a container that runs an
   agentic CLI", swapping `claude` for `aider`, `codex`, or `gemini-cli` is a
   Dockerfile change. The orchestrator's state machine, MCP tools,
   verification gate, CI gate — none of it cares. Sized backlog entries
   in `BACKLOG.md` for each.

The trade-offs are real (concurrency, slightly weaker sandbox without runsc,
no per-session cost telemetry). For sandbox / personal / OSS use, those are
acceptable. For enterprise repos requiring strong isolation guarantees,
Managed Agents stays the right choice — but the orchestrator now supports
both, and the user picks at install time.

## Lessons learned (post-v1)

Surfacing two recurring patterns that came out of the F-001 cycle review
loop, worth filing for future feature work:

- **State-desync on spawn timeout.** When `spawn_unit` errors after the
  coder agent reaches GitHub but before the orchestrator records the
  session id (e.g. read-timeout mid-spawn), the PR exists but the unit's
  `coder_session_id` is empty — `address_review` and `cycle_review` then
  refuse to advance the unit. Workaround: manual state.db patch
  (`UPDATE work_units SET pr_number=...`) or bypass-via-worktree. Real
  fix on the backlog: detect orphan PRs in `check_unit_pr` and offer an
  adopt path via `reconcile_unit_pr`.
- **Coder-session silent failure.** Twice during F-001 (U-5 specifically),
  `address_review` returned empty output with no FIX_PUSHED/BLOCKED marker,
  burning cycle counters. Cause unclear (possibly upstream Anthropic
  Managed Agents transient). Backlog item: session-health probe before
  burning a cycle.

Both are tracked in `BACKLOG.md` as orchestrator-self-improvement tasks.

## Related BACKLOG entries

- `LocalWorker (self-driven loop on user infra)` — this proposal supersedes
  it; LocalWorker was the naïve "subprocess on host" version that this
  improves on with Docker sandboxing.
- `Self-hosted Worker backend (enterprise enabler)` — separate concern;
  that's about running workers in an enterprise's own infra (K8s/ECS/Modal)
  to reach internal services at scale. Docker workers solve the laptop
  variant of the same problem.
- `DockerAiderWorker`, `DockerOpenAICodexWorker`, `BedrockClaudeWorker` —
  sibling Worker implementations on the same `~150 LOC + tests` template.
- `--session-id <uuid> optimization` — retire stdout parsing in
  `extract_session_id`.
- `Modular worker image variants` — slim base + per-role layers to bring
  pull time down from the current ~2–3 GB monolithic image.
