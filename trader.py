"""Paper trader with two sleeves (NO real orders). Account in GBP: USD source prices are converted with the latest
stored USD->GBP rate (table fx).
  CORE (long holds): fixed crypto target weights (MON/WLD/XRP/BTC/ETH) + stocks picked weekly from the themed xStock
        universe by composite score (stockrank.py; max per theme, count fitted to the min trade size), positions built in
        tranches (~1/day/asset, buy-the-dip vs 24h average, delayed on strongly negative sentiment), weekly rebalance with
        a drift band, wide stop-loss from average cost, no take-profit.
  DAY  (intraday): momentum + sentiment signal, tight stop / take-profit, max 3 positions, entry window, force-flat at
        22:00 UK (first cycle at/after 22:00, or any cycle before 08:00 / on a later UK day), daily loss cap on the sleeve.
  RISK (speculative, v4): small/mid-cap crypto (CoinGecko top-300 breakout screen, liquidity/market-cap floors, Monad
        ecosystem tokens with a KyberSwap liquidity check) + speculative stocks (v5: quantum, British early-stage, rare
        earths, global small caps, photonics/robotics small caps); breakout score +
        sentiment, max 5 x 20% positions, 12% stop, trailing take-profit (10% from peak after +15%), 7-day max hold,
        30% sleeve drawdown cap (no new entries until RISK_RESUME is created).
Each sleeve has its own cash and positions; the same asset can be held in both.
v6 (2026-10-10): two accounts - crypto (Monad wallet) and stocks (Trading 212). Cash is kept per pot x account (table
acct_cash); a crypto trade only uses/returns the pot's crypto cash, a stock trade only its stocks cash (buy() refuses to
overdraw an account). CORE targets are % of the matching account's slice of the Core pot. Nothing is moved between
accounts automatically (manual: transfer_accounts.py); the only automatic move is the one-off pot settlement INSIDE each
account that completes the v6 split (state account_split.pending, see migrate_v6_accounts.py).
v5 (2026-10-10): stocks are normal exchange tickers (Yahoo prices; US, London .L incl. AIM, ASX .AX) and are only traded
while their exchange is open (markets.py): CORE tranches/stops wait for the open, CORE rebalance orders decided while a
market is closed go to state 'order_queue' and fill at the first open cycle, DAY stock entries stop 30 min before the
close and DAY stock positions are flattened 15 min before it, RISK stock entries/exits only during the session.
Stock costs are broker-like (cost_pct: spread, FX fee on non-GBP, UK stamp duty on non-AIM London buys; no gas).
Costs per trade: fee+slippage (risk.fee_slippage_pct, in the fill price) + simulated Monad gas (state 'gas' from the
collector, or gas.fallback_gbp) paid from cash on top and recorded in trades.gas."""
import json
from datetime import datetime, timedelta, timezone, time as dtime
from common import ACCOUNTS, KILL, LON, RISK_RESUME, SLEEVES, acct_of, acct_start, cfg, db, funding, iso, log, move_cash, now, sync_pot_cash, usd_gbp
import markets, riskpot, stockrank

L = log("trader")
ACCT = {"crypto": "crypto-account", "stocks": "stocks-account"}
clip = lambda x, lo=-1.0, hi=1.0: max(lo, min(hi, x))

def ema(xs, n):
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]: e = x * k + e * (1 - k)
    return e

def rsi(xs, n=14):
    d = [b - a for a, b in zip(xs, xs[1:])][-n:]
    up, dn = sum(x for x in d if x > 0), -sum(x for x in d if x < 0)
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)

def history(con, asset, hours=72):
    since, until = iso(now() - timedelta(hours=hours)), iso(now() + timedelta(seconds=1))
    return [(datetime.fromisoformat(r[0]), r[1]) for r in con.execute(
        "SELECT ts, price FROM prices WHERE asset=? AND ts>=? AND ts<=? AND source!='kyberswap' ORDER BY ts",
        (asset, since, until))]

def momentum(pts, min_h):
    """Returns (score, info) or (None, reason)."""
    if not pts or (pts[-1][0] - pts[0][0]).total_seconds() < min_h * 3600:
        return None, "insufficient history"
    t, p = pts[-1]
    def ret(h):
        old = [x for ts, x in pts if ts <= t - timedelta(hours=h)]
        return p / old[-1] - 1 if old else None
    hourly = list({ts.strftime("%Y%m%d%H"): x for ts, x in pts}.values())
    r4, r24 = ret(4), ret(24)
    cross = 1 if ema(hourly, 6) > ema(hourly, 24) else -1
    rs = rsi(hourly) if len(hourly) > 14 else 50
    rsi_term = -1 if rs > 75 else 0.5 if rs < 25 else 0
    score = 0.35 * clip((r4 or 0) / 0.03) + 0.35 * clip((r24 if r24 is not None else r4 or 0) / 0.08) \
        + 0.2 * cross + 0.1 * rsi_term
    fmt = lambda r: "n/a" if r is None else f"{r*100:+.1f}%"
    return round(score, 3), f"4h {fmt(r4)}, 24h {fmt(r24)}, EMA6/24 {'up' if cross > 0 else 'down'}, RSI {rs:.0f}"

def avg24(pts):
    t = pts[-1][0] if pts else None
    xs = [p for ts, p in pts if ts >= t - timedelta(hours=24)] if pts else []
    return sum(xs) / len(xs) if xs else None

def latest_prices(con, max_age_min, rate):
    """Latest prices converted to GBP (price) with the raw USD kept as price_usd."""
    rows = con.execute("""SELECT asset, klass, price, MAX(ts) ts FROM prices
                          WHERE klass IN ('crypto','stock') AND source!='backfill' AND ts<=? GROUP BY asset""",
                       (iso(now() + timedelta(seconds=1)),)).fetchall()
    cutoff = iso(now() - timedelta(minutes=max_age_min))
    return {r["asset"]: dict(klass=r["klass"], price=r["price"] * rate, price_usd=r["price"], fresh=r["ts"] >= cutoff,
                             ts=r["ts"]) for r in rows}

