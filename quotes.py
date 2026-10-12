"""Fresh quotes immediately before a paper fill. No-op in sim / tests (TRADING_DB or TRADING_OFFLINE):
those runs must keep trading the stored snapshots. Live: the same price router as the collector
(one Kraken ticker for listed coins, Coinbase tickers, KyberSwap for Monad tokens, one CoinGecko
simple/price for whatever is still missing). Yahoo for at most 8 stocks. A failed quote leaves the
stored price in place.
"""
import os, time
from datetime import datetime, timezone
from common import iso

def enabled():
    return not (os.environ.get("TRADING_DB") or os.environ.get("TRADING_OFFLINE"))

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
    import collector, pricesrc
    from common import usd_gbp
    from freshness import to_iso
    ts = iso(); n = 0
    own = collector._BUDGET_CON is None
    if own: collector.bind_budget(con, c)
    try:
        n = _prefetch_bound(con, c, symbols, px, ts, collector, pricesrc, usd_gbp, to_iso)
    finally:
        if own: collector.bind_budget(None, None)
    return n

def _prefetch_bound(con, c, symbols, px, ts, collector, pricesrc, usd_gbp, to_iso):
    n = 0
    crypto = [s for s in symbols if (px.get(s) or {}).get("klass") == "crypto"]
    stocks = [s for s in symbols if (px.get(s) or {}).get("klass") == "stock"][:8]
    routes = pricesrc.ensure_routes(con, c, crypto, collector.get) if crypto else {}
    fx = usd_gbp(con)
    priced, missed = pricesrc.quote_exchanges(
        crypto, routes, fx[0] if fx else None, collector.get, lambda route: collector._kyber_usd(c, route), ts, pace=time.sleep)
    for sym, row in priced.items():
        if not row.get("usd"): continue
        collector._write_crypto(con, ts, sym, row)
        _apply(px, sym, row["usd"], ts, row.get("src_ts") or ts); n += 1
    ids = {}
    cg = [s for s in crypto if (routes.get(s) or {}).get("src") == "coingecko"]
    for s in missed:
        if s not in cg: cg.append(s)
    for s in cg:
        cid = (routes.get(s) or {}).get("id") or pricesrc._cgid(con, s)
        if cid: ids.setdefault(cid, s)
    if ids:
        d = collector.get(f"{collector.CG}/simple/price",
                          {"ids": ",".join(list(ids)[:250]), "vs_currencies": "usd", "include_last_updated_at": "true"},
                          tries=2, purpose="live")
        for sym, row in pricesrc.parse_cg_simple(d, ids, ts).items():
            collector._write_crypto(con, ts, sym, row)
            _apply(px, sym, row["usd"], ts, row.get("src_ts") or ts); n += 1
    for s in stocks:
        sym, price, meta, _vi, bars = collector.stock_quote(s)
        if not price or not meta: continue
        f = collector.usd_factor(con, meta.get("currency"))
        if not f: continue
        collector.store_stock_bars(con, sym, meta, bars)
        rmt = meta.get("regularMarketTime")
        src = datetime.fromtimestamp(rmt, timezone.utc).replace(microsecond=0).isoformat() if rmt else (to_iso(meta.get("regularMarketTime")) or ts)
        usd = price * f
        _insert(con, ts, sym, "stock", usd, "yahoo-live", src)
        _apply(px, sym, usd, ts, src); n += 1
    return n
