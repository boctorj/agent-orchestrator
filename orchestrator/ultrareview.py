"""Fire Claude Code's ``/ultrareview`` as an optional final pre-merge gate.

F-007 wants ``/ultrareview`` (Anthropic's multi-agent cloud bug-hunter) to
run *after* our reviewer emits ``REVIEW_RECOMMEND_MERGE``; only emit the
ready-to-merge marker if it passes. This module is the bare invocation
primitive — orchestration code (cycle_review wiring, feature flag,
findings surfacing) lives elsewhere in F-007.

Invocation mechanism (pinned)
-----------------------------

Three transports are documented for ``/ultrareview``:

  1. Interactive slash command (``/ultrareview <PR>``) typed inside a
     live Claude Code session. Requires a TTY — not usable from the
     orchestrator.
  2. The ``claude ultrareview`` CLI subcommand. Documented at
     https://code.claude.com/docs/en/ultrareview for use from CI or
     scripts: launches the same review as the slash command, blocks
     until the remote sandbox finishes, prints findings to stdout, and
     exits 0 on success / 1 on failure. Progress messages and the
     live-session URL go to stderr so stdout stays parseable.
  3. PR-comment trigger. Not documented as a supported invocation
     surface; Anthropic's docs are explicit that "/ultrareview only
     runs when you invoke it explicitly. Claude does not start an
     ultrareview on its own."

We pin transport #2. It is the only mechanism Anthropic ships for
non-interactive callers, and its exit-code contract is exactly what
F-007 needs to gate ready-to-merge on.

Surface
-------

Two functions, both named for F-007's task description:

  * :func:`trigger(pr_url)` — spawns ``claude ultrareview <N>`` as a
    background subprocess and records the handle keyed by ``pr_url``.
    Non-blocking, returns ``None``.
  * :func:`wait_for_result(pr_url, timeout=600)` — polls the handle
    until the subprocess exits, the timeout elapses, or both. Returns
    ``{"passed": bool, "findings": list[str]}``. ``passed`` is the
    subprocess exit code (``0`` → True). ``findings`` is the
    whitespace-stripped non-empty lines of stdout, kept verbatim so the
    caller decides how to render them.

The split lets the orchestrator fire-and-forget the review during the
reviewer-recommends-merge hand-off, then come back to harvest the
verdict after other work — same shape as F-005's ``wait_for_ci``.

Tests inject ``spawn``, ``sleep``, and ``now`` so we never touch the
real ``claude`` CLI nor wall-clock time.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess  # nosec B404 — invoking `claude` is the whole point
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# --------------------------- defaults ---------------------------

# Total wall-clock cap on one ultrareview run. Anthropic's docs say a
# typical run takes 5-10 minutes; default to 10 to match the
# documentation's upper bound. Override per call by passing timeout=.
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("ULTRAREVIEW_TIMEOUT_SECONDS", "600"))

# Poll interval while waiting. Long enough that we don't spin the CPU on
# a multi-minute run, short enough that the orchestrator reacts within
# the same poll budget cycle_review uses.
DEFAULT_POLL_INTERVAL_SECONDS = int(os.getenv("ULTRAREVIEW_POLL_INTERVAL", "5"))

# Path to the Claude CLI binary. ``claude`` on PATH for the standard
# install; override via env for sandboxed deployments or test stubs.
ULTRAREVIEW_CLI = os.getenv("ULTRAREVIEW_CLI", "claude")

_PR_URL_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+?)/pull/(\d+)(?:/|$)")


# --------------------------- internal state ---------------------------


@dataclass
class _RunHandle:
    """In-flight ultrareview subprocess + bookkeeping.

    ``process`` is a :class:`subprocess.Popen` in production and a
    test-injected stub in unit tests; the only methods we call on it are
    ``poll()``, ``communicate()``, ``terminate()``, and ``kill()``.
    """

    pr_url: str
    pr_number: int
    process: Any
    started_at: float


# pr_url → handle. Module-level so ``wait_for_result`` can find the run
# started by an earlier ``trigger`` without callers passing a handle.
# Distinct pr_urls hash to distinct keys, so concurrent runs on
# different PRs don't collide; ``trigger`` on a repeated pr_url
# overwrites the prior handle (caller-error recovery).
_runs: dict[str, _RunHandle] = {}


# --------------------------- public API ---------------------------


def trigger(
    pr_url: str,
    *,
    spawn: Callable[[list[str]], Any] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Start an ultrareview run for the PR identified by ``pr_url``.

    Spawns ``claude ultrareview <PR>`` in the background. Non-blocking;
    call :func:`wait_for_result` to harvest the verdict.

    Args:
        pr_url: full ``https://github.com/owner/repo/pull/N`` URL.
        spawn: injection point for tests. Takes the argv list and
            returns a Popen-shaped object. Defaults to
            :func:`_default_spawn`.
        now: injection point for the started-at timestamp.

    Raises:
        ValueError: if ``pr_url`` is not a parseable PR URL.
    """
    _, _, pr_number = _parse_pr_url(pr_url)
    spawner = spawn if spawn is not None else _default_spawn
    argv = [ULTRAREVIEW_CLI, "ultrareview", str(pr_number)]
    process = spawner(argv)
    _runs[pr_url] = _RunHandle(
        pr_url=pr_url,
        pr_number=pr_number,
        process=process,
        started_at=now(),
    )


