"""sent_split paper book. The main book keeps missing sentiment at 0.25 and lets
positive sentiment qualify a Risk dip. This book does not.

Usage: python3 test/test_sent_split.py
"""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "book.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ.pop("TRADING_VARIANT", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common, dashboard, daypot, ingest_sentiment, riskpot, variants

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond:
        fails.append(msg)

base = common.cfg()
os.environ["TRADING_VARIANT"] = "sent_split"
over = common.cfg()
os.environ.pop("TRADING_VARIANT", None)
Roff = variants.rules_of(base)
Ron = variants.rules_of(over)

check(Roff["sent_split"] is False and Ron["sent_split"] is True, "the flag is off on the main book and on only by overlay")
check(not variants.active(Roff) and variants.active(Ron), "sent_split is an active paper book")
check(over["rules"]["frozen_since"] == base["rules"]["frozen_since"], "the overlay does not move the freeze date")
check("sent_split" in (base.get("variants") or {}).get("run"), "the cycle, the nightly line and the watcher read this name")
check(variants.LABELS["sent_split"] == "Sentiment split" and dashboard.book_menu_label("sent_split") == "Sentiment split",
      "the variants tab and the book menu use the same label")
check("sent_split" in variants.TABLE_BOOKS and "48-hour uptrend" in variants.BLURB["sent_split"]
      and "0.50" in variants.BLURB["sent_split"],
      "the comparison table has a blurb and a sleeve table")
js = (common.ROOT / "web" / "app.js").read_text()
check("sent_split: '#a3e635'" in js and "sent_split: 'Sentiment split'" in js, "the variants chart names this book")
check(base["day"]["burst"]["sentiment_missing"] == 0.25 and over["day"]["burst"]["sentiment_missing"] == 0.5,
      "burst missing sentiment is 0.25 on main and 0.50 on this book")
check(base["riskpot"]["sentiment_missing"] == 0.25 and over["riskpot"]["sentiment_missing"] == 0.5, "Risk breakout matches")
check((base["riskpot"]["dip"].get("sentiment_missing", 0.25) == 0.25)
      and over["riskpot"]["dip"]["sentiment_missing"] == 0.5, "Risk dip matches")
check(base["day"]["burst"]["sentiment_min"] == over["day"]["burst"]["sentiment_min"], "the burst sentiment gate is unchanged")

c_main, p_main = daypot.confidence(base["day"]["burst"], 0.8, 3.0, None, 0.6, 0.5)
c_var, p_var = daypot.confidence(over["day"]["burst"], 0.8, 3.0, None, 0.6, 0.5)
check(abs(p_main["sentiment"] - 0.25) < 1e-9 and abs(p_var["sentiment"] - 0.5) < 1e-9 and c_var > c_main,
      "a missing burst print is below neutral on main and neutral here, so confidence is higher")
r_main, rp_main = riskpot.confidence(base["riskpot"], 0.7, 3.0, None, 0.6, 0.5)
r_var, rp_var = riskpot.confidence(over["riskpot"], 0.7, 3.0, None, 0.6, 0.5)
check(abs(rp_main["sentiment"] - 0.25) < 1e-9 and abs(rp_var["sentiment"] - 0.5) < 1e-9 and r_var > r_main,
      "a missing Risk print is neutral only on this book")
d_main, dp_main = riskpot.dip_confidence(base["riskpot"], 0.8, 0.8, 0.8, None, 0.6, 0.5)
d_var, dp_var = riskpot.dip_confidence(over["riskpot"], 0.8, 0.8, 0.8, None, 0.6, 0.5)
check(abs(dp_main["sentiment"] - 0.25) < 1e-9 and abs(dp_var["sentiment"] - 0.5) < 1e-9 and d_var > d_main,
      "a missing dip print is neutral only on this book")
_real, _rp = daypot.confidence(base["day"]["burst"], 0.8, 3.0, 0.0, 0.6, 0.5)
check(abs(_rp["sentiment"] - 0.5) < 1e-9 and abs(p_main["sentiment"] - 0.25) < 1e-9,
      "a real zero stays neutral on the main book; only a missing print is 0.25")

