"""Historical replay of the RISK pot's own entry and exit rules, to measure an empirical EV prior.

Not run by the cycle. It produced the numbers in config/variants/risk_ev_fix.yaml.

Crypto: Binance 15-minute klines (USDT pairs) for the coins the RISK screen has scored. Each bar is one
cycle. Entry rules are the pot's own: the breakout screen (riskpot.crypto_score), the intraday trend
gate (trader.momentum >= confirm_momentum), the 7-day run-up rule (riskpot.runup_adjust), the dip setup
(riskpot.dip, crypto path), and the confidence threshold (riskpot.confidence / dip_confidence). Inputs
the replay cannot see historically are set to the values a live cycle uses when they are missing:
sentiment missing (sentiment_missing), hit rate 0.5. The market regime is the DAY formula on BTC 1h/4h.
Turnover (vol / mcap) is not in klines, so the breakout score uses a fixed 0.10 for that part.
Exits are the pot's own: the 12% stop and the trail (arm at +15% net, sell 10% under the peak) on the
bar low/high, the 7-day max hold at the bar close. One position per coin and the 24h re-entry clock.
Not modelled: max_open, cash, sentiment block and exit, the drawdown halt.
A control (buy each coin once a day at a fixed time, same exits) shows what the market alone gave.

Stocks: Yahoo daily bars, split-adjusted (breakout only: riskpot.stock_score on each close, entry at that
close, the same stop and trail on later daily lows and highs, 5 trading days for the 7-day hold). Rougher
than crypto: no intraday entry, no volume (unknown = 0.4), no dip. The stock list is today's hand-picked
themes, so it carries selection bias.

Usage: python3 scripts/risk_ev_replay.py KLINES_DIR OUT_JSON [--split 2026-01-01] [--workers 6]
                                         [--universe U.json --stocks DAILY_DIR]
"""
import json, os, sqlite3, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from multiprocessing import Pool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRADING_LOG", "/tmp/risk_ev_replay.log")

import common, riskpot
from trader import momentum

C = common.cfg()
RP = C["riskpot"]
THR = float(RP.get("conf_threshold", 0.55))
STOP = float(RP["stop_loss_pct"]) / 100
ARM = float(RP["trail_after_pct"]) / 100
TRAIL = float(RP["trail_pct"]) / 100
HOLD_BARS = int(float(RP["max_hold_days"]) * 96)
COOL_BARS = int(float(RP.get("cooldown_h", 24)) * 4)
CRYPTO_RT = 0.50          # % round trip, typical of the live RISK candidate table (0.30-0.57 for crypto)
STOCK_RT = 0.50           # % round trip (0.2 US, 0.7 UK with stamp duty)
TURN = 0.10               # vol/mcap stand-in for the breakout score (not in klines)
MIN_VOL_USD = float((RP.get("crypto") or {}).get("min_vol_usd", 5e6))


def _btc_regime(btc):
    """{open_ms: regime 0-1} with the DAY formula on BTC 1h and 4h returns."""
    out = {}
    cl = [r[4] for r in btc]
    for i, r in enumerate(btc):
        if i < 16:
            continue
        r1, r4 = cl[i] / cl[i - 4] - 1, cl[i] / cl[i - 16] - 1
        out[r[0]] = max(0.0, min(1.0, 0.5 + r1 * 100 * 0.3 + r4 * 100 / 3.0 * 0.2))
    return out


def _exit(bars, i, fee, hold_bars=None):
    """Walk forward from the bar after entry. Returns (exit index, exit price, reason)."""
    hold = HOLD_BARS if hold_bars is None else hold_bars
    px = bars[i][4]
    E = px * (1 + fee)
    stop_px = E * (1 - STOP) / (1 - fee)
    arm_px = E * (1 + ARM) / (1 - fee)
    pk = px
    end = min(len(bars) - 1, i + hold)
    for j in range(i + 1, end + 1):
        _t, o, h, l, c, _v = bars[j]
        armed = pk >= arm_px
        if o <= stop_px:
            return j, o, "stop"
        if armed and o <= pk * (1 - TRAIL):
            return j, o, "trail"
        if l <= stop_px:
            return j, stop_px, "stop"
        if armed and l <= pk * (1 - TRAIL):
            return j, pk * (1 - TRAIL), "trail"
        pk = max(pk, h)
        if pk >= arm_px and c <= pk * (1 - TRAIL):
            return j, c, "trail"
        if j >= i + hold:
            return j, c, "max hold"
    return None, None, "open at end"


