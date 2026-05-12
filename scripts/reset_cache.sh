#!/usr/bin/env bash
# Drop all cached_resources rows. Use when routine signature-based
# invalidation doesn't cover your case (Anthropic API change, stale
# agent_id, debugging).
#
# Routine changes (prompt edits, model/networking/tools config edits)
# auto-invalidate the cache via resource_signature in agents.py — you
# don't need to run this for those.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f state.db ]; then
  echo "no state.db — nothing to reset"
  exit 0
fi

before=$(sqlite3 state.db "SELECT COUNT(*) FROM cached_resources;" 2>/dev/null || echo "0")
sqlite3 state.db "DELETE FROM cached_resources;"
echo "cleared ${before} cached_resources row(s)"
echo "next spawn will create fresh agent + environment on Anthropic's side"
