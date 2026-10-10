"""One-off v6 migration (2026-10-10): one GBP paper account -> TWO accounts, each pot with separate cash per account.
  crypto = Crypto account (Monad wallet) | stocks = Stocks account (Trading 212 Invest). Target funding (config
  accounts.funding): CORE 50/50, DAY 30/30, RISK 20/20 => Crypto £100 / Stocks £100. NO reset, no trades, no P&L effect:
  * holdings stay where they are and belong to the account of their asset class (crypto -> crypto, stock -> stocks);
  * each pot's existing CASH is split between its two accounts (TRANSFER rows, kind 'split'; before the split all pot cash
    counted as the original single account = crypto). For every pot x account the money already committed is
    C = cost basis of open positions - realised P&L of that pot/asset class; ideal cash = target - C. Where a pot holds
    more of one asset class than its target (e.g. DAY £40 crypto vs £30), its cash goes to the other account and the
    ACCOUNT totals are evened out with the other pots' cash (CORE first), so Crypto/Stocks start at £100/£100;
  * the remaining pot-level difference becomes a pending SETTLEMENT inside each account (pot -> pot, same account,
    e.g. crypto DAY -> CORE £10 and stocks CORE -> DAY £10). The trader executes all legs together at the first cycle
    where every leg is covered by cash (DAY: once its crypto positions are sold, at the latest at the 22:00 UK flatten),
    so pot totals never change and nothing moves between accounts. After that every pot x account starts at its target;
  * opening baselines per pot x account (state 'account_split'.opening) are measured like the pots: the money committed
    at the split, so per-account P&L = current equity - baseline (+ later transfers);
  * earlier equity rows get pro-forma per-account values (opening + realised + unrealised P&L of that account at that
    time, from the trade history and stored prices) so the account charts start at the beginning.
Idempotent (state key 'account_split'). Usage: python3 migrate_v6_accounts.py   (backs up to data/trading.db.bak-pre-v6)."""
import json, re, sqlite3
from bisect import bisect_right
from datetime import datetime
from common import ACCOUNTS, DB, SLEEVES, acct_of, cfg, db, funding, iso, move_cash, start_cash, sync_pot_cash

def plain(a):
    m = re.match(r"^([A-Z][A-Z0-9.]*)x$", a or ""); return m.group(1) if m else a

def committed(con):
    """C[pot][acct] = cost basis of open positions - realised P&L (money of the account that is not cash)."""
    C = {s: {a: 0.0 for a in ACCOUNTS} for s in SLEEVES}
    for r in con.execute("SELECT sleeve, klass, cost FROM positions"): C[r[0]][acct_of(r[1])] += r[2]
    for r in con.execute("SELECT sleeve, klass, pnl FROM trades WHERE side='SELL' AND pnl IS NOT NULL"): C[r[0]][acct_of(r[1])] -= r[2]
    return C

def plan_split(cash, C, T, start):
    """Returns (cash per pot x acct, opening per pot x acct, pending settlement legs, residual per pot)."""
    new, res = {}, {}
    for s in SLEEVES:
        res[s] = cash[s] + sum(C[s].values()) - start[s]               # ~0 (rounding / history); spread pro rata
        ideal = {a: T[s][a] - C[s][a] + res[s] * T[s][a] / (start[s] or 1) for a in ACCOUNTS}
        cr = min(max(ideal["crypto"], 0.0), cash[s]); new[s] = {"crypto": cr, "stocks": cash[s] - cr}
    open_ = lambda s, a: new[s][a] + C[s][a] - res[s] * T[s][a] / (start[s] or 1)
    tgt_c = sum(T[s]["crypto"] for s in SLEEVES)
    d = tgt_c - sum(open_(s, "crypto") for s in SLEEVES)                 # >0: crypto account short -> move pot cash stocks->crypto
    for s in ("core", "risk", "day"):
        if abs(d) < 1e-9: break
        mv = min(d, new[s]["stocks"]) if d > 0 else -min(-d, new[s]["crypto"])
        new[s]["crypto"] += mv; new[s]["stocks"] -= mv; d -= mv
    O = {s: {a: open_(s, a) for a in ACCOUNTS} for s in SLEEVES}
    legs = []
    for a in ACCOUNTS:
        over = [[s, O[s][a] - T[s][a]] for s in SLEEVES if O[s][a] - T[s][a] > 0.005]
        under = [[s, T[s][a] - O[s][a]] for s in SLEEVES if T[s][a] - O[s][a] > 0.005]
        for u in under:
            for o in over:
                m = min(u[1], o[1])
                if m > 0.005: legs.append(dict(account=a, frm=o[0], to=u[0], amount=round(m, 6))); u[1] -= m; o[1] -= m
    return new, O, [dict(account=l["account"], **{"from": l["frm"]}, to=l["to"], amount=l["amount"]) for l in legs], res

