#!/bin/bash
# Commit the cycle's state (data/: trading.db, processed sentiment, daily_summary.txt) + site (docs/) and push to main
# with the workflow's GITHUB_TOKEN. Robust to concurrent pushes from the box (push_sentiment.sh / repo_flag.sh):
#   push -> if rejected: fetch + rebase (box only adds data/sentiment/*.json or KILL/RISK_RESUME, so no conflicts);
#   if a rebase still conflicts: reset to origin/main and re-apply OUR trading.db/docs/summary (Actions is the only
#   writer of those); unprocessed sentiment files simply get ingested next cycle (re-ingest is idempotent).
# History control: when main has more than SQUASH_AT commits, the tree is re-committed as a single orphan commit and
# force-pushed with a lease (old blobs of the binary DB become unreachable), so the repo stays small.
set -u
SQUASH_AT=${SQUASH_AT:-100}
git config user.name "${BOT_NAME:-trading-bot}"
git config user.email "${BOT_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
git add -A
if git diff --cached --quiet; then echo "no changes to commit"; exit 0; fi
MSG="cycle $(date -u +%FT%TZ)"
git commit -q -m "$MSG"

reapply() {
  local tmp; tmp=$(mktemp -d)
  cp -a data/trading.db data/daily_summary.txt "$tmp/" 2>/dev/null; cp -a docs "$tmp/docs"
  git rebase --abort 2>/dev/null; git reset -q --hard origin/main
  cp -a "$tmp/trading.db" data/trading.db; cp -a "$tmp/daily_summary.txt" data/ 2>/dev/null
  rm -rf docs; cp -a "$tmp/docs" docs
  git add -A data/trading.db data/daily_summary.txt docs && git commit -q -m "$MSG (re-applied on top of concurrent push)"
}

n=$(git rev-list --count HEAD)
if [ "$n" -gt "$SQUASH_AT" ]; then
  git fetch -q origin main
  if [ "$(git rev-parse origin/main)" != "$(git rev-parse HEAD~1)" ]; then git rebase -q origin/main || reapply; fi
  NEW=$(git commit-tree "HEAD^{tree}" -m "Squashed history ($n commits) at $(date -u +%FT%TZ) - state + site snapshot")
  if git push -q --force-with-lease=main:"$(git rev-parse origin/main)" origin "$NEW":main; then
    echo "squashed $n commits into $NEW and pushed"; exit 0
  fi
  echo "squash push lost the race - normal push instead"
fi
for i in 1 2 3 4 5 6; do
  if git push -q origin HEAD:main; then echo "pushed $(git rev-parse --short HEAD) (try $i)"; exit 0; fi
  sleep $((i * 3)); git fetch -q origin main
  git rebase -q origin/main || reapply
done
echo "::error::push failed after retries"; exit 1
