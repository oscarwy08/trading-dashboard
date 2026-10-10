"""v7.3 inter-pot loans: deterministic test of trader.py on a throwaway COPY of the live DB with one synthetic RISK breakout
coin (TESTX, risk-only, rising 24h history, screen score set by the test). Checks:
  1. exceptional breakout (>= 0.65, momentum > 0) short of RISK cash -> borrows the shortfall from CORE (same account only),
     at most one position (normal size), buys; CORE/RISK pot values unchanged by the loan (receivable / liability)
  2. position closes (stop-loss) -> principal repaid to CORE first, P&L stays with RISK, loan status 'repaid'
  3. CORE idle cash below today's tranche reserve -> lends nothing; the other short-term pot (DAY) lends instead
  4. cap: outstanding per account <= max_outstanding_pct of the account start; 5. score 0.60 (< 0.65) -> no loan
  6. never crypto <-> stocks: every loan + cash change stays in the crypto account
Usage: python3 test/test_loans.py   (writes only to a temp DB)."""
import json, os, sqlite3, sys, tempfile
from datetime import timedelta
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp()); DBF = TMP / "loans.db"
os.environ["TRADING_DB"] = str(DBF); os.environ["TRADING_LOG"] = str(TMP / "loans.log")
sys.path.insert(0, str(HERE))
src = sqlite3.connect(HERE / "data" / "trading.db"); dst = sqlite3.connect(DBF); src.backup(dst); dst.close(); src.close()
import common, trader, contextlib, io
C0 = common.cfg()
_c0 = sqlite3.connect(DBF); PLAN0 = _c0.execute("SELECT value FROM state WHERE key='core_plan'").fetchone()[0]; _c0.close()
fails = []
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond: fails.append(what)

def setup(score, risk_cash, core_cash=None, day_cash=None, cap_pct=20):
    con = common.db(); t = common.now() if common._NOW else None
    common._NOW = (common._NOW or trader.now()) + timedelta(minutes=15)
    t = common._NOW
    for tb in ("loans",): con.execute(f"DELETE FROM {tb}")
    con.execute("DELETE FROM positions WHERE asset='TESTX'")
    con.execute("DELETE FROM trades WHERE sleeve='risk' AND asset='TESTX'"); con.execute("DELETE FROM state WHERE key LIKE 'risk_exit:%' OR key='riskpot_halt'")
    con.execute("DELETE FROM trades WHERE sleeve='risk' AND side='BUY' AND ts>=?", ((t - timedelta(days=1)).isoformat(),))
    for (sl, ac), v in {("risk", "crypto"): risk_cash, ("core", "crypto"): core_cash, ("day", "crypto"): day_cash}.items():
        if v is not None: con.execute("UPDATE acct_cash SET amount=? WHERE sleeve=? AND account=?", (v, sl, ac))
    con.execute("UPDATE state SET value=? WHERE key='core_plan'", (PLAN0,))   # CORE coins still building (no tranche bought today)
    con.execute("DELETE FROM prices WHERE asset='TESTX'")
    for i in range(97):          # 24h of 15-min snapshots, +0.1% per step (breakout, momentum > 0), last one fresh
        ts = (t - timedelta(minutes=15 * (96 - i))).isoformat()
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, "TESTX", "crypto", 1.0 * 1.001 ** i, None, None, 5e7, 3e8, "coingecko", ts))
    con.execute("INSERT OR REPLACE INTO state VALUES('risk_screen',?)", (json.dumps(dict(ts=t.isoformat(), top=[dict(a="TESTX", id="testx", score=score, ok=True, why="synthetic breakout")])),))
    con.execute("INSERT OR REPLACE INTO state VALUES('risk_only',?)", (json.dumps(["TESTX"]),))
    con.execute("INSERT OR REPLACE INTO state VALUES('riskpot_base',?)", (json.dumps(None),))
    con.commit(); con.close()
    c = json.loads(json.dumps(C0)); c["loans"]["max_outstanding_pct"] = cap_pct; trader.cfg = lambda: c
    return c

def run():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): trader.main()
    return buf.getvalue()

def snap():
    con = common.db()
    cash = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, account, amount FROM acct_cash")}
    loans = [dict(r) for r in con.execute("SELECT * FROM loans ORDER BY id")]
    pos = {(r[0], r[1]): r[2] for r in con.execute("SELECT sleeve, asset, cost FROM positions")}
    ln = common.loan_net(con); con.close()
    return cash, loans, pos, ln

