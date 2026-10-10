"""CORE stock selection (no network): ranks the stock universe (normal exchange tickers, v5) (config stocks.universe, grouped by theme) from the daily
closes stored by collector.py (table daily_closes, Yahoo daily closes of the underlying), picks the CORE stocks and
builds the display-only 'investment plans' for bigger top-ups.

Composite score (-1 .. +1), computed at the weekly CORE rebalance (and shown on the dashboard):
  momentum  (weight core.score_weights.momentum): risk-adjusted returns r / (daily vol * sqrt(days)) over 6 months (126d),
            3 months (63d) and 1 month (21d), weighted 0.4/0.4/0.2 (only horizons with data), clipped to +-3 and divided by 3 (-1..1)
  quality   (weight .quality): above the 50-day average (+-0.4), 50-day average rising vs 20 days ago (+-0.3),
            distance from the 6-month high (> -10%: +0.3, -10..-25%: 0, < -25%: -0.3)
  sentiment (weight .sentiment, only if fresh): confidence-weighted mean sentiment for the xStock over the last 7 days;
            if there is none the other two weights are rescaled.
Eligible: 3-month return > 0 (6-month if 3-month is n/a), price above its 50-day average, momentum > 0, not excluded.
Picks: best composite first, at most core.max_per_theme per theme, count = stock budget / core.min_pick_gbp
(clamped to core.stock_picks_min .. core.stock_picks_max) so every pick is >= the minimum trade size."""
import json, math
from datetime import datetime, timedelta, timezone

clip = lambda x, lo=-1.0, hi=1.0: max(lo, min(hi, x))

def themes(c):
    """{ticker: (theme_key, label)} for the whole universe (first theme wins if a ticker is listed twice)."""
    out = {}
    for k, v in (c["stocks"].get("universe") or {}).items():
        for s in v.get("tickers", []): out.setdefault(s, (k, v.get("label", k)))
    return out

def watch_themes(c):
    """theme keys marked 'watch: true' (the user's 'watch closely' themes)."""
    return {k for k, v in (c["stocks"].get("universe") or {}).items() if v.get("watch")}

def risk_stocks(c):
    """RISK pot stock universe: riskpot.stock_themes (all tickers of those themes) + riskpot.stocks (extra names)."""
    R = c.get("riskpot") or {}; U = c["stocks"].get("universe") or {}
    out = [t for k in R.get("stock_themes") or [] for t in (U.get(k) or {}).get("tickers", [])]
    return list(dict.fromkeys(out + list(R.get("stocks") or [])))

def closes(con, asset, days=400):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    return [r[0] for r in con.execute("SELECT close FROM daily_closes WHERE asset=? AND d>=? AND close>0 ORDER BY d", (asset, since))]

def features(px):
    n = len(px)
    if n < 30: return None
    p = px[-1]
    ret = lambda k: (p / px[-1 - k] - 1) if n > k else None
    r1, r3, r6 = ret(21), ret(63), ret(126)
    if r6 is None and n >= 100: r6 = p / px[0] - 1          # most of 6 months available
    dr = [b / a - 1 for a, b in zip(px[-64:], px[-63:])]
    mu = sum(dr) / len(dr); vol = math.sqrt(sum((x - mu) ** 2 for x in dr) / max(len(dr) - 1, 1)) or 1e-9
    w = [(0.4, r6, 126), (0.4, r3, 63), (0.2, r1, 21)]
    w = [(wt, r, k) for wt, r, k in w if r is not None]
    mom = sum(wt * clip(r / (vol * math.sqrt(k)), -3, 3) for wt, r, k in w) / sum(wt for wt, _, _ in w)
    sma50 = sum(px[-50:]) / len(px[-50:])
    sma50_prev = sum(px[-70:-20]) / len(px[-70:-20]) if n >= 70 else None
    hi = max(px[-126:]); dd = p / hi - 1
    q = (0.4 if p > sma50 else -0.4) + ((0.3 if sma50 > sma50_prev else -0.3) if sma50_prev else 0) \
        + (0.3 if dd > -0.10 else 0 if dd > -0.25 else -0.3)
    return dict(close=p, n=n, r1=r1, r3=r3, r6=r6, vol=vol, mom=clip(mom / 3), above50=p > sma50,
                rising50=bool(sma50_prev and sma50 > sma50_prev), dd=dd, quality=round(q, 3))

def sentiment(con, asset, days=7):
    rows = con.execute("SELECT score, confidence FROM sentiment WHERE asset=? AND ts>=?",
                       (asset, (datetime.now(timezone.utc) - timedelta(days=days)).isoformat())).fetchall()
    w = sum(r[1] or 0 for r in rows)
    return (sum(r[0] * (r[1] or 0) for r in rows) / w, len(rows)) if w > 0 else (None, 0)

