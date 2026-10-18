# Council Review: Strategy Rethink (`gemini-4-argon`)

**Benchmark (18 Oct 22:35 UK):** Main £183.61 (−8.20% vs £200 start; −9.27% since 11 Oct), `best_of` £189.19 (−6.51%), passive BTC (£) −2.79% since start (−4.18% since 11 Oct). All 16 books lag BTC by 230–650 bps.

## 1. Why the Current Architecture Structurally Loses vs BTC (8 Flaws)

1. **Core Crypto Alt Dilution (−£6.48 drag):** Core Crypto (£50) holds 16% BTC (£7.99, −3.3%) and 84% alts: MON (stopped −£2.61/−20.3%), WLD (−£1.86/−18.7%), XRP (−£1.34/−10.3%), ETH (−£0.40/−6.7%). Alts suffer 2.5x–5x BTC downside in flat/down regimes.
2. **Core Equity Tech Concentration (−£4.04 drag):** Core Stocks (£50) holds 6 correlated growth names (ZS −12.7%, AMD −11.7%, PLTR −11.6%, ONT.L −7.4%, TEAM −2.8%, MSFT −2.4%) with zero commodity/index ballast.
3. **15m Churn vs Fee Hurdle (−£3.55 Day drag):** 82 closed Day trades paid £2.20 fees (62% of net loss) + £0.07 gas. At 52–100 bps round-trip cost, 15m/5m noise needs >55% WR; actual is 29.5% (Momentum, PF 0.58) and 39.5% (Burst, PF 0.58).
4. **Buying Late-Cycle Hype:** Until `sent_split`, X buzz spikes (`buzz>=3x`) boosted scores after +5% to +15% runups—buying retail tops (`main` Momentum −£2.52 vs `sent_split` Momentum +£2.15).
5. **Knife-Catching in Risk Dip (−£2.80 on 7 trades, 29% WR):** Buying −6% to −25% 24h drops with a −12% stop and no trend filter catches breakdowns (5/7 stopped out).
6. **Negative Skew from Clipped Winners:** Tight targets (+4% Burst) and early trail arms (+1.5%) cap winners at +1.92% avg while gap-through stops average −2.14% (`partial_take`: 46.8% WR yet −10.07% return).
7. **No Macro Regime Gate:** Deploys 24/7 long setups even when BTC and NDX sit below falling 20d/50d MAs.
8. **Pro-Cyclical EV Feedback:** Rolling 20-trade history gates (`ev_exit`/`day_ev_blend` −10.29%) liquidate at troughs and block rebounds.

## 2. Four Structural Strategy Replacements (Pass Bar: Beat BTC Net of Costs)

### A. BTC + Gold/Index Core with Time-Series Momentum (TSMOM) Regime Gate
- **Rules:** Crypto Core holds **85% BTC / 15% ETH** when `BTC>SMA_20d` and `ret_20d>0`; rotates 100% to cash when both turn negative (00:00 UTC daily). Stocks Core holds **40% Gold (`SGLN.L`), 30% World/S&P (`VWRP.L`/`VUAG.L`), 30% top-2 RS stocks** (`stockrank>0.70`, `price>SMA_50d`).
- **Edge vs BTC:** Captures 85%+ of BTC uptrends while sidestepping −20% to −50% drawdowns below 20d MA, replacing bleeding alts/tech with Gold and index trend.
- **Paper test:** Add `core_regime_tsmom` in `variants.py`; 1–2 trades/mo cuts fees >90%.

### B. Cross-Sectional Relative Strength vs BTC (Weekly Dual-Momentum Rotation)
- **Rules:** Rebalance Sundays 20:00 UTC across top 12 crypto + top 15 stocks. Rank by BTC-relative strength (`RS_btc=0.6*ret_7d+0.4*ret_30d-btc_blend`). Hold max 2 alts only if `RS_btc>+5%` and `BTC>SMA_20d`; else hold **100% BTC** (or cash if `BTC<SMA_20d`). Exit if `RS_btc<0` or −10% trail.
- **Edge vs BTC:** Defaults to BTC unless an alt proves multi-week outperformance net of 60 bps fees, eliminating passive alt bleed.
- **Paper test:** Replace Day Momentum with weekly rotation in `variants.py`.

