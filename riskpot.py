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

# ---------------- v7.4: Day-style decision for the RISK pot (confidence + EV after costs + capital-based threshold) ----------------
def clip01(v): return max(0.0, min(1.0, v))

def setup_stats(con, R, t=None):
    """Measured record of the RISK pot's closed trades over the last record_days: hit rate with a Beta prior
    ((wins + p0*n0) / (n + n0)) and the observed expectancy in % per trade after all fees (same model as the DAY burst)."""
    from datetime import datetime, timedelta, timezone
    p0, n0 = float(R.get("hit_prior", 0.5)), float(R.get("hit_prior_n", 20))
    since = ((t or datetime.now(timezone.utc)) - timedelta(days=float(R.get("record_days", 30)))).isoformat()
    pcts = []
    for v, g, pnl in con.execute("SELECT value, gas, pnl FROM trades WHERE sleeve='risk' AND side='SELL' AND ts>=? ORDER BY id DESC", (since,)):
        basis = (v or 0) - (g or 0) - (pnl or 0); pcts.append((pnl or 0) / basis * 100 if basis > 0 else 0)
    w = sum(1 for x in pcts if x > 0)
    return dict(p=(w + p0 * n0) / (len(pcts) + n0), n=len(pcts), wins=w, exp=sum(pcts) / len(pcts) if pcts else None)

def confidence(R, brk, vol, sent, regime, hr, freshness=None):
    """Weighted 0-1 confidence of a RISK setup (a ranking heuristic, not a probability of profit). Components 0-1:
    breakout = the screen's breakout score (-1..1, negative = 0); volume = last hour vs the average hour (2x = half way,
    4x+ = full; unknown = 0.4); sentiment = (score + 1) / 2 (none = 0.5); market = the DAY burst market-regime score;
    hit_rate = the RISK pot's measured hit rate (Beta prior). Freshness scales the result (1 = unchanged)."""
    import freshness as frsh
    W = dict(R.get("confidence_weights") or dict(breakout=0.30, volume=0.20, sentiment=0.15, market=0.15, hit_rate=0.20))
    wf = float(W.pop("freshness", 0) or 0)
    vx = float(R.get("volume_x", 2))
    parts = dict(breakout=clip01(brk or 0), volume=0.4 if vol is None else clip01((vol - 1) / (2 * vx - 1)),
                 sentiment=0.5 if sent is None else clip01((sent + 1) / 2), market=clip01(regime if regime is not None else 0.5), hit_rate=clip01(hr))
    base = sum(W[k] * parts[k] for k in W) / sum(W.values())
    conf, fr = frsh.adjust_confidence(base, 1.0 if freshness is None else freshness, wf, sum(W.values()))
    out = {k: round(v, 2) for k, v in parts.items()}; out["freshness"] = fr
    return conf, out

def setup_ev(R, conf, atr, rt_cost, ss):
    """Expected value in % per trade AFTER costs, for THIS setup. The stop (12%) and the trail (arm at +15%, sell 10%
    off the peak) are unchanged; only the estimate moves. Win distance is the gain locked when the trail first arms,
    loss distance is the prior average loss (usually cut before the full stop), both scaled by this setup's volatility
    (1h ATR vs ev_atr_ref_pct; unknown ATR keeps the fixed priors). Hit probability comes from the setup's confidence,
    blended with the measured hit rate as trades close, then with the measured expectancy (daypot.expected_value).
    Returns (ev, win%, loss%, p)."""
    import daypot
    stop = float(R.get("stop_loss_pct", 12))
    target = float(R.get("trail_after_pct", 15)) * (1 - float(R.get("trail_pct", 10)) / 100)
    win_prior, loss_prior = float(R.get("ev_win_pct", 12)), float(R.get("ev_loss_pct", 8))
    ref = float(R.get("ev_atr_ref_pct", 4)) or 4.0
    if atr and atr > 0:
        scale = max(0.55, min(1.8, float(atr) / ref))
        win, loss = target * scale, min(stop, loss_prior * scale)
    else:
        win, loss = win_prior, loss_prior
    p_conf = max(0.05, min(0.95, float(0.5 if conf is None else conf)))
    n = int((ss or {}).get("n") or 0)
    if n and (ss or {}).get("wins") is not None:
        pn = float(R.get("ev_prior_n", 10))
        p = (n * (ss["wins"] / n) + pn * p_conf) / (n + pn)
    else:
        p = p_conf
    p = max(0.05, min(0.95, p))
    ev = daypot.expected_value(dict(p=p, n=n, exp=(ss or {}).get("exp")), win, loss, rt_cost, int(R.get("ev_prior_n", 10)))
    return ev, round(win, 3), round(loss, 3), round(p, 3)

def threshold(R, dd):
    """Capital-based caution: conf_threshold (0.55) at no drawdown, rising linearly to conf_threshold_at_stop (0.85) as the
    pot's drawdown (dd, e.g. -0.12) approaches the drawdown_cap_pct (30%) stop; gains never lower it."""
    base, top, cap = float(R.get("conf_threshold", 0.55)), float(R.get("conf_threshold_at_stop", 0.85)), float(R.get("drawdown_cap_pct", 30)) / 100
    frac = clip01(max(0.0, -dd) / cap) if cap else 0.0
    return round(min(0.99, base + (top - base) * frac), 3)

def size(R, conf, cash_free, pot_equity, mint):
    """Position size by confidence on the CURRENT remaining cash of the account: conf x conf_size_pct% of the free cash,
    at most min(max_position_gbp £8, max_position_pct 20% of the pot) and at least the min trade (mint, £5). Returns
    (size, cap)."""
    cap = min(float(R.get("max_position_gbp", 8)), float(R.get("max_position_pct", R.get("position_pct", 20))) / 100 * max(0.0, pot_equity))
    raw = clip01(conf) * float(R.get("conf_size_pct", 50)) / 100 * max(0.0, cash_free)
    return round(max(mint, min(raw, cap)), 4), round(cap, 4)      # the min trade wins if 20% of a shrunken pot is below it
