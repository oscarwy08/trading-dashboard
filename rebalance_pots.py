"""Move UNINVESTED cash between pots (sleeves) without trading and without resetting the account.
Usage: python3 rebalance_pots.py FROM TO AMOUNT|cash|topup [reason]
  AMOUNT  GBP to move (must be <= FROM's cash)
  cash    all of FROM's cash
  topup   whatever brings TO's cash+holdings up to its config start (sleeve_split_pct), limited by FROM's cash
Each move is recorded in table transfers (shown as TRANSFER rows on the dashboard Orders tab). Sleeve history is rebased
by later transfers in stats()/dashboard, so a transfer is not a gain/loss and does not trip drawdown limits; the DAY
pot's UK-day start equity is adjusted too so the DAY daily loss cap does not fire. Backs up the DB first.
After changing sleeve_split_pct in config.yaml, run moves so each pot's start = old start +/- transfers (v4, 2026-10-10:
CORE 120 -> 100, DAY 80 -> 60, RISK 0 -> 40)."""
import sqlite3, sys
from datetime import datetime
from common import DB, LON, SLEEVES, cfg, db, iso, start_cash

def transfer(con, frm, to, amount, reason):
    assert frm in SLEEVES and to in SLEEVES and frm != to, "unknown/same sleeve"
    cash = {r[0]: r[1] for r in con.execute("SELECT sleeve, amount FROM cash")}
    have = cash.get(frm, 0.0)
    if amount == "cash": amount = have
    elif amount == "topup":
        eq_to = cash.get(to, 0.0) + sum(r[0] * r[1] for r in con.execute(
            "SELECT p.qty, (SELECT price*(SELECT rate FROM fx WHERE pair='USDGBP' ORDER BY ts DESC LIMIT 1) FROM prices WHERE asset=p.asset "
            "AND source!='backfill' ORDER BY ts DESC LIMIT 1) FROM positions p WHERE sleeve=?", (to,)))
        amount = min(have, max(0.0, start_cash()[to] - eq_to))
    amount = float(amount)
    if amount <= 0: print(f"nothing to move {frm}->{to}"); return 0.0
    if amount > have + 1e-9: raise SystemExit(f"{frm} has only £{have:.4f} cash (asked £{amount:.4f}) - would need selling; not done")
    ts = iso()
    con.execute("INSERT OR REPLACE INTO cash VALUES(?,?)", (frm, have - amount))
    con.execute("INSERT OR REPLACE INTO cash VALUES(?,?)", (to, cash.get(to, 0.0) + amount))
    con.execute("INSERT INTO transfers VALUES(?,?,?,?,?)", (ts, frm, to, amount, reason))
    day = datetime.now(LON).date().isoformat()
    for sl, d in ((frm, -amount), (to, amount)):          # keep same-day loss caps neutral
        k = f"day_start:{sl}:{day}"
        r = con.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
        if r: con.execute("UPDATE state SET value=? WHERE key=?", (str(float(r[0]) + d), k))
    print(f"TRANSFER £{amount:.5f} cash {frm} -> {to} ({reason})")
    return amount

if __name__ == "__main__":
    if len(sys.argv) < 4: raise SystemExit(__doc__)
    bak = DB.with_name(DB.name + f".bak-transfer-{datetime.now(LON):%Y%m%d-%H%M%S}")
    s = sqlite3.connect(DB); d = sqlite3.connect(bak); s.backup(d); d.close(); s.close(); print("backup", bak)
    con = db()
    transfer(con, sys.argv[1], sys.argv[2], sys.argv[3] if sys.argv[3] in ("cash", "topup") else float(sys.argv[3]),
             " ".join(sys.argv[4:]) or "pot rebalance")
    con.commit()
    print({r[0]: round(r[1], 4) for r in con.execute("SELECT sleeve, amount FROM cash")})
