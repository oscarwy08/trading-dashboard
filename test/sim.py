"""Backtest-style dry run of the CORE/DAY sleeves on a COPY of the live DB with a simulated clock and synthetic prices.
v3: also checks the dynamic CORE stock picks (Monday re-pick from the themed universe, dropped pick sold, replacement
built, pick count fitted to min trade size), min trade sizes, and simulated Monad gas (live estimate, then fallback).
v4: RISK pot - synthetic small-cap coins run through the real breakout screen (riskpot.screen): entry on breakout,
12% stop, trailing take-profit, 7-day max hold, max 5 open / 3 new per day / <= £8 each, drawdown-cap halt + RISK_RESUME,
DAY ignores risk-only coins, cash reconciliation for all three pots, TRANSFER between pots (rebased stats).
Usage: python3 test/sim.py [--keep]   (writes only to data/sim.db, deleted at the end unless --keep; logs/sim_cycles.log)."""
import io, json, os, random, sqlite3, sys, contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
SIM = HERE / "data" / "sim.db"
os.environ["TRADING_DB"] = str(SIM); os.environ["TRADING_LOG"] = str(HERE / "logs" / "sim.log")
sys.path.insert(0, str(HERE))
if SIM.exists(): SIM.unlink()
src = sqlite3.connect(HERE / "data" / "trading.db"); dst = sqlite3.connect(SIM); src.backup(dst); dst.close(); src.close()
import common, trader, stockrank, riskpot, rebalance_pots, dashboard
from common import LON
trend = (sqlite3.connect(SIM).execute("SELECT value FROM state WHERE key='stock_trends'").fetchone() or [None])[0]
c = common.cfg()

T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)
T0 = T0 + timedelta(minutes=15 - T0.minute % 15)
common._NOW = T0
con = sqlite3.connect(SIM)
for t in ("trades", "positions", "cash", "equity", "signals", "transfers"): con.execute(f"DROP TABLE IF EXISTS {t}")
con.execute("DELETE FROM state WHERE key='risk' OR key LIKE 'day_start:%' OR key LIKE 'day_halt:%' OR key LIKE 'core_%' "
            "OR key LIKE 'risk%' OR key LIKE 'riskpot%'")
if trend: con.execute("INSERT OR REPLACE INTO state VALUES('stock_trends',?)", (trend,))
con.commit(); con.close()
con = common.db()
con.executemany("INSERT INTO cash VALUES(?,?)", list(common.start_cash(c).items()))
print("init picks:", trader.init_core(con, c)); con.commit()

last = {r[0]: (r[1], r[2]) for r in con.execute(
    "SELECT asset, price, klass FROM prices WHERE klass IN ('crypto','stock') AND source!='backfill' GROUP BY asset HAVING MAX(ts)")}
RANK0 = stockrank.rank(con, c)
for a, r in RANK0.items():          # eligible universe stocks get synthetic intraday prices too (starting at the last daily close)
    if r["eligible"] and a not in last: last[a] = (r["close"], "stock")
PICKS0 = json.loads(con.execute("SELECT value FROM state WHERE key='core_stocks'").fetchone()[0])["picks"]
DROP = PICKS0[0] if PICKS0 else None   # scripted: this pick's daily trend collapses before the first Monday -> must be replaced
GAS_LIVE = 0.0004                       # simulated live gas estimate (GBP) for the first 2 days, then fallback from config
rate = con.execute("SELECT rate FROM fx ORDER BY ts DESC LIMIT 1").fetchone()[0]
# ---- RISK pot: synthetic small caps (go through the real riskpot.screen every step) ----
RISKC = {"MOONX": 1.0, "DUMPX": 1.0, "SLOWX": 1.0, "BRKA": 2.0, "BRKB": 2.0, "BRKC": 2.0, "BRKD": 2.0, "FADEX": 1.0}
NOISE = {"SLOWX": 0.05, "BRKA": 0.2, "BRKB": 0.2, "BRKC": 0.2, "BRKD": 0.2, "MOONX": 0.3, "DUMPX": 0.3}
for a, p0 in RISKC.items(): last[a] = (p0, "crypto")
series = {a: [] for a in RISKC}
rng = random.Random(42); drift = {a: 0.0 for a in last}
lt = lambda t: t.astimezone(LON)
def uk(day_off, h, m=0):   # UK local time relative to T0's UK date
    d = lt(T0).date() + timedelta(days=day_off)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(timezone.utc)
