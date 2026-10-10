# Paper trading system (Oscar) — PAPER ONLY

No real money, no wallets, no private keys, no swaps. Plain Python; the only AI involvement is an external routine that
writes sentiment JSON files. GBP £200 paper account in three pots: CORE £100 (50%) / DAY £60 (30%) / RISK £40 (20%).

## Architecture (v4, 2026-10-10): runs 24/7 on GitHub Actions
The box sleeps when the AI is not active, so the */15 cron missed hours. The engine now runs on GitHub Actions in the
public repo **oscarwy08/trading-dashboard** (public => free Actions minutes). Single source of truth = branch `main`:
```
main
├── .github/workflows/cycle.yml   schedule */15 + workflow_dispatch; concurrency group 'trading-cycle' (no overlaps);
│                                 permissions contents: write (+ pages: write); setup-python with pip cache
├── scripts/ci_cycle.sh           collector -> ingest_sentiment -> trader -> maintenance -> dashboard -> daily_summary
├── scripts/ci_push.sh            commit data/ + docs/ and push with GITHUB_TOKEN (rebase/re-apply on races; squash)
├── *.py, config.yaml, requirements.txt, web/ (SPA templates), test/sim.py
├── data/trading.db               STATE (SQLite) - committed every cycle
├── data/sentiment/*.json         new sentiment files (pushed from the box) -> processed/ | rejected/ after ingest
├── data/daily_summary.txt        latest scorecard (every cycle)
├── docs/                         PUBLISHED SITE (index.html, app.js, styles.css, data.json, .nojekyll) - GitHub Pages
├── index.html + .nojekyll        redirect to docs/ (only used while Pages still serves the repo root)
├── KILL                          (create to halt all trading)       RISK_RESUME (create to resume the RISK pot after review)
└── push_sentiment.sh, pull_state.sh, repo_flag.sh, repo_lib.sh   box-side helpers (see below)
```
INSTALL STATUS / HANDOVER: the fine-grained PAT used by the box cannot push files under .github/workflows/ without the
'Workflows: Read and write' permission, so the workflow is stored as scripts/github-workflow-cycle.yml. Until it is
installed, the box runs the identical cycle from its clone with `box_cycle.sh` (cron */15; same ci_cycle.sh/ci_push.sh,
pushes state + site to main - repo stays the single source of truth, but runs only while the box is awake).
box_cycle.sh tries to install the workflow every run; once the token has the permission (or the file is created via
the GitHub web UI at .github/workflows/cycle.yml), it stops cycling on the box (no two writers) and, after the first
successful Actions run, comments out its own cron entry. Check: https://github.com/oscarwy08/trading-dashboard/actions

Site: https://oscarwy08.github.io/trading-dashboard/ - GitHub Pages source should be **main / docs** (Settings -> Pages
-> Build and deployment -> Deploy from a branch -> main, /docs). While Pages still serves main / (root) the root
index.html redirects to /trading-dashboard/docs/, so the URL keeps working either way.

Repo size: the SQLite DB is committed every cycle, so (1) maintenance.py prunes it daily (15-min prices kept 3 days,
hourly to 30 days; HOLD/SKIP signals thinned after 2 days, dropped after 7; trades/equity/sentiment/fx kept) and VACUUMs;
(2) scripts/ci_push.sh squashes main into a single orphan commit (force-push with lease) once it has > 100 commits
(~1 day), so old binary DB versions become unreachable. Never rely on git history of main for audit - the DB itself
holds the full trade/equity history.

Timing: GitHub may start scheduled runs late (often 5-20 min) or skip some under load. The trader is delay-tolerant:
DAY positions are force-closed by the first cycle at/after 22:00 UK, or by any cycle before 08:00 UK, or by the first
cycle on a later UK day (opened_day != today); CORE rebalance runs at the first cycle at/after Monday 09:00 UK.

