"""Parallel paper books. The main database is never written here.

Each name in config `variants.run` is a SQLite file under data/variants/, copied once from a snapshot
of the main book and then fed the same prices, sentiment, FX and collector screens. The trader runs
in a child process with TRADING_VARIANT set, so a crash or a rule change stays in that file.
Pre-trade quotes are not fetched again: TRADING_DB makes quotes.prefetch a no-op, and the rows the
main cycle just wrote are copied in before the child starts.
"""
import os, shutil, sqlite3, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

from common import DATA, DB, LON, ROOT, cfg, now

LABELS = {
    "ev_exit": "EV exit",
    "review_leftovers": "Review leftovers",
    "all_combined": "Both together",
    "burst_runner": "Burst runner",
}
BLURB = {
    "ev_exit": "Sells a Day or Risk position when live expected value is below -0.4% and at least 0.8 points worse than the value stored at entry. While live value is negative but still above -0.4%, the stop is three quarters of the stored stop.",
    "review_leftovers": "Burst stop is 1.5x wider unless live EV is already below -0.4%. Core's 18:00 fallback tranche is half size when the coin is up more than 15% over 7 days or RSI is above 75. New Day, Risk and Core entries need the coin's own 3-day trend up. After 12:00 UK, Day and Core will not each buy the same coin.",
    "all_combined": "EV exit and the review leftovers, on one book. The burst runner is a separate book, not part of this one.",
    "burst_runner": "Same rules as the main book, except a Burst position that hits its target keeps running when the entry signal is still intact. The trail then gives back 20% of the peak gain. Each sell says sold at target or ran, with the extra gain and the giveback.",
}
INPUT_STATE = ("risk_screen", "day_excluded", "risk_only", "risk_only_stocks", "gas")
MARKET = (("prices", "ts"), ("sentiment", "ts"), ("fx", "ts"), ("daily_closes", "d"))


def rules_of(c):
    """Flag values. Missing keys stay off, which is the main book's behaviour."""
    d = dict(ev_exit=False, ev_exit_now_pct=-0.4, ev_exit_below_entry_pp=0.8, ev_exit_stop_mult=0.75,
             burst_stop_widen=False, burst_stop_mult=1.5, core_fallback_half=False, core_ext_7d_pct=15.0,
             core_ext_rsi=75.0, entry_own_trend=False, entry_trend_days=3, no_day_core_same_afternoon=False,
             afternoon_hour_uk=12, burst_runner=False, burst_runner_giveback_pct=20)
    for k, v in ((c or {}).get("variant_rules") or {}).items():
        if k in d and v is not None:
            d[k] = v
    return d


def active(R):
    return bool(R.get("ev_exit") or R.get("burst_stop_widen") or R.get("core_fallback_half")
                or R.get("entry_own_trend") or R.get("no_day_core_same_afternoon") or R.get("burst_runner"))


def ev_action(ev_now, entry_ev, R):
    """None, 'tighten', or 'exit'. Exit when EV now is below ev_exit_now_pct and (if an entry EV was
    stored) at least ev_exit_below_entry_pp worse than that entry EV. Tighten when EV now is negative
    but the exit line has not been crossed. Off unless ev_exit is on."""
    if not R.get("ev_exit") or ev_now is None:
        return None
    line, gap = float(R["ev_exit_now_pct"]), float(R["ev_exit_below_entry_pp"])
    worse = entry_ev is None or float(ev_now) <= float(entry_ev) - gap
    if float(ev_now) < line and worse:
        return "exit"
    if float(ev_now) < 0:
        return "tighten"
    return None


def ev_exit_reason(ev_now, entry_ev, R):
    line, gap = float(R["ev_exit_now_pct"]), float(R["ev_exit_below_entry_pp"])
    if entry_ev is None:
        return f"EV exit: EV now {float(ev_now):+.2f}% < {line:g}% (no entry EV stored)"
    return (f"EV exit: EV now {float(ev_now):+.2f}% < {line:g}% and "
            f"{float(entry_ev) - float(ev_now):.2f}pp below entry EV {float(entry_ev):+.2f}% (need {gap:g}pp)")


def _below_line(ev_now, R):
    return ev_now is not None and float(ev_now) < float(R["ev_exit_now_pct"])


def effective_stop_pct(stored, ev_now, R, kind):
    """Stop distance in percent (a positive number). The wider burst stop is not used once EV now is
    below ev_exit_now_pct. A negative EV that has not reached the exit line tightens the stop."""
    sl = float(stored)
    if kind == "burst" and R.get("burst_stop_widen") and not _below_line(ev_now, R):
        sl *= float(R["burst_stop_mult"])
    if ev_action(ev_now, None, R) == "tighten":
        # ev_action with no entry EV exits on the absolute line, so 'tighten' here is only the band
        # between 0 and that line. Apply it to the (possibly already widened) stop.
        if not _below_line(ev_now, R):
            sl *= float(R["ev_exit_stop_mult"])
    return sl


