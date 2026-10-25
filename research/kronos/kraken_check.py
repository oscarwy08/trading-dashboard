"""Cross-check the Binance 1h candles used by the replay against Kraken's public OHLC.
Kraken's public OHLC endpoint only returns the most recent 720 bars (30 days at 1h), which is
why the months-long replay uses Binance's public mirror. This compares hourly returns on the overlap."""
import json, math, os, sys, requests
from pathlib import Path
WORK = Path(os.environ.get("KRONOS_WORK", "/workspace/kronos_eval"))
PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD", "ADA": "ADAUSD",
         "DOGE": "XDGUSD", "LINK": "LINKUSD", "AVAX": "AVAXUSD", "DOT": "DOTUSD", "SUI": "SUIUSD"}

def corr(x, y):
    n = len(x); mx, my = sum(x) / n, sum(y) / n
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))

for coin in (sys.argv[1].split(",") if len(sys.argv) > 1 else PAIRS):
    r = requests.get("https://api.kraken.com/0/public/OHLC", params=dict(pair=PAIRS[coin], interval=60), timeout=20).json()
    if r.get("error"): print(coin, r["error"]); continue
    k = {int(row[0]) * 1000: float(row[4]) for row in next(v for kk, v in r["result"].items() if kk != "last")}
    b = {row[0]: row[4] for row in json.loads((WORK / "data" / f"{coin}.json").read_text())}
    ts = sorted(t for t in k if t in b and t - 3600000 in k and t - 3600000 in b)
    rk = [k[t] / k[t - 3600000] - 1 for t in ts]; rb = [b[t] / b[t - 3600000] - 1 for t in ts]
    gap = sum(abs(k[t] / b[t] - 1) for t in ts) / len(ts)
    print(json.dumps(dict(coin=coin, overlap_bars=len(ts), ret_corr=round(corr(rk, rb), 4), mean_abs_price_gap_pct=round(gap * 100, 3))), flush=True)
