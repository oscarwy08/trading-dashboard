#!/bin/bash
# Reclaim unreachable trading.db blobs left by the history squash.
# Run only when trading-cycle is not active. A second pack-objects on top of
# one already running will use the same multi-gigabyte working set.
set -u
cd "$(dirname "$0")/.."
if pgrep -f pack-objects >/dev/null 2>&1; then
  echo "pack-objects is already running; not starting another"
  exit 1
fi
git config gc.auto 0
git config gc.autopacklimit 0
git config maintenance.auto false
git gc --prune=now
echo "idle repack finished"
