"""Export dashboard data (dashboard/data.json) and install the static single-page app
(dashboard/index.html + app.js + styles.css, templates in web/). No AI, no network: pure SQLite -> JSON.
The page loads data.json client-side and re-fetches it every 60 s (works on GitHub Pages)."""
import hashlib, json, os, re
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from common import KILL, LON, RISK_RESUME, ROOT, SITE, SLEEVES, cfg, db, now, rebase, start_cash, transfer_adj, usd_gbp
import stockrank

OUT = SITE                # docs/ in the repo (GitHub Pages); override with SITE_DIR
WEB = ROOT / "web"
PRICE_DAYS = 14          # OHLC history per asset: hourly for the last PRICE_HOURLY_DAYS, 4-hourly before that
PRICE_HOURLY_DAYS = 7
SIGNAL_DAYS = 7          # signal-score history per asset: hourly for the last 48 h, 3-hourly before that
EQUITY_FULL_H = 48       # keep every equity point for the last 48 h, hourly before that
MAX_TRADES = 500
MAX_SENTIMENT = 300
MAX_REASONS = 12         # per asset: latest signal + recent non-HOLD signals with full reasons

def stats(con, sleeve=None):
    """Account stats for the whole account (sleeve=None) or one sleeve ('core'/'day'/'risk'). Also used by daily_summary.py.
    Sleeve equity history is rebased by later cash TRANSFERs (common.transfer_adj) so moves between pots are not P&L."""
    col = {None: "equity", "core": "core", "day": "day", "risk": "risk"}[sleeve]
    adj = transfer_adj(con, sleeve) if sleeve else []
    eq = [(r[0], r[1] + rebase(r[0], adj), r[2]) for r in con.execute(f"SELECT ts, {col}, btc FROM equity ORDER BY ts").fetchall()
          if r[1] is not None]
    q = "SELECT pnl FROM trades WHERE side='SELL'" + (" AND sleeve=?" if sleeve else "")
    sells = [r[0] for r in con.execute(q, (sleeve,) if sleeve else ())]
    c = cfg(); start = float(c["starting_balance"]) if not sleeve else start_cash(c)[sleeve]
    peak, mdd = 0, 0
    for r in eq: peak = max(peak, r[1]); mdd = min(mdd, r[1] / peak - 1) if peak else mdd
    btc0 = next((r[2] for r in eq if r[2]), None)
    btc_ret = (eq[-1][2] / btc0 - 1) if eq and btc0 and eq[-1][2] else None
    nt = con.execute("SELECT COUNT(*) FROM trades" + (" WHERE sleeve=?" if sleeve else ""), (sleeve,) if sleeve else ()).fetchone()[0]
    return dict(start=start, equity=eq[-1][1] if eq else start, ret=(eq[-1][1] / start - 1) if eq else 0,
                realized=sum(sells), trades=nt, closed=len(sells),
                win_rate=(sum(p > 0 for p in sells) / len(sells)) if sells else None,
                max_dd=mdd, btc_ret=btc_ret, best=max(sells, default=None), worst=min(sells, default=None))

def ep(ts):  # ISO -> epoch seconds (UTC)
    d = datetime.fromisoformat(ts)
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())

def sig(x, n=6):  # round to n significant digits (keeps JSON small)
    return None if x is None else float(f"{x:.{n}g}")

def r2(x): return None if x is None else round(x, 2)

