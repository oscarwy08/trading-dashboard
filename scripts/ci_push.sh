#!/bin/bash
# Commit the cycle's state (data/: trading.db, processed sentiment, daily_summary.txt) + site data (docs/) and push to main.
# A cycle must not drop a non-cycle commit that landed on main while this run was in progress (a merged PR, a
# sentiment push). The squash used to `commit-tree` an orphan and `--force-with-lease=main:$(git rev-parse origin/main)`.
# That lease SHA is the remote-tracking ref: a fetch can move it to a tip this tree was never rebased onto, and the
# orphan then replaces that tip. Instead: fetch, rebase this cycle onto that exact SHA, fetch once more, and only
# then push, leasing the SHA we integrated. If the second fetch moved, do not push; the next cycle retries.
# Squash only when this cycle's parent is still that tip, and no pull request is open against main.
# An orphan force-push has no merge-base with an open pull request, and GitHub closes it. If the
# pull-request check cannot be made, fast-forward instead. SQUASH_EVEN_WITH_PRS=1 squashes anyway.
# Otherwise fast-forward the rebased cycle so the new commits stay on main.
set -u
SQUASH_AT=${SQUASH_AT:-100}
git config user.name "${BOT_NAME:-trading-bot}"
git config user.email "${BOT_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"

# data/exit_shadow.db is the watcher's log. It used to be untracked inside the clone, so `git add -A` committed
# it. Never stage it (or its wal/shm/journal), even if an older commit still has it in the index.
unstage_shadow() {
  git reset -q HEAD -- data/exit_shadow.db data/exit_shadow.db-wal data/exit_shadow.db-shm data/exit_shadow.db-journal 2>/dev/null || true
}
git add -A
unstage_shadow
if git diff --cached --quiet; then echo "no changes to commit"; exit 0; fi
MSG="cycle $(date -u +%FT%TZ)"
git commit -q -m "$MSG"
BASE_BEFORE=$(git rev-parse HEAD~1)

