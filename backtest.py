"""Replay stored prices through the main book and each paper variant. Report only.

The live trader runs on a temporary copy of the price database, so fees and slippage
are the ones a real cycle would charge. Nothing here writes data/trading.db, and the
frozen rules are not edited. The clock steps every 5 minutes, like the live cycle.
Before each step, daily closes dated that UTC day or later are hidden, and the stored
Risk screen is hidden until its own timestamp. Core picks are rebuilt from the closes
that remain. A close dated D is visible only once the clock is on a later UTC date.

Usage:
    python3 backtest.py
    python3 backtest.py --books main --step 15
"""
import argparse, contextlib, io, json, os, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAIL_TRADES = 100
FAIL_DD = 0.20
FAIL_PF = 1.1


def clock(src, step_min=5):
    """One timestamp per step, at the end of each bucket, so that bucket's prints are visible."""
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts FROM prices WHERE source NOT IN ('backfill','kyberswap') ORDER BY ts").fetchall()
    finally:
        con.close()
    out, seen = [], None
    step = timedelta(minutes=int(step_min))
    for (ts,) in rows:
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        minutes = (dt.hour * 60 + dt.minute) // int(step_min) * int(step_min)
        bucket = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
        if seen is not None and bucket <= seen:
            continue
        seen = bucket
        out.append(bucket + step - timedelta(seconds=1))
    return out


def fresh_book(src, dest):
    """Copy prices and market state, then drop the live book so the replay starts flat."""
    dest = Path(dest)
    if dest.exists():
        dest.unlink()
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    con = sqlite3.connect(dest)
    try:
        for table in ("trades", "positions", "cash", "acct_cash", "equity", "signals", "transfers", "loans"):
            con.execute(f"DELETE FROM {table}")
        con.execute("""DELETE FROM state WHERE key IN (
            'core_plan','core_stocks','core_crypto','core_last_rebalance','order_queue','account_split',
            'day_meta','risk_meta','risk_peaks','riskpot_base','riskpot_halt','day_subs','variant_reset')
            OR key LIKE 'day_start:%' OR key LIKE 'day_halt:%' OR key LIKE 'day_exit:%'
            OR key LIKE 'risk_exit:%'""")
        con.commit()
    finally:
        con.close()
    return dest


def closed_stats(sells):
    """(n, win_rate, profit_factor) from sell P&L. No losses and some wins is an infinite profit factor."""
    n = len(sells)
    if not n:
        return 0, None, None
    wins = [x for x in sells if x > 0]
    losses = [x for x in sells if x < 0]
    win = len(wins) / n
    if not losses:
        pf = float("inf") if wins else None
    else:
        pf = sum(wins) / abs(sum(losses))
    return n, win, pf


def max_drawdown(equity):
    """Worst peak-to-trough as a negative fraction. Empty curve is 0."""
    peak, mdd = 0.0, 0.0
    for v in equity:
        if v is None:
            continue
        peak = max(peak, float(v))
        if peak:
            mdd = min(mdd, float(v) / peak - 1)
    return mdd


def btc_hold(con, start_eq, t0, t1, fee):
    """P&L of buying BTC at the first print in the window and selling at the last, with one fee each side."""
    def px(cmp, order):
        row = con.execute(
            f"SELECT price FROM prices WHERE asset='BTC' AND source!='backfill' AND price>0 AND ts {cmp} ? ORDER BY ts {order} LIMIT 1",
            (t0 if cmp == ">=" else t1,)).fetchone()
        return float(row[0]) if row else None
    p0, p1 = px(">=", "ASC"), px("<=", "DESC")
    if not p0 or not p1:
        return None
    end = float(start_eq) * (1 - fee) / (1 + fee) * (p1 / p0)
    return end - float(start_eq)


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _snap_p(raw):
    """Entry probability stored on a buy. None when the snapshot has no p."""
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict) or raw.get("p") is None:
        return None
    try:
        return float(raw["p"])
    except (TypeError, ValueError):
        return None


def pair_entries(rows):
    """(entry p, win) for each buy that stored p and later sold. Win is sell P&L after costs > 0.

    The first buy of a sleeve and asset keeps its p until the next sell. A buy with no p is skipped.
    `rows` are trades in id order with side, sleeve, asset, pnl, snap.
    """
    pending = {}
    out = []
    for r in rows:
        key = (r["sleeve"], r["asset"])
        if r["side"] == "BUY":
            p = _snap_p(r["snap"])
            if key not in pending and p is not None:
                pending[key] = p
            continue
        p = pending.pop(key, None)
        if p is None:
            continue
        out.append((p, float(r["pnl"] or 0) > 0))
    return out


