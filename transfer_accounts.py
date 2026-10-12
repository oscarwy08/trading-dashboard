"""MANUAL transfer between the two accounts (v6) - the paper version of moving real money between the Trading 212 Stocks
account and the Monad wallet (Crypto account). The trader NEVER does this on its own; run it only after you actually
moved (or decided to move) money in real life. No fees are simulated for the move itself (record any real cost by moving
the net amount). Recorded in table transfers (kind 'account', TRANSFER rows on the Orders tab); account and pot history
are rebased, so it is not P&L, and each account's baseline (start) changes by the amount moved.
Usage: python3 transfer_accounts.py FROM_ACCOUNT TO_ACCOUNT AMOUNT|cash POT [TO_POT] [-- reason]
  e.g.  python3 transfer_accounts.py stocks crypto 10 core -- "moved £10 from Trading 212 to the wallet"
        python3 transfer_accounts.py crypto stocks cash risk day      (all RISK crypto cash -> DAY pot in the Stocks account)
Only uninvested cash can move (sell first if needed). Backs up the DB first. In the repo setup: run on the box clone under
the repo lock, then commit + push data/trading.db (see README v6)."""
import sqlite3, sys
from datetime import datetime
from common import ACCOUNTS, DB, LON, SLEEVES, db, move_cash

def transfer(con, frm_acct, to_acct, amount, pot, to_pot=None, reason="manual account transfer"):
    to_pot = to_pot or pot
    assert frm_acct in ACCOUNTS and to_acct in ACCOUNTS and frm_acct != to_acct, "accounts must be crypto/stocks and differ"
    assert pot in SLEEVES and to_pot in SLEEVES, "pot must be core/day/risk"
    have = (con.execute("SELECT amount FROM acct_cash WHERE sleeve=? AND account=?", (pot, frm_acct)).fetchone() or [0.0])[0]
    amount = have if amount == "cash" else float(amount)
    if amount <= 0: print("nothing to move"); return 0.0
    move_cash(con, (pot, frm_acct), (to_pot, to_acct), amount, reason, "account")
    print(f"ACCOUNT TRANSFER £{amount:.2f}: {pot}/{frm_acct} -> {to_pot}/{to_acct} ({reason})")
    return amount

if __name__ == "__main__":
    args = sys.argv[1:]; reason = "manual account transfer"
    if "--" in args: i = args.index("--"); reason = " ".join(args[i + 1:]) or reason; args = args[:i]
    if len(args) < 4: raise SystemExit(__doc__)
    bak = DB.with_name(DB.name + f".bak-acct-transfer-{datetime.now(LON):%Y%m%d-%H%M%S}")
    s = sqlite3.connect(DB); d = sqlite3.connect(bak); s.backup(d); d.close(); s.close(); print("backup", bak)
    con = db()
    transfer(con, args[0], args[1], args[2] if args[2] == "cash" else float(args[2]), args[3], args[4] if len(args) > 4 else None, reason)
    con.commit()
    print({f"{r[0]}/{r[1]}": round(r[2], 4) for r in con.execute("SELECT sleeve, account, amount FROM acct_cash ORDER BY 1, 2")})
