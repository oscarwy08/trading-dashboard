#!/bin/bash
# INTERIM runner: runs the SAME cycle as GitHub Actions (scripts/ci_cycle.sh + scripts/ci_push.sh) from the box clone
# /workspace/trading-repo against the repo state, so the repo stays the single source of truth even before Actions runs.
# Only needed while the workflow cannot be installed (fine-grained PAT lacks 'Workflows: Read and write').
# Each run:
#   1. sync the clone to origin/main (picks up sentiment / KILL / RISK_RESUME pushed meanwhile)
#   2. if .github/workflows/cycle.yml is missing, try to install it from scripts/github-workflow-cycle.yml
#      (succeeds automatically once the token has the Workflows permission)
#   3. handover: if the workflow exists and a cycle.yml run has succeeded in the last 3 h -> Actions is live: comment out
#      this cron entry and stop. If the workflow exists but Actions has not succeeded yet -> skip (avoid two writers)
#      for up to 90 min after the workflow appeared, then fall back to running the cycle here.
#   4. run ci_cycle.sh + ci_push.sh, then mirror the repo into /workspace/trading (pull_state.sh).
set -u
. /workspace/trading/repo_lib.sh
log() { echo "[$(date '+%F %T %Z')] $*"; }
API="https://api.github.com/repos/$REPO_SLUG"
api() { curl -s -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" "$API$1"; }
exec 9>/tmp/trading_repo.lock; flock -w 600 9 || { log "repo busy, skip"; exit 1; }
repo_auth || exit 1
git -C "$REPO_DIR" config credential.helper "" 2>/dev/null
for i in 1 2 3; do repo_sync && break; sleep $((i * 5)); done || { log "ERROR: sync failed"; exit 1; }
cd "$REPO_DIR" || exit 1
WF=.github/workflows/cycle.yml; PENDING=scripts/github-workflow-cycle.yml
if [ ! -f "$WF" ] && [ -f "$PENDING" ]; then
  mkdir -p .github/workflows && cp "$PENDING" "$WF" && G add "$WF" && G commit -q -m "Install the GitHub Actions trading cycle (box_cycle.sh)"
  if G push -q origin HEAD:main 2>/dev/null; then log "workflow installed on main - Actions takes over"
  else G reset -q --hard origin/main; rm -rf .github; log "workflow push rejected (token lacks 'Workflows: Read and write') - cycle runs on the box"; fi
fi
if [ -f "$WF" ]; then
  ok=$(api "/actions/workflows/cycle.yml/runs?status=success&per_page=1" | python3 -c 'import json,sys,datetime as D
try:
    r=json.load(sys.stdin)["workflow_runs"][0]; t=D.datetime.fromisoformat(r["updated_at"].replace("Z","+00:00"))
    print(1 if D.datetime.now(D.timezone.utc)-t < D.timedelta(hours=3) else 0)
except Exception: print(0)')
  if [ "$ok" = 1 ]; then
    log "GitHub Actions cycle is live - removing the box_cycle.sh cron entry (box no longer trades)"
    crontab -l 2>/dev/null | sed -E 's@^([^#].*box_cycle\.sh.*)$@#REMOVED-v4 (GitHub Actions live) \1@' | crontab -
    exit 0
  fi
  since=$(api "/commits?path=$WF&per_page=1" | python3 -c 'import json,sys,datetime as D
try: print(int(D.datetime.fromisoformat(json.load(sys.stdin)[0]["commit"]["committer"]["date"].replace("Z","+00:00")).timestamp()))
except Exception: print(0)')
  if [ $(( $(date +%s) - since )) -lt 5400 ]; then log "workflow present, waiting for the first successful Actions run - skip"; exit 0; fi
  log "WARNING: workflow present for > 90 min but no successful Actions run - running the cycle on the box as fallback"
fi
log "cycle on the box (interim)"
PY=python3 bash scripts/ci_cycle.sh; rc=$?
BOT_NAME="box-cycle" BOT_EMAIL="box-ai-routine@users.noreply.github.com" bash scripts/ci_push.sh 2>&1 | sed -E 's/(gh[pousr]_|github_pat_)[A-Za-z0-9_]+/[REDACTED]/g'
flock -u 9; exec 9>&-
"$BASE/pull_state.sh" >/dev/null 2>&1 || log "pull_state failed"
exit $rc