def wait_for_result(
    pr_url: str,
    timeout: int | None = DEFAULT_TIMEOUT_SECONDS,
    *,
    poll_interval_seconds: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Block until the ultrareview run for ``pr_url`` finishes or times out.

    Args:
        pr_url: same URL passed to :func:`trigger`.
        timeout: seconds to wait before giving up. ``None`` uses
            :data:`DEFAULT_TIMEOUT_SECONDS`.
        poll_interval_seconds: poll cadence. Defaults to env /
            :data:`DEFAULT_POLL_INTERVAL_SECONDS`.
        sleep / now: injection points for tests.

    Returns:
        ``{"passed": bool, "findings": list[str]}`` where ``passed`` is
        ``True`` iff the subprocess exited 0. On timeout, ``passed`` is
        ``False`` and ``findings`` includes a synthetic
        ``"[timeout after Ns]"`` entry so callers can surface the reason.

    Raises:
        RuntimeError: if no prior :func:`trigger` was issued for this URL.
    """
    handle = _runs.get(pr_url)
    if handle is None:
        raise RuntimeError(f"no ultrareview run registered for {pr_url!r}; call trigger() first")

    timeout_seconds = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    poll = DEFAULT_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds

    start = now()
    while True:
        rc = handle.process.poll()
        if rc is not None:
            stdout, _stderr = handle.process.communicate()
            return _interpret(rc, stdout or "")

        elapsed = now() - start
        if elapsed >= timeout_seconds:
            _kill_safely(handle.process)
            return {
                "passed": False,
                "findings": [f"[timeout after {timeout_seconds}s]"],
            }

        sleep(poll)


# --------------------------- internals ---------------------------


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Split ``https://github.com/owner/repo/pull/N[/...]`` into parts."""
    m = _PR_URL_RE.match(pr_url)
    if not m:
        raise ValueError(f"not a github PR url: {pr_url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def _default_spawn(argv: list[str]) -> subprocess.Popen[str]:
    """Real-world subprocess launch. Tests inject a stub instead.

    stdout/stderr are captured (text mode) so :func:`wait_for_result`
    can read findings via ``.communicate()``. We pass the argv list
    rather than a shell string so there's no shell-injection surface.
    """
    return subprocess.Popen(  # nosec B603 — argv list, no shell; binary path from env
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _interpret(returncode: int, stdout: str) -> dict[str, Any]:
    """Translate (exit code, stdout) into the public result shape."""
    return {
        "passed": returncode == 0,
        "findings": _parse_findings(stdout),
    }


def _parse_findings(stdout: str) -> list[str]:
    """Split stdout into a list of finding strings.

    Conservative: every non-empty whitespace-stripped line becomes one
    finding. Keeps raw text intact so the caller (which renders into a
    PR comment or escalation push) decides formatting. The ultrareview
    CLI's exact output schema is the source of truth for "passed"; this
    list is purely contextual.
    """
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _kill_safely(process: Any) -> None:
    """SIGTERM then SIGKILL the subprocess on timeout.

    Best-effort — a stuck cloud-side review whose CLI wrapper won't
    honor SIGTERM still gets killed, but if even SIGKILL fails (caller-
    supplied stub, exotic platform) we swallow rather than re-raise so
    the timeout path keeps a clean return contract.
    """
    with contextlib.suppress(Exception):
        process.terminate()
    with contextlib.suppress(Exception):
        process.kill()


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ULTRAREVIEW_CLI",
    "trigger",
    "wait_for_result",
]
