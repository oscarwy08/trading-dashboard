"""Max drawdown is the book's own equity since the test opened, not a sleeve snapshot.

Usage: python3 test/test_drawdown.py
"""
import os
import sys
from pathlib import Path

os.environ["TRADING_LOG"] = "/tmp/dd.log"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import curve_drawdown

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

# The trader writes the £30 day/risk slice, then the strategy engine writes the whole book.
ghost = []
t = 1_000_000
for i in range(5):
    ghost.append((t, 30.0))
    ghost.append((t + 15, 200.0 + i * 0.1))
    t += 600
dd = curve_drawdown(ghost, 200)
check(dd > -0.01, f"sleeve snapshots are not an 85% drawdown (got {dd:.3%})")

real = [(1, 200.0), (2, 190.0), (3, 170.0), (4, 180.0)]
dd = curve_drawdown(real, 200)
check(abs(dd - (170 / 200 - 1)) < 1e-9, f"a real fall from the £200 open is kept ({dd:.3%})")

# A low print with no full-book neighbour is a real mark, not a ghost.
alone = [(1, 200.0), (1 + 3600, 30.0)]
dd = curve_drawdown(alone, 200)
check(abs(dd - (30 / 200 - 1)) < 1e-9, "a low print that stays low is a real drawdown")

print("drawdown " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
