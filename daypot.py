"""DAY pot v7 helpers (pure functions over the DB; no network): two sub-strategies share the Day pot.
  momentum - the original momentum + sentiment score entries; exits now volatility-scaled (1h ATR), a trailing stop after
             +trail_after_pct (gives back trail_giveback_pct of the peak gain) and an optional 'runner' at the target.
  burst    - quick breakout trades: 24h-high (or tight 4h-range) breakout + volume spike + RSI < rsi_max + sentiment not
             negative + broad market not falling. Every setup gets a CONFIDENCE score 0-1 (breakout strength, volume,
             sentiment, market regime, the setup type's measured hit rate). A setup is taken when its confidence >= the threshold
             (base + a raise proportional to the DAY pot's loss today relative to the daily loss cap) AND its expected value after
             costs is positive; size = confidence x a capped % of the burst cash still free (shrinks as losses deplete it).
             No loss-streak cooldown, no trades-per-day cap. Confidence is a ranking heuristic, NOT a probability of profit and never a guarantee.
Data granularity: prices are 15-minute snapshots (cycle cadence), so hourly bars/ATR are built from ~4 points per hour
(true ranges are under-estimated) and exits react at most every 15 minutes (a stop can fill beyond its level)."""
import json
from datetime import datetime, timedelta, timezone

clip = lambda x, lo=0.0, hi=1.0: max(lo, min(hi, x))
SUBS = ("momentum", "burst")

def hourly_bars(pts):
    """[(hour_key, high, low, close)] from (ts, price) points (prices table, USD)."""
    bars = {}
    for ts, p in pts:
        k = ts.strftime("%Y%m%d%H"); b = bars.get(k)
        bars[k] = [max(b[0], p), min(b[1], p), p] if b else [p, p, p]
    return [(k, *v) for k, v in sorted(bars.items())]

def atr_pct(pts, n=14, min_bars=4):
    """1h ATR as % of the last price (Wilder TR, simple mean of the last n hourly bars); None if too few bars."""
    b = hourly_bars(pts)
    if len(b) < min_bars + 1: return None
    trs = [max(h - l, abs(h - b[i - 1][3]), abs(l - b[i - 1][3])) for i, (_, h, l, _c) in enumerate(b) if i > 0][-n:]
    return sum(trs) / len(trs) / pts[-1][1] * 100 if trs and pts[-1][1] else None

def levels(cfg_tp, cfg_sl, atr):
    """(take-profit %, stop %) = clamp(max(min, atr_x * ATR%), ..., max). ATR unknown -> the minimums."""
    f = lambda d: clip(max(float(d["min_pct"]), float(d.get("atr_x", 0)) * (atr or 0)), float(d["min_pct"]), float(d.get("max_pct", 99)))
    return f(cfg_tp), f(cfg_sl)

def ret_h(pts, h):
    if not pts: return None
    t, p = pts[-1]; old = [x for ts, x in pts if ts <= t - timedelta(hours=h)]
    return p / old[-1] - 1 if old else None

def rsi_h(pts, n=14):
    hc = [c for *_x, c in hourly_bars(pts)]
    if len(hc) <= n: return None
    d = [b - a for a, b in zip(hc, hc[1:])][-n:]
    up, dn = sum(x for x in d if x > 0), -sum(x for x in d if x < 0)
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)

