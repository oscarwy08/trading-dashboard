"""Short text scorecard for the AI routine to relay. All amounts in GBP (£). Total + one line per account (v6: Crypto =
Monad wallet, Stocks = Trading 212) + one line per pot (with its crypto / stocks split)."""
import json
from datetime import datetime, timezone
from common import ACCOUNTS, KILL, LON, SLEEVES, acct_start, db, rebase, transfer_adj, transfer_adj_acct, usd_gbp
from dashboard import stats

con = db(); s = stats(con)
day = datetime.now(LON).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
eq0 = con.execute("SELECT ts, equity, core, day, risk, crypto, stocks FROM equity WHERE ts<? ORDER BY ts DESC LIMIT 1", (day,)).fetchone() \
      or con.execute("SELECT ts, equity, core, day, risk, crypto, stocks FROM equity ORDER BY ts LIMIT 1").fetchone()
today = con.execute("SELECT sleeve, side, asset, pnl FROM trades WHERE ts>=?", (day,)).fetchall()
pct = lambda v: "n/a" if v is None else f"{v*100:+.2f}%"
dp = s["equity"] - eq0["equity"] if eq0 else 0
print(f"PAPER scorecard {datetime.now(LON):%a %d %b %H:%M} UK")
print(f"TOTAL Equity £{s['equity']:.2f} ({pct(s['ret'])} since £{s['start']:.0f} start) | Day P&L £{dp:+.2f}")
print(f"vs BTC buy&hold (£) {pct(s['btc_ret'])} | Max DD {pct(s['max_dd'])} | Win rate {pct(s['win_rate']).lstrip('+')} over {s['closed']} closed")
# ---- v6: the two accounts (crypto = Monad wallet, stocks = Trading 212) ----
base = acct_start(con)
acash = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}
rate = (usd_gbp(con) or [None])[0]
lpx = {r[0]: r[1] for r in con.execute("SELECT asset, price FROM prices WHERE source!='backfill' GROUP BY asset HAVING MAX(ts)")}
pv = {}
for r in con.execute("SELECT sleeve, asset, klass, qty, cost FROM positions"):
    k = (r["sleeve"], "stocks" if r["klass"] == "stock" else "crypto")
    pv[k] = pv.get(k, 0.0) + (r["qty"] * lpx[r["asset"]] * rate if rate and lpx.get(r["asset"]) else r["cost"])
fee = {a: 0.0 for a in ACCOUNTS}; fdet = {a: {} for a in ACCOUNTS}
import trader as _t, re as _re
from common import cfg as _cfg
_c = _cfg()
for r in con.execute("SELECT asset, klass, side, cost, gas FROM trades WHERE side IN ('BUY','SELL')"):
    a = "stocks" if r["klass"] == "stock" else "crypto"; cost = r["cost"] or 0; g = r["gas"] or 0
    if a == "crypto": parts = {"fee": cost, "gas": g}
    elif g > 0 or _re.match(r"^[A-Z][A-Z0-9.]*x$", r["asset"] or ""): parts = {"xStock fee+gas": cost + g}
    else:
        pp = _t.cost_parts(_c, r["asset"], "stock", r["side"]); tot = sum(pp.values()) or 1; parts = {k: cost * v / tot for k, v in pp.items()}
    for k, v in parts.items(): fdet[a][k] = fdet[a].get(k, 0.0) + v; fee[a] += v
