"""Price collector. Crypto prices: Kraken and Coinbase public markets when the coin is listed, KyberSwap for Monad
tokens, CoinGecko only for the rest and for the top-300 screen. Stocks: Yahoo (normal exchange tickers). USD->GBP FX,
Monad gas (simulated network fee), daily closes of the CORE universe.
All asset prices are stored in raw USD; the FX rate is stored in table fx and applied by trader/dashboard."""
import json, os, time, requests
from datetime import datetime, timedelta, timezone
from common import cfg, db, iso, log, usd_gbp
import freshness

L = log("collector")
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0 (paper-trader)"
CG = "https://api.coingecko.com/api/v3"

def cg_headers(extra=None):
    """Headers for api.coingecko.com. When COINGECKO_API_KEY is set, send it as x-cg-demo-api-key.
    The public base URL stays the same. Unset or blank: no extra header (the free unauthenticated call).
    The key is never logged and is not attached to any other host."""
    h = dict(extra or {})
    key = (os.environ.get("COINGECKO_API_KEY") or "").strip()
    if key:
        h["x-cg-demo-api-key"] = key
    return h

_BUDGET_CON = None
_BUDGET_CFG = None

def bind_budget(con, c):
    """Count CoinGecko calls against cg_calls while this is set (collector.main and the pre-trade quote)."""
    global _BUDGET_CON, _BUDGET_CFG
    _BUDGET_CON, _BUDGET_CFG = con, c

def get(url, params=None, headers=None, tries=3, purpose="other", timeout=20):
    cg = str(url).startswith(CG)
    if cg:
        headers = cg_headers(headers)
        if _BUDGET_CON is not None:
            import pricesrc
            from common import now as _now
            if not pricesrc.allow(_BUDGET_CON, _BUDGET_CFG, _now(), purpose):
                L.info("coingecko %s skipped (day budget)", purpose)
                return None
    for i in range(tries):
        try:
            if cg and _BUDGET_CON is not None:
                import pricesrc
                from common import now as _now
                pricesrc.add_calls(_BUDGET_CON, _now(), 1, purpose)
            r = S.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429:
                if tries == 1: L.warning("429 from %s", url); return None
                wait = min(int(r.headers.get("Retry-After", 20) or 20), 30)
                L.warning("429 from %s, sleeping %ss", url, wait); time.sleep(wait); continue
            r.raise_for_status(); return r.json()
        except Exception as e:
            L.warning("GET %s failed (%d/%d): %s", url, i + 1, tries, e)
            if i + 1 < tries:
                time.sleep(2 * (i + 1))
    return None

def st_get(con, k, default=None):
    r = con.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
    try: return json.loads(r[0]) if r else default
    except Exception: return default

def st_set(con, k, v): con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, json.dumps(v)))

PCT = "1h,24h,7d"

def markets_top(n):
    """Top-n coins by market cap in as few calls as possible: 250 per page, then the remainder in pages of 50.
    Both calls count against the CoinGecko day budget (purpose scan)."""
    rows = get(f"{CG}/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": min(n, 250), "page": 1,
                                       "price_change_percentage": PCT}, purpose="scan") or []
    if n > 250 and rows:
        time.sleep(2)
        for pg in range(6, n // 50 + 1):      # per_page=50: page 6 = ranks 251-300
            rows += get(f"{CG}/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 50, "page": pg,
                                               "price_change_percentage": PCT}, purpose="scan") or []
    return rows

def monad_tokens(con, c, status):
    """Bonus: Monad-ecosystem tokens (CoinGecko category, refreshed every few hours) with a KyberSwap liquidity check on
    Monad (USDC -> token for riskpot.crypto.monad.dex_check_usd; loss incl. price impact must be < max_impact_pct).
    Token addresses come from CoinGecko coin details (max 2 lookups per cycle, cached in state monad_addr)."""
    M = ((c.get("riskpot") or {}).get("crypto") or {}).get("monad") or {}
    if not M: return []
    R = c["riskpot"]["crypto"]; cache = st_get(con, "monad_eco") or {}
    if not cache.get("ts") or datetime.fromisoformat(cache["ts"]) < datetime.now(timezone.utc) - timedelta(hours=float(M.get("refresh_h", 6))):
        time.sleep(2)
        rows = get(f"{CG}/coins/markets", {"vs_currency": "usd", "category": M.get("category", "monad-ecosystem"), "order": "market_cap_desc",
                                           "per_page": 100, "price_change_percentage": PCT}, tries=1, purpose="meta")
        if rows is not None:
            keep = [x for x in rows if (x.get("market_cap") or 0) >= float(R["min_mcap_usd"]) and (x.get("total_volume") or 0) >= float(R["min_vol_usd"])]
            cache = dict(ts=iso(), rows=keep, dex={}); st_set(con, "monad_eco", cache)
    rows = cache.get("rows") or []
    addr = st_get(con, "monad_addr") or {}; looked = 0
    for x in rows:
        if x["id"] in addr or looked >= 2: continue
        time.sleep(2); looked += 1
        d = get(f"{CG}/coins/{x['id']}", {"localization": "false", "tickers": "false", "market_data": "false",
                                           "community_data": "false", "developer_data": "false"}, tries=1, purpose="meta")
        if d is None: break
        dp = (d.get("detail_platforms") or {}).get("monad") or {}
        addr[x["id"]] = dict(a=dp.get("contract_address"), dec=dp.get("decimal_place"))
    st_set(con, "monad_addr", addr)
    import riskpot
    usdc = c["crypto"]["kyberswap"]["token_out"]; amt = float(M.get("dex_check_usd", 200)); out = []
    base_ex = {s.upper() for s in c["crypto"]["exclude_symbols"]}
    for x in rows:
        a = (addr.get(x["id"]) or {}).get("a")
        if not a or a.lower() == usdc.lower() or riskpot.excluded(x, R, base_ex): continue   # stables/wrapped: no check needed
        dx = cache.setdefault("dex", {}).get(x["id"])
        if not dx or dx.get("ts", "") < (datetime.now(timezone.utc) - timedelta(hours=float(M.get("refresh_h", 6)))).isoformat():
            q = get("https://aggregator-api.kyberswap.com/monad/api/v1/routes", {"tokenIn": usdc, "tokenOut": a, "amountIn": str(int(amt * 1e6))},
                    headers={"x-client-id": "paper-trader"}, tries=1)
            try:
                rs = q["data"]["routeSummary"]; loss = (1 - float(rs["amountOutUsd"]) / float(rs["amountInUsd"])) * 100
                dx = dict(ts=iso(), loss_pct=round(loss, 2), ok=loss < float(M.get("max_impact_pct", 3)))
            except Exception:
                dx = dict(ts=iso(), loss_pct=None, ok=False)
            cache["dex"][x["id"]] = dx
        out.append(dict(x, _monad=True, _dex_ok=dx["ok"], _dex=dx.get("loss_pct")))
    st_set(con, "monad_eco", cache)
    status["monad"] = f"{len(rows)} liquid eco tokens, {sum(1 for x in out if x['_dex_ok'])} pass KyberSwap check"
    return out

