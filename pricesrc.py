"""Tiered crypto prices. Kraken and Coinbase public markets first, KyberSwap for Monad tokens,
CoinGecko only for what those do not cover. CoinGecko calls are counted per UK day and capped
(demo key: 100/min but 10,000/month). No exchange API key. The key, when set, stays in collector.cg_headers
and is never logged here."""
from datetime import datetime, timedelta, timezone

KRAKEN = "https://api.kraken.com/0/public"
COINBASE = "https://api.exchange.coinbase.com"
QUOTE_RANK = {"USD": 0, "USDT": 1, "USDC": 1, "GBP": 2}
# Kraken's own tickers. Matching ignores the X/Z prefixes on the result keys.
_FROM = (("XBT", "BTC"), ("XDG", "DOGE"))


def cfg_of(c):
    d = (c or {}).get("coingecko") or {}
    return dict(day_budget=int(d.get("day_budget", 300)), month_budget=int(d.get("month_budget", 9000)),
                scan_min=float(d.get("scan_every_min", 180)), scan_min_tight=float(d.get("scan_every_min_tight", 360)),
                fallback_min=float(d.get("fallback_every_min", 15)), fallback_min_tight=float(d.get("fallback_every_min_tight", 60)),
                tight=float(d.get("tight_pct", 0.70)))


def day_key(now):
    """UK calendar day. The budget resets at midnight in London, same clock as the flatten."""
    from common import LON
    n = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return n.astimezone(LON).date().isoformat()


def usage(con, now):
    day = day_key(now)
    today = con.execute("SELECT n FROM cg_calls WHERE day=?", (day,)).fetchone()
    month = con.execute("SELECT COALESCE(SUM(n),0) FROM cg_calls WHERE day LIKE ?", (day[:7] + "%",)).fetchone()
    return int(today[0] if today else 0), int(month[0] if month else 0)


def add_calls(con, now, n=1):
    if not n: return
    day = day_key(now)
    con.execute("INSERT INTO cg_calls(day, n) VALUES(?, ?) ON CONFLICT(day) DO UPDATE SET n = n + ?", (day, int(n), int(n)))


def tight(con, c, now):
    P = cfg_of(c)
    today, month = usage(con, now)
    return today >= P["day_budget"] * P["tight"] or month >= P["month_budget"] * P["tight"]


def allow(con, c, now, purpose):
    """False when this CoinGecko call should not be sent. Scan, charts, category refreshes and the
    FX cross-check stop first. A scheduled fallback price slows down. A pre-trade quote ('live')
    keeps going until the hard daily or monthly cap."""
    if con is None or c is None: return True
    P = cfg_of(c)
    today, month = usage(con, now)
    if today >= P["day_budget"] or month >= P["month_budget"]: return False
    if purpose in ("scan", "meta", "chart", "fx") and tight(con, c, now): return False
    return True


def _due(last, now, minutes):
    if not last: return True
    try: return datetime.fromisoformat(str(last).strip('"')) <= now - timedelta(minutes=float(minutes))
    except Exception: return True


def _state(con, key):
    r = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if not r or not r[0]: return None
    return str(r[0]).strip('"')


def scan_due(con, c, now):
    if not allow(con, c, now, "scan"): return False
    P = cfg_of(c)
    every = P["scan_min_tight"] if tight(con, c, now) else P["scan_min"]
    return _due(_state(con, "cg_scan_ts"), now, every)


def fallback_due(con, c, now):
    """Scheduled simple/price for coins with no exchange. Exchange failures do not wait for this."""
    if not allow(con, c, now, "fallback"): return False
    P = cfg_of(c)
    every = P["fallback_min_tight"] if tight(con, c, now) else P["fallback_min"]
    return _due(_state(con, "cg_fallback_ts"), now, every)


def mark(con, key, ts):
    import json
    con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(ts)))


def routes_of(con):
    return dict(load_routes(con).get("routes") or {})


def stats(con, now, c):
    today, month = usage(con, now)
    P = cfg_of(c)
    return dict(today=today, month=month, day_budget=P["day_budget"], month_budget=P["month_budget"])


def kraken_norm(name):
    """XXBTZUSD, XBTUSD and XBT/USD all become BTCUSD. Used only to match a ticker row to the pair we asked for."""
    n = (name or "").upper().replace("/", "").replace("-", "")
    for a, b in _FROM: n = n.replace(a, b)
    for a, b in (("ZUSD", "USD"), ("ZGBP", "GBP"), ("ZEUR", "EUR")):
        if n.endswith(a):
            n = n[:-len(a)] + b
            break
    if n.startswith("X") and not n.startswith(("XRP", "XLM", "XTZ")):
        n = n[1:]
    return n


