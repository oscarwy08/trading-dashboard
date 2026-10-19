#!/bin/bash
# Keep the VPS checkout small. Every cycle commits data/trading.db, and that
# blob does not delta. gc.auto stays off: a repack during a cycle blocks the
# push. After `git fetch --depth 1`, older commits are unreachable once the
# reflog is expired, and `git prune` deletes the loose objects. This does not
# run `git gc`.
# Usage, from the repo root: scripts/git_slim.sh sync | prune
# sync: if / has under 3GB free, prune, then fetch --depth 1 and hard-reset.
# prune: at most once every GIT_SLIM_EVERY_HOURS (default 6), unless sync
# already decided the disk is low. Stops if it would run longer than 60s.
set -u
if [ -n "${GIT_SLIM_ROOT:-}" ]; then
  cd "$GIT_SLIM_ROOT" || { echo "[git-slim] ERROR: cannot enter GIT_SLIM_ROOT" >&2; exit 1; }
fi
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "[git-slim] ERROR: not a git checkout ($(pwd))" >&2
  exit 1
fi
cd "$(git rev-parse --show-toplevel)" || exit 1

EVERY_HOURS=${GIT_SLIM_EVERY_HOURS:-6}
TIMEOUT=${GIT_SLIM_PRUNE_TIMEOUT:-60}
MIN_FREE_KB=${GIT_SLIM_MIN_FREE_KB:-$((3 * 1024 * 1024))}
STAMP=.git/slim-prune-stamp
STARTED=$(date +%s)
if [ -n "${GIT_SLIM_PRUNE_CMD:-}" ]; then
  PRUNE_INNER=$GIT_SLIM_PRUNE_CMD
else
  PRUNE_INNER='git reflog expire --expire=now --expire-unreachable=now --all && git prune --expire now'
fi

free_kb() {
  if [ -n "${GIT_SLIM_FREE_KB:-}" ]; then
    printf '%s\n' "$GIT_SLIM_FREE_KB"
    return 0
  fi
  df -Pk / | awk 'NR==2 {print $4}'
}

disk_low() {
  local free
  free=$(free_kb)
  case "$free" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$free" -lt "$MIN_FREE_KB" ]
}

warn_disk() {
  local free mb limit
  free=$(free_kb)
  case "$free" in
    ''|*[!0-9]*) mb='?' ;;
    *) mb=$((free / 1024)) ;;
  esac
  limit=$((MIN_FREE_KB / 1024))
  echo "DISK WARNING: ${mb}MB free on / (limit ${limit}MB). $1"
  echo "DISK WARNING: ${mb}MB free on / (limit ${limit}MB). $1" >&2
}

disable_auto_gc() {
  git config gc.auto 0 2>/dev/null || true
  git config gc.autopacklimit 0 2>/dev/null || true
  git config maintenance.auto false 2>/dev/null || true
  export GIT_CONFIG_COUNT=3
  export GIT_CONFIG_KEY_0=gc.auto
  export GIT_CONFIG_VALUE_0=0
  export GIT_CONFIG_KEY_1=gc.autopacklimit
  export GIT_CONFIG_VALUE_1=0
  export GIT_CONFIG_KEY_2=maintenance.auto
  export GIT_CONFIG_VALUE_2=false
}

budget_left() {
  local left
  left=$((TIMEOUT - ($(date +%s) - STARTED)))
  if [ "$left" -lt 0 ]; then left=0; fi
  echo "$left"
}

stamp_write() { date +%s > "$STAMP" 2>/dev/null || true; }

prune_due() {
  local now last
  now=$(date +%s)
  last=0
  if [ -f "$STAMP" ]; then
    last=$(tr -cd '0-9' < "$STAMP" || true)
    [ -n "$last" ] || last=0
  fi
  [ $((now - last)) -ge $((EVERY_HOURS * 3600)) ]
}

# Always returns 0. A slow prune must not fail the cycle.
prune_now() {
  local why=$1 left t0 rc
  if pgrep -f '[p]ack-objects' >/dev/null 2>&1; then
    echo "[git-slim] skip prune ($why); pack-objects is already running"
    return 0
  fi
  left=$(budget_left)
  if [ "$left" -le 0 ]; then
    echo "[git-slim] skip prune ($why); time budget is ${TIMEOUT}s"
    return 0
  fi
  if ! command -v timeout >/dev/null 2>&1; then
    echo "[git-slim] skip prune ($why); timeout is not installed"
    return 0
  fi
  echo "[git-slim] prune ($why), limit ${left}s"
  t0=$(date +%s)
  if timeout "$left" bash -c "$PRUNE_INNER"; then
    stamp_write
    echo "[git-slim] prune done in $(( $(date +%s) - t0 ))s"
  else
    rc=$?
    # Tried and did not finish. Do not start another one until the stamp is old,
    # unless the disk guard (which ignores the stamp) runs on a later cycle.
    stamp_write
    echo "[git-slim] prune stopped after $(( $(date +%s) - t0 ))s (status $rc); cycle continues"
  fi
  return 0
}

# True when a prune was attempted in the last two minutes. The cycle calls sync
# twice (run_cycle.sh, then ci_cycle.sh). The second call must not spend another
# 60s. The next cycle is five minutes later, so a still-full disk is tried again.
prune_recent() {
  local now last
  now=$(date +%s)
  last=0
  if [ -f "$STAMP" ]; then
    last=$(tr -cd '0-9' < "$STAMP" || true)
    [ -n "$last" ] || last=0
  fi
  [ $((now - last)) -lt "${GIT_SLIM_RETRY_GAP:-120}" ]
}

cmd_sync() {
  local i ok git_mb already
  disable_auto_gc
  already=0
  if disk_low && prune_recent; then
    echo "[git-slim] disk still low; a prune already ran. Not starting another."
    already=1
  elif disk_low; then
    warn_disk "Pruning git objects before sync."
    prune_now "disk"
  fi
  ok=0
  for i in 1 2 3; do
    if git fetch -q --depth 1 origin main && git reset -q --hard origin/main; then
      ok=1
      break
    fi
    echo "[git-slim] shallow sync failed (try $i)" >&2
    if [ "$i" -lt 3 ]; then
      sleep "${GIT_SLIM_RETRY_SLEEP:-$((i * 5))}"
    fi
  done
  if [ "$ok" -ne 1 ]; then
    echo "[git-slim] ERROR: git fetch --depth 1 origin main failed" >&2
    return 1
  fi
  if [ "$(git rev-parse --is-shallow-repository 2>/dev/null || echo false)" != "true" ]; then
    echo "[git-slim] WARNING: checkout is not shallow after fetch --depth 1. Old history is still on disk." >&2
  fi
  # The depth-1 fetch is what makes the previous database blob unreachable, so
  # prune again in this same run. A second sync in this cycle sees the stamp.
  if [ "$already" -eq 0 ] && disk_low; then
    warn_disk "Pruning git objects after sync."
    prune_now "disk-after-sync"
    if disk_low; then
      git_mb=$(du -sm .git 2>/dev/null | awk '{print $1}')
      warn_disk "Still low after prune (.git is ${git_mb:-?}MB). git prune drops loose objects only. Do not run git gc during a cycle."
    fi
  fi
  return 0
}

cmd_prune() {
  disable_auto_gc
  if prune_due; then
    prune_now "scheduled"
  fi
  return 0
}

case "${1:-}" in
  sync) cmd_sync ;;
  prune) cmd_prune ;;
  *) echo "usage: git_slim.sh sync|prune" >&2; exit 2 ;;
esac
