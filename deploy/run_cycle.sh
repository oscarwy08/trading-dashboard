#!/bin/bash
# VPS trading cycle. systemd trading-cycle.timer runs /home/bot/run_cycle.sh.
# That path is outside the repo. This file is the copy.
# After it is on main, install it with:
#   cp /home/bot/trading-dashboard/deploy/run_cycle.sh /home/bot/run_cycle.sh
# The filename has an underscore between run and cycle.
set -u
REPO=${TRADING_CLONE:-/home/bot/trading-dashboard}
LOCK=${TRADING_CYCLE_LOCK:-/tmp/trading-cycle.lock}
STAMP=${COLLECT_FULL_STAMP:-$REPO/.git/collect-full-at}
EVERY=${COLLECT_FULL_EVERY:-900}
cd "$REPO" || { echo "ERROR: cannot cd to $REPO" >&2; exit 1; }
exec 9>"$LOCK" || { echo "ERROR: cannot open $LOCK" >&2; exit 1; }
flock -n 9 || { echo "cycle already running; skip"; exit 0; }
echo "=== vps cycle $(date '+%F %T %Z') ==="
# Shallow sync before anything else, inside this lock. ci_cycle.sh repeats it
# when the checkout is already shallow, including before this file is installed.
export VPS_CYCLE=1
bash "$REPO/scripts/git_slim.sh" sync || { echo "ERROR: shallow sync failed" >&2; exit 1; }
now=$(date +%s)
last=0
if [ -f "$STAMP" ]; then
  last=$(tr -cd '0-9' < "$STAMP" || true)
  [ -n "${last:-}" ] || last=0
fi
# Full collector about every 15 minutes. Other ticks re-price only.
if [ $((now - last)) -ge "$EVERY" ]; then
  unset COLLECT_MODE
  echo "$now" > "$STAMP" || echo "WARNING: could not write collect stamp" >&2
  echo "[cycle] full collector"
else
  export COLLECT_MODE=fast
  echo "[cycle] fast collector"
fi
export PY="${PY:-python3}"
bash scripts/ci_cycle.sh
rc=$?
if ! BOT_NAME="${BOT_NAME:-vps-cycle}" \
     BOT_EMAIL="${BOT_EMAIL:-vps-cycle@users.noreply.github.com}" \
     bash scripts/ci_push.sh; then
  rc=1
fi
exit "$rc"
