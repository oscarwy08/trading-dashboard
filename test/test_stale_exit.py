"""A held quote older than the dead window must not stop, trail, or flatten.

Offline the window is max_price_age_min (45). Live it is price_dead_min (180):
that is the STONK case, a CoinGecko print hours older than the fill. The position
is marked unpriced and sells on the first fresh print.
"""
import os, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(__file__).resolve().parent / "_stale_exit"
TMP.mkdir(exist_ok=True)
DB = TMP / "book.db"
if DB.exists():
    DB.unlink()
os.environ["TRADING_DB"] = str(DB)
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["EXIT_SHADOW_DB"] = str(TMP / "shadow.db")
os.environ.pop("TRADING_VARIANT", None)

import common, trader, exit_watch
from common import LON

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

def price(con, ts, asset, px):
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, asset, "crypto", px, None, None, None, 1e11, "test", ts))

T = datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc)  # 22:30 UK
common._NOW = T
con = common.db()
ts = T.isoformat()
old = (T - timedelta(minutes=400)).isoformat()
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
for sl in common.SLEEVES:
    for ac in common.ACCOUNTS:
        con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
con.execute("INSERT INTO state VALUES('core_plan',?)", ('{}',))
con.execute("INSERT INTO state VALUES('core_last_rebalance',?)", ('"' + ts + '"',))
con.execute("INSERT INTO state VALUES('order_queue',?)", ('[]',))
# STONK: Day, flatten is due, quote is 6h40m old (the live case was 6h15m).
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "STONK", "crypto", 1, 7.4, 7.4, old, 1))
price(con, old, "STONK", 7.4)
# Risk: 20% through a 12% stop, same dead quote. Must not book it.
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("risk", "DEAD", "crypto", 1, 100, 100, old, 1))
price(con, old, "DEAD", 80.0)
con.commit(); con.close()

trader.main()
con = common.db()
held = {r[0] for r in con.execute("SELECT asset FROM positions")}
check(held == {"STONK", "DEAD"}, f"dead quotes do not flatten or stop (still held: {sorted(held)})")
reasons = {r[0]: r[1] for r in con.execute(
    "SELECT asset, reason FROM signals WHERE action='HOLD' ORDER BY rowid")}
check("unpriced" in (reasons.get("STONK") or ""), f"STONK is marked unpriced ({reasons.get('STONK')})")
check("unpriced" in (reasons.get("DEAD") or ""), f"the risk stop is marked unpriced ({reasons.get('DEAD')})")

fresh = (T - timedelta(minutes=5)).isoformat()
price(con, fresh, "STONK", 7.4)
price(con, fresh, "DEAD", 80.0)
con.commit(); con.close()
trader.main()
con = common.db()
left = {r[0] for r in con.execute("SELECT asset FROM positions")}
sells = {r[0]: r[1] for r in con.execute("SELECT asset, reason FROM trades WHERE side='SELL'")}
check(left == set() and "flatten" in (sells.get("STONK") or "") and "stop-loss" in (sells.get("DEAD") or ""),
      f"the first fresh print sells both ({sells})")
con.close()

# Live window is 180 min, not the offline 45. A 100 min quote is usable; 200 min is dead.
con = common.db()
price(con, (T - timedelta(minutes=100)).isoformat(), "NEW", 1.0)
price(con, (T - timedelta(minutes=200)).isoformat(), "OLD", 1.0)
con.commit()
saved = os.environ.pop("TRADING_DB")
try:
    px = trader.latest_prices(con, 45, 1.0, {"price_dead_min": 180, "max_price_age_min": 45, "fresh_full_min": {}})
finally:
    os.environ["TRADING_DB"] = saved
con.close()
check(px["NEW"]["fresh"] is True and px["OLD"]["fresh"] is False,
      "live: 100 min is inside 180, 200 min is unpriced")

# The watcher uses the same dead window when it has no fresh tick.
os.environ["EXIT_WATCH_SHADOW"] = "1"
common._NOW = T
con = common.db()
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "QUOTE", "crypto", 1, 1.0, 1.0, old, 1))
price(con, old, "QUOTE", 1.0)
con.commit()
quiet = exit_watch.shadow_evaluate(con, {})
loud = exit_watch.shadow_evaluate(con, {"QUOTE": 1.0})
con.close()
check(quiet == [], "the watcher does not flatten a 400 min quote")
check(any(e.get("asset") == "QUOTE" and "flatten" in (e.get("reason") or "") for e in loud),
      "the watcher flattens that position on the first fresh tick")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
