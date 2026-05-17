"""Fire Claude Code's ``/ultrareview`` as an optional final pre-merge gate.

F-007 wants ``/ultrareview`` (Anthropic's multi-agent cloud bug-hunter) to
run *after* our reviewer emits ``REVIEW_RECOMMEND_MERGE``; only emit the
ready-to-merge marker if it passes. This module is the bare invocation
primitive — orchestration code (cycle_review wiring, feature flag,
findings surfacing) lives elsewhere in F-007.

Invocation mechanism (pinned)
-----------------------------

Three transports are documented for ``/ultrareview``:

  1. Interactive slash command (``/ultrareview <N>``) typed inside a
     live Claude Code session. Requires a TTY — not usable from the
     orchestrator.
  2. The ``claude ultrareview`` CLI subcommand. Documented at
     https://code.claude.com/docs/en/ultrareview for use from CI or
     scripts: launches the same review as the slash command, blocks
     until the remote sandbox finishes, prints findings to stdout, and
     exits ``0`` when the review *completes* (with or without bugs),
     ``1`` when the review fails to launch / the remote session errors
     / the CLI's own ``--timeout`` elapses, and ``130`` on SIGINT.
     Progress messages and the live-session URL go to stderr.
  3. PR-comment trigger. Not documented as a supported invocation
     surface; Anthropic's docs are explicit that "/ultrareview only
     runs when you invoke it explicitly. Claude does not start an
     ultrareview on its own."

We pin transport #2. It is the only mechanism Anthropic ships for
non-interactive callers.

Passed semantics
----------------

The exit-code contract above means **rc == 0 does NOT mean "no bugs."**
A buggy PR whose review completes cleanly still exits 0; the bugs are
in the ``bugs.json`` payload on stdout. So we spawn with ``--json``
(documented flag for the raw machine-readable payload) and compute::

    passed = (rc == 0 AND len(bugs) == 0)

That is the only state that means "ultrareview ran end-to-end AND
found nothing." ``rc != 0`` means "the review never produced a
verdict" — also ``passed = False``, but for a different reason. The
caller (cycle_review) treats both as "do not emit ready-to-merge."

The gate **fails closed**: rc==0 with stdout we can't parse (non-JSON,
or JSON without the expected ``bugs`` key) flips ``passed`` to False
via a sentinel finding. Schema drift / CLI variants should block
ready-to-merge, not silently endorse it. See :func:`_parse_bugs`.

Timeouts
--------

The CLI's own ``--timeout`` flag defaults to 30 minutes (per the docs).
The wrapper exposes a single timeout knob used by both functions, so
wrapper-side and cloud-side timers always fire together — no built-in
billing gap on the default call shape:

* :data:`SPEC_WAIT_TIMEOUT_SECONDS` (default 600s, env-overridable via
  ``ULTRAREVIEW_TIMEOUT_SECONDS``) is the function-default for both
  :func:`trigger`'s ``timeout_seconds`` and :func:`wait_for_result`'s
  ``timeout``. Matches the unit-description literal
  ``wait_for_result(pr_url, timeout=600)``.
* :data:`DEFAULT_TIMEOUT_SECONDS` is a back-compat alias for callers
  that already imported the older two-constant name.

Callers can still pass an explicit ``timeout_seconds`` to
:func:`trigger` and the same value as ``timeout=`` to
:func:`wait_for_result` — the design accepts caller-tightened caps
without leaking cloud-side time.

Surface
-------

Two functions, both named for F-007's task description:

  * :func:`trigger(pr_url)` — spawns ``claude ultrareview <N>
    --json --timeout <m>`` as a background subprocess and records the
    handle keyed by the *canonical* PR URL. Non-blocking, returns
    ``None``.
  * :func:`wait_for_result(pr_url, timeout=...)` — polls the handle
    until the subprocess exits, the timeout elapses, or both. Returns
    ``{"passed": bool, "findings": list[str]}``.

The split lets the orchestrator fire-and-forget the review during the
reviewer-recommends-merge hand-off, then come back to harvest the
verdict after other work — same shape as F-005's ``wait_for_ci``.

Tests inject ``spawn``, ``sleep``, and ``now`` so we never touch the
real ``claude`` CLI nor wall-clock time.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess  # nosec B404 — invoking `claude` is the whole point
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# --------------------------- defaults ---------------------------

# Single source of truth for the timeout, used by BOTH
# :func:`trigger` (forwarded as ``--timeout <minutes>`` to the CLI)
# AND :func:`wait_for_result` (wrapper-side SIGKILL cap). Default
# value 600 matches the unit-description literal
# ``wait_for_result(pr_url, timeout=600)``; env-overridable via
# ``ULTRAREVIEW_TIMEOUT_SECONDS`` for callers who want the CLI's
# documented 30-minute default (set the env to 1800). Keeping
# one constant means wrapper-side and cloud-side timers always fire
# together — no built-in billing gap on the default call shape.
SPEC_WAIT_TIMEOUT_SECONDS = int(os.getenv("ULTRAREVIEW_TIMEOUT_SECONDS", "600"))

# Back-compat alias retained in ``__all__``. Earlier rounds of this
# module shipped two distinct constants (``DEFAULT_TIMEOUT_SECONDS``
# = 1800 + ``SPEC_WAIT_TIMEOUT_SECONDS`` = 600); the split built in a
# 20-minute cloud-billing gap on the default call shape (reviewer M4).
# Collapsed to one source of truth; the alias stays so callers that
# already import ``DEFAULT_TIMEOUT_SECONDS`` don't break.
DEFAULT_TIMEOUT_SECONDS = SPEC_WAIT_TIMEOUT_SECONDS

# Poll interval while waiting. Long enough that we don't spin the CPU on
# a multi-minute run, short enough that the orchestrator reacts within
# the same poll budget cycle_review uses.
DEFAULT_POLL_INTERVAL_SECONDS = int(os.getenv("ULTRAREVIEW_POLL_INTERVAL", "5"))

# Path to the Claude CLI binary. ``claude`` on PATH for the standard
# install; override via env for sandboxed deployments or test stubs.
ULTRAREVIEW_CLI = os.getenv("ULTRAREVIEW_CLI", "claude")

# Tolerates trailing slash, path suffixes (``/files``, ``/commits``),
# query strings (``?w=1``), and fragments (``#issuecomment-…``). The
# captured PR number is enough; whatever follows it is URL structural
# noise the canonicalizer drops.
_PR_URL_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+?)/pull/(\d+)(?:[/?#]|$)")


# --------------------------- internal state ---------------------------


@dataclass
class _RunHandle:
    """In-flight ultrareview subprocess + bookkeeping.

    ``process`` is a :class:`subprocess.Popen` in production and a
    test-injected stub in unit tests; the only methods we call on it are
    ``poll()``, ``communicate()``, ``terminate()``, ``kill()``, and
    ``wait()``.
    """

    canonical_url: str
    pr_number: int
    process: Any


# canonical pr_url → handle. Keys are normalized via :func:`_canonical_pr_url`
# so callers can trigger with one URL spelling (trailing slash, ``/files``
# suffix) and wait with another and still hit the same registry entry.
# Distinct PRs hash to distinct keys, so concurrent runs on different PRs
# don't collide; ``trigger`` on a repeated PR overwrites the prior handle.
_runs: dict[str, _RunHandle] = {}


# --------------------------- public API ---------------------------


def trigger(
    pr_url: str,
    *,
    timeout_seconds: int | None = None,
    spawn: Callable[[list[str]], Any] | None = None,
) -> None:
    """Start an ultrareview run for the PR identified by ``pr_url``.

    Spawns ``claude ultrareview <N> --json --timeout <minutes>`` in the
    background. Non-blocking; call :func:`wait_for_result` to harvest
    the verdict.

    Args:
        pr_url: full ``https://github.com/owner/repo/pull/N`` URL.
            Trailing slashes, path suffixes (``/files``), query strings
            (``?w=1``), and fragments are tolerated; the registry is
            keyed on a canonical form.
        timeout_seconds: the *same* timeout the caller will eventually
            pass to :func:`wait_for_result`. Forwarded as
            ``--timeout <minutes>`` so wrapper-side and cloud-side
            timers fire together — without this, a caller-tightened
            wait timeout SIGKILLs the local subprocess while the
            cloud session keeps running (and billing) until the CLI's
            own ``--timeout`` elapses. Defaults to
            :data:`SPEC_WAIT_TIMEOUT_SECONDS` — the same value
            :func:`wait_for_result` uses, so a default-on-default call
            keeps both timers aligned (no billing gap).
        spawn: injection point for tests. Takes the argv list and
            returns a Popen-shaped object. Defaults to
            :func:`_default_spawn`.

    Re-trigger semantics:
        Calling ``trigger`` twice with the same canonical PR URL
        kills+reaps the prior subprocess via :func:`_kill_and_reap`
        before spawning the new one. This stops the orphaned local
        process AND signals the cloud session to terminate (via the
        CLI's signal-handling) so a retry doesn't leak two parallel
        billing runs.

    Raises:
        ValueError: if ``pr_url`` is not a parseable PR URL.
    """
    canonical, pr_number = _canonical_pr_url(pr_url)
    spawner = spawn if spawn is not None else _default_spawn
    effective_timeout = SPEC_WAIT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    argv = [
        ULTRAREVIEW_CLI,
        "ultrareview",
        str(pr_number),
        # Machine-readable bugs.json on stdout. Without --json the CLI
        # writes a human-formatted report; the gate would have to
        # heuristic-parse banners vs findings.
        "--json",
        # Align the CLI's internal timer with the wrapper-side cap so
        # an early wrapper SIGKILL also stops the cloud session and
        # stops billing. Minutes, per the CLI's flag spec — rounded up
        # so sub-minute caller timeouts still produce a valid value.
        "--timeout",
        str(max(1, (effective_timeout + 59) // 60)),
    ]
    # Re-trigger on the same canonical URL: kill+reap the prior
    # subprocess before storing the new handle. Earlier rounds let the
    # prior handle just fall out of the registry, which orphaned both
    # the local subprocess and the (still-running, still-billing) cloud
    # session for the rest of the orchestrator's lifetime (reviewer M2).
    prior = _runs.pop(canonical, None)
    if prior is not None:
        _kill_and_reap(prior.process)
    process = spawner(argv)
    _runs[canonical] = _RunHandle(
        canonical_url=canonical,
        pr_number=pr_number,
        process=process,
    )


def wait_for_result(
    pr_url: str,
    timeout: int | None = SPEC_WAIT_TIMEOUT_SECONDS,
    *,
    poll_interval_seconds: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Block until the ultrareview run for ``pr_url`` finishes or times out.

    Args:
        pr_url: same URL passed to :func:`trigger` (URL-spelling
            tolerated — canonicalized to match the registry).
        timeout: seconds to wait before giving up. Defaults to
            :data:`SPEC_WAIT_TIMEOUT_SECONDS` (600 — the unit-description
            literal). Pass ``None`` to use the same value internally.
            For aligned wrapper/cloud timers, pass the same value to
            :func:`trigger` via its ``timeout_seconds`` kwarg.
        poll_interval_seconds: poll cadence. Defaults to env /
            :data:`DEFAULT_POLL_INTERVAL_SECONDS`.
        sleep / now: injection points for tests.

    Returns:
        ``{"passed": bool, "findings": list[str]}`` where:

        * ``passed`` is ``True`` iff the subprocess exited 0 *and* the
          parsed ``bugs.json`` payload contained zero bugs. The gate
          fails *closed* on rc==0 with unparseable / schema-drifted
          stdout — see :func:`_parse_bugs`.
        * ``findings`` is a list of human-rendered bug entries
          (``"path:line — summary"``). Carries sentinel entries on
          schema drift (``"[ultrareview …]"``) and on timeout
          (``"[timeout after Ns]"``).

    Raises:
        RuntimeError: if no prior :func:`trigger` was issued for this URL.
    """
    canonical, _ = _canonical_pr_url(pr_url)
    handle = _runs.get(canonical)
    if handle is None:
        raise RuntimeError(f"no ultrareview run registered for {pr_url!r}; call trigger() first")

    timeout_seconds = SPEC_WAIT_TIMEOUT_SECONDS if timeout is None else timeout
    poll = DEFAULT_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds

    start = now()
    while True:
        rc = handle.process.poll()
        if rc is not None:
            stdout, _stderr = handle.process.communicate()
            return _interpret(rc, stdout or "")

        elapsed = now() - start
        if elapsed >= timeout_seconds:
            _kill_and_reap(handle.process)
            return {
                "passed": False,
                "findings": [f"[timeout after {timeout_seconds}s]"],
            }

        sleep(poll)


# --------------------------- internals ---------------------------


def _canonical_pr_url(pr_url: str) -> tuple[str, int]:
    """Parse and normalize ``pr_url`` into ``(canonical_url, pr_number)``.

    The canonical form is the bare ``https://github.com/owner/repo/pull/N``
    — no trailing slash, no ``/files``/``/commits``/etc. suffix.
    Using this as the registry key means callers can trigger and wait
    with different URL spellings of the same PR and still hit the same
    entry.
    """
    m = _PR_URL_RE.match(pr_url)
    if not m:
        raise ValueError(f"not a github PR url: {pr_url!r}")
    owner, repo, num_str = m.group(1), m.group(2), m.group(3)
    pr_number = int(num_str)
    return f"https://github.com/{owner}/{repo}/pull/{pr_number}", pr_number


def _default_spawn(argv: list[str]) -> subprocess.Popen[str]:
    """Real-world subprocess launch. Tests inject a stub instead.

    stdout is captured (text mode) so :func:`wait_for_result` can read
    the ``bugs.json`` payload via ``.communicate()``. stderr — which
    carries progress chatter + the live-session URL across a multi-
    minute run — is sent to ``DEVNULL`` because (a) we don't surface
    it, and (b) buffering it via PIPE risked a write-side deadlock if
    the chatter exceeded the OS pipe buffer before we drained it.
    stdin is pinned to ``DEVNULL`` rather than inherited: when the
    orchestrator runs as the MCP stdio server, the parent's fd 0 IS
    the JSON-RPC transport, and a child reading from it would either
    intercept transport bytes or block forever on ``read()``.
    We pass the argv list rather than a shell string so there's no
    shell-injection surface.
    """
    return subprocess.Popen(  # nosec B603 — argv list, no shell; binary path from env
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _interpret(returncode: int, stdout: str) -> dict[str, Any]:
    """Translate ``(rc, stdout)`` into the public result shape.

    ``passed`` is True only when the review completed cleanly *and*
    found no bugs. ``rc != 0`` means the review never produced a
    verdict (launch failure / session error / CLI timeout); ``rc == 0``
    with a non-empty ``bugs.json`` means the review ran and *did* find
    bugs — the exact case the F-007 gate exists to catch.
    """
    bugs = _parse_bugs(stdout, completed=(returncode == 0))
    passed = returncode == 0 and len(bugs) == 0
    return {"passed": passed, "findings": bugs}


def _parse_bugs(stdout: str, *, completed: bool) -> list[str]:
    """Render the ``bugs.json`` payload on stdout into a list of findings.

    The gate **fails closed** on the rc==0 path: a buggy PR whose
    ultrareview run produces unparseable or schema-drifted stdout
    surfaces a sentinel finding, which flips
    :func:`_interpret`'s ``passed`` bit to ``False``. That's the
    direction a safety gate must default to — endorsing a merge based
    on output we couldn't read is the case this module exists to
    prevent.

    Three rc==0 branches:

    * **Empty stdout** → ``[]``. The CLI shipping no bugs.json at all
      is the only "trust the completion bit" path; ``passed=True``
      lands here.
    * **Non-JSON stdout** → one sentinel finding wrapping the raw
      output (``"[ultrareview output not parseable as JSON: …]"``).
      The operator sees what the CLI actually emitted so they can
      decide what to do.
    * **JSON dict without the ``bugs`` key** → one sentinel finding
      (``"[ultrareview output schema unrecognized: keys=…]"``). The
      research-preview schema may rename ``bugs`` to ``findings`` /
      ``results`` / etc.; we'd rather block and surface the schema
      than silently treat a missing key as "no bugs."

    On the rc != 0 path (``completed=False``) the gate is already
    closed via rc; non-JSON output is still surfaced as a finding so
    operators see error banners.
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if not completed:
            return [text]
        # Truncate the raw text so a huge non-JSON dump doesn't
        # balloon the surfaced finding — the operator just needs
        # enough context to debug.
        snippet = text if len(text) <= 500 else text[:500] + "…"
        return [f"[ultrareview output not parseable as JSON: {snippet}]"]

    if isinstance(data, list):
        return [_format_bug(b) for b in data]
    if isinstance(data, dict):
        if "bugs" not in data:
            keys = sorted(data.keys())
            return [f"[ultrareview output schema unrecognized: keys={keys}]"]
        bugs = data["bugs"]
        if not isinstance(bugs, list):
            # null / int / str / dict for the ``bugs`` slot is valid JSON
            # but non-iterable in the format loop — and would propagate
            # an uncaught TypeError out of ``wait_for_result`` if we
            # didn't catch it here. Same fail-closed direction as the
            # missing-key path: surface a schema sentinel.
            return [
                f"[ultrareview output schema unrecognized: "
                f"bugs is {type(bugs).__name__}, expected list]"
            ]
        return [_format_bug(b) for b in bugs]
    return [json.dumps(data, sort_keys=True)]


def _format_bug(bug: Any) -> str:
    """Render one ``bugs.json`` entry as ``path:line — summary``.

    Defensive about the exact schema: ``claude ultrareview --json`` is
    a research-preview surface and the field names may evolve. Falls
    back to the JSON-stringified entry when none of the expected
    summary fields are present.
    """
    if not isinstance(bug, dict):
        return str(bug)
    path = bug.get("path") or bug.get("file") or "?"
    line = bug.get("line") or bug.get("start_line") or "?"
    summary = (
        bug.get("summary")
        or bug.get("message")
        or bug.get("description")
        or bug.get("title")
        or json.dumps(bug, sort_keys=True)
    )
    return f"{path}:{line} — {summary}"


def _kill_and_reap(process: Any) -> None:
    """SIGTERM → SIGKILL → ``communicate()`` the subprocess on timeout.

    The ``communicate()`` step matters for two reasons:

    1. It reaps the zombie — without it a real :class:`subprocess.Popen`
       would leave one until GC, and since ``_runs`` retains handles
       past :func:`wait_for_result`'s return that could be the
       orchestrator's full lifetime.
    2. It drains the PIPE'd stdout — ``wait()`` alone reaps the zombie
       but leaves the read-side fd open until the ``Popen`` object is
       collected, leaking one fd per timeout. ``communicate()`` does
       both in one call.

    Best-effort throughout — a stub that won't honor
    ``terminate``/``kill``/``communicate`` still gets a clean return
    contract.
    """
    with contextlib.suppress(Exception):
        process.terminate()
    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        # Short timeout: at this point the process should already be
        # dying (SIGTERM + SIGKILL above). One-second budget reaps the
        # zombie + drains the PIPE without blocking the orchestrator.
        process.communicate(timeout=1)


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "SPEC_WAIT_TIMEOUT_SECONDS",
    "ULTRAREVIEW_CLI",
    "trigger",
    "wait_for_result",
]
