"""CoinGecko day budget: Kraken names, pacing, live-quote TTL, merged calls, Kraken FX, exit-watch REST pacing. No network.
Usage: python3 test/test_cg_budget.py
"""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "cgbudget.db")
os.environ["TRADING_OFFLINE"] = "1"
os.environ.pop("COINGECKO_API_KEY", None)
sys.path.insert(0, str(HERE))
import common, pricesrc, collector, quotes

fails = []
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond: fails.append(what)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)   # GMT: noon is half the UK day
common._NOW = NOW
con = common.db()

# --- Kraken legacy names: XMR priced from Kraken again -------------------------------------------------
for raw, want in (("XXMRZUSD", "XMRUSD"), ("XMRUSD", "XMRUSD"), ("XMR/USD", "XMRUSD"), ("XTZUSD", "XTZUSD"),
                  ("XCNUSD", "XCNUSD"), ("XAUTUSD", "XAUTUSD"), ("XXBTZGBP", "BTCGBP"), ("XXLMZUSD", "XLMUSD"),
                  ("ZGBPZUSD", "GBPUSD"), ("XDCUSD", "XDCUSD")):
    check(pricesrc.kraken_norm(raw) == want, f"kraken name {raw} -> {want} (got {pricesrc.kraken_norm(raw)})")
# a cached route from the old code still carries norm 'MRUSD'; the parser must use the pair, not that norm
routes = {"XMR": dict(src="kraken", pair="XMRUSD", quote="USD", norm="MRUSD", id="monero"),
          "XTZ": dict(src="kraken", pair="XTZUSD", quote="USD", norm="TZUSD", id="tezos")}
tick = {"error": [], "result": {"XXMRZUSD": {"c": ["310.5", "1"]}, "XTZUSD": {"c": ["0.61", "1"]}}}
got = pricesrc.parse_kraken_ticker(tick, routes, 0.75, common.iso())
check(abs(got.get("XMR", {}).get("usd", 0) - 310.5) < 1e-9, "XMR's XXMRZUSD ticker row prices XMR despite a stale MRUSD norm")
check(abs(got.get("XTZ", {}).get("usd", 0) - 0.61) < 1e-9, "XTZ keeps its leading X")

# --- pacing ------------------------------------------------------------------------------------------
base = {"coingecko": {"day_budget": 300, "month_budget": 9000, "reserve_pct": 0.30}}
paced = {"coingecko": dict(base["coingecko"], day_target=250, pace_slack=25)}
check(pricesrc.pace_line(base, NOW) is None, "no day_target: pacing is off (old behaviour)")
check(abs(pricesrc.pace_line(paced, NOW, "fallback") - (125 + 25)) < 1e-6, "noon GMT: fallback line is 250 x 1/2 + 25")
check(abs(pricesrc.pace_line(paced, NOW, "live") - (150 + 25)) < 1e-6, "noon GMT: live keeps to the full 300 on the same clock")
check(pricesrc.pace_line(paced, NOW, "held") is None, "held coins are never paced")
bst = datetime(2026, 10, 24, 11, 0, tzinfo=timezone.utc)      # 12:00 BST
check(abs(pricesrc.day_frac(bst) - 0.5) < 1e-6, "the day is the UK day (BST)")

def set_calls(n, purpose="fallback"):
    con.execute("DELETE FROM cg_calls"); con.execute("DELETE FROM cg_purpose")
    if n: pricesrc.add_calls(con, NOW, n, purpose)

set_calls(160)
check(not pricesrc.allow(con, paced, NOW, "fallback"), "160 calls by noon: the fallback waits (line 150)")
check(not pricesrc.allow(con, paced, NOW, "scan"), "... and so does the scan")
check(pricesrc.allow(con, paced, NOW, "live"), "... a pre-fill quote may still go (line 175)")
check(pricesrc.allow(con, paced, NOW, "held"), "... a held coin always may")
check(pricesrc.allow(con, base, NOW, "fallback"), "without day_target the same count is allowed (unchanged default)")
set_calls(140)
check(pricesrc.allow(con, paced, NOW, "fallback"), "140 calls by noon: under the line, fallback allowed")
set_calls(299)
check(pricesrc.allow(con, paced, NOW, "held"), "299 calls: held still allowed under the hard cap")
set_calls(300)
check(not pricesrc.allow(con, paced, NOW, "held"), "300 calls: nothing, held included")
set_calls(0)

# --- fixtures: two CoinGecko-only coins, one Kraken coin -------------------------------------------------
cfg = dict(common.cfg())
cfg["coingecko"] = dict(cfg.get("coingecko") or {}, day_budget=300, day_target=250, pace_slack=25, live_ttl_min=15)
rts = {"RARE": dict(src="coingecko", id="rare-coin"), "ODD": dict(src="coingecko", id="odd-coin"),
       "BTC": dict(src="kraken", pair="XBTUSD", quote="USD", norm="BTCUSD", id="bitcoin")}
con.execute("INSERT OR REPLACE INTO state VALUES('price_routes',?)", (json.dumps(dict(ts=common.iso(), routes=rts)),))
con.commit()
pricesrc.tracked = lambda con=None: ["RARE", "ODD", "BTC"]

