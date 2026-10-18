"""A source switch more than 10% from the last good print is not a price.

DexScreener 1.25 after CoinGecko 1.00 is dropped. The next CoinGecko print near 1.00 is kept.
A same-source move, and a 5% switch, are kept. Two pool quotes do not confirm each other.
Usage: python3 test/test_price_guard.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "px.db")
os.environ["TRADING_LOG"] = str(TMP / "px.log")
os.environ["TRADING_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricesrc

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE prices(ts TEXT, asset TEXT, klass TEXT, price REAL, high24 REAL, low24 REAL, vol24 REAL, mcap REAL, source TEXT, source_ts TEXT)")
con.execute("CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT)")
t0 = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)

def add(ts, px, src):
    con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts.isoformat(), "BP", "crypto", px, None, None, None, None, src, ts.isoformat()))

add(t0, 1.00, "coingecko")
t1 = t0 + timedelta(minutes=5)
check(not pricesrc.accept_print(con, "BP", 1.25, "dexscreener", now_iso=t1.isoformat()), "dex 1.25 after coingecko 1.00 is dropped")
check(pricesrc.accept_print(con, "BP", 1.02, "coingecko", now_iso=(t1 + timedelta(seconds=20)).isoformat()), "the next coingecko print near 1.00 is kept")
add(t1 + timedelta(seconds=20), 1.02, "coingecko")
check(pricesrc.accept_print(con, "BP", 1.20, "coingecko", now_iso=(t1 + timedelta(minutes=5)).isoformat()), "a same-source 18% move is kept")
add(t1 + timedelta(minutes=5), 1.20, "coingecko")
check(pricesrc.accept_print(con, "NEW", 2.0, "kraken", now_iso=t0.isoformat()), "no history accepts the print")
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (t0.isoformat(), "SOL", "crypto", 100, None, None, None, None, "kraken", t0.isoformat()))
check(pricesrc.accept_print(con, "SOL", 105, "coinbase", now_iso=t1.isoformat()), "a 5% source switch is kept")
jump = t1 + timedelta(minutes=1)
check(not pricesrc.accept_print(con, "SOL", 130, "dexscreener", also=[{"usd": 131, "source": "geckoterminal"}], now_iso=jump.isoformat()),
      "two pool quotes do not overrule Kraken")
check(not pricesrc.accept_print(con, "SOL", 130, "dexscreener", now_iso=(jump + timedelta(seconds=30)).isoformat()), "the same jump inside a minute stays dropped")
check(pricesrc.accept_print(con, "SOL", 131, "dexscreener", now_iso=(jump + timedelta(seconds=90)).isoformat()), "the next print a minute later confirms the new price")

print("price guard " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
