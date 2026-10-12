"""Variant books: overlays, rule flags, and isolation from the main database.
The trader cases use a tiny fixture and a frozen clock, not the live book."""
import json, os, shutil, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "main.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["VARIANTS_DIR"] = str(TMP / "variants")
os.environ["VARIANTS_MAIN_DB"] = str(TMP / "main.db")
os.environ.pop("TRADING_VARIANT", None)

import common, trader, variants
from common import LON

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

# --- overlays leave the frozen rules alone and turn one flag on ---
base = common.cfg()
check(all(not base["variant_rules"][k] for k in
          ("ev_exit", "burst_stop_widen", "core_fallback_half", "entry_own_trend", "no_day_core_same_afternoon",
           "burst_runner", "midnight_crypto", "entry_watch")),
      "main config flags are off")
os.environ["TRADING_VARIANT"] = "ev_exit"
over = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(over["variant_rules"]["ev_exit"] is True and over["strategy"]["buy_threshold"] == base["strategy"]["buy_threshold"],
      "ev_exit overlay turns on only its flag")
os.environ["TRADING_VARIANT"] = "all_combined"
both = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(both["variant_rules"]["ev_exit"] and both["variant_rules"]["burst_stop_widen"]
      and both["variant_rules"]["core_fallback_half"] and not both["variant_rules"]["burst_runner"]
      and both["rules"]["frozen_since"] == base["rules"]["frozen_since"],
      "all_combined turns on both ideas, leaves the burst runner off, and does not move the freeze date")
os.environ["TRADING_VARIANT"] = "everything"
every = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(every["variant_rules"]["ev_exit"] and every["variant_rules"]["burst_stop_widen"]
      and every["variant_rules"]["core_fallback_half"] and every["variant_rules"]["entry_own_trend"]
      and every["variant_rules"]["no_day_core_same_afternoon"] and every["variant_rules"]["burst_runner"]
      and every["rules"]["frozen_since"] == base["rules"]["frozen_since"]
      and not both["variant_rules"]["burst_runner"],
      "everything turns on all_combined and the burst runner, and all_combined stays unchanged")
os.environ["TRADING_VARIANT"] = "midnight_crypto"
mid = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(mid["variant_rules"]["midnight_crypto"] is True
      and not base["variant_rules"]["midnight_crypto"]
      and not every["variant_rules"]["midnight_crypto"]
      and not both["variant_rules"]["midnight_crypto"]
      and mid["day"]["entry_start_uk"] == base["day"]["entry_start_uk"] == "08:00"
      and mid["day"]["entry_cutoff_uk"] == base["day"]["entry_cutoff_uk"] == "21:00"
      and mid["day"]["flatten_uk"] == base["day"]["flatten_uk"] == "22:00"
      and mid["day"]["stock_flatten_before_close_min"] == base["day"]["stock_flatten_before_close_min"] == 15
      and mid["rules"]["frozen_since"] == base["rules"]["frozen_since"]
      and variants.rules_of(mid)["midnight_entry_cutoff_uk"] == "23:45"
      and variants.rules_of(mid)["midnight_flatten_uk"] == "00:00"
      and variants.active(variants.rules_of(mid)) and not variants.active(variants.rules_of(base)),
      "midnight_crypto turns on only its flag and leaves the frozen day clocks in place")
os.environ["TRADING_VARIANT"] = "entry_watch"
ew = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(ew["variant_rules"]["entry_watch"] is True
      and not base["variant_rules"]["entry_watch"]
      and not ew["variant_rules"]["midnight_crypto"]
      and not ew["variant_rules"]["ev_exit"]
      and ew["day"]["flatten_uk"] == base["day"]["flatten_uk"] == "22:00"
      and ew["rules"]["frozen_since"] == base["rules"]["frozen_since"]
      and variants.rules_of(ew)["entry_watch_expiry_min"] == 15
      and variants.active(variants.rules_of(ew)) and not variants.active(variants.rules_of(base)),
      "entry_watch turns on only its flag and leaves the frozen day clocks in place")
check(not Path("config/variants/missing.yaml").exists() or True, "placeholder")

