#!/usr/bin/env bash
set -euo pipefail

# Stage 1 launcher: starts Claude Code as the lead agent with Remote Control.
# The MCP server is auto-started by Claude Code as a subprocess via
# .claude/settings.json — no separate process to manage.

cd "$(dirname "$0")"

# Activate venv if present
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Sanity-check that .env exists and has the API key — without exporting it.
# We deliberately do NOT put ANTHROPIC_API_KEY into claude's environment, so
# claude itself uses your claude.ai Team-plan token for the lead session.
# The MCP server subprocess (spawned by claude) loads ANTHROPIC_API_KEY from
# .env via python-dotenv for Managed Agents API calls.
if [ ! -f .env ]; then
  echo "Missing .env — copy .env.example and fill in ANTHROPIC_API_KEY"
  exit 1
fi
if ! grep -qE '^ANTHROPIC_API_KEY=sk-ant-' .env; then
  echo "ANTHROPIC_API_KEY not set in .env (value must start with sk-ant-)"
  exit 1
fi

# Belt-and-suspenders: clear from the launching shell if it leaked in.
unset ANTHROPIC_API_KEY

# Launch Claude Code with remote control enabled.
# Verify the exact flag with `claude --help` — recent feature, name may vary.
exec claude --remote-control