def replay_coin(args):
    path, btc_reg = args
    a = Path(path).stem
    bars = json.load(open(path))
    if len(bars) < 800:
        return a, []
    fee = CRYPTO_RT / 200.0
    dts = [datetime.fromtimestamp(r[0] / 1000 + 900, tz=timezone.utc) for r in bars]   # bar close time
    cl = [r[4] for r in bars]
    hi = [r[2] for r in bars]
    qv = [r[5] for r in bars]
    out, busy_until = [], -1
    for i in range(672, len(bars) - 1):
        if i <= busy_until:
            continue
        p = cl[i]
        c24, c7, c1 = p / cl[i - 96] - 1, p / cl[i - 672] - 1, p / cl[i - 4] - 1
        v24 = sum(qv[i - 95:i + 1])
        if v24 < MIN_VOL_USD:
            continue
        h36 = max(cl[i - 144:i - 5])
        maybe_dip = h36 > 0 and (h36 - min(cl[i - 144:i + 1])) / h36 * 100 >= float(RP["dip"]["pullback_min_pct"])
        maybe_brk = c24 >= 0.04
        if not (maybe_dip or maybe_brk):
            continue
        reg = btc_reg.get(bars[i][0], 0.5)
        hv = sum(qv[i - 3:i + 1]); avg_h = v24 / 24.0
        vol = hv / avg_h if avg_h > 0 else None
        pts = list(zip(dts[i - 287:i + 1], cl[i - 287:i + 1]))
        cands = []
        if maybe_brk:
            x = dict(current_price=p, high_24h=max(hi[i - 95:i + 1]), price_change_percentage_1h_in_currency=c1 * 100,
                     price_change_percentage_24h_in_currency=c24 * 100, price_change_percentage_7d_in_currency=c7 * 100,
                     total_volume=TURN, market_cap=1.0)
            sc, ok, _why = riskpot.crypto_score(x)
            if ok:
                m, _info, _rs = momentum(pts, RP.get("min_history_h", 2))
                run_why, scale, _nl = riskpot.runup_adjust(c7 * 100, RP)
                if m is not None and m >= float(RP["confirm_momentum"]) and not run_why:
                    conf, _parts = riskpot.confidence(RP, sc * scale, vol, None, reg, 0.5, freshness=1.0)
                    cands.append(("breakout", conf))
        if maybe_dip:
            d = riskpot.dip(pts, RP, sent=None, regime=reg, hr=0.5, freshness=1.0, vol=vol, atr=None)
            if d.get("ok"):
                cands.append(("dip", d["conf"]))
        cands = [x for x in cands if x[1] >= THR]
        if not cands:
            continue
        setup, conf = max(cands, key=lambda x: x[1]) if len(cands) == 2 and cands[1][1] > cands[0][1] else cands[0]
        j, xp, why = _exit(bars, i, fee)
        if j is None:
            break
        gross = (xp / p - 1) * 100
        net = (xp * (1 - fee) / (p * (1 + fee)) - 1) * 100
        out.append(dict(a=a, klass="crypto", setup=setup, t=dts[i].isoformat(), conf=round(conf, 3),
                        gross=round(gross, 3), net=round(net, 3), why=why, hold_h=round((j - i) / 4, 2)))
        busy_until = j + COOL_BARS
    # Control: no setup at all. Buy the same coin once a day at a fixed time (same volume floor) with the
    # same exits. If the setups do no better than this, the entry rules add nothing over the market.
    for i in range(672, len(bars) - 1, 96):
        if sum(qv[i - 95:i + 1]) < MIN_VOL_USD:
            continue
        j, xp, why = _exit(bars, i, fee)
        if j is None:
            break
        p = cl[i]
        out.append(dict(a=a, klass="crypto", setup="control", t=dts[i].isoformat(), conf=None,
                        gross=round((xp / p - 1) * 100, 3), net=round((xp * (1 - fee) / (p * (1 + fee)) - 1) * 100, 3),
                        why=why, hold_h=round((j - i) / 4, 2)))
    return a, out


class _Con:
    def __init__(self, closes): self.closes = closes
    def execute(self, _sql, _args): return [(x,) for x in self.closes[::-1][:70]]


