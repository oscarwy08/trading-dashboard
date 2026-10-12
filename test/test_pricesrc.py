"""Price router, exchange fallback, and the CoinGecko day budget. No network.
Usage: python3 test/test_pricesrc.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "pricesrc.db")
os.environ["TRADING_OFFLINE"] = "1"
os.environ.pop("COINGECKO_API_KEY", None)
sys.path.insert(0, str(HERE))
import common, pricesrc, collector

fails = []
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond: fails.append(what)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
common._NOW = NOW

# --- pair names ---
for raw, want in (("XXBTZUSD", "BTCUSD"), ("XBTUSD", "BTCUSD"), ("XETHZUSD", "ETHUSD"),
                  ("ETHUSD", "ETHUSD"), ("XDGUSD", "DOGEUSD"), ("XXDGZUSD", "DOGEUSD"), ("SOLUSD", "SOLUSD"),
                  ("XXRPZUSD", "XRPUSD")):
    check(pricesrc.kraken_norm(raw) == want, f"kraken name {raw} -> {want} (got {pricesrc.kraken_norm(raw)})")

kidx = pricesrc.kraken_index({
    "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "status": "online"},
    "XBTGBP": {"altname": "XBTGBP", "wsname": "XBT/GBP", "status": "online"},
    "ETHUSD": {"altname": "ETHUSD", "wsname": "ETH/USD", "status": "online"},
    "DARK": {"altname": "BTCUSD.d", "wsname": "BTC/USD.d", "status": "online"},
    "OFF": {"altname": "SOLUSD", "wsname": "SOL/USD", "status": "cancel_only"},
})
cidx = pricesrc.coinbase_index([
    {"id": "BTC-USD", "base_currency": "BTC", "quote_currency": "USD", "status": "online"},
    {"id": "SOL-USD", "base_currency": "SOL", "quote_currency": "USD", "status": "online"},
    {"id": "ETH-GBP", "base_currency": "ETH", "quote_currency": "GBP", "status": "online"},
    {"id": "DOGE-USD", "base_currency": "DOGE", "quote_currency": "USD", "status": "online", "trading_disabled": True},
])
check(kidx["BTC"]["quote"] == "USD" and kidx["BTC"]["pair"] == "XBTUSD", "Kraken prefers USD over GBP")
check("SOL" not in kidx, "offline Kraken pairs are skipped")
check(cidx["SOL"]["pair"] == "SOL-USD" and "DOGE" not in cidx, "Coinbase skips a disabled market")
btc = pricesrc.choose("BTC", kidx, cidx, None, "bitcoin")
sol = pricesrc.choose("SOL", kidx, cidx, None, "solana")
mon = pricesrc.choose("MON", kidx, cidx, {"token": "0xabc", "dec": 18, "mon": True}, "monad")
rare = pricesrc.choose("RARE", kidx, cidx, None, "rare-coin")
check(btc["src"] == "kraken" and btc["alt"]["src"] == "coinbase" and btc["id"] == "bitcoin", "BTC listed on both uses Kraken, Coinbase is the alt")
check(sol["src"] == "coinbase" and "alt" not in sol, "SOL only on Coinbase uses Coinbase")
check(mon["src"] == "kyber" and mon["mon"] and mon["token"] == "0xabc", "an unlisted Monad token uses KyberSwap")
check(rare["src"] == "coingecko" and rare["id"] == "rare-coin", "anything else is CoinGecko")
check(abs(pricesrc.to_usd(74, "GBP", 0.74) - 100) < 1e-9, "a GBP pair converts with the stored USD->GBP rate")
check(pricesrc.to_usd(74, "GBP", None) is None, "GBP with no FX rate is not a price")
check(pricesrc.to_usd(3, "USDT", 0.74) == 3, "USDT is treated as USD")

# --- one Kraken call, then fallback ---
routes = {
    "BTC": dict(src="kraken", pair="XBTUSD", quote="USD", norm="BTCUSD", id="bitcoin",
                alt=dict(src="coinbase", pair="BTC-USD", quote="USD")),
    "ETH": dict(src="kraken", pair="ETHGBP", quote="GBP", norm="ETHGBP", id="ethereum"),
    "SOL": dict(src="coinbase", pair="SOL-USD", quote="USD", id="solana"),
    "MON": dict(src="kyber", token="0xabc", dec=18, mon=True, id="monad"),
    "RARE": dict(src="coingecko", id="rare-coin"),
}
calls = []
def fetch(url, params=None, headers=None, tries=1):
    calls.append(url)
    if url.endswith("/Ticker"):
        return {"error": [], "result": {
            "XXBTZUSD": {"c": ["50000", "1"], "h": ["1", "51000"], "l": ["1", "49000"], "v": ["1", "10"]},
            "XETHZGBP": {"c": ["1850", "1"], "h": ["1", "1900"], "l": ["1", "1800"], "v": ["1", "4"]},
        }}
    if url.endswith("/SOL-USD/ticker"):
        return {"price": "150", "time": "2026-10-11T11:59:00Z", "volume": "20"}
    return None
paced = []
priced, missed = pricesrc.quote_exchanges(
    ["BTC", "ETH", "SOL", "MON", "RARE"], routes, 0.74, fetch, lambda route: 0.25, "2026-10-11T12:00:00+00:00", pace=lambda s: paced.append(s))
check(sum(1 for u in calls if u.endswith("/Ticker")) == 1, "one Kraken Ticker call covers every Kraken coin")
check(not any("coingecko" in u for u in calls), "the exchange pass does not call CoinGecko")
check(priced["BTC"]["source"] == "kraken" and abs(priced["BTC"]["usd"] - 50000) < 1e-6, "BTC last price from the Kraken batch")
check(abs(priced["BTC"]["vol"] - 500000) < 1e-6, "Kraken base volume is stored in USD")
check(abs(priced["ETH"]["usd"] - 1850 / 0.74) < 1e-6, "ETH GBP ticker converted to USD")
check(priced["SOL"]["source"] == "coinbase" and abs(priced["SOL"]["usd"] - 150) < 1e-9, "SOL from its Coinbase ticker")
check(priced["SOL"]["src_ts"].startswith("2026-10-11T11:59:00"), "Coinbase time is the provider timestamp")
check(priced["MON"]["source"] == "kyberswap" and abs(priced["MON"]["usd"] - 0.25) < 1e-9, "MON from the Kyber route")
check(missed == [] and "RARE" not in priced, "a CoinGecko coin is left for the one fallback call")
check(paced == [0.11], "Coinbase tickers are paced")

calls.clear()
def fetch_down(url, params=None, headers=None, tries=1):
    calls.append(url)
    if "kraken" in url: return None
    if url.endswith("/BTC-USD/ticker"): return {"price": "49900", "time": "2026-10-11T11:58:00Z", "volume": "1"}
    if url.endswith("/SOL-USD/ticker"): return None
    return None
priced, missed = pricesrc.quote_exchanges(
    ["BTC", "ETH", "SOL", "MON"], routes, 0.74, fetch_down, lambda route: None, "2026-10-11T12:00:00+00:00", pace=lambda s: None)
check(priced.get("BTC", {}).get("source") == "coinbase" and abs(priced["BTC"]["usd"] - 49900) < 1e-6, "Kraken down: BTC uses its Coinbase alt")
check("ETH" in missed and "SOL" in missed and "MON" in missed, "a failed alt, a failed Coinbase coin and a failed Kyber quote are misses")
check(sum(1 for u in calls if u.endswith("/Ticker")) == 1, "a failed Kraken batch is still one call")

# --- candles ---
ohlc = pricesrc.parse_kraken_ohlc({"result": {"XXBTZUSD": [[1700000000, "1", "2", "0.5", "9", "0", "1", 1]], "last": 1}},
                                  {"pair": "XBTUSD", "norm": "BTCUSD"})
check(ohlc == [(1700000000, 9.0)], f"Kraken OHLC close (got {ohlc})")
cb = pricesrc.parse_coinbase_candles([[1700000000, 1, 3, 2, 2.5, 10]])
check(cb == [(1700000000, 2.5)], f"Coinbase candle close (got {cb})")
usd = [(t, pricesrc.to_usd(p, "GBP", 0.5)) for t, p in ohlc]
check(abs(usd[0][1] - 18) < 1e-9, "OHLC close converts with FX")

cg = pricesrc.parse_cg_simple({"bitcoin": {"usd": 10, "last_updated_at": 1700000000}}, {"bitcoin": "BTC"}, "2026-10-11T12:00:00+00:00")
check(cg["BTC"]["source"] == "coingecko" and cg["BTC"]["usd"] == 10 and cg["BTC"]["src_ts"].endswith("+00:00"), "simple/price keeps the provider time")

# --- budget ---
con = common.db()
c = {"coingecko": {"day_budget": 300, "month_budget": 9000, "tight_pct": 0.70,
                   "scan_every_min": 180, "scan_every_min_tight": 360,
                   "fallback_every_min": 15, "fallback_every_min_tight": 60}}
check(pricesrc.day_key(datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc)) == "2026-10-11", "UK midnight rolls the budget day (BST)")
common._NOW = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)   # still 10 Oct in London
check(pricesrc.day_key(common.now()) == "2026-10-10", "22:00 UTC is still the 10th in London")
pricesrc.add_calls(con, datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc), 5)
check(pricesrc.usage(con, datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)) == (0, 5), "the rolled day does not count against the 10th")
check(pricesrc.usage(con, datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc))[0] == 5, "those calls sit on the 11th")
con.execute("DELETE FROM cg_calls"); con.commit()
common._NOW = NOW
pricesrc.add_calls(con, NOW, 209)
check(not pricesrc.tight(con, c, NOW) and pricesrc.allow(con, c, NOW, "scan"), "209 of 300 is not tight")
pricesrc.add_calls(con, NOW, 1)
check(pricesrc.tight(con, c, NOW) and not pricesrc.allow(con, c, NOW, "scan"), "70% stops the top-300 scan")
check(not pricesrc.allow(con, c, NOW, "chart") and not pricesrc.allow(con, c, NOW, "fx") and not pricesrc.allow(con, c, NOW, "meta"), "charts, FX and category refreshes stop with the scan")
check(not pricesrc.allow(con, c, NOW, "fallback"), "the general fallback cannot spend the reserved 30%")
check(pricesrc.allow(con, c, NOW, "held") and pricesrc.allow(con, c, NOW, "live"), "held and pre-trade quotes keep the reserve")
con.execute("DELETE FROM cg_calls"); con.execute("DELETE FROM cg_purpose")
con.execute("DELETE FROM state WHERE key='cg_fallback_ts'"); con.commit()
check(pricesrc.fallback_due(con, c, NOW), "no prior fallback is due")
# Tight at 40% still leaves the reserved slice. The general fallback then waits an hour, and still may not cross the reserve.
c_cadence = {"coingecko": dict(c["coingecko"], tight_pct=0.40)}
pricesrc.add_calls(con, NOW, 120)
pricesrc.mark(con, "cg_fallback_ts", NOW.isoformat())
check(pricesrc.tight(con, c_cadence, NOW) and pricesrc.allow(con, c_cadence, NOW, "fallback"), "tight, but the reserve is still untouched")
check(not pricesrc.fallback_due(con, c_cadence, NOW + timedelta(minutes=16)), "once tight, the fallback waits an hour")
check(pricesrc.fallback_due(con, c_cadence, NOW + timedelta(minutes=61)), "the fallback runs again after that hour")
con.execute("DELETE FROM cg_calls"); con.commit()
pricesrc.add_calls(con, NOW, 299)
check(not pricesrc.allow(con, c, NOW, "fallback") and pricesrc.allow(con, c, NOW, "held") and pricesrc.allow(con, c, NOW, "live"), "one under the cap still allows a held quote, not the general fallback")
pricesrc.add_calls(con, NOW, 1)
check(not pricesrc.allow(con, c, NOW, "fallback") and not pricesrc.allow(con, c, NOW, "live") and not pricesrc.allow(con, c, NOW, "held"), "the hard daily cap blocks every CoinGecko call")
check(not pricesrc.scan_due(con, c, NOW), "a full budget means the scan is not due")

# collector.get must refuse before the socket when the budget is bound
hit = []
class R:
    status_code = 200
    headers = {}
    def json(self): return {"ok": 1}
    def raise_for_status(self): pass
def fake_http(url, params=None, headers=None, timeout=20):
    hit.append(url); return R()
collector.S.get = fake_http
collector.bind_budget(con, c)
try:
    assert collector.get(collector.CG + "/ping", tries=1, purpose="fallback") is None
    check(hit == [], "a capped CoinGecko call is not sent")
    con.execute("DELETE FROM cg_calls"); con.commit()
    pricesrc.add_calls(con, NOW, 210)
    hit.clear()
    check(collector.get(collector.CG + "/ping", tries=1, purpose="scan") is None and hit == [], "a tight budget skips the scan call")
    check(collector.get(collector.CG + "/ping", tries=1, purpose="live")["ok"] == 1 and len(hit) == 1, "a live quote is still sent")
    check(pricesrc.usage(con, NOW)[0] == 211, "only the call that was sent is counted")
    hit.clear()
    check(collector.get("https://api.kraken.com/0/public/Ticker", tries=1)["ok"] == 1, "Kraken is not part of the CoinGecko budget")
    check(pricesrc.usage(con, NOW)[0] == 211, "an exchange call does not increment the counter")
finally:
    collector.bind_budget(None, None)

# --- router choice is cached for a day; a new symbol waits ---
con.execute("DELETE FROM cg_calls"); con.commit()
book_calls = []
def books(url, params=None, headers=None, tries=1):
    book_calls.append(url)
    if "AssetPairs" in url:
        return {"result": {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "status": "online"},
                           "XETHZUSD": {"altname": "ETHUSD", "wsname": "ETH/USD", "status": "online"}}}
    if url.endswith("/products"):
        return [{"id": "SOL-USD", "base_currency": "SOL", "quote_currency": "USD", "status": "online"},
                {"id": "BTC-USD", "base_currency": "BTC", "quote_currency": "USD", "status": "online"}]
    return None
routes = pricesrc.ensure_routes(con, {}, ["BTC", "ETH", "SOL", "NOPE"], books)
check(routes["BTC"]["src"] == "kraken" and routes["ETH"]["src"] == "kraken" and routes["SOL"]["src"] == "coinbase", "books map listed coins")
check(routes["BTC"]["alt"]["pair"] == "BTC-USD", "BTC keeps a Coinbase alt")
check(routes["NOPE"]["src"] == "coingecko", "an unlisted coin is CoinGecko")
nbooks = len(book_calls)
again = pricesrc.ensure_routes(con, {}, ["BTC", "ETH", "SOL", "NOPE", "NEW"], books)
check(len(book_calls) == nbooks and again["NEW"]["src"] == "coingecko", "a symbol that appears later in the day does not refetch the books")
# both books empty: do not pin a fresh all-CoinGecko map
con.execute("DELETE FROM state WHERE key='price_routes'"); con.commit()
empty = pricesrc.ensure_routes(con, {}, ["BTC"], lambda *a, **k: None)
check(empty["BTC"]["src"] == "coingecko", "with no books yet the coin is CoinGecko for this cycle")
check(pricesrc.load_routes(con) == {}, "a failed book refresh is not cached for the day")

# --- tracked coins: Kraken batch, and a miss falls straight through to one simple/price ---
ts = NOW.isoformat()
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 0.74, "test", None, None))
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, "BTC", "crypto", 1, 2, 0.5, 9, 5e11, "coingecko", ts))
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, "RARE", "crypto", 1, None, None, None, 8e7, "coingecko", ts))
con.execute("INSERT OR REPLACE INTO state VALUES('cgid:BTC', 'bitcoin')")
con.execute("INSERT OR REPLACE INTO state VALUES('cgid:RARE', 'rare-coin')")
con.execute("DELETE FROM state WHERE key='price_routes'")
con.execute("DELETE FROM state WHERE key='cg_fallback_ts'"); con.commit()
seen = []
def routed(url, params=None, headers=None, tries=3, purpose="other"):
    seen.append((url, purpose, params))
    if "AssetPairs" in (url or ""):
        return {"result": {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "status": "online"}}}
    if str(url).endswith("/products"):
        return [{"id": "BTC-USD", "base_currency": "BTC", "quote_currency": "USD", "status": "online"}]
    if str(url).endswith("/Ticker"):
        return None
    if "BTC-USD/ticker" in str(url):
        return None
    if "simple/price" in str(url):
        return {"bitcoin": {"usd": 42000, "last_updated_at": 1760000000}, "rare-coin": {"usd": 1.5, "last_updated_at": 1760000000}}
    return None
collector.get = routed
status = {}
wrote = collector.price_tracked(con, {}, ts, status)
src = {r[0]: r[1] for r in con.execute("SELECT asset, source FROM prices WHERE ts=? AND source!='backfill'", (ts,))}
# the inserted rows share ts with the seed rows; the latest rowid is the one just written
latest = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
    """SELECT asset, source, mcap, price FROM prices WHERE ts=? ORDER BY rowid""", (ts,))}
# last write wins
last = {}
for asset, source, mcap, price in con.execute("SELECT asset, source, mcap, price FROM prices WHERE ts=? ORDER BY rowid", (ts,)):
    last[asset] = (source, mcap, price)
check(last["BTC"][0] == "coingecko" and abs(last["BTC"][2] - 42000) < 1e-6, "Kraken and Coinbase both down: BTC falls back to CoinGecko immediately")
check(last["BTC"][1] == 5e11, "the fallback row keeps the last market cap")
check(last["RARE"][0] == "coingecko" and abs(last["RARE"][2] - 1.5) < 1e-9, "an unlisted coin is in the same simple/price call")
check(sum(1 for u, p, _ in seen if "simple/price" in u and p == "fallback") == 1, "one CoinGecko simple/price for the miss and the uncovered coin")
check("kraken" in status.get("prices", "") or "CoinGecko" in status.get("prices", ""), f"status names the sources ({status.get('prices')})")
# next cycle Kraken works, and the fallback interval has not elapsed, so RARE is not re-fetched
seen.clear()
def routed_up(url, params=None, headers=None, tries=3, purpose="other"):
    seen.append((url, purpose))
    if str(url).endswith("/Ticker"):
        return {"result": {"XXBTZUSD": {"c": ["43000", "1"], "h": ["1", "44000"], "l": ["1", "42000"], "v": ["1", "2"]}}}
    if "simple/price" in str(url):
        return {"rare-coin": {"usd": 9, "last_updated_at": 1760000000}}
    return None
collector.get = routed_up
ts2 = (NOW + timedelta(minutes=5)).isoformat()
common._NOW = NOW + timedelta(minutes=5)
collector.price_tracked(con, c, ts2, {})
check(not any("simple/price" in u for u, _ in seen), "inside the fallback interval an uncovered coin waits, and a Kraken success needs no CoinGecko")
row = con.execute("SELECT source, price, mcap FROM prices WHERE asset='BTC' AND ts=? ORDER BY rowid DESC LIMIT 1", (ts2,)).fetchone()
check(row["source"] == "kraken" and abs(row["price"] - 43000) < 1e-6 and row["mcap"] == 5e11, "the Kraken row keeps market cap from the previous print")

est = pricesrc.estimate({"BTC": {"src": "kraken"}, "SOL": {"src": "coinbase"}, "RARE": {"src": "coingecko"}})
check(est["uncovered"] == 1 and est["cg_per_day"] < 300 and est["cg_per_month"] < 10000, f"the steady cadence stays inside the month ({est})")

# --- reserve is only for held / armed simple-price; purpose counts are stored ---
con.execute("DELETE FROM cg_calls"); con.execute("DELETE FROM cg_purpose"); con.commit()
pricesrc.add_calls(con, NOW, 40, "scan")
pricesrc.add_calls(con, NOW, 12, "fx")
pricesrc.add_calls(con, NOW, 3, "held")
got = pricesrc.purpose_counts(con, NOW)
check(got == {"scan": 40, "fx": 12, "held": 3} and pricesrc.usage(con, NOW)[0] == 55, f"purpose counts add up ({got})")
st = pricesrc.stats(con, NOW, c)
check(st["purpose"]["scan"] == 40 and st["reserve_pct"] == 0.30, "the dashboard payload names what spent the budget")
con.execute("INSERT INTO positions VALUES('risk','STONK','crypto',1,1,1,?,1)", (NOW.isoformat(),))
con.execute("""INSERT INTO armed_entries(ts,expires,sleeve,sub,setup,asset,account,klass,trigger,level,size_gbp,stop_pct,target_pct,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat(), "day", "burst", "24h", "PEND", "crypto", "crypto",
             "break_above", 1, 5, 12, 15, "armed"))
