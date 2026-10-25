# Kronos walk-forward replay — results (evaluation only)

Script: `scripts/kronos_replay.py` (fetch → forecast → daycheck → report). No data, forecasts or weights are committed;
they live outside the repo (`KRONOS_WORK`, default `/workspace/kronos_eval`). Nothing here is wired into trading.

## Setup
- **Model**: Kronos-mini (`NeoQuasar/Kronos-mini` + `Kronos-Tokenizer-2k`, 4.1M params), CPU only. Kronos-small (24.7M) on BTC/ETH/SOL/XRP as a check (see below).
- **Data**: Binance 1h candles, 20 coins, 240 days (28 Feb – 25 Oct 2026): BTC, ETH, SOL, XRP + Day-pot alts ADA, DOT, NEAR, TIA, STX, STRK, ENA, CRV, QNT, RAY, ZRO, WLD, DOGE, AVAX, LINK, SUI. (AKE, BTW, CARDS, XMR, TRAC, AKT are not on Binance.) Kronos was released in 2025, so the whole window is after its training data.
- **Walk-forward**: every 8 hours (fixed UTC grid), Kronos sees only the last 512 closed bars and forecasts the next 4 hourly closes (3 sampled paths averaged, T=1.0, top_p=0.9). 656 forecasts per coin, 13,120 in total.
- **Split**: in-sample 20 Mar – 7 Jul 2026, out-of-sample 7 Jul – 24 Oct 2026. Kronos is not fitted on this data; the only choice made in-sample is the entry threshold, which is then applied unchanged out-of-sample.
- **Trade test**: long-only, no shorting. At each grid time buy (equal weight) every coin whose forecast return beats the threshold, hold 1h or 4h, pay 0.5% round trip (the Day book's own trades log 0.39–0.56% costs). Compared with equal-weight buy-and-hold and with 500 random runs taking the same number of entries at the same times.

## Results — Kronos-mini, 20 coins

| horizon | period | direction hit-rate | share of bars that went up | correlation forecast vs realised | rank corr | cross-coin rank IC |
|---|---|---|---|---|---|---|
| 1h | in-sample | 49.9% | 48.2% | −0.029 | −0.029 | −0.015 |
| 1h | out-of-sample | 50.6% | 47.3% | −0.017 | +0.010 | +0.028 |
| 4h | in-sample | 51.1% | 48.0% | −0.016 | +0.023 | +0.011 |
| 4h | out-of-sample | 51.2% | 49.4% | −0.001 | +0.034 | +0.007 |

A hit-rate of about 51% is no better than always guessing "down" (51–53% in both periods).

Long-only after 0.5% round-trip cost:

| horizon | period | threshold | entries | win after cost | avg net / trade | strategy | random, same entries (mean / 95th pct) | buy-and-hold |
|---|---|---|---|---|---|---|---|---|
| 4h | in-sample | 0.5% | 1638 | 31.1% | −0.49% | −82.8% | −81.2% / −77.9% | −14.2% |
| 4h | in-sample | 1.5% (chosen IS) | 329 | 34.7% | −0.57% | −58.3% | −57.6% / −48.6% | −14.2% |
| 4h | out-of-sample | 0.5% | 1660 | 35.5% | −0.35% | −74.8% | −74.6% / −68.3% | +54.8% |
| 4h | out-of-sample | 1.5% (chosen IS) | 424 | 42.9% | −0.09% | −49.8% | −54.8% / −40.7% | +54.8% |
| 1h | in-sample | 0.5% | 1139 | 22.6% | −0.54% | −80.3% | −80.0% / −77.8% | −14.2% |
| 1h | out-of-sample | 0.5% | 1357 | 24.8% | −0.52% | −77.9% | −80.2% / −77.9% | +54.8% |
| 1h | out-of-sample | 1.5% (chosen IS) | 260 | 28.1% | −0.53% | −47.1% | −53.4% / −45.9% | +54.8% |

Even at a 0.1% round trip (major-coin cost) the 4h test loses: −0.02 to −0.03% per trade out-of-sample, behind random entries (−11%) and far behind buy-and-hold.
("Strategy" compounds every grid time's equal-weight basket, so many small losing trades add up to a large total.)
Kronos is also biased bullish: about a quarter of all forecasts call for more than +0.5% in 4h, but those entries do no better than random ones.

## Day entry signals (does Kronos add anything?)
14 closed Day-pot trades (19–21 Oct) in coins with Binance candles. Kronos forecast at the last closed hour before each buy:
- forecast 4h > 0: 5 trades, 1 winner, total P&L −£0.18
- forecast 4h ≤ 0: 9 trades, 3 winners, total P&L −£0.41
- a "Kronos > 0.5% cost" filter would have kept 1 trade (NEAR, +£0.09).
That is far too few trades to show anything, and no separation is visible.

## Kronos-small check
SMALL_PLACEHOLDER

## VPS feasibility (measured on this box, 1 CPU thread)
| | Kronos-mini | Kronos-small |
|---|---|---|
| params | 4.1M | 24.7M |
| process RAM (torch + model) | ~550–750 MB | ~740 MB+ |
| CPU per forecast, 512 bars → 4h, 1 path | ~0.26 s | ~0.5–0.6 s (2 threads) |
| CPU per forecast, 3 paths averaged | ~0.7 s | SMALL_CPU |
| load time | ~6 s (first download), 0.2 s cached | ~6 s |

20 coins once an hour would be about 15 CPU-seconds an hour with mini: the CPU cost is light. The real cost is RAM (about 0.6–0.75 GB resident while it runs) and a CPU torch install (~700 MB on disk). Mini would fit a VPS with ≥ 2 GB free, run as a short separate job, not inside the 5-minute cycle.

## Verdict
No edge. Over 13,120 walk-forward forecasts, Kronos-mini's direction calls are a coin flip, and its forecast returns do not correlate with realised returns, in-sample or out-of-sample. After costs, long-only entries on its signal lose about as much as random entries at the same times, and far trail buy-and-hold. Out-of-sample does not "clearly help after costs", so no paper book or forecast signal is sketched. Not worth deploying to the VPS for trading signals.

## Reproduce
```
python3 scripts/kronos_replay.py fetch --coins BTC,ETH,SOL,XRP,ADA --days 240
python3 scripts/kronos_replay.py forecast --model mini --coins BTC,ETH --threads 1   # one process per coin group
python3 scripts/kronos_replay.py daycheck --model mini
python3 scripts/kronos_replay.py report --model mini --horizons 1,4 --cost 0.005
```
