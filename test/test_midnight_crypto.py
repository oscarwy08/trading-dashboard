"""Midnight crypto: Day crypto flattens at 00:00 UK and can be entered again on that cycle.

Stocks keep the frozen 08:00 / 21:00 / 22:00 clocks and still flatten 15 minutes before
the market close. The main book is unchanged. EV uses the hours until the flatten the
book will actually apply.
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

import common, daypot, markets, trader, variants
import dashboard
from common import LON

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

FLOOR, FULL = 0.40, 6.0

def wt(hours):
    return daypot.flatten_p(hours, FLOOR, FULL)

# --- the session itself, before a book is built ---
check(variants.crypto_hours_to_flat(22 * 60, 22 * 60, False) == 0.0, "main crypto has no hours left at 22:00 UK")
check(variants.crypto_hours_to_flat(22 * 60, 22 * 60, True) == 2.0, "midnight crypto has 2 hours left at 22:00 UK")
check(variants.crypto_hours_to_flat(23 * 60, 22 * 60, True) == 1.0, "midnight crypto has 1 hour left at 23:00 UK")
check(variants.crypto_hours_to_flat(23 * 60 + 45, 22 * 60, True) == 0.25, "midnight crypto has 15 minutes left at 23:45 UK")
check(variants.crypto_hours_to_flat(0, 22 * 60, True) == 24.0, "a new entry at 00:00 UK has 24 hours until the next midnight")
h_main, h_mid = 0.0, 2.0
w_main, w_mid = wt(h_main), wt(h_mid)
check(w_main == 1.0 and w_mid < w_main, "a later flatten lowers the clock-exit weight")
check(daypot.day_ev(0.5, 4, 2, 0.2, w_mid) > daypot.day_ev(0.5, 4, 2, 0.2, w_main),
      "the same setup has a higher EV when the flatten is midnight")
check(variants.crypto_entry_open(0, 0, 23 * 60 + 45), "00:00 UK is inside the crypto entry window")
check(variants.crypto_entry_open(23 * 60 + 44, 0, 23 * 60 + 45), "23:44 UK is still inside the window")
check(not variants.crypto_entry_open(23 * 60 + 45, 0, 23 * 60 + 45), "23:45 UK is the cutoff")
check(not variants.crypto_entry_open(23 * 60 + 50, 0, 23 * 60 + 45), "23:50 UK is outside the window")
check(not variants.crypto_flatten_due("2026-10-14", "2026-10-14", True, True),
      "crypto opened today is not flattened at 22:00 on the midnight book")
check(variants.crypto_flatten_due("2026-10-14", "2026-10-15", False, True),
      "crypto opened yesterday is flattened on the first cycle of the new UK day")
check(variants.crypto_flatten_due("2026-10-14", "2026-10-14", True, False),
      "the main book still flattens at 22:00")

# Day page clocks. No session stored -> the frozen clocks. A traded midnight book stores its own.
DY = dict(entry_start_uk="08:00", entry_cutoff_uk="21:00", flatten_uk="22:00")
check(dashboard.day_rule_clocks(DY, {})[:3] == ("08:00", "21:00", "22:00"), "the main Day page keeps 08:00-21:00 and 22:00")
sess = {"session": dict(midnight_crypto=True, start="00:00", cutoff="23:45", flatten="00:00")}
check(dashboard.day_rule_clocks(DY, sess) == ("00:00", "23:45", "00:00", True),
      "a midnight book shows 00:00-23:45 and a 00:00 flatten")
opened = datetime(2026, 10, 14, 12, tzinfo=LON).timestamp()
at_22 = datetime(2026, 10, 14, 22, tzinfo=LON)
rows = [{"sleeve": "day", "asset": "NIGHT", "klass": "crypto", "opened": opened},
        {"sleeve": "day", "asset": "AAPL", "klass": "stock", "opened": opened}]
check(dashboard.day_overdue_assets(rows, at_22, "08:00", "22:00", False) == ["NIGHT", "AAPL"],
      "on the main book both names are overdue at 22:00 UK")
check(dashboard.day_overdue_assets(rows, at_22, "08:00", "22:00", True) == ["AAPL"],
      "on the midnight book only the stock is overdue at 22:00 UK")

def fresh():
    p = Path(os.environ["TRADING_DB"])
    if p.exists(): p.unlink()
    for ext in ("-wal", "-shm"):
        q = Path(str(p) + ext)
        if q.exists(): q.unlink()
    common._NOW = None
    os.environ.pop("TRADING_VARIANT", None)
    return common.db()

def fund(con, ts):
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
    for sl in common.SLEEVES:
        for ac in common.ACCOUNTS:
            con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
    con.commit()

def add_px(con, ts, asset, px, klass):
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, asset, klass, px, None, None, None, 1e11, "test", ts))

def history(con, t, asset, klass, px=100.0):
    tick = t - timedelta(hours=5)
    while tick <= t:
        add_px(con, tick.isoformat(), asset, px, klass)
        tick += timedelta(minutes=15)

def cycle(t, variant, positions, names, spy=False):
    """positions: list of (asset, klass, opened). names: assets that only need a price, for entries."""
    con = fresh()
    ts = t.isoformat()
    fund(con, ts)
    meta = {}
    for asset, klass, opened in positions:
        history(con, t, asset, klass)
        add_px(con, ts, asset, 100.0, klass)
        con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                    ("day", asset, klass, 1, 100.0, 100.0, opened, 1))
        meta[asset] = dict(sub="momentum", tp=12, sl=8, atr=1, peak=100.0, opened=opened)
    for asset, klass in names:
        history(con, t, asset, klass)
        add_px(con, ts, asset, 100.0, klass)
    if meta:
        con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps(meta),))
    con.commit(); con.close()
    common._NOW = t
    weights = []
    real_ev = daypot.day_ev
    def wrapped(p, tp, sl, rt, w):
        weights.append(round(float(w), 6))
        return real_ev(p, tp, sl, rt, w)
    if spy:
        daypot.day_ev = wrapped
    if variant:
        os.environ["TRADING_VARIANT"] = variant
    try:
        trader.main()
    finally:
        daypot.day_ev = real_ev
        os.environ.pop("TRADING_VARIANT", None)
    con = common.db()
    return con, weights

def reasons(con, asset):
    return [r[0] for r in con.execute(
        "SELECT reason FROM trades WHERE asset=? AND side='SELL' ORDER BY id", (asset,)).fetchall()]

def signals(con, asset):
    return [(r[0], r[1]) for r in con.execute(
        "SELECT action, reason FROM signals WHERE asset=? ORDER BY rowid", (asset,)).fetchall()]

_score = trader.score_asset
def forced(con, asset, st, t):
    *_, pts = _score(con, asset, st, t)
    return 2.0, 0.2, 2.0, "forced", 55.0, pts

# 22:00 UK Wednesday. Crypto opened today: main flattens, midnight holds. The stock is
# already after the US close, so both books flatten it for the market close.
# The score stays a buy so a flat tape does not sell the crypto on the signal.
t22 = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)   # 22:00 UK
opened_today = datetime(2026, 10, 14, 12, 0, tzinfo=LON).astimezone(timezone.utc).isoformat()
held = [("NIGHT", "crypto", opened_today), ("AAPL", "stock", opened_today)]
trader.score_asset = forced
try:
    con, _ = cycle(t22, None, held, [])
    night_main, stock_main = reasons(con, "NIGHT"), reasons(con, "AAPL")
    con.close()
    con, _ = cycle(t22, "midnight_crypto", held, [])
    night_mid = con.execute("SELECT COUNT(*) FROM positions WHERE asset='NIGHT' AND sleeve='day'").fetchone()[0]
    stock_mid = reasons(con, "AAPL")
    con.close()
finally:
    trader.score_asset = _score
check(night_main and "flatten 22:00 UK" in night_main[0], "main flattens Day crypto at 22:00 UK")
check(night_mid == 1, "midnight crypto is still open at 22:00 UK")
check(stock_main and stock_mid and stock_main[0] == stock_mid[0] and "close" in stock_main[0],
      "both books flatten the stock for the market close, with the same reason")

# EV at 22:00 UK: crypto weight is 1 on main and the 2-hour weight on midnight. A stock's weight matches.
trader.score_asset = forced
try:
    con, w_main_c = cycle(t22, None, [], [("NIGHT", "crypto")], spy=True)
    con.close()
    con, w_mid_c = cycle(t22, "midnight_crypto", [], [("NIGHT", "crypto")], spy=True)
    con.close()
    con, w_main_s = cycle(t22, None, [], [("AAPL", "stock")], spy=True)
    con.close()
    con, w_mid_s = cycle(t22, "midnight_crypto", [], [("AAPL", "stock")], spy=True)
    con.close()
finally:
    trader.score_asset = _score
check(w_main_c and set(w_main_c) == {round(wt(0.0), 6)}, f"main crypto EV uses a 22:00 flatten (weights {w_main_c})")
check(w_mid_c and set(w_mid_c) == {round(wt(2.0), 6)} and wt(2.0) < 1.0,
      f"midnight crypto EV uses 2 hours until 00:00 (weights {w_mid_c})")
check(w_main_s and w_main_s == w_mid_s, f"a stock's flatten weight is the same on both books ({w_main_s})")

# While the US market is open, the stock weight is the time until 15 minutes before the close, on both books.
t18 = datetime(2026, 10, 14, 17, 0, tzinfo=timezone.utc)   # 18:00 UK
mleft = markets.mins_to_close("AAPL", t18)
stock_h = 0.0 if mleft is None else max(0.0, (mleft - 15) / 60.0)
trader.score_asset = forced
try:
    con, s_main = cycle(t18, None, [], [("AAPL", "stock")], spy=True)
    con.close()
    con, s_mid = cycle(t18, "midnight_crypto", [], [("AAPL", "stock")], spy=True)
    con.close()
finally:
    trader.score_asset = _score
check(mleft is not None and stock_h > 0, f"AAPL is still open at 18:00 UK ({mleft} min to the close)")
check(s_main and s_main == s_mid and set(s_main) == {round(wt(stock_h), 6)},
      f"stock EV uses 15 minutes before the close on both books (weights {s_main}, hours {stock_h})")
check(set(s_main) != {round(wt(variants.crypto_hours_to_flat(18 * 60, 22 * 60, True)), 6)},
      "the stock does not pick up the crypto midnight clock")

# 23:50 UK: crypto stays open, and a new crypto entry is refused. A stock is still on 08:00-21:00.
t2350 = datetime(2026, 10, 14, 22, 50, tzinfo=timezone.utc)   # 23:50 UK
trader.score_asset = forced
try:
    con, _ = cycle(t2350, "midnight_crypto", [("NIGHT", "crypto", opened_today)], [("OPEN", "crypto"), ("AAPL", "stock")])
finally:
    trader.score_asset = _score
still = con.execute("SELECT COUNT(*) FROM positions WHERE asset='NIGHT' AND sleeve='day'").fetchone()[0]
open_sig = " | ".join(a + " " + r for a, r in signals(con, "OPEN"))
aapl_sig = " | ".join(a + " " + r for a, r in signals(con, "AAPL"))
bought = con.execute("SELECT COUNT(*) FROM trades WHERE asset='OPEN' AND side='BUY'").fetchone()[0]
con.close()
check(still == 1, "crypto opened today is still open at 23:50 UK")
check(bought == 0 and "outside entry window 00:00-23:45 UK" in open_sig,
      f"no new crypto entry at 23:50 UK ({open_sig})")
check("outside entry window 08:00-21:00 UK" in aapl_sig, f"a stock at 23:50 still uses 08:00-21:00 ({aapl_sig})")

# 00:05 UK the next day: yesterday's crypto flattens, then a new crypto entry is allowed.
# Yesterday's loss halt does not carry over. The stock stays on the morning window.
t0005 = datetime(2026, 10, 14, 23, 5, tzinfo=timezone.utc)   # 00:05 UK on 15 Oct
yday = "2026-10-14"
trader.score_asset = forced
try:
    con = fresh()
    ts = t0005.isoformat()
    fund(con, ts)
    opened_yday = datetime(2026, 10, 14, 12, 0, tzinfo=LON).astimezone(timezone.utc).isoformat()
    for asset, klass in (("NIGHT", "crypto"), ("OPEN", "crypto"), ("AAPL", "stock")):
        history(con, t0005, asset, klass)
    add_px(con, ts, "NIGHT", 100.0, "crypto")
    add_px(con, ts, "OPEN", 100.0, "crypto")
    add_px(con, ts, "AAPL", 100.0, "stock")
    con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                ("day", "NIGHT", "crypto", 1, 100.0, 100.0, opened_yday, 1))
    con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps({"NIGHT": dict(
        sub="momentum", tp=12, sl=8, atr=1, peak=100.0, opened=opened_yday)}),))
    con.execute("INSERT INTO state VALUES(?,?)", (f"day_halt:{yday}", json.dumps(ts)))
    con.commit(); con.close()
    common._NOW = t0005
    os.environ["TRADING_VARIANT"] = "midnight_crypto"
    trader.main()
finally:
    trader.score_asset = _score
    os.environ.pop("TRADING_VARIANT", None)
con = common.db()
sold = reasons(con, "NIGHT")
rebought = con.execute("SELECT COUNT(*) FROM trades WHERE asset='NIGHT' AND side='BUY'").fetchone()[0]
open_sig = " | ".join(a + " " + r for a, r in signals(con, "OPEN"))
aapl_sig = " | ".join(a + " " + r for a, r in signals(con, "AAPL"))
today = "2026-10-15"
halt_today = con.execute("SELECT COUNT(*) FROM state WHERE key=?", (f"day_halt:{today}",)).fetchone()[0]
start_today = con.execute("SELECT COUNT(*) FROM state WHERE key=?", (f"day_start:day:{today}",)).fetchone()[0]
halt_yday = con.execute("SELECT COUNT(*) FROM state WHERE key=?", (f"day_halt:{yday}",)).fetchone()[0]
sleeve = json.loads(con.execute("SELECT value FROM state WHERE key='risk'").fetchone()[0])["day_sleeve"]
con.close()
check(sold and "flatten 00:00 UK" in sold[0], f"yesterday's crypto is flattened at 00:00 UK ({sold[:1]})")
check(rebought == 1, "that crypto can be bought again on the same cycle, after the flatten")
check("outside entry window 00:00-23:45 UK" not in open_sig and "daily loss cap" not in open_sig,
      f"a new crypto entry is allowed at 00:05 UK ({open_sig})")
check("outside entry window 08:00-21:00 UK" in aapl_sig, f"a stock is not enterable at 00:05 UK ({aapl_sig})")
check(halt_today == 0 and halt_yday == 1 and start_today == 1,
      "the daily loss budget resets with the UK date and yesterday's halt does not carry over")
check(sleeve.get("entries_open") is True and sleeve.get("flat_now") is False
      and sleeve.get("session", {}).get("flatten") == "00:00",
      "the book reports crypto entries open and a midnight flatten")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
