"""CLI entry point for the agent orchestrator.

Subcommands:
    orchestrator version          — show version
    orchestrator doctor           — health check (env + tokens + claude CLI + shadowing audit)
    orchestrator init             — interactive setup wizard
    orchestrator dashboard        — launch the live TUI dashboard
    orchestrator run              — launch Claude Code (+ detached watcher daemon when opted in)
    orchestrator daemon start     — run the F-016 watcher daemon in the foreground
    orchestrator daemon status    — show the workspace's daemon-lock holder + heartbeat
    orchestrator daemon stop      — SIGTERM (→ 10 s → SIGKILL fallback) the workspace daemon
    orchestrator verify-repo URL  — run + cache the branch-protection policy check

Install: `pip install -e .` from project root.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from orchestrator import env_guard

PKG_NAME = "agent-orchestrator"

# How long ``orchestrator run`` waits for the detached daemon's
# ``daemon_locks`` row to appear before continuing to ``execvpe(claude)``.
# Long enough for a fork + Python interpreter startup + SQLite write
# (~1-2s on a cold cache); short enough that the chat banner doesn't
# noticeably stall. The chat boots regardless of whether the daemon
# actually claimed the lock — this is a courtesy "tell the user the
# daemon's up" wait, not a hard prerequisite.
_DAEMON_BOOTSTRAP_TIMEOUT_S = 5.0
_DAEMON_BOOTSTRAP_POLL_S = 0.1


def _silence_httpx_logging() -> None:
    """httpx default logger spams INFO 'HTTP Request: …' lines.

    The doctor / init commands make a quick GET /user call — without this,
    the rich console output gets interleaved with httpx's logger output.
    """
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _version() -> str:
    try:
        return _pkg_version(PKG_NAME)
    except PackageNotFoundError:
        return "unknown (not installed)"


def _docker_daemon_warning() -> str | None:
    """Return a one-line warning if Docker isn't usable yet, else None.

    Two failure modes the init wizard surfaces to a user who picked the
    docker backend:
      1. The `docker` CLI is not on PATH at all.
      2. The CLI is present but `docker version` fails (daemon not running,
         socket permissions, etc.).

    Never raises — a successful check or any unexpected error returns the
    appropriate string (or None). The wizard treats this as advisory; the
    .env still records `ORCH_WORKER_BACKEND=docker` so a subsequent
    `orchestrator doctor` can re-check after the user fixes the issue.
    """
    if not shutil.which("docker"):
        return (
            "`docker` CLI not found on PATH — install Docker Desktop / Engine "
            "before running `orchestrator run`."
        )
    try:
        # subprocess imported lazily so the import cost only hits the
        # docker branch of the wizard.
        import subprocess  # noqa: PLC0415  # nosec B404 — invoking `docker` is the point

        proc = subprocess.run(  # nosec B603 B607 — argv list, no shell; docker on PATH  # noqa: S603, S607
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"could not probe Docker daemon ({exc.__class__.__name__}): {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "non-zero exit").strip().splitlines()
        first = detail[0] if detail else "non-zero exit"
        return f"Docker daemon not reachable yet: {first}"
    return None


def _resolve_anthropic_key(env_file: Path) -> tuple[str, str]:
    """Return ``(resolved, file_value)`` for the F-016-U-7 credential checks.

    ``resolved`` is the value the MCP server / daemon will see:
    ``ANTHROPIC_API_KEY`` from the process env if set, otherwise the
    value parsed from ``env_file``. This mirrors python-dotenv's
    ``load_dotenv(override=False)`` semantics without actually mutating
    ``os.environ`` — the CLI runs in the same process as the test
    suite, and a stray ``load_dotenv`` would pollute pytest's shared
    process state across test cases.

    ``file_value`` is the raw ``.env`` value (empty when the file is
    missing or doesn't contain the key) so the diagnostic can name the
    foot-gun source.
    """
    file_value = env_guard.read_env_file_values(env_file).get("ANTHROPIC_API_KEY", "")
    resolved = os.environ.get("ANTHROPIC_API_KEY", "") or file_value
    return resolved, file_value


def _guard_anthropic_key(console: Console, *, label: str) -> bool:
    """Validate ``ANTHROPIC_API_KEY`` shape; print a diagnostic and return False on bad.

    F-016-U-7 credential hardening (b): every orchestrator entry point
    that will spawn workers refuses to start if the resolved
    ``ANTHROPIC_API_KEY`` doesn't match the well-known ``sk-ant-``
    prefix. Composes :func:`env_guard.is_valid_anthropic_key` with the
    spec-mandated diagnostic so a stale shell-rc export shadowing a
    valid ``.env`` key produces an actionable error instead of an
    opaque Anthropic 401 minutes later.

    Returns False when the check fails so the caller can pick its own
    exit code (``orchestrator run`` uses 1, ``daemon start`` uses 4 —
    distinct from the other ``daemon start`` codes for systemd /
    launchd branching).
    """
    resolved, file_value = _resolve_anthropic_key(Path(".env"))
    if env_guard.is_valid_anthropic_key(resolved):
        return True
    diag = env_guard.anthropic_key_diagnostic(resolved, file_value)
    console.print(f"[red]✗ {label}: {diag}[/red]")
    return False


def _start_daemon_detached(console: Console) -> None:
    """F-016-U-7 unified bootstrap: spawn the watcher daemon as a detached child.

    Called from ``orchestrator run`` when ``ORCH_DAEMON_DRIVE=true``.
    The daemon survives the chat session's death by design (spec
    § "Open questions": "Separate process, not an MCP subprocess —
    decoupled lifetimes so a killed lead doesn't kill the watcher").

    Behavior:

      * If a daemon already holds the workspace's lock, print a one-liner
        with the existing holder's ``started_at`` and no-op.
      * Otherwise, ``subprocess.Popen([sys.executable, "-m",
        "orchestrator.daemon"], start_new_session=True)`` with
        stdout/stderr redirected to ``daemon.log`` in the workspace.
      * Wait up to ``_DAEMON_BOOTSTRAP_TIMEOUT_S`` for the lock row to
        appear so the banner can confirm the daemon claimed it.
        Whether or not the lock lands within the budget, the chat
        boots — the wait is a courtesy, not a prerequisite.

    Loud failures (file-open error on ``daemon.log``, Popen raises) are
    surfaced as a one-line warning so the operator can fix and re-run
    ``orchestrator daemon start`` manually; the chat continues to boot
    in the soft-fail case because the daemon was opt-in to begin with.
    """
    from orchestrator import state

    state.init_db()  # need the table before reading daemon_locks
    path = str(state.STATE_DB.resolve())
    existing = state.get_daemon_lock(path)
    if existing is not None:
        started = existing.get("started_at") or "?"
        console.print(f"[dim]daemon already running (started at {started})[/dim]")
        return

    # Lazy-import subprocess so the rest of the CLI doesn't pay its
    # import cost (and a future security audit of the launcher chain
    # sees one explicit Popen callsite, not a module-level import).
    import subprocess  # noqa: PLC0415  # nosec B404 — the whole helper is a Popen

    log_path = Path("daemon.log").resolve()
    try:
        log_handle = open(log_path, "ab")  # noqa: SIM115 — keep open across Popen
    except OSError as exc:
        console.print(
            f"[yellow]could not open {log_path} for daemon log "
            f"({exc.__class__.__name__}: {exc}); daemon not started[/yellow]"
        )
        return

    # POSIX gets ``start_new_session=True`` so the daemon detaches from
    # the chat session's controlling terminal (a Ctrl-C in the lead won't
    # propagate to the daemon's process group). Windows has no concept
    # of POSIX sessions and rejects ``start_new_session`` with
    # ``ValueError``; the equivalent there is the ``CREATE_NEW_PROCESS_GROUP``
    # creation flag (subprocess module constant), which puts the child
    # in its own process group so it doesn't receive a Ctrl-C delivered
    # to the lead.
    detach_kwargs: dict[str, Any]
    if sys.platform == "win32":
        # ``CREATE_NEW_PROCESS_GROUP`` is defined on Windows only; the
        # ``getattr`` guard keeps mypy happy on the POSIX type stub.
        detach_kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    else:
        detach_kwargs = {"start_new_session": True}

    try:
        proc = subprocess.Popen(  # nosec B603 — argv list, no shell; sys.executable is trusted  # noqa: S603
            [sys.executable, "-m", "orchestrator.daemon"],
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            cwd=os.getcwd(),
            env=dict(os.environ),
            **detach_kwargs,
        )
    except OSError as exc:
        console.print(
            f"[yellow]daemon spawn failed ({exc.__class__.__name__}: {exc}); "
            f"chat continues without a watcher. Run `orchestrator daemon start` "
            f"manually in another terminal to retry.[/yellow]"
        )
        return
    finally:
        # We hand the fd to the child via Popen above; close our copy so
        # this process doesn't keep the log file referenced past spawn.
        # ``file.close()`` is idempotent — the early-return paths above
        # are safe to share this single cleanup.
        with contextlib.suppress(OSError):
            log_handle.close()

    deadline = time.monotonic() + _DAEMON_BOOTSTRAP_TIMEOUT_S
    while time.monotonic() < deadline:
        if state.get_daemon_lock(path) is not None:
            console.print(f"[dim]daemon started (pid {proc.pid}, log: {log_path.name})[/dim]")
            return
        # ``poll()`` is None while the child is still alive. If it
        # already exited (config error → exit 2 / lock-held → exit 3),
        # surface that immediately rather than waiting out the budget.
        ret = proc.poll()
        if ret is not None:
            console.print(
                f"[yellow]daemon exited immediately (rc={ret}); "
                f"see {log_path.name} for details[/yellow]"
            )
            return
        time.sleep(_DAEMON_BOOTSTRAP_POLL_S)

    # Timed out waiting for the lock. The daemon may still be coming
    # up — don't crash the chat, just tell the operator what we saw.
    console.print(
        f"[yellow]daemon spawned (pid {proc.pid}) but no lock row visible "
        f"after {_DAEMON_BOOTSTRAP_TIMEOUT_S:.1f}s; check {log_path.name}[/yellow]"
    )


def _daemon_drive_enabled(*, env_file: Path | None = None) -> bool:
    """Coarse ``ORCH_DAEMON_DRIVE`` boolean — mirrors :func:`orchestrator.daemon.is_drive_enabled`.

    Pulled into ``cli`` so ``orchestrator run`` can decide whether to
    auto-spawn the watcher without importing ``orchestrator.daemon``
    at module load (which would drag the worker / health graph onto
    every ``--version`` / ``init`` / ``doctor`` invocation).

    Reads the process env first; if not set there, falls back to
    parsing ``env_file`` (since the daemon child process will load
    ``.env`` on its own startup, the user's expectation is that
    ``ORCH_DAEMON_DRIVE=true`` in ``.env`` is enough to trigger the
    auto-bootstrap).
    """
    raw = os.environ.get("ORCH_DAEMON_DRIVE", "")
    if not raw and env_file is not None:
        raw = env_guard.read_env_file_values(env_file).get("ORCH_DAEMON_DRIVE", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


@click.group(help="Multi-agent SDLC orchestrator.")
@click.version_option(_version(), prog_name=PKG_NAME)
def cli() -> None:
    pass


# --------------------------- version ---------------------------


@cli.command(help="Show the installed version.")
def version() -> None:
    click.echo(f"{PKG_NAME} {_version()}")


# --------------------------- dashboard ---------------------------


@cli.command(help="Launch the live TUI dashboard (Ctrl+C to quit).")
def dashboard() -> None:
    from orchestrator.dashboard import main as dashboard_main

    raise SystemExit(dashboard_main())


# --------------------------- daemon ---------------------------


@cli.group(help="Watcher daemon (F-016 Phase 3) — opt-in via ORCH_DAEMON_DRIVE=true.")
def daemon() -> None:
    """Manage the reconciliation daemon.

    The daemon runs the level-triggered reconciler from
    :mod:`orchestrator.daemon`. One daemon per workspace (keyed by the
    absolute ``state.db`` path); a second ``daemon start`` against the
    same workspace prints the existing holder's heartbeat and exits.
    """


@daemon.command("start", help="Run the reconciliation daemon in the foreground.")
def daemon_start() -> None:
    """Loop until SIGINT / SIGTERM.

    Loads the workspace's ``.env`` so ``GITHUB_TOKEN`` / ``ANTHROPIC_API_KEY``
    / ``ORCH_DAEMON_DRIVE`` reach the loop. Without ``ORCH_DAEMON_DRIVE=true``
    the loop is a no-op (the daemon refuses to claim the lock).

    Validates ``ANTHROPIC_API_KEY`` format before claiming the lock
    (F-016-U-7 credential hardening): a stale shell-rc export
    (``ANTHROPIC_API_KEY='lkj'``) that shadowed the ``.env`` value
    would have produced opaque worker 401s on every tick; the daemon
    refuses to start instead so the operator sees the foot-gun
    immediately.

    Exit codes (operator-facing — branch on ``$?`` from a systemd /
    launchd / shell-script supervisor):

      * ``0`` — clean shutdown (SIGINT / SIGTERM after some number of
        ticks, or a no-op exit because nothing was actionable).
      * ``2`` — ``ORCH_DAEMON_DRIVE`` is unset / falsy. Treat as a
        config nudge; do NOT retry without operator intervention.
      * ``3`` — another daemon already owns this workspace's
        ``state.db`` lock. Same-workspace contention is a
        configuration error, not a transient.
      * ``4`` — ``ANTHROPIC_API_KEY`` failed the format check (F-016-U-7).
    """
    from dotenv import load_dotenv

    from orchestrator import daemon as daemon_module
    from orchestrator import state

    # ``daemon start`` runs as its own process in production (either
    # the ``orchestrator run`` detached child or an operator's manual
    # invocation), so the ``load_dotenv`` mutation is contained to
    # that process. Validate the key against the resolved env after
    # the load so a stale shell-rc export shadowing a valid ``.env``
    # key fires the diagnostic.
    load_dotenv(dotenv_path=Path(".env"))
    if not _guard_anthropic_key(Console(), label="orchestrator daemon start"):
        raise SystemExit(4)
    state.init_db()
    raise SystemExit(daemon_module.exit_code_for_run(daemon_module.run_daemon()))


@daemon.command("stop", help="Stop the running daemon (SIGTERM → 10s wait → SIGKILL).")
def daemon_stop() -> None:
    """Signal the workspace's daemon and wait for the lock row to clear.

    Reads ``daemon_locks`` for the resolved ``state.STATE_DB`` path,
    sends SIGTERM to the holder's PID, polls for the row to vanish for
    up to ``_DAEMON_STOP_TIMEOUT_S`` (10 s), then falls back to SIGKILL
    if the daemon ignored SIGTERM. F-016-U-7.

    Exit codes (per spec):

      * ``0`` — stopped cleanly (the SIGTERM path landed; the lock row
        is gone).
      * ``1`` — no daemon running for this workspace (no lock row, or
        the row exists with no PID recorded).
      * ``2`` — the daemon ignored SIGTERM AND SIGKILL within the
        timeout, or the PID was already dead / not ours. Operator must
        investigate (zombie, permission error, etc.).
    """
    import signal as _signal

    from orchestrator import state

    state.init_db()
    console = Console()
    path = str(state.STATE_DB.resolve())
    row = state.get_daemon_lock(path)
    if row is None:
        console.print(f"[dim]No daemon running for {path}[/dim]")
        raise SystemExit(1)

    pid = row.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        console.print(
            f"[red]Lock row exists for {path} but has no recorded pid.[/red]\n"
            f"  holder_id={row.get('holder_id')} heartbeat_at={row.get('heartbeat_at')}\n"
            f"  Likely a pre-F-016-U-7 daemon — kill it manually and remove the "
            f"row with `orchestrator daemon status` to find the holder."
        )
        raise SystemExit(1)

    console.print(f"[dim]Sending SIGTERM to daemon pid {pid}…[/dim]")
    try:
        os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        console.print("[yellow]PID not alive — clearing stale lock row.[/yellow]")
        state.release_daemon_lock(path, row.get("holder_id", ""))
        raise SystemExit(0) from None
    except PermissionError:
        console.print(f"[red]Permission denied signaling pid {pid}.[/red]")
        raise SystemExit(2) from None

    if _wait_for_daemon_lock_clear(path, timeout_s=_DAEMON_STOP_TIMEOUT_S):
        console.print("[green]✓ daemon stopped[/green]")
        raise SystemExit(0)

    console.print(
        f"[yellow]Daemon ignored SIGTERM for {_DAEMON_STOP_TIMEOUT_S:.0f}s. "
        f"Escalating to SIGKILL pid {pid}…[/yellow]"
    )
    # ``signal.SIGKILL`` is POSIX-only; Windows treats ``os.kill`` with
    # ``signal.SIGTERM`` itself as a forceful terminate (it calls
    # ``TerminateProcess`` under the hood). Resolve the strongest
    # available signal at call time so the type checker (which sees
    # ``signal.SIGKILL`` undefined on Windows) and the Windows runtime
    # both stay happy.
    kill_signal: int = getattr(_signal, "SIGKILL", _signal.SIGTERM)
    try:
        os.kill(pid, kill_signal)
    except ProcessLookupError:
        # Race — it died after SIGTERM but before we sent SIGKILL.
        state.release_daemon_lock(path, row.get("holder_id", ""))
        console.print("[green]✓ daemon stopped (between SIGTERM and SIGKILL)[/green]")
        raise SystemExit(0) from None
    except PermissionError:
        console.print(f"[red]Permission denied SIGKILLing pid {pid}.[/red]")
        raise SystemExit(2) from None

    if _wait_for_daemon_lock_clear(path, timeout_s=_DAEMON_STOP_AFTER_KILL_S):
        console.print("[green]✓ daemon killed (SIGKILL)[/green]")
        raise SystemExit(0)
    console.print("[red]Lock row still present after SIGKILL — manual cleanup required.[/red]")
    raise SystemExit(2)


_DAEMON_STOP_TIMEOUT_S = 10.0
"""How long ``daemon stop`` waits for the SIGTERM path to clear the
``daemon_locks`` row before escalating to SIGKILL. Spec § Phase 5: "wait
up to 10s for the lock to clear, falls back to SIGKILL after that"."""


_DAEMON_STOP_AFTER_KILL_S = 2.0
"""Brief window for the OS to reap the SIGKILL'd process and the
daemon's ``release_singleton`` to land before we declare the operation
failed. SIGKILL bypasses the daemon's signal handler, so the lock
gets cleared via the stale-takeover path on the next start — but we
still want to surface a clean "stopped" vs. "lock row stuck" verdict
to the operator immediately."""


def _wait_for_daemon_lock_clear(path: str, *, timeout_s: float) -> bool:
    """Poll ``daemon_locks`` until the row disappears or the timeout fires.

    Returns True iff the row cleared. Polling cadence matches the
    bootstrap helper's 100ms — fast enough to feel snappy on a clean
    shutdown, light enough on SQLite that we don't busy-spin.
    """
    from orchestrator import state

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if state.get_daemon_lock(path) is None:
            return True
        time.sleep(0.1)
    return state.get_daemon_lock(path) is None


@daemon.command("status", help="Show the current daemon lock holder (if any).")
def daemon_status() -> None:
    """Print the workspace's ``daemon_locks`` row in JSON.

    Read-only — does not claim, heartbeat, or release. Use to debug
    "is the daemon actually running?" without restarting it.

    The JSON branch emits via :func:`click.echo` rather than
    ``rich.Console.print`` because Rich soft-wraps at the terminal
    width (or ~80 cols when not attached to a TTY). On macOS / Windows
    the workspace's ``state.db`` path can run past 100 chars
    (``/private/var/folders/...``); a wrap landing INSIDE the
    ``state_db_path`` string value injects a raw newline into the
    middle of a JSON string and breaks ``json.loads`` on the receiver.
    Machine-readable output goes through the non-wrapping channel; the
    "no lock" human hint stays on Rich so its ``[dim]`` style renders
    when a user runs the command interactively.
    """
    import json as _json

    from orchestrator import state

    state.init_db()
    path = str(state.STATE_DB.resolve())
    row = state.get_daemon_lock(path)
    if row is None:
        console = Console()
        console.print(f"[dim]No daemon lock for {path}[/dim]")
        raise SystemExit(0)
    click.echo(_json.dumps(row, indent=2))


# --------------------------- run ---------------------------


@cli.command(help="Launch Claude Code with the orchestrator MCP server (Remote Control on).")
@click.option("--no-remote-control", is_flag=True, help="Skip --remote-control flag.")
def run(no_remote_control: bool) -> None:
    console = Console()
    env_file = Path(".env")
    if not env_file.exists():
        console.print("[red]Missing .env[/red] — run [cyan]orchestrator init[/cyan] first.")
        raise SystemExit(1)

    # F-016-U-7 credential hardening (b): refuse to start when the
    # resolved ``ANTHROPIC_API_KEY`` doesn't pass the shape check. The
    # diagnostic names ``~/.zshrc`` / ``.bashrc`` / ``.zprofile`` so a
    # stale shell export shadowing a valid ``.env`` key is the obvious
    # foot-gun the message points at. ``_guard_anthropic_key`` reads
    # ``.env`` without mutating ``os.environ`` so the CLI surface stays
    # pure (the MCP server subprocess loads ``.env`` itself).
    if not _guard_anthropic_key(console, label="orchestrator run"):
        raise SystemExit(1)

    if not shutil.which("claude"):
        console.print(
            "[red]`claude` CLI not found in PATH.[/red] Install Claude Code first: "
            "https://claude.com/product/claude-code"
        )
        raise SystemExit(1)

    # F-016-U-7 unified bootstrap: when ``ORCH_DAEMON_DRIVE`` is on,
    # spawn the watcher as a detached child BEFORE handing off to
    # Claude Code. The daemon survives the chat session's death by
    # design (spec § Open questions). Idempotent — if the daemon is
    # already running, the helper no-ops with a one-line banner.
    # ``_daemon_drive_enabled`` reads the resolved env (which includes
    # ``.env`` since the daemon child loads it on its own startup), so
    # ``ORCH_DAEMON_DRIVE=true`` works whether it's in ``.env`` or the
    # shell.
    if _daemon_drive_enabled(env_file=env_file):
        _start_daemon_detached(console)

    # Belt-and-suspenders: clear BOTH ``ANTHROPIC_API_KEY`` and
    # ``ANTHROPIC_AUTH_TOKEN`` from the parent env so the MCP server
    # subprocess sees credentials only via ``.env`` (which it loads
    # explicitly via ``dotenv``). F-016-U-7 (a): without the
    # ``ANTHROPIC_AUTH_TOKEN`` strip a stale OAuth token in the parent
    # shell would shadow the API key flow we just validated.
    new_env = dict(os.environ)
    new_env.pop("ANTHROPIC_API_KEY", None)
    new_env.pop("ANTHROPIC_AUTH_TOKEN", None)

    cmd = ["claude"]
    if not no_remote_control:
        cmd.append("--remote-control")

    console.print(f"[dim]launching: {' '.join(cmd)}[/dim]")
    # Intentional no-shell exec: cmd[0] is the literal "claude",
    # cmd is a list (no shell interpretation), new_env is fully controlled above.
    os.execvpe(cmd[0], cmd, new_env)  # noqa: S606  # nosec B606


# --------------------------- doctor ---------------------------


@cli.command(help="Health check — verify everything is configured correctly.")
def doctor() -> None:
    _silence_httpx_logging()
    console = Console()
    console.print("\n[bold]Orchestrator health check[/bold]\n")

    all_pass = True

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal all_pass
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        line = f"  {icon} {name}"
        if detail:
            line += f" [dim]· {detail}[/dim]"
        console.print(line)
        if not ok:
            all_pass = False

    # 1. Python version
    report(
        "Python >= 3.11",
        sys.version_info >= (3, 11),
        f"running {sys.version.split()[0]}",
    )

    # 2. Package importable
    try:
        from orchestrator import mcp_server  # noqa: F401

        report("orchestrator package importable", True, f"{PKG_NAME} {_version()}")
    except Exception as e:
        report("orchestrator package importable", False, str(e))

    # 3. .env file
    env_file = Path(".env")
    env_exists = env_file.exists()
    report(".env file in current directory", env_exists)

    env_content = env_file.read_text() if env_exists else ""
    # ``ant_match`` is no longer needed — F-016-U-7 routes the
    # ANTHROPIC_API_KEY check through the resolved-env path
    # (env_guard.is_valid_anthropic_key) so the diagnostic catches
    # shell-rc shadowing rather than just the ``.env`` source.
    gh_match = re.search(r"^GITHUB_TOKEN=(\S+)", env_content, re.MULTILINE)
    ntfy_match = re.search(r"^NTFY_TOPIC=(\S+)", env_content, re.MULTILINE)
    app_id_match = re.search(r"^GITHUB_APP_ID=(\S+)", env_content, re.MULTILINE)
    app_inst_match = re.search(r"^GITHUB_APP_INSTALLATION_ID=(\S+)", env_content, re.MULTILINE)
    app_keypath_match = re.search(r"^GITHUB_APP_PRIVATE_KEY_PATH=(\S+)", env_content, re.MULTILINE)
    app_keyinline_match = re.search(r"^GITHUB_APP_PRIVATE_KEY=(.+)$", env_content, re.MULTILINE)

    # F-016-U-7 credential hardening: resolve env vars without mutating
    # ``os.environ`` (the CLI shares its process with the pytest suite
    # — a stray ``load_dotenv`` would pollute every subsequent test).
    # ``shell_env`` snapshots the parent-process state pre-merge so the
    # shadowing audit (check 12 below) can flag divergences.
    shell_env = {k: v for k, v in os.environ.items() if k in env_guard.SHADOWING_ENV_VARS}
    env_file_values = env_guard.read_env_file_values(env_file) if env_exists else {}
    env_file_anthropic = env_file_values.get("ANTHROPIC_API_KEY", "")
    resolved_anthropic = shell_env.get("ANTHROPIC_API_KEY", "") or env_file_anthropic

    # 4. ANTHROPIC_API_KEY — validate the RESOLVED value (process env
    # if set, else ``.env``), not just the ``.env`` source. F-016-U-7
    # credential hardening (b): a stale shell-rc export
    # (``export ANTHROPIC_API_KEY='lkj'``) silently shadowing a valid
    # ``.env`` key is the foot-gun this check exists to catch — the
    # diagnostic names ``~/.zshrc`` / ``.bashrc`` / ``.zprofile`` so
    # the operator knows where to look.
    if env_guard.is_valid_anthropic_key(resolved_anthropic):
        report(
            "ANTHROPIC_API_KEY format",
            True,
            f"prefix {resolved_anthropic[:12]}…",
        )
    else:
        diag = env_guard.anthropic_key_diagnostic(resolved_anthropic, env_file_anthropic)
        report("ANTHROPIC_API_KEY format", False, diag)

    # 5. GitHub auth — App takes precedence over PAT
    # Load .env values into os.environ so github_app helpers see them
    import os as _os

    if app_id_match:
        _os.environ["GITHUB_APP_ID"] = app_id_match.group(1)
    if app_inst_match:
        _os.environ["GITHUB_APP_INSTALLATION_ID"] = app_inst_match.group(1)
    if app_keypath_match:
        _os.environ["GITHUB_APP_PRIVATE_KEY_PATH"] = app_keypath_match.group(1)
    if app_keyinline_match:
        _os.environ["GITHUB_APP_PRIVATE_KEY"] = app_keyinline_match.group(1)
    if gh_match:
        _os.environ["GITHUB_TOKEN"] = gh_match.group(1)

    from orchestrator import github_app

    if github_app.is_app_configured():
        # is_app_configured() implies these regex matches succeeded too
        assert app_id_match is not None and app_inst_match is not None
        report(
            "GitHub App configured",
            True,
            f"app_id={app_id_match.group(1)} installation={app_inst_match.group(1)}",
        )
        # Try minting a real token to confirm App auth works end-to-end
        try:
            github_app.clear_token_cache()
            token = github_app.mint_installation_token()
            import httpx

            r = httpx.get(
                "https://api.github.com/installation/repositories",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if r.status_code == 200:
                repos = r.json().get("total_count", "?")
                report("GitHub App installation token mints", True, f"{repos} repo(s)")
            else:
                report("GitHub App installation token mints", False, f"HTTP {r.status_code}")
        except Exception as e:
            report("GitHub App installation token mints", False, str(e))
    elif gh_match and gh_match.group(1):
        token = gh_match.group(1)
        fmt_ok = token.startswith(("github_pat_", "ghp_"))
        report("GITHUB_TOKEN format (PAT fallback)", fmt_ok, f"prefix {token[:15]}…")
        if fmt_ok:
            import httpx

            try:
                r = httpx.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if r.status_code == 200:
                    report(
                        "GITHUB_TOKEN authenticates",
                        True,
                        f"as {r.json().get('login', '?')}",
                    )
                else:
                    report("GITHUB_TOKEN authenticates", False, f"HTTP {r.status_code}")
            except Exception as e:
                report("GITHUB_TOKEN authenticates", False, str(e))
    else:
        report(
            "GitHub auth configured",
            False,
            "set GITHUB_APP_* (recommended) or GITHUB_TOKEN (PAT fallback)",
        )

    # 6. NTFY_TOPIC (informational only — not a failure if missing)
    if ntfy_match and ntfy_match.group(1):
        topic = ntfy_match.group(1)
        report("NTFY_TOPIC set (optional)", True, f"{topic[:20]}…")
    else:
        report("NTFY_TOPIC set (optional)", True, "[dim]unset → push notifications disabled[/dim]")
        # don't fail on this

    # 7. Claude Code CLI
    claude_path = shutil.which("claude")
    report("claude CLI installed", bool(claude_path), claude_path or "not in PATH")

    # 8. .mcp.json
    mcp_path = Path(".mcp.json")
    report(
        ".mcp.json in project root",
        mcp_path.exists(),
        "registers orchestrator MCP server" if mcp_path.exists() else "missing",
    )

    # 9. state.db (informational)
    from orchestrator import state

    state_exists = state.STATE_DB.exists()
    if state_exists:
        # quick read-only sanity check
        try:
            features = state.list_features()
            report("state.db readable", True, f"{len(features)} feature(s) tracked")
        except Exception as e:
            report("state.db readable", False, str(e))
    else:
        report(
            "state.db",
            True,
            "[dim]missing — will be created on first orchestrator run[/dim]",
        )

    # 10. gh CLI (informational — used by snapshot script + can help debug)
    gh_path = shutil.which("gh")
    if gh_path:
        report("gh CLI installed (optional)", True, gh_path)
    else:
        report(
            "gh CLI installed (optional)",
            True,
            "[dim]not in PATH — agents bring their own inside containers[/dim]",
        )

    # 11. Worker backend + credential boundary audit. Always show the
    # backend; the docker-specific receipts only render under the
    # `docker` backend (the managed_agents path runs on Anthropic infra,
    # not in a local container, so there's nothing to audit here).
    backend = os.environ.get("ORCH_WORKER_BACKEND", "managed_agents")
    if backend == "docker":
        from orchestrator.workers.docker_claude_code import (
            DEFAULT_IMAGE,
            audit_registry_passthrough_for_repo,
            build_cred_audit,
            run_doctor_probes,
        )

        # The DEFAULT_IMAGE module docstring (in docker_claude_code.py)
        # documents ORCH_DOCKER_WORKER_IMAGE as the per-environment
        # override. Honor it here so the audit + probes both target the
        # tag the user actually built (PR #16 review M1).
        image_override = os.environ.get("ORCH_DOCKER_WORKER_IMAGE", DEFAULT_IMAGE)

        console.print()
        console.print("[bold]Worker credential audit (ORCH_WORKER_BACKEND=docker)[/bold]")
        audit = build_cred_audit(image=image_override)
        console.print(audit.render())

        console.print()
        console.print("[bold]Docker worker probes[/bold]")
        for probe in run_doctor_probes(image=audit.image, network=audit.network):
            report(probe.name, probe.ok, probe.detail)

        # F-001-U-4: warn if the CWD looks like a repo that needs
        # internal-registry passthrough but no passthrough is wired.
        # Heuristic: package.json with `"registry"` field OR requirements.txt
        # with `--index-url <private-host>`. The doctor command runs in
        # the user's working directory, so the CWD doubles as "the repo
        # they probably want to spawn against".
        passthrough_warnings = audit_registry_passthrough_for_repo(Path.cwd())
        if passthrough_warnings:
            console.print()
            console.print("[bold yellow]Internal-registry passthrough[/bold yellow]")
            for warning in passthrough_warnings:
                console.print(f"  [yellow]![/yellow] {warning}")
    else:
        report(
            f"Worker backend: {backend}",
            True,
            "[dim]managed_agents → no local container audit[/dim]",
        )

    # 12. Env-vs-``.env`` shadowing audit (F-016-U-7 credential
    # hardening (c)). Compare the parent-process snapshot against the
    # file values — a key set in the shell-env AND in ``.env`` with
    # divergent values is a stale shell-rc export silently winning,
    # the exact foot-gun the spec calls out.
    if env_exists:
        shadow_findings = env_guard.detect_env_shadowing(shell_env, env_file_values)
        if shadow_findings:
            console.print()
            console.print("[bold yellow]Env-vs-.env shadowing audit[/bold yellow]")
            for finding in shadow_findings:
                # Use ``report`` so the overall pass/fail rollup reflects
                # the shadowing — a stale export silently winning over
                # ``.env`` is a real failure, not advisory noise.
                report(
                    f"shadowing: {finding['name']}",
                    False,
                    env_guard.format_shadowing_finding(finding).split(": ", 1)[1],
                )
        else:
            report(
                "Env-vs-.env shadowing audit",
                True,
                "[dim]no orchestrator-relevant env vars shadowed[/dim]",
            )

    console.print()
    if all_pass:
        console.print("[bold green]✓ all checks passed[/bold green]")
        console.print("\nNext: [cyan]orchestrator run[/cyan]")
        raise SystemExit(0)
    else:
        console.print("[bold red]✗ some checks failed[/bold red]")
        console.print("\nFix: [cyan]orchestrator init[/cyan] (interactive setup)")
        raise SystemExit(1)


# --------------------------- verify-repo ---------------------------


@cli.command(
    "verify-repo",
    help="Verify a target repo against the orchestrator's policy and cache it.",
)
@click.argument("repo_url")
def verify_repo_cmd(repo_url: str) -> None:
    """Run policy verification against a repo. Caches the result on success.

    The same check that the orchestrator runs implicitly before any spawn —
    invoke it manually at setup time to confirm branch protection and
    permissions are in place BEFORE you load a feature.
    """
    _silence_httpx_logging()
    console = Console()

    # Load .env from the current working directory ONLY — not via the
    # default find_dotenv() walk, which traverses up from this file's
    # location and would pick up the orchestrator repo's .env during
    # tests / when the user invokes the CLI from an unrelated directory.
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path(".env"))

    from orchestrator import github_app, repo_verify, state

    try:
        token = github_app.get_agent_token()
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        console.print("\nFix: run [cyan]orchestrator init[/cyan] to configure GitHub auth.")
        raise SystemExit(1) from None

    auth_mode = github_app.auth_mode()

    try:
        result = repo_verify.verify(repo_url, token, auth_mode=auth_mode)
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise SystemExit(1) from None
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗ error contacting GitHub: {e}[/red]")
        raise SystemExit(1) from None

    for line in repo_verify.format_result_lines(result):
        console.print(line)

    if result.passed:
        state.init_db()  # ensure table exists in case this is run pre-init
        state.save_verified_repo(result)
        console.print()
        console.print("[green]✓ cached — spawns against this repo are allowed for 24h[/green]")
        raise SystemExit(0)
    else:
        console.print()
        console.print("[red]✗ verification FAILED — spawns against this repo will be blocked[/red]")
        raise SystemExit(1)


# --------------------------- init ---------------------------


@cli.command(help="Interactive setup wizard: writes .env, initializes state.db.")
@click.option("--force", is_flag=True, help="Overwrite existing .env without asking.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run the prompts and print the .env that would be written; touch no files.",
)
def init(force: bool, dry_run: bool) -> None:
    _silence_httpx_logging()
    console = Console()
    console.print("\n[bold cyan]Agent orchestrator setup[/bold cyan]\n")
    if dry_run:
        console.print(
            "[yellow]--dry-run:[/yellow] no files will be written; the planned "
            ".env contents will be printed at the end.\n"
        )

    # Sanity: are we in an orchestrator project directory?
    pyproject = Path("pyproject.toml")
    in_project = pyproject.exists() and "agent-orchestrator" in pyproject.read_text()
    if not in_project:
        console.print(
            "[yellow]Warning:[/yellow] this doesn't look like the agent-orchestrator "
            "project directory (no matching pyproject.toml).\n"
            "The wizard will still write to .env here, but Claude Code expects to "
            "launch from the orchestrator project root."
        )
        if not click.confirm("Continue anyway?", default=False):
            return

    # Check for existing .env. --dry-run never touches disk so the
    # overwrite prompt is irrelevant; skip it so the snapshot tests can
    # run against a workspace that already happens to have a .env file.
    env_file = Path(".env")
    if (
        env_file.exists()
        and not force
        and not dry_run
        and not click.confirm(
            f".env already exists at {env_file.resolve()} — overwrite?", default=False
        )
    ):
        console.print("[dim]aborted; existing .env left untouched[/dim]")
        return

    # 1. Anthropic API key
    console.print("\n[bold]1. Anthropic API key[/bold]")
    console.print(
        "Get one from [link]https://console.anthropic.com[/link] → Settings → API Keys.\n"
        "Note: this is separate from your claude.ai subscription billing."
    )
    api_key = click.prompt("  ANTHROPIC_API_KEY", hide_input=True)
    while not api_key.startswith("sk-ant-"):
        console.print("  [red]Must start with sk-ant-[/red]")
        api_key = click.prompt("  ANTHROPIC_API_KEY", hide_input=True)

    # 2. GitHub auth — App (recommended) or PAT
    console.print("\n[bold]2. GitHub authentication for worker agents[/bold]")
    console.print(
        "Choose [cyan]a[/cyan] for a GitHub App (recommended: bot identity, "
        "1-hr tokens, easier audit/revocation)\n"
        "       [cyan]p[/cyan] for a fine-grained PAT (faster setup, single-user)"
    )
    choice = click.prompt(
        "  GitHub auth method (a/p)",
        type=click.Choice(["a", "p"], case_sensitive=False),
        default="p",
    ).lower()

    gh_app_id = ""
    gh_app_inst = ""
    gh_app_key_path = ""
    gh_token = ""  # nosec B105 — placeholder init for the PAT branch below, not a secret

    if choice == "a":
        console.print(
            "\n  Register an App at https://github.com/settings/apps/new\n"
            "  Required permissions: contents:rw, pull_requests:rw, issues:rw,\n"
            "                        checks:read, metadata:read; webhook off.\n"
            "  Install it on your target repos."
        )
        gh_app_id = click.prompt("  GITHUB_APP_ID (numeric)").strip()
        gh_app_inst = click.prompt(
            "  GITHUB_APP_INSTALLATION_ID (numeric, from install URL)"
        ).strip()
        gh_app_key_path = click.prompt("  Path to App private key .pem (chmod 600 first)").strip()
        # Live mint a token to verify
        import httpx

        from orchestrator import github_app

        os.environ["GITHUB_APP_ID"] = gh_app_id
        os.environ["GITHUB_APP_INSTALLATION_ID"] = gh_app_inst
        os.environ["GITHUB_APP_PRIVATE_KEY_PATH"] = gh_app_key_path
        try:
            github_app.clear_token_cache()
            tok = github_app.mint_installation_token()
            r = httpx.get(
                "https://api.github.com/installation/repositories",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=10,
            )
            if r.status_code == 200:
                console.print(
                    f"  [green]✓ App token mints OK · "
                    f"{r.json().get('total_count', '?')} repo(s) installed[/green]"
                )
            else:
                console.print(f"  [yellow]token mint returned HTTP {r.status_code}[/yellow]")
                if not click.confirm("  Continue anyway?", default=True):
                    return
        except Exception as e:
            console.print(f"  [yellow]could not verify App auth live: {e}[/yellow]")
            if not click.confirm("  Continue anyway?", default=True):
                return
    else:
        console.print(
            "  Create a fine-grained PAT: https://github.com/settings/personal-access-tokens/new\n"
            "  Permissions: contents:rw, pull_requests:rw, issues:rw,\n"
            "               metadata:read, actions:read, checks:read, commit_statuses:read"
        )
        gh_token = click.prompt("  GITHUB_TOKEN", hide_input=True)
        while not gh_token.startswith(("github_pat_", "ghp_")):
            console.print("  [red]Must start with github_pat_ or ghp_[/red]")
            gh_token = click.prompt("  GITHUB_TOKEN", hide_input=True)

        import httpx

        try:
            r = httpx.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {gh_token}"},
                timeout=10,
            )
            if r.status_code == 200:
                console.print(f"  [green]✓ authenticated as {r.json()['login']}[/green]")
            else:
                console.print(
                    f"  [yellow]token returned HTTP {r.status_code} — verify manually[/yellow]"
                )
                if not click.confirm("  Continue anyway?", default=True):
                    return
        except Exception as e:
            console.print(f"  [yellow]could not verify token live: {e}[/yellow]")

    # 3. NTFY topic (optional)
    console.print("\n[bold]3. ntfy.sh push topic (optional)[/bold]")
    console.print(
        "For phone push notifications on escalations + ready-to-merge events.\n"
        "Use a hard-to-guess string (treat like a password)."
    )
    # Suggested topic must be deterministic under --dry-run so the
    # snapshot test isn't flaky. The placeholder is intentionally
    # obvious (`<random>`) so a real user pasting from a dry run sees
    # they need to substitute it.
    suggested = "agent-orch-<random>" if dry_run else "agent-orch-" + os.urandom(9).hex()[:12]
    ntfy = click.prompt(
        f"  NTFY_TOPIC (suggested: {suggested}, blank to skip)",
        default="",
        show_default=False,
    )

    # 4. Worker backend selection (F-001-U-5).
    console.print("\n[bold]4. Worker backend[/bold]")
    console.print(
        "Where coder / tester / reviewer agents actually run.\n"
        "  [cyan]m[/cyan]anaged_agents — Anthropic-hosted gVisor sandboxes "
        "(default; needs ANTHROPIC_API_KEY only)\n"
        "  [cyan]d[/cyan]ocker         — locally-managed containers + your "
        "claude.ai OAuth session\n"
        "                   (internal-registry passthrough; Docker daemon "
        "required)\n"
        "See [link]docs/PROPOSAL-docker-workers.md[/link] for the threat model + "
        "trade-offs."
    )
    backend_choice = click.prompt(
        "  Worker backend (m/d)",
        type=click.Choice(["m", "d"], case_sensitive=False),
        default="m",
    ).lower()
    worker_backend = "docker" if backend_choice == "d" else "managed_agents"

    # Best-effort daemon reachability check when the user picks docker.
    # Surfaces a one-line warning if `docker version` fails; never blocks
    # the wizard (the user may be configuring on a host before installing
    # Docker, or building the image on a separate machine).
    if worker_backend == "docker":
        warning = _docker_daemon_warning()
        if warning:
            console.print(f"  [yellow]![/yellow] {warning}")
            console.print(
                "  [dim]Run `orchestrator doctor` after starting Docker to re-check.[/dim]"
            )

    # 5. Assemble .env
    env_lines = [
        "# Anthropic API key for Managed Agents (separate from claude.ai subscription)",
        f"ANTHROPIC_API_KEY={api_key}",
        "",
        "# GitHub auth — App (preferred) or PAT (fallback)",
        f"GITHUB_APP_ID={gh_app_id}",
        f"GITHUB_APP_INSTALLATION_ID={gh_app_inst}",
        f"GITHUB_APP_PRIVATE_KEY_PATH={gh_app_key_path}",
        f"GITHUB_TOKEN={gh_token}",
        "",
        "# Optional ntfy.sh push topic (blank to disable)",
        f"NTFY_TOPIC={ntfy}",
        "",
        "# Worker backend: managed_agents (default) or docker.",
        "# See README.md 'Choosing a worker backend' and docs/PROPOSAL-docker-workers.md.",
        f"ORCH_WORKER_BACKEND={worker_backend}",
        "",
    ]

    if dry_run:
        console.print("\n[bold]--dry-run: planned .env contents[/bold]")
        console.print("[dim]" + "-" * 64 + "[/dim]")
        for line in env_lines:
            console.print(line)
        console.print("[dim]" + "-" * 64 + "[/dim]")
        console.print(
            "\n[yellow]No files were written.[/yellow] "
            "Re-run without [cyan]--dry-run[/cyan] to apply."
        )
        return

    env_file.write_text("\n".join(env_lines))
    console.print(f"\n[green]✓ wrote {env_file.resolve()}[/green]")

    # 6. Initialize state.db
    from orchestrator import state

    state.init_db()
    console.print(f"[green]✓ initialized state.db at {state.STATE_DB}[/green]")

    # 7. Subscribe ntfy reminder
    if ntfy:
        console.print(
            f"\n[bold]Don't forget:[/bold] subscribe to topic [cyan]{ntfy}[/cyan] "
            "in the ntfy mobile app to receive push notifications."
        )

    # 8. Next steps
    console.print("\n[bold]Done![/bold] Next:\n")
    console.print("  1. [cyan]orchestrator doctor[/cyan]     — verify everything")
    console.print("  2. [cyan]orchestrator run[/cyan]        — launch Claude Code")
    console.print(
        "  3. [cyan]orchestrator dashboard[/cyan]  — live state view (in another terminal)"
    )


if __name__ == "__main__":
    cli()
