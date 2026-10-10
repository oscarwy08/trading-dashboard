"""CORE stock selection (no network): ranks the xStock universe (config stocks.universe, grouped by theme) from the daily
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

def rank(con, c):
    """Score every universe ticker that has daily data. Returns {ticker: dict(...)} incl. eligibility + reason."""
    K = c["core"]; W = K.get("score_weights") or dict(momentum=0.55, quality=0.30, sentiment=0.15)
    ex = set(K.get("stock_exclude") or []); th = themes(c); out = {}
    for s, (tk, tl) in th.items():
        f = features(closes(con, s))
        if not f: continue
        sent, sn = sentiment(con, s)
        if sent is None: comp = (W["momentum"] * f["mom"] + W["quality"] * f["quality"]) / (W["momentum"] + W["quality"])
        else: comp = W["momentum"] * f["mom"] + W["quality"] * f["quality"] + W["sentiment"] * clip(sent)
        lead = f["r3"] if f["r3"] is not None else f["r6"]
        bad = ("excluded" if s in ex else "3m return <= 0" if lead is None or lead <= 0 else
               "below 50-day avg" if not f["above50"] else "momentum <= 0" if f["mom"] <= 0 else None)
        out[s] = dict(theme=tk, theme_label=tl, score=round(comp, 3), mom=round(f["mom"], 3), quality=f["quality"],
                      sent=None if sent is None else round(sent, 3), sent_n=sn, r1=_r(f["r1"]), r3=_r(f["r3"]), r6=_r(f["r6"]),
                      vol=round(f["vol"], 4), dd=round(f["dd"], 4), above50=f["above50"], rising50=f["rising50"],
                      close=f["close"], n=f["n"], eligible=bad is None, why=why(f, sent), not_eligible=bad)
    return out

def _r(x): return None if x is None else round(x, 4)

def n_picks(c, core_equity, max_picks=None):
    K = c["core"]; budget = core_equity * float(K["stocks_pct"]) / 100
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

def pick_weights(c, picks):
    """Target weight (fraction of CORE equity) per pick: stocks_pct split equally, capped at max_pick_pct."""
    if not picks: return {}
    K = c["core"]; w = float(K["stocks_pct"]) / 100 / len(picks)
    cap = float(K.get("max_pick_pct", 100)) / 100
    return {s: min(w, cap) for s in picks}

def plans(c, ranked, core_equity, holdings, core_cash):
    """Display-only top-up plans. holdings = {asset: current CORE value £}. The top-up goes into the Core pot; target
    weights are the same as now (crypto targets + stocks split equally over more picks), buys fill the gaps to target,
    funded by the top-up plus Core cash not yet invested; nothing is sold."""
    K = c["core"]; mint = float(c["risk"]["min_trade_size"]); out = []
    for tier in c.get("plans") or []:
        add = float(tier["topup"]); E = core_equity + add
        n = n_picks(c, E, tier.get("max_picks")); picks = select(c, ranked, n, tier.get("max_per_theme"))
        tg = {a: float(v) / 100 for a, v in K["targets_pct"].items()}
        tg.update({s: float(K["stocks_pct"]) / 100 / len(picks) for s in picks} if picks else {})
        rows = []
        for a, w in tg.items():
            have = holdings.get(a, 0.0); gap = max(0.0, w * E - have)
            rows.append(dict(a=a, kind="stock" if a in ranked else "crypto", theme=(ranked.get(a) or {}).get("theme_label"),
                             score=(ranked.get(a) or {}).get("score"), target_pct=round(w * 100, 2), target=round(w * E, 2),
                             have=round(have, 2), gap=gap))
        avail = add + max(0.0, core_cash); need = sum(r["gap"] for r in rows)
        f = min(1.0, avail / need) if need > 0 else 0
        for r in rows:
            b = r.pop("gap") * f; r["buy"] = round(b, 2) if b >= mint else 0.0; r["skip"] = 0 < b < mint
        # holdings that would no longer be targets are kept (plans never sell)
        out.append(dict(topup=add, core_after=round(E, 2), deploy=round(avail, 2), picks=picks, n_picks=len(picks),
                        themes=sorted({ranked[s]["theme_label"] for s in picks}), buy_total=round(sum(r["buy"] for r in rows), 2),
                        stock_pct=float(K["stocks_pct"]), per_pick=round(float(K["stocks_pct"]) / 100 / len(picks) * E, 2) if picks else 0,
                        rows=sorted(rows, key=lambda r: -r["buy"])))
    return out