# scripted events (multiplicative move spread over the window)
EVENTS = [("WLD", uk(2, 19), uk(3, 8), 1.50),     # WLD rally Sun eve -> Mon morning => overweight at Monday rebalance
          ("XRP", uk(4, 10), uk(4, 12), 0.72),    # XRP -28% Tue => CORE 20% stop-loss
          ("SOL", uk(1, 10), uk(1, 11), 1.07),    # SOL pump Sat => DAY entry / take-profit
          ("BTC", uk(1, 15), uk(1, 15, 30), 0.95),
          ("MOONX", uk(1, 9), uk(1, 12), 1.25), ("MOONX", uk(1, 12), uk(2, 0), 1.20), ("MOONX", uk(2, 6), uk(2, 9), 0.85),   # -> trailing TP
          ("DUMPX", uk(1, 10), uk(1, 13), 1.12), ("DUMPX", uk(1, 19), uk(1, 21), 0.80),                                   # -> 12% stop (peak < +15%: no trail)
          ("SLOWX", uk(3, 9), uk(3, 12), 1.18),                                                                            # -> 7-day max hold
          *[(a, uk(4, 9), uk(4, 12), 1.22) for a in ("BRKA", "BRKB", "BRKC", "BRKD")],                                     # -> 3/day, max 5 open
          ("FADEX", uk(1, 9), uk(1, 9, 15), 1.80), ("FADEX", uk(1, 9, 15), uk(1, 15), 0.97)]                              # +80% one-candle pump: never chased
RHALT, RRESUME = uk(6, 12), uk(7, 12)                # drawdown cap forced, then reviewed
SENT = [("MON", uk(1, 23, 30), -0.7, 0.8)]       # strongly negative sentiment Sat 23:30 UK -> MON Sunday tranche delayed ~12h
END = uk(10, 20)                                     # through the following Monday 10:00 UK (two rebalances)
CAP_DAY = uk(5, 0).astimezone(LON).date().isoformat()   # Wed: force the DAY loss cap mid-morning

