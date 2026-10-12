"""One-off 'great reset' (2026-10-10): archive the old run, then start fresh at £200 with all current (v7) rules.
  1. ARCHIVE: online backup of data/trading.db -> data/archive/trading-pre-reset-<tag>.db (VACUUMed), the dashboard data
     docs/data.json -> data/archive/dashboard-pre-reset-<tag>.json and a short summary data/archive/trading-pre-reset-<tag>.md
     (final equity, trades, P&L per pot / account / pot x account, open positions at the reset).
  2. RESET: clears trades, positions, cash, acct_cash, equity, transfers, signals and the account/risk/day state (order
     queue, CORE plan/picks, DAY day-start/halt/exit cooldowns, burst/momentum status, RISK peaks/halt/base/exits,
     pending v6 settlement). KEEPS prices, fx, sentiment, daily_closes and collector/universe state (stock_rank, stock_meta,
     stock_trends, stock_vol, market_proxy, risk_screen/risk_watch/risk_only*, cgid/bf/dfetch, monad_*, gas, migrated_v5).
     Funds every pot x account from config accounts.funding (Crypto £100 = CORE 50 / DAY 30 / RISK 20, Stocks £100 = same),
     writes a fresh 'account_split' (opening = funding, NO pending settlement) so migrate_v6_accounts.py stays a no-op,
     runs the CORE stock picking now (trader.init_core: plan 'building' -> staggered tranches; Monday 09:00 UK rebalance
     re-picks as usual) and inserts an opening equity row (£200, current BTC price = restart of the BTC benchmark).
  State 'great_reset' / 'account_reset' mark the reset (scripts/ci_push.sh refuses to re-apply a cycle computed on a DB
  with a different 'account_reset' value, so a concurrent cycle can never resurrect the old run).
Usage (box clone, under /tmp/trading_repo.lock, after repo_sync): python3 great_reset.py [tag]   - refuses to run twice."""
import json, sqlite3, subprocess, sys, os
from common import ACCOUNTS, DATA, DB, ROOT, SITE, SLEEVES, cfg, db, funding, iso, sync_pot_cash, usd_gbp

TAG = sys.argv[1] if len(sys.argv) > 1 else "2026-10-10"
ARCH = DATA / "archive"; ARCH.mkdir(exist_ok=True)
ADB, AJS, AMD = ARCH / f"trading-pre-reset-{TAG}.db", ARCH / f"dashboard-pre-reset-{TAG}.json", ARCH / f"trading-pre-reset-{TAG}.md"

con = db()
r = con.execute("SELECT value FROM state WHERE key='great_reset'").fetchone()
if r and json.loads(r[0]).get("tag") == TAG: sys.exit(f"great reset {TAG} already done ({json.loads(r[0]).get('ts')}) - refusing to run again")
if ADB.exists(): sys.exit(f"{ADB} already exists - refusing to overwrite")
con.close()

# ---------------- 1. archive ----------------
tmp = ARCH / f".tmp-{TAG}.db"
if tmp.exists(): tmp.unlink()
s = sqlite3.connect(DB); d = sqlite3.connect(tmp); s.backup(d); d.close(); s.close()
d = sqlite3.connect(tmp); d.execute(f"VACUUM INTO '{ADB}'"); d.close(); tmp.unlink()
if (SITE / "data.json").exists(): AJS.write_bytes((SITE / "data.json").read_bytes())

