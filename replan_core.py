"""One-off: ask the trader to run the CORE rebalance at its next cycle (off-schedule re-pick), e.g. after changing
core targets / the stock universe in config.yaml. Backs up the DB first. Does NOT reset the account: positions, cash and
history stay; the next trader cycle re-picks the stocks, sells dropped picks, trims built assets beyond the drift band
and marks new picks for tranche building - exactly like the Monday rebalance.
Usage: python3 replan_core.py "reason" [backup-name]"""
import json, sqlite3, sys
from common import DB, db, iso
reason = sys.argv[1] if len(sys.argv) > 1 else "manual re-plan"
backup = DB.parent / (sys.argv[2] if len(sys.argv) > 2 else f"trading.db.bak-replan-{iso()[:16].replace(':', '')}")
if backup.exists(): sys.exit(f"backup {backup} already exists - refusing to overwrite")
src = sqlite3.connect(DB); dst = sqlite3.connect(backup); src.backup(dst); dst.close(); src.close()
con = db(); con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", ("core_force_rebalance", json.dumps(f"{iso()} {reason}"))); con.commit()
print("backup ->", backup, "| core_force_rebalance set:", reason)
