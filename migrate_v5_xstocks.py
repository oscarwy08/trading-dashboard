"""One-off v5 migration (2026-10-10): xStocks (tokenised stocks, NOT available to UK residents) -> normal exchange tickers.
Idempotent (state key 'migrated_v5'). Backs up the DB first (data/trading.db.bak-pre-v5, not committed - see .gitignore).
  * CORE xStock positions (long holds) are CONVERTED 1:1 to the ordinary share (AMDx -> AMD): same quantity, same GBP cost
    basis and average price, no fee and no P&L event (an xStock tracks 1 share of the underlying; the current xStock
    quotes are the Yahoo price of that share). One 'CONVERT' row per position is written to the trades table (Orders tab).
  * DAY xStock positions are CLOSED at the last price as a normal SELL (fee + gas as before): the Day pot must be flat
    overnight and it is the weekend (US market closed), so carrying a converted day position to Monday would break the
    flat-overnight rule.
  * prices / daily_closes / signals / sentiment / state keys are renamed to the plain ticker so momentum history and the
    weekly ranking continue. Historical trades keep their xStock names (they really were xStock trades).
Usage: python3 migrate_v5_xstocks.py   (the trader/collector then run normally)."""
import json, re, shutil, sqlite3
from common import DB, cfg, db, iso, usd_gbp
import trader

XS = re.compile(r"^([A-Z][A-Z0-9.]*)x$")      # xStock symbols end with a lowercase x (crypto symbols are upper case)
def plain(a):
    m = XS.match(a or ""); return m.group(1) if m else a

def main():
    c = cfg(); con = db()
    if con.execute("SELECT 1 FROM state WHERE key='migrated_v5'").fetchone(): print("[migrate v5] already done"); return
    con.close(); bak = DB.with_name(DB.name + ".bak-pre-v5"); shutil.copy2(DB, bak); con = db()
    rate = usd_gbp(con)[0]; ts = iso(); out = []
    lastpx = lambda a: (con.execute("SELECT price FROM prices WHERE asset=? AND source!='backfill' ORDER BY ts DESC LIMIT 1", (a,)).fetchone() or [None])[0]
    gas = trader.gas_gbp(con, c); f = float(c["risk"]["fee_slippage_pct"]) / 100
    for p in [dict(r) for r in con.execute("SELECT * FROM positions WHERE klass='stock'")]:
        a, new = p["asset"], plain(p["asset"])
        if a == new: continue
        usd = lastpx(a); price = usd * rate
        if p["sleeve"] == "day":     # close at last price (normal SELL incl. fee + gas, like the old xStock trade)
            fill = price * (1 - f); proceeds = p["qty"] * fill - gas; pnl = proceeds - p["cost"]
            con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason,price_usd,usd_gbp,gas) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, "day", a, "stock", "SELL", p["qty"], price, fill, p["qty"] * fill, p["qty"] * price * f, pnl,
                         f"xStocks retired (not available to UK residents): Day position closed at the last price (US market closed, Day pot flat overnight); P&L £{pnl:+.2f}",
                         usd, rate, gas))
            con.execute("UPDATE cash SET amount=amount+? WHERE sleeve='day'", (proceeds,))
            con.execute("DELETE FROM positions WHERE sleeve=? AND asset=?", (p["sleeve"], a))
            out.append(f"DAY {a}: closed at £{price:.4f} P&L £{pnl:+.2f}")
        else:                        # convert 1:1, no P&L event
            con.execute("UPDATE positions SET asset=? WHERE sleeve=? AND asset=?", (new, p["sleeve"], a))
            con.execute("INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason,price_usd,usd_gbp,gas) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, p["sleeve"], new, "stock", "CONVERT", p["qty"], price, price, p["qty"] * price, 0.0, None,
                         f"conversion {a} -> {new}: xStocks are not available to UK residents; same {p['qty']:.6g} shares, cost basis £{p['cost']:.2f} "
                         f"(avg £{p['entry']:.4f}) kept, no fee, no P&L event; market value £{p['qty']*price:.2f} at ${usd:.4g}", usd, rate, 0.0))
            out.append(f"{p['sleeve'].upper()} {a} -> {new}: {p['qty']:.6g} sh, cost £{p['cost']:.2f}, value £{p['qty']*price:.2f}")
    # history tables: rename xStock symbols (stock class only for prices)
    for tbl, cond in (("prices", "klass='stock'"), ("daily_closes", "1=1"), ("signals", "1=1"), ("sentiment", "1=1")):
        for (a,) in con.execute(f"SELECT DISTINCT asset FROM {tbl} WHERE {cond}").fetchall():
            n = plain(a)
            if n != a:
                if tbl in ("daily_closes", "sentiment"): con.execute(f"DELETE FROM {tbl} WHERE asset=? AND EXISTS (SELECT 1 FROM {tbl} t2 WHERE t2.asset=? AND t2.{'d' if tbl == 'daily_closes' else 'ts'}={tbl}.{'d' if tbl == 'daily_closes' else 'ts'})", (a, n))
                con.execute(f"UPDATE {tbl} SET asset=? WHERE asset=?", (n, a))
    # state
    def st(k):
        r = con.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
        try: return json.loads(r[0]) if r else None
        except Exception: return None
    def put(k, v): con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, json.dumps(v)))
    cs = st("core_stocks")
    if cs:
        cs["picks"] = [plain(a) for a in cs.get("picks", [])]; cs["info"] = {plain(k): v for k, v in (cs.get("info") or {}).items()}
        cs["source"] = (cs.get("source") or "") + " (v5: renamed from xStocks)"; put("core_stocks", cs)
    for k in ("core_plan", "risk_peaks"):
        v = st(k)
        if v: put(k, {plain(a): x for a, x in v.items()})
    rl = st("core_rebalance_log")
    if rl and rl.get("picks"): rl["picks"] = [plain(a) for a in rl["picks"]]; put("core_rebalance_log", rl)
    for (k,) in con.execute("SELECT key FROM state WHERE key LIKE 'bf:%' OR key LIKE 'dfetch:%' OR key LIKE 'risk_exit:%'").fetchall():
        pre, a = k.split(":", 1)
        if plain(a) != a:
            con.execute("DELETE FROM state WHERE key=?", (f"{pre}:{plain(a)}",)); con.execute("UPDATE state SET key=? WHERE key=?", (f"{pre}:{plain(a)}", k))
    con.execute("DELETE FROM state WHERE key LIKE 'dfetch:%'")      # re-fetch all daily closes once (currency-converted, new names)
    con.execute("DELETE FROM state WHERE key IN ('stock_rank','risk_only_stocks')")
    put("migrated_v5", dict(ts=ts, actions=out, backup=bak.name)); con.commit()
    print("[migrate v5]", *out, sep="\n  ")

if __name__ == "__main__":
    main()
