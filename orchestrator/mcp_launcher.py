"""Minimum-env launcher for the orchestrator MCP server.

Cross-platform (Linux/macOS/Windows). Strips the parent process's
environment down to a small allowlist before re-execing the actual MCP
server. Reduces blast radius if the subprocess is somehow compromised:
without inherited env, an attacker inside the MCP process can't read
ANTHROPIC_API_KEY, GITHUB_TOKEN, AWS credentials, SSH agent env, or
anything else from the user's shell.

The MCP server reads its own secrets from `.env` via `python-dotenv`,
so a minimal env is enough.

Wired in via `.mcp.json`:

    {
      "mcpServers": {
        "orchestrator": {
          "command": ".../.venv/bin/python",
          "args": ["-m", "orchestrator.mcp_launcher"]
        }
      }
    }

The launcher then exec's `python -m orchestrator.mcp_server` with the
minimized env.
"""

from __future__ import annotations

import os
import sys

# Variables the launcher passes through to the MCP server subprocess.
# Everything else from the parent's os.environ is dropped.
#
# - HOME / USERPROFILE / HOMEDRIVE / HOMEPATH: where ~/.gitconfig, ~/.cache, etc. live
# - PATH: rarely needed (MCP server makes no shell subprocess calls today),
#   but we keep it minimal-but-present in case a dep wants it
# - TMPDIR / TEMP / TMP: where Python writes temp files
# - LANG / LC_ALL / LC_CTYPE: locale for unicode handling
# - PYTHONIOENCODING: forced to utf-8 below
# - PYTHONPATH: extended with cwd so `import orchestrator` works
# - SYSTEMROOT (Windows): required by some Windows Python internals
# - APPDATA / LOCALAPPDATA (Windows): user-config locations
ALLOWLIST: tuple[str, ...] = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "SYSTEMROOT",
    "APPDATA",
    "LOCALAPPDATA",
)


def build_min_env() -> dict[str, str]:
    """Return the minimized env dict for the subprocess."""
    parent = dict(os.environ)
    filtered = {k: parent[k] for k in ALLOWLIST if k in parent}
    # Always force utf-8 so the MCP stdio protocol doesn't choke on emoji
    filtered["PYTHONIOENCODING"] = "utf-8"
    # Make sure `orchestrator` is importable from the project root, in case
    # the user launched from somewhere else
    cwd = os.getcwd()
    if "PYTHONPATH" in filtered and filtered["PYTHONPATH"]:
        filtered["PYTHONPATH"] = cwd + os.pathsep + filtered["PYTHONPATH"]
    else:
        filtered["PYTHONPATH"] = cwd
    return filtered


def main() -> None:
    new_env = build_min_env()
    os.environ.clear()
    os.environ.update(new_env)
    # exec replaces this process — same PID, no extra subprocess hop.
    # We invoke the SAME python interpreter we're running under
    # (sys.executable) so we don't depend on `python` being on PATH.
    # Intentional no-shell exec: argv is a static literal list, not user-
    # influenced. The whole point of this launcher is min-env subprocess.
    os.execve(  # nosec B606
        sys.executable,
        [sys.executable, "-m", "orchestrator.mcp_server"],
        new_env,
    )


if __name__ == "__main__":
    main()
