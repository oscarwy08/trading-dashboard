"""Deterministic equivalence: origin/main trader.py vs the exits.py refactor.

test/sim.py cannot prove this. Its prices are a random walk and its clock starts
on a wall-clock boundary, so two runs never match. This test copies the current
main book, plants one fixed price path, and runs the same cycles through both
trader.py files. The shadow watcher is off. The trades columns (side, asset, qty,
fill, pnl, reason) and the final equity must be byte-identical.

The path is several UK days and is meant to fire, on assets that are not already
in the book: a day stop, a day trailing exit (40% giveback), a burst target, a
momentum runner that then trails at 20% of the peak gain, a burst max-hold, the
22:00 UK flatten, a Risk trail, and a Risk max-hold on a later day.
"""
import difflib, json, os, shutil, sqlite3, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEE = 0.003
# Sunday 11 Oct 2026 is still BST (UK = UTC+1). 22:00 UK is 21:00 UTC.
# The book snapshot is 20:27 UTC, so the first cycle is after that and still
# before the flatten. Later clocks are the next three UK afternoons.
CLOCKS = [
    datetime(2026, 10, 11, 20, 35, tzinfo=timezone.utc),
    datetime(2026, 10, 11, 20, 45, tzinfo=timezone.utc),
    datetime(2026, 10, 11, 20, 55, tzinfo=timezone.utc),
    datetime(2026, 10, 11, 21, 5, tzinfo=timezone.utc),   # 22:05 UK, flatten
    datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc),   # risk max-hold day
]
OPEN_SUN = "2026-10-11T12:00:00+00:00"
OPEN_RMAX = "2026-10-07T12:00:00+00:00"
# Names are outside the priced universe. day_excluded blocks a later re-entry.
DAY = ["EQSTOP", "EQTRAIL", "EQTARG", "EQRUN", "EQHOLD", "EQFLAT"]
RISK = ["EQRISK", "EQRMAX"]
NEEDLES = (
    "stop-loss",
    "gives back 40%",
    "take-profit",
    "gives back 20%",
    "max hold 6h",
    "flatten 22:00 UK",
    "trailing take-profit",
    "max hold 7 days",
)


def iso(t):
    return t.isoformat()


def gbp_for(net):
    """GBP price whose fee-net change from entry 1.0 is `net` (mcap unknown, fee 0.3%)."""
    return (1.0 + net) / (1.0 - FEE)


def add_px(con, ts, asset, usd):
    con.execute(
        "INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ts, asset, "crypto", usd, None, None, None, None, "test", ts))


def add_pos(con, sleeve, asset, opened):
    con.execute(
        "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
        (sleeve, asset, "crypto", 10.0, 1.0, 10.0, opened, 1))


def seed(db):
    con = sqlite3.connect(db)
    rate = con.execute(
        "SELECT rate FROM fx WHERE pair='USDGBP' AND rate>0 ORDER BY ts DESC LIMIT 1").fetchone()[0]

    def usd(net):
        return gbp_for(net) / rate

    flat = usd(0.0)
    for a in DAY + RISK:
        add_pos(con, "day" if a in DAY else "risk", a, OPEN_RMAX if a == "EQRMAX" else OPEN_SUN)
    dm = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0])
    dm.update({
        "EQSTOP": dict(sub="momentum", tp=12.0, sl=3.0, atr=1.0, peak=1.0, opened=OPEN_SUN),
        "EQTRAIL": dict(sub="momentum", tp=12.0, sl=5.0, atr=1.0, peak=1.0, opened=OPEN_SUN),
        "EQTARG": dict(sub="burst", tp=4.0, sl=2.0, atr=1.0, peak=1.0, opened=OPEN_SUN),
        "EQRUN": dict(sub="momentum", tp=4.0, sl=2.0, atr=1.0, peak=1.0, opened=OPEN_SUN),
        "EQHOLD": dict(sub="burst", tp=8.0, sl=2.0, atr=1.0, peak=1.0, opened=OPEN_SUN),
        "EQFLAT": dict(sub="momentum", tp=12.0, sl=5.0, atr=1.0, peak=1.0, opened=OPEN_SUN),
    })
    con.execute("INSERT OR REPLACE INTO state VALUES('day_meta',?)", (json.dumps(dm),))
    peaks = json.loads(con.execute("SELECT value FROM state WHERE key='risk_peaks'").fetchone()[0])
    peaks["EQRISK"] = 1.0
    peaks["EQRMAX"] = 1.0
    con.execute("INSERT OR REPLACE INTO state VALUES('risk_peaks',?)", (json.dumps(peaks),))
    excluded = json.loads(con.execute("SELECT value FROM state WHERE key='day_excluded'").fetchone()[0])
    for a in DAY + RISK:
        excluded[a] = "equivalence fixture, not a day entry"
    con.execute("INSERT OR REPLACE INTO state VALUES('day_excluded',?)", (json.dumps(excluded),))

    # EQRUN needs >= 4h of rising history so the target tick is still a buy (runner).
    t = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)
    while t <= end:
        add_px(con, iso(t), "EQRUN", usd(-0.08))
        t += timedelta(hours=1)

    px = {
        "EQSTOP": {CLOCKS[0]: usd(-0.035)},
        "EQTRAIL": {CLOCKS[0]: usd(0.06), CLOCKS[1]: usd(0.03)},
        "EQTARG": {CLOCKS[0]: usd(0.05)},
        "EQRUN": {CLOCKS[0]: usd(0.05), CLOCKS[1]: usd(0.10), CLOCKS[2]: usd(0.07)},
        "EQHOLD": {CLOCKS[0]: flat},
        "EQFLAT": {CLOCKS[0]: flat},
        "EQRISK": {CLOCKS[0]: usd(0.16), CLOCKS[1]: usd(0.16) * 0.89},
        "EQRMAX": {c: flat for c in CLOCKS},
    }
    for asset, series in px.items():
        for t, price in series.items():
            add_px(con, iso(t), asset, price)
    con.commit()
    con.close()


