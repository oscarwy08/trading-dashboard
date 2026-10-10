# Improvement review, 10 October 2026

Research and review only. No strategy code was changed. This file is the review.

**Book used.** Closed results are the live dashboard export generated 2026-10-10 21:20 UTC (22:20 UK), after the 22:00 UK flatten. The `origin/main` database at commit `2d55110` (cycle 20:40 UTC) still had the day positions open; it matches the same buys and is the source for fill prices before the flatten. Equity in that earlier snapshot was £199.83. After the flatten the book was **£200.40**.

**Code reviewed.** `PROJECT_BRIEF.md`, `README.md`, `trader.py`, `daypot.py`, `riskpot.py`, `config.yaml`, plus the cost, freshness, and quote path those files call. `stockrank.py`, `freshness.py`, and `quotes.py` were checked for the claims below (ranking weights, freshness scaling, quote-before-fill). The Risk dip and the entry-EV display live on the dip branch. They are not in this book, and they are not proposed again here.

---

## Is the system comprehensive?

The **control system is comprehensive** for a £200 paper book. Two accounts that cannot lend to each other, three pots with their own cash, a flatten that actually fired, a kill file, a drawdown halt that asks for a human file before Risk buys again, reconciliation checks, a freshness score, a gas estimate from the Monad RPC, and a written rule that live money waits on several weeks of paper profit. Saturday left the stocks account at £100 cash and zero trades, which is what a closed US, London, and Sydney session should do.

The **decision layer is not a tested strategy**. The book opened on the morning the rules were still being rewritten (confidence scoring, cost floors, gold tokens, a dip setup). Five closed trades, three of them one sleeve, cannot say whether any of that works. Feature completeness is ahead of evidence. Adding another setup would make that gap wider.

One correction to the question as it was put. Day momentum did **not** finish 2 for 2. It finished **1 win and 1 loss**, net about **+£0.09**, and the fees on those two round trips were about **£0.09**. Burst did finish **0 for 3**, about **−£0.20**. The day’s **+£0.40** is an open Risk mark, almost all of it one coin (STRK), on positions that have not closed.

---

## What the book did today

Start £200 at the great reset, split £100 crypto (Monad wallet, paper) and £100 stocks (Trading 212 Invest, paper). Each account is Core £50, Day £30, Risk £20.

| Sleeve | Closed | Wins | Net P&L | Costs on those trades | What actually happened |
| --- | --- | --- | --- | --- | --- |
| Day momentum | 2 | 1 | **+£0.09** | ~£0.09 | Both sold at the 22:00 flatten, not at the +4% target or the −2% stop |
| Day burst | 3 | 0 | **−£0.20** | ~£0.09 | One stop, two flattens. Modelled EV on two of the three buys was **+0.14%** |
| Day combined (crypto) | 5 | 1 | **−£0.11** | **£0.18** | Win rate on the dashboard: 20%. Stocks Day: £30 cash, no trades |
| Risk | 0 | — | open **+£0.40** | £0.07 on the buys | Three coins, cash £0, plus a £3.97 loan from Core |
| Core crypto | 0 | — | open **+£0.12** | £0.10 on the buys | All five buys were the 18:00 UK “no dip today” fallback |
| Core / Day / Risk stocks | 0 | — | £0 | £0 | Markets closed. Picks waiting: TEAM, ZS, ONT.L, AMD, MSFT, PLTR |

Book equity £200.40 (+0.20% on the day). Closed P&L −£0.11. Open mark about +£0.52. Gas across 18 fills was £0.0069. The 30bp model, not the gas, is the cost.

### Day momentum (1/2, net +£0.09)

Both bought 13:52 UTC. Both flattened 21:00 UTC. Neither hit a target or a stop.

- **WLD**, £7.50, score 0.73 against a focus threshold of 0.30. RSI **82**. Sentiment +0.45 at confidence 0.50. Target +4% / stop −2%, ATR 1.26%. Mid price **+3.08%** by the flatten, fill-to-fill **+2.46%**, net **+£0.18**.
- **ADA**, £7.49, score 0.521 against 0.35. RSI **85**. The reason string says **no fresh sentiment**. Target +4% / stop −2%, ATR 0.80%. Mid price **−0.58%**, fill-to-fill **−1.18%**, net **−£0.09**.

The momentum score does penalise RSI above 75, by 0.10 (`trader.momentum`). That was not enough to refuse either buy. Burst already refuses RSI at or above 80. Momentum does not.

On a missing sentiment print, momentum sets the sentiment weight to zero, so the combined score equals the price score (`trader` score loop). ADA was bought on price alone. That is a different bug from burst, where a missing print is scored as a neutral 0.5 and still counts as evidence.

### Day burst (0/3, net −£0.20)

Round trip on a £5 crypto trade in this model is about **0.60%** (0.30% per side) plus ~£0.0008 of gas. A 1.5% stop is two and a half round trips. A 3% target is five.

