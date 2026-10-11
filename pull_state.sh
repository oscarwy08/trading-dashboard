#!/bin/bash
# Fetch the latest repo state (engine code + data/trading.db + docs/ site) from GitHub and mirror it into
# /workspace/trading, so the AI scorecard runs on current data. The box no longer trades: GitHub Actions is the single
# source of truth and the box copy is a read-only mirror (local edits to tracked files are overwritten).
# Usage: ./pull_state.sh [--summary] [--snapshot]
#   --summary   print the scorecard (daily_summary.py) and save it to data/daily_summary.txt
#   --snapshot  render docs/snapshot.png (headless Chrome) for the scorecard
set -u
. /workspace/trading/repo_lib.sh
LOG="$BASE/logs/pull_state.log"
exec 9>/tmp/trading_repo.lock; flock -w 180 9 || { echo "repo busy" >&2; exit 1; }
repo_auth || exit 1
for i in 1 2 3; do repo_sync && break; sleep $((i * 5)); done || { echo "ERROR: fetch failed" >&2; exit 1; }
n=0
while IFS= read -r -d '' f; do
  case "$f" in data/sentiment/*) continue ;; esac        # the box drop folder is separate (see push_sentiment.sh)
  mkdir -p "$BASE/$(dirname "$f")"
  if ! cmp -s "$REPO_DIR/$f" "$BASE/$f"; then
    cp -f "$REPO_DIR/$f" "$BASE/$f.pull.$$" && mv -f "$BASE/$f.pull.$$" "$BASE/$f"; n=$((n + 1))   # atomic replace
  fi
done < <(G ls-files -z)
echo "[$(date '+%F %T %Z')] pulled $(G log -1 --format='%h %cd' --date=iso) - $n file(s) updated" | tee -a "$LOG"
cd "$BASE" || exit 1
for a in "$@"; do
  case "$a" in
    --summary) python3 daily_summary.py | tee data/daily_summary.txt ;;
    --snapshot) python3 snapshot.py ;;
  esac
done
