#!/bin/bash
# Create or remove a control file in the repo root (picked up by the next GitHub Actions cycle):
#   ./repo_flag.sh KILL on|off        kill switch: halts ALL trading (no entries, no exits; prices keep being collected)
#   ./repo_flag.sh RISK_RESUME on     after reviewing a RISK pot drawdown halt: resume entries (trader deletes the file)
set -u
. /workspace/trading/repo_lib.sh
F=${1:-}; ACT=${2:-}
case "$F:$ACT" in KILL:on|KILL:off|RISK_RESUME:on) ;; *) echo "usage: $0 KILL on|off | RISK_RESUME on" >&2; exit 1 ;; esac
exec 9>/tmp/trading_repo.lock; flock -w 180 9 || exit 1
repo_auth || exit 1
for i in 1 2 3 4 5; do
  repo_sync || { sleep $((i * 5)); continue; }
  if [ "$ACT" = on ]; then echo "$(date -u +%FT%TZ) set from the box" > "$REPO_DIR/$F"; G add -- "$F"; else G rm -q --ignore-unmatch -- "$F"; fi
  G diff --cached --quiet && { echo "$F already $ACT"; exit 0; }
  G commit -q -m "$F $ACT (box)" && G push -q origin HEAD:main && { echo "$F $ACT pushed - applies at the next cycle"; exit 0; }
  sleep $((i * 5))
done
echo "ERROR: push failed" >&2; exit 1