def meme_ids(con, U):
    """CoinGecko 'meme-token' category (ids + symbols), refreshed once a day with ONE markets call (top 250 memes by market cap -
    every memecoin big enough for the DAY floors). Cached in state meme_ids; on failure the cached set is kept and the config
    blocklist still applies."""
    cache = st_get(con, "meme_ids") or {}
    if not cache.get("ts") or datetime.fromisoformat(cache["ts"]) < datetime.now(timezone.utc) - timedelta(hours=float(U.get("meme_refresh_h", 24))):
        time.sleep(2)
        rows = get(f"{CG}/coins/markets", {"vs_currency": "usd", "category": U.get("meme_category", "meme-token"), "order": "market_cap_desc",
                                           "per_page": 250, "page": 1}, tries=1, purpose="meta")
        if rows:
            cache = dict(ts=iso(), ids=sorted({x["id"] for x in rows}), syms=sorted({(x.get("symbol") or "").upper() for x in rows}))
            st_set(con, "meme_ids", cache)
        else:
            L.warning("meme-token category fetch failed - %s", "using the cached set" if cache else "only the meme blocklist applies")
            if cache: cache["ts"] = iso(); st_set(con, "meme_ids", cache)    # retry tomorrow, not every cycle (rate limits)
            else: st_set(con, "meme_ids", dict(ts=iso(), ids=[], syms=[], failed=True))
    return set(cache.get("ids") or [])

def day_excluded(x, c, memes):
    """v7.2 DAY universe exclusion reason for one CoinGecko markets row (None = allowed): stablecoins, wrapped/staked/bridged,
    exchange tokens (crypto.exclude_symbols + day_universe.exclude_symbols + the riskpot.crypto rules), memecoins (CoinGecko
    meme category ids + meme_blocklist) unless in meme_allow."""
    import riskpot
    cc = c["crypto"]; U = cc.get("day_universe") or {}; R = (c.get("riskpot") or {}).get("crypto") or {}
    R2 = dict(R, exclude_symbols=list(R.get("exclude_symbols", [])) + list(U.get("exclude_symbols", [])))
    sym = (x.get("symbol") or "").upper()
    why = riskpot.excluded(x, R2, {s.upper() for s in cc["exclude_symbols"]})
    if not why and (sym in {s.upper() for s in U.get("meme_blocklist", [])} or x["id"] in memes) and sym not in {s.upper() for s in U.get("meme_allow", [])}:
        why = "memecoin"
    return why

def day_universe(con, c, top, ts):
    """v7.2 wider DAY crypto universe from the SAME top-300 markets snapshot (no extra price calls): the unchanged base set
    (top_n by market cap + core ids) PLUS every coin with market cap >= min_mcap_usd, 24h volume >= min_vol_usd and 24h volume
    >= min_vol_mcap x market cap, excluding stablecoins, wrapped/staked/bridged and exchange tokens (crypto.exclude_symbols +
    the riskpot.crypto exclusion rules) and memecoins (CoinGecko 'meme-token' category + meme_blocklist); meme_allow (DOGE,
    PEPE) overrides the meme exclusion. Returns the extra markets rows (market-cap order) and stores a summary in state
    'day_universe' (Markets tab, sentiment ingest)."""
    import riskpot
    cc = c["crypto"]; U = cc.get("day_universe") or {}
    if not U or not U.get("enabled", True) or not top: return []
    R = (c.get("riskpot") or {}).get("crypto") or {}; base_ex = {s.upper() for s in cc["exclude_symbols"]}
    R2 = dict(R, exclude_symbols=list(R.get("exclude_symbols", [])) + list(U.get("exclude_symbols", [])))
    memes = meme_ids(con, U)
    allow = {s.upper() for s in U.get("meme_allow", [])}; block = {s.upper() for s in U.get("meme_blocklist", [])}
    keep, why_n, seen = [], {}, set()
    for i, x in enumerate(top):
        sym = (x.get("symbol") or "").upper(); mc, v = x.get("market_cap") or 0, x.get("total_volume") or 0
        why = day_excluded(x, c, memes)
        if not why and mc < float(U.get("min_mcap_usd", 5e7)): why = "market cap < floor"
        if not why and v < float(U.get("min_vol_usd", 5e6)): why = "24h volume < floor"
        if not why and (not mc or v / mc < float(U.get("min_vol_mcap", 0.02))): why = "volume/market cap < floor"
        if not why and sym in seen: why = "duplicate symbol"
        if why: k = why.split(" (")[0]; why_n[k] = why_n.get(k, 0) + 1; continue
        seen.add(sym); keep.append(dict(x, _rank=i + 1))
    keep = keep[:int(U.get("max_coins", 150))]
    st_set(con, "day_universe", dict(ts=ts, n=len(keep), screened=len(top), excluded=why_n, memes_known=len(memes), meme_allow=sorted(allow),
                                     coins={x["symbol"].upper(): [x["id"], x["_rank"], round(x.get("market_cap") or 0), round(x.get("total_volume") or 0)] for x in keep}))
    return keep

def _write_crypto(con, ts, sym, row):
    """Insert a crypto price. Market cap and, when the source has none, volume and the 24h range stay from the last row
    so the liquidity cost still sees the last CoinGecko size."""
    prev = con.execute("""SELECT high24, low24, vol24, mcap FROM prices
                          WHERE asset=? AND klass='crypto' AND source!='backfill' ORDER BY ts DESC LIMIT 1""", (sym,)).fetchone()
    high = row.get("high") if row.get("high") else (prev["high24"] if prev else None)
    low = row.get("low") if row.get("low") else (prev["low24"] if prev else None)
    vol = row.get("vol") if row.get("vol") else (prev["vol24"] if prev else None)
    mcap = prev["mcap"] if prev else None
    con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, sym, "crypto", row["usd"], high, low, vol, mcap, row.get("source") or "coingecko", row.get("src_ts") or ts))

