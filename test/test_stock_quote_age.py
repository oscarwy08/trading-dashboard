"""Stock quote age counts only regular-session minutes.

A London or US share is not 'unpriced' while its exchange is closed: the
dashboard says the last close, and a stop, trail, or flatten still waits.
Crypto keeps the wall-clock window. Offline the trader's window is 45 min;
the label uses the live 180.
"""
import os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import markets
from exits import dead_quote, held_quote_view

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

LON = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")

def uk(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=LON)

def mins(sym, a, b):
    return markets.open_minutes(sym, a, b)

# Friday 16:00 UK to Monday 08:30 UK: 30 min left on Friday plus 30 min Monday. The weekend is zero.
fri = uk(2026, 10, 9, 16, 0)
mon_open = uk(2026, 10, 12, 8, 30)
check(abs(mins("ONT.L", fri, mon_open) - 60) < 1e-6, f"weekend gap is not quote age ({mins('ONT.L', fri, mon_open):.1f})")

# After the Monday close, a 16:20 print has 10 min of market time and hours of wall time.
close_px = uk(2026, 10, 12, 16, 20)
evening = uk(2026, 10, 12, 20, 0)
check(not markets.is_open("ONT.L", evening), "LSE is closed at 20:00 UK")
check(abs(mins("FTC.L", close_px, evening) - 10) < 1e-6, "after the close only the last 10 min of the session count")
view = held_quote_view("stock", "ONT.L", close_px, evening, 180)
check(view["market_closed"] and not view["unpriced"], f"closed LSE is the last close, not unpriced ({view})")
check(dead_quote(view["price_age_min"], 180, klass="stock", asset="ONT.L", quote_ts=close_px, now=evening),
      "while closed, a quote past 180 wall-clock minutes still must not stop or flatten")

# Next morning the same print is 10 min from Monday plus 10 min of Tuesday.
tue = uk(2026, 10, 13, 8, 10)
check(markets.is_open("ONT.L", tue), "LSE is open at 08:10 UK")
check(abs(mins("ONT.L", close_px, tue) - 20) < 1e-6, f"next open adds only the new session ({mins('ONT.L', close_px, tue):.1f})")
check(not dead_quote(9999, 180, klass="stock", asset="ONT.L", quote_ts=close_px, now=tue),
      "20 min of market time is inside 180 even though the wall clock is not")
open_view = held_quote_view("stock", "FTC.L", close_px, tue, 180)
check(not open_view["unpriced"] and not open_view["market_closed"], f"the next open is a normal price ({open_view})")

# Still in the session, 200 min of trading is unpriced. 13:30 to 16:50 UK is inside 08:00-16:30.
aged = uk(2026, 10, 12, 12, 0)
later = uk(2026, 10, 12, 15, 30)
check(abs(mins("ONT.L", aged, later) - 210) < 1e-6, "a session with no gap counts every minute")
stale = held_quote_view("stock", "ONT.L", aged, later, 180)
check(stale["unpriced"] and not stale["market_closed"], f"210 open minutes is unpriced ({stale})")

# US 14:30-21:00 UK on this October date. 20:00 UK is 60 min before the 21:00 close; 21:30 is closed.
us_px = uk(2026, 10, 12, 20, 0)
us_shut = uk(2026, 10, 12, 21, 30)
check(markets.is_open("AMD", us_px) and not markets.is_open("AMD", us_shut), "US session is 14:30-21:00 UK in October")
check(abs(mins("AMD", us_px, us_shut) - 60) < 1e-6, "the US close cuts the age at 21:00 UK")
us_view = held_quote_view("stock", "AMD", us_px, us_shut, 180)
check(us_view["market_closed"] and not us_view["unpriced"], f"a closed US name is the last close ({us_view})")

# Holiday and early close. 24 Dec 2026 is an early LSE close; 25 and 28 Dec are shut; 29 Dec is the next open.
early = markets.session("LSE", datetime(2026, 12, 24).date())
check(early and early[1].astimezone(LON).strftime("%H:%M") == "12:30", "24 Dec LSE closes at 12:30 UK")
check(markets.session("LSE", datetime(2026, 12, 25).date()) is None, "Christmas Day is not a session")
check(markets.session("LSE", datetime(2026, 12, 28).date()) is None, "the LSE Christmas Monday is not a session")
xmas_px = uk(2026, 12, 24, 12, 0)
box_open = uk(2026, 12, 29, 8, 30)
check(abs(mins("ONT.L", xmas_px, box_open) - 60) < 1e-6,
      f"Christmas days add nothing between the early close and the next open ({mins('ONT.L', xmas_px, box_open):.1f})")
