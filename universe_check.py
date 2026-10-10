"""Re-verify the CORE stock universe (config stocks.universe): every ticker must be listed in the public xStocks assets
list (api.xstocks.fi, all pages), return a live xStocks quote, and have >= 100 Yahoo daily closes (6 months). Also lists
whether it is deployed on Monad. Read-only (no DB writes). Usage: python3 universe_check.py"""
import time, requests
from common import cfg
from stockrank import themes
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0 (paper-trader)"
listed, p = {}, 0
while True:
    d = S.get("https://api.xstocks.fi/api/v2/public/assets", params={"page": p}, timeout=30).json()
    for x in d["nodes"]: listed[x["symbol"]] = x
    if not d["page"].get("hasNextPage") or p > 50: break
    p += 1
bad = []
for s, (k, lbl) in themes(cfg()).items():
    x = listed.get(s); q = n = None
    if x:
        try: q = S.get(f"https://api.xstocks.fi/api/v2/public/assets/{s}/price-data", timeout=20).json().get("quote")
        except Exception: pass
        try:
            r = S.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{s[:-1]}", params={"range": "6mo", "interval": "1d"}, timeout=20).json()
            n = len([c for c in r["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c])
        except Exception: n = 0
        time.sleep(0.3)
    ok = bool(x and q and n and n >= 100)
    if not ok: bad.append(s)
    print(f"{s:8} {lbl:28} listed={bool(x)} quote={q} yahoo_days={n} monad={bool(x and any(dp['network'] == 'Monad' for dp in x.get('deployments', [])))} {'OK' if ok else 'FAIL'}")
print(f"{len(listed)} xStocks listed; universe {len(themes(cfg()))}; failing: {bad or 'none'}")
