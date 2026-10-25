"""Throttled export of the paper-book page files (dashboard.book_export).

Only docs/data_<book>.json is throttled. data.json is rebuilt every cycle, and a book is rebuilt at once
when it traded, a position moved or it was reset, or when its export reaches max_age_min."""
import json, os, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "main.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["TRADING_OFFLINE"] = "1"
os.environ["SITE_DIR"] = str(TMP / "site")
os.environ["VARIANTS_DIR"] = str(TMP / "variants")
os.environ["VARIANTS_MAIN_DB"] = str(TMP / "main.db")
os.environ.pop("TRADING_VARIANT", None)

import dashboard

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

P = dict(enabled=True, every_cycles=3, max_age_min=45)
T = 1_800_000_000
names = ["a", "b", "c", "d", "e", "f"]
present = set(names)
sigs = {n: "s" + n for n in names}
prev = {n: dict(exported=T - 600 - i, sig="s" + n) for i, n in enumerate(names)}

# --- plan_exports ---
check(dashboard.plan_exports(names, prev, sigs, T, dict(P, enabled=False), present) == set(names),
      "throttling off rebuilds every book")
check(dashboard.plan_exports(names, {}, sigs, T, P, present) == set(names), "no earlier export: every book")
got = dashboard.plan_exports(names, prev, sigs, T, P, present)
check(got == {"e", "f"}, f"unchanged books: only the ceil(6/3)=2 oldest exports ({sorted(got)})")
moved = dict(sigs, b="traded")
got = dashboard.plan_exports(names, prev, moved, T, P, present)
check(got == {"b", "e", "f"}, f"a book that traded is rebuilt besides the rotation ({sorted(got)})")
got = dashboard.plan_exports(names, prev, sigs, T, P, present - {"a"})
check("a" in got, "a missing file is rebuilt")
old = dict(prev, c=dict(exported=T - 45 * 60, sig="sc"))
got = dashboard.plan_exports(names, old, sigs, T, P, present)
check("c" in got and len(got) == 3, "an export max_age_min old is always rebuilt")
unknown = {n: dict(exported=T - 600, sig="?") for n in names}
got = dashboard.plan_exports(names, unknown, {n: "x" for n in names}, T, P, present)
check(len(got) == 2, "an unknown stored fingerprint does not force a rebuild (first cycle rotates)")
check(dashboard.plan_exports(names, prev, sigs, T, dict(P, every_cycles=1), present) == set(names),
      "every_cycles 1 rebuilds every book")

# Rotation: with nothing traded, every book is rebuilt within every_cycles cycles.
st = dict(prev); seen = {n: [] for n in names}
for cyc in range(6):
    now_ep = T + cyc * 600
    got = dashboard.plan_exports(names, st, sigs, now_ep, P, present)
    for n in got:
        st[n] = dict(exported=now_ep, sig=sigs[n]); seen[n].append(cyc)
gaps = [b - a for n in names for a, b in zip(seen[n], seen[n][1:])]
check(all(seen[n] for n in names) and max(gaps) <= 3, f"every book rebuilt at least every 3 cycles ({seen})")

# --- prev_exports ---
site = Path(os.environ["SITE_DIR"]); site.mkdir()
(site / "data.json").write_text(json.dumps({"generated": T, "books": [
    {"name": "main", "file": "data.json"}, {"name": "a", "label": "A", "file": "data_a.json"}]}))
pe = dashboard.prev_exports(site)
check(pe["a"]["exported"] == T and pe["a"]["sig"] == "?", "a catalog from before throttling counts as exported then, fingerprint unknown")
(site / "data.json").write_text("not json")
check(dashboard.prev_exports(site) == {}, "an unreadable data.json means no earlier exports")
(site / "data.json").unlink()

