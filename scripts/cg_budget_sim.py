"""Simulate one UK day of CoinGecko spending with the real price code and a fake network. Report only.

Runs collector.price_tracked, collector.fxrate, the top-300 scan schedule, the trader's four pre-fill quote
lists (quotes) and the exit watcher's REST loop (every 20 s) against a COPY of a price database. Every HTTP
call is answered locally: Kraken keys its old coins the way the real API does (XXMRZUSD), CoinGecko and the
pool sources answer with the last stored price. CoinGecko calls are counted by the real budget code.

Usage:
    python3 scripts/cg_budget_sim.py [--repo PATH] [--db data/trading.db] [--step 5] [--scenario busy|quiet]

--repo points at the checkout whose code is measured (so the same script measures main and a branch).
"""
import argparse, json, os, random, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap.add_argument("--db", default=None)
ap.add_argument("--step", type=int, default=5, help="minutes between cycles")
ap.add_argument("--scenario", default="busy", choices=("busy", "quiet"))
ap.add_argument("--day", default="2026-10-26", help="UK day to simulate (GMT after 25 Oct)")
a = ap.parse_args()

REPO = os.path.abspath(a.repo)
src_db = os.path.abspath(a.db or os.path.join(REPO, "data", "trading.db"))
tmp = tempfile.mkdtemp()
dbp = os.path.join(tmp, "sim.db")
shutil.copy(src_db, dbp)
os.environ["TRADING_DB"] = dbp
os.environ["TRADING_OFFLINE"] = "1"
os.environ.pop("COINGECKO_API_KEY", None)
sys.path.insert(0, REPO)
os.chdir(REPO)

import common, pricesrc, collector, quotes   # noqa: E402
import requests                              # noqa: E402
import logging
logging.disable(logging.CRITICAL)

START = datetime.fromisoformat(a.day + "T00:00:00+00:00")
common._NOW = START
con = common.db()
c = common.cfg()
con.execute("DELETE FROM cg_calls"); con.execute("DELETE FROM cg_purpose")
for k in ("cg_fallback_ts", "cg_held_ts", "cg_scan_ts"):
    con.execute("DELETE FROM state WHERE key=?", (k,))
# The copy's last prices are up to a day old; restamp the last 48h of rows so tracked() sees the same set on the sim day.
shift = START - datetime.fromisoformat(con.execute("SELECT max(ts) FROM prices").fetchone()[0])
con.execute("CREATE TEMP TABLE keep AS SELECT rowid r FROM prices WHERE ts >= ?",
            ((START - shift - timedelta(hours=48)).isoformat(),))
rows = con.execute("SELECT rowid, ts FROM prices WHERE rowid IN (SELECT r FROM keep)").fetchall()
con.executemany("UPDATE prices SET ts=?, source_ts=? WHERE rowid=?",
                [((datetime.fromisoformat(t) + shift).isoformat(), (datetime.fromisoformat(t) + shift).isoformat(), r) for r, t in rows])
con.execute("DELETE FROM prices WHERE ts > ?", (START.isoformat(),))
con.execute("DELETE FROM fx WHERE ts > ?", (START.isoformat(),))
routes_blob = json.loads(con.execute("SELECT value FROM state WHERE key='price_routes'").fetchone()[0])
routes_blob["ts"] = START.isoformat()
con.execute("INSERT OR REPLACE INTO state VALUES('price_routes',?)", (json.dumps(routes_blob),))
routes = routes_blob["routes"]
con.commit()

last = {r[0]: r[1] for r in con.execute(
    "SELECT asset, price FROM prices p WHERE klass='crypto' AND price>0 AND ts=(SELECT max(ts) FROM prices q WHERE q.asset=p.asset AND q.klass='crypto' AND q.price>0)")}
last.setdefault("BTC", 80000.0)
id2sym = {}
for s, r in routes.items():
    if r.get("id"): id2sym.setdefault(r["id"], s)
