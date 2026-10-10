"""One-off: reset the paper account to a fresh £200 with CORE/DAY/RISK sleeves (split from config sleeve_split_pct).
Backs up the DB first (sqlite online backup), keeps prices / sentiment / fx / collector state, wipes account tables."""
import sqlite3, sys
from common import DB, cfg, db, iso, start_cash
from trader import init_core

backup = DB.parent / (sys.argv[1] if len(sys.argv) > 1 else "trading.db.bak-pre-sleeves")
if backup.exists(): sys.exit(f"backup {backup} already exists - refusing to overwrite")
src = sqlite3.connect(DB); dst = sqlite3.connect(backup); src.backup(dst); dst.close(); src.close()
print("backup ->", backup)
con = sqlite3.connect(DB)
for t in ("trades", "positions", "cash", "equity", "signals", "transfers"): con.execute(f"DROP TABLE IF EXISTS {t}")
con.execute("""DELETE FROM state WHERE key='risk' OR key LIKE 'day_start:%' OR key LIKE 'day_halt:%' OR key LIKE 'core_%'
               OR key LIKE 'risk_peaks' OR key LIKE 'riskpot_%' OR key LIKE 'risk_exit:%'""")
con.commit(); con.close()
con = db(); c = cfg()
con.executemany("INSERT INTO cash VALUES(?,?)", list(start_cash(c).items()))
picks = init_core(con, c)
con.execute("INSERT OR REPLACE INTO state VALUES('account_reset',?)",
            (f"{iso()} GBP {c['starting_balance']} sleeves {c['sleeve_split_pct']} (backup data/{backup.name})",))
con.commit()
print("cash:", dict(con.execute("SELECT sleeve, amount FROM cash").fetchall()), "| core stock picks:", picks)
print("kept rows: prices", con.execute("SELECT COUNT(*) FROM prices").fetchone()[0], "sentiment",
      con.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0], "fx", con.execute("SELECT COUNT(*) FROM fx").fetchone()[0])
