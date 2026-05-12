#!/usr/bin/env bash
# Launch the orchestrator TUI dashboard.
# Run in a separate terminal pane (or tmux split) while Claude Code runs.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "missing .venv — run pip install -e . first"
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate
exec python -m orchestrator.dashboard