def _kyber_usd(c, route):
    """USD price of a Monad token from one KyberSwap route. MON uses the configured MON->USDC size; other tokens a $50 USDC probe."""
    k = ((c.get("crypto") or {}).get("kyberswap") or {})
    if not k: return None
    if route.get("mon"):
        d = get("https://aggregator-api.kyberswap.com/monad/api/v1/routes",
                {"tokenIn": k["token_in"], "tokenOut": k["token_out"], "amountIn": str(int(float(k["amount_mon"]) * 10 ** 18))},
                headers={"x-client-id": "paper-trader"}, tries=1)
        try: return int(d["data"]["routeSummary"]["amountOut"]) / 1e6 / float(k["amount_mon"])
        except Exception: return None
    token, dec, usdc = route.get("token"), route.get("dec"), k.get("token_out")
    if not token or dec is None or not usdc: return None
    amt = 50.0
    d = get("https://aggregator-api.kyberswap.com/monad/api/v1/routes",
            {"tokenIn": usdc, "tokenOut": token, "amountIn": str(int(amt * 1e6))},
            headers={"x-client-id": "paper-trader"}, tries=1)
    try:
        out = int(d["data"]["routeSummary"]["amountOut"]) / (10 ** int(dec))
        return amt / out if out else None
    except Exception:
        return None

def price_tracked(con, c, ts, status):
    """Re-price every tracked coin. One Kraken Ticker call, then Coinbase tickers, then Kyber for Monad tokens.
    CoinGecko simple/price covers coins with no exchange, and any exchange call that just failed.
    Held coins and armed entries may use the reserved slice of the day budget. The general fallback may not.
    A CoinGecko-only coin that still has no price is tried on GeckoTerminal and DexScreener."""
    import pricesrc
    from collections import Counter
    from common import now as now_fn
    syms = pricesrc.tracked(con)
    routes = pricesrc.ensure_routes(con, c, syms, get)
    fx = usd_gbp(con)
    priced, missed = pricesrc.quote_exchanges(syms, routes, fx[0] if fx else None, get, lambda route: _kyber_usd(c, route), ts, pace=time.sleep)
    wrote = set()
    for sym, row in priced.items():
        if row.get("usd"): _write_crypto(con, ts, sym, row); wrote.add(sym)
    now = now_fn()
    guard = set(pricesrc.protected(con, now))
    def _cid(s):
        return (routes.get(s) or {}).get("id") or pricesrc._cgid(con, s)
    held_ids, other_ids = {}, {}
    if pricesrc.held_due(con, c, now):
        for s in syms:
            if s not in guard: continue
            if (routes.get(s) or {}).get("src") == "coingecko" or s in missed:
                cid = _cid(s)
                if cid: held_ids.setdefault(cid, s)
        if held_ids: pricesrc.mark(con, "cg_held_ts", ts)
    if pricesrc.fallback_due(con, c, now):
        for s in syms:
            if s in guard: continue
            if (routes.get(s) or {}).get("src") == "coingecko" or s in missed:
                cid = _cid(s)
                if cid and cid not in held_ids: other_ids.setdefault(cid, s)
        pricesrc.mark(con, "cg_fallback_ts", ts)
    def _pull(ids, purpose):
        if not ids: return False
        d = get(f"{CG}/simple/price", {"ids": ",".join(list(ids)[:250]), "vs_currencies": "usd", "include_last_updated_at": "true"},
                tries=2, purpose=purpose)
        if not d: return False
        for sym, row in pricesrc.parse_cg_simple(d, ids, ts).items():
            _write_crypto(con, ts, sym, row); wrote.add(sym)
        return True
    _pull(held_ids, "held")
    _pull(other_ids, "fallback")
    # CoinGecko-only coins still unpriced: a public pool, most liquid, checked against the last good price.
    # Run when this cycle asked CoinGecko and it missed, or when the general fallback is not allowed to spend.
    need = [s for s in syms if s not in wrote and (routes.get(s) or {}).get("src") == "coingecko"]
    if need and (held_ids or other_ids or not pricesrc.allow(con, c, now, "fallback")):
        for sym, row in pricesrc.backup_quotes(con, c, need, get, ts).items():
            if row.get("usd"): _write_crypto(con, ts, sym, row); wrote.add(sym)
    cnt = Counter()
    for sym in wrote:
        src = con.execute("SELECT source FROM prices WHERE asset=? AND ts=? AND klass='crypto' ORDER BY rowid DESC LIMIT 1", (sym, ts)).fetchone()
        cnt[(src["source"] if src else None) or "?"] += 1
    today, _month = pricesrc.usage(con, now)
    by = pricesrc.purpose_counts(con, now)
    purpose = ", ".join(f"{k} {v}" for k, v in sorted(by.items()))
    status["prices"] = (", ".join(f"{k} {v}" for k, v in sorted(cnt.items()))
                        + f" of {len(syms)}; CoinGecko {today}/{pricesrc.cfg_of(c)['day_budget']} today"
                        + (f" ({purpose})" if purpose else ""))
    return wrote