# --- catalyst and buzz fall back when the fields are absent ---
row = {"score": 0.2, "catalyst_score": 0.7, "buzz_change": 4}
check(variants.sent_score(row, Roff) == 0.2 and variants.sent_score(row, Ron) == 0.7, "this book prefers catalyst_score")
check(variants.sent_score({"score": 0.2}, Ron) == 0.2, "without catalyst_score the raw score stands")
check(variants.sent_score({"score": 0.2, "catalyst_score": None}, Ron) == 0.2, "a null catalyst is absent")
check(variants.sent_score({"score": 0.2, "catalyst_score": "n/a"}, Ron) == 0.2, "a non-numeric catalyst is ignored")
check(variants.buzz_of(row) == 4.0 and variants.buzz_of({"score": 0.2}) is None, "buzz_change is absent when the file has none")

# --- overheat and quiet strength ---
same, note = variants.confidence_adjust(0.60, Roff, 20, 0.8, 5, 3, True)
check(same == 0.60 and note is None, "the main book does not apply the penalty or the bonus")
hot, note = variants.confidence_adjust(0.60, Ron, 15, 0.3, None, None, True)
check(abs(hot - 0.50) < 1e-9 and note == "overheat -0.10", "24h +15% and sentiment 0.30 subtracts 0.10")
mild, note = variants.confidence_adjust(0.60, Ron, 14.9, 0.9, None, 3, True)
check(mild == 0.60 and note is None, "a 14.9% run-up is not overheated")
cool, note = variants.confidence_adjust(0.60, Ron, 20, 0.29, None, 3, True)
check(cool == 0.60 and note is None, "sentiment 0.29 with no buzz is not overheated")
buzz_hot, note = variants.confidence_adjust(0.60, Ron, 20, 0.0, 3, 1, False)
check(abs(buzz_hot - 0.50) < 1e-9 and "overheat" in note, "buzz_change 3 with the run-up is the same penalty")
no_buzz, note = variants.confidence_adjust(0.60, Ron, 20, 0.0, None, 4, True)
check(no_buzz == 0.60 and note is None, "a missing buzz does not invent the penalty")
quiet, note = variants.confidence_adjust(0.60, Ron, 2, 0.1, 1.19, 2, True)
check(abs(quiet - 0.65) < 1e-9 and note == "quiet strength +0.05", "quiet buzz, 2x volume and a confirmed setup adds 0.05")
edge, note = variants.confidence_adjust(0.60, Ron, 2, 0.1, 1.2, 2, True)
check(edge == 0.60 and note is None, "buzz_change 1.2 is not quiet")
thin, note = variants.confidence_adjust(0.60, Ron, 2, 0.1, 0.5, 1.99, True)
check(thin == 0.60 and note is None, "volume under 2x does not earn the bonus")
unconfirmed, note = variants.confidence_adjust(0.60, Ron, 2, 0.1, 0.5, 3, False)
check(unconfirmed == 0.60 and note is None, "an unconfirmed setup does not earn the bonus")
both, note = variants.confidence_adjust(0.60, Ron, 20, 0.5, 0.5, 2, True)
check(abs(both - 0.55) < 1e-9 and "overheat -0.10" in note and "quiet strength +0.05" in note,
      "a hot score and a quiet buzz both apply")
floor, _ = variants.confidence_adjust(0.04, Ron, 30, 0.8, None, None, False)
check(floor == 0.0, "the penalty does not go below 0")
cap, _ = variants.confidence_adjust(0.98, Ron, 0, 0.0, 0.2, 4, True)
check(cap == 1.0, "the bonus does not go above 1")

# --- Day momentum weight ---
t0 = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)
up = [(t0 - timedelta(hours=5), 1.0), (t0, 1.05)]
flat = [(t0 - timedelta(hours=5), 1.0), (t0, 1.049)]
check(variants.momentum_weight(0.4, up, 0.3, None, Roff) == 0.4, "the main book keeps the sentiment weight")
check(variants.momentum_weight(0.4, up, 0.3, None, Ron) == 0.0, "4h +5% and sentiment 0.30 zeroes the entry weight")
check(variants.momentum_weight(0.4, flat, 0.9, None, Ron) == 0.4, "4h +4.9% keeps the weight")
check(variants.momentum_weight(0.4, up, 0.1, 3, Ron) == 0.0, "buzz_change 3 with the 4h run-up zeroes the weight")
check(variants.momentum_weight(0.4, up, 0.1, None, Ron) == 0.4, "a missing buzz does not zero a cool sentiment")
check(variants.price_runup_pct([], 4) is None and variants.price_runup_pct(up[:1], 4) is None,
      "no print far enough back is not a run-up")
check(variants.momentum_blend(0.40, 0.0, 0.80) == 0.40, "weight 0 leaves the momentum score")
check(variants.momentum_blend(0.20, 0.4, 0.70) == round(0.6 * 0.20 + 0.4 * 0.70, 3), "the blend is the usual weighted score")