- **HYPE**, £5, 24h-high break of **+0.10%**, confidence 0.69, **EV +0.14%**, volume ×7.7, RSI 62. Stop fired at **−1.7%** against a **−1.5%** level. Mid move was only **−1.07%**. The other ~0.6% is the two 30bp fills, and the 15-minute bar went through the stop. Net **−£0.08** (database −£0.0837).
- **XRP**, £5, “4h-range break +0.71% **(range 0.0%)**”, volume ×13.8, RSI 70, confidence 0.73, **EV +0.14%**. Mid price at the flatten was **the same print as the buy**. Fill-to-fill **−0.60%**. Net **−£0.03**. The loss is the fee.
- **ZK**, £5, 24h-high break +0.38%, confidence 0.61, **EV +1.24%** because the ATR target was +7.1% and the stop −2.4%. Bought at 19:50 UTC, flattened 70 minutes later. Mid **−1.11%**, fill-to-fill **−1.70%**, net **−£0.09**.

The +0.14% figure is what the formula produces with a 50% prior, a 3% target, a 1.5% stop, and 0.60% of costs: `0.5×3 − 0.5×1.5 − 0.6 = 0.15`. It is not a measured edge. ZK was bought after HYPE had already stopped out, and the prior of 20 pseudo-trades (`hit_prior_n`) kept the setup “positive EV”.

Four of the five day exits were the flatten. The EV model does not include the chance of being closed at 22:00 UK. The code comment in `daypot.expected_value` says so.

### Risk (0 closed, open +£0.40)

All three were bought on the **old** rule, breakout score ≥ 0.45. `conf` is null. They are not the v7.4 confidence-and-EV rule, and they are not the dip rule on the other branch.

- **CAP**, £8, score 0.789, 24h +26.7%, 7d +28.5%. Mark **−£0.15 (−1.84%)**.
- **DRV**, £8, score 0.725, 24h +11.6%, 7d +46.1%. Mark **−£0.29 (−3.57%)**.
- **STRK**, £7.97, score 0.868, 24h +28.7%, **7d +111.8%**, 1h +7.5%, **RSI 83**, at the 24h high. Mark **+£0.84 (+10.52%)**. Trail arms at +15% and is not armed. Stop is −12% from cost. Core lent Risk **£3.97** inside the crypto account to do this buy. Reason on the loan: score 0.868 ≥ 0.65.

Crypto Risk cash is £0. Stocks Risk is £20 untouched, so the whole Risk book is three correlated small-cap crypto names. STRK’s open gain is the entire +£0.40 on the £200 book. A give-back inside the 12% stop, which is allowed until +15%, hands that gain back. CAP and DRV are already down.

The chase cap on a breakout is “no more than 3% above the high just broken”. A coin can be up 100% on the week and still pass, if it is sitting on the high. STRK did.

### Core (open +£0.12)

All five crypto tranches fired at 17:01 UTC as “daily tranche after 18:00 UK (no dip today)”:

| Asset | Cost | Mark | Target |
| --- | --- | --- | --- |
| MON | £5.00 | +1.99% | 26% of Core crypto, £13 |
| XRP | £5.00 | +0.41% | 26%, £13 |
| WLD | £9.99 | +0.28% | 20%, £10, marked built |
| BTC | £7.99 | −0.25% | 16%, £8, marked built |
| ETH | £5.99 | −0.18% | 12%, £6, marked built |

Core crypto cash left is £12.05 after the £3.97 loan. The loan is still inside Core equity as a receivable. Stocks Core is £50 cash.

WLD was in the Day pot from 13:52 and in Core from 17:01 until the 21:00 flatten. About four hours with the same coin in two pots. A crash would have hit both.

Freshness on the export: score 0.655, 189 names, 70 stale, oldest about 25 hours. That oldest age is the Friday US cash close, seen on Saturday night. The crypto buys themselves required a usable quote. Stale *universe* coverage is a Monday problem, not a bad fill today.

---

## What is structurally unlikely at this size

These are not “needs a tweak”. They are shapes that fight the account.

**1. Day burst, as specified, on £5.** A 0.60% round trip, a 1.5–2.5% stop, a 15-minute bar, and a hard flatten at 22:00. The model’s own EV on the first trades was +0.14%, which is smaller than one extra 15bp of slippage. The first three trades lost £0.20. Shen, Urquhart, and Wang (Financial Review, 2022; doi:10.1111/fire.12290) find Bitcoin *intraday* time-series momentum with unlevered breakeven costs of about **3, 7, and 10 basis points** per trade, against a 25bp Bitstamp fee. The strategies that clear a retail fee in that paper use leverage. This book is unlevered, on thinner names than Bitcoin, with a larger cost. Hudson and Urquhart (Annals of Operations Research, 2019) find that *daily* technical rules in large coins often clear 50bp, and that **support-resistance rules mostly do not**. A 15-minute break of a flat range is the weak end of that family, not the daily trend end.