def _best(cands):
    cands = [x for x in cands if x.get("quote") in QUOTE_RANK]
    if not cands: return None
    return dict(sorted(cands, key=lambda x: QUOTE_RANK[x["quote"]])[0])


def kraken_index(result):
    """AssetPairs result dict -> {SYM: {pair, quote, norm}}. USD beats USDT beats GBP. Offline and dark-pool pairs are skipped."""
    out = {}
    for info in (result or {}).values():
        if not isinstance(info, dict): continue
        if info.get("status") not in (None, "online"): continue
        ws = info.get("wsname") or ""
        if "/" not in ws or "." in ws: continue
        base, quote = ws.split("/", 1)
        if quote not in QUOTE_RANK: continue
        sym = base
        for a, b in _FROM:
            if sym == a: sym = b
        pair = info.get("altname") or (base + quote)
        if "." in pair: continue
        out.setdefault(sym, []).append(dict(pair=pair, quote=quote, norm=kraken_norm(pair)))
    return {s: _best(v) for s, v in out.items()}


def coinbase_index(products):
    """GET /products list -> {SYM: {pair, quote}}. Online spot markets only."""
    out = {}
    for p in products or []:
        if not isinstance(p, dict): continue
        if p.get("status") not in (None, "online"): continue
        if p.get("trading_disabled"): continue
        b, q = p.get("base_currency"), p.get("quote_currency")
        if not b or q not in QUOTE_RANK: continue
        if p.get("auction_mode"): continue
        out.setdefault(b, []).append(dict(pair=p.get("id") or f"{b}-{q}", quote=q))
    return {s: _best(v) for s, v in out.items()}


def choose(sym, kidx, cidx, kyber, cg_id):
    """One cached route. Kraken wins (it batches). Coinbase is the alt if Kraken fails, or the source if Kraken has no pair.
    Kyber only when neither exchange lists it. Otherwise CoinGecko."""
    sym = (sym or "").upper()
    if kidx.get(sym):
        route = dict(kidx[sym], src="kraken", id=cg_id)
        if cidx.get(sym): route["alt"] = dict(cidx[sym], src="coinbase")
        return route
    if cidx.get(sym):
        return dict(cidx[sym], src="coinbase", id=cg_id)
    if kyber and kyber.get("token"):
        return dict(src="kyber", token=kyber["token"], dec=kyber.get("dec"), id=cg_id, mon=bool(kyber.get("mon")))
    return dict(src="coingecko", id=cg_id)


def to_usd(price, quote, fx):
    """fx is USD->GBP (GBP per 1 USD), the rate already stored in table fx."""
    if price is None: return None
    try: p = float(price)
    except (TypeError, ValueError): return None
    if p <= 0: return None
    q = (quote or "USD").upper()
    if q in ("USD", "USDT", "USDC"): return p
    if q == "GBP":
        if not fx or float(fx) <= 0: return None
        return p / float(fx)
    return None


def parse_kraken_ticker(data, routes, fx, now_iso):
    """One Ticker response covering many pairs. routes is {sym: route}."""
    res = (data or {}).get("result") or {}
    by = {}
    for sym, r in (routes or {}).items():
        by[r.get("norm") or kraken_norm(r.get("pair"))] = sym
    out = {}
    for key, t in res.items():
        if not isinstance(t, dict): continue
        sym = by.get(kraken_norm(key))
        if not sym: continue
        r = routes[sym]
        try:
            last = float(t["c"][0])
            high = float(t["h"][1]) if t.get("h") and len(t["h"]) > 1 else None
            low = float(t["l"][1]) if t.get("l") and len(t["l"]) > 1 else None
            vol_b = float(t["v"][1]) if t.get("v") and len(t["v"]) > 1 else None
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        usd = to_usd(last, r.get("quote"), fx)
        if not usd: continue
        out[sym] = dict(usd=usd, high=to_usd(high, r.get("quote"), fx) if high else None,
                        low=to_usd(low, r.get("quote"), fx) if low else None,
                        vol=(vol_b * usd) if vol_b else None, src_ts=now_iso, source="kraken")
    return out


def parse_coinbase_ticker(data, route, fx, now_iso):
    if not isinstance(data, dict) or data.get("price") in (None, ""): return None
    usd = to_usd(data.get("price"), (route or {}).get("quote"), fx)
    if not usd: return None
    src = now_iso
    raw = data.get("time")
    if raw:
        try: src = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(microsecond=0).isoformat()
        except Exception: src = now_iso
    vol = None
    try:
        if data.get("volume") not in (None, ""): vol = float(data["volume"]) * usd
    except (TypeError, ValueError): vol = None
    return dict(usd=usd, high=None, low=None, vol=vol, src_ts=src, source="coinbase")


