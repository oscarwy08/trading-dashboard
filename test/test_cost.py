"""DEX liquidity cost (11 Oct 2026 Kyber £5/£8).
Deep books stay at the 0.05% floor, an unknown cap stays at 0.30%, a thin cap sits on the 0.40% ceiling.
Venue volume must not lift a mid-cap onto that ceiling: turn_thin is 0 because stored vol24 is venue volume.
Usage: python3 test/test_cost.py
"""
import os, sys, tempfile
from pathlib import Path
TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "cost.db")
os.environ["TRADING_LOG"] = str(TMP / "cost.log")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import trader

c = trader.cfg()
K = c["risk"]["liquidity_cost"]
assert K["major_pct"] == 0.05 and K["thin_pct"] == 0.40 and K["turn_thin"] == 0, "calibration params"

q = lambda mcap, vol, impact=None: dict(mcap=mcap, vol24=vol, price=1, price_usd=1.3, **({} if impact is None else {"impact_pct": impact}))
deep = trader.crypto_fee_pct(c, q(5e11, 2e11), 8)
thin = trader.crypto_fee_pct(c, q(2e7, 6e5), 8)
unk = trader.crypto_fee_pct(c, {}, 8)
assert deep < unk < thin and abs(unk - c["risk"]["fee_slippage_pct"] / 100) < 1e-12, "deep < 0.30% < thin, unknown stays 0.30%"
assert abs(deep - 0.0005) < 1e-12 and abs(thin - 0.004) < 1e-12, "floor 0.05%, ceiling 0.40%"

# $150m, tiny venue volume: the live case that used to sit near 1% per side.
mid = trader.crypto_fee_pct(c, q(1.5e8, 1e3), 8)
same = trader.crypto_fee_pct(c, q(1.5e8, 0.04 * 1.5e8), 8)
assert mid < 0.005 and abs(mid - same) < 1e-12, "a mid-cap stays under 0.50% and venue turnover does not lift it"
hot = trader.crypto_fee_pct(c, q(1.5e8, 0.40 * 1.5e8), 8)
assert abs(hot - mid * 0.7) < 1e-12, "turnover at or above turn_deep still trims the curve"

clamped = trader.crypto_fee_pct(c, q(1.5e8, 1e3, 38.0), 8)
assert abs(clamped - 0.004) < 1e-12, "a stale Kyber impact is clamped to the thin ceiling"
print(f"cost calibration ok  deep {deep*100:.3f}%  mid {mid*100:.3f}%  thin {thin*100:.3f}%")
