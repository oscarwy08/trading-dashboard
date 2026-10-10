"""RISK dip / pullback buy (v7.7).
Pure series: a rise, a pullback and a bounce is a dip; a series still making new lows is not; a steady rise is not a dip
at all (so the loan test's monotonic TESTX stays a breakout). One trader cycle on a copy of the live DB: the bounce is
bought, the falling knife is skipped.
Usage: python3 test/test_dip.py   (writes only to a temp DB)."""
import json, os, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp()); DBF = TMP / "dip.db"
os.environ["TRADING_DB"] = str(DBF); os.environ["TRADING_LOG"] = str(TMP / "dip.log")
sys.path.insert(0, str(HERE))
src = sqlite3.connect(HERE / "data" / "trading.db"); dst = sqlite3.connect(DBF); src.backup(dst); dst.close(); src.close()
import common, trader, riskpot, daypot
fails = []
def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond: fails.append(what)

def series(end, spec):
    """(hours_before_end, price) waypoints, filled every 15 minutes. Oldest waypoint first or last, either is fine."""
    spec = sorted(spec, key=lambda z: -z[0])
    start, steps = spec[0][0], int(round(spec[0][0] * 4))
    pts = []
    for i in range(steps + 1):
        h = start - i * 0.25
        price = spec[-1][1]
        for (h0, p0), (h1, p1) in zip(spec, spec[1:]):
            if h1 - 1e-9 <= h <= h0 + 1e-9:
                w = 0 if h0 == h1 else (h0 - h) / (h0 - h1)
                price = p0 + (p1 - p0) * w
                break
        pts.append((end - timedelta(hours=h), price))
    return pts

R = common.cfg()["riskpot"]
END = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
bounce = series(END, [(48, 1.00), (18, 1.16), (1.5, 1.078), (0, 1.097)])
knife = series(END, [(48, 1.00), (30, 1.14), (0, 1.02)])
deep = series(END, [(48, 1.00), (24, 1.20), (2, 0.90), (0, 0.93)])
steady = series(END, [(48, 1.00), (0, 1.00 * (1.001 ** 192))])
b, k, d, s = (riskpot.dip(x, R, regime=0.6, hr=0.5, freshness=1) for x in (bounce, knife, deep, steady))
print("bounce:", b["ok"], b["why"], "conf", b["conf"])
print("knife:", k["ok"], k["why"])
print("deep:", d["ok"], d["why"])
print("steady show:", s["show"])
check(b["show"] and b["ok"] and b["trend_ok"] and "turned up" in b["why"] and b["conf"] >= R["conf_threshold"], "dip-and-bounce qualifies, confidence at least the base threshold")
check(b["parts"] and set(R["dip"]["weights"]) <= set(b["parts"]) | {"freshness"} and "freshness" in b["parts"], "dip confidence has a bar for every weight, including freshness")
check(k["show"] and not k["ok"] and "still falling" in k["why"].lower(), "falling knife is shown and rejected")
check(d["show"] and not d["ok"] and "too deep" in d["why"].lower(), "a drop past the maximum is not a dip")
check(not s["show"], "a steady rise is not a dip")
# freshness 1 leaves the score unchanged; 0 scales it down
c1, _ = riskpot.dip_confidence(R, 0.8, 0.8, 0.8, 0.2, 0.6, 0.5, freshness=1)
c0, p0 = riskpot.dip_confidence(R, 0.8, 0.8, 0.8, 0.2, 0.6, 0.5, freshness=0)
check(c0 < c1 and p0["freshness"] == 0, "a dead quote scales dip confidence down; a fresh one does not")
mid = daypot.ev_now(0.6, 1.0, 1.10, 0.90, 0.5)
check(mid == round(0.6 * 10 - 0.4 * 10 - 0.5, 3), f"EV now halfway between stop and target is {mid}")
above = daypot.ev_now(0.6, 1.2, 1.10, 0.90, 0.5)
check(above == round(0 - 0.4 * (1 - 0.90 / 1.2) * 100 - 0.5, 3), f"EV now has no upside once price is through the target ({above})")
below = daypot.ev_now(0.6, 0.8, 1.10, 0.90, 0.5)
check(below == round(0.6 * (1.10 / 0.8 - 1) * 100 - 0.5, 3), f"EV now has no downside once price is through the stop ({below})")
check(daypot.ev_now(None, 1, 1.1, 0.9, 0) is None, "EV now needs a probability")

