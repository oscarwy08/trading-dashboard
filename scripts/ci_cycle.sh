#!/bin/bash
# One trading cycle on GitHub Actions: collector -> sentiment ingest -> trader -> maintenance -> dashboard -> scorecard.
# A collector/ingest failure is logged but the trader still runs (it refuses to buy on stale prices/FX on its own).
# Exit 1 if trader or dashboard failed (the workflow still commits whatever state was written, then marks the run red).
set -u
mkdir -p logs data/sentiment/processed data/sentiment/rejected
export TZ=Europe/London
# Optional secrets for the VPS (not in git). Example line: COINGECKO_API_KEY=...
# TRADING_ENV overrides the path. The file is sourced, never printed.
_env="${TRADING_ENV:-$HOME/.trading_env}"
if [ -f "$_env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$_env"
  set +a
fi
unset _env
fail=0
echo "=== cycle $(date '+%F %T %Z') ==="
_cycle_t=$(date +%s)
# Seconds for one step, then its exit code. The echo stays on the cycle log.
run_step() {
  local name="$1"
  shift
  local t0 rc
  t0=$(date +%s)
  "$@"
  rc=$?
  echo "[timing] ${name} $(( $(date +%s) - t0 ))s"
  return "$rc"
}
# one-off migrations (migrate_v5_xstocks.py, migrate_v6_accounts.py) are NOT run any more: both are done and the great reset
# (2026-10-10, great_reset.py) started fresh v7 accounts - re-running them could only corrupt the fresh state.
run_step collector ${PY:-python} collector.py        || echo "::warning::collector failed"
run_step ingest ${PY:-python} ingest_sentiment.py || echo "::warning::sentiment ingest failed"
# Once, on the first cycle after this lands: archive Test 1 and restart every book at £200.
# Later cycles see data/tests/generation.json and do not reset again.
run_step test_reset ${PY:-python} testreset.py    || echo "::warning::test reset failed"
run_step trader ${PY:-python} trader.py           || { echo "::error::trader failed"; fail=1; }
# Variant books use the prices this cycle already stored. A failure here must not fail the main book.
run_step variants ${PY:-python} variants.py         || echo "::warning::variants failed"
run_step maintenance ${PY:-python} maintenance.py      || echo "::warning::maintenance failed"
run_step dashboard ${PY:-python} dashboard.py        || { echo "::error::dashboard failed"; fail=1; }
run_step check_data ${PY:-python} test/check_data.py docs/data.json || echo "::warning::dashboard figures do not reconcile (test/check_data.py)"
# The scorecard file must not swallow the timing line.
_t=$(date +%s)
${PY:-python} daily_summary.py > data/daily_summary.txt 2>&1 || echo "::warning::daily_summary failed"
echo "[timing] daily_summary $(( $(date +%s) - _t ))s"
cat data/daily_summary.txt
# Shadow report is outside the clone. Publish a copy at most hourly so the push carries it.
# A missing ~/exit_shadow/exit_shadow.db writes a no-data note and does not fail the cycle.
run_step exit_shadow ${PY:-python} exit_shadow_report.py --write || echo "::warning::exit shadow report failed"
echo "[timing] cycle $(( $(date +%s) - _cycle_t ))s"
if [ -s logs/trading.log ] && grep -qE " (WARNING|ERROR) " logs/trading.log; then
  echo "--- warnings/errors this cycle (logs/trading.log) ---"; grep -E " (WARNING|ERROR) " logs/trading.log | tail -40
fi
exit $fail