# reset marker of a DB (state 'account_reset'): a cycle computed on a DB with a different marker than main's must never be
# re-applied - that would resurrect the pre-reset run after a reset (great_reset.py) was pushed meanwhile.
reset_mark() { python3 -c 'import sqlite3,sys
try: r=sqlite3.connect(sys.argv[1]).execute("SELECT value FROM state WHERE key=?",("account_reset",)).fetchone(); print(r[0] if r else "")
except Exception as e: print("ERR")' "$1"; }

reapply() {
  # Conflict while rebasing onto $1. Keep that tip's code (including docs/app.js) and put back only this
  # cycle's book and the generated data json. Do not copy the whole docs tree: that would overwrite a
  # site change that landed on main while the cycle was running.
  local tip=$1
  local tmp; tmp=$(mktemp -d)
  cp -a data/trading.db data/daily_summary.txt "$tmp/" 2>/dev/null || true
  cp -a data/exit_shadow_report.txt data/exit_shadow_report.json "$tmp/" 2>/dev/null || true
  mkdir -p "$tmp/docs"
  cp -a docs/data.json "$tmp/docs/" 2>/dev/null || true
  cp -a docs/data_*.json "$tmp/docs/" 2>/dev/null || true
  git rebase --abort 2>/dev/null || true
  git reset -q --hard "$tip"
  local ours theirs; ours=$(reset_mark "$tmp/trading.db"); theirs=$(reset_mark data/trading.db)
  if [ "$ours" != "$theirs" ]; then
    echo "::warning::main's state was reset meanwhile (account_reset '$theirs' != '$ours') - dropping this cycle's state, not pushing"
    exit 0
  fi
  cp -a "$tmp/trading.db" data/trading.db
  cp -a "$tmp/daily_summary.txt" data/ 2>/dev/null || true
  cp -a "$tmp/exit_shadow_report.txt" "$tmp/exit_shadow_report.json" data/ 2>/dev/null || true
  cp -a "$tmp/docs/." docs/ 2>/dev/null || true
  git add -A -- data/trading.db data/daily_summary.txt docs
  if [ -f data/exit_shadow_report.txt ]; then git add -A -- data/exit_shadow_report.txt data/exit_shadow_report.json; fi
  unstage_shadow
  git commit -q -m "$MSG (re-applied on top of concurrent push)"
}

# Fetch main and make HEAD a descendant of that exact commit. A second fetch must still see the same SHA.
# Returns 0 and sets TIP, or 2 if main moved (caller must not push), or 1 on a hard failure.
integrate() {
  git fetch -q origin main || { echo "::error::fetch origin/main failed" >&2; return 1; }
  local tip now
  tip=$(git rev-parse origin/main)
  if ! git merge-base --is-ancestor "$tip" HEAD; then
    if ! git rebase -q "$tip"; then
      echo "rebase onto ${tip:0:7} conflicted; re-applying cycle state onto that tip" >&2
      reapply "$tip" || return 1
    fi
  fi
  if [ -n "${CI_PUSH_BEFORE_CONFIRM:-}" ]; then
    # Test seam: move main after the rebase and before the confirming fetch.
    eval "$CI_PUSH_BEFORE_CONFIRM"
  fi
  git fetch -q origin main || { echo "::error::fetch origin/main failed" >&2; return 1; }
  now=$(git rev-parse origin/main)
  if [ "$now" != "$tip" ]; then
    echo "main moved (${tip:0:7} -> ${now:0:7}) after the cycle was rebased; not pushing, next cycle will retry" >&2
    return 2
  fi
  if ! git merge-base --is-ancestor "$tip" HEAD; then
    echo "::error::cycle is not based on fetched main ${tip:0:7}; not pushing" >&2
    return 1
  fi
  local bad
  bad=$(git diff --name-only "$tip" HEAD | grep -v -E '^(data/|docs/)' || true)
  if [ -n "$bad" ]; then
    echo "::error::refusing to push; this cycle would change non-state paths:" >&2
    echo "$bad" >&2
    return 1
  fi
  TIP=$tip
  return 0
}

push_ff() {
  local i rc
  for i in 1 2 3 4 5 6; do
    if git push -q origin HEAD:main; then echo "pushed $(git rev-parse --short HEAD) (try $i)"; return 0; fi
    echo "push rejected; rebasing onto latest main (try $i)" >&2
    sleep "${CI_PUSH_RETRY_SLEEP:-$((i * 3))}"
    integrate
    rc=$?
    if [ "$rc" -eq 2 ]; then
      echo "main moved during retry; not pushing, next cycle will retry" >&2
      return 0
    fi
    [ "$rc" -eq 0 ] || return 1
  done
  echo "::error::push failed after retries" >&2
  return 1
}

_t=$(date +%s)
integrate
rc=$?
echo "[timing] ci_push integrate $(( $(date +%s) - _t ))s"
if [ "$rc" -eq 2 ]; then exit 0; fi
[ "$rc" -eq 0 ] || exit 1

# How many pull requests are open against main. Prints a count and returns 0, or returns 1
# when the count is unknown. SQUASH_OPEN_PRS is the offline seam (tests, no network).
# SQUASH_EVEN_WITH_PRS=1 reports zero so the squash still runs.
open_prs_against_main() {
  if [ "${SQUASH_EVEN_WITH_PRS:-}" = "1" ]; then
    echo 0
    return 0
  fi
  if [ -n "${SQUASH_OPEN_PRS+x}" ]; then
    echo "${SQUASH_OPEN_PRS}"
    return 0
  fi
  local token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  local slug="${GITHUB_REPOSITORY:-${REPO_SLUG:-oscarwy08/trading-dashboard}}"
  if [ -z "$token" ]; then
    echo "skipping squash; no GitHub token to check open pull requests against main" >&2
    return 1
  fi
  local body
  if ! body=$(curl -fsS --max-time 20 \
      -H "Authorization: Bearer $token" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/repos/${slug}/pulls?state=open&base=main&per_page=100"); then
    echo "skipping squash; the GitHub pull-request list failed" >&2
    return 1
  fi
  local count
  if ! count=$(printf '%s' "$body" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(2)
if not isinstance(data, list):
    sys.exit(2)
print(len(data))'); then
    echo "skipping squash; the GitHub pull-request list was not a list" >&2
    return 1
  fi
  echo "$count"
}

n=$(git rev-list --count HEAD)
# Squash only when nobody else committed: our parent is still the tip we just leased.
# A code commit that landed during the cycle stays in history (fast-forward below).
# An open pull request also stays. The orphan force-push would close it.
if [ "$n" -gt "$SQUASH_AT" ] && [ "$TIP" = "$BASE_BEFORE" ]; then
  open_n=$(open_prs_against_main)
  open_rc=$?
  if [ "$open_rc" -ne 0 ]; then
    :
  elif [ "${open_n:-}" != "0" ]; then
    echo "skipping squash; ${open_n} open pull request(s) against main"
  else
    NEW=$(git commit-tree "HEAD^{tree}" -m "Squashed history ($n commits) at $(date -u +%FT%TZ) - state + site snapshot")
    if git push -q --force-with-lease=main:"$TIP" origin "$NEW":main; then
      echo "squashed $n commits into $NEW and pushed"
      exit 0
    fi
    echo "squash push lost the race (main was no longer ${TIP:0:7}); not force-pushing, next cycle will retry" >&2
    exit 0
  fi
fi
_t=$(date +%s)
push_ff
rc=$?
echo "[timing] ci_push push $(( $(date +%s) - _t ))s"
exit $rc
