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
        earths, global small caps, photonics/robotics small caps). Two entries: a breakout, or a dip that has bounced
        (v7.7). Confidence + sentiment, max 5 positions, 12% stop, trailing take-profit (10% from peak after +15%),
        7-day max hold, 30% sleeve drawdown cap (no new entries until RISK_RESUME is created).
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
import json, math
from datetime import datetime, timedelta, timezone, time as dtime
from common import ACCOUNTS, KILL, LON, RISK_RESUME, SLEEVES, acct_of, acct_start, cfg, db, funding, iso, loans_open, log, move_cash, now, sync_pot_cash, usd_gbp
import daypot, freshness, markets, quotes, riskpot, stockrank, variants
from exits import book_lock, day_flat_reason, day_reason, gap_tag, risk_reason, stock_flatten_reason

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
    """Returns (score, info, rsi) or (None, reason, None)."""
    if not pts or (pts[-1][0] - pts[0][0]).total_seconds() < min_h * 3600:
        return None, "insufficient history", None
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
    return round(score, 3), f"4h {fmt(r4)}, 24h {fmt(r24)}, EMA6/24 {'up' if cross > 0 else 'down'}, RSI {rs:.0f}", rs

def avg24(pts):
    t = pts[-1][0] if pts else None
    xs = [p for ts, p in pts if ts >= t - timedelta(hours=24)] if pts else []
    return sum(xs) / len(xs) if xs else None

def latest_prices(con, max_age_min, rate, rk=None):
    """Latest prices converted to GBP. Age is measured from the provider timestamp (source_ts), falling back to the
    row time. tradable/fresh: offline uses max_price_age_min (45); live uses price_dead_min (180) after a missed refresh.
    A missing price is the only hard skip. fr_price is the price half of the freshness score."""
    rows = con.execute("""SELECT asset, klass, price, ts, source_ts, vol24, mcap FROM (
                            SELECT asset, klass, price, ts, source_ts, vol24, mcap,
                                   ROW_NUMBER() OVER (PARTITION BY asset ORDER BY ts DESC, rowid DESC) rn
                            FROM prices WHERE klass IN ('crypto','stock') AND source!='backfill' AND ts<=?)
                          WHERE rn=1""", (iso(now() + timedelta(seconds=1)),)).fetchall()
    rk = rk or {}; fulls = rk.get("fresh_full_min") or {}
    dead = float(max_age_min if not quotes.enabled() else rk.get("price_dead_min", 180))
    out = {}
    for r in rows:
        age = freshness.age_min(r["source_ts"] or r["ts"], now())
        full = float(fulls.get("stock" if r["klass"] == "stock" else "crypto", 5 if r["klass"] == "stock" else 10))
        tradable = age is not None and age <= dead
        out[r["asset"]] = dict(klass=r["klass"], price=r["price"] * rate, price_usd=r["price"], fresh=tradable, tradable=tradable,
                               ts=r["ts"], source_ts=r["source_ts"] or r["ts"], age_min=age, fr_price=freshness.price_score(age, full, dead),
                               vol24=r["vol24"], mcap=r["mcap"])
    return out

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

def pick_crypto(con, c, crypto_equity):
    """CORE crypto candidates (core.crypto_universe) with the same score, eligibility and per-theme cap as the stocks.
    Nothing here is a fixed target. Returns (picks, info, source)."""
    rk = stockrank.rank_crypto(con, c)
    n = stockrank.n_picks(c, crypto_equity)
    picks = stockrank.select(c, rk, n)
    info = {s: rk[s] for s in picks}
    return picks, info, f"composite score, {sum(1 for r in rk.values() if r['eligible'])}/{len(rk)} eligible, {n} slots for £{crypto_equity:.0f} of Core crypto money"

def core_targets(c, picks, crypto_picks=None):
    """{asset: (account, weight within the CORE money of that account)} - fixed crypto targets plus any ranked
    crypto candidates, in the Crypto account; stock picks in the Stocks account (v6)."""
    t = {a: ("crypto", w) for a, w in stockrank.crypto_weights(c, crypto_picks).items()}
    t.update({s: ("stocks", w) for s, w in stockrank.pick_weights(c, picks).items()})
    return t

def crypto_fee_pct(c, q=None, notional_gbp=None):
    """Per-side crypto fee from liquidity, as a fraction of price. Gas stays separate.
    A large market cap pays about the pool-fee floor. A small cap pays more. Thin turnover (volume/mcap below turn_thin)
    lifts it when turn_thin is set; turn_thin 0 leaves the market-cap curve alone.
    Unknown market cap keeps risk.fee_slippage_pct. A stored Kyber loss (impact_pct, on impact_check_usd) is scaled by
    sqrt(this trade / that check) and used when it is larger. Result stays between the floor and the thin cap."""
    rk = c["risk"]; K = rk.get("liquidity_cost") or {}; q = q or {}
    floor = float(K.get("major_pct", 0.05)) / 100
    thin = float(K.get("thin_pct", 1.0)) / 100
    unknown = float(rk["fee_slippage_pct"]) / 100
    mcap = q.get("mcap")
    if not mcap or float(mcap) <= 0:
        base = unknown
    else:
        lo, hi = math.log(float(K.get("thin_mcap_usd", 2e7))), math.log(float(K.get("deep_mcap_usd", 1e10)))
        t = 1.0 if hi <= lo else max(0.0, min(1.0, (math.log(float(mcap)) - lo) / (hi - lo)))
        base = thin + (floor - thin) * t
        turn = float(q.get("vol24") or 0) / float(mcap)
        turn_thin, turn_deep = float(K.get("turn_thin", 0.05)), float(K.get("turn_deep", 0.30))
        if turn_thin and turn < turn_thin:
            base *= 1 + 0.5 * min(1.0, (turn_thin - turn) / turn_thin)
        elif turn >= turn_deep:
            base *= 0.7
    impact = None
    if q.get("impact_pct") is not None:
        check = float(K.get("impact_check_usd", 200)) or 200.0
        if notional_gbp and q.get("price") and q.get("price_usd"):
            usd = float(notional_gbp) * float(q["price_usd"]) / float(q["price"])
        elif notional_gbp:
            usd = float(notional_gbp) / 0.75
        else:
            usd = check
        impact = (float(q["impact_pct"]) / 100.0) * math.sqrt(max(usd, 1.0) / check)
    return min(thin, max(floor, max(base, impact or 0.0)))

def cost_parts(c, asset, klass, side):
    """Per-side cost components as fractions of the price. crypto: {'fee'} is the flat fallback (the fill uses
    crypto_fee_pct, which can be lower or higher); stocks: {'spread', 'fx', 'stamp'} (Trading 212-like, FX 0.15%/side)."""
    if klass != "stock": return {"fee": float(c["risk"]["fee_slippage_pct"]) / 100}
    C = c["stocks"].get("costs") or {}; ex = markets.exchange(asset)
    return {"spread": float(C.get("spread_pct", 0.1)) / 100,
            "fx": float(C.get("fx_fee_pct", 0.15)) / 100 if ex != "LSE" else 0.0,
            "stamp": float(C.get("uk_stamp_duty_pct", 0.5)) / 100 if side == "BUY" and ex == "LSE" and asset not in set(C.get("stamp_exempt") or []) else 0.0}

def cost_pct(c, asset, klass, side, q=None, notional_gbp=None):
    """Per-side trading cost as a fraction of the price (applied in the fill price).
    crypto: liquidity (mcap, volume/mcap, optional Kyber route impact). Unknown mcap keeps fee_slippage_pct.
    Gas is on top (gas_gbp). stocks: spread + FX 0.15% per side on non-GBP listings + UK stamp on London buys
    unless AIM/ETF (stamp_exempt). No commission."""
    if klass != "stock": return crypto_fee_pct(c, q, notional_gbp)
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
    """Called on account reset: pick stocks and any qualifying crypto candidates, mark every target as 'building'."""
    picks, info, src = pick_stocks(con, c, funding(c)["core"]["stocks"])
    cpicks, cinfo, csrc = pick_crypto(con, c, funding(c)["core"]["crypto"])
    sset(con, "core_stocks", dict(picks=picks, info=info, source=src, ts=iso()))
    sset(con, "core_crypto", dict(picks=cpicks, info=cinfo, source=csrc, ts=iso()))
    sset(con, "core_plan", {a: dict(status="building", n=0, last_day=None, day_n=0) for a in core_targets(c, picks, cpicks)})
    sset(con, "core_last_rebalance", iso())
    return picks

