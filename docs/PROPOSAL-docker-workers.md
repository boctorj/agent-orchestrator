# Proposal: Docker workers + claude.ai auth + LLM abstraction

**Status:** exploratory — not yet committed to v1 roadmap. Captured here so we
don't have to re-derive it later.

**Last updated:** 2026-05-12

---

## Goal

Run the orchestrator end-to-end **without an Anthropic API key**, on
infrastructure the user controls (Docker), with a clean seam for swapping in
different LLMs later. Workers execute on the user's laptop inside locked-down
containers; auth piggybacks on the same claude.ai subscription the lead already
uses.

## The shape of the change

The orchestrator already has the right abstraction: the `Worker` protocol in
`orchestrator/agents.py`. Today there's one implementation
(`ManagedAgentWorker`) targeting Anthropic's Managed Agents API. This proposal
adds a second:

```
orchestrator/
  workers/
    __init__.py            ← `make_worker(role)` factory; picks backend from env
    base.py                ← Worker protocol (moved from agents.py)
    managed_agent.py       ← existing ManagedAgentWorker (relocated)
    docker_claude_code.py  ← NEW
  network/
    allowlist.dnsmasq.conf ← outbound DNS allowlist for worker containers
docker/
  worker.Dockerfile        ← claude + git + gh + standard tooling
scripts/
  build-worker-image.sh
  run-worker-dns.sh        ← starts dnsmasq on 127.0.0.1:5353
```

The factory reads `ORCH_WORKER_BACKEND=managed_agents|docker` from `.env`.
Everything downstream — `cycle_review`, the gate logic, the prompts, the
verification cache, the dashboard — sees an opaque `Worker` interface and
doesn't care which implementation answered.

## Auth: how claude.ai flows into containers

Claude Code stores OAuth credentials in `~/.claude/credentials.json` (and
related files). The Docker worker:

1. Mounts `~/.claude` **read-only** into each container:
   `-v ~/.claude:/root/.claude:ro`
2. Launches `claude -p "<task>"` inside the container
3. Claude Code finds the creds, authenticates as the user, runs on the user's
   subscription

No API key needed anywhere. The same identity that runs the lead now also runs
every worker.

**Threat caveat:** a mounted credentials file means a rogue worker can read and
exfiltrate the OAuth token. Different threat profile than gVisor + API keys.
Mitigations below (network allowlist, capability drop, read-only mount).

**Concurrency caveat:** claude.ai subscriptions limit concurrent sessions
(typically 1–2 on Pro, more on Team/Max). Parallel workers from `cycle_review`
will serialize. Real cap depends on plan. Documented as a known limitation;
mitigated by the cycle being naturally sequential most of the time (one tester
at a time, one reviewer at a time per unit).

## Sandbox model: Docker as the isolation boundary

A locked-down `docker run` invocation, applied to every worker container:

```bash
docker run --rm \
  --name "orch-${role}-${session_id}" \
  --user 1000:1000 \                          # non-root inside container
  --cap-drop=ALL \                             # drop every capability
  --security-opt=no-new-privileges \
  --read-only \                                # rootfs is read-only
  --tmpfs /tmp:size=512M \                     # writable tmpfs for /tmp
  --tmpfs /home/agent/.cache:size=512M \       # writable for pip/npm caches
  -v "${WORKDIR}:/workspace" \                 # one writable mount: the workspace
  -v "${HOME}/.claude:/home/agent/.claude:ro" \   # creds read-only
  --network=orch-net \                          # custom bridge
  --dns=127.0.0.1 --dns-search=. \             # forced through allowlist resolver
  --memory=4g --cpus=2 \                       # resource limits
  --pids-limit=512 \
  orchestrator/worker:latest \
  claude -p "<task>"
```

Optionally, if the user has `runsc` installed: `--runtime=runsc` adds gVisor's
userspace-kernel isolation on top of Docker's namespacing — essentially closes
the gap with Managed Agents.

This isn't *as good as* Managed Agents' default gVisor (host-kernel exploits
still apply without `runsc`), but it's much better than a `LocalWorker` running
unrestricted on the user's filesystem. The capability drop, read-only rootfs,
and tmpfs scratch space mean a compromised worker can scribble in `/workspace`
(intended) and `/tmp` (ephemeral) but can't touch the host.

## Network policy: replicating the allowlist

Today's `ALLOWED_NETWORK_HOSTS` is enforced kernel-side by Anthropic's network
policy. For Docker workers, the most pragmatic equivalent is **DNS-level
filtering**:

1. The orchestrator launches a local dnsmasq on `127.0.0.1:5353` from
   `network/allowlist.dnsmasq.conf`:
   ```
   address=/github.com/<resolved-ip>
   address=/api.github.com/<resolved-ip>
   address=/raw.githubusercontent.com/<resolved-ip>
   address=/pypi.org/<resolved-ip>
   address=/files.pythonhosted.org/<resolved-ip>
   address=/registry.npmjs.org/<resolved-ip>
   ... (mirror of ALLOWED_NETWORK_HOSTS + package managers)
   no-resolv
   server=  # empty upstream — no recursive resolution
   ```
