# Watcher daemon — operations guide

The F-016 watcher daemon is a long-lived background process that drives
the orchestrator's state machine without the chat in the loop. It's the
"watch" half of the dispatcher/watcher split (see
[`PROPOSAL-async-orchestrator.md`](PROPOSAL-async-orchestrator.md)).
This document covers operations: how to start / stop / inspect it, where
its logs go, and how the credential-shadowing diagnostic works.

For the *design* — why a daemon exists, why it's a separate process,
why singleton enforcement is SQLite-backed instead of pidfile-based —
read the proposal. This file is about running it.

## TL;DR

- **One daemon per workspace.** Keyed by the absolute path to
  `state.db`. A second `orchestrator daemon start` against the same
  workspace exits 3 (lock-held).
- **Opt-in.** Set `ORCH_DAEMON_DRIVE=true` in `.env`. Without it the
  daemon refuses to claim the lock and exits 2.
- **Started automatically by `orchestrator run`** when
  `ORCH_DAEMON_DRIVE=true` — no separate terminal needed.
- **Lives past the chat.** Spawned as a detached child via
  `subprocess.Popen(..., start_new_session=True)` so a killed lead
  doesn't kill the watcher.
- **Logs to `daemon.log`** in the workspace root.
- **Stop it with `orchestrator daemon stop`** (SIGTERM → 10 s wait →
  SIGKILL fallback).

## Starting the daemon

### Recommended: auto-bootstrap via `orchestrator run`

```bash
# .env
ORCH_DAEMON_DRIVE=true
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=github_pat_...
NTFY_TOPIC=agent-orch-XXXX
```

```bash
$ orchestrator run
daemon started (pid 41213, log: daemon.log)
launching: claude --remote-control
```

`orchestrator run` reads `ORCH_DAEMON_DRIVE` from `.env` (or the shell
env if you exported it there), and if truthy:

1. Calls `state.get_daemon_lock(<resolved state.db path>)` to check
   whether a daemon is already running. If so, the banner says
   `daemon already running (started at <T>)` and no new process is
   spawned.
2. Otherwise `subprocess.Popen([python, "-m", "orchestrator.daemon"],
   start_new_session=True)` with stdout/stderr redirected to
   `daemon.log`.
3. Polls `daemon_locks` for up to 5 s waiting for the new daemon to
   claim the row. If the lock lands, the banner is `daemon started
   (pid X, log: daemon.log)`. If it doesn't land within the budget,
   the banner says so but the chat boots anyway — the daemon spawn is
   a courtesy, not a hard prerequisite.

The detached child survives the chat session's death by design. If you
quit Claude Code, the daemon keeps ticking and the orchestrator's
in-flight units keep advancing.

### Manual start (operator-controlled supervisor)

For systemd / launchd setups, run the daemon directly in the
foreground:

```bash
orchestrator daemon start
```

Exit codes:

| Code | Meaning | What to do |
|---|---|---|
| 0 | Clean shutdown (SIGINT / SIGTERM after some ticks, or a no-op exit because nothing was actionable) | Normal |
| 2 | `ORCH_DAEMON_DRIVE` is unset / falsy | Set it in `.env` and re-run; do NOT retry without operator intervention |
| 3 | Another daemon already owns this workspace's `state.db` lock | Configuration error, not a transient. Find the other instance and stop it |
| 4 | `ANTHROPIC_API_KEY` failed the format check (F-016-U-7) | See "Credential shadowing" below |

A systemd service file would set `Restart=on-failure` and treat exit 2
/ 3 / 4 as "stop trying" via `RestartPreventExitStatus=2 3 4`.

## Inspecting the daemon

```bash
$ orchestrator daemon status
{
  "state_db_path": "/Users/joe/work/agent-orchestrator/state.db",
  "holder_id": "8a3c7f9b4d2e...",
  "heartbeat_at": "2026-06-08T14:32:11.412+00:00",
  "started_at": "2026-06-08T14:15:03.001+00:00",
  "pid": 41213
}
```

Read-only — does not claim, heartbeat, or release the lock. Safe to
call from a second terminal while the daemon is running.

