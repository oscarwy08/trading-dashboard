"""Export dashboard data (dashboard/data.json) and install the static single-page app
(dashboard/index.html + app.js + styles.css, templates in web/). No AI, no network: pure SQLite -> JSON.
The page loads data.json client-side and re-fetches it every 60 s (works on GitHub Pages).
Each variant database that exists is also written to data_<name>.json, read-only, for the Book menu."""
import hashlib, json, os, re, sqlite3, time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from common import (loans_open, ACCOUNTS, KILL, LON, RISK_RESUME, ROOT, SITE, SLEEVES, acct_of, acct_start, cfg, db, now, rebase,
                    transfer_adj, transfer_adj_acct, usd_gbp)
import daypot, freshness, markets, pricesrc, riskpot, stockrank, trader
from exits import held_quote_view

def plain(a):   # v5: xStock name -> normal ticker (AMDx -> AMD); historical orders keep the xStock name, alias links them
    m = re.match(r"^([A-Z][A-Z0-9.]*)x$", a or ""); return m.group(1) if m else a

OUT = SITE                # docs/ in the repo (GitHub Pages); override with SITE_DIR
WEB = ROOT / "web"
PRICE_DAYS = 14          # OHLC history per asset: hourly for the last PRICE_HOURLY_DAYS, 4-hourly before that
PRICE_HOURLY_DAYS = 7
SIGNAL_DAYS = 7          # signal-score history per asset: hourly for the last 48 h, 3-hourly before that
EQUITY_FULL_H = 48       # keep every equity point for the last 48 h, hourly before that
MAX_TRADES = 500
MAX_SENTIMENT = 300
MAX_REASONS = 12         # per asset: latest signal + recent non-HOLD signals with full reasons

def _hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)

def day_rule_clocks(day_cfg, day_sleeve):
    """Start, cutoff, flatten shown on the Day page. The main book uses the frozen clocks.
    A midnight crypto book stores its own clocks on day_sleeve.session after it has traded."""
    start, cut, flat = day_cfg["entry_start_uk"], day_cfg["entry_cutoff_uk"], day_cfg["flatten_uk"]
    sess = (day_sleeve or {}).get("session") if isinstance(day_sleeve, dict) else None
    if isinstance(sess, dict) and sess.get("midnight_crypto"):
        return sess.get("start") or start, sess.get("cutoff") or cut, sess.get("flatten") or flat, True
    return start, cut, flat, False

def day_overdue_assets(positions, lt, entry_start_uk, flatten_uk, midnight_crypto=False):
    """Day names that should already be flat. The main book uses 22:00 and 08:00 for every name.
    Midnight crypto uses that clock for stocks only. A crypto name is overdue when it was opened
    on an earlier UK day."""
    mins = lt.hour * 60 + lt.minute
    clock = mins >= _hm(flatten_uk) or mins < _hm(entry_start_uk)
    out = []
    for p in positions:
        if p.get("sleeve") != "day":
            continue
        if midnight_crypto and p.get("klass") != "stock":
            opened = p.get("opened")
            if opened and datetime.fromtimestamp(opened, LON).date().isoformat() < lt.date().isoformat():
                out.append(p["asset"])
        elif clock:
            out.append(p["asset"])
    return out

def day_sub_stats(con, lt):
    """v7: per DAY sub-strategy (momentum | burst; pre-v7 day trades count as momentum) closed-trade stats - all time and
    today (UK), for the whole pot and per account: trades, win rate, avg win/loss %, profit factor, P&L after all fees,
    expectancy (£ and % per trade), fees paid (buys + sells incl. gas)."""
    d0 = datetime.combine(lt.date(), datetime.min.time(), LON).astimezone(timezone.utc).isoformat()
    rows = [dict(r) for r in con.execute("SELECT ts, asset, klass, side, value, gas, pnl, cost, COALESCE(sub,'momentum') sub FROM trades "
                                         "WHERE sleeve='day' AND side IN ('BUY','SELL') ORDER BY id")]
    opened = {r[0]: r[1] for r in con.execute("SELECT asset, opened FROM positions WHERE sleeve='day'")}
    for r in rows: r["open"] = r["side"] == "BUY" and r["asset"] in opened and r["ts"] >= opened[r["asset"]]   # buy of a still-open position
    out = {}
    for sb in daypot.SUBS:
        out[sb] = {}
        for per in ("all", "today"):
            for sc in ("all",) + ACCOUNTS:
                xs = [r for r in rows if r["sub"] == sb and (per == "all" or r["ts"] >= d0) and (sc == "all" or acct_of(r["klass"]) == sc)]
                st_ = daypot.sub_stats([r for r in xs if r["side"] == "SELL"])
                fo = sum((r["cost"] or 0) + (r["gas"] or 0) for r in xs if r["open"])
                st_["fees"] = round(sum((r["cost"] or 0) + (r["gas"] or 0) for r in xs), 4)       # all fees paid (closed + still open)
                st_["fees_open"] = round(fo, 4)                                                     # buy fees + gas of positions still open
                st_["fees_closed"] = round(st_["fees"] - fo, 4)                                     # fees of closed trades (already inside their P&L)
                out[sb][f"{per}:{sc}"] = st_
    return out

def risk_stats(con, lt):
    """v7.4 RISK pot closed-trade stats (same model as the DAY sub-strategy table): all time and today (UK), whole pot and
    per account: trades, win rate, avg win/loss %, profit factor, P&L after all fees, expectancy, fees (closed / open buys)."""
    d0 = datetime.combine(lt.date(), datetime.min.time(), LON).astimezone(timezone.utc).isoformat()
    rows = [dict(r) for r in con.execute("SELECT ts, asset, klass, side, value, gas, pnl, cost FROM trades WHERE sleeve='risk' AND side IN ('BUY','SELL') ORDER BY id")]
    opened = {r[0]: r[1] for r in con.execute("SELECT asset, opened FROM positions WHERE sleeve='risk'")}
    for r in rows: r["open"] = r["side"] == "BUY" and r["asset"] in opened and r["ts"] >= opened[r["asset"]]
    out = {}
    for per in ("all", "today"):
        for sc in ("all",) + ACCOUNTS:
            xs = [r for r in rows if (per == "all" or r["ts"] >= d0) and (sc == "all" or acct_of(r["klass"]) == sc)]
            st_ = daypot.sub_stats([r for r in xs if r["side"] == "SELL"])
            fo = sum((r["cost"] or 0) + (r["gas"] or 0) for r in xs if r["open"])
            st_["fees"] = round(sum((r["cost"] or 0) + (r["gas"] or 0) for r in xs), 4); st_["fees_open"] = round(fo, 4)
            st_["fees_closed"] = round(st_["fees"] - fo, 4); out[f"{per}:{sc}"] = st_
    return out

def _setup_name(r):
    sl, sub, reason = r["sleeve"], r["sub"] or "", r["reason"] or ""
    if sl == "day": return "burst" if sub == "burst" or reason.startswith("[burst") else "momentum"
    if sl != "risk": return None
    if sub == "dip" or reason.lower().startswith("risk dip"): return "risk dip"
    return "risk breakout"

def _exit_bucket(reason):
    s = (reason or "").lower()
    if "flatten" in s: return "flatten"
    if "stop-loss" in s or "stop loss" in s: return "stop"
    if "trail" in s: return "trail"
    if "take-profit" in s: return "target"
    if "max hold" in s: return "time"
    if "signal " in s: return "signal"
    return "other"

def setup_scorecard(con, since=None):
    """Closed trades by setup: count, net after fees, fees/gross, win rate, exit mix.
    Gross is net plus the round-trip fees. Fee/gross is omitted when the gross is not positive.
    since (variant comparison): only sells after that timestamp count. Earlier buys stay so their fee still pairs."""
    rows = [dict(r) for r in con.execute(
        "SELECT id, ts, sleeve, asset, sub, side, reason, pnl, cost, gas FROM trades WHERE side IN ('BUY','SELL') ORDER BY id")]
    order = ("momentum", "burst", "risk breakout", "risk dip")
    buys, groups = {}, {k: dict(closed=0, net=0.0, fees=0.0, wins=0, exits={}) for k in order}
    for r in rows:
        k = _setup_name(r)
        if not k: continue
        fee = (r["cost"] or 0) + (r["gas"] or 0)
        key = (k, r["asset"])
        if r["side"] == "BUY":
            buys.setdefault(key, []).append(fee)
        elif since and (r["ts"] or "") <= since:
            continue
        else:
            g = groups[k]
            bf = buys.get(key, []).pop(0) if buys.get(key) else 0.0
            g["closed"] += 1; g["net"] += r["pnl"] or 0; g["fees"] += fee + bf
            if (r["pnl"] or 0) > 0: g["wins"] += 1
            ex = _exit_bucket(r["reason"]); g["exits"][ex] = g["exits"].get(ex, 0) + 1
    out = []
    for k in order:
        g = groups[k]; n = g["closed"]; gross = g["net"] + g["fees"]
        out.append(dict(setup=k, closed=n, net=round(g["net"], 4), fees=round(g["fees"], 4), gross=round(gross, 4),
                        fee_gross=round(g["fees"] / gross, 3) if gross > 0.005 else None,
                        win_rate=round(100.0 * g["wins"] / n, 1) if n else None, exits=g["exits"]))
    return out