print("trader cycle: bounce bought, knife skipped")
common._NOW = END
con = common.db()
c = json.loads(json.dumps(common.cfg())); c["riskpot"]["stocks"] = []; c["riskpot"]["stock_themes"] = []; trader.cfg = lambda: c
con.execute("DELETE FROM positions WHERE sleeve='risk'")
con.execute("DELETE FROM state WHERE key='riskpot_halt' OR key LIKE 'risk_exit:%'")
con.execute("INSERT OR REPLACE INTO state VALUES('riskpot_base',?)", (json.dumps(1.0),))
con.execute("UPDATE acct_cash SET amount=? WHERE sleeve='risk' AND account='crypto'", (25.0,))
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (END.isoformat(), "USDGBP", 0.75, "sim", None, None))
for name, spec in (("DIPX", bounce), ("KNIFEX", knife)):
    con.execute("DELETE FROM prices WHERE asset=?", (name,))
    for ts, px in spec:
        # Deep book (mcap $200bn). A $100m cap is a thin name under the liquidity cost and fails the EV hurdle on purpose.
        con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (ts.isoformat(), name, "crypto", px, None, None, 5e10, 2e11, "sim", ts.isoformat()))
con.execute("INSERT OR REPLACE INTO state VALUES('risk_screen',?)", (json.dumps(dict(ts=END.isoformat(), top=[
    dict(a="DIPX", id="dipx", score=0.1, ok=False, why="faded from the high | no: 24h < +4%, faded > 8% from 24h high"),
    dict(a="KNIFEX", id="knifex", score=-0.4, ok=False, why="still falling | no: 24h < +4%, dropping (1h < -2%)")])),))