2. Worker containers launch with `--dns=127.0.0.1`, `--dns-search=.`
3. Any DNS query for a non-allowlisted host returns NXDOMAIN → the worker can't
   reach it

Bypassable if a worker hits raw IPs, but every standard agent action (`gh`,
`git clone`, `pip install`) goes through DNS. For real exfiltration attempts
(which use named C2 servers), this works.

The killer side-effect: **internal package registries work**. Mount the user's
`~/.npmrc`, `~/.pip/pip.conf`, `~/.docker/config.json` into the container and
the worker can pull from internal artifactory, internal PyPI, etc. This is the
same capability the `SelfHostedWorker` backlog item targeted, but achievable on
the user's laptop without a Kubernetes cluster.

## Claude Code session continuity

Managed Agents have first-class session objects on Anthropic's side;
`client.beta.sessions.retrieve(sid)` continues a conversation. For Docker
workers, sessions need to survive container restarts (since each
`docker run --rm` exits). Two paths:

1. **Volume-mounted session store.** Mount
   `~/.claude/sessions:/home/agent/.claude/sessions` (writable, NOT read-only —
   sessions go in here). Claude Code writes session state on exit; the next
   container reads it on start. Each spawn captures the session ID; resume
   calls pass `claude --resume <session-id> -p "<msg>"`.

2. **Orchestrator-managed transcript.** The orchestrator stores conversation
   history in `state.db`, prepends it to each new container invocation.
   Simpler in implementation, lossier in semantics (the model doesn't see
   exactly the same context Claude Code would have remembered).

Path 1 is cleaner — same session semantics as Managed Agents. Requires Claude
Code's `--resume` flag to exist and accept an arbitrary session ID. Path 2 is
the fallback if that doesn't work.

### Validation (2026-05-12)

Path 1 confirmed working end-to-end with a round-trip test on the host's
`claude` CLI (version 2.1.140):

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
branches in `extract_session_id`. Worth a follow-on optimization (fold into
U-6 or file as a follow-up).

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
more headroom. Stress-test this in U-6 before claiming production parity
with Managed Agents on parallelism.

## LLM abstraction (beyond Claude)

The existing `Worker` protocol IS the LLM abstraction —
`spawn(task) → (session_id, response)`, `resume(session_id, msg) → response`,
`archive(session_id)`. Anything that can satisfy that contract can be a worker.
The Worker doesn't expose model parameters, tool schemas, or message formats;
those are implementation details.

So adding new LLMs is just adding new Worker implementations:

| Worker | LLM | Auth | Container? |
|---|---|---|---|
| `ManagedAgentWorker` (exists) | Claude via Anthropic Managed Agents | API key | gVisor (Anthropic-managed) |
| `DockerClaudeCodeWorker` (this proposal) | Claude via Claude Code CLI | claude.ai OAuth | Docker (user-managed) |
| `DockerAiderWorker` (future) | Multi-model via aider (Claude, GPT, Gemini, Llama) | per-model API keys or local | Docker |
| `DockerOpenAICodexWorker` (future) | GPT-5/Codex via OpenAI Assistants | OpenAI key | Docker |
| `BedrockClaudeWorker` (BACKLOG) | Claude via AWS Bedrock | AWS creds | AWS-managed |

The factory `make_worker(role)` reads `ORCH_WORKER_BACKEND` and instantiates
the right one. Adding a new LLM = ~150 lines + tests, no changes anywhere else
in the orchestrator.

The **agent prompts** (`orchestrator/prompts/coder.md` etc.) are written for
Claude today. They reference Claude's tool-use conventions (`gh`, `bash`,
etc.) and Claude's terminal markers (`PR_URL`, `TESTS_PASS`). They'd need
light re-tuning for a different model family. That's a one-time cost per new
LLM, not a structural problem.

## Trade-offs vs Managed Agents

| Capability | Managed Agents | Docker + claude.ai |
|---|---|---|
| Auth | `sk-ant-` API key | claude.ai OAuth (your subscription) |
| Cost | $0.08/session-hour + token costs | Flat (your subscription) |
| Sandbox | gVisor by default | Docker hardened; `--runtime=runsc` for gVisor parity |
| Network | Kernel-side allowlist | DNS-level allowlist (slightly weaker — raw-IP bypassable) |
| Parallelism | High | Limited by claude.ai concurrency (1–2 on Pro, more on Team/Max) |
| Cross-platform | Linux/macOS/Windows | Docker required; same OS support but adds the Docker dep |
| Cost telemetry | Per-session billing data | None (flat subscription) |
| **Internal package registries** | No | **Yes** — mount the user's npmrc/pip.conf |
| Setup | API key in `.env` | Docker daemon, image build, dnsmasq config |
| Cold start per spawn | ~direct API latency (~100ms) | ~500ms–1s container start |
| Image distribution | Anthropic-managed | We maintain `orchestrator/worker` image |

