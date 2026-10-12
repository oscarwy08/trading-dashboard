#!/bin/bash
# A cycle push must not drop a code commit that landed on main while the cycle was running.
# Local bare repos only (no network).
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PUSH=$ROOT/scripts/ci_push.sh
fail=0
ok() { printf 'OK   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fail=1; }

new_db() {
  python3 - "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT)")
c.execute("INSERT OR REPLACE INTO state VALUES('account_reset','same')")
c.commit()
PY
}
mark_db() {
  python3 - "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT OR REPLACE INTO state VALUES('cycle_mark','yes')")
c.commit()
PY
}
cycle_mark() {
  local repo=$1 shown
  shown=$(mktemp)
  git -C "$repo" show origin/main:data/trading.db > "$shown"
  python3 - "$shown" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute("SELECT value FROM state WHERE key='cycle_mark'").fetchone()
print(r[0] if r else "")
PY
  rm -f "$shown"
}

# Leaves OTHER, CODE, RC, TMP set. $1 is extra env (empty or 'SQUASH_AT=1').
setup_and_push() {
  local envx=$1
  TMP=$(mktemp -d)
  local origin=$TMP/origin.git base=$TMP/base
  OTHER=$TMP/other
  git init -q --bare -b main "$origin"
  git clone -q "$origin" "$base"
  git -C "$base" config user.email test@example.com
  git -C "$base" config user.name test
  mkdir -p "$base/data" "$base/docs"
  new_db "$base/data/trading.db"
  printf 'v1\n' > "$base/code.txt"
  printf 'summary\n' > "$base/data/daily_summary.txt"
  printf '<html></html>\n' > "$base/docs/index.html"
  git -C "$base" add -A
  git -C "$base" commit -q -m init
  git -C "$base" push -q -u origin main
  git clone -q "$origin" "$OTHER"
  git -C "$OTHER" config user.email test@example.com
  git -C "$OTHER" config user.name test
  printf 'v2\n' > "$OTHER/code.txt"
  git -C "$OTHER" add code.txt
  git -C "$OTHER" commit -q -m 'land code'
  git -C "$OTHER" push -q origin HEAD:main
  CODE=$(git -C "$OTHER" rev-parse HEAD)
  printf 'SECRET\n' > "$base/data/exit_shadow.db"
  mark_db "$base/data/trading.db"
  if (cd "$base" && env CI_PUSH_RETRY_SLEEP=0 $envx bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
  git -C "$OTHER" fetch -q origin main
}

check_kept() {
  local name=$1
  if git -C "$OTHER" merge-base --is-ancestor "$CODE" origin/main; then
    ok "$name keeps the code commit on main"
  else
    bad "$name dropped the code commit"
    sed -n '1,30p' "$TMP/push.log" >&2
  fi
  if [ "$(git -C "$OTHER" show origin/main:code.txt)" = "v2" ]; then
    ok "$name keeps code.txt v2"
  else
    bad "$name code.txt is not v2"
  fi
  if git -C "$OTHER" ls-tree -r --name-only origin/main | grep -q '^data/exit_shadow.db$'; then
    bad "$name committed data/exit_shadow.db"
  else
    ok "$name did not commit data/exit_shadow.db"
  fi
}

setup_and_push ""
check_kept "rebase"
if [ "$RC" -eq 0 ] && [ "$(cycle_mark "$OTHER")" = "yes" ]; then
  ok "rebase pushed the cycle book on top of the code commit"
else
  bad "rebase did not push the cycle book (rc=$RC)"
  sed -n '1,30p' "$TMP/push.log" >&2
fi

setup_and_push "SQUASH_AT=1"
check_kept "squash-race"
if [ "$RC" -eq 0 ] && git -C "$OTHER" rev-parse --verify -q origin/main^ >/dev/null; then
  ok "squash-race fast-forwarded instead of replacing main with an orphan"
else
  bad "squash-race force-pushed an orphan (rc=$RC)"
  sed -n '1,30p' "$TMP/push.log" >&2
fi

# Main moves after the rebase, before the confirming fetch.
TMP=$(mktemp -d)
origin=$TMP/origin.git
base=$TMP/base
OTHER=$TMP/other
git init -q --bare -b main "$origin"
git clone -q "$origin" "$base"
git -C "$base" config user.email test@example.com
git -C "$base" config user.name test
mkdir -p "$base/data" "$base/docs"
new_db "$base/data/trading.db"
printf 'v1\n' > "$base/code.txt"
printf 'summary\n' > "$base/data/daily_summary.txt"
printf '<html></html>\n' > "$base/docs/index.html"
git -C "$base" add -A
git -C "$base" commit -q -m init
git -C "$base" push -q -u origin main
git clone -q "$origin" "$OTHER"
git -C "$OTHER" config user.email test@example.com
git -C "$OTHER" config user.name test
printf 'v2\n' > "$OTHER/code.txt"
git -C "$OTHER" commit -q -am 'land code'
git -C "$OTHER" push -q origin HEAD:main
CODE=$(git -C "$OTHER" rev-parse HEAD)
mark_db "$base/data/trading.db"
confirm="git -C '$OTHER' commit --allow-empty -q -m 'landed during confirm' && git -C '$OTHER' push -q origin HEAD:main"
if (cd "$base" && CI_PUSH_RETRY_SLEEP=0 SQUASH_AT=1 CI_PUSH_BEFORE_CONFIRM="$confirm" bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
git -C "$OTHER" fetch -q origin main
tip_msg=$(git -C "$OTHER" log -1 --format=%s origin/main)
if [ "$RC" -eq 0 ] && [ "$tip_msg" = "landed during confirm" ] && git -C "$OTHER" merge-base --is-ancestor "$CODE" origin/main; then
  ok "abort leaves the commit that landed during the confirming fetch"
else
  bad "abort pushed over a moved main (rc=$RC tip=$tip_msg)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi
if [ -z "$(cycle_mark "$OTHER")" ]; then
  ok "abort did not publish this cycle's book"
else
  bad "abort still published the cycle book"
fi

echo
if [ "$fail" -eq 0 ]; then echo "0 failure(s)"; else echo "$fail failure(s)"; fi
exit "$fail"
