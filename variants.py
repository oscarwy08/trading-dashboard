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
    "everything": "Everything combined",
    "midnight_crypto": "Midnight crypto",
    "entry_watch": "Entry watch",
    "risk_giveback_15": "Risk giveback 15",
    "day_trail_arm": "Day trail arm",
    "day_ev_blend": "Day EV blend",
    "no_risk_clock": "No risk clock",
    "day_capital_caution": "Day capital caution",
}
BLURB = {
    "ev_exit": "Sells a Day or Risk position when live expected value is below -0.4% and at least 0.8 points worse than the value stored at entry. While live value is negative but still above -0.4%, the stop is three quarters of the stored stop.",
    "review_leftovers": "Burst stop is 1.5x wider unless live EV is already below -0.4%. Core's 18:00 fallback tranche is half size when the coin is up more than 15% over 7 days or RSI is above 75. New Day, Risk and Core entries need the coin's own 3-day trend up. After 12:00 UK, Day and Core will not each buy the same coin.",
    "all_combined": "EV exit and the review leftovers, on one book. The burst runner is a separate book, not part of this one.",
    "burst_runner": "Same rules as the main book, except a Burst position that hits its target keeps running when the entry signal is still intact. The trail then gives back 20% of the peak gain. Each sell says sold at target or ran, with the extra gain and the giveback.",
    "everything": "EV exit, the review leftovers, and the burst runner, on one book. It is copied from the same snapshot as the other books.",
    "midnight_crypto": "Same as the main book, except Day crypto entries run until 23:45 UK and those positions flatten at 00:00 UK. New crypto entries are allowed again from 00:00, and the daily loss budget still resets then. Stocks still flatten 15 minutes before their market closes. Copied from the same snapshot as the other books.",
    "entry_watch": "Same scoring as the main book. Setups the cycle would buy are also armed with a price trigger, a size, and the same stop and target. This is the only book the live watcher is allowed to buy when that trigger trades between cycles. The main book only logs those as WOULD BUY.",
    "risk_giveback_15": "Risk only. The trail still arms at +15% net. Once armed, it sells when the net gain falls to 65% of the peak net gain, and the 7-day exit waits while that trail is armed. The -12% stop stays. Not combined with the other paper books.",
    "day_trail_arm": "Day only. A trail arms only when 60% of the peak gain is at least the greater of 4x that trade's round-trip cost and 1x its 1-hour ATR. Once armed, a crossed trail sells. Stop, target, the burst 6-hour hold and the 22:00 flatten stay.",
    "day_ev_blend": "Day only. Each sub-strategy's expected value is its measured expectancy blended with the model: (n x measured + 10 x model) / (n + 10). Momentum and burst are measured apart. The Risk win is not changed on this book.",
    "no_risk_clock": "Risk only. The 24-hour re-entry clock is deleted. The next setup buys when its own confidence and expected value clear. No new-high gate and no stop-out cooldown.",
    "day_capital_caution": "Day only, per account. f is that account's Day loss today divided by 15% of that account's start-of-day Day equity, with no cap at 1. Momentum's threshold is its base (0.35, or 0.30 on MON, WLD and XRP) plus 0.30 x f. Burst uses the same f. Size is multiplied by (1 - min(f, 1)). The -15% halt on the whole Day pot stays.",
}
INPUT_STATE = ("risk_screen", "day_excluded", "risk_only", "risk_only_stocks", "gas")
MARKET = (("prices", "ts"), ("sentiment", "ts"), ("fx", "ts"), ("daily_closes", "d"))


def rules_of(c):
    """Flag values. Missing keys stay off, which is the main book's behaviour."""
    d = dict(ev_exit=False, ev_exit_now_pct=-0.4, ev_exit_below_entry_pp=0.8, ev_exit_stop_mult=0.75,
             burst_stop_widen=False, burst_stop_mult=1.5, core_fallback_half=False, core_ext_7d_pct=15.0,
             core_ext_rsi=75.0, entry_own_trend=False, entry_trend_days=3, no_day_core_same_afternoon=False,
             afternoon_hour_uk=12, burst_runner=False, burst_runner_giveback_pct=20,
             midnight_crypto=False, midnight_entry_start_uk="00:00", midnight_entry_cutoff_uk="23:45",
             midnight_flatten_uk="00:00", entry_watch=False, entry_watch_expiry_min=15,
             risk_giveback_15=False, risk_giveback_keep_pct=65,
             day_trail_arm=False, day_trail_cost_x=4, day_trail_atr_x=1,
             day_ev_blend=False,
             no_risk_clock=False,
             day_capital_caution=False, day_caution_raise=0.30,
             risk_ev_legacy_win=False)
    for k, v in ((c or {}).get("variant_rules") or {}).items():
        if k in d and v is not None:
            d[k] = v
    return d