def day_ledger(con, c, positions, acash, base):
    """v7.1 DAY sub-strategy money per account (daypot.sub_ledger - the SAME definition the trader sizes with), rounded so
    every figure reconciles to the penny with the Day pot cards:
      budget (= equity) = the sub-strategy's own money incl. its open positions at market = cash + value
      cash / free       = its money NOT tied up in open positions (free = what it may still spend, capped by the account cash)
      value             = market value of its open positions (sum of the rounded position values shown in the table)
    Per account: momentum cash + burst cash = the Day pot's cash in that account, momentum + burst budget = its Day value.
    'all' = the two accounts summed. P&L: realised (closed trades, after all fees) + open (value - cost incl. buy fees)."""
    DY = c["day"]; sh = daypot.sub_shares(DY); since = (json.loads((con.execute("SELECT value FROM state WHERE key='account_split'").fetchone() or ["null"])[0]) or {}).get("ts", "")
    dp = [p for p in positions if p["sleeve"] == "day"]
    raw = {(r["asset"]): r["cost"] for r in con.execute("SELECT asset, cost FROM positions WHERE sleeve='day'")}
    real = daypot.sub_realized(con, since); LO = loans_open(con)
    lent_ac = {ac: sum(l["out"] for l in LO if l["lender"] == "day" and l["account"] == ac) for ac in ACCOUNTS}     # v7.3 loans
    L = daypot.sub_ledger({ac: base["day"][ac] for ac in ACCOUNTS}, {ac: acash.get(("day", ac), 0.0) for ac in ACCOUNTS},
                          [dict(sub=p.get("sub", "momentum"), acct=p["acct"], cost=raw.get(p["asset"], p["cost"]), value=p["value"]) for p in dp], real, sh, ACCOUNTS,
                          borrowed={(sb, ac): sum(l["out"] for l in LO if l["borrower"] == "day" and l["account"] == ac and l["sub"] == sb) for sb in daypot.SUBS for ac in ACCOUNTS},
                          lent=lent_ac)
    R2 = lambda x: round(x + 0.0, 2); out = {}
    for ac in ACCOUNTS:
        cash_ac = R2(acash.get(("day", ac), 0.0)); cell = {}; left = cash_ac; left_l = R2(lent_ac[ac])
        for i, sb in enumerate(daypot.SUBS):
            ps = [p for p in dp if p["acct"] == ac and p.get("sub", "momentum") == sb]
            val = R2(sum(p["value"] or 0 for p in ps)); cost = R2(sum(p["cost"] or 0 for p in ps))
            cash_ = left if i == len(daypot.SUBS) - 1 else R2(L[ac][sb]["cash"]); left = R2(left - cash_)
            st_ = R2(sh[sb] * base["day"][ac])
            ow = R2(L[ac][sb]["owed"]); ln = left_l if i == len(daypot.SUBS) - 1 else R2(L[ac][sb]["lent"]); left_l = R2(left_l - ln)
            eq_ = R2(cash_ + val + ln - ow)          # v7.3: + its share of money the Day pot lent - what it borrowed
            cell[sb] = dict(start=st_, budget=eq_, cash=cash_, free=R2(max(0.0, min(cash_, cash_ac))), value=val, cost=cost, owed=ow, lent=ln,
                            open_pnl=R2(sum(p["pnl"] or 0 for p in ps)), realized=R2(real.get((sb, ac), 0.0)), pnl=R2(eq_ - st_), open_n=len(ps))
        cell["residual"] = round(L[ac]["residual"], 4); out[ac] = cell
    out["all"] = {sb: {k: (sum(out[ac][sb][k] for ac in ACCOUNTS) if k == "open_n" else R2(sum(out[ac][sb][k] for ac in ACCOUNTS)))
                       for k in out[ACCOUNTS[0]][sb]} for sb in daypot.SUBS}
    out["shares"] = {sb: round(v * 100, 2) for sb, v in sh.items()}
    return out

def _as_epoch(ts):
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        d = datetime.fromisoformat(str(ts))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def test_since(con):
    """Timestamp the current test opened, if this book recorded one. Earlier equity is a different test."""
    try:
        row = con.execute("SELECT value FROM state WHERE key='test_generation'").fetchone()
        blob = json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None
    if not isinstance(blob, dict):
        return None
    return blob.get("started") or blob.get("ts")


def partial_equity_point(ts, value, rows, start):
    """A sleeve-only snapshot sitting next to the full book. best_of_plus writes £30 for the
    day/risk slice, then the whole £200 a few seconds later. That £30 is not a drawdown."""
    if value is None or start is None or float(value) >= float(start) * 0.5:
        return False
    t = _as_epoch(ts)
    if t is None:
        return False
    for ts2, v2 in rows:
        if v2 is None:
            continue
        t2 = _as_epoch(ts2)
        if t2 is None or abs(t2 - t) > 180:
            continue
        if float(v2) >= max(float(start) * 0.8, float(value) * 2):
            return True
    return False


def curve_drawdown(rows, start=None):
    """Peak-to-trough of one book's equity since the test opened.

    `rows` is (timestamp, equity) in time order. The peak starts at `start`, the Test 2
    opening equity, so a book that leaves £200 and comes back is measured from £200.
    A partial sleeve row next to a full-book row is left out.
    """
    kept = [(ts, v) for ts, v in rows if v is not None and not partial_equity_point(ts, v, rows, start)]
    peak = float(start) if start else 0.0
    mdd = 0.0
    for _, v in kept:
        v = float(v)
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1)
    return mdd


def stats(con, sleeve=None, account=None):
    """Stats for the whole paper money (sleeve=None, account=None), one pot ('core'/'day'/'risk'), one account
    ('crypto' = Monad wallet / 'stocks' = Trading 212, v6) or one pot x account cell. Also used by daily_summary.py.
    Equity history is rebased by later cash TRANSFERs (common.transfer_adj / transfer_adj_acct) so moves are not P&L;
    start = the current baseline (opening at the v6 split + net later transfers, common.acct_start)."""
    c = cfg(); base = acct_start(con, c)
    if account:
        adj = transfer_adj_acct(con, account, sleeve)
        rows = con.execute(f"SELECT ts, {account}, btc, accts FROM equity ORDER BY ts").fetchall()
        if sleeve:
            def cell(r):
                try: return json.loads(r[3])[sleeve][account][0] if r[3] else None
                except Exception: return None
            eq = [(r[0], v + rebase(r[0], adj), r[2]) for r in rows for v in [cell(r)] if v is not None]
        else:
            eq = [(r[0], r[1] + rebase(r[0], adj), r[2]) for r in rows if r[1] is not None]
        kl = "('stock')" if account == "stocks" else "('crypto')"
        w, a = (" AND sleeve=?", (sleeve,)) if sleeve else ("", ())
        sells = [r[0] for r in con.execute(f"SELECT pnl FROM trades WHERE side='SELL' AND (CASE WHEN klass='stock' THEN 'stock' ELSE 'crypto' END) IN {kl}{w}", a)]
        nt = con.execute(f"SELECT COUNT(*) FROM trades WHERE side IN ('BUY','SELL') AND (CASE WHEN klass='stock' THEN 'stock' ELSE 'crypto' END) IN {kl}{w}", a).fetchone()[0]
        start = base[sleeve][account] if sleeve else sum(base[s_][account] for s_ in SLEEVES)
        since = test_since(con)
        eq = [r for r in eq if not since or (r[0] or "") >= since]
        mdd = curve_drawdown([(r[0], r[1]) for r in eq], start)
        return dict(start=start, equity=eq[-1][1] if eq else start, ret=(eq[-1][1] / start - 1) if eq and start else 0,
                    realized=sum(x or 0 for x in sells), trades=nt, closed=len(sells),
                    win_rate=(sum((x or 0) > 0 for x in sells) / len(sells)) if sells else None, max_dd=mdd, btc_ret=None,
                    best=max(sells, default=None), worst=min(sells, default=None))
    col = {None: "equity", "core": "core", "day": "day", "risk": "risk"}[sleeve]
    adj = transfer_adj(con, sleeve) if sleeve else []
    eq = [(r[0], r[1] + rebase(r[0], adj), r[2]) for r in con.execute(f"SELECT ts, {col}, btc FROM equity ORDER BY ts").fetchall()
          if r[1] is not None]
    q = "SELECT pnl FROM trades WHERE side='SELL'" + (" AND sleeve=?" if sleeve else "")
    sells = [r[0] for r in con.execute(q, (sleeve,) if sleeve else ())]
    start = float(c["starting_balance"]) if not sleeve else sum(base[sleeve].values())
    since = test_since(con)
    eq = [r for r in eq if not since or (r[0] or "") >= since]
    # Only the whole-book equity column has the sleeve-only ghost rows. A pot's own column is already that pot.
    mdd = curve_drawdown([(r[0], r[1]) for r in eq], None if sleeve else start)
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

_PEAK_RE = re.compile(r"peak \+([+-]?\d+(?:\.\d+)?)%")

