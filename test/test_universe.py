"""v7.2 offline unit test of the DAY crypto universe filter (collector.day_universe) on a synthetic top-300 snapshot and a
throwaway DB (no network: the meme-token category is pre-cached in state meme_ids). Also checks the sentiment asset mapping.
Usage: python3 test/test_universe.py"""
import json, os, sqlite3, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp()) / "u.db"; os.environ["TRADING_DB"] = str(tmp); os.environ["TRADING_LOG"] = str(tmp.with_suffix(".log"))
sys.path.insert(0, str(HERE))
import collector, ingest_sentiment
from common import cfg, db
c = cfg(); con = db()
con.execute("INSERT OR REPLACE INTO state VALUES('meme_ids',?)", (json.dumps(dict(ts=datetime.now(timezone.utc).isoformat(), ids=["dogecoin", "pepe", "bonk", "newmeme"], syms=[])),))
def coin(i, sym, name, mc, vol, cid=None, c24=3.0, c7=8.0):
    return dict(id=cid or name.lower().replace(" ", "-"), symbol=sym.lower(), name=name, market_cap=mc, total_volume=vol, current_price=1.0,
                price_change_percentage_24h_in_currency=c24, price_change_percentage_7d_in_currency=c7)
top = [coin(1, "BTC", "Bitcoin", 2e12, 2.2e10, "bitcoin"),                  # ~1% turnover -> not an EXTRA (it is in the top_n base set)
       coin(2, "USDT", "Tether", 1.8e11, 3e10, "tether"),                    # stable
       coin(3, "BNB", "BNB", 9e10, 5e8, "binancecoin"),                      # exchange token
       coin(4, "DOGE", "Dogecoin", 3e10, 9e8, "dogecoin"),                   # meme, allowed
       coin(5, "WBTC", "Wrapped Bitcoin", 1e10, 2e8, "wrapped-bitcoin"),     # wrapped
       coin(6, "PEPE", "Pepe", 4e9, 3e8, "pepe"),                            # meme, allowed
       coin(7, "SHIB", "Shiba Inu", 7e9, 1.4e8, "shiba-inu"),               # meme (blocklist only)
       coin(8, "RAIN", "Rain", 5e9, 1.3e7, "rain"),                          # 0.26% turnover -> out
       coin(9, "LINK", "Chainlink", 9e9, 4e8, "chainlink")]               # 4.4% -> in
top += [coin(80, "BONK", "Bonk", 1e9, 1e8, "bonk"),                          # meme (category)
        coin(81, "NEWM", "New Meme", 2e8, 5e7, "newmeme"),                  # meme (category only)
        coin(82, "MID", "Mid Cap", 3e8, 2e7, "mid-cap"),                    # kept: >= 50M, >= 5M, 6.7%
        coin(83, "THIN", "Thin Cap", 3e8, 5.5e6, "thin-cap"),               # 1.8% turnover -> out
        coin(84, "TINY", "Tiny Cap", 4e7, 9e6, "tiny-cap"),                 # < $50M -> out
        coin(85, "LOWV", "Low Volume", 2e8, 4e6, "low-vol"),                # < $5M -> out
        coin(86, "STSOL", "Staked SOL", 5e8, 5e7, "staked-sol")]            # staked -> out
keep = {x["symbol"].upper() for x in collector.day_universe(con, c, top, "2026-10-10T14:00:00+00:00")}
st = json.loads(con.execute("SELECT value FROM state WHERE key='day_universe'").fetchone()[0])
exp_in, exp_out = {"LINK", "DOGE", "PEPE", "MID"}, {"BTC", "USDT", "BNB", "WBTC", "SHIB", "RAIN", "BONK", "NEWM", "THIN", "TINY", "LOWV", "STSOL"}
fails = [f"{s} should be in" for s in exp_in - keep] + [f"{s} should be out" for s in exp_out & keep]
assert st["n"] == len(keep) and "memecoin" in st["excluded"], st
# base-set exclusions for DAY trading (collector.day_excluded; used for state day_excluded)
memes = collector.meme_ids(con, c["crypto"]["day_universe"])
for x, want in ((top[2], True), (top[0], False), (top[3], False), (top[5], False), (top[6], True), (top[8], False),
                (coin(10, "LEO", "LEO Token", 8e9, 2e6, "leo-token"), True), (coin(11, "PAXG", "PAX Gold", 1e9, 9e7, "pax-gold"), True)):
    why = collector.day_excluded(x, c, memes)
    if bool(why) != want: fails.append(f"base {x['symbol'].upper()}: day_excluded={why!r}, want {'excluded' if want else 'allowed'}")
m, known = ingest_sentiment.id_map(con)
for a, want in (("$mid", "MID"), ("mid-cap", "MID"), ("chainlink", "LINK"), ("RPI.L", "RPI.L"), ("XYZNEW", "XYZNEW")):
    got = ingest_sentiment.norm_asset(a, m, known)
    if got != want: fails.append(f"sentiment asset {a!r} -> {got!r}, want {want!r}")
print(f"[test_universe] kept {sorted(keep)}; excluded {st['excluded']}")
if fails: print("FAIL:", "; ".join(fails)); sys.exit(1)
print("[test_universe] OK")