def backfill(con, O, upto):
    """Pro-forma per-account columns for equity rows before the split: opening + realised + unrealised P&L at that time."""
    trades = [dict(r) for r in con.execute("SELECT ts, sleeve, asset, klass, side, qty, value, gas, pnl FROM trades WHERE side IN ('BUY','SELL') ORDER BY id")]
    fx = [(r[0], r[1]) for r in con.execute("SELECT ts, rate FROM fx WHERE pair='USDGBP' AND rate>0 ORDER BY ts")]
    fxt = [x[0] for x in fx]; pxc = {}
    def price(a, ts):
        if a not in pxc:
            rows = con.execute("SELECT ts, price FROM prices WHERE asset=? AND source!='kyberswap' ORDER BY ts", (a,)).fetchall()
            pxc[a] = ([r[0] for r in rows], [r[1] for r in rows])
        t, p = pxc[a]; i = bisect_right(t, ts) - 1
        return p[i] if i >= 0 else None
    rows = con.execute("SELECT rowid, ts FROM equity WHERE ts<=? AND accts IS NULL ORDER BY ts", (upto,)).fetchall()
    n = 0
    for rid, ts in rows:
        pos, real = {}, {s: {a: 0.0 for a in ACCOUNTS} for s in SLEEVES}
        for x in trades:
            if x["ts"] > ts: break
            k = (x["sleeve"], plain(x["asset"])); p = pos.setdefault(k, dict(qty=0.0, cost=0.0, klass=x["klass"]))
            if x["side"] == "BUY": p["qty"] += x["qty"]; p["cost"] += x["value"] + (x["gas"] or 0)
            else:
                part = p["cost"] * min(1, x["qty"] / p["qty"]) if p["qty"] else 0
                p["qty"] -= x["qty"]; p["cost"] -= part; real[x["sleeve"]][acct_of(x["klass"])] += x["pnl"] or 0
        i = bisect_right(fxt, ts) - 1; rate = fx[max(i, 0)][1] if fx else None
        ae = {s: {a: O[s][a] + real[s][a] for a in ACCOUNTS} for s in SLEEVES}
        for (s, a), p in pos.items():
            if p["qty"] <= 1e-12: continue
            usd = price(a, ts); val = p["qty"] * usd * rate if usd and rate else p["cost"]
            ae[s][acct_of(p["klass"])] += val - p["cost"]
        tot = {a: sum(ae[s][a] for s in SLEEVES) for a in ACCOUNTS}
        con.execute("UPDATE equity SET crypto=?, stocks=?, accts=? WHERE rowid=?",
                    (tot["crypto"], tot["stocks"], json.dumps({s: {a: [round(ae[s][a], 6), None] for a in ACCOUNTS} for s in SLEEVES}, separators=(",", ":")), rid))
        n += 1
    return n

def migrate(con, c=None):
    c = c or cfg()
    if con.execute("SELECT 1 FROM state WHERE key='account_split'").fetchone(): return None
    T, start = funding(c), start_cash(c)
    cash = {s: 0.0 for s in SLEEVES}
    cash.update({r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")})
    C = committed(con)
    new, O, legs, res = plan_split(cash, C, T, start)
    ts = iso()
    # before the split every pot's cash counted in the original single account (= crypto); split it with TRANSFER rows
    con.execute("DELETE FROM acct_cash")
    con.executemany("INSERT INTO acct_cash VALUES(?,?,?)", [(s, "crypto", cash[s]) for s in SLEEVES] + [(s, "stocks", 0.0) for s in SLEEVES])
    for s in SLEEVES:
        if new[s]["stocks"] > 1e-9:
            move_cash(con, (s, "crypto"), (s, "stocks"), new[s]["stocks"],
                      f"v6 account split: {s.upper()} cash £{cash[s]:.2f} -> Crypto account £{new[s]['crypto']:.2f} / Stocks account £{new[s]['stocks']:.2f} "
                      f"(holdings stay in the account of their asset class; targets {T[s]['crypto']:.0f}/{T[s]['stocks']:.0f})", "split")
    sync_pot_cash(con)
    split_ts = con.execute("SELECT MAX(ts) FROM transfers").fetchone()[0] or ts
    st = dict(ts=split_ts, opening=O, targets=T, pending=legs, committed=C, cash_before=cash, cash_after=new, residual=res,
              note="opening = money of each pot x account at the split; pending = one-off settlement between pots INSIDE one account")
    con.execute("INSERT OR REPLACE INTO state VALUES('account_split',?)", (json.dumps(st),))
    n = backfill(con, O, split_ts)
    con.commit()
    print(f"[migrate v6] account split at {split_ts}: " + " | ".join(
        f"{s.upper()} crypto £{O[s]['crypto']:.2f} (cash £{new[s]['crypto']:.2f}) / stocks £{O[s]['stocks']:.2f} (cash £{new[s]['stocks']:.2f})" for s in SLEEVES)
        + f" | accounts crypto £{sum(O[s]['crypto'] for s in SLEEVES):.2f} / stocks £{sum(O[s]['stocks'] for s in SLEEVES):.2f}"
        + f" | pending settlement {legs} | pro-forma account history for {n} earlier equity rows")
    return st

if __name__ == "__main__":
    con = db()
    if con.execute("SELECT 1 FROM state WHERE key='account_split'").fetchone(): print("[migrate v6] already done")
    else:
        con.close(); bak = DB.with_name(DB.name + ".bak-pre-v6")
        s = sqlite3.connect(DB); d = sqlite3.connect(bak); s.backup(d); d.close(); s.close(); print("backup", bak)
        migrate(db())