checks = {"flat_violation": [], "entry_window_violation": [], "max_open_violation": [], "neg_cash": [], "kill_trades": None}
log = open(HERE / "logs" / "sim_cycles.log", "w")
t = T0; steps = 0
while t <= END:
    common._NOW = t
    for a, (p, k) in list(last.items()):
        L = lt(t); stock_open = L.weekday() < 5 and (14 * 60 + 30) <= L.hour * 60 + L.minute < 21 * 60
        if k == "stock" and not stock_open: r = 0
        else:
            if steps % 24 == 0: drift[a] = rng.gauss(0, 0.0015)
            r = (drift[a] + rng.gauss(0, 0.004 if k == "crypto" else 0.002)) * NOISE.get(a, 1)
        for ea, e0, e1, mult in EVENTS:
            if ea == a and e0 <= t < e1:
                n = max(1, int((e1 - e0).total_seconds() // 900)); r += mult ** (1 / n) - 1
        p *= 1 + r; last[a] = (p, k)
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?)", (common.iso(t), a, k, p, None, None, None, None, "sim"))
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (common.iso(t), "USDGBP", rate, "sim", None, None))
    rows = []
    for a in RISKC:
        series[a].append(last[a][0]); sr = series[a]; px_ = sr[-1]
        ch = lambda n: (px_ / sr[max(0, len(sr) - 1 - n)] - 1) * 100
        rows.append(dict(id=a.lower(), symbol=a.lower(), name=a.title(), market_cap_rank=150, market_cap=1e8, total_volume=2e7,
                         current_price=px_, high_24h=max(sr[-97:]), price_change_percentage_1h_in_currency=ch(4),
                         price_change_percentage_24h_in_currency=ch(96), price_change_percentage_7d_in_currency=ch(672)))
    cand = riskpot.screen(rows, c)
    con.execute("INSERT OR REPLACE INTO state VALUES('risk_screen',?)", (json.dumps(dict(ts=common.iso(t), universe=len(rows), liquid=len(cand),
                ok=sum(d["ok"] for d in cand), monad=0, top=cand)),))
    con.execute("INSERT OR REPLACE INTO state VALUES('risk_only',?)", (json.dumps(list(RISKC)),))
    if t == RHALT:                       # force the RISK drawdown cap: pretend the pot started 40% higher
        req = con.execute("SELECT risk FROM equity ORDER BY ts DESC LIMIT 1").fetchone()[0]
        con.execute("INSERT OR REPLACE INTO state VALUES('riskpot_base',?)", (json.dumps(req / 0.6),))
        print(f"[test] {lt(t):%a %H:%M} UK: RISK base set to £{req/0.6:.2f} (pot £{req:.2f} => -40%) to trip the 30% drawdown cap")
    if t == RRESUME:
        rr = HERE / "test" / "RISK_RESUME"; rr.touch(); trader.RISK_RESUME = rr
        print(f"[test] {lt(t):%a %H:%M} UK: RISK_RESUME created (review done)")
    if t < uk(2, 0):
        con.execute("INSERT OR REPLACE INTO state VALUES('gas',?)", (json.dumps(dict(ts=common.iso(t), gbp=GAS_LIVE, gwei=100, units=200000, mon=0.02, src="sim")),))
    elif steps and con.execute("SELECT 1 FROM state WHERE key='gas'").fetchone():
        con.execute("DELETE FROM state WHERE key='gas'")         # RPC 'down' -> trader must use gas.fallback_gbp
    if DROP and t == uk(3, 6):          # Monday 06:00 UK: DROP's daily closes fall 35% over 15 'days' -> below 50d avg
        base = RANK0[DROP]["close"]; d0 = lt(t).date()
        for i in range(15):
            con.execute("INSERT OR REPLACE INTO daily_closes VALUES(?,?,?)", (DROP, (d0 + timedelta(days=i + 1)).isoformat(), base * (1 - 0.35 * (i + 1) / 15)))
        print(f"[test] {lt(t):%a %H:%M} UK: {DROP} daily closes -35% (should be dropped at the Monday rebalance)")
    for a, ts_, sc, cf in SENT:
        if ts_ <= t < ts_ + timedelta(minutes=15):
            con.execute("INSERT OR REPLACE INTO sentiment VALUES(?,?,?,?,?,?,?,?,?)", (common.iso(t), a, sc, 10, cf, "sim bearish", "[]", "sim", common.iso(t)))
    L = lt(t)
    if L.date().isoformat() == CAP_DAY and L.hour == 11 and L.minute == 0:   # forced loss-cap scenario
        deq = float(con.execute(f"SELECT value FROM state WHERE key='day_start:day:{CAP_DAY}'").fetchone()[0])
        cur = con.execute("SELECT day FROM equity ORDER BY ts DESC LIMIT 1").fetchone()[0]
        con.execute("UPDATE state SET value=? WHERE key=?", (str(cur / 0.94), f"day_start:day:{CAP_DAY}"))
        print(f"[test] {L:%a %H:%M} UK: set DAY day-start equity to £{cur/0.94:.2f} (current £{cur:.2f} => -6%) to trip the 5% cap")
    con.commit()
    if t == uk(3, 12):   # KILL test on Mon 12:00 UK
        kill = HERE / "test" / "KILL"; kill.touch(); trader.KILL = kill
        n0 = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): trader.main()
        checks["kill_trades"] = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] - n0
        print(f"[test] KILL cycle {L:%a %H:%M} UK -> {buf.getvalue().strip()} | trades during KILL: {checks['kill_trades']}")
        kill.unlink(); trader.KILL = common.KILL
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): trader.main()
    o = buf.getvalue(); log.write(o)
    if "no trades this cycle" not in o or "REBALANCE" in o: print(o.rstrip())
    mins = L.hour * 60 + L.minute
    nday = con.execute("SELECT COUNT(*) FROM positions WHERE sleeve='day'").fetchone()[0]
    if (mins >= 22 * 60 or mins < 8 * 60) and nday: checks["flat_violation"].append(L.isoformat())
    if nday > 3: checks["max_open_violation"].append(L.isoformat())
    nrisk = con.execute("SELECT COUNT(*) FROM positions WHERE sleeve='risk'").fetchone()[0]
    if nrisk > c["riskpot"]["max_open"]: checks["max_open_violation"].append("risk " + L.isoformat())
    if t == RHALT: checks["risk_halt_state"] = bool(con.execute("SELECT 1 FROM state WHERE key='riskpot_halt'").fetchone())
    if t == RRESUME:
        checks["risk_resumed"] = not con.execute("SELECT 1 FROM state WHERE key='riskpot_halt'").fetchone() and not (HERE / "test" / "RISK_RESUME").exists()
        trader.RISK_RESUME = common.RISK_RESUME
    if any(r[0] < -1e-9 for r in con.execute("SELECT amount FROM cash")): checks["neg_cash"].append(L.isoformat())
    t += timedelta(minutes=15); steps += 1

