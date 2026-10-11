#!/usr/bin/env python3
"""Compare shadow WOULD EXIT notes with the sells the 5-minute cycle made.

Run from the clone, after the watcher has been up in shadow mode:

    python3 exit_shadow_report.py

Each shadow row is paired with the cycle's sell of that same position
(same sleeve and asset, sell time at or after the position opened).
The line under the pair is how much later the cycle was, and how far
its fill price moved from the watcher's trigger price.
"""
import os, sqlite3, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("TRADING_OFFLINE", "1")

from common import db
from exit_watch import shadow_db_path


def _ts(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def compare_exits(shadow_rows, sells):
    """Pair shadow exits with cycle sells. Also return cycle sells in the
    shadow window that the watcher did not flag.

    `shadow_rows` need ts, sleeve, asset, opened, trigger, price.
    `sells` need ts, sleeve, asset, price, reason.
    """
    indexed = list(enumerate(sells))
    used = set()
    compared = []
    for sh in shadow_rows:
        opened = sh.get("opened") or ""
        cands = [(i, s) for i, s in indexed
                 if i not in used and s["sleeve"] == sh["sleeve"] and s["asset"] == sh["asset"]
                 and (s.get("ts") or "") >= opened]
        cands.sort(key=lambda x: x[1]["ts"])
        sp = float(sh["price"])
        if not cands:
            compared.append(dict(asset=sh["asset"], sleeve=sh["sleeve"], shadow_ts=sh["ts"],
                                 shadow_price=sp, trigger=sh.get("trigger") or "", status="no cycle sell",
                                 cycle_ts=None, cycle_price=None, cycle_reason=None,
                                 dt_s=None, price_diff=None, price_pct=None))
            continue
        i, s = cands[0]
        used.add(i)
        cp = float(s["price"])
        compared.append(dict(
            asset=sh["asset"], sleeve=sh["sleeve"], shadow_ts=sh["ts"], shadow_price=sp,
            trigger=sh.get("trigger") or "", status="matched", cycle_ts=s["ts"], cycle_price=cp,
            cycle_reason=s.get("reason") or "",
            dt_s=(_ts(s["ts"]) - _ts(sh["ts"])).total_seconds(),
            price_diff=cp - sp, price_pct=((cp - sp) / sp * 100.0) if sp else None))
    start = min((sh["ts"] for sh in shadow_rows), default=None)
    missed = []
    if start:
        for i, s in indexed:
            if i in used or s["sleeve"] not in ("day", "risk") or (s.get("ts") or "") < start:
                continue
            missed.append(s)
    return compared, missed


def format_report(compared, missed):
    lines = ["Shadow exits vs the 5-minute cycle", ""]
    if not compared and not missed:
        lines.append("No shadow exits yet.")
        return "\n".join(lines) + "\n"
    for r in compared:
        lines.append(f"{r['asset']} {r['sleeve']}")
        lines.append(f"  shadow  {r['shadow_ts']}  £{r['shadow_price']:.6g}  {r['trigger']}")
        if r["status"] != "matched":
            lines.append("  cycle   no sell yet")
        else:
            lines.append(f"  cycle   {r['cycle_ts']}  £{r['cycle_price']:.6g}  {r['cycle_reason']}")
            when = "later" if r["dt_s"] >= 0 else "earlier"
            lines.append(f"  cycle was {abs(r['dt_s']):.0f}s {when}, price £{r['price_diff']:+.6g} ({r['price_pct']:+.2f}%)")
        lines.append("")
    if missed:
        lines.append("Cycle sells with no shadow exit")
        for s in missed:
            lines.append(f"  {s['asset']} {s['sleeve']}  {s['ts']}  £{float(s['price']):.6g}  {s.get('reason') or ''}")
    return "\n".join(lines).rstrip() + "\n"


def load_report(book, shadow_path):
    sh = sqlite3.connect(shadow_path)
    sh.row_factory = sqlite3.Row
    try:
        shadow_rows = [dict(r) for r in sh.execute(
            "SELECT ts, sleeve, asset, opened, trigger, price FROM would_exit ORDER BY ts, id")]
    except sqlite3.OperationalError:
        shadow_rows = []
    finally:
        sh.close()
    sells = [dict(r) for r in book.execute(
        "SELECT ts, sleeve, asset, price, reason FROM trades WHERE side='SELL' AND sleeve IN ('day','risk') ORDER BY ts, id")]
    return compare_exits(shadow_rows, sells)


def main():
    path = shadow_db_path()
    if not path.exists():
        print(f"No shadow log yet ({path}).")
        print("Deploy the watcher with EXIT_WATCH_SHADOW=1, let it run, then run this again.")
        return 0
    book = db()
    try:
        compared, missed = load_report(book, path)
    finally:
        book.close()
    print(format_report(compared, missed), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
