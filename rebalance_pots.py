"""Move UNINVESTED cash between pots (sleeves) INSIDE ONE ACCOUNT without trading and without resetting the account.
v6: cash is kept per pot x account (crypto = Monad wallet, stocks = Trading 212). A pot move is bookkeeping inside one real
account (no money leaves the wallet / the broker), so it never crosses accounts - for that use transfer_accounts.py.
Usage: python3 rebalance_pots.py FROM TO AMOUNT|cash|topup ACCOUNT [reason]
  ACCOUNT crypto | stocks
  AMOUNT  GBP to move (must be <= FROM's cash in that account)
  cash    all of FROM's cash in that account
  topup   whatever brings TO's slice of that account (cash + holdings) up to its baseline, limited by FROM's cash
Each move is recorded in table transfers (kind 'pot'; TRANSFER rows on the dashboard Orders tab). Pot and account
history is rebased by later transfers, so a transfer is not a gain/loss and does not trip drawdown limits; the DAY
pot's UK-day start equity is adjusted too so the DAY daily loss cap does not fire. Backs up the DB first.
After changing accounts.funding in config.yaml, run moves so each pot x account baseline matches."""
import sqlite3, sys
from datetime import datetime
from common import ACCOUNTS, DB, LON, SLEEVES, acct_of, acct_start, db, move_cash

def transfer(con, frm, to, amount, reason, account="crypto"):
    assert frm in SLEEVES and to in SLEEVES and frm != to and account in ACCOUNTS, "unknown/same sleeve or account"
    have = (con.execute("SELECT amount FROM acct_cash WHERE sleeve=? AND account=?", (frm, account)).fetchone() or [0.0])[0]
    if amount == "cash": amount = have
    elif amount == "topup":
        rate = (con.execute("SELECT rate FROM fx WHERE pair='USDGBP' ORDER BY ts DESC LIMIT 1").fetchone() or [1.0])[0]
        held = sum(r["qty"] * ((con.execute("SELECT price FROM prices WHERE asset=? AND source!='backfill' ORDER BY ts DESC LIMIT 1",
                   (r["asset"],)).fetchone() or [0])[0]) * rate
                   for r in con.execute("SELECT asset, klass, qty FROM positions WHERE sleeve=?", (to,)) if acct_of(r["klass"]) == account)
        eq_to = (con.execute("SELECT amount FROM acct_cash WHERE sleeve=? AND account=?", (to, account)).fetchone() or [0.0])[0] + held
        amount = min(have, max(0.0, acct_start(con)[to][account] - eq_to))
    amount = float(amount)
    if amount <= 0: print(f"nothing to move {frm}->{to} ({account})"); return 0.0
    move_cash(con, (frm, account), (to, account), amount, reason, "pot")
    print(f"TRANSFER £{amount:.5f} {account} cash {frm} -> {to} ({reason})")
    return amount

if __name__ == "__main__":
    if len(sys.argv) < 5 or sys.argv[4] not in ACCOUNTS: raise SystemExit(__doc__)
    bak = DB.with_name(DB.name + f".bak-transfer-{datetime.now(LON):%Y%m%d-%H%M%S}")
    s = sqlite3.connect(DB); d = sqlite3.connect(bak); s.backup(d); d.close(); s.close(); print("backup", bak)
    con = db()
    transfer(con, sys.argv[1], sys.argv[2], sys.argv[3] if sys.argv[3] in ("cash", "topup") else float(sys.argv[3]),
             " ".join(sys.argv[5:]) or "pot rebalance", sys.argv[4])
    con.commit()
    print({f"{r[0]}/{r[1]}": round(r[2], 4) for r in con.execute("SELECT sleeve, account, amount FROM acct_cash ORDER BY 1, 2")})
