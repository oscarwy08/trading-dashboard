# Paper trading system (Oscar) — PAPER ONLY

> **For AI agents**: Read [AGENTS.md](AGENTS.md) first, then [PROJECT_BRIEF.md](PROJECT_BRIEF.md) and this file.  
> **For humans**: Start with [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the big picture, then return here for technical details.

No real money, no wallets, no private keys, no swaps. Plain Python; the only AI involvement is an external routine that
writes sentiment JSON files. GBP £200 paper money in two accounts (Crypto £100 = Monad wallet, Stocks £100 = Trading 212) and three pots: CORE £100 (50%) / DAY £60 (30%) / RISK £40 (20%), each pot split 50/50 across the accounts.

## CoinGecko budget and backup prices (2026-10-12)

The last 30% of the daily CoinGecko budget (`reserve_pct`) is only for `simple/price` of coins we hold and coins with an armed entry, plus a quote immediately before a fill. A top-300 scan, category refresh, chart or backfill, the FX cross-check, and the general fallback for every unlisted coin stop before that slice. When CoinGecko is skipped or returns nothing, a coin with no Kraken, Coinbase or Kyber route can take the most liquid GeckoTerminal or DexScreener pool, rejected if it is more than twice the last good price. The price row is tagged with that source. The markets page lists how many calls each purpose spent.

## Volume sanity (2026-10-15)

A last-hour volume above 50 times the average hour, or a rolling 24h total that jumps more than 5 times inside the ratio window, is rejected. It is not a spike and it is not missing data, so it cannot open a Day or Risk entry on the main book, any paper book, or the watcher. The 2x volume gate is unchanged. The 13 Oct 2026 LDO print (about 2,449x, from one Kraken vol24 that reverted on the next quote) is the case this catches.

## Backtest (report only)

`python3 backtest.py` replays the stored prices in `data/trading.db` through the main book and each name in `variants.run`. It uses the live trader, so fees and slippage are the ones the cycle charges. It writes a temporary copy only. It does not change the frozen rules or the live database.

Each book is marked FAIL when closed trades are under 100, max drawdown is over 20%, profit factor is under 1.1, or net P&L does not beat buying and holding BTC over the same window (one buy and one sell at `fee_slippage_pct`). The clock steps every 5 minutes (`--step` changes it). Before each step, a daily close dated that UTC day or later is hidden, and the stored Risk screen is hidden until its own timestamp, so Core picks and Risk crypto cannot see later bars. The report also buckets the probability stored at entry against whether the later sell made money after costs.

## v7.9 (2026-10-12): dead quotes, EV exit off, Risk win is the trail lock (paper only)

Three corrections on the main book. The frozen thresholds are otherwise unchanged.

- **Dead exit price.** A held quote older than `price_dead_min` (180 minutes live, 45 offline) does not stop, trail, or flatten. The position is marked unpriced and is sold on the first fresh print. Entries already refused a dead price. For a stock, those minutes are time the exchange was open (London 08:00–16:30 UK, the US session 14:30–21:00 UK most of the year, weekdays, with the exchange holiday calendar). While that market is closed the row says the last close and is not unpriced; stops, trails, and the flatten still wait for the next open.
- **EV exit off.** `ev_now` rises as the trade falls, so it was a take-profit on winners. `ev_action` stays off until the upside is taken from the current price and the chance is recomputed. `ev_exit`, `all_combined` and `everything` are reset from the current main book; sells before that reset are not counted. The dashboard says so.
- **Risk expected win.** The win in `setup_ev` is the gain the trail locks when it arms: `(1 + 15%) × (1 − 10%) − 1 = 3.5%`, the same on every coin. It is not the old 13.5%, and it is not scaled by ATR. The loss prior is still scaled by ATR. The 12% stop and the trail rules are unchanged.

## Paper books (2026-10-12): six variants beside the frozen book

None of these changes the main book. Each is its own database, shadow-logged by the exit watcher, and compared on the variants page with the same columns as the Day sub-strategy table.

- **risk_giveback_15.** Arm stays at +15% net. Once armed, sell when the net gain falls to 65% of the peak net gain. The 7-day exit waits while the trail is armed. The −12% stop stays.
- **day_trail_arm.** A Day trail arms only when 60% of the peak gain is at least the greater of 4× that trade's round-trip cost and 1× its 1-hour ATR. Stop, target, the burst 6-hour hold and the 22:00 flatten stay.
- **day_ev_blend.** Per sub-strategy, EV = (n × measured expectancy + 10 × the model) / (n + 10). Momentum and burst are measured apart. This book keeps the older Risk win (`risk_ev_legacy_win`), not the 3.5% trail lock.
- **no_risk_clock.** The 24-hour Risk re-entry clock is deleted. No new-high gate and no stop-out cooldown.
- **day_capital_caution.** Per account, f = that account's Day loss today / 15% of that account's start-of-day Day equity, with no cap at 1. The hurdle rises by 0.30 × f and size is multiplied by (1 − min(f, 1)). The −15% halt on the whole Day pot stays.
- **partial_take.** At the trail arm, sell half of a Day or Risk position and leave the rest on that book's trail. Buy the half back on a later dip only when a fresh expected value (recomputed chance, upside and downside from the current price) clears the hurdle. That half may be £1. The £5 minimum is unchanged on every other order. The sell logs the extra fee of splitting one exit into two.
- **stock_calibrated.** Stocks only. Momentum is scaled by the 1-hour ATR (a 4-hour move of 1 ATR, or a 24-hour move of 2 ATR, fills that part of the score). Day target and stop floors are 2 ATR and 1 ATR, not the frozen percent floors. While more than 4 hours remain until that stock's own close, the 0.40 clock-exit floor is not applied. Stock hit rates use only stock closes and a prior of 0.45 over 12 trades. A Risk dip's uptrend is 2 times ATR when a sentiment print exists; with no print the plain 48-hour uptrend is enough. The Risk list is the watch themes. Crypto on this book is unchanged.
- **sent_split.** Burst and Risk only, beside the frozen book. A missing sentiment print scores 0.50 instead of 0.25. A Risk dip needs the 48-hour uptrend; positive sentiment does not replace it. A 24-hour run-up of 15% or more (4-hour run-up of 5% or more on a Day momentum entry) with sentiment at least 0.30, or a buzz change of 3 or more, subtracts 0.10 from burst and Risk confidence and sets that Day momentum entry's sentiment weight to 0. When a sentiment file carries `catalyst_score`, that score is used instead of the raw score. A quiet tape (`buzz_change` under 1.2) with volume at least 2× and a confirmed breakout or bounce adds 0.05. Files without those fields score as before.

## Stock intraday history (2026-10-12)

Day and Risk stock rows were scoring RSI 50 and 4h +0.0% because Yahoo's 15-minute closes were fetched and then thrown away. Only one print per cycle was stored, so a weekend left fewer than 15 hourly closes and the score filled the gap with a neutral RSI and a zero move. The collector and the pre-trade quote now keep those 15-minute closes (USD, no duplicate timestamps). Stock momentum looks back eight calendar days so prior sessions survive the weekend. If the 4h move or the 15 hourly closes are still missing, the signal is `stale history` and the name does not score. Buy and sell thresholds are unchanged. Crypto prices still come from `pricesrc.py` (Kraken / Coinbase / Kyber / CoinGecko); that router does not carry stock bars.

## v7.8 (2026-10-10): honest costs, honest EV, and a frozen scorecard (paper only)

The rule set is frozen at `rules.frozen_since` (shown on the dashboard). The next change to a threshold needs a reason that is not one day's P&L.

- **Scorecard.** Overview and the daily summary list, per setup (momentum, burst, risk breakout, risk dip): closed trades, net after fees, fee/gross, win rate, and how the exits split (flatten, stop, target, trail, time, signal).
- **Expected value.** A confidence score is not a win probability. With no closes, P(win) is 0.5. It moves toward the score only as closes build (`ev_conf_full_n`, 30) and is blended with the measured hit rate. A trade needs EV after costs above `ev_min_pct` (0.3%), not merely above zero. Day EV includes the 22:00 flatten (and the burst time stop): that share of the trade pays the round trip and keeps none of the target. Burst stays in the code. The hurdle is what keeps it rare. There is no off switch and no loss-streak cooldown.
- **No chase.** A 4h range tighter than 0.3% is stuck prints, not a breakout. Momentum entries at RSI 80 or above are refused (burst already did this). A Risk breakout up 80% or more over 7 days is refused, and from +40% the breakout score is scaled down. Core cash is not lent into either of those names.
- **Costs.** Crypto fills use market cap, volume/market-cap, and a stored Kyber route loss when one exists, between a 0.05% floor and a 1% cap. Unknown market cap keeps the old 0.30% per side. Trading 212 FX stays 0.15% per side. Gas is unchanged.
- **Missing sentiment** scores 0.25, below a real neutral 0.5, on burst, Risk breakout, and dip confidence.
- **After 22:00.** A Day position still open is force-closed at the last stored price. If there is no price at all, the dashboard flags it.

## v7.7 (2026-10-10): RISK pot can buy a dip, not only a breakout (paper only)
- **Two setup types.** A breakout is unchanged (near a recent high, short-term trend agreeing). A dip is a coin already in an uptrend, or with positive sentiment, that has pulled back toward a recent low, the day average or a short average, and then turned up (higher low or the short average reclaimed, last bar up). A move still making new lows, a drop deeper than 18%, or a coin still up more than 60% on the day is not bought.
- **Its own confidence** (0-1): pullback depth against typical volatility, how close price is to support, bounce confirmation, sentiment, market regime, that setup's own measured hit rate, and data freshness. Same threshold (0.55, rising toward 0.85 as the drawdown approaches 30%), same rule that expected value after costs must clear the hurdle (0.3%), same size, same 12% stop, trail, 7-day hold and 5-open cap. One row per coin: if both a breakout and a dip qualify, the higher confidence is kept.
- **Borrowing.** A breakout still borrows only when the short-term trend is up. A dip borrows when confidence is at least 0.65, the prior uptrend is there, and expected value clears the same 0.3% hurdle — the 4-hour trend is often still down at the bounce. Core cash is not lent into a name the 7-day run-up rule has marked.
- **Dashboard.** The Risk setups table, open positions and orders say Breakout or Dip, with a one-sentence reason. Dip rows use their own mini-bars.
- **At entry, and EV now.** A Day or Risk buy stores its confidence (a Day momentum trade stores its score instead), the component bars, the chance used, the target and the loss, the costs and the expected value. Open positions, the Orders list and the asset panel show that entry score the same way as the setups table, plus a live **EV now**: the chance of hitting times the percent still left up to the target, minus the chance of losing times the percent down to the stop or the trail, minus the sell cost still to pay. A position bought before this was stored shows a dash, "bought before confidence scoring", for the entry figures, and still shows EV now.
- **Day pot.** Not given a dip entry. Day momentum already buys a pullback when its combined score is still a buy, and every Day position is flat by 22:00 UK, so a second entry would need its own hold and exit rules. Left as a breakout-plus-momentum pot.

## v7.6 (2026-10-10): gold-backed tokens in the Core crypto universe (paper only)
- **PAXG and XAUT** are candidates in the Crypto account's Core universe, theme "Real-world assets (tokenised)" (`core.crypto_universe`). They are not in `targets_pct`. The Monday rebalance scores them with the same momentum / trend-quality / sentiment rules as the stocks and adds one only if it qualifies, at one normal slot (the fixed coins keep their ratios and share what is left). No fixed share.
- They stay out of the Day pot (`day_universe.exclude_symbols`, and still excluded from the Risk screen). Daily closes come from CoinGecko, one coin per cycle.

## v7.5 (2026-10-10): P&L in words, fresher fills, burst floor, Risk EV per setup (paper only)
- **P&L cards** (Overview today and all-time, each account, each pot, Day today) show `open positions` and `closed trades (after fees)` underneath the headline. The two add up to that headline exactly, in pence. Fees are already inside both numbers. Today's closed figure is sells since midnight UK; the open figure is the rest of the day's move.
- **Data freshness.** Every price stores the provider's last-updated time. Right before a live fill the trader asks for a fresh quote from the same router as the collector (Kraken or Coinbase when the coin is listed, KyberSwap for a Monad token, otherwise one CoinGecko simple/price; at most 8 Yahoo quotes). Age is a confidence component on Day burst and Risk: a fresh price leaves the score unchanged, an older one scales it down, and a setup skipped for that says so. Full credit is 10 min for crypto and 5 min for stocks. Sentiment's weight fades to zero by 12 hours; the hard sentiment gates still use the raw score. If a cycle was missed and price went through a stop, the fill is the real price and the reason says `[gap fill]`. Only a missing price (or, offline, one older than 45 min) blocks a trade. Sim and tests stay offline.
- **Burst cash.** If the burst budget is under the £5 minimum but at least £4 (`day.burst.cash_floor_gbp`), that remaining cash is spent. Below £4, an exceptional setup still borrows under the existing inter-pot rules.
- **Day burst list.** If the account filter hides every setup, the rows are shown anyway with a note. A quiet cycle (nothing broke out) still says so.
- **Risk setups.** The table shows the top 8 by confidence (anything bought stays visible) with a show-all toggle.
- **Risk EV** is per setup: confidence as the hit chance, the trail distance and the usual loss scaled by that coin's volatility, minus its own costs, blended with measured results as trades close. The 12% stop and the trail rules are unchanged.

## v7.4 (2026-10-10): RISK pot decides like the Day pot's burst (paper only)
- **Confidence 0-1 per setup** (riskpot.confidence, weights `riskpot.confidence_weights`): breakout strength (the screen's
  breakout score) 30%, volume spike (last hour vs the average hour; 2x = half, 4x+ = full) 20%, news sentiment 15%, market
  regime (BTC 1h/4h for coins, the exchange's index for shares - the same model as the DAY burst) 15%, and the RISK pot's
  measured hit rate (closed trades of the last 30 days, Beta prior 50% x 20 trades) 20%. A ranking, not a probability.
- **Trade only if** confidence >= threshold **and** expected value after costs > 0 (p x 12% avg win - (1 - p) x 8% avg
  loss - round-trip costs, blended with the measured expectancy as trades build up; `ev_win_pct` / `ev_loss_pct`).
  Hard gates kept: real breakout, intraday trend >= 0, sentiment block, drawdown halt, market open, fresh prices, 24h
  cooldown, max 5 open.
- **Threshold** rises linearly from 0.55 (no drawdown) to 0.85 as the drawdown approaches the 30% stop (riskpot.threshold).
- **Size** = confidence x 50% (`conf_size_pct`) of the account's remaining RISK cash, min £5, max £8 or 20% of the pot -
  sizes shrink as cash is used up. The **3-new-per-day cap is removed**; 5 open max, 12% stop, trailing (after +15%, sell
  10% below the peak), 7-day max hold and the 30% drawdown stop are unchanged. Short of cash for a setup with confidence
  >= 0.65 and the trend agreeing -> v7.3 loan.
- **Risk tab** in the Day tab's style: summary cards, gauges (drawdown vs stop, confidence threshold, cash per account),
  results table (closed trades, win rate, avg win/loss, profit factor, closed / open P&L, fees closed vs open buys,
  expectancy; all time / today), positions with their entry confidence + EV, setups table (confidence bar with the
  threshold marker, component mini-bars, EV after costs, decision + reason) and a confidence column on the orders.
- State `riskpot_status` gains threshold / setup stats / per-candidate conf, parts, ev, size, decision; `risk_meta`
  keeps confidence + EV per open position; orders carry `conf`. check_data.py checks the Risk results (crypto + stocks
  = total, fees = closed + open buys, closed P&L = realised), the threshold formula, confidence = weighted components and
  that every buy met threshold + EV + size rules; test/sim.py asserts the same on every cycle of its run.

## v7.3 (2026-10-10): temporary inter-pot loans (same account only, paper only)
- A RISK or DAY pot that lacks the cash for an **exceptional** setup may borrow from another pot **in the same account**
  (crypto lends to crypto, stocks to stocks; never across accounts). Exceptional = RISK setup score >= 0.65 with the
  intraday trend agreeing (momentum > 0), or DAY burst confidence >= 0.75 with EV after costs > 0 (config `loans:`).
- Lenders, in order: CORE's uninvested cash, then the other short-term pot's cash. CORE never lends what it needs today
  (the next tranche of every CORE holding still being built that has not had today's tranche, plus queued CORE buys).
  A pot that owes money does not lend, and a pot borrows for one position at a time.
- Size: enough for ONE position (min trade £5, at most that pot's normal max position); no loan if the trade still could
  not reach £5. Total outstanding per account <= `loans.max_outstanding_pct` (20%) of the account's starting value.
- Accounting: table `loans` (lender, borrower, sub, asset, amount, repaid, status) and LOAN / REPAY rows on the Orders tab
  (filter "Loans between pots"). The lender keeps a receivable, the borrower a debt, so a loan never shows as P&L
  (pot value = cash + positions + lent - borrowed; account totals unchanged). When the position closes the principal is
  repaid to the lender first (partly if the borrower is short; the rest is retried every cycle); the P&L stays with the
  borrower. The DAY burst sub-strategy carries its own loans in the Day ledger (budget = cash + positions - owed).
- Dashboard: Overview pots and the Core/Day/Risk tabs show e.g. "Borrowed £8.00 from Core for STRK" / "Lent £8.00 to
  Risk"; data.json `loans` + `loan_rules`; daily_summary shows pot values incl. loans plus outstanding / repaid loans.
- Tests: `python3 test/test_loans.py` (throwaway DB copy: RISK borrows from CORE, repays on close with the loss kept by RISK,
  CORE tranche reserve -> DAY lends, 20% cap, score < 0.65 -> no loan, DAY burst loan at confidence 0.85 but not at 0.70
  or EV < 0); check_data.py checks lent = borrowed per account, the cap and pot value = cash + invested + lent - borrowed;
  test/sim.py checks every loan of its random run and runs test_loans.py.

## v7.2 (2026-10-10): wider DAY crypto universe (paper only)
- **Universe** (`crypto.day_universe` in config.yaml, built by collector.py from the top-300 CoinGecko snapshot it already
  fetches - no extra price calls): the unchanged top-N base coins **plus** every coin with market cap >= $50M, 24h volume
  >= $5M and 24h volume >= 2% of market cap (max 150 extras). Excluded: stablecoins, wrapped / staked / bridged / liquid-staking
  coins, exchange tokens, `exclude_symbols`, and memecoins (CoinGecko `meme-token` category fetched once a day and cached in
  state `meme_ids`, plus the config `meme_blocklist`). `meme_allow` (DOGE, PEPE) overrides the meme filter. The same
  exclusions apply to the base set for DAY trading: base coins such as BNB / LEO / WBT (exchange tokens) or PAXG / XAUT (gold
  tokens) stay priced (Markets, CORE, RISK) but the DAY pot does not open trades in them (state `day_excluded`; CORE coins
  `core_ids` are never excluded). Saved in state
  `day_universe` (coins, counts) and exported as `data.json.day_universe`.
- **Rules unchanged**: momentum + burst signals, the burst volume rule (last hour >= 2x the average hour), sizing and stops
  are exactly as in v7. New coins are only a bigger pool.
- **Warming up**: intraday history for a new coin is built from the 15-min snapshots (no backfill). Until it has
  `crypto.warmup_h` (4h) of history the DAY pot skips it with "warming up: Xh of 4h price history" (momentum and burst);
  the Markets tab shows a "warming up x/4h" tag, CoinGecko rank + market cap, and groups crypto into top tokens / mid caps.
- **Sentiment**: ingest_sentiment.py accepts any universe symbol (and CoinGecko ids / common names mapped to it).
- **Tests**: `python3 test/test_universe.py` (filter rules, meme allowlist, sentiment mapping); test/sim.py lists a coin
  mid-run and asserts it warms up (no DAY entry, burst setup skipped with "warming up") for 4h.

## v7.1 (2026-10-10): dashboard figures reconcile + one sub-strategy money definition
- **Sub-strategy ledger** (`daypot.sub_ledger`, used by BOTH the trader for sizing and dashboard.py for display): per account,
  a sub-strategy's **cash** = its share (50%) of the Day start money + its realised P&L (closed trades, after all fees) - the
  cost (incl. buy fees + gas) of its open positions; **budget** = its own equity = cash + its open positions at market;
  **free** = its cash (capped by the account's Day cash). Momentum + Burst cash = the Day cash of that account and Momentum +
  Burst budget = its Day value, to the penny. Before v7.1 the budget was 50% of the *shared* Day equity and "free" = budget -
  cost, so one sub-strategy's fees/losses shrank the other's budget (Burst showed £14.96 of £15 after Momentum's buy fees)
  and the free amounts did not add up to the cash (£44.96 vs £45.01).
- **dashboard.py** builds every total from its rounded parts from ONE price snapshot: cell (pot x account) value = cash +
  invested (sum of the shown position values), position P&L = shown value - shown cost; pot = its two cells; account = its
  three pot cells; total = the pots; today's P&L = value now - value at midnight UK. `sleeves.day.ledger` = per account and
  `all` (summed) x sub-strategy: start, budget, cash, free, value, cost, open_pnl, realized, pnl, open_n. Sub stats add
  `fees_closed` (already inside the closed P&L) and `fees_open` (buy fees of positions still open, inside the open P&L).
- **Day tab**: cards say what they are made of (value = cash + positions; cash = momentum free + burst free), tooltips (ⓘ)
  explain each figure, "Free cash per account and sub-strategy" bars come from the ledger, the sub-strategy table shows
  Budget (both accounts summed, or the selected account) = free + positions, Closed trades, Closed P&L, Open P&L and Fees =
  closed + on open buys (so "fees £0.05 with 0 trades" now reads "£0.00 closed · £0.05 on open buys"). Overview / Portfolio /
  account / Core / Risk cards use the same value = cash + invested definitions and have tooltips; the Core tab's
  "invested" is the pot's invested figure (was the sum of target rows only). daily_summary labels closed vs open-buy fees.
- **Asset panel clicks (item 2)**: the Day tab's Burst setups table passed `{asset: undefined}` to openAsset (->
  `#asset=[object Object]`, panel did not open). openAsset now accepts a symbol or a row object and ignores anything else,
  and every asset table opens the panel on a row click: Overview latest orders, Portfolio, Core targets/picks/ranking, Day
  positions/setups/orders, Risk positions/candidates/screen/orders, Markets, Orders (not cash transfers), Sentiment.
  check.js clicks a (priced) row in each of these and checks hash + open panel.
- **Checks**: `python3 test/check_data.py [docs/data.json | URL]` asserts ~190 reconciliations (positions, pot x account
  cells, pots, accounts, totals, Day ledger vs pot cards, sub stats); ci_cycle.sh runs it every cycle (warning in the log on a
  mismatch). `/workspace/tools/check.js` (box) runs it on the exact data.json the page loaded, checks the Day tab in the DOM
  (cash card = sum of free bars, sub budgets = pot value) and fails on JS errors (exit 2). test/sim.py runs check_data on a
  dashboard export every 3 simulated hours and checks that the trader's ledger (state day_subs) equals the Day tab's.

## Great reset (2026-10-10, v7 rules): fresh £200
The first run (2026-10-09 20:22 UTC -> 2026-10-10 ~14:50 UK, v3-v7 with migrations; final numbers in the archive summary) was archived
and the account restarted from scratch with all current rules (`great_reset.py 2026-10-10`, run in the box clone under the lock):
- **Archive** (kept in the repo): `data/archive/trading-pre-reset-2026-10-10.db` (full old DB, VACUUMed),
  `data/archive/dashboard-pre-reset-2026-10-10.json` (old docs/data.json) and `data/archive/trading-pre-reset-2026-10-10.md`
  (final equity, trades, P&L per pot / account / pot x account, positions discarded, old scorecard).
- **Cleared**: trades, positions, cash/acct_cash, equity history, transfers, signals, order queue, CORE plan/picks, DAY
  day-start/halt/exit cooldowns + burst/momentum status (per-sub stats come from trades, so they restart at 0), RISK
  peaks/halt/base/exits, the pending v6 £10 settlement. **Kept**: prices, fx, sentiment, daily_closes, universe/collector state.
- **Fresh balances**: Crypto £100 (CORE 50 / DAY 30 = momentum 15 + burst 15 budget / RISK 20), Stocks £100 (CORE 50 / DAY 30 /
  RISK 20). `account_split` is rewritten with opening = funding and no pending legs; an opening equity row (£200 + BTC price)
  restarts the BTC benchmark and the chart at the reset time. CORE stocks were re-picked at the reset (init_core: tranche
  building, ~1 tranche/asset/UK day; stock tranches wait for the market open); the Monday 09:00 UK rebalance re-picks weekly.
- **One-off scripts can no longer touch the fresh state**: ci_cycle.sh does not run migrate_v5_xstocks.py /
  migrate_v6_accounts.py any more and both skip when state `great_reset` exists; reset_sleeves.py is disabled (superseded).
  scripts/ci_push.sh compares the DB reset marker (state `account_reset`) with main's before re-applying a cycle after a
  push race and drops the cycle if they differ, so a concurrent runner can never resurrect the pre-reset run.

## v7 (2026-10-10): DAY pot = Momentum + Burst sub-strategies (paper only)
The Day pot (£30 crypto + £30 stocks) is shared by two sub-strategies; each has a BUDGET of `share_pct` (50/50) of the
DAY money (cash + positions) in each account and can only use what its own open positions leave free (never more than the
account's real cash). Every DAY order is tagged with its sub-strategy (`trades.sub` = momentum | burst; pre-v7 day trades
count as momentum) and carries `trades.conf` (burst confidence 0-1 / momentum signal score). Config: `day.momentum`,
`day.burst`; code: `daypot.py` (pure helpers) + the DAY sections of trader.py.
- **Momentum** (original score entries, strategy.buy_threshold): position = 50% of the momentum budget of that account
  (min £5), max 3 open. Exits upgraded: target = max(4%, 2.5 x 1h ATR) capped 12%, stop = max(2%, 1.2 x ATR) capped 5%;
  a trailing stop arms at +3% (net of the sell cost) and gives back 40% of the peak gain (peak +8% -> exit at +4.8%); at the
  target a position whose signal is still a buy becomes a 'runner' (trail tightened to 20% of the gain) instead of being
  sold, so winners can run past the old fixed +5%. Signal exit (score <= sell_threshold) as before.
- **Burst** (quick breakout trades): price breaks the prior 24h high (crypto: also CoinGecko's high_24h) or the high of a
  tight (<= 4%) 4h range, by >= 0.1% and <= 3% (no chasing); hard filters: last-hour volume >= 2x the average hour
  (crypto: CoinGecko rolling 24h total_volume deltas; stocks: Yahoo 15-min bars, state `stock_vol`; with no volume data
  only a 24h-high break may trade), hourly RSI < 80, fresh sentiment >= -0.1 (none = neutral), broad market not falling
  (crypto: BTC 1h >= -0.5% and 4h >= -1.5%; stocks: index proxy of the listing exchange ^GSPC / ^FTSE / ^AXJO, state
  `market_proxy`, fetched by the collector only while that exchange is open and never written to prices; fallback
  EQQQ.L/VUSA.L). Target = clamp(3 x ATR, 3%, 8%), stop = clamp(1 x ATR, 1.5%, 2.5%), trailing after +2% (40% give-back),
  max hold 6h, 1h cooldown per asset after any DAY exit, max 3 open bursts per account, NO trades-per-day cap.
  - **Confidence** 0-1 per setup = weighted breakout strength 0.25, volume spike 0.25, sentiment 0.15, market regime 0.15,
    the setup type's measured hit rate 0.20 (24h-high and 4h-range breaks tracked separately over 14 days, Beta prior
    0.5 x 20 pseudo-trades). It is a ranking heuristic, NOT a probability of profit and never a guarantee.
  - **Expected value after costs** = p x target - (1-p) x stop - round-trip costs (p = setup hit rate), blended with the
    setup's measured expectancy as trades accumulate (weight n vs 10). Taken only if confidence >= threshold AND EV > 0.
  - **Capital-based caution** (no loss-streak cooldown, no separate loss budget): threshold = 0.55 + 0.30 x (DAY pot loss
    today / start-of-day equity) / 15% -> rises gradually over the whole daily-stop range (0.85 at the stop); gains never
    lower it. Size = confidence x 33% of the burst cash still free (shrinks as losses deplete it); raised to the £5 min trade
    when that much is free - with ~£15 per burst budget the min trade dominates, so in practice most bursts are £5.
- **Both**: only trades whose target (%) >= 4 x round-trip costs (`day.min_move_cost_x`; crypto 0.6% -> 2.4%, US stocks
  0.5% -> 2%, London non-AIM 0.7% -> 2.8%; gas both sides included). Entries 08:00-21:00 UK (stocks not in the last 30 min),
  everything flat by 22:00 UK (stocks 15 min before their close). **DAY daily loss stop raised from 5% to 15%** of the
  pot's start-of-day equity (`day.daily_loss_cap_pct`, user request 2026-10-10): at the stop all new DAY entries halt for
  the rest of the UK day; open positions keep their own stops. CORE and RISK unchanged. KILL halts everything.
- **Cycle granularity**: cycles run every 5 min on the VPS (full collector screen every ~15 min; before 2026-10-11 every 15 min
  on GitHub Actions, often late). Breakouts, stops, targets and trailing stops are only
  checked at those snapshots, so fills can be well past a level (a -1.5% stop can fill at -3% after a fast drop) and short
  spikes between cycles are missed. ATR is built from ~4 snapshots per hour, so it under-states the true hourly range.
- **Dashboard (Day tab)**: gauges for today's loss vs the 15% stop, the current burst confidence threshold (base marked)
  and remaining cash per account x sub-strategy; a sub-strategy table (budget/free, trades, win rate, avg win %, avg loss %,
  profit factor, P&L after fees, fees, expectancy per trade; All time / Today; account switch); open positions with strategy,
  ATR levels, trailing level, sell-by time; Burst setups with a confidence bar (threshold line), component mini-bars,
  volume, RSI, target/stop, EV after costs and the decision (taken / skipped + reason); today's orders with strategy and
  confidence. data.json: sleeves.day.subs (stats), sleeves.day.status (state day_subs), positions[].sub/tp_pct/sl_pct/trail.
- **Realistic expectations**: positions are £5-£15, so a +5% winner is £0.25-£0.75 and a 2% stop ~£0.10-£0.30 plus ~£0.03-
  £0.09 fees. Crypto round trips cost 0.6% - a 3% burst target nets ~2.4%. Breakout setups typically win 35-50% of the time;
  with 2:1 targets that is roughly break-even to modestly positive before slippage at 15-min granularity. Expect single-digit
  £ per month either way on £60; the value is in measuring (per-sub stats) which setups actually have an edge.
- test/sim.py v7 scenario: see Layout. Last run: logs/sim_v7.txt.

## v6 (2026-10-10): two accounts - Crypto (Monad wallet) + Stocks (Trading 212 Invest)
The £200 is modelled as two real-world accounts, 50/50: **Crypto account £100** (Monad wallet: all crypto, fees = venue
fee + gas) and **Stocks account £100** (Trading 212 Invest: all stocks/ETFs, fees = spread + FX + UK stamp duty). Each
pot is split across both (config.yaml `accounts.funding`): CORE crypto £50 / stocks £50, DAY £30 / £30, RISK £20 / £20.
- **Cash per pot x account** (table `acct_cash`; table `cash` mirrors the pot totals). A crypto trade only uses/returns the
  pot's crypto cash, a stock trade only its stocks cash; `trader.buy()` refuses to overdraw an account cell. The trader
  NEVER moves money between accounts.
- **CORE targets** are weights inside each account's slice of the Core pot: crypto `core.targets_pct` (MON 26 / XRP 26 /
  WLD 20 / BTC 16 / ETH 12 = 100% of Core crypto money), stock picks `core.stocks_pct` = 100% of Core stocks money
  (`max_pick_pct` 20% of it = the old 10% of the pot). Number of picks follows the Core stocks money.
- **Migration** `migrate_v6_accounts.py` (one-off, idempotent via state `account_split`; also run by ci_cycle.sh; backs up
  to data/trading.db.bak-pre-v6): no reset - holdings stay in the account of their asset class, each pot's cash is split
  with TRANSFER rows (kind `split`, no P&L) so account totals are as close as possible to 100/100. If a pot held more of
  one asset class than its target (DAY had £40 of crypto), the remainder is a **pending settlement** between pots INSIDE
  one account (crypto DAY->CORE, stocks CORE->DAY, equal amounts so pot totals do not change); the trader executes it
  automatically, all legs at once, as soon as the cash is free (DAY is flat after 22:00 UK). Earlier equity rows get
  pro-forma per-account values (opening + realised + unrealised P&L at that time).
- **Baselines**: account/pot x account starts = opening at the split + later pot/account transfers; history is rebased
  (`transfer_adj`, `transfer_adj_acct`) so transfers never show as P&L. Table `equity` has crypto/stocks/crypto_cash/
  stocks_cash and `accts` (JSON {pot: {account: [equity, cash]}}); `transfers` has from_account/to_account/kind
  (split | settle | pot | account).
- **Manual scripts** (box clone, under the repo lock, then commit + push data/trading.db):
  `python3 transfer_accounts.py FROM_ACCOUNT TO_ACCOUNT AMOUNT|cash POT [TO_POT] [-- reason]` - between accounts (only after
  you really moved money); `python3 rebalance_pots.py FROM TO AMOUNT|cash|topup ACCOUNT [reason]` - between pots, same account.
- **Dashboard**: combined total + two account cards (value, P&L, cash, invested) on Overview, equity chart toggle Total /
  Crypto / Stocks, account switch All / Crypto / Stocks (Portfolio, Core/Day/Risk tabs, Orders, Markets), Core tab with
  crypto targets and stock picks in separate sections with each account's cash, investment plans (+£100/£500/£1000)
  split 50/50 into the two accounts. Fees per account: crypto fee + gas, stocks spread + FX + stamp duty.
- **daily_summary.py**: CRYPTO / STOCKS lines (value, P&L, cash, invested, fees, by pot) and per-pot account split.
- **test/sim.py**: per pot x account cash reconciliation (cash never crosses accounts), no negative account cell,
  pot move + manual account transfer (rebased), and the live-DB split + settlement on a copy.

## v5 (2026-10-10): UK-accessible stocks - normal exchange tickers, real market hours
xStocks (tokenised stocks) are NOT available to UK residents (Kraken: "xStocks are not available in: United States,
Australia, Canada, UK"), so the stock side is now broker-agnostic: plain exchange tickers as Yahoo Finance symbols -
US (NVDA), London main market + AIM (RPI.L, ITM.L), ASX (LYC.AX) - the same instruments a UK broker such as Trading 212
(public API: Invest + Stocks ISA, fractional, demo environment) or IBKR sells. Crypto is unchanged.
- Prices: collector.py fetches Yahoo quotes/daily closes and converts by the currency Yahoo reports (GBp = pence /100 ->
  GBP -> USD with the stored USD->GBP rate, so the trader's £ price equals the London quote exactly; AUD etc. via Yahoo FX).
  The prices/daily_closes tables stay USD. Ticker meta (currency, exchange, name) in state 'stock_meta'.
- Rate limits: daily closes once a day per ticker (30 stale tickers per cycle); intraday quotes only for the watchlist,
  Core picks, held stocks, the top-8 ranked names and Risk-pot names with a daily breakout (+ top 8 by breakout score),
  and only while their exchange is open (+ one fetch after the close for the closing price). ~0 stock calls at weekends.
- Market hours (markets.py, computed calendars incl. Easter/bank holidays/early closes, zoneinfo DST): US 09:30-16:00
  New York (= 14:30-21:00 UK, 13:30-20:00 UK in the 2-3 weeks a year when US/UK DST differ), LSE/AIM 08:00-16:30 UK,
  ASX 10:00-16:00 Sydney. No stock trades on weekends/holidays. CORE tranches/stops wait for the open (a tranche also
  fires in the last hour before the close if no dip came); CORE rebalance orders decided while closed (Monday 09:00 UK
  is before the US open) are queued in state 'order_queue' and filled at the first open cycle (shown on the Core pot tab);
  DAY: no stock entries in the last 30 min, stock positions flattened 15 min before the close; RISK: stock entries/exits
  only during the session.
- Costs (config stocks.costs, Trading 212-like): 0.1% spread per side, +0.15% FX fee per side on non-GBP shares, +0.5% UK
  stamp duty on London buys except AIM/ETFs (stamp_exempt); no gas on stock trades. Crypto: 0.3% + simulated Monad gas.
- Universe (config stocks.universe, 156 tickers, verified with `python3 universe_check.py`: >= 100 days of Yahoo history
  and 20-day liquidity >= $1M US / GBP 0.2M UK): the previous 9 themes (as normal tickers, + VST, CCJ, SPOT, RGTI which
  were not xStocks) plus 'watch closely' themes (watch: true): Photonics, Quantum, Robotics & automation, Real assets
  (gold/silver miners + royalty cos), Rare earths, Land & farmland REITs, British early-stage (LSE/AIM), Global early-stage
  small caps; crypto watch coins PAXG, XAUT, ONDO (crypto.watch_ids, theme 'Real-world assets (tokenised)'). Watchlist
  ETFs are UCITS (VUSA.L, EQQQ.L, ISF.L) because UK retail cannot buy US-domiciled SPY/QQQ.
- CORE picks from the whole universe (max 2 per theme, weekly). RISK stock universe = riskpot.stock_themes (quantum,
  uk_early, rare_earths, global_small) + riskpot.stocks (photonics/robotics small caps).
- Migration (migrate_v5_xstocks.py, idempotent, also run by ci_cycle.sh; DB backup data/trading.db.bak-pre-v5): CORE
  xStock positions converted 1:1 to the ordinary share (same qty + cost basis, no fee, no P&L; 'CONVERT' rows in Orders);
  the one DAY xStock position (CRCLx) was closed at its last price (weekend, Day pot is flat overnight); price/daily/
  signal/sentiment history renamed to the plain tickers; historical orders keep their xStock names (alias on the site).
- Dashboard: Markets grouped by theme with a theme filter (★ watch closely first), market open/closed per exchange,
  names without a live quote show the last daily close; Core pot shows theme + ★ per pick and queued stock orders.
- test/sim.py v5 checks: no stock trade outside its session, queued rebalance sell filled at the US open, DAY stock flatten
  before the LSE close, DST gap, holidays, early close, 664.5p -> £6.645, stock costs. Last run: logs/sim_v5.txt.

## Operations (since 2026-10-11): cycle runs on a Hetzner VPS
The cycle runs 24/7 on a Hetzner VPS (Ubuntu 24.04, Nuremberg, user `bot`): systemd `trading-cycle.timer`
(`OnCalendar *:02/5`, Persistent, 14 min timeout) -> `/home/bot/run_cycle.sh` (flock; hard-sync to origin/main;
`scripts/ci_cycle.sh`; `scripts/ci_push.sh` as `vps-cycle`; log `/home/bot/logs/cycle.log`, logrotate daily x14).
Cadence: every 5 min the fast cycle (collector `COLLECT_MODE=fast`: crypto re-priced from Kraken / Coinbase / KyberSwap,
CoinGecko only for coins those do not list, then stocks, gas, market proxy, sentiment ingest, trader incl. exits,
maintenance, dashboard, push); every ~15 min the full collector (daily closes, backfills, and the top-300 screen when it
is due). FX stays hourly. Pushes only when state changed (at most one per 5-min cycle). Health: `/home/bot/health.sh`.
Exit and entry watcher (one process, separate from the timer): `exit_watch.py` runs as systemd
`trading-exit-watch` (`deploy/trading-exit-watch.service`, `Restart=on-failure`). It watches open
Day positions, then Risk, on Kraken and Coinbase public websockets, and the same streams cover
assets with an armed entry. A coin those do not list, or a socket that goes quiet, is polled
about every 20 seconds (CoinGecko only inside the existing daily budget). Exits use the same
stops, trails, targets, max-hold and costs as the cycle. The cycle still exits if the watcher
or a stream is down. Both take the same book lock so a position is sold once. `KILL` in the
clone stops the watcher as well as the cycle.

Entries stay scored on the 5-minute cycle. For a Burst, momentum, or Risk breakout/dip the cycle
would buy, it records an armed entry (price level, optional volume multiple, size, stop, target,
expiry about 15 minutes). The watcher fires when that level trades. On the main book this is a
shadow log only, so the frozen book does not change. Live buys go to the `entry_watch` variant
(`config/variants/entry_watch.yaml`, Variants tab and Book menu) and only when
`ENTRY_WATCH_SHADOW=0`. That switch is independent of `EXIT_WATCH_SHADOW`.
Every other paper book under `data/variants/` is shadow-logged as well
(WOULD EXIT and WOULD BUY), using that book's own rules. Those books are not
sold or bought by the watcher. Each shadow row is tagged with the book name,
and `exit_shadow_report.py` prints, per book, the pairs, the average time gap,
the average price gap, and the moves the cycle missed. One price fetch per
symbol is shared across the books. A buy is idempotent
with the cycle: the position is visible on the next cycle, and a second tick does not buy again.
The watcher re-checks session, flatten time, loss caps, cooldown, open slots, and cash before it buys.

First deploy is shadow mode. `EXIT_WATCH_SHADOW` and `ENTRY_WATCH_SHADOW` both default to on
(the unit sets both to `1`). The process appends `WOULD EXIT` and `WOULD BUY` lines (trigger,
price, time) to its log and to `~/exit_shadow/exit_shadow.db` (`EXIT_SHADOW_DB` overrides the
path). That directory is outside the clone, so `git reset --hard` in `run_cycle.sh` does not
delete it. A copy left under `data/exit_shadow.db` (plus `-wal`, `-shm`, `-journal`) is
gitignored, so `git add -A` in `scripts/ci_push.sh` will not commit it, and an ignored
untracked file also survives `git reset --hard`. Shadow mode does not write trades, cash, or
positions, and it does not move the cycle's peaks. Install only after this is on main (user
`bot`; point the unit at the clone `run_cycle.sh` uses):

```
sudo cp deploy/trading-exit-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-exit-watch
journalctl -u trading-exit-watch -f
```

Review, from that clone:

```
python3 exit_shadow_report.py
```

Each cycle also runs `exit_shadow_report.py --write` at most once an hour and commits
`data/exit_shadow_report.txt` plus `data/exit_shadow_report.json` with the usual state push.
If `~/exit_shadow/exit_shadow.db` is missing, those files say `no data` and the cycle still
pushes. The shadow database itself stays gitignored. A history squash keeps the report,
because it is part of the committed tree under `data/`.

The report pairs each shadow exit with the sell the 5-minute cycle actually made for that same
position, and each shadow entry with the cycle's buy (or marks a move the cycle missed). It
prints the time difference and the price difference. When the exits look right, add
`EXIT_WATCH_SHADOW=0` to `/home/bot/.trading_env`. When the entries look right, add
`ENTRY_WATCH_SHADOW=0` as well. Then `sudo systemctl restart trading-exit-watch`. The unit
sources that file (`set -a; . ~/.trading_env`) instead of `EnvironmentFile`, which rejects
lines that start with `export`. The unit's own defaults are `EXIT_WATCH_SHADOW=1` and
`ENTRY_WATCH_SHADOW=1`; a value in the file replaces it. The log line changes from
`WOULD EXIT` to `EXIT` for sells. Entry buys are written only on the entry_watch variant;
the main book keeps the `WOULD BUY` line.
CoinGecko Demo key (optional): the demo tier is 100 calls/min but only 10,000 calls/month. Prices therefore come from
public Kraken (one Ticker call for every listed coin) and Coinbase, with KyberSwap for Monad tokens. CoinGecko is the
fallback (one simple/price about every 15 min for whatever is left) and the top-300 screen (about every 3 hours, every
6 hours once 70% of the day's budget is used). Target is 300 calls per UK day, about 9,000 a month. Calls are counted
in table `cg_calls`; the Markets tab shows today and this month. On the VPS put `COINGECKO_API_KEY=...` in
`$HOME/.trading_env` (mode 600, not in git; `TRADING_ENV` overrides the path). `scripts/ci_cycle.sh` sources that file
when it is present. When the variable is set, every CoinGecko request (collector and the live quote) sends header
`x-cg-demo-api-key`. The base URL stays `https://api.coingecko.com/api/v3`. When it is unset, those requests are
unchanged. The key is never written to the log. No key is required for Kraken or Coinbase public endpoints.
The GitHub Actions workflow (.github/workflows/cycle.yml) is manual-only (workflow_dispatch) - no two scheduled writers.
Box fallback: `/workspace/tools/vps_watchdog.sh` (box cron */10) runs `FORCE_BOX=1 box_cycle.sh` if the newest
`cycle` commit on main is older than 20 min.

## Architecture (v4, 2026-10-10): runs 24/7 on GitHub Actions
The box sleeps when the AI is not active, so the */15 cron missed hours. The engine now runs on GitHub Actions in the
public repo **oscarwy08/trading-dashboard** (public => free Actions minutes). Single source of truth = branch `main`:
```
main
├── .github/workflows/cycle.yml   schedule */15 + workflow_dispatch; concurrency group 'trading-cycle' (no overlaps);
│                                 permissions contents: write (+ pages: write); setup-python with pip cache
├── scripts/ci_cycle.sh           (no migrations since the great reset) collector -> ingest_sentiment -> trader -> maintenance -> dashboard -> daily_summary
├── scripts/ci_push.sh            commit data/ + docs/ and push with GITHUB_TOKEN (rebase/re-apply on races; squash)
├── *.py, config.yaml, requirements.txt, web/ (SPA templates), test/sim.py
├── data/trading.db               STATE (SQLite) - committed every cycle
├── data/sentiment/*.json         new sentiment files (pushed from the box) -> processed/ | rejected/ after ingest
├── data/daily_summary.txt        latest scorecard (every cycle)
├── data/exit_shadow_report.txt   watcher shadow report (hourly; .json beside it)
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
(2) scripts/ci_push.sh squashes main into a single orphan commit once it has > 100 commits
(~1 day), so old binary DB versions become unreachable. The squash is skipped while any pull request
is open against main (GitHub API, using `GH_TOKEN` or `GITHUB_TOKEN`). If that check cannot be made,
the cycle fast-forwards instead of force-pushing. `SQUASH_EVEN_WITH_PRS=1` squashes anyway. The squash
force-push is allowed only when this cycle's parent is still the main tip just fetched. If another
commit landed, the cycle is rebased onto that tip and pushed as a fast-forward, or skipped until the
next cycle if main moves again. It never force-pushes a tree that is not based on that tip. Never rely
on git history of main for audit - the DB itself holds the full trade/equity history.

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
                      (+ Monad-ecosystem tokens with a KyberSwap liquidity check), KyberSwap MON/USDC, stocks (v5: Yahoo, normal
                      tickers, currency-converted, market-hours aware, fetched in parallel), USD->GBP FX (hourly), Monad gas, daily closes of the stock universe (Yahoo) + ranking
riskpot.py            RISK pot screen/scores (crypto breakout from the markets snapshot; stock breakout from daily closes)
stockrank.py          CORE stock scoring/selection + display-only investment plans (pure DB, no network)
trader.py             three sleeves: CORE long holds / DAY intraday / RISK speculative: signals + paper trades + risk rules
daypot.py             v7 DAY helpers: hourly bars/ATR, breakout, volume ratios, market regime, confidence, EV, sizing, per-sub stats
maintenance.py        daily DB pruning + VACUUM (keeps the committed DB small)
rebalance_pots.py     move UNINVESTED cash between pots inside ONE account (TRANSFER, no reset; backs up the DB)
transfer_accounts.py  MANUAL move between the Crypto and Stocks accounts (TRANSFER, rebased; backs up the DB)
migrate_v6_accounts.py one-off split of the single cash into Crypto + Stocks accounts (idempotent)
replan_core.py        one-off: force the CORE rebalance/re-pick at the next trader cycle (backs up the DB; no reset)
universe_check.py     re-verify the stock universe: Yahoo history (>= 100 days) + liquidity (v5)
markets.py            exchange sessions/holidays (US, LSE/AIM, ASX) used by trader/collector/dashboard (v5)
migrate_v5_xstocks.py one-off xStocks -> normal tickers migration (idempotent; run by ci_cycle.sh)
ingest_sentiment.py   loads data/sentiment/*.json into SQLite
reset_sleeves.py      DISABLED (superseded by great_reset.py)
great_reset.py        one-off great reset 2026-10-10: archive to data/archive/ + fresh £200 v7 start (refuses to run twice)
test/test_loans.py    v7.3 inter-pot loans (borrow / repay / reserve / cap / burst)
test/test_universe.py v7.2 DAY crypto universe filter + sentiment symbol mapping
test/check_data.py    v7.1 reconciliation of docs/data.json (every total = sum of its parts; run by ci_cycle.sh + check.js)
test/sim.py           backtest-style dry run on a throwaway DB copy (simulated clock + synthetic prices); deletes the copy.
                      v4 adds RISK: breakout entries via the real screen, 12% stop, trailing take-profit, 7-day max hold,
                      max 5 open / <= £8 (v7.4: no daily cap; confidence / EV / drawdown threshold checked), drawdown-cap halt + RISK_RESUME, DAY ignores risk-only coins,
                      cash reconciliation for all three pots, TRANSFER rebasing. Last run: logs/sim_v4.txt (ALL CHECKS PASSED)
                      v7 adds a scripted DAY day: burst take-profit / trailing / ATR stop / 6h max hold, skips (no volume, RSI >= 80,
                      negative sentiment, BTC falling), 1h cooldown, momentum winner via trailing/target, stock burst flattened
                      before the close, max open per account, sub tags + stats, threshold over the 15% stop, sizing, EV, 4x-cost filter
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
stocks 40% split equally over the picks. Stock universe (v3 text - superseded by the v5 section above): config `stocks.universe` - 84 growth names that exist as xStocks
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
Buy when combined >= 0.35 (0.30 for MON/WLD/XRP); exit on combined <= -0.20. v7: this is now the 'momentum'
sub-strategy with ATR-scaled target/stop + trailing stop (was fixed stop-loss 3% / take-profit 5%, 25% of DAY equity per
position) and a second 'burst' sub-strategy shares the pot - see the v7 section at the top. Max 3 open momentum positions. New entries only 08:00-21:00 UK; ALL day positions are
force-closed at/after 22:00 UK (the */15 cron cycle at 22:00), and any day position found before 08:00 or carried from a
previous UK day is closed too (flat overnight). Daily loss cap: DAY equity <= -15% (v7; was 5%) vs its UK-day start halts new DAY
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
the 50d average. v7.4: the decision is Day-style (see the v7.4 section): confidence >= threshold AND expected value after
costs > 0, with the intraday momentum model (same as DAY, >= 2h history) >= 0 as a hard gate; no entry while fresh
sentiment <= -0.3 (conf >= 0.5). v7.7 adds a second setup, a dip / pullback buy (see the v7.7 section): same threshold,
expected value, size and exits, its own confidence. One position per coin, max 5 open. Size = confidence x 50% of the account's remaining RISK cash (min £5, max £8 / 20% of
the pot); no daily cap, 24h cooldown per asset after an exit.
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
  "asset": "XRP",                         // required; ticker as used here: BTC, ETH, XRP, WLD, MON, SOL, ... or stocks NVDA, RPI.L etc. (v5)
  "score": 0.35,                          // required, -1 (very bearish) .. 1 (very bullish)
  "volume": 1240,                         // int, number of posts/articles considered (default 0)
  "confidence": 0.7,                      // required, 0..1 (scales sentiment weight)
  "summary": "Short human-readable rationale",
  "sources": ["https://..."]              // list of URLs
}
```
In the repo, valid files move to `data/sentiment/processed/`, invalid ones to `rejected/` (reason in the Actions run log).
(timestamp, asset) is unique; re-sending replaces. Tip: write to a temp name then rename to *.json.