**2. Day momentum, unless the intraday trend is several percent.** The sleeve can pay 0.60% when the move is like WLD’s +3% on the mid. It cannot pay 0.60% when the signal is “RSI 85 and the score cleared 0.35”. Today the fees on the sleeve equalled the net. The academic momentum that survives costs is monthly (Moskowitz, Ooi, and Pedersen, 2012) or 3–12 months (Jegadeesh and Titman, 1993). The one-week reversal (Jegadeesh, 1990) runs the other way: buying RSI 82–85 is the reversal side of the evidence.

**3. Risk micro-caps at £8 with a flat 30bp cost and a 12% stop.** £8 is 20% of a £40 Risk pot (and, with the loan, more than the pot’s own cash). CAP, DRV, and STRK are exactly the names where a £5–£8 swap is a large fraction of the near touch. The paper fill charges the same 30bp it charges BTC. If the live Monad pool impact is 1–3%, the open +£0.40 is an accounting number. Zero closes means the 12% stop and the 7-day hold are untested in this book.

**4. Day-trading stocks inside a 4% target.** Trading 212 Invest charges **no commission**. The FX fee on a GBP account buying a USD or AUD share is **0.15% per conversion** (Trading 212 UK costs and charges disclosure, Invest section; the CFD account’s 0.50% FX fee is a different product and is not this bot). UK stamp is **0.50% on the buy** of a main-market share. The paper model then adds 0.10% per side for the spread. A US day round trip is about **0.50%** before any spread wider than 10bp. A stamped UK day round trip is about **0.70%**, paid to hold for hours, and the position must be flat the same day. Liquid US names can be close to the 10bp spread. AIM names in the Risk and theme lists (and any name whose real spread is 1% or more) will not. Core *holds* can absorb one stamp and one FX conversion. Day trades and tight Risk stops cannot. None of this has traded yet. Monday’s first stock fills will be the test of the 0.10% assumption, and the assumption is the optimistic one.

**5. Calling a few weeks of this sample “consistent profitability”.** Five closes, one winner, rules changed the same day. A 95% Wilson interval on 0 wins in 3 burst trades is about **0% to 56%**. Detecting a true **0.30%** average edge per trade when the trade-level noise is about **1.5%** needs on the order of **200 closes of that one setup** at 80% power (`n ≈ 7.8 × (1.5 / 0.30)²`). The edge the burst model actually advertised, 0.14%, would need several times that. Bailey, Borwein, López de Prado, and Zhu’s backtest-overfitting results, and the Harvey, Liu, and Zhu (2016) multiple-testing haircut, both say a search across momentum, burst, several breakout definitions, and a dip rule on the same week is a search, and the winner of a search is biased up. The institutional bar those authors describe is a few hundred trades **across regimes**, or a pre-registered rule held still. It is not a green week.

Going live on this sample would be a decision about the account, not a decision the data can support.

---

## What the evidence actually supports

Short, so the ranked list can point at it.

- **Costs dominate short holding periods.** Shen, Urquhart, and Wang: unlevered intraday Bitcoin momentum dies at a 25bp fee; breakevens were 3–10bp. Hudson and Urquhart: daily rules on BTC, ETH, LTC, and XRP often survive 50bp, support-resistance usually does not, and most rules do not beat buy-and-hold on raw return. They do better on drawdown. That is a holding-period result. Core is on the side of the evidence that can survive costs. Burst is on the side that cannot, at this fee.
- **Momentum has a horizon.** Time-series momentum in futures is a 1-to-12-month effect. Cross-sectional equity momentum is a 3-to-12-month effect. The short horizon reverses. Crypto factor papers find a similar split: time-series momentum at daily and weekly horizons, little reliable cross-sectional momentum, and mean returns that look better than they are because of skew. A 15-minute score with a 0.35 threshold is not that evidence.
- **Sentiment is real and small.** Tetlock (Journal of Finance, 2007) finds that a burst of negative news lines up with next-day selling and then a partial reversal. The effect is days, not 15 minutes, and it is not large next to a 60bp round trip. Here, Grok scores a short list of names about three times a day, often at confidence 0.5 or below. Raising `sentiment_weight` above 0.4 would be weighting a sparse, slow series as if it were a tick signal.
- **Position sizing from Kelly or from volatility needs a size you can vary.** Moreira and Muir (Journal of Finance, 2017) scale a portfolio by the inverse of recent variance and improve Sharpe at portfolio scale. Kelly fractions do the same job for a single bet, and the honest fraction is a small one (quarter-Kelly is the usual caution) because the edge estimate is noisy. This Day momentum budget is about £15 in the crypto account. Half of that is £7.50, and the minimum trade is £5. The size formula is already a floor. A Kelly function would print a weight and then trade £5 anyway.
- **A paper test means something when the rule is frozen and the sample can reject “the mean is zero”.** Thirty trades is the point where a normal approximation starts to be less silly. It is not validation. A 50% win rate over 30 trades still has a Wilson interval of roughly 33% to 67%. Report the mean after costs, the interval, the result with the best trade removed, and the mix of exits (target, stop, flatten, time stop). Harvey and Liu’s point is that every extra rule you tried has to come out of the Sharpe you claim.

