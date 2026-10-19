#!/bin/bash
# Manual repack for a full clone, only while trading-cycle is stopped.
# The VPS cycle does not use this. A shallow checkout drops old loose objects
# with scripts/git_slim.sh (git prune, 60s cap). git gc during a cycle is what
# blocked pushes; do not put it back on the timer.
# A second pack-objects on top of one already running uses the same working set.
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