def score_asset(con, asset, st, t):
    """(momentum, sentiment, combined, info, rsi, history). The cycle and the exit watcher share this blend."""
    pts = history(con, asset)
    m, info, rs_ = momentum(pts, st["min_history_h"])
    if m is None:
        return None, None, None, info, None, pts
    s = sentiment(con, asset, st["sentiment_max_age_h"])
    fac = 1.0
    if s:     # weight decays to 0 by sentiment_max_age_h; the score in the blend stays raw. Hard gates use the raw score.
        fac = freshness.sentiment_factor((t - datetime.fromisoformat(s["ts"])).total_seconds() / 3600, st["sentiment_max_age_h"])
    w = st["sentiment_weight"] * s["confidence"] * fac if s else 0
    comb = round((1 - w) * m + w * (s["score"] if s else 0), 3)
    info = info + (f", sentiment {s['score']:+.2f} (conf {s['confidence']:.2f}{'' if fac > 0.999 else f', weight x{fac:.2f}'})" if s else ", no fresh sentiment")
    return m, (s["score"] if s else None), comb, info, rs_, pts


def apply_sell(con, c, ts, rate, gas, sleeve, asset, klass, qty, price, reason, q, sub=None, conf=None, snap=None):
    """Write the sell the 5-minute cycle writes. Returns None if the row is already gone (the other writer sold it)."""
    row = con.execute("SELECT qty, cost, klass FROM positions WHERE sleeve=? AND asset=?", (sleeve, asset)).fetchone()
    if not row or not row["qty"] or float(row["qty"]) <= 0:
        return None
    qty_held, cost_basis = float(row["qty"]), float(row["cost"])
    qty = min(float(qty), qty_held)
    klass = row["klass"] or klass
    fp = cost_pct(c, asset, klass, "SELL", q, qty * price)
    gas_paid = gas if klass == "crypto" else 0.0
    proceeds = qty * price * (1 - fp) - gas_paid
    part = cost_basis * qty / qty_held
    pnl = proceeds - part
    fill = price * (1 - fp)
    blob = json.dumps(snap, separators=(",", ":")) if snap else None
    con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason,price_usd,usd_gbp,gas,sub,conf,snap) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, sleeve, asset, klass, "SELL", qty, price, fill, qty * fill, qty * price * fp, pnl,
                 f"{reason}; P&L £{pnl:+.2f}", price / rate, rate, gas_paid, sub, conf, blob))
    ac = acct_of(klass)
    cur = con.execute("SELECT amount FROM acct_cash WHERE sleeve=? AND account=?", (sleeve, ac)).fetchone()
    con.execute("INSERT OR REPLACE INTO acct_cash VALUES(?,?,?)", (sleeve, ac, (float(cur[0]) if cur else 0.0) + proceeds))
    sync_pot_cash(con)
    closed = qty >= qty_held * (1 - 1e-9)
    if closed:
        con.execute("DELETE FROM positions WHERE sleeve=? AND asset=?", (sleeve, asset))
        qty_left, cost_left = 0.0, 0.0
    else:
        qty_left, cost_left = qty_held - qty, cost_basis - part
        con.execute("UPDATE positions SET qty=?, cost=? WHERE sleeve=? AND asset=?", (qty_left, cost_left, sleeve, asset))
    return dict(pnl=pnl, proceeds=proceeds, closed=closed, qty_left=qty_left, cost_left=cost_left)


def main():
    """One cycle. Holds the book lock the exit watcher also uses, so a position is sold once."""
    with book_lock():
        _cycle()


