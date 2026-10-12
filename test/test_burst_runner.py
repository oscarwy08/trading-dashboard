"""Burst runner: a paper book that can hold a Burst past its target.

The main book (flag off) still sells at the target. On the burst_runner book, an intact
entry signal keeps the position and the trail then gives back 20% of the peak gain
(peak +18% exits near +14.4%). A faded signal sells at the target.

Prices are inserted with no market cap so the sell-side fee stays at fee_slippage_pct
(0.3%). Net gain is price * (1 - fee) / entry - 1.
"""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "main.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["TRADING_OFFLINE"] = "1"
os.environ.pop("TRADING_VARIANT", None)

import common, trader, variants

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

FEE = 0.003
def px_for(net):
    return (1.0 + net) / (1.0 - FEE)

# --- the decision itself, before any book is built ---
keep, why = variants.burst_target_action(True, True, 60, 80, True, True, 0.70, 0.55)
check(keep and "0.70" in why, "intact signal at the target keeps the position")
keep, why = variants.burst_target_action(True, True, 80, 80, True, True, 0.70, 0.55)
check(not keep and "RSI 80" in why, "RSI at the entry cap sells at the target")
keep, why = variants.burst_target_action(False, True, 50, 80, True, True, 0.70, 0.55)
check(not keep and "breakout" in why, "a broken breakout sells at the target")
keep, why = variants.burst_target_action(True, True, 50, 80, True, True, 0.50, 0.55)
check(not keep and "confidence 0.50" in why, "confidence under the entry line sells at the target")
note = variants.ran_note(0.144, 0.18, 8, 20)
check("extra +6.4pp" in note and "target +8.0%" in note and "giveback 3.6pp" in note
      and "20% of peak +18.0%" in note, "ran note records extra gain and the 20% giveback")
check("sold at target" in variants.sold_at_target_note("breakout no longer intact"),
      "a fade is logged as sold at target")

base = common.cfg()
os.environ["TRADING_VARIANT"] = "burst_runner"
over = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(over["variant_rules"]["burst_runner"] is True and not over["variant_rules"]["ev_exit"]
      and not base["variant_rules"]["burst_runner"]
      and over["rules"]["frozen_since"] == base["rules"]["frozen_since"]
      and over["day"]["burst"]["trail_giveback_pct"] == base["day"]["burst"]["trail_giveback_pct"] == 40,
      "burst_runner turns on only its flag and leaves the frozen trail at 40%")
check(variants.rules_of(over)["burst_runner_giveback_pct"] == 20, "the runner giveback is 20% of the peak gain")
fee = trader.cost_pct(base, "TEST", "crypto", "SELL", {"price": 1.0, "mcap": None}, 10)
check(abs(fee - FEE) < 1e-12, f"unknown market cap keeps the 0.3% fee (got {fee})")

def fresh():
    p = Path(os.environ["TRADING_DB"])
    if p.exists(): p.unlink()
    for ext in ("-wal", "-shm"):
        q = Path(str(p) + ext)
        if q.exists(): q.unlink()
    common._NOW = None
    return common.db()

def add_px(con, ts, asset, px, vol=None):
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts.isoformat(), asset, "crypto", px, None, None, vol, None, "test", ts.isoformat()))

def seed(t0, prior_above=False):
    """Open burst at entry 1.0. History makes a 1.2% 24h break with RSI near 50 and volume 4x,
    unless prior_above puts the old high above the target price so the breakout is gone."""
    con = fresh()
    ts = t0.isoformat()
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
    for sl in common.SLEEVES:
        for ac in common.ACCOUNTS:
            con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
    con.execute("INSERT INTO state VALUES('core_plan',?)", (json.dumps({}),))
    con.execute("INSERT INTO state VALUES('core_last_rebalance',?)", (json.dumps(ts),))
    con.execute("INSERT INTO state VALUES('order_queue',?)", (json.dumps([]),))
    cur = px_for(0.08)
    hi = cur * 1.05 if prior_above else cur / 1.012
    for i in range(-23, 0):
        tick = t0 + timedelta(hours=i)
        close = hi if i % 2 == 0 else hi * 0.985
        if i >= -8:
            for m in (0, 15, 30, 45):
                pt = tick + timedelta(minutes=m)
                val = close if m == 45 else min(close, hi * 0.99)
                vol = 1000.0 if i == -1 and m == 0 else None
                add_px(con, pt, "TEST", val, vol)
        else:
            add_px(con, tick, "TEST", close)
    add_px(con, t0, "TEST", cur, 1125.0)
    for i in range(-24, 1):
        add_px(con, t0 + timedelta(hours=i), "BTC", 100.0)
    opened = (t0 - timedelta(hours=2)).isoformat()
    con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                ("day", "TEST", "crypto", 10, 1.0, 10, opened, 1))
    con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps({"TEST": dict(
        sub="burst", tp=8, sl=2.5, atr=1, peak=1.0, opened=opened)}),))
    con.commit(); con.close()

