"""Fresh quotes immediately before a paper fill. No-op in sim / tests (TRADING_DB or TRADING_OFFLINE):
those runs must keep trading the stored snapshots. Live: one batched CoinGecko simple/price for the
coins about to fill, Yahoo for at most 8 stocks, KyberSwap for at most 4 Monad tokens. A failed
quote leaves the stored price in place.
"""
import json, os
from datetime import datetime, timezone
from common import iso

def enabled():
    return not (os.environ.get("TRADING_DB") or os.environ.get("TRADING_OFFLINE"))

def _cg_id(con, sym):
    r = con.execute("SELECT value FROM state WHERE key=?", (f"cgid:{sym}",)).fetchone()
    if not r: return None
    v = r[0]
    try: v = json.loads(v)
    except Exception: pass
    return v if isinstance(v, str) and v else None

def _apply(px, sym, usd, ts, src_ts):
    q = px.get(sym)
    if not q or not q.get("price_usd"): return
    q["price"] = usd * (q["price"] / q["price_usd"])
    q["price_usd"] = usd
    q["ts"] = ts
    q["source_ts"] = src_ts

def _insert(con, ts, sym, klass, usd, source, src_ts):
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, sym, klass, usd, None, None, None, None, source, src_ts))

def prefetch(con, c, symbols, px):
    """Refresh `symbols` that are about to be filled. Returns how many quotes landed. Never raises."""
    if not enabled() or not symbols: return 0
    try: return _prefetch(con, c, list(dict.fromkeys(symbols)), px)
    except Exception:
        return 0

def _prefetch(con, c, symbols, px):
    import collector
    from freshness import to_iso
    ts = iso(); n = 0
    crypto = [s for s in symbols if (px.get(s) or {}).get("klass") == "crypto"]
    stocks = [s for s in symbols if (px.get(s) or {}).get("klass") == "stock"][:8]
    ids = {}
    for s in crypto:
        i = _cg_id(con, s)
        if i: ids.setdefault(i, s)
    idlist = list(ids)
    for i in range(0, len(idlist), 80):
        chunk = idlist[i:i + 80]
        d = collector.get(f"{collector.CG}/simple/price",
                          {"ids": ",".join(chunk), "vs_currencies": "usd", "include_last_updated_at": "true"}, tries=2)
        if not d: continue
        for cid in chunk:
            row = d.get(cid) or {}; usd = row.get("usd")
            if not usd or usd <= 0: continue
            lu = row.get("last_updated_at")
            src = datetime.fromtimestamp(lu, timezone.utc).replace(microsecond=0).isoformat() if lu else ts
            sym = ids[cid]
            _insert(con, ts, sym, "crypto", usd, "coingecko-live", src)
            _apply(px, sym, usd, ts, src); n += 1
    for s in stocks:
        sym, price, meta, _vi = collector.stock_quote(s)
        if not price or not meta: continue
        f = collector.usd_factor(con, meta.get("currency"))
        if not f: continue
        rmt = meta.get("regularMarketTime")
        src = datetime.fromtimestamp(rmt, timezone.utc).replace(microsecond=0).isoformat() if rmt else (to_iso(meta.get("regularMarketTime")) or ts)
        usd = price * f
        _insert(con, ts, sym, "stock", usd, "yahoo-live", src)
        _apply(px, sym, usd, ts, src); n += 1
    n += _kyber(con, c, crypto, px, ts)
    return n

def _kyber(con, c, crypto, px, ts):
    """Direct Monad route for MON and up to 3 other tokens we have an address for. Quote time is the provider time."""
    import collector
    k = ((c.get("crypto") or {}).get("kyberswap") or {})
    if not k or not crypto: return 0
    n = 0; done = 0
    if "MON" in crypto and "MON" in px and done < 4:
        d = collector.get("https://aggregator-api.kyberswap.com/monad/api/v1/routes",
                          {"tokenIn": k["token_in"], "tokenOut": k["token_out"], "amountIn": str(int(float(k["amount_mon"]) * 10**18))},
                          headers={"x-client-id": "paper-trader"}, tries=1)
        try:
            usd = int(d["data"]["routeSummary"]["amountOut"]) / 1e6 / float(k["amount_mon"])
            if usd > 0:
                _insert(con, ts, "MON", "crypto", usd, "kyberswap-live", ts)
                _apply(px, "MON", usd, ts, ts); n += 1; done += 1
        except Exception:
            pass
    try:
        addr = json.loads((con.execute("SELECT value FROM state WHERE key='monad_addr'").fetchone() or ["{}"])[0]) or {}
    except Exception:
        addr = {}
    usdc = k.get("token_out")
    for s in crypto:
        if done >= 4 or s == "MON": continue
        cid = _cg_id(con, s); meta = addr.get(cid) if cid else None
        token, dec = (meta or {}).get("a"), (meta or {}).get("dec")
        if not token or dec is None or not usdc: continue
        amt = 50  # USDC, small probe
        d = collector.get("https://aggregator-api.kyberswap.com/monad/api/v1/routes",
                          {"tokenIn": usdc, "tokenOut": token, "amountIn": str(int(amt * 1e6))},
                          headers={"x-client-id": "paper-trader"}, tries=1)
        try:
            out = int(d["data"]["routeSummary"]["amountOut"]) / (10 ** int(dec))
            usd = amt / out if out else 0
            if usd > 0:
                _insert(con, ts, s, "crypto", usd, "kyberswap-live", ts)
                _apply(px, s, usd, ts, ts); n += 1; done += 1
        except Exception:
            continue
    return n