def sentiment(con, asset, max_age_h):
    return con.execute("SELECT score, confidence, ts FROM sentiment WHERE asset=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                       (asset, iso(now() - timedelta(hours=max_age_h)), iso(now() + timedelta(seconds=1)))).fetchone()

def hm(s):
    h, m = str(s).split(":"); return int(h) * 60 + int(m)

def sget(con, key, default=None):
    r = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if not r: return default
    try: return json.loads(r[0])
    except Exception: return r[0]

def sset(con, key, val):
    con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(val)))

def pick_stocks(con, c, stock_equity):
    """CORE stock picks from the themed universe: composite score (momentum / trend quality / sentiment), eligible only,
    max core.max_per_theme per theme, count = CORE stocks-account money / core.min_pick_gbp (clamped). Returns (picks, info, source)."""
    rk = stockrank.rank(con, c)
    n = stockrank.n_picks(c, stock_equity)
    picks = stockrank.select(c, rk, n)
    info = {s: rk[s] for s in picks}
    return picks, info, f"composite score, {sum(1 for r in rk.values() if r['eligible'])}/{len(rk)} eligible, {n} slots for £{stock_equity:.0f} of Core stocks money"

def core_targets(c, picks):
    """{asset: (account, weight within the CORE money of that account)} - crypto targets in the Crypto account,
    stock picks in the Stocks account (v6)."""
    t = {a: ("crypto", w) for a, w in stockrank.crypto_weights(c).items()}
    t.update({s: ("stocks", w) for s, w in stockrank.pick_weights(c, picks).items()})
    return t

def cost_parts(c, asset, klass, side):
    """Per-side cost components as fractions of the price. crypto: {'fee'} (fee + slippage; gas is separate);
    stocks: {'spread', 'fx', 'stamp'} (Trading 212-like)."""
    if klass != "stock": return {"fee": float(c["risk"]["fee_slippage_pct"]) / 100}
    C = c["stocks"].get("costs") or {}; ex = markets.exchange(asset)
    return {"spread": float(C.get("spread_pct", 0.1)) / 100,
            "fx": float(C.get("fx_fee_pct", 0.15)) / 100 if ex != "LSE" else 0.0,
            "stamp": float(C.get("uk_stamp_duty_pct", 0.5)) / 100 if side == "BUY" and ex == "LSE" and asset not in set(C.get("stamp_exempt") or []) else 0.0}

def cost_pct(c, asset, klass, side):
    """Per-side trading cost as a fraction of the price (applied in the fill price).
    crypto: risk.fee_slippage_pct (+ simulated Monad gas on top, see gas_gbp).
    stocks (v5, broker-like, Trading 212-style): stocks.costs.spread_pct (no commission) + fx_fee_pct for non-GBP listings
    (GBP account buying USD/AUD shares; both sides) + UK stamp duty (SDRT) on London BUYS unless AIM/ETF (stamp_exempt)."""
    return sum(cost_parts(c, asset, klass, side).values())

def gas_gbp(con, c):
    """Simulated Monad gas per trade in GBP: latest collector estimate if fresh, else the configured fallback."""
    g = c.get("gas") or {}; d = sget(con, "gas")
    try:
        if d and datetime.fromisoformat(d["ts"]) >= now() - timedelta(hours=float(g.get("max_age_h", 6))) and d["gbp"] >= 0:
            return float(d["gbp"])
    except Exception: pass
    return float(g.get("fallback_gbp", 0.001))

def init_core(con, c):
    """Called on account reset: pick stocks, mark every target asset as 'building', no rebalance until next week."""
    picks, info, src = pick_stocks(con, c, funding(c)["core"]["stocks"])
    sset(con, "core_stocks", dict(picks=picks, info=info, source=src, ts=iso()))
    sset(con, "core_plan", {a: dict(status="building", n=0, last_day=None, day_n=0) for a in core_targets(c, picks)})
    sset(con, "core_last_rebalance", iso())
    return picks