def build(con):
    c = cfg(); st, rk, K, DY = c["strategy"], c["risk"], c["core"], c["day"]
    sc = start_cash(c)
    start = float(c["starting_balance"]); focus = list(st["focus_assets"])
    t_now = now(); n_ep = int(t_now.timestamp())
    fx = usd_gbp(con, c["fx"]["max_age_h"]); rate = fx[0] if fx else None
    fx_rows = [(ep(r[0]), r[1]) for r in con.execute("SELECT ts, rate FROM fx WHERE pair='USDGBP' AND rate>0 ORDER BY ts")]
    fx_t = [r[0] for r in fx_rows]
    def fx_at(t):
        if not fx_rows: return rate
        i = bisect_right(fx_t, t) - 1
        return fx_rows[max(i, 0)][1]
    fx_alt = con.execute("SELECT alt_rate, alt_source FROM fx WHERE pair='USDGBP' ORDER BY ts DESC LIMIT 1").fetchone()

    # ---- equity curve + BTC benchmark (both £, benchmark rebased to the £start) ----
    eq = con.execute("SELECT ts, equity, cash, pos_value, btc, open_n, core, day, risk FROM equity ORDER BY ts").fetchall()
    btc0 = next((r["btc"] for r in eq if r["btc"]), None)
    adj = {sl: transfer_adj(con, sl) for sl in SLEEVES}
    reb = lambda r, sl: r2(r[sl] + rebase(r["ts"], adj[sl])) if r[sl] is not None else None   # pot history rebased by later transfers
    cut = n_ep - EQUITY_FULL_H * 3600; seen = {}
    for r in eq:
        t = ep(r["ts"]); key = t if t >= cut else t // 3600   # older points: last one per hour
        seen[key] = (t, r2(r["equity"]), r2(start * r["btc"] / btc0) if btc0 and r["btc"] else None, r2(r["cash"]),
                     reb(r, "core"), reb(r, "day"), reb(r, "risk"))
    pts = sorted(seen.values())[-3000:]
    equity = dict(t=[p[0] for p in pts], eq=[p[1] for p in pts], btc=[p[2] for p in pts], cash=[p[3] for p in pts],
                  core=[p[4] for p in pts], day=[p[5] for p in pts], risk=[p[6] for p in pts])

    # ---- latest prices / 24h change ----
    latest = con.execute("""SELECT asset, klass, price, source, MAX(ts) ts FROM prices
                            WHERE source!='backfill' GROUP BY asset""").fetchall()
    lp = {r["asset"]: r for r in latest}
    mon = {a: lp[a]["price"] for a in ("MON", "MON.DEX") if a in lp}

    # ---- hourly OHLC (USD) for the last PRICE_DAYS, from all sources incl. backfill (no DEX quotes) ----
    since = (t_now - timedelta(days=PRICE_DAYS)).isoformat()
    ohlc, closes = {}, {}
    hourly_cut = n_ep - PRICE_HOURLY_DAYS * 86400
    for r in con.execute("""SELECT asset, ts, price FROM prices WHERE ts>=? AND klass IN ('crypto','stock')
                            AND source!='kyberswap' ORDER BY asset, ts""", (since,)):
        t = ep(r["ts"]); p = r["price"]
        h = t - t % 3600 if t >= hourly_cut else t - t % 14400
        closes.setdefault(r["asset"], []).append((t, p))
        b = ohlc.setdefault(r["asset"], {}).get(h)
        if b: b[2] = max(b[2], p); b[3] = min(b[3], p); b[4] = p
        else: ohlc[r["asset"]][h] = [h, p, p, p, p]
    prices = {}
    for a, d in ohlc.items():   # open = previous close so sparse (1 point/hour) buckets still draw real candles
        bars, prev = [], None
        for b in sorted(d.values()):
            o = prev if prev is not None else b[1]
            bars.append([b[0], sig(o, 5), sig(max(b[2], o), 5), sig(min(b[3], o), 5), sig(b[4], 5)]); prev = b[4]
        prices[a] = bars
    fx_series = sorted({b[0]: None for d in ohlc.values() for b in d.values()})
    fx_hourly = [[h, sig(fx_at(h + 3599), 5)] for h in fx_series] if fx_rows else []

    def chg24(a):
        pts = closes.get(a); cur = lp.get(a)
        if not pts or not cur: return None
        tgt = ep(cur["ts"]) - 86400
        old = [p for t, p in pts if t <= tgt]
        if not old: return None
        return round((cur["price"] / old[-1] - 1) * 100, 2)

    # ---- signals: latest per asset + hourly score history + reasons ----
    sig_since = (t_now - timedelta(days=SIGNAL_DAYS)).isoformat()
    shist, reasons, last_sig = {}, {}, {}
    for r in con.execute("SELECT ts, sleeve, asset, momentum, sentiment, combined, action, reason FROM signals WHERE ts>=? ORDER BY ts",
                         (sig_since,)):
        if r["asset"] == "*": continue
        t = ep(r["ts"])
        if r["sleeve"] != "day":   # CORE/RISK events: reasons only (the score chart / asset table show the DAY signal)
            if r["action"] not in ("HOLD", "SKIP"):
                reasons.setdefault(r["asset"], []).append(dict(t=t, sleeve=r["sleeve"], action=r["action"],
                                                               score=r["combined"] if r["sleeve"] == "risk" else None, reason=r["reason"]))
            continue
        bucket = t // 3600 if t >= n_ep - 48 * 3600 else t // 10800 * 1000   # hourly last 48 h, 3-hourly before
        prev = shist.setdefault(r["asset"], {}).get(bucket)
        act = r["action"] if not prev or r["action"] in ("BUY", "SELL") or prev[2] not in ("BUY", "SELL") else prev[2]
        shist[r["asset"]][bucket] = [t, None if r["combined"] is None else round(r["combined"], 3), act]
        last_sig[r["asset"]] = r
        if r["action"] not in ("HOLD", "SKIP"):
            reasons.setdefault(r["asset"], []).append(dict(t=t, sleeve="day", action=r["action"], score=r["combined"], reason=r["reason"]))
    for a, r in last_sig.items():   # always include the latest DAY reason
        lst = reasons.setdefault(a, []); t = ep(r["ts"])
        if not any(x["t"] == t and x["sleeve"] == "day" for x in lst):
            lst.append(dict(t=t, sleeve="day", action=r["action"], score=r["combined"], mom=r["momentum"], sent=r["sentiment"], reason=r["reason"]))
    reasons = {a: sorted(v, key=lambda x: x["t"], reverse=True)[:MAX_REASONS] for a, v in reasons.items()}
    sig_hist = {a: sorted(v.values()) for a, v in shist.items()}
    halt = con.execute("SELECT ts, reason FROM signals WHERE asset='*' ORDER BY ts DESC LIMIT 1").fetchone()

    # ---- sentiment rows ----
    srows = con.execute("SELECT ts, asset, score, volume, confidence, summary, sources FROM sentiment ORDER BY ts DESC LIMIT ?",
                        (MAX_SENTIMENT,)).fetchall()
    def urls(s):
        try: return [u for u in json.loads(s or "[]") if isinstance(u, str) and re.match(r"https?://", u)][:8]
        except Exception: return []
    sentiment = [dict(t=ep(r["ts"]), a=r["asset"], s=r["score"], c=r["confidence"], v=r["volume"],
                      sum=r["summary"] or "", src=urls(r["sources"])) for r in srows]
    sent_latest = {}
    for s in sentiment: sent_latest.setdefault(s["a"], s)

    # ---- positions ----
    positions = []
    RP = c.get("riskpot") or {}
    stops = dict(core=(K["stop_loss_pct"], None), day=(DY["stop_loss_pct"], DY["take_profit_pct"]), risk=(RP.get("stop_loss_pct"), None))
    peaks = json.loads((con.execute("SELECT value FROM state WHERE key='risk_peaks'").fetchone() or ["{}"])[0]) or {}
    for r in con.execute("SELECT * FROM positions ORDER BY opened"):
        sl_pct, tp_pct = stops.get(r["sleeve"], (None, None))
        usd = lp[r["asset"]]["price"] if r["asset"] in lp else None
        now_gbp = usd * rate if usd is not None and rate else None
        val = r["qty"] * now_gbp if now_gbp is not None else None
        positions.append(dict(sleeve=r["sleeve"], tranches=r["tranches"], asset=r["asset"], klass=r["klass"], qty=sig(r["qty"]), entry=sig(r["entry"]), now=sig(now_gbp),
                              now_usd=sig(usd), cost=r2(r["cost"]), value=r2(val),
                              pnl=r2(val - r["cost"]) if val is not None else None,
                              pnl_pct=round((now_gbp / r["entry"] - 1) * 100, 2) if now_gbp and r["entry"] else None,
                              stop=sig(r["entry"] * (1 - sl_pct / 100)) if sl_pct else None,
                              take=sig(r["entry"] * (1 + tp_pct / 100)) if tp_pct else None, opened=ep(r["opened"])))
        if r["sleeve"] == "risk":
            pk = peaks.get(r["asset"]); on = bool(pk and pk * (1 - rk["fee_slippage_pct"] / 100) / r["entry"] - 1 >= RP["trail_after_pct"] / 100)
            positions[-1].update(peak=sig(pk), trail_on=on, trail=sig(pk * (1 - RP["trail_pct"] / 100)) if on else None,
                                 trail_arm=sig(r["entry"] * (1 + RP["trail_after_pct"] / 100) / (1 - rk["fee_slippage_pct"] / 100)),
                                 expires=ep(r["opened"]) + int(RP["max_hold_days"] * 86400))
    pos_by = {}
    for p in positions: pos_by.setdefault(p["asset"], []).append(p["sleeve"])

    # ---- trades ----
    trades = [dict(id=r["id"], t=ep(r["ts"]), sleeve=r["sleeve"], side=r["side"], asset=r["asset"], klass=r["klass"], qty=sig(r["qty"]),
                   price=sig(r["price"]), fill=sig(r["fill"]), usd=sig(r["price_usd"]), fx=sig(r["usd_gbp"], 5),
                   value=r2(r["value"]), fee=round(r["cost"], 4) if r["cost"] is not None else None, pnl=r2(r["pnl"]),
                   gas=sig(r["gas"], 3) if r["gas"] is not None else None,
                   reason=r["reason"] or "")
              for r in con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (MAX_TRADES,))]
    POTN = dict(core="Core pot", day="Day pot", risk="Risk pot")
    for i, r in enumerate(con.execute("SELECT rowid, * FROM transfers ORDER BY ts")):   # cash moves between pots (no fees)
        for sl, sgn in ((r["from_sleeve"], -1), (r["to_sleeve"], 1)):
            trades.append(dict(id=-(i * 2 + (sgn > 0) + 1), t=ep(r["ts"]), sleeve=sl, side="TRANSFER", asset="CASH", klass="cash", qty=None,
                               price=None, fill=None, usd=None, fx=None, value=r2(sgn * r["amount"]), fee=0, pnl=None, gas=None,
                               reason=f"£{r['amount']:.2f} cash {POTN.get(r['from_sleeve'], r['from_sleeve'])} -> {POTN.get(r['to_sleeve'], r['to_sleeve'])}: {r['reason']}"))
    trades.sort(key=lambda x: (x["t"], x["id"]), reverse=True)

    # ---- CORE plan / DAY status ----
    plan = json.loads((con.execute("SELECT value FROM state WHERE key='core_plan'").fetchone() or ["{}"])[0])
    cst = json.loads((con.execute("SELECT value FROM state WHERE key='core_stocks'").fetchone() or ["{}"])[0])
    rblog = json.loads((con.execute("SELECT value FROM state WHERE key='core_rebalance_log'").fetchone() or ["null"])[0])
    last_rb = (con.execute("SELECT value FROM state WHERE key='core_last_rebalance'").fetchone() or [None])[0]
    picks = cst.get("picks", [])
    core_t = {a: float(v) for a, v in K["targets_pct"].items()}
    core_t.update({s_: w * 100 for s_, w in stockrank.pick_weights(c, picks).items()})
    try: ranked = stockrank.rank(con, c)
    except Exception: ranked = {}
    th = stockrank.themes(c)
    # ---- asset table ----
    assets = []
    for a, r in sorted(lp.items()):
        if r["klass"] not in ("crypto", "stock"): continue
        s = last_sig.get(a); se = sent_latest.get(a); usd = r["price"]
        spark_pts = [sig(b[4], 5) for b in prices.get(a, [])[-48:]]
        assets.append(dict(a=a, k=r["klass"], focus=a in focus, usd=sig(usd), gbp=sig(usd * rate) if rate else None,
                           chg=chg24(a), src=r["source"], t=ep(r["ts"]),
                           score=s["combined"] if s else None, mom=s["momentum"] if s else None,
                           sent_used=s["sentiment"] if s else None, action=s["action"] if s else None,
                           reason=s["reason"] if s else None,
                           sent=dict(s=se["s"], c=se["c"], t=se["t"]) if se else None,
                           pos=bool(a in pos_by), held=pos_by.get(a, []), core_t=core_t.get(a), spark=spark_pts,
                           theme=th[a][1] if a in th else None, rank=(ranked.get(a) or {}).get("score")))

    # ---- risk / status ----
    risk = json.loads((con.execute("SELECT value FROM state WHERE key='risk'").fetchone() or ["{}"])[0])
    coll = (con.execute("SELECT value FROM state WHERE key='collector_status'").fetchone() or [""])[0]
    s = stats(con)
    cur_eq = s["equity"]; day_start = risk.get("day_start")
    updated = max([ep(x) for x in (risk.get("ts"), eq[-1]["ts"] if eq else None) if x] or [n_ep])
    cash_now = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
    lt = t_now.astimezone(LON)
    rb_day = lt.date() - timedelta(days=(lt.weekday() - int(K["rebalance_weekday"])) % 7)
    hh, mm = map(int, K["rebalance_time_uk"].split(":"))
    rb_at = datetime.combine(rb_day, datetime.min.time().replace(hour=hh, minute=mm), LON)
    if last_rb and datetime.fromisoformat(last_rb.strip('"')) >= rb_at: rb_at += timedelta(days=7)
    sleeves = {}
    for sl in SLEEVES:
        ss = stats(con, sl); sp = [p for p in positions if p["sleeve"] == sl]
        sleeves[sl] = dict(start=r2(sc[sl]), pct=c["sleeve_split_pct"].get(sl, 0), equity=r2(ss["equity"]), ret=round(ss["ret"] * 100, 3),
                           pnl=r2(ss["equity"] - sc[sl]), realized=r2(ss["realized"]), unrealized=r2(sum(p["pnl"] or 0 for p in sp)),
                           win_rate=round(ss["win_rate"] * 100, 1) if ss["win_rate"] is not None else None, closed=ss["closed"],
                           trades=ss["trades"], cash=r2(cash_now.get(sl)), open_n=len(sp), max_dd=round(ss["max_dd"] * 100, 3),
                           invested=r2(sum(p["value"] or 0 for p in sp)))
    ceq = sleeves["core"]["equity"] or 0
    cpos = {p["asset"]: p for p in positions if p["sleeve"] == "core"}
    sleeves["core"].update(
        targets=[dict(a=a, target=round(w, 2), weight=round((cpos[a]["value"] or 0) / ceq * 100, 2) if a in cpos and ceq else 0,
                      value=cpos[a]["value"] if a in cpos else 0, status=plan.get(a, {}).get("status", "building"),
                      n=plan.get(a, {}).get("n", 0)) for a, w in core_t.items()],
        picks=picks, picks_info=cst.get("info", {}), picks_source=cst.get("source"), picks_ts=ep(cst["ts"]) if cst.get("ts") else None,
        stock_pct=float(K["stocks_pct"]), n_slots=stockrank.n_picks(c, ceq), min_pick=float(K.get("min_pick_gbp", 8)),
        max_per_theme=int(K.get("max_per_theme", 2)), weights=K.get("score_weights"),
        ranking=[dict(a=a, **{k: v for k, v in r.items() if k not in ("close", "n")}, pick=a in picks)
                 for a, r in sorted(ranked.items(), key=lambda kv: (not kv[1]["eligible"], -kv[1]["score"]))],
        universe=[dict(key=k, label=v.get("label", k), tickers=v.get("tickers", [])) for k, v in (c["stocks"].get("universe") or {}).items()],
        would_pick=stockrank.select(c, ranked, stockrank.n_picks(c, ceq)) if ranked else [],
        plans=stockrank.plans(c, ranked, ceq, {a: p["value"] or 0 for a, p in cpos.items()}, cash_now.get("core", 0)) if ranked else [],
        last_rebalance=ep(last_rb.strip('"')) if last_rb else None, next_rebalance=int(rb_at.timestamp()), rebalance_log=rblog,
        rules=dict(tranches=K["tranches"], per_day=K["max_tranches_per_day"], dip=K["dip_vs_avg24_pct"], fallback_h=K["fallback_hour_uk"],
                   band=K["drift_band_pp"], stop=K["stop_loss_pct"], sent_delay=K["sentiment_delay_score"],
                   rebalance=f"{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][int(K['rebalance_weekday'])]} {K['rebalance_time_uk']} UK"))
    ds = risk.get("day_sleeve") or {}
    sleeves["day"].update(day_start=r2(ds.get("start")), day_pnl=r2(ds["equity"] - ds["start"]) if ds.get("start") else None,
                          day_pnl_pct=ds.get("pnl_pct"), halted=bool(ds.get("halted")), entries_open=bool(ds.get("entries_open")),
                          flat_now=bool(ds.get("flat_now")),
                          rules=dict(alloc=DY["alloc_pct"], max_open=DY["max_open_positions"], stop=DY["stop_loss_pct"],
                                     take=DY["take_profit_pct"], start=DY["entry_start_uk"], cutoff=DY["entry_cutoff_uk"],
                                     flatten=DY["flatten_uk"], cap=DY["daily_loss_cap_pct"]))
    rst = json.loads((con.execute("SELECT value FROM state WHERE key='riskpot_status'").fetchone() or ["{}"])[0]) or {}
    rsc = json.loads((con.execute("SELECT value FROM state WHERE key='risk_screen'").fetchone() or ["{}"])[0]) or {}
    RC = RP.get("crypto") or {}
    sleeves["risk"].update(
        halted=bool(rst.get("halted")), halt=rst.get("halt"), base=r2(rst.get("base")), dd_pct=rst.get("dd_pct"), new_today=rst.get("new_today"),
        resume_pending=RISK_RESUME.exists(), cands=rst.get("cands") or [], status_t=ep(rst["ts"]) if rst.get("ts") else None,
        screen=dict(t=ep(rsc["ts"]) if rsc.get("ts") else None, universe=rsc.get("universe"), liquid=rsc.get("liquid"), ok=rsc.get("ok"),
                    monad=rsc.get("monad"), top=[{k: (sig(v, 4) if isinstance(v, float) else v) for k, v in d.items()} for d in (rsc.get("top") or [])[:25]]),
        stocks=RP.get("stocks") or [],
        rules=dict(entry=RP.get("entry_score"), pos_pct=RP.get("position_pct"), max_pos=RP.get("max_position_gbp"), max_open=RP.get("max_open"),
                   per_day=RP.get("max_new_per_day"), stop=RP.get("stop_loss_pct"), trail_after=RP.get("trail_after_pct"), trail=RP.get("trail_pct"),
                   hold=RP.get("max_hold_days"), cap=RP.get("drawdown_cap_pct"), cooldown=RP.get("cooldown_h"), top_n=RC.get("top_n"),
                   min_mcap=RC.get("min_mcap_usd"), max_mcap=RC.get("max_mcap_usd"), min_vol=RC.get("min_vol_usd"), sent_block=RP.get("sentiment_block"),
                   sent_exit=RP.get("sentiment_exit")))
    transfers = [dict(t=ep(r["ts"]), frm=r["from_sleeve"], to=r["to_sleeve"], amount=r2(r["amount"]), reason=r["reason"])
                 for r in con.execute("SELECT * FROM transfers ORDER BY ts")]
    gd = json.loads((con.execute("SELECT value FROM state WHERE key='gas'").fetchone() or ["null"])[0]) or {}
    G = c.get("gas") or {}
    gas_tot = con.execute("SELECT COALESCE(SUM(gas),0), COALESCE(SUM(cost),0), COUNT(gas) FROM trades").fetchone()
    gas = dict(gbp=sig(gd.get("gbp"), 3), gwei=gd.get("gwei"), mon=sig(gd.get("mon"), 4), units=gd.get("units", G.get("gas_per_swap")),
               t=ep(gd["ts"]) if gd.get("ts") else None, src=gd.get("src"), fallback=G.get("fallback_gbp"),
               paid=sig(gas_tot[0], 4), fees=r2(gas_tot[1]), n=gas_tot[2])
    return dict(
        v=4, gas=gas, generated=n_ep, updated=updated, currency="GBP", start=start,
        sleeves=sleeves, focus=focus, transfers=transfers,
        fx=dict(rate=sig(rate, 6), gbpusd=round(1 / rate, 4) if rate else None, t=ep(fx[1]) if fx else None,
                source=fx[2] if fx else None, stale=bool(fx[3]) if fx else True,
                alt=round(1 / fx_alt["alt_rate"], 4) if fx_alt and fx_alt["alt_rate"] else None,
                alt_source=fx_alt["alt_source"] if fx_alt else None),
        stats=dict(equity=r2(cur_eq), ret=round(s["ret"] * 100, 3), btc_ret=round(s["btc_ret"] * 100, 3) if s["btc_ret"] is not None else None,
                   realized=r2(s["realized"]), trades=s["trades"], closed=s["closed"],
                   win_rate=round(s["win_rate"] * 100, 1) if s["win_rate"] is not None else None,
                   max_dd=round(s["max_dd"] * 100, 3), best=r2(s["best"]), worst=r2(s["worst"]),
                   unrealized=r2(sum(p["pnl"] or 0 for p in positions)),
                   cash=equity["cash"][-1] if equity["cash"] else None,
                   day_pnl=r2(cur_eq - day_start) if day_start else None, day_pnl_pct=risk.get("day_pnl_pct")),
        risk=dict(halted=bool(risk.get("halted")), day=risk.get("day"), day_start=day_start, kill=KILL.exists(),
                  fx_stale=bool(fx[3]) if fx else True, open_n=len(positions), last_halt=dict(t=ep(halt["ts"]), reason=halt["reason"]) if halt else None,
                  limits=dict(max_open=DY["max_open_positions"], max_pos_pct=DY["max_position_pct"], stop=DY["stop_loss_pct"],
                              take=DY["take_profit_pct"], daily_cap=DY["daily_loss_cap_pct"], fee=rk["fee_slippage_pct"],
                              core_stop=K["stop_loss_pct"], risk_stop=RP.get("stop_loss_pct"), risk_cap=RP.get("drawdown_cap_pct"),
                              min_trade=rk["min_trade_size"], buy=st["buy_threshold"], focus_buy=st["focus_buy_threshold"],
                              sell=st["sell_threshold"])),
        collector=coll,
        mon_check=dict(cg=sig(mon["MON"]), dex=sig(mon["MON.DEX"]), diff=round((mon["MON.DEX"] / mon["MON"] - 1) * 100, 2))
                  if len(mon) == 2 else None,
        equity=equity, positions=positions, trades=trades, assets=assets, prices=prices, fx_hist=fx_hourly,
        signals=sig_hist, reasons=reasons, sentiment=sentiment)

def write_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp"); tmp.write_bytes(data); os.replace(tmp, path)

def main():
    OUT.mkdir(exist_ok=True)
    data = build(db())
    (OUT / ".nojekyll").touch()
    write_atomic(OUT / "data.json", json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
    # static assets: copy templates, cache-bust js/css by content hash in index.html
    ver = {}
    for name in ("app.js", "styles.css"):
        b = (WEB / name).read_bytes(); ver[name] = hashlib.sha1(b).hexdigest()[:10]
        if not (OUT / name).exists() or (OUT / name).read_bytes() != b: write_atomic(OUT / name, b)
    page = (WEB / "index.html").read_text(encoding="utf-8")
    page = page.replace("__APPJS_V__", ver["app.js"]).replace("__CSS_V__", ver["styles.css"])
    if not (OUT / "index.html").exists() or (OUT / "index.html").read_text(encoding="utf-8") != page:
        write_atomic(OUT / "index.html", page.encode())
    print(f"[dashboard] wrote {OUT}/data.json ({(OUT / 'data.json').stat().st_size // 1024} KB) + index.html/app.js/styles.css")

if __name__ == "__main__":
    main()
