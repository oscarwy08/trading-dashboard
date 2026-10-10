"""Price collector: CoinGecko (crypto), KyberSwap (MON/USDC on Monad), xStocks (+Yahoo fallback), USD->GBP FX,
Monad gas price (simulated network fee), daily closes of the CORE stock universe (Yahoo) + ranking.
All asset prices are stored in raw USD; the FX rate is stored in table fx and applied by trader/dashboard."""
import json, time, requests
from datetime import datetime, timedelta, timezone
from common import cfg, db, iso, log

L = log("collector")
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0 (paper-trader)"
CG = "https://api.coingecko.com/api/v3"

def get(url, params=None, headers=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                if tries == 1: L.warning("429 from %s", url); return None
                wait = min(int(r.headers.get("Retry-After", 20) or 20), 30)
                L.warning("429 from %s, sleeping %ss", url, wait); time.sleep(wait); continue
            r.raise_for_status(); return r.json()
        except Exception as e:
            L.warning("GET %s failed (%d/%d): %s", url, i + 1, tries, e); time.sleep(2 * (i + 1))
    return None

def st_get(con, k, default=None):
    r = con.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
    try: return json.loads(r[0]) if r else default
    except Exception: return default

def st_set(con, k, v): con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, json.dumps(v)))

PCT = "1h,24h,7d"

def markets_top(n):
    """Top-n coins by market cap in as few calls as possible: 250 per page, then the remainder in pages of 50."""
    rows = get(f"{CG}/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": min(n, 250), "page": 1,
                                       "price_change_percentage": PCT}) or []
    if n > 250 and rows:
        time.sleep(2)
        for pg in range(6, n // 50 + 1):      # per_page=50: page 6 = ranks 251-300
            rows += get(f"{CG}/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 50, "page": pg,
                                               "price_change_percentage": PCT}) or []
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
                                           "per_page": 100, "price_change_percentage": PCT}, tries=1)
        if rows is not None:
            keep = [x for x in rows if (x.get("market_cap") or 0) >= float(R["min_mcap_usd"]) and (x.get("total_volume") or 0) >= float(R["min_vol_usd"])]
            cache = dict(ts=iso(), rows=keep, dex={}); st_set(con, "monad_eco", cache)
    rows = cache.get("rows") or []
    addr = st_get(con, "monad_addr") or {}; looked = 0
    for x in rows:
        if x["id"] in addr or looked >= 2: continue
        time.sleep(2); looked += 1
        d = get(f"{CG}/coins/{x['id']}", {"localization": "false", "tickers": "false", "market_data": "false",
                                           "community_data": "false", "developer_data": "false"}, tries=1)
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

def crypto(con, c, ts, status):
    """ONE top-300 markets snapshot (2 calls) feeds: the base set (top_n + core ids, priced as before), and the RISK pot
    breakout screen (riskpot.screen). Only the best riskpot.crypto.watch_n candidates (+ recently screened, + held) are
    stored as prices each cycle; the screen itself goes to state 'risk_screen' (small JSON) - keeps the DB small."""
    import riskpot
    cc = c["crypto"]; ex = {s.upper() for s in cc["exclude_symbols"]}
    R = (c.get("riskpot") or {}).get("crypto") or {}
    top = markets_top(int(R.get("top_n", 40)) if R else 40)
    ids = {x["id"] for x in top}
    held = {r[0]: (st_get(con, f"cgid:{r[0]}") or (con.execute("SELECT value FROM state WHERE key=?", (f"cgid:{r[0]}",)).fetchone() or [None])[0])
            for r in con.execute("SELECT DISTINCT asset FROM positions WHERE klass='crypto'")}
    need = [i for i in list(cc["core_ids"]) + [v for v in held.values() if v] if i not in ids]
    extra = []
    if need:
        time.sleep(2); extra = get(f"{CG}/coins/markets", {"vs_currency": "usd", "ids": ",".join(dict.fromkeys(need)), "price_change_percentage": PCT}) or []
    base = [x for x in top if x["symbol"].upper() not in ex and "USD" not in x["symbol"].upper()][:cc["top_n"]]
    by_id = {x["id"]: x for x in top + extra}
    rows = {x["id"]: x for x in base}
    for i in cc["core_ids"]:
        if i in by_id: rows[i] = by_id[i]
    base_syms = {x["symbol"].upper() for x in rows.values()}
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
    seen = set()
    for x in rows.values():
        sym = x["symbol"].upper()
        if sym in seen: continue                  # symbol clash between two coins: keep the first (base/higher ranked)
        seen.add(sym)
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?)", (ts, sym, "crypto",
                    x["current_price"], x.get("high_24h"), x.get("low_24h"), x.get("total_volume"),
                    x.get("market_cap"), "coingecko"))
        con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (f"cgid:{sym}", x["id"]))
    status["coingecko"] = (f"ok ({len(seen)} coins priced, top {len(top)} screened, {len(risk_syms)} risk-only)" if rows else "FAILED")
    return rows