---

## Ranked improvements

Each item is something to change in the rules or the measurement. Effort is S (a gate or a config change plus a test), M (a new input or a new report that touches fills), L (a new data source or a redesign of a sleeve). “Conflict” is against the owner’s rules in `PROJECT_BRIEF.md`: dynamic counts, capital-based caution, no loss-streak cooldowns, no panic shutdown, freshness degrades confidence, sentiment plus market data, MON/WLD/XRP focus, pots stay separate, loans stay inside one account, several weeks of paper profit before live.

### 1. Freeze the rules and publish a scorecard before the next idea

**What.** Pick the rule set that is on main after the current dip branch is either merged or dropped. Write down, in the repo, the setups under test, the cost model, and the statistics that will be read later: closed count per setup, net after fees, fees divided by gross, exit mix (target / stop / trail / flatten / max-hold), mean trade, and a running interval. Do not change a threshold because one day looked good or bad. Do not add a sleeve during the window.

**Why.** Five closes, and the Risk entries are a different rule from the code that will buy the next one. v7.4 through v7.7 landed on the opening day. Shen’s breakeven and López de Prado’s sample-size results both say a retuned rule has no track record.

**Impact.** This is the only item that makes the next month usable as evidence. Without it, every later number is a new backtest.

**Effort.** S. **Risk.** Low. The operational risk is boredom: a bad burst day will look like a reason to edit `config.yaml`. **Conflict.** Aligns with “several weeks of profitable paper trading before live”. Conflicts with same-day rule churn. It does not add a trade cap or a shutdown.

### 2. Stop using a score as the probability, and put the flatten in the Day EV

**What.** In `riskpot.setup_ev`, hit probability is the confidence, clipped to 0.05–0.95, and confidence is documented in the same file as “a ranking heuristic, not a probability”. With no closes, that probability is the score. Shrink it hard toward 0.5 until the setup has a real `n`, and keep shrinking until `n` is large relative to `hit_prior_n` (20) and `ev_prior_n` (10). Raise `ev_min_pct` above 0 so that +0.14% cannot trade. For Day, the EV has to include the chance the position is still open at 22:00 UK and is sold at the market. Four of five day exits today were that event. A setup bought at 19:50 (ZK, 70 minutes of life, +7.1% target) should not be valued as if it had six hours and a full target.

**Why.** The +0.14% on HYPE and XRP is `0.5 × 3 − 0.5 × 1.5 − 0.6`. ZK’s +1.24% was the same formula with a wider ATR target, after HYPE had already lost. The prior of 20 pseudo-trades means three losses barely move the hit rate. The measured-expectancy blend will eventually go negative, but only after several losses of the *same* subtype, and the first trades are positive by construction.

**Impact.** Fewer Day and Risk entries, concentrated on setups whose target is a multiple of the real cost and of the time left in the session. This is the honest version of “size by edge”.

**Effort.** M. **Risk.** Medium: a higher hurdle will make the Day pot look idle, and an idle pot is not a broken pot. **Conflict.** Aligns with “expected value after costs decides”. It does not add an emotional halt. The threshold still rises with drawdown exactly as the brief asks.

### 3. Leave burst rare until that cost-aware EV has a sample

**What.** Keep the sleeve in the code. Require the post-fix EV (item 2) to clear a hurdle that is at least one extra 15–25bp of slippage, not `ev_min_pct: 0`. Until there are about 30 closed burst trades under the frozen rule and the after-cost mean is above zero, do not widen the universe or add a Day dip beside it. If the hurdle admits nothing, the sleeve is quiet. That is the result, not a cooldown.

**Why.** 0/3, −£0.20, and a modelled edge smaller than the cost error. The academic intraday result says the same thing without leverage.

**Impact.** Stops the sleeve that lost money today from spending the next £15 the same way. The Day momentum budget and both Core pots are untouched.

**Effort.** S, once item 2 exists. **Risk.** The sleeve may not trade for the whole paper window. **Conflict.** A hard “burst off forever” switch conflicts with “do not shut the system down” and with dynamic, confidence-driven entries. An EV floor does not: the brief already says EV after costs is the gate. A loss-streak cooldown would conflict, and this item is not that.

### 4. Reject a degenerate 4h range, and give momentum the RSI cap burst already has

**What.** In `daypot.breakout`, a 4h break is valid when there are at least six earlier prints, the range is at most 4%, and price is at least 0.1% above that high. A stuck quote has a range of zero, and the strength formula *rewards* tightness (`0.45 + 0.25 × (1 − range/max)`), so a flat line scores well. Require a real range (on the order of 0.3% or more, and more than one distinct price) before a 4h break can count. Separately, refuse a momentum entry at RSI ≥ 80, the same gate burst uses (`rsi_max`). The existing −0.10 score penalty at RSI > 75 did not stop WLD at 82 or ADA at 85.