def calibration(paired, edges=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)):
    """Buckets of entry probability against how often that sell made money.

    Each bucket is lo, hi, n, wins, win_rate, mean_p. The last bucket includes 1.
    """
    out = []
    for i, lo in enumerate(edges[:-1]):
        hi = edges[i + 1]
        last = i == len(edges) - 2
        xs = []
        for p, win in paired:
            if last:
                if lo <= p <= hi:
                    xs.append((p, win))
            elif lo <= p < hi:
                xs.append((p, win))
        n = len(xs)
        wins = sum(1 for _, w in xs if w)
        out.append(dict(
            lo=lo, hi=hi, n=n, wins=wins,
            win_rate=(wins / n) if n else None,
            mean_p=(sum(p for p, _ in xs) / n) if n else None))
    return out


def judge(row):
    """FAIL reasons. A missing profit factor fails the 1.1 bar. Drawdown is a negative fraction."""
    why = []
    if (row.get("trades") or 0) < FAIL_TRADES:
        why.append(f"trades<{FAIL_TRADES}")
    dd = row.get("max_dd")
    if dd is None or dd < -FAIL_DD:
        why.append("max drawdown>20%")
    pf = row.get("pf")
    if pf is None or pf < FAIL_PF:
        why.append("profit factor<1.1")
    bh = row.get("bh_pnl")
    if bh is not None and (row.get("pnl") or 0) <= bh:
        why.append("does not beat buy-and-hold")
    return why


def _fmt_pf(pf):
    if pf is None:
        return "n/a"
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


def _fmt_pct(x):
    return "n/a" if x is None else f"{x*100:.1f}%"


def _fmt_gbp(x):
    return "n/a" if x is None else f"{x:+.2f}"


def format_calibration(book_rows):
    """One table of entry-p buckets. A book with no stored probability says so."""
    lines = ["", "calibration: stored entry probability vs realised win after costs",
             f"{'book':<22} {'p':>9} {'n':>6} {'wins':>6} {'win':>7} {'mean p':>7}"]
    any_row = False
    for r in book_rows:
        buckets = r.get("calibration") or []
        if not any(b.get("n") for b in buckets):
            lines.append(f"{r['book']:<22} {'n/a':>9} {0:>6} {0:>6} {'n/a':>7} {'n/a':>7}")
            continue
        for b in buckets:
            if not b.get("n"):
                continue
            any_row = True
            label = f"{b['lo']:.1f}-{b['hi']:.1f}"
            lines.append(
                f"{r['book']:<22} {label:>9} {b['n']:>6} {b['wins']:>6} {_fmt_pct(b.get('win_rate')):>7} "
                f"{b['mean_p']:.2f}" if b.get("mean_p") is not None else
                f"{r['book']:<22} {label:>9} {b['n']:>6} {b['wins']:>6} {'n/a':>7} {'n/a':>7}")
    if not any_row:
        lines.append("(no closed trade stored an entry probability)")
    return "\n".join(lines)


def report_table(rows):
    head = f"{'book':<22} {'trades':>6} {'win':>6} {'pf':>6} {'maxDD':>7} {'pnl':>9} {'btc':>9} {'result'}"
    lines = [head]
    for r in rows:
        why = r.get("fail") or []
        result = "PASS" if not why else "FAIL " + ", ".join(why)
        if r.get("error"):
            result = "ERROR " + r["error"]
        lines.append(
            f"{r['book']:<22} {r.get('trades') or 0:>6} {_fmt_pct(r.get('win_rate')):>6} {_fmt_pf(r.get('pf')):>6} "
            f"{_fmt_pct(r.get('max_dd')):>7} {_fmt_gbp(r.get('pnl')):>9} {_fmt_gbp(r.get('bh_pnl')):>9} {result}")
    return "\n".join(lines)


class AsOf:
    """Hides daily closes and the Risk screen that are still in the future.

    Closes live in bt_daily_closes. A date D is copied into daily_closes only once
    the clock's UTC date is after D. The Risk screen stored at copy time stays
    hidden until the clock reaches its timestamp; Core then rebuilds picks from
    the closes that are already visible. The clock only moves forward.
    """

    def __init__(self, con):
        self.con = con
        self.shown = ""
        self.screen_on = None
        con.execute("DROP TABLE IF EXISTS bt_daily_closes")
        con.execute("CREATE TABLE bt_daily_closes(asset TEXT, d TEXT, close REAL, PRIMARY KEY(asset, d))")
        con.execute("INSERT INTO bt_daily_closes SELECT asset, d, close FROM daily_closes")
        row = con.execute("SELECT value FROM state WHERE key='risk_screen'").fetchone()
        con.execute("INSERT OR REPLACE INTO state VALUES('bt_risk_screen',?)", ((row[0] if row else "") or "",))
        con.execute("DELETE FROM daily_closes")
        con.commit()

    def apply(self, now):
        day = now.astimezone(timezone.utc).date().isoformat()
        if day != self.shown:
            self.con.execute(
                "INSERT OR IGNORE INTO daily_closes SELECT asset, d, close FROM bt_daily_closes WHERE d < ? AND d >= ?",
                (day, self.shown))
            self.shown = day
        raw = self.con.execute("SELECT value FROM state WHERE key='bt_risk_screen'").fetchone()
        screen = {}
        if raw and raw[0]:
            try:
                screen = json.loads(raw[0])
            except (TypeError, ValueError):
                screen = {}
        ts = _parse_ts(screen.get("ts"))
        visible = bool(ts and ts <= now)
        if visible != self.screen_on:
            if not visible:
                screen = {"ts": now.isoformat(), "top": [], "universe": 0, "liquid": 0, "ok": 0, "monad": 0}
            self.con.execute("INSERT OR REPLACE INTO state VALUES('risk_screen',?)", (json.dumps(screen),))
            self.screen_on = visible
        self.con.commit()