def parse_kraken_ohlc(data, route):
    """[(unix_seconds, close)] from OHLC. The pair key in the result is the internal name, matched loosely."""
    res = (data or {}).get("result") or {}
    want = (route or {}).get("norm") or kraken_norm((route or {}).get("pair"))
    rows = None
    for k, v in res.items():
        if k == "last" or not isinstance(v, list): continue
        if kraken_norm(k) == want: rows = v; break
    if rows is None:
        lists = [v for k, v in res.items() if k != "last" and isinstance(v, list)]
        if len(lists) == 1: rows = lists[0]
    out = []
    for row in rows or []:
        try:
            t, close = int(row[0]), float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if close > 0: out.append((t, close))
    return out


def parse_coinbase_candles(data):
    """[(unix_seconds, close)]. Coinbase rows are [time, low, high, open, close, volume]."""
    out = []
    for row in data or []:
        try:
            t, close = int(row[0]), float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if close > 0: out.append((t, close))
    return out


def parse_cg_simple(data, id_to_sym, now_iso):
    """simple/price with include_last_updated_at -> {sym: price row}."""
    out = {}
    for cid, sym in (id_to_sym or {}).items():
        row = (data or {}).get(cid) or {}
        try: usd = float(row.get("usd"))
        except (TypeError, ValueError): continue
        if usd <= 0: continue
        lu = row.get("last_updated_at")
        try: src = datetime.fromtimestamp(int(lu), timezone.utc).replace(microsecond=0).isoformat() if lu else now_iso
        except (TypeError, ValueError, OSError): src = now_iso
        out[sym] = dict(usd=usd, high=None, low=None, vol=None, src_ts=src, source="coingecko")
    return out


def quote_exchanges(symbols, routes, fx, fetch, kyber_usd, now_iso, pace=None):
    """Price every symbol whose route is an exchange or Kyber.
    fetch(url, params=None, headers=None, tries=1) -> decoded JSON or None.
    kyber_usd(route) -> USD or None. One Kraken Ticker call covers every Kraken pair (split only past 200, which is already more than the tracked set).
    A miss (call failed, or that pair was absent) is returned so the caller can try the next source.
    Returns (priced, missed). Coins routed to CoinGecko are neither priced nor missed here."""
    routes = routes or {}
    priced, missed = {}, []
    kraken = [s for s in symbols if (routes.get(s) or {}).get("src") == "kraken"]
    chunks = [kraken[i:i + 200] for i in range(0, len(kraken), 200)] or []
    got = {}
    for ch in chunks:
        if not ch: continue
        pair = ",".join(routes[s]["pair"] for s in ch)
        data = fetch(f"{KRAKEN}/Ticker", {"pair": pair}, None, 2)
        part = parse_kraken_ticker(data, {s: routes[s] for s in ch}, fx, now_iso) if data else {}
        got.update(part)
    for s in kraken:
        if s in got:
            priced[s] = got[s]
            continue
        alt = (routes[s].get("alt") or {})
        row = None
        if alt.get("src") == "coinbase" and alt.get("pair"):
            if pace: pace(0.11)
            data = fetch(f"{COINBASE}/products/{alt['pair']}/ticker", None, None, 1)
            row = parse_coinbase_ticker(data, alt, fx, now_iso)
        if row: priced[s] = row
        else: missed.append(s)
    for s in symbols:
        r = routes.get(s) or {}
        if r.get("src") != "coinbase": continue
        if pace: pace(0.11)
        data = fetch(f"{COINBASE}/products/{r['pair']}/ticker", None, None, 1)
        row = parse_coinbase_ticker(data, r, fx, now_iso)
        if row: priced[s] = row
        else: missed.append(s)
    for s in symbols:
        r = routes.get(s) or {}
        if r.get("src") != "kyber": continue
        try: usd = kyber_usd(r)
        except Exception: usd = None
        if usd and usd > 0:
            priced[s] = dict(usd=float(usd), high=None, low=None, vol=None, src_ts=now_iso, source="kyberswap")
        else:
            missed.append(s)
    return priced, missed


def load_routes(con):
    import json
    r = con.execute("SELECT value FROM state WHERE key='price_routes'").fetchone()
    if not r or not r[0]: return {}
    try: return json.loads(r[0]) or {}
    except Exception: return {}


def _cgid(con, sym):
    r = con.execute("SELECT value FROM state WHERE key=?", (f"cgid:{sym}",)).fetchone()
    if not r or not r[0]: return None
    v = r[0]
    try:
        import json
        v = json.loads(v)
    except Exception:
        v = str(v).strip('"')
    return v if isinstance(v, str) and v else None


