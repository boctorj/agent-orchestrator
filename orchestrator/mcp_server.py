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

from dotenv import load_dotenv

from orchestrator import state
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
    # Initialize schema before serving any tool calls. Idempotent.
    state.init_db()
    mcp.run()


if __name__ == "__main__":
    main()