# ---------------- post-run checks ----------------
tr = [dict(r) for r in common.db().execute("SELECT * FROM trades ORDER BY id")]
for x in tr: x["L"] = lt(datetime.fromisoformat(x["ts"]))
for x in tr:
    if x["sleeve"] == "day" and x["side"] == "BUY" and not (8 * 60 <= x["L"].hour * 60 + x["L"].minute < 21 * 60):
        checks["entry_window_violation"].append(x["L"].isoformat())
start = common.start_cash(c)
cash = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
recon = {s: start[s] - sum(x["value"] + (x["gas"] or 0) for x in tr if x["sleeve"] == s and x["side"] == "BUY")
            + sum(x["value"] - (x["gas"] or 0) for x in tr if x["sleeve"] == s and x["side"] == "SELL") for s in start}
mint = float(c["risk"]["min_trade_size"])
small_buys = [x for x in tr if x["side"] == "BUY" and x["value"] < mint - 1e-6]
gas_vals = sorted({round(x["gas"], 6) for x in tr if x["gas"] is not None}); no_gas = [x for x in tr if x["gas"] is None]
rbl = json.loads(con.execute("SELECT value FROM state WHERE key='core_rebalance_log'").fetchone()[0])
cs = json.loads(con.execute("SELECT value FROM state WHERE key='core_stocks'").fetchone()[0])
drop_sells = [x for x in tr if x["asset"] == DROP and x["side"] == "SELL" and "no longer a CORE pick" in x["reason"]]
new_picks = [a for a in cs["picks"] if a not in PICKS0]
new_buys = {a: [round(x["value"], 2) for x in tr if x["asset"] == a and x["sleeve"] == "core" and x["side"] == "BUY"] for a in new_picks}
eqc = con.execute("SELECT core FROM equity ORDER BY ts DESC LIMIT 1").fetchone()[0]
tranche = {}
for x in tr:
    if x["sleeve"] == "core" and x["side"] == "BUY" and x["reason"].startswith("CORE tranche"):
        tranche.setdefault(x["asset"], []).append(f"{x['L']:%a %H:%M} £{x['value']:.2f}")
per_day = {}
for x in tr:
    if x["sleeve"] == "core" and x["reason"].startswith("CORE tranche"):
        k = (x["asset"], x["L"].date()); per_day[k] = per_day.get(k, 0) + 1