def sell_basis_pct(value, gas, pnl):
    """Realised P&L as a percent of what the sold slice cost, including buy fees.
    A sell stores the fill value, the sell gas and the P&L. Cost basis is value minus gas minus P&L."""
    if pnl is None or value is None:
        return None
    basis = float(value) - float(gas or 0) - float(pnl)
    if basis <= 0.005:
        return None
    return round(float(pnl) / basis * 100, 2)

def peak_pct_of(reason):
    """Best gain during the hold, when a trailing exit wrote 'peak +6.1%' into the reason. Otherwise None."""
    m = _PEAK_RE.search(reason or "")
    return float(m.group(1)) if m else None

def sell_text(pnl, pct, peak=None, asset=None):
    """'+£0.12 (+2.4%), peak +6.1%'. asset prefixes the coin when given."""
    if pnl is None:
        return None
    sign = "+" if pnl > 0.0049 else ("-" if pnl < -0.0049 else "")
    s = f"{sign}£{abs(float(pnl)):.2f}"
    if pct is not None:
        s += f" ({pct:+.1f}%)"
    if peak is not None:
        s += f", peak {peak:+.1f}%"
    return f"{asset} {s}" if asset else s

def parse_snap(raw):
    """Entry snapshot stored on a buy and copied onto its sell: confidence or score, components, p, target %, loss %, costs, EV.
    None when that trade was bought before this was stored."""
    if not raw: return None
    if isinstance(raw, dict): d = raw
    else:
        try: d = json.loads(raw)
        except Exception: return None
    if not isinstance(d, dict) or d.get("target") is None or d.get("ev") is None: return None
    return d

def live_ev(p, now, entry, target_pct, loss_pct, trail_px, exit_cost):
    """(EV now, target price, stop-or-trail price). Remaining upside is entry to the stored target %; remaining downside
    is to the live trail once it is armed, otherwise to the stored loss %. The sell cost still to pay is subtracted once."""
    if p is None or not now or not entry or target_pct is None or loss_pct is None: return None, None, None
    tgt = sig(float(entry) * (1 + float(target_pct) / 100.0))
    stop = sig(float(trail_px) if trail_px else float(entry) * (1 - float(loss_pct) / 100.0))
    cost = round(float(exit_cost or 0), 4)
    if not tgt or not stop: return None, None, None
    return daypot.ev_now(p, now, tgt, stop, cost), tgt, stop

def pnl_parts(headline, closed):
    """Split a headline P&L into open positions and closed trades so the two sum to the headline exactly (integer pence).
    Fees are already inside both numbers; they are not a third component."""
    h = int(round(float(headline or 0) * 100)); c = int(round(float(closed or 0) * 100))
    return {"open": round((h - c) / 100.0, 2), "closed": round(c / 100.0, 2)}

