"""Five paper books. None of them changes the main book's flags.

risk_giveback_15, day_trail_arm, day_ev_blend, no_risk_clock, day_capital_caution.
"""
import json, os, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(__file__).resolve().parent / "_paper_variants"
TMP.mkdir(exist_ok=True)
DB = TMP / "book.db"
if DB.exists():
    DB.unlink()
os.environ["TRADING_DB"] = str(DB)
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["EXIT_SHADOW_DB"] = str(TMP / "shadow.db")
os.environ["VARIANTS_DIR"] = str(TMP / "variants")
os.environ["VARIANTS_MAIN_DB"] = str(DB)
os.environ.pop("TRADING_VARIANT", None)

import common, riskpot, trader, variants, exit_watch
from exits import day_reason, risk_reason

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

NAMES = ("risk_giveback_15", "day_trail_arm", "day_ev_blend", "no_risk_clock", "day_capital_caution", "partial_take", "sent_split", "stock_calibrated")
base = common.cfg()
check(all(not base["variant_rules"][k] for k in NAMES), "main leaves the paper flags off")
check(base["rules"]["frozen_since"] == common.cfg()["rules"]["frozen_since"], "freeze date is the config's own")
for name in NAMES:
    os.environ["TRADING_VARIANT"] = name
    over = common.cfg()
    os.environ.pop("TRADING_VARIANT", None)
    R = variants.rules_of(over)
    check(R[name] is True and over["rules"]["frozen_since"] == base["rules"]["frozen_since"],
          f"{name} turns on only by overlay and does not move the freeze date")
    check(variants.active(R) and not variants.active(variants.rules_of(base)), f"{name} is an active paper book")

os.environ["TRADING_VARIANT"] = "day_ev_blend"
blend = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(blend["variant_rules"]["risk_ev_legacy_win"] is True and not blend["variant_rules"]["day_trail_arm"],
      "the EV blend does not turn on the Day trail and opts out of a Risk-win change")
_ss0 = dict(p=0.5, n=0, wins=0, exp=None)
_blend_rp = dict(blend["riskpot"], _legacy_arm_win=True)
_blend_ev = riskpot.setup_ev(_blend_rp, 0.8, 3.0, 0.6, _ss0)
_main_ev = riskpot.setup_ev(base["riskpot"], 0.8, 3.0, 0.6, _ss0)
check(abs(_main_ev[1] - 3.5) < 1e-9 and abs(_blend_ev[1] - 13.5 * 0.75) < 1e-6,
      "day_ev_blend keeps the old ATR-scaled Risk win; the main book locks 3.5%")
os.environ["TRADING_VARIANT"] = "no_risk_clock"
clock = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(clock["riskpot"]["cooldown_h"] == 0 and base["riskpot"]["cooldown_h"] == 24,
      "no_risk_clock deletes the 24h clock on that book only")

# --- numbers from the review ---
check(abs(variants.blend_ev(0.8, 10, -1.10, 10) - (-0.15)) < 1e-9,
      "10 momentum closes at -1.10% blended halfway with a +0.8% model is -0.15%")
check(variants.blend_ev(0.8, 0, None, 10) == 0.8, "with no closes the model stands alone")
f = variants.caution_f(-0.00705, 15)
check(abs(f - 0.047) < 1e-9, f"a -0.705% day is f={f:.4f}")
check(abs(variants.caution_threshold(0.35, f, 0.30) - 0.364) < 1e-9, "momentum 0.35 rises by 0.014 to 0.364")
check(abs(variants.caution_threshold(0.30, f, 0.30) - 0.314) < 1e-9, "MON/WLD/XRP base 0.30 rises by the same 0.014")
check(abs(variants.caution_threshold(0.55, f, 0.30) - 0.564) < 1e-9, "burst 0.55 rises to 0.564")
check(abs(variants.caution_size_scale(f) - (1 - 0.047)) < 1e-9, "size is multiplied by 1 - f")
check(variants.caution_f(0.02, 15) == 0.0, "a gain does not lower the hurdle")
check(variants.caution_f(-0.30, 15) == 2.0, "f is not capped at 1")
check(variants.caution_threshold(0.35, 2.0, 0.30) == 0.95, "past the stop the hurdle keeps rising")
check(variants.caution_size_scale(2.0) == 0.0, "size scale stops at 0 once f reaches 1")
# Half the loss is the other account: crypto took it all, stocks took none.
check(variants.caution_f(-0.0141, 15) > f and variants.caution_f(0.0, 15) == 0.0,
      "a loss that is only in one account raises only that account's f")