con.commit()
check(set(pricesrc.protected(con, NOW)) == {"STONK", "PEND"}, f"held and armed coins are protected ({pricesrc.protected(con, NOW)})")

# --- pool backup: most liquid pool, rejected when it is far from the last good price ---
ds = {"pairs": [
    {"chainId": "ethereum", "priceUsd": "0.10", "liquidity": {"usd": 1000}, "baseToken": {"address": "0xlow", "symbol": "STONK"}},
    {"chainId": "ethereum", "priceUsd": "0.27", "liquidity": {"usd": 90000}, "baseToken": {"address": "0xgood", "symbol": "STONK"}},
    {"chainId": "ethereum", "priceUsd": "5", "liquidity": {"usd": 500000}, "baseToken": {"address": "0xother", "symbol": "OTHER"}},
]}
gt = {"data": [{"attributes": {"name": "STONK / WETH", "base_token_price_usd": "0.268", "reserve_in_usd": "150000"},
                "relationships": {"base_token": {"data": {"id": "eth_0xstonk"}}}}]}
picked = pricesrc._liq_pick(pricesrc.parse_ds_pairs(ds, "STONK") + pricesrc.parse_gt_pools(gt, "STONK"))
check(picked["source"] == "geckoterminal" and abs(picked["usd"] - 0.268) < 1e-9 and picked["address"] == "0xstonk", "the most liquid STONK pool wins")
check(pricesrc.sane_price(0.27, 0.268, 2) and not pricesrc.sane_price(2.0, 0.268, 2), "a pool many times the last good price is rejected")
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (NOW.isoformat(), "STONK", "crypto", 0.268, None, None, None, None, "coingecko", NOW.isoformat()))
con.execute("DELETE FROM state WHERE key='dex_tokens'"); con.commit()
calls = []
def dex_fetch(url, params=None, headers=None, tries=1):
    calls.append(url)
    if "search/pools" in url: return gt
    if url.endswith("/search"): return ds
    return None