def why(f, s):
    pc = lambda r: "n/a" if r is None else f"{r*100:+.0f}%"
    parts = [f"6m {pc(f['r6'])}", f"3m {pc(f['r3'])}", f"1m {pc(f['r1'])}",
             "above 50-day avg" if f["above50"] else "below 50-day avg",
             "50-day avg rising" if f["rising50"] else "50-day avg flat/falling",
             f"{-f['dd']*100:.0f}% below 6m high" if f["dd"] < -0.005 else "at 6m high", f"vol {f['vol']*100:.1f}%/day"]
    if s is not None: parts.append(f"sentiment {s:+.2f}")
    return ", ".join(parts)

def _score(con, s, tk, tl, W, excluded):
    """One name on the shared CORE score. None if there is not enough daily history."""
    f = features(closes(con, s))
    if not f: return None
    sent, sn = sentiment(con, s)
    if sent is None: comp = (W["momentum"] * f["mom"] + W["quality"] * f["quality"]) / (W["momentum"] + W["quality"])
    else: comp = W["momentum"] * f["mom"] + W["quality"] * f["quality"] + W["sentiment"] * clip(sent)
    lead = f["r3"] if f["r3"] is not None else f["r6"]
    bad = ("excluded" if excluded else "3m return <= 0" if lead is None or lead <= 0 else
           "below 50-day avg" if not f["above50"] else "momentum <= 0" if f["mom"] <= 0 else None)
    return dict(theme=tk, theme_label=tl, score=round(comp, 3), mom=round(f["mom"], 3), quality=f["quality"],
                sent=None if sent is None else round(sent, 3), sent_n=sn, r1=_r(f["r1"]), r3=_r(f["r3"]), r6=_r(f["r6"]),
                vol=round(f["vol"], 4), dd=round(f["dd"], 4), above50=f["above50"], rising50=f["rising50"],
                close=f["close"], n=f["n"], eligible=bad is None, why=why(f, sent), not_eligible=bad)

def rank(con, c):
    """Score every stock-universe ticker that has daily data. Returns {ticker: dict(...)} incl. eligibility + reason."""
    K = c["core"]; W = K.get("score_weights") or dict(momentum=0.55, quality=0.30, sentiment=0.15)
    ex = set(K.get("stock_exclude") or []); out = {}
    for s, (tk, tl) in themes(c).items():
        row = _score(con, s, tk, tl, W, s in ex)
        if row: out[s] = row
    return out

def crypto_themes(c):
    """{ticker: (theme_key, label)} for CORE crypto candidates (core.crypto_universe). Not the fixed targets_pct coins."""
    out = {}
    for k, v in (c["core"].get("crypto_universe") or {}).items():
        for s in v.get("tickers", []): out.setdefault(s, (k, v.get("label", k)))
    return out

def crypto_ids(c):
    """{ticker: coingecko id} for the crypto candidate universe."""
    out = {}
    for v in (c["core"].get("crypto_universe") or {}).values():
        ids = v.get("ids") or {}
        for s in v.get("tickers", []):
            if ids.get(s): out[s] = ids[s]
    return out

def rank_crypto(con, c):
    """Same score as rank(), for the crypto-account candidate universe only. Stocks are not included."""
    K = c["core"]; W = K.get("score_weights") or dict(momentum=0.55, quality=0.30, sentiment=0.15)
    out = {}
    for s, (tk, tl) in crypto_themes(c).items():
        row = _score(con, s, tk, tl, W, False)
        if row: out[s] = dict(row, acct="crypto")
    return out

def _r(x): return None if x is None else round(x, 4)