t0 = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
RP = dict(base["riskpot"])
# Peak +100% net, price +80% (10% under the peak). The price trail sells. The 65% lock holds.
old = risk_reason(100, 180, 200, t0.isoformat(), RP, None, t0, 0.0)
new = risk_reason(100, 180, 200, t0.isoformat(), RP, None, t0, 0.0, keep_pct=65)
check(old["reason"] and "trailing" in old["reason"], "the main trail sells 10% under a doubled peak")
check(new["reason"] is None and new["trail_on"], "65% of a +100% peak is +65%, so +80% stays open")
# Peak just armed, price still above the 10% price trail but under 65% of the peak gain.
armed = risk_reason(100, 110, 120, t0.isoformat(), RP, None, t0, 0.0, keep_pct=65)
held = risk_reason(100, 114, 120, t0.isoformat(), RP, None, t0, 0.0, keep_pct=65)
main_hold = risk_reason(100, 110, 120, t0.isoformat(), RP, None, t0, 0.0)
check(armed["reason"] and "keeps 65%" in armed["reason"], "a giveback through 65% of the peak sells")
check(held["reason"] is None, "a price still above 65% of the peak does not sell")
check(main_hold["reason"] is None, "the same price is above the main book's 10% price trail")
late = (t0 - timedelta(days=8)).isoformat()
waiting = risk_reason(100, 118, 120, late, RP, None, t0, 0.0, keep_pct=65)
flat7 = risk_reason(100, 105, 110, late, RP, None, t0, 0.0, keep_pct=65)
check(waiting["reason"] is None and waiting["trail_on"], "day 7 does not sell while the trail is armed")
check(flat7["reason"] and "max hold" in flat7["reason"], "day 7 still sells when the trail never armed")
stopped = risk_reason(100, 80, 100, t0.isoformat(), RP, None, t0, 0.0, keep_pct=65)
check(stopped["reason"] and "stop-loss" in stopped["reason"], "the -12% stop still sells")

DM = base["day"]["momentum"]
md = dict(sub="burst", tp=8, sl=5, peak=102.1, runner=False)
st = dict(sell_threshold=-0.2)
# 60% of a +2.1% peak is 1.26%, under a 1.48% bar (4 x 0.37% cost). The old +2% burst arm would sell.
quiet = day_reason(100, 101, t0.isoformat(), dict(md), base["day"]["burst"], st, 0.5, "", t0, 0.0, 0.35, "X",
                   arm_floor_pct=1.48)
plain = day_reason(100, 101, t0.isoformat(), dict(md), base["day"]["burst"], st, 0.5, "", t0, 0.0, 0.35, "X")
check(quiet["reason"] is None and not quiet["trail_on"], "a peak whose lock is under the cost/ATR bar does not arm")
check(plain["reason"] and "trailing" in plain["reason"], "without the bar the same +2.1% peak does arm and sell")
wide = day_reason(100, 103, t0.isoformat(), dict(sub="burst", tp=8, sl=5, peak=106, runner=False),
                  base["day"]["burst"], st, 0.5, "", t0, 0.0, 0.35, "X", arm_floor_pct=1.48)
check(wide["reason"] and "trailing" in wide["reason"], "once 60% of the peak clears the bar, a crossed trail sells")
check(variants.day_arm_bar(variants.rules_of(base), 0.37, 2.10) is None, "the main book does not raise the Day arm")
os.environ["TRADING_VARIANT"] = "day_trail_arm"
bar = variants.day_arm_bar(variants.rules_of(common.cfg()), 0.37, 2.10)
os.environ.pop("TRADING_VARIANT", None)
check(abs(bar - max(4 * 0.37, 1 * 2.10)) < 1e-9, "the bar is the greater of 4x cost and 1x ATR")