def breakout(con, a, pts, B):
    """Breakout check on USD points. Returns dict(kind '24h'|'4h'|None, strength 0-1, info, ext_pct)."""
    if len(pts) < 3: return dict(kind=None, strength=0, info="too little history")
    t, p = pts[-1]
    lb, rl = float(B.get("breakout_lookback_h", 24)), float(B.get("range_lookback_h", 4))
    mb, mx = float(B.get("min_break_pct", 0.1)) / 100, float(B.get("max_extension_pct", 3)) / 100
    prior24 = [x for ts, x in pts if t - timedelta(hours=lb) <= ts < t]
    cover = (t - min(ts for ts, _ in pts if ts >= t - timedelta(hours=lb))).total_seconds() / 3600
    hi24 = max(prior24) if prior24 and cover >= lb * 0.75 else None
    r = con.execute("SELECT high24 FROM prices WHERE asset=? AND ts<? AND high24 IS NOT NULL ORDER BY ts DESC LIMIT 1",
                    (a, t.isoformat())).fetchone()       # CoinGecko 24h high of the previous snapshot (crypto)
    if r and r[0]: hi24 = max(hi24 or 0, r[0])
    prior4 = [x for ts, x in pts if t - timedelta(hours=rl) <= ts < t]
    out = dict(kind=None, strength=0.0, hi24=hi24, ext_pct=None)
    if hi24 and p >= hi24 * (1 + mb):
        ext = p / hi24 - 1
        if ext <= mx: return dict(out, kind="24h", ext_pct=ext * 100, strength=clip(0.7 + 0.3 * ext / mx),
                                  info=f"24h-high break +{ext*100:.2f}% (high ${hi24:.5g})")
        out["info"] = f"24h high broken but +{ext*100:.1f}% above it (> {mx*100:.0f}%: not chased)"; return out
    if len(prior4) >= 6:
        h4, l4 = max(prior4), min(prior4); rng = h4 / l4 - 1 if l4 else 9
        if p >= h4 * (1 + mb):
            ext = p / h4 - 1
            if rng <= float(B.get("range_max_pct", 4)) / 100 and ext <= mx:
                return dict(out, kind="4h", ext_pct=ext * 100, strength=clip(0.45 + 0.25 * (1 - rng / (float(B.get('range_max_pct', 4)) / 100)) + 0.2 * ext / mx),
                            info=f"4h-range break +{ext*100:.2f}% (range {rng*100:.1f}%)")
            out["info"] = f"4h high broken but range {rng*100:.1f}% / extension {ext*100:.1f}% too wide"; return out
    out["info"] = f"no breakout (24h high {'$%.5g' % hi24 if hi24 else 'n/a'}, now ${p:.5g})"
    return out

def crypto_vol_ratio(con, a, t):
    """Last-hour volume vs the average hour, from CoinGecko rolling 24h total_volume deltas:
    v_dt ~= dV + V_prev*dt/24h (the slice that dropped out ~ an average slice) -> ratio = 1 + dV / (V_prev*dt/24h)."""
    rows = con.execute("SELECT ts, vol24 FROM prices WHERE asset=? AND vol24>0 AND ts>=? AND ts<=? ORDER BY ts",
                       (a, (t - timedelta(hours=2)).isoformat(), (t + timedelta(seconds=1)).isoformat())).fetchall()
    if len(rows) < 2: return None
    tn, vn = datetime.fromisoformat(rows[-1][0]), rows[-1][1]
    prev = [r for r in rows if datetime.fromisoformat(r[0]) <= tn - timedelta(minutes=45)]
    if not prev: return None
    tp_, vp = datetime.fromisoformat(prev[-1][0]), prev[-1][1]
    dt_h = (tn - tp_).total_seconds() / 3600
    if dt_h <= 0 or dt_h > 1.75 or tn < t - timedelta(minutes=45): return None
    return max(0.0, 1 + (vn - vp) / (vp * dt_h / 24))

def stock_vol_ratio(con, a, t, sget):
    v = (sget(con, "stock_vol") or {}).get(a)
    if not v or not v.get("ratio") or datetime.fromisoformat(v["ts"]) < t - timedelta(minutes=40): return None
    return float(v["ratio"])