import dashboard
os.environ["TRADING_DB"] = str(ADB)
summ = subprocess.run([sys.executable, "daily_summary.py"], cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, TRADING_LOG="/dev/null")).stdout
del os.environ["TRADING_DB"]
a = sqlite3.connect(ADB); a.row_factory = sqlite3.Row
import common
first, last = a.execute("SELECT MIN(ts), MAX(ts) FROM equity").fetchone()
tot = dashboard.stats(a)
n_tr = a.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
n_bs = dict(a.execute("SELECT side, COUNT(*) FROM trades GROUP BY side").fetchall())
fees = a.execute("SELECT COALESCE(SUM(cost),0)+COALESCE(SUM(gas),0) FROM trades").fetchone()[0]
realised = a.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='SELL'").fetchone()[0]
L = [f"# Archive of the paper-trading run before the great reset ({TAG})", "",
     f"- Database: `data/archive/{ADB.name}` ({ADB.stat().st_size/1e6:.1f} MB, full trade/equity/signal history; open with sqlite3)",
     f"- Dashboard data at the reset: `data/archive/{AJS.name}`" if AJS.exists() else "- Dashboard data: n/a",
     f"- Period: {first} -> {last} (UTC equity snapshots)", "",
     f"**Final equity £{tot['equity']:.2f}** vs £{tot['start']:.0f} start = **£{tot['equity']-tot['start']:+.2f} ({tot['ret']*100:+.2f}%)**; "
     f"vs BTC buy&hold {('%+.2f%%' % (tot['btc_ret']*100)) if tot['btc_ret'] is not None else 'n/a'}; max drawdown {tot['max_dd']*100:.2f}%",
     f"Trades: {n_tr} order rows ({', '.join(f'{k} {v}' for k, v in sorted(n_bs.items()))}); closed {tot['closed']}, realised £{realised:+.2f}; fees+gas £{fees:.2f}", "",
     "| Pot / account | Start £ | Final £ | P&L £ | P&L % | Trades | Realised £ |", "|---|---|---|---|---|---|---|"]
rows = {}
for sl in SLEEVES:
    x = dashboard.stats(a, sl); nt = a.execute("SELECT COUNT(*) FROM trades WHERE sleeve=? AND side IN ('BUY','SELL')", (sl,)).fetchone()[0]
    rel = a.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE sleeve=? AND side='SELL'", (sl,)).fetchone()[0]
    L.append(f"| {sl.upper()} pot | {x['start']:.2f} | {x['equity']:.2f} | {x['equity']-x['start']:+.2f} | {x['ret']*100:+.2f}% | {nt} | {rel:+.2f} |")
    rows[sl] = dict(start=x["start"], equity=x["equity"], pnl=x["equity"] - x["start"])
for ac in ACCOUNTS:
    x = dashboard.stats(a, None, ac)
    L.append(f"| {common.ACCT_LABEL[ac]} | {x['start']:.2f} | {x['equity']:.2f} | {x['equity']-x['start']:+.2f} | {x['ret']*100:+.2f}% | {x['trades']} | {x['realized']:+.2f} |")
    rows[ac] = dict(start=x["start"], equity=x["equity"], pnl=x["equity"] - x["start"])
for sl in SLEEVES:
    for ac in ACCOUNTS:
        x = dashboard.stats(a, sl, ac)
        L.append(f"| {sl.upper()} x {ac} | {x['start']:.2f} | {x['equity']:.2f} | {x['equity']-x['start']:+.2f} | {x['ret']*100:+.2f}% | {x['trades']} | {x['realized']:+.2f} |")
L += ["", "Open positions at the reset (cost basis £; discarded by the reset, never sold):", ""]
for p in a.execute("SELECT sleeve, asset, klass, qty, cost FROM positions ORDER BY sleeve, asset"):
    L.append(f"- {p['sleeve'].upper()} {p['asset']} ({p['klass']}): qty {p['qty']:.6g}, cost £{p['cost']:.2f}")
asp = json.loads((a.execute("SELECT value FROM state WHERE key='account_split'").fetchone() or ["null"])[0]) or {}
if asp.get("pending"): L += ["", "Pending v6 settlement dropped by the reset: " + "; ".join(f"{l['account']} {l['from'].upper()}->{l['to'].upper()} £{l['amount']:.2f}" for l in asp["pending"])]
L += ["", "Scorecard at the reset (daily_summary.py on the archived DB):", "", "```", summ.strip(), "```", ""]
AMD.write_text("\n".join(L)); a.close()
print("archive ->", ADB, f"({ADB.stat().st_size/1e6:.2f} MB)", "|", AJS.name, "|", AMD.name)

