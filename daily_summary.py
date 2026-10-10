"""Short text scorecard for the AI routine to relay. All amounts in GBP (£). Total + one line per sleeve."""
import json
from datetime import datetime, timezone
from common import KILL, LON, SLEEVES, db, rebase, transfer_adj, usd_gbp
from dashboard import stats

con = db(); s = stats(con)
day = datetime.now(LON).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
eq0 = con.execute("SELECT ts, equity, core, day, risk FROM equity WHERE ts<? ORDER BY ts DESC LIMIT 1", (day,)).fetchone() \
      or con.execute("SELECT ts, equity, core, day, risk FROM equity ORDER BY ts LIMIT 1").fetchone()
today = con.execute("SELECT sleeve, side, asset, pnl FROM trades WHERE ts>=?", (day,)).fetchall()
pct = lambda v: "n/a" if v is None else f"{v*100:+.2f}%"
dp = s["equity"] - eq0["equity"] if eq0 else 0
print(f"PAPER scorecard {datetime.now(LON):%a %d %b %H:%M} UK")
print(f"TOTAL Equity £{s['equity']:.2f} ({pct(s['ret'])} since £{s['start']:.0f} start) | Day P&L £{dp:+.2f}")
print(f"vs BTC buy&hold (£) {pct(s['btc_ret'])} | Max DD {pct(s['max_dd'])} | Win rate {pct(s['win_rate']).lstrip('+')} over {s['closed']} closed")
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
          + f" | {extra} | open: {', '.join(opn) or 'none'}")
fx = usd_gbp(con)
print(f"GBP/USD {1/fx[0]:.4f} ({fx[2]}, {fx[1][:16]} UTC)" if fx else "GBP/USD n/a")
if KILL.exists(): print("KILL SWITCH ON")
