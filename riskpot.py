"""RISK pot helpers (pure functions + DB reads, no network): crypto breakout screen from one CoinGecko markets snapshot
and the daily-close breakout score for the speculative stocks (normal exchange tickers, v5). Used by collector.py (screen -> which coins to price),
trader.py (entries) and dashboard.py (candidate table)."""
import re
from statistics import mean

clip = lambda x, lo=-1.0, hi=1.0: max(lo, min(hi, x))
SYM_OK = re.compile(r"^[A-Z0-9]{2,10}$")

def excluded(x, R, base_ex):
    """Stablecoins, wrapped/staked/bridged tokens, exchange tokens, tokenized stocks/funds/gold -> reason, else None."""
    sym, name = (x.get("symbol") or "").upper(), " " + (x.get("name") or "").lower()
    if not SYM_OK.match(sym): return "odd symbol"
    if sym in base_ex or sym in {s.upper() for s in R.get("exclude_symbols", [])}: return "excluded (stable/wrapped/exchange)"
    if "USD" in sym or "EUR" in sym: return "stablecoin"
    for w in R.get("exclude_name_words", []):
        if w.lower() in name: return f"excluded ('{w.strip()}' in name)"
    c24, c7 = x.get("price_change_percentage_24h_in_currency"), x.get("price_change_percentage_7d_in_currency")
    if c24 is not None and c7 is not None and abs(c24) < 0.3 and abs(c7) < 0.6: return "pegged / stable-like"
    return None

def crypto_score(x):
    """Breakout score -1..1 from one markets row + hard conditions. Returns (score, ok, why)."""
    p, hi = x.get("current_price") or 0, x.get("high_24h") or 0
    c1 = (x.get("price_change_percentage_1h_in_currency") or 0) / 100
    c24 = (x.get("price_change_percentage_24h_in_currency") or 0) / 100
    c7 = (x.get("price_change_percentage_7d_in_currency") or 0) / 100
    turn = (x.get("total_volume") or 0) / (x.get("market_cap") or 1)
    nh = p / hi if hi else 0
    score = round(0.35 * clip(c24 / 0.20) + 0.25 * clip(c7 / 0.40) + 0.2 * clip((nh - 0.9) / 0.1) + 0.2 * clip(turn / 0.25), 3)
    fails = [w for cond, w in ((c24 >= 0.04, "24h < +4%"), (c7 >= 0.05, "7d < +5%"), (nh >= 0.92, "faded > 8% from 24h high"),
                                (c1 >= -0.02, "dropping (1h < -2%)"), (c24 <= 0.60, "24h > +60% (chasing a pump)")) if not cond]
    why = f"24h {c24*100:+.1f}%, 7d {c7*100:+.1f}%, 1h {c1*100:+.1f}%, {nh*100:.0f}% of 24h high, vol/mcap {turn:.2f}"
    return score, not fails, why + ("" if not fails else " | no: " + ", ".join(fails))

def screen(rows, c, monad_extra=()):
    """rows = CoinGecko markets rows (top 300 by mcap). Returns sorted candidate dicts (liquid, non-excluded small/mid caps)."""
    R = (c.get("riskpot") or {}).get("crypto") or {}
    base_ex = {s.upper() for s in c["crypto"]["exclude_symbols"]}
    out, seen = [], set()
    for x in list(rows) + list(monad_extra):
        sym = (x.get("symbol") or "").upper()
        if x["id"] in seen: continue
        seen.add(x["id"])
        rank, mcap, vol = x.get("market_cap_rank") or 9999, x.get("market_cap") or 0, x.get("total_volume") or 0
        is_monad = bool(x.get("_monad"))
        why_not = excluded(x, R, base_ex)
        if not why_not and not is_monad and rank <= int(R.get("max_rank_large", 40)): why_not = "large cap"
        if not why_not and not (float(R.get("min_mcap_usd", 2e7)) <= mcap <= float(R.get("max_mcap_usd", 5e9))): why_not = "market cap out of range"
        if not why_not and vol < float(R.get("min_vol_usd", 5e6)): why_not = "24h volume too low"
        if not why_not and vol / (mcap or 1) < float(R.get("min_vol_mcap", 0.03)): why_not = "turnover too low"
        if not why_not and is_monad and not x.get("_dex_ok"): why_not = "no KyberSwap liquidity on Monad"
        if why_not: continue
        sc, ok, why = crypto_score(x)
        out.append(dict(id=x["id"], a=sym, name=x.get("name"), rank=rank, mcap=mcap, vol=vol, usd=x.get("current_price"),
                        c1=x.get("price_change_percentage_1h_in_currency"), c24=x.get("price_change_percentage_24h_in_currency"),
                        c7=x.get("price_change_percentage_7d_in_currency"), score=sc, ok=ok, why=why, monad=is_monad,
                        dex=x.get("_dex")))
    return sorted(out, key=lambda d: (not d["ok"], -d["score"]))

def stock_score(con, sym, price_usd=None):
    """Breakout score for a stock from Yahoo daily closes (USD-converted) (+ live price). Returns (score, ok, why) or (None, False, why)."""
    closes = [r[0] for r in con.execute("SELECT close FROM daily_closes WHERE asset=? ORDER BY d DESC LIMIT 70", (sym,))][::-1]
    if len(closes) < 55: return None, False, "not enough daily history"
    p = price_usd or closes[-1]
    hi20 = max(closes[-21:-1]); r5 = p / closes[-6] - 1; r20 = p / closes[-21] - 1; nh = p / hi20
    a50 = mean(closes[-50:]); a50_prev = mean(closes[-55:-5]); above, rising = p > a50, a50 > a50_prev
    score = round(0.35 * clip(r5 / 0.15) + 0.25 * clip(r20 / 0.40) + 0.2 * clip((nh - 0.95) / 0.08)
                  + 0.2 * (1 if above and rising else 0 if above else -1), 3)
    fails = [w for cond, w in ((r5 >= 0.03, "5d < +3%"), (nh >= 0.97, "not at a 20-day high"), (above, "below 50d avg")) if not cond]
    why = f"5d {r5*100:+.1f}%, 20d {r20*100:+.1f}%, {nh*100:.0f}% of 20d high, {'above' if above else 'below'} 50d avg{' (rising)' if rising else ''}"
    return score, not fails, why + ("" if not fails else " | no: " + ", ".join(fails))