def main():
    c, con = cfg(), db()
    t = now(); ts = iso(t); lt = t.astimezone(LON); day = lt.date().isoformat(); mins = lt.hour * 60 + lt.minute
    st, rk, K, D = c["strategy"], c["risk"], c["core"], c["day"]
    f, mint = rk["fee_slippage_pct"] / 100, float(rk["min_trade_size"])
    gas = gas_gbp(con, c)
    out = []

    def sig(sleeve, asset, m, s, comb, action, reason):
        con.execute("INSERT INTO signals(ts,sleeve,asset,momentum,sentiment,combined,action,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (ts, sleeve, asset, m, s, comb, action, reason))
        if action not in ("HOLD", "SKIP"): out.append(f"[{sleeve}] {action} {asset}: {reason}")

    if KILL.exists():
        sig("*", "*", None, None, None, "HALT", "KILL file present"); con.commit()
        print("[trader] KILL file present - halted, no actions"); return
    fx = usd_gbp(con, c["fx"]["max_age_h"])
    if not fx:
        sig("*", "*", None, None, None, "HALT", "no USD->GBP rate stored yet"); con.commit()
        print("[trader] no USD->GBP rate available yet - no actions"); return
    rate, fx_stale = fx[0], fx[3]
    if fx_stale: L.warning("USD->GBP rate from %s is older than %sh - new buys blocked", fx[1], c["fx"]["max_age_h"])
    if not con.execute("SELECT 1 FROM acct_cash").fetchone():
        if con.execute("SELECT 1 FROM cash").fetchone() or con.execute("SELECT 1 FROM positions").fetchone():
            import migrate_v6_accounts; migrate_v6_accounts.migrate(con)        # existing single-cash account -> split (v6)
        else:                                                                  # fresh account: config funding per pot x account
            con.executemany("INSERT INTO acct_cash VALUES(?,?,?)", [(k, a, v) for k, d in funding(c).items() for a, v in d.items()])
    for s_ in SLEEVES:
        for a_ in ACCOUNTS: con.execute("INSERT OR IGNORE INTO acct_cash VALUES(?,?,0)", (s_, a_))   # new cells get money only via TRANSFER
    if sget(con, "core_plan") is None: init_core(con, c)
    px = latest_prices(con, rk["max_price_age_min"], rate)
    cash = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}   # (pot, account) -> £
    pos = {(r["sleeve"], r["asset"]): dict(r) for r in con.execute("SELECT * FROM positions")}

    def gas_of(klass): return gas if klass == "crypto" else 0.0     # stocks: no network fee (broker trade)
    def fsell(a):
        k = (px.get(a) or {}).get("klass") or next((p["klass"] for (sl, x), p in pos.items() if x == a), "crypto")
        return cost_pct(c, a, k, "SELL")
    def klass_of(a):
        return (px.get(a) or {}).get("klass") or next((p["klass"] for (sl, x), p in pos.items() if x == a), "crypto")
    def mopen(a):          # crypto trades 24/7; stocks only during their exchange's regular session (markets.py)
        return klass_of(a) != "stock" or markets.is_open(a, t)
    def mclose_in(a):      # minutes to the close of the current session (None = closed); crypto: large
        return 1e9 if klass_of(a) != "stock" else markets.mins_to_close(a, t)
    def acc(a): return acct_of(klass_of(a))
    def cash_for(sleeve, a): return cash[(sleeve, acc(a))]          # the ONLY cash a trade in asset a may use
    def acash(sleeve, a): return f"{sleeve} {ACCT[acc(a)]} cash £{cash_for(sleeve, a):.2f}"
    def closed_msg(a):
        no = markets.next_open(a, t)
        return f"market closed ({markets.EXCH[markets.exchange(a)]['label']}; opens {no.astimezone(LON):%a %H:%M} UK)" if no else "market closed"

    def trade(sleeve, asset, klass, side, qty, price, reason, pnl=None):
        fp = cost_pct(c, asset, klass, side)
        fill = price * (1 + fp if side == "BUY" else 1 - fp)
        con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason,price_usd,usd_gbp,gas) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, sleeve, asset, klass, side, qty, price, fill, qty * fill, qty * price * fp, pnl, reason, price / rate, rate, gas_of(klass)))
        return fill

    def buy(sleeve, a, value, reason):
        """Spends value on the asset (fill incl. fees/slippage/FX/stamp duty) + gas on top (crypto only); all go into the cost basis."""
        q = px[a]; fp = cost_pct(c, a, q["klass"], "BUY")
        fill = trade(sleeve, a, q["klass"], "BUY", value / (q["price"] * (1 + fp)), q["price"], reason)
        qty = value / fill; p = pos.get((sleeve, a)); spent = value + gas_of(q["klass"]); ck = (sleeve, acct_of(q["klass"]))
        if spent > cash[ck] + 1e-9: raise RuntimeError(f"{sleeve} {a}: £{spent:.4f} > {ck[1]} cash £{cash[ck]:.4f} (cash never crosses accounts)")
        if p:
            p["qty"] += qty; p["cost"] += spent; p["entry"] = p["cost"] / p["qty"]; p["tranches"] = (p["tranches"] or 1) + 1
            con.execute("UPDATE positions SET qty=?, cost=?, entry=?, tranches=? WHERE sleeve=? AND asset=?",
                        (p["qty"], p["cost"], p["entry"], p["tranches"], sleeve, a))
        else:
            pos[(sleeve, a)] = dict(sleeve=sleeve, asset=a, klass=q["klass"], qty=qty, entry=spent / qty, cost=spent, opened=ts, tranches=1)
            con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", (sleeve, a, q["klass"], qty, spent / qty, spent, ts, 1))
        cash[ck] -= spent

    def sell(sleeve, a, qty, reason, price=None):
        p = pos[(sleeve, a)]; price = price or px[a]["price"]; qty = min(qty, p["qty"])
        proceeds = qty * price * (1 - cost_pct(c, a, p["klass"], "SELL")) - gas_of(p["klass"]); part = p["cost"] * qty / p["qty"]; pnl = proceeds - part
        trade(sleeve, a, p["klass"], "SELL", qty, price, f"{reason}; P&L £{pnl:+.2f}", pnl)
        cash[(sleeve, acct_of(p["klass"]))] += proceeds
        if qty >= p["qty"] * (1 - 1e-9):
            con.execute("DELETE FROM positions WHERE sleeve=? AND asset=?", (sleeve, a)); del pos[(sleeve, a)]
        else:
            p["qty"] -= qty; p["cost"] -= part
            con.execute("UPDATE positions SET qty=?, cost=? WHERE sleeve=? AND asset=?", (p["qty"], p["cost"], sleeve, a))
        return pnl

    def val(p): q = px.get(p["asset"]); return p["qty"] * q["price"] if q else p["cost"]
    def acct_eq(s, ac): return cash[(s, ac)] + sum(val(p) for (sl, _), p in pos.items() if sl == s and acct_of(p["klass"]) == ac)
    def sleeve_eq(s): return sum(acct_eq(s, ac) for ac in ACCOUNTS)

    # ---- score every asset (DAY signal; sentiment also used by CORE tranche timing) ----
    scores, hist = {}, {}
    for a, p in px.items():
        hist[a] = history(con, a)
        m, info = momentum(hist[a], st["min_history_h"])
        s = sentiment(con, a, st["sentiment_max_age_h"])
        if m is None: scores[a] = (None, None, None, info); continue
        w = st["sentiment_weight"] * s["confidence"] if s else 0
        comb = round((1 - w) * m + w * (s["score"] if s else 0), 3)
        scores[a] = (m, s["score"] if s else None, comb,
                     info + (f", sentiment {s['score']:+.2f} (conf {s['confidence']:.2f})" if s else ", no fresh sentiment"))

    risk_only = set(sget(con, "risk_only") or []) | set(sget(con, "risk_only_stocks") or [])   # priced only for the RISK pot
    RP = c.get("riskpot") or {}

    # ================= RISK exits: stop / trailing take-profit / max hold / sentiment =================
    peaks = sget(con, "risk_peaks") or {}
    for (sl, a), p in list(pos.items()):
        if sl != "risk": continue
        q = px.get(a)
        if not mopen(a): sig("risk", a, None, None, None, "HOLD", f"{closed_msg(a)} - exits checked when it opens"); continue
        if not q or not q["fresh"]: sig("risk", a, None, None, None, "HOLD", "stale/missing price, cannot evaluate exit"); continue
        f = fsell(a)
        pk = peaks[a] = max(float(peaks.get(a) or 0), q["price"])
        chg = q["price"] * (1 - f) / p["entry"] - 1; pk_gain = pk * (1 - f) / p["entry"] - 1
        held_d = (t - datetime.fromisoformat(p["opened"])).total_seconds() / 86400
        se = sentiment(con, a, st["sentiment_max_age_h"])
        trail_on = pk_gain >= RP["trail_after_pct"] / 100
        reason = (f"risk stop-loss {chg*100:.1f}% <= -{RP['stop_loss_pct']}%" if chg <= -RP["stop_loss_pct"] / 100 else
                  f"trailing take-profit: {(q['price']/pk-1)*100:.1f}% from peak (peak +{pk_gain*100:.1f}%), now {chg*100:+.1f}%"
                  if trail_on and q["price"] <= pk * (1 - RP["trail_pct"] / 100) else
                  f"max hold {RP['max_hold_days']} days reached ({held_d:.1f} d), {chg*100:+.1f}%" if held_d >= RP["max_hold_days"] else
                  f"sentiment exit {se['score']:+.2f} (conf {se['confidence']:.2f})"
                  if se and se["score"] <= RP["sentiment_exit"] and se["confidence"] >= 0.6 else None)
        if reason:
            pnl = sell("risk", a, p["qty"], reason); peaks.pop(a, None)
            sset(con, f"risk_exit:{a}", ts); sig("risk", a, None, None, None, "SELL", f"{reason}; P&L £{pnl:+.2f}")
        else:
            sig("risk", a, None, None, None, "HOLD", f"open {chg*100:+.1f}%, peak +{pk_gain*100:.1f}%, {held_d:.1f} d"
                + (f", trailing stop £{pk*(1-RP['trail_pct']/100):.5g}" if trail_on else ""))

    # ================= DAY exits: flatten window / stop / take-profit / signal =================
    e0, e1, flat = hm(D["entry_start_uk"]), hm(D["entry_cutoff_uk"]), hm(D["flatten_uk"])
    flat_now = mins >= flat or mins < e0
    for (sl, a), p in list(pos.items()):
        if sl != "day": continue
        q = px.get(a); m, s, comb, info = scores.get(a, (None, None, None, ""))
        opened_day = datetime.fromisoformat(p["opened"]).astimezone(LON).date().isoformat()
        if p["klass"] == "stock" and (mclose_in(a) is None or mclose_in(a) <= int(D.get("stock_flatten_before_close_min", 15))):
            if not q: sig("day", a, m, s, comb, "HOLD", "flatten due but no price at all"); continue
            cl = mclose_in(a)
            why = (f"flatten {int(cl)} min before the {markets.exchange(a)} close (flat overnight)" if cl is not None else
                   "flatten: market already closed - at the last (closing) price")
            pnl = sell("day", a, p["qty"], why); sig("day", a, m, s, comb, "SELL", f"{why}; P&L £{pnl:+.2f}"); continue
        if flat_now or opened_day != day:
            if not q: sig("day", a, m, s, comb, "HOLD", "flatten due but no price at all"); continue
            why = (f"flatten {D['flatten_uk']} UK (flat overnight)" if mins >= flat else
                   f"flatten: overnight/before {D['entry_start_uk']} UK" if mins < e0 else "flatten: carried from previous UK day")
            pnl = sell("day", a, p["qty"], why + ("" if q["fresh"] else " [last price, stale]"))
            sig("day", a, m, s, comb, "SELL", f"{why}; P&L £{pnl:+.2f}"); continue
        if not q or not q["fresh"]: sig("day", a, None, None, None, "HOLD", "stale/missing price, cannot evaluate exit"); continue
        chg = q["price"] * (1 - fsell(a)) / p["entry"] - 1
        reason = (f"stop-loss {chg*100:.1f}% <= -{D['stop_loss_pct']}%" if chg <= -D["stop_loss_pct"] / 100 else
                  f"take-profit {chg*100:.1f}% >= {D['take_profit_pct']}%" if chg >= D["take_profit_pct"] / 100 else
                  f"signal {comb} <= {st['sell_threshold']} ({info})" if comb is not None and comb <= st["sell_threshold"] else None)
        if reason:
            pnl = sell("day", a, p["qty"], reason); sig("day", a, m, s, comb, "SELL", f"{reason}; P&L £{pnl:+.2f}")
        else:
            sig("day", a, m, s, comb, "HOLD", f"open day position {chg*100:+.1f}%; {info}")

    # ================= v6 one-off pot settlement inside each account (completes the account split) =================
    # migrate_v6_accounts.py could not reach every pot x account target at once (e.g. DAY held £40 of crypto); the pending
    # legs move cash between POTS within ONE account (crypto->crypto, stocks->stocks, never between accounts) as soon as
    # every leg is covered by cash - all legs together, so each pot total stays unchanged.
    acs = sget(con, "account_split") or {}
    if acs.get("pending") and all(cash[(l["from"], l["account"])] >= l["amount"] - 1e-9 for l in acs["pending"]):
        # move_cash checks the DB cells -> first write the in-memory cash (sales in this cycle) for the involved cells
        for k in {(l[x], l["account"]) for l in acs["pending"] for x in ("from", "to")}:
            con.execute("INSERT OR REPLACE INTO acct_cash VALUES(?,?,?)", (*k, cash[k]))
        for l in acs["pending"]:
            move_cash(con, (l["from"], l["account"]), (l["to"], l["account"]), l["amount"],
                      f"v6 account split settlement: {ACCT[l['account']]} cash {l['from'].upper()} -> {l['to'].upper()} "
                      f"(pot targets {acs['targets'][l['to']][l['account']]:.0f}/{acs['targets'][l['from']][l['account']]:.0f})", "settle")
            cash[(l["from"], l["account"])] -= l["amount"]; cash[(l["to"], l["account"])] += l["amount"]
            out.append(f"[{l['account']}] SETTLE £{l['amount']:.2f} {l['from']} -> {l['to']} (same account, pot totals unchanged)")
        acs["settled"] = ts; acs["settled_legs"] = acs.pop("pending"); sset(con, "account_split", acs)

    # ================= CORE =================
    plan = sget(con, "core_plan") or {}
    picks = (sget(con, "core_stocks") or {}).get("picks", [])
    # queued stock orders (rebalance decided while the stock's market was closed) -> execute at the first open cycle
    queue = sget(con, "order_queue") or []; keep = []
    for o in queue:
        a = o["a"]; q = px.get(a)
        if datetime.fromisoformat(o["queued"]) < t - timedelta(days=7):
            out.append(f"[{o['sleeve']}] queued {o['side']} {a} expired (7 days)"); continue
        if not mopen(a) or not q or not q["fresh"] or (o["side"] == "BUY" and fx_stale): keep.append(o); continue
        if o["side"] == "SELL":
            p = pos.get((o["sleeve"], a))
            if not p: continue
            pnl = sell(o["sleeve"], a, p["qty"] * min(1.0, o["frac"]), f"{o['reason']} [queued {o['queued'][:16]}Z, filled at the open]")
            sig(o["sleeve"], a, None, None, None, "SELL", f"queued: {o['reason']}; P&L £{pnl:+.2f}")
        else:
            v = min(o["value"], cash_for(o["sleeve"], a) - gas_of(klass_of(a)))
            if v < mint: out.append(f"[{o['sleeve']}] queued BUY {a} dropped: {acash(o['sleeve'], a)}"); continue
            buy(o["sleeve"], a, v, f"{o['reason']} [queued {o['queued'][:16]}Z, filled at the open]")
            sig(o["sleeve"], a, None, None, None, "BUY", f"queued: {o['reason']} £{v:.2f}")
    queue = keep
    def enqueue(sleeve, a, side, reason, frac=None, value=None):
        queue[:] = [o for o in queue if not (o["sleeve"] == sleeve and o["a"] == a)] + [
            dict(sleeve=sleeve, a=a, side=side, frac=frac, value=value, reason=reason, queued=ts)]
    # stop-loss from average cost
    for (sl, a), p in list(pos.items()):
        if sl != "core": continue
        q = px.get(a)
        if not q or not q["fresh"] or not mopen(a): continue
        chg = q["price"] * (1 - fsell(a)) / p["entry"] - 1
        if chg <= -K["stop_loss_pct"] / 100:
            pnl = sell("core", a, p["qty"], f"core stop-loss {chg*100:.1f}% <= -{K['stop_loss_pct']}% from avg cost")
            plan.setdefault(a, {}).update(status="stopped", stopped=ts)
            sig("core", a, None, None, None, "SELL", f"stop-loss {chg*100:.1f}% from avg cost; P&L £{pnl:+.2f}; "
                                                    "re-enters in tranches after next weekly rebalance")

    # weekly rebalance (first cycle at/after Monday 09:00 UK; caught up later in the week if missed)
    rb_day = lt.date() - timedelta(days=(lt.weekday() - int(K["rebalance_weekday"])) % 7)
    rh, rm = divmod(hm(K["rebalance_time_uk"]), 60)
    rb_at = datetime.combine(rb_day, dtime(rh, rm), LON)
    last_rb = sget(con, "core_last_rebalance"); forced = sget(con, "core_force_rebalance")
    if forced or (t >= rb_at and (not last_rb or datetime.fromisoformat(last_rb) < rb_at)):
        new_picks, info, src = pick_stocks(con, c, acct_eq("core", "stocks"))
        if not new_picks: new_picks = picks       # keep old picks rather than dropping stocks on missing data
        sset(con, "core_stocks", dict(picks=new_picks, info=info, source=src, ts=ts))
        tg = core_targets(c, new_picks); ceq = sleeve_eq("core"); aeq = {ac: acct_eq("core", ac) for ac in ACCOUNTS}
        band = K["drift_band_pp"]; log_rb = []
        for a in list(plan):
            if a not in tg: plan.pop(a)
        for a in tg:
            pl = plan.setdefault(a, dict(status="building", n=0, last_day=None, day_n=0))
            if pl.get("status") == "stopped" or (pl.get("status") == "built" and ("core", a) not in pos):
                pl.update(status="building", n=0)
        # dropped assets (old stock picks) -> sell completely
        for (sl, a), p in list(pos.items()):
            if sl == "core" and a not in tg and a in px:
                if not mopen(a):
                    enqueue("core", a, "SELL", "rebalance: no longer a CORE pick (target 0%)", frac=1.0)
                    log_rb.append(f"SELL {a} all queued ({closed_msg(a)})"); continue
                pnl = sell("core", a, p["qty"], "rebalance: no longer a CORE pick (target 0%)")
                log_rb.append(f"SELL {a} all (dropped pick)"); sig("core", a, None, None, None, "SELL", f"rebalance: dropped pick; P&L £{pnl:+.2f}")
        moves = []
        for a, (ac, w_t) in tg.items():     # weights are % of the CORE money in the asset's own account
            if plan[a]["status"] != "built" or a not in px or (not px[a]["fresh"] and mopen(a)): continue
            v = val(pos[("core", a)]) if ("core", a) in pos else 0; E = aeq[ac]
            w = v / E if E else 0; drift = (w - w_t) * 100
            if abs(drift) > band and abs((w_t - w) * E) >= mint and not mopen(a):
                if w_t < w: enqueue("core", a, "SELL", f"rebalance trim {w*100:.1f}% -> {w_t*100:.0f}% of Core {ac} (band {band}pp)", frac=min(1, (w - w_t) * E / v))
                else: enqueue("core", a, "BUY", f"rebalance top-up {w*100:.1f}% -> {w_t*100:.0f}% of Core {ac} (band {band}pp)", value=(w_t - w) * E)
                log_rb.append(f"{a} {w*100:.1f}% vs {w_t*100:.0f}% of {ac}: order queued ({closed_msg(a)})")
            elif abs(drift) > band and abs((w_t - w) * E) >= mint: moves.append((a, (w_t - w) * E, w, w_t))
            else: log_rb.append(f"{a} {w*100:.1f}% vs {w_t*100:.0f}% of {ac} ok")
        for a, dv, w, w_t in sorted(moves, key=lambda x: x[1]):          # sells first, then buys
            if dv < 0:
                p = pos[("core", a)]; qty = p["qty"] * min(1, -dv / val(p))
                pnl = sell("core", a, qty, f"rebalance trim {w*100:.1f}% -> {w_t*100:.0f}% (band {band}pp)")
                log_rb.append(f"SELL {a} £{-dv:.2f}"); sig("core", a, None, None, None, "SELL", f"rebalance trim {w*100:.1f}% -> {w_t*100:.0f}%; P&L £{pnl:+.2f}")
            else:
                v = min(dv, cash_for("core", a) - gas_of(klass_of(a)))
                if v < mint or fx_stale: log_rb.append(f"{a} under target but {acash('core', a)}"); continue
                buy("core", a, v, f"rebalance top-up {w*100:.1f}% -> {w_t*100:.0f}% (band {band}pp)")
                log_rb.append(f"BUY {a} £{v:.2f}"); sig("core", a, None, None, None, "BUY", f"rebalance top-up £{v:.2f} {w*100:.1f}% -> {w_t*100:.0f}%")
        sset(con, "core_last_rebalance", ts)
        if forced: con.execute("DELETE FROM state WHERE key='core_force_rebalance'")
        sset(con, "core_rebalance_log", dict(ts=ts, picks=new_picks, equity=ceq, acct_equity=aeq, actions=log_rb, forced=forced or None))
        out.append(f"[core] REBALANCE{' (forced: ' + str(forced) + ')' if forced else ''} (picks {', '.join(new_picks)}): " + "; ".join(log_rb))
        picks = new_picks

    # staggered entry: tranches for assets still being built
    tg = core_targets(c, picks); ntr = int(K["tranches"])
    for a, (ac, w_t) in tg.items():
        pl = plan.setdefault(a, dict(status="building", n=0, last_day=None, day_n=0))
        if pl.get("status") != "building": continue
        q = px.get(a); T = w_t * acct_eq("core", ac); cur = val(pos[("core", a)]) if ("core", a) in pos else 0; gap = T - cur
        if gap < mint:
            pl["status"] = "built"; sig("core", a, None, None, None, "HOLD", f"built: £{cur:.2f} of £{T:.2f} target"); continue
        if pl.get("last_day") != day: pl.update(last_day=day, day_n=0)
        why = None
        if pl["day_n"] >= int(K["max_tranches_per_day"]): continue
        if not q: why = "no price"
        elif not mopen(a): why = closed_msg(a)
        elif not q["fresh"]: why = "stale price"
        elif fx_stale: why = "stale FX rate"
        s = sentiment(con, a, st["sentiment_max_age_h"])
        if not why and s and s["score"] <= K["sentiment_delay_score"] and s["confidence"] >= K["sentiment_delay_conf"]:
            why = f"delayed: sentiment {s['score']:+.2f} (conf {s['confidence']:.2f})"
        trig = None
        if not why:
            av = avg24(hist.get(a) or [])
            dip = av is not None and q["price_usd"] <= av * (1 - K["dip_vs_avg24_pct"] / 100)
            if dip: trig = f"dip: ${q['price_usd']:.5g} <= 24h avg ${av:.5g}"
            elif lt.hour >= int(K["fallback_hour_uk"]): trig = f"daily tranche after {int(K['fallback_hour_uk']):02d}:00 UK (no dip today)"
            elif (mclose_in(a) or 1e9) <= 60: trig = f"daily tranche in the last hour before the {markets.exchange(a)} close (no dip today)"
            else: why = f"waiting for dip (${q['price_usd']:.5g} vs 24h avg ${av:.5g}) or {int(K['fallback_hour_uk']):02d}:00 UK" if av else "waiting (no 24h average)"
        if not why:
            size = max(T / ntr, mint)
            if pl["n"] >= ntr - 1 or gap - size < mint: size = gap
            size = min(size, cash[("core", ac)] - gas_of("stock" if ac == "stocks" else "crypto"))
            if size < mint: why = f"core {ACCT[ac]} cash £{cash[('core', ac)]:.2f} < min trade £{mint:.0f}"
        if why: sig("core", a, None, None, None, "HOLD", f"tranche {pl['n']+1}/{ntr} {why}; £{cur:.2f}/£{T:.2f}"); continue
        pl["n"] += 1; pl["day_n"] += 1
        fin = " final" if size >= gap - 1e-9 else ""
        buy("core", a, size, f"CORE tranche {pl['n']}/{ntr}{fin} ({trig}); target {w_t*100:.0f}% of Core {ac} = £{T:.2f}")
        cur += size
        if T - cur < mint: pl["status"] = "built"
        sig("core", a, None, None, None, "BUY", f"tranche {pl['n']}/{ntr}{fin} £{size:.2f} ({trig}); now £{cur:.2f}/£{T:.2f}")
    sset(con, "core_plan", plan); sset(con, "order_queue", queue)

    # ================= DAY risk + entries =================
    deq = sleeve_eq("day")
    con.execute("INSERT OR IGNORE INTO state VALUES(?,?)", (f"day_start:day:{day}", str(deq)))
    dstart = float(con.execute("SELECT value FROM state WHERE key=?", (f"day_start:day:{day}",)).fetchone()[0])
    teq = sum(sleeve_eq(s) for s in SLEEVES)
    con.execute("INSERT OR IGNORE INTO state VALUES(?,?)", (f"day_start:{day}", str(teq)))
    tstart = float(con.execute("SELECT value FROM state WHERE key=?", (f"day_start:{day}",)).fetchone()[0])
    day_pnl = deq / dstart - 1 if dstart else 0
    halted = day_pnl <= -D["daily_loss_cap_pct"] / 100
    if halted: sset(con, f"day_halt:{day}", ts)
    halted = halted or sget(con, f"day_halt:{day}") is not None          # stays halted for the rest of the UK day
    window = e0 <= mins < e1 and not flat_now
    nday = sum(1 for (sl, _) in pos if sl == "day")
    for a, (m, s, comb, info) in sorted(scores.items(), key=lambda kv: -(kv[1][2] or -9)):
        if ("day", a) in pos or a in risk_only: continue
        q, focus = px[a], a in st["focus_assets"]
        thr = st["focus_buy_threshold"] if focus else st["buy_threshold"]
        if comb is None: sig("day", a, m, s, comb, "SKIP", info); continue
        if comb < thr: sig("day", a, m, s, comb, "HOLD", f"score {comb} < {thr}; {info}"); continue
        why = ("day-sleeve daily loss cap hit" if halted else
               f"outside entry window {D['entry_start_uk']}-{D['entry_cutoff_uk']} UK" if not window else
               closed_msg(a) if not mopen(a) else
               f"too close to the {markets.exchange(a)} close" if (mclose_in(a) or 0) <= 30 else
               "stale price" if not q["fresh"] else "stale FX rate" if fx_stale else
               "max open day positions" if nday >= D["max_open_positions"] else None)
        pct = min(D["alloc_pct"], D["max_position_pct"]) / 100
        value = min(deq * pct, cash_for("day", a) - gas_of(q["klass"]))
        if not why and value < mint:
            why = f"size £{value:.2f} < min trade £{mint:.0f} (cap £{deq * pct:.2f}, {acash('day', a)})"
        if why: sig("day", a, m, s, comb, "SKIP", f"score {comb} >= {thr} but {why}"); continue
        buy("day", a, value, f"DAY score {comb} >= {thr}; {info}"); nday += 1
        sig("day", a, m, s, comb, "BUY", f"£{value:.2f} (cap {pct*100:.0f}% of day sleeve); {info}")

    # ================= RISK entries: breakout + sentiment, drawdown cap =================
    req = sleeve_eq("risk"); rstart = sum(acct_start(con, c)["risk"].values())
    base = float(sget(con, "riskpot_base") or rstart); halt = sget(con, "riskpot_halt")
    if RISK_RESUME.exists():
        if halt: out.append(f"[risk] RESUME after review (was halted {halt.get('ts')}); drawdown base reset to £{req:.2f}")
        sset(con, "riskpot_base", req); base = req; halt = None; con.execute("DELETE FROM state WHERE key='riskpot_halt'")
        try: RISK_RESUME.unlink()
        except OSError: pass
    dd = req / base - 1 if base else 0
    if not halt and dd <= -RP.get("drawdown_cap_pct", 30) / 100:
        halt = dict(ts=ts, equity=req, base=base); sset(con, "riskpot_halt", halt)
        out.append(f"[risk] HALT new entries: pot £{req:.2f} is {dd*100:.1f}% vs £{base:.2f} (cap -{RP['drawdown_cap_pct']}%) - create RISK_RESUME after review")
    nrisk = sum(1 for (sl, _) in pos if sl == "risk")
    new_today = con.execute("SELECT COUNT(*) FROM trades WHERE sleeve='risk' AND side='BUY' AND ts>=?",
                            (datetime.combine(lt.date(), dtime(0), LON).astimezone(timezone.utc).isoformat(),)).fetchone()[0]
    cands = []
    rs = sget(con, "risk_screen") or {}
    for d in rs.get("top") or []:
        if d["a"] in px and px[d["a"]]["klass"] == "crypto": cands.append((d["a"], d["score"], d["ok"], d["why"], "crypto"))
    for sym in stockrank.risk_stocks(c):
        if sym not in px: continue
        sc_, ok_, why_ = riskpot.stock_score(con, sym, px[sym]["price_usd"])
        if sc_ is not None: cands.append((sym, sc_, ok_, why_, "stock"))
    rstat = []
    for a, bscore, ok, why, kind in sorted(cands, key=lambda x: (not x[2], -x[1])):
        if ("risk", a) in pos: continue
        q = px[a]; m, minfo = momentum(hist.get(a) or history(con, a), RP.get("min_history_h", 2))
        se = sentiment(con, a, st["sentiment_max_age_h"]); w = st["sentiment_weight"] * se["confidence"] if se else 0
        comb = round((1 - w) * bscore + w * (se["score"] if se else 0), 3)
        last_exit = sget(con, f"risk_exit:{a}")
        no = ("not a breakout (" + why.split("| no: ")[-1] + ")" if not ok else
              f"score {comb} < {RP['entry_score']}" if comb < RP["entry_score"] else
              f"momentum {minfo}" if m is None else
              f"intraday momentum {m} < {RP['confirm_momentum']} ({minfo})" if m < RP["confirm_momentum"] else
              f"sentiment {se['score']:+.2f} blocks entry" if se and se["score"] <= RP["sentiment_block"] and se["confidence"] >= 0.5 else
              "drawdown cap: entries halted until review" if halt else
              closed_msg(a) if not mopen(a) else
              f"too close to the {markets.exchange(a)} close" if (mclose_in(a) or 0) <= 15 else
              "stale price" if not q["fresh"] else "stale FX rate" if fx_stale else
              f"cooldown after exit {last_exit[:16]}" if last_exit and datetime.fromisoformat(last_exit) > t - timedelta(hours=RP["cooldown_h"]) else
              f"max {RP['max_open']} open" if nrisk >= RP["max_open"] else
              f"max {RP['max_new_per_day']} new per day" if new_today >= RP["max_new_per_day"] else None)
        value = min(req * RP["position_pct"] / 100, float(RP["max_position_gbp"]), cash_for("risk", a) - gas_of(q["klass"]))
        if not no and value < mint: no = f"size £{value:.2f} < min trade £{mint:.0f} ({acash('risk', a)})"
        rstat.append(dict(a=a, kind=kind, score=bscore, comb=comb, mom=m, ok=ok, why=why, block=no))
        if no:
            if ok: sig("risk", a, m, se["score"] if se else None, comb, "SKIP", f"{no}; {why}")
            continue
        buy("risk", a, value, f"RISK breakout {comb} >= {RP['entry_score']} ({kind}): {why}; {minfo}")
        peaks[a] = q["price"]; nrisk += 1; new_today += 1
        sig("risk", a, m, se["score"] if se else None, comb, "BUY", f"£{value:.2f} breakout {comb}; {why}; {minfo}")
    sset(con, "risk_peaks", {a: v for a, v in peaks.items() if ("risk", a) in pos})
    sset(con, "riskpot_status", dict(ts=ts, equity=sleeve_eq("risk"), start=rstart, base=base, dd_pct=dd * 100, halted=bool(halt),
                                     halt=halt, open_n=nrisk, new_today=new_today, cands=sorted(rstat, key=lambda x: (x["block"] is not None, -x["comb"]))[:30]))

    for (k, ac), v in cash.items(): con.execute("INSERT OR REPLACE INTO acct_cash VALUES(?,?,?)", (k, ac, v))
    sync_pot_cash(con)
    eqs = {s: sleeve_eq(s) for s in SLEEVES}; equity = sum(eqs.values()); pv = equity - sum(cash.values())
    pc = {s: sum(cash[(s, ac)] for ac in ACCOUNTS) for s in SLEEVES}
    ae = {s: {ac: [round(acct_eq(s, ac), 6), round(cash[(s, ac)], 6)] for ac in ACCOUNTS} for s in SLEEVES}
    at = {ac: sum(ae[s][ac][0] for s in SLEEVES) for ac in ACCOUNTS}; atc = {ac: sum(cash[(s, ac)] for s in SLEEVES) for ac in ACCOUNTS}
    con.execute("INSERT OR REPLACE INTO state VALUES('risk',?)", (json.dumps(dict(
        ts=ts, day=day, day_start=tstart, equity=equity, day_pnl_pct=(equity / tstart - 1) * 100 if tstart else 0,
        halted=halted, day_sleeve=dict(start=dstart, equity=eqs["day"], pnl_pct=day_pnl * 100, halted=halted,
                                       entries_open=window, flat_now=flat_now, open_n=nday))),))
    con.execute("INSERT INTO equity(ts,equity,cash,pos_value,btc,open_n,usd_gbp,core,day,core_cash,day_cash,risk,risk_cash,"
                "crypto,stocks,crypto_cash,stocks_cash,accts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, equity, sum(cash.values()), pv, px.get("BTC", {}).get("price"), len(pos), rate,
                 eqs["core"], eqs["day"], pc["core"], pc["day"], eqs["risk"], pc["risk"],
                 at["crypto"], at["stocks"], atc["crypto"], atc["stocks"], json.dumps(ae, separators=(",", ":"))))
    con.commit()
    built = sum(1 for a in tg if plan.get(a, {}).get("status") == "built")
    print(f"[trader {ts} / {lt:%a %H:%M} UK] equity £{equity:.2f} = Crypto acct £{at['crypto']:.2f} (cash £{atc['crypto']:.2f}) "
          f"+ Stocks acct £{at['stocks']:.2f} (cash £{atc['stocks']:.2f}) | CORE £{eqs['core']:.2f} (cash £{pc['core']:.2f}, "
          f"{sum(1 for k in pos if k[0]=='core')} pos, {built}/{len(tg)} built) | DAY £{eqs['day']:.2f} (cash £{pc['day']:.2f}, "
          f"{nday} pos, day {day_pnl*100:+.2f}%{' HALTED' if halted else ''}, entries {'open' if window else 'closed'}) "
          f"| RISK £{eqs['risk']:.2f} (cash £{pc['risk']:.2f}, {nrisk} pos{', HALTED' if halt else ''}) "
          f"| GBP/USD {1/rate:.4f} ({fx[2]}{', STALE' if fx_stale else ''})")
    for line in out: print("  " + line)
    if not out: print("  no trades this cycle")

if __name__ == "__main__":
    main()