alt2sym = {pricesrc.kraken_norm(r["pair"]): s for s, r in routes.items() if r.get("src") == "kraken" and r.get("pair")}
LEGACY = {"BTC": "XXBT", "ETH": "XETH", "XRP": "XXRP", "XMR": "XXMR", "LTC": "XLTC", "XLM": "XXLM", "ETC": "XETC",
          "ZEC": "XZEC", "MLN": "XMLN"}
rnd = random.Random(7)
def px(sym): return float(last.get(sym) or 1.0) * (1 + rnd.uniform(-0.002, 0.002))

class Resp:
    def __init__(self, data, code=200): self._d, self.status_code, self.headers = data, code, {}
    def json(self): return self._d
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)

HITS = {"kraken": 0, "coinbase": 0, "coingecko": 0, "pool": 0, "other": 0}
def fake_get(url, params=None, headers=None, timeout=20):
    u, p = str(url), dict(params or {})
    if "api.kraken.com" in u and u.endswith("/Ticker"):
        HITS["kraken"] += 1
        out = {}
        for pair in str(p.get("pair", "")).split(","):
            if not pair: continue
            n = pricesrc.kraken_norm(pair)
            if n in ("BTCGBP", "BTCUSD"):
                v = px("BTC") * (0.75 if n == "BTCGBP" else 1)
                out["XXBTZ" + n[3:]] = {"c": [str(v), "1"], "h": ["0", str(v)], "l": ["0", str(v)], "v": ["0", "10"]}
                continue
            sym = alt2sym.get(n)
            if not sym: continue
            q = routes[sym].get("quote") or "USD"
            key = (LEGACY[sym] + "Z" + q) if sym in LEGACY and q in ("USD", "GBP", "EUR") else pair
            v = px(sym)
            out[key] = {"c": [str(v), "1"], "h": ["0", str(v)], "l": ["0", str(v)], "v": ["0", "10"]}
        return Resp({"error": [], "result": out})
    if "kraken.com" in u:
        HITS["kraken"] += 1; return Resp({"error": [], "result": {}})
    if "coinbase.com" in u:
        HITS["coinbase"] += 1
        sym = u.split("/products/")[-1].split("-")[0] if "/products/" in u else None
        return Resp({"price": str(px(sym)), "time": common.iso(), "volume": "10"} if sym else [])
    if "coingecko.com" in u:
        HITS["coingecko"] += 1
        if "simple/price" in u:
            ids = str(p.get("ids", "")).split(",")
            if ids == ["bitcoin"]:
                return Resp({"bitcoin": {"usd": px("BTC"), "gbp": px("BTC") * 0.75}})
            return Resp({i: {"usd": px(id2sym[i]), "last_updated_at": int(common.now().timestamp())} for i in ids if i in id2sym})
        return Resp([])
    if "geckoterminal" in u or "dexscreener" in u:
        HITS["pool"] += 1
        return Resp({"data": [], "pairs": []})
    if "er-api" in u:
        HITS["other"] += 1
        return Resp({"result": "success", "rates": {"GBP": 0.75}})
    HITS["other"] += 1
    return Resp({})

collector.S.get = fake_get
requests.get = lambda url, params=None, timeout=20, headers=None: fake_get(url, params, headers, timeout)

import exit_watch   # noqa: E402  (after the patch: it calls requests.get directly)

cg_only = sorted(s for s, r in routes.items() if r.get("src") == "coingecko")
tracked = pricesrc.tracked(con)
cg_tracked = [s for s in tracked if s in cg_only]