## Implementation plan

Rough ordering (each step shippable on its own):

### Phase 1 — Worker abstraction cleanup (~half day)
Move `ManagedAgentWorker` to `orchestrator/workers/managed_agent.py`. Extract
`Worker` protocol into `workers/base.py`. Add `make_worker(role)` factory
reading `ORCH_WORKER_BACKEND` (default: `managed_agents`). No behavior change.

### Phase 2 — Docker worker MVP (~2 days)
- `docker/worker.Dockerfile` building `orchestrator/worker:latest` (Python
  3.12 + git + gh + claude + common test deps)
- `orchestrator/workers/docker_claude_code.py` implementing `Worker` via
  `subprocess.run("docker", "run", ...)`
- Session store via volume mount on `~/.claude/sessions`
- Doctor check: Docker daemon reachable, image built, `claude --version` works
- Tests: subprocess mocked, verify the right `docker run` flags are emitted

### Phase 3 — Network allowlist (~1 day)
- `scripts/run-worker-dns.sh` starts dnsmasq sidecar
- `network/allowlist.dnsmasq.conf` mirrors `ALLOWED_NETWORK_HOSTS`
- Worker containers launch with `--dns=127.0.0.1`
- Integration test: from a worker container, `curl github.com` succeeds,
  `curl evil.com` fails

### Phase 4 — Internal-registry support (~half day)
Optional volume mounts for `~/.npmrc`, `~/.pip/pip.conf`, etc. configurable
via `.env`. Doctor check warns when no internal-registry config is detected
on a repo that looks like it needs one (`package.json` with `"registry"`
field, etc.).

### Phase 5 — Polish (~1 day)
- `orchestrator init` wizard asks "Managed Agents or Docker workers?" and
  configures accordingly
- README + ARCHITECTURE.md gain a "Choosing a worker backend" section
- BACKLOG entries for the next LLMs (aider, Codex, Gemini) reference the
  abstraction

**Total: ~5 days for a polished v1.**

## Open questions

1. ~~**Claude Code's `--resume` semantics**~~ **RESOLVED 2026-05-12.** Tested
   via round-trip; `claude --resume <arbitrary-uuid>` works on CLI version
   2.1.140. Path 2 (orchestrator-managed transcript) not needed. See
   "Validation (2026-05-12)" addendum in the session-continuity section above.
2. **claude.ai concurrency limit**: what's the actual cap per plan? Need to
   test, then either gate `parallel_units` accordingly or document the limit.
3. **Image size / pull time**: Claude Code + Node + Python + common test deps
   is probably ~2–3 GB. Acceptable for a one-time pull; might want to make
   the image modular.
4. **Apple Silicon / Linux / Windows parity**: Docker Desktop on macOS /
   Windows is fine for development; Linux native is faster. Need to test the
   dnsmasq sidecar on each host OS (Windows has its own networking quirks).
5. **Cost of "flat subscription" framing**: claude.ai usage limits exist.
   Heavy orchestrator use might bump against them faster than API usage
   would. Worth measuring before the README claims "free."

## Why this is worth doing

Three things you get that Managed Agents can't deliver today:

1. **No API key billing surprise.** Hobbyists, students, and OSS maintainers
   who already have a Pro/Max plan can just use it. Removes the biggest
   onboarding friction.
2. **Internal-network access.** The `SelfHostedWorker` BACKLOG entry exists
   exactly because Managed Agents can't reach corporate VPNs / internal
   artifactory. Docker workers solve this without a Kubernetes cluster — the
   user's laptop already has the network access.
3. **LLM portability.** Once the worker is "spawn a container that runs an
   agentic CLI", swapping `claude` for `aider`, `codex`, or `gemini-cli` is a
   Dockerfile change. The orchestrator's state machine, MCP tools,
   verification gate, CI gate — none of it cares.

The trade-offs are real (concurrency, slightly weaker sandbox without runsc,
no per-session cost telemetry). For sandbox / personal / OSS use, those are
acceptable. For enterprise repos requiring strong isolation guarantees,
Managed Agents stays the right choice — but the orchestrator now supports
both, and the user picks at install time.

## Status / next step

This is captured for later exploration, not on the v1 roadmap. To pick it up:

1. Re-read this doc.
2. Run a feasibility spike on the open questions (especially #1 — Claude
   Code's `--resume` semantics).
3. Move Phase 1 from this doc into `BACKLOG.md` as a sized task and start
   there; the rest stays as design until Phase 1 lands cleanly.

Related BACKLOG entries:
- `LocalWorker (self-driven loop on user infra)` — this proposal supersedes
  it; LocalWorker was the naïve "subprocess on host" version that this
  improves on with Docker sandboxing
- `Self-hosted Worker backend (enterprise enabler)` — separate concern;
  that's about running workers in an enterprise's own infra (K8s/ECS/Modal)
  to reach internal services at scale. Docker workers solve the laptop
  variant of the same problem.
