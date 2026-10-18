# Strategy rethink: why every book trails BTC, and what to test instead

Council review, report only. Data: `data/trading.db`, `docs/data*.json` and `data/daily_summary.txt` at main `55f49f9` (18 Oct 22:26 UK). Two-year daily BTC/ETH/SOL closes are from Kraken's public OHLC endpoint, because the repo keeps no crypto daily history.

## 1. Why the current approach loses

1. **Core is the main loss, not Day.** Since 11 Oct the book is down £16.36. Core lost £10.49 of that, Day £3.55 and Risk £2.31. Core holds altcoins and growth stocks, and in a falling tape they fell further than BTC: BTC −3.1%, ETH −6.8%, SOL −8.9%, XRP −9.9%, and MON was sold at −20%. That is altcoin beta, not a signal problem.
2. **The Day signal points the wrong way.** I sampled 7,343 Day scores from 11 to 18 Oct, leaving out the coins with bad price feeds. Coins that scored at or above the 0.35 buy line returned −0.26% on average over the next 4 hours. All coins averaged −0.18%. Coins scoring −0.35 or lower returned +0.03%. On this universe and horizon, short-term momentum reverses.
3. **Costs exceed any edge.** Day bought and sold £930 on a £60 pot in 8 days, about 15 times its size. Costs are 0.23% per side (0.46% a round trip) and came to £2.15, which is 61% of Day's loss. Day still lost £1.40 before fees.
4. **Churn.** The median Day hold is 1.3 hours, and 82 trades closed in 8 days. Bursts have 18 stops to 2 targets, so ATR-sized stops of about 1.5% sit inside normal intraday noise.
5. **Payoffs are lopsided the wrong way.** Momentum wins 29.5% of the time, with an average win of +4.2% and an average loss of −3.0%. It needs about 42% wins just to break even.
6. **Small-cap tilt.** Day and Risk trade thin coins, which are the most expensive to trade (up to 0.40% per side) and fall hardest in risk-off weeks.
7. **Cash drag looks protective but isn't.** Day and Risk sit mostly in cash, yet the realised losses are larger than what holding BTC lost.
8. **Benchmark mismatch.** Half the capital is the stocks account, which can't hold BTC. Measured against BTC it is a different bet, not a worse one. It should be judged against an index fund such as VUSA or EQQQ.
9. **Tuning can't fix this.** Fifteen variants span only −£10.5 to −£20.8, because they all share the same entries. The best Day result, `sent_split` momentum at +£1.42 over 40 trades, is within noise.
10. **The sample is short.** Eight days in a down market is not a test of anything. But no sign of an edge shows up even before costs.

## 2. Different strategies (each must beat BTC buy-and-hold after costs)

Quick check on Kraken daily closes over about 2 years, starting December 2024, at 0.15% per side, ending 18 Oct 2026. BTC hold returned −14.1% with a −53% max drawdown. This is a single path and in-sample, so treat it only as a sanity check.

**A. BTC trend filter, long or flat (time-series momentum).**
- Rule: at each daily close, hold 100% BTC if the close is above its 50-day average, otherwise hold cash.
- Result: 46 trades in 2 years, **+16.8%, max drawdown −26%**. A 20-day average returned only −1.7%, so this is sensitive to the window.
- Why it should beat BTC: it sidesteps long bear runs. There is published evidence of trend in BTC (Liu & Tsyvinski 2021; Moskowitz, Ooi & Pedersen 2012).
- Risk: it lags sharp V-shaped rebounds.

**B. A, plus volatility targeting.**
- Rule: when in trend, hold min(1, 40% ÷ 30-day realised volatility).
- Result: +15.2%, max drawdown −24%. Volatility targeting alone returned −15.5%, so it only lowers risk and doesn't beat BTC without leverage.
- Keep it only as a way to size A.

**C. Relative-strength rotation across BTC, ETH and SOL (cross-sectional momentum).**
- Rule: each week, hold the one with the best 28-day return among those above their own 50-day average. If none qualify, hold cash.
- Result: 28 switches, **+48%, but max drawdown −50%**.
- Why: the momentum factor shows up in crypto (Liu, Tsyvinski & Wu 2022). Liquid majors cost about 0.05% per side.
- Risk: it holds one coin at a time, and the result depends on the path.

**D. Quiet strength, the low-buzz version of the owner's view.**
- Rule: once a month, take the top 30 coins by market cap and remove any with a buzz jump of 3× or more, or a 30-day return in the top 20%. Hold the 3 strongest remaining by 90-day return against BTC, but only those above their 50-day average. Otherwise hold BTC.
- Why: attention spikes tend to reverse (Da, Engelberg & Gao 2011; pump-and-dump studies).
- Data: buzz has only been logged since 9 Oct, so it can't be tested until about December.

**E. Shorting (addendum).**
- **Kraken, UK retail:** derivatives and futures are for professional clients only. Spot margin shorts of crypto against fiat (for example BTC/USD) are allowed without the high-net-worth declaration. Margin covers majors only, so hyped small caps can't be shorted. Costs are an opening fee plus a rollover fee every 4 hours; check Kraken's current schedule.
- **Trading 212:** the Invest account and ISA are long-only. Shorting needs a separate CFD account: stocks only, 5:1 maximum leverage, overnight financing, and no crypto CFDs for UK retail. Flag this: it's probably not on the user's current plan.
- **Test:** A with a −0.5× BTC short below the 50-day average returned +25.3% before borrow costs. At 0.06% a day it falls to +13.8%, and at 0.12% a day to +3.3%. Both are worse than long or flat.
- **Other short ideas:** shorting coins after a buzz spike can't be done on Kraken for thin coins. Pairs against BTC only work for alts Kraken offers on margin.
- **Engine:** it is long-only. Positions, `apply_sell` and the backtester have no short positions, borrow accrual or margin calls. A short variant needs new accounting code. Until then, test shorts offline on price history only.
- **Verdict:** don't build shorts yet.

**How to paper-test A, B and C**
- Add daily crypto closes to `daily_closes`; today they hold only stocks, PAXG and XAUT.
- Run each as a `variants.py` book on the crypto account with the real Kraken costs.
- Pass bar: beat BTC hold after costs on the same window, profit factor of at least 1.1, and drawdown no worse than −20%. Trend strategies trade too rarely to reach 100 trades soon, so judge them on a multi-year replay plus live paper results.
- For the stocks account, run the same trend filter on VUSA or EQQQ and benchmark it against that fund.

## 3. What to keep from `best_of`

- **Keep:** the price-source sanity check (`5a970108`); the exit watcher and `entry_watch` timing, which are infrastructure; the 20% giveback runner trail, which gives fat-tailed exits; and `sent_split`'s run-up penalty, as a filter for D.
- **Demote:** Day momentum and burst as written. Cap them at 10–15% of capital or run them only in paper until a forward-return test shows a positive edge after the 0.46% round trip.
- **Drop:** Risk dips (2 wins in 7, stopped at −12%) and Core's altcoin and growth-stock selection. Replace Core with A, optionally sized with B.

## Verdict

The losses come from altcoin beta and a short-term signal that reverses, not from bad settings. Make Core a 50-day BTC trend filter (with C as the challenger), shrink Day trading to an experiment, and leave shorts until the engine supports them and borrow costs are known.
