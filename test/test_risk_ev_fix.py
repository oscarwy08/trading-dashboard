"""risk_ev_fix paper book. The main book's Risk EV is a 3.5% win against a scaled 8% loss at a 50% chance,
which can never clear the 0.3% hurdle, so the pot never trades and never measures itself. This book starts
each setup from a replay of the pot's own rules (config/variants/risk_ev_fix.yaml).

Usage: python3 test/test_risk_ev_fix.py
"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "book.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["VARIANTS_DIR"] = str(TMP / "variants")
os.environ["VARIANTS_MAIN_DB"] = str(TMP / "book.db")
os.environ.pop("TRADING_VARIANT", None)

import common, riskpot, variants, dashboard, strategytext

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

ROOT = Path(__file__).resolve().parent.parent
base = common.cfg()
os.environ["TRADING_VARIANT"] = "risk_ev_fix"
over = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
Rm, Rv = variants.rules_of(base), variants.rules_of(over)
RPm, RPv = base["riskpot"], over["riskpot"]
ss0 = dict(p=0.5, n=0, wins=0, exp=None)

# --- wiring: on only by overlay, nothing else moves ---
check(Rm["risk_ev_fix"] is False and Rv["risk_ev_fix"] is True, "the flag is off on main and on only by overlay")
check(not variants.active(Rm) and variants.active(Rv), "risk_ev_fix is an active paper book")
check(all(Rv[k] == Rm[k] for k in Rm if k != "risk_ev_fix"), "the overlay turns on no other flag")
check("ev_prior_empirical" not in RPm, "main config has no replayed prior")
check({k: v for k, v in RPv.items() if k != "ev_prior_empirical"} == RPm,
      "every other riskpot number (stop, trail, hurdle, threshold, priors) is the main book's")
check(all(over[k] == base[k] for k in base if k not in ("riskpot", "variant_rules")), "no other config section changes")
check("risk_ev_fix" in base["variants"]["run"], "the cycle runs this book")
check(variants.LABELS["risk_ev_fix"] == "Risk EV fix" and dashboard.book_menu_label("risk_ev_fix") == "Risk EV fix",
      "the book has a label and a menu entry")
check("risk_ev_fix" in variants.TABLE_BOOKS and "risk_ev_fix" in variants.BLURB, "the scorecard shows its Risk row and blurb")
check(strategytext.by_name()["risk_ev_fix"]["kind"] == "tweak", "the rule book lists it as a tweak")
js = (ROOT / "web" / "app.js").read_text()
check("risk_ev_fix: '#" in js and "risk_ev_fix: 'Risk EV fix'" in js, "the variants chart names this book")
src = (ROOT / "trader.py").read_text()
check(src.count("variants.risk_ev_params(RP, R,") == 2 and "_legacy_arm_win=True) if R.get" not in src,
      "both Risk entry EVs (the cycle row and the armed dip) go through risk_ev_params")

# --- main book EV is unchanged ---
check(variants.risk_ev_params(RPm, Rm, "breakout", "crypto") is RPm, "main book passes its riskpot dict through untouched")
check(variants.risk_ev_params(RPm, Rm, "dip", "stock") is RPm, "main book: dip and stocks too")
ev, w, l, p = riskpot.setup_ev(RPm, 0.6, 0.75, 0.5, ss0)
check((ev, w, l, p) == (-0.95, 3.5, 4.4, 0.5), f"main EV stays 0.5 x 3.5 - 0.5 x 4.4 - 0.5 = -0.95 (got {ev})")
ev, w, l, p = riskpot.setup_ev(RPm, 0.6, 3.46, 0.571, ss0)
check(ev == -2.281 and l == 6.92, f"main EV for a CAP-like row stays 0.5 x 3.5 - 0.5 x 6.92 - 0.571 = -2.281 (got {ev})")
os.environ["TRADING_VARIANT"] = "day_ev_blend"
blend = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
Rb = variants.rules_of(blend)
rpb = variants.risk_ev_params(blend["riskpot"], Rb, "breakout", "crypto")
check(rpb.get("_legacy_arm_win") is True and "_ev_prior" not in rpb, "day_ev_blend keeps its legacy Risk win")
check(riskpot.setup_ev(rpb, 0.8, 3.0, 0.6, ss0)[1] == round(13.5 * 0.75, 3), "day_ev_blend win is still 13.5 x ATR scale")

# --- the variant uses the replayed prior ---
E = RPv["ev_prior_empirical"]
used = 0
for setup in ("breakout", "dip"):
    for klass in ("crypto", "stock"):
        row = (E.get(setup) or {}).get(klass)
        rp = variants.risk_ev_params(RPv, Rv, setup, klass)
        prior = riskpot.empirical_prior(RPv, setup, klass)
        if prior is None:
            check(rp is RPv, f"{setup}/{klass}: no usable prior, so the main model is kept")
            continue
        used += 1
        check(rp["_ev_prior"] == prior and abs(prior["p"] - float(row["p"])) < 1e-12, f"{setup}/{klass}: the replayed prior is passed in")
        ev, w, l, p = riskpot.setup_ev(rp, 0.7, 2.0, 0.5, ss0)
        want = round(float(row["p"]) * float(row["win"]) - (1 - float(row["p"])) * float(row["loss"]) - 0.5, 3)
        check(abs(ev - want) < 1e-9 and w == round(float(row["win"]), 3) and l == round(float(row["loss"]), 3)
              and abs(p - max(0.05, min(0.95, float(row["p"])))) < 1e-9,
              f"{setup}/{klass}: EV {ev:+.3f}% = p x win - (1-p) x loss - cost from the replay, not 3.5% and 0.5")
        ev_atr = riskpot.setup_ev(rp, 0.7, 6.0, 0.5, ss0)[0]
        check(ev_atr == ev, f"{setup}/{klass}: the replayed loss is not rescaled by ATR")
check(used >= 1, "at least one setup has a replayed prior")

# --- a historically positive setup can clear the hurdle; a negative one cannot ---
good = dict(RPv, ev_prior_empirical=dict(min_n=30, breakout=dict(crypto=dict(p=0.42, win=14.0, loss=7.0, n=200))))
rpg = variants.risk_ev_params(good, Rv, "breakout", "crypto")
ev = riskpot.setup_ev(rpg, 0.62, 3.46, 0.571, ss0)[0]
check(abs(ev - (0.42 * 14 - 0.58 * 7 - 0.571)) < 1e-9 and ev > float(RPv["ev_min_pct"]),
      f"a setup that replayed positive ({ev:+.2f}%) clears the {RPv['ev_min_pct']}% hurdle on this book")
check(riskpot.setup_ev(good, 0.62, 3.46, 0.571, ss0)[0] < 0, "the same candidate on the main model is still negative")
bad = dict(RPv, ev_prior_empirical=dict(min_n=30, breakout=dict(crypto=dict(p=0.3, win=9.0, loss=10.0, n=200))))
check(riskpot.setup_ev(variants.risk_ev_params(bad, Rv, "breakout", "crypto"), 0.9, 2.0, 0.5, ss0)[0] < 0,
      "a setup that replayed negative stays below the hurdle (no fudge)")
thin = dict(RPv, ev_prior_empirical=dict(min_n=30, breakout=dict(crypto=dict(p=0.6, win=20.0, loss=5.0, n=12))))
check(variants.risk_ev_params(thin, Rv, "breakout", "crypto") is thin, "a prior from too few replayed trades is not used")

# --- live closes still move it (ev_prior_n weighting) ---
ss_bad = dict(p=0.2, n=10, wins=2, exp=-6.0)
ev_live = riskpot.setup_ev(rpg, 0.62, 3.46, 0.571, ss_bad)[0]
p_live = riskpot.setup_ev(rpg, 0.62, 3.46, 0.571, ss_bad)[3]
check(abs(p_live - (2 + 10 * (0.42 + 0.12 * 10 / 30)) / 20) < 1e-3, f"p blends 2/10 live wins with the replayed 0.42 (got {p_live})")
check(ev_live < ev and ev_live < float(RPv["ev_min_pct"]), f"ten bad live closes pull the EV down to {ev_live:+.2f}%")
ss_conf = dict(p=0.5, n=0, wins=0, exp=None)
check(riskpot.setup_ev(rpg, 0.95, 3.46, 0.571, ss_conf)[3] == 0.42, "with no closes confidence does not move p off the replayed rate")

if fails:
    print(f"\n{len(fails)} FAILED"); sys.exit(1)
print("\nall risk_ev_fix checks passed")
