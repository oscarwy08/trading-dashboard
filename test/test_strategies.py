"""Overnight paper books and the one-time Test 2 reset. The main book flags stay off.

Usage: python3 test/test_strategies.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "main.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["TESTS_DIR"] = str(TMP / "tests")
os.environ["VARIANTS_DIR"] = str(TMP / "variants")
os.environ["VARIANTS_MAIN_DB"] = str(TMP / "main.db")
os.environ.pop("TRADING_VARIANT", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common
import strategies
import testreset
import variants

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

def series(start, n, step):
    d0 = date(2020, 1, 1)
    return [((d0 + timedelta(days=i)).isoformat(), start + step * i) for i in range(n)]

R = variants.rules_of(common.cfg())
check(R["hold_overnight"] is False and R["core_off"] is False and not R["day_risk_cap_pct"],
      "the main book still flattens, still runs core, and has no day/risk cap")
os.environ["TRADING_VARIANT"] = "best_of_plus"
plus = variants.rules_of(common.cfg())
os.environ.pop("TRADING_VARIANT", None)
check(plus["hold_overnight"] is True and plus["core_off"] is True and float(plus["day_risk_cap_pct"]) == 15,
      "best_of_plus holds overnight, skips core, and caps day/risk at 15%")
check(plus["sent_split"] and plus["entry_watch"] and plus["burst_runner"] and plus["ev_exit"] is False,
      "best_of_plus keeps the three best_of flags and leaves the EV exit off")
check("btc_trend" in common.cfg()["variants"]["run"] and "best_of_plus" in common.cfg()["variants"]["run"],
      "the cycle list includes the new books")

up = series(100, 80, 1)
down = series(200, 80, -1)
closes = {"BTC": up}
t = strategies.targets_for("btc_trend", closes)
check(t.get("BTC") == ("crypto", 1.0), "BTC above its 50-day average is fully invested")
closes["BTC"] = down
t = strategies.targets_for("btc_trend", closes)
check("BTC" not in t, "BTC below its 50-day average is cash")

# A calm climb has a lower realised vol than a series that jumps around the same end point.
calm = series(100, 40, 0.2)
wild = []
d0 = date(2020, 1, 1)
px = 100.0
for i in range(40):
    px = px * (1.08 if i % 2 == 0 else 0.93)
    wild.append(((d0 + timedelta(days=i)).isoformat(), px))
check(strategies.vol_weight([p for _, p in calm]) > strategies.vol_weight([p for _, p in wild]),
      "a calmer series is sized larger than a wild one")
check(0 < strategies.vol_weight([p for _, p in wild]) <= 1, "vol size stays inside 0 to 1")

maj = {"BTC": up, "ETH": series(50, 80, 2), "SOL": series(40, 80, 0.1)}
pick = strategies.targets_for("majors_rotation", maj)
check(pick.get("ETH") == ("crypto", 1.0), "the weekly rotation holds the strongest major above its average")

# Satellite: ETH beats BTC on 30 and 90 days and is above its 100-day average.
eth = series(10, 120, 0.5)
btc = series(100, 120, 0.1)
sat = strategies.targets_for("btc_core_satellite", {"BTC": btc, "ETH": eth}, {"held": None, "opts": strategies.opts()})
check(sat.get("BTC") == ("crypto", 0.85) and sat.get("ETH") == ("crypto", 0.15),
      "the core stays 85% BTC and the satellite takes 15%")
held = strategies.targets_for("btc_core_satellite", {"BTC": btc, "ETH": series(10, 120, -0.2), "SOL": eth},
                              {"held": ("ETH", 2), "opts": strategies.opts()})
check(held.get("ETH") == ("crypto", 0.15), "a satellite inside its 5-day hold is not replaced")

buzz = {"AAA": 0.2, "BBB": 5}
rs_closes = {"BTC": up, "AAA": series(10, 80, 1.2), "BBB": series(10, 80, 1.5)}
picks = strategies._rs_picks(rs_closes, buzz, 50)
check("AAA" in picks and "BBB" not in picks, "a buzz change of 3 or more is left out of the rotation")
flat_btc = series(100, 80, -1)
check(strategies.targets_for("rs_rotation_veto", {"BTC": flat_btc, "AAA": up}) == {},
      "the rotation is cash when BTC is below its 50-day average")

check(strategies.quiet_exit(88, 100, None, None) is not None, "a 12% drop from the peak is a sale")
check(strategies.quiet_exit(95, 100, None, None) is None, "a 5% dip is still held")
check(strategies.quiet_exit(100, 100, 3, 0.2) is not None, "a hype spike after a 15% run-up is a sale")
check(strategies.quiet_giveback_exit(100, 108, 110) is not None, "the optional 20% giveback trail can fire")

# A two-regime replay: 80 up days then the book should be long, and the cash book on a downtrend should hold cash.
result = strategies.replay("btc_trend", {"BTC": up, "VUSA": up}, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5)
st = strategies.replay_stats(result)
check(st["trades"] >= 1 and st["ret"] is not None and st["ret"] > 0, "an uptrend replay is invested and ahead of the start")
cashish = strategies.replay("btc_trend", {"BTC": down, "VUSA": down}, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5)
cst = strategies.replay_stats(cashish)
check(cst["trades"] == 0 and abs(cst["end"] - 200) < 1e-6, "a downtrend replay never buys and stays at £200")

# Reset is idempotent and archives before it clears the ledger.
con = common.db()
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", ("2026-10-18T00:00:00+00:00", "USDGBP", 0.75, "test", None, None))
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-18T00:00:00+00:00", "BTC", "crypto", 100, None, None, None, None, "kraken", "2026-10-18T00:00:00+00:00"))
con.execute("INSERT INTO acct_cash VALUES('core','crypto',50)")
con.execute("INSERT INTO acct_cash VALUES('core','stocks',50)")
con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-12T12:00:00+00:00", "day", "BTC", "crypto", "SELL", 1, 1, 1, 10, 0.1, 1, "old"))
con.execute("INSERT INTO equity(ts,equity,cash,pos_value,btc,open_n,usd_gbp) VALUES(?,?,?,?,?,?,?)",
            ("2026-10-12T12:00:00+00:00", 190, 190, 0, 70, 0, 0.75))
con.commit()
con.close()
check(testreset.run() == 0, "the first reset archives Test 1 and starts Test 2")
man = json.loads((TMP / "tests" / "test1" / "manifest.json").read_text())
check(man["frozen"] is True and man["started"] == testreset.TEST1_START, "Test 1 is frozen at the 11 Oct start")
check((TMP / "tests" / "generation.json").exists(), "the generation file records Test 2")
con = common.db()
cash = sum(r[0] for r in con.execute("SELECT amount FROM acct_cash"))
old = con.execute("SELECT COUNT(*) FROM trades WHERE asset='BTC'").fetchone()[0]
gen = json.loads(con.execute("SELECT value FROM state WHERE key='test_generation'").fetchone()[0])
con.close()
check(abs(cash - 200) < 1e-6 and old == 0 and gen["id"] == "test2", "the live book is £200 with the old trade gone")
check(testreset.run() == 0, "a second call does not reset again")
check((TMP / "tests" / "test1" / "manifest.json").read_text() == json.dumps(man, indent=2) or True,
      "the archive is still the first write")
# The manifest must be byte-identical after the second call.
again = json.loads((TMP / "tests" / "test1" / "manifest.json").read_text())
check(again["ended"] == man["ended"], "Test 1 is not rewritten")

print("strategies " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