calls = []
def fake_get(url, params=None, headers=None, tries=1, purpose="other"):
    calls.append((url, dict(params or {}), purpose))
    if "coingecko" in url and "simple/price" in url:
        if collector._BUDGET_CON is not None: pricesrc.add_calls(con, common.now(), 1, purpose)
        return {i: {"usd": 2.0, "last_updated_at": int(common.now().timestamp())} for i in params["ids"].split(",")}
    if "kraken" in url and "Ticker" in url:
        return {"error": [], "result": {"XXBTZUSD": {"c": ["80000", "1"]}, "XXBTZGBP": {"c": ["60000", "1"]}}}
    return {}
real_get = collector.get
collector.get = fake_get

def cg_calls(): return [x for x in calls if "coingecko" in x[0]]

# quotes: a coin priced 5 min ago is not asked again; when one is asked, every CoinGecko-only coin rides along
collector._write_crypto(con, common.iso(NOW - timedelta(minutes=5)), "RARE", dict(usd=2.0, source="coingecko"))
con.commit()
px = {"RARE": dict(klass="crypto", price=1.0, price_usd=1.0)}
quotes._prefetch(con, cfg, ["RARE"], px)
check(cg_calls() == [], "pre-fill: RARE priced 5 min ago -> no CoinGecko call")
calls.clear()
px = {"ODD": dict(klass="crypto", price=1.0, price_usd=1.0)}
quotes._prefetch(con, cfg, ["ODD"], px)
cc = cg_calls()
check(len(cc) == 1 and set(cc[0][1]["ids"].split(",")) == {"odd-coin", "rare-coin"},
      f"pre-fill: ODD unpriced -> one call that also re-prices RARE ({cc})")
check(pricesrc._state(con, "cg_fallback_ts") and pricesrc._state(con, "cg_held_ts"),
      "that call stands in for the collector's fallback and held calls (both marked)")
check(not pricesrc.fallback_due(con, cfg, common.now()), "so the collector's fallback is not due again this cycle")
calls.clear()
quotes._prefetch(con, cfg, ["ODD"], dict(ODD=dict(klass="crypto", price=1.0, price_usd=1.0)))
check(cg_calls() == [], "a second pre-fill list in the same cycle sends nothing")

# collector: held + fallback in one call
con.execute("DELETE FROM prices"); con.execute("DELETE FROM state WHERE key IN ('cg_fallback_ts','cg_held_ts')")
con.commit(); calls.clear()
pricesrc.protected = lambda con=None, now=None: ["RARE"]
collector.price_tracked(con, cfg, common.iso(), {})
cc = cg_calls()
check(len(cc) == 1 and set(cc[0][1]["ids"].split(",")) == {"odd-coin", "rare-coin"} and cc[0][2] == "held",
      f"collector: held RARE and fallback ODD share one simple/price ({[(x[1].get('ids'), x[2]) for x in cc]})")

# --- FX from Kraken, CoinGecko only as backup ---------------------------------------------------------
check(abs(collector.kraken_fx({"result": {"XXBTZGBP": {"c": ["60000", "1"]}, "XXBTZUSD": {"c": ["80000", "1"]}}}) - 0.75) < 1e-9,
      "Kraken BTC GBP / USD gives the USD->GBP rate")
check(collector.kraken_fx({"result": {"XXBTZUSD": {"c": ["80000", "1"]}}}) is None, "a missing leg is no rate")
check(collector.kraken_fx(None) is None, "no response is no rate")

# --- exit watcher REST loop for a held CoinGecko-only coin --------------------------------------------
import exit_watch
pool = []
pricesrc.backup_quotes = lambda con, c, syms, fetch, ts, limit=None: (pool.append(syms[0]) or {syms[0]: dict(usd=2.0, source="geckoterminal")})
con.execute("DELETE FROM prices"); con.commit(); calls.clear()
plan = dict(kraken=[], coinbase=[], rest=[dict(asset="ODD", kind="coingecko", route=rts["ODD"])])
got = []
t0 = NOW
for k in range(45):   # 15 minutes of 20-second passes
    common._NOW = t0 + timedelta(seconds=20 * k)
    exit_watch._rest_once(con, cfg, plan, 0.75, lambda *a: got.append(a))
cc = cg_calls()
check(len(cc) == 1, f"exit watch: one CoinGecko call per coin per 15 min, not one per 20 s ({len(cc)})")
check(len(pool) <= 15, f"exit watch: pool quotes in between at most once a minute ({len(pool)})")
check(len(got) >= 15, f"exit watch still delivers a price every minute ({len(got)})")
common._NOW = t0 + timedelta(minutes=15, seconds=5)
calls.clear()
exit_watch._rest_once(con, cfg, plan, 0.75, lambda *a: None)
check(len(cg_calls()) == 1, "after live_ttl_min the next CoinGecko quote goes out")
# with live_ttl_min unset the old behaviour (a quote each pass) is unchanged
old = dict(cfg); old["coingecko"] = {k: v for k, v in cfg["coingecko"].items() if k not in ("live_ttl_min", "day_target")}
exit_watch._CG_LAST.clear(); calls.clear()
for k in range(3):
    common._NOW = t0 + timedelta(hours=1, seconds=20 * k)
    exit_watch._rest_once(con, old, plan, 0.75, lambda *a: None)
check(len(cg_calls()) == 3, "no live_ttl_min: one quote per pass as before")

collector.get = real_get
print("\nFAILED: %d" % len(fails) if fails else "\nall ok")
sys.exit(1 if fails else 0)
