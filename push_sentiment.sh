#!/bin/bash
# Push the AI routine's sentiment JSON files to the repo so the next GitHub Actions cycle ingests them.
#   source : /workspace/trading/data/sentiment/*.json (the drop folder the AI routine already writes to; schema in README)
#   target : repo main:data/sentiment/<same name>.json  (Actions moves them to processed/ or rejected/ after ingesting)
# Invalid JSON never leaves the box (moved to data/sentiment/rejected/). Pushed files are moved to data/sentiment/pushed/.
# Retries up to 6x (fetch + hard reset to origin/main + re-copy) when an Actions cycle pushed in the meantime.
# Usage: ./push_sentiment.sh [file.json ...]     (default: every *.json in the drop folder)
set -u
. /workspace/trading/repo_lib.sh
SRC="$BASE/data/sentiment"; LOG="$BASE/logs/push_sentiment.log"
log() { echo "[$(date '+%F %T %Z')] $*" | tee -a "$LOG"; }
exec 9>/tmp/trading_repo.lock; flock -w 180 9 || { log "ERROR: repo busy"; exit 1; }
mkdir -p "$SRC/pushed" "$SRC/rejected"
if [ $# -gt 0 ]; then FILES=("$@"); else mapfile -t FILES < <(find "$SRC" -maxdepth 1 -type f -name '*.json' | sort); fi
GOOD=()
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d=d if isinstance(d,list) else [d]; assert all({"timestamp","asset","score","confidence"} <= set(x) for x in d)' "$f" 2>/dev/null; then
    GOOD+=("$f")
  else log "rejected locally (invalid JSON/schema): $(basename "$f")"; mv -f "$f" "$SRC/rejected/"; fi
done
[ ${#GOOD[@]} -gt 0 ] || { log "no sentiment files to push"; exit 0; }
repo_auth || exit 1
repo_leakcheck "${GOOD[@]}" || exit 2
for i in 1 2 3 4 5 6; do
  repo_sync || { log "sync failed (try $i)"; sleep $((i * 5)); continue; }
  mkdir -p "$REPO_DIR/data/sentiment"
  for f in "${GOOD[@]}"; do cp -f "$f" "$REPO_DIR/data/sentiment/$(basename "$f")"; done
  G add -- data/sentiment
  if G diff --cached --quiet; then log "nothing new (already in repo)"; break; fi
  G commit -q -m "sentiment: ${#GOOD[@]} file(s) from the AI routine $(date -u +%FT%TZ)"
  if G push -q origin HEAD:main 2>>"$LOG"; then
    log "pushed ${#GOOD[@]} file(s): $(for f in "${GOOD[@]}"; do printf '%s ' "$(basename "$f")"; done)"
    for f in "${GOOD[@]}"; do mv -f "$f" "$SRC/pushed/"; done
    exit 0
  fi
  log "push rejected (Actions pushed meanwhile?) - retry $i"; sleep $((i * 5))
done
log "ERROR: could not push sentiment after retries (files kept in $SRC)"; exit 1