R = variants.rules_of(base)
check(variants.ev_action(-0.2, 1.0, R) is None, "EV exit does nothing while the flag is off")
Ron = variants.rules_of(over)
check(variants.ev_action(-0.2, 1.0, Ron) == "tighten", "EV now -0.2% only tightens the stop")
check(variants.ev_action(-1.5, 0.4, Ron) == "exit", "EV now -1.5% and 1.9pp worse than entry is an exit")
check(variants.ev_action(-1.5, -1.0, Ron) == "tighten", "below the line but not 0.8pp worse than entry does not exit")
check("entry EV" in variants.ev_exit_reason(-1.5, 0.4, Ron), "exit reason names both numbers")
check(abs(variants.effective_stop_pct(2.0, 0.5, variants.rules_of(both), "burst") - 3.0) < 1e-9,
      "burst stop widens to 1.5x while EV is positive")
check(abs(variants.effective_stop_pct(2.0, -1.0, variants.rules_of(both), "burst") - 2.0) < 1e-9,
      "the wide burst stop stands down once EV is below -0.4%")
check(variants.fallback_fraction(20, 50, R) == 1.0, "fallback stays full size while the flag is off")
Rf = variants.rules_of({"variant_rules": dict(base["variant_rules"], core_fallback_half=True)})
check(variants.fallback_fraction(20, 50, Rf) == 0.5, "7d +20% halves the 18:00 tranche")
check(variants.fallback_fraction(2, 80, Rf) == 0.5, "RSI 80 halves it too")
check(variants.fallback_fraction(2, 40, Rf) == 1.0, "a quiet coin keeps the full tranche")
now = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)
pts = [(now - timedelta(days=4), 80.0), (now - timedelta(days=3, hours=1), 100.0), (now, 95.0)]
check(variants.trend_up(pts, 3) is False, "a coin still below its price 3 days ago is not an uptrend")
pts[-1] = (now, 110.0)
check(variants.trend_up(pts, 3) is True, "a coin above its price 3 days ago is an uptrend")
check(variants.own_trend_block(pts[:1], Ron) is None, "own-trend block is off on the ev_exit book")
Rb = variants.rules_of(both)
check(variants.own_trend_block([(now, 1.0)], Rb).startswith("own 3d trend"), "not enough history blocks the entry")
check(variants.trend_lookback_h(Rb) >= 3 * 24 + 6, "the trend load is longer than a bare 72h window")
end = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)
last = end - timedelta(minutes=10)
short = []
tick = end - timedelta(hours=72)
while tick <= last:
    short.append((tick, 100.0)); tick += timedelta(minutes=15)
check(variants.trend_up(short, 3) is None, "72h of prices has no tick at or before 3 days before the last one")
longp = []
tick = end - timedelta(hours=variants.trend_lookback_h(Rb))
while tick <= last:
    longp.append((tick, 90.0)); tick += timedelta(minutes=15)
longp[-1] = (last, 110.0)
check(variants.trend_up(longp, 3) is True, "the longer window sees a coin above its price 3 days ago")

# afternoon: a Core buy at 13:00 UK blocks a Day buy; a Core buy yesterday does not
con = common.db()
lt = datetime(2026, 10, 14, 15, 0, tzinfo=LON)
buy_ts = datetime(2026, 10, 14, 13, 0, tzinfo=LON).astimezone(timezone.utc).isoformat()
old_ts = datetime(2026, 10, 13, 13, 0, tzinfo=LON).astimezone(timezone.utc).isoformat()
con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (buy_ts, "core", "BTC", "crypto", "BUY", 1, 1, 1, 1, 0, "test"))
con.commit()
check(variants.bought_this_afternoon(con, "core", "BTC", lt, 12) is True, "a Core buy at 13:00 UK counts as this afternoon")
con.execute("DELETE FROM trades")
con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (old_ts, "core", "BTC", "crypto", "BUY", 1, 1, 1, 1, 0, "test"))
con.commit()
check(variants.bought_this_afternoon(con, "core", "BTC", lt, 12) is False, "yesterday's Core buy does not block today")
con.close()

def fresh():
    p = Path(os.environ["TRADING_DB"])
    if p.exists(): p.unlink()
    for ext in ("-wal", "-shm"):
        q = Path(str(p) + ext)
        if q.exists(): q.unlink()
    common._NOW = None
    return common.db()

def fund(con, ts, rate=1.0):
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", rate, "test", None, None))
    for sl in common.SLEEVES:
        for ac in common.ACCOUNTS:
            con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
    con.execute("INSERT INTO state VALUES('core_plan',?)", (json.dumps({}),))
    con.execute("INSERT INTO state VALUES('core_last_rebalance',?)", (json.dumps(ts),))
    con.execute("INSERT INTO state VALUES('order_queue',?)", (json.dumps([]),))
    con.commit()