def _kyber_map(con, c):
    """{sym: {token, dec, mon?}} from the Monad address cache, plus MON from config when it is not an exchange coin."""
    import json
    out = {}
    k = ((c or {}).get("crypto") or {}).get("kyberswap") or {}
    if k.get("token_in"):
        out["MON"] = dict(token=k["token_in"], dec=18, mon=True)
    raw = con.execute("SELECT value FROM state WHERE key='monad_addr'").fetchone()
    try: addr = json.loads(raw[0]) if raw and raw[0] else {}
    except Exception: addr = {}
    if not isinstance(addr, dict): return out
    rows = con.execute("SELECT key, value FROM state WHERE key LIKE 'cgid:%'").fetchall()
    for key, val in rows:
        sym = key.split(":", 1)[1].upper()
        try: cid = json.loads(val)
        except Exception: cid = str(val).strip('"')
        meta = addr.get(cid) if isinstance(cid, str) else None
        if isinstance(meta, dict) and meta.get("a"):
            out.setdefault(sym, dict(token=meta["a"], dec=meta.get("dec")))
    return out


def ensure_routes(con, c, symbols, fetch, now=None):
    """Cached symbol -> source, refreshed once a day from Kraken AssetPairs and Coinbase /products (no CoinGecko).
    A symbol that appears later in the day is marked CoinGecko until the next refresh, so a new coin cannot
    trigger an exchange-list fetch on every cycle. fetch is collector.get."""
    import json
    from common import iso, now as now_fn
    now = now or now_fn()
    symbols = [s.upper() for s in symbols if s]
    cache = load_routes(con)
    routes = dict(cache.get("routes") or {})
    fresh = cache.get("ts") and not _due(cache.get("ts"), now, 24 * 60)
    missing = [s for s in symbols if s not in routes]
    ky = _kyber_map(con, c)
    if fresh and not missing:
        return routes
    if fresh and missing:
        for s in missing:
            routes[s] = choose(s, {}, {}, ky.get(s), _cgid(con, s))
        cache["routes"] = routes
        con.execute("INSERT OR REPLACE INTO state VALUES('price_routes',?)", (json.dumps(cache),))
        return routes
    kr = fetch(f"{KRAKEN}/AssetPairs", None, None, 2) or {}
    cb = fetch(f"{COINBASE}/products", None, None, 2)
    kidx = kraken_index((kr or {}).get("result") if isinstance(kr, dict) else {})
    cidx = coinbase_index(cb if isinstance(cb, list) else [])
    if not kidx and not cidx:
        # Both books failed. Keep yesterday's map and try again next cycle, instead of pinning every coin to CoinGecko for a day.
        if routes:
            return routes
        return {s: choose(s, {}, {}, ky.get(s), _cgid(con, s)) for s in symbols}
    syms = list(dict.fromkeys(list(routes) + symbols + list(ky)))
    new = {}
    for s in syms:
        new[s] = choose(s, kidx, cidx, ky.get(s), _cgid(con, s))
    blob = json.dumps(dict(
        ts=iso(now), routes=new,
        counts={k: sum(1 for r in new.values() if r.get("src") == k) for k in ("kraken", "coinbase", "kyber", "coingecko")}))
    con.execute("INSERT OR REPLACE INTO state VALUES('price_routes',?)", (blob,))
    return new


def tracked(con):
    """Crypto symbols to re-price every cycle: anything priced in the last 48h, anything held, the Day universe and the Risk watch."""
    import json
    from common import now
    since = (now() - timedelta(hours=48)).isoformat()
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT asset FROM prices WHERE klass='crypto' AND source NOT IN ('backfill') AND ts>=?", (since,))]
    syms += [r[0] for r in con.execute("SELECT DISTINCT asset FROM positions WHERE klass='crypto'")]
    for key in ("day_universe", "risk_watch"):
        raw = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        try: blob = json.loads(raw[0]) if raw and raw[0] else {}
        except Exception: blob = {}
        if key == "day_universe":
            syms += list((blob.get("coins") or {}).keys())
        else:
            syms += list((blob or {}).keys())
    out, seen = [], set()
    for s in syms:
        s = (s or "").upper()
        if not s or "." in s or s in seen: continue
        seen.add(s); out.append(s)
    return out


def estimate(routes, scan_per_day=8, fallback_per_day=96, fx_per_day=26, meta_per_day=6, charts_per_day=2):
    """Rough CoinGecko calls if the cadence in config holds. One fallback call covers every uncovered coin."""
    counts = {}
    for r in (routes or {}).values():
        counts[r.get("src") or "?"] = counts.get(r.get("src") or "?", 0) + 1
    uncovered = counts.get("coingecko", 0)
    day = scan_per_day * 2 + (fallback_per_day if uncovered else 0) + fx_per_day + meta_per_day + charts_per_day
    return dict(counts=counts, uncovered=uncovered, cg_per_day=day, cg_per_month=day * 30)
