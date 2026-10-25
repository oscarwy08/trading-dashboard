"""RISK pot helpers (pure functions + DB reads, no network): crypto breakout screen from one CoinGecko markets snapshot,
the daily-close breakout score for the speculative stocks (normal exchange tickers, v5), and the dip / pullback buy (v7.7).
Used by collector.py (screen -> which coins to price), trader.py (entries) and dashboard.py (candidate table)."""
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
        if why_not:
            from common import prefilter_reject
            prefilter_reject("risk-breakout", sym, why_not)
            continue
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

def setup_stats(con, R, t=None, sub=None, klass=None):
    """Measured record of the RISK pot's closed trades over the last record_days: hit rate with a Beta prior
    ((wins + p0*n0) / (n + n0)) and the observed expectancy in % per trade after all fees (same model as the DAY burst).
    sub='dip' counts only dip sells; sub='breakout' counts breakout sells and older rows with no setup tag.
    sub=None counts every RISK sell (the pot's own record). klass='stock' or 'crypto' keeps the other
    market out of that hit rate. Omit klass only for the pooled pot record."""
    from datetime import datetime, timedelta, timezone
    p0, n0 = float(R.get("hit_prior", 0.5)), float(R.get("hit_prior_n", 20))
    since = ((t or datetime.now(timezone.utc)) - timedelta(days=float(R.get("record_days", 30)))).isoformat()
    pcts = []
    sql = "SELECT value, gas, pnl, sub FROM trades WHERE sleeve='risk' AND side='SELL' AND ts>=?"
    args = [since]
    if klass:
        sql += " AND klass=?"; args.append(klass)
    for v, g, pnl, sb in con.execute(sql + " ORDER BY id DESC", args):
        if sub == "dip" and sb != "dip": continue
        if sub == "breakout" and sb not in (None, "", "breakout"): continue
        basis = (v or 0) - (g or 0) - (pnl or 0); pcts.append((pnl or 0) / basis * 100 if basis > 0 else 0)
    w = sum(1 for x in pcts if x > 0)
    return dict(p=(w + p0 * n0) / (len(pcts) + n0), n=len(pcts), wins=w, exp=sum(pcts) / len(pcts) if pcts else None)

def confidence(R, brk, vol, sent, regime, hr, freshness=None):
    """Weighted 0-1 confidence of a RISK setup (a ranking heuristic, not a probability of profit). Components 0-1:
    breakout = the screen's breakout score (-1..1, negative = 0); volume = last hour vs the average hour (2x = half way,
    4x+ = full; unknown = 0.4); sentiment = (score + 1) / 2 (a missing print is below neutral, not 0.5); market = the DAY burst market-regime score;
    hit_rate = the RISK pot's measured hit rate (Beta prior). Freshness scales the result (1 = unchanged)."""
    import freshness as frsh
    W = dict(R.get("confidence_weights") or dict(breakout=0.30, volume=0.20, sentiment=0.15, market=0.15, hit_rate=0.20))
    wf = float(W.pop("freshness", 0) or 0)
    vx = float(R.get("volume_x", 2))
    import daypot
    parts = dict(breakout=clip01(brk or 0), volume=0.4 if vol is None else clip01((vol - 1) / (2 * vx - 1)),
                 sentiment=daypot.sent_part(sent, R.get("sentiment_missing", 0.25)), market=clip01(regime if regime is not None else 0.5), hit_rate=clip01(hr))
    base = sum(W[k] * parts[k] for k in W) / sum(W.values())
    conf, fr = frsh.adjust_confidence(base, 1.0 if freshness is None else freshness, wf, sum(W.values()))
    out = {k: round(v, 2) for k, v in parts.items()}; out["freshness"] = fr
    return conf, out