def ret_over_days(pts, days):
    """Percent change versus the last price at or before `days` ago. None without that history."""
    if not pts:
        return None
    t, p = pts[-1]
    old = [x for ts, x in pts if ts <= t - timedelta(days=days)]
    if not old or not old[-1]:
        return None
    return (p / old[-1] - 1) * 100


def trend_up(pts, days):
    r = ret_over_days(pts, days)
    if r is None:
        return None
    return r > 0


def trend_lookback_h(R):
    """Hours of prices the entry check needs. history()'s default is 72h, which ends exactly `entry_trend_days`
    before now. The last tick is a few minutes earlier than now, so that window has no price at or before
    (last tick - 3 days) and every entry would be blocked for missing history. Six extra hours covers the gap."""
    return int(R["entry_trend_days"]) * 24 + 6


def own_trend_block(pts, R):
    if not R.get("entry_own_trend"):
        return None
    days = int(R["entry_trend_days"])
    ok = trend_up(pts, days)
    if ok is None:
        return f"own {days}d trend: not enough history"
    if not ok:
        return f"own {days}d trend is not up"
    return None


def bought_this_afternoon(con, sleeve, asset, lt, hour):
    """True when `sleeve` bought `asset` today at or after `hour` UK. An older hold does not count."""
    if lt.hour < hour:
        return False
    row = con.execute("SELECT opened FROM positions WHERE sleeve=? AND asset=?", (sleeve, asset)).fetchone()
    if row and row[0]:
        d = datetime.fromisoformat(row[0]).astimezone(LON)
        if d.date() == lt.date() and d.hour >= hour:
            return True
    for (ts,) in con.execute("SELECT ts FROM trades WHERE sleeve=? AND asset=? AND side='BUY'", (sleeve, asset)):
        d = datetime.fromisoformat(ts).astimezone(LON)
        if d.date() == lt.date() and d.hour >= hour:
            return True
    return False


def entry_block(con, others, asset, pts, lt, R):
    """Reason to skip a new entry, or None. `others` is the sleeves that must not have bought today."""
    msg = own_trend_block(pts, R)
    if msg:
        return msg
    if R.get("no_day_core_same_afternoon"):
        for oth in others:
            if bought_this_afternoon(con, oth, asset, lt, int(R["afternoon_hour_uk"])):
                return f"{oth} already bought {asset} this afternoon"
    return None


def burst_target_action(breakout, volume_ok, rsi, rsi_max, sentiment_ok, market_ok, conf, threshold):
    """At a burst take-profit: keep the position only when the entry signal is still a buy.

    The same gates as a new burst entry: the breakout is still there, volume has not faded,
    RSI is under the entry cap, sentiment and the market are still acceptable, and confidence
    is still at least the entry threshold. Returns (keep, why).
    """
    fails = []
    if not breakout:
        fails.append("breakout no longer intact")
    if not volume_ok:
        fails.append("volume faded")
    if rsi is not None and float(rsi) >= float(rsi_max):
        fails.append(f"RSI {float(rsi):.0f} >= {float(rsi_max):g}")
    if not sentiment_ok:
        fails.append("sentiment below the entry line")
    if not market_ok:
        fails.append("market no longer supports it")
    shown = 0.0 if conf is None else float(conf)
    if conf is None or shown < float(threshold):
        fails.append(f"confidence {shown:.2f} < {float(threshold):.2f}")
    if fails:
        return False, "; ".join(fails)
    return True, f"confidence {shown:.2f} >= {float(threshold):.2f}"


def sold_at_target_note(why):
    return f"burst runner: sold at target ({why})"


def ran_note(chg, peak_gain, target_pct, giveback_pct):
    """Extra gain is the exit versus the target. Giveback is how far the exit is under the peak."""
    extra = float(chg) * 100 - float(target_pct)
    gave = (float(peak_gain) - float(chg)) * 100
    return (f"burst runner: ran, extra {extra:+.1f}pp vs target +{float(target_pct):.1f}%, "
            f"giveback {gave:.1f}pp ({float(giveback_pct):.0f}% of peak +{float(peak_gain)*100:.1f}%)")