def snapshot(con, after_id):
    rows = con.execute(
        "SELECT id, side, asset, qty, fill, pnl, reason FROM trades ORDER BY id").fetchall()
    full, new = [], []
    for id_, side, asset, qty, fill, pnl, reason in rows:
        line = f"{side}\t{asset}\t{qty!r}\t{fill!r}\t{pnl!r}\t{reason}"
        full.append(line)
        if id_ > after_id:
            new.append(line)
    eq = con.execute(
        "SELECT equity, cash, pos_value FROM equity ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
    full.append(f"EQUITY\t{eq[0]!r}\t{eq[1]!r}\t{eq[2]!r}")
    return "\n".join(full), "\n".join(new)


def run_child(db_path):
    os.environ["TRADING_DB"] = db_path
    os.environ["TRADING_OFFLINE"] = "1"
    os.environ["EXIT_WATCH_SHADOW"] = "0"
    os.environ.pop("TRADING_VARIANT", None)
    import common
    import trader
    con = sqlite3.connect(db_path)
    after = con.execute("SELECT COALESCE(MAX(id),0) FROM trades").fetchone()[0]
    con.close()
    for t in CLOCKS:
        common._NOW = t
        trader.main()
    con = sqlite3.connect(db_path)
    full, new = snapshot(con, after)
    con.close()
    uses = "1" if "exits" in sys.modules else "0"
    print("TRADER_FILE " + str(Path(trader.__file__).resolve()))
    print("USES_EXITS " + uses)
    print("EQUIV_FULL")
    print(full)
    print("EQUIV_NEW")
    print(new)
    print("EQUIV_END")


def between(text, start, end):
    a = text.index(start) + len(start)
    b = text.index(end, a)
    return text[a:b].strip("\n")


def launch(which, db, tmp):
    env = os.environ.copy()
    env["TRADING_DB"] = str(db)
    env["TRADING_OFFLINE"] = "1"
    env["EXIT_WATCH_SHADOW"] = "0"
    env["TRADING_LOG"] = str(tmp / f"{which}.log")
    env.pop("TRADING_VARIANT", None)
    env.pop("PYTHONPATH", None)
    if which == "main":
        shadow = tmp / "origin_main"
        shadow.mkdir()
        src = subprocess.check_output(["git", "show", "origin/main:trader.py"], cwd=ROOT)
        (shadow / "trader.py").write_bytes(src)
        env["PYTHONPATH"] = str(shadow) + os.pathsep + str(ROOT)
    else:
        env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", str(db)],
        cwd=ROOT, env=env, capture_output=True, text=True)
    if proc.returncode != 0 or "EQUIV_END" not in proc.stdout:
        sys.stderr.write(proc.stdout[-4000:] + "\n" + proc.stderr[-4000:])
        raise SystemExit(f"{which} trader run failed (exit {proc.returncode})")
    return proc.stdout


def main():
    src = ROOT / "data" / "trading.db"
    if not src.exists():
        raise SystemExit(f"missing {src}")
    tmp = Path(tempfile.mkdtemp(prefix="exit-equiv-"))
    base = tmp / "base.db"
    with sqlite3.connect(src) as s, sqlite3.connect(base) as d:
        s.backup(d)
    seed(base)
    books = {}
    outs = {}
    for which in ("main", "pr"):
        db = tmp / f"{which}.db"
        shutil.copy(base, db)
        books[which] = db
        outs[which] = launch(which, db, tmp)
    files = {w: Path(outs[w].split("TRADER_FILE ", 1)[1].splitlines()[0]) for w in outs}
    uses = {w: outs[w].split("USES_EXITS ", 1)[1].splitlines()[0] for w in outs}
    full = {w: between(outs[w], "EQUIV_FULL", "EQUIV_NEW") for w in outs}
    new = {w: between(outs[w], "EQUIV_NEW", "EQUIV_END") for w in outs}
    pr_file = (ROOT / "trader.py").resolve()
    ok_files = files["pr"] == pr_file and files["main"] != pr_file
    ok_uses = uses["pr"] == "1" and uses["main"] == "0"
    same = full["main"] == full["pr"]
    missing = [n for n in NEEDLES if n not in new["pr"]]
    print(("OK  " if ok_files else "FAIL") + " ran origin/main trader.py and this branch's trader.py")
    print(("OK  " if ok_uses else "FAIL") + " shadow watcher off; only the PR trader imports exits.py")
    print(("OK  " if same else "FAIL") + " trades (side, asset, qty, fill, pnl, reason) and final equity are byte-identical")
    if missing:
        print("FAIL price path did not fire: " + ", ".join(missing))
    else:
        print("OK   path fired stop, 40% trail, target, 20% runner trail, 6h max-hold, 22:00 flatten, Risk trail, 7d max-hold")
    if not same:
        diff = list(difflib.unified_diff(
            full["main"].splitlines(), full["pr"].splitlines(),
            fromfile="origin/main", tofile="pr", lineterm=""))
        print("\n".join(diff[:80]))
    if not (ok_files and ok_uses and same and not missing):
        raise SystemExit(1)


if __name__ == "__main__":
    if "--child" in sys.argv:
        run_child(sys.argv[sys.argv.index("--child") + 1])
    else:
        main()