**Why.** XRP was bought on “range 0.0%” and the mid price never moved. The £0.03 loss is the round trip. WLD and ADA were bought as momentum at RSI 82 and 85, which is the short-horizon reversal side of Jegadeesh (1990), and one of them lost.

**Impact.** Removes a class of trades whose gross expectation is zero. Small pounds, high clarity.

**Effort.** S. **Risk.** Low. A quiet afternoon will produce fewer 4h breaks. **Conflict.** None material. This is a definition of “breakout” and of “overbought”, which burst already uses. It is not a cooldown and not a block on stale data.

### 5. Price the fill from the pool and the book, not from a flat 30bp

**What.** Keep Monad gas as measured (~£0.00039 at 102 gwei and 200,000 units; it is not the problem). Replace `risk.fee_slippage_pct: 0.3` as a single number for every crypto name. Majors in a deep pool are often a 1bp or 5bp pool fee plus negligible impact on £5, so 30bp overstates BTC, ETH, and liquid XRP. Micro-caps (CAP, DRV, STRK, ZK, and Monad names outside the top of the book) need the Kyber route’s price impact on the **actual £ size**, applied to the paper fill. The collector already requests a $200 route for some Monad-ecosystem names (`dex_check_usd`, `max_impact_pct: 3`) and does not use it as the fill. For stocks, keep the Trading 212 FX fee at 0.15% per side and UK stamp at 0.50% where it is due. Replace the 0.10% spread with a quoted bid/ask when Yahoo or the broker provides one, and with a much wider default for AIM and for the thin US names on the Risk list, before any stock paper P&L is believed.

**Why.** The open Risk gain and every future stock fill depend on this number. A flat 30bp is the dangerous kind of error: it makes the book look slightly worse on BTC (harmless) and slightly better on the names Risk actually holds (not harmless). Uniswap v3’s fee tiers are 0.01%, 0.05%, 0.30%, and 1%; the tier plus the impact is the cost, and £8 chooses the impact. ONT.L is not in `stamp_exempt`, so the model will correctly charge 0.50% stamp on a main-market London buy. That is fine for a Core hold and expensive for a day trade. AIM tickers that *are* exempt still do not have a 10bp spread.

**Impact.** High, because it decides whether the Risk mark and Monday’s stock trades are information. It will likely cut Risk entries and widen Day stock costs. That is the point.

**Effort.** M. **Risk.** Medium: a wide impact number can zero out the Risk crypto universe on Monad, which may be the true answer at £8. **Conflict.** Aligns with “live data priority” and with fetching a fresh quote before the fill. No conflict with pot structure.

### 6. Do not buy a vertical week just because price is on the high, and do not lend Core into that name

**What.** The breakout chase cap is extension above the broken high (`max_extension_pct: 3`), not the size of the move that got there. STRK was +112% over 7 days, RSI 83, at 100% of the 24h high, and it passed. Apply a lookback cap on the breakout path as well (the dip work already rejects a 24h chase; the breakout path should refuse a similar extension, and a weekly move of this size). Cap one name at a smaller share of Risk when the book is three coins, and do not borrow from Core to finish a position that is already the whole crypto Risk pot. The loan rules can stay: exceptional, inside one account, repaid when the position closes. “Exceptional” should include “not a seventh-day double”.

**Why.** STRK is +£0.84 of the book’s +£0.40. The trail does not arm until +15%. Crypto Risk cash is £0. CAP and DRV are down. Stocks Risk never diversified the book because the market was shut. One Monad micro-cap is the P&L.

**Impact.** Smaller blow-up if STRK mean-reverts inside the 12% stop. Less Core cash locked in a receivable (£3.97 of £50, with £12.05 cash left for later MON and XRP tranches).

**Effort.** S for the chase cap, M if loan eligibility gains a trend filter. **Risk.** Missed trend continuation. STRK’s +10% open gain is what that risk looks like when it works, for a day. **Conflict.** Mild tension with “high-risk bets are allowed”. Consistent with the chase limit the owner already asked for on dips, and with capital-based size. Not a panic sell: open positions keep their stops.

### 7. The 1.5% burst stop is inside the sampler

**What.** Either widen the burst stop to at least about twice a typical 15-minute bar of that name, or drop the setup (item 3 does the second, quietly, if the EV hurdle is honest). HYPE’s stop was −1.5% and the fill was −1.7% on the position, with the mid only −1.07%. The paper engine already fills through the level. A live Kyber swap in a fast market will be worse, because the quote moves between the signal and the transaction.

**Why.** A stop that is smaller than the bar is a market order with a label. The cost model then stacks 60bp on top of the overshoot.

**Impact.** Fewer stop-outs that are really noise, or no burst trades. Both are improvements on today’s HYPE fill.