def replay_stocks(sdir, names):
    """Breakout only, on daily bars (Yahoo, split-adjusted). Entry at the close where riskpot.stock_score is ok;
    exits on later daily lows/highs with the same stop and trail; the 7-day hold is 5 trading days."""
    fee = STOCK_RT / 200.0
    out = []
    for a in names:
        f = Path(sdir) / f"{a}.json"
        if not f.exists():
            continue
        bars = json.load(open(f))
        closes = [r[4] for r in bars]
        busy = -1
        for i in range(55, len(bars) - 1):
            if i <= busy:
                continue
            sc, ok, _w = riskpot.stock_score(_Con(closes[:i + 1]), a, closes[i])
            if sc is None or not ok:
                continue
            conf, _parts = riskpot.confidence(RP, sc, None, None, 0.5, 0.5, freshness=1.0)
            j, xp, why = _exit(bars, i, fee, hold_bars=5)
            if j is None:
                break
            p = closes[i]
            out.append(dict(a=a, klass="stock", setup="breakout",
                            t=datetime.fromtimestamp(bars[i][0] / 1000, tz=timezone.utc).date().isoformat(),
                            conf=round(conf, 3), gross=round((xp / p - 1) * 100, 3),
                            net=round((xp * (1 - fee) / (p * (1 + fee)) - 1) * 100, 3), why=why, gated=conf >= THR,
                            hold_d=j - i))
            busy = j + 1
        for i in range(55, len(bars) - 1, 5):
            j, xp, why = _exit(bars, i, fee, hold_bars=5)
            if j is None:
                break
            p = closes[i]
            out.append(dict(a=a, klass="stock", setup="control",
                            t=datetime.fromtimestamp(bars[i][0] / 1000, tz=timezone.utc).date().isoformat(),
                            conf=None, gross=round((xp / p - 1) * 100, 3),
                            net=round((xp * (1 - fee) / (p * (1 + fee)) - 1) * 100, 3), why=why, gated=False, hold_d=j - i))
    return out


def stats(tr, rt):
    if not tr:
        return dict(n=0)
    wins = [x for x in tr if x["net"] > 0]
    loss = [x for x in tr if x["net"] <= 0]
    p = len(wins) / len(tr)
    aw = sum(x["gross"] for x in wins) / len(wins) if wins else 0.0
    al = -sum(x["gross"] for x in loss) / len(loss) if loss else 0.0
    return dict(n=len(tr), p=round(p, 3), win=round(aw, 2), loss=round(al, 2), rt=rt,
                ev=round(p * aw - (1 - p) * al - rt, 3), mean_net=round(sum(x["net"] for x in tr) / len(tr), 3),
                med_net=round(sorted(x["net"] for x in tr)[len(tr) // 2], 3),
                coins=len({x["a"] for x in tr}))


def main(argv):
    kdir, out_path = Path(argv[1]), Path(argv[2])
    split = "2026-01-01"; workers = 6
    if "--split" in argv: split = argv[argv.index("--split") + 1]
    if "--workers" in argv: workers = int(argv[argv.index("--workers") + 1])
    btc = json.load(open(kdir / "BTC.json"))
    reg = _btc_regime(btc)
    files = sorted(str(f) for f in kdir.glob("*.json") if f.stem != "BTC")
    trades = []
    with Pool(workers) as pool:
        for a, tr in pool.imap_unordered(replay_coin, [(f, reg) for f in files]):
            trades += tr
            print(a, len(tr), flush=True)
    U = json.load(open(argv[argv.index("--universe") + 1])) if "--universe" in argv else {}
    sdir = argv[argv.index("--stocks") + 1] if "--stocks" in argv else None
    stocks = replay_stocks(sdir, U.get("stocks") or []) if sdir else []
    res = dict(split=split, crypto_rt=CRYPTO_RT, stock_rt=STOCK_RT, summary={})
    for setup in ("breakout", "dip", "control"):
        tr = [x for x in trades if x["setup"] == setup]
        res["summary"][f"crypto_{setup}"] = dict(
            all=stats(tr, CRYPTO_RT), insample=stats([x for x in tr if x["t"] < split], CRYPTO_RT),
            heldout=stats([x for x in tr if x["t"] >= split], CRYPTO_RT))
    sc = [x for x in stocks if x["setup"] == "control"]
    res["summary"]["stock_control"] = dict(
        all=stats(sc, STOCK_RT), insample=stats([x for x in sc if x["t"] < split], STOCK_RT),
        heldout=stats([x for x in sc if x["t"] >= split], STOCK_RT))
    stocks_b = [x for x in stocks if x["setup"] == "breakout"]
    for gated in (True, False):
        tr = [x for x in stocks_b if x["gated"] or not gated]
        res["summary"]["stock_breakout" + ("" if gated else "_ungated")] = dict(
            all=stats(tr, STOCK_RT), insample=stats([x for x in tr if x["t"] < split], STOCK_RT),
            heldout=stats([x for x in tr if x["t"] >= split], STOCK_RT))
    res["trades"] = trades + stocks
    out_path.write_text(json.dumps(res, indent=1))
    print(json.dumps(res["summary"], indent=1))


if __name__ == "__main__":
    main(sys.argv)