flat = [x for x in tr if x["sleeve"] == "day" and "flatten" in x["reason"]]
dsell = {k: sum(1 for x in tr if x["sleeve"] == "day" and x["side"] == "SELL" and x["reason"].startswith(k)) for k in ("stop-loss", "take-profit", "signal", "flatten")}
cap_buys = [x for x in tr if x["sleeve"] == "day" and x["side"] == "BUY" and x["L"].date().isoformat() == CAP_DAY and x["L"].hour >= 11]
halt_sig = con.execute(f"SELECT COUNT(*) FROM signals WHERE sleeve='day' AND reason LIKE '%daily loss cap hit%' AND ts>=?", (common.iso(uk(5, 11)),)).fetchone()[0]
next_day_buys = [x for x in tr if x["sleeve"] == "day" and x["side"] == "BUY" and x["L"].date() == datetime.fromisoformat(CAP_DAY).date() + timedelta(days=1)]
rb = [x for x in tr if "rebalance" in x["reason"]]
stops = [x for x in tr if "core stop-loss" in x["reason"]]
big = [x for x in tr if x["sleeve"] == "day" and x["side"] == "BUY" and x["value"] > 0.25 * start["day"] * 1.2]
RP = c["riskpot"]
rb_ = [x for x in tr if x["sleeve"] == "risk" and x["side"] == "BUY"]; rs_ = [x for x in tr if x["sleeve"] == "risk" and x["side"] == "SELL"]
rpd = {}
for x in rb_: rpd[x["L"].date()] = rpd.get(x["L"].date(), 0) + 1
risk_exit = {k: [(f"{x['L']:%a %d %H:%M}", x["asset"], round(x["pnl"], 2)) for x in rs_ if x["reason"].startswith(k)]
             for k in ("risk stop-loss", "trailing take-profit", "max hold", "sentiment exit")}
halt_buys = [x for x in rb_ if RHALT <= datetime.fromisoformat(x["ts"]) < RRESUME]
day_in_risk = [x for x in tr if x["sleeve"] == "day" and x["asset"] in RISKC]
stop_pnl = [x for x in rs_ if x["reason"].startswith("risk stop-loss")]
print("\n================ SIM SUMMARY ================")
print(f"steps {steps}, {lt(T0):%a %d %b %H:%M} -> {lt(END):%a %d %b %H:%M} UK; trades {len(tr)}")
print("CORE tranches per asset:", json.dumps(tranche, indent=1))
print("max CORE tranches per asset per UK day:", max(per_day.values()))
print("rebalance trades:", [(f"{x['L']:%a %d %H:%M}", x["asset"], x["side"], round(x["value"], 2), x["reason"][:70]) for x in rb])
print("core stop-loss trades:", [(f"{x['L']:%a %d %H:%M}", x["asset"], round(x["pnl"], 2)) for x in stops])
print("DAY sells by reason:", dsell, "| flatten trades:", sorted({f"{x['L']:%a %d %H:%M}" for x in flat}))
print(f"loss-cap day {CAP_DAY}: halt signals {halt_sig}, DAY buys after 11:00 that day {len(cap_buys)}, DAY buys next day {len(next_day_buys)}")
print("day buys > 25% sleeve (+20% slack):", len(big))
print("RISK buys:", [(f"{x['L']:%a %d %H:%M}", x["asset"], round(x["value"], 2)) for x in rb_])
print("RISK exits:", risk_exit)
print(f"RISK buys per UK day {dict((str(k), v) for k, v in rpd.items())} | buys during drawdown halt {len(halt_buys)} | DAY trades in risk-only coins {len(day_in_risk)}")
print("cash vs reconstruction (incl. gas):", {s: (round(cash[s], 4), round(recon[s], 4)) for s in cash})
print(f"initial picks {PICKS0} ({len(PICKS0)} = n_picks(£{common.start_cash(c)['core']:.0f}) {stockrank.n_picks(c, common.start_cash(c)['core'])}); "
      f"targets per pick £{common.start_cash(c)['core'] * c['core']['stocks_pct'] / 100 / max(len(PICKS0), 1):.2f}")
print(f"Monday re-pick: {cs['picks']} | dropped {DROP} sold: {[(f'{x['L']:%a %d %H:%M}', round(x['pnl'], 2)) for x in drop_sells]} | "
      f"replacements {new_picks} bought: {new_buys} | rebalance log: {rbl.get('actions')}")