def market_regime(con, klass, a, hist, t, B, sget, exchange):
    """(ok, regime 0-1, info). crypto: BTC 1h/4h; stocks: index proxy of the listing exchange (state market_proxy, from the
    collector) with VUSA.L/EQQQ.L as fallback while London is open."""
    M = B.get("market") or {}
    if klass != "stock":
        m = M.get("crypto") or {}; pts = hist.get(m.get("proxy", "BTC")) or []
        r1, r4 = ret_h(pts, 1), ret_h(pts, 4)
        if r1 is None: return False, 0.0, "BTC trend unknown"
        ok = r1 * 100 >= float(m.get("min_1h_pct", -0.5)) and (r4 is None or r4 * 100 >= float(m.get("min_4h_pct", -1.5)))
        reg = clip(0.5 + r1 * 100 / 1.0 * 0.3 + (r4 or 0) * 100 / 3.0 * 0.2)
        return ok, reg, f"BTC 1h {r1*100:+.2f}%, 4h {'n/a' if r4 is None else f'{r4*100:+.2f}%'}"
    m = M.get("stocks") or {}; ex = exchange(a)
    px = (sget(con, "market_proxy") or {}).get(ex)
    if px and datetime.fromisoformat(px["ts"]) >= t - timedelta(minutes=45) and px.get("chg1h") is not None:
        r1, rd, name = px["chg1h"], px.get("chg_day"), px["sym"]
    else:
        fb = [s for s in (m.get("fallback") or ["EQQQ.L", "VUSA.L"]) if hist.get(s) and hist[s][-1][0] >= t - timedelta(minutes=45)]
        if not fb: return False, 0.0, f"{ex} market proxy unavailable"
        r1, rd, name = (ret_h(hist[fb[0]], 1) or 0) * 100, None, fb[0]
    ok = r1 >= float(m.get("min_1h_pct", -0.3)) and (rd is None or rd >= float(m.get("min_day_pct", -1.0)))
    reg = clip(0.5 + r1 / 0.6 * 0.3 + (rd or 0) / 2.0 * 0.2)
    return ok, reg, f"{name} 1h {r1:+.2f}%" + (f", day {rd:+.2f}%" if rd is not None else "")

def setup_stats(con, kind, B=None, t=None):
    """Measured record of one burst setup type ('24h' | '4h') over the last record_days: hit rate with a Beta prior
    (hit_prior x hit_prior_n pseudo-trades: (wins + p0*n0) / (n + n0)) and the observed expectancy in % per trade (net of
    all fees). The time window lets an old bad patch age out (no permanent lock-out after a few early losses).
    Sell reasons carry '[burst <kind>]'."""
    B = B or {}; p0, n0 = float(B.get("hit_prior", 0.5)), float(B.get("hit_prior_n", 20))
    since = ((t or datetime.now(timezone.utc)) - timedelta(days=float(B.get("record_days", 14)))).isoformat()
    rows = con.execute("SELECT value, gas, pnl FROM trades WHERE sleeve='day' AND sub='burst' AND side='SELL' AND reason LIKE ? "
                       "AND ts>=? ORDER BY id DESC", (f"[burst {kind}]%", since)).fetchall()
    pcts = []
    for v, g, pnl in rows:
        basis = (v or 0) - (g or 0) - (pnl or 0); pcts.append((pnl or 0) / basis * 100 if basis > 0 else 0)
    w = sum(1 for x in pcts if x > 0)
    return dict(p=(w + p0 * n0) / (len(pcts) + n0), n=len(pcts), wins=w, exp=sum(pcts) / len(pcts) if pcts else None)

def expected_value(ss, tp, sl, rt_cost, prior_n=10):
    """Expected value in % per trade AFTER costs. Model: p x target - (1 - p) x stop - round-trip costs, with p = the setup
    type's smoothed hit rate; blended with the measured expectancy of that setup (weight n vs prior_n) as data builds up.
    (Real exits also include trailing / max-hold / flatten, so this is an estimate.)"""
    model = ss["p"] * tp - (1 - ss["p"]) * sl - rt_cost
    if not ss["n"] or ss["exp"] is None: return round(model, 3)
    return round((ss["n"] * ss["exp"] + prior_n * model) / (ss["n"] + prior_n), 3)

