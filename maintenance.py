"""Keep data/trading.db small (it is committed to the repo every cycle). Runs every cycle, prunes at most once per
UK day (state 'last_prune'), then VACUUMs:
  prices   : 15-min rows kept for 3 days, then thinned to the last row per asset per hour, deleted after 30 days
             (the dashboard shows 14 days, the trader needs 72 h, RISK holds up to 7 days)
  signals  : HOLD/SKIP rows kept 2 days at full resolution, then one per asset/sleeve/hour up to 7 days, then deleted;
             BUY/SELL/HALT rows kept 90 days
  state    : stale per-day keys (day_start:*, day_halt:*, risk_exit:*) older than 14 days
Trades, positions, cash, equity, transfers, sentiment, fx and daily closes are never pruned.
Usage: python3 maintenance.py [--force]"""
import sys, time
from datetime import timedelta
from common import LON, db, iso, now

def prune(con):
    t = now(); n = {}
    d3, d7, d30, d2, d90 = (iso(t - timedelta(days=d)) for d in (3, 7, 30, 2, 90))
    n["prices_old"] = con.execute("DELETE FROM prices WHERE ts<?", (d30,)).rowcount
    n["prices_thin"] = con.execute("""DELETE FROM prices WHERE ts<? AND rowid NOT IN (
        SELECT MAX(rowid) FROM prices WHERE ts<? GROUP BY asset, source, substr(ts,1,13))""", (d3, d3)).rowcount
    n["signals_old"] = con.execute("DELETE FROM signals WHERE (action IN ('HOLD','SKIP') AND ts<?) OR ts<?", (d7, d90)).rowcount
    n["signals_thin"] = con.execute("""DELETE FROM signals WHERE ts<? AND action IN ('HOLD','SKIP') AND rowid NOT IN (
        SELECT MAX(rowid) FROM signals WHERE ts<? AND action IN ('HOLD','SKIP') GROUP BY sleeve, asset, substr(ts,1,13))""", (d2, d2)).rowcount
    cut = (t - timedelta(days=14)).astimezone(LON).date().isoformat()
    old = [r[0] for r in con.execute("SELECT key FROM state WHERE key LIKE 'day_start:%' OR key LIKE 'day_halt:%'") if r[0][-10:] < cut]
    old += [r[0] for r in con.execute("SELECT key, value FROM state WHERE key LIKE 'risk_exit:%'") if r[1].strip('"') < iso(t - timedelta(days=14))]
    for k in old: con.execute("DELETE FROM state WHERE key=?", (k,))
    n["state"] = len(old)
    return n

def main():
    t0 = time.perf_counter()
    con = db(); day = now().astimezone(LON).date().isoformat()
    last = (con.execute("SELECT value FROM state WHERE key='last_prune'").fetchone() or [""])[0]
    if last == day and "--force" not in sys.argv:
        print(f"[timing] maintenance skip {time.perf_counter() - t0:.1f}s")
        return
    n = prune(con)
    con.execute("INSERT OR REPLACE INTO state VALUES('last_prune',?)", (day,)); con.commit()
    con.execute("VACUUM"); con.close()
    print("[maintenance] pruned", n, f"in {time.perf_counter() - t0:.1f}s")

if __name__ == "__main__":
    main()
