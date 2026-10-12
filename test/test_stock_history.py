"""Stock intraday history: Yahoo 15-minute closes are stored, and a series that cannot support
4h / RSI is stale rather than a neutral RSI 50 and a 0% move. Thresholds are not involved.
Usage: python3 test/test_stock_history.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "hist.db")
os.environ["TRADING_LOG"] = str(TMP / "hist.log")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common, collector, trader

UTC = timezone.utc
fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)

# ---- momentum: missing bars are stale; a real flat tape is not RSI 50 by default ----
short = [(at(12, 14) + timedelta(minutes=15 * i), 100.0) for i in range(24)]   # 6h, one session, under 15 hourly closes
m, info, rs = trader.momentum(short, 4)
check(m is None and info == "stale history" and rs is None, "6h of prints with no 15 hourly closes is stale, not RSI 50")
check("RSI 50" not in (info or ""), "stale reason does not claim RSI 50")

tiny = [(at(12, 15), 100.0), (at(12, 16), 101.0)]
m, info, rs = trader.momentum(tiny, 4)
check(m is None and info == "insufficient history", "under min_history_h stays insufficient history")

flat = [(at(6, 14) + timedelta(hours=i), 100.0) for i in range(20)]
m, info, rs = trader.momentum(flat, 4)
check(m is not None and rs == 100 and "4h +0.0%" in info, "a real flat tape scores (RSI 100 when nothing fell, 4h +0.0%)")
check("RSI 50" not in info, "the flat tape is not the neutral default")

rise = [(at(6, 14) + timedelta(hours=i), 100.0 + i) for i in range(30)]
m, info, rs = trader.momentum(rise, 4)
check(m is not None and rs == 100 and "4h " in info and "24h " in info and "EMA6/24 up" in info, "30 hourly closes produce a real 4h/24h/RSI/EMA")
check(abs(m) > 0.2, "a real up-move is not stuck on the +/-0.2 EMA-only score")

# ---- stored Yahoo bars: USD, pence, idempotent; stock score uses sessions outside 72h ----
con = common.db()
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", ("2026-10-12T12:00:00+00:00", "USDGBP", 0.75, "test", None, None))
meta_usd = {"currency": "USD"}
t0 = int(at(8, 14, 0).timestamp())
bars = [(t0 + 900 * i, 50.0 + i * 0.1) for i in range(8)]
n1 = collector.store_stock_bars(con, "USDSTK", meta_usd, bars)
n2 = collector.store_stock_bars(con, "USDSTK", meta_usd, bars)
check(n1 == 8 and n2 == 0, "15m bars insert once")
check(con.execute("SELECT COUNT(*), MIN(price), MAX(price) FROM prices WHERE asset='USDSTK'").fetchone()[0] == 8, "eight USD rows")
px = con.execute("SELECT price FROM prices WHERE asset='USDSTK' ORDER BY ts LIMIT 1").fetchone()[0]
check(abs(px - 50.0) < 1e-9, "USD close stored as-is")

n3 = collector.store_stock_bars(con, "GBPSTK", {"currency": "GBp"}, [(t0, 664.5)])
got = con.execute("SELECT price FROM prices WHERE asset='GBPSTK'").fetchone()[0]
check(n3 == 1 and abs(got - 664.5 * 0.01 / 0.75) < 1e-9, "GBp 15m close converts to USD")
check(collector.store_stock_bars(con, "USDSTK", {"currency": "ZZZ"}, [(t0, 1)]) == 0, "unknown currency stores nothing")

# Wednesday-Friday session hours, scored on Monday. The 72h window only sees Friday afternoon.
def session(day, start, n, p0):
    return [(at(day, start) + timedelta(hours=i), p0 + i) for i in range(n)]

pts = session(7, 14, 7, 100) + session(8, 14, 7, 110) + session(9, 14, 7, 120)
for ts, p in pts:
    con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts.isoformat(), "WEEK", "stock", p, None, None, None, None, "yahoo-15m", ts.isoformat()))
monday = at(12, 16)
common._NOW = monday
st = {"min_history_h": 4, "sentiment_max_age_h": 12, "sentiment_weight": 0.25}
m72, info72, _ = trader.momentum(trader.history(con, "WEEK", 72), 4)
check(m72 is None and info72 == "stale history", "Friday afternoon alone is stale (the old RSI-50 case)")
m, _s, comb, info, rs_, hist = trader.score_asset(con, "WEEK", st, monday, "stock")
check(m is not None and rs_ not in (None, 50) and "4h " in info and "24h " in info and "RSI" in info, "eight-day stock history scores a real RSI, 4h and 24h")
check(hist and hist[0][0] >= monday - timedelta(hours=72), "breakout history stays the 72h window")
check("RSI 50" not in info, "scored stock row is not the neutral default")
con.close()

print(f"\n{len(fails)} failed" if fails else "stock history ok")
sys.exit(1 if fails else 0)