def confidence(B, brk, vol, sent, regime, hr, freshness=None):  # hr = setup-type hit rate (0-1)
    """Weighted 0-1 confidence of a burst setup (a heuristic ranking score, not a probability).
    The freshness weight does not join the average: fr=1 leaves the score identical to the other components alone,
    and a staler quote scales it down (freshness.adjust_confidence)."""
    import freshness as frsh
    W = dict(B.get("confidence_weights") or dict(breakout=0.25, volume=0.25, sentiment=0.15, market=0.15, hit_rate=0.20))
    wf = float(W.pop("freshness", 0) or 0)
    vx = float(B.get("volume_x", 2))
    parts = dict(breakout=brk, volume=0.4 if vol is None else clip((vol - 1) / (2 * vx - 1)),
                 sentiment=0.5 if sent is None else clip((sent + 1) / 2), market=regime, hit_rate=hr)
    base = sum(W[k] * parts[k] for k in W) / sum(W.values())
    conf, fr = frsh.adjust_confidence(base, 1.0 if freshness is None else freshness, wf, sum(W.values()))
    out = {k: round(v, 2) for k, v in parts.items()}; out["freshness"] = fr
    return conf, out

def burst_spend(free_cash, gas, raw, mint, floor):
    """How much of the burst budget to spend on one setup.
    avail >= min trade: the usual size (confidence-sized, raised to the min trade, capped at the cash free).
    floor <= avail < min trade: spend the remaining cash (a £4.98 budget must not skip a good setup).
    avail < floor: spend nothing here; the caller may still borrow under the inter-pot rules.
    Returns (value, avail)."""
    avail = (free_cash or 0.0) - (gas or 0.0)
    mint, floor = float(mint), float(floor)
    if avail + 1e-9 >= mint: return min(max(raw, mint), avail), avail
    if avail + 1e-9 >= floor and avail > 0: return avail, avail
    return 0.0, avail

def conf_size(B, conf, free_cash):
    """Position size proportional to confidence: confidence x max_position_pct of the burst sub-strategy's CURRENT
    remaining cash (so sizes shrink as losses deplete it)."""
    return round(clip(conf) * float(B.get("max_position_pct", 33)) / 100 * max(0.0, free_cash), 4)

def threshold(B, day_pnl, cap_pct):
    """Capital-based caution: base + conf_raise_at_cap x (DAY pot loss today / start-of-day equity) / daily loss cap -
    rises linearly across the whole cap range (entries stop at the cap); gains never lower it below the base."""
    frac = clip(max(0.0, -day_pnl) / (float(cap_pct) / 100)) if cap_pct else 0.0
    return round(min(0.99, float(B.get("conf_threshold", 0.55)) + float(B.get("conf_raise_at_cap", 0.30)) * frac), 3)

def sub_stats(rows):
    """rows: dicts with pnl, value, gas, fees (round-trip fees of the sell + its buy share are not separated -> P&L is
    already net of all fees). Returns trades, win rate, avg win/loss %, profit factor, P&L, expectancy."""
    pcts, pnls = [], []
    for r in rows:
        basis = (r["value"] or 0) - (r["gas"] or 0) - (r["pnl"] or 0)
        pnls.append(r["pnl"] or 0); pcts.append((r["pnl"] or 0) / basis * 100 if basis > 0 else 0)
    w = [p for p in pcts if p > 0]; l = [p for p in pcts if p <= 0]
    gw, gl = sum(x for x in pnls if x > 0), -sum(x for x in pnls if x <= 0)
    n = len(pnls); wr = len(w) / n if n else None
    aw, al = (sum(w) / len(w) if w else None), (sum(l) / len(l) if l else None)
    return dict(trades=n, win_rate=round(wr * 100, 1) if wr is not None else None, avg_win=round(aw, 2) if aw is not None else None,
                avg_loss=round(al, 2) if al is not None else None, pf=round(gw / gl, 2) if gl > 0 else (None if not gw else 99.0),
                pnl=round(sum(pnls), 4), exp_gbp=round(sum(pnls) / n, 4) if n else None,
                exp_pct=round(wr * (aw or 0) + (1 - wr) * (al or 0), 2) if n else None)