def crypto(con, c, ts, status):
    """Prices come from the router (Kraken / Coinbase / Kyber / CoinGecko fallback). The top-300 markets snapshot
    still feeds the DAY universe and the RISK screen, but only every few hours so it stays inside the monthly CoinGecko cap.
    A coin already priced by an exchange this cycle keeps that source; the snapshot only fills in market cap and volume."""
    import pricesrc
    from common import now as now_fn
    priced_now = price_tracked(con, c, ts, status)
    if not pricesrc.scan_due(con, c, now_fn()):
        status["screen"] = "top-300 not due (CoinGecko budget; prices from exchanges)"
        return {}
    import riskpot
    cc = c["crypto"]; ex = {s.upper() for s in cc["exclude_symbols"]}
    R = (c.get("riskpot") or {}).get("crypto") or {}
    top = markets_top(int(R.get("top_n", 40)) if R else 40)
    ids = {x["id"] for x in top}
    held = {r[0]: (st_get(con, f"cgid:{r[0]}") or (con.execute("SELECT value FROM state WHERE key=?", (f"cgid:{r[0]}",)).fetchone() or [None])[0])
            for r in con.execute("SELECT DISTINCT asset FROM positions WHERE klass='crypto'")}
    wids = list((cc.get("watch_ids") or {}).keys())          # always-priced watch coins (tokenised gold, RWA tokens)
    need = [i for i in list(cc["core_ids"]) + wids + [v for v in held.values() if v] if i not in ids]
    extra = []
    if need:
        time.sleep(2); extra = get(f"{CG}/coins/markets", {"vs_currency": "usd", "ids": ",".join(dict.fromkeys(need)), "price_change_percentage": PCT}, purpose="scan") or []
    base = [x for x in top if x["symbol"].upper() not in ex and "USD" not in x["symbol"].upper()][:cc["top_n"]]
    uni = day_universe(con, c, top, ts)                       # v7.2: + liquid mid/small caps (same snapshot, no extra calls)
    bids = {x["id"] for x in base}; base += [x for x in uni if x["id"] not in bids]
    by_id = {x["id"]: x for x in top + extra}
    rows = {x["id"]: x for x in base}
    for i in list(cc["core_ids"]) + wids:
        if i in by_id: rows[i] = by_id[i]
    base_syms = {x["symbol"].upper() for x in rows.values()}
    # v7.2: the same exclusions apply to the unchanged base set for DAY trading (exchange tokens like BNB/LEO, gold/RWA watch
    # tokens, memecoins outside meme_allow) - they stay priced (Markets, CORE, RISK) but the DAY pot does not trade them.
    # CORE coins (core_ids) are never excluded; the 'pegged' price heuristic is not applied to the base set.
    U = cc.get("day_universe") or {}
    if U and U.get("enabled", True) and top:
        memes = meme_ids(con, U); dx = {}
        for x in rows.values():
            if x["id"] in cc["core_ids"]: continue
            why = day_excluded(x, c, memes)
            if why and why != "pegged / stable-like": dx[x["symbol"].upper()] = why
        st_set(con, "day_excluded", dx)
    # ---- RISK screen ----
    mon = []
    if R:
        try: mon = monad_tokens(con, c, status)
        except Exception as e: L.warning("monad tokens failed: %s", e)
    risk_syms = set()
    if R and top:
        cand = riskpot.screen(top, c, mon)
        watch = st_get(con, "risk_watch") or {}
        now_ = datetime.now(timezone.utc); until = (now_ + timedelta(hours=float(R.get("watch_hours", 48)))).isoformat()
        for d in [d for d in cand if d["ok"]][:int(R.get("watch_n", 12))]:
            watch[d["a"]] = dict(id=d["id"], until=until)
        watch = {k: v for k, v in watch.items() if v["until"] >= now_.isoformat()}
        mon_by = {x["id"]: x for x in mon}
        for sym, w in watch.items():
            x = by_id.get(w["id"]) or mon_by.get(w["id"])
            if x and x["id"] not in rows and sym not in base_syms: rows[x["id"]] = x; risk_syms.add(sym)
        st_set(con, "risk_watch", watch)
        st_set(con, "risk_screen", dict(ts=ts, universe=len(top), liquid=len(cand), ok=sum(1 for d in cand if d["ok"]),
                                        monad=len(mon), top=cand[:40]))
    for a, i in held.items():                     # held coins are always priced (even if they left the top 300)
        if i and i in by_id and i not in rows: rows[i] = by_id[i]; risk_syms.add(a) if a not in base_syms else None
    st_set(con, "risk_only", sorted(risk_syms))
    from stockrank import themes, risk_stocks
    stock_syms = {s.upper() for s in list(themes(c)) + risk_stocks(c) + list(c["stocks"]["watchlist"])}
    seen = set(); kept = 0
    for x in rows.values():
        sym = x["symbol"].upper()
        if sym in stock_syms: continue                # a coin with the same symbol as a stock ticker (e.g. ONDO.L / B / PL) - skip
        if sym in seen: continue                  # symbol clash between two coins: keep the first (base/higher ranked)
        seen.add(sym)
        con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (f"cgid:{sym}", x["id"]))
        if sym in priced_now:
            # The exchange row written a moment ago stays the price. This snapshot only fills size.
            con.execute("""UPDATE prices SET mcap=COALESCE(?, mcap), vol24=COALESCE(?, vol24),
                            high24=COALESCE(?, high24), low24=COALESCE(?, low24)
                            WHERE ts=? AND asset=? AND klass='crypto'""",
                        (x.get("market_cap"), x.get("total_volume"), x.get("high_24h"), x.get("low_24h"), ts, sym))
            kept += 1
            continue
        if x.get("current_price") is None: continue
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, sym, "crypto",
                    x["current_price"], x.get("high_24h"), x.get("low_24h"), x.get("total_volume"),
                    x.get("market_cap"), "coingecko", freshness.to_iso(x.get("last_updated")) or ts))
    if top:
        pricesrc.mark(con, "cg_scan_ts", ts)
    status["coingecko"] = (f"ok ({len(seen)} screened, {len(seen) - kept} CoinGecko prices, {kept} already from an exchange, "
                           f"top {len(top)}, DAY universe +{len(uni)}, {len(risk_syms)} risk-only)" if top else "FAILED")
    return rows

def crypto_fast(con, c, ts, status):
    """COLLECT_MODE=fast (VPS 5-min cycles between the ~15-min full ones): re-price tracked coins from Kraken, Coinbase
    and KyberSwap. CoinGecko simple/price only for coins with no listing, and only on the fallback interval. No top-300
    screen and no category refresh (those stay from the last full cycle that was allowed to spend the day budget)."""
    price_tracked(con, c, ts, status)
    status["coingecko"] = status.get("prices") or "fast: nothing to re-price"
    return {}

def kyber(con, c, ts, status):
    k = c["crypto"]["kyberswap"]
    d = get("https://aggregator-api.kyberswap.com/monad/api/v1/routes",
            {"tokenIn": k["token_in"], "tokenOut": k["token_out"], "amountIn": str(int(k["amount_mon"] * 10**18))},
            headers={"x-client-id": "paper-trader"})
    try:
        price = int(d["data"]["routeSummary"]["amountOut"]) / 1e6 / k["amount_mon"]
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ts, "MON.DEX", "dex", price, None, None, None, None, "kyberswap", ts))
        status["kyberswap"] = f"ok (MON={price:.5f} USDC)"
    except Exception as e:
        L.error("kyberswap parse failed: %s %s", e, str(d)[:200]); status["kyberswap"] = "FAILED"

