"""News scores, out-of-sample signal weights, and the signal-engine book. No network.

Usage: python3 test/test_signal_engine.py
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("TRADING_VARIANT", None)
os.environ["NEWS_DB"] = str(Path(__file__).resolve().parent / "_news_test.db")

import common
import newssent
import signalcomb
import signalhist
import strategies
import variants

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

def geo(start, n, daily, d0=date(2024, 1, 1)):
    px, out = start, []
    for i in range(n):
        out.append(((d0 + timedelta(days=i)).isoformat(), px))
        px *= daily
    return out

# --- headlines ---
rss = """<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin surges after approval</title><link>https://example.test/a</link></item>
<item><title>NVDA crash</title><link>https://example.test/b</link></item>
</channel></rss>"""
items = newssent.parse_feed(rss)
check(len(items) == 2 and items[0]["url"].endswith("/a"), "an RSS item keeps its title and link")
check(newssent.lexicon_score("Bitcoin surges after approval") > 0, "a rally headline scores positive")
check(newssent.lexicon_score("Exchange hack and bankruptcy") < 0, "a hack headline scores negative")
check(newssent.lexicon_score("The calendar today") == 0, "a headline with no finance words scores zero")
uni = {"BTC", "ETH", "NVDA", "THE"}
check(newssent.map_assets("Bitcoin surges", uni) == ["BTC"], "bitcoin maps to BTC")
check("NVDA" in newssent.map_assets("NVDA crash", uni), "a capitalised ticker maps")
check(newssent.map_assets("The market today", uni) == [], "an ordinary word is not a ticker")
path = os.environ["NEWS_DB"]
if os.path.exists(path):
    os.remove(path)
added = newssent.store_items([dict(source="test", title="Bitcoin surges", url="https://example.test/a",
                                   score=0.5, assets=["BTC"])], path=path)
again = newssent.store_items([dict(source="test", title="Bitcoin surges", url="https://example.test/a",
                                   score=0.5, assets=["BTC"])], path=path)
check(added == 1 and again == 0, "the same headline is stored once")
check("newssent" not in (Path(__file__).resolve().parent.parent / "scripts" / "ci_cycle.sh").read_text(),
      "the trading cycle does not fetch news")

# --- weights: agree to keep, disagree or a perfect fit to drop ---
check(abs(signalcomb.ic_hurdle(400) - signalcomb.ic_hurdle(400, 21)) < 1e-12,
      "the hurdle counts all 21 pre-registered tries")
check(signalcomb.ic_hurdle(400, 21) > signalcomb.ic_hurdle(400, 9), "the extra tries raise the bar")
check(signalcomb.decide_weight(0.14, 0.13, 400, 400)[1] is True, "two windows that clear the multiple-test bar are kept")
w, kept = signalcomb.decide_weight(0.14, 0.13, 400, 400)
check(kept and abs(w - 0.13) < 1e-9, "the weight is the weaker correlation")
check(signalcomb.decide_weight(0.12, 0.11, 400, 400)[1] is False, "a correlation inside the multiple-test noise bar is dropped")
check(signalcomb.decide_weight(0.08, -0.06, 40, 20)[1] is False, "a sign change is dropped")
check(signalcomb.decide_weight(0.01, 0.01, 40, 20)[1] is False, "a tiny correlation is dropped")
check(signalcomb.decide_weight(0.9, 0.9, 40, 20)[1] is False, "a perfect correlation is dropped")
check(signalcomb.decide_weight(0.08, 0.06, 10, 20)[1] is False, "a short window is dropped")

prices = [100.0 + i * 0.4 for i in range(80)]
leaked = []
for i in range(80):
    leaked.append(None if i + 5 >= 80 else prices[i + 5] / prices[i] - 1.0)
weight, kept, *_ = signalcomb.measure_series(leaked, prices)
check(kept is False, "a signal that copies the future return is not used")
idx = list(range(100))
train_idx, confirm_idx = signalcomb._purged_windows(idx)
cut = int(len(idx) * 0.70)
check(64 in train_idx and 65 not in train_idx, "a train point whose forward return overlaps confirm is excluded")
check(confirm_idx and confirm_idx[0] == cut + signalcomb.HORIZON, "the first confirm points are embargoed")
check(train_idx[-1] + signalcomb.HORIZON < confirm_idx[0], "purged train labels end before the confirm window")
flat = [0.2 for _ in range(80)]
weight, kept, *_ = signalcomb.measure_series(flat, prices)
check(kept is False, "a signal that never changes is not used")

# --- the book is long or cash from the regime, and it does not go short ---
up = geo(100, 80, 1.004)
closes = {"BTC": up, "VUSA": geo(50, 80, 1.002)}
check("BTC" not in strategies.targets_for("signal_engine", closes, {}),
      "no regime leaves the book in cash")
long = strategies.targets_for("signal_engine", closes, {"regime": {"BTC": 1, "VUSA": 1}})
check(long.get("BTC") == ("crypto", 1.0) and long.get("VUSA") == ("stocks", 1.0),
      "a long regime is fully invested in BTC and VUSA")
down = geo(200, 80, 0.99)
check(strategies.targets_for("signal_engine", {"BTC": down}, {"regime": {"BTC": 1}}).get("BTC") == ("crypto", 1.0),
      "the timing rule can be long when the 50-day trend is down")
check("BTC" not in strategies.targets_for("signal_engine", closes, {"regime": {"BTC": 0}}),
      "a cash regime holds no coin")
check(strategies.due("signal_engine", date(2026, 10, 5), date(2026, 10, 6)) is True,
      "the book looks every day; the regime series is what keeps turnover low")
check(signalcomb.tilt(0, 1) == 0, "the old tilt helper still cannot turn cash into a position")
check(signalhist.book_regime({"BTC": geo(100, 20, 1.01)}) == {},
      "a short tape with no side history stays in cash")

R = variants.rules_of(common.cfg())
check(R["hold_overnight"] is False, "the main book still flattens")
os.environ["TRADING_VARIANT"] = "signal_engine"
eng = variants.rules_of(common.cfg())
os.environ.pop("TRADING_VARIANT", None)
check(eng["hold_overnight"] is True, "the signal book holds overnight")
check("signal_engine" in common.cfg()["variants"]["run"] and "signal_engine" in strategies.PURE,
      "the cycle list runs the book through the strategy engine")

# A supplied long regime invests. A supplied cash regime does not.
window = {"BTC": geo(100, 40, 1.004), "VUSA": geo(40, 40, 1.001)}
days = [d for d, _ in window["BTC"]]
full = strategies.replay("signal_engine", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5,
                         carry={"positions": {d: {"BTC": 1, "VUSA": 1} for d in days}})
cut = strategies.replay("signal_engine", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5,
                        carry={"positions": {d: {"BTC": 0, "VUSA": 0} for d in days}})
fs, cs = strategies.replay_stats(full), strategies.replay_stats(cut)
check(full["pos"].get("BTC") and cut["cash"]["crypto"] > full["cash"]["crypto"],
      "a cash regime leaves more crypto cash than a long regime on a rising tape")
check(fs["ret"] > cs["ret"], "full exposure finishes ahead of cash")
print(f"replay long {fs['ret']*100:+.1f}% cash {cs['ret']*100:+.1f}%")

try:
    os.remove(path)
except OSError:
    pass


days = [(date(2023, 1, 1) + timedelta(days=i)).isoformat() for i in range(80)]
btc = [(d, 100.0) for d in days]
low = [(d, 20.0) for d in days]
high = [(d, 20.0) for d in days]
for i in range(60, 80):
    high[i] = (days[i], 80.0)
raw_low, _ = signalhist.vrp_rows(signalhist.Tape(btc), signalhist.Tape(low))
raw_high, _ = signalhist.vrp_rows(signalhist.Tape(btc), signalhist.Tape(high))
check(raw_high.get(days[62]) == 1, "a jump in implied variance above the trailing median is long")
check(raw_low.get(days[62]) == 0, "a flat premium equal to its own median is not long")

print("signal-engine " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
