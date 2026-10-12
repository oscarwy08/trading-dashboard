"""Exit watcher: the same Day trail as the 5-minute cycle, without a second sell.
Usage: python3 test/test_exit_watch.py
"""
import os, subprocess, sys, tempfile, threading, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_watch_tmp = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(_watch_tmp / "watch.db")
os.environ["EXIT_SHADOW_DB"] = str(_watch_tmp / "exit_shadow.db")
os.environ["TRADING_OFFLINE"] = "1"
os.environ["EXIT_WATCH_SHADOW"] = "0"   # these checks sell for real; shadow is the deploy default

import common
from common import db, iso
from exits import book_lock
import exit_watch
from exit_watch import coinbase_tick, evaluate, kraken_symbol, kraken_ticks, to_gbp
from trader import cost_pct

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

# --- pure helpers (no book) ---
check(kraken_symbol(dict(pair="XBTUSD", quote="USD")) == "XBT/USD", "Kraken BTC pair becomes the XBT/USD stream name")
check(kraken_symbol(dict(pair="BTC-USD", quote="USD")) is None, "a Coinbase product id is not a Kraken stream name")
msg = {"channel": "ticker", "type": "update", "data": [{"symbol": "XBT/USD", "last": 100}]}
check(kraken_ticks(msg) == [("XBT/USD", 100.0)], "a Kraken ticker update yields the last price")
check(coinbase_tick({"type": "ticker", "product_id": "ETH-USD", "price": "20.5"}) == ("ETH-USD", 20.5), "a Coinbase ticker yields the price")
check(abs(to_gbp(100, "USD", 0.8) - 80) < 1e-9, "100 dollars at 0.80 is £80")

# --- book: burst trail, peak +6.1% then a drop exits near +3.7% ---
common._NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
FEE = cost_pct(common.cfg(), "BAT", "crypto", "SELL", {}, 10)
check(abs(FEE - 0.003) < 1e-12, "unknown market cap keeps the 0.3% sell cost")

def px_for(net):
    """GBP price whose gain, net of the sell cost, is `net` (0.061 == +6.1%)."""
    return (1 + net) / (1 - FEE)

con = db()
opened = "2026-10-11T10:00:00+00:00"
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (iso(), "USDGBP", 0.8, "test", None, None))
con.execute("INSERT INTO acct_cash VALUES(?,?,?)", ("day", "crypto", 40.0))
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "BAT", "crypto", 10, 1.0, 10.0, opened, 1))
con.execute("INSERT INTO state VALUES(?,?)", ("day_meta",
    '{"BAT":{"sub":"burst","tp":8,"sl":2.5,"peak":1.0,"opened":"%s"}}' % opened))
con.commit()

def step(net, mono):
    return evaluate(con, {"BAT": px_for(net)}, {"BAT": mono})

# Walk up to +6.1%. The target is +8% and the trail gives back 40% of the peak, so this is not a sell.
mono = time.monotonic()
for net in (0.01, 0.03, 0.05, 0.061):
    got = step(net, mono)
    check(got == [], f"no sell while the burst is at {net * 100:.1f}%")

# Still above the trail (6.1% * 0.6 = 3.66%).
check(step(0.05, mono) == [], "a fade from +6.1% to +5% does not sell yet")

sold = None
net = 0.049
while net >= 0.02:
    got = step(net, mono)
    if got:
        sold = (net, got[0]); break
    net = round(net - 0.0001, 10)

check(sold is not None, "the drop after +6.1% does sell")
if sold:
    net_hit, ex = sold
    # 6.1 * 0.60 = 3.66, which the reason prints as +3.7%. A later price would be well below that.
    check(abs(net_hit * 100 - 3.66) < 0.05, f"exit net is {net_hit * 100:.2f}%, near +3.7% (not a later worse price)")
    check("peak +6.1%" in ex["reason"], "the reason records the +6.1% peak")
    check(ex["reason"].startswith("[burst] trailing stop"), "it is the trailing stop, not the take-profit")
    check("trigger" in ex["log"] and "latency" in ex["log"], "the log line has the trigger price and the latency")
    n = con.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]
    check(n == 1, "one sell row")
    check(con.execute("SELECT 1 FROM positions WHERE asset='BAT'").fetchone() is None, "the position is closed")
    again = step(0.0, mono)
    check(again == [] and con.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0] == 1,
          "a second tick does not sell again")