def replay(src, book, steps, dest):
    """Run one book. `book` is 'main' or a variants.run name. Returns the result row."""
    import common, trader
    from common import funding, sync_pot_cash
    fresh_book(src, dest)
    os.environ["TRADING_DB"] = str(dest)
    os.environ.pop("TRADING_VARIANT", None)
    if book != "main":
        os.environ["TRADING_VARIANT"] = book
    common.DB = Path(dest)
    c = common.cfg()
    con = common.db()
    try:
        asof = AsOf(con)
        if steps:
            common._NOW = steps[0]
            asof.apply(steps[0])
        fund = funding(c)
        con.executemany("INSERT INTO acct_cash VALUES(?,?,?)", [(k, a, v) for k, d in fund.items() for a, v in d.items()])
        sync_pot_cash(con)
        trader.init_core(con, c)
        con.commit()
    except Exception as e:
        return _row(book, error=str(e).splitlines()[-1][:160])
    finally:
        con.close()
    for t in steps:
        common._NOW = t
        con = sqlite3.connect(dest)
        try:
            asof.con = con
            asof.apply(t)
        finally:
            con.close()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                trader.main()
        except Exception as e:
            return _row(book, error=str(e).splitlines()[-1][:160])
    return _measure(dest, book, steps, c)


def _measure(dest, book, steps, c):
    con = sqlite3.connect(dest)
    con.row_factory = sqlite3.Row
    try:
        sells = [float(r[0] or 0) for r in con.execute("SELECT pnl FROM trades WHERE side='SELL'")]
        n, win, pf = closed_stats(sells)
        curve = [r[0] for r in con.execute("SELECT equity FROM equity ORDER BY ts")]
        from common import start_cash
        start = sum(start_cash(c).values())
        last = curve[-1] if curve else start
        pnl = (last - start) if last is not None else None
        fee = float(c["risk"]["fee_slippage_pct"]) / 100
        t0 = steps[0].isoformat() if steps else ""
        t1 = steps[-1].isoformat() if steps else ""
        bh = btc_hold(con, start, t0, t1, fee) if steps else None
        paired = pair_entries([dict(r) for r in con.execute(
            "SELECT sleeve, asset, side, pnl, snap FROM trades WHERE side IN ('BUY','SELL') ORDER BY id")])
    finally:
        con.close()
    row = dict(book=book, trades=n, win_rate=win, pf=pf, max_dd=max_drawdown(curve), pnl=pnl, bh_pnl=bh,
               calibration=calibration(paired), error=None)
    row["fail"] = judge(row)
    return row


def _row(book, error):
    row = dict(book=book, trades=0, win_rate=None, pf=None, max_dd=None, pnl=None, bh_pnl=None, error=error, fail=[])
    return row


def books_of(c, only=None):
    names = ["main"] + list((c.get("variants") or {}).get("run") or [])
    if not only:
        return names
    want = {x.strip() for x in only.split(",") if x.strip()}
    return [n for n in names if n in want]


def run(src, dest_dir, step_min=5, only=None, limit=None):
    import common
    os.environ.pop("TRADING_VARIANT", None)
    os.environ["TRADING_DB"] = str(src)
    common.DB = Path(src)
    names = books_of(common.cfg(), only)
    steps = clock(src, step_min)
    if limit:
        steps = steps[:int(limit)]
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in names:
        rows.append(replay(src, name, steps, dest_dir / f"{name}.db"))
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description="Replay stored prices through each paper book. Report only.")
    p.add_argument("--db", default=str(HERE / "data" / "trading.db"))
    p.add_argument("--out", default=str(HERE / "test" / "_backtest"))
    p.add_argument("--step", "--step-min", dest="step_min", type=int, default=5,
                   help="replay step in minutes (default 5, the live cycle)")
    p.add_argument("--books", default="", help="comma list, default main plus variants.run")
    p.add_argument("--limit", type=int, default=0, help="stop after this many clock steps (0 = the whole history)")
    p.add_argument("--daily", action="store_true",
                   help="replay the overnight strategy books on Kraken daily closes instead of the 5-minute trader")
    args = p.parse_args(argv)
    if args.daily:
        import strategies
        names = [n.strip() for n in (args.books or "btc_trend,btc_trend_vol,majors_rotation,btc_core_satellite,rs_rotation_veto").split(",") if n.strip()]
        rows = strategies.replay_books(names, pause=0.35)
        print(strategies.report_table(rows))
        return 0
    rows = run(args.db, args.out, args.step_min, args.books or None, args.limit or None)
    print(report_table(rows))
    print(format_calibration(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
