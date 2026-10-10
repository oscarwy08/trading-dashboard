"""Backtest-style dry run of the CORE/DAY sleeves on a COPY of the live DB with a simulated clock and synthetic prices.
v3: also checks the dynamic CORE stock picks (Monday re-pick from the themed universe, dropped pick sold, replacement
built, pick count fitted to min trade size), min trade sizes, and simulated Monad gas (live estimate, then fallback).
v4: RISK pot - synthetic small-cap coins run through the real breakout screen (riskpot.screen): entry on breakout,
12% stop, trailing take-profit, 7-day max hold, max 5 open / 3 new per day / <= £8 each, drawdown-cap halt + RISK_RESUME,
DAY ignores risk-only coins, cash reconciliation for all three pots, TRANSFER between pots (rebased stats).
v5: stocks are normal exchange tickers with real market hours (markets.py): synthetic stock prices only move while the
listing exchange is open; checks that no stock trade happens outside its session (US / LSE / ASX, weekends), that rebalance
orders decided while a market is closed are QUEUED and filled at the open, that DAY stock positions are flat at the close,
plus unit checks of the calendar (DST gap, holidays), the pence -> pounds conversion and the broker-like stock costs.
v6: two accounts (crypto = Monad wallet, stocks = Trading 212) with cash per pot x account: every trade must use the
cash of its own account (per-account cash reconciliation for all 6 pot x account cells, no account ever negative, no
cash crossing accounts), per-account equity columns, pot moves inside one account and a manual account transfer (rebased,
baselines move), plus the live-DB account split (migrate_v6_accounts: totals 100/100, pot totals unchanged, settlement
inside each account executed by the trader once the DAY pot is flattened -> every pot x account at its target).
v7: DAY pot = two sub-strategies (momentum with ATR-scaled target/stop + trailing stop, burst breakout trades with a
confidence score, EV after costs, capital-based threshold/sizing): a scripted one-day scenario checks burst take-profit,
trailing stop, ATR stop, 6h max hold, skips (no volume / RSI / negative sentiment / BTC falling), 1h cooldown, momentum
winner beyond the old 5% cap, stock burst flattened before the close, max open per account, sub tags + stats, the
threshold scaling across the 15% daily stop, sizing, EV and the min expected move (4x costs) filter.
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
import common, trader, stockrank, riskpot, rebalance_pots, dashboard, markets, collector, transfer_accounts, migrate_v6_accounts
from common import LON
trend = (sqlite3.connect(SIM).execute("SELECT value FROM state WHERE key='stock_trends'").fetchone() or [None])[0]
c = common.cfg()

T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)
T0 = T0 + timedelta(minutes=15 - T0.minute % 15)
common._NOW = T0
con = sqlite3.connect(SIM)
for t in ("trades", "positions", "cash", "acct_cash", "equity", "signals", "transfers"): con.execute(f"DROP TABLE IF EXISTS {t}")
con.execute("DELETE FROM state WHERE key='risk' OR key LIKE 'day_start:%' OR key LIKE 'day_halt:%' OR key LIKE 'core_%' "
            "OR key LIKE 'risk%' OR key LIKE 'riskpot%' OR key='account_split' OR key='order_queue'")
if trend: con.execute("INSERT OR REPLACE INTO state VALUES('stock_trends',?)", (trend,))
con.commit(); con.close()
con = common.db()
FUND = common.funding(c)
con.executemany("INSERT INTO acct_cash VALUES(?,?,?)", [(k, a, v) for k, d in FUND.items() for a, v in d.items()]); common.sync_pot_cash(con)
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

sys.path.insert(0, str(HERE / "test")); import check_data
RECON = dict(runs=0, checks=0, fails=[])
checks = {"flat_violation": [], "entry_window_violation": [], "max_open_violation": [], "neg_cash": [], "kill_trades": None}
log = open(HERE / "logs" / "sim_cycles.log", "w")
t = T0; steps = 0
while t <= END:
    common._NOW = t
    for a, (p, k) in list(last.items()):
        L = lt(t); stock_open = markets.is_open(a, t)
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
        capf = 1 - (c["day"]["daily_loss_cap_pct"] + 1) / 100      # v7: cap is configurable (15%) -> force cap + 1%
        con.execute("UPDATE state SET value=? WHERE key=?", (str(cur / capf), f"day_start:day:{CAP_DAY}"))
        print(f"[test] {L:%a %H:%M} UK: set DAY day-start equity to £{cur/capf:.2f} (current £{cur:.2f} => -{(1-capf)*100:.0f}%) to trip the {c['day']['daily_loss_cap_pct']}% cap")
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
    dm_ = json.loads((con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone() or ["{}"])[0]) or {}   # v7 sub-strategy limits
    kl_ = {r[0]: r[1] for r in con.execute("SELECT asset, klass FROM positions WHERE sleeve='day'")}
    if sum(1 for a_ in kl_ if (dm_.get(a_) or {}).get("sub", "momentum") == "momentum") > c["day"]["momentum"]["max_open"]: checks["max_open_violation"].append("momentum " + L.isoformat())
    for ac_ in common.ACCOUNTS:
        if sum(1 for a_, k_ in kl_.items() if (dm_.get(a_) or {}).get("sub") == "burst" and common.acct_of(k_) == ac_) > c["day"]["burst"]["max_open_per_account"]:
            checks["max_open_violation"].append(f"burst {ac_} " + L.isoformat())
    nrisk = con.execute("SELECT COUNT(*) FROM positions WHERE sleeve='risk'").fetchone()[0]
    if nrisk > c["riskpot"]["max_open"]: checks["max_open_violation"].append("risk " + L.isoformat())
    if t == RHALT: checks["risk_halt_state"] = bool(con.execute("SELECT 1 FROM state WHERE key='riskpot_halt'").fetchone())
    if t == RRESUME:
        checks["risk_resumed"] = not con.execute("SELECT 1 FROM state WHERE key='riskpot_halt'").fetchone() and not (HERE / "test" / "RISK_RESUME").exists()
        trader.RISK_RESUME = common.RISK_RESUME
    if any(r[0] < -1e-9 for r in con.execute("SELECT amount FROM acct_cash")): checks["neg_cash"].append(L.isoformat())
    if steps % 12 == 5 or t + timedelta(minutes=15) > END:   # v7.1: every 3h the dashboard export must reconcile (test/check_data.py)
        check_data.fails.clear(); D_ = json.loads(json.dumps(dashboard.build(common.db())))
        check_data.main(D_); RECON["runs"] += 1; RECON["checks"] = check_data.n[0]
        RECON["fails"] += [f"{L:%a %H:%M} {f_}" for f_ in check_data.fails]
        ss_ = json.loads((con.execute("SELECT value FROM state WHERE key='day_subs'").fetchone() or ["{}"])[0]).get("budgets") or {}
        for sb_, x_ in ss_.items():          # the trader sizes with the same ledger the Day tab shows
            for ac_, y_ in x_.items():
                lg_ = D_["sleeves"]["day"]["ledger"][ac_][sb_]
                if abs(y_["cash"] - lg_["cash"]) > 0.011: RECON["fails"].append(f"{L:%a %H:%M} trader {sb_}/{ac_} cash {y_['cash']:.4f} != Day tab {lg_['cash']}")
                if any(p_["value"] is None for p_ in D_["positions"]): continue
                if abs(y_["budget"] - lg_["budget"]) > 0.011 + 0.006 * lg_.get("open_n", 0): RECON["fails"].append(f"{L:%a %H:%M} trader {sb_}/{ac_} budget {y_['budget']:.4f} != Day tab {lg_['budget']}")
    t += timedelta(minutes=15); steps += 1

# ---------------- post-run checks ----------------
tr = [dict(r) for r in common.db().execute("SELECT * FROM trades ORDER BY id")]
for x in tr: x["L"] = lt(datetime.fromisoformat(x["ts"]))
# v5: market hours / queue / costs / conversion
closed_trades = [(f"{x['L']:%a %d %H:%M}", x["asset"], x["side"], x["reason"][:50]) for x in tr if x["klass"] == "stock" and x["side"] in ("BUY", "SELL")
                 and not markets.is_open(x["asset"], datetime.fromisoformat(x["ts"])) and "already closed" not in x["reason"]]
queued = [x for x in tr if "[queued" in x["reason"]]
stock_tr = [x for x in tr if x["klass"] == "stock" and x["side"] in ("BUY", "SELL")]
print("v5 stock trades:", len(stock_tr), "| outside market hours:", closed_trades[:5], "| queued orders filled at the open:",
      [(f"{x['L']:%a %d %H:%M}", x["asset"], x["side"], round(x["value"], 2)) for x in queued])
print("v5 DAY stock trades:", [(f"{x['L']:%a %d %H:%M}", x["asset"], x["side"], x["reason"][:40]) for x in tr if x["sleeve"] == "day" and x["klass"] == "stock"][:8])
assert not closed_trades, "stock trade outside market hours"
assert all((x["gas"] or 0) == 0 for x in stock_tr), "stocks pay no gas"
fxr = con.execute("SELECT rate FROM fx ORDER BY ts DESC LIMIT 1").fetchone()[0]
assert abs(collector.usd_factor(con, "GBp") - 0.01 / fxr) < 1e-12 and abs(collector.usd_factor(con, "GBP") - 1 / fxr) < 1e-12, "pence conversion"
assert abs(664.5 * collector.usd_factor(con, "GBp") * fxr - 6.645) < 1e-9, "664.5p -> £6.645"
cp = lambda a, sd: round(trader.cost_pct(c, a, "stock", sd) * 100, 4)
assert (cp("NVDA", "BUY"), cp("RPI.L", "BUY"), cp("RPI.L", "SELL"), cp("ITM.L", "BUY"), round(trader.cost_pct(c, "BTC", "crypto", "BUY") * 100, 4)) == (0.25, 0.6, 0.1, 0.1, 0.3), "stock costs"
from datetime import date as _d
o_, c_ = markets.session("US", _d(2026, 10, 27)); assert (lt(o_).hour, lt(o_).minute, lt(c_).hour) == (13, 30, 20), "US/UK DST gap"
o_, c_ = markets.session("US", _d(2026, 10, 12)); assert (lt(o_).hour, lt(o_).minute, lt(c_).hour) == (14, 30, 21), "US session in UK time"
assert markets.session("US", _d(2026, 11, 26)) is None and markets.session("LSE", _d(2026, 12, 28)) is None and markets.session("LSE", _d(2026, 10, 10)) is None, "holidays/weekend"
assert lt(markets.session("US", _d(2026, 11, 27))[1]).hour == 18, "early close day after Thanksgiving (13:00 NY = 18:00 UK)"
print("v5 unit checks ok: calendar (DST gap 13:30-20:00 UK, holidays, early close), GBp->£, costs NVDA 0.25% / RPI.L buy 0.6% (stamp) / ITM.L (AIM) 0.1%")
for x in tr:
    if x["sleeve"] == "day" and x["side"] == "BUY" and not (8 * 60 <= x["L"].hour * 60 + x["L"].minute < 21 * 60):
        checks["entry_window_violation"].append(x["L"].isoformat())
start = common.start_cash(c)
cash = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
recon = {s: start[s] - sum(x["value"] + (x["gas"] or 0) for x in tr if x["sleeve"] == s and x["side"] == "BUY")
            + sum(x["value"] - (x["gas"] or 0) for x in tr if x["sleeve"] == s and x["side"] == "SELL") for s in start}
# v6: per pot x account - a trade may only move the cash of its own account (crypto -> crypto cash, stock -> stocks cash)
acash = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}
arecon = {(s, a): FUND[s][a] - sum(x["value"] + (x["gas"] or 0) for x in tr if x["sleeve"] == s and common.acct_of(x["klass"]) == a and x["side"] == "BUY")
          + sum(x["value"] - (x["gas"] or 0) for x in tr if x["sleeve"] == s and common.acct_of(x["klass"]) == a and x["side"] == "SELL")
          for s in common.SLEEVES for a in common.ACCOUNTS}
# v7.3: inter-pot loans move cash inside one account (borrower +amount -repaid, lender -amount +repaid); never across accounts
for l_ in con.execute("SELECT account, lender, borrower, amount, repaid FROM loans"):
    net_ = l_[3] - (l_[4] or 0)
    recon[l_[2]] += net_; recon[l_[1]] -= net_; arecon[(l_[2], l_[0])] += net_; arecon[(l_[1], l_[0])] -= net_
n_cross = con.execute("SELECT COUNT(*) FROM transfers WHERE from_account IS NOT NULL AND from_account != to_account").fetchone()[0]
eqrow = con.execute("SELECT equity, cash, crypto, stocks, crypto_cash, stocks_cash, accts FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
acc_eq_ok = abs(eqrow["crypto"] + eqrow["stocks"] - eqrow["equity"]) < 1e-4 and abs(eqrow["crypto_cash"] + eqrow["stocks_cash"] - eqrow["cash"]) < 1e-6
stock_buys_acct = {a: sum(1 for x in tr if x["side"] == "BUY" and common.acct_of(x["klass"]) == a) for a in common.ACCOUNTS}
mint = float(c["risk"]["min_trade_size"])
small_buys = [x for x in tr if x["side"] == "BUY" and x["value"] < mint - 1e-6]
gas_vals = sorted({round(x["gas"], 6) for x in tr if x["gas"] is not None}); no_gas = [x for x in tr if x["gas"] is None]
rbl = json.loads(con.execute("SELECT value FROM state WHERE key='core_rebalance_log'").fetchone()[0])
cs = json.loads(con.execute("SELECT value FROM state WHERE key='core_stocks'").fetchone()[0])
drop_sells = [x for x in tr if x["asset"] == DROP and x["side"] == "SELL" and "no longer a CORE pick" in x["reason"]]
new_picks = [a for a in cs["picks"] if a not in PICKS0]
new_buys = {a: [round(x["value"], 2) for x in tr if x["asset"] == a and x["sleeve"] == "core" and x["side"] == "BUY"] for a in new_picks}
eqc = json.loads(con.execute("SELECT accts FROM equity ORDER BY ts DESC LIMIT 1").fetchone()[0])["core"]["stocks"][0]   # v6: Core stocks money
tranche = {}
for x in tr:
    if x["sleeve"] == "core" and x["side"] == "BUY" and x["reason"].startswith("CORE tranche"):
        tranche.setdefault(x["asset"], []).append(f"{x['L']:%a %H:%M} £{x['value']:.2f}")
per_day = {}
for x in tr:
    if x["sleeve"] == "core" and x["reason"].startswith("CORE tranche"):
        k = (x["asset"], x["L"].date()); per_day[k] = per_day.get(k, 0) + 1
flat = [x for x in tr if x["sleeve"] == "day" and "flatten" in x["reason"]]
dsell = {k: sum(1 for x in tr if x["sleeve"] == "day" and x["side"] == "SELL" and k in x["reason"]) for k in ("stop-loss", "trailing stop", "take-profit", "max hold", "signal", "flatten")}
cap_buys = [x for x in tr if x["sleeve"] == "day" and x["side"] == "BUY" and x["L"].date().isoformat() == CAP_DAY and x["L"].hour >= 11]
halt_sig = con.execute(f"SELECT COUNT(*) FROM signals WHERE sleeve='day' AND reason LIKE '%daily loss cap hit%' AND ts>=?", (common.iso(uk(5, 11)),)).fetchone()[0]
next_day_buys = [x for x in tr if x["sleeve"] == "day" and x["side"] == "BUY" and x["L"].date() == datetime.fromisoformat(CAP_DAY).date() + timedelta(days=1)]
rb = [x for x in tr if "rebalance" in x["reason"]]
stops = [x for x in tr if "core stop-loss" in x["reason"]]
big = [x for x in tr if x["sleeve"] == "day" and x["side"] == "BUY" and x["value"] > 0.5 * start["day"] / 2 * 1.2]   # v7: > momentum 50% of half an account's DAY money
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
print("day buys > 50% of a sub-strategy budget (+20% slack):", len(big), "| untagged day trades:", sum(1 for x in tr if x["sleeve"] == "day" and x["sub"] not in ("momentum", "burst")))
assert not [x for x in tr if x["sleeve"] == "day" and x["sub"] not in ("momentum", "burst")], "v7: every DAY order tagged with its sub-strategy"
print("RISK buys:", [(f"{x['L']:%a %d %H:%M}", x["asset"], round(x["value"], 2)) for x in rb_])
print("RISK exits:", risk_exit)
print(f"RISK buys per UK day {dict((str(k), v) for k, v in rpd.items())} | buys during drawdown halt {len(halt_buys)} | DAY trades in risk-only coins {len(day_in_risk)}")
print("cash vs reconstruction (incl. gas):", {s: (round(cash[s], 4), round(recon[s], 4)) for s in cash})
print("v6 cash per pot x account vs reconstruction (only same-account trades):", {f"{k[0]}/{k[1]}": (round(acash[k], 4), round(arecon[k], 4)) for k in arecon},
      "| buys per account", stock_buys_acct, "| cross-account transfers", n_cross, "| equity = crypto + stocks:", acc_eq_ok)
print(f"initial picks {PICKS0} ({len(PICKS0)} = n_picks(Core stocks £{FUND['core']['stocks']:.0f}) {stockrank.n_picks(c, FUND['core']['stocks'])}); "
      f"targets per pick £{FUND['core']['stocks'] * c['core']['stocks_pct'] / 100 / max(len(PICKS0), 1):.2f}")
print(f"Monday re-pick: {cs['picks']} | dropped {DROP} sold: {[(f'{x['L']:%a %d %H:%M}', round(x['pnl'], 2)) for x in drop_sells]} | "
      f"replacements {new_picks} bought: {new_buys} | rebalance log: {rbl.get('actions')}")
print(f"themes of picks: {[RANK0.get(a, {}).get('theme') for a in cs['picks']]} (max {c['core']['max_per_theme']} per theme)")
print(f"buys below £{mint:.0f} min trade: {len(small_buys)} | gas values seen: {gas_vals} | trades without gas: {len(no_gas)} | gas total £{sum(x['gas'] or 0 for x in tr):.4f}")
assert not small_buys, "buy below min trade"
assert rb_ and all(x["value"] <= RP["max_position_gbp"] + 1e-9 for x in rb_), "risk position size"
assert max(rpd.values()) <= RP["max_new_per_day"], "risk max new per day"
assert risk_exit["risk stop-loss"] and risk_exit["trailing take-profit"] and risk_exit["max hold"], "risk exits (stop / trailing / max hold)"
assert all(x["pnl"] / (x["value"] + 1e-9) < -0.10 for x in stop_pnl), "risk stop around -12%"
assert not [x for x in rb_ if x["asset"] == "FADEX" and uk(1, 9) <= datetime.fromisoformat(x["ts"]) < uk(2, 9, 15)], "a +80% pump (24h > +60%) must not be chased"
# (v7.1: a random-walk breakout BEFORE the scripted pump is a legitimate entry, not chasing - the old check was flaky)
assert not halt_buys and checks["risk_halt_state"] and checks["risk_resumed"], "risk drawdown cap halt / resume"
assert not day_in_risk, "DAY traded a risk-only coin"
assert not checks["max_open_violation"], "max open"
assert all(abs(cash[s] - recon[s]) < 1e-6 for s in cash), "cash reconciliation"
assert all(abs(acash[k] - arecon[k]) < 1e-6 for k in arecon), "v6 cash reconciliation per pot x account (cash crossed accounts)"
assert n_cross == 0 and acc_eq_ok and all(stock_buys_acct.values()), "v6: no automatic cross-account move; equity = crypto + stocks; both accounts traded"
assert GAS_LIVE in gas_vals and float(c["gas"]["fallback_gbp"]) in gas_vals, "gas live + fallback"
# every replacement pick gets bought - unless the pick count grew (n_picks rises with Core equity) and Core's stocks cash
# is already below one min trade (it is then built from the next rebalance trims; not a bug)
if DROP: assert DROP not in cs["picks"] and drop_sells and new_picks and any(new_buys.values()) and (
    all(new_buys.values()) or (len(cs["picks"]) > len(PICKS0) and acash[("core", "stocks")] < float(c["risk"]["min_trade_size"]))), "Monday re-pick"
assert len(cs["picks"]) == stockrank.n_picks(c, eqc) or len(cs["picks"]) == len([1 for r in RANK0.values() if r["eligible"]]), "pick count"
print(f"v7.1 reconciliation (check_data on dashboard.build every 3h + trader ledger = Day tab): {RECON['runs']} exports x {RECON['checks']} checks, failures: {RECON['fails'][:10]}")
assert not RECON["fails"], "dashboard figures must reconcile"
print("checks:", {k: (v if isinstance(v, int) or v is None else len(v)) for k, v in checks.items()})
print("final:", [dict(r) for r in con.execute("SELECT ts, equity, core, day FROM equity ORDER BY ts DESC LIMIT 1")])
print("MON tranche times (sentiment delay Sat 23:30 until ~11:30 UK Sun):", tranche.get("MON"))
print("delay signals:", tuple(con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM signals WHERE reason LIKE '%delayed: sentiment%'").fetchone()))
print("core plan at end:", {a: (v["status"], v["n"]) for a, v in json.loads(con.execute("SELECT value FROM state WHERE key='core_plan'").fetchone()[0]).items()})
print("re-entry tranches after 2nd rebalance:", [(f"{x['L']:%a %d %H:%M}", x["asset"], round(x["value"], 2), x["reason"][:60]) for x in tr if x["sleeve"] == "core" and x["side"] == "BUY" and x["L"] >= lt(uk(10, 9))])
print("MON signals Sun:", [tuple(r) for r in con.execute("SELECT ts, action, substr(reason,1,90) FROM signals WHERE sleeve='core' AND asset='MON' AND ts BETWEEN ? AND ? AND (reason LIKE '%delayed%' OR action='BUY') ORDER BY ts", (common.iso(uk(1, 23)), common.iso(uk(2, 23)))).fetchall()][:3], "...")

# ---- TRANSFER: move £1 Day -> Risk inside the stocks account; cash moves, history is rebased (no fake P&L / drawdown) ----
s0 = {s: dashboard.stats(con, s) for s in ("day", "risk")}; c0 = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
a0 = {r[0] + "/" + r[1]: r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}
st0 = {a: dashboard.stats(con, None, a) for a in common.ACCOUNTS}
common._NOW = END + timedelta(minutes=1)
rebalance_pots.transfer(con, "day", "risk", 1.0, "sim test", "stocks"); con.commit()
c1 = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
adj = common.transfer_adj(con, "risk"); last_ts = con.execute("SELECT MAX(ts) FROM equity").fetchone()[0]
print("transfer:", {k: round(c1[k] - c0[k], 4) for k in c1}, "| risk rebase of last point £", common.rebase(last_ts, adj),
      "| risk max_dd before/after", round(s0["risk"]["max_dd"], 4), round(dashboard.stats(con, "risk")["max_dd"], 4))
assert abs(c1["day"] - c0["day"] + 1) < 1e-9 and abs(c1["risk"] - c0["risk"] - 1) < 1e-9 and common.rebase(last_ts, adj) == 1.0, "transfer"
assert dashboard.stats(con, "risk")["start"] == s0["risk"]["start"] + 1 and abs(dashboard.stats(con, "risk")["ret"] - s0["risk"]["equity"] / s0["risk"]["start"] + 1) < 0.05, "pot baseline follows the transfer"
# ---- v6: manual account transfer (Stocks -> Crypto, Risk pot): baselines move, account P&L unchanged ----
common._NOW = END + timedelta(minutes=2)
b0 = common.acct_start(con)   # after the pot move (it shifts the pot x account baselines, not the account totals)
assert all(abs(sum(b0[k][a] for k in b0) - sum(FUND[k][a] for k in FUND)) < 1e-9 for a in common.ACCOUNTS), "pot move keeps account baselines"
have = con.execute("SELECT amount FROM acct_cash WHERE sleeve='risk' AND account='stocks'").fetchone()[0]
try: transfer_accounts.transfer(con, "stocks", "crypto", have + 1, "risk"); over = False
except SystemExit: over = True
amt = min(2.0, have); transfer_accounts.transfer(con, "stocks", "crypto", amt, "risk", reason="sim manual move"); con.commit()
st1 = {a: dashboard.stats(con, None, a) for a in common.ACCOUNTS}; b1 = common.acct_start(con)
a1 = {r[0] + "/" + r[1]: r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}
print(f"account transfer £{amt:.2f} risk stocks->crypto: cash {round(a1['risk/stocks'] - a0['risk/stocks'], 4)} / {round(a1['risk/crypto'] - a0['risk/crypto'], 4)} | "
      f"baselines risk {b0['risk']} -> {b1['risk']} | account P&L before/after", {a: (round(st0[a]['equity'] - st0[a]['start'], 4), round(st1[a]['equity'] - st1[a]['start'], 4)) for a in st0},
      "| overdraw refused", over)
assert over and amt > 0 and abs(b1["risk"]["crypto"] - b0["risk"]["crypto"] - amt) < 1e-9 and abs(b1["risk"]["stocks"] - b0["risk"]["stocks"] + amt) < 1e-9, "account transfer baselines"
assert all(abs((st1[a]["equity"] - st1[a]["start"]) - (st0[a]["equity"] - st0[a]["start"])) < 1e-6 for a in st0), "account transfer is not P&L"
con.close()

# ---- v7.3: inter-pot loans taken during the random run (exceptional setups short of cash) ----
con = sqlite3.connect(SIM); con.row_factory = sqlite3.Row
LNS = [dict(r) for r in con.execute("SELECT * FROM loans ORDER BY id")]
held = {(r[0], r[1]) for r in con.execute("SELECT sleeve, asset FROM positions")}
lcash = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}
capc = float(common.cfg()["loans"]["max_outstanding_pct"]) / 100
for l in LNS:
    assert l["lender"] != l["borrower"] and l["lender"] in common.SLEEVES and l["borrower"] in ("day", "risk") and l["account"] in common.ACCOUNTS, f"loan shape {l}"
    assert -1e-9 <= (l["repaid"] or 0) <= l["amount"] + 1e-9, f"repaid <= amount {l}"
    assert l["borrower"] != "day" or l["sub"] == "burst", f"only DAY burst setups borrow {l}"
    if l["status"] == "open": assert (l["borrower"], l["asset"]) in held or lcash[(l["borrower"], l["account"])] < 0.01, f"closed position's loan not repaid {l}"
    else: assert abs(l["repaid"] - l["amount"]) < 1e-6, f"repaid loan fully repaid {l}"
    out_at = sum(x["amount"] for x in LNS if x["account"] == l["account"] and x["ts"] <= l["ts"] and (x["repaid_ts"] is None or x["repaid_ts"] > l["ts"]))
    assert out_at <= capc * 100 + 0.02, f"outstanding £{out_at:.2f} > cap at {l['ts']}"
print(f"v7.3 loans in the random run: {len(LNS)} taken ({sum(1 for l in LNS if l['status'] == 'repaid')} repaid, {sum(1 for l in LNS if l['status'] == 'open')} open): "
      + "; ".join(f"{l['borrower']}<-{l['lender']} £{l['amount']:.2f} {l['asset']} ({l['account']})" for l in LNS[:8]))
con.close()

# ---- v6: the real account split on a copy of the LIVE DB (no reset), then the settlement inside each account ----
MIG = HERE / "data" / "sim_mig.db"
s_ = sqlite3.connect(HERE / "data" / "trading.db"); d_ = sqlite3.connect(MIG); s_.backup(d_); d_.close(); s_.close()
common.DB = MIG; common._NOW = None
con = common.db()
for t_ in ("acct_cash",): con.execute(f"DELETE FROM {t_}")
con.execute("DELETE FROM transfers WHERE kind IS NOT NULL"); con.execute("DELETE FROM state WHERE key='account_split'"); con.execute("UPDATE equity SET accts=NULL, crypto=NULL, stocks=NULL")
pot_cash0 = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
lt_ = con.execute("SELECT MAX(ts) FROM equity").fetchone()[0]
common._NOW = datetime.fromisoformat(lt_) + timedelta(minutes=1)
st = migrate_v6_accounts.migrate(con, c)
O = st["opening"]; tot = {a: sum(O[k][a] for k in O) for a in common.ACCOUNTS}
pot_cash1 = {s: sum(r[0] for r in con.execute("SELECT amount FROM acct_cash WHERE sleeve=?", (s,))) for s in common.SLEEVES}
print("v6 migration of the live DB: opening", {k: {a: round(v, 2) for a, v in d.items()} for k, d in O.items()}, "| account totals", {a: round(v, 4) for a, v in tot.items()},
      "| pending", st["pending"])
assert all(abs(tot[a] - sum(FUND[k][a] for k in FUND)) < 0.01 for a in tot), "account totals = funding targets"
assert all(abs(pot_cash1[s] - pot_cash0[s]) < 1e-9 for s in pot_cash0), "pot cash unchanged by the split"
assert all(abs(sum(O[k].values()) - common.start_cash(c)[k]) < 0.01 for k in O), "pot baselines unchanged"
for leg in st["pending"]: assert leg["account"] in common.ACCOUNTS and leg["from"] != leg["to"], "settlement stays inside one account"
# DAY flatten at 22:05 UK on the live DB day frees the crypto cash -> trader settles all legs at once
d_uk = datetime.fromisoformat(lt_).astimezone(LON).date()
common._NOW = datetime(d_uk.year, d_uk.month, d_uk.day, 22, 5, tzinfo=LON).astimezone(timezone.utc)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): trader.main()
print("trader at 22:05 UK:", buf.getvalue().strip().replace("\n", " / ")[:900])
st2 = json.loads(con.execute("SELECT value FROM state WHERE key='account_split'").fetchone()[0])
b2 = common.acct_start(con, c)
print("after settlement: baselines", {k: {a: round(v, 4) for a, v in d.items()} for k, d in b2.items()}, "| settled", st2.get("settled"))
if st["pending"]:
    assert st2.get("settled") and not st2.get("pending"), "settlement executed"
    assert all(abs(b2[k][a] - FUND[k][a]) < 0.01 for k in FUND for a in FUND[k]), "every pot x account at its target after settlement"
assert not con.execute("SELECT COUNT(*) FROM transfers WHERE kind='settle' AND from_account != to_account").fetchone()[0], "settlement never crosses accounts"
con.close(); MIG.unlink()

# =====================================================================================================================
# v7: DAY pot sub-strategies (momentum + burst) - deterministic one-day scenario on a fresh throwaway DB
# =====================================================================================================================
print("\n================ v7 DAY momentum + burst scenario ================")
import daypot
V7 = HERE / "data" / "sim_v7.db"
if V7.exists(): V7.unlink()
s_ = sqlite3.connect(HERE / "data" / "trading.db"); d_ = sqlite3.connect(V7); s_.backup(d_); d_.close(); s_.close()
common.DB = V7; common._NOW = None
con = common.db()
for t_ in ("trades", "positions", "cash", "acct_cash", "equity", "signals", "transfers", "prices", "sentiment"): con.execute(f"DELETE FROM {t_}")
con.execute("DELETE FROM state WHERE key NOT IN ('stock_meta')")
con.executemany("INSERT INTO acct_cash VALUES(?,?,?)", [(k, a, v) for k, d in FUND.items() for a, v in d.items()]); common.sync_pot_cash(con)
for k_ in ("core_plan", "core_stocks", "order_queue"): con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k_, json.dumps({} if k_ == "core_plan" else [] if k_ == "order_queue" else {"picks": []})))
con.execute("INSERT OR REPLACE INTO state VALUES('core_last_rebalance',?)", (json.dumps(common.iso(T0 + timedelta(days=30))),))
con.execute("INSERT OR REPLACE INTO state VALUES('risk_only',?)", (json.dumps([]),)); con.execute("INSERT OR REPLACE INTO state VALUES('risk_only_stocks',?)", (json.dumps([]),))
con.commit()
# a weekday with a normal US session (UK 14:30-21:00)
D7 = lt(T0).date() + timedelta(days=1)
while D7.weekday() >= 5 or not markets.session("US", D7): D7 += timedelta(days=1)
U = lambda h, m=0: datetime(D7.year, D7.month, D7.day, h, m, tzinfo=LON).astimezone(timezone.utc)
START, STOP = U(8) - timedelta(hours=30), U(22, 15)
# scripted paths: piecewise multiplicative moves (asset, from, to, total multiplier); vol24 jumps (asset, at, x)
CRY = ["BTC", "BRK_TP", "BRK_TRAIL", "BRK_STOP", "BRK_HOLD", "BRK_NOVOL", "BRK_RSI", "BRK_NEG", "BRK_MKT", "BRK_CD", "BRK_HOT", "MOMO"]
B1 = 1.01      # breakout candle: +1% above a quiet 30h range (stored prices = scripted path x (1 + iid noise 0.1%))
MOVES = [("BRK_TP", U(9, 30), U(9, 45), B1), ("BRK_TP", U(9, 45), U(10, 30), 1.06), ("BRK_TP", U(10, 30), U(11, 30), 1.004),     # break -> +6% -> TP
         ("BRK_TRAIL", U(10), U(10, 15), B1), ("BRK_TRAIL", U(10, 15), U(11), 1.032), ("BRK_TRAIL", U(11), U(12), 0.97),         # +3.2% then fades -> trailing
         ("BRK_STOP", U(11), U(11, 15), B1), ("BRK_STOP", U(11, 15), U(12), 0.965),                                               # break then -3.5% -> stop
         ("BRK_HOLD", U(12), U(12, 15), B1),                                                                                      # break then flat -> 6h max hold
         ("BRK_NOVOL", U(12, 30), U(12, 45), B1),                                                                                 # break, no volume spike
         ("BRK_RSI", U(0), U(13), 1.12), ("BRK_RSI", U(13), U(13, 15), B1),                                                       # relentless climb (momentum buys it)
         ("BRK_CD", U(14), U(14, 15), B1), ("BRK_CD", U(14, 15), U(14, 45), 0.97), ("BRK_CD", U(14, 45), U(15), 1.04),               # stopped out, re-breaks < 1h later
         ("BRK_HOT", U(16), U(19), 1.04), ("BRK_HOT", U(19), U(19, 15), B1),                                                      # overbought breakout (RSI > 80)
         ("BRK_NEG", U(13, 30), U(13, 45), B1),                                                                                   # break, negative sentiment
         ("BTC", U(15), U(16), 0.985), ("BRK_MKT", U(15, 30), U(15, 45), B1),                                                     # break while BTC falls
         ("MOMO", U(8) - timedelta(hours=24), U(8, 30), 1.10), ("MOMO", U(8, 30), U(12), 1.09), ("MOMO", U(12), U(13), 0.95),    # momentum: run then fade
         ("SIMSTK", U(15, 30), U(15, 45), B1), ("SIMSTK", U(15, 45), U(16, 30), 1.01),
         ("NEWC", U(12), U(12, 15), B1), ("NEWC", U(14, 15), U(14, 30), B1)]                                                       # v7.2: new universe coin listed 09:45
NEWC0 = U(9, 45)                                                                                                                  #   -> warming up until 13:45 (4h)                                            # US stock breakout (flattened before close)
VOLJ = {"BRK_TP": [U(9, 30)], "BRK_TRAIL": [U(10)], "BRK_STOP": [U(11)], "BRK_HOLD": [U(12)], "BRK_RSI": [U(13)], "BRK_NEG": [U(13, 30)], "BRK_MKT": [U(15, 30)],
        "BRK_CD": [U(14), U(14, 45)], "BRK_HOT": [U(19)], "NEWC": [U(12), U(14, 15)]}
P7 = {a: 1.0 for a in CRY + ["NEWC"]}; P7["BTC"] = 80000.0; P7["SIMSTK"] = 50.0; V24 = {a: 1e8 for a in CRY + ["NEWC"]}
rng7 = random.Random(7)
def step_prices(t):
    for a in list(P7):
        if a == "SIMSTK" and not markets.is_open(a, t): continue
        if a == "NEWC" and t < NEWC0: continue          # v7.2: not in the universe before 09:45 -> history builds from snapshots
        r = 0.0
        for ea, f0, f1, mult in MOVES:
            if ea == a and f0 <= t < f1: r += mult ** (1 / max(1, int((f1 - f0).total_seconds() // 900))) - 1
        P7[a] *= 1 + r
        if any(v <= t < v + timedelta(minutes=15) for v in VOLJ.get(a, [])): V24[a] *= 1.15     # last hour ~4.6x the average hour
        k = "stock" if a == "SIMSTK" else "crypto"
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?)", (common.iso(t), a, k, P7[a] * (1 + rng7.gauss(0, 0.001)), None, None, V24.get(a), None, "sim"))
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (common.iso(t), "USDGBP", rate, "sim", None, None))
    if markets.is_open("US", t):
        con.execute("INSERT OR REPLACE INTO state VALUES('market_proxy',?)", (json.dumps({"US": dict(sym="^GSPC", ts=common.iso(t), price=1, chg1h=0.1, chg_day=0.3)}),))
        con.execute("INSERT OR REPLACE INTO state VALUES('stock_vol',?)", (json.dumps({"SIMSTK": dict(ts=common.iso(t), ratio=3.0 if t >= U(15, 45) else 1.0)}),))
t = START
while t < U(7, 45): common._NOW = t; step_prices(t); t += timedelta(minutes=15)       # 30h of history, no trading
con.execute("INSERT OR REPLACE INTO sentiment VALUES(?,?,?,?,?,?,?,?,?)", (common.iso(U(8)), "BRK_NEG", -0.6, 10, 0.8, "sim bearish", "[]", "sim", common.iso(U(8))))
con.execute("INSERT OR REPLACE INTO sentiment VALUES(?,?,?,?,?,?,?,?,?)", (common.iso(U(15)), "BRK_HOT", -1.0, 10, 1.0, "sim: keeps momentum out", "[]", "sim", common.iso(U(15))))
con.commit()
v7 = dict(burst_open_max={a: 0 for a in common.ACCOUNTS}, flat_after=[], cands_seen={}, thr=[], neg=[])
while t <= STOP:
    common._NOW = t; step_prices(t)
    if t == U(14, 7): con.execute("INSERT OR REPLACE INTO sentiment VALUES(?,?,?,?,?,?,?,?,?)", (common.iso(U(14)), "BRK_NEG", -0.6, 10, 0.8, "sim bearish", "[]", "sim", common.iso(U(14))))
    con.commit()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): trader.main()
    o = buf.getvalue()
    if "no trades this cycle" not in o: print(o.rstrip()[:1500])
    ds = json.loads(con.execute("SELECT value FROM state WHERE key='day_subs'").fetchone()[0])
    v7["thr"].append(ds["threshold"])
    for d in ds["cands"]: v7["cands_seen"].setdefault(d["a"], set()).add("BUY" if d.get("bought") else (d["block"] or "")[:60])
    dm = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0])
    for ac in common.ACCOUNTS:
        n = sum(1 for r in con.execute("SELECT asset, klass FROM positions WHERE sleeve='day'") if (dm.get(r[0]) or {}).get("sub") == "burst" and common.acct_of(r[1]) == ac)
        v7["burst_open_max"][ac] = max(v7["burst_open_max"][ac], n)
    L = lt(t)
    if L.hour * 60 + L.minute >= 22 * 60 and con.execute("SELECT COUNT(*) FROM positions WHERE sleeve='day'").fetchone()[0]: v7["flat_after"].append(L.isoformat())
    if any(r[0] < -1e-9 for r in con.execute("SELECT amount FROM acct_cash")): v7["neg"].append(L.isoformat())
    t += timedelta(minutes=15)
tr7 = [dict(r) for r in con.execute("SELECT * FROM trades WHERE sleeve='day' ORDER BY id")]
for x in tr7: x["L"] = lt(datetime.fromisoformat(x["ts"]))
fmt = lambda x: f"{x['L']:%H:%M} {x['side']} {x['asset']} [{x['sub']}] £{x['value']:.2f}" + (f" P&L £{x['pnl']:+.3f}" if x["pnl"] is not None else "") + f" | {x['reason'][:110]}"
print("v7 DAY trades:"); [print("  " + fmt(x)) for x in tr7]
print("v7 burst candidate decisions:", {a: sorted(v) for a, v in v7["cands_seen"].items()})
sells, buys = {}, {}
for x in tr7: (sells if x["side"] == "SELL" else buys).setdefault(x["asset"], x)     # first round trip per asset
def why(a): return " | ".join(sorted(v7["cands_seen"].get(a, [])))
assert all(x["sub"] in ("momentum", "burst") for x in tr7), "v7: every DAY order tagged"
assert buys.get("BRK_TP", {}).get("sub") == "burst" and "take-profit" in sells["BRK_TP"]["reason"] and sells["BRK_TP"]["pnl"] > 0, "burst take-profit"
assert "trailing stop" in sells["BRK_TRAIL"]["reason"] and sells["BRK_TRAIL"]["pnl"] > 0, "burst trailing stop locks a gain"
assert "stop-loss" in sells["BRK_STOP"]["reason"] and sells["BRK_STOP"]["pnl"] < 0, "burst ATR stop-loss"
assert "max hold" in sells["BRK_HOLD"]["reason"], "burst 6h max hold"
assert "BRK_NOVOL" not in buys and "volume" in why("BRK_NOVOL"), "no volume spike -> skipped"
assert "BRK_HOT" not in buys and "RSI" in why("BRK_HOT"), "RSI >= 80 -> skipped"
assert "BRK_NEG" not in buys and "sentiment" in why("BRK_NEG"), "negative sentiment -> skipped"
assert "BRK_MKT" not in buys and "market falling" in why("BRK_MKT"), "BTC falling -> skipped"
assert buys.get("MOMO", {}).get("sub") == "momentum" and sells.get("MOMO") and sells["MOMO"]["pnl"] > 0 and ("trailing stop" in sells["MOMO"]["reason"] or "take-profit" in sells["MOMO"]["reason"]), "momentum winner exits via trailing / target"
mo_gain = sells["MOMO"]["pnl"] / (sells["MOMO"]["value"] - sells["MOMO"]["gas"] - sells["MOMO"]["pnl"]) * 100
print(f"MOMO momentum round trip {mo_gain:+.2f}% (old fixed cap was +5%)")
assert buys.get("SIMSTK", {}).get("sub") == "burst" and ("flatten" in sells["SIMSTK"]["reason"] or "take-profit" in sells["SIMSTK"]["reason"] or "trailing" in sells["SIMSTK"]["reason"]), "stock burst (index proxy + Yahoo volume), flat before the close"
cool = [x for x in tr7 if x["side"] == "BUY" and x["sub"] == "burst" and x["asset"] in sells and x["ts"] > sells[x["asset"]]["ts"] and
        datetime.fromisoformat(x["ts"]) < datetime.fromisoformat(sells[x["asset"]]["ts"]) + timedelta(hours=1)]
assert "stop-loss" in sells["BRK_CD"]["reason"] and not cool and any("cooldown" in s_ for s_ in v7["cands_seen"].get("BRK_CD", [])), "1h re-entry cooldown after an exit"
assert all(v <= c["day"]["burst"]["max_open_per_account"] for v in v7["burst_open_max"].values()), "max open bursts per account"
assert not v7["flat_after"] and not v7["neg"], "flat by 22:00 UK, no negative cash"
# per-trade economics: target >= 4x round-trip costs, size = confidence x 33% of free burst cash (min trade floor)
for x in tr7:
    if x["side"] == "BUY" and x["sub"] == "burst":
        assert x["conf"] is not None and x["conf"] >= daypot.threshold(c["day"]["burst"], 0, 15) - 1e-9, "burst buy above the confidence threshold"
# stats per sub-strategy
st7 = dashboard.day_sub_stats(con, lt(STOP))
for sb in ("momentum", "burst"):
    x = st7[sb]["all:all"]; print(f"v7 stats {sb}: {x}")
    assert x["trades"] == sum(1 for y in tr7 if y["side"] == "SELL" and y["sub"] == sb) and abs(x["pnl"] - sum(y["pnl"] for y in tr7 if y["side"] == "SELL" and y["sub"] == sb)) < 1e-3, "sub stats"
# unit checks: capital-based threshold across the 15% cap, sizing proportional to confidence x free cash, EV, cost filter
B7 = c["day"]["burst"]; cap = c["day"]["daily_loss_cap_pct"]
th = [daypot.threshold(B7, -x / 100, cap) for x in (0, 3, 7.5, 15, 20)]
print("threshold at day loss 0/3/7.5/15/20%:", th)
assert th[0] == B7["conf_threshold"] and th[0] < th[1] < th[2] < th[3] and abs(th[3] - (B7["conf_threshold"] + B7["conf_raise_at_cap"])) < 1e-9 and th[4] == th[3], "threshold scales over the cap"
assert daypot.threshold(B7, 0.05, cap) == B7["conf_threshold"], "gains never lower the threshold"
assert abs(daypot.conf_size(B7, 0.8, 15) - 0.8 * 0.33 * 15) < 1e-6 and daypot.conf_size(B7, 0.8, 7.5) < daypot.conf_size(B7, 0.8, 15), "size = confidence x 33% of the remaining cash"
assert daypot.expected_value(dict(p=0.5, n=0, exp=None), 3, 1.5, 0.6) == round(0.5 * 3 - 0.5 * 1.5 - 0.6, 3), "EV model"
assert daypot.expected_value(dict(p=0.3, n=0, exp=None), 3, 2.5, 0.6) < 0, "negative EV setup"
print(f"v7 thresholds seen during the day: {min(v7['thr'])}..{max(v7['thr'])} | max open bursts {v7['burst_open_max']}")
# min-move filter: with min_move_cost_x = 20 (crypto round trip 0.6% -> 12% > every target) nothing may be bought
import copy
c20 = copy.deepcopy(c); c20["day"]["min_move_cost_x"] = 20; _cfg = trader.cfg; trader.cfg = lambda: c20
n0 = con.execute("SELECT COUNT(*) FROM trades WHERE side='BUY'").fetchone()[0]
common._NOW = U(10); con.execute("DELETE FROM state WHERE key LIKE 'day_exit:%'"); con.commit()
with contextlib.redirect_stdout(io.StringIO()): trader.main()
trader.cfg = _cfg
ds = json.loads(con.execute("SELECT value FROM state WHERE key='day_subs'").fetchone()[0])
print("min-move filter (x20):", [(d["a"], d["block"]) for d in ds["cands"]][:4])
assert con.execute("SELECT COUNT(*) FROM trades WHERE side='BUY'").fetchone()[0] == n0, "min expected move filter"
# v7.2: a coin that just joined the DAY universe is 'warming up' (no momentum/burst entry) until warmup_h of history exist
wh = float(c["crypto"].get("warmup_h", 4))
newc_sig = con.execute("SELECT ts, action, reason FROM signals WHERE sleeve='day' AND asset='NEWC' ORDER BY ts").fetchall()
warm_sig = [r for r in newc_sig if "warming up" in (r[2] or "")]
early_buy = [x for x in tr7 if x["asset"] == "NEWC" and x["side"] == "BUY" and datetime.fromisoformat(x["ts"]) < NEWC0 + timedelta(hours=wh)]
print(f"v7.2 NEWC (listed {lt(NEWC0):%H:%M} UK): {len(warm_sig)} 'warming up' signals until {lt(datetime.fromisoformat(warm_sig[-1][0])):%H:%M} UK, "
      f"burst decisions {sorted(v7['cands_seen'].get('NEWC', []))}, buys before warm: {len(early_buy)}")
assert warm_sig and not early_buy, "new universe coin warms up before any DAY entry"
assert datetime.fromisoformat(warm_sig[-1][0]) < NEWC0 + timedelta(hours=wh, minutes=1), "warm-up ends after warmup_h"
assert any("warming up" in x for x in v7["cands_seen"].get("NEWC", [])), "burst setup on a warming coin is skipped with 'warming up'"
con.close(); V7.unlink(); common.DB = SIM
import subprocess
r_ = subprocess.run([sys.executable, str(HERE / "test" / "test_universe.py")], capture_output=True, text=True); print(r_.stdout.strip())
assert r_.returncode == 0, "test_universe.py (DAY universe filter + sentiment mapping)"
r_ = subprocess.run([sys.executable, str(HERE / "test" / "test_loans.py")], capture_output=True, text=True); print(r_.stdout.strip()[-1500:])
assert r_.returncode == 0, "test_loans.py (v7.3 inter-pot loans: triggers, lenders, CORE tranche reserve, cap, repayment)"
print("v7 DAY checks passed")

print("ALL CHECKS PASSED")

if "--keep" not in sys.argv:
    SIM.unlink(); print("deleted test copy", SIM)
