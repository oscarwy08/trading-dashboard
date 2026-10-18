# Strategy rethink — why the book structurally loses, and different directions

Council review, 18 Oct 2026. Data: `trading.db`, scorecard 22:25 UK 18 Oct, exit shadow report, variant scorecards. Window 11–18 Oct unless noted. Report only; rules frozen.

## 1) Diagnosis — why the core approach structurally loses

1. **No measurable entry edge.** Entries with confidence ≥0.75 average −1.37%/trade; <0.75 average −1.18% (84 trips since 11 Oct). Higher conviction does not win more; the score is ranking noise.
2. **The fee floor exceeds any plausible edge.** Average trade £5.70; effective one-side cost 0.49% (£2.61 fees on £532 turnover) → ~1% round trip on a 1.5h median hold. Crypto-account fees £2.73 ≈ 2.7% of a £100 account in 8 days.
3. **Churn compounds the fee drag.** 85 closes in 8 days, median hold 1.5h, 19 re-entries within 6h of the previous exit (KAIA bought 4× in one hour, BP 5×). The same coin is re-bought after each stop and re-fee'd each time.
4. **Stops leak beyond their level.** 42 stop exits, mean −4.8%; 11 gap-filled; overshoot averages −0.75pp (worst −4.7pp). A 5-minute cycle on 5-minute bars means the stop you set is not the stop you get.
5. **Winners are real but rare.** Trailing exits: 22/23 winners, +3.4% mean. Everything else loses. A few good trails pay for dozens of stops — PF 0.58 on both Day setups.
6. **Signal asymmetry is backwards.** Momentum buys strength in a falling tape (BTC −2.3% over the window, alts worse), then the same score exits hours later: 10/10 "signal" exits are losses. Whipsaw by construction.
7. **Beta carrier is concentrated alt beta.** CORE (−10.5%) is ~70% MON/WLD/XRP-emphasis; MON realized −20.3% on 18 Oct. Long high-beta alts, downtrend, no hedge.
8. **Day pot has no beta excuse.** Flat overnight with 1.5h holds; its −5.9% is pure negative alpha plus costs, not market exposure.
9. **Variants cannot fix a zero-edge core.** All 15 paper books lose; the ~6pp best-worst spread reflects which trades they happened to share, not rule quality. Threshold-tuning a noise signal stays noise.
10. **Small-sample caveat.** 93 closes is enough to indict the cost/churn arithmetic (which needs no alpha estimate), not enough to rank setups or bless the alternatives below.

## 2) Genuinely different strategies

**S1 — BTC-core with trend regime overlay.** Base holding: BTC (optionally + ETH). Rule: hold while BTC's daily close is above its 50-day average (or 30-day return positive); move the sleeve to GBP cash when below. Check once daily, no intraday trading. *Why it beats BTC hold:* crypto bear legs drive all of BTC-hold's underperformance vs any risk-managed book; time-series momentum keeps most upside and truncates −30…−60% legs. *Paper-test:* replay `daily_closes` through backtest.py; needs no new data.

**S2 — Cross-sectional relative-strength rotation.** Universe: liquid top-50 (existing screens). Weekly: rank by 30-day return excluding the last 7 days; hold the top 5 equally; drop names falling out of the top 10. Buzz veto per your thesis: exclude any name with `buzz_change` ≥3 or 24h run-up ≥15%. *Why:* alt relative strength persists (crypto momentum factor); you own the leaders, not the index; the veto avoids buying the top of hype spikes. *Paper-test:* 30 days of stored prices replay; buzz fields only exist since ~15 Oct, so shadow-run the veto forward.

**S3 — Volatility-targeted holding.** Hold BTC or the S2 basket sized target_vol/realised_vol (target ~40% annualised, weight = min(1, 40/realised-20d)); delever to cash when realised vol >80%, re-lever gradually. *Why:* cuts the left tail (the DD ≤20% bar becomes reachable) at similar mean return — better geometric compounding than raw hold. *Paper-test:* realised vol from the prices table; replay.

**S4 — Quiet-strength swing (your hypothesis, long side).** Buy quality alts with vol/mcap in the top universe quartile, price within 10% of its 30-day high, `buzz_level` below the universe median (and `catalyst_score` ≥0.3 when present). Hold days-to-weeks. Exit: sell *into* hype (buzz_change ≥3 with run-up ≥15%), or a 12% trail. *Why:* harvests the attention-reversal premium (Barber–Odean; Liu–Tsyvinski) without needing a short venue — quiet accumulation, distribution into spikes. *Paper-test:* sentiment archive is short, so replay price/volume rules now and shadow the buzz fields forward.

**S5 — Short-side (evaluated; mostly venue-blocked).**
- *Regime short BTC / pairs vs BTC:* strongest theoretical fit with S1–S4, but UK retail cannot short crypto: FCA PS20/10 bans crypto derivatives to retail, and Kraken UK retail is spot-only — no margin, no futures. Flag: Trading 212 CFD is a separate account, **not in your plan**, high-risk, and offers no crypto to UK retail anyway; stock CFDs would carry benchmark+~3% overnight funding plus borrow.
- *Executable approximation:* buy an inverse ETP (e.g. 21Shares Short Bitcoin ETP, LSE: SBTC) in the stocks account when the S1 regime is down. Costs: ~1–2%/yr TER plus daily-reset decay — fine for weeks-long bear legs, poor as a permanent hedge. The engine already trades stocks, so this is paper-testable today as an ordinary long.
- *Shorting hyped coins after buzz spikes:* the best alpha in the list and no venue for it as retail; typical perp funding ~0.01%/8h where it exists. Paper-test only.
- *Engine reality:* the book is long-only (no negative qty, no funding/borrow accrual, no margin). True shorts are paper-testable as shadow accounting in a backtest.py-style replay; live trading is venue-blocked regardless.

## 3) What to keep from `best_of`

Keep: **sent_split inputs** (catalyst/buzz fields — they become the S2 veto and S4 trigger), **entry_watch** (execution latency is strategy-orthogonal; it caught 28 entries and beat cycle exits by +2.4pp), **burst_runner trailing logic** (the only profitable exit family — reuse for S4 swing exits), **ev_exit-off** (the live-EV exit sold +1…+5% winners). Drop: the 5-minute momentum/burst/dip entries as the *primary* engine; the arithmetic in §1 says they cannot clear their own fee floor.

Pass bar for any candidate: ≥100 paper trades (or ≥6 months replay for S1/S3), PF ≥1.1, DD ≤20%, and beat BTC buy-and-hold net of costs — the bar the current book fails by ~2.7pp.