**No lock:**

```bash
$ orchestrator daemon status
No daemon lock for /Users/joe/work/agent-orchestrator/state.db
```

The dashboard (`orchestrator dashboard` or `./scripts/dashboard.sh`)
shows the daemon's heartbeat alongside the in-flight units — useful for
"is the daemon still ticking?" debugging without restarting it.

## Stopping the daemon

```bash
$ orchestrator daemon stop
Sending SIGTERM to daemon pid 41213…
✓ daemon stopped
```

The command reads `daemon_locks` for the workspace, sends SIGTERM to
the holder's PID, polls for the lock row to vanish for up to 10 s, and
falls back to SIGKILL if the daemon ignored SIGTERM.

Exit codes (per spec):

| Code | Meaning |
|---|---|
| 0 | Stopped cleanly (lock row gone) |
| 1 | No daemon running, OR the lock row has no recorded PID (pre-F-016-U-7 daemon — kill it manually and remove the row) |
| 2 | Daemon ignored both SIGTERM and SIGKILL within the window, or a permission error blocked the signal |

## What happens on a stale lock takeover

Daemons crash. Force-quit, OOM kill, host reboot, sandbox restart — any
of these leaves the `daemon_locks` row in place but the heartbeat
freezes. The takeover semantics:

1. The fresh daemon's first `claim_daemon_lock` checks the existing
   row's `heartbeat_at`.
2. If the heartbeat is older than `DEFAULT_DAEMON_LOCK_STALE_AFTER_S`
   (30 s — six missed ticks at the 5 s default poll interval), the
   row is takeover-eligible.
3. The fresh daemon CASes the row (`holder_id = ?, heartbeat_at = ?,
   started_at = ?` WHERE the prior `holder_id` matches) so two
   concurrent takeovers can't both win.
4. The reconcile loop runs as normal — level-triggered, idempotent
   transitions, no special recovery path. Phase 0's
   `unit_events.dedupe_key` UNIQUE constraint dedupes any markers the
   crashed instance already recorded; the F-014 probe re-derives
   from the current PR state regardless.

