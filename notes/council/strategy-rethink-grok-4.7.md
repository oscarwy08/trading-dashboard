# Strategy rethink — 18 Oct 2026

Scorecard 18 Oct 22:35 UK. Main equity £183.61 from £200 (−8.2%, max drawdown −9.3%, 93 closes, win 34%). P&L since the 11 Oct 18:07 copy is −£18.76. BTC spot £82,891 to £80,885 (−2.4%), and the book trails that hold by 2.8 points. Best mature book: sentiment-split £188.03. `best_of` is £189.19, win 12%. Every book is behind BTC. Pass bar: beat BTC buy-and-hold after costs.

## Why this approach loses

- Long alts and single stocks, judged against BTC. Core is −10.5% in a −2.4% BTC tape. MON sold at −20.3%. Higher beta and no hedge loses to the benchmark.
- Payoffs need a win rate the signals miss. Momentum: 44 closes, win 30%, +4.2% / −3.0%, profit factor 0.58 (break-even near 42%). Burst: 38 closes, win 40%, +1.9% / −2.1%, profit factor 0.58 (break-even near 53%).
- Fees on 1.5-hour holds. Momentum fees £1.34 on −£2.52. Burst fees £0.86 on −£1.03. Crypto fees £2.73. About 91 buys since 11 Oct. Momentum exits are 16 stops, 11 trails, 10 signal sells, 7 flattens. Burst exits are 21 stops and 2 targets. Trails paid. Stops and the 22:00 flatten did not.
- Core is a static theme book with no vol cap. One name moved the pot more than a week of Day trades. Sentiment is a small weight, missing on most buys, and high mention-count was not the losing bucket on live fills.
- Variant flags stay inside the same entries. Worst: midnight crypto £182.50, Day EV blend £181.55, partial-take £181.98. `everything` at £183.81 trails the runner. Size is a flat cash slice. A SELL only closes a long. There is no borrow, funding, or negative quantity.

## Different books

Fresh book, same prices. Benchmark: BTC buy-and-hold of the same cash, 0.3% a side on crypto, current stock costs. Stop if behind BTC after costs at 30 round trips or 100 days. Drawdown may not exceed BTC’s by more than 5 points.

**1. BTC core, one satellite.** Eighty-five percent of the crypto sleeve stays in BTC. Rebalance only if that weight drifts more than 5 points. The other 15% may hold one coin whose 20-day return beats BTC and whose 20-day vol is outside the top quarter. Hold at least 5 days. No intraday entry and no 22:00 flatten.

**2. Weekly time-series momentum.** Universe: BTC, ETH, SOL. Each Friday, hold a name if its 12-week return is positive. Otherwise cash. Size to 20% annualised vol from 20-day realised vol, cap 1×. That is the Moskowitz, Ooi, and Pedersen horizon (Journal of Financial Economics, 2012) and the Liu, Tsyvinski, and Wu crypto-factor horizon. Cash in a downtrend is how it can beat a forced long.

**3. Relative strength, BTC filter.** Once a week, rank liquid names by 4-week return minus BTC’s. Hold the top three. Drop a name that leaves the top ten. If BTC’s 4-week return is negative, hold BTC instead.

**4. Volatility-targeted BTC.** Hold only BTC. Daily weight = min(1, 15% / 20-day realised vol). Rest is cash (Moreira and Muir, Journal of Finance, 2017). Keep it only if the 100-day path still clears BTC.

**5. Quiet 30-day basket.** Once a month, among coins above $1B, pick three with the lowest 7-day mention count and a positive 90-day return versus BTC. Hold 30 days. A buzz change ≥ 3, or a 24-hour jump of 15% with sentiment ≥ 0.30, blocks a new buy for 7 days. Da, Engelberg, and Gao (2011) and Barber, Huang, Odean, and Schwarz (2022) find that reversal over weeks. Shut it if it lags BTC for 90 days.

## Shorts

UK retail on Kraken cannot trade derivatives. Futures need elective-professional status. Fiat or stablecoin margin needs a high-net-worth declaration (£150k income or £500k assets, plus an accountant’s letter) or business use. Kraken’s table puts crypto-versus-crypto margin, and shorting crypto against fiat, outside that restriction. Opening fee is about 0.01–0.02% on BTC and 0.02–0.04% on alts, charged again every 4 hours. A day-long alt short near 0.03% is about 0.2% before trade fees. A week is about 1.3%.

Trading 212 Invest cannot short. CFDs are a separate account, need an appropriateness check, and are not in the Invest plan. Retail equity CFDs cap leverage at 5:1. A short is credited overnight interest and pays the dividend.

Test only if the account can borrow. If BTC’s 4-week return is negative, short a small high-beta basket. Otherwise stay in cash, and stay in cash whenever the planned move is smaller than the rollover. Hype fade: buzz change ≥ 3 and a 24-hour jump ≥ 15%, short 3 days, stop +8%, cover −12%. Pair: long BTC and short the alt in equal notional after it beats BTC by 20% over 14 days. Cover at the 20-day average ratio. That pair can beat BTC if the spread exceeds the rollover.

A paper short needs negative quantity, a cover trade, and a daily funding debit. New book. Not a flag on `best_of`.

## What to keep

Keep `best_of` as the control: `sent_split`, `entry_watch` (15 minutes), `burst_runner` (20% giveback), both EV switches off. It led the old books and it is already running. It is still behind BTC. Leave out leftovers, a wider burst stop, midnight crypto, EV blend, partial take, capital caution, a harder Day trail, and shorts. Next book: BTC-core or weekly majors momentum, kept only if it beats holding BTC after costs.