def price(con, ts, asset, px, klass="crypto"):
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, asset, klass, px, None, None, None, 1e11, "test", ts))

# --- EV exit sells a burst that has not hit its stop; the flag off does not ---
def ev_book():
    con = fresh()
    t = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)   # 15:00 UK
    ts = t.isoformat()
    fund(con, ts)
    opened = (t - timedelta(minutes=30)).isoformat()
    price(con, ts, "TEST", 99.0)
    snap = dict(p=0.2, target=3, loss=8, ev=1.2, entry=100)
    con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                ("day", "TEST", "crypto", 1, 100, 100, opened, 1))
    con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps({"TEST": dict(
        sub="burst", tp=3, sl=8, atr=1, peak=100, opened=opened, snap=snap)}),))
    con.commit(); con.close()
    common._NOW = t

ev_book()
trader.main()
con = common.db()
held = con.execute("SELECT COUNT(*) FROM positions WHERE asset='TEST'").fetchone()[0]
sold = con.execute("SELECT reason FROM trades WHERE asset='TEST' AND side='SELL'").fetchone()
check(held == 1 and sold is None, "flag off: a -1% burst inside an 8% stop is still open")
con.close()

ev_book()
os.environ["TRADING_VARIANT"] = "ev_exit"
trader.main()
os.environ.pop("TRADING_VARIANT", None)
con = common.db()
sold = con.execute("SELECT reason FROM trades WHERE asset='TEST' AND side='SELL'").fetchone()
check(sold is not None and "EV exit" in sold[0], "ev_exit: the same position is sold because EV now is clearly negative")
con.close()

# --- widened burst stop holds a -2.5% dip that the stored 2% stop would sell ---
def burst_book(widen):
    con = fresh()
    t = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)
    ts = t.isoformat()
    fund(con, ts)
    opened = (t - timedelta(minutes=20)).isoformat()
    price(con, ts, "TEST", 97.5)
    snap = dict(p=0.8, target=3, loss=2, ev=1.0, entry=100)
    con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                ("day", "TEST", "crypto", 1, 100, 100, opened, 1))
    con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps({"TEST": dict(
        sub="burst", tp=3, sl=2, atr=1, peak=100, opened=opened, snap=snap)}),))
    con.commit(); con.close()
    common._NOW = t
    if widen:
        os.environ["TRADING_VARIANT"] = "review_leftovers"
    trader.main()
    os.environ.pop("TRADING_VARIANT", None)
    con = common.db()
    n = con.execute("SELECT COUNT(*) FROM trades WHERE asset='TEST' AND side='SELL'").fetchone()[0]
    con.close()
    return n

check(burst_book(False) == 1, "stored 2% burst stop sells a -2.5% dip")
check(burst_book(True) == 0, "review book keeps that dip because the stop is 1.5x and EV is still positive")

# --- half-size 18:00 fallback when the coin is extended ---
def core_book(half):
    con = fresh()
    t = datetime(2026, 10, 14, 17, 30, tzinfo=timezone.utc)   # 18:30 UK
    ts = t.isoformat()
    fund(con, ts)
    start = t - timedelta(days=8)
    for i in range(8 * 24):
        tick = start + timedelta(hours=i)
        px = 80.0 if tick < t - timedelta(hours=30) else 100.0
        price(con, tick.isoformat(), "BTC", px)
    price(con, ts, "BTC", 101.0)
    con.execute("INSERT OR REPLACE INTO state VALUES('core_plan',?)", (json.dumps(
        {"BTC": dict(status="building", n=0, last_day=None, day_n=0)}),))
    con.commit(); con.close()
    common._NOW = t
    if half:
        os.environ["TRADING_VARIANT"] = "review_leftovers"
    trader.main()
    os.environ.pop("TRADING_VARIANT", None)
    con = common.db()
    row = con.execute("SELECT value, reason FROM trades WHERE asset='BTC' AND side='BUY' AND sleeve='core'").fetchone()
    con.close()
    return row

full, halved = core_book(False), core_book(True)
check(full is not None and halved is not None and abs(halved[0] - full[0] / 2) < 0.05,
      f"18:00 fallback on an extended coin is half size (£{None if not full else round(full[0], 2)} -> £{None if not halved else round(halved[0], 2)})")
check(halved is not None and "half size" in (halved[1] or ""), "the half-size reason is written on the fill")