# ---------------- 2. reset ----------------
from trader import init_core
c = cfg(); con = db(); ts = iso()
for t in ("trades", "positions", "cash", "acct_cash", "equity", "signals", "transfers"): con.execute(f"DELETE FROM {t}")
con.execute("""DELETE FROM state WHERE key IN ('risk','day_meta','day_subs','order_queue','account_split','account_reset',
               'risk_peaks','riskpot_base','riskpot_halt','riskpot_status') OR key LIKE 'core_%' OR key LIKE 'day_start:%'
               OR key LIKE 'day_halt:%' OR key LIKE 'day_exit:%' OR key LIKE 'risk_exit:%'""")
F = funding(c)
con.executemany("INSERT INTO acct_cash VALUES(?,?,?)", [(k, ac, v) for k, dd in F.items() for ac, v in dd.items()]); sync_pot_cash(con)
con.execute("INSERT OR REPLACE INTO state VALUES('account_split',?)", (json.dumps(dict(
    ts=ts, opening=F, targets=F, pending=[], great_reset=TAG,
    note=f"great reset {TAG}: fresh start, every pot x account opens at its funding target; no settlement pending")),))
picks = init_core(con, c)
fx = usd_gbp(con); rate = fx[0] if fx else None
btc = con.execute("SELECT price FROM prices WHERE asset='BTC' AND source NOT IN ('backfill','kyberswap') ORDER BY ts DESC LIMIT 1").fetchone()
btc_gbp = btc[0] * rate if btc and rate else None
ae = {s_: {ac: [F[s_][ac], F[s_][ac]] for ac in ACCOUNTS} for s_ in SLEEVES}
pot = {s_: sum(F[s_].values()) for s_ in SLEEVES}; acc = {ac: sum(F[s_][ac] for s_ in SLEEVES) for ac in ACCOUNTS}; total = sum(pot.values())
con.execute("INSERT INTO equity(ts,equity,cash,pos_value,btc,open_n,usd_gbp,core,day,core_cash,day_cash,risk,risk_cash,crypto,stocks,crypto_cash,stocks_cash,accts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, total, total, 0.0, btc_gbp, 0, rate, pot["core"], pot["day"], pot["core"], pot["day"], pot["risk"], pot["risk"],
             acc["crypto"], acc["stocks"], acc["crypto"], acc["stocks"], json.dumps(ae, separators=(",", ":"))))
con.execute("INSERT OR REPLACE INTO state VALUES('account_reset',?)",
            (f"{ts} GREAT RESET GBP {total:.0f} (Crypto £{acc['crypto']:.0f} / Stocks £{acc['stocks']:.0f}; v7 rules; archive data/archive/{ADB.name})",))
con.execute("INSERT OR REPLACE INTO state VALUES('great_reset',?)", (json.dumps(dict(
    tag=TAG, ts=ts, archive=f"data/archive/{ADB.name}", summary=f"data/archive/{AMD.name}", dashboard=f"data/archive/{AJS.name}",
    old=dict(equity=round(tot["equity"], 4), pnl=round(tot["equity"] - tot["start"], 4), trades=n_tr,
             by=({k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in rows.items()})),
    funding=F, core_picks=picks, btc_gbp=btc_gbp)),))
con.commit()
print(f"reset at {ts}: cash", {k: {ac: v for ac, v in dd.items()} for k, dd in F.items()}, "| total £", total,
      "| BTC benchmark £", round(btc_gbp, 2) if btc_gbp else None, "| CORE stock picks:", picks)
print("kept rows: prices", con.execute("SELECT COUNT(*) FROM prices").fetchone()[0], "fx", con.execute("SELECT COUNT(*) FROM fx").fetchone()[0],
      "sentiment", con.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0], "daily_closes", con.execute("SELECT COUNT(*) FROM daily_closes").fetchone()[0])
con.close()