def active(R):
    return bool(R.get("ev_exit") or R.get("burst_stop_widen") or R.get("core_fallback_half")
                or R.get("entry_own_trend") or R.get("no_day_core_same_afternoon") or R.get("burst_runner")
                or R.get("midnight_crypto") or R.get("entry_watch")
                or R.get("risk_giveback_15") or R.get("day_trail_arm") or R.get("day_ev_blend")
                or R.get("no_risk_clock") or R.get("day_capital_caution"))


TABLE_BOOKS = ("risk_giveback_15", "day_trail_arm", "day_ev_blend", "no_risk_clock", "day_capital_caution")


def blend_ev(model, n, exp, prior_n=10):
    """(n x measured expectancy + prior_n x model) / (n + prior_n). The model stands alone until a close exists."""
    model = float(model)
    if not n or exp is None:
        return round(model, 3)
    n, prior = float(n), float(prior_n)
    return round((n * float(exp) + prior * model) / (n + prior), 3)


def caution_f(pnl_frac, cap_pct):
    """That account's Day loss today / the daily-stop fraction. No cap at 1. A gain is 0."""
    if not cap_pct or pnl_frac is None or float(pnl_frac) >= 0:
        return 0.0
    return (-float(pnl_frac)) / (float(cap_pct) / 100.0)


def caution_threshold(base, f, raise_):
    """Base plus raise x f. f is not clipped at 1, so the hurdle keeps rising past the daily stop."""
    return round(min(0.99, float(base) + float(raise_) * max(0.0, float(f))), 3)


def caution_size_scale(f):
    """1 at no loss, 0 once f reaches 1. Size is multiplied by this. f above 1 does not go negative."""
    return max(0.0, 1.0 - min(max(0.0, float(f)), 1.0))


def day_arm_bar(R, rt_cost_pct, atr_pct):
    """Percent the locked share of the peak gain must clear before a Day trail may arm.

    None on the main book. Otherwise max(cost_x x round-trip cost, atr_x x 1-hour ATR).
    The lock is 60% of the peak when the trail gives back 40%.
    """
    if not R.get("day_trail_arm"):
        return None
    cost = 0.0 if rt_cost_pct is None else float(rt_cost_pct)
    atr = 0.0 if not atr_pct else float(atr_pct)
    return max(float(R.get("day_trail_cost_x", 4)) * cost, float(R.get("day_trail_atr_x", 1)) * atr)


def risk_on_clock(R, last_exit, t, cooldown_h):
    """True when the re-entry clock still blocks. no_risk_clock deletes it.

    There is no new-high gate and no separate cooldown after a stop.
    """
    if R.get("no_risk_clock") or not last_exit:
        return False
    hours = float(cooldown_h or 0)
    if hours <= 0:
        return False
    return datetime.fromisoformat(str(last_exit)) > t - timedelta(hours=hours)


def crypto_hours_to_flat(mins, flat_mins, midnight):
    """Hours until Day crypto is flattened.

    The main book uses flatten_uk (22:00). At or after that clock the hours are 0.
    Midnight crypto uses the next 00:00 UK. At exactly 00:00 a new entry has 24 hours,
    because yesterday's positions are flattened before the entry loop runs.
    """
    mins = int(mins)
    if midnight:
        left = (24 * 60 - mins) % (24 * 60)
        if left == 0:
            left = 24 * 60
        return left / 60.0
    return max(0.0, (int(flat_mins) - mins) / 60.0)


def crypto_entry_open(mins, start_mins, cutoff_mins):
    """True from start_mins inclusive until cutoff_mins exclusive. 00:00 is open; 23:45 is closed."""
    return int(start_mins) <= int(mins) < int(cutoff_mins)


def crypto_flatten_due(opened_day, today, flat_now, midnight):
    """Main book: the 22:00 / before-08:00 window, or a position opened on an earlier UK day.
    Midnight crypto: only a position opened on an earlier UK day (the 00:00 flatten).
    A position opened today stays open through 22:00 and through 23:45."""
    if midnight:
        return str(opened_day) != str(today)
    return bool(flat_now) or str(opened_day) != str(today)


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