def fxrate(con, c, ts, status):
    """USD->GBP: primary = CoinGecko BTC price in GBP / USD (intraday); fallback = open.er-api.com (daily).
    Both are free, keyless HTTPS. Both are fetched so the stored row carries a cross-check.
    Fetched at most every fx.refresh_min (default 55) minutes - saves CoinGecko calls; the trader uses the last stored
    rate (new buys are blocked only if it is older than fx.max_age_h)."""
    last = con.execute("SELECT ts FROM fx WHERE pair='USDGBP' AND rate>0 ORDER BY ts DESC LIMIT 1").fetchone()
    if last and datetime.fromisoformat(last[0]) > datetime.fromisoformat(ts) - timedelta(minutes=float(c["fx"].get("refresh_min", 55))):
        status["fx"] = f"ok (last rate {last[0][11:16]} UTC, refreshed hourly)"; return
    cg = er = None
    d = get(f"{CG}/simple/price", {"ids": "bitcoin", "vs_currencies": "gbp,usd"}, tries=2, purpose="fx")
    try: cg = d["bitcoin"]["gbp"] / d["bitcoin"]["usd"]
    except Exception as e: L.warning("fx coingecko parse failed: %s %s", e, str(d)[:200])
    d = get("https://open.er-api.com/v6/latest/USD", tries=2)
    try: er = float(d["rates"]["GBP"]) if d.get("result") == "success" else None
    except Exception as e: L.warning("fx er-api parse failed: %s %s", e, str(d)[:200])
    ok = lambda r: r is not None and 0.4 < r < 1.5          # sanity band for USD->GBP
    cg, er = (cg if ok(cg) else None), (er if ok(er) else None)
    if cg and er and abs(cg / er - 1) > 0.03: L.warning("fx sources disagree: coingecko %.5f vs er-api %.5f", cg, er)
    if cg: rate, src, alt, alt_src = cg, "coingecko", er, "open.er-api.com"
    elif er: rate, src, alt, alt_src = er, "open.er-api.com", None, None
    else:
        L.error("fx: all sources failed, keeping last stored rate"); status["fx"] = "FAILED (using last stored rate)"; return
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", rate, src, alt, alt_src))
    status["fx"] = f"ok (GBP/USD {1/rate:.4f} via {src}" + (f", er-api {1/er:.4f}" if alt else "") + ")"

def yahoo(sym, rng="1d", interval="15m", tries=3, timeout=20):
    """Yahoo Finance chart API -> (result, [(epoch, close in the QUOTE currency)]). result['meta']['currency'] tells the
    currency: 'GBp' (pence) for most London shares, 'GBP' for some ETFs, 'USD', 'AUD', ...
    Intraday quotes pass a shorter timeout. A failed quote leaves the last stored price in place."""
    d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", {"range": rng, "interval": interval},
            tries=tries, timeout=timeout)
    r = d["chart"]["result"][0]
    return r, [(t, p) for t, p in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if p]

def usd_factor(con, cur):
    """USD per 1 unit of a Yahoo quote currency. GBp (pence) = 0.01 GBP. GBP uses OUR stored USD->GBP rate (table fx) so the
    trader's GBP price (usd x the same rate) equals the London quote exactly; other currencies via Yahoo '<CUR>USD=X'
    (cached hourly in state fxx:<CUR>). Returns None if unknown."""
    cur = (cur or "USD").strip()
    if cur == "USD": return 1.0
    if cur in ("GBp", "GBX", "GBP"):
        r = con.execute("SELECT rate FROM fx WHERE pair='USDGBP' AND rate>0 ORDER BY ts DESC LIMIT 1").fetchone()
        return ((0.01 if cur != "GBP" else 1.0) / r[0]) if r else None
    k = f"fxx:{cur.upper()}"; cache = st_get(con, k)
    if cache and datetime.fromisoformat(cache["ts"]) > datetime.now(timezone.utc) - timedelta(hours=1): return cache["f"]
    try:
        f = float(yahoo(f"{cur.upper()}USD=X", "5d", "1d")[0]["meta"]["regularMarketPrice"])
        if not 0 < f < 1000: raise ValueError(f)
        st_set(con, k, dict(ts=iso(), f=f)); return f
    except Exception as e:
        L.warning("fx %s->USD failed: %s", cur, e); return cache["f"] if cache else None

def stock_meta(con, sym, meta):
    """Remember currency / exchange / name per ticker (state stock_meta) for the dashboard and the trader's cost model."""
    sm = st_get(con, "stock_meta") or {}
    sm[sym] = dict(cur=meta.get("currency"), ex=meta.get("fullExchangeName") or meta.get("exchangeName"),
                   name=meta.get("longName") or meta.get("shortName"), tz=meta.get("exchangeTimezoneName"))
    st_set(con, "stock_meta", sm)

def intraday_stocks(con, c, risk=True):
    """Stocks priced intraday: the fixed watchlist + current CORE picks + held stocks + the top N of the latest universe
    ranking (+ RISK pot: held, today's daily-breakout names and the top riskpot.stock_intraday_n by breakout score).
    The rest of the universe only needs daily closes - keeps Yahoo calls low."""
    import riskpot, stockrank
    out = list(c["stocks"]["watchlist"])
    out += (st_get(con, "core_stocks") or {}).get("picks", [])
    out += [r[0] for r in con.execute("SELECT DISTINCT asset FROM positions WHERE klass='stock'")]
    rk = (st_get(con, "stock_rank") or {}).get("data") or {}
    out += sorted([s for s, r in rk.items() if r.get("eligible")], key=lambda s: -rk[s]["score"])[:int(c["stocks"].get("intraday_top_n", 8))]
    if risk:
        sc = []
        for s in stockrank.risk_stocks(c):
            v, ok, _ = riskpot.stock_score(con, s)
            if v is not None: sc.append((not ok, -v, s))
        n = int((c.get("riskpot") or {}).get("stock_intraday_n", 8))
        out += [s for bad, _, s in sorted(sc) if not bad] + [s for _, _, s in sorted(sc)[:n]]
    return list(dict.fromkeys(out))

def vol_ratio(r, now_ts=None):
    """v7: last-hour volume vs the average hour from Yahoo 15-min bars (5 days): sum of the last 4 COMPLETED bars vs the
    mean 4-bar sum of all earlier bars. None if volume is missing/zero (e.g. indices, some London ETFs)."""
    try:
        ts_, vs = r["timestamp"], r["indicators"]["quote"][0]["volume"]
        bars = [(t, v) for t, v in zip(ts_, vs) if v is not None]
        now_ts = now_ts or datetime.now(timezone.utc).timestamp()
        bars = [b for b in bars if b[0] + 900 <= now_ts]                     # drop the bar still in progress
        while bars and bars[-1][1] == 0: bars.pop()                          # trailing empty (after-close) bars
        if len(bars) < 12: return None
        last, prev = bars[-4:], bars[:-4]
        avg = sum(v for _, v in prev) / len(prev) * 4
        return dict(ratio=round(sum(v for _, v in last) / avg, 3), v1h=sum(v for _, v in last), avg1h=round(avg)) if avg > 0 else None
    except Exception: return None

