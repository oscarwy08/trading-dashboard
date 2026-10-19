"""Archive Test 1 once, then start Test 2 at £200 on every book. Trading rules are not changed.

The first cycle after this is deployed:

1. Writes data/tests/test1/ from the books on disk (main and every variant database).
   The window is 11 Oct 2026 18:07 UK through the reset. The files are not rewritten later.
2. Clears the main book's ledger and funds it again at £100 crypto and £100 stocks, the
   same split as config. Prices, daily closes, sentiment and FX stay.
3. Replaces the variant snapshot with that flat book and deletes the old variant databases
   so the next variant run recopies them at £200. The seven strategy books are created
   flat by that run, not from the old history.
4. Stores the BTC and VUSA prices at the reset as the benchmark, and a generation file.
   A later cycle sees the file and does nothing.

Usage: python3 testreset.py
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from common import ACCOUNTS, DATA, DB, SLEEVES, cfg, db, funding, iso, sync_pot_cash, usd_gbp

# 11 Oct 2026 18:07 Europe/London is BST (UTC+1).
TEST1_START = "2026-10-11T17:07:00+00:00"
TEST_DIR = Path(os.environ.get("TESTS_DIR") or DATA / "tests")
GEN_FILE = TEST_DIR / "generation.json"
TEST1_DIR = TEST_DIR / "test1"


def generation():
    if not GEN_FILE.exists():
        return None
    try:
        return json.loads(GEN_FILE.read_text())
    except Exception:
        return None


def already_reset():
    gen = generation()
    if gen and gen.get("id") == "test2":
        return True
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = con.execute("SELECT value FROM state WHERE key='test_generation'").fetchone()
        con.close()
        if row and row[0] and json.loads(row[0]).get("id") == "test2":
            if not gen:
                GEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                GEN_FILE.write_text(row[0])
            return True
    except Exception:
        return False
    return False


def _book_paths():
    import variants
    paths = [("main", Path(DB))]
    root = variants.dir_path()
    if root.exists():
        for name in (cfg().get("variants") or {}).get("run") or []:
            path = root / f"{name}.db"
            if path.exists():
                paths.append((name, path))
    return paths


def _curve(con, start, book_start=None):
    from dashboard import partial_equity_point
    rows = con.execute("SELECT ts, equity, btc FROM equity WHERE ts>=? ORDER BY ts", (start,)).fetchall()
    rows = [r for r in rows if not partial_equity_point(r[0], r[1], [(x[0], x[1]) for x in rows], book_start)]
    out = []
    for ts, eq, btc in rows:
        try:
            t = int(datetime.fromisoformat(ts).timestamp())
        except Exception:
            continue
        out.append(dict(t=t, eq=round(float(eq), 2) if eq is not None else None,
                        btc=None if btc is None else round(float(btc), 4)))
    if len(out) > 400:
        step = (len(out) - 1) / 399
        out = [out[min(int(i * step), len(out) - 1)] for i in range(400)]
    return out


def _stats(con, start, end):
    eq = con.execute("SELECT equity, btc FROM equity WHERE ts>=? AND ts<=? ORDER BY ts", (start, end)).fetchall()
    from dashboard import curve_drawdown, partial_equity_point
    start_eq = 200.0
    raw = [(r[0], r[1]) for r in con.execute(
        "SELECT ts, equity FROM equity WHERE ts>=? AND ts<=? ORDER BY ts", (start, end)) if r[1] is not None]
    kept = [(ts, v) for ts, v in raw if not partial_equity_point(ts, v, raw, start_eq)]
    end_eq = float(kept[-1][1]) if kept else (float(eq[-1][0]) if eq and eq[-1][0] else start_eq)
    ret = end_eq / start_eq - 1 if start_eq else None
    mdd = curve_drawdown(kept, start_eq)
    btc_ret = None
    if eq and eq[0][1] and eq[-1][1]:
        btc_ret = float(eq[-1][1]) / float(eq[0][1]) - 1
    sells = [float(r[0] or 0) for r in con.execute(
        "SELECT pnl FROM trades WHERE side='SELL' AND ts>=? AND ts<=?", (start, end))]
    wins = sum(x for x in sells if x > 0)
    losses = sum(x for x in sells if x < 0)
    if not sells:
        pf = None
    elif losses == 0:
        pf = None if wins == 0 else 99.0
    else:
        pf = wins / abs(losses)
    fees = con.execute(
        "SELECT COALESCE(SUM(cost),0)+COALESCE(SUM(gas),0) FROM trades WHERE ts>=? AND ts<=?", (start, end)).fetchone()[0]
    n = con.execute(
        "SELECT COUNT(*) FROM trades WHERE side IN ('BUY','SELL') AND ts>=? AND ts<=?", (start, end)).fetchone()[0]
    holdings = [dict(sleeve=r[0], asset=r[1], qty=r[2]) for r in con.execute(
        "SELECT sleeve, asset, qty FROM positions ORDER BY asset")]
    trades = []
    for r in con.execute("""SELECT ts, sleeve, asset, klass, side, qty, price, value, cost, gas, pnl, reason
                            FROM trades WHERE ts>=? AND ts<=? ORDER BY id""", (start, end)):
        trades.append(dict(ts=r[0], sleeve=r[1], asset=r[2], klass=r[3], side=r[4], qty=r[5],
                           price=r[6], value=r[7], cost=r[8], gas=r[9], pnl=r[10], reason=r[11]))
    vusa = None
    for name in ("VUSA", "VUSA.L", "EQQQ", "EQQQ.L"):
        closes = con.execute("SELECT close FROM daily_closes WHERE asset=? AND close>0 ORDER BY d", (name,)).fetchall()
        if len(closes) >= 2 and closes[0][0]:
            vusa = closes[-1][0] / closes[0][0] - 1
            break
    return dict(
        equity=round(end_eq, 2), start=round(start_eq, 2),
        ret=None if ret is None else round(ret, 6),
        excess_btc=None if ret is None or btc_ret is None else round(ret - btc_ret, 6),
        excess_vusa=None if ret is None or vusa is None else round(ret - vusa, 6),
        max_dd=round(mdd, 6), trades=n,
        pf=None if pf is None else round(pf, 4),
        fees=round(float(fees or 0), 4),
        holdings=holdings, trade_rows=trades, btc_ret=None if btc_ret is None else round(btc_ret, 6),
    )


def archive_test1(end_ts):
    """Write the immutable Test 1 archive. A second call does not touch the files."""
    if (TEST1_DIR / "manifest.json").exists():
        return False
    TEST1_DIR.mkdir(parents=True, exist_ok=True)
    books = []
    import variants
    for name, path in _book_paths():
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            st = _stats(con, TEST1_START, end_ts)
            curve = _curve(con, TEST1_START, st["start"])
        finally:
            con.close()
        label = "Main book" if name == "main" else variants.LABELS.get(name, name)
        payload = dict(name=name, label=label, **st, curve=curve)
        (TEST1_DIR / f"{name}.json").write_text(json.dumps(payload))
        books.append({k: payload[k] for k in ("name", "label", "equity", "start", "ret", "excess_btc", "excess_vusa",
                                               "max_dd", "trades", "pf", "fees")})
    manifest = dict(id="test1", name="Test 1", started=TEST1_START, ended=end_ts, frozen=True,
                    note="11 Oct 2026 18:07 UK through the Test 2 reset. This directory is not rewritten.",
                    books=books)
    (TEST1_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return True


def _ensure_tests_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS tests(
        id TEXT PRIMARY KEY, name TEXT, started TEXT, ended TEXT, frozen INTEGER, note TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS test_books(
        test_id TEXT, book TEXT, equity REAL, ret REAL, excess_btc REAL, excess_vusa REAL,
        max_dd REAL, trades INTEGER, pf REAL, fees REAL, PRIMARY KEY(test_id, book))""")