def n_picks(c, stock_equity, max_picks=None):
    """Number of CORE stock picks for a CORE *stocks-account* equity of stock_equity (v6: picks live in the Stocks account)."""
    K = c["core"]; budget = stock_equity * float(K["stocks_pct"]) / 100
    per = max(float(K.get("min_pick_gbp", 8)), float(c["risk"]["min_trade_size"]))
    n = int(budget // per)
    return max(int(K.get("stock_picks_min", 1)), min(int(max_picks or K.get("stock_picks_max", 8)), n)) if n >= 1 else 0

def select(c, ranked, n, max_per_theme=None):
    mpt = int(max_per_theme or c["core"].get("max_per_theme", 2)); per, picks = {}, []
    for s in sorted([s for s, r in ranked.items() if r["eligible"]], key=lambda s: -ranked[s]["score"]):
        if len(picks) >= n: break
        t = ranked[s]["theme"]
        if per.get(t, 0) >= mpt: continue
        per[t] = per.get(t, 0) + 1; picks.append(s)
    return picks

def crypto_slot_weight(c):
    """Share of Core crypto money for one ranked candidate: one slot of the same size the stock picker uses
    for this account's starting Core money (1 / n_picks), never more than max_pick_pct. Not a config percentage."""
    from common import funding
    slots = n_picks(c, float(funding(c)["core"]["crypto"])) or 1
    cap = float(c["core"].get("max_pick_pct", 100)) / 100
    return min(cap, 1.0 / slots)

def crypto_weights(c, crypto_picks=None):
    """CORE crypto targets as fractions of the Core crypto-account money.
    The fixed targets_pct coins keep their ratios. A candidate from the weekly ranking (crypto_picks) is not in
    targets_pct: it takes one normal slot, and the fixed coins share what is left. No picks -> targets_pct only."""
    t = {a: float(v) for a, v in c["core"]["targets_pct"].items()}; tot = sum(t.values()) or 1
    base = {a: v / tot for a, v in t.items()}
    picks = [a for a in (crypto_picks or []) if a not in base]
    if not picks: return base
    per = crypto_slot_weight(c)
    if per * len(picks) > 1: per = 1.0 / len(picks)
    scale = 1.0 - per * len(picks)
    out = {a: w * scale for a, w in base.items()}
    out.update({a: per for a in picks})
    return out

def pick_weights(c, picks):
    """Target weight per pick as a fraction of the CORE *stocks-account* money: stocks_pct split equally, capped at max_pick_pct."""
    if not picks: return {}
    K = c["core"]; w = float(K["stocks_pct"]) / 100 / len(picks)
    cap = float(K.get("max_pick_pct", 100)) / 100
    return {s: min(w, cap) for s in picks}

def core_split(c):
    """Share of CORE money per account from accounts.funding.core (e.g. {'crypto': 0.5, 'stocks': 0.5})."""
    from common import funding
    f = funding(c)["core"]; tot = sum(f.values()) or 1
    return {a: v / tot for a, v in f.items()}

def plans(c, ranked, acct_eq, holdings, acct_cash, crypto_picks=None):
    """Display-only top-up plans (v6: per account). acct_eq / acct_cash = CORE equity / cash per account
    {'crypto': .., 'stocks': ..}; holdings = {asset: current CORE value £}. Each top-up is deposited into the two accounts
    by the CORE funding ratio (50/50); the crypto deposit (+ Crypto-account Core cash) fills the crypto target gaps, the
    stocks deposit (+ Stocks-account Core cash) buys the stock picks (more picks for a bigger pot). Never sells."""
    K = c["core"]; mint = float(c["risk"]["min_trade_size"]); out = []; sh = core_split(c)
    for tier in c.get("plans") or []:
        add = float(tier["topup"]); dep = {a: round(add * sh[a], 2) for a in sh}
        E = {a: acct_eq.get(a, 0.0) + dep[a] for a in sh}
        n = n_picks(c, E["stocks"], tier.get("max_picks")); picks = select(c, ranked, n, tier.get("max_per_theme"))
        cth = crypto_themes(c)
        tg = {a: ("crypto", w) for a, w in crypto_weights(c, crypto_picks).items()}
        tg.update({s: ("stocks", w) for s, w in pick_weights(c, picks).items()})
        rows = []
        for a, (ac, w) in tg.items():
            have = holdings.get(a, 0.0); gap = max(0.0, w * E[ac] - have)
            theme = (ranked.get(a) or {}).get("theme_label") or (cth.get(a) or (None, None))[1]
            rows.append(dict(a=a, acct=ac, kind="stock" if ac == "stocks" else "crypto", theme=theme,
                             score=(ranked.get(a) or {}).get("score"), target_pct=round(w * 100, 2), target=round(w * E[ac], 2),
                             have=round(have, 2), gap=gap))
        acc = {}
        for ac in sh:
            avail = dep[ac] + max(0.0, acct_cash.get(ac, 0.0)); need = sum(r["gap"] for r in rows if r["acct"] == ac)
            f = min(1.0, avail / need) if need > 0 else 0
            for r in rows:
                if r["acct"] != ac: continue
                b = r["gap"] * f; r["buy"] = round(b, 2) if b >= mint else 0.0; r["skip"] = 0 < b < mint
            acc[ac] = dict(deposit=dep[ac], cash=round(max(0.0, acct_cash.get(ac, 0.0)), 2), deploy=round(avail, 2), after=round(E[ac], 2),
                           buy_total=round(sum(r["buy"] for r in rows if r["acct"] == ac), 2))
        for r in rows: r.pop("gap", None)
        out.append(dict(topup=add, core_after=round(sum(E.values()), 2), deploy=round(sum(a["deploy"] for a in acc.values()), 2),
                        accounts=acc, picks=picks, n_picks=len(picks),
                        themes=sorted({ranked[s]["theme_label"] for s in picks}), buy_total=round(sum(r["buy"] for r in rows), 2),
                        stock_pct=float(K["stocks_pct"]), per_pick=round(pick_weights(c, picks)[picks[0]] * E["stocks"], 2) if picks else 0,
                        rows=sorted(rows, key=lambda r: (r["acct"], -r["buy"]))))
    return out
