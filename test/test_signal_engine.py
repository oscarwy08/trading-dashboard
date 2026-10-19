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
check(signalcomb.decide_weight(0.08, 0.06, 40, 20)[1] is True, "two windows that agree are kept")
w, kept = signalcomb.decide_weight(0.08, 0.06, 40, 20)
check(kept and abs(w - 0.06) < 1e-9, "the weight is the weaker correlation")
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
flat = [0.2 for _ in range(80)]
weight, kept, *_ = signalcomb.measure_series(flat, prices)
check(kept is False, "a signal that never changes is not used")

# --- the book is a vol trend with a tilt, and it does not go short ---
up = geo(100, 80, 1.004)
closes = {"BTC": up, "VUSA": geo(50, 80, 1.002)}
base = strategies.targets_for("btc_trend_vol", closes)
plain = strategies.targets_for("signal_engine", closes, {"composite": {}})
check(abs(plain["BTC"][1] - base["BTC"][1]) < 1e-9 and abs(plain["VUSA"][1] - base["VUSA"][1]) < 1e-9,
      "no composite leaves the vol-sized trend unchanged")
faded = strategies.targets_for("signal_engine", closes, {"composite": {"BTC": -1, "VUSA": -1}})
check(abs(faded["BTC"][1] - base["BTC"][1] * 0.5) < 1e-9, "a negative composite halves exposure")
check(faded["BTC"][1] > 0, "the tilt stays long")
hot = strategies.targets_for("signal_engine", closes, {"composite": {"BTC": 1}})
check(hot["BTC"][1] <= 1.0 and hot["BTC"][1] >= base["BTC"][1], "a positive composite does not exceed full size")
down = geo(200, 80, 0.99)
check("BTC" not in strategies.targets_for("signal_engine", {"BTC": down}, {"composite": {"BTC": 1}}),
      "a downtrend stays in cash even when the composite is hot")
check(strategies.due("signal_engine", date(2026, 10, 5), date(2026, 10, 6)) is False
      and strategies.due("signal_engine", date(2026, 10, 5), date(2026, 10, 12)) is True,
      "the book waits for a new week")
check(signalcomb.tilt(0, 1) == 0, "cash is not turned into a position by the composite")

R = variants.rules_of(common.cfg())
check(R["hold_overnight"] is False, "the main book still flattens")
os.environ["TRADING_VARIANT"] = "signal_engine"
eng = variants.rules_of(common.cfg())
os.environ.pop("TRADING_VARIANT", None)
check(eng["hold_overnight"] is True, "the signal book holds overnight")
check("signal_engine" in common.cfg()["variants"]["run"] and "signal_engine" in strategies.PURE,
      "the cycle list runs the book through the strategy engine")

# A rising tape with no edge should invest. A constant negative tilt should finish with more cash.
window = {"BTC": geo(100, 100, 1.004), "VUSA": geo(40, 100, 1.001)}
full = strategies.replay("signal_engine", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5)
cut_days = {}
for day, _ in window["BTC"]:
    cut_days[day] = {"BTC": -1.0, "VUSA": -1.0}
cut = strategies.replay("signal_engine", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5,
                        carry={"by_day": cut_days})
vol = strategies.replay("btc_trend_vol", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5)
fs, cs, vs = strategies.replay_stats(full), strategies.replay_stats(cut), strategies.replay_stats(vol)
check(full["pos"].get("BTC") and cut["cash"]["crypto"] > full["cash"]["crypto"],
      "halving the tilt leaves more crypto cash on a rising tape")
check(fs["ret"] > cs["ret"] > 0, "full exposure finishes ahead of the halved book, and both are up")
print(f"replay signal {fs['ret']*100:+.1f}% dd {fs['max_dd']*100:.1f}% trades {fs['trades']} vs btc {fs['excess_btc']*100:+.1f}%")
print(f"replay halved {cs['ret']*100:+.1f}% dd {cs['max_dd']*100:.1f}% trades {cs['trades']}")
print(f"replay btc_trend_vol {vs['ret']*100:+.1f}% dd {vs['max_dd']*100:.1f}% trades {vs['trades']} "
      f"btc hold {vs['btc_ret']*100:+.1f}%")

try:
    os.remove(path)
except OSError:
    pass

print("signal-engine " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
