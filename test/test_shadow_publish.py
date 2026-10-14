"""The cycle publishes the shadow report under data/, at most hourly.

A missing shadow database writes a no-data note. The database itself stays gitignored.
Usage: python3 test/test_shadow_publish.py
"""
import json, os, sqlite3, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(__file__).resolve().parent / "_shadow_publish"
TMP.mkdir(exist_ok=True)
os.environ["TRADING_DB"] = str(TMP / "book.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["TRADING_OFFLINE"] = "1"
os.environ.pop("TRADING_VARIANT", None)
sys.path.insert(0, str(ROOT))

import exit_shadow_report as report

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

dest = TMP / "out"
dest.mkdir(exist_ok=True)
missing = TMP / "absent.db"
wrote = report.write_published(max_age_s=0, shadow_path=missing, dest_dir=dest)
text = (dest / "exit_shadow_report.txt").read_text()
payload = json.loads((dest / "exit_shadow_report.json").read_text())
check(wrote and text.startswith("no data\n"), f"a missing shadow log writes a no-data note ({text!r})")
check(payload.get("status") == "no data" and payload.get("books") == [], "the JSON says no data")

stamp = (dest / "exit_shadow_report.txt").stat().st_mtime
time.sleep(0.05)
again = report.write_published(max_age_s=3600, shadow_path=missing, dest_dir=dest)
check(again is False and (dest / "exit_shadow_report.txt").stat().st_mtime == stamp,
      "a report younger than an hour is left in place")

shadow = TMP / "shadow.db"
sh = sqlite3.connect(shadow)
sh.execute("""CREATE TABLE would_exit (
    id INTEGER PRIMARY KEY, ts TEXT, sleeve TEXT, asset TEXT, opened TEXT, trigger TEXT,
    price REAL, chg REAL, peak REAL, qty REAL, entry REAL, klass TEXT, book TEXT)""")
sh.execute("""CREATE TABLE would_buy (
    id INTEGER PRIMARY KEY, ts TEXT, sleeve TEXT, sub TEXT, setup TEXT, asset TEXT, armed TEXT,
    expires TEXT, trigger TEXT, level REAL, price REAL, size_gbp REAL, stop_pct REAL,
    target_pct REAL, klass TEXT, reason TEXT, book TEXT)""")
sh.execute("INSERT INTO would_exit(ts,sleeve,asset,opened,trigger,price,book) VALUES(?,?,?,?,?,?,?)",
           ("2026-10-12T02:42:00+00:00", "risk", "CFX", "2026-10-11T04:07:00+00:00", "trail", 0.0483, "main"))
sh.commit(); sh.close()
con = report.db()
con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-12T02:42:14+00:00", "risk", "CFX", "crypto", "SELL", 1, 0.04834, 0.04823, 5, 0.01, 0.39, "trailing take-profit"))
con.commit(); con.close()
fresh = TMP / "fresh"
check(report.write_published(max_age_s=0, shadow_path=shadow, dest_dir=fresh), "a shadow row is published")
body = (fresh / "exit_shadow_report.txt").read_text()
got = json.loads((fresh / "exit_shadow_report.json").read_text())
check("CFX risk" in body and got["status"] == "ok" and got["books"][0]["exits"]["pairs"] == 1,
      f"the published pair is CFX ({got['books']})")

ignored = subprocess.run(["git", "check-ignore", "-v", "data/exit_shadow_report.txt", "data/exit_shadow_report.json"],
                         cwd=ROOT, capture_output=True, text=True)
check(ignored.returncode != 0 and ignored.stdout == "", "the published report is not gitignored")
kept = subprocess.run(["git", "check-ignore", "data/exit_shadow.db"], cwd=ROOT, capture_output=True, text=True)
check(kept.stdout.strip() == "data/exit_shadow.db", "the shadow database stays gitignored")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
