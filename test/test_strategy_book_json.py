"""Strategy-book dashboard export. A trend-sleeve trade must publish, and the £200 must add up.

Usage: python3 test/test_strategy_book_json.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("TRADING_VARIANT", None)
os.environ["TRADING_OFFLINE"] = "1"
os.environ["TRADING_DB"] = str(Path(tempfile.mkdtemp()) / "book.db")
os.environ["TRADING_LOG"] = str(Path(tempfile.mkdtemp()) / "t.log")

import common
import dashboard
import importlib.util
_spec = importlib.util.spec_from_file_location("check_data", Path(__file__).resolve().parent / "check_data.py")
check_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_data)

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

OPENING = {
    "core": {"crypto": 50.0, "stocks": 50.0},
    "day": {"crypto": 30.0, "stocks": 30.0},
    "risk": {"crypto": 20.0, "stocks": 20.0},
}

def fresh():
    path = Path(os.environ["TRADING_DB"])
    if path.exists():
        path.unlink()
    con = common.db()
    con.execute("INSERT INTO state VALUES('account_split',?)", (json.dumps({
        "ts": "2026-10-18T00:00:00+00:00", "opening": OPENING, "targets": {},
    }),))
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)",
                ("2026-10-19T12:00:00+00:00", "USDGBP", 0.8, "test", None, None))
    con.execute("INSERT INTO prices(ts,asset,klass,price,source) VALUES(?,?,?,?,?)",
                ("2026-10-19T12:00:00+00:00", "BTC", "crypto", 100.0, "test"))
    con.commit()
    return con

def trend_position(con, crypto_cash=30, stocks_cash=100):
    """Crypto: cash + a position that cost £70 and is now worth £80. Stocks: cash only."""
    con.execute("INSERT INTO acct_cash VALUES('trend','crypto',?)", (crypto_cash,))
    con.execute("INSERT INTO acct_cash VALUES('trend','stocks',?)", (stocks_cash,))
    con.execute("INSERT INTO positions VALUES('trend','BTC','crypto',1,70,70,'2026-10-19T12:00:00+00:00',1)")
    con.execute("""INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason,price_usd,usd_gbp,gas)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("2026-10-19T12:00:00+00:00", "trend", "BTC", "crypto", "BUY", 1, 70, 70, 70, 0.4, None,
                 "target weight", 87.5, 0.8, 0))
    con.execute("""INSERT INTO equity(ts,equity,cash,pos_value,btc,open_n,usd_gbp,core,day,core_cash,day_cash,risk,risk_cash,crypto,stocks)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("2026-10-19T12:00:00+00:00", 210, 130, 80, 100, 1, 0.8, 0, 0, 0, 0, 0, 0, 110, 100))

def reconcile(payload, label):
    check_data.fails.clear()
    check_data.n[0] = 0
    bad = check_data.main(payload)
    check(not bad, label + (" ; ".join(bad) if bad else " reconciles"))

# A book with no trend sleeve keeps the copied pot baselines.
con = fresh()
con.execute("INSERT INTO acct_cash VALUES('core','crypto',50)")
con.execute("INSERT INTO acct_cash VALUES('core','stocks',50)")
con.commit()
plain = dashboard.build(con)
check("trend" not in plain["sleeves"], "a book with no trend sleeve does not invent one")
check(abs(plain["stats"]["start"] - 200) < 0.02, "that book still starts at the copied £200")
check(abs(plain["sleeves"]["core"]["start"] - 100) < 0.02, "core keeps its own start")
con.close()

# The live failure: a trend buy used to raise KeyError and the JSON was never written.
con = fresh()
con.execute("INSERT INTO state VALUES('strategy_book',?)", (json.dumps({"funded": True}),))
trend_position(con)
con.commit()
book = dashboard.build(con)
check("trend" in book["sleeves"], "a strategy book publishes a trend sleeve")
check(abs(book["stats"]["equity"] - 210) < 0.02, "headline value is trend cash plus the position (£210)")
check(abs(book["stats"]["start"] - 200) < 0.02, "headline start stays £200, not the copied pots plus trend")
check(abs(book["stats"]["pnl"] - 10) < 0.02, "P&L is the £10 open gain")
check(abs(book["sleeves"]["core"]["start"]) < 0.02 and abs(book["sleeves"]["core"]["equity"]) < 0.02,
      "blanked core is not still owed the copied £100")
check(abs(book["sleeves"]["trend"]["equity"] - 210) < 0.02, "the trend sleeve holds the whole book")
check(abs(book["accounts"]["crypto"]["equity"] - 110) < 0.02, "the crypto account includes the trend position")
reconcile(book, "strategy book")
con.close()

# best_of_plus keeps 15% in day and risk. The trend trade must not drop the file.
con = fresh()
con.execute("INSERT INTO state VALUES('strategy_alloc',?)", (json.dumps({"trend": 85.0, "day_risk": 7.5}),))
for sl, amt in (("day", 7.5), ("risk", 7.5)):
    con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, "crypto", amt))
    con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, "stocks", amt))
# £85 start, £70 cost, £15 cash left, position now £80.
trend_position(con, crypto_cash=15, stocks_cash=85)
con.commit()
plus = dashboard.build(con)
check(abs(plus["sleeves"]["trend"]["start"] - 170) < 0.02, "best_of_plus trend start is 85 percent")
check(abs(plus["sleeves"]["day"]["start"] - 15) < 0.02 and abs(plus["sleeves"]["risk"]["start"] - 15) < 0.02,
      "best_of_plus day and risk keep their 15%")
check(abs(plus["stats"]["start"] - 200) < 0.02, "best_of_plus still starts at £200")
reconcile(plus, "best_of_plus")
con.close()

print("strategy-book-json " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