def arm_lock_pct(R):
    """Percent gain the trail actually locks when it first arms.

    The arm is a fixed +trail_after_pct on the price (15% on every coin). The sell is trail_pct under that
    peak price, so the locked gain is (1 + arm) * (1 - trail) - 1. At 15% and 10% that is 1.15 * 0.90 - 1 = 3.5%.
    It is not 15 * 0.9 = 13.5, and it is not scaled by ATR.
    """
    arm = float(R.get("trail_after_pct", 15))
    trail = float(R.get("trail_pct", 10))
    # Percent points, so 15 and 10 stay exact: (100+15)*(100-10)/100 - 100 = 3.5.
    return (100.0 + arm) * (100.0 - trail) / 100.0 - 100.0

def setup_ev(R, conf, atr, rt_cost, ss):
    """Expected value in % per trade AFTER costs, for THIS setup. The stop (12%) and the trail (arm at +15%, sell 10%
    off the peak) are unchanged; only the estimate moves. The win is the executable lock at the arm (arm_lock_pct,
    3.5% at the frozen 15/10 trail), the same on every coin, not scaled by ATR. A paper book that must not inherit
    this fix sets _legacy_arm_win and keeps the old ATR-scaled 15 x 0.9 distance. Loss distance is the prior average
    loss (usually cut before the full stop), scaled by this setup's volatility (1h ATR vs ev_atr_ref_pct; unknown ATR
    keeps the fixed loss prior). Hit probability is the confidence shrunk toward 0.5 until ev_conf_full_n closes,
    blended with the measured hit rate (daypot.hit_p), then with the measured expectancy. Returns (ev, win%, loss%, p)."""
    import daypot
    stop = float(R.get("stop_loss_pct", 12))
    win_prior, loss_prior = float(R.get("ev_win_pct", 12)), float(R.get("ev_loss_pct", 8))
    ref = float(R.get("ev_atr_ref_pct", 4)) or 4.0
    prior = R.get("_ev_prior")
    if prior:
        # risk_ev_fix paper book: the win, the loss and the starting hit rate come from a replay of this pot's own
        # entry and exit rules on past prices (config riskpot.ev_prior_empirical), not from the 3.5% arm lock and
        # a 50% coin flip. Live closes still move p (daypot.hit_p, weight n vs ev_prior_n) and the measured
        # expectancy is still blended in (expected_value). The stop and trail RULES are unchanged.
        return _empirical_ev(R, prior, conf, rt_cost, ss)
    if R.get("_legacy_arm_win"):
        target = float(R.get("trail_after_pct", 15)) * (1 - float(R.get("trail_pct", 10)) / 100)
        if atr and atr > 0:
            scale = max(0.55, min(1.8, float(atr) / ref))
            win, loss = target * scale, min(stop, loss_prior * scale)
        else:
            win, loss = win_prior, loss_prior
    else:
        win = arm_lock_pct(R)
        if atr and atr > 0:
            scale = max(0.55, min(1.8, float(atr) / ref))
            loss = min(stop, loss_prior * scale)
        else:
            loss = loss_prior
    n = int((ss or {}).get("n") or 0)
    p = daypot.hit_p(conf, ss, float(R.get("ev_prior_n", 10)), float(R.get("ev_conf_full_n", 30)))
    ev = daypot.expected_value(dict(p=p, n=n, exp=(ss or {}).get("exp")), win, loss, rt_cost, int(R.get("ev_prior_n", 10)))
    return ev, round(win, 3), round(loss, 3), round(p, 3)

def _empirical_ev(R, prior, conf, rt_cost, ss):
    """setup_ev for a measured prior: dict(p, win, loss) in % (win and loss are price moves before costs; the
    live round-trip cost is subtracted once). With no closes p is the replayed hit rate. Confidence moves p
    around that rate (not around 0.5) only as closes build, the same shrink as daypot.hit_p."""
    import daypot
    p0 = max(0.05, min(0.95, float(prior["p"])))
    win, loss = float(prior["win"]), float(prior["loss"])
    n = int((ss or {}).get("n") or 0)
    prior_n, full_n = float(R.get("ev_prior_n", 10)), float(R.get("ev_conf_full_n", 30))
    c = 0.5 if conf is None else max(0.0, min(1.0, float(conf)))
    shrink = min(1.0, n / full_n) if full_n else 1.0
    p_rank = max(0.0, min(1.0, p0 + (c - 0.5) * shrink))
    wins = (ss or {}).get("wins")
    p = (wins + prior_n * p_rank) / (n + prior_n) if n and wins is not None else p_rank
    p = max(0.05, min(0.95, p))
    ev = daypot.expected_value(dict(p=p, n=n, exp=(ss or {}).get("exp")), win, loss, rt_cost, int(prior_n))
    return ev, round(win, 3), round(loss, 3), round(p, 3)