def build(con, variant_report=None):
    c = cfg(); st, rk, K, DY = c["strategy"], c["risk"], c["core"], c["day"]
    base = acct_start(con, c); sc = {s_: sum(base[s_].values()) for s_ in SLEEVES}
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
    eq = con.execute("SELECT ts, equity, cash, pos_value, btc, open_n, core, day, risk, crypto, stocks FROM equity ORDER BY ts").fetchall()
    btc0 = next((r["btc"] for r in eq if r["btc"]), None)
    adj = {sl: transfer_adj(con, sl) for sl in SLEEVES}
    adj.update({ac: transfer_adj_acct(con, ac) for ac in ACCOUNTS})
    reb = lambda r, sl: r2(r[sl] + rebase(r["ts"], adj[sl])) if r[sl] is not None else None   # pot/account history rebased by later transfers
    cut = n_ep - EQUITY_FULL_H * 3600; seen = {}
    for r in eq:
        t = ep(r["ts"]); key = t if t >= cut else t // 3600   # older points: last one per hour
        seen[key] = (t, r2(r["equity"]), r2(start * r["btc"] / btc0) if btc0 and r["btc"] else None, r2(r["cash"]),
                     reb(r, "core"), reb(r, "day"), reb(r, "risk"), reb(r, "crypto"), reb(r, "stocks"))
    pts = sorted(seen.values())[-3000:]
    equity = dict(t=[p[0] for p in pts], eq=[p[1] for p in pts], btc=[p[2] for p in pts], cash=[p[3] for p in pts],
                  core=[p[4] for p in pts], day=[p[5] for p in pts], risk=[p[6] for p in pts], crypto=[p[7] for p in pts], stocks=[p[8] for p in pts])

    # ---- latest prices / 24h change ----
    latest = con.execute("""SELECT asset, klass, price, source, ts, source_ts FROM (
                              SELECT asset, klass, price, source, ts, source_ts,
                                     ROW_NUMBER() OVER (PARTITION BY asset ORDER BY ts DESC, rowid DESC) rn
                              FROM prices WHERE source!='backfill') WHERE rn=1""").fetchall()
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
    stops = dict(core=(K["stop_loss_pct"], None), day=(None, None), risk=(RP.get("stop_loss_pct"), None))
    dmeta = json.loads((con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone() or ["{}"])[0]) or {}
    peaks = json.loads((con.execute("SELECT value FROM state WHERE key='risk_peaks'").fetchone() or ["{}"])[0]) or {}
    rmeta = json.loads((con.execute("SELECT value FROM state WHERE key='risk_meta'").fetchone() or ["{}"])[0]) or {}   # v7.4 conf/EV at entry
    buy_hist, latest_buy = {}, {}
    if "snap" in {x[1] for x in con.execute("PRAGMA table_info(trades)")}:
        for br in con.execute("SELECT id, sleeve, asset, snap FROM trades WHERE side='BUY' AND sleeve IN ('day','risk') ORDER BY id"):
            buy_hist.setdefault((br["sleeve"], br["asset"]), []).append((br["id"], parse_snap(br["snap"])))
        latest_buy = {k: v[-1][1] for k, v in buy_hist.items()}
    def attach_entry(row, prec):
        """Entry snapshot plus EV now. A day/risk position with no snapshot still gets EV now (p = 0.5, the pot's target and stop)."""
        if prec["sleeve"] not in ("day", "risk"): return
        meta = (dmeta if prec["sleeve"] == "day" else rmeta).get(prec["asset"]) or {}
        snap = parse_snap(meta.get("snap")) or latest_buy.get((prec["sleeve"], prec["asset"]))
        exit_cost = trader.cost_pct(c, prec["asset"], prec["klass"], "SELL") * 100
        trail_px = row.get("trail") if row.get("trail_on") else None
        if snap:
            p_, tgt_pct, loss_pct = snap.get("p", 0.5), snap.get("target"), snap.get("loss")
        elif prec["sleeve"] == "risk":
            snap = None
            p_ = 0.5
            tgt_pct = float(RP.get("trail_after_pct", 15)) * (1 - float(RP.get("trail_pct", 10)) / 100.0)
            loss_pct = float(RP.get("stop_loss_pct", 12))
        else:
            md_ = dmeta.get(prec["asset"]) or {}
            snap = None
            if md_.get("tp") is not None and md_.get("sl") is not None:
                p_, tgt_pct, loss_pct = 0.5, md_.get("tp"), md_.get("sl")
            else:
                p_, tgt_pct, loss_pct = None, None, None
        evn, tgt, stop = live_ev(p_, row.get("now"), prec["entry"], tgt_pct, loss_pct, trail_px, exit_cost)
        row.update(snap=snap, ev_now=evn, ev_tgt=tgt, ev_stop=stop,
                   ev_p=None if p_ is None else round(float(p_), 4), ev_cost=round(exit_cost, 4) if evn is not None else None)
    for r in con.execute("SELECT * FROM positions ORDER BY opened"):
        sl_pct, tp_pct = stops.get(r["sleeve"], (None, None))
        usd = lp[r["asset"]]["price"] if r["asset"] in lp else None
        now_gbp = usd * rate if usd is not None and rate else None
        val = r["qty"] * now_gbp if now_gbp is not None else None
        src = lp.get(r["asset"])
        qts = (src["source_ts"] or src["ts"]) if src else None
        dead_m = float(rk.get("price_dead_min", 180))
        # A quote older than price_dead_min is still shown (so the totals reconcile). Crypto, and a
        # stock while its market is open, are marked unpriced when that age is past the window
        # (a stock counts only open-session minutes). A closed stock market shows the last close.
        view = held_quote_view(r["klass"], r["asset"], qts, t_now, dead_m, has_quote=bool(src))
        age_m, open_age = view["price_age_min"], view["open_age_min"]
        positions.append(dict(sleeve=r["sleeve"], acct=acct_of(r["klass"]), tranches=r["tranches"], asset=r["asset"], klass=r["klass"], qty=sig(r["qty"]), entry=sig(r["entry"]), now=sig(now_gbp),
                              now_usd=sig(usd), cost=r2(r["cost"]), value=r2(val),
                              pnl=r2(r2(val) - r2(r["cost"])) if val is not None else None,   # = shown value - shown cost (reconciles)
                              pnl_pct=round((now_gbp / r["entry"] - 1) * 100, 2) if now_gbp and r["entry"] else None,
                              unpriced=view["unpriced"], market_closed=view["market_closed"],
                              price_age_min=round(age_m, 1) if age_m is not None else None,
                              open_age_min=round(open_age, 1) if open_age is not None else None,
                              stop=sig(r["entry"] * (1 - sl_pct / 100)) if sl_pct else None,
                              take=sig(r["entry"] * (1 + tp_pct / 100)) if tp_pct else None, opened=ep(r["opened"])))
        if r["sleeve"] == "day":     # v7: per-position ATR-scaled levels (prices at which the net-of-sell-cost P&L hits them)
            md = dmeta.get(r["asset"]) or {}; SC = DY.get(md.get("sub", "momentum")) or {}
            fs = trader.cost_pct(c, r["asset"], r["klass"], "SELL"); lvl = lambda x: sig(r["entry"] * (1 + x / 100) / (1 - fs))
            pkg = (md["peak"] * (1 - fs) / r["entry"] - 1) * 100 if md.get("peak") else None
            gb = float(SC.get("runner_giveback_pct", 20) if md.get("runner") else SC.get("trail_giveback_pct", 40))
            ton = pkg is not None and pkg >= float(SC.get("trail_after_pct", 99))
            positions[-1].update(sub=md.get("sub", "momentum"), stop=lvl(-md["sl"]) if md.get("sl") else None, take=lvl(md["tp"]) if md.get("tp") else None,
                                 tp_pct=md.get("tp"), sl_pct=md.get("sl"), atr=md.get("atr"), conf=md.get("conf"), ev=md.get("ev"), score=md.get("score"), runner=bool(md.get("runner")),
                                 peak_pct=round(pkg, 2) if pkg is not None else None, trail_on=ton, trail=lvl(pkg * (1 - gb / 100)) if ton else None,
                                 expires=ep(r["opened"]) + int(float(SC["max_hold_h"]) * 3600) if SC.get("max_hold_h") else None)
        if r["sleeve"] == "risk":
            pk = peaks.get(r["asset"]); on = bool(pk and pk * (1 - rk["fee_slippage_pct"] / 100) / r["entry"] - 1 >= RP["trail_after_pct"] / 100)
            positions[-1].update(peak=sig(pk), trail_on=on, trail=sig(pk * (1 - RP["trail_pct"] / 100)) if on else None,
                                 trail_arm=sig(r["entry"] * (1 + RP["trail_after_pct"] / 100) / (1 - rk["fee_slippage_pct"] / 100)),
                                 expires=ep(r["opened"]) + int(RP["max_hold_days"] * 86400),
                                 conf=(rmeta.get(r["asset"]) or {}).get("conf"), ev=(rmeta.get(r["asset"]) or {}).get("ev"),
                                 setup=(rmeta.get(r["asset"]) or {}).get("setup"), why=(rmeta.get(r["asset"]) or {}).get("why"))
        attach_entry(positions[-1], r)
    pos_by = {}
    for p in positions: pos_by.setdefault(p["asset"], []).append(p["sleeve"])

    # ---- trades ----
    # ---- fees per account (crypto: fee + gas; stocks: spread + FX + stamp duty; early tokenised-stock trades: fee + gas) ----
    fees = {s_: {ac: dict(fee=0.0, gas=0.0, spread=0.0, fx=0.0, stamp=0.0, xstock=0.0) for ac in ACCOUNTS} for s_ in SLEEVES}
    def fparts(r):
        cost = r["cost"] or 0.0; gas_ = r["gas"] or 0.0
        if r["klass"] != "stock": return dict(fee=cost, gas=gas_)
        if gas_ > 0 or re.match(r"^[A-Z][A-Z0-9.]*x$", r["asset"] or ""): return dict(xstock=cost + gas_)   # pre-v5 xStock trade on Monad
        pp = trader.cost_parts(c, r["asset"], "stock", r["side"]); tot = sum(pp.values()) or 1
        return {k: cost * v / tot for k, v in pp.items()}
    tparts = {}
    for r in con.execute("SELECT id, sleeve, asset, klass, side, cost, gas FROM trades WHERE side IN ('BUY','SELL')"):
        fp_ = fparts(r); tparts[r["id"]] = fp_
        for k, v in fp_.items(): fees[r["sleeve"]][acct_of(r["klass"])][k] += v
    trades = [dict(id=r["id"], t=ep(r["ts"]), sleeve=r["sleeve"], acct=acct_of(r["klass"]), side=r["side"], asset=r["asset"], klass=r["klass"], qty=sig(r["qty"]),
                   price=sig(r["price"]), fill=sig(r["fill"]), usd=sig(r["price_usd"]), fx=sig(r["usd_gbp"], 5),
                   value=r2(r["value"]), fee=round(r["cost"], 4) if r["cost"] is not None else None, pnl=r2(r["pnl"]),
                   pnl_pct=sell_basis_pct(r["value"], r["gas"], r["pnl"]) if r["side"] == "SELL" else None,
                   peak_pct=peak_pct_of(r["reason"]) if r["side"] == "SELL" else None,
                   gas=sig(r["gas"], 3) if r["gas"] is not None else None,
                   reason=r["reason"] or "", sub=(r["sub"] or ("momentum" if r["sleeve"] == "day" else None)), conf=r["conf"], alias=plain(r["asset"]) if plain(r["asset"]) != r["asset"] else None,
                   snap=parse_snap(r["snap"]) if "snap" in r.keys() else None,
                   fees={k: sig(v, 3) for k, v in tparts.get(r["id"], {}).items() if v})
              for r in con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (MAX_TRADES,))]
    open_by = {(p["sleeve"], p["asset"]): p for p in positions}
    def gbp_of(asset):
        usd = lp[asset]["price"] if asset in lp else None
        return sig(usd * rate) if usd is not None and rate else None
    for trow in trades:
        if trow.get("sleeve") not in ("day", "risk") or trow.get("side") not in ("BUY", "SELL"): continue
        snap = trow.get("snap")
        if not snap and trow["side"] == "SELL":
            prev = [(i, s) for i, s in buy_hist.get((trow["sleeve"], trow["asset"]), []) if i < trow["id"]]
            snap = prev[-1][1] if prev else None
            trow["snap"] = snap
        held = open_by.get((trow["sleeve"], trow["asset"]))
        if held and trow["side"] == "BUY" and held.get("ev_tgt"):
            trow.update(ev_now=held.get("ev_now"), ev_tgt=held.get("ev_tgt"), ev_stop=held.get("ev_stop"),
                        ev_p=held.get("ev_p"), ev_cost=held.get("ev_cost"), ev_px=held.get("now"))
            continue
        exit_cost = trader.cost_pct(c, trow["asset"], trow.get("klass") or "crypto", "SELL") * 100
        if snap:
            p_, tgt_pct, loss_pct = snap.get("p", 0.5), snap.get("target"), snap.get("loss")
            entry_px = snap.get("entry") or trow.get("fill")
        elif trow["sleeve"] == "risk" and trow.get("fill"):
            p_ = 0.5
            tgt_pct = float(RP.get("trail_after_pct", 15)) * (1 - float(RP.get("trail_pct", 10)) / 100.0)
            loss_pct = float(RP.get("stop_loss_pct", 12))
            entry_px = trow.get("fill")
        else:
            continue
        evn, tgt, stop = live_ev(p_, gbp_of(trow["asset"]), entry_px, tgt_pct, loss_pct, None, exit_cost)
        trow.update(ev_now=evn, ev_tgt=tgt, ev_stop=stop, ev_p=None if p_ is None else round(float(p_), 4),
                    ev_cost=round(exit_cost, 4) if evn is not None else None, ev_px=gbp_of(trow["asset"]))
    POTN = dict(core="Core pot", day="Day pot", risk="Risk pot"); ACN = dict(crypto="Crypto account", stocks="Stocks account")
    for i, r in enumerate(con.execute("SELECT rowid, * FROM transfers ORDER BY ts")):   # cash moves (no fees): pots / accounts
        fa, ta = r["from_account"], r["to_account"]
        lbl = lambda sl, ac: POTN.get(sl, sl) + (f" ({ACN[ac]})" if ac else "")
        for sl, ac, sgn in ((r["from_sleeve"], fa, -1), (r["to_sleeve"], ta, 1)):
            trades.append(dict(id=-(i * 2 + (sgn > 0) + 1), t=ep(r["ts"]), sleeve=sl, acct=ac, kind=r["kind"] or "pot", side="TRANSFER", asset="CASH", klass="cash", qty=None,
                               price=None, fill=None, usd=None, fx=None, value=r2(sgn * r["amount"]), fee=0, pnl=None, gas=None,
                               reason=f"£{r['amount']:.2f} cash {lbl(r['from_sleeve'], fa)} -> {lbl(r['to_sleeve'], ta)}: {r['reason']}"))
    # v7.3 inter-pot loans: LOAN rows (borrower +, lender -) and REPAY rows (principal back), like TRANSFER rows (no fees)
    for r in con.execute("SELECT * FROM loans ORDER BY id"):
        sub_ = f" ({r['sub']})" if r["sub"] else ""
        for sl, sgn in ((r["borrower"], 1), (r["lender"], -1)):
            trades.append(dict(id=-(10 ** 6 + r["id"] * 4 + (sgn > 0)), t=ep(r["ts"]), sleeve=sl, acct=r["account"], kind="loan", side="LOAN", asset=r["asset"], klass="cash",
                               qty=None, price=None, fill=None, usd=None, fx=None, value=r2(sgn * r["amount"]), fee=0, pnl=None, gas=None, loan_id=r["id"],
                               reason=f"{POTN.get(r['borrower'])}{sub_} borrowed £{r['amount']:.2f} from {POTN.get(r['lender'])} ({ACN[r['account']]}) for {r['asset']}" + (f" - {r['reason'].split(': ', 1)[-1]}" if r["reason"] else "")))
            if (r["repaid"] or 0) > 0.004:
                trades.append(dict(id=-(10 ** 6 + r["id"] * 4 + 2 + (sgn > 0)), t=ep(r["repaid_ts"] or r["ts"]), sleeve=sl, acct=r["account"], kind="loan", side="REPAY", asset=r["asset"], klass="cash",
                                   qty=None, price=None, fill=None, usd=None, fx=None, value=r2(-sgn * r["repaid"]), fee=0, pnl=None, gas=None, loan_id=r["id"],
                                   reason=f"{POTN.get(r['borrower'])}{sub_} repaid £{r['repaid']:.2f} of £{r['amount']:.2f} to {POTN.get(r['lender'])} ({ACN[r['account']]}) - {r['asset']} closed"
                                          + ("" if r["status"] == "repaid" else f"; £{r['amount'] - r['repaid']:.2f} still owed")))
    trades.sort(key=lambda x: (x["t"], x["id"]), reverse=True)

    # ---- CORE plan / DAY status ----
    plan = json.loads((con.execute("SELECT value FROM state WHERE key='core_plan'").fetchone() or ["{}"])[0])
    cst = json.loads((con.execute("SELECT value FROM state WHERE key='core_stocks'").fetchone() or ["{}"])[0])
    rblog = json.loads((con.execute("SELECT value FROM state WHERE key='core_rebalance_log'").fetchone() or ["null"])[0])
    last_rb = (con.execute("SELECT value FROM state WHERE key='core_last_rebalance'").fetchone() or [None])[0]
    picks = cst.get("picks", [])
    ccy = json.loads((con.execute("SELECT value FROM state WHERE key='core_crypto'").fetchone() or ["{}"])[0]) or {}
    crypto_picks = ccy.get("picks") or []
    core_tg = trader.core_targets(c, picks, crypto_picks)        # {asset: (account, weight within that account's Core money)}
    core_t = {a: w * 100 for a, (ac, w) in core_tg.items()}
    try: ranked = stockrank.rank(con, c)
    except Exception: ranked = {}
    try: cranked = stockrank.rank_crypto(con, c)
    except Exception: cranked = {}
    th = stockrank.themes(c); watch = stockrank.watch_themes(c); rset = set(stockrank.risk_stocks(c))
    smeta = json.loads((con.execute("SELECT value FROM state WHERE key='stock_meta'").fetchone() or ["{}"])[0]) or {}
    CC = c["crypto"]; cth = CC.get("watch_themes") or {}
    cg_sym = {json.loads(v) if v.startswith('"') else v: k[5:] for k, v in con.execute("SELECT key, value FROM state WHERE key LIKE 'cgid:%'")}
    ctheme = {cg_sym[i]: tk for i, tk in (CC.get("watch_ids") or {}).items() if i in cg_sym}     # e.g. PAXG -> rwa
    t_mk = now()
    # v7.2: DAY crypto universe tiers for the Markets tab + warm-up (hours of stored price history vs crypto.warmup_h)
    DU = json.loads((con.execute("SELECT value FROM state WHERE key='day_universe'").fetchone() or ["{}"])[0]) or {}
    dx_ = json.loads((con.execute("SELECT value FROM state WHERE key='day_excluded'").fetchone() or ["{}"])[0]) or {}
    du = DU.get("coins") or {}; risk_only = set(json.loads((con.execute("SELECT value FROM state WHERE key='risk_only'").fetchone() or ["[]"])[0]) or [])
    warm_h = float(CC.get("warmup_h", st["min_history_h"])); since72 = (t_mk - timedelta(hours=72)).isoformat()
    first_ts = {r[0]: r[1] for r in con.execute("SELECT asset, MIN(ts) FROM prices WHERE ts>=? AND source!='kyberswap' AND klass='crypto' GROUP BY asset", (since72,))}
    def warm(a):
        f = first_ts.get(a)
        if not f or a in dx_ or (a in risk_only and a not in du): return None    # warm-up only matters for coins the DAY pot trades
        h = (t_mk - datetime.fromisoformat(f)).total_seconds() / 3600
        return round(h, 1) if h < warm_h else None
    cg_core = {cg_sym[i] for i in CC["core_ids"] if i in cg_sym}
    def ctier(a):
        if a in ctheme: return ctheme[a]
        if a in risk_only: return "crypto_risk"
        r = (du.get(a) or [None, None])[1]
        return "crypto_mid" if r and r > 40 and a not in cg_core else "crypto_large"
    CTL = dict(crypto_large="Crypto - top tokens & core coins", crypto_mid="Crypto - liquid mid & small caps (DAY universe)", crypto_risk="Crypto - Risk pot screen only")
    def tinfo(a, k):
        if k == "stock":
            tk, tl = th.get(a, (None, None))
            if not tk and a in c["stocks"]["watchlist"]: tk, tl = "watchlist", "Market watchlist"
            if not tk and a in rset: tk, tl = "risk_extra", "Risk pot small caps"
            m = smeta.get(a) or {}
            return dict(tk=tk, theme=tl, watch=tk in watch, ex=markets.exchange(a), open=markets.is_open(a, t_mk), cur=m.get("cur"),
                        name=m.get("name"), risk_u=a in rset)
        tk = ctier(a); u = du.get(a)
        return dict(tk=tk, theme=cth.get(tk) or CTL.get(tk, "Crypto"), watch=tk in cth, ex="crypto", open=True, day_u=(bool(u) or a not in risk_only) and a not in dx_, day_x=dx_.get(a),
                    cg_rank=u[1] if u else None, mcap=u[2] if u else None, warm=warm(a), warm_h=warm_h)
    # ---- asset table ----
    assets = []
    for a, r in sorted(lp.items()):
        if r["klass"] not in ("crypto", "stock"): continue
        s = last_sig.get(a); se = sent_latest.get(a); usd = r["price"]
        spark_pts = [sig(b[4], 5) for b in prices.get(a, [])[-48:]]
        age = freshness.age_min(r["source_ts"] or r["ts"], t_now)
        src_ep = ep(freshness.to_iso(r["source_ts"]) or r["ts"]) if (r["source_ts"] or r["ts"]) else None
        assets.append(dict(a=a, k=r["klass"], acct=acct_of(r["klass"]), focus=a in focus, usd=sig(usd), gbp=sig(usd * rate) if rate else None,
                           chg=chg24(a), src=r["source"], t=ep(r["ts"]), src_t=src_ep, age_min=round(age, 1) if age is not None else None,
                           score=s["combined"] if s else None, mom=s["momentum"] if s else None,
                           sent_used=s["sentiment"] if s else None, action=s["action"] if s else None,
                           reason=s["reason"] if s else None,
                           sent=dict(s=se["s"], c=se["c"], t=se["t"]) if se else None,
                           pos=bool(a in pos_by), held=pos_by.get(a, []), core_t=core_t.get(a), spark=spark_pts,
                           rank=(ranked.get(a) or {}).get("score"), **tinfo(a, r["klass"])))
    have = {x["a"] for x in assets}       # universe stocks without intraday prices: last daily close (Markets shows every theme)
    for a in list(th) + [x for x in stockrank.risk_stocks(c) if x not in th]:
        if a in have: continue
        dc = con.execute("SELECT d, close FROM daily_closes WHERE asset=? ORDER BY d DESC LIMIT 22", (a,)).fetchall()
        if not dc: continue
        usd = dc[0][1]; chg = round((usd / dc[1][1] - 1) * 100, 2) if len(dc) > 1 and dc[1][1] else None
        assets.append(dict(a=a, k="stock", acct="stocks", focus=False, usd=sig(usd), gbp=sig(usd * rate) if rate else None, chg=chg, chg_d=True,
                           src="daily close " + dc[0][0], t=ep(dc[0][0] + "T21:00:00+00:00"), score=None, mom=None, sent_used=None,
                           action=None, reason=None, sent=None, pos=a in pos_by, held=pos_by.get(a, []), core_t=core_t.get(a),
                           spark=[sig(x[1], 5) for x in reversed(dc)], rank=(ranked.get(a) or {}).get("score"), **tinfo(a, "stock")))

    # ---- risk / status ----
    risk = json.loads((con.execute("SELECT value FROM state WHERE key='risk'").fetchone() or ["{}"])[0])
    coll = (con.execute("SELECT value FROM state WHERE key='collector_status'").fetchone() or [""])[0]
    s = stats(con)
    cur_eq = s["equity"]; day_start = risk.get("day_start")
    px_ts = con.execute("SELECT MAX(ts) FROM prices WHERE source!='backfill' AND price>0").fetchone()[0]
    updated = max([ep(x) for x in (risk.get("ts"), eq[-1]["ts"] if eq else None, px_ts) if x] or [n_ep])
    acash = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}
    cash_now = {s_: sum(acash.get((s_, ac), 0.0) for ac in ACCOUNTS) for s_ in SLEEVES}
    lt = t_now.astimezone(LON)
    rb_day = lt.date() - timedelta(days=(lt.weekday() - int(K["rebalance_weekday"])) % 7)
    hh, mm = map(int, K["rebalance_time_uk"].split(":"))
    rb_at = datetime.combine(rb_day, datetime.min.time().replace(hour=hh, minute=mm), LON)
    if last_rb and datetime.fromisoformat(last_rb.strip('"')) >= rb_at: rb_at += timedelta(days=7)
    # v7.1 reconciled figures: every total is the sum of its (already rounded) parts, all from ONE price snapshot:
    #   cell (pot x account): cash = rounded cash, invested = sum of the rounded position values, equity = cash + invested,
    #   P&L = equity - start; pot = sum of its two cells; account = sum of its three pot cells; total = sum of the pots.
    #   (The equity HISTORY - chart, max drawdown - still comes from the stored equity rows.)
    R2 = lambda x: round(x + 0.0, 2)
    sleeves = {}; LO = loans_open(con)
    for sl in SLEEVES:
        ss = stats(con, sl); sp = [p for p in positions if p["sleeve"] == sl]
        cells = {}
        for ac in ACCOUNTS:                                   # v6: the pot's slice of each account
            xs = stats(con, sl, ac); ap = [p for p in sp if p["acct"] == ac]; ff = fees[sl][ac]
            inv_ = R2(sum(p["value"] or 0 for p in ap)); cash_ = R2(acash.get((sl, ac), 0.0))
            lent_ = R2(sum(l["out"] for l in LO if l["lender"] == sl and l["account"] == ac)); owed_ = R2(sum(l["out"] for l in LO if l["borrower"] == sl and l["account"] == ac))
            eq_ = R2(cash_ + inv_ + lent_ - owed_)                # v7.3: + money lent to another pot - money borrowed from one
            cells[ac] = dict(start=r2(base[sl][ac]), target=r2((c.get("accounts") or {}).get("funding", {}).get(sl, {}).get(ac)),
                             equity=eq_, cash=cash_, invested=inv_, lent=lent_, borrowed=owed_, open_n=len(ap),
                             pnl=R2(eq_ - R2(base[sl][ac])), ret=round((eq_ / base[sl][ac] - 1) * 100, 3) if base[sl][ac] else None,
                             realized=r2(xs["realized"]), unrealized=R2(sum(p["pnl"] or 0 for p in ap)), fees=r2(sum(ff.values())))
        tot = lambda k: R2(sum(cells[ac][k] for ac in ACCOUNTS))
        st0 = tot("start"); eq0 = tot("equity")
        sleeves[sl] = dict(start=st0, pct=c["sleeve_split_pct"].get(sl, 0), equity=eq0, ret=round((eq0 / st0 - 1) * 100, 3) if st0 else 0,
                           pnl=tot("pnl"), realized=tot("realized"), unrealized=tot("unrealized"),
                           win_rate=round(ss["win_rate"] * 100, 1) if ss["win_rate"] is not None else None, closed=ss["closed"],
                           trades=ss["trades"], cash=tot("cash"), open_n=len(sp), max_dd=round(ss["max_dd"] * 100, 3),
                           invested=tot("invested"), fees=tot("fees"), lent=tot("lent"), borrowed=tot("borrowed"))
        sleeves[sl]["accts"] = cells
    cur_eq = R2(sum(sleeves[s_]["equity"] for s_ in SLEEVES)); tstart_ = R2(sum(sleeves[s_]["start"] for s_ in SLEEVES))
    
    # P&L breakdown: closed = realised P&L (already net of fees), open = headline minus closed, in integer pence
    # so the two lines under a card add up to that card exactly. Fees are not a third added component.
    for sl in SLEEVES:
        sleeves[sl]["pnl_breakdown"] = pnl_parts(sleeves[sl]["pnl"], sleeves[sl]["realized"])
        for ac in ACCOUNTS:
            cell = sleeves[sl]["accts"][ac]
            cell["pnl_breakdown"] = pnl_parts(cell["pnl"], cell["realized"])
    midnight = datetime.combine(lt.date(), datetime.min.time(), LON).astimezone(timezone.utc).isoformat()
    def realized_since(sleeve=None):
        q = "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='SELL' AND ts>=?"
        args = [midnight]
        if sleeve: q += " AND sleeve=?"; args.append(sleeve)
        return con.execute(q, args).fetchone()[0]
    
    ceq = sleeves["core"]["equity"] or 0
    caeq = {ac: sleeves["core"]["accts"][ac]["equity"] or 0 for ac in ACCOUNTS}
    cpos = {p["asset"]: p for p in positions if p["sleeve"] == "core"}
    sleeves["core"].update(
        targets=[dict(a=a, acct=core_tg[a][0], target=round(w, 2), target_gbp=r2(w / 100 * caeq[core_tg[a][0]]),
                      pot_pct=round(w * caeq[core_tg[a][0]] / ceq, 2) if ceq else None,
                      weight=round((cpos[a]["value"] or 0) / caeq[core_tg[a][0]] * 100, 2) if a in cpos and caeq[core_tg[a][0]] else 0,
                      value=cpos[a]["value"] if a in cpos else 0, status=plan.get(a, {}).get("status", "building"),
                      n=plan.get(a, {}).get("n", 0)) for a, w in core_t.items()],
        picks=picks, picks_info=cst.get("info", {}), picks_source=cst.get("source"), picks_ts=ep(cst["ts"]) if cst.get("ts") else None,
        stock_pct=float(K["stocks_pct"]), n_slots=stockrank.n_picks(c, caeq["stocks"]), min_pick=float(K.get("min_pick_gbp", 8)),
        split=stockrank.core_split(c), extra_pos=[p["asset"] for p in positions if p["sleeve"] == "core" and p["asset"] not in core_tg],
        max_per_theme=int(K.get("max_per_theme", 2)), weights=K.get("score_weights"),
        ranking=[dict(a=a, **{k: v for k, v in r.items() if k not in ("close", "n")}, pick=a in picks or a in crypto_picks)
                 for a, r in sorted(list(ranked.items()) + list(cranked.items()), key=lambda kv: (not kv[1]["eligible"], -kv[1]["score"]))],
        universe=[dict(key=k, label=v.get("label", k), tickers=v.get("tickers", []), watch=bool(v.get("watch"))) for k, v in (c["stocks"].get("universe") or {}).items()]
                 + [dict(key=k, label=v.get("label", k), tickers=v.get("tickers", []), watch=bool(v.get("watch")), acct="crypto") for k, v in (K.get("crypto_universe") or {}).items()],
        crypto_picks=crypto_picks, crypto_source=ccy.get("source"),
        queue=json.loads((con.execute("SELECT value FROM state WHERE key='order_queue'").fetchone() or ["[]"])[0]) or [],
        would_pick=stockrank.select(c, ranked, stockrank.n_picks(c, caeq["stocks"])) if ranked else [],
        plans=stockrank.plans(c, ranked, caeq, {a: p["value"] or 0 for a, p in cpos.items()},
                              {ac: acash.get(("core", ac), 0.0) for ac in ACCOUNTS}, crypto_picks) if ranked else [],
        last_rebalance=ep(last_rb.strip('"')) if last_rb else None, next_rebalance=int(rb_at.timestamp()), rebalance_log=rblog,
        rules=dict(tranches=K["tranches"], per_day=K["max_tranches_per_day"], dip=K["dip_vs_avg24_pct"], fallback_h=K["fallback_hour_uk"],
                   band=K["drift_band_pp"], stop=K["stop_loss_pct"], sent_delay=K["sentiment_delay_score"],
                   rebalance=f"{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][int(K['rebalance_weekday'])]} {K['rebalance_time_uk']} UK"))
    ds = risk.get("day_sleeve") or {}
    d_start_uk, d_cut_uk, d_flat_uk, midnight_book = day_rule_clocks(DY, ds)
    dst_ = r2(ds.get("start")); deq_ = sleeves["day"]["equity"]
    day_pnl_ = R2(deq_ - dst_) if dst_ else None
    sleeves["day"].update(day_start=dst_, day_pnl=day_pnl_,
                          day_pnl_breakdown=None if day_pnl_ is None else pnl_parts(day_pnl_, realized_since("day")),
                          day_pnl_pct=round((deq_ / dst_ - 1) * 100, 3) if dst_ else None, ledger=day_ledger(con, c, positions, acash, base), halted=bool(ds.get("halted")), entries_open=bool(ds.get("entries_open")),
                          flat_now=bool(ds.get("flat_now")),
                          rules=dict(start=d_start_uk, cutoff=d_cut_uk, flatten=d_flat_uk, cap=DY["daily_loss_cap_pct"],
                                     cost_x=DY.get("min_move_cost_x"), momentum=DY["momentum"], burst=DY["burst"],
                                     max_open=DY["momentum"]["max_open"] + 2 * DY["burst"]["max_open_per_account"]),
                          subs=day_sub_stats(con, lt), status=json.loads((con.execute("SELECT value FROM state WHERE key='day_subs'").fetchone() or ["{}"])[0]) or {})
    rst = json.loads((con.execute("SELECT value FROM state WHERE key='riskpot_status'").fetchone() or ["{}"])[0]) or {}
    rsc = json.loads((con.execute("SELECT value FROM state WHERE key='risk_screen'").fetchone() or ["{}"])[0]) or {}
    RC = RP.get("crypto") or {}
    sleeves["risk"].update(
        halted=bool(rst.get("halted")), halt=rst.get("halt"), base=r2(rst.get("base")), dd_pct=rst.get("dd_pct"), new_today=rst.get("new_today"),
        resume_pending=RISK_RESUME.exists(), cands=rst.get("cands") or [], status_t=ep(rst["ts"]) if rst.get("ts") else None,
        screen=dict(t=ep(rsc["ts"]) if rsc.get("ts") else None, universe=rsc.get("universe"), liquid=rsc.get("liquid"), ok=rsc.get("ok"),
                    monad=rsc.get("monad"), top=[{k: (sig(v, 4) if isinstance(v, float) else v) for k, v in d.items()} for d in (rsc.get("top") or [])[:25]]),
        stocks=stockrank.risk_stocks(c), stock_themes=RP.get("stock_themes") or [],
        threshold=rst.get("threshold"), base_threshold=rst.get("base_threshold"), top_threshold=rst.get("top_threshold"), ev_min=rst.get("ev_min"),
        setup=rst.get("setup") or {}, stats=risk_stats(con, lt),
        rules=dict(pos_pct=RP.get("max_position_pct"), max_pos=RP.get("max_position_gbp"), max_open=RP.get("max_open"),
                   conf_threshold=RP.get("conf_threshold"), conf_at_stop=RP.get("conf_threshold_at_stop"), conf_size_pct=RP.get("conf_size_pct"),
                   weights=RP.get("confidence_weights"), dip_weights=(RP.get("dip") or {}).get("weights"),
                   ev_min=RP.get("ev_min_pct"), ev_win=RP.get("ev_win_pct"), ev_loss=RP.get("ev_loss_pct"),
                   volume_x=RP.get("volume_x"), record_days=RP.get("record_days"), confirm=RP.get("confirm_momentum"), min_trade=c["risk"]["min_trade_size"],
                   stop=RP.get("stop_loss_pct"), trail_after=RP.get("trail_after_pct"), trail=RP.get("trail_pct"),
                   ev_lock=round(riskpot.arm_lock_pct(RP), 2),
                   hold=RP.get("max_hold_days"), cap=RP.get("drawdown_cap_pct"), cooldown=RP.get("cooldown_h"), top_n=RC.get("top_n"),
                   min_mcap=RC.get("min_mcap_usd"), max_mcap=RC.get("max_mcap_usd"), min_vol=RC.get("min_vol_usd"), sent_block=RP.get("sentiment_block"),
                   sent_exit=RP.get("sentiment_exit")))
    transfers = [dict(t=ep(r["ts"]), frm=r["from_sleeve"], to=r["to_sleeve"], frm_acct=r["from_account"], to_acct=r["to_account"],
                      kind=r["kind"] or "pot", amount=r2(r["amount"]), reason=r["reason"])
                 for r in con.execute("SELECT * FROM transfers ORDER BY ts")]
    # ---- v6: the two real-world accounts ----
    AC = c.get("accounts") or {}
    accounts = {}
    for ac in ACCOUNTS:
        xs = stats(con, None, ac); ap = [p for p in positions if p["acct"] == ac]
        PC = [sleeves[s_]["accts"][ac] for s_ in SLEEVES]; sm = lambda k: R2(sum(x[k] for x in PC))
        inv_, cash_, st_, eq_ = sm("invested"), sm("cash"), sm("start"), sm("equity")
        ff = {k: sum(fees[s_][ac][k] for s_ in SLEEVES) for k in fees["core"][ac]}
        accounts[ac] = dict(label=(AC.get(ac) or {}).get("label", ac), where=(AC.get(ac) or {}).get("where", ""),
                            start=st_, target=r2(sum(float((AC.get("funding") or {}).get(s_, {}).get(ac, 0)) for s_ in SLEEVES)),
                            equity=eq_, cash=cash_, invested=inv_, pnl=sm("pnl"), lent=sm("lent"), borrowed=sm("borrowed"),
                            ret=round((eq_ / st_ - 1) * 100, 3) if st_ else None, open_n=len(ap),
                            realized=sm("realized"), unrealized=sm("unrealized"), closed=xs["closed"], trades=xs["trades"],
                            win_rate=round(xs["win_rate"] * 100, 1) if xs["win_rate"] is not None else None, max_dd=round(xs["max_dd"] * 100, 3),
                            fees={k: sig(v, 4) for k, v in ff.items() if v}, fees_total=sm("fees"),
                            pnl_breakdown=pnl_parts(sm("pnl"), sm("realized")),
                            pots={s_: sleeves[s_]["accts"][ac] for s_ in SLEEVES})
    asplit = json.loads((con.execute("SELECT value FROM state WHERE key='account_split'").fetchone() or ["null"])[0])
    if asplit:
        asplit = dict(t=ep(asplit["ts"]), opening={k: {a: r2(v) for a, v in d.items()} for k, d in asplit["opening"].items()},
                      targets=asplit["targets"], pending=asplit.get("pending") or [], settled=ep(asplit["settled"]) if asplit.get("settled") else None,
                      settled_legs=asplit.get("settled_legs") or [])
    gd = json.loads((con.execute("SELECT value FROM state WHERE key='gas'").fetchone() or ["null"])[0]) or {}
    G = c.get("gas") or {}
    gas_tot = con.execute("SELECT COALESCE(SUM(gas),0), COALESCE(SUM(cost),0), COUNT(gas) FROM trades").fetchone()
    gas = dict(gbp=sig(gd.get("gbp"), 3), gwei=gd.get("gwei"), mon=sig(gd.get("mon"), 4), units=gd.get("units", G.get("gas_per_swap")),
               t=ep(gd["ts"]) if gd.get("ts") else None, src=gd.get("src"), fallback=G.get("fallback_gbp"),
               paid=sig(gas_tot[0], 4), fees=r2(gas_tot[1]), n=gas_tot[2])
    live_px = [a for a in assets if a.get("age_min") is not None and not a.get("chg_d")]
    full_of = lambda a: float((rk.get("fresh_full_min") or {}).get("stock" if a.get("k") == "stock" else "crypto", 5 if a.get("k") == "stock" else 10))
    dead_m = float(rk.get("price_dead_min", 180))
    fresh_status = dict(score=round(sum(freshness.price_score(a["age_min"], full_of(a), dead_m) for a in live_px) / len(live_px), 3) if live_px else None,
                        n=len(live_px), stale=sum(1 for a in live_px if a["age_min"] > full_of(a)),
                        oldest_min=round(max((a["age_min"] for a in live_px), default=0), 1) if live_px else None,
                        full_min=rk.get("fresh_full_min") or {"crypto": 10, "stock": 5})
    return dict(
        v=6, gas=gas, generated=n_ep, updated=updated, currency="GBP", start=start, accounts=accounts, account_split=asplit,
        rules=dict(version=(c.get("rules") or {}).get("version"), frozen_since=(c.get("rules") or {}).get("frozen_since")),
        scorecard=setup_scorecard(con),
        day_overdue=day_overdue_assets(positions, lt, DY["entry_start_uk"], DY["flatten_uk"], midnight_book),
        freshness=fresh_status,
        sleeves=sleeves, focus=focus, transfers=transfers, markets=markets.status(t_now),
        themes=[dict(key=k, label=v.get("label", k), watch=bool(v.get("watch")), n=len(v.get("tickers", []))) for k, v in (c["stocks"].get("universe") or {}).items()]
               + [dict(key=k, label=l, watch=True, n=sum(1 for x in (CC.get("watch_ids") or {}).values() if x == k)) for k, l in cth.items()]
               + [dict(key=k, label=l, watch=False, n=sum(1 for x in assets if x.get("tk") == k), crypto=True) for k, l in CTL.items()],
        loans=[dict(id=r["id"], t=ep(r["ts"]), account=r["account"], lender=r["lender"], borrower=r["borrower"], sub=r["sub"], asset=r["asset"],
                    amount=r2(r["amount"]), repaid=r2(r["repaid"] or 0), out=r2(r["amount"] - (r["repaid"] or 0)), status=r["status"],
                    repaid_t=ep(r["repaid_ts"]) if r["repaid_ts"] else None, reason=r["reason"])
               for r in con.execute("SELECT * FROM loans WHERE status='open' OR ts>=? ORDER BY id DESC LIMIT 50", ((t_now - timedelta(days=7)).isoformat(),))],
        loan_rules=c.get("loans") or {},
        day_universe=dict(t=ep(DU["ts"]) if DU.get("ts") else None, n=DU.get("n"), screened=DU.get("screened"), excluded=DU.get("excluded") or {}, base_excluded=dx_,
                          memes_known=DU.get("memes_known"), meme_allow=DU.get("meme_allow") or [], warm_h=warm_h, top_n=CC.get("top_n"),
                          warming=sorted(x["a"] for x in assets if x.get("warm") is not None), rules={k: v for k, v in (CC.get("day_universe") or {}).items() if k != "meme_blocklist"},
                          blocklist_n=len((CC.get("day_universe") or {}).get("meme_blocklist") or [])),
        costs=c["stocks"].get("costs"), migrated=json.loads((con.execute("SELECT value FROM state WHERE key='migrated_v5'").fetchone() or ["null"])[0]),
        fx=dict(rate=sig(rate, 6), gbpusd=round(1 / rate, 4) if rate else None, t=ep(fx[1]) if fx else None,
                source=fx[2] if fx else None, stale=bool(fx[3]) if fx else True,
                alt=round(1 / fx_alt["alt_rate"], 4) if fx_alt and fx_alt["alt_rate"] else None,
                alt_source=fx_alt["alt_source"] if fx_alt else None),
        stats=dict(equity=cur_eq, start=tstart_, ret=round((cur_eq / tstart_ - 1) * 100, 3) if tstart_ else 0, btc_ret=round(s["btc_ret"] * 100, 3) if s["btc_ret"] is not None else None,
                   realized=R2(sum(sleeves[s_]["realized"] for s_ in SLEEVES)), trades=s["trades"], closed=s["closed"],
                   win_rate=round(s["win_rate"] * 100, 1) if s["win_rate"] is not None else None,
                   max_dd=round(s["max_dd"] * 100, 3), best=r2(s["best"]), worst=r2(s["worst"]),
                   unrealized=R2(sum(sleeves[s_]["unrealized"] for s_ in SLEEVES)),
                   cash=R2(sum(sleeves[s_]["cash"] for s_ in SLEEVES)), invested=R2(sum(sleeves[s_]["invested"] for s_ in SLEEVES)),
                   pnl=R2(cur_eq - tstart_), day_start=r2(day_start),
                   day_pnl=R2(cur_eq - r2(day_start)) if day_start else None, day_pnl_pct=round((cur_eq / r2(day_start) - 1) * 100, 3) if day_start else None,
                   pnl_breakdown=pnl_parts(R2(cur_eq - tstart_), R2(sum(sleeves[s_]["realized"] for s_ in SLEEVES))),
                   day_pnl_breakdown=None if not day_start else pnl_parts(R2(cur_eq - r2(day_start)), realized_since())),
        risk=dict(halted=bool(risk.get("halted")), day=risk.get("day"), day_start=day_start, kill=KILL.exists(),
                  fx_stale=bool(fx[3]) if fx else True, open_n=len(positions), last_halt=dict(t=ep(halt["ts"]), reason=halt["reason"]) if halt else None,
                  limits=dict(max_open=DY["momentum"]["max_open"], burst_max_open=DY["burst"]["max_open_per_account"],
                              stop=DY["momentum"]["stop"]["min_pct"], take=DY["momentum"]["take_profit"]["min_pct"], daily_cap=DY["daily_loss_cap_pct"], fee=rk["fee_slippage_pct"],
                              core_stop=K["stop_loss_pct"], risk_stop=RP.get("stop_loss_pct"), risk_cap=RP.get("drawdown_cap_pct"),
                              min_trade=rk["min_trade_size"], buy=st["buy_threshold"], focus_buy=st["focus_buy_threshold"],
                              sell=st["sell_threshold"])),
        collector=coll,
        cg=pricesrc.stats(con, t_now, c),
        mon_check=dict(cg=sig(mon["MON"]), dex=sig(mon["MON.DEX"]), diff=round((mon["MON.DEX"] / mon["MON"] - 1) * 100, 2))
                  if len(mon) == 2 else None,
        equity=equity, positions=positions, trades=trades, assets=assets, prices=prices, fx_hist=fx_hourly,
        signals=sig_hist, reasons=reasons, sentiment=sentiment,
        variants=variant_report if variant_report is not None else _variant_report(),
        strategies=_strategy_rows(),
        tests=_tests_view())