ny_hol = markets.session("US", datetime(2026, 11, 26).date())
check(ny_hol is None, "US Thanksgiving is not a session")

# Crypto is unchanged: 220 wall-clock minutes is unpriced, with no market-closed label.
crypto_now = evening
crypto_then = crypto_now - timedelta(minutes=220)
crypto = held_quote_view("crypto", "STONK", crypto_then, crypto_now, 180)
check(crypto["unpriced"] and not crypto["market_closed"], f"crypto still uses the wall clock ({crypto})")
check(dead_quote(220, 180), "crypto dead_quote ignores the session calendar")
check(not dead_quote(100, 180), "a crypto quote inside 180 is usable")

# The cycle, offline. After the LSE close both names stay. At the next open the
# day book flattens and the risk book stops, because 20 market minutes is inside
# the offline 45. A quote that already used 210 session minutes stays unpriced.
TMP = Path(__file__).resolve().parent / "_stock_quote_age"
TMP.mkdir(exist_ok=True)
DB = TMP / "book.db"
if DB.exists():
    DB.unlink()
shadow = TMP / "shadow.db"
if shadow.exists():
    shadow.unlink()
os.environ["TRADING_DB"] = str(DB)
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["EXIT_SHADOW_DB"] = str(shadow)
os.environ["EXIT_WATCH_SHADOW"] = "1"
os.environ.pop("TRADING_VARIANT", None)

import common, trader, exit_watch

def stock_price(con, ts, asset, px):
    iso = ts.isoformat()
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (iso, asset, "stock", px, None, None, None, None, "test", iso))

def stamp_fx(con, ts):
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts.isoformat(), "USDGBP", 1.0, "test", None, None))

opened = uk(2026, 10, 12, 9, 0)
common._NOW = evening
con = common.db()
stamp_fx(con, evening)
for sl in common.SLEEVES:
    for ac in common.ACCOUNTS:
        con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
con.execute("INSERT INTO state VALUES('core_plan',?)", ('{}',))
con.execute("INSERT INTO state VALUES('core_last_rebalance',?)", ('"' + evening.isoformat() + '"',))
con.execute("INSERT INTO state VALUES('order_queue',?)", ('[]',))
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "FTC.L", "stock", 1, 10.0, 10.0, opened.isoformat(), 1))
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("risk", "ONT.L", "stock", 1, 10.0, 10.0, opened.isoformat(), 1))
stock_price(con, close_px, "FTC.L", 10.0)
stock_price(con, close_px, "ONT.L", 8.0)
con.commit(); con.close()

trader.main()
con = common.db()
held = {r[0] for r in con.execute("SELECT asset FROM positions")}
check(held == {"FTC.L", "ONT.L"}, f"after the close nothing is flattened or stopped ({sorted(held)})")
reasons = {r[0]: r[1] for r in con.execute("SELECT asset, reason FROM signals WHERE action='HOLD' ORDER BY rowid")}
check("market closed" in (reasons.get("ONT.L") or ""), f"risk waits for the open ({reasons.get('ONT.L')})")
check("unpriced" in (reasons.get("FTC.L") or ""), f"a day flatten still waits on a wall-clock-dead close ({reasons.get('FTC.L')})")
quiet = exit_watch.shadow_evaluate(con, {})
check(quiet == [], f"the watcher does not sell a closed market on the stored close ({quiet})")
con.close()

common._NOW = tue
con = common.db()
stamp_fx(con, tue)
con.commit()
woke = exit_watch.shadow_evaluate(con, {})
check(any(e.get("asset") == "FTC.L" and "flatten" in (e.get("reason") or "") for e in woke),
      f"at the next open the day flatten uses the stored close ({woke})")
check(not any(e.get("asset") == "ONT.L" for e in woke),
      "a risk stop still waits for a live tick, as it does for any stored quote")
live = exit_watch.shadow_evaluate(con, {"ONT.L": 8.0})
check(any(e.get("asset") == "ONT.L" and "stop-loss" in (e.get("reason") or "") for e in live),
      f"the first live tick at the open stops ({live})")
con.close()
trader.main()
con = common.db()
left = {r[0] for r in con.execute("SELECT asset FROM positions")}
sells = {r[0]: r[1] for r in con.execute("SELECT asset, reason FROM trades WHERE side='SELL'")}
check(left == set(), f"the next open fills both ({sorted(left)})")
check("flatten" in (sells.get("FTC.L") or ""), f"the day book flattens at the open ({sells.get('FTC.L')})")
check("stop-loss" in (sells.get("ONT.L") or ""), f"the risk book stops at the open ({sells.get('ONT.L')})")
con.close()

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