def kyber(con, c, ts, status):
    k = c["crypto"]["kyberswap"]
    d = get("https://aggregator-api.kyberswap.com/monad/api/v1/routes",
            {"tokenIn": k["token_in"], "tokenOut": k["token_out"], "amountIn": str(int(k["amount_mon"] * 10**18))},
            headers={"x-client-id": "paper-trader"})
    try:
        price = int(d["data"]["routeSummary"]["amountOut"]) / 1e6 / k["amount_mon"]
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?)",
                    (ts, "MON.DEX", "dex", price, None, None, None, None, "kyberswap"))
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
    d = get(f"{CG}/simple/price", {"ids": "bitcoin", "vs_currencies": "gbp,usd"}, tries=2)
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

def yahoo(sym, rng="1d", interval="15m"):
    d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", {"range": rng, "interval": interval})
    r = d["chart"]["result"][0]
    return r, [(t, p) for t, p in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if p]

def intraday_stocks(con, c, risk=True):
    """xStocks priced every cycle: the fixed watchlist + current CORE picks + held stocks + the top N of the latest
    universe ranking (the rest of the universe only needs daily closes for the weekly ranking - keeps API calls low)."""
    out = list(c["stocks"]["watchlist"])
    def st(k):
        r = con.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
        try: return json.loads(r[0]) if r else None
        except Exception: return None
    out += (st("core_stocks") or {}).get("picks", [])
    out += [r[0] for r in con.execute("SELECT DISTINCT asset FROM positions WHERE klass='stock'")]
    if risk: out += list((c.get("riskpot") or {}).get("stocks") or [])          # RISK pot speculative xStocks
    rk = (st("stock_rank") or {}).get("data") or {}
    top = sorted([s for s, r in rk.items() if r.get("eligible")], key=lambda s: -rk[s]["score"])[:int(c["stocks"].get("intraday_top_n", 8))]
    return list(dict.fromkeys(out + top))

def stock_quote(sym):
    d = get(f"https://api.xstocks.fi/api/v2/public/assets/{sym}/price-data", tries=2)
    price = (d or {}).get("quote")
    if price: return sym, price, "xstocks"
    try: return sym, yahoo(sym.rstrip("x"))[0]["meta"]["regularMarketPrice"], "yahoo"     # e.g. weekends: xStocks quote is null
    except Exception as e: L.error("stock %s: all sources failed: %s", sym, e); return sym, None, None

