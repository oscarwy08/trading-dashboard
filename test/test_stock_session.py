"""Stock hit rates stay apart from crypto, and stock clocks use market hours.

A weekend gap is not a 4-hour stock move. Crypto without klass= still uses wall-clock time.
Usage: python3 test/test_stock_session.py
"""
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "sess.db")
os.environ["TRADING_LOG"] = str(TMP / "sess.log")
os.environ["TRADING_OFFLINE"] = "1"
os.environ.pop("TRADING_VARIANT", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common
import daypot
import riskpot
import trader

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

UTC = timezone.utc
# Friday close sold off to 80. Monday 10:00 UK is still 80. Wall-clock 4h is that close.
# Four session hours reaches back to Friday morning at 100, so the stock 4h move is the selloff.
mon = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
pts = []
for day in (6, 7, 8):
    for h in range(8, 16):
        pts.append((datetime(2026, 10, day, h, 0, tzinfo=UTC), 100.0))
pts.append((datetime(2026, 10, 9, 8, 0, tzinfo=UTC), 100.0))
pts.append((datetime(2026, 10, 9, 15, 0, tzinfo=UTC), 80.0))
pts.append((mon, 80.0))

wall = trader.momentum(pts, 4)
sess = trader.momentum(pts, 4, asset="RBW.L", klass="stock")
check(wall[0] is not None and "4h +0.0%" in wall[1], f"wall-clock 4h is just the Friday close ({wall[1]})")
check(sess[0] is not None and "4h -20.0%" in sess[1], f"session 4h reaches Friday morning ({sess[1]})")

# Crypto path is the wall-clock function. Passing klass crypto does not switch on session hours.
crypto_pts = [(mon - timedelta(hours=30 - i), 100 + i) for i in range(30)]
a = trader.momentum(crypto_pts, 4)
b = trader.momentum(crypto_pts, 4, asset="BTC", klass="crypto")
check(a == b, "crypto momentum is unchanged when klass is crypto or omitted")

con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE trades(id INTEGER, ts TEXT, sleeve TEXT, asset TEXT, klass TEXT, side TEXT, value REAL, gas REAL, pnl REAL, reason TEXT, sub TEXT)")
now = mon.isoformat()
con.execute("INSERT INTO trades VALUES(1,?,?,?,?,?,?,?,?,?,?)", (now, "day", "LDO", "crypto", "SELL", 10, 0, -1, "[burst 24h] stop", "burst"))
con.execute("INSERT INTO trades VALUES(2,?,?,?,?,?,?,?,?,?,?)", (now, "day", "BARC.L", "stock", "SELL", 10, 0, 1, "[burst 24h] target", "burst"))
con.execute("INSERT INTO trades VALUES(3,?,?,?,?,?,?,?,?,?,?)", (now, "risk", "LDO", "crypto", "SELL", 10, 0, -1, "stop", "dip"))
con.execute("INSERT INTO trades VALUES(4,?,?,?,?,?,?,?,?,?,?)", (now, "risk", "RBW.L", "stock", "SELL", 10, 0, -1, "stop", "dip"))
pooled = daypot.setup_stats(con, "24h", {}, mon)
crypto = daypot.setup_stats(con, "24h", {}, mon, klass="crypto")
stock = daypot.setup_stats(con, "24h", {}, mon, klass="stock")
check(pooled["n"] == 2 and crypto["n"] == 1 and stock["n"] == 1, f"burst stats split by market ({pooled['n']}, {crypto['n']}, {stock['n']})")
check(crypto["wins"] == 0 and stock["wins"] == 1, "a stock win does not count as a crypto win")
rd = riskpot.setup_stats(con, {"hit_prior": 0.5, "hit_prior_n": 20, "record_days": 30}, mon, sub="dip", klass="crypto")
rs = riskpot.setup_stats(con, {"hit_prior": 0.5, "hit_prior_n": 20, "record_days": 30}, mon, sub="dip", klass="stock")
check(rd["n"] == 1 and rs["n"] == 1, "risk dip stats split by market")

class H(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, rec):
        self.lines.append(rec.getMessage())

h = H()
logging.getLogger("prefilter").addHandler(h)
common.prefilter_reject("burst", "RBW.L", "no breakout")
os.environ["TRADING_VARIANT"] = "stock_calibrated"
common.prefilter_reject("risk-dip", "ONT.L", "not a dip")
os.environ.pop("TRADING_VARIANT", None)
check(any(x == "book=main setup=burst asset=RBW.L reason=no breakout" for x in h.lines), f"main prefilter line ({h.lines})")
check(any(x.startswith("book=stock_calibrated setup=risk-dip asset=ONT.L reason=") for x in h.lines), "variant book is named on the line")

print("stock session " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
