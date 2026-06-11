"""Credential-hardening helpers shared by CLI entry points and the MCP boot.

F-016-U-7 § "Credential hardening". Three concerns:

  * **``ANTHROPIC_API_KEY`` format validation.** Every entry point that
    will spawn workers (``orchestrator run``, ``orchestrator daemon
    start``, ``orchestrator doctor``, the MCP server boot) refuses to
    start if the resolved key doesn't match the well-known ``sk-ant-``
    prefix. Closes the foot-gun where a stale shell-rc export
    (``export ANTHROPIC_API_KEY='lkj'`` in ``~/.zshrc``) shadows the
    real key in ``.env`` and every worker spawn fails with an opaque
    Anthropic 401.
  * **``ANTHROPIC_AUTH_TOKEN`` stripping.** ``ANTHROPIC_API_KEY`` and
    ``ANTHROPIC_AUTH_TOKEN`` are both consulted by Anthropic's SDK and
    Claude Code; either one inherited from the parent shell can shadow
    the ``.env``-loaded value. ``orchestrator run`` strips both before
    handing off to Claude Code so credentials reach the MCP server only
    from ``.env``.
  * **Env-vs-``.env`` shadowing detection.** ``orchestrator doctor``
    cross-references the resolved process env against the values
    parsed from ``.env``; any divergence is a "shadowing detected"
    diagnostic naming both sources so the operator knows which to fix.

The helpers in this module are deliberately pure (no I/O beyond the
``.env`` read in :func:`read_env_file_values`) so the call sites can
compose them without dragging Click / Rich into the daemon's import
graph.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------- constants

ANTHROPIC_KEY_PREFIX = "sk-ant-"
"""The well-known Anthropic console key prefix. Both API keys and OAuth
``ANTHROPIC_AUTH_TOKEN`` values start with this prefix today; we validate
``ANTHROPIC_API_KEY`` against it as a coarse "is this even an Anthropic
credential" gate (full validity checks happen on first API call)."""


SHADOWING_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_PRIVATE_KEY",
    "NTFY_TOPIC",
    "ORCH_WORKER_BACKEND",
    "ORCH_DAEMON_DRIVE",
    "ORCH_DOCKER_WORKER_IMAGE",
    "ORCH_HEALTH_SNAPSHOT_INTERVAL_HOURS",
)
"""Env vars ``orchestrator doctor`` compares against ``.env`` to detect
shadowing. New env vars that influence orchestrator behavior should be
added here; the list is the source of truth for the doctor audit."""


# ---------------------------------------------------------------- parsing

# Match ``KEY=value`` lines. Comments (``# ...``) and blank lines are
# skipped. The value is everything after the first ``=`` on the line,
# stripped of surrounding whitespace and one optional layer of quotes
# (the shell-style ``KEY="value"`` form that python-dotenv accepts).
_ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read_env_file_values(env_path: Path) -> dict[str, str]:
    """Return ``{KEY: value}`` parsed from ``env_path``.

    Empty dict when the file is missing or unreadable — the caller
    decides whether that's a hard error (``orchestrator run`` exits 1
    if ``.env`` is missing) or a soft skip (``orchestrator doctor``
    reports the missing-file finding and continues).

    Quoted values lose ONE surrounding quote pair (so a key written as
    ``KEY="sk-ant-..."`` is reported as ``sk-ant-...`` to keep the
    comparison with the resolved env value sensible). Inline ``#``
    comments are NOT supported — python-dotenv treats them literally
    when no whitespace precedes them, and the orchestrator's ``.env``
    template never emits inline comments anyway.
    """
    if not env_path.exists():
        return {}
    try:
        text = env_path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


# ---------------------------------------------------------------- API-key validation


def is_valid_anthropic_key(value: str) -> bool:
    """Coarse ``ANTHROPIC_API_KEY`` shape check.

    The Anthropic SDK validates on first request; this is a pre-flight
    "does the prefix even look right?" gate so an obvious stale shell
    export fails fast with an actionable diagnostic instead of an
    opaque 401 minutes later inside a Managed Agent.
    """
    return bool(value) and value.startswith(ANTHROPIC_KEY_PREFIX)


def anthropic_key_diagnostic(resolved_value: str, env_file_value: str = "") -> str:
    """Format the per-spec diagnostic when ``ANTHROPIC_API_KEY`` is bad.

    Matches the spec verbatim: when the ``.env`` has a valid-looking
    key but the resolved value doesn't, name the foot-gun
    (``~/.zshrc`` / ``.bashrc`` / ``.zprofile`` stale exports) so the
    operator knows where to look.
    """
    prefix = (resolved_value[:8] + "…") if resolved_value else "(empty)"
    if env_file_value and is_valid_anthropic_key(env_file_value):
        return (
            f"ANTHROPIC_API_KEY in env doesn't look like an Anthropic key "
            f"(got {prefix}). Check ~/.zshrc / .bashrc / .zprofile for "
            f"stale exports; your .env has a valid-looking key that's "
            f"being shadowed."
        )
    return (
        f"ANTHROPIC_API_KEY in env doesn't look like an Anthropic key "
        f"(got {prefix}). Expected a value starting with "
        f"'{ANTHROPIC_KEY_PREFIX}' — fix the value in .env (or unset the "
        f"stale shell export shadowing it)."
    )


# ---------------------------------------------------------------- shadowing detection


def detect_env_shadowing(
    process_env: dict[str, str],
    env_file_values: dict[str, str],
    *,
    keys: tuple[str, ...] = SHADOWING_ENV_VARS,
) -> list[dict[str, str]]:
    """Find env vars whose process value diverges from the ``.env`` value.

    Returns one finding per shadowed key: ``{name, env_value,
    file_value}``. ``env_value`` / ``file_value`` may be empty strings
    if one side is unset. A key absent from both sides produces no
    finding.

    The shadowing diagnostic exists because the F-016-U-7 spec's
    "credential foot-gun" walkthrough hinges on a stale shell-rc
    export silently shadowing the ``.env``-loaded value — the operator
    sees worker spawns fail with an opaque Anthropic 401 and can't
    tell which source is wrong. The finding names both sources so the
    operator knows which to fix.
    """
    findings: list[dict[str, str]] = []
    for name in keys:
        env_value = process_env.get(name, "")
        file_value = env_file_values.get(name, "")
        if env_value and file_value and env_value != file_value:
            findings.append({"name": name, "env_value": env_value, "file_value": file_value})
    return findings


def _redact(value: str) -> str:
    """Single-source redaction for shadowing findings.

    Credentials never round-trip to the user's terminal — show only the
    first few characters so the operator can tell the two sides apart
    without leaking the full secret. Empty input is reported as
    ``(unset)`` for clarity in the doctor's table.
    """
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return value[:2] + "…"
    return value[:8] + "…"


def format_shadowing_finding(finding: dict[str, str]) -> str:
    """One-line human-readable shadowing diagnostic for ``doctor``.

    The leading "shadowing detected" tag matches the spec verbatim
    ("Flag any env var where the shell value differs from the .env
    value with a 'shadowing detected' warning").
    """
    return (
        f"shadowing detected: {finding['name']} — "
        f"shell={_redact(finding['env_value'])} "
        f"vs .env={_redact(finding['file_value'])}; "
        f"unset the stale shell export or align the two values."
    )
