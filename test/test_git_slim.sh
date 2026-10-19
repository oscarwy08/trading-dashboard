#!/bin/bash
# Shallow sync, bounded prune, disk guard, and the VPS run_cycle.sh wrapper.
# Local bare repos only (file:// so --depth is honoured).
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SLIM=$ROOT/scripts/git_slim.sh
RUN=$ROOT/deploy/run_cycle.sh
fail=0
ok() { printf 'OK   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fail=1; }

if [ "$(git -C "$ROOT" rev-parse --is-shallow-repository)" != "false" ]; then
  echo "refusing to run; workspace is already shallow" >&2
  exit 2
fi
cleanup() {
  local shallow
  shallow=$(git -C "$ROOT" rev-parse --is-shallow-repository 2>/dev/null || echo unknown)
  if [ "$shallow" != "false" ]; then
    echo "FAIL workspace became shallow" >&2
    exit 1
  fi
}
trap cleanup EXIT

loose_kb() { git -C "$1" count-objects -v | awk '/^size:/ {print $2}'; }

# $1 = number of commits, each with a fresh 100KB blob. Sets WORK, SEED, URL.
make_origin() {
  local n=$1 i
  WORK=$(mktemp -d)
  local origin=$WORK/origin.git
  SEED=$WORK/seed
  git init -q --bare -b main "$origin"
  URL="file://${origin}"
  git clone -q "$URL" "$SEED"
  git -C "$SEED" config user.email t@example.com
  git -C "$SEED" config user.name t
  mkdir -p "$SEED/data"
  i=1
  dd if=/dev/urandom of="$SEED/data/blob" bs=1024 count=100 status=none
  printf 'v%s\n' "$i" > "$SEED/code.txt"
  git -C "$SEED" add -A
  git -C "$SEED" commit -q -m "c$i"
  git -C "$SEED" push -q origin main
  while [ "$i" -lt "$n" ]; do
    i=$((i + 1))
    dd if=/dev/urandom of="$SEED/data/blob" bs=1024 count=100 status=none
    printf 'v%s\n' "$i" > "$SEED/code.txt"
    git -C "$SEED" commit -q -am "c$i"
    git -C "$SEED" push -q origin main
  done
}

make_origin 4
git clone -q "$URL" "$WORK/full"
if GIT_SLIM_ROOT="$WORK/full" GIT_SLIM_EVERY_HOURS=10000 bash "$SLIM" sync >"$WORK/sync.log" 2>&1; then
  :
else
  bad "sync on a full clone failed"
  sed -n '1,30p' "$WORK/sync.log" >&2
fi
if [ "$(git -C "$WORK/full" rev-parse --is-shallow-repository)" = "true" ] \
   && [ "$(git -C "$WORK/full" rev-list --count HEAD)" = "1" ] \
   && [ "$(cat "$WORK/full/code.txt")" = "v4" ]; then
  ok "fetch --depth 1 shortens a full clone to the tip"
else
  bad "full clone was not shortened (shallow=$(git -C "$WORK/full" rev-parse --is-shallow-repository) count=$(git -C "$WORK/full" rev-list --count HEAD))"
fi

# Three more commits on a shallow clone, with sync between them and no prune.
# The cut-off blobs stay loose until a prune.
make_origin 1
git clone -q --depth 1 "$URL" "$WORK/vps"
git -C "$WORK/vps" config user.email t@example.com
git -C "$WORK/vps" config user.name t
git -C "$WORK/vps" config gc.auto 0
git -C "$WORK/vps" config gc.autopacklimit 0
i=0
while [ "$i" -lt 3 ]; do
  i=$((i + 1))
  dd if=/dev/urandom of="$WORK/vps/data/blob" bs=1024 count=100 status=none
  git -C "$WORK/vps" add -A
  git -C "$WORK/vps" commit -q -m "cycle $i"
  git -C "$WORK/vps" push -q origin HEAD:main
  if ! GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_EVERY_HOURS=10000 bash "$SLIM" sync >"$WORK/grow.log" 2>&1; then
    bad "sync during growth cycle $i failed"
    sed -n '1,20p' "$WORK/grow.log" >&2
    break
  fi
done
before=$(loose_kb "$WORK/vps")
count_before=$(git -C "$WORK/vps" rev-list --count HEAD)
if GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_FREE_KB=1 GIT_SLIM_EVERY_HOURS=10000 bash "$SLIM" sync >"$WORK/disk.log" 2>&1; then
  :
else
  bad "disk-guard sync failed"
  sed -n '1,30p' "$WORK/disk.log" >&2
fi
after=$(loose_kb "$WORK/vps")
if [ "$count_before" = "1" ] && [ "$before" -ge 150 ] && [ "$after" -lt "$before" ]; then
  ok "disk guard prune dropped loose objects ($before KiB -> $after KiB)"
else
  bad "disk guard did not shrink loose objects (count=$count_before before=$before after=$after)"
  sed -n '1,30p' "$WORK/disk.log" >&2
fi
if grep -q "DISK WARNING" "$WORK/disk.log"; then
  ok "disk guard logs when free space is under 3GB"
else
  bad "disk guard did not log"
fi
if [ "$(git -C "$WORK/vps" rev-list --count HEAD)" = "1" ] \
   && [ "$(git -C "$WORK/vps" rev-parse --is-shallow-repository)" = "true" ]; then
  ok "disk-guard sync left a depth-1 checkout"
else
  bad "disk-guard sync did not leave depth 1"
fi
t0=$(date +%s)
if GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_FREE_KB=1 GIT_SLIM_EVERY_HOURS=10000 bash "$SLIM" sync >"$WORK/disk2.log" 2>&1; then
  elapsed=$(( $(date +%s) - t0 ))
  if [ "$elapsed" -le 3 ] && grep -q "already ran" "$WORK/disk2.log"; then
    ok "a second sync in the same cycle does not prune again"
  else
    bad "second sync pruned again (elapsed ${elapsed}s)"
    cat "$WORK/disk2.log" >&2
  fi
else
  bad "second disk-guard sync failed"
  cat "$WORK/disk2.log" >&2
fi

# A fresh stamp suppresses the scheduled prune. Forcing the interval runs it.
# Make another unreachable loose blob, then show the stamp blocks prune.
dd if=/dev/urandom of="$WORK/vps/data/blob" bs=1024 count=100 status=none
git -C "$WORK/vps" add -A
git -C "$WORK/vps" commit -q -m held
git -C "$WORK/vps" push -q origin HEAD:main
GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_EVERY_HOURS=10000 bash "$SLIM" sync >/dev/null
held=$(loose_kb "$WORK/vps")
date +%s > "$WORK/vps/.git/slim-prune-stamp"
GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_EVERY_HOURS=6 bash "$SLIM" prune >"$WORK/skip.log" 2>&1
skipped=$(loose_kb "$WORK/vps")
if [ "$skipped" = "$held" ] && ! grep -q "prune done" "$WORK/skip.log"; then
  ok "scheduled prune waits for the stamp"
else
  bad "scheduled prune ignored the stamp ($held -> $skipped)"
  cat "$WORK/skip.log" >&2
fi
GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_EVERY_HOURS=0 bash "$SLIM" prune >"$WORK/due.log" 2>&1
due=$(loose_kb "$WORK/vps")
if [ "$due" -lt "$held" ] && grep -q "prune done" "$WORK/due.log"; then
  ok "scheduled prune runs when the stamp is due ($held KiB -> $due KiB)"
else
  bad "due prune did not shrink loose objects ($held -> $due)"
  cat "$WORK/due.log" >&2
fi

# Orphan squash on the remote is fetched as a single commit.
NEW=$(git -C "$SEED" commit-tree "HEAD^{tree}" -m "orphan squash")
git -C "$SEED" push -q --force origin "$NEW":main
if GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_EVERY_HOURS=10000 bash "$SLIM" sync >"$WORK/orphan.log" 2>&1 \
   && [ "$(git -C "$WORK/vps" rev-list --count HEAD)" = "1" ] \
   && [ "$(git -C "$WORK/vps" log -1 --format=%s)" = "orphan squash" ] \
   && [ "$(git -C "$WORK/vps" rev-parse --is-shallow-repository)" = "true" ]; then
  ok "shallow sync follows an orphan squash"
else
  bad "shallow sync did not follow the orphan"
  sed -n '1,20p' "$WORK/orphan.log" >&2
fi

# Prune must not hold the cycle. A 5s command under a 1s budget is killed.
# Disk stays "low", so sync tries again; the shared budget has to stop that too.
rm -f "$WORK/vps/.git/slim-prune-stamp"
t0=$(date +%s)
if GIT_SLIM_ROOT="$WORK/vps" GIT_SLIM_FREE_KB=1 GIT_SLIM_PRUNE_TIMEOUT=1 \
     GIT_SLIM_PRUNE_CMD='sleep 5' GIT_SLIM_EVERY_HOURS=10000 \
     bash "$SLIM" sync >"$WORK/timeout.log" 2>&1; then
  elapsed=$(( $(date +%s) - t0 ))
  if [ "$elapsed" -le 6 ] && grep -Eq "prune stopped|time budget" "$WORK/timeout.log"; then
    ok "prune stops inside the time budget (${elapsed}s)"
  else
    bad "prune budget was not enforced (elapsed ${elapsed}s)"
    cat "$WORK/timeout.log" >&2
  fi
else
  bad "sync failed when prune was killed"
  cat "$WORK/timeout.log" >&2
fi

# Bad remote: fail fast, no long sleep.
git clone -q --depth 1 "$URL" "$WORK/bad"
git -C "$WORK/bad" remote set-url origin "file:///tmp/git-slim-missing-origin"
t0=$(date +%s)
if GIT_SLIM_ROOT="$WORK/bad" GIT_SLIM_RETRY_SLEEP=0 bash "$SLIM" sync >"$WORK/bad.log" 2>&1; then
  bad "sync succeeded against a missing origin"
else
  elapsed=$(( $(date +%s) - t0 ))
  if [ "$elapsed" -le 5 ]; then
    ok "sync fails fast when fetch fails (${elapsed}s)"
  else
    bad "failed sync slept too long (${elapsed}s)"
  fi
fi

# deploy/run_cycle.sh: depth-1 sync, full collector, then fast, then full again.
make_origin 4
git clone -q "$URL" "$WORK/cycle"
mkdir -p "$WORK/cycle/scripts"
cp "$SLIM" "$WORK/cycle/scripts/git_slim.sh"
MODE=$WORK/mode
export MODE_OUT=$MODE
cat > "$WORK/cycle/scripts/ci_cycle.sh" <<'EOF'
#!/bin/bash
if [ "${COLLECT_MODE:-}" = "fast" ]; then echo fast > "$MODE_OUT"; else echo full > "$MODE_OUT"; fi
exit 0
EOF
cat > "$WORK/cycle/scripts/ci_push.sh" <<'EOF'
#!/bin/bash
echo push >> "$MODE_OUT"
exit 0
EOF
chmod +x "$WORK/cycle/scripts/ci_cycle.sh" "$WORK/cycle/scripts/ci_push.sh"
run_cycle() {
  TRADING_CLONE="$WORK/cycle" TRADING_CYCLE_LOCK="$WORK/cycle.lock" \
    COLLECT_FULL_STAMP="$WORK/collect-stamp" \
    bash "$RUN"
}
if run_cycle >"$WORK/run1.log" 2>&1; then
  :
else
  bad "run_cycle failed"
  cat "$WORK/run1.log" >&2
fi
if [ "$(cat "$MODE")" = $'full\npush' ] \
   && [ "$(git -C "$WORK/cycle" rev-parse --is-shallow-repository)" = "true" ] \
   && [ "$(git -C "$WORK/cycle" rev-list --count HEAD)" = "1" ] \
   && [ "$(cat "$WORK/cycle/code.txt")" = "v4" ] \
   && grep -q "full collector" "$WORK/run1.log"; then
  ok "run_cycle shallow-syncs and starts on a full collector"
else
  bad "run_cycle first pass (mode=$(cat "$MODE" 2>/dev/null || echo missing))"
  cat "$WORK/run1.log" >&2
fi
if run_cycle >"$WORK/run2.log" 2>&1 && [ "$(head -n 1 "$MODE")" = "fast" ] \
   && grep -q "fast collector" "$WORK/run2.log"; then
  ok "run_cycle stays on the fast collector inside 15 minutes"
else
  bad "run_cycle did not stay fast"
  cat "$WORK/run2.log" >&2
fi
echo 0 > "$WORK/collect-stamp"
if run_cycle >"$WORK/run3.log" 2>&1 && [ "$(head -n 1 "$MODE")" = "full" ]; then
  ok "run_cycle runs a full collector once the stamp is old"
else
  bad "run_cycle did not return to a full collector"
  cat "$WORK/run3.log" >&2
fi

echo
if [ "$fail" -eq 0 ]; then echo "0 failure(s)"; else echo "$fail failure(s)"; fi
exit "$fail"
