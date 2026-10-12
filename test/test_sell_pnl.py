"""Realised P&L on a sell: pounds and percent of the cost basis, plus the peak when the exit recorded one.
Usage: python3 test/test_sell_pnl.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TRADING_OFFLINE", "1")
from dashboard import peak_pct_of, sell_basis_pct, sell_text

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

# cost basis £5.00, profit £0.12 after fees -> +2.4%. value = basis + gas + pnl.
check(abs(sell_basis_pct(5.121, 0.001, 0.12) - 2.4) < 1e-9, "£0.12 on a £5 cost basis is +2.4%")
check(sell_text(0.12, 2.4, 6.1) == "+£0.12 (+2.4%), peak +6.1%", "the line reads '+£0.12 (+2.4%), peak +6.1%'")
check(sell_text(-0.40, -8.0) == "-£0.40 (-8.0%)", "a loss keeps the minus on both numbers")
check(sell_text(0.12, 2.4, None, "BTC") == "BTC +£0.12 (+2.4%)", "a coin name can lead the line")
reason = "trailing stop +4.2% (peak +6.1%, gives back 20% of the gain); P&L £+0.12"
check(peak_pct_of(reason) == 6.1, "a trailing exit's reason yields the peak")
check(peak_pct_of("trailing take-profit: -1.0% from peak (peak +15.0%), now +12.0%") == 15.0, "the risk trailing reason yields the peak")
check(peak_pct_of("stop-loss -2.5% <= -2.0% (ATR-scaled); P&L £-0.20") is None, "a stop that never named a peak has none")
check(sell_basis_pct(None, 0, 0.1) is None, "no fill value means no percent")
check(sell_basis_pct(0.12, 0, 0.12) is None, "a zero cost basis is left blank")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