common._NOW = trader.now().replace(second=0)
# v7.4: the RISK loan trigger is the setup CONFIDENCE (>= 0.65, trend agreeing). Stub: TESTX's confidence = its screen score
# (0.80 / 0.60 below), so this test is about the loan rules, not about the confidence model (test/sim.py covers that).
import riskpot
_rconf = riskpot.confidence
riskpot.confidence = lambda R, brk, *a: (brk, dict(breakout=brk, volume=brk, sentiment=brk, market=brk, hit_rate=brk)) if brk in (0.80, 0.60) else _rconf(R, brk, *a)
print("1. exceptional breakout short of cash -> borrow from CORE")
c = setup(0.80, risk_cash=3.0, core_cash=60.0)
before, _, _, _ = snap(); out = run(); cash, loans, pos, ln = snap()
check(len(loans) == 1 and loans[0]["lender"] == "core" and loans[0]["borrower"] == "risk" and loans[0]["account"] == "crypto", f"one loan core -> risk in the crypto account: {loans}")
check(("risk", "TESTX") in pos and pos[("risk", "TESTX")] >= 5 - 1e-6, f"TESTX bought for >= £5 ({pos.get(('risk', 'TESTX'))})")
if loans:
    L = loans[0]; check(L["amount"] <= C0["riskpot"]["max_position_gbp"] + 1e-6, f"loan £{L['amount']:.2f} <= one position (max £{C0['riskpot']['max_position_gbp']})")
    check(abs((before[("core", "crypto")] - cash[("core", "crypto")]) - L["amount"]) < 1e-6, "core cash down by exactly the loan")
    check(abs(ln.get(("core", "crypto"), 0) - L["amount"]) < 1e-6 and abs(ln.get(("risk", "crypto"), 0) + L["amount"]) < 1e-6, "receivable on core = liability on risk")
check(all(abs(before[k] - cash[k]) < 1e-9 for k in before if k[1] == "stocks"), "stocks account cash untouched (never crypto <-> stocks)")
check("[loan] risk borrows" in out, "loan logged in the cycle output")

print("2. position closes -> principal repaid first, P&L stays with RISK")
con = common.db(); common._NOW += timedelta(minutes=15)
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (common._NOW.isoformat(), "TESTX", "crypto", 0.7, None, None, 5e7, 3e8, "coingecko", common._NOW.isoformat())); con.commit(); con.close()
c1, _, _, _ = snap(); out = run(); cash2, loans2, pos2, ln2 = snap()
check(("risk", "TESTX") not in pos2, "TESTX sold (stop-loss)")
check(loans2 and loans2[0]["status"] == "repaid" and abs(loans2[0]["repaid"] - loans2[0]["amount"]) < 1e-6, f"loan repaid in full: {loans2}")
if loans2:
    check(abs((cash2[("core", "crypto")] - c1[("core", "crypto")]) - loans2[0]["amount"]) < 1e-6, "core got exactly the principal back (no share of the loss)")
con = common.db(); pnl = con.execute("SELECT pnl FROM trades WHERE sleeve='risk' AND asset='TESTX' AND side='SELL'").fetchone(); con.close()
check(pnl is not None and pnl[0] < 0, f"the loss stays with RISK (sell P&L {pnl[0] if pnl else None})")
check(not ln2, "no outstanding loans left")

print("3. CORE idle cash needed for today's tranches -> DAY lends instead")
setup(0.80, risk_cash=3.0, core_cash=19.0, day_cash=30.0)
con = common.db(); rate = common.usd_gbp(con)[0]; btc = con.execute("SELECT price FROM prices WHERE asset='BTC' ORDER BY ts DESC LIMIT 1").fetchone()[0] * rate
con.execute("INSERT OR REPLACE INTO positions VALUES('core','BTC','crypto',?,?,?,?,1)", (40 / btc, btc, 40.0, common._NOW.isoformat()))   # Core crypto £59: £40 BTC + £19 cash
con.commit(); con.close()   # -> MON/XRP/WLD/ETH each still need a £5 tranche today (= £20 + gas > £19 cash): Core has no idle cash
run(); _, loans3, pos3, _ = snap()
check(loans3 and all(l["lender"] != "core" for l in loans3) and any(l["lender"] == "day" for l in loans3), f"lender = day: {loans3}")