LBL = {"crypto": "CRYPTO acct (Monad wallet)", "stocks": "STOCKS acct (Trading 212)"}
for a in ACCOUNTS:
    x = stats(con, None, a); cash = sum(acash.get((s_, a), 0.0) for s_ in SLEEVES); inv = sum(pv.get((s_, a), 0.0) for s_ in SLEEVES)
    st = sum(base[s_][a] for s_ in SLEEVES); eqv = cash + inv
    d0 = (x["equity"] - eq0[a] - rebase(eq0["ts"], transfer_adj_acct(con, a))) if eq0 and eq0[a] is not None else 0
    print(f"{LBL[a]} £{eqv:.2f} ({pct(eqv / st - 1 if st else None)} vs £{st:.0f}) | P&L £{eqv - st:+.2f} | day £{d0:+.2f} | cash £{cash:.2f} | "
          f"invested £{inv:.2f} | realised £{x['realized']:+.2f} | fees £{fee[a]:.2f} ("
          + ", ".join(f"{k} £{v:.3f}" for k, v in fdet[a].items() if v) + ")"
          + " | by pot: " + ", ".join(f"{s_.upper()} £{acash.get((s_, a), 0) + pv.get((s_, a), 0):.2f} (cash £{acash.get((s_, a), 0):.2f})" for s_ in SLEEVES))
asp = json.loads((con.execute("SELECT value FROM state WHERE key='account_split'").fetchone() or ["null"])[0])
if asp and asp.get("pending"):
    print("Account split: pending settlement inside each account (when the cash is free): "
          + "; ".join(f"{l['account']} {l['from'].upper()}->{l['to'].upper()} £{l['amount']:.2f}" for l in asp["pending"]))
plan = json.loads((con.execute("SELECT value FROM state WHERE key='core_plan'").fetchone() or ["{}"])[0])
risk = json.loads((con.execute("SELECT value FROM state WHERE key='risk'").fetchone() or ["{}"])[0]).get("day_sleeve", {})
rp = json.loads((con.execute("SELECT value FROM state WHERE key='riskpot_status'").fetchone() or ["{}"])[0]) or {}
for sl in SLEEVES:
    x = stats(con, sl); t = [r for r in today if r["sleeve"] == sl]; closed = [r for r in t if r["pnl"] is not None]
    opn = [r[0] for r in con.execute("SELECT asset FROM positions WHERE sleeve=?", (sl,))]
    # pot history is rebased by cash transfers made after the reference point (a transfer is not P&L)
    d0 = (x["equity"] - eq0[sl] - rebase(eq0["ts"], transfer_adj(con, sl))) if eq0 and eq0[sl] is not None else \
         (x["equity"] - x["start"] if sl == "risk" else 0)
    best = max(closed, key=lambda r: r["pnl"], default=None); worst = min(closed, key=lambda r: r["pnl"], default=None)
    extra = (f"built {sum(1 for v in plan.values() if v.get('status') == 'built')}/{len(plan)}" if sl == "core" else
             (f"PAUSED (drawdown {rp.get('dd_pct', 0):+.1f}%, needs review: create RISK_RESUME)" if rp.get("halted") else
              f"active, dd {rp.get('dd_pct', 0):+.1f}%, {rp.get('new_today', 0)} new today") if sl == "risk" else
             ("HALTED (loss cap)" if risk.get("halted") else "entries open" if risk.get("entries_open") else "entries closed"))
    print(f"{sl.upper():4} £{x['equity']:.2f} ({pct(x['ret'])} vs £{x['start']:.0f}) | day £{d0:+.2f} | realised £{x['realized']:+.2f} | "
          f"win {pct(x['win_rate']).lstrip('+')} of {x['closed']} | today {len(t)} trades ({sum(r['side']=='BUY' for r in t)} buys, {len(closed)} sells)"
          + (f", best {best['asset']} £{best['pnl']:+.2f}, worst {worst['asset']} £{worst['pnl']:+.2f}" if closed else "")
          + f" | {extra} | open: {', '.join(opn) or 'none'}"
          + " | " + " / ".join(f"{a} £{acash.get((sl, a), 0) + pv.get((sl, a), 0):.2f} (cash £{acash.get((sl, a), 0):.2f})" for a in ACCOUNTS))
fx = usd_gbp(con)
print(f"GBP/USD {1/fx[0]:.4f} ({fx[2]}, {fx[1][:16]} UTC)" if fx else "GBP/USD n/a")
if KILL.exists(): print("KILL SWITCH ON")
