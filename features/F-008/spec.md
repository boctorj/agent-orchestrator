# F-008: Worker observability — `tail_worker(unit_id, role)`

> Backfilled retrospectively on 2026-05-18 — F-008-U-1 (PR #29) shipped before this spec.md existed.

## Intent

Add a way for the lead to peek at what worker agents are actually doing mid-flight. Today the only signals between "spawned" and "terminal marker emitted" are session-status (idle / running / terminated / not_found via `resume_unit`) and silence. A spawn that blocks for 10+ minutes is opaque — the lead can't tell whether the agent is making progress, stuck on a bad path, or dead.

**Proactive observability investment**, not driven by a specific incident. Closes the visibility gap before it bites us during a long-running spawn or a debugging session on a flaky agent.

**Who benefits:** the lead persona (faster debugging when a spawn stalls), and indirectly the user (escalations include actionable context — "agent's been stuck on X for N minutes" beats "agent isn't responding").

## Acceptance

- The lead can call `tail_worker(unit_id, role)` while the named agent is mid-session and see the last ~20 messages it has emitted.
- If the agent's session has died, the tool says so explicitly via a `terminated` status — the lead doesn't have to guess between "still working" and "crashed."
- Calling `tail_worker` is **read-only** — it doesn't perturb the agent's session, doesn't write to state.db, doesn't trigger any side effects.
- All four status paths (`running` / `idle` / `terminated` / `not_found`) implemented and tested on **managed_agents**.
- Three of four status paths (`idle` / `terminated` / `not_found`) implemented and tested on **docker**. The `running` path on docker is a documented limitation pending a follow-up unit (see Open questions).
- Output is **status-aware**:
  - `running` → "worker active, last N messages"
  - `idle` → "worker completed, final messages"
  - `terminated` → "worker dead (reason); last messages before death"
  - `not_found` → "no session for unit_id/role — likely never spawned"

## Out of scope

- **Live streaming** (WebSocket, SSE) — `tail_worker` is poll-on-demand.
- **Push notifications on agent progress** (e.g. ntfy on every status flip) — separate concern; this is a *pull* surface.
- **Multi-source aggregation** (combining PR comments + session messages + CI logs into one view) — bigger system; out of scope.
- **Background daemon polling** — the lead initiates every read.
- **Anthropic API rate-limiting handling** — the SDK / Anthropic backend owns throttling; this tool just calls the API normally.

## Approach

Two-layer split:

**U-1 — Backend abstraction.** Add `tail_messages(session_id, *, limit=50)` to the `Worker` protocol, returning `{status, messages: list[{ts, role, text}], reason: str | None}`. Per-backend implementation:
- `managed_agents`: `sessions.retrieve()` + `events.list(types=['agent.message'], order='desc')` filtered to assistant/agent messages.
- `docker`: `docker inspect <container>` for state mapping (running / created / exited / dead / removing / paused / restarting → tail status taxonomy), then read JSONL from the container's transcript file, filtered to assistant messages only.
- Shared `_validate_limit` helper in `base.py` so both backends raise the same `ValueError` on `limit < 1`.

**U-2 — MCP tool layer.** `tail_worker(unit_id, role, limit=20)` resolves the unit row to the right `session_id`, calls `backend.tail_messages`, formats output per the status-aware messages above. Default `limit=20` at the MCP layer; backend protocol supports higher values for power-callers.

## Constraints

- **Read-only.** No side effects on the agent's session, no state.db writes, no event records. Safe to call from anywhere — dashboards, diagnostics, monitors.
- **Status taxonomy must match `resume_unit`'s** four-value enum (running / idle / terminated / not_found). Consistency lets the lead reason about both tools the same way.
- **Cross-backend role vocabulary.** Both backends filter to assistant/agent messages before emitting; callers can rely on a stable role set without per-backend handling. Documented on the `TailMessage` docstring.
- **Fail-safe on docker `inspect` errors.** Daemon down / permission denied / timeout surfaces as `terminated` with `reason='docker inspect failed: <detail>'`, not silently masquerading as `not_found`. Programmer errors still propagate.
- **Unknown managed-agents SDK statuses default to `running`.** Caller won't accidentally archive an in-flight session because of SDK status drift; `reason='session.status=<raw>'` populated so observability survives the drift.

## Decisions

**Two-layer split (backend abstraction + thin MCP tool).**
**Why:** the worker backend protocol is the right home for the implementation differences (managed_agents API vs docker filesystem); the MCP tool layer owns the user-facing formatting and the unit_id → session_id resolution. Keeps backend changes (e.g. a future third backend) from touching the MCP surface.

**Default `limit=20` at the MCP layer; backend supports more.**
**Why:** 20 is the right default for a "show me recent context" call — cheaper, faster, fits in a chat reply. The backend protocol's higher default (50) is the technical ceiling; the MCP tool's default is the ergonomic floor. Callers needing more can override.

**Status-aware formatting in the MCP tool, not the backend.**
**Why:** the backend returns structured data (`{status, messages, reason}`) so it's reusable; the MCP tool decides how to phrase the output for the lead's read. Keeps the backend independently testable without coupling to chat-friendly strings.

**Fail-safe error mapping on docker, not propagation.**
**Why:** a daemon-down / permission-denied error during a `tail_worker` call shouldn't bubble up as an exception to the lead — it's a recoverable observability degradation, not a fatal error. Surfacing it as `terminated` + a `reason` field gives the lead the info to act on without crashing the chat flow.

**Pull-only surface, not push.**
**Why:** ntfy / WebSocket push pipelines were considered and rejected — they introduce daemon lifecycle, message-loss recovery, and ordering concerns the lead doesn't need today. A poll-on-demand MCP tool with status-aware formatting answers the actual question ("what is the agent doing right now?") without the supporting infrastructure.

**JSONL filter requires assistant/agent role on docker.**
**Why:** without the filter, docker's `_extract_tail_message` would return user-prompt records, tool turns, and system messages — the noisy raw transcript. Filtering to `_ASSISTANT_OUTER_TYPES` / `_ASSISTANT_ROLES` matches the managed_agents `types=['agent.message']` filter so both backends emit the same shape. (PR #29 reviewer C1 finding; addressed in cycle-1 fix `3831619c`.)

## Open questions

- **Docker `running` path unreachability.** The `running` branch in `docker.tail_messages` is unreachable in production today because `spawn()` doesn't pass `--name <session_id>` to the container — `docker inspect <session_id>` returns `No such object`. Of docker's four status paths, only `idle` and `not_found` are reachable until a follow-up unit lands the `--name` wiring. The test pinning the convention for that follow-up (`test_docker_inspect_called_with_session_id_as_container_name`) is in place. **Open:** when does the `--name` wiring land? Either a new unit in F-008, folded into the next docker-backend refactor, or shipped opportunistically with the F-009 state-machine work.
- **JSONL streaming for long transcripts.** `_read_session_messages` reads the full JSONL into memory then takes the last `limit`. For multi-MB transcripts this is O(transcript), not O(limit). Deferred to a perf-pass if profiles show a real hotspot. (PR #29 Copilot observation; deferred.)
- **`_runs` registry overlap with F-007.** F-007's `_runs` registry tracks long-running ultrareview subprocesses; F-008's `tail_worker` doesn't have its own registry but opens short-lived `docker inspect` calls. No conflict today; flag in case a future feature wants a unified worker-observation surface.
- **Cross-feature: F-008-U-2's status-aware formatting** assumes the four `tail_status` values are the right taxonomy for the lead's chat output. Open until U-2 ships and we see actual usage — may need a fifth state (e.g. `unknown` distinct from `not_found`) once the docker `running` path lands.

## References

- `orchestrator/agents.py` — `Worker` protocol; `tail_messages` lives here.
- `orchestrator/workers/managed_agent.py` — managed_agents backend `tail_messages` implementation.
- `orchestrator/workers/docker_claude_code.py` — docker backend `tail_messages` implementation; includes the docker state-map and assistant-only JSONL filter.
- `orchestrator/workers/base.py` — `_validate_limit` shared helper; `TailMessage` / `TailResult` dataclasses.
- PR #29 (F-008-U-1, awaiting merge as of 2026-05-18) — backend abstraction.