def empirical_prior(R, setup, klass):
    """The replayed prior for one setup and market from riskpot.ev_prior_empirical, or None when there is none
    (that setup then keeps the main model). A prior with too few replayed trades is not used."""
    E = (R or {}).get("ev_prior_empirical") or {}
    row = (E.get(setup or "breakout") or {}).get(klass or "crypto")
    if not row or int(row.get("n") or 0) < int(E.get("min_n", 30)):
        return None
    if row.get("p") is None or row.get("win") is None or row.get("loss") is None:
        return None
    return dict(p=float(row["p"]), win=float(row["win"]), loss=float(row["loss"]), n=int(row["n"]))

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

def runup_adjust(c7, RP):
    """(block reason or None, breakout-score scale, refuse a Core loan).
    A 7-day run at or above max_runup_7d_pct is not a breakout. From runup_7d_penalty_from_pct up to that cap the
    breakout score is scaled from 1 down to 0.5, and Core cash is not lent into the name."""
    if c7 is None: return None, 1.0, False
    max7 = float(RP.get("max_runup_7d_pct", 80))
    pen7 = float(RP.get("runup_7d_penalty_from_pct", 40))
    c7 = float(c7)
    if c7 >= max7:
        return f"7d run-up +{c7:.0f}% >= {max7:.0f}% (not chased)", 1.0, True
    if c7 >= pen7 and max7 > pen7:
        return None, 1.0 - 0.5 * (c7 - pen7) / (max7 - pen7), True
    return None, 1.0, False

# ---------------- v7.7: dip / pullback buy (second RISK setup; breakouts are unchanged) ----------------
def dip_confidence(R, pullback, support, bounce, sent, regime, hr, freshness=None):
    """Weighted 0-1 confidence of a DIP setup. Components 0-1: pullback depth vs typical volatility, proximity to
    support, bounce confirmation, sentiment ((score+1)/2; a missing print is below neutral), market regime, this setup's own measured hit
    rate (Beta prior while no dip has closed). Freshness scales the result (1 = unchanged) and is its own mini-bar."""
    import freshness as frsh
    import daypot
    D = R.get("dip") or {}
    W = dict(D.get("weights") or dict(pullback=0.20, support=0.15, bounce=0.20, sentiment=0.15, market=0.10, hit_rate=0.20))
    wf = float(W.pop("freshness", 0) or 0)
    parts = dict(pullback=clip01(pullback), support=clip01(support), bounce=clip01(bounce),
                 sentiment=daypot.sent_part(sent, D.get("sentiment_missing", 0.25)),
                 market=clip01(0.5 if regime is None else regime), hit_rate=clip01(0.5 if hr is None else hr))
    base = sum(W[k] * parts[k] for k in W) / (sum(W.values()) or 1)
    conf, fr = frsh.adjust_confidence(base, 1.0 if freshness is None else freshness, wf, sum(W.values()))
    out = {k: round(v, 2) for k, v in parts.items()}; out["freshness"] = fr
    return conf, out