print(f"themes of picks: {[RANK0.get(a, {}).get('theme') for a in cs['picks']]} (max {c['core']['max_per_theme']} per theme)")
print(f"buys below £{mint:.0f} min trade: {len(small_buys)} | gas values seen: {gas_vals} | trades without gas: {len(no_gas)} | gas total £{sum(x['gas'] or 0 for x in tr):.4f}")
assert not small_buys, "buy below min trade"
assert rb_ and all(x["value"] <= RP["max_position_gbp"] + 1e-9 for x in rb_), "risk position size"
assert max(rpd.values()) <= RP["max_new_per_day"], "risk max new per day"
assert risk_exit["risk stop-loss"] and risk_exit["trailing take-profit"] and risk_exit["max hold"], "risk exits (stop / trailing / max hold)"
assert all(x["pnl"] / (x["value"] + 1e-9) < -0.10 for x in stop_pnl), "risk stop around -12%"
assert not [x for x in rb_ if x["asset"] == "FADEX" and datetime.fromisoformat(x["ts"]) < uk(2, 9, 15)], "a +80% pump (24h > +60%) must not be chased"
assert not halt_buys and checks["risk_halt_state"] and checks["risk_resumed"], "risk drawdown cap halt / resume"
assert not day_in_risk, "DAY traded a risk-only coin"
assert not checks["max_open_violation"], "max open"
assert all(abs(cash[s] - recon[s]) < 1e-6 for s in cash), "cash reconciliation"
assert GAS_LIVE in gas_vals and float(c["gas"]["fallback_gbp"]) in gas_vals, "gas live + fallback"
if DROP: assert DROP not in cs["picks"] and drop_sells and new_picks and all(new_buys.values()), "Monday re-pick"
assert len(cs["picks"]) == stockrank.n_picks(c, eqc) or len(cs["picks"]) == len([1 for r in RANK0.values() if r["eligible"]]), "pick count"
print("checks:", {k: (v if isinstance(v, int) or v is None else len(v)) for k, v in checks.items()})
print("final:", [dict(r) for r in con.execute("SELECT ts, equity, core, day FROM equity ORDER BY ts DESC LIMIT 1")])
print("MON tranche times (sentiment delay Sat 23:30 until ~11:30 UK Sun):", tranche.get("MON"))
print("delay signals:", tuple(con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM signals WHERE reason LIKE '%delayed: sentiment%'").fetchone()))
print("core plan at end:", {a: (v["status"], v["n"]) for a, v in json.loads(con.execute("SELECT value FROM state WHERE key='core_plan'").fetchone()[0]).items()})
print("re-entry tranches after 2nd rebalance:", [(f"{x['L']:%a %d %H:%M}", x["asset"], round(x["value"], 2), x["reason"][:60]) for x in tr if x["sleeve"] == "core" and x["side"] == "BUY" and x["L"] >= lt(uk(10, 9))])
print("MON signals Sun:", [tuple(r) for r in con.execute("SELECT ts, action, substr(reason,1,90) FROM signals WHERE sleeve='core' AND asset='MON' AND ts BETWEEN ? AND ? AND (reason LIKE '%delayed%' OR action='BUY') ORDER BY ts", (common.iso(uk(1, 23)), common.iso(uk(2, 23)))).fetchall()][:3], "...")

# ---- TRANSFER: move £1 Day -> Risk; cash moves, history is rebased (no fake P&L / drawdown) ----
s0 = {s: dashboard.stats(con, s) for s in ("day", "risk")}; c0 = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
common._NOW = END + timedelta(minutes=1); rebalance_pots.iso = common.iso
rebalance_pots.transfer(con, "day", "risk", 1.0, "sim test"); con.commit()
c1 = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
adj = common.transfer_adj(con, "risk"); last_ts = con.execute("SELECT MAX(ts) FROM equity").fetchone()[0]
print("transfer:", {k: round(c1[k] - c0[k], 4) for k in c1}, "| risk rebase of last point £", common.rebase(last_ts, adj),
      "| risk max_dd before/after", round(s0["risk"]["max_dd"], 4), round(dashboard.stats(con, "risk")["max_dd"], 4))
assert abs(c1["day"] - c0["day"] + 1) < 1e-9 and abs(c1["risk"] - c0["risk"] - 1) < 1e-9 and common.rebase(last_ts, adj) == 1.0, "transfer"
print("ALL CHECKS PASSED")

if "--keep" not in sys.argv:
    con.close(); SIM.unlink(); print("deleted test copy", SIM)
