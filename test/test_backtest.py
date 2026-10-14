"""Report-only backtest: the scorecard math and a two-step replay. No live rules change.
Usage: python3 test/test_backtest.py
"""
import json, os, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "empty.db")
os.environ["TRADING_LOG"] = str(TMP / "bt.log")
os.environ.pop("TRADING_VARIANT", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backtest

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

n, win, pf = backtest.closed_stats([2.0, -1.0, 3.0])
check(n == 3 and abs(win - 2 / 3) < 1e-9 and abs(pf - 5.0) < 1e-9, "profit factor is wins over absolute losses")
n, win, pf = backtest.closed_stats([1.0])
check(pf == float("inf") and win == 1, "a book with no losses has an infinite profit factor")
check(backtest.closed_stats([]) == (0, None, None), "no closed trades has no win rate")
check(abs(backtest.max_drawdown([100, 120, 90, 90]) - (90 / 120 - 1)) < 1e-9, "drawdown is the worst peak to trough")

good = dict(trades=100, win_rate=0.5, pf=1.1, max_dd=-0.2, pnl=5, bh_pnl=4)
check(backtest.judge(good) == [], "a book on every bar passes")
bad = dict(trades=12, win_rate=0.4, pf=0.8, max_dd=-0.25, pnl=1, bh_pnl=2)
why = backtest.judge(bad)
check("trades<100" in why and "max drawdown>20%" in why and "profit factor<1.1" in why
      and "does not beat buy-and-hold" in why, "each miss is its own fail flag")

# Two prints, one step. BTC doubles, so buy-and-hold is largely the double minus fees.
src = TMP / "src.db"
os.environ["TRADING_DB"] = str(src)
import common
common.DB = src
con = common.db()
t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
t1 = t0 + timedelta(hours=2)
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (t0.isoformat(), "USDGBP", 0.75, "test", None, None))
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
            (t0.isoformat(), "BTC", "crypto", 100.0, None, None, 1e9, 1e12, "test", t0.isoformat()))
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
            (t1.isoformat(), "BTC", "crypto", 200.0, None, None, 1e9, 1e12, "test", t1.isoformat()))
con.commit()
con.close()
steps = backtest.clock(src, 60)
check(len(steps) == 2, f"two hours of prices are two clock steps ({len(steps)})")
bh = backtest.btc_hold(sqlite3.connect(src), 200, t0.isoformat(), t1.isoformat(), 0.003)
expect = 200 * (1 - 0.003) / (1 + 0.003) * 2 - 200
check(bh is not None and abs(bh - expect) < 1e-6, f"BTC buy-and-hold P&L is {bh}")

row = backtest.replay(src, "main", steps[:1], TMP / "main.db")
check(row["book"] == "main" and row.get("error") is None, f"one replay step finishes ({row.get('error')})")
check("trades<100" in (row.get("fail") or []), "a one-step book fails the 100-trade bar")
text = backtest.report_table([row])
check("main" in text and "FAIL" in text, "the table names the book and the fail")

# Default step is 5 minutes, the live cycle. Two prints ten minutes apart are two steps.
t_a = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
t_b = t_a + timedelta(minutes=10)
con = sqlite3.connect(src)
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
            (t_a.isoformat(), "ETH", "crypto", 1.0, None, None, 1, 1, "test", t_a.isoformat()))
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
            (t_b.isoformat(), "ETH", "crypto", 1.0, None, None, 1, 1, "test", t_b.isoformat()))
con.commit(); con.close()
five = [s for s in backtest.clock(src) if s.date() == t_a.date() and s.hour == 12]
check(len(five) == 2, f"the default clock is 5 minutes ({[s.isoformat() for s in five]})")

# A later daily close and a later Risk screen are not visible at an earlier clock.
early = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
later_day = "2026-10-05"
con = sqlite3.connect(src)
con.execute("INSERT INTO daily_closes VALUES(?,?,?)", ("FUTURE", later_day, 9.0))
con.execute("INSERT INTO daily_closes VALUES(?,?,?)", ("PAST", "2026-10-01", 3.0))
con.execute("INSERT OR REPLACE INTO state VALUES('risk_screen',?)", (json.dumps(
    dict(ts="2026-10-05T12:00:00+00:00", top=[dict(a="FUTURE", score=1, ok=True, why="later")], universe=1, liquid=1, ok=1)),))
con.commit(); con.close()
seen = backtest.replay(src, "main", [early], TMP / "asof.db")
check(seen.get("error") is None, f"rewind replay finishes ({seen.get('error')})")
con = sqlite3.connect(TMP / "asof.db")
future = con.execute("SELECT d FROM daily_closes WHERE asset='FUTURE'").fetchall()
past = con.execute("SELECT d FROM daily_closes WHERE asset='PAST'").fetchall()
screen = json.loads(con.execute("SELECT value FROM state WHERE key='risk_screen'").fetchone()[0])
kept = con.execute("SELECT d FROM bt_daily_closes WHERE asset='FUTURE'").fetchall()
con.close()
check(future == [] and past == [("2026-10-01",)], f"a close after the clock is hidden ({future}, past {past})")
check(kept == [(later_day,)], "the full close history is kept for later clocks")
check((screen.get("top") or []) == [], f"a Risk screen timestamped later is hidden ({screen.get('ts')})")

paired = backtest.pair_entries([
    dict(sleeve="risk", asset="CFX", side="BUY", pnl=None, snap='{"p":0.55}'),
    dict(sleeve="risk", asset="CFX", side="SELL", pnl=1.5, snap=None),
    dict(sleeve="day", asset="AAA", side="BUY", pnl=None, snap='{"p":0.85}'),
    dict(sleeve="day", asset="AAA", side="SELL", pnl=-0.4, snap=None),
])
buckets = { (b["lo"], b["hi"]): b for b in backtest.calibration(paired) }
check(buckets[(0.4, 0.6)]["n"] == 1 and buckets[(0.4, 0.6)]["wins"] == 1, "p 0.55 that then wins sits in 0.4-0.6")
check(buckets[(0.8, 1.0)]["n"] == 1 and buckets[(0.8, 1.0)]["wins"] == 0, "p 0.85 that then loses sits in 0.8-1.0")
cal = backtest.format_calibration([dict(book="main", calibration=backtest.calibration(paired))])
check("0.4-0.6" in cal and "0.8-1.0" in cal, "the calibration table names the buckets")

print(f"\n{len(fails)} failed" if fails else "backtest ok")
sys.exit(1 if fails else 0)