### C. "Quiet Accumulation" Swing Breakout (4h/1d Compression -> Expansion)
- **Rules:** Replace 15m scalping with 4h/1d swings (hold 2–10d, max 3 trades/wk). Require: (1) 7d Bollinger width <25th percentile, (2) 24h vol >=2.5x 20d avg, (3) 7d high breakout, (4) X `buzz_ratio<=1.3` with `catalyst>=0.6` (volume before chatter). Stop: 2x ATR_14d; exit: 20% profit-giveback trail (`burst_runner`, no TP cap).
- **Edge vs BTC:** Targets +12% to +35% waves where 52 bps fees are <4% of gross move, scaling `sent_split` + `burst_runner` alpha to 4h/1d bars.
- **Paper test:** Add `day_swing_4h` in `variants.py`.

### D. Volatility-Targeted Capitulation Mean-Reversion (Majors Only)
- **Rules:** Replace `risk_dip` on illiquid alts. Trade only **BTC, ETH, SOL, MSFT, NVDA, AAPL** when: (1) `price>SMA_200d`, (2) 3d RSI <18 or price <2.5SD below 20d VWAP, (3) 4h close above prior 4h high. Size for 20% annualised vol; exit at 20d SMA or +6%.
- **Edge vs BTC:** Buys panic liquidations in surviving leaders with confirmed turnarounds instead of falling knives.
- **Paper test:** Restrict `riskpot.py` dip universe to majors + 4h higher-close trigger.

## 3. Short-Side & Betting-Against Evaluation (UK Retail & Engine)

- **Setups:** (1) **Post-Hype Short** (inverse `sent_split`): short tokens after `24h_ret>+20%` and `buzz>=3.5x` when 1h price breaks 4h VWAP (+8% stop, −15% target, 48h max). (2) **Alt/BTC Bear Basket:** when `BTC<SMA_20d`, Long £15 BTC / Short £15 weak alts (`RS_btc<-5%`).
- **UK Venues & Costs:**
  - **Kraken+ / Pro:** **FCA PS20/10 bans crypto derivatives (perps/futures) for UK retail.** Spot margin is blocked unless Elective Professional (€500k+ portfolio). Consumer **Kraken+** is spot-only (~1% spread, or 0% fee to £10k/mo on £4.99/mo plan)—**no shorting**.
  - **Trading 212:** Invest/ISA accounts are **long-only** (inverse ETPs restricted). Shorting requires a **T212 CFD** account (outside plan), charging **~0.05%–0.15%/day overnight swap (~18%–55% APR)** + wide spreads and 20%–50% margin—prohibitive on £100.
  - **DeFi (Monad/Aave):** On-chain borrow-shorting works from self-custody but costs **10%–45% APR borrow/funding** + liquidation risk.
- **Paper Engine (`trader.py`/`common.py`):** **Not supported natively** (`positions` requires `qty>0`, `sell()` clamps `qty=min(qty,pos['qty'])` and deletes at 0 at `trader.py:1303`, `test/check_data.py` asserts non-negative values). Add `side IN ('long','short')` + `borrow_bps_per_day:8` in `variants.py` to shadow-test **Post-Hype Short**, executing live UK bearish views via **Cash/Gold rotation**.

## 4. Existing `best_of` Components to Keep

Keep all 3 modules in `config/variants/best_of.yaml` (£189.19, #1/16 books, +£5.58 vs Main):
1. **`sent_split` (`variants.py:163`):** Overheat penalty (`-0.10` on `buzz>=3x`), quiet bonus (`+0.05` on low buzz + >=2x vol), `0.5` fallback.
2. **`entry_watch` (`variants.py:255`):** 1m pullback/breakout trigger (15m expiry) vs blind 15m buys.
3. **`burst_runner` (`variants.py:194`):** Uncapped upside with 20% profit-giveback trail after fees.
4. **Drop:** `ev_exit`, `ev_action`, `partial_take`, `midnight_crypto`, `day_trail_arm`.