now_ = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)
earlier = (now_ - timedelta(hours=2)).isoformat()
check(variants.risk_on_clock(variants.rules_of(base), earlier, now_, 24), "the main book still blocks for 24h")
check(not variants.risk_on_clock(variants.rules_of(clock), earlier, now_, 0), "no_risk_clock does not block")
check(not variants.risk_on_clock(variants.rules_of(clock), earlier, now_, 24),
      "the flag deletes the clock even if a 24h number is still configured")

# The giveback book sells a position the main book holds. Fresh quote, peak stored at 120, price 110.
def book(variant):
    if DB.exists():
        DB.unlink()
    common._NOW = now_
    con = common.db()
    ts = now_.isoformat()
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
    for sl in common.SLEEVES:
        for ac in common.ACCOUNTS:
            con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
    con.execute("INSERT INTO state VALUES('core_plan',?)", ('{}',))
    con.execute("INSERT INTO state VALUES('order_queue',?)", ('[]',))
    con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("risk", "STRK", "crypto", 1, 100, 100, ts, 1))
    con.execute("INSERT INTO state VALUES('risk_peaks',?)", (json.dumps({"STRK": 120.0}),))
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, "STRK", "crypto", 110.0, None, None, None, 1e11, "test", ts))
    con.commit(); con.close()
    if variant:
        os.environ["TRADING_VARIANT"] = variant
    trader.main()
    os.environ.pop("TRADING_VARIANT", None)
    con = common.db()
    sold = con.execute("SELECT reason FROM trades WHERE asset='STRK' AND side='SELL'").fetchone()
    con.close()
    return None if sold is None else sold[0]

check(book(None) is None, "main holds STRK at 110 against a 120 peak (above the 10% price trail)")
sold = book("risk_giveback_15")
check(sold is not None and "keeps 65%" in sold, f"risk_giveback_15 sells that peak ({sold})")

# Watcher shadow-logs the giveback on that book and does not log it on main.
os.environ["EXIT_WATCH_SHADOW"] = "1"
Path(os.environ["EXIT_SHADOW_DB"]).unlink(missing_ok=True)
common._NOW = now_
con = common.db()
# book() sold the position. Re-open it for the shadow pass.
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
            ("risk", "STRK", "crypto", 1, 100, 100, now_.isoformat(), 1))
con.execute("INSERT OR REPLACE INTO state VALUES('risk_peaks',?)", (json.dumps({"STRK": 120.0}),))
con.commit()
main_hit = exit_watch.shadow_evaluate(con, {"STRK": 110.0}, book="main", c=exit_watch.config_for("main"))
var_hit = exit_watch.shadow_evaluate(con, {"STRK": 110.0}, book="risk_giveback_15",
                                    c=exit_watch.config_for("risk_giveback_15"))
con.close()
check(not any("keeps 65%" in (e.get("reason") or "") for e in main_hit), "main does not shadow the giveback")
check(any("keeps 65%" in (e.get("reason") or "") and "[risk_giveback_15]" in (e.get("log") or "") for e in var_hit),
      "the watcher tags a would-exit with the giveback book")

# The comparison row uses the Day table columns.
con = common.db()
rows = variants.sleeve_table(con)
con.close()
check([r["strategy"] for r in rows] == ["Momentum", "Burst", "Risk"], "each paper book has momentum, burst and risk rows")
need = {"budget", "trades", "win_rate", "avg_win", "avg_loss", "pf", "pnl", "open_pnl", "fees", "exp_gbp"}
check(need <= set(rows[0]), f"the row has the Day table columns ({sorted(need - set(rows[0]))})")

# --- partial take: half at the arm, buyback only on a fresh EV, £1 only for that half ---
check(base["risk"]["min_trade_size"] == 5, "the £5 minimum is unchanged on the main book")
os.environ["TRADING_VARIANT"] = "partial_take"
pt = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
check(pt["risk"]["min_trade_size"] == 5 and variants.rules_of(pt)["partial_min_gbp"] == 1,
      "partial_take keeps the £5 minimum and allows a £1 half")
check(abs(variants.fresh_ev(0.5, 3.5, 12, 0.6) - (0.5 * 3.5 - 0.5 * 12 - 0.6)) < 1e-9,
      "fresh EV is p x upside - (1-p) x downside - cost, from the current price")