def _strategy_rows():
    try:
        import strategies
        return strategies.comparison_rows()
    except Exception as e:
        return [dict(name="strategies", error=str(e), excess_btc=None)]

def _tests_view():
    try:
        import testreset
        return testreset.tests_view()
    except Exception as e:
        return dict(archived=[], live=None, error=str(e))

def _variant_report():
    try:
        import variants
        return variants.report()
    except Exception as e:
        return dict(enabled=False, books=[], error=str(e))

# Header menu. The comparison tab keeps its own labels (all_combined stays "Both together" there).
BOOK_MENU = {
    "main": "Main (frozen)",
    "ev_exit": "EV exit",
    "review_leftovers": "Review leftovers",
    "all_combined": "Both",
    "burst_runner": "Burst runner",
    "everything": "Everything combined",
    "midnight_crypto": "Midnight crypto",
    "entry_watch": "Entry watch",
    "risk_giveback_15": "Risk giveback 15",
    "day_trail_arm": "Day trail arm",
    "day_ev_blend": "Day EV blend",
    "no_risk_clock": "No risk clock",
    "day_capital_caution": "Day capital caution",
    "partial_take": "Partial take",
    "sent_split": "Sentiment split",
    "best_of": "Best of",
    "btc_trend": "BTC trend",
    "btc_trend_vol": "BTC trend, vol sized",
    "majors_rotation": "Majors rotation",
    "btc_core_satellite": "BTC core + satellite",
    "rs_rotation_veto": "RS rotation, veto",
    "quiet_strength": "Quiet strength",
    "best_of_plus": "Best of plus trend",
    "trend_carry_unlock": "Trend, funding and unlocks",
    "insider_rs_stocks": "Insider relative strength",
}
_BOOK_NAME = re.compile(r"^[a-z0-9_]+$")