def sells():
    con = common.db()
    rows = con.execute("SELECT reason, price FROM trades WHERE asset='TEST' AND side='SELL'").fetchall()
    held = con.execute("SELECT COUNT(*) FROM positions WHERE sleeve='day' AND asset='TEST'").fetchone()[0]
    meta = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0] or "{}")
    con.close()
    return rows, held, meta

def cycle(t, net=None):
    if net is not None:
        con = common.db()
        add_px(con, t, "TEST", px_for(net))
        con.commit(); con.close()
    common._NOW = t
    trader.main()

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)   # 13:00 UK, same day, before the 22:00 flatten

# --- flag off: the frozen book sells at the target even when the signal is intact ---
seed(T0)
cycle(T0)
rows, held, meta = sells()
reason = rows[0]["reason"] if rows else ""
check(len(rows) == 1 and "take-profit" in reason and "burst runner" not in reason,
      f"flag off sells a plain take-profit ({reason or 'no sell'}; still open {held})")

# --- faded signal: sell at the target and say so ---
seed(T0, prior_above=True)
os.environ["TRADING_VARIANT"] = "burst_runner"
cycle(T0)
os.environ.pop("TRADING_VARIANT", None)
rows, held, meta = sells()
reason = rows[0]["reason"] if rows else ""
check(len(rows) == 1 and held == 0 and "sold at target" in reason and "breakout" in reason and "ran," not in reason,
      f"faded signal sells at the target ({reason or 'no sell'})")

# --- intact signal: hold through +18%, then a 20% giveback of that peak exits near +14.4% ---
seed(T0)
os.environ["TRADING_VARIANT"] = "burst_runner"
cycle(T0)
rows, held, meta = sells()
runner = (meta.get("TEST") or {}).get("runner")
check(not rows and held == 1 and runner is True,
      f"intact signal at +8% becomes a runner (sells {len(rows)}, open {held}, runner {runner}"
      + (f", reason {rows[0]['reason']}" if rows else "") + ")")
if runner and held:
    t = T0
    for net in (0.12, 0.18):
        t += timedelta(minutes=5)
        cycle(t, net)
        rows, held, meta = sells()
        check(not rows and held == 1, f"runner still open at +{net*100:.0f}%")
    exit_reason, exit_net = "", None
    net = 0.145
    for _ in range(60):
        if rows or not held:
            break
        t += timedelta(minutes=1)
        cycle(t, net)
        rows, held, meta = sells()
        if rows:
            exit_reason = rows[0]["reason"]
            exit_net = rows[0]["price"] * (1 - FEE) - 1
            break
        net = round(net - 0.0001, 4)
    ok = (exit_net is not None and abs(exit_net * 100 - 14.4) < 0.05 and "ran," in exit_reason
          and "peak +18" in exit_reason and "gives back 20%" in exit_reason and "extra +" in exit_reason)
    check(ok, f"pullback from +18% exits near +14.4% (got {None if exit_net is None else round(exit_net*100, 3)}%: {exit_reason})")
    if rows:
        t += timedelta(minutes=1)
        cycle(t, 0.10)
        rows2, held2, _ = sells()
        check(len(rows2) == 1 and held2 == 0, "a second tick does not sell again")
os.environ.pop("TRADING_VARIANT", None)

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
