#!/usr/bin/env bash
# Snapshot state.db to snapshots/ directory. Keeps the last 30 snapshots.
# Run manually, or wire into cron / launchd for periodic backups.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f state.db ]; then
  echo "no state.db to snapshot"
  exit 0
fi

mkdir -p snapshots
ts=$(date +%Y%m%d-%H%M%S)
target="snapshots/state.db.${ts}"

# Use sqlite3 .backup for a consistent online snapshot (safer than cp during writes)
sqlite3 state.db ".backup '${target}'"

# Keep the 30 most-recent snapshots; delete older
# shellcheck disable=SC2012
ls -1t snapshots/state.db.* 2>/dev/null | tail -n +31 | xargs -r rm

echo "snapshot: ${target}"