def sub_shares(DC):
    """{sub: share 0-1} of the DAY money per account from day.<sub>.share_pct (normalised to sum to 1)."""
    raw = {sb: float((DC.get(sb) or {}).get("share_pct", 50)) for sb in SUBS}; tot = sum(raw.values()) or 1.0
    return {sb: v / tot for sb, v in raw.items()}

def sub_realized(con, since=""):
    """{(sub, account): realised P&L} of closed DAY trades (SELL pnl is already net of ALL fees incl. the buy's, which sit in
    the position cost). Pre-v7 day trades without a sub count as momentum. since = ISO ts (only trades after it)."""
    out = {}
    for sb, k, p in con.execute("SELECT COALESCE(sub,'momentum'), klass, pnl FROM trades WHERE sleeve='day' AND side='SELL' AND ts>?", (since or "",)):
        ac = "stocks" if k == "stock" else "crypto"; out[(sb, ac)] = out.get((sb, ac), 0.0) + (p or 0.0)
    return out

def sub_ledger(start, cash, positions, realized, shares, accounts=("crypto", "stocks"), borrowed=None, lent=None):
    """v7.1 DAY sub-strategy ledger - ONE definition used by the trader (sizing) and the dashboard (display).
    Per account ac and sub-strategy sb:
      cash[sb]   = share x DAY start money of ac + realised P&L of sb in ac - cost (incl. buy fees + gas) of sb's open positions
                   (= the money sb has NOT got tied up; 'free to open new trades' = this, never more than the account's real cash)
      value[sb]  = market value of sb's open positions
      equity[sb] = cash[sb] + value[sb]  (= the sub-strategy's BUDGET: its own money incl. open positions; a loss or fee of one
                   sub-strategy only shrinks its own budget, never the other's)
    Identity: DAY cash of ac = start + all realised P&L - cost of all open positions, so the sub cash sums to the account's
    DAY cash and the sub equities to its DAY equity. Any residual (should be ~0; e.g. pre-split history) is spread by share
    and reported as 'residual'.
    v7.3 loans: borrowed {(sb, ac): £ outstanding} adds to that sub's cash and is a liability (equity = cash + value - owed);
    lent {ac: £ the Day pot lent to another pot} is taken from the subs' cash by share and is a receivable (equity + lent share).
    start: {ac: £}, cash: {ac: £}, positions: [dict(sub, acct, cost, value)], realized: {(sb, ac): £}, shares: {sb: 0-1}.
    Returns {ac: {sb: dict(cash, free, value, cost, equity, open_n), 'residual': £}} (unrounded)."""
    out = {}
    for ac in accounts:
        cell = {}
        for sb in SUBS:
            ps = [p for p in positions if p["acct"] == ac and p["sub"] == sb]
            cost = sum(p["cost"] or 0.0 for p in ps); value = sum((p["value"] if p["value"] is not None else p["cost"]) or 0.0 for p in ps)
            ow = (borrowed or {}).get((sb, ac), 0.0); ln = (lent or {}).get(ac, 0.0) * shares[sb]
            cell[sb] = dict(cash=shares[sb] * start.get(ac, 0.0) + realized.get((sb, ac), 0.0) - cost + ow - ln, value=value, cost=cost,
                            open_n=len(ps), owed=ow, lent=ln)
        resid = cash.get(ac, 0.0) - sum(cell[sb]["cash"] for sb in SUBS)
        for sb in SUBS:
            x = cell[sb]; x["cash"] += resid * shares[sb]
            x["free"] = max(0.0, min(x["cash"], cash.get(ac, 0.0))); x["equity"] = x["cash"] + x["value"] + x["lent"] - x["owed"]
        cell["residual"] = resid; out[ac] = cell
    return out