def stock_quote(sym):
    """(sym, price in quote currency, meta, volume info, [(epoch, close)]) from Yahoo (regularMarketPrice: real-time
    for US, ~15 min delayed for London). The 5-day 15-minute closes come from the same call: burst volume, and the
    intraday series stored for 4h / 24h / RSI / EMA."""
    try:
        r, bars = yahoo(sym, "5d", "15m", tries=2, timeout=12); return sym, r["meta"]["regularMarketPrice"], r["meta"], vol_ratio(r), bars
    except Exception as e:
        L.error("stock %s: Yahoo quote failed: %s", sym, e); return sym, None, None, None, None

def store_stock_bars(con, sym, meta, bars, source="yahoo-15m"):
    """Insert Yahoo intraday closes that are not already stored, converted to USD. Returns how many rows were new.
    The cycle's regularMarketPrice row is separate (newer timestamp), so the latest price stays the live print.
    Calling this again does not duplicate bars already in the table."""
    if not sym or not bars or not meta: return 0
    f = usd_factor(con, meta.get("currency"))
    if not f: return 0
    epochs = []
    for t, p in bars:
        if t is None or not p or p <= 0: continue
        epochs.append((datetime.fromtimestamp(t, timezone.utc).replace(microsecond=0).isoformat(), p * f))
    if not epochs: return 0
    have = {r[0] for r in con.execute("SELECT ts FROM prices WHERE asset=? AND ts>=?", (sym, epochs[0][0]))}
    n = 0
    for tsx, usd in epochs:
        if tsx in have: continue
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (tsx, sym, "stock", usd, None, None, None, None, source, tsx))
        have.add(tsx); n += 1
    return n

def market_proxy(con, c, ts, status):
    """v7: broad-market filter for DAY burst stock entries - index proxy per exchange (config day.burst.market.stocks.proxy,
    e.g. ^GSPC / ^FTSE / ^AXJO), fetched only while that exchange is open: 1h change (last vs 4 bars back) and change vs
    the previous session close. Stored in state 'market_proxy' (NOT in prices, so nothing ever trades an index)."""
    import markets
    P = ((((c.get("day") or {}).get("burst") or {}).get("market") or {}).get("stocks") or {}).get("proxy") or {}
    now_ = datetime.now(timezone.utc); cur = st_get(con, "market_proxy") or {}; done = []
    for ex, sym in P.items():
        if not markets.is_open(ex, now_): continue
        try:
            r, pts = yahoo(sym, "5d", "15m", tries=2, timeout=12)
            tz = markets.EXCH[ex]["tz"]; today = datetime.fromtimestamp(pts[-1][0], timezone.utc).astimezone(tz).date()
            prev = [p for t_, p in pts if datetime.fromtimestamp(t_, timezone.utc).astimezone(tz).date() < today]
            last = r["meta"].get("regularMarketPrice") or pts[-1][1]
            ago = [p for t_, p in pts if t_ <= pts[-1][0] - 3600]
            cur[ex] = dict(sym=sym, ts=ts, price=last, chg1h=round((last / ago[-1] - 1) * 100, 3) if ago else None,
                           chg_day=round((last / prev[-1] - 1) * 100, 3) if prev else None)
            done.append(f"{sym} 1h {cur[ex]['chg1h']}% day {cur[ex]['chg_day']}%")
        except Exception as e:
            L.warning("market proxy %s failed: %s", sym, e)
    st_set(con, "market_proxy", cur)
    status["market_proxy"] = ", ".join(done) or "all markets closed"

def stocks(con, c, ts, status):
    """Intraday stock quotes (Yahoo, 8 threads; DB writes on the main thread), converted to USD (prices table is USD).
    Market-hours aware: a ticker is fetched while its exchange is open, and once more after the close (to capture the
    closing price); otherwise skipped (saves calls nights/weekends - the trader does not trade closed markets anyway).
    Each fetch also stores the 5-day 15-minute closes Yahoo already returned. Names the Day/Risk table still scores
    after they leave the top-N list are refreshed while their market is open, at most every 15 minutes."""
    from concurrent.futures import ThreadPoolExecutor
    import markets
    syms = intraday_stocks(con, c)
    base = set(intraday_stocks(con, c, risk=False))
    st_set(con, "risk_only_stocks", sorted(set(syms) - base))      # priced only for the RISK pot (DAY ignores them)
    now_ = datetime.now(timezone.utc); todo = []
    for s in syms:
        if markets.is_open(s, now_): todo.append(s); continue
        lc = markets.last_close(s, now_)
        last = con.execute("SELECT MAX(ts) FROM prices WHERE asset=? AND source='yahoo'", (s,)).fetchone()[0]
        if not last or (lc and datetime.fromisoformat(last) < lc + timedelta(minutes=25)): todo.append(s)   # closing price (Yahoo LSE delay ~15 min)
    cut = (now_ - timedelta(minutes=15)).isoformat(); symset = set(syms)
    for s, in con.execute("SELECT DISTINCT asset FROM prices WHERE klass='stock'"):
        if s in symset or not markets.is_open(s, now_): continue
        last = con.execute("SELECT MAX(ts) FROM prices WHERE asset=? AND klass='stock' AND source!='backfill'", (s,)).fetchone()[0]
        if not last or last < cut: todo.append(s)
    with ThreadPoolExecutor(8) as ex: res = list(ex.map(stock_quote, todo))
    ok = bars_n = 0
    svol = st_get(con, "stock_vol") or {}
    for sym, price, meta, vi, bars in res:
        if not price: continue
        if vi: svol[sym] = dict(ts=ts, **vi)
        f = usd_factor(con, meta.get("currency"))
        if not f: L.warning("stock %s: unknown currency %s", sym, meta.get("currency")); continue
        stock_meta(con, sym, meta)
        bars_n += store_stock_bars(con, sym, meta, bars)
        rmt = meta.get("regularMarketTime")
        src_ts = datetime.fromtimestamp(rmt, timezone.utc).replace(microsecond=0).isoformat() if rmt else ts
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, sym, "stock", price * f, None, None, None, None, "yahoo", src_ts))
        ok += 1
    st_set(con, "stock_vol", {k: v for k, v in svol.items() if v["ts"] >= (now_ - timedelta(days=2)).isoformat()})
    status["stocks"] = (f"{ok}/{len(todo)} quotes ok (Yahoo), {bars_n} new 15m bars, "
                        f"{len(syms) - len([s for s in syms if s in set(todo)])} skipped (market closed, closing price stored)")