**Effort.** S. **Risk.** A wider stop loses more pounds when it is hit. At £5, 2.5% is £0.13 versus £0.08. The worse risk is pretending 1.5% is executable. **Conflict.** The 1.5% minimum is the owner’s chosen burst stop in `config.yaml`. Changing it conflicts with that parameter. Leaving it, and letting item 2 refuse the trade because the stop is smaller than the noise, does not. Say which one you are doing and do not silently edit the 1.5.

### 8. A missing sentiment print is uncertainty, not a zero or a half

**What.** Momentum currently drops the sentiment term when nothing is fresh, so a strong price score buys anyway (ADA). Burst and Risk confidence use 0.5 for a missing print (`daypot.confidence`, `riskpot.confidence`), which is “mildly positive evidence”. Treat a missing or low-confidence print as a wider band: scale confidence down the way freshness already does, and keep the hard block on a *present* negative print (`sentiment_block`). Do not raise `sentiment_weight`.

**Why.** Tetlock’s effect needs a real text signal and lasts about a day. Three Grok passes a day, on a short list, cannot confirm a 15-minute break. ADA’s buy reason is “no fresh sentiment” at RSI 85.

**Impact.** Fewer price-only momentum buys in names Grok did not score. Small, and in the right direction.

**Effort.** S or M. **Risk.** Low. Focus names that do get scored (WLD did) are unchanged. **Conflict.** Aligns with “stale data degrades confidence rather than hard-blocking” and with “sentiment combined with market data”. A hard block on *missing* sentiment would conflict with that; a scale-down does not.

### 9. The 18:00 fallback should not pay full size for an extended afternoon

**What.** All five Core crypto tranches were “no dip today”, including WLD on an afternoon when the Day book had bought it at RSI 82. Keep the fallback so a quiet market still builds the position, which is the point of the rule. Cut that tranche (half, or a confidence scale) when the day’s move is already extended, and buy the rest on a later session. Do not skip the build entirely.

**Why.** The fallback did its job (the positions exist) and also bought strength at the close of the first session, in the same name the Day pot was already long.

**Impact.** Slightly better average entry on Core, which is the sleeve most likely to be worth having. Pounds are small at tranche 1. The habit matters at £50 and at the next top-up.

**Effort.** S. **Risk.** A runaway market never dips, and half-size delays the target. That is why the fallback exists. **Conflict.** A rule that *skips* the fallback conflicts with “build the position, and if there is no dip, buy anyway so the day is not missed”. Half size on an extended day does not. Flag this if anyone proposes to delete the 18:00 path.

### 10. Do not implement Kelly or volatility targeting as a formula

**What.** Leave size as it is: confidence scales a cash budget, then the £5 minimum and the £8 / 20% cap bind. Document, next to `riskpot.size` and `daypot.conf_size`, that the minimum trade is the position. If the edge is too small for a £5 bet, the answer is item 2 (do not trade), not a smaller weight.

**Why.** Moreira and Muir help when you can scale a book from 0.4× to 1.5×. Quarter-Kelly on a noisy 0.14% edge is a fraction of £5. The code would round it back up to £5 and look scientific. Today’s momentum position was £7.50 on a £15 budget. There is no room under the floor.

**Impact.** Avoids a false sense that sizing is optimised. The real sizing decision is whether the trade exists.

**Effort.** S to document. Implementing it would be wasted M. **Risk.** None. **Conflict.** None, provided the formula is not added. Adding it would pretend a precision the brief’s £5 minimum does not allow.

### 11. “BTC is not crashing” is not a regime filter

**What.** Burst’s market gate is BTC 1h ≥ −0.5% and 4h ≥ −1.5% (`daypot.market_regime`). HYPE broke a high by +0.10% with BTC 1h +0.19% and 4h +0.23%. That is a flat tape. Require the *setup’s own* 24h or multi-day trend to agree, which the Risk dip path already does, before a 15-minute break can be called momentum. A flat or down name that pokes a 24h high by 0.1% is noise.

**Why.** The momentum evidence is a trend that already exists at a daily or weekly horizon. A gate that only blocks a BTC crash lets every coin break a high on a dull Saturday.

**Impact.** Cuts the +0.10% class of burst. Those are the trades whose target cannot pay the fee.

**Effort.** M, because the definition has to be the same in the scorer, the tests, and the reason string. **Risk.** Missed sharp reversals off a low. That is acceptable: this pot is not a reversal pot, and the short-horizon reversal evidence would want the opposite trade. **Conflict.** Mild tension with “degrade confidence rather than hard-block”. The book already hard-blocks on a negative sentiment print and on a closed market. A trend gate is that kind of rule. If you would rather scale confidence than block, do it in the same function and keep it visible in the reason string.

### 12. One name should not be Day and Core at the same time without a deliberate reason

**What.** Optional: if Day is long a name, delay that name’s Core tranche until the Day position is flat. WLD was both from 17:01 to 21:00.

**Why.** Pots are separate so that a Core hold is not a Day trade. They are not separate economically if they hold the same coin into the same hour. A gap through both stops is one bet counted twice.

