"""trend_carry_unlock and insider_rs_stocks. No network. The main book flags stay off.

Usage: python3 test/test_carry_insider.py
"""
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("TRADING_VARIANT", None)
os.environ.pop("FINNHUB_API_KEY", None)
os.environ.pop("FINNHUB_TOKEN", None)
os.environ.pop("TOKENOMIST_API_KEY", None)

import common
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

# --- main book is unchanged; the new books hold overnight ---
R = variants.rules_of(common.cfg())
check(R["hold_overnight"] is False and R["core_off"] is False and not R["day_risk_cap_pct"],
      "the main book still flattens and still runs core")
os.environ["TRADING_VARIANT"] = "trend_carry_unlock"
carry_rules = variants.rules_of(common.cfg())
os.environ["TRADING_VARIANT"] = "insider_rs_stocks"
ins_rules = variants.rules_of(common.cfg())
os.environ.pop("TRADING_VARIANT", None)
check(carry_rules["hold_overnight"] is True and ins_rules["hold_overnight"] is True,
      "both new books hold through the night")
check("trend_carry_unlock" in common.cfg()["variants"]["run"]
      and "insider_rs_stocks" in common.cfg()["variants"]["run"],
      "the cycle list includes both books")
check("trend_carry_unlock" in strategies.PURE and "insider_rs_stocks" in strategies.PURE,
      "both books run as strategy books, not the main trader")
names = strategies._equity_universe()
check("AAPL" in names and "FRES.L" in names and "LYC.AX" not in names,
      "the stock book is US and UK names, without the Australian listing")

# --- funding: no data means no change; 25% or a jump into the top tenth halves ---
check(strategies.funding_halve([]) is False, "missing funding does not change the weight")
check(strategies.funding_halve([0.10] * 10) is False, "a short history under 25% does not halve")
check(strategies.funding_halve([0.30]) is True, "funding at 30% a year halves even with no history")
check(strategies.funding_halve([0.10] * 40) is False, "a flat year under 25% does not halve")
check(strategies.funding_halve([0.05] * 40 + [0.12]) is True, "a jump above the past year's top tenth halves")
check(strategies.funding_halve([0.25] * 40) is True, "25% a year halves on the level rule")

kraken = {"rates": [
    {"timestamp": "2026-01-15T08:00:00Z", "relativeFundingRate": 0.000001},
    {"timestamp": "2026-01-15T23:00:00Z", "relativeFundingRate": 0.000002},
    {"timestamp": "2026-01-16T01:00:00Z", "relativeFundingRate": 0.000004},
]}
pts = strategies.parse_kraken_funding(kraken)
check(len(pts) == 2 and abs(pts[0][1] - 0.000002 * 24 * 365) < 1e-9,
      "Kraken funding keeps the last hour of each day, annualised")
eight = strategies.parse_binance_funding([{"fundingTime": 1760000000000, "fundingRate": "0.0001"}])
check(len(eight) == 1 and abs(eight[0][1] - 0.0001 * 3 * 365) < 1e-9, "an 8-hour funding rate is annualised")

# --- unlocks: unknown alts drop; a known alt under 1% stays; BTC always stays ---
quiet = {"data": [
    {"symbol": "SOL", "date": "2026-10-25", "pct_circulating": 0.4},
    {"symbol": "ARB", "date": "2026-10-21", "pct_circulating": 2.5},
]}
allowed, known = strategies.parse_pattern_unlocks(quiet, date(2026, 10, 19))
check(allowed == {"BTC", "ETH", "SOL"} and "ARB" in known and "ARB" not in allowed,
      "SOL under 1% is held, and a 2.5% unlock is not")
hot = {"data": [{"symbol": "SOL", "date": "2026-11-01", "pct_circulating": 1.2}]}
allowed, known = strategies.parse_pattern_unlocks(hot, date(2026, 10, 19))
check("SOL" in known and "SOL" not in allowed and "ETH" in allowed and "BTC" in allowed,
      "SOL unlocking 1.2% within 30 days is left out")
later = {"data": [{"symbol": "SOL", "date": "2026-12-20", "pct_circulating": 5}]}
allowed, _known = strategies.parse_pattern_unlocks(later, date(2026, 10, 19))
check("SOL" in allowed, "an unlock outside 30 days does not exclude SOL")
unknown = {"data": [{"symbol": "ARB", "date": "2026-10-21", "pct_circulating": 0.1}]}
allowed, known = strategies.parse_pattern_unlocks(unknown, date(2026, 10, 19))
check("SOL" not in known and "SOL" not in allowed and allowed == {"BTC", "ETH"},
      "an alt the feed does not mention is left out")
