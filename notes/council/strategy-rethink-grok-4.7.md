# Strategy rethink — 18 Oct 2026

Scorecard 18 Oct 22:35 UK. Main equity £183.61 from £200 (−8.2%, max drawdown −9.3%, 93 closes, win 34%). Variant line since 11 Oct 18:07: main P&L −£18.76. BTC spot £82,891 to £80,885 (−2.4%). The book trails a BTC hold by 2.8 points. Best mature book is sentiment-split at £188.03. `best_of` is £189.19 with a 12% win rate on a young book. Every book is behind BTC. Pass bar for anything new: beat BTC buy-and-hold after costs.

## Why this approach loses

- The book is long alts and single stocks while BTC is the benchmark. In a −2.4% BTC tape, Core is −10.5% (£89.47 vs £100). MON alone was sold at −20.3%. Higher beta with no hedge loses to the thing it is judged against.
- Payoffs need a win rate the signals do not have. Day momentum: 44 closes, win 30%, average win +4.2%, average loss −3.0%, profit factor 0.58. That payoff breaks even near a 42% win rate. Burst: 38 closes, win 40%, +1.9% vs −2.1%, profit factor 0.58, break-even near 53%.
- Costs eat the small tickets. Momentum fees £1.34 on a −£2.52 result. Burst fees £0.86 on −£1.03. Crypto fees £2.73. About 91 buys since 11 Oct. Median hold 1.5 hours. A round trip on a £5–8 name is a large share of a 2% move.
- Stops realise the left tail. Momentum exits: 16 stops, 11 trails, 10 signal sells, 7 flattens. Burst exits: 21 stops, 10 trails, 2 targets. The earlier shadow set showed trails as the only profitable exit family and stops as the loss.
- The clock throws away the horizon where momentum exists. Flat at 22:00 UK, and a signal flip, close trades the same day. Published time-series momentum is a weekly or monthly effect. A 90-minute hold pays the fee and keeps the noise.
- Core is a static multi-name book, not a BTC substitute. It is fully invested in themes, rebalanced slowly, and has no volatility cap. One name moved the pot more than a week of Day trades.
- Sentiment does not fix the sign of the edge. It is a small weight, missing on most buys, and on the live fills a high mention count was not the losing bucket. Tuning it still leaves a long breakout book.
- Variant flags stay inside the same entries: breakout, dip, momentum, a tighter trail, a longer clock. Midnight crypto, Day EV blend, and partial-take are the worst equities (£182.50, £181.55, £181.98). Stacking leftovers with the runner (`everything`, £183.81) is worse than the runner alone.
- Position size is a flat cash slice. Nothing scales down when realised vol rises, so the book stays fully exposed on the days alts gap through stops (18 gap fills in the prior close set).
- The engine can only be long. A SELL closes a position. There is no borrow, funding debit, or negative quantity, so a short rule cannot be paper-tested on the current books.

## Different books

Paper-test each on a fresh book from the same prices. Benchmark is BTC buy-and-hold of the same cash, same fee (0.3% a side on crypto, stock costs as now). Kill a book if it is behind BTC after costs once it has 30 round trips, or after 100 calendar days, whichever comes first. Drawdown may not exceed BTC’s drawdown by more than 5 points.

**1. BTC core, one satellite.** Eighty-five percent of the crypto sleeve sits in BTC. Rebalance only when BTC drifts by more than 5 points. The other 15% may hold one coin from the priced universe if its 20-day return beats BTC’s and its 20-day vol is outside the top quarter. Minimum hold 5 days. No intraday entry, no Day flatten. This should beat BTC because almost all the capital is the benchmark, and the satellite is added only when relative strength is already positive. Fees hit the 15%, not the whole sleeve.

**2. Weekly time-series momentum on three majors.** Universe: BTC, ETH, SOL. Each Friday, hold a name if its 12-week return is positive, otherwise hold cash for that slot. Size each holding to 20% annualised vol using 20-day realised vol, and cap at 1×. No alts, no stops inside the week. Moskowitz, Ooi, and Pedersen (Journal of Financial Economics, 2012) and the crypto factor work (Liu, Tsyvinski, Wu) find this horizon, not a 90-minute breakout. Cash in a downtrend is how it beats a forced long BTC hold.