# --- isolation: running the variant runner does not change the main file ---
ev_book()
main_path = Path(os.environ["TRADING_DB"])
blob = main_path.read_bytes()
variants.run_all()
check(main_path.read_bytes() == blob, "variant runner leaves the main database byte for byte")
# a child that cannot trade still must not be required for the main hash; the books exist beside it
check((TMP / "variants" / "_snapshot.db").exists(), "snapshot is taken at the first run")
check((TMP / "variants" / "ev_exit.db").exists() and (TMP / "variants" / "all_combined.db").exists()
      and (TMP / "variants" / "everything.db").exists() and (TMP / "variants" / "midnight_crypto.db").exists()
      and (TMP / "variants" / "entry_watch.db").exists(),
      "each variant has its own database")
# a book added after the snapshot exists is copied from that snapshot, not from the main book as it is now
(TMP / "variants" / "everything.db").unlink()
late = sqlite3.connect(os.environ["TRADING_DB"])
late.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             ("2026-10-11T19:00:00+00:00", "day", "LATER", "crypto", "SELL", 1, 1, 1, 1, 0, 9, "after the snapshot"))
late.commit(); late.close()
variants.prepare("everything")
evcon = sqlite3.connect(f"file:{TMP / 'variants' / 'everything.db'}?mode=ro", uri=True)
snapcon = sqlite3.connect(f"file:{TMP / 'variants' / '_snapshot.db'}?mode=ro", uri=True)
try:
    check(evcon.execute("SELECT COUNT(*) FROM trades WHERE asset='LATER'").fetchone()[0] == 0,
          "everything does not pick up a trade added to main after the snapshot")
    check(evcon.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
          == snapcon.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
          "everything starts with the snapshot's trades")
    man = json.loads((TMP / "variants" / "manifest.json").read_text())
    rep = variants.report()
    eb = next(b for b in rep["books"] if b["name"] == "everything")
    check(eb["label"] == "Everything combined" and rep["since"] == (man.get("equity_ts") or man.get("created")),
          "everything is labelled and measured from the original snapshot time")
finally:
    evcon.close(); snapcon.close()
(TMP / "variants" / "midnight_crypto.db").unlink()
variants.prepare("midnight_crypto")
mdcon = sqlite3.connect(f"file:{TMP / 'variants' / 'midnight_crypto.db'}?mode=ro", uri=True)
snapcon = sqlite3.connect(f"file:{TMP / 'variants' / '_snapshot.db'}?mode=ro", uri=True)
try:
    check(mdcon.execute("SELECT COUNT(*) FROM trades WHERE asset='LATER'").fetchone()[0] == 0,
          "midnight crypto does not pick up a trade added to main after the snapshot")
    check(mdcon.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
          == snapcon.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
          "midnight crypto starts with the snapshot's trades")
    man = json.loads((TMP / "variants" / "manifest.json").read_text())
    rep = variants.report()
    mb = next(b for b in rep["books"] if b["name"] == "midnight_crypto")
    check(mb["label"] == "Midnight crypto" and rep["since"] == (man.get("equity_ts") or man.get("created")),
          "midnight crypto is labelled and measured from the original snapshot time")
finally:
    mdcon.close(); snapcon.close()

# comparison P&L is the change since the snapshot, not since the original £200
cmp = TMP / "cmp.db"
old_db = common.DB
common.DB = cmp
ccon = common.db()
ccon.execute("INSERT INTO equity(ts,equity,cash,pos_value,btc,open_n) VALUES(?,?,?,?,?,?)",
             ("2026-10-01T12:00:00+00:00", 180, 100, 80, 1, 1))
ccon.execute("INSERT INTO equity(ts,equity,cash,pos_value,btc,open_n) VALUES(?,?,?,?,?,?)",
             ("2026-10-11T12:00:00+00:00", 190, 110, 80, 1, 1))
ccon.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             ("2026-09-01T12:00:00+00:00", "day", "OLD", "crypto", "SELL", 1, 1, 1, 1, 0, -50, "old"))
ccon.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             ("2026-10-11T11:00:00+00:00", "day", "NEW", "crypto", "SELL", 1, 1, 1, 1, 0, 4, "new"))
ccon.commit()
book = variants._book(ccon, "main", "Main book", "", "2026-10-10T00:00:00+00:00", 180)
ccon.close()
common.DB = old_db
check(book["pnl"] == 10 and book["trades"] == 1 and book["win_rate"] == 100.0,
      f"variant table measures from the snapshot (P&L £{book['pnl']}, trades {book['trades']}, win {book['win_rate']})")
check(book["curve"] and book["curve"][0]["eq"] == 190, "the curve starts at the first equity point on or after the snapshot")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