sol_ts = datetime(2026, 10, 25, tzinfo=timezone.utc).timestamp()
llama = [{"token": "SOL", "circSupply": 1000, "events": [{"timestamp": sol_ts, "noOfTokens": 5}]}]
allowed, known = strategies.parse_llama_emissions(llama, date(2026, 10, 19))
check("SOL" in allowed and "SOL" in known, "a 0.5% DeFiLlama unlock keeps SOL")
llama[0]["events"][0]["noOfTokens"] = 20
allowed, _known = strategies.parse_llama_emissions(llama, date(2026, 10, 19))
check("SOL" not in allowed, "a 2% DeFiLlama unlock drops SOL")
eth = [{"token": "ETH", "circSupply": 100, "events": [{"timestamp": sol_ts, "noOfTokens": 5}]}]
allowed, _known = strategies.parse_llama_emissions(eth, date(2026, 10, 19))
check("ETH" not in allowed and "BTC" in allowed, "ETH is dropped only when its own unlock is over 1%")

# --- weekly carry book: best allowed major, vol sized, funding halves crypto only ---
closes = {"BTC": geo(100, 80, 1.004), "ETH": geo(100, 80, 1.01), "SOL": geo(100, 80, 1.02),
          "VUSA": geo(50, 80, 1.002)}
base = strategies.targets_for("trend_carry_unlock", closes, {"unlock_allowed": {"BTC", "ETH"}})
check(base.get("ETH", (None, 0))[0] == "crypto" and "SOL" not in base and "BTC" not in base,
      "the carry book holds the strongest allowed major, which is ETH")
held_sol = strategies.targets_for("trend_carry_unlock", closes, {"unlock_allowed": {"BTC", "ETH", "SOL"}})
check(held_sol.get("SOL", (None, 0))[0] == "crypto" and "ETH" not in held_sol,
      "SOL is held when it leads and its unlock is small enough")
halved = strategies.targets_for("trend_carry_unlock", closes,
                                {"unlock_allowed": {"BTC", "ETH"}, "funding_halve": True})
check(abs(halved["ETH"][1] - base["ETH"][1] * 0.5) < 1e-9, "rich funding halves the crypto weight")
check(abs(halved["VUSA"][1] - base["VUSA"][1]) < 1e-9, "funding does not halve the stock sleeve")
fallback = strategies.targets_for("trend_carry_unlock", closes, {})
check("ETH" in fallback and "SOL" not in fallback, "with no unlock feed the book sticks to BTC and ETH")
check(strategies.due("trend_carry_unlock", date(2026, 10, 5), date(2026, 10, 6)) is False
      and strategies.due("trend_carry_unlock", date(2026, 10, 5), date(2026, 10, 12)) is True,
      "the carry book waits for a new week")
check(strategies.due("btc_trend", date(2026, 10, 5), date(2026, 10, 6)) is True,
      "the daily trend book still acts the next day")

# --- 10% of the position, not of the account ---
cash = {"crypto": 50.0, "stocks": 100.0}
pos = {"ETH": dict(qty=1, cost=100, account="crypto", peak=100, entry=100)}
prices = {"ETH": 100.0}
# Equity is £150. A £91 target sells £9, which is 9% of the £100 position.
small = strategies.apply_targets(dict(cash), {"ETH": dict(pos["ETH"])}, prices,
                                 {"ETH": ("crypto", 91 / 150)}, {"crypto": 0.0}, 5, 0.0, 0.10)
check(small == [], "a trim under 10% of the position is skipped")
pos2 = {"ETH": dict(qty=1, cost=100, account="crypto", peak=100, entry=100)}
cash2 = {"crypto": 50.0, "stocks": 100.0}
big = strategies.apply_targets(cash2, pos2, prices, {"ETH": ("crypto", 80 / 150)}, {"crypto": 0.0}, 5, 0.0, 0.10)
check(len(big) == 1 and big[0]["side"] == "SELL" and abs(big[0]["value"] - 20) < 1e-6,
      "a trim of 20% of the position trades")
opened = strategies.apply_targets({"crypto": 100.0, "stocks": 100.0}, {}, {"ETH": 100.0},
                                  {"ETH": ("crypto", 0.5)}, {"crypto": 0.0}, 5, 0.0, 0.10)
