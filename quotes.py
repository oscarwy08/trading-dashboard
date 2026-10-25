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
        if not collector._write_crypto(con, ts, sym, row): continue
        _apply(px, sym, row["usd"], ts, row.get("src_ts") or ts); n += 1
    ids = {}
    cg = [s for s in crypto if (routes.get(s) or {}).get("src") == "coingecko"]
    for s in missed:
        if s not in cg: cg.append(s)
    from common import now as now_fn
    now = now_fn()
    ttl = pricesrc.cfg_of(c)["live_ttl"]
    recent = set()
    for s in cg:
        # Already priced in the last live_ttl_min (this cycle's collector, or an earlier quote): no new call.
        if pricesrc.priced_within(con, s, now, ttl):
            recent.add(s); continue
        cid = (routes.get(s) or {}).get("id") or pricesrc._cgid(con, s)
        if cid: ids.setdefault(cid, s)
    if ids:
        # The same call also re-prices every other CoinGecko-only coin and stands in for the scheduled
        # fallback, so the collector does not spend a second call a few minutes later.
        extra = {}
        if pricesrc.allow(con, c, now, "fallback"):
            for cid, s in pricesrc.cg_only(con, routes).items():
                if cid not in ids: extra[cid] = s
        ask = dict(list(ids.items()) + list(extra.items()))
        ask = dict(list(ask.items())[:250])
        d = collector.get(f"{collector.CG}/simple/price",
                          {"ids": ",".join(ask), "vs_currencies": "usd", "include_last_updated_at": "true"},
                          tries=2, purpose="live")
        if d and extra:
            pricesrc.mark(con, "cg_fallback_ts", ts); pricesrc.mark(con, "cg_held_ts", ts)
        for sym, row in pricesrc.parse_cg_simple(d, ask, ts).items():
            if not collector._write_crypto(con, ts, sym, row): continue
            if sym in px:
                _apply(px, sym, row["usd"], ts, row.get("src_ts") or ts); n += 1
    missed_cg = [s for s in cg if s not in recent and (routes.get(s) or {}).get("src") == "coingecko" and not con.execute(
        "SELECT 1 FROM prices WHERE ts=? AND asset=?", (ts, s)).fetchone()]
    if missed_cg:
        for sym, row in pricesrc.backup_quotes(con, c, missed_cg, collector.get, ts).items():
            if not row.get("usd"): continue
            if not collector._write_crypto(con, ts, sym, row): continue
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
