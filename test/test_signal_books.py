"""Seventeen signal paper books. No network. Each book is its own £200 sleeve.

Usage: python3 test/test_signal_books.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signalbooks
import signalcache
import signalcomb
import signalhist
import strategies
from common import cfg

fails = []


def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


root = Path(__file__).resolve().parent.parent
run = list((cfg().get("variants") or {}).get("run") or [])
cycle = (root / "scripts" / "ci_cycle.sh").read_text()
check("newssent" not in cycle, "the 15-minute cycle does not score news")
check(len(signalbooks.ORDER) == 17, "seventeen signal books")
check(signalbooks.ORDER == tuple(run[-17:]), "the run list ends with the seventeen signal books")

for name in signalbooks.ORDER:
    text = (root / "config" / "variants" / f"{name}.yaml").read_text()
    check(name in strategies.PURE, name + " is a strategy sleeve, not the main trader")
    check("hold_overnight: true" in text, name + " holds overnight")
    kind, mode = signalbooks.kind(name)
    check(kind in ("tilt", "regime") and mode, name + " has a mode")

# Weights. Baseline has no nine-trial hurdle. Equal is a vote. Multitest uses that hurdle.
w, kept = signalcomb.decide_weight(0.14, 0.13, 400, 400)
check(kept and abs(w - 0.13) < 1e-9, "baseline keeps the weaker correlation, 0.13")
w, kept = signalcomb.decide_weight(0.08, 0.06, 40, 20)
check(kept and abs(w - 0.06) < 1e-9, "baseline keeps a small agreed correlation")
check(signalcomb.decide_weight(0.01, 0.01, 400, 400)[1] is False, "a tiny correlation is dropped")
check(signalcomb.decide_weight(0.9, 0.9, 400, 400)[1] is False, "a perfect correlation is dropped")
check(signalcomb.decide_weight(0.2, -0.2, 400, 400)[1] is False, "a sign flip is dropped")
check(signalcomb.decide_weight(0.2, 0.2, 20, 20)[1] is False, "a short window is dropped")
check(signalcomb.decide_weight(0.2, 0.2, 400, 400, mode="equal") == (1.0, True), "equal weight is one vote")
check(signalcomb.decide_weight(-0.2, -0.2, 400, 400, mode="equal") == (-1.0, True), "equal weight keeps the sign")
hurdle = signalcomb.ic_hurdle(400, 9)
check(signalcomb.decide_weight(0.08, 0.07, 400, 400, mode="multitest")[1] is False
      and 0.07 < hurdle, "multitest drops a correlation under the nine-signal bar")
w, kept = signalcomb.decide_weight(0.20, 0.18, 400, 400, mode="multitest")
check(kept and abs(w - 0.15) < 1e-9, "multitest keeps a correlation above the bar, capped at 0.15")

idx = list(range(100))
train, confirm = signalcomb._purged_windows(idx)
check(confirm and confirm[0] == 75 and all(i + 5 < 70 for i in train) and 64 in train and 65 not in train,
      "purge drops overlapping labels and embargoes five confirm days")
faded = signalcomb.news_value_on([("2024-01-01", 0.8)], "2024-01-04")
check(abs(faded - 0.4) < 1e-9, "a three-day gap halves a news score")

# Cash stays cash. A hot composite cannot buy a downtrend.
start = date(2024, 1, 1)
down = [((start + timedelta(days=i)).isoformat(), 200 - i) for i in range(60)]
up = [((start + timedelta(days=i)).isoformat(), 100 + i * 2) for i in range(60)]
opts = strategies.opts()
check(strategies.targets_for("signal_engine", {"BTC": down},
                              {"signal_kind": "tilt", "composite": {"BTC": 1}, "opts": opts}) == {},
      "a tilt cannot put a downtrend into the market")
above = strategies.targets_for("signal_engine", {"BTC": up},
                               {"signal_kind": "tilt", "composite": {"BTC": 0}, "opts": opts})
check(above.get("BTC", (None, 0))[0] == "crypto" and above["BTC"][1] > 0, "an uptrend keeps the vol size")
check(strategies.targets_for("signal_dominance", {"BTC": up},
                             {"signal_kind": "regime", "regime": {}}) == {},
      "a regime book with no reading stays in cash")
macro = strategies.targets_for(
    "signal_macro",
    {"BTC": up, "VUSA.L": up},
    {"signal_kind": "regime", "regime": {"BTC": 0, "VUSA": 1}, "opts": opts})
check("BTC" not in macro and macro.get("VUSA.L") == ("stocks", 1.0), "macro can hold the stock sleeve and leave BTC in cash")

# A 40% funding print changes the next day, not the day it arrives.
days = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(8)]
btc = [(d, 100.0) for d in days]
fund = [(d, 0.01) for d in days]
fund[5] = (days[5], 0.40)
raw, _score = signalhist.funding_rows(signalhist.Tape(btc), signalhist.Tape(fund), False)
check(raw.get(days[5]) != 0 and raw.get(days[6]) == 0, "hot funding is cash from the next day")

# Monthly momentum holds the stock sleeve across a Saturday with no print.
span = []
d = date(2023, 1, 2)
while d <= date(2025, 1, 6):
    span.append(d)
    d += timedelta(days=1)
btc_px = [(d.isoformat(), 100 * (1.002 ** i)) for i, d in enumerate(span)]
vusa = [(d.isoformat(), 50 * (1.001 ** i)) for i, d in enumerate(span) if d.weekday() < 5]
held = signalhist.book_regime({"BTC": btc_px, "VUSA": vusa}, {}, "tsmom")
check(held.get("2025-01-04", {}).get("VUSA") == 1 and held.get("2025-01-03", {}).get("VUSA") == 1,
      "12-month momentum keeps VUSA through Saturday")

mon = date(2026, 10, 12)
check(strategies.due("signal_engine", mon, date(2026, 10, 13)) is False
      and strategies.due("signal_engine", mon, date(2026, 10, 19)) is True,
      "tilt books rebalance on a new ISO week")
check(strategies.due("signal_dominance", mon, date(2026, 10, 13)) is True,
      "a timing book can update the day the period flips")

# £200, split across the two accounts.
tmp = Path(tempfile.mkdtemp())
con = strategies._connect(tmp / "book.db")
try:
    strategies._blank_trend(con)
    con.commit()
    rows = con.execute("SELECT account, amount FROM acct_cash WHERE sleeve='trend'").fetchall()
finally:
    con.close()
check(abs(sum(r[1] for r in rows) - 200) < 1e-9 and {r[0] for r in rows} == {"crypto", "stocks"},
      "a signal book is funded £100 crypto and £100 stocks")

# The shared decision file is enough. Children do not fetch.
calls = []


def _no_net(*_a, **_k):
    calls.append(1)
    raise RuntimeError("network")


import urllib.request
urllib.request.urlopen = _no_net
live = tmp / "live.json"
signalcache.LIVE = live
signalcache.CACHE = tmp / "cache.json"
signalcache.ATTEMPTS = tmp / "attempts.json"
os.environ["SIGNAL_OFFLINE"] = "1"
series = [((date(2024, 1, 1) + timedelta(days=i)).isoformat(), 100 + i) for i in range(800)]
dt = signalcache.prepare({"BTC": series}, today=date(2026, 10, 13))
check(calls == [] and dt < 8, "an offline prepare makes no request and stays under 8s (%.2fs)" % dt)
row = signalcache.row_for("signal_funding_contrarian", {"BTC": series})
check(row.get("signal_kind") == "regime" and calls == [], "a child reads the decision and does not fetch")
os.environ.pop("SIGNAL_OFFLINE", None)
signalcache.ATTEMPTS.write_text("{}")
signalcache.prepare({"BTC": series}, today=date(2026, 10, 19))
first = len(calls)
signalcache.prepare({"BTC": series}, today=date(2026, 10, 19))
check(first == 1 and len(calls) == 1, "a stale series is asked once that day, not every cycle")

if fails:
    print("\n%d FAILED" % len(fails))
    sys.exit(1)
print("signal books ok")
