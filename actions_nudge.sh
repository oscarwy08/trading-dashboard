#!/bin/bash
# Backup for GitHub's unreliable schedule: if no cycle has run in 20+ min, run one from the box (same code, same repo).
source /workspace/trading/.github_token
R=oscarwy08/trading-dashboard
last=$(curl -s -H "Authorization: Bearer $GH_TOKEN" "https://api.github.com/repos/$R/commits?per_page=1" | python3 -c "import json,sys;print(json.load(sys.stdin)[0]['commit']['committer']['date'])" 2>/dev/null)
[ -z "$last" ] && exit 0
age=$(( $(date +%s) - $(date -d "$last" +%s) ))
if [ $age -gt 1200 ]; then
  echo "[$(date '+%F %T')] last commit ${age}s ago - running backup cycle on the box"
  FORCE_BOX=1 /workspace/trading/box_cycle.sh >> /workspace/trading/logs/box_cycle.log 2>&1
fi