**Impact.** Small on a £10 overlap. Real if both pots scale up.

**Effort.** S. **Risk.** The Core fallback misses that day’s tranche in a name Day happened to like. Core can buy it the next session. **Conflict.** Soft. The brief wants pots separate, which this supports, and also wants the daily tranche to happen, which a one-day delay softens. Do not block Core from ever holding a Day universe name. MON, WLD, and XRP are supposed to be in Core.

### 13. Put the exit mix and the fee ratio on the dashboard

**What.** For each setup (momentum, burst 24h, burst 4h, Risk breakout, and later the dip), show closed count, net after costs, costs as a fraction of gross, and the share of exits that were flatten, stop, target, trail, and time stop. The next review should not require a manual pass over `data.json`.

**Why.** Today’s story is invisible in a single win-rate number. Momentum “made” £0.09 and spent £0.09 to do it. Burst’s EV said +0.14% and the flatten, not the target, was the exit. A 20% account win rate mixes those.

**Impact.** Stops the next retune from being argued off a rounded P&L. No change to what is traded.

**Effort.** M. **Risk.** Low. **Conflict.** None. Aligns with “every number must reconcile”. The entry-EV display is already on the dip branch; this item is the *after-the-fact* table, and it is still missing.

### 14. Treat a missed 22:00 cycle as an overnight position, and keep the backup runner

**What.** The flatten fired today (21:00 UTC, 22:00 UK in British Summer Time). The brief already says GitHub’s scheduler is often late or skipped, and that the owner’s machine runs a cycle if none has landed in 20 minutes. Keep that. Add a visible flag on the Day pot if a position is still open after 22:00 UK, so a skipped cycle is an incident and not a silent swing trade.

**Why.** Day risk with no flatten is overnight gap risk on a sleeve that was not sized for it. That is a different strategy, with a different cost argument (overnight can actually match the momentum evidence, and it is not what this pot claims to be).

**Impact.** Operational, not statistical. One missed evening is enough to dominate a week of £5 trades.

**Effort.** S for the flag, none for the backup if it is already running. **Risk.** Low. **Conflict.** None.

---

## Bugs and risky logic, listed on their own

These are defects or foot-guns, including ones already covered above so they are not only buried in recommendations.

1. **`daypot.breakout` treats a zero-width 4h range as a coil.** Six identical prints, range 0, strength starting at 0.45 and rising because the range is tight. XRP’s reason string was “range 0.0%”. The mid price did not move. Net loss equalled the fee.
2. **The volume ratio can print a spike from a revision of the 24h total.** `crypto_vol_ratio` turns a change in CoinGecko’s rolling 24h volume into an hourly multiple. XRP showed ×13.8 on the same bar as a flat price. A data revision looks like a breakout confirmation.
3. **Momentum has no RSI hard cap.** Burst refuses RSI ≥ 80. Momentum subtracts 0.10 from the score above 75 and then bought RSI 82 and 85.
4. **Confidence is used as P(win).** `riskpot.setup_ev` sets `p` from `conf`. The docstring above `confidence` says it is not a probability. With `n = 0` the prior is the whole estimate.
5. **Burst EV ignores the flatten**, which was 4 of 5 Day exits. A buy at 19:50 with a +7% target is priced as a full target. `hit_prior_n: 20` keeps a 0-for-1 setup looking positive.
6. **One slippage number for every crypto name.** 0.30% per side on BTC and on STRK. Gas is ~£0.0004 and is not the error. The $200 Kyber impact check is not the fill.
7. **Stops are evaluated on 15-minute snapshots.** HYPE filled at −1.7% against a −1.5% stop. Live impact will overshoot further.
8. **The live Risk book is not under the rule the code will use next.** CAP, DRV, and STRK have null confidence. They were “score ≥ 0.45”. New buys will use the confidence gate. The open positions still follow the 12% stop, the +15% trail, and the 7-day clock. Entry quality and exit rules are from different versions. The £3.97 STRK loan is still open.
9. **The £5 minimum deletes the sizing formula.** `riskpot.size` and the burst sizer both lift the result to `min_trade_size`. On a £15 momentum budget, “50% of budget” and “Kelly” are the same trade.
10. **Core lent after it had just spent its first tranches.** Cash left £12.05 of £50, receivable £3.97. Not a broken invariant (the loan is inside one account, under the 20% cap). It does mean the next MON and XRP tranches compete with a loan that does not repay until STRK closes.
11. **Stock spreads of 0.10% per side will be wrong for AIM and for thin US names**, starting when those markets open. The FX 0.15% and the stamp list are the parts that match Trading 212’s Invest schedule. ONT.L is not stamp-exempt in config; if the intent was “AIM, no stamp”, ONT is main-market and the charge is right, but the spread still is not 10bp just because the stamp line is correct.
12. **Same-name overlap.** WLD in Day and Core together for about four hours.
13. **Freshness 70/189 stale** is mostly the closed stock session. It should not be read as “the crypto fills were stale”. It should be read, on Monday morning, as “the stock ranks may be Friday’s closes until the first cycle”.

