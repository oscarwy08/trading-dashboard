"""Kronos walk-forward replay (evaluation only; nothing here touches live trading).

Steps (run from any directory; data, forecasts and weights stay OUTSIDE the repo):
  fetch     1h klines from the Binance public mirror -> WORK/data/<COIN>.json
            (Kraken public OHLC only serves the last 720 bars; see kraken_check.py for the cross-check)
  forecast  walk-forward Kronos forecasts, fed only bars <= t -> WORK/out/<model>/<COIN>.csv
  daycheck  one forecast at the last closed hour before each Day-pot BUY in data/trading.db
  report    metrics + after-cost long-only test -> markdown on stdout

Needs: torch (CPU), pandas, einops, huggingface_hub, safetensors and a clone of
https://github.com/shiyu-coder/Kronos (KRONOS_SRC). Weights download from Hugging Face.
"""
import argparse, json, os, sys, time, math, random, resource
from pathlib import Path

WORK = Path(os.environ.get("KRONOS_WORK", "/workspace/kronos_eval"))
KRONOS_SRC = os.environ.get("KRONOS_SRC", "/workspace/Kronos")
MODELS = {"mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k", 2048),
          "small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base", 512)}
COLS = ["open", "high", "low", "close", "volume", "amount"]


def load(coin):
    import pandas as pd
    d = json.loads((WORK / "data" / f"{coin}.json").read_text())
    df = pd.DataFrame(d, columns=["t"] + COLS)
    df["ts"] = pd.to_datetime(df["t"], unit="ms")
    return df


def predictor(model, threads):
    sys.path.insert(0, KRONOS_SRC)
    import torch
    torch.set_num_threads(threads)
    from model import Kronos, KronosTokenizer, KronosPredictor
    name, tok, ctx = MODELS[model]
    return KronosPredictor(Kronos.from_pretrained(name), KronosTokenizer.from_pretrained(tok), device="cpu", max_context=ctx)


def forecast_at(p, df, idxs, lookback, horizon, samples, batch=8):
    """Forecast closes for bars i+1..i+horizon using only bars i-lookback+1..i."""
    import pandas as pd, torch
    out = []
    for k in range(0, len(idxs), batch):
        chunk = idxs[k:k + batch]
        xs = [df.iloc[i - lookback + 1:i + 1].reset_index(drop=True) for i in chunk]
        ys = [pd.Series(pd.date_range(x["ts"].iloc[-1] + pd.Timedelta(hours=1), periods=horizon, freq="h")) for x in xs]
        with torch.no_grad():
            preds = p.predict_batch([x[COLS] for x in xs], [x["ts"] for x in xs], ys, pred_len=horizon,
                                    T=1.0, top_p=0.9, sample_count=samples, verbose=False)
        for i, pr in zip(chunk, preds):
            out.append((i, [float(v) for v in pr["close"].values]))
    return out


def cmd_fetch(a):
    import requests
    (WORK / "data").mkdir(parents=True, exist_ok=True)
    end = int(time.time() // 3600 * 3600 * 1000)
    start = end - a.days * 24 * 3600 * 1000
    for c in a.coins.split(","):
        rows, t = [], start
        while t < end:
            r = requests.get("https://data-api.binance.vision/api/v3/klines",
                             params=dict(symbol=c + "USDT", interval="1h", startTime=t, limit=1000), timeout=20)
            if r.status_code != 200 or not r.json():
                break
            d = r.json(); rows += d; t = d[-1][0] + 3600_000; time.sleep(0.2)
        if rows:
            (WORK / "data" / f"{c}.json").write_text(json.dumps(
                [[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[7])] for x in rows]))
        print(c, len(rows), flush=True)