# A stored price already through the trail, with no live tick, is left for the 5-min cycle.
import json
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "WLD", "crypto", 10, 1.0, 10.0, opened, 1))
dmeta = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0])
dmeta["WLD"] = dict(sub="burst", tp=8, sl=2.5, peak=px_for(0.061), opened=opened)
con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", ("day_meta", json.dumps(dmeta)))
con.execute("INSERT INTO prices(ts,asset,klass,price,source) VALUES(?,?,?,?,?)",
            (iso(), "WLD", "crypto", px_for(0.02) / 0.8, "test"))
con.commit()
quiet = evaluate(con, {}, {})
check(quiet == [] and con.execute("SELECT 1 FROM positions WHERE asset='WLD'").fetchone(),
      "a dropped stream does not fire the trail; the 5-min cycle still can")

# KILL blocks a fresh position that is already through the trail.
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "NEAR", "crypto", 10, 1.0, 10.0, opened, 1))
dmeta = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0])
dmeta["NEAR"] = dict(sub="burst", tp=8, sl=2.5, peak=px_for(0.061), opened=opened)
con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", ("day_meta", json.dumps(dmeta)))
con.commit()
kill = Path(tempfile.mkdtemp()) / "KILL"
old_kill = exit_watch.KILL
exit_watch.KILL = kill
kill.write_text("1")
try:
    blocked = evaluate(con, {"NEAR": px_for(0.02)}, {"NEAR": mono})
    check(blocked == [] and con.execute("SELECT 1 FROM positions WHERE asset='NEAR'").fetchone(),
          "KILL stops the watcher from selling")
finally:
    exit_watch.KILL = old_kill

# The lock is exclusive, so the cycle and the watcher cannot both pass a sell.
held, release = threading.Event(), threading.Event()
def hold():
    with book_lock(timeout=2):
        held.set(); release.wait(3)
th = threading.Thread(target=hold); th.start()
check(held.wait(2), "the book lock can be taken")
t0 = time.time(); busy = False
try:
    with book_lock(timeout=0.3):
        pass
except TimeoutError:
    busy = True
release.set(); th.join(timeout=2)
check(busy and time.time() - t0 < 1.5, "a second seller waits, then gives up rather than double-selling")

# --- shadow mode: same trail, but the book is not written ---
saved_shadow = os.environ.pop("EXIT_SHADOW_DB")
default_shadow = exit_watch.shadow_db_path()
check(default_shadow == Path.home() / "exit_shadow" / "exit_shadow.db",
      "shadow db defaults to ~/exit_shadow/exit_shadow.db")
check(ROOT not in default_shadow.parents, "the default shadow db is outside the repo")
os.environ["EXIT_SHADOW_DB"] = saved_shadow
check(exit_watch.shadow_db_path() == Path(saved_shadow), "EXIT_SHADOW_DB overrides the default")
ignored = subprocess.run(["git", "check-ignore", "data/exit_shadow.db", "data/exit_shadow.db-wal",
                          "data/exit_shadow.db-shm", "data/exit_shadow.db-journal"],
                         cwd=ROOT, capture_output=True, text=True)
check(ignored.stdout.splitlines() == ["data/exit_shadow.db", "data/exit_shadow.db-wal",
                                      "data/exit_shadow.db-shm", "data/exit_shadow.db-journal"],
      "data/exit_shadow.db and its wal, shm, and journal are gitignored")
os.environ.pop("EXIT_WATCH_SHADOW", None)
check(exit_watch.shadow_mode() is True, "shadow is on when EXIT_WATCH_SHADOW is unset")
os.environ["EXIT_WATCH_SHADOW"] = "false"
check(exit_watch.shadow_mode() is False, "false sells for real")
os.environ["EXIT_WATCH_SHADOW"] = "1"

from exit_shadow_report import compare_exits, format_report, load_report

opened_p = "2026-10-11T10:00:00+00:00"
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "PEPE", "crypto", 10, 1.0, 10.0, opened_p, 1))
dmeta = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0])
dmeta["PEPE"] = dict(sub="burst", tp=8, sl=2.5, peak=1.0, opened=opened_p)
con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", ("day_meta", json.dumps(dmeta)))
con.commit()
book_before = con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0]
cash_before = con.execute("SELECT amount FROM acct_cash WHERE sleeve='day' AND account='crypto'").fetchone()[0]
trades_before = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
signals_before = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

def step_pepe(net):
    return evaluate(con, {"PEPE": px_for(net)}, {"PEPE": mono})