def sleeve_table(con):
    """Momentum, burst and risk rows in the Day sub-strategy table shape.

    Closed-trade columns come from the same stats the Day and Risk pages use.
    Budget is that sleeve's equity (cash + open positions). Open P&L is value minus cost.
    """
    from dashboard import day_sub_stats, risk_stats
    c = cfg()
    lt = now().astimezone(LON)
    day = day_sub_stats(con, lt)
    risk = risk_stats(con, lt).get("all:all") or {}
    rate = None
    try:
        from common import usd_gbp
        fx = usd_gbp(con, (c.get("fx") or {}).get("max_age_h", 12))
        rate = fx[0] if fx else None
    except Exception:
        rate = None
    px = {}
    try:
        for asset, price in con.execute(
                """SELECT asset, price FROM (
                     SELECT asset, price, ROW_NUMBER() OVER (PARTITION BY asset ORDER BY ts DESC, rowid DESC) rn
                     FROM prices WHERE source!='backfill') WHERE rn=1"""):
            px[asset] = price
    except Exception:
        px = {}
    import json as _json
    try:
        dmeta = _json.loads((con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone() or ["{}"])[0]) or {}
    except Exception:
        dmeta = {}
    open_day = {"momentum": 0.0, "burst": 0.0}
    open_risk = 0.0
    cost_day = {"momentum": 0.0, "burst": 0.0}
    val_day = {"momentum": 0.0, "burst": 0.0}
    cost_risk = val_risk = 0.0
    for r in con.execute("SELECT sleeve, asset, klass, qty, cost FROM positions"):
        usd = px.get(r["asset"])
        val = (r["qty"] * usd * rate) if (usd is not None and rate) else (r["cost"] or 0)
        pnl = (val or 0) - (r["cost"] or 0)
        if r["sleeve"] == "day":
            sb = (dmeta.get(r["asset"]) or {}).get("sub") or "momentum"
            if sb not in open_day:
                sb = "momentum"
            open_day[sb] += pnl
            cost_day[sb] += r["cost"] or 0
            val_day[sb] += val or 0
        elif r["sleeve"] == "risk":
            open_risk += pnl
            cost_risk += r["cost"] or 0
            val_risk += val or 0
    cash = {}
    try:
        for sl, ac, amt in con.execute("SELECT sleeve, account, amount FROM acct_cash"):
            cash[(sl, ac)] = amt or 0
    except Exception:
        cash = {}
    risk_cash = sum(v for (sl, _), v in cash.items() if sl == "risk")
    day_budget = {"momentum": None, "burst": None}
    try:
        import daypot
        from common import acct_start, loans_open
        base = acct_start(con, c)
        sh = daypot.sub_shares(c["day"])
        since = ""
        try:
            raw = con.execute("SELECT value FROM state WHERE key='account_split'").fetchone()
            since = (_json.loads(raw[0]) or {}).get("ts", "") if raw else ""
        except Exception:
            since = ""
        held = []
        for r in con.execute("SELECT asset, klass, qty, cost FROM positions WHERE sleeve='day'"):
            sb = (dmeta.get(r["asset"]) or {}).get("sub") or "momentum"
            usd = px.get(r["asset"])
            val = (r["qty"] * usd * rate) if (usd is not None and rate) else (r["cost"] or 0)
            ac = "stocks" if r["klass"] == "stock" else "crypto"
            held.append(dict(sub=sb if sb in ("momentum", "burst") else "momentum", acct=ac, cost=r["cost"] or 0, value=val or 0))
        real = daypot.sub_realized(con, since)
        LO = loans_open(con)
        L = daypot.sub_ledger(
            {ac: base["day"][ac] for ac in ("crypto", "stocks")},
            {ac: cash.get(("day", ac), 0.0) for ac in ("crypto", "stocks")},
            held, real, sh, ("crypto", "stocks"),
            borrowed={(sb, ac): sum(l["out"] for l in LO if l["borrower"] == "day" and l["account"] == ac and l["sub"] == sb)
                      for sb in ("momentum", "burst") for ac in ("crypto", "stocks")},
            lent={ac: sum(l["out"] for l in LO if l["lender"] == "day" and l["account"] == ac) for ac in ("crypto", "stocks")})
        for sb in ("momentum", "burst"):
            day_budget[sb] = sum(L[ac][sb]["equity"] for ac in ("crypto", "stocks"))
    except Exception:
        day_budget = {"momentum": None, "burst": None}

    def row(label, st, budget, open_pnl):
        return dict(
            strategy=label,
            budget=None if budget is None else round(budget, 2),
            trades=st.get("trades") or 0,
            win_rate=st.get("win_rate"),
            avg_win=st.get("avg_win"),
            avg_loss=st.get("avg_loss"),
            pf=st.get("pf"),
            pnl=st.get("pnl"),
            open_pnl=round(open_pnl, 2),
            fees=st.get("fees"),
            fees_closed=st.get("fees_closed"),
            fees_open=st.get("fees_open"),
            exp_gbp=st.get("exp_gbp"),
            exp_pct=st.get("exp_pct"),
        )

    rows = []
    for sb, label in (("momentum", "Momentum"), ("burst", "Burst")):
        st = (day.get(sb) or {}).get("all:all") or {}
        rows.append(row(label, st, day_budget[sb], open_day[sb]))
    rows.append(row("Risk", risk, risk_cash + val_risk, open_risk))
    return rows


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
        table=sleeve_table(con) if name in TABLE_BOOKS else None,
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