# --- Risk dip: sentiment no longer replaces the 48h uptrend ---
def flat_dip(end):
    pts = []
    steps = 40 * 4
    for i in range(steps + 1):
        h = i * 0.25
        if h > 3:
            px = 1.00
        elif h > 1:
            px = 0.95
        elif h > 0.2:
            px = 0.952
        else:
            px = 0.956
        pts.append((end - timedelta(hours=h), px))
    return list(reversed(pts))

END = datetime(2026, 10, 14, 16, tzinfo=timezone.utc)
no_trend = flat_dip(END)
plain = riskpot.dip(no_trend, base["riskpot"], sent=0.4, regime=0.6, hr=0.5, freshness=1, vol=2.0)
gated = riskpot.dip(no_trend, over["riskpot"], sent=0.4, regime=0.6, hr=0.5, freshness=1, vol=2.0, require_trend=True)
check(plain["show"] and plain["ok"] and not plain["trend_ok"] and plain["bounce"],
      f"main still buys a bounce on positive sentiment ({plain['why']})")
check(gated["show"] and not gated["ok"] and not gated["trend_ok"] and "48h uptrend" in gated["why"],
      f"this book refuses that dip ({gated['why']})")

def series(end, spec):
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

trend = series(END, [(48, 1.00), (18, 1.16), (1.5, 1.078), (0, 1.097)])
kept = riskpot.dip(trend, over["riskpot"], sent=0.1, regime=0.6, hr=0.5, freshness=1, vol=2.0, require_trend=True)
check(kept["show"] and kept["ok"] and kept["trend_ok"], f"a real 48h uptrend still qualifies ({kept['why']})")
src = (common.ROOT / "trader.py").read_text()
check("confidence_adjust" in src and "momentum_weight" in src and "require_trend=bool(R.get(\"sent_split\"))" in src,
      "the trader applies the penalty, the weight and the trend gate only when the flag is on")

# --- ingest stores the extra fields and still accepts a file without them ---
sent_dir = TMP / "sentiment"
sent_dir.mkdir()
ingest_sentiment.SENT_DIR = sent_dir
(sent_dir / "hot.json").write_text(json.dumps(dict(
    timestamp="2026-10-14T18:00:00Z", asset="BTC", score=0.4, confidence=0.8, volume=3,
    summary="hot", sources=[], catalyst_score=0.15, buzz_change=3.4)), encoding="utf-8")
(sent_dir / "plain.json").write_text(json.dumps(dict(
    timestamp="2026-10-14T18:05:00Z", asset="ETH", score=-0.2, confidence=0.6, volume=1,
    summary="plain", sources=[])), encoding="utf-8")
(sent_dir / "junk.json").write_text(json.dumps(dict(
    timestamp="2026-10-14T18:10:00Z", asset="SOL", score=0.1, confidence=0.5,
    summary="junk", sources=[], catalyst_score="n/a", buzz_change="lots")), encoding="utf-8")
ingest_sentiment.main()
con = common.db()
rows = {r["asset"]: r for r in con.execute("SELECT asset, score, catalyst_score, buzz_change FROM sentiment")}
check(set(rows) == {"BTC", "ETH", "SOL"}, f"all three files ingested ({sorted(rows)})")
check(abs(rows["BTC"]["catalyst_score"] - 0.15) < 1e-9 and abs(rows["BTC"]["buzz_change"] - 3.4) < 1e-9,
      "catalyst_score and buzz_change are stored")
check(rows["ETH"]["catalyst_score"] is None and rows["ETH"]["buzz_change"] is None, "a file without the fields stores NULL")
check(rows["SOL"]["catalyst_score"] is None and rows["SOL"]["buzz_change"] is None and abs(rows["SOL"]["score"] - 0.1) < 1e-9,
      "non-numeric extras do not reject the file")
btc = con.execute("SELECT * FROM sentiment WHERE asset='BTC'").fetchone()
check(variants.sent_score(btc, Ron) == 0.15 and variants.sent_score(btc, Roff) == 0.4,
      "a stored row prefers catalyst_score only on this book")
eth = con.execute("SELECT * FROM sentiment WHERE asset='ETH'").fetchone()
check(variants.sent_score(eth, Ron) == -0.2 and variants.buzz_of(eth) is None, "a row without the fields falls back")
check(not (sent_dir / "hot.json").exists() and (sent_dir / "processed" / "plain.json").exists(),
      "accepted files move to processed")

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