def fallback_fraction(ret_7d, rsi, R):
    """1.0, or 0.5 when the 18:00 fallback is on and the coin is extended."""
    if not R.get("core_fallback_half"):
        return 1.0
    extended = ((ret_7d is not None and ret_7d > float(R["core_ext_7d_pct"]))
                or (rsi is not None and rsi > float(R["core_ext_rsi"])))
    return 0.5 if extended else 1.0


def extended_note(ret_7d, rsi, R):
    bits = []
    if ret_7d is not None and ret_7d > float(R["core_ext_7d_pct"]):
        bits.append(f"7d {ret_7d:+.0f}% > {float(R['core_ext_7d_pct']):g}%")
    if rsi is not None and rsi > float(R["core_ext_rsi"]):
        bits.append(f"RSI {rsi:.0f} > {float(R['core_ext_rsi']):g}")
    return ", ".join(bits)


def dir_path():
    return Path(os.environ.get("VARIANTS_DIR") or DATA / "variants")


def main_db():
    return Path(os.environ.get("VARIANTS_MAIN_DB") or DB)


def _backup(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close(); s.close()


def ensure_snapshot(root=None):
    """Copy the main book once. Later cycles keep this file so every variant shares the start."""
    root = root or dir_path()
    root.mkdir(parents=True, exist_ok=True)
    snap = root / "_snapshot.db"
    man = root / "manifest.json"
    if snap.exists() and man.exists():
        return snap
    src = main_db()
    _backup(src, snap)
    import json
    con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT equity, ts FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        con.close()
    man.write_text(json.dumps(dict(created=now().isoformat(), source=str(src),
                                   equity=None if not row else row[0], equity_ts=None if not row else row[1]), indent=2))
    return snap


def _sync(src_path, dst_path):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        for table, col in MARKET:
            scols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            dcols = {r[1] for r in dst.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in scols if c in dcols]
            if not cols or col not in cols:
                continue
            mx = dst.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
            q = f"SELECT {','.join(cols)} FROM {table}"
            args = ()
            if mx:
                q += f" WHERE {col}>?"
                args = (mx,)
            rows = src.execute(q, args).fetchall()
            if rows:
                ph = ",".join("?" * len(cols))
                dst.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})", rows)
        for key in INPUT_STATE:
            row = src.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            if row:
                dst.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, row[0]))
        dst.commit()
    finally:
        dst.close(); src.close()


def prepare(name, root=None):
    root = root or dir_path()
    snap = ensure_snapshot(root)
    path = root / f"{name}.db"
    if not path.exists():
        shutil.copy(snap, path)
    _sync(main_db(), path)
    return path


def _trade_child(name):
    """common.DB is bound when this process imported common, which is before TRADING_DB exists.
    Point that global at the variant file, then run the trader."""
    import common
    os.environ["TRADING_VARIANT"] = name
    path = prepare(name)
    common.DB = path
    os.environ["TRADING_DB"] = str(path)
    os.environ.setdefault("TRADING_LOG", str(ROOT / "logs" / f"variant-{name}.log"))
    import trader
    trader.main()


def run_all():
    """After the main trader. Failures are printed and skipped. Returns total seconds."""
    c = cfg()
    block = c.get("variants") or {}
    names = list(block.get("run") or [])
    if not block.get("enabled") or not names:
        print("[variants] off")
        return 0.0
    t0 = time.perf_counter()
    parts = []
    for name in names:
        if not (ROOT / "config" / "variants" / f"{name}.yaml").exists():
            print(f"[variants] {name} has no overlay, skipped")
            continue
        one = time.perf_counter()
        env = os.environ.copy()
        env["TRADING_VARIANT"] = name
        env["VARIANTS_DIR"] = str(dir_path())
        env["VARIANTS_MAIN_DB"] = str(main_db())
        env["TRADING_OFFLINE"] = "1"   # the child reads prices the main cycle already stored
        env.pop("TRADING_DB", None)
        try:
            r = subprocess.run([sys.executable, str(ROOT / "variants.py"), "--trade", name],
                               cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            print(f"[variants] {name} timed out after 180s")
            parts.append(f"{name} timeout")
            continue
        except Exception as e:
            print(f"[variants] {name} failed to start: {e}")
            parts.append(f"{name} error")
            continue
        dt = time.perf_counter() - one
        tail = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("[trader")]
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            print(f"[variants] {name} failed in {dt:.1f}s" + (f": {err[-1]}" if err else ""))
            parts.append(f"{name} failed {dt:.1f}s")
        else:
            print(f"[variants] {name} {dt:.1f}s" + (f" | {tail[-1]}" if tail else ""))
            parts.append(f"{name} {dt:.1f}s")
    total = time.perf_counter() - t0
    print(f"[variants] total {total:.1f}s ({', '.join(parts)})")
    return total