def _cycle():
    c, con = cfg(), db()
    R = variants.rules_of(c)   # all false on the main book; a variant overlay turns flags on
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
    px = latest_prices(con, rk["max_price_age_min"], rate, rk)
    _screen = sget(con, "risk_screen") or {}
    for _d in (_screen.get("top") or []):
        _q = px.get(_d.get("a"))
        if not _q: continue
        if _d.get("mcap") and not _q.get("mcap"): _q["mcap"] = _d.get("mcap")
        if _d.get("vol") and not _q.get("vol24"): _q["vol24"] = _d.get("vol")
        if _d.get("dex") is not None: _q["impact_pct"] = _d.get("dex")
    _eco = sget(con, "monad_eco") or {}
    _dex = _eco.get("dex") if isinstance(_eco, dict) else None
    if _dex:
        _id = {_d.get("id"): _d.get("a") for _d in (_screen.get("top") or []) if _d.get("id")}
        for _cid, _dx in _dex.items():
            _a = _id.get(_cid)
            if _a in px and isinstance(_dx, dict) and _dx.get("loss_pct") is not None:
                px[_a]["impact_pct"] = _dx["loss_pct"]
    def restamp():
        fulls = rk.get("fresh_full_min") or {}
        dead = float(rk["max_price_age_min"] if not quotes.enabled() else rk.get("price_dead_min", 180))
        for q in px.values():
            age = freshness.age_min(q.get("source_ts") or q.get("ts"), now())
            full = float(fulls.get("stock" if q["klass"] == "stock" else "crypto", 5 if q["klass"] == "stock" else 10))
            q["age_min"] = age; q["fr_price"] = freshness.price_score(age, full, dead)
            q["tradable"] = q["fresh"] = bool(age is not None and age <= dead)
    def refresh(symbols):
        if quotes.prefetch(con, c, symbols, px): restamp()
    cash = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}   # (pot, account) -> £
    pos = {(r["sleeve"], r["asset"]): dict(r) for r in con.execute("SELECT * FROM positions")}
    refresh([a for (sl, a) in pos])          # a fresh quote before any exit fill (no-op in sim)

    def gas_of(klass): return gas if klass == "crypto" else 0.0     # stocks: no network fee (broker trade)
    def fsell(a):
        q = px.get(a) or {}
        k = q.get("klass") or next((p["klass"] for (sl, x), p in pos.items() if x == a), "crypto")
        held = next((p for (sl, x), p in pos.items() if x == a), None)
        notion = held["qty"] * q["price"] if held and q.get("price") else None
        return cost_pct(c, a, k, "SELL", q, notion)
    def klass_of(a):
        return (px.get(a) or {}).get("klass") or next((p["klass"] for (sl, x), p in pos.items() if x == a), "crypto")
    def pos_ev(meta, price, asset):
        """(EV now, entry EV) from the snapshot stored at the buy. None when that snapshot has no probability."""
        snap = (meta or {}).get("snap") or {}
        p_hit, tgt, loss, entry = snap.get("p"), snap.get("target"), snap.get("loss"), snap.get("entry")
        if p_hit is None or tgt is None or loss is None or not entry or not price:
            return None, snap.get("ev")
        cost = cost_pct(c, asset, klass_of(asset), "SELL", px.get(asset)) * 100
        evn = daypot.ev_now(p_hit, price, float(entry) * (1 + float(tgt) / 100.0),
                            float(entry) * (1 - float(loss) / 100.0), cost)
        return evn, snap.get("ev")
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

    def trade(sleeve, asset, klass, side, qty, price, reason, pnl=None, sub=None, conf=None, snap=None, notional=None):
        fp = cost_pct(c, asset, klass, side, px.get(asset), notional if notional is not None else qty * price)
        fill = price * (1 + fp if side == "BUY" else 1 - fp)
        blob = None
        if snap:
            if side == "BUY": snap["entry"] = round(fill, 8)   # the fill the plan was priced from; the caller's dict keeps it for the sell
            blob = json.dumps(snap, separators=(",", ":"))
        con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason,price_usd,usd_gbp,gas,sub,conf,snap) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, sleeve, asset, klass, side, qty, price, fill, qty * fill, qty * price * fp, pnl, reason, price / rate, rate, gas_of(klass), sub, conf, blob))
        return fill

    def buy(sleeve, a, value, reason, sub=None, conf=None, snap=None):
        """Spends value on the asset (fill incl. fees/slippage/FX/stamp duty) + gas on top (crypto only); all go into the cost basis."""
        q = px[a]; fp = cost_pct(c, a, q["klass"], "BUY", q, value)
        fill = trade(sleeve, a, q["klass"], "BUY", value / (q["price"] * (1 + fp)), q["price"], reason, sub=sub, conf=conf, snap=snap, notional=value)
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

    def sell(sleeve, a, qty, reason, price=None, sub=None, conf=None, snap=None):
        p = pos.get((sleeve, a))
        if not p: return None
        price = price or px[a]["price"]
        done = apply_sell(con, c, ts, rate, gas, sleeve, a, p["klass"], min(qty, p["qty"]), price, reason, px.get(a), sub=sub, conf=conf, snap=snap)
        if done is None:
            pos.pop((sleeve, a), None); return None
        cash[(sleeve, acct_of(p["klass"]))] += done["proceeds"]
        if done["closed"]: pos.pop((sleeve, a), None)
        else: p["qty"], p["cost"] = done["qty_left"], done["cost_left"]
        return done["pnl"]

    def val(p): q = px.get(p["asset"]); return p["qty"] * q["price"] if q else p["cost"]
    loans = loans_open(con)                               # v7.3 inter-pot loans inside one account (outstanding)
    def lent(s, ac): return sum(l["out"] for l in loans if l["lender"] == s and l["account"] == ac)
    def owed(s, ac, sub=None): return sum(l["out"] for l in loans if l["borrower"] == s and l["account"] == ac and (sub is None or l["sub"] == sub))
    def acct_eq(s, ac): return cash[(s, ac)] + lent(s, ac) - owed(s, ac) + sum(val(p) for (sl, _), p in pos.items() if sl == s and acct_of(p["klass"]) == ac)
    def sleeve_eq(s): return sum(acct_eq(s, ac) for ac in ACCOUNTS)

    # ---- score every asset (DAY signal; sentiment also used by CORE tranche timing) ----
    scores, hist, rsi_at = {}, {}, {}
    for a, p in px.items():
        m, s, comb, info, rs_, pts = score_asset(con, a, st, t)
        hist[a] = pts; rsi_at[a] = rs_; scores[a] = (m, s, comb, info)

    def exit_ok(q):
        """Offline/sim: same 45-minute cutoff as before. Live: any stored price can exit (a missed cycle fills at the real price)."""
        if not q: return False
        return True if quotes.enabled() else bool(q.get("fresh"))
    def tag_gap(a, level, cur, reason):
        return gap_tag(hist.get(a) or [], level, cur, reason)
    def sent_in(se):
        """Sentiment fed to a confidence score: decayed toward 0 (neutral) as it ages. None stays None. Hard gates use se['score']."""
        if not se: return None
        fac = freshness.sentiment_factor((t - datetime.fromisoformat(se["ts"])).total_seconds() / 3600, st["sentiment_max_age_h"])
        return se["score"] * fac
    def fr_of(a, q, se):
        gaps = freshness.history_gaps(hist.get(a) or [], t)
        sfac = None
        if se:
            sfac = freshness.sentiment_factor((t - datetime.fromisoformat(se["ts"])).total_seconds() / 3600, st["sentiment_max_age_h"])
        return freshness.combine(q.get("fr_price", 1), freshness.gap_score(gaps), sfac)
    risk_only = set(sget(con, "risk_only") or []) | set(sget(con, "risk_only_stocks") or [])   # priced only for the RISK pot
    day_x = sget(con, "day_excluded") or {}    # v7.2: base coins outside the DAY universe (exchange tokens, gold/RWA tokens, memes)
    RP = c.get("riskpot") or {}

    # ================= RISK exits: stop / trailing take-profit / max hold / sentiment =================
    peaks = sget(con, "risk_peaks") or {}; rmeta = sget(con, "risk_meta") or {}     # v7.4 rmeta: confidence + EV at entry
    for (sl, a), p in list(pos.items()):
        if sl != "risk": continue
        q = px.get(a)
        if not mopen(a): sig("risk", a, None, None, None, "HOLD", f"{closed_msg(a)} - exits checked when it opens"); continue
        if not exit_ok(q): sig("risk", a, None, None, None, "HOLD", "stale/missing price, cannot evaluate exit"); continue
        f = fsell(a)
        se = sentiment(con, a, st["sentiment_max_age_h"])
        stop_pct = float(RP["stop_loss_pct"])
        ev_reason = None
        if R["ev_exit"]:
            evn, eev = pos_ev(rmeta.get(a) or {}, q["price"], a)
            if variants.ev_action(evn, eev, R) == "exit":
                ev_reason = variants.ev_exit_reason(evn, eev, R)
            else:
                stop_pct = variants.effective_stop_pct(stop_pct, evn, R, "risk")
        d = risk_reason(p["entry"], q["price"], peaks.get(a), p["opened"], RP, se, t, f, stop_pct)
        pk = peaks[a] = d["peak"]; chg, pk_gain, trail_on = d["chg"], d["pk_gain"], d["trail_on"]
        reason = ev_reason or d["reason"]
        if reason:
            if "stop-loss" in reason:
                reason = tag_gap(a, p["entry"] * (1 - stop_pct / 100) / (1 - f), q["price"], reason)
            elif "trailing" in reason:
                reason = tag_gap(a, pk * (1 - RP["trail_pct"] / 100), q["price"], reason)
            meta = rmeta.get(a) or {}
            pnl = sell("risk", a, p["qty"], reason, sub=meta.get("setup") or "breakout", conf=meta.get("conf"), snap=meta.get("snap")); peaks.pop(a, None)
            if pnl is None: continue
            sset(con, f"risk_exit:{a}", ts); sig("risk", a, None, None, None, "SELL", f"{reason}; P&L £{pnl:+.2f}")
        else:
            sig("risk", a, None, None, None, "HOLD", d["hold"])

    # ================= DAY exits (v7: momentum + burst sub-strategies) =================
    # flatten window (both subs) / ATR-scaled stop / trailing stop / ATR-scaled target (momentum 'runner') / burst max hold /
    # momentum signal exit. Per-position levels live in state 'day_meta' {asset: {sub, tp, sl, atr, peak, opened, runner}}.
    e0, e1, flat = hm(D["entry_start_uk"]), hm(D["entry_cutoff_uk"]), hm(D["flatten_uk"])
    flat_now = mins >= flat or mins < e0
    day_late = []          # still open after the flatten because there was no price at all
    DM, DB = D["momentum"], D["burst"]; SUBC = {"momentum": DM, "burst": DB}
    ATRC = D.get("atr") or {}
    dmeta = {a: v for a, v in (sget(con, "day_meta") or {}).items() if ("day", a) in pos}
    def atr_of(a): return daypot.atr_pct(hist.get(a) or [], int(ATRC.get("bars", 14)), int(ATRC.get("min_bars", 4)))
    def thr_of(a): return st["focus_buy_threshold"] if a in st["focus_assets"] else st["buy_threshold"]
    def day_exit(a, p, why, m, s, comb):
        md_ = dmeta.get(a) or {}; sub = md_.get("sub", "momentum"); tag = sub + (f" {md_['kind']}" if md_.get("kind") else "")
        if R["burst_runner"] and sub == "burst" and md_.get("runner") and "burst runner:" not in why:
            q_ = px.get(a)
            if q_ and p.get("entry"):
                ff = fsell(a)
                chg_ = q_["price"] * (1 - ff) / p["entry"] - 1
                pk_ = max(float(md_.get("peak") or 0), q_["price"])
                pkg_ = pk_ * (1 - ff) / p["entry"] - 1
                why += "; " + variants.ran_note(chg_, pkg_, md_.get("tp") or 0, float(R.get("burst_runner_giveback_pct", 20)))
        pnl = sell("day", a, p["qty"], f"[{tag}] {why}", sub=sub, conf=md_.get("conf", md_.get("score")), snap=md_.get("snap"))
        if pnl is None:
            dmeta.pop(a, None); return
        dmeta.pop(a, None); sset(con, f"day_exit:{a}", ts)
        sig("day", a, m, s, comb, "SELL", f"[{tag}] {why}; P&L £{pnl:+.2f}")
    def burst_keep(a, q_):
        """Re-score the burst entry signal for an open position that has just hit its target."""
        pts = hist.get(a) or []
        brk = daypot.breakout(con, a, pts, DB)
        vol = daypot.crypto_vol_ratio(con, a, t) if (q_.get("klass") or "crypto") != "stock" else daypot.stock_vol_ratio(con, a, t, sget)
        r_ = daypot.rsi_h(pts)
        se = sentiment(con, a, st["sentiment_max_age_h"])
        mok, reg, _minfo = daypot.market_regime(con, q_.get("klass") or "crypto", a, hist, t, DB, sget, markets.exchange)
        kind = brk.get("kind")
        ss = daypot.setup_stats(con, kind or "24h", DB, t)
        conf, _parts = daypot.confidence(DB, brk.get("strength") or 0, vol, sent_in(se), reg, ss["p"], freshness=fr_of(a, q_, se))
        row = con.execute("SELECT value FROM state WHERE key=?", (f"day_start:day:{day}",)).fetchone()
        base_ = float(row[0]) if row else sleeve_eq("day")
        pnl_ = sleeve_eq("day") / base_ - 1 if base_ else 0.0
        thr = daypot.threshold(DB, pnl_, D["daily_loss_cap_pct"])
        vx = float(DB["volume_x"])
        vol_ok = not ((vol is not None and vol < vx) or (vol is None and (DB.get("require_volume") or kind != "24h")))
        sent_ok = not (se and se["score"] < float(DB["sentiment_min"]))
        return variants.burst_target_action(bool(kind), vol_ok, r_, DB["rsi_max"], sent_ok, bool(mok), conf, thr)
    for (sl, a), p in list(pos.items()):
        if sl != "day": continue
        q = px.get(a); m, s, comb, info = scores.get(a, (None, None, None, ""))
        opened_day = datetime.fromisoformat(p["opened"]).astimezone(LON).date().isoformat()
        if p["klass"] == "stock" and (mclose_in(a) is None or mclose_in(a) <= int(D.get("stock_flatten_before_close_min", 15))):
            if not q:
                last = con.execute("SELECT price FROM prices WHERE asset=? AND source!='backfill' AND ts<=? ORDER BY ts DESC LIMIT 1",
                                   (a, ts)).fetchone()
                if last:
                    q = px[a] = dict(klass=p["klass"], price=last[0] * rate, price_usd=last[0], fresh=False, tradable=True,
                                     ts=ts, source_ts=ts, age_min=None, fr_price=0, vol24=None, mcap=None)
                else:
                    day_late.append(a); sig("day", a, m, s, comb, "HOLD", "flatten due but no price at all"); continue
            cl = mclose_in(a)
            day_exit(a, p, stock_flatten_reason(cl, markets.exchange(a)), m, s, comb); continue
        if flat_now or opened_day != day:
            if not q:
                last = con.execute("SELECT price FROM prices WHERE asset=? AND source!='backfill' AND ts<=? ORDER BY ts DESC LIMIT 1",
                                   (a, ts)).fetchone()
                if last:
                    q = px[a] = dict(klass=p["klass"], price=last[0] * rate, price_usd=last[0], fresh=False, tradable=True,
                                     ts=ts, source_ts=ts, age_min=None, fr_price=0, vol24=None, mcap=None)
                else:
                    day_late.append(a)
                    sig("day", a, m, s, comb, "HOLD", f"flatten {D['flatten_uk']} UK due but no price at all"); continue
            why = day_flat_reason(mins, flat, e0, D["flatten_uk"], D["entry_start_uk"], q.get("fresh"))
            day_exit(a, p, why, m, s, comb); continue
        if not exit_ok(q): sig("day", a, None, None, None, "HOLD", "stale/missing price, cannot evaluate exit"); continue
        md = dmeta.get(a)
        if not md:     # pre-v7 position: becomes a momentum trade with ATR levels computed now
            at_ = atr_of(a); tp_, sl_ = daypot.levels(DM["take_profit"], DM["stop"], at_)
            md = dmeta[a] = dict(sub="momentum", tp=round(tp_, 3), sl=round(sl_, 3), atr=round(at_, 3) if at_ else None,
                                 peak=max(q["price"], p["entry"]), opened=p["opened"], legacy=True)
        C = SUBC[md["sub"]]; f_ = fsell(a)
        stop_pct = md["sl"]
        ev_reason = None
        if R["ev_exit"] or (md.get("sub") == "burst" and R["burst_stop_widen"]):
            evn, eev = pos_ev(md, q["price"], a)
            if variants.ev_action(evn, eev, R) == "exit":
                ev_reason = variants.ev_exit_reason(evn, eev, R)
            else:
                stop_pct = variants.effective_stop_pct(md["sl"], evn, R, md.get("sub") or "day")
        gb_override = float(R.get("burst_runner_giveback_pct", 20)) if (
            md.get("runner") and md.get("sub") == "burst" and R["burst_runner"]) else None
        d = day_reason(p["entry"], q["price"], p["opened"], md, C, st, comb, info, t, f_, thr_of(a), a,
                       stop_pct, gb_override, defer_burst_target=bool(R["burst_runner"]))
        chg, pkg, trail_on, lvl, gb = d["chg"], d["pkg"], d["trail_on"], d["lvl"], d["gb"]
        reason = ev_reason or d["reason"]
        if d["runner"] and not ev_reason:
            out.append(d["runner"])
        elif d["at_target"] and not ev_reason and q:
            keep, why_b = burst_keep(a, q)
            gb_b = float(R.get("burst_runner_giveback_pct", C.get("runner_giveback_pct", 20)))
            if keep:
                md["runner"] = True
                out.append(f"[day] BURST RUNNER {a}: target +{md['tp']:.1f}% reached at {chg*100:+.1f}% ({why_b}) - trailing {gb_b:.0f}% of the gain")
            else:
                reason = f"take-profit {chg*100:.1f}% >= {md['tp']:.1f}% (ATR-scaled); {variants.sold_at_target_note(why_b)}"
        if reason:
            if reason.startswith("stop-loss"):
                reason = tag_gap(a, p["entry"] * (1 - stop_pct / 100) / (1 - f_), q["price"], reason)
            elif reason.startswith("trailing"):
                # trail sells when the gain has given back `gb` of the peak gain, not `gb` of the price
                lvl_px = p["entry"] * (1 + pkg * (1 - gb)) / (1 - f_)
                reason = tag_gap(a, lvl_px, q["price"], reason)
            day_exit(a, p, reason, m, s, comb)
        else:
            sig("day", a, m, s, comb, "HOLD", f"[{md['sub']}] open {chg*100:+.1f}% (target +{md['tp']:.1f}% / stop -{md['sl']:.1f}%"
                + (f", trailing at {lvl*100:+.1f}%" if trail_on else "")
                + ("; burst runner" if md.get("runner") and md.get("sub") == "burst" else "") + f"); {info}")

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
    crypto_picks = (sget(con, "core_crypto") or {}).get("picks", [])
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
            if pnl is None: continue
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
            if pnl is None: continue
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
        new_cp, cinfo, csrc = pick_crypto(con, c, acct_eq("core", "crypto"))
        if not new_cp:                            # no daily history yet is not a reason to drop a candidate
            prev = sget(con, "core_crypto") or {}
            new_cp = crypto_picks
            if prev.get("picks"): cinfo, csrc = prev.get("info") or cinfo, prev.get("source") or csrc
        sset(con, "core_stocks", dict(picks=new_picks, info=info, source=src, ts=ts))
        sset(con, "core_crypto", dict(picks=new_cp, info=cinfo, source=csrc, ts=ts))
        tg = core_targets(c, new_picks, new_cp); ceq = sleeve_eq("core"); aeq = {ac: acct_eq("core", ac) for ac in ACCOUNTS}
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
                if pnl is None: continue
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
                if pnl is None: continue
                log_rb.append(f"SELL {a} £{-dv:.2f}"); sig("core", a, None, None, None, "SELL", f"rebalance trim {w*100:.1f}% -> {w_t*100:.0f}%; P&L £{pnl:+.2f}")
            else:
                v = min(dv, cash_for("core", a) - gas_of(klass_of(a)))
                if v < mint or fx_stale: log_rb.append(f"{a} under target but {acash('core', a)}"); continue
                buy("core", a, v, f"rebalance top-up {w*100:.1f}% -> {w_t*100:.0f}% (band {band}pp)")
                log_rb.append(f"BUY {a} £{v:.2f}"); sig("core", a, None, None, None, "BUY", f"rebalance top-up £{v:.2f} {w*100:.1f}% -> {w_t*100:.0f}%")
        sset(con, "core_last_rebalance", ts)
        if forced: con.execute("DELETE FROM state WHERE key='core_force_rebalance'")
        sset(con, "core_rebalance_log", dict(ts=ts, picks=new_picks, crypto_picks=new_cp, equity=ceq, acct_equity=aeq, actions=log_rb, forced=forced or None))
        out.append(f"[core] REBALANCE{' (forced: ' + str(forced) + ')' if forced else ''} (picks {', '.join(new_picks)}): " + "; ".join(log_rb))
        picks = new_picks; crypto_picks = new_cp

    def entry_pts(a):
        """Prices for a variant entry gate. The own-trend check needs a tick at or before N days before the last one."""
        if R["entry_own_trend"]:
            return history(con, a, variants.trend_lookback_h(R))
        return hist.get(a) or []

    # staggered entry: tranches for assets still being built
    tg = core_targets(c, picks, crypto_picks); ntr = int(K["tranches"])
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
        elif not q["fresh"]: why = "no current price"
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
            if not why and R["core_fallback_half"] and trig.startswith("daily tranche after"):
                pts7 = history(con, a, 24 * 8)
                r7 = variants.ret_over_days(pts7, 7)
                rs7 = rsi(list({ts.strftime("%Y%m%d%H"): px_ for ts, px_ in pts7}.values())) if len(pts7) > 14 else rsi_at.get(a)
                frac = variants.fallback_fraction(r7, rs7, R)
                if frac < 1:
                    note = variants.extended_note(r7, rs7, R)
                    size = size * frac
                    trig = f"{trig}; half size ({note})"
                    if size < mint - 1e-9:
                        why = f"extended day, half tranche £{size:.2f} is under the £{mint:.0f} minimum ({note})"
            if not why and (R["entry_own_trend"] or R["no_day_core_same_afternoon"]):
                blk = variants.entry_block(con, ("day",), a, entry_pts(a), lt, R)
                if blk: why = blk
        if why: sig("core", a, None, None, None, "HOLD", f"tranche {pl['n']+1}/{ntr} {why}; £{cur:.2f}/£{T:.2f}"); continue
        pl["n"] += 1; pl["day_n"] += 1
        fin = " final" if size >= gap - 1e-9 else ""
        buy("core", a, size, f"CORE tranche {pl['n']}/{ntr}{fin} ({trig}); target {w_t*100:.0f}% of Core {ac} = £{T:.2f}")
        cur += size
        if T - cur < mint: pl["status"] = "built"
        sig("core", a, None, None, None, "BUY", f"tranche {pl['n']}/{ntr}{fin} £{size:.2f} ({trig}); now £{cur:.2f}/£{T:.2f}")
    sset(con, "core_plan", plan); sset(con, "order_queue", queue)

    # ================= v7.3 inter-pot loans (same account only, never crypto <-> stocks) =================
    LN = c.get("loans") or {}
    def core_reserve(ac):    # cash CORE still needs today: the next tranche of every asset still being built that has not had
        r = 0.0              # today's tranche yet + queued CORE buys (never lent out)
        for a_, (ac_, w_) in tg.items():
            pl_ = plan.get(a_) or {}
            if ac_ != ac or pl_.get("status") != "building": continue
            if pl_.get("last_day") == day and pl_.get("day_n", 0) >= int(K["max_tranches_per_day"]): continue
            T_ = w_ * acct_eq("core", ac); cur_ = val(pos[("core", a_)]) if ("core", a_) in pos else 0.0
            r += min(max(T_ - cur_, 0.0), max(T_ / ntr, mint)) + gas_of("stock" if ac == "stocks" else "crypto")
        r += sum(float(o.get("value") or 0) for o in queue if o.get("side") == "BUY" and o.get("sleeve") == "core" and acc(o["a"]) == ac)
        return r
    def loan_room(ac):       # total outstanding per account <= max_outstanding_pct of the account's starting value
        startv = sum(acct_start(con, c)[s_][ac] for s_ in SLEEVES)
        return float(LN.get("max_outstanding_pct", 20)) / 100 * startv - sum(l["out"] for l in loans if l["account"] == ac)
    def borrow(borrower, a, need, sub=None, why="", min_need=0.0, skip=()):
        """Borrow `need` (the shortfall for ONE position of asset a) from CORE's uninvested cash (minus what today's
        tranches / queued CORE buys need), then the other short-term pot's cash - same account only, total outstanding per
        account <= max_outstanding_pct of its start value. A loan that cannot cover min_need (the trade would still be
        impossible) is not taken. Returns the amount borrowed (0 = none). Lender keeps a receivable, borrower a
        liability, so neither pot's value changes."""
        if not LN.get("enabled", True) or need <= 0: return 0.0
        ac = acc(a); amt = min(need, loan_room(ac)); other = "risk" if borrower == "day" else "day"
        if owed(borrower, ac) > 1e-9 and not LN.get("allow_multiple", False): return 0.0   # one loan (= one position) at a time per pot x account
        def avail_of(lender):
            if lender == borrower or owed(lender, ac) > 1e-9: return 0.0      # a pot that owes money does not lend
            return max(0.0, cash[(lender, ac)] - (core_reserve(ac) if lender == "core" else 0.0))
        lenders = [x for x in ("core", other) if x not in skip]
        if amt < max(min_need, 0.01) - 1e-9 or min(amt, sum(avail_of(x) for x in lenders)) < max(min_need, 0.01) - 1e-9: return 0.0
        got = 0.0
        for lender in lenders:
            av_ = avail_of(lender); take = min(math.ceil((amt - got) * 100 - 1e-6) / 100, av_)   # whole pence, rounded up (no 4.9999 < £5)
            take = round(take, 4)
            if take < 0.01: continue
            cash[(lender, ac)] -= take; cash[(borrower, ac)] += take; got += take
            reason = f"{borrower} borrows £{take:.2f} from {lender} ({ac} account) for {a}: {why}"
            cur = con.execute("INSERT INTO loans(ts,account,lender,borrower,sub,asset,amount,repaid,status,reason) VALUES(?,?,?,?,?,?,?,0,'open',?)",
                              (ts, ac, lender, borrower, sub, a, take, reason))
            loans.append(dict(id=cur.lastrowid, account=ac, lender=lender, borrower=borrower, sub=sub, asset=a, amount=take, repaid=0.0, out=take, ts=ts))
            out.append(f"[loan] {reason}")
            if got >= amt - 1e-9: break
        return got

    # ================= DAY risk + entries (v7: momentum + burst) =================
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
    def sub_of(a): return (dmeta.get(a) or {}).get("sub", "momentum")
    # v7.1 sub-strategy ledger (daypot.sub_ledger, same definition as the dashboard): budget = the sub-strategy's own equity
    # (its share of the DAY start money + its realised P&L, incl. its open positions at market); free = its cash not tied up
    # in its open positions (never more than the account's DAY cash). Recomputed on every call (buys/sells change it).
    d_start = acct_start(con, c)["day"]; d_since = (sget(con, "account_split") or {}).get("ts", ""); d_sh = daypot.sub_shares(SUBC)
    def ledger():
        dp = [dict(sub=sub_of(x), acct=acct_of(p["klass"]), cost=p["cost"], value=val(p)) for (sl, x), p in pos.items() if sl == "day"]
        return daypot.sub_ledger(d_start, {ac: cash[("day", ac)] for ac in ACCOUNTS}, dp, daypot.sub_realized(con, d_since), d_sh, ACCOUNTS,
                                 borrowed={(sb, ac): owed("day", ac, sb) for sb in daypot.SUBS for ac in ACCOUNTS}, lent={ac: lent("day", ac) for ac in ACCOUNTS})
    def budget(sub, ac): return max(0.0, ledger()[ac][sub]["equity"])
    def in_use(sub, ac): return ledger()[ac][sub]["value"]
    def free(sub, ac): return ledger()[ac][sub]["free"]
    def rt_cost(a, value):   # round-trip cost in % of the position (both sides + gas both sides)
        q = px.get(a) or {}; k = q.get("klass") or klass_of(a)
        return (cost_pct(c, a, k, "BUY", q, value) + cost_pct(c, a, k, "SELL", q, value)) * 100 + 2 * gas_of(k) / max(value, 1e-9) * 100
    def hours_to_flat(a):
        if klass_of(a) == "stock":
            mleft = mclose_in(a)
            if mleft is None: return 0.0
            return max(0.0, (mleft - int(D.get("stock_flatten_before_close_min", 15))) / 60.0)
        return max(0.0, (flat - mins) / 60.0)
    def clock_p(a, max_hold_h=None):
        h = hours_to_flat(a)
        if max_hold_h is not None: h = min(h, float(max_hold_h))
        return daypot.flatten_p(h, float(D.get("ev_flatten_floor", 0.40)), float(D.get("ev_flatten_full_h", 6)))
    mcx = float(D.get("min_move_cost_x", 4)); scl = int(D.get("stock_entry_stop_before_close_min", 30))
    def gate(a):             # shared entry gates (None = ok)
        q = px[a]
        g = ("day-pot daily loss cap hit" if halted else
             f"outside entry window {D['entry_start_uk']}-{D['entry_cutoff_uk']} UK" if not window else
             closed_msg(a) if not mopen(a) else
             f"too close to the {markets.exchange(a)} close" if (mclose_in(a) or 0) <= scl else
             "no current price" if not q["fresh"] else "stale FX rate" if fx_stale else None)
        if g or not (R["entry_own_trend"] or R["no_day_core_same_afternoon"]):
            return g
        return variants.entry_block(con, ("core",), a, entry_pts(a), lt, R) or g
    warm_h = float(c["crypto"].get("warmup_h", st["min_history_h"]))
    def warming(a):          # v7.2: wide-universe coins build history from the 15-min snapshots -> no DAY entries until warm
        if klass_of(a) != "crypto": return None    # stocks: intraday history restarts every session (not a universe coin)
        pts = hist.get(a) or []; h = (pts[-1][0] - pts[0][0]).total_seconds() / 3600 if len(pts) > 1 else 0.0
        return f"warming up: {h:.1f}h of {warm_h:g}h price history" if h < warm_h else None
    def open_day(a, sub, value, tp_, sl_, at_, reason, extra=None):
        extra = dict(extra or {})
        buy("day", a, value, reason, sub=sub, conf=extra.get("conf", extra.get("score")), snap=extra.get("snap"))
        dmeta[a] = dict(sub=sub, tp=round(tp_, 3), sl=round(sl_, 3), atr=round(at_, 3) if at_ else None, peak=px[a]["price"], opened=ts, **extra)

    # ---- momentum: original score entries, ATR-scaled levels ----
    nmom = sum(1 for (sl, x) in pos if sl == "day" and sub_of(x) == "momentum")
    ss_mom = daypot.sell_record(con, "day", "momentum", t, float(DM.get("record_days", 14)))
    refresh([a for a, (m, s, comb, info) in scores.items() if comb is not None and comb >= thr_of(a) and ("day", a) not in pos])
    for a, (m, s, comb, info) in sorted(scores.items(), key=lambda kv: -(kv[1][2] or -9)):
        if ("day", a) in pos or a in risk_only or a in day_x: continue
        q = px[a]; thr = thr_of(a); ac = acc(a)
        if comb is None or warming(a): sig("day", a, m, s, comb, "SKIP", warming(a) or info); continue
        if comb < thr: sig("day", a, m, s, comb, "HOLD", f"score {comb} < {thr}; {info}"); continue
        at_ = atr_of(a); tp_, sl_ = daypot.levels(DM["take_profit"], DM["stop"], at_)
        value = min(max(budget("momentum", ac) * float(DM["position_pct"]) / 100, mint), free("momentum", ac) - gas_of(q["klass"]))
        why = gate(a) or ("max open momentum positions" if nmom >= int(DM["max_open"]) else None)
        if not why and value < mint:
            why = f"momentum budget: £{value:.2f} free < min trade £{mint:.0f} (budget £{budget('momentum', ac):.2f}, {acash('day', a)})"
        if not why and tp_ < mcx * rt_cost(a, value): why = f"target {tp_:.1f}% < {mcx:.0f} x round-trip costs {rt_cost(a, value):.2f}%"
        rs_m = rsi_at.get(a)
        if not why and rs_m is not None and rs_m >= float(DM.get("rsi_max", 80)):
            why = f"RSI {rs_m:.0f} >= {float(DM.get('rsi_max', 80)):g} (overbought)"
        p_m = daypot.hit_p(comb, ss_mom, int(DM.get("ev_prior_n", 10)), int(DM.get("ev_conf_full_n", 30)))
        rtc_m = rt_cost(a, max(value, mint)); ev_m = daypot.day_ev(p_m, tp_, sl_, rtc_m, clock_p(a))
        evmin_m = float(DM.get("ev_min_pct", 0.3))
        if not why and ev_m <= evmin_m: why = f"expected value {ev_m:+.2f}% after costs <= {evmin_m:g}%"
        if why: sig("day", a, m, s, comb, "SKIP", f"[momentum] score {comb} >= {thr} but {why}"); continue
        open_day(a, "momentum", value, tp_, sl_, at_, f"[momentum] DAY score {comb} >= {thr}; target +{tp_:.1f}% / stop -{sl_:.1f}% (ATR {at_ or 0:.2f}%); EV {ev_m:+.2f}%; {info}",
                 extra=dict(score=comb, snap=dict(score=comb, p=round(p_m, 3), target=round(tp_, 3), loss=round(sl_, 3), costs=round(rtc_m, 3), ev=ev_m)))
        nmom += 1
        sig("day", a, m, s, comb, "BUY", f"[momentum] £{value:.2f}; target +{tp_:.1f}% / stop -{sl_:.1f}%; {info}")

    # ---- burst: breakout + volume + RSI + sentiment + market regime, confidence-scored, EV after costs, capital-based threshold + sizing ----
    sstats = {k: daypot.setup_stats(con, k, DB, t) for k in ("24h", "4h")}
    thr_b, evmin = daypot.threshold(DB, day_pnl, D["daily_loss_cap_pct"]), float(DB.get("ev_min_pct", 0))
    cands = []
    for a, q in px.items():
        if ("day", a) in pos or a in risk_only or a in day_x or q["klass"] not in ("crypto", "stock"): continue
        pts = hist.get(a) or []
        brk = daypot.breakout(con, a, pts, DB)
        if not brk["kind"] and "broken" not in (brk.get("info") or ""): continue      # only actual / near breakouts are listed
        ac = acct_of(q["klass"]); d = dict(a=a, acct=ac, brk=brk.get("kind"), info=brk.get("info"))
        vol = daypot.crypto_vol_ratio(con, a, t) if q["klass"] == "crypto" else daypot.stock_vol_ratio(con, a, t, sget)
        r_ = daypot.rsi_h(pts);         se = sentiment(con, a, st["sentiment_max_age_h"])
        mok, reg, minfo = daypot.market_regime(con, q["klass"], a, hist, t, DB, sget, markets.exchange)
        at_ = atr_of(a); tp_, sl_ = daypot.levels(DB["take_profit"], DB["stop"], at_)
        ss = sstats.get(brk["kind"]) or dict(p=0.5, n=0, wins=0, exp=None)
        fr = fr_of(a, q, se)
        conf, parts = daypot.confidence(DB, brk["strength"], vol, sent_in(se), reg, ss["p"], freshness=fr)
        size0 = max(daypot.conf_size(DB, conf, free("burst", acct_of(q["klass"]))), mint)
        rtc = rt_cost(a, size0)
        p_ev = daypot.hit_p(conf, ss, int(DB.get("ev_prior_n", 10)), int(DB.get("ev_conf_full_n", 30)))
        ev = daypot.day_ev(p_ev, tp_, sl_, rtc, clock_p(a, DB.get("max_hold_h")))
        d.update(kind=brk.get("kind"), ev=ev, rt_cost=round(rtc, 3), hit=round(p_ev, 3), hit_n=ss["n"], vol=round(vol, 2) if vol is not None else None, rsi=round(r_) if r_ is not None else None, sent=se["score"] if se else None,
                 market=minfo, atr=round(at_, 2) if at_ else None, tp=round(tp_, 2), sl=round(sl_, 2), conf=conf, parts=parts)
        vx = float(DB["volume_x"])
        lx = sget(con, f"day_exit:{a}")
        d["block"] = (brk["info"] if not brk["kind"] else
                      warming(a) if warming(a) else
                      f"cooldown {DB['cooldown_h']}h after exit {lx[11:16]}Z" if lx and datetime.fromisoformat(lx) > t - timedelta(hours=float(DB["cooldown_h"])) else
                      f"volume x{vol:.1f} < {vx:g}x average" if vol is not None and vol < vx else
                      "no volume data (only a 24h-high break may trade without it)" if vol is None and (DB.get("require_volume") or brk["kind"] != "24h") else
                      f"RSI {r_:.0f} >= {DB['rsi_max']} (overbought)" if r_ is not None and r_ >= float(DB["rsi_max"]) else
                      f"sentiment {se['score']:+.2f} < {DB['sentiment_min']}" if se and se["score"] < float(DB["sentiment_min"]) else
                      f"market falling ({minfo})" if not mok else None)
        if d["block"] and fr < 0.999 and "freshness" not in (d["block"] or ""):
            age = q.get("age_min")
            d["block"] += f"; data freshness {fr:.0%}" + (f" (price {age:.0f} min)" if age is not None else "")
        cands.append(d)
    nb = {ac: sum(1 for (sl, x), p in pos.items() if sl == "day" and sub_of(x) == "burst" and acct_of(p["klass"]) == ac) for ac in ACCOUNTS}
    refresh([d["a"] for d in cands if not d["block"]])   # quote the breakouts we might actually buy
    for d in sorted(cands, key=lambda d: (d["block"] is not None, -d["conf"])):
        if d["block"]: continue
        a, ac = d["a"], d["acct"]; q = px[a]
        floor = float(DB.get("cash_floor_gbp", 4))
        raw = daypot.conf_size(DB, d["conf"], free("burst", ac))
        value, avail = daypot.burst_spend(free("burst", ac), gas_of(q["klass"]), raw, mint, floor)
        if (value < floor - 1e-9 and d["conf"] >= max(thr_b, float(LN.get("burst_min_conf", 0.75))) and d["ev"] > max(evmin, 0.0) and not gate(a)
                and nb[ac] < int(DB["max_open_per_account"])):
            # v7.3: exceptional burst setup short of cash -> borrow the shortfall for ONE position: confidence-sized on the burst
            # budget (its equity), at least the min trade, at most max_position_pct of that budget
            bud = max(0.0, ledger()[ac]["burst"]["equity"])
            tgt = max(mint, min(daypot.conf_size(DB, d["conf"], bud), float(DB.get("max_position_pct", 33)) / 100 * bud))
            fr = max(0.0, free("burst", ac)) - gas_of(q["klass"])
            if borrow("day", a, tgt - fr, "burst", f"burst confidence {d['conf']:.2f} >= {float(LN.get('burst_min_conf', 0.75)):g}, EV {d['ev']:+.2f}%", min_need=mint - fr):
                avail = free("burst", ac) - gas_of(q["klass"]); value = min(tgt, avail); d["loan"] = True
        why = (gate(a) or
               (f"confidence {d['conf']:.2f} < threshold {thr_b:.2f}" if d["conf"] < thr_b else None) or
               (f"expected value {d['ev']:+.2f}% after costs <= {evmin:g}%" if d["ev"] <= evmin else None) or
               (f"max {DB['max_open_per_account']} open burst positions in the {ac} account" if nb[ac] >= int(DB["max_open_per_account"]) else None) or
               (f"burst budget: £{max(avail, 0):.2f} free < £{floor:.0f} floor (min trade £{mint:.0f})" if value < floor - 1e-9 else None) or
               (f"target {d['tp']:.1f}% < {mcx:.0f} x round-trip costs {rt_cost(a, value):.2f}%" if d["tp"] < mcx * rt_cost(a, value) else None))
        if why: d["block"] = why; continue
        open_day(a, "burst", value, d["tp"], d["sl"], d["atr"],
                 f"[burst {d['kind']}] {d['info']}; vol {'n/a' if d['vol'] is None else 'x%.1f' % d['vol']}, RSI {d['rsi']}, {d['market']}; confidence {d['conf']:.2f} >= {thr_b:.2f}, EV {d['ev']:+.2f}% after costs; "
                 f"target +{d['tp']:.1f}% / stop -{d['sl']:.1f}% / max {DB['max_hold_h']}h",
                 extra=dict(conf=d["conf"], ev=d["ev"], kind=d["kind"],
                            snap=dict(conf=d["conf"], parts=dict(d["parts"]), p=d["hit"], target=d["tp"], loss=d["sl"], costs=d["rt_cost"], ev=d["ev"])))
        nb[ac] += 1; d["block"] = None; d["bought"] = round(value, 2)
        sig("day", a, None, d["sent"], None, "BUY", f"[burst] £{value:.2f} confidence {d['conf']:.2f}, EV {d['ev']:+.2f}%; {d['info']}; target +{d['tp']:.1f}% / stop -{d['sl']:.1f}%")
    if cands:
        out.append(f"[day] burst setups: " + "; ".join(f"{d['a']} conf {d['conf']:.2f} EV {d['ev']:+.2f}% -> " + (f"BUY £{d['bought']:.2f}" if d.get("bought") else f"skip ({d['block']})")
                                                       for d in sorted(cands, key=lambda d: -d["conf"])[:6]))
    nday = sum(1 for (sl, _) in pos if sl == "day")
    sset(con, "day_meta", dmeta)
    sset(con, "day_subs", dict(ts=ts, threshold=thr_b, base_threshold=float(DB.get("conf_threshold", 0.55)), day_pnl_pct=round(day_pnl * 100, 3), ev_min=evmin, setups={k: dict(p=round(v["p"], 3), n=v["n"], exp=round(v["exp"], 3) if v["exp"] is not None else None) for k, v in sstats.items()},
                               budgets=(lambda L: {sb: {ac: dict(budget=round(L[ac][sb]["equity"], 4), cash=round(L[ac][sb]["cash"], 4), in_use=round(L[ac][sb]["value"], 4),
                                                              cost=round(L[ac][sb]["cost"], 4), free=round(L[ac][sb]["free"], 4), open_n=L[ac][sb]["open_n"])
                                                     for ac in ACCOUNTS} for sb in SUBC})(ledger()),
                               cands=sorted(cands, key=lambda d: (d.get("bought") is None, d["block"] is not None, -d["conf"]))[:25]))

    # ================= RISK entries (v7.4 Day-style: confidence + EV after costs, drawdown-based threshold) =================
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
                            (datetime.combine(lt.date(), dtime(0), LON).astimezone(timezone.utc).isoformat(),)).fetchone()[0]   # info only (v7.4: no daily cap)
    # v7.4 Day-style decision: every setup gets a confidence 0-1 (breakout strength, volume spike, sentiment, market regime,
    # measured hit rate); it is taken only if confidence >= threshold AND expected value after costs > ev_min_pct. The
    # threshold rises from conf_threshold (0.55) to conf_threshold_at_stop (0.85) as the drawdown approaches the 30% stop.
    # Size = confidence x conf_size_pct% of the account's remaining RISK cash (min £5, max £8 / 20% of the pot).
    rss_all = riskpot.setup_stats(con, RP, t)
    rss = riskpot.setup_stats(con, RP, t, sub="breakout"); dss = riskpot.setup_stats(con, RP, t, sub="dip")
    thr_r = riskpot.threshold(RP, dd); evmin_r = float(RP.get("ev_min_pct", 0.0))
    raw_c = []
    rs = sget(con, "risk_screen") or {}
    for d in rs.get("top") or []:
        if d["a"] in px and px[d["a"]]["klass"] == "crypto": raw_c.append((d["a"], d["score"], d["ok"], d["why"], "crypto", d.get("c7")))
    for sym in stockrank.risk_stocks(c):
        if sym not in px: continue
        sc_, ok_, why_ = riskpot.stock_score(con, sym, px[sym]["price_usd"])
        if sc_ is not None: raw_c.append((sym, sc_, ok_, why_, "stock", None))
    def pack(a, kind, setup, plain, bscore, ok, why, m, minfo, conf, parts, ss, vol, se, mkinfo, at_, block, trend_ok=False):
        q = px[a]
        size0, cap0 = riskpot.size(RP, conf, cash_for("risk", a) - gas_of(q["klass"]), req, mint)
        rtc = rt_cost(a, size0); ev, ev_w, ev_l, ev_p = riskpot.setup_ev(RP, conf, at_, rtc, ss)
        return dict(a=a, kind=kind, setup=setup, acct=acc(a), score=bscore, mom=m, ok=ok, why=why, plain=plain,
                    conf=conf, parts=parts, ev=ev, rt_cost=round(rtc, 3), ev_win=ev_w, ev_loss=ev_l, ev_p=ev_p,
                    atr=round(at_, 2) if at_ else None, hit=round(ss["p"], 3), hit_n=ss["n"],
                    vol=round(vol, 2) if vol is not None else None, sent=se["score"] if se else None,
                    market=mkinfo, size=round(size0, 2), cap=cap0, block=block, minfo=minfo, trend_ok=bool(trend_ok))
    rstat = []
    for a, bscore, ok, why, kind, c7 in raw_c:
        if ("risk", a) in pos or any(x["a"] == a for x in rstat): continue
        q = px[a]; pts = hist.get(a) or history(con, a); m, minfo, _rs7 = momentum(pts, RP.get("min_history_h", 2))
        se = sentiment(con, a, st["sentiment_max_age_h"])
        vol = daypot.crypto_vol_ratio(con, a, t) if q["klass"] == "crypto" else daypot.stock_vol_ratio(con, a, t, sget)
        mok, reg, mkinfo = daypot.market_regime(con, q["klass"], a, hist, t, DB, sget, markets.exchange)
        at_ = atr_of(a); fr = fr_of(a, q, se); sin = sent_in(se)
        last_exit = sget(con, f"risk_exit:{a}")
        shared = (f"sentiment {se['score']:+.2f} blocks entry" if se and se["score"] <= RP["sentiment_block"] and se["confidence"] >= 0.5 else
                  "drawdown cap: entries halted until review" if halt else
                  closed_msg(a) if not mopen(a) else
                  f"too close to the {markets.exchange(a)} close" if (mclose_in(a) or 0) <= 15 else
                  "no current price" if not q["fresh"] else "stale FX rate" if fx_stale else
                  f"cooldown after exit {last_exit[:16]}" if last_exit and datetime.fromisoformat(last_exit) > t - timedelta(hours=RP["cooldown_h"]) else None)
        if shared is None and (R["entry_own_trend"] or R["no_day_core_same_afternoon"]):
            shared = variants.entry_block(con, ("core",), a, entry_pts(a), lt, R)
        def note(hard):
            if hard and fr < 0.999 and "freshness" not in hard:
                age = q.get("age_min"); hard += f"; data freshness {fr:.0%}" + (f" (price {age:.0f} min)" if age is not None else "")
            return hard
        head, _, tail = (why or "").partition(" | no: ")
        brk_plain = f"Breakout: {head}." if ok else f"Not a breakout: {tail or head}."
        if not ok: brk_spec = "not a breakout (" + (tail or head) + ")"
        elif m is None: brk_spec = f"trend: {minfo}"
        elif m < RP["confirm_momentum"]: brk_spec = f"short-term trend {m} < {RP['confirm_momentum']} ({minfo})"
        else: brk_spec = shared
        run_why, run_scale, no_loan = riskpot.runup_adjust(c7 if kind == "crypto" else None, RP)
        if run_why: brk_spec = run_why
        elif run_scale < 0.999 and ok:
            brk_plain += f" 7d +{float(c7):.0f}% scales the breakout score (Core will not lend)."
        brk_hard = note(brk_spec)
        conf, parts = riskpot.confidence(RP, (bscore or 0) * run_scale, vol, sin, reg, rss["p"], freshness=fr)
        brk = pack(a, kind, "breakout", brk_plain, bscore, ok, why, m, minfo, conf, parts, rss, vol, se, mkinfo, at_, brk_hard)
        brk["no_core_loan"] = bool(no_loan)
        dip_r = riskpot.dip(pts, RP, sent=sin, regime=reg, hr=dss["p"], freshness=fr, vol=vol, atr=at_) if (RP.get("dip") or {}).get("enabled", True) else None
        chosen = brk
        if dip_r and dip_r.get("show"):
            dip_hard = note(None if dip_r["ok"] else dip_r["why"])
            if dip_hard is None: dip_hard = note(shared)
            dip_row = pack(a, kind, "dip", dip_r["why"], bscore, ok, why, m, minfo, dip_r["conf"], dip_r["parts"], dss, vol, se, mkinfo, at_, dip_hard, dip_r["trend_ok"])
            brk_open, dip_open = brk["block"] is None, dip_row["block"] is None
            if dip_open and not brk_open: chosen = dip_row
            elif brk_open and dip_open and dip_row["conf"] > brk["conf"]: chosen = dip_row
            elif not brk_open: chosen = dip_row          # both blocked: explain the pullback
        rstat.append(chosen)
    refresh([d["a"] for d in rstat if not d["block"]])
    for d in sorted(rstat, key=lambda d: (d["block"] is not None, -d["conf"])):
        a = d["a"]; q = px[a]; m = d["mom"]
        if not d["block"]:
            avail = cash_for("risk", a) - gas_of(q["klass"])
            value, cap_ = riskpot.size(RP, d["conf"], avail, req, mint); value = min(value, avail)
            # A breakout borrows when 4h momentum agrees. A dip borrows when the prior uptrend is there
            # (4h momentum is often still negative at the bounce) — same confidence, EV and cash rules.
            trend_agrees = bool(d.get("trend_ok")) if d["setup"] == "dip" else (m is not None and m > 0)
            trend_txt = "uptrend" if d["setup"] == "dip" else f"trend {m:+.2f}"
            if (value < mint and d["conf"] >= max(thr_r, float(LN.get("risk_min_score", 0.65))) and d["ev"] > max(evmin_r, 0.0)
                    and trend_agrees and nrisk < RP["max_open"]):
                tgt = max(mint, min(cap_, riskpot.size(RP, d["conf"], max(avail, 0.0), req, mint)[0]))
                if borrow("risk", a, tgt - max(avail, 0.0), None, f"{d['setup']} confidence {d['conf']:.2f} >= {float(LN.get('risk_min_score', 0.65)):g}, {trend_txt}, EV {d['ev']:+.2f}%",
                          min_need=mint - max(avail, 0.0), skip=("core",) if d.get("no_core_loan") else ()):
                    avail = cash_for("risk", a) - gas_of(q["klass"]); value = min(tgt, avail); d["loan"] = True
            d["block"] = ((f"confidence {d['conf']:.2f} < threshold {thr_r:.2f}" if d["conf"] < thr_r else None) or
                          (f"expected value {d['ev']:+.2f}% after costs <= {evmin_r:g}%" if d["ev"] <= evmin_r else None) or
                          (f"max {RP['max_open']} open" if nrisk >= RP["max_open"] else None) or
                          (f"size £{value:.2f} < min trade £{mint:.0f} ({acash('risk', a)})" if value < mint else None))
        if d["block"]:
            if d["setup"] == "dip" or d["ok"]: sig("risk", a, m, d["sent"], d["conf"], "SKIP", f"{d['block']}; {d['plain']}")
            continue
        label = "dip" if d["setup"] == "dip" else "breakout"
        snap = dict(conf=d["conf"], parts=dict(d["parts"]), p=d["ev_p"], target=d["ev_win"], loss=d["ev_loss"],
                    costs=d["rt_cost"], ev=d["ev"], setup=d["setup"])
        buy("risk", a, value, f"RISK {label} ({d['kind']}): {d['plain']} Confidence {d['conf']:.2f} >= {thr_r:.2f}, EV {d['ev']:+.2f}% after costs; {d['minfo']}; {d['market']}",
            sub=d["setup"], conf=d["conf"], snap=snap)
        peaks[a] = q["price"]; nrisk += 1; new_today += 1; d["bought"] = round(value, 2)
        rmeta[a] = dict(conf=d["conf"], ev=d["ev"], opened=ts, setup=d["setup"], why=d["plain"], snap=snap)
        sig("risk", a, m, d["sent"], d["conf"], "BUY", f"£{value:.2f} {label} confidence {d['conf']:.2f}, EV {d['ev']:+.2f}%; {d['plain']}")
    sset(con, "risk_peaks", {a: v for a, v in peaks.items() if ("risk", a) in pos})
    sset(con, "risk_meta", {a: v for a, v in rmeta.items() if ("risk", a) in pos})
    if rstat:
        out.append("[risk] setups: " + "; ".join(f"{d['a']} {d['setup']} conf {d['conf']:.2f} EV {d['ev']:+.2f}% -> " + (f"BUY £{d['bought']:.2f}" if d.get("bought") else f"skip ({d['block']})")
                                                   for d in sorted(rstat, key=lambda d: -d["conf"])[:5]))
    def _ss(s): return dict(p=round(s["p"], 3), n=s["n"], wins=s["wins"], exp=round(s["exp"], 3) if s["exp"] is not None else None)
    sset(con, "riskpot_status", dict(ts=ts, equity=sleeve_eq("risk"), start=rstart, base=base, dd_pct=dd * 100, halted=bool(halt),
                                     halt=halt, open_n=nrisk, new_today=new_today, threshold=thr_r, base_threshold=float(RP.get("conf_threshold", 0.55)),
                                     top_threshold=float(RP.get("conf_threshold_at_stop", 0.85)), ev_min=evmin_r,
                                     setup=_ss(rss_all), setups=dict(breakout=_ss(rss), dip=_ss(dss)),
                                     cands=sorted(rstat, key=lambda x: (x.get("bought") is None, x["block"] is not None, -x["conf"]))[:30]))

    # v7.3: repay loans whose position has closed - principal first, borrower -> lender, out of the sale proceeds (the P&L
    # stays with the borrower). If the borrower's cash is short (a loss), it repays what it has; the rest stays owed and is
    # retried every cycle.
    for l in loans:
        if l["out"] <= 1e-9 or (l["borrower"], l["asset"]) in pos: continue
        k = (l["borrower"], l["account"]); pay = round(min(l["out"], max(0.0, cash[k])), 6)
        if pay <= 1e-9: continue
        cash[k] -= pay; cash[(l["lender"], l["account"])] += pay; l["repaid"] += pay; l["out"] -= pay
        done = l["out"] <= 1e-6
        if done: l["out"] = 0.0
        con.execute("UPDATE loans SET repaid=?, status=?, repaid_ts=? WHERE id=?", (l["repaid"], "repaid" if done else "open", ts, l["id"]))
        out.append(f"[loan] {l['borrower']} repays £{pay:.2f} to {l['lender']} ({l['account']} account) - {l['asset']} closed" + ("" if done else f"; £{l['out']:.2f} still owed"))
    for (k, ac), v in cash.items(): con.execute("INSERT OR REPLACE INTO acct_cash VALUES(?,?,?)", (k, ac, v))
    sync_pot_cash(con)
    eqs = {s: sleeve_eq(s) for s in SLEEVES}; equity = sum(eqs.values()); pv = equity - sum(cash.values())
    pc = {s: sum(cash[(s, ac)] for ac in ACCOUNTS) for s in SLEEVES}
    ae = {s: {ac: [round(acct_eq(s, ac), 6), round(cash[(s, ac)], 6)] for ac in ACCOUNTS} for s in SLEEVES}
    at = {ac: sum(ae[s][ac][0] for s in SLEEVES) for ac in ACCOUNTS}; atc = {ac: sum(cash[(s, ac)] for s in SLEEVES) for ac in ACCOUNTS}
    con.execute("INSERT OR REPLACE INTO state VALUES('risk',?)", (json.dumps(dict(
        ts=ts, day=day, day_start=tstart, equity=equity, day_pnl_pct=(equity / tstart - 1) * 100 if tstart else 0,
        halted=halted, day_sleeve=dict(start=dstart, equity=eqs["day"], pnl_pct=day_pnl * 100, halted=halted,
                                       entries_open=window, flat_now=flat_now, open_n=nday, overdue=day_late))),))
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