**3. Relative-strength rotation, BTC filter.** Once a week, rank the liquid universe by 4-week return minus BTC’s 4-week return. Hold the top three, equal weight, and drop a name that leaves the top ten. If BTC’s own 4-week return is negative, hold BTC instead of the top three. This stops buying alts in a falling market, which is the Core failure mode, and keeps the cross-section only when the benchmark trend agrees.

**4. Volatility-targeted BTC.** Hold only BTC. Each day, weight = min(1, 15% / 20-day realised vol). The rest is cash. No other asset. Moreira and Muir (Journal of Finance, 2017) show vol-managed weights cut exposure when vol clusters. It beats raw BTC mainly through drawdowns. In a smooth rally it can lag. That is an acceptable paper result only if the full path, after the single rebalance cost, still clears BTC. If it does not after 100 days, drop it.

**5. Quiet 30-day basket.** Once a month, among coins above $1B market cap, pick three with the lowest 7-day mention count and a positive 90-day return versus BTC. Hold 30 days. A buzz change of 3 or more, or a 24-hour jump of 15% with sentiment at or above 0.30, blocks a new buy for 7 days. This is the attention result at the right horizon (Da, Engelberg, Gao, Journal of Finance, 2011; Barber, Huang, Odean, Schwarz, Journal of Finance, 2022): the reversal is weeks to months, not the next candle. The live 1-day sample did not show quiet names winning. Treat this as a test, and shut it if the basket lags BTC for 90 days.

## Shorts

UK retail on Kraken cannot trade derivatives. Futures need elective-professional status (FCA criteria, including a history of leveraged trades). Spot margin in fiat or a fiat stablecoin needs a high-net-worth declaration (£150k income or £500k net assets, with an accountant’s letter) or a business-use declaration. Kraken’s own table says crypto-versus-crypto margin, and a short of crypto against fiat, sits outside that fiat-margin restriction. Confirm on the account before counting on it. Opening fee is about 0.01–0.02% on BTC and 0.02–0.04% on alts, and the same rate rolls every 4 hours (Kraken fee schedule). A day-long alt short near 0.03% costs about 0.2% in rollover before the two trade fees. A week is about 1.3%. A 2% dip does not pay for that.

Trading 212 Invest, which this bot models, cannot short. A CFD account is a separate product, still FCA-regulated, and it is not part of the Invest plan. Retail equity CFDs cap leverage at 5:1. A short is credited overnight interest and pays 100% of the dividend. Spreads still apply. Do not assume the current plan can short a stock.

Three short tests, only if the account can actually borrow:

- Regime: if BTC’s 4-week return is negative, short a small high-beta basket, else hold cash. Prefer cash when the expected move is smaller than the rollover for the planned hold.
- Hype fade: buzz change at least 3 and a 24-hour jump of at least 15%, short for 3 days, stop +8%, cover at −12%. The live long sample did not show these names falling next. Paper it. Do not trade it live on that evidence.
- Pair versus BTC: long BTC, short the alt in equal notional when the alt has beaten BTC by 20% over 14 days, and cover when the ratio returns to its 20-day average. Funding sits only on the short leg. This is the short that can beat BTC without a market call, and only if the spread exceeds the rollover.

The current trader cannot express any of these. A paper short needs a negative quantity, a cover trade, and a daily funding debit. That is a new book, not a flag on `best_of`.

## What to keep

Keep `best_of` as the control for the old engine: `sent_split`, `entry_watch` (15 minutes), `burst_runner` (20% giveback), and both EV switches off. It is the least-bad mix of the books that actually led the scorecard, and it is already running. It is not the repair. Do not add leftovers, a wider burst stop, midnight crypto, EV blend, partial take, capital caution, a harder Day trail, or a short flag. The next book that matters is BTC-core or weekly majors momentum, judged only by whether it beats holding BTC after costs.