con.execute("INSERT OR REPLACE INTO state VALUES('risk_only',?)", (json.dumps(["DIPX", "KNIFEX"]),))
con.commit()
import contextlib, io
buf = io.StringIO()
with contextlib.redirect_stdout(buf): trader.main()
out = buf.getvalue(); print(out.strip()[:1500])
rst = json.loads(con.execute("SELECT value FROM state WHERE key='riskpot_status'").fetchone()[0])
by = {x["a"]: x for x in rst.get("cands") or []}
buy = con.execute("SELECT sub, conf, reason, value FROM trades WHERE asset='DIPX' AND side='BUY'").fetchone()
knife_buy = con.execute("SELECT COUNT(*) FROM trades WHERE asset='KNIFEX' AND side='BUY'").fetchone()[0]
meta = json.loads(con.execute("SELECT value FROM state WHERE key='risk_meta'").fetchone()[0])
pos = con.execute("SELECT 1 FROM positions WHERE sleeve='risk' AND asset='DIPX'").fetchone()
raw = con.execute("SELECT snap FROM trades WHERE asset='DIPX' AND side='BUY'").fetchone()
snap = json.loads(raw[0]) if raw and raw[0] else {}
con.close()
dx, kx = by.get("DIPX") or {}, by.get("KNIFEX") or {}
check(dx.get("setup") == "dip" and dx.get("bought") and not dx.get("block"), f"DIPX row is a taken dip ({dx.get('setup')}, bought {dx.get('bought')}, block {dx.get('block')})")
check(dx.get("plain") and "turned up" in dx["plain"], f"DIPX why: {dx.get('plain')}")
check(buy is not None and buy[0] == "dip" and buy[1] is not None and buy[1] >= rst["threshold"] - 1e-9 and "EV +" in (buy[2] or "") and "RISK dip" in (buy[2] or ""), f"DIPX order tagged dip, confidence >= threshold, EV positive: {buy}")
check(pos and (meta.get("DIPX") or {}).get("setup") == "dip" and "turned up" in ((meta.get("DIPX") or {}).get("why") or ""), "open position remembers Dip and the reason")
check(all(snap.get(k) is not None for k in ("conf", "parts", "p", "target", "loss", "costs", "ev", "entry")), f"DIPX buy stores confidence, components, p, target, loss, costs, EV and the fill: {sorted(snap)}")
check(isinstance(snap.get("parts"), dict) and "pullback" in (snap.get("parts") or {}), "the stored components include the dip bars")
meta_snap = (meta.get("DIPX") or {}).get("snap") or {}
check(meta_snap.get("entry") == snap.get("entry") and meta_snap.get("ev") == snap.get("ev"), "the open position keeps the same snapshot the buy stored")
check(kx.get("setup") == "dip" and not kx.get("bought") and kx.get("block") and "still falling" in kx["block"].lower(), f"KNIFEX rejected: {kx.get('block')}")
check(knife_buy == 0, "falling knife was not bought")
import dashboard
D = dashboard.build(common.db())
pos = next(p for p in D["positions"] if p["asset"] == "DIPX" and p["sleeve"] == "risk")
order = next(t for t in D["trades"] if t.get("asset") == "DIPX" and t.get("side") == "BUY")
krow = next(x for x in D["sleeves"]["risk"]["cands"] if x["a"] == "KNIFEX")
drow = next(x for x in D["sleeves"]["risk"]["cands"] if x["a"] == "DIPX")
check(pos.get("setup") == "dip" and "turned up" in (pos.get("why") or ""), "dashboard position row is a Dip with the reason")
check(order.get("sub") == "dip" and "RISK dip" in (order.get("reason") or ""), "dashboard order is tagged dip")
check(drow.get("setup") == "dip" and krow.get("setup") == "dip" and D["sleeves"]["risk"]["rules"].get("dip_weights"), "dashboard candidates carry the setup, and dip weights are exported")
need = ("conf", "parts", "p", "target", "loss", "costs", "ev")
check(pos.get("snap") and all(pos["snap"].get(k) is not None for k in need), f"dashboard position carries the entry snapshot: {pos.get('snap')}")
check(isinstance(pos.get("ev_now"), (int, float)), f"open dip has a live EV now ({pos.get('ev_now')})")
check(abs(pos["ev_now"] - daypot.ev_now(pos["ev_p"], pos["now"], pos["ev_tgt"], pos["ev_stop"], pos["ev_cost"])) < 1e-6, "position EV now matches p x upside - (1-p) x downside - sell cost")
check(order.get("snap") and order.get("ev_now") == pos.get("ev_now") and order.get("ev_tgt") == pos.get("ev_tgt"), "the open buy row shows the same entry snapshot and the same EV now as the position")
# A position with no snapshot (bought before scoring) still gets EV now. A stop-out copies the snapshot onto the sell.
con = common.db()
rate = con.execute("SELECT rate FROM fx WHERE pair='USDGBP' AND rate>0 ORDER BY ts DESC LIMIT 1").fetchone()[0]
px0 = con.execute("SELECT price FROM prices WHERE asset='DIPX' ORDER BY ts DESC LIMIT 1").fetchone()[0]
later = END + timedelta(minutes=20)
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (later.isoformat(), "DIPX", "crypto", px0 * 0.7, None, None, 5e10, 2e11, "sim", later.isoformat()))
con.commit(); con.close()
common._NOW = later
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2): trader.main()
print(buf2.getvalue().strip()[:800])
con = common.db()
sell = con.execute("SELECT snap FROM trades WHERE asset='DIPX' AND side='SELL' ORDER BY id DESC LIMIT 1").fetchone()
ss = json.loads(sell[0]) if sell and sell[0] else {}
check(ss.get("conf") == snap.get("conf") and ss.get("ev") == snap.get("ev") and ss.get("parts"), f"the closing sell copies the entry snapshot (conf {ss.get('conf')}, EV {ss.get('ev')})")
con.execute("INSERT INTO positions VALUES('risk','LEGX','crypto',10,?,?,?,1)", (1.2 * rate, 10 * 1.2 * rate, later.isoformat()))
con.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", (later.isoformat(), "LEGX", "crypto", 1.2, None, None, 5e10, 2e11, "sim", later.isoformat()))
con.commit(); con.close()
D = dashboard.build(common.db())
leg = next(p for p in D["positions"] if p.get("asset") == "LEGX" and p.get("sleeve") == "risk")
check(leg.get("snap") is None and isinstance(leg.get("ev_now"), (int, float)), f"legacy position has no entry snapshot and still has EV now ({leg.get('ev_now')})")
check(abs(leg["ev_now"] - daypot.ev_now(leg["ev_p"], leg["now"], leg["ev_tgt"], leg["ev_stop"], leg["ev_cost"])) < 1e-6, "legacy EV now uses p 0.5 and the pot's target and stop")
closed = next(t for t in D["trades"] if t.get("asset") == "DIPX" and t.get("side") == "SELL")
check(closed.get("snap") and isinstance(closed.get("ev_now"), (int, float)), "a closed order keeps the entry snapshot and a live EV now")
check(abs(closed["ev_now"] - daypot.ev_now(closed["ev_p"], closed["ev_px"], closed["ev_tgt"], closed["ev_stop"], closed["ev_cost"])) < 1e-6, "closed-order EV now matches the formula from the current price")
site = Path("/tmp/dipui")
import shutil
if site.exists(): shutil.rmtree(site)
site.mkdir()
dashboard.OUT = site
dashboard.main()
print("FAILS:", len(fails))
for f in fails: print(" ", f)
sys.exit(1 if fails else 0)