# --- book_sig ---
db = TMP / "sig.db"
con = sqlite3.connect(db)
con.executescript("""CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT);
CREATE TABLE IF NOT EXISTS positions(sleeve TEXT, asset TEXT, qty REAL, cost REAL);
CREATE TABLE IF NOT EXISTS transfers(ts TEXT); CREATE TABLE IF NOT EXISTS loans(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS equity(ts TEXT, equity REAL);""")
s0 = dashboard.book_sig(con)
con.execute("INSERT INTO equity VALUES('t', 1)")
check(dashboard.book_sig(con) == s0, "a new equity mark does not change the fingerprint")
con.execute("INSERT INTO trades(ts) VALUES('t')")
s1 = dashboard.book_sig(con)
check(s1 != s0, "a trade changes the fingerprint")
con.execute("INSERT INTO positions VALUES('day','BTC',1,10)")
check(dashboard.book_sig(con) != s1, "a position changes the fingerprint")
con.close()
check(isinstance(dashboard.book_sig(sqlite3.connect(":memory:")), str), "a database with no tables still has a fingerprint")

# --- main(): two cycles end to end ---
root = Path(os.environ["VARIANTS_DIR"]); root.mkdir()
run = ["ev_exit", "burst_runner", "everything", "midnight_crypto", "entry_watch", "partial_take"]
for n in run:
    c = sqlite3.connect(root / f"{n}.db")
    c.executescript("CREATE TABLE trades(id INTEGER PRIMARY KEY, ts TEXT); CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT);")
    c.commit(); c.close()
real_cfg = dashboard.cfg
mode = {"P": dict(P)}
def fake_cfg():
    c = dict(real_cfg())
    c["variants"] = dict(c.get("variants") or {}, run=run)
    c["dashboard"] = {"book_export": mode["P"]}
    return c
clock = {"t": datetime.fromtimestamp(T, timezone.utc)}
built = []
def fake_build(con, variant_report=None, strategies=None, engine=None):
    path = con.execute("PRAGMA database_list").fetchone()[2] or ""
    built.append(Path(path).stem)
    return {"generated": int(clock["t"].timestamp()), "updated": int(clock["t"].timestamp())}
dashboard.cfg = fake_cfg
dashboard.now = lambda: clock["t"]
dashboard.build = fake_build
dashboard._variant_report = lambda: {"enabled": True, "books": []}
dashboard._strategy_rows = lambda report=None: []
dashboard._engine_view = lambda con: {}

dashboard.main()
check(sorted(built) == sorted(["main"] + run), "first cycle (no earlier export) rebuilds every book")
cat = json.loads((site / "data.json").read_text())["books"]
check([r["name"] for r in cat] == ["main"] + run and all(r.get("exported") == T for r in cat),
      "the catalog stamps each file with its export time")
ex = json.loads((site / "data_ev_exit.json").read_text())
check(ex.get("export") == {"every_cycles": 3, "max_age_min": 45}, "a rebuilt book file says how often it is rebuilt")
mt = {n: (site / f"data_{n}.json").read_bytes() for n in run}

built.clear(); clock["t"] += timedelta(minutes=9)
c = sqlite3.connect(root / "entry_watch.db"); c.execute("INSERT INTO trades(ts) VALUES('x')"); c.commit(); c.close()
dashboard.main()
rebuilt = [b for b in built if b != "main"]
check("main" in built, "data.json is rebuilt every cycle")
check("entry_watch" in rebuilt and len(rebuilt) == 3, f"second cycle: the book that traded plus 2 in rotation ({rebuilt})")
cat2 = {r["name"]: r for r in json.loads((site / "data.json").read_text())["books"]}
check([r for r in cat2] == ["main"] + run, "kept books stay in the catalog")
kept = [n for n in run if n not in rebuilt]
check(all((site / f"data_{n}.json").read_bytes() == mt[n] for n in kept), "a kept book's file is not rewritten")
check(all(cat2[n]["exported"] == T for n in kept) and all(cat2[n]["exported"] == T + 540 for n in rebuilt),
      "the catalog keeps each file's own export time")

built.clear(); clock["t"] += timedelta(minutes=50)
dashboard.main()
check(sorted(b for b in built if b != "main") == sorted(run), "every export older than max_age_min is rebuilt")

mode["P"] = dict(P, enabled=False); built.clear(); clock["t"] += timedelta(minutes=1)
dashboard.main()
check(sorted(built) == sorted(["main"] + run), "enabled: false rebuilds every book every cycle")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