for net in (0.01, 0.03, 0.05, 0.061):
    check(step_pepe(net) == [], f"shadow does not note an exit at {net * 100:.1f}%")
check(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0] == book_before,
      "a shadow climb does not write the cycle's day_meta peak")

shadow_hit = None
net = 0.049
while net >= 0.02:
    got = step_pepe(net)
    if got:
        shadow_hit = (net, got[0]); break
    net = round(net - 0.0001, 10)

check(shadow_hit is not None, "shadow notes the drop after +6.1%")
if shadow_hit:
    net_hit, ex = shadow_hit
    check(abs(net_hit * 100 - 3.66) < 0.05, f"shadow exit net is {net_hit * 100:.2f}%, near +3.7%")
    check(ex.get("shadow") is True and ex["log"].startswith("WOULD EXIT"), "the log says WOULD EXIT")
    check("at 2026-10-11T12:00:00+00:00" in ex["log"] and "trigger £" in ex["log"], "the log has the time and the trigger price")
    check("peak +6.1%" in ex["reason"], "the shadow trigger records the +6.1% peak")
    check(con.execute("SELECT 1 FROM positions WHERE asset='PEPE'").fetchone() is not None, "shadow leaves the position open")
    check(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == trades_before, "shadow writes no trade")
    check(con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == signals_before, "shadow writes no signal")
    check(abs(con.execute("SELECT amount FROM acct_cash WHERE sleeve='day' AND account='crypto'").fetchone()[0] - cash_before) < 1e-9,
          "shadow does not move cash")
    check(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0] == book_before,
          "shadow does not replace day_meta")
    again = step_pepe(net_hit)
    n_shadow = 0
    sh = __import__("sqlite3").connect(exit_watch.shadow_db_path())
    n_shadow = sh.execute("SELECT COUNT(*) FROM would_exit WHERE asset='PEPE'").fetchone()[0]
    sh.close()
    check(again == [] and n_shadow == 1, "a second shadow tick does not log the same exit again")

    pure_sh = [dict(ts="2026-10-11T12:00:00+00:00", sleeve="day", asset="X", opened="2026-10-11T10:00:00+00:00",
                    price=1.04, trigger="trailing stop")]
    pure_sells = [dict(ts="2026-10-11T12:00:12+00:00", sleeve="day", asset="X", price=1.03, reason="cycle trail"),
                  dict(ts="2026-10-09T12:00:00+00:00", sleeve="day", asset="OLD", price=1, reason="old"),
                  dict(ts="2026-10-11T12:01:00+00:00", sleeve="risk", asset="STRK", price=2, reason="stop")]
    paired, missed = compare_exits(pure_sh, pure_sells)
    check(len(paired) == 1 and abs(paired[0]["dt_s"] - 12) < 1e-9 and abs(paired[0]["price_diff"] - (1.03 - 1.04)) < 1e-9,
          "the report's time gap is 12s and the price gap is the fill minus the trigger")
    check(len(missed) == 1 and missed[0]["asset"] == "STRK", "a later cycle sell with no shadow note is listed, an older one is not")
    check("12s later" in format_report(paired, missed), "the printed report says how much later the cycle was")

    quiet, _miss = load_report(con, exit_watch.shadow_db_path())
    pepe = [r for r in quiet if r["asset"] == "PEPE"]
    check(len(pepe) == 1 and pepe[0]["status"] == "no cycle sell", "before the cycle sells, the report says no sell yet")
    cycle_px = ex["price"] - 0.01
    con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-10-11T12:05:00+00:00", "day", "PEPE", "crypto", "SELL", 10, cycle_px, cycle_px, 1, 0, 0.1, "cycle trail"))
    con.commit()
    paired, missed = load_report(con, exit_watch.shadow_db_path())
    pepe = [r for r in paired if r["asset"] == "PEPE"][0]
    check(abs(pepe["dt_s"] - 300) < 1e-6 and abs(pepe["price_diff"] - (cycle_px - ex["price"])) < 1e-9,
          f"PEPE's cycle sell is 300s later and £{pepe['price_diff']:+.4f} from the shadow trigger")
    text = format_report(paired, missed)
    check("300s later" in text and "Cycle sells with no shadow exit" in text and "BAT" in text,
          "the report shows the PEPE gap and the live BAT sell the shadow watcher did not see")

os.environ["EXIT_WATCH_SHADOW"] = "0"

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