Box side (the AI routine, 3x/day; token in /workspace/trading/.github_token is passed via GIT_ASKPASS, never printed):
- `/workspace/trading/push_sentiment.sh` - pushes data/sentiment/*.json from the box drop folder to the repo
  (validates, leak-checks, fetch + hard-reset + re-copy + retry up to 6x if Actions pushed meanwhile); pushed files move
  to data/sentiment/pushed/. Actions ingests them at the next cycle.
- `/workspace/trading/pull_state.sh [--summary] [--snapshot]` - mirrors the repo (code, data/trading.db, docs/) into
  /workspace/trading; `--summary` prints daily_summary.py, `--snapshot` renders docs/snapshot.png (headless Chrome, box only).
- `/workspace/trading/box_cycle.sh` - INTERIM box runner (see install status above); logs/box_cycle.log.
- `/workspace/trading/repo_flag.sh KILL on|off` / `repo_flag.sh RISK_RESUME on` - control files via the repo.
- Box clone: /workspace/trading-repo (shallow, no secrets). Edit code there, commit, push; then pull_state.sh.
- The box no longer trades: its cron entries for run_cycle.sh / ingest+dashboard / publish / snapshot are commented out
  (backup: .bak-v4/crontab.txt). run_cycle.sh and publish_dashboard.sh are DEPRECATED - running them would fork state.
  dashboard/ on the box is a symlink to docs/ (local server dashboard_server.py on 127.0.0.1:8787 still works).

## Layout
```
config.yaml           strategy + risk parameters (edit here)
common.py             paths (SITE = docs/), SQLite schema (+ transfers table), sleeves core/day/risk, transfer rebasing, logging
collector.py          prices (USD): ONE CoinGecko top-300 markets snapshot (2 calls) -> base coins + RISK breakout screen
                      (+ Monad-ecosystem tokens with a KyberSwap liquidity check), KyberSwap MON/USDC, xStocks (-> Yahoo fallback,
                      fetched in parallel), USD->GBP FX (hourly), Monad gas, daily closes of the stock universe (Yahoo) + ranking
riskpot.py            RISK pot screen/scores (crypto breakout from the markets snapshot; xStock breakout from daily closes)
stockrank.py          CORE stock scoring/selection + display-only investment plans (pure DB, no network)
trader.py             three sleeves: CORE long holds / DAY intraday / RISK speculative: signals + paper trades + risk rules
maintenance.py        daily DB pruning + VACUUM (keeps the committed DB small)
rebalance_pots.py     move UNINVESTED cash between pots (TRANSFER, no reset; backs up the DB)
replan_core.py        one-off: force the CORE rebalance/re-pick at the next trader cycle (backs up the DB; no reset)
universe_check.py     re-verify the stock universe against the public xStocks assets list + quotes + Yahoo history
ingest_sentiment.py   loads data/sentiment/*.json into SQLite
reset_sleeves.py      one-off account reset to £200 with the configured split (backs up the DB first)
test/sim.py           backtest-style dry run on a throwaway DB copy (simulated clock + synthetic prices); deletes the copy.
                      v4 adds RISK: breakout entries via the real screen, 12% stop, trailing take-profit, 7-day max hold,
                      max 5 open / 3 new per day / <= £8, drawdown-cap halt + RISK_RESUME, DAY ignores risk-only coins,
                      cash reconciliation for all three pots, TRANSFER rebasing. Last run: logs/sim_v4.txt (ALL CHECKS PASSED)
dashboard.py          exports docs/data.json (pure SQLite -> JSON, no AI) and installs the static SPA from web/
web/                  SPA templates: index.html, app.js, styles.css (edit here; dashboard.py copies them to docs/)
snapshot.py           docs/snapshot.png via headless Chrome (box only, at scorecard time)
daily_summary.py      text scorecard (TOTAL + CORE / DAY / RISK lines)
data/trading.db       SQLite: prices (USD), fx, sentiment, signals, trades/positions/cash/equity/transfers (GBP), state
logs/                 trading.log (errors/warnings; not committed - on Actions see the run log)
KILL                  create this file IN THE REPO to halt all trading (./repo_flag.sh KILL on)
```
Backups: .bak-v4/ (code + crontab + DB before v4), data/trading.db.bak-pre-v4, data/trading.db.bak-transfer-* (box).

Dashboard (v3, 2026-10-09; v4 adds the Risk pot tab): single-page app in an X.com-style look (pure black, white/grey text, one blue accent #1D9BF0,
green/red only for gains/losses, Inter/system font, rounded cards), TradingView Lightweight Charts from jsDelivr. index.html
loads data.json and re-fetches it every 60 s. Linkable hash tabs (scroll horizontally on mobile):
#overview (account value, total + today's P&L, cash vs invested, vs-Bitcoin in plain words, account value chart, pots,
latest orders, collapsible risk/feeds), #portfolio (cash, holdings table with avg buy price / P&L / allocation / pot,
allocation donut), #core (Core pot: target vs actual, current picks with theme/score/why, build progress, next rebalance,
investment plans for +£100/+£500/+£1,000, full watchlist ranking), #day (Day pot status, open positions, today's trades), #risk (Risk pot: rules, status/drawdown, open positions with
stop/trailing level/sell-by date, breakout candidates with the decision, the crypto screen, risk orders),
#markets (prices, 24h, sparkline, signal, sentiment; row opens the asset detail #asset=XRP: candles with buy/sell markers,
signal score, reasons, sentiment + sources, orders), #sentiment (per-asset scores, summaries + sources), #orders (full order
history with fee + gas, filters incl. pot = Core/Day/Risk and cash TRANSFER rows, CSV export). UI words: Cash, Holdings, Orders, Core pot, Day pot, Risk pot (code/DB still say sleeve).
Previous UI: .bak-v3/web/ (and .bak-dashboard-v1/). data.json keeps hourly OHLC for 7 days + 4-hourly to 14 days, signal
scores 7 days, last 500 trades, last 300 sentiment rows. Public: https://oscarwy08.github.io/trading-dashboard/ (served from main:/docs).
Local: http://127.0.0.1:8787/ (box only; HTTP Basic Auth, user oscar, password in .dashboard_auth; served by dashboard_server.py). Opening dashboard/index.html via file:// will not load data.json (browser fetch rules) - use the server.
Public tunnel: tunnel_watchdog.sh (cloudflared quick tunnel) - NOT active: box egress only allows HTTPS/443, cloudflared needs port 7844.

## Currency (GBP)
The paper account is in GBP: starting_balance £200 (config.yaml `currency: GBP`, `starting_balance: 200`,
`sleeve_split_pct` core 50 / day 30 / risk 20). Source prices are USD and stay raw USD in the `prices` table (momentum is computed on USD).
Each collector cycle stores a USD->GBP rate in table `fx`: primary = CoinGecko `simple/price?ids=bitcoin&vs_currencies=gbp,usd`
(gbp/usd ratio; fetched at most hourly - fx.refresh_min), cross-check/fallback = https://open.er-api.com/v6/latest/USD (free, no key, updates daily).
If both fail the last stored rate is used; if it is older than fx.max_age_h (24h) new buys are blocked (exits still run).
Trader converts prices to GBP at the current rate, so cash, entries, fills, trade values, fees, P&L, equity and the
BTC buy-and-hold benchmark (BTC price in GBP, restarted from £200 at the reset) are all GBP and include FX moves.
Trades also record price_usd and the usd_gbp rate used. Dashboard/summary show £ (prices in £ and USD).
Gas: every simulated trade also pays a Monad network fee (config `gas:`): collector reads eth_gasPrice from rpc.monad.xyz
each cycle (with retries), x 200,000 gas per swap -> MON -> USD (latest MON price) -> GBP, stored in state 'gas'; the trader
uses it if < 6 h old, else gas.fallback_gbp (£0.001). Gas is paid from cash on top of the trade value (buys: added to the
position's cost basis; sells: deducted from proceeds/P&L) and recorded in trades.gas. Typical: ~102 gwei -> ~0.02 MON -> ~£0.0004.

## Strategy: three sleeves (all parameters in config.yaml `core:` / `day:` / `riskpot:`; shared costs in `risk:`)
£200 split 50/30/20 into three sleeves with SEPARATE cash and positions (table positions is keyed by (sleeve, asset); the
same asset can be held in several). Trades, signals and equity rows carry the sleeve. Fees/slippage 0.3% per side, min trade £5.

v4 split change WITHOUT a reset (2026-10-10 11:26 UK, `rebalance_pots.py`): the old split was CORE £120 / DAY £80. Only
uninvested cash was moved, recorded as TRANSFER rows in table `transfers` (shown on the Orders tab):
DAY -> RISK £19.99885 (all DAY cash; its 3 open day positions were left alone) and CORE -> RISK £20.00115 (from CORE's
£41.99 cash), so RISK starts with exactly £40.00 and the pot baselines are CORE £100 / DAY £60 / RISK £40 (config
split). Pot returns/drawdowns are measured against these baselines; earlier pot history is rebased by later transfers
(common.transfer_adj/rebase), so a transfer never looks like profit/loss, and the DAY pot's UK-day start equity was
adjusted the same way so the 5% daily loss cap did not fire. Account total (£200 start) is unaffected.

CORE £100 (long holds). Targets (% of CORE equity, v3 2026-10-09): crypto 60% = MON 15, WLD 12, XRP 15, BTC 10, ETH 8;
stocks 40% split equally over the picks. Stock universe: config `stocks.universe` - 84 growth names that exist as xStocks
(verified against api.xstocks.fi public assets list + live quote + 6 months of Yahoo history; all deployed on Monad) in 9
themes: AI chips & semis, AI platforms & big tech, crypto-linked, energy/nuclear/AI power, cloud/software/cyber, consumer
tech, space & defence, quantum, fintech. Not available as xStocks: VST, CCJ, SPOT, RGTI. Index ETFs excluded (stock_exclude).
Data: collector refreshes 1 year of Yahoo daily closes (table daily_closes) for at most 20 stale tickers per cycle (each
~once a day) and re-ranks (state stock_rank). Intraday xStocks quotes only for the fixed watchlist + CORE picks + held
stocks + the top 8 ranked names. Weekly scoring at the Monday rebalance (stockrank.py): composite -1..1 = 0.55 momentum
(6m/3m/1m returns divided by vol*sqrt(days), weights 0.4/0.4/0.2) + 0.30 trend quality (above 50d avg, 50d avg rising,
distance from 6m high) + 0.15 sentiment (7-day confidence-weighted, if any; else weights rescaled). Eligible: 3m return > 0,
above 50d average, momentum > 0. Picks: best first, max 2 per theme, count = 40% of CORE equity / £7.50 (min_pick_gbp),
clamped 1..8 (so with ~£100: 5 picks of ~£8, every buy >= £5 min trade); max 10% per pick if few are eligible.
Investment plans (display only): +£100 / +£500 / +£1,000 into the Core pot, same targets, more picks (8 / 12 / 16, max 2/2/3
per theme), showing what would be bought (gaps to target, funded by the top-up + Core cash; never sells). Staggered entry: each position is built in up to 5 tranches (target/5, but at
least £5, so small targets need fewer), max 1 tranche per asset per UK day; a tranche fires when price <= 24h average
(buy the dip) or, if no dip that day, from 18:00 UK. Strongly negative fresh sentiment (<= -0.5, conf >= 0.5) delays the
tranche; sentiment never changes targets. Weekly rebalance Monday 09:00 UK (first cycle at/after; caught up later that week
if missed): re-picks the stocks, sells dropped picks, and trades built assets back to target only if drift > 5pp and the
trade is >= £5. Stop-loss 20% from average cost (incl. fees), no take-profit; a stopped asset stays in cash until the next
rebalance, then is rebuilt in tranches. Plan state: state keys core_plan / core_stocks / core_last_rebalance / core_rebalance_log.
`python3 replan_core.py "reason"` sets core_force_rebalance so the next cycle runs the rebalance immediately (used on
2026-10-09 to switch to the v3 targets/picks without resetting the account; DB backup data/trading.db.bak-pre-v3).

DAY £60 (intraday). Same momentum + sentiment score as before (coins/stocks priced only for the RISK pot are ignored):
momentum = 0.35*4h return + 0.35*24h return (scaled, clipped) + 0.2*EMA6/24 hourly cross + 0.1*RSI(14h) term;
with sentiment < 12h old: combined = (1-w)*momentum + w*score, w = 0.4*confidence.
Buy when combined >= 0.35 (0.30 for MON/WLD/XRP); exit on combined <= -0.20, stop-loss 3%, take-profit 5%.
Size 25% of DAY sleeve equity per position, max 3 open. New entries only 08:00-21:00 UK; ALL day positions are
force-closed at/after 22:00 UK (the */15 cron cycle at 22:00), and any day position found before 08:00 or carried from a
previous UK day is closed too (flat overnight). Daily loss cap: DAY equity <= -5% vs its UK-day start halts new DAY
entries for the rest of that UK day (exits still run; CORE unaffected).
RISK £40 (speculative, v4). Universe:
- crypto: each cycle ONE CoinGecko markets snapshot of the top 300 by market cap (per_page 250 + page 6 of 50; plus a
  markets-by-ids call only if a core coin / held coin is missing). Filters: rank > 40 (no large caps), market cap $20M-$5B,
  24h volume >= $5M and >= 3% of market cap, not a stablecoin / wrapped / staked / bridged / exchange token / tokenized
  stock-fund-gold (symbol + name rules in riskpot.crypto.exclude_*; pegged-looking coins dropped). Bonus: the
  CoinGecko 'monad-ecosystem' category (refreshed every 6 h) - tokens with the same floors and a Monad contract address
  (CoinGecko coin details, cached) are added if a KyberSwap route on Monad for $200 USDC loses < 3% (liquidity check).
  Only the 12 best breakout candidates (+ those that screened in within 48 h, + held coins) are priced/stored each cycle;
  the screen is kept as state 'risk_screen' (small JSON) - shown on the Risk pot tab.
