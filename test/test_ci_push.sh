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
  printf 'shadow report\n' > "$base/data/exit_shadow_report.txt"
  printf '{}\n' > "$base/data/exit_shadow_report.json"
  mark_db "$base/data/trading.db"
  if (cd "$base" && env CI_PUSH_RETRY_SLEEP=0 $envx bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
  GC_AUTO=$(git -C "$base" config --get gc.auto || true)
  GC_PACK=$(git -C "$base" config --get gc.autopacklimit || true)
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
  if [ "$(git -C "$OTHER" show origin/main:data/exit_shadow_report.txt 2>/dev/null || true)" = "shadow report" ]; then
    ok "$name committed the shadow report"
  else
    bad "$name dropped data/exit_shadow_report.txt"
  fi
}

setup_and_push ""
check_kept "rebase"
if [ "$GC_AUTO" = "0" ] && [ "$GC_PACK" = "0" ]; then
  ok "cycle turns automatic repack off"
else
  bad "cycle left automatic repack on (auto=$GC_AUTO pack=$GC_PACK)"
fi
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

# A cycle whose parent is still the leased tip, with nothing else landing. $1 is extra env.
solo_push() {
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
  INIT=$(git -C "$base" rev-parse HEAD)
  printf 'shadow report\n' > "$base/data/exit_shadow_report.txt"
  mark_db "$base/data/trading.db"
  # No token: a missing SQUASH_OPEN_PRS must fail closed without calling GitHub.
  if (cd "$base" && env -u GH_TOKEN -u GITHUB_TOKEN CI_PUSH_RETRY_SLEEP=0 $envx bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
  git -C "$OTHER" fetch -q origin main
}

kept_history() {
  [ "$RC" -eq 0 ] && git -C "$OTHER" rev-parse --verify -q origin/main^ >/dev/null \
    && git -C "$OTHER" merge-base --is-ancestor "$INIT" origin/main \
    && [ "$(cycle_mark "$OTHER")" = "yes" ]
}

solo_push "SQUASH_AT=1 SQUASH_OPEN_PRS=1"
if kept_history && grep -q "skipping squash" "$TMP/push.log"; then
  ok "open pull requests fast-forward instead of orphaning main"
else
  bad "open pull requests did not keep history (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi

solo_push "SQUASH_AT=1"
if kept_history && grep -q "no GitHub token" "$TMP/push.log"; then
  ok "a failed open-PR check fast-forwards instead of orphaning main"
else
  bad "a failed open-PR check still squashed (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi

solo_push "SQUASH_AT=1 SQUASH_OPEN_PRS=0"
if [ "$RC" -eq 0 ] && ! git -C "$OTHER" rev-parse --verify -q origin/main^ >/dev/null \
   && [ "$(cycle_mark "$OTHER")" = "yes" ] && grep -q "squashed" "$TMP/push.log" \
   && [ "$(git -C "$OTHER" show origin/main:data/exit_shadow_report.txt 2>/dev/null || true)" = "shadow report" ]; then
  ok "no open pull requests still squashes to an orphan and keeps the shadow report"
else
  bad "quiet main did not squash (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi

solo_push "SQUASH_AT=1 SQUASH_OPEN_PRS=3 SQUASH_EVEN_WITH_PRS=1"
if [ "$RC" -eq 0 ] && ! git -C "$OTHER" rev-parse --verify -q origin/main^ >/dev/null \
   && grep -q "squashed" "$TMP/push.log"; then
  ok "SQUASH_EVEN_WITH_PRS still squashes while pull requests are open"
else
  bad "SQUASH_EVEN_WITH_PRS did not squash (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi

# Shallow clone (the VPS). file:// so --depth is honoured. A plain fetch must
# still rebase onto a commit that landed on main; --depth 1 at integrate time
# replays the old shallow root and conflicts. Rev-list on a depth-1 clone is
# not the squash count: that is .git/slim-pushes.
make_shallow_pair() {
  local n=$1 i
  TMP=$(mktemp -d)
  local origin=$TMP/origin.git seed=$TMP/seed
  git init -q --bare -b main "$origin"
  local url="file://${origin}"
  git clone -q "$url" "$seed"
  git -C "$seed" config user.email test@example.com
  git -C "$seed" config user.name test
  mkdir -p "$seed/data" "$seed/docs"
  new_db "$seed/data/trading.db"
  printf 'v1\n' > "$seed/code.txt"
  printf 'summary\n' > "$seed/data/daily_summary.txt"
  printf '<html></html>\n' > "$seed/docs/index.html"
  git -C "$seed" add -A
  git -C "$seed" commit -q -m init
  git -C "$seed" push -q origin main
  for i in $(seq 2 "$n"); do
    printf 'v%s\n' "$i" > "$seed/code.txt"
    git -C "$seed" commit -q -am "c$i"
    git -C "$seed" push -q origin main
  done
  git clone -q --depth 1 "$url" "$TMP/base"
  git -C "$TMP/base" config user.email test@example.com
  git -C "$TMP/base" config user.name test
  git clone -q "$url" "$TMP/other"
  git -C "$TMP/other" config user.email test@example.com
  git -C "$TMP/other" config user.name test
}

make_shallow_pair 6
printf 'v-code\n' > "$TMP/other/code.txt"
git -C "$TMP/other" commit -q -am 'land code'
git -C "$TMP/other" push -q origin HEAD:main
CODE=$(git -C "$TMP/other" rev-parse HEAD)
mark_db "$TMP/base/data/trading.db"
if (cd "$TMP/base" && CI_PUSH_RETRY_SLEEP=0 bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
git -C "$TMP/other" fetch -q origin main
if [ "$RC" -eq 0 ] && git -C "$TMP/other" merge-base --is-ancestor "$CODE" origin/main \
   && [ "$(git -C "$TMP/other" show origin/main:code.txt)" = "v-code" ] \
   && [ "$(cycle_mark "$TMP/other")" = "yes" ]; then
  ok "shallow rebase pushed the cycle book on top of the code commit"
else
  bad "shallow rebase did not push the cycle book (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi
if grep -q conflicted "$TMP/push.log"; then
  bad "shallow rebase conflicted on a data-only cycle"
else
  ok "shallow rebase did not conflict"
fi
shallow_n=$(git -C "$TMP/base" rev-list --count HEAD)
if [ "$(git -C "$TMP/base" rev-parse --is-shallow-repository)" = "true" ] && [ "$shallow_n" -le 4 ]; then
  ok "shallow rebase stayed shallow (count $shallow_n)"
else
  bad "shallow rebase deepened the clone (count $shallow_n)"
fi
if [ "$(tr -cd '0-9' < "$TMP/base/.git/slim-pushes")" = "1" ]; then
  ok "shallow fast-forward recorded push count 1"
else
  bad "shallow push count was not 1"
fi

# Rev-list after one commit on depth 1 is 2, which is already over SQUASH_AT=1.
# The counter is 1, so this must fast-forward and keep the parent.
make_shallow_pair 8
mark_db "$TMP/base/data/trading.db"
if (cd "$TMP/base" && CI_PUSH_RETRY_SLEEP=0 SQUASH_AT=1 SQUASH_OPEN_PRS=0 bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
git -C "$TMP/other" fetch -q origin main
if [ "$RC" -eq 0 ] && git -C "$TMP/other" rev-parse --verify -q origin/main^ >/dev/null \
   && [ "$(cycle_mark "$TMP/other")" = "yes" ] && ! grep -q "squashed" "$TMP/push.log"; then
  ok "shallow clone does not squash from rev-list depth"
else
  bad "shallow clone squashed from rev-list depth (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi

# Counter 5 + this push = 6, and 6 > 3. Rev-list is only 2, so only the counter can squash.
make_shallow_pair 4
echo 5 > "$TMP/base/.git/slim-pushes"
mark_db "$TMP/base/data/trading.db"
printf 'shadow report\n' > "$TMP/base/data/exit_shadow_report.txt"
if (cd "$TMP/base" && CI_PUSH_RETRY_SLEEP=0 SQUASH_AT=3 SQUASH_OPEN_PRS=0 bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
git -C "$TMP/other" fetch -q origin main
if [ "$RC" -eq 0 ] && ! git -C "$TMP/other" rev-parse --verify -q origin/main^ >/dev/null \
   && [ "$(cycle_mark "$TMP/other")" = "yes" ] && grep -q "squashed" "$TMP/push.log" \
   && [ "$(tr -cd '0-9' < "$TMP/base/.git/slim-pushes")" = "0" ]; then
  ok "shallow push counter still squashes and then resets"
else
  bad "shallow push counter did not squash (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi

make_shallow_pair 4
echo 5 > "$TMP/base/.git/slim-pushes"
mark_db "$TMP/base/data/trading.db"
if (cd "$TMP/base" && CI_PUSH_RETRY_SLEEP=0 SQUASH_AT=3 SQUASH_OPEN_PRS=2 bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
git -C "$TMP/other" fetch -q origin main
if [ "$RC" -eq 0 ] && git -C "$TMP/other" rev-parse --verify -q origin/main^ >/dev/null \
   && grep -q "skipping squash" "$TMP/push.log"; then
  ok "shallow clone still skips squash while pull requests are open"
else
  bad "shallow clone squashed while pull requests are open (rc=$RC)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi

# Main moves after the rebase and before the confirming fetch.
make_shallow_pair 4
printf 'v-code\n' > "$TMP/other/code.txt"
git -C "$TMP/other" commit -q -am 'land code'
git -C "$TMP/other" push -q origin HEAD:main
CODE=$(git -C "$TMP/other" rev-parse HEAD)
mark_db "$TMP/base/data/trading.db"
confirm="git -C '$TMP/other' commit --allow-empty -q -m 'landed during confirm' && git -C '$TMP/other' push -q origin HEAD:main"
if (cd "$TMP/base" && CI_PUSH_RETRY_SLEEP=0 SQUASH_AT=100 CI_PUSH_BEFORE_CONFIRM="$confirm" bash "$PUSH") >"$TMP/push.log" 2>&1; then RC=0; else RC=$?; fi
git -C "$TMP/other" fetch -q origin main
tip_msg=$(git -C "$TMP/other" log -1 --format=%s origin/main)
if [ "$RC" -eq 0 ] && [ "$tip_msg" = "landed during confirm" ] \
   && git -C "$TMP/other" merge-base --is-ancestor "$CODE" origin/main \
   && [ -z "$(cycle_mark "$TMP/other")" ]; then
  ok "shallow abort leaves the commit that landed during the confirming fetch"
else
  bad "shallow abort pushed over a moved main (rc=$RC tip=$tip_msg)"
  sed -n '1,40p' "$TMP/push.log" >&2
fi
if [ -f "$TMP/base/.git/slim-pushes" ]; then
  bad "shallow abort recorded a push that did not happen"
else
  ok "shallow abort did not advance the push counter"
fi

echo
if [ "$fail" -eq 0 ]; then echo "0 failure(s)"; else echo "$fail failure(s)"; fi
exit "$fail"