def cmd_forecast(a):
    out = WORK / "out" / a.model; out.mkdir(parents=True, exist_ok=True)
    p = predictor(a.model, a.threads)
    for coin in a.coins.split(","):
        f = out / f"{coin}.csv"
        if f.exists():
            continue
        df = load(coin)
        # fixed UTC grid (every STEP hours) so coins line up for the portfolio test
        idxs = [i for i in range(a.lookback - 1, len(df) - a.horizon)
                if (int(df["t"].iloc[i]) // 3600000) % a.step == 0]
        t0 = time.process_time(); w0 = time.time()
        rows = forecast_at(p, df, idxs, a.lookback, a.horizon, a.samples)
        cpu = time.process_time() - t0
        with open(str(f) + ".tmp", "w") as fh:
            fh.write("t,close," + ",".join(f"f{h+1}" for h in range(a.horizon)) + "," + ",".join(f"c{h+1}" for h in range(a.horizon)) + "\n")
            for i, pr in rows:
                real = [float(df["close"].iloc[i + h + 1]) for h in range(a.horizon)]
                fh.write(f"{int(df['t'].iloc[i])},{float(df['close'].iloc[i])}," + ",".join(f"{v:.8g}" for v in pr) + "," + ",".join(f"{v:.8g}" for v in real) + "\n")
        os.replace(str(f) + ".tmp", f)
        print(json.dumps(dict(coin=coin, n=len(rows), cpu_s_per_forecast=round(cpu / max(1, len(rows)), 3),
                              wall_s=round(time.time() - w0, 1),
                              maxrss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024))), flush=True)


def cmd_daycheck(a):
    """Kronos forecast at the last closed 1h bar before each Day-pot crypto BUY (coins we have candles for)."""
    import sqlite3
    con = sqlite3.connect(a.db)
    tr = con.execute("select id, ts, asset, side, pnl, sub from trades where sleeve='day' and klass='crypto' order by ts, id").fetchall()
    p = predictor(a.model, a.threads)
    open_buys, res = {}, []
    from datetime import datetime
    for tid, ts, asset, side, pnl, sub in tr:
        if side == "BUY":
            open_buys[asset] = (ts, sub)
        elif side == "SELL" and asset in open_buys:
            bts, bsub = open_buys.pop(asset)
            res.append(dict(asset=asset, buy=bts, sell=ts, pnl=pnl, sub=bsub))
    rows = []
    for r in res:
        if not (WORK / "data" / f"{r['asset']}.json").exists():
            continue
        df = load(r["asset"])
        bt = int(datetime.fromisoformat(r["buy"]).timestamp() * 1000)
        i = int((df["t"] + 3600000 <= bt).values.nonzero()[0][-1])  # last bar fully closed before the buy
        if i < a.lookback:
            continue
        (_, pr), = forecast_at(p, df, [i], a.lookback, a.horizon, a.samples)
        c0 = float(df["close"].iloc[i])
        rows.append(dict(r, f1=pr[0] / c0 - 1, f4=pr[min(3, a.horizon - 1)] / c0 - 1))
    print(json.dumps(rows, indent=1))
    (WORK / "out" / f"daycheck_{a.model}.json").write_text(json.dumps(rows, indent=1))


# ---------------------------------------------------------------- report
def _corr(x, y):
    n = len(x)
    if n < 3: return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((v - mx) ** 2 for v in x)); sy = math.sqrt(sum((v - my) ** 2 for v in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx and sy else float("nan")


def _rank(v):
    o = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v)
    for k, i in enumerate(o): r[i] = k
    return r


def load_fc(model, h):
    import csv
    rows = []
    for f in sorted((WORK / "out" / model).glob("*.csv")):
        for d in csv.DictReader(open(f)):
            c0 = float(d["close"])
            rows.append(dict(coin=f.stem, t=int(d["t"]), f=float(d[f"f{h}"]) / c0 - 1, r=float(d[f"c{h}"]) / c0 - 1))
    return rows


def stats(rows):
    if not rows: return {}
    nz = [x for x in rows if x["r"] != 0 and x["f"] != 0]
    hit = sum((x["f"] > 0) == (x["r"] > 0) for x in nz) / len(nz)
    up = sum(x["r"] > 0 for x in nz) / len(nz)
    f = [x["f"] for x in rows]; r = [x["r"] for x in rows]
    # mean per-timestamp cross-sectional rank IC
    by_t = {}
    for x in rows: by_t.setdefault(x["t"], []).append(x)
    ics = [_corr(_rank([x["f"] for x in g]), _rank([x["r"] for x in g])) for g in by_t.values() if len(g) >= 5]
    ics = [v for v in ics if v == v]
    return dict(n=len(rows), hit=hit, base_up=up, corr=_corr(f, r), spearman=_corr(_rank(f), _rank(r)),
                xs_ic=sum(ics) / len(ics) if ics else float("nan"))


def trade_test(rows, thr, cost, seed=7, sims=500):
    """Long-only: at each grid time put equal weight in every coin whose forecast > thr, hold h hours, pay cost round trip.
    Baselines: equal-weight buy-and-hold of all coins over the same span; random = same count of entries per time, random coins."""
    by_t = {}
    for x in rows: by_t.setdefault(x["t"], []).append(x)
    ts = sorted(by_t)
    eq, ntr, wins, nets = 1.0, 0, 0, []
    picks_per_t = []
    for t in ts:
        g = by_t[t]; pick = [x for x in g if x["f"] > thr]
        picks_per_t.append(len(pick))
        if pick:
            net = sum(x["r"] - cost for x in pick) / len(pick)
            eq *= 1 + net; ntr += len(pick); wins += sum(x["r"] > cost for x in pick); nets += [x["r"] - cost for x in pick]
    rnd = random.Random(seed); sim_eq = []
    for _ in range(sims):
        e = 1.0
        for t, k in zip(ts, picks_per_t):
            if k:
                g = rnd.sample(by_t[t], k); e *= 1 + sum(x["r"] - cost for x in g) / k
        sim_eq.append(e)
    sim_eq.sort()
    return dict(trades=ntr, win=wins / ntr if ntr else float("nan"), avg_net=sum(nets) / ntr if ntr else float("nan"),
                total=eq - 1, rnd_mean=sum(sim_eq) / sims - 1, rnd_p95=sim_eq[int(0.95 * sims)] - 1,
                beat_rnd=sum(s < eq for s in sim_eq) / sims)


def bh(model, t0, t1):
    """Equal-weight buy-and-hold of every coin from the first to the last grid close in [t0, t1]."""
    import csv
    rs = []
    for f in sorted((WORK / "out" / model).glob("*.csv")):
        d = [x for x in csv.DictReader(open(f)) if t0 <= int(x["t"]) <= t1]
        if d: rs.append(float(d[-1]["close"]) / float(d[0]["close"]) - 1)
    return sum(rs) / len(rs) if rs else float("nan")


def cmd_report(a):
    from datetime import datetime, timezone
    fmt = lambda t: datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%d %b %Y")
    pct = lambda v: f"{v*100:+.2f}%"
    for h in [int(x) for x in a.horizons.split(",")]:
        rows = load_fc(a.model, h)
        ts = sorted({x["t"] for x in rows}); cut = ts[int(len(ts) * a.is_frac)]
        parts = {"in-sample": [x for x in rows if x["t"] < cut], "out-of-sample": [x for x in rows if x["t"] >= cut]}
        print(f"\n### Kronos-{a.model}, {h}h horizon ({len({x['coin'] for x in rows})} coins, forecast every {a.step}h)\n")
        print("| period | span | forecasts | direction hit | coin went up | corr (pearson) | rank corr | cross-coin rank IC |")
        print("|---|---|---|---|---|---|---|---|")
        for k, rr in parts.items():
            s = stats(rr); tt = [x["t"] for x in rr]
            print(f"| {k} | {fmt(min(tt))} – {fmt(max(tt))} | {s['n']} | {s['hit']*100:.1f}% | {s['base_up']*100:.1f}% | {s['corr']:+.3f} | {s['spearman']:+.3f} | {s['xs_ic']:+.3f} |")
        print(f"\nLong-only, round-trip cost {a.cost*100:.2f}%, hold {h}h, enter when forecast > threshold:\n")
        print("| period | threshold | entries | win after cost | avg net/trade | strategy total | random same-count (mean / 95th pct) | beats random | buy-and-hold (eq-wt) |")
        print("|---|---|---|---|---|---|---|---|---|")
        # pick the threshold on in-sample only, then apply it unchanged out-of-sample
        grid = [a.cost, a.cost * 1.5, a.cost * 2, a.cost * 3]
        best = max(grid, key=lambda th: trade_test(parts["in-sample"], th, a.cost, sims=100)["total"])
        for k, rr in parts.items():
            tt = [x["t"] for x in rr]; b = bh(a.model, min(tt), max(tt))
            for th in sorted({a.cost, best}):
                r = trade_test(rr, th, a.cost)
                tag = " (IS-chosen)" if th == best else ""
                print(f"| {k} | {th*100:.2f}%{tag} | {r['trades']} | {r['win']*100:.1f}% | {pct(r['avg_net'])} | {pct(r['total'])} | {pct(r['rnd_mean'])} / {pct(r['rnd_p95'])} | {r['beat_rnd']*100:.0f}% of sims | {pct(b)} |")


def main():
    ap = argparse.ArgumentParser(); sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch"); f.add_argument("--coins", required=True); f.add_argument("--days", type=int, default=240)
    for n in ("forecast", "daycheck"):
        g = sp.add_parser(n); g.add_argument("--model", default="mini", choices=list(MODELS))
        g.add_argument("--lookback", type=int, default=512); g.add_argument("--horizon", type=int, default=4)
        g.add_argument("--samples", type=int, default=3); g.add_argument("--threads", type=int, default=1)
        if n == "forecast":
            g.add_argument("--coins", required=True); g.add_argument("--step", type=int, default=8)
        else:
            g.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "data" / "trading.db"))
    r = sp.add_parser("report"); r.add_argument("--model", default="mini"); r.add_argument("--horizons", default="1,4")
    r.add_argument("--cost", type=float, default=0.005); r.add_argument("--is-frac", type=float, default=0.5)
    r.add_argument("--step", type=int, default=8)
    a = ap.parse_args(); globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
