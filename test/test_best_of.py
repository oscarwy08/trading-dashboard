"""best_of stacks sentiment split, the entry watcher and the burst runner only.

The main book stays off. A database that exists is not described as not started.
Usage: python3 test/test_best_of.py
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("TRADING_VARIANT", None)

import common
import variants
import exit_watch

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

base = common.cfg()
R = variants.rules_of(base)
check(R["sent_split"] is False and R["entry_watch"] is False and R["burst_runner"] is False, "main leaves the three flags off")
check(R["ev_exit"] is False and R["ev_action"] is False, "main leaves the EV exit off")
os.environ["TRADING_VARIANT"] = "best_of"
over = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
Rv = variants.rules_of(over)
check(Rv["sent_split"] and Rv["entry_watch"] and Rv["burst_runner"], "best_of turns on the three flags")
check(Rv["ev_exit"] is False and Rv["ev_action"] is False, "best_of does not turn the EV exit on")
check(Rv["partial_take"] is False and Rv["day_ev_blend"] is False and Rv["risk_giveback_15"] is False, "best_of leaves the other paper flags off")
for key, val in (("sent_hot", 0.3), ("sent_overheat", 0.10), ("sent_quiet_bonus", 0.05),
                 ("sent_runup_24h_pct", 15), ("sent_runup_4h_pct", 5), ("sent_buzz_hot", 3),
                 ("sent_buzz_quiet", 1.2), ("sent_quiet_vol_x", 2), ("entry_watch_expiry_min", 15),
                 ("burst_runner_giveback_pct", 20)):
    check(abs(float(Rv[key]) - val) < 1e-9, f"{key} is {Rv[key]}")
check(over["day"]["burst"]["sentiment_missing"] == 0.5, "missing burst sentiment is neutral")
check(over["riskpot"]["sentiment_missing"] == 0.5 and over["riskpot"]["dip"]["sentiment_missing"] == 0.5, "missing Risk sentiment is neutral")
check(over["rules"]["frozen_since"] == base["rules"]["frozen_since"], "the freeze date does not move")
check("best_of" in base["variants"]["run"] and "best_of" in variants.TABLE_BOOKS, "the cycle, watcher and scorecard list this book")
check(variants.active(Rv) and not variants.active(R), "best_of is an active paper book")
check(exit_watch._entry_live_ok("best_of", over) and exit_watch._entry_live_ok("entry_watch", base), "the watcher may buy best_of and entry_watch")
check(not exit_watch._entry_live_ok("main", base) and not exit_watch._entry_live_ok("sent_split", base), "main and a book without the flag are not bought live")
check("best_of: 'Best of'" in (ROOT / "web/app.js").read_text(), "the comparison page names this book")
check("Comparison rows are missing" in (ROOT / "web/app.js").read_text(), "a started comparison is not labelled as not started")

# A variant file on disk must not be described as not started, even if the report cannot open it.
slot = Path(os.environ.get("VARIANTS_DIR") or (ROOT / "data" / "variants"))
# Use a throwaway directory so this does not touch the live books.
tmp = ROOT / "test" / "_best_of_variants"
if tmp.exists():
    for p in tmp.glob("*"):
        p.unlink()
else:
    tmp.mkdir(parents=True)
dbp = tmp / "best_of.db"
con = sqlite3.connect(dbp)
con.execute("CREATE TABLE state(key TEXT, value TEXT)")
con.commit(); con.close()
os.environ["VARIANTS_DIR"] = str(tmp)
os.environ["VARIANTS_MAIN_DB"] = str(tmp / "missing-main.db")
line = variants.summary_line()
os.environ.pop("VARIANTS_DIR", None)
os.environ.pop("VARIANTS_MAIN_DB", None)
check("not started yet" not in line, f"an existing book file is not 'not started' ({line})")
dbp.unlink()

print("best of " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
