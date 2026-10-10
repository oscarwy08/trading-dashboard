#!/bin/bash
# One trading cycle on GitHub Actions: collector -> sentiment ingest -> trader -> maintenance -> dashboard -> scorecard.
# A collector/ingest failure is logged but the trader still runs (it refuses to buy on stale prices/FX on its own).
# Exit 1 if trader or dashboard failed (the workflow still commits whatever state was written, then marks the run red).
set -u
mkdir -p logs data/sentiment/processed data/sentiment/rejected
export TZ=Europe/London
fail=0
echo "=== cycle $(date '+%F %T %Z') ==="
${PY:-python} collector.py        || echo "::warning::collector failed"
${PY:-python} ingest_sentiment.py || echo "::warning::sentiment ingest failed"
${PY:-python} trader.py           || { echo "::error::trader failed"; fail=1; }
${PY:-python} maintenance.py      || echo "::warning::maintenance failed"
${PY:-python} dashboard.py        || { echo "::error::dashboard failed"; fail=1; }
${PY:-python} daily_summary.py > data/daily_summary.txt 2>&1 || echo "::warning::daily_summary failed"
cat data/daily_summary.txt
if [ -s logs/trading.log ] && grep -qE " (WARNING|ERROR) " logs/trading.log; then
  echo "--- warnings/errors this cycle (logs/trading.log) ---"; grep -E " (WARNING|ERROR) " logs/trading.log | tail -40
fi
exit $fail
