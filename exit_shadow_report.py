#!/usr/bin/env python3
"""Compare shadow WOULD EXIT and WOULD BUY notes with the cycle's trades.

Run from the clone, after the watcher has been up in shadow mode:

    python3 exit_shadow_report.py

The shadow database is ~/exit_shadow/exit_shadow.db unless EXIT_SHADOW_DB is set.
The same default is used by exit_watch.py.

Each WOULD EXIT is paired with the cycle's sell of that same position.
Each WOULD BUY is paired with the cycle's buy of that same sleeve and asset
at or after the cycle armed it. The line under the pair is the time gap and
the price gap. A WOULD BUY with no cycle buy is a move the cycle missed.
"""
import os, sqlite3, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("TRADING_OFFLINE", "1")

from common import db
from exit_watch import shadow_db_path
import entries


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


def compare_buys(shadow_rows, buys):
    """Pair WOULD BUY rows with cycle buys. Unmatched shadow rows are moves the cycle missed.

    `shadow_rows` need ts, sleeve, asset, armed, trigger, price.
    `buys` need ts, sleeve, asset, price, reason.
    A cycle buy matches when it is the same sleeve and asset and its time is at
    or after the arm (the cycle that wrote the trigger, or a later one).
    """
    indexed = list(enumerate(buys))
    used = set()
    compared = []
    for sh in shadow_rows:
        armed = sh.get("armed") or sh.get("ts") or ""
        cands = [(i, s) for i, s in indexed
                 if i not in used and s["sleeve"] == sh["sleeve"] and s["asset"] == sh["asset"]
                 and (s.get("ts") or "") >= armed]
        cands.sort(key=lambda x: x[1]["ts"])
        sp = float(sh["price"])
        if not cands:
            compared.append(dict(asset=sh["asset"], sleeve=sh["sleeve"], shadow_ts=sh["ts"],
                                 shadow_price=sp, trigger=sh.get("trigger") or "", status="cycle missed",
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


def format_buys(compared, missed):
    lines = ["Shadow entries vs the 5-minute cycle", ""]
    if not compared and not missed:
        lines.append("No shadow entries yet.")
        return "\n".join(lines) + "\n"
    missed_moves = [r for r in compared if r["status"] != "matched"]
    for r in compared:
        lines.append(f"{r['asset']} {r['sleeve']}")
        lines.append(f"  shadow  {r['shadow_ts']}  £{r['shadow_price']:.6g}  {r['trigger']}")
        if r["status"] != "matched":
            lines.append("  cycle   no buy — the cycle missed this move")
        else:
            lines.append(f"  cycle   {r['cycle_ts']}  £{r['cycle_price']:.6g}  {r['cycle_reason']}")
            when = "later" if r["dt_s"] >= 0 else "earlier"
            lines.append(f"  cycle was {abs(r['dt_s']):.0f}s {when}, price £{r['price_diff']:+.6g} ({r['price_pct']:+.2f}%)")
        lines.append("")
    if missed_moves:
        lines.append("Moves the cycle missed")
        for r in missed_moves:
            lines.append(f"  {r['asset']} {r['sleeve']}  {r['shadow_ts']}  £{r['shadow_price']:.6g}  {r['trigger']}")
        lines.append("")
    if missed:
        lines.append("Cycle buys with no shadow entry")
        for s in missed:
            lines.append(f"  {s['asset']} {s['sleeve']}  {s['ts']}  £{float(s['price']):.6g}  {s.get('reason') or ''}")
    return "\n".join(lines).rstrip() + "\n"


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


def _shadow_rows(shadow_path, table, cols):
    sh = sqlite3.connect(shadow_path)
    sh.row_factory = sqlite3.Row
    try:
        try:
            return [dict(r) for r in sh.execute(f"SELECT {cols} FROM {table} ORDER BY ts, id")]
        except sqlite3.OperationalError:
            base = ", ".join(c.strip() for c in cols.split(",") if c.strip() != "book")
            rows = [dict(r) for r in sh.execute(f"SELECT {base} FROM {table} ORDER BY ts, id")]
            for r in rows:
                r.setdefault("book", "main")
            return rows
    except sqlite3.OperationalError:
        return []
    finally:
        sh.close()


def for_book(rows, book):
    """Rows tagged for this book. A missing tag is the main book."""
    return [r for r in rows if (r.get("book") or "main") == (book or "main")]


def pair_stats(compared):
    """pairs, average time gap in seconds, average price gap in percent, missed shadow rows."""
    matched = [r for r in compared if r.get("status") == "matched" and r.get("dt_s") is not None]
    missed = [r for r in compared if r.get("status") != "matched"]

    def avg(key):
        xs = [r[key] for r in matched if r.get(key) is not None]
        return (sum(xs) / len(xs)) if xs else None

    return dict(pairs=len(matched), avg_dt_s=avg("dt_s"), avg_price_pct=avg("price_pct"), missed=len(missed))


def format_book_summary(name, exit_stats, buy_stats):
    def line(label, st):
        if not st or (st["pairs"] == 0 and st["missed"] == 0):
            return f"  {label}: no shadow rows"
        dt = "n/a" if st["avg_dt_s"] is None else f"{st['avg_dt_s']:.0f}s"
        px = "n/a" if st["avg_price_pct"] is None else f"{st['avg_price_pct']:+.2f}%"
        return (f"  {label}: {st['pairs']} pairs, avg time gap {dt}, avg price gap {px}, "
                f"missed moves {st['missed']}")
    return f"{name}\n{line('exits', exit_stats)}\n{line('entries', buy_stats)}\n"


def load_report(book, shadow_path, book_name="main"):
    shadow_rows = for_book(_shadow_rows(shadow_path, "would_exit", "ts, sleeve, asset, opened, trigger, price, book"), book_name)
    sells = [dict(r) for r in book.execute(
        "SELECT ts, sleeve, asset, price, reason FROM trades WHERE side='SELL' AND sleeve IN ('day','risk') ORDER BY ts, id")]
    return compare_exits(shadow_rows, sells)


def load_buys(book, shadow_path, book_name="main"):
    shadow_rows = for_book(_shadow_rows(shadow_path, "would_buy", "ts, sleeve, asset, armed, trigger, price, book"), book_name)
    buys = [dict(r) for r in book.execute(
        "SELECT ts, sleeve, asset, price, reason FROM trades WHERE side='BUY' AND sleeve IN ('day','risk') ORDER BY ts, id")]
    return compare_buys(shadow_rows, buys)


def _trade_db(name):
    """Connection for the book the cycle wrote. None if that file is not there yet."""
    if not name or name == "main":
        return db()
    from variants import dir_path
    path = dir_path() / f"{name}.db"
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def report_names(exit_rows, buy_rows):
    names = ["main"]
    try:
        for name, _path in entries.variant_books():
            if name not in names:
                names.append(name)
    except Exception:
        pass
    for r in list(exit_rows) + list(buy_rows):
        b = r.get("book") or "main"
        if b not in names:
            names.append(b)
    return names


def format_all_books(exit_rows, buy_rows, opener):
    """One block per book: pair count, average time gap, average price gap, missed moves.

    `opener(name)` returns a sqlite connection of that book's trades, or None.
    The caller closes it.
    """
    lines = ["Per book", ""]
    for name in report_names(exit_rows, buy_rows):
        con = opener(name)
        try:
            if con is None:
                exits_c, buys_c = [], []
            else:
                exits_c, _m = compare_exits(for_book(exit_rows, name), [dict(r) for r in con.execute(
                    "SELECT ts, sleeve, asset, price, reason FROM trades WHERE side='SELL' AND sleeve IN ('day','risk') ORDER BY ts, id")])
                buys_c, _bm = compare_buys(for_book(buy_rows, name), [dict(r) for r in con.execute(
                    "SELECT ts, sleeve, asset, price, reason FROM trades WHERE side='BUY' AND sleeve IN ('day','risk') ORDER BY ts, id")])
        finally:
            if con is not None:
                con.close()
        lines.append(format_book_summary(name, pair_stats(exits_c), pair_stats(buys_c)).rstrip())
    return "\n".join(lines).rstrip() + "\n"


def main():
    path = shadow_db_path()
    if not path.exists():
        print(f"No shadow log yet ({path}).")
        print("Deploy the watcher with EXIT_WATCH_SHADOW=1 and ENTRY_WATCH_SHADOW=1, let it run, then run this again.")
        return 0
    exit_rows = _shadow_rows(path, "would_exit", "ts, sleeve, asset, opened, trigger, price, book")
    buy_rows = _shadow_rows(path, "would_buy", "ts, sleeve, asset, armed, trigger, price, book")
    book = db()
    try:
        compared, missed = compare_exits(for_book(exit_rows, "main"), [dict(r) for r in book.execute(
            "SELECT ts, sleeve, asset, price, reason FROM trades WHERE side='SELL' AND sleeve IN ('day','risk') ORDER BY ts, id")])
        buy_compared, buy_missed = compare_buys(for_book(buy_rows, "main"), [dict(r) for r in book.execute(
            "SELECT ts, sleeve, asset, price, reason FROM trades WHERE side='BUY' AND sleeve IN ('day','risk') ORDER BY ts, id")])
    finally:
        book.close()
    print(format_report(compared, missed), end="")
    print()
    print(format_buys(buy_compared, buy_missed), end="")
    print()
    print(format_all_books(exit_rows, buy_rows, _trade_db), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