check(len(opened) == 1 and opened[0]["side"] == "BUY", "a new entry is not blocked by the 10% rule")
flat = {"ETH": dict(qty=1, cost=100, account="crypto", peak=100, entry=100)}
exited = strategies.apply_targets({"crypto": 0.0, "stocks": 0.0}, flat, prices, {}, {"crypto": 0.0}, 5, 0.0, 0.10)
check(len(exited) == 1 and exited[0]["side"] == "SELL" and "ETH" not in flat, "a full exit still sells")
same = {"ETH": dict(qty=1, cost=100, account="crypto", peak=100, entry=100)}
legacy = strategies.apply_targets({"crypto": 50.0, "stocks": 0.0}, same, prices,
                                  {"ETH": ("crypto", 91 / 150)}, {"crypto": 0.0}, 5, 0.0, 0.0)
check(len(legacy) == 1 and legacy[0]["side"] == "SELL", "books without the 10% rule still place a small trim")

# --- insider filter and the monthly stock book ---
check(strategies.owner_is_routine(["2024-03", "2025-03"], "2026-03") is True, "a repeat March trader is routine")
check(strategies.owner_is_routine(["2026-01"], "2026-03") is False, "a first trade in that month is opportunistic")
xml = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner><reportingOwnerId><rptOwnerName>Roe Ann</rptOwnerName></reportingOwnerId></reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-03-02</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-03-03</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""
parsed = strategies.parse_form4(xml)
check(len(parsed) == 2 and parsed[0]["purchase"] is True and parsed[1]["purchase"] is False
      and parsed[0]["owner"] == "ROE ANN",
      "only an open-market P is a buy; an award still marks the month")
cache = {"tickers": {
    "AAA": {"history_ok": True, "owners": {"DOE": {"months": ["2024-03", "2026-03"], "buys": ["2026-03-02"]}}},
    "BBB": {"history_ok": True, "owners": {"ROE ANN": {"months": ["2026-03"], "buys": ["2026-03-02"]}}},
    "CCC": {"history_ok": False, "owners": {"NEW": {"months": ["2026-03"], "buys": ["2026-03-02"]}}},
}}
flags = strategies.insider_flags(cache, date(2026, 3, 10))
check(flags == {"BBB": True}, "routine buyers and an unread history do not tilt")
check(strategies.due("insider_rs_stocks", date(2026, 10, 5), date(2026, 10, 20)) is False
      and strategies.due("insider_rs_stocks", date(2026, 10, 5), date(2026, 11, 2)) is True,
      "the insider book waits for a new month")

universe = []
stock_closes = {}
for i in range(12):
    sym = f"S{i}"
    universe.append(sym)
    stock_closes[sym] = geo(10, 210, 1.001 + i * 0.0005)
stock_closes["BTC"] = geo(100, 210, 1.01)
flags = {universe[6]: True}  # sixth strongest: inside the top half, outside the top five
picked = strategies.targets_for("insider_rs_stocks", stock_closes, {"universe": universe, "insider": flags})
check(set(picked) <= set(universe) and "BTC" not in picked, "the insider book does not buy crypto")
check(abs(sum(w for _a, w in picked.values()) - 1) < 1e-9, "stock weights sum to the whole stock sleeve")
check(universe[11] in picked and universe[6] in picked and len(picked) == 6,
      "the top five are held and an opportunistic buy in the top half is added")
check(picked[universe[6]][1] > picked[universe[7]][1], "the tilted name outweighs an untilted one")
no_tilt = strategies.targets_for("insider_rs_stocks", stock_closes, {"universe": universe, "insider": {}})
check(len(no_tilt) == 5 and universe[6] not in no_tilt, "with no insider cache the book is pure relative strength")

# A prior-year month we did not finish reading blocks the tilt.
filings = [(f"2024-03-0{n}", f"old{n}", "a.xml") for n in range(1, 6)]
filings.append(("2026-03-02", "new", "b.xml"))
chosen, ok = strategies._choose_filings(filings, date(2026, 3, 10), 180, xml_cap=4)
check(ok is False and len(chosen) <= 4, "a crowded prior March is not treated as fully read")
sparse, sparse_ok = strategies._choose_filings(
    [("2026-03-02", "new", "b.xml"), ("2024-03-02", "old", "a.xml")], date(2026, 3, 10), 180, xml_cap=4)
check(sparse_ok is True and len(sparse) == 2, "one prior filing in that month is enough to classify it")

# --- caches are read on the second call the same day ---
con = sqlite3.connect(":memory:")
con.row_factory = sqlite3.Row
con.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
calls = {"n": 0}