def _curve(con, since):
    rows = con.execute("SELECT ts, equity FROM equity WHERE ts>=? ORDER BY ts", (since or "",)).fetchall()
    if len(rows) > 400:
        step = (len(rows) - 1) / 399
        rows = [rows[min(int(i * step), len(rows) - 1)] for i in range(400)]
    out, seen = [], set()
    for r in rows:
        try:
            t = int(datetime.fromisoformat(r[0]).timestamp())
        except Exception:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(dict(t=t, eq=round(r[1], 2)))
    return out


def _since(con, since, start_equity):
    """Comparison figures from the shared snapshot. Lifetime stats would hide a new rule under the identical history."""
    from dashboard import stats
    st = stats(con)
    base = start_equity if start_equity is not None else st["start"]
    if not since:
        return dict(equity=st["equity"], start=base, pnl=st["equity"] - base, trades=st["trades"], closed=st["closed"],
                    win_rate=st["win_rate"], max_dd=st["max_dd"], realized=st["realized"])
    sells = [r[0] for r in con.execute("SELECT pnl FROM trades WHERE side='SELL' AND ts>?", (since,))]
    nt = con.execute("SELECT COUNT(*) FROM trades WHERE side IN ('BUY','SELL') AND ts>?", (since,)).fetchone()[0]
    peak, mdd = 0.0, 0.0
    for (v,) in con.execute("SELECT equity FROM equity WHERE ts>=? ORDER BY ts", (since,)):
        if v is None:
            continue
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1)
    return dict(equity=st["equity"], start=base, pnl=st["equity"] - base, trades=nt, closed=len(sells),
                win_rate=(sum((x or 0) > 0 for x in sells) / len(sells)) if sells else None,
                max_dd=mdd, realized=sum(x or 0 for x in sells))


def _book(con, name, label, blurb, since, start_equity):
    from dashboard import setup_scorecard
    st = _since(con, since, start_equity)
    return dict(
        name=name, label=label, blurb=blurb,
        equity=round(st["equity"], 2), start=round(st["start"], 2),
        pnl=round(st["pnl"], 2),
        ret=round((st["equity"] / st["start"] - 1) * 100, 3) if st["start"] else None,
        trades=st["trades"], closed=st["closed"],
        win_rate=None if st["win_rate"] is None else round(st["win_rate"] * 100, 1),
        max_dd=round(st["max_dd"] * 100, 3),
        realized=round(st["realized"], 2),
        scorecard=setup_scorecard(con, since),
        curve=_curve(con, since),
    )


def report():
    """Dashboard payload. Missing books are an empty list, not an error."""
    import json
    c = cfg()
    block = c.get("variants") or {}
    root = dir_path()
    man = {}
    if (root / "manifest.json").exists():
        try:
            man = json.loads((root / "manifest.json").read_text())
        except Exception:
            man = {}
    books = []
    since = man.get("equity_ts") or man.get("created")
    if (root / "_snapshot.db").exists() and main_db().exists():
        con = sqlite3.connect(f"file:{main_db()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            books.append(_book(con, "main", "Main book", "Frozen rules. This is the book the cycle commits.", since, man.get("equity")))
        except Exception as e:
            books.append(dict(name="main", label="Main book", error=str(e)))
        finally:
            con.close()
    for name in list(block.get("run") or []):
        path = root / f"{name}.db"
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            books.append(_book(con, name, LABELS.get(name, name), BLURB.get(name, ""), since, man.get("equity")))
        except Exception as e:
            books.append(dict(name=name, label=LABELS.get(name, name), error=str(e)))
        finally:
            con.close()
    return dict(enabled=bool(block.get("enabled")), since=since, start_equity=man.get("equity"),
                books=books)


def summary_line():
    rep = report()
    if not rep["books"]:
        return "VARIANTS not started yet" if rep["enabled"] else "VARIANTS off"
    bits = []
    for b in rep["books"]:
        if b.get("error"):
            bits.append(f"{b['label']} error")
            continue
        wr = "n/a" if b["win_rate"] is None else f"{b['win_rate']:.0f}%"
        bits.append(f"{b['label']} £{b['equity']:.2f} (P&L £{b['pnl']:+.2f}, win {wr}, max DD {b['max_dd']:.1f}%)")
    since = (rep.get("since") or "")[:16].replace("T", " ")
    return "VARIANTS since " + (since or "?") + ": " + " | ".join(bits)


def main(argv):
    if len(argv) >= 3 and argv[1] == "--trade":
        _trade_child(argv[2])
        return
    run_all()


if __name__ == "__main__":
    main(sys.argv)
