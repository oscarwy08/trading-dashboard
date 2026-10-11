"""Exit watcher: the same Day trail as the 5-minute cycle, without a second sell.
Usage: python3 test/test_exit_watch.py
"""
import os, sys, tempfile, threading, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TRADING_DB"] = str(Path(tempfile.mkdtemp()) / "watch.db")
os.environ["TRADING_OFFLINE"] = "1"

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

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