def dip(pts, R, sent=None, regime=None, hr=0.5, freshness=1.0, vol=None, atr=None, require_trend=False, klass=None, asset=None):
    """A pullback buy on snapshot prices [(ts, price), ...]. Shows a row once the drop from a recent high is large
    enough to be a dip. It is tradable only when that dip follows an uptrend or, unless require_trend, positive
    sentiment, stays shallower than a breakdown, is not still a one-day pump, and has turned up (higher low or a
    short average reclaimed, last bar up, off the low). require_trend is the sent_split book: positive sentiment
    does not replace the 48h uptrend. A steady rise is not a dip. Returns show, ok, why (one sentence), trend_ok,
    bounce, conf, parts."""
    from datetime import timedelta
    D = R.get("dip") or {}
    blank = dict(show=False, ok=False, why="", trend_ok=False, bounce=False, pullback=None, conf=None, parts=None, rsi=None)
    if not D.get("enabled", True) or not pts or len(pts) < 8:
        return blank
    t, price = pts[-1]
    stock = klass == "stock" and bool(asset)
    if stock:
        import daypot
        ages = daypot._session_ages(asset, pts, t)
        def sess_age(ts, end):
            if end == t:
                return ages.get(ts)
            return daypot._session_age_h(asset, ts, end)
        span = sess_age(pts[0][0], t)
        span_h = 0 if span is None else span
        excl_h = float(D.get("swing_exclude_h", 1.5))
        hi_h = float(D.get("high_lookback_h", 36))
        window = [(ts, p) for ts, p in pts if (ages.get(ts) or -1) >= excl_h and (ages.get(ts) or 1e9) <= hi_h]
    else:
        span_h = (t - pts[0][0]).total_seconds() / 3600
        excl = timedelta(hours=float(D.get("swing_exclude_h", 1.5)))
        window = [(ts, p) for ts, p in pts if t - timedelta(hours=float(D.get("high_lookback_h", 36))) <= ts <= t - excl]
    if span_h < float(D.get("min_history_h", 30)):
        return blank
    if len(window) < 4:
        return blank
    high_px = max(p for _, p in window)
    high_ts = max(ts for ts, p in window if p == high_px)
    after = [(ts, p) for ts, p in pts if ts >= high_ts]
    low_px = min(p for _, p in after)
    if high_px <= 0 or low_px <= 0:
        return blank
    pullback = (high_px - low_px) / high_px * 100
    lo_min = float(D.get("pullback_min_pct", 4))
    hi_max = float(D.get("pullback_max_pct", 18))
    if pullback < lo_min:
        return blank                                   # still rising, or only a wiggle: leave the breakout row in charge
    if stock:
        tr_h = float(D.get("trend_lookback_h", 48))
        ages_high = daypot._session_ages(asset, pts, high_ts)
        before = [p for ts, p in pts if ts <= high_ts and ages_high.get(ts) is not None and ages_high.get(ts) <= tr_h]
    else:
        before = [p for ts, p in pts if t - timedelta(hours=float(D.get("trend_lookback_h", 48))) <= ts <= high_ts]
    swing = min(before) if before else high_px
    trend_ok = bool(before) and swing > 0 and high_px / swing - 1 >= float(D.get("trend_min_pct", 8)) / 100 and swing < high_px
    sent_pos = sent is not None and float(sent) > float(D.get("sentiment_min", 0))
    trend_gate = bool(trend_ok) if require_trend else bool(trend_ok or sent_pos)
    prices = [p for _, p in pts]
    last_up = prices[-1] > prices[-2]
    nb = max(2, int(D.get("bounce_bars", 3)))
    higher_low = len(prices) >= nb * 2 and min(prices[-nb:]) > min(prices[-2 * nb:-nb])
    ma_n = max(2, int(D.get("ma_bars", 6)))
    ma = sum(prices[-ma_n:]) / ma_n if len(prices) >= ma_n else None
    reclaimed = ma is not None and price >= ma
    if stock:
        day = [p for ts, p in pts if ages.get(ts) is not None and ages.get(ts) <= 24]
    else:
        day = [p for ts, p in pts if ts >= t - timedelta(hours=24)]
    avg = sum(day) / len(day) if day else None
    slow_n = max(ma_n * 4, 16)
    slow = sum(prices[-slow_n:]) / slow_n if len(prices) >= slow_n else None
    import daypot
    if atr is None or atr <= 0:
        atr = daypot.atr_pct(pts) or 2.0
    rsi = daypot.rsi_h(pts)
    band = float(D.get("support_near_pct", 2))
    dists = [abs(price / lv - 1) * 100 for lv in (low_px, avg, slow, ma) if lv]
    near = min(dists) if dists else 99
    if near <= band: sup_s = 1.0
    elif near >= band * 4: sup_s = 0.0
    else: sup_s = 1 - (near - band) / (band * 3)
    typical = max(float(atr), 0.8) * float(D.get("pullback_atr_x", 1.2))
    full_until = hi_max * 0.65
    if pullback < typical * 0.5: pb_s = 0.25 * pullback / (typical * 0.5)
    elif pullback < typical: pb_s = 0.35 + 0.65 * (pullback - typical * 0.5) / (typical * 0.5)
    elif pullback <= full_until: pb_s = 1.0
    elif pullback >= hi_max: pb_s = 0.1
    else: pb_s = 1 - 0.9 * (pullback - full_until) / (hi_max - full_until)
    lift = float(D.get("bounce_min_pct", 0.4))
    lifted = price >= low_px * (1 + lift / 100)
    vol_ok = vol is not None and vol >= float(D.get("volume_x", 1.2))
    confirmed = bool(lifted and last_up and (higher_low or reclaimed or vol_ok))
    b_s = 0.0
    if confirmed:
        b_s = 0.62 + (0.15 if higher_low else 0) + (0.13 if reclaimed else 0) + (0.10 if vol_ok else 0)
        if rsi is not None and rsi <= float(D.get("rsi_oversold", 40)): b_s += 0.08
    else:
        if lifted: b_s += 0.25
        if last_up: b_s += 0.15
    if stock:
        old = [p for ts, p in pts if sess_age(ts, t) is not None and sess_age(ts, t) >= 24]
    else:
        old = [p for ts, p in pts if ts <= t - timedelta(hours=24)]
    r24 = (price / old[-1] - 1) * 100 if old and old[-1] else None
    chasing = r24 is not None and r24 > float(D.get("chase_24h_pct", 60))
    too_deep = pullback > hi_max
    if chasing:
        why = f"Still a one-day pump: up {r24:.0f}% over 24 hours, so this dip is not bought."
    elif too_deep:
        why = f"Still falling: down {pullback:.1f}% from the recent high, too deep to be a dip."
    elif not trend_gate:
        if require_trend and not trend_ok:
            why = f"Pulled back {pullback:.1f}%, but there is no 48h uptrend."
        else:
            why = f"Pulled back {pullback:.1f}%, but there is no uptrend and sentiment is not positive."
    elif not confirmed:
        why = f"Still falling: pulled back {pullback:.1f}% and the last bars have not turned up."
    else:
        lead = "Uptrend" if trend_ok else "Positive sentiment"
        if abs(price / low_px - 1) * 100 <= band * 1.5: loc = " toward the recent low"
        elif avg and abs(price / avg - 1) * 100 <= band: loc = " toward the day average"
        elif reclaimed: loc = " and back above a short average"
        else: loc = ""
        rsi_bit = f" RSI {rsi:.0f} was oversold." if rsi is not None and rsi <= float(D.get("rsi_oversold", 40)) else ""
        why = f"{lead}, pulled back {pullback:.1f}%{loc}, and the last bars turned up.{rsi_bit}"
    ok = bool(confirmed and trend_gate and not too_deep and not chasing)
    conf, parts = dip_confidence(R, pb_s, sup_s, b_s, sent, regime, hr, freshness)
    # USD price that confirms the bounce. The entry watcher arms this when the dip has not turned up yet.
    level = low_px * (1 + lift / 100.0)
    return dict(show=True, ok=ok, why=why, trend_ok=trend_ok, bounce=bool(confirmed), pullback=round(pullback, 2),
                conf=conf, parts=parts, rsi=round(rsi, 1) if rsi is not None else None, level=level)