**Corrupted heartbeat (clock skew, hand-edit, disk corruption):** the
state layer refuses takeover when `heartbeat_at` can't be parsed
(`state.claim_daemon_lock` returns False unconditionally for the
unparseable case so a new daemon can't silently snipe the active row).
Manual recovery:

```bash
sqlite3 state.db "DELETE FROM daemon_locks WHERE state_db_path = '/abs/path/state.db';"
orchestrator daemon start    # or restart orchestrator run
```

The `orchestrator daemon status` JSON shows the `heartbeat_at` value,
so you can confirm the corruption before deleting.

## Env vars that control behavior

| Env var | Default | What it does |
|---|---|---|
| `ORCH_DAEMON_DRIVE` | unset (off) | Truthy values (`true` / `1` / `yes` / `on`, case-insensitive) opt the daemon in. Without it `daemon start` exits 2 and `orchestrator run` skips the auto-spawn |
| `ORCH_DAEMON_POLL_INTERVAL_S` | `5.0` | Seconds between reconcile ticks. Floor 0.1 s. Non-finite values (`nan` / `inf`) fall back to the default |
| `NTFY_TOPIC` | unset | Phone push notifications for escalations + ready-to-merge events. When set AND the daemon is running, `cycle_review` defaults to async (≤2 s handoff to the daemon); without it `cycle_review` stays blocking |
| `ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS` | `24` | Per-unit `health_report_snapshot` cadence. The F-014 probe always runs; this knob throttles only the forensics snapshot write |

## Credential shadowing — the F-016-U-7 foot-gun

A surprisingly common setup failure: the operator writes a real
`ANTHROPIC_API_KEY=sk-ant-...` into `.env` but their shell rc file
still has a stale `export ANTHROPIC_API_KEY='lkj'` from an earlier
experiment. The shell env wins (python-dotenv's `override=False`
default keeps explicit shell exports), every `spawn_unit` returns an
opaque Anthropic 401 minutes later, and the user can't tell which
source is wrong.

The F-016-U-7 hardening:

1. **`orchestrator run`** validates the resolved `ANTHROPIC_API_KEY`
   shape (must start with `sk-ant-`) before exec'ing Claude Code.
   On failure: refuses to start with a diagnostic naming
   `~/.zshrc` / `~/.bashrc` / `~/.zprofile` as the likely shadow.
2. **`orchestrator daemon start`** does the same check before
   claiming the lock. Exit code 4 distinguishes credential failures
   from drive-disabled (2) and lock-held (3).
3. **The MCP server** (the subprocess Claude Code spawns from
   `.mcp.json`) does the same check on boot. Without it, a bad key
   slipping past the CLI guard would still produce opaque worker
   errors.
4. **`orchestrator doctor`** runs a positive shadowing audit:
   compares the parent-process env (pre-dotenv) against the parsed
   `.env` values and surfaces every orchestrator-relevant key whose
   values diverge. The line reads:

   ```
   ✗ shadowing: ANTHROPIC_API_KEY — shell=lkj…  vs .env=sk-ant-…;
     unset the stale shell export or align the two values.
   ```

5. **`orchestrator run`** also strips both `ANTHROPIC_API_KEY` AND
   `ANTHROPIC_AUTH_TOKEN` from the env passed to Claude Code, so the
   MCP server receives credentials only via `.env`. A stale OAuth
   token in the parent shell can't shadow the API key flow we just
   validated.

The fix is always one of:
- Remove the stale `export` line from your shell rc, OR
- Sync the two values so they match.

`orchestrator doctor` is the canonical way to check.

## Logs

`daemon.log` lives in the workspace root (next to `state.db`). It
contains stderr from `orchestrator daemon start` (the default
logger emits `HH:MM:SS daemon: <message>` lines) plus any stack
traces from a `tick raised; continuing` catch-all. Rotation is the
operator's responsibility — for a long-running setup, wire `logrotate`
or equivalent. The daemon doesn't open the file itself; the parent
process spawning it (`orchestrator run`) attaches it via
`subprocess.Popen(stdout=..., stderr=...)`.

To re-attach to the running daemon's output:

```bash
tail -f daemon.log
```

## Troubleshooting

### `orchestrator daemon stop` says "no recorded pid"

Pre-F-016-U-7 daemons didn't write a PID into `daemon_locks`. The stop
command refuses to nuke the row blindly. Find and kill the daemon
manually:

```bash
ps aux | grep "orchestrator.daemon"     # find the PID
kill <pid>                              # SIGTERM
sqlite3 state.db "DELETE FROM daemon_locks WHERE state_db_path = '<path>';"
```

Then restart with a current orchestrator build.

### `cycle_review` keeps staying blocking even though `NTFY_TOPIC` is set

The Phase 4 dispatcher routes to async only when the daemon is alive.
`orchestrator daemon status` will show the lock row missing or stale.
Either:

- The daemon never started — check `orchestrator run` output for
  `daemon started (pid X)`; the bootstrap may have failed silently if
  `daemon.log` couldn't be opened.
- The daemon crashed — look at the bottom of `daemon.log` for a stack
  trace.
- `ORCH_DAEMON_DRIVE` is unset — the daemon refuses to claim the lock
  without the opt-in.

### "another instance holds the lock" but I'm sure nothing is running

Stale lock from a crashed daemon. The state layer waits 30 s after
the last heartbeat before allowing takeover. Either wait it out, or
force the takeover:

```bash
sqlite3 state.db "DELETE FROM daemon_locks WHERE state_db_path = '<path>';"
orchestrator daemon start
```

### Multi-workspace setups

The lock is keyed by the resolved `state.db` path. Two distinct
workspaces (e.g. `/work/project-a/state.db` and `/work/project-b/state.db`)
get distinct rows and run independently. There is no cross-workspace
coordination — the spec was scoped to "single user, single host" and
multi-tenancy is out of scope (see
[`features/F-016/spec.md`](../features/F-016/spec.md) § "Out of
scope").
