"""Armed entries: the cycle records a trigger and does not double-buy a watcher fill.

The main book's immediate buys are unchanged. A price that has not broken the level
is armed and not bought. A price that has broken it is bought once. If the watcher
already opened the position, the next cycle leaves it alone.
"""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "book.db")
os.environ["EXIT_SHADOW_DB"] = str(TMP / "exit_shadow.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["TRADING_OFFLINE"] = "1"
os.environ["ENTRY_WATCH_SHADOW"] = "0"
os.environ["EXIT_WATCH_SHADOW"] = "1"
os.environ.pop("TRADING_VARIANT", None)

import common
import entries
import exit_watch
import trader

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)  # 13:00 UK, inside the day window


def fresh():
    p = Path(os.environ["TRADING_DB"])
    if p.exists():
        p.unlink()
    for ext in ("-wal", "-shm"):
        q = Path(str(p) + ext)
        if q.exists():
            q.unlink()
    common._NOW = T0
    return common.db()


def add_px(con, ts, asset, px, vol=None):
    con.execute(
        "INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ts.isoformat(), asset, "crypto", px, None, None, vol, None, "test", ts.isoformat()))


def seed(assets):
    """assets: {symbol: last price}. History swings about 3% so the burst target is wide enough to clear EV.
    The prior high is 100. A last price of 100.05 has not broken it; 100.20 has."""
    con = fresh()
    ts = T0.isoformat()
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
    for sl in common.SLEEVES:
        for ac in common.ACCOUNTS:
            con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
    con.execute("INSERT INTO state VALUES('core_plan',?)", (json.dumps({}),))
    con.execute("INSERT INTO state VALUES('core_last_rebalance',?)", (json.dumps(ts),))
    con.execute("INSERT INTO state VALUES('order_queue',?)", (json.dumps([]),))
    for i in range(1, 26):
        tick = T0 - timedelta(hours=i)
        add_px(con, tick, "BTC", 100.0)
        for name in assets:
            # Hourly range about 3% and a close that alternates, so RSI stays under 80.
            # The high of the window is still 100.
            if i % 2 == 0:
                add_px(con, tick, name, 100.0)
                add_px(con, tick + timedelta(minutes=30), name, 97.0)
            else:
                add_px(con, tick, name, 97.0)
                add_px(con, tick + timedelta(minutes=30), name, 100.0)
    for name, last in assets.items():
        add_px(con, T0 - timedelta(hours=1), name, 100.0, 24000.0)
        add_px(con, T0, name, last, 48000.0)
    add_px(con, T0, "BTC", 100.0)
    con.commit()
    con.close()


def buys(asset):
    con = common.db()
    rows = con.execute("SELECT reason FROM trades WHERE asset=? AND side='BUY'", (asset,)).fetchall()
    armed = con.execute(
        "SELECT status, level, sub FROM armed_entries WHERE asset=? ORDER BY id", (asset,)).fetchall()
    held = con.execute("SELECT COUNT(*) FROM positions WHERE asset=?", (asset,)).fetchone()[0]
    con.close()
    return rows, armed, held


seed({"NEAR": 100.05, "BROKE": 100.20})
trader.main()
near_buys, near_arms, near_held = buys("NEAR")
broke_buys, broke_arms, broke_held = buys("BROKE")
check(not near_buys and near_held == 0, "a price that has not broken the 24h high is not bought")
check(any(r["status"] == "armed" and r["sub"] == "burst" and r["level"] for r in near_arms),
      "that setup is armed at the break level")
if not any(r["status"] == "armed" and r["sub"] == "burst" for r in near_arms):
    print("  NEAR arms:", [(r["status"], r["sub"], r["level"]) for r in near_arms])
check(len(broke_buys) == 1 and broke_held == 1, "a price that has broken the high is bought by the cycle")
check(not any(r["status"] == "armed" for r in broke_arms), "the cycle buy closes the arm, so the watcher will not buy it too")

# Watcher fills an armed name. The next cycle, with the price now through the level, must not buy again.
con = common.db()
row = con.execute("SELECT * FROM armed_entries WHERE asset='NEAR' AND status='armed'").fetchone()
check(row is not None, "NEAR is still armed for the watcher")
if row:
    level = float(row["level"])
    got = exit_watch.act_entries(con, {"NEAR": level + 0.01}, live=True, book="entry_watch")
    check(len(got) == 1 and got[0]["log"].startswith("BUY"), "the watcher buys when the armed level trades")
    check(exit_watch.act_entries(con, {"NEAR": level + 0.02}, live=True, book="entry_watch") == [], "the watcher does not buy twice")
con.close()
# The break is now in the stored prices, which is what the cycle would buy on its own.
con = common.db()
add_px(con, T0 + timedelta(minutes=5), "NEAR", 100.20, 50000.0)
con.commit()
con.close()
common._NOW = T0 + timedelta(minutes=5)
trader.main()
near_buys, near_arms, near_held = buys("NEAR")
check(len(near_buys) == 1 and near_held == 1, "the cycle sees the watcher fill and does not buy NEAR again")
check(not any(r["status"] == "armed" for r in near_arms), "the open position settles the arm")

# Shadow on the same book does not add a second position. EXIT_WATCH_SHADOW stays on.
os.environ["ENTRY_WATCH_SHADOW"] = "1"
check(exit_watch.entry_shadow_mode() is True and exit_watch.shadow_mode() is True,
      "entry shadow can be on while exit shadow is also on")
os.environ["EXIT_WATCH_SHADOW"] = "0"
check(exit_watch.entry_shadow_mode() is True and exit_watch.shadow_mode() is False,
      "turning exit shadow off leaves entry shadow on")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