def _record_test(con, manifest):
    _ensure_tests_table(con)
    con.execute("INSERT OR REPLACE INTO tests VALUES(?,?,?,?,?,?)",
                (manifest["id"], manifest["name"], manifest["started"], manifest.get("ended"),
                 1 if manifest.get("frozen") else 0, manifest.get("note")))
    for book in manifest.get("books") or []:
        con.execute("INSERT OR REPLACE INTO test_books VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (manifest["id"], book["name"], book.get("equity"), book.get("ret"), book.get("excess_btc"),
                     book.get("excess_vusa"), book.get("max_dd"), book.get("trades"), book.get("pf"), book.get("fees")))


def reset_main(ts, btc_gbp, vusa_gbp):
    """Clear the ledger and fund £200 again. Market data stays. Rules stay."""
    from trader import init_core
    c = cfg()
    con = db()
    try:
        for table in ("trades", "positions", "cash", "acct_cash", "equity", "signals", "transfers", "loans", "armed_entries"):
            try:
                con.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
        con.execute("""DELETE FROM state WHERE key IN (
            'risk','day_meta','day_subs','order_queue','account_split','account_reset',
            'risk_peaks','risk_meta','riskpot_base','riskpot_halt','riskpot_status','variant_reset')
            OR key LIKE 'core_%' OR key LIKE 'day_start:%' OR key LIKE 'day_halt:%'
            OR key LIKE 'day_exit:%' OR key LIKE 'risk_exit:%'""")
        fund = funding(c)
        con.executemany("INSERT INTO acct_cash VALUES(?,?,?)",
                        [(k, ac, v) for k, dd in fund.items() for ac, v in dd.items()])
        sync_pot_cash(con)
        con.execute("INSERT OR REPLACE INTO state VALUES('account_split',?)", (json.dumps(dict(
            ts=ts, opening=fund, targets=fund, pending=[],
            note="Test 2: fresh £200, same crypto/stock split, same rules")),))
        init_core(con, c)
        fx = usd_gbp(con)
        rate = fx[0] if fx else None
        total = sum(sum(v.values()) for v in fund.values())
        pot = {s: sum(fund[s].values()) for s in SLEEVES}
        acc = {ac: sum(fund[s][ac] for s in SLEEVES) for ac in ACCOUNTS}
        ae = {s: {ac: [fund[s][ac], fund[s][ac]] for ac in ACCOUNTS} for s in SLEEVES}
        con.execute("""INSERT INTO equity(ts,equity,cash,pos_value,btc,open_n,usd_gbp,core,day,core_cash,day_cash,
                       risk,risk_cash,crypto,stocks,crypto_cash,stocks_cash,accts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, total, total, 0.0, btc_gbp, 0, rate, pot["core"], pot["day"], pot["core"], pot["day"],
                     pot["risk"], pot["risk"], acc["crypto"], acc["stocks"], acc["crypto"], acc["stocks"],
                     json.dumps(ae, separators=(",", ":"))))
        gen = dict(id="test2", name="Test 2", ts=ts, btc_gbp=btc_gbp, vusa_gbp=vusa_gbp,
                   note="All books restarted at £200. Rules unchanged.")
        con.execute("INSERT OR REPLACE INTO state VALUES('test_generation',?)", (json.dumps(gen),))
        con.execute("INSERT OR REPLACE INTO state VALUES('test_benchmark',?)", (json.dumps(
            dict(ts=ts, btc_gbp=btc_gbp, vusa_gbp=vusa_gbp)),))
        con.execute("INSERT OR REPLACE INTO state VALUES('account_reset',?)",
                    (f"{ts} TEST 2 GBP {total:.0f} (Crypto £{acc['crypto']:.0f} / Stocks £{acc['stocks']:.0f}; rules unchanged)",))
        manifest = json.loads((TEST1_DIR / "manifest.json").read_text()) if (TEST1_DIR / "manifest.json").exists() else None
        if manifest:
            _record_test(con, manifest)
        _record_test(con, dict(id="test2", name="Test 2", started=ts, ended=None, frozen=False,
                               note="Live test. Same rules, fresh £200, fresh BTC and VUSA baseline.", books=[]))
        con.commit()
    finally:
        con.close()


def _benchmarks():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        fx = con.execute("SELECT rate FROM fx WHERE pair='USDGBP' ORDER BY ts DESC LIMIT 1").fetchone()
        rate = float(fx[0]) if fx and fx[0] else None
        btc = con.execute("""SELECT price FROM prices WHERE asset='BTC' AND source NOT IN ('backfill','kyberswap')
                             AND price>0 ORDER BY ts DESC LIMIT 1""").fetchone()
        btc_gbp = float(btc[0]) * rate if btc and rate else None
        vusa = None
        for name in ("VUSA", "VUSA.L", "EQQQ", "EQQQ.L"):
            row = con.execute("SELECT close FROM daily_closes WHERE asset=? AND close>0 ORDER BY d DESC LIMIT 1", (name,)).fetchone()
            if row:
                vusa = float(row[0]) * (rate or 1)
                break
    finally:
        con.close()
    return btc_gbp, vusa


def refresh_snapshot():
    """The variant snapshot becomes the flat £200 book, and old variant files are removed."""
    import variants
    root = variants.dir_path()
    root.mkdir(parents=True, exist_ok=True)
    for path in root.glob("*.db"):
        path.unlink()
    snap = root / "_snapshot.db"
    variants._backup(DB, snap)
    man = dict(created=iso(), source=str(DB), equity=200.0, equity_ts=iso(), test="test2")
    (root / "manifest.json").write_text(json.dumps(man, indent=2))


def run():
    if already_reset():
        print("[test] Test 2 already started; not resetting again")
        return 0
    ts = iso()
    wrote = archive_test1(ts)
    btc, vusa = _benchmarks()
    reset_main(ts, btc, vusa)
    refresh_snapshot()
    gen = dict(id="test2", name="Test 2", started=ts, btc_gbp=btc, vusa_gbp=vusa, archived=bool(wrote))
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    GEN_FILE.write_text(json.dumps(gen, indent=2))
    print(f"[test] Test 1 archived ({'written' if wrote else 'already on disk'}); Test 2 starts at {ts}; "
          f"BTC benchmark {None if btc is None else round(btc, 2)} GBP; VUSA {None if vusa is None else round(vusa, 2)}")
    return 0


def tests_view():
    """Archived tests plus the live one, for the dashboard. Reads the files, not the live ledger alone."""
    archived = []
    if (TEST1_DIR / "manifest.json").exists():
        try:
            man = json.loads((TEST1_DIR / "manifest.json").read_text())
            books = []
            for item in man.get("books") or []:
                path = TEST1_DIR / f"{item['name']}.json"
                if path.exists():
                    full = json.loads(path.read_text())
                    full.pop("trade_rows", None)
                    books.append(full)
                else:
                    books.append(item)
            man["books"] = books
            archived.append(man)
        except Exception as e:
            archived.append(dict(id="test1", name="Test 1", error=str(e), books=[]))
    live = None
    gen = generation()
    if gen and gen.get("id") == "test2":
        live = dict(id="test2", name="Test 2", started=gen.get("started"), frozen=False,
                    btc_gbp=gen.get("btc_gbp"), vusa_gbp=gen.get("vusa_gbp"),
                    note="Live. Every book restarted at £200 with the rules unchanged.",
                    books=[])
    return dict(archived=archived, live=live)


if __name__ == "__main__":
    raise SystemExit(run())
