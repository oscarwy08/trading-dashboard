"""Impossible volume prints must not open an entry.

The 13 Oct 2026 LDO print: vol24 719,748 at 13:39:48Z, then 66,476,205 at 14:33:33Z
(15:33 UK). The old formula returned about 2,449x an average hour and the cycle bought it.
That reading is rejected. A normal spike still passes. The 2x gate is unchanged.

Usage: python3 test/test_volume_sanity.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "vol.db")
os.environ["TRADING_LOG"] = str(TMP / "vol.log")
os.environ["TRADING_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daypot
import entries

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

T = datetime(2026, 10, 13, 14, 33, 46, tzinfo=timezone.utc)
PREV = datetime(2026, 10, 13, 13, 39, 48, tzinfo=timezone.utc)
GLITCH = datetime(2026, 10, 13, 14, 33, 33, tzinfo=timezone.utc)

con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE prices(ts TEXT, asset TEXT, klass TEXT, price REAL, high24 REAL, low24 REAL, vol24 REAL, mcap REAL, source TEXT, source_ts TEXT)")

def add(ts, vol, asset="LDO"):
    con.execute("INSERT INTO prices(ts,asset,klass,price,vol24,source,source_ts) VALUES(?,?,?,?,?,?,?)",
                (ts.isoformat(), asset, "crypto", 0.32, vol, "kraken", ts.isoformat()))

add(PREV, 719748.0650741557)
add(GLITCH, 66476205.0)
ratio = daypot.crypto_vol_ratio(con, "LDO", T)
check(ratio is None, f"LDO glitch ratio is not a number ({ratio})")
check(daypot.crypto_volume_rejected(con, "LDO", T), "LDO glitch is rejected, not missing")
check(daypot.entry_volume_block(ratio, True, 2.0, False, "24h") == "volume reading rejected",
      "a 24h break cannot use the glitch as missing data")
check(daypot.entry_volume_block(ratio, True, 2.0, False, "4h") == "volume reading rejected",
      "a 4h break cannot use the glitch either")

# The next print, 18s later, was a normal vol24. Scored against the pre-glitch print it is a real spike.
con.execute("DELETE FROM prices")
add(PREV, 719748.065)
add(datetime(2026, 10, 13, 14, 33, 51, tzinfo=timezone.utc), 823976.98)
ok_ratio = daypot.crypto_vol_ratio(con, "LDO", datetime(2026, 10, 13, 14, 34, tzinfo=timezone.utc))
check(ok_ratio is not None and 2 < ok_ratio < 10, f"the reverted print is a normal spike ({ok_ratio})")
check(not daypot.crypto_volume_rejected(con, "LDO", datetime(2026, 10, 13, 14, 34, tzinfo=timezone.utc)),
      "the reverted print is not rejected")

# A quiet hour stays under the 2x line. A doubled 24h total in one hour is about 25x and still allowed.
con.execute("DELETE FROM prices")
t2 = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)
add(t2 - timedelta(hours=1), 700000, "QUIET")
add(t2, 700000, "QUIET")
quiet = daypot.crypto_vol_ratio(con, "QUIET", t2)
check(quiet is not None and abs(quiet - 1) < 1e-6, f"flat vol24 is 1x ({quiet})")
check(daypot.entry_volume_block(quiet, False, 2.0, False, "24h") == "volume x1.0 < 2x average",
      "a real low print still fails the 2x line")
add(t2 - timedelta(hours=1), 700000, "HOT")
add(t2, 1400000, "HOT")
hot = daypot.crypto_vol_ratio(con, "HOT", t2)
check(hot is not None and abs(hot - 25) < 0.01, f"a doubled 24h total is 25x and kept ({hot})")
check(not daypot.crypto_volume_rejected(con, "HOT", t2), "25x is not rejected")
check(daypot.entry_volume_block(hot, False, 2.0, False, "24h") is None, "25x still clears the 2x gate")
check(daypot.entry_volume_block(None, False, 2.0, False, "24h") is None, "missing volume still allows a 24h break")
check("no volume data" in (daypot.entry_volume_block(None, False, 2.0, False, "4h") or ""),
      "missing volume still blocks a 4h break")

def sget(_con, key):
    return {"BAD": {"ts": t2.isoformat(), "ratio": 2449}, "OK": {"ts": t2.isoformat(), "ratio": 3.0}}

check(daypot.stock_vol_ratio(con, "BAD", t2, sget) is None and daypot.stock_volume_rejected(con, "BAD", t2, sget),
      "a stock ratio of 2449x is rejected")
sane, rej = daypot.volume_for_entry(con, "OK", t2, "stock", sget)
check(sane == 3.0 and not rej, f"a stock ratio of 3x passes ({sane}, {rej})")

arm = {"asset": "LDO", "klass": "crypto", "sleeve": "day", "volume_min": None, "level": 0.3}
con.execute("DELETE FROM prices")
add(PREV, 719748.065)
add(GLITCH, 66476205.0)
check(entries._volume_why(con, arm, T) == "volume reading rejected",
      "the watcher blocks the glitch even when the arm has no volume minimum")
check(entries.skip_reason(con, {}, arm, 0.33, T, 1.0) == "volume reading rejected",
      "skip_reason names the rejected reading")

root = Path(__file__).resolve().parent.parent
for name in ("trader.py", "exit_watch.py", "entries.py"):
    text = (root / name).read_text()
    check("volume_for_entry" in text and "entry_volume_block" in text or name == "entries.py" and "volume reading rejected" in text,
          f"{name} uses the shared volume gate")

print("volume sanity " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