rows = pricesrc.backup_quotes(con, {"dex": {"max_jump": 2, "max_per_cycle": 4}}, ["STONK", "BTC"], dex_fetch, NOW.isoformat())
check(rows["STONK"]["source"] == "geckoterminal" and abs(rows["STONK"]["usd"] - 0.268) < 1e-9, "STONK is priced from GeckoTerminal when CoinGecko missed")
check("BTC" not in rows or rows.get("BTC", {}).get("source") != "geckoterminal", "backup does not invent a BTC pool in this fixture")
saved = pricesrc._dex_cache(con)
check(saved.get("STONK", {}).get("address") == "0xstonk", "the contract is remembered for the next lookup")
wild = {"pairs": [{"chainId": "ethereum", "priceUsd": "9", "liquidity": {"usd": 10}, "baseToken": {"address": "0xbad", "symbol": "WILD"}}]}
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (NOW.isoformat(), "WILD", "crypto", 1.0, None, None, None, None, "coingecko", NOW.isoformat()))
con.commit()
none = pricesrc.backup_quotes(con, {"dex": {"max_jump": 2, "max_per_cycle": 2}}, ["WILD"], lambda *a, **k: wild, NOW.isoformat())
check(none == {}, "a 9x pool is not stored against the last good price")

print("FAILS:", len(fails))
if fails:
    for f in fails: print(" -", f)
    sys.exit(1)
print("[test_pricesrc] OK")