check(abs(variants.risk_lock_pct(base["riskpot"]) - 3.5) < 1e-9, "the Risk upside in that formula is the 3.5% trail lock")
check(abs(variants.split_extra_gbp(10, 5, 0.003, 0.003, 0.001, "crypto") - 0.001) < 1e-9,
      "splitting a crypto sell costs one extra gas")
check(variants.split_extra_gbp(10, 5, 0.003, 0.003, 0.001, "stock") == 0,
      "a stock split has no extra gas")
check(variants.partial_half(0.01, 100, 1)[0] is None, "a half under £1 is not an order")
check(not variants.buyback_ok(104, 104, 1.0, 0.3), "the arm price is not a dip")
check(not variants.buyback_ok(100, 104, -1.0, 0.3), "a dip whose fresh EV misses 0.3% is not bought back")
check(variants.buyback_ok(100, 104, 0.4, 0.3), "a dip whose fresh EV clears 0.3% is bought back")

def arm_book(asset, sleeve, entry, px, qty, meta, variant, peaks=None):
    if DB.exists():
        DB.unlink()
    common._NOW = now_
    con = common.db()
    ts = now_.isoformat()
    con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
    for sl in common.SLEEVES:
        for ac in common.ACCOUNTS:
            con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
    con.execute("INSERT INTO state VALUES('core_plan',?)", ('{}',))
    con.execute("INSERT INTO state VALUES('order_queue',?)", ('[]',))
    con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", (sleeve, asset, "crypto", qty, entry, qty * entry, ts, 1))
    con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, asset, "crypto", px, None, None, None, 1e11, "test", ts))
    if sleeve == "day":
        con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps({asset: meta}),))
    else:
        con.execute("INSERT INTO state VALUES('risk_meta',?)", (json.dumps({asset: meta}),))
        if peaks:
            con.execute("INSERT INTO state VALUES('risk_peaks',?)", (json.dumps(peaks),))
    con.commit(); con.close()
    if variant:
        os.environ["TRADING_VARIANT"] = variant
    trader.main()
    os.environ.pop("TRADING_VARIANT", None)
    con = common.db()
    row = con.execute("SELECT qty FROM positions WHERE asset=?", (asset,)).fetchone()
    sold = con.execute("SELECT reason FROM trades WHERE asset=? AND side='SELL' ORDER BY id DESC LIMIT 1", (asset,)).fetchone()
    bought = con.execute("SELECT reason FROM trades WHERE asset=? AND side='BUY' ORDER BY id DESC LIMIT 1", (asset,)).fetchone()
    con.close()
    return (None if row is None else row[0], None if sold is None else sold[0], None if bought is None else bought[0])

day_meta = dict(sub="momentum", tp=8, sl=2, atr=1, peak=100, opened=now_.isoformat(), conf=0.6, score=0.6)
qty, reason, _buy = arm_book("HALF", "day", 100, 104, 10, day_meta, None)
check(qty == 10 and reason is None, "main does not sell half at the Day arm")
qty, reason, _buy = arm_book("HALF", "day", 100, 104, 10, day_meta, "partial_take")
check(qty is not None and abs(qty - 5) < 1e-6 and reason and "partial take" in reason and "extra fee of the split" in reason,
      f"partial_take sells half at the Day arm and logs the extra fee ({qty}, {reason})")
qty, reason, _buy = arm_book("TINY", "day", 100, 104, 0.01, day_meta, "partial_take")
check(qty is not None and abs(qty - 0.01) < 1e-9, "a half under £1 is left on the trail")
risk_meta = dict(conf=0.7, setup="breakout", ev=0.4)
qty, reason, _buy = arm_book("CAP", "risk", 100, 116, 2, risk_meta, "partial_take", peaks={"CAP": 100})
check(qty is not None and abs(qty - 1) < 1e-6 and reason and "partial take" in reason and "+15%" in reason,
      f"partial_take sells half of a Risk position at the +15% arm ({qty}, {reason})")

# Dip after the half-sale. A wide stop misses 0.3%; the stored target clears it.
if DB.exists():
    DB.unlink()
common._NOW = now_
con = common.db()
ts = now_.isoformat()
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
for sl in common.SLEEVES:
    for ac in common.ACCOUNTS:
        con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
