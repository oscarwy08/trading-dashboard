"""Stock session time is the same number, computed once per series instead of once per bar.

Usage: python3 test/test_session_clock.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["TRADING_LOG"] = "/tmp/session-clock.log"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import markets

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

end = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
starts = [end - timedelta(minutes=15 * i) for i in range(0, 400, 3)]
many = markets.open_minutes_many("VUSA.L", starts, end)
for s in starts:
    one = markets.open_minutes("VUSA.L", s, end)
    got = many[s]
    check(got == one or (got is not None and one is not None and abs(got - one) < 1e-9),
          f"batch matches single for {s.isoformat()} ({got} vs {one})")
    if fails:
        break

print("session clock " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