def gas(con, c, ts, status):
    """Simulated Monad network fee per swap: eth_gasPrice (public RPC) * gas_per_swap, MON -> USD (latest MON price) -> GBP.
    Stored in state 'gas'; the trader falls back to gas.fallback_gbp if this is missing/stale."""
    g = c.get("gas") or {}
    try:
        wei = None
        for i in range(4):                     # the public RPC intermittently drops TLS handshakes - retry
            try:
                r = S.post(g.get("rpc", "https://rpc.monad.xyz"), json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}, timeout=15)
                wei = int(r.json()["result"], 16); break
            except Exception as e:
                err = e; time.sleep(1.5 * (i + 1))
        if wei is None: raise err
        mon = con.execute("SELECT price FROM prices WHERE asset='MON' AND source!='backfill' ORDER BY ts DESC LIMIT 1").fetchone()
        fx = con.execute("SELECT rate FROM fx WHERE pair='USDGBP' ORDER BY ts DESC LIMIT 1").fetchone()
        if not mon or not fx: raise ValueError("no MON price / FX rate yet")
        units = int(g.get("gas_per_swap", 200000)); m = wei * units / 1e18
        d = dict(ts=ts, gwei=round(wei / 1e9, 3), units=units, mon=m, usd=m * mon[0], gbp=m * mon[0] * fx[0], mon_usd=mon[0], src="rpc.monad.xyz")
        con.execute("INSERT OR REPLACE INTO state VALUES('gas',?)", (json.dumps(d),))
        status["gas"] = f"ok ({d['gwei']:.1f} gwei, {m:.4f} MON = £{d['gbp']:.5f}/swap)"
    except Exception as e:
        L.warning("gas price failed: %s", e); status["gas"] = f"FAILED ({e}); trader uses fallback"

def daily(con, c, status):
    """Daily closes (Yahoo, 1 year; converted to USD) for the CORE universe + RISK stocks, at most stocks.daily_batch stale tickers per cycle so the whole
    universe refreshes over a few cycles each day without hammering the API. Then re-ranks (state 'stock_rank')."""
    from stockrank import rank, themes
    from stockrank import risk_stocks
    S_ = c["stocks"]; uni = list(dict.fromkeys(list(themes(c)) + risk_stocks(c) + list(S_["watchlist"]))); maxh = float(S_.get("daily_max_age_h", 20)); n = int(S_.get("daily_batch", 20))
    cut = (datetime.now(timezone.utc) - timedelta(hours=maxh)).isoformat()
    last = {r[0][len("dfetch:"):]: r[1] for r in con.execute("SELECT key, value FROM state WHERE key LIKE 'dfetch:%'")}
    todo = sorted([s for s in uni if last.get(s, "") < cut], key=lambda s: last.get(s, ""))[:n]
    ok = 0
    for sym in todo:
        try:
            r, pts = yahoo(sym, "1y", "1d")
            f = usd_factor(con, r["meta"].get("currency"))     # whole history at today's FX: returns stay in local currency
            if not f: raise ValueError(f"unknown currency {r['meta'].get('currency')}")
            stock_meta(con, sym, r["meta"])
            for t, p in pts:
                con.execute("INSERT OR REPLACE INTO daily_closes VALUES(?,?,?)",
                            (sym, datetime.fromtimestamp(t, timezone.utc).date().isoformat(), p * f))
            con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (f"dfetch:{sym}", iso())); ok += 1
            time.sleep(0.4)
        except Exception as e:
            L.warning("daily closes %s failed: %s", sym, e)
    have = sum(1 for s in uni if last.get(s, "") >= cut) + ok
    if todo or not con.execute("SELECT 1 FROM state WHERE key='stock_rank'").fetchone():
        rk = rank(con, c)
        con.execute("INSERT OR REPLACE INTO state VALUES('stock_rank',?)", (json.dumps(dict(ts=iso(), data=rk)),))
    try: extra = crypto_daily(con, c)
    except Exception as e: L.warning("crypto daily failed: %s", e); extra = 0
    status["daily"] = f"{have}/{len(uni)} fresh (+{ok} this cycle)" + (f", crypto candidates +{extra}" if extra else "")

def exchange_points(route, kind, fx):
    """[(unix seconds, USD close)] from Kraken or Coinbase OHLC. None when this coin is not on an exchange,
    or the call failed (the caller then uses CoinGecko). kind is 'daily' or 'hourly'."""
    import pricesrc
    src, pair = (route or {}).get("src"), (route or {}).get("pair")
    if src not in ("kraken", "coinbase") or not pair: return None
    if src == "kraken":
        d = get(f"{pricesrc.KRAKEN}/OHLC", {"pair": pair, "interval": 1440 if kind == "daily" else 60}, tries=1)
        raw = pricesrc.parse_kraken_ohlc(d, route) if d else None
    else:
        d = get(f"{pricesrc.COINBASE}/products/{pair}/candles", {"granularity": 86400 if kind == "daily" else 3600}, tries=1)
        raw = pricesrc.parse_coinbase_candles(d) if isinstance(d, list) else None
    if not raw: return None
    out = []
    for t, close in raw:
        usd = pricesrc.to_usd(close, route.get("quote"), fx)
        if usd: out.append((t, usd))
    return out or None