def stocks(con, c, ts, status):
    """Intraday xStocks quotes, fetched in parallel (8 threads; DB writes stay on the main thread)."""
    from concurrent.futures import ThreadPoolExecutor
    syms = intraday_stocks(con, c)
    base = set(intraday_stocks(con, c, risk=False))
    st_set(con, "risk_only_stocks", sorted(set(syms) - base))      # priced only for the RISK pot (DAY ignores them)
    with ThreadPoolExecutor(8) as ex: res = list(ex.map(stock_quote, syms))
    ok = fb = 0
    for sym, price, src in res:
        if not price: continue
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?)", (ts, sym, "stock", price, None, None, None, None, src))
        ok += 1; fb += src == "yahoo"
    status["stocks"] = f"{ok}/{len(syms)} ok, {fb} via Yahoo fallback"

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
    """Daily closes (Yahoo, 1 year) for the CORE universe, at most stocks.daily_batch stale tickers per cycle so the whole
    universe refreshes over a few cycles each day without hammering the API. Then re-ranks (state 'stock_rank')."""
    from stockrank import rank, themes
    S_ = c["stocks"]; uni = list(themes(c)); maxh = float(S_.get("daily_max_age_h", 20)); n = int(S_.get("daily_batch", 20))
    cut = (datetime.now(timezone.utc) - timedelta(hours=maxh)).isoformat()
    last = {r[0][len("dfetch:"):]: r[1] for r in con.execute("SELECT key, value FROM state WHERE key LIKE 'dfetch:%'")}
    todo = sorted([s for s in uni if last.get(s, "") < cut], key=lambda s: last.get(s, ""))[:n]
    ok = 0
    for sym in todo:
        try:
            r, pts = yahoo(sym.rstrip("x"), "1y", "1d")
            for t, p in pts:
                con.execute("INSERT OR REPLACE INTO daily_closes VALUES(?,?,?)",
                            (sym, datetime.fromtimestamp(t, timezone.utc).date().isoformat(), p))
            con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (f"dfetch:{sym}", iso())); ok += 1
            time.sleep(0.4)
        except Exception as e:
            L.warning("daily closes %s failed: %s", sym, e)
    have = sum(1 for s in uni if last.get(s, "") >= cut) + ok
    if todo or not con.execute("SELECT 1 FROM state WHERE key='stock_rank'").fetchone():
        rk = rank(con, c)
        con.execute("INSERT OR REPLACE INTO state VALUES('stock_rank',?)", (json.dumps(dict(ts=iso(), data=rk)),))
    status["daily"] = f"{have}/{len(uni)} fresh (+{ok} this cycle)"

def backfill(con, c, crypto_rows, cg_limit=3):
    """One-off hourly history so momentum works from day one. Yahoo for stocks (all at once),
    CoinGecko for crypto (a few per run, focus coins first, to respect free-tier rate limits)."""
    focus = c["strategy"]["focus_assets"]
    coins = sorted(crypto_rows.values(), key=lambda x: x["symbol"].upper() not in focus)
    todo = [(s, "stock", s.rstrip("x")) for s in intraday_stocks(con, c)]
    todo += [(x["symbol"].upper(), "crypto", x["id"]) for x in coins]
    done = cg = 0
    for asset, klass, ref in todo:
        if con.execute("SELECT 1 FROM state WHERE key=?", (f"bf:{asset}",)).fetchone(): continue
        if klass == "crypto" and cg >= cg_limit: break
        try:
            if klass == "crypto":
                cg += 1; time.sleep(6)
                d = get(f"{CG}/coins/{ref}/market_chart", {"vs_currency": "usd", "days": 2}, tries=1)
                if not d: L.info("backfill %s deferred (rate limit)", asset); break
                pts = [(t / 1000, p) for t, p in d["prices"]]
            else:
                pts = yahoo(ref, "5d", "60m")[1]
            first = con.execute("SELECT MIN(ts) FROM prices WHERE asset=?", (asset,)).fetchone()[0]
            for t, p in pts:
                tsx = datetime.fromtimestamp(t, timezone.utc).replace(microsecond=0).isoformat()
                if first and tsx >= first: continue
                con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?)",
                            (tsx, asset, klass, p, None, None, None, None, "backfill"))
            con.execute("INSERT INTO state VALUES(?,?)", (f"bf:{asset}", iso()))
            done += 1
        except Exception as e:
            L.warning("backfill %s failed: %s", asset, e)
    return done

def main():
    c, con, ts, status = cfg(), db(), iso(), {}
    rows = {}
    for name, fn in (("coingecko", crypto), ("fx", fxrate), ("kyberswap", kyber), ("gas", gas), ("stocks", stocks)):
        try:
            r = fn(con, c, ts, status)
            if name == "coingecko": rows = r
        except Exception as e:
            L.exception("%s failed", name); status[name] = f"FAILED: {e}"
        con.commit()
    try: daily(con, c, status)
    except Exception as e: L.exception("daily closes failed"); status["daily"] = f"FAILED: {e}"
    n = backfill(con, c, rows); con.commit()
    con.execute("INSERT OR REPLACE INTO state VALUES('collector_status',?)", (f"{ts} " + "; ".join(f"{k}: {v}" for k, v in status.items()),))
    con.commit()
    print(f"[collector {ts}]", status, f"backfilled={n}")

if __name__ == "__main__":
    main()