def uk(h, m=0): return START + timedelta(hours=h, minutes=m)   # GMT on the sim day
def scenario(t):
    """Open Day positions and the trader's candidate lists at time t. 'busy' is shaped on 22-23 Oct:
    a CoinGecko-only coin (BTW) held most of the trading day, XMR (Kraken) and CoinGecko-only coins on the
    candidate lists every cycle in the entry window."""
    core = ["MON", "XRP", "WLD", "BTC", "ETH"]
    if a.scenario == "quiet":
        return core, [], ["ETH", "SOL"], []
    window = uk(8) <= t < uk(21)
    held = (["BTW", "AKE"] if uk(10) <= t < uk(15, 30) else []) + (["XMR"] if uk(8, 45) <= t < uk(10, 15) else [])
    day = ["XMR", "AKE"] if window else []
    burst = ["XMR", "LUNC", "KAIA", "ETH"] + (["TWT"] if window else [])
    risk = ["BR", "KAIA"]
    return core + held, day, burst, risk

def set_positions(held):
    con.execute("DELETE FROM positions WHERE sleeve='day'")
    for s in held:
        if s in ("MON", "XRP", "WLD", "BTC", "ETH"): continue
        con.execute("INSERT INTO positions VALUES('day',?,'crypto',1,1,1,?,1)", (s, common.iso()))

stale_samples, n_cycles, live_lists = [], 0, 0
t = START
END = START + timedelta(days=1)
while t < END:
    common._NOW = t
    ts = common.iso()
    held, day, burst, risk = scenario(t)
    set_positions(held)
    collector.bind_budget(con, c)
    try:
        status = {}
        collector.price_tracked(con, c, ts, status)
        if pricesrc.scan_due(con, c, t):
            for _ in range(3): collector.get(f"{collector.CG}/coins/markets", {"page": 1}, tries=1, purpose="scan")
            pricesrc.mark(con, "cg_scan_ts", ts)
        if t.minute == 0 and t.hour % 6 == 0:
            collector.get(f"{collector.CG}/coins/markets", {"category": "x"}, tries=1, purpose="meta")
        collector.fxrate(con, c, ts, {})
    finally:
        collector.bind_budget(None, None)
    pxmap = {s: dict(klass="crypto", price=1.0, price_usd=1.0) for s in set(held + day + burst + risk)}
    for lst in (held, day, burst, risk):
        if lst:
            live_lists += 1
            quotes._prefetch(con, c, lst, pxmap)
    # Exit watcher: REST pass every 20 s for the coins it cannot stream (CoinGecko-only, held in Day/Risk).
    plan = exit_watch.watch_plan([dict(asset=s, klass="crypto") for s in held if s not in ("MON", "XRP", "WLD", "BTC", "ETH")], routes)
    rest_plan = dict(kraken=[], coinbase=[], rest=[x for x in plan["rest"] if x.get("kind") == "coingecko"])
    if rest_plan["rest"]:
        for k in range(a.step * 3):
            common._NOW = t + timedelta(seconds=20 * k)
            exit_watch._rest_once(con, c, rest_plan, 0.75, lambda *x: None)
    common._NOW = t
    con.commit()
    # coverage: tracked CoinGecko-only coins whose newest price is older than 30 min
    old = 0
    for s in cg_tracked + ["XMR"]:
        r = con.execute("SELECT max(ts) FROM prices WHERE asset=? AND klass='crypto' AND source!='backfill'", (s,)).fetchone()
        if not r[0] or datetime.fromisoformat(r[0]) < t - timedelta(minutes=30): old += 1
    stale_samples.append(old)
    n_cycles += 1
    t += timedelta(minutes=a.step)

common._NOW = END - timedelta(minutes=1)
today, _m = pricesrc.usage(con, common._NOW)
by = pricesrc.purpose_counts(con, common._NOW)
# when during the day the budget ran out (first cycle where the count reached the hard cap)
print(json.dumps(dict(repo=REPO, scenario=a.scenario, step_min=a.step, cycles=n_cycles, cg_calls=today, by_purpose=by,
                      day_budget=pricesrc.cfg_of(c)["day_budget"], http=HITS,
                      cg_only_tracked=len(cg_tracked), avg_stale_coins=round(sum(stale_samples) / len(stale_samples), 2),
                      max_stale_coins=max(stale_samples)), indent=1))
shutil.rmtree(tmp, ignore_errors=True)