con.execute("INSERT INTO state VALUES('core_plan',?)", ('{}',))
con.execute("INSERT INTO state VALUES('order_queue',?)", ('[]',))
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "HALF", "crypto", 10, 100, 1000, ts, 1))
con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, "HALF", "crypto", 104, None, None, None, 1e11, "test", ts))
meta = dict(day_meta, partial=None)
con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps({"HALF": day_meta}),))
con.commit(); con.close()
os.environ["TRADING_VARIANT"] = "partial_take"
trader.main()
later = now_ + timedelta(minutes=20)
common._NOW = later
con = common.db()
con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (later.isoformat(), "HALF", "crypto", 103, None, None, None, 1e11, "test", later.isoformat()))
# Stored target/stop make the fresh value miss the hurdle.
meta = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0])
meta["HALF"]["tp"], meta["HALF"]["sl"] = 3, 8
con.execute("UPDATE state SET value=? WHERE key='day_meta'", (json.dumps(meta),))
con.commit(); con.close()
trader.main()
con = common.db()
miss = con.execute("SELECT COUNT(*) FROM trades WHERE asset='HALF' AND side='BUY'").fetchone()[0]
qty_miss = con.execute("SELECT qty FROM positions WHERE asset='HALF'").fetchone()[0]
con.close()
check(miss == 0 and abs(qty_miss - 5) < 1e-4, "a dip is not bought back when fresh EV misses 0.3%")
con = common.db()
meta = json.loads(con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone()[0])
meta["HALF"]["tp"], meta["HALF"]["sl"] = 8, 2
con.execute("UPDATE state SET value=? WHERE key='day_meta'", (json.dumps(meta),))
con.execute("INSERT INTO prices(ts,asset,klass,price,high24,low24,vol24,mcap,source,source_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ((later + timedelta(minutes=5)).isoformat(), "HALF", "crypto", 102.8, None, None, None, 1e11, "test",
             (later + timedelta(minutes=5)).isoformat()))
con.commit(); con.close()
common._NOW = later + timedelta(minutes=5)
trader.main()
os.environ.pop("TRADING_VARIANT", None)
con = common.db()
back = con.execute("SELECT reason FROM trades WHERE asset='HALF' AND side='BUY' ORDER BY id DESC LIMIT 1").fetchone()
qty_back = con.execute("SELECT qty FROM positions WHERE asset='HALF'").fetchone()[0]
con.close()
check(back and "buyback half" in back[0] and "fresh EV" in back[0] and qty_back > 9,
      f"the sold half is bought back once fresh EV clears ({None if not back else back[0]}, qty {qty_back})")

# Watcher shadow-logs the half-sale on this book only.
os.environ["EXIT_WATCH_SHADOW"] = "1"
Path(os.environ["EXIT_SHADOW_DB"]).unlink(missing_ok=True)
common._NOW = now_
if DB.exists():
    DB.unlink()
con = common.db()
ts = now_.isoformat()
con.execute("INSERT INTO fx VALUES(?,?,?,?,?,?)", (ts, "USDGBP", 1.0, "test", None, None))
for sl in common.SLEEVES:
    for ac in common.ACCOUNTS:
        con.execute("INSERT INTO acct_cash VALUES(?,?,?)", (sl, ac, 500.0))
con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)", ("day", "HALF", "crypto", 10, 100, 1000, ts, 1))
con.execute("INSERT INTO state VALUES('day_meta',?)", (json.dumps({"HALF": day_meta}),))
con.commit()
main_hit = exit_watch.shadow_evaluate(con, {"HALF": 104.0}, book="main", c=exit_watch.config_for("main"))
var_hit = exit_watch.shadow_evaluate(con, {"HALF": 104.0}, book="partial_take", c=exit_watch.config_for("partial_take"))
con.close()
check(not any("partial take" in (e.get("reason") or "") for e in main_hit), "main does not shadow a half-sale")
check(any("partial take" in (e.get("reason") or "") and "extra fee" in (e.get("reason") or "")
          and "[partial_take]" in (e.get("log") or "") for e in var_hit),
      "the watcher tags the half-sale and its extra fee with the book")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
