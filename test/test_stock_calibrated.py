"""stock_calibrated is a paper book. The main book does not take its stock rules.

Usage: python3 test/test_stock_calibrated.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("TRADING_VARIANT", None)

import daypot
import trader
import variants

fails = []

def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

base = variants.rules_of({})
check(base["stock_calibrated"] is False, "the flag is off when no overlay is loaded")
os.environ["TRADING_VARIANT"] = "stock_calibrated"
over = variants.rules_of(__import__("common").cfg())
os.environ.pop("TRADING_VARIANT", None)
check(over["stock_calibrated"] is True, "the overlay turns the flag on")
check(variants.active(over) and not variants.active(base), "only the overlay is an active book")
check("stock_calibrated" in __import__("common").cfg()["variants"]["run"], "the cycle and the watcher read this name")
check("stock_calibrated" in variants.TABLE_BOOKS, "the scorecard table includes this book")
themes = __import__("yaml", fromlist=["safe_load"]).safe_load((ROOT / "config/variants/stock_calibrated.yaml").read_text())
check(themes["riskpot"]["stock_themes"] == ["photonics", "quantum", "robotics", "metals_rwa", "rare_earths", "farmland", "uk_early", "global_small"],
      "the Risk list is the watch themes")

# A 1% ATR. A 1% 4h move is one ATR (full on that part) and was only a third of the old 3% unit.
pts = [(datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc) + timedelta(hours=i), 100.0) for i in range(20)]
pts += [(datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc) + timedelta(hours=i), 100.0 + i * 0.25) for i in range(6)]
raw = trader.momentum(pts, 4)
scaled = trader.momentum(pts, 4, atr_norm=(1.0, 1.0, 2.0))
check(raw[0] is not None and scaled[0] is not None and scaled[0] != raw[0], f"ATR scaling changes the stock score ({raw[0]} -> {scaled[0]})")
check(trader.momentum(pts, 4) == raw, "calling momentum without atr_norm is the frozen score")

tp, sl = variants.stock_tp_sl(4.0, 2.0, 0.5, over, {"min_pct": 4, "atr_x": 2.5, "max_pct": 12}, {"min_pct": 2, "atr_x": 1.2, "max_pct": 5}, "stock")
check(abs(tp - 1.25) < 1e-9 and abs(sl - 0.6) < 1e-9, f"a quiet stock uses ATR floors, not 4% and 2% ({tp}, {sl})")
same = variants.stock_tp_sl(4.0, 2.0, 0.5, base, {"max_pct": 12, "atr_x": 2.5}, {"max_pct": 5, "atr_x": 1.2}, "stock")
check(same == (4.0, 2.0), "the main book keeps the levels it was given")
check(variants.stock_tp_sl(4.0, 2.0, 0.5, over, {"max_pct": 12, "atr_x": 2.5}, {"max_pct": 5, "atr_x": 1.2}, "crypto") == (4.0, 2.0),
      "crypto on this book keeps the frozen floors")

check(variants.stock_flatten_floor(over, "stock", 5 * 60, 0.40) == 0.0, "more than 4h to the close drops the 0.40 floor")
check(variants.stock_flatten_floor(over, "stock", 3 * 60, 0.40) == 0.40, "inside 4h the floor stays")
check(variants.stock_flatten_floor(base, "stock", 5 * 60, 0.40) == 0.40, "the main book keeps the floor with hours left")
check(variants.stock_flatten_floor(over, "crypto", 5 * 60, 0.40) == 0.40, "crypto keeps the floor")

RP = {"dip": {"trend_min_pct": 8}, "hit_prior": 0.5}
kept = variants.stock_dip_rules(RP, over, 1.5, None, "stock")
scaled_dip = variants.stock_dip_rules(RP, over, 1.5, 0.2, "stock")
check(kept is RP, "no sentiment print leaves the 48h uptrend alone")
check(abs(scaled_dip["dip"]["trend_min_pct"] - 3.0) < 1e-9, "a sentiment print scales the uptrend to 2 x ATR")
check(variants.stock_dip_rules(RP, base, 1.5, 0.2, "stock") is RP, "the main book does not scale the dip")

check(variants.stock_prior_n(over, "stock", 10) == 12 and variants.stock_prior_n(over, "crypto", 10) == 10, "the stock prior count does not apply to crypto")
check("photonics" in (ROOT / "config/variants/stock_calibrated.yaml").read_text(), "the wider list is in the overlay")
check("stock_calibrated: 'Stock calibrated'" in (ROOT / "web/app.js").read_text(), "the variants page names this book")

print("stock calibrated " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