- stocks: riskpot.stocks (quantum IONQx QUBTx; space RKLBx ASTSx LUNRx RDWx; nuclear OKLOx SMRx NNEx UUUUx; miners/AI
  hosting IRENx CORZx WULFx CLSKx BMNRx MARAx RIOTx HUTx CRWVx NBISx; SOFIx HOODx), priced intraday every cycle.
Entry (breakout + sentiment): crypto score = 0.35 x 24h move (20% = full) + 0.25 x 7d (40%) + 0.2 x closeness to the
24h high + 0.2 x volume/market-cap; must be up >= 4% (24h) and >= 5% (7d), within 8% of the 24h high, not dropping
(1h > -2%) and not a > +60% one-day pump. Stocks: 0.35 x 5-day return (15%) + 0.25 x 20-day (40%) + 0.2 x closeness to
the 20-day high + 0.2 x trend (above a rising 50d average); must be +3% over 5 days, at >= 97% of the 20-day high, above
the 50d average. Combined with fresh sentiment like DAY (w = 0.4 x confidence); buy when combined >= 0.45 AND the
intraday momentum model (same as DAY, >= 2h history) >= 0; no entry while fresh sentiment <= -0.3 (conf >= 0.5).
Size 20% of the pot equity, max £8; max 5 open, max 3 new per UK day, 24h cooldown per asset after an exit.
Exits: stop-loss 12% from average cost (incl. fees/gas); trailing take-profit: once the peak is >= +15% above cost, sell
when price falls 10% from the peak (peak in state risk_peaks); max hold 7 days; sentiment exit <= -0.5 (conf >= 0.6).
Drawdown cap: if the pot is down 30% vs its base (start £40, or the equity at the last review) new entries stop
(state riskpot_halt; exits continue) until RISK_RESUME exists in the repo (./repo_flag.sh RISK_RESUME on); the trader then
resets the base to the current equity and deletes the file.

KILL file: trader does nothing at all for any sleeve (no entries, no exits); prices keep being collected.
FX older than 24h blocks all buys (exits still run).

## Sentiment drop-folder schema
Write files to `/workspace/trading/data/sentiment/` with extension `.json`, then run `/workspace/trading/push_sentiment.sh`
(pushes them to the repo; GitHub Actions ingests them at the next cycle). One object or a list of objects:
```json
{
  "timestamp": "2026-10-09T18:00:00Z",   // ISO 8601, required (Z or offset)
  "asset": "XRP",                         // required; ticker as used here: BTC, ETH, XRP, WLD, MON, SOL, ... or NVDAx etc.
  "score": 0.35,                          // required, -1 (very bearish) .. 1 (very bullish)
  "volume": 1240,                         // int, number of posts/articles considered (default 0)
  "confidence": 0.7,                      // required, 0..1 (scales sentiment weight)
  "summary": "Short human-readable rationale",
  "sources": ["https://..."]              // list of URLs
}
```
In the repo, valid files move to `data/sentiment/processed/`, invalid ones to `rejected/` (reason in the Actions run log).
(timestamp, asset) is unique; re-sending replaces. Tip: write to a temp name then rename to *.json.