def book_menu_label(name, fallback=None):
    return BOOK_MENU.get(name) or fallback or name

def book_file(name):
    """Published JSON name. main is data.json; a variant is data_<name>.json."""
    if name == "main":
        return "data.json"
    if not _BOOK_NAME.fullmatch(name or ""):
        raise ValueError(f"bad book name {name!r}")
    return f"data_{name}.json"

def open_ro(path):
    """Read-only connection. A variant export must not migrate or write that file."""
    con = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con

def stamp_books(payloads):
    """Mark each payload with its own identity and the catalog of files actually written.
    payloads is an insertion-ordered mapping, main first, then variants.run."""
    catalog = []
    for name in payloads:
        catalog.append(dict(name=name, label=book_menu_label(name), file=book_file(name), variant=name != "main"))
    for name, data in payloads.items():
        row = next(c for c in catalog if c["name"] == name)
        data["book"] = dict(name=name, label=row["label"], variant=row["variant"])
        data["books"] = catalog
    return catalog

def write_book_files(out, payloads):
    """Write one JSON per book and delete data_<name>.json files that are no longer published."""
    out = Path(out)
    catalog = stamp_books(payloads)
    for row in catalog:
        body = json.dumps(payloads[row["name"]], separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        write_atomic(out / row["file"], body)
    keep = {row["file"] for row in catalog}
    for p in out.glob("data_*.json"):
        if p.name not in keep:
            p.unlink()
    return catalog

def _variant_payloads(payloads, variant_report=None):
    """Fill payloads with one build() per existing variant database. A failure skips that book.

    variant_report is the comparison already computed for this cycle. Building it again
    inside every book reopened all thirteen databases and re-parsed config each time.
    """
    import variants
    root = variants.dir_path()
    for name in list((cfg().get("variants") or {}).get("run") or []):
        path = root / f"{name}.db"
        if not path.exists():
            continue
        try:
            book_file(name)
        except ValueError as e:
            print(f"[dashboard] {e}, skipped")
            continue
        con = None
        try:
            con = open_ro(path)
            payloads[name] = build(con, variant_report)
        except Exception as e:
            print(f"[dashboard] {name} export skipped: {e}")
            payloads.pop(name, None)
        finally:
            if con is not None:
                con.close()

def write_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp"); tmp.write_bytes(data); os.replace(tmp, path)

def main():
    t0 = time.perf_counter()
    OUT.mkdir(exist_ok=True)
    t_report = time.perf_counter()
    report = _variant_report()
    print(f"[timing] dashboard report {time.perf_counter() - t_report:.1f}s", flush=True)
    payloads = {"main": build(db(), report)}
    _variant_payloads(payloads, report)
    catalog = write_book_files(OUT, payloads)
    (OUT / ".nojekyll").touch()
    # static assets: copy templates, cache-bust js/css by content hash in index.html
    ver = {}
    for name in ("app.js", "styles.css"):
        b = (WEB / name).read_bytes(); ver[name] = hashlib.sha1(b).hexdigest()[:10]
        if not (OUT / name).exists() or (OUT / name).read_bytes() != b: write_atomic(OUT / name, b)
    page = (WEB / "index.html").read_text(encoding="utf-8")
    page = page.replace("__APPJS_V__", ver["app.js"]).replace("__CSS_V__", ver["styles.css"])
    if not (OUT / "index.html").exists() or (OUT / "index.html").read_text(encoding="utf-8") != page:
        write_atomic(OUT / "index.html", page.encode())
    bits = ", ".join(f"{r['file']} ({(OUT / r['file']).stat().st_size // 1024} KB)" for r in catalog)
    print(f"[dashboard] wrote {bits} + index.html/app.js/styles.css in {time.perf_counter() - t0:.1f}s")

if __name__ == "__main__":
    main()
