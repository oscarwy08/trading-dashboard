"""Strategies tab cards. Tweaks of the main rules stay off that list.

Usage: python3 test/test_strategy_cards.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
import strategytext
from common import cfg

fails = []


def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


root = Path(__file__).resolve().parent.parent
html = (root / "web" / "index.html").read_text()
js = (root / "web" / "app.js").read_text()
catalog = strategytext.by_name()
run = list((cfg().get("variants") or {}).get("run") or [])

check("id=\"st-cards\"" in html, "the Strategies tab has a card list")
check("Main strategy" in js and "rulebook" in js, "the Variants tab groups main-rule tweaks from the rule book")
check("ev_exit" not in strategytext.strategy_names(), "a tweak is not a strategy card")
check("partial_take" not in strategytext.strategy_names(), "partial take stays off the Strategies tab")
check("day_trail_arm" not in strategytext.strategy_names(), "the day trail tweak stays off the Strategies tab")

for name in strategies.BOOKS:
    card = catalog.get(name)
    if card and card.get("family") == "signal":
        check(card["kind"] == "book" and card["show"] == "when_live", name + " appears once it has a Test 2 row")
    else:
        check(card and card["kind"] == "book" and card["show"] == "always", name + " is a strategy card")
    if card:
        for key in ("holds", "entry", "exit", "rebalance", "data"):
            check(bool(card.get(key)), name + " explains " + key)

for name in run:
    card = catalog.get(name)
    check(card is not None, name + " is in the rule book")
    if not card:
        continue
    if name in strategies.BOOKS:
        check(card["kind"] == "book", name + " is not filed as a tweak")
    else:
        check(card["kind"] == "tweak" and card.get("tweak"), name + " is a one-line tweak")

check(catalog["main"]["kind"] == "rules", "the main card is the Core, Day and Risk rules")
for key in ("holds", "entry", "exit", "rebalance", "data"):
    check(bool(catalog["main"].get(key)), "main rules explain " + key)

signals = [c for c in strategytext.cards() if c.get("family") == "signal"]
check(len(signals) == 17, "seventeen signal books are described")
check(all(c["show"] == "when_live" and c["kind"] == "book" and c["name"] in strategies.PURE for c in signals),
      "signal books are their own sleeves and wait for a Test 2 row")
check(not (set(strategytext.tweak_names()) & set(strategytext.strategy_names())),
      "a tweak name is never also a strategy name")

if fails:
    print(f"\n{len(fails)} FAILED")
    sys.exit(1)
print("strategy cards ok")