def crypto_daily(con, c):
    """Daily USD closes for one CORE crypto candidate. Kraken or Coinbase OHLC when that coin is listed there;
    CoinGecko market_chart only otherwise (and if the exchange call failed). One coin per cycle. A skipped chart
    is not marked done, so the next cycle retries it."""
    import pricesrc
    from stockrank import crypto_ids
    ids = crypto_ids(c)
    if not ids: return 0
    maxh = float(c["stocks"].get("daily_max_age_h", 20))
    cut = (datetime.now(timezone.utc) - timedelta(hours=maxh)).isoformat()
    last = {r[0][len("dfetch:"):]: r[1] for r in con.execute("SELECT key, value FROM state WHERE key LIKE 'dfetch:%'")}
    todo = sorted([s for s in ids if str(last.get(s, "")).strip('"') < cut], key=lambda s: str(last.get(s, "")))
    if not todo: return 0
    sym = todo[0]; cid = ids[sym]
    route = pricesrc.routes_of(con).get(sym) or {}
    fx = usd_gbp(con)
    by = {}
    pts = exchange_points(route, "daily", fx[0] if fx else None)
    if pts:
        for t, p in pts:
            by[datetime.fromtimestamp(t, timezone.utc).date().isoformat()] = p
    if not by:
        time.sleep(2)
        d = get(f"{CG}/coins/{cid}/market_chart", {"vs_currency": "usd", "days": "365"}, tries=1, purpose="chart")
        for ms, p in ((d or {}).get("prices") or []):
            if p and p > 0: by[datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()] = float(p)
    if not by:
        L.warning("crypto daily %s (%s) unavailable", sym, cid); return 0
    for day, p in by.items():
        con.execute("INSERT OR REPLACE INTO daily_closes VALUES(?,?,?)", (sym, day, p))
    con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (f"cgid:{sym}", cid))
    con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (f"dfetch:{sym}", iso()))
    L.info("crypto daily %s: %d closes via %s", sym, len(by), route.get("src") if pts else "coingecko")
    return 1

def backfill(con, c, crypto_rows, cg_limit=3):
    """One-off hourly history so momentum works from day one. Yahoo for stocks. Exchange OHLC for coins listed on
    Kraken or Coinbase (a few per cycle, no CoinGecko). CoinGecko market_chart only for the rest, a few per cycle,
    and a chart the budget skipped is left unmarked so the next cycle retries it."""
    import pricesrc
    focus = set(c["strategy"]["focus_assets"])
    routes = pricesrc.routes_of(con)
    fx = usd_gbp(con); fxr = fx[0] if fx else None
    coins = {}
    for s in pricesrc.tracked(con):
        if (routes.get(s) or {}).get("src") in ("kraken", "coinbase"): coins[s] = (routes.get(s) or {}).get("id")
    for x in (crypto_rows or {}).values():
        sym = x["symbol"].upper()
        if (routes.get(sym) or {}).get("src") in ("kraken", "coinbase"): continue
        coins.setdefault(sym, x.get("id"))
    ordered = sorted(coins, key=lambda s: ((routes.get(s) or {}).get("src") not in ("kraken", "coinbase"), s not in focus, s))
    todo = [(s, "stock", s) for s in intraday_stocks(con, c)]
    todo += [(s, "crypto", coins[s]) for s in ordered]
    done = cg = ex = 0; cg_blocked = False
    for asset, klass, ref in todo:
        if con.execute("SELECT 1 FROM state WHERE key=?", (f"bf:{asset}",)).fetchone(): continue
        try:
            if klass != "crypto":
                r_, pts = yahoo(ref, "5d", "60m"); f = usd_factor(con, r_["meta"].get("currency")) or 1.0
                pts = [(t_, p * f) for t_, p in pts]
            else:
                route = routes.get(asset) or {}
                listed = route.get("src") in ("kraken", "coinbase")
                pts = None
                if listed:
                    if ex >= cg_limit: continue
                    ex += 1
                    pts = exchange_points(route, "hourly", fxr)
                if not pts:
                    if cg_blocked or cg >= cg_limit or not ref: continue
                    cg += 1; time.sleep(6)
                    d = get(f"{CG}/coins/{ref}/market_chart", {"vs_currency": "usd", "days": 2}, tries=1, purpose="chart")
                    if not d:
                        L.info("backfill %s deferred (CoinGecko budget or rate limit)", asset); cg_blocked = True; continue
                    pts = [(t / 1000, p) for t, p in d["prices"]]
            first = con.execute("SELECT MIN(ts) FROM prices WHERE asset=?", (asset,)).fetchone()[0]
            for t, p in pts:
                tsx = datetime.fromtimestamp(t, timezone.utc).replace(microsecond=0).isoformat()
                if first and tsx >= first: continue
                con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (tsx, asset, klass, p, None, None, None, None, "backfill", tsx))
            con.execute("INSERT INTO state VALUES(?,?)", (f"bf:{asset}", iso()))
            done += 1
        except Exception as e:
            L.warning("backfill %s failed: %s", asset, e)
    return done

def main():
    """COLLECT_MODE=fast (the VPS 5-min cycles): exchange prices, FX if due, KyberSwap, gas, stocks, market proxy.
    Full (~15 min): the same prices, plus the top-300 screen when the CoinGecko day budget allows it, categories,
    daily closes and a few backfills. GitHub Actions and the box fallback always run full."""
    fast = os.environ.get("COLLECT_MODE", "").lower() == "fast"
    c, con, ts, status = cfg(), db(), iso(), {}
    bind_budget(con, c)
    rows = {}; n = 0
    t_all = time.perf_counter()
    try:
        for name, fn in (("coingecko", crypto_fast if fast else crypto), ("fx", fxrate), ("kyberswap", kyber), ("gas", gas), ("stocks", stocks), ("market_proxy", market_proxy)):
            t1 = time.perf_counter()
            try:
                r = fn(con, c, ts, status)
                if name == "coingecko": rows = r or {}
            except Exception as e:
                L.exception("%s failed", name); status[name] = f"FAILED: {e}"
            con.commit()
            print(f"[timing] collector {name} {time.perf_counter() - t1:.1f}s", flush=True)
        if not fast:
            t1 = time.perf_counter()
            try: daily(con, c, status)
            except Exception as e: L.exception("daily closes failed"); status["daily"] = f"FAILED: {e}"
            print(f"[timing] collector daily {time.perf_counter() - t1:.1f}s", flush=True)
            t1 = time.perf_counter()
            uni_only = {i for i, x in rows.items() if x.get("_rank")} - set(c["crypto"]["core_ids"]) - set((c["crypto"].get("watch_ids") or {}).keys())
            n = backfill(con, c, {i: x for i, x in rows.items() if i not in uni_only}); con.commit()   # universe coins warm up from snapshots; listed coins use exchange OHLC
            print(f"[timing] collector backfill {time.perf_counter() - t1:.1f}s", flush=True)
        con.execute("INSERT OR REPLACE INTO state VALUES('collector_status',?)", (f"{ts} " + ("[fast] " if fast else "") + "; ".join(f"{k}: {v}" for k, v in status.items()),))
        con.commit()
    finally:
        bind_budget(None, None)
    print(f"[timing] collector total {time.perf_counter() - t_all:.1f}s", flush=True)
    print(f"[collector {ts}{' fast' if fast else ''}]", status, f"backfilled={n}")

if __name__ == "__main__":
    main()