print("4. cap: outstanding loans per account <= max_outstanding_pct of its start")
setup(0.80, risk_cash=3.0, core_cash=60.0, cap_pct=1)       # 1% of £100 = £1 -> not enough for a £5 trade
run(); _, loans4, pos4, _ = snap()
check(not loans4, f"no loan: the £1 cap cannot make a £5 trade possible ({loans4})")
check(("risk", "TESTX") not in pos4, "still no trade (size < min after the capped loan)")

print("5. score 0.60 (< 0.65) -> no loan")
setup(0.60, risk_cash=3.0, core_cash=60.0)
run(); _, loans5, pos5, _ = snap()
check(not loans5 and ("risk", "TESTX") not in pos5, "no loan, no trade")

print("6. DAY burst setup (confidence >= 0.75, EV > 0) short of burst cash -> borrows for ONE position, tagged 'burst'")
import daypot
_orig = {k: getattr(daypot, k) for k in ("breakout", "crypto_vol_ratio", "rsi_h", "market_regime", "confidence", "expected_value")}
def burst_case(conf, ev):
    """TESTB = a clean 24h-high breakout with 3x volume (the scoring functions are stubbed for TESTB only, so the test is
    about the loan rules, not about building a perfect chart)."""
    c = setup(0.10, risk_cash=40.0, core_cash=60.0, day_cash=3.0)        # RISK not interested (score 0.10); DAY crypto cash £3
    c["day"].update(entry_start_uk="00:00", entry_cutoff_uk="23:59", flatten_uk="23:59")
    con = common.db(); t = common._NOW
    con.execute("DELETE FROM prices WHERE asset='TESTB'"); con.execute("DELETE FROM loans"); con.execute("DELETE FROM positions WHERE asset='TESTB'")
    con.execute("DELETE FROM state WHERE key LIKE 'day_exit:%' OR key LIKE 'day_halt:%'")
    for i in range(97):
        _ts = (t - timedelta(minutes=15 * (96 - i))).isoformat()
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (_ts, "TESTB", "crypto", 2.0 * (1.0005 ** i), None, None, 5e7, 4e8, "coingecko", _ts))
    con.commit(); con.close()
    daypot.breakout = lambda con_, a_, pts, DB: dict(kind="24h", strength=0.9, info="synthetic 24h-high break") if a_ == "TESTB" else _orig["breakout"](con_, a_, pts, DB)
    daypot.crypto_vol_ratio = lambda con_, a_, t_: 3.0 if a_ == "TESTB" else _orig["crypto_vol_ratio"](con_, a_, t_)
    daypot.rsi_h = lambda pts: 60.0
    daypot.market_regime = lambda con_, k_, a_, *r: (True, 1.0, "market ok (stub)") if a_ == "TESTB" else _orig["market_regime"](con_, k_, a_, *r)
    daypot.confidence = lambda DB, strength, *r: (conf, dict(stub=conf)) if strength == 0.9 else _orig["confidence"](DB, strength, *r)
    daypot.expected_value = lambda ss, tp, sl, rtc, n: ev
    try:
        out = run()
    finally:
        for k, v in _orig.items(): setattr(daypot, k, v)
    return out, snap()
out6, (cash6, loans6, pos6, ln6) = burst_case(0.85, 1.0)
bl = [l for l in loans6 if l["borrower"] == "day"]
check(len(bl) == 1 and bl[0]["sub"] == "burst" and bl[0]["lender"] == "core" and bl[0]["account"] == "crypto" and bl[0]["asset"] == "TESTB", f"one burst loan core -> day (crypto): {bl}")
check(("day", "TESTB") in pos6 and pos6[("day", "TESTB")] >= 5 - 1e-6, f"TESTB bought for >= £5 ({pos6.get(('day', 'TESTB'))})")
B = common.cfg()["day"]["burst"]
if bl: check(bl[0]["amount"] <= 0.33 * 60 + 1e-6, f"loan £{bl[0]['amount']:.2f} sized for one position")
out7, (cash7, loans7, pos7, _) = burst_case(0.70, 1.0)
check(not loans7 and ("day", "TESTB") not in pos7, f"confidence 0.70 < 0.75 -> no loan, no trade ({loans7})")
out8, (cash8, loans8, pos8, _) = burst_case(0.85, -0.2)
check(not loans8 and ("day", "TESTB") not in pos8, f"EV < 0 -> no loan, no trade ({loans8})")

print("[test_loans] " + ("OK" if not fails else f"{len(fails)} FAILED: " + "; ".join(fails)))
sys.exit(1 if fails else 0)
