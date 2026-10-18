# Strategy rethink: why every book trails BTC, and what to test instead

Report only. Data: `data/trading.db`, `docs/data*.json` and `data/daily_summary.txt` at main `55f49f9` (18 Oct 22:26 UK), plus two years of Kraken daily closes (the repo keeps no crypto daily history).

## 1. Why it loses

1. **Core is the main loss.** Of the −£16.36 since 11 Oct, Core lost £10.49, Day £3.55 and Risk £2.31. Core's altcoins and growth stocks fell further than BTC: BTC −3.1%, ETH −6.8%, SOL −8.9%, XRP −9.9%, and MON was sold at −20%. That is altcoin beta, not a signal problem.
2. **The Day signal points the wrong way.** I sampled 7,343 Day scores from 11 to 18 Oct, leaving out coins with bad feeds. Coins at or above the 0.35 buy line returned −0.26% over the next 4 hours, against −0.18% for all coins. Coins scoring −0.35 or lower returned +0.03%. At this horizon, momentum reverses.
3. **Costs exceed the edge.** Day traded £930 on a £60 pot in 8 days, 15 times its size. Costs are 0.23% per side and came to £2.15, 61% of Day's loss. Day still lost £1.40 before fees.
4. **Churn and noise stops.** The median hold is 1.3 hours. Bursts have 18 stops to 2 targets, because stops of about 1.5% sit inside normal intraday noise.
5. **Payoffs are lopsided the wrong way.** Momentum wins 29.5%, with +4.2% average wins and −3.0% average losses. It needs about 42% wins to break even.
6. **Small-cap tilt.** Thin coins cost up to 0.40% per side and fall hardest in risk-off weeks.
7. **Tuning can't fix it.** All 15 variants sit between −£10.5 and −£20.8; they share the same entries.
8. **Benchmark mismatch.** The stocks account can't hold BTC. Judge it against VUSA or EQQQ.

## 2. Different strategies

The backtest below uses Kraken daily closes from December 2024 to 18 Oct 2026 at 0.15% per side. It is one in-sample path, so treat it only as a sanity check. BTC hold returned −14.1%, with a −53% max drawdown.

**A. BTC trend filter, long or flat.**
- Rule: at each daily close, hold 100% BTC if the close is above its 50-day average, otherwise cash.
- Result: 46 trades, **+16.8%, max drawdown −26%**. A 20-day average returned −1.7%, so the window matters.
- Evidence: time-series momentum (Moskowitz, Ooi & Pedersen 2012; Liu & Tsyvinski 2021).
- Risk: it lags V-shaped rebounds.

**B. A, sized by volatility.**
- Rule: in trend, hold min(1, 40% ÷ 30-day realised volatility).
- Result: +15.2%, max drawdown −24%. Volatility targeting alone returned −15.5%, so use it only to size A.

**C. Relative-strength rotation across BTC, ETH and SOL.**
- Rule: each week, hold the one with the best 28-day return among those above their own 50-day average, otherwise cash.
- Result: 28 switches, **+48%, but max drawdown −50%**.
- Evidence: the crypto momentum factor (Liu, Tsyvinski & Wu 2022). Majors cost about 0.05% per side.
- Risk: it holds one coin at a time and depends on the path.

**D. Quiet strength, the low-buzz version of the owner's view.**
- Rule: once a month, take the top 30 coins by market cap and remove any with a buzz jump of 3× or more, or a 30-day return in the top 20%. Hold the 3 strongest remaining by 90-day return against BTC, but only those above their 50-day average. Otherwise hold BTC.
- Evidence: attention spikes tend to reverse (Da, Engelberg & Gao 2011).
- Data: buzz has only been logged since 9 Oct, so it can't be tested until about December.

**E. Shorting.**
- **Kraken, UK retail:** derivatives are professional-only. Spot margin shorts against fiat (for example BTC/USD) are allowed, but only on majors, so hyped small caps can't be shorted. Costs: an opening fee plus a 4-hourly rollover.
- **Trading 212:** Invest and ISA are long-only. Shorting needs a separate CFD account (stocks only, 5:1 leverage, overnight financing, no crypto for UK retail). Probably not on the user's plan.
- **Test:** A with a −0.5× BTC short below the 50-day average returned +25.3% before borrow costs. At 0.06% a day it falls to +13.8%, and at 0.12% a day to +3.3%. Both are worse than long or flat.
- **Engine:** long-only, with no short positions, borrow accrual or margin calls. Test shorts offline only; don't build them yet.

**How to paper-test A, B and C**
- Store daily crypto closes in `daily_closes`.
- Run each as a `variants.py` book on the crypto account with Kraken costs.
- Pass bar: beat BTC hold after costs, profit factor ≥1.1, drawdown no worse than −20%. Trend books trade too rarely for 100 trades soon, so use a multi-year replay plus live paper.
- Stocks account: the same trend filter on VUSA or EQQQ, benchmarked against that fund.

## 3. What to keep from `best_of`

- **Keep:** the price-source sanity check (`5a970108`), the exit watcher and `entry_watch` timing, the 20% giveback runner trail, and `sent_split`'s run-up penalty as a filter for D.
- **Demote:** Day momentum and burst. Limit them to 10–15% of capital or run them only in paper until a forward test shows an edge after the 0.46% round trip.
- **Drop:** Risk dips (2 wins in 7) and Core's altcoin and growth-stock selection. Replace Core with A, optionally sized with B.

## Verdict

The losses come from altcoin beta and a short-term signal that reverses, not from bad settings. Make Core a 50-day BTC trend filter with C as challenger, shrink Day trading to an experiment, and leave shorts until the engine supports them and borrow costs are known.