def fake_get(url, timeout=20, headers=None):
    calls["n"] += 1
    if "historical-funding" in url:
        rates = [{"timestamp": f"2026-01-{d:02d}T22:00:00Z", "relativeFundingRate": 0.000001} for d in range(1, 32)]
        rates.append({"timestamp": "2026-10-19T22:00:00Z", "relativeFundingRate": 0.00004})
        return {"rates": rates}
    if "llama" in url or "binance" in url or "deribit" in url or "tokenomist" in url:
        raise OSError("closed")
    if "pattern.plus" in url:
        return {"data": [{"symbol": "SOL", "date": "2026-10-25", "pct_circulating": 0.2}]}
    raise OSError(url)

halve, allowed = strategies.refresh_carry(con, date(2026, 10, 19), get=fake_get)
first = calls["n"]
halve2, allowed2 = strategies.refresh_carry(con, date(2026, 10, 19), get=fake_get)
check(halve is True and halve2 is True and allowed == allowed2 == {"BTC", "ETH", "SOL"},
      "a stored funding print above 25% halves, and a small SOL unlock is allowed")
check(calls["n"] == first, "the same day does not fetch funding or unlocks again")

sec_calls = {"n": 0}

def fake_sec(url, kind="json"):
    sec_calls["n"] += 1
    if url.endswith("company_tickers.json"):
        return {"0": {"cik_str": 9, "ticker": "BBB"}}
    if "submissions" in url:
        return {"filings": {"recent": {
            "form": ["4", "4"],
            "filingDate": ["2026-03-02", "2024-03-02"],
            "accessionNumber": ["new", "old"],
            "primaryDocument": ["new.xml", "old.xml"],
        }}}
    if kind == "text" and "new" in url:
        return xml
    if kind == "text":
        return xml.replace("Roe Ann", "Other Person").replace("2026-03-02", "2024-03-02").replace(">P<", ">S<")
    raise OSError(url)

# The live universe may not contain BBB. Point the refresh at this one name.
real_us = strategies._us_names
strategies._us_names = lambda: ["BBB"]
try:
    flags = strategies.refresh_insiders(con, date(2026, 3, 10), get=fake_sec)
    used = sec_calls["n"]
    again = strategies.refresh_insiders(con, date(2026, 3, 10), get=fake_sec)
finally:
    strategies._us_names = real_us
check(flags == {"BBB": True} and again == flags, "an opportunistic Form 4 buy is cached")
check(sec_calls["n"] == used, "a second cycle the same day does not call the SEC")

# --- replay: halved carry holds less crypto than the same book with no funding cut ---
window = {"BTC": geo(100, 100, 1.004), "ETH": geo(100, 100, 1.008), "SOL": geo(100, 100, 1.02)}
full = strategies.replay("trend_carry_unlock", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5,
                         carry={"allowed": {"BTC", "ETH"}})
cut_points = [(row[0], 0.30) for row in window["BTC"]]
cut = strategies.replay("trend_carry_unlock", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5,
                        carry={"halve": strategies.halve_series(cut_points), "allowed": {"BTC", "ETH"}})
base_book = strategies.replay("btc_trend_vol", window, fees={"crypto": 0.004, "stocks": 0.0015}, min_trade=5)
full_s, cut_s, base_s = strategies.replay_stats(full), strategies.replay_stats(cut), strategies.replay_stats(base_book)
check(full["pos"].get("ETH") and "SOL" not in full["pos"], "the replay holds ETH and leaves the unknown-alt rule on SOL")
check(cut["cash"]["crypto"] > full["cash"]["crypto"], "halving funding leaves more crypto cash")
check(full_s["ret"] > cut_s["ret"] > 0, "on this rising tape, full exposure finishes ahead of the halved book")
print(f"replay full {full_s['ret']*100:+.1f}% dd {full_s['max_dd']*100:.1f}% trades {full_s['trades']} "
      f"vs btc {full_s['excess_btc']*100:+.1f}%")
print(f"replay halved {cut_s['ret']*100:+.1f}% dd {cut_s['max_dd']*100:.1f}% trades {cut_s['trades']} "
      f"vs btc {cut_s['excess_btc']*100:+.1f}%")
print(f"replay btc_trend_vol {base_s['ret']*100:+.1f}% dd {base_s['max_dd']*100:.1f}% trades {base_s['trades']} "
      f"vs btc {base_s['excess_btc']*100:+.1f}%  btc hold {base_s['btc_ret']*100:+.1f}%")

print("carry-insider " + ("ok" if not fails else f"FAILED {len(fails)}"))
raise SystemExit(1 if fails else 0)
