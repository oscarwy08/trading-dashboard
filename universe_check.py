"""Re-verify the stock universe (config stocks.universe + riskpot stock universe + watchlist), v5 (normal exchange tickers):
every ticker must return >= 100 Yahoo daily closes (about 5 months; recent listings/SPACs with less are flagged 'young')
and enough liquidity (20-day average traded value >= $1M for US/ASX, >= GBP 0.2M for London - Yahoo quotes London in
pence, converted here). Prints currency, exchange and whether the exchange is open now. Read-only (no DB writes).
Usage: python3 universe_check.py"""
import time, requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from common import cfg
from stockrank import themes, risk_stocks
import markets
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0 (paper-trader)"

def chk(sym):
    for i in range(3):
        try:
            r = S.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": "1y", "interval": "1d"}, timeout=20)
            if r.status_code == 429: time.sleep(3 * (i + 1)); continue
            d = r.json()["chart"]
            if d.get("error"): return sym, 0, None, None, str(d["error"].get("description"))[:50]
            res = d["result"][0]; m = res["meta"]; q = res["indicators"]["quote"][0]
            n = len([x for x in q["close"] if x]); cur = m.get("currency")
            v = [c * vv for c, vv in zip(q["close"], q["volume"]) if c and vv][-20:]
            liq = (sum(v) / len(v) if v else 0) / (100 if cur == "GBp" else 1)
            return sym, n, cur, liq, f"{m.get('fullExchangeName')} {m.get('longName') or m.get('shortName')}"
        except Exception as e: err = str(e)
    return sym, 0, None, None, err

c = cfg(); th = themes(c); uni = list(dict.fromkeys(list(th) + risk_stocks(c) + list(c["stocks"]["watchlist"])))
bad, young = [], []
with ThreadPoolExecutor(6) as ex: res = list(ex.map(chk, uni))
now = datetime.now(timezone.utc)
for sym, n, cur, liq, info in res:
    floor = 0.2e6 if markets.exchange(sym) == "LSE" else 1e6
    ok = n >= 100 and (liq or 0) >= floor; y = 0 < n < 100
    if y: young.append(sym)
    if not ok and not (y and (liq or 0) >= floor): bad.append(sym)
    print(f"{sym:8} {th.get(sym, ('', 'watchlist/risk'))[1][:30]:30} days={n:3} {cur or '-':4} liq/day={(liq or 0)/1e6:7.2f}M "
          f"{markets.exchange(sym):3} {'open' if markets.is_open(sym, now) else 'closed':6} {info} {'OK' if ok else 'YOUNG' if y else 'FAIL'}")
print(f"universe {len(uni)}; failing: {bad or 'none'}; young (< 100 days, kept if liquid): {young or 'none'}")
