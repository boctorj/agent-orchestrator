"""Orchestrator MCP server — entry point.

All MCP tools live in `orchestrator/tools/` submodules. This file is a thin
launcher that:
  1. Loads `.env` so submodules can read GITHUB_TOKEN / NTFY_TOPIC / etc.
  2. Imports every tool submodule to trigger `@mcp.tool()` registration.
  3. Initializes the SQLite state DB (inside `main()` — see note below).
  4. Calls `mcp.run()` for stdio MCP serving.

Run via: `python -m orchestrator.mcp_server` (typically spawned by Claude
Code through `.mcp.json`).

NOTE: state.init_db() lives inside `main()`, not at module import time.
That keeps `from orchestrator import mcp_server` side-effect-free w.r.t.
the filesystem — `orchestrator doctor` (and any other importer) can probe
the module without silently creating state.db.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from orchestrator import env_guard, state
from orchestrator.tools import mcp

load_dotenv()

# Import each tool submodule for its side effect of decorating @mcp.tool().
# These imports must come after load_dotenv so submodules see env vars
# from .env if they read os.getenv() at import time. Ruff E402 is correct
# in general but doesn't apply here; the noqa keeps the lint green.
from orchestrator.tools import (  # noqa: E402
    execution,  # noqa: F401
    health,  # noqa: F401
    observability,  # noqa: F401
    ops,  # noqa: F401
    planning,  # noqa: F401
    scheduling,  # noqa: F401
)


def main() -> None:
    # F-016-U-7 credential hardening (b): refuse to serve MCP tools
    # when ``ANTHROPIC_API_KEY`` doesn't pass the shape check. Without
    # this gate a stale shell-rc export silently shadows ``.env`` and
    # every ``spawn_unit`` returns an opaque Anthropic 401 minutes
    # later. The diagnostic goes to stderr so Claude Code surfaces it
    # in the MCP server logs.
    resolved = os.environ.get("ANTHROPIC_API_KEY", "")
    if not env_guard.is_valid_anthropic_key(resolved):
        from pathlib import Path  # noqa: PLC0415 — cold-path only

        env_file_value = env_guard.read_env_file_values(Path(".env")).get("ANTHROPIC_API_KEY", "")
        diag = env_guard.anthropic_key_diagnostic(resolved, env_file_value)
        print(f"orchestrator MCP server: refusing to start — {diag}", file=sys.stderr)
        raise SystemExit(1)

    # Initialize schema before serving any tool calls. Idempotent.
    state.init_db()
    mcp.run()


if __name__ == "__main__":
    main()