---

## Top 5 to do next

1. **Freeze the rules and write the scorecard.** Do not retune, and do not go live, on five trades. The next change to a threshold needs a reason that is not today’s P&L.
2. **Fix expected value.** A confidence score is not a win probability. The hurdle has to sit above zero by enough to cover a wrong slippage guess. Day EV has to include the 22:00 flatten and the time left.
3. **Reject a 4h range that is not a range, and refuse momentum entries at RSI ≥ 80.** XRP’s fee-only loss and the RSI 82/85 buys are the examples.
4. **Price fills from real liquidity** before believing the Risk micro-cap marks or any stock paper P&L. Kyber impact on the £ size actually traded; Trading 212’s 0.15% FX, kept; stock spreads wider than 0.10% wherever the book is not a liquid US name.
5. **Leave burst rare** until that cost-aware EV has about 30 closes and a non-negative after-cost mean. Do not add a Day dip. Do not add a loss-streak cooldown.

Items 6 through 14 are real and smaller. They can wait for the scorecard, except the liquidity fill (item 4 in this list, item 5 in the ranking), which should be in place before Monday’s stock session is treated as data.

---

## What should stay

Worth stating, because a review otherwise gets read as a request to add brakes the owner has already rejected.

- No fixed number of trades per day. The confidence gate and the cash are the cap.
- No cooldown after a loss, and no halt because of a losing streak. The daily loss cap at 15%, and the threshold rising from 0.55 toward 0.85 as capital is lost, are the caution the brief asked for.
- Freshness should keep scaling confidence. A hard block on a slightly old quote fights the brief and, today, would have been aimed at Friday’s stock closes.
- Sentiment stays in the score at a modest weight. It should not be turned up, and it should not be deleted.
- MON, WLD, and XRP stay the Core focus. Pots do not lend across the crypto/stocks boundary. The kill file and the Risk resume file stay.
- Core’s 18:00 fallback stays. The improvement is the size on an extended day, not deleting the build.
- The dip-at-entry work and the entry-EV display, if they land from the other branch, are measurement and a slower Risk entry. They are not a substitute for items 1–5 here.

---

## Sources

- Live book: dashboard `data.json` generated 2026-10-10 21:20:19 UTC. Cross-check: `data/trading.db` on `origin/main` at `2d55110` (20:40 UTC), trades 1–14 and the HYPE close.
- Shen, Urquhart, and Wang (2022), “Bitcoin intraday time-series momentum”, *Financial Review* 57(2), 319–344. doi:10.1111/fire.12290. Unlevered breakeven costs about 3–10bp; Bitstamp fee cited at 25bp; leverage is what pushes breakevens through a retail fee.
- Hudson and Urquhart (2019), “Technical trading and cryptocurrencies”, *Annals of Operations Research*. About 15,000 daily rules. Breakevens from under 10bp (support-resistance on Litecoin) to well above 50bp (filters and some breakouts). Support-resistance rarely clears 50bp. Most rules do not beat buy-and-hold on raw return; more of them win on drawdown-aware ratios. Multiple-testing adjustments remove a large share of the “significant” rules and leave a smaller share standing.
- Moskowitz, Ooi, and Pedersen (2012), time-series momentum, 1–12 month horizon. Jegadeesh and Titman (1993), 3–12 month cross-sectional momentum. Jegadeesh (1990), short-horizon reversal.
- Moreira and Muir (2017), “Volatility-Managed Portfolios”, *Journal of Finance*. Inverse-variance scaling helps a portfolio that can change size. It does not apply under a £5 minimum on a £15 budget.
- Tetlock (2007), “Giving Content to Investor Sentiment”, *Journal of Finance*. News tone and next-day returns, small and partly reversing.
- Harvey, Liu, and Zhu (2016), “…and the Cross-Section of Expected Returns”, on haircutting a searched-over t-statistic. Bailey, Borwein, López de Prado, and Zhu, on the probability of backtest overfitting. The practical bar used here is their order of magnitude: a few hundred trades across more than one regime, or a rule that was written down before the trades.
- Trading 212 UK costs and charges disclosure (Invest versus CFD). Invest: no commission, FX fee 0.15% when a currency is converted, UK stamp 0.50% on eligible buys, no custody fee. The 0.50% FX figure in that document is the CFD account. PTM levy is £1.50 and only above £10,000, so it is irrelevant here.
- Uniswap v3 fee tiers (1bp, 5bp, 30bp, 100bp) as the shape of a pool fee. This book’s 30bp is a config constant (`risk.fee_slippage_pct`), not a Kyber quote for the £5–£8 size.
- Sample-size arithmetic in the text: normal power formula `n ≈ (z_α + z_β)² (σ / μ)²` with z ≈ 1.96 and 0.84. Wilson interval on 0/3 is about 0–56%. These describe this book. They are not a forecast of the next trade.
