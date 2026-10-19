"""Per-asset composite from signals that have kept an edge out of sample.

Each signal is coded so that a higher number is the bullish reading. An older window
and a newer window both have to agree on the sign of its rank correlation with the
next five daily returns, and both have to clear a small bar. A kept signal then gets
the same weight as every other kept signal: the size of the correlation is too noisy
to use as a bet size. A correlation that is suspiciously perfect is dropped: that is
leakage or a one-off, not an edge. A signal with no history, or no agreement, gets
weight zero and does not tilt the book.

Nothing here fetches the network. News is read from the hourly file. Funding and insider
marks are read from those books' own databases when they already exist.
"""
import math
import sqlite3
from pathlib import Path

from common import DATA

HORIZON = 5
IC_MIN = 0.03
IC_CAP = 0.50
TRAIN_N = 30
CONFIRM_N = 15
SIGNALS = ("news", "routine", "funding", "unlock", "insider", "trend", "momentum", "vol", "forecast")
LABELS = {
    "news": "News",
    "routine": "Routine file",
    "funding": "Funding",
    "unlock": "Unlocks",
    "insider": "Insider buys",
    "trend": "Trend",
    "momentum": "Momentum",
    "vol": "Volatility",
    "forecast": "5-day forecast",
}
BLURB = {
    "news": "Mean finance-lexicon score of headlines in the hourly news file.",
    "routine": "The sentiment file the cycle already ingests, from -1 to 1.",
    "funding": "Low BTC perpetual funding is the bullish reading. High funding is negative.",
    "unlock": "No large unlock is the bullish reading. A token unlocking supply is negative.",
    "insider": "An opportunistic open-market insider buy in the last six months.",
    "trend": "1 when the close is above its 50-day average, otherwise -1.",
    "momentum": "The last 28-day return, shrunk so a 20% move is the full score.",
    "vol": "Calm volatility is bullish. A realised vol of 40% is neutral and 80% is -1.",
    "forecast": "Half of the last 5-day return, shrunk. Kept only when that has predicted the next week.",
}


def clip(value, lo=-1.0, hi=1.0):
    return max(lo, min(hi, float(value)))


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mid = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = mid
        i = j + 1
    return ranks


def spearman(xs, ys):
    """(correlation, n). None when the two series do not both vary, or there are fewer than 8 pairs."""
    pairs = [(float(a), float(b)) for a, b in zip(xs, ys) if a is not None and b is not None]
    n = len(pairs)
    if n < 8:
        return None, n
    rx, ry = _ranks([a for a, _ in pairs]), _ranks([b for _, b in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx < 1e-12 or dy < 1e-12:
        return None, n
    return num / (dx * dy), n


def decide_weight(train_ic, confirm_ic, train_n, confirm_n, ic_min=IC_MIN, ic_cap=IC_CAP):
    """(weight, kept). Both windows must agree. A kept signal gets weight +1 or -1.

    Equal weights beat fitted magnitudes out of sample when the correlations
    themselves are noisy (DeMiguel, Garlappi and Uppal, 2009). The composite
    then averages the kept signals. A perfect correlation is still rejected.
    """
    if train_ic is None or confirm_ic is None:
        return 0.0, False
    if train_n < TRAIN_N or confirm_n < CONFIRM_N:
        return 0.0, False
    if train_ic * confirm_ic <= 0:
        return 0.0, False
    if abs(train_ic) < ic_min or abs(confirm_ic) < ic_min:
        return 0.0, False
    if abs(train_ic) > ic_cap or abs(confirm_ic) > ic_cap:
        return 0.0, False
    return math.copysign(1.0, confirm_ic), True


def _vol(prices, end, n=30):
    if end + 1 < n + 1:
        return None
    window = prices[end - n:end + 1]
    rets = []
    for a, b in zip(window, window[1:]):
        if a <= 0:
            return None
        rets.append(b / a - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365.0)


def price_signal(kind, prices, i):
    """Bullish reading at index i, or None when the window is not long enough."""
    px = prices[i]
    if kind == "trend":
        if i + 1 < 50:
            return None
        avg = sum(prices[i + 1 - 50:i + 1]) / 50.0
        return 1.0 if px > avg else -1.0
    if kind == "momentum":
        if i < 28 or prices[i - 28] <= 0:
            return None
        return clip((px / prices[i - 28] - 1.0) / 0.20)
    if kind == "vol":
        vol = _vol(prices, i)
        if vol is None:
            return None
        return clip((0.40 - vol) / 0.40)
    if kind == "forecast":
        if i < 5 or prices[i - 5] <= 0:
            return None
        return clip(0.5 * (px / prices[i - 5] - 1.0) / 0.05)
    return None


def _asof_map(pairs):
    """day -> latest value on or before that day. pairs is sorted (day, value)."""
    return list(pairs or [])


def value_on(pairs, day):
    if not pairs:
        return None
    best = None
    for d, value in pairs:
        if d <= day:
            best = value
        else:
            break
    return best


def funding_reading(ann):
    """10% a year is neutral. 30% is -1. Zero funding is mildly bullish."""
    if ann is None:
        return None
    return clip((0.10 - float(ann)) / 0.20)


def forward(prices, i, horizon=HORIZON):
    j = i + horizon
    if j >= len(prices) or prices[i] <= 0:
        return None
    return prices[j] / prices[i] - 1.0


def _split_weight(signals, fwds):
    """Weight from samples whose forward return is already known. The newest 30% confirms the older 70%."""
    idx = [i for i, (s, f) in enumerate(zip(signals, fwds)) if s is not None and f is not None]
    if len(idx) < TRAIN_N + CONFIRM_N:
        ic, n = spearman([signals[i] for i in idx], [fwds[i] for i in idx])
        return 0.0, False, ic, n, None, 0
    cut = int(len(idx) * 0.70)
    train, confirm = idx[:cut], idx[cut:]
    tr, ntr = spearman([signals[i] for i in train], [fwds[i] for i in train])
    cf, ncf = spearman([signals[i] for i in confirm], [fwds[i] for i in confirm])
    weight, kept = decide_weight(tr, cf, ntr, ncf)
    return weight, kept, tr, ntr, cf, ncf


def measure_series(signals, prices):
    """One signal against one price path. samples stop where the forward week is not known yet."""
    fwds = [forward(prices, i) for i in range(len(prices))]
    # The last HORIZON points have no completed forward return. They stay out of the fit.
    return _split_weight(signals, fwds)


def composite(values, weights):
    """Weighted mean of the signals that have a weight. No kept signal means 0, which is no tilt."""
    num = den = 0.0
    for name, weight in weights.items():
        if not weight:
            continue
        value = values.get(name)
        if value is None:
            continue
        num += float(weight) * float(value)
        den += abs(float(weight))
    if den <= 0:
        return 0.0
    return clip(num / den)


def tilt(base, score):
    """Vol-sized exposure, nudged by the composite. Cash stays cash. The result stays in 0 to 1.

    A score of 0 leaves the vol weight unchanged. +1 adds half of that weight, capped at 1.
    -1 cuts it in half. There is no short.
    """
    base = float(base or 0)
    if base <= 0:
        return 0.0
    score = 0.0 if score is None else clip(score)
    return min(1.0, base * (1.0 + 0.5 * score))


def _price_rows(prices):
    """Aligned signal columns for the four price features. Each is a list the same length as prices."""
    out = {name: [] for name in ("trend", "momentum", "vol", "forecast")}
    for i in range(len(prices)):
        for name in out:
            out[name].append(price_signal(name, prices, i))
    return out


def _align(pairs, days):
    if not pairs:
        return None
    pairs = sorted(pairs)
    return [value_on(pairs, day) for day in days]


def edges_for(days, prices, extras=None):
    """Rows for one asset. extras maps a signal name to [(day, value), ...]."""
    extras = extras or {}
    cols = _price_rows(prices)
    for name in ("news", "routine", "funding", "unlock", "insider"):
        aligned = _align(extras.get(name), days)
        cols[name] = aligned if aligned is not None else [None] * len(days)
    rows = []
    weights = {}
    latest = {}
    for name in SIGNALS:
        series = cols[name]
        weight, kept, train, ntr, confirm, ncf = measure_series(series, prices)
        weights[name] = weight
        latest[name] = series[-1] if series else None
        rows.append(dict(signal=name, label=LABELS[name], blurb=BLURB[name], value=latest[name],
                         train_ic=train, confirm_ic=confirm, n=ntr, confirm_n=ncf, kept=kept, weight=weight))
    return rows, weights, composite(latest, weights)


def composites_by_day(closes, assets):
    """{day: {asset: composite}} using only information that was knowable that day.

    Weights are refit on each decision day from samples whose forward week had already ended.
    Early days, before the windows are long enough, stay at 0 so the book matches vol sizing.
    """
    out = {}
    prepared = {}
    for asset in assets:
        series = closes.get(asset) or []
        if len(series) < 80:
            continue
        days = [d for d, _ in series]
        prices = [p for _, p in series]
        prepared[asset] = (days, prices, _price_rows(prices))
    if not prepared:
        return out
    # Refit once a week (every 5th point) and carry the weights forward. The book itself trades weekly.
    length = max(len(v[0]) for v in prepared.values())
    cache = {asset: {} for asset in prepared}
    for i in range(length):
        for asset, (days, prices, cols) in prepared.items():
            if i >= len(days):
                continue
            if i % 5 == 0 or not cache[asset]:
                weights = {}
                fwds = [forward(prices, j) if j + HORIZON < i else None for j in range(len(prices))]
                for name in ("trend", "momentum", "vol", "forecast"):
                    weight, kept, *_ = _split_weight(cols[name], fwds)
                    weights[name] = weight if kept else 0.0
                cache[asset][i] = weights
            else:
                prev = max(cache[asset])
                weights = cache[asset][prev]
            values = {name: cols[name][i] for name in ("trend", "momentum", "vol", "forecast")}
            out.setdefault(days[i], {})[asset] = composite(values, weights)
    return out


def _read_rows(path, sql, args=()):
    path = Path(path)
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _sentiment_series(con):
    try:
        rows = con.execute("SELECT asset, ts, score FROM sentiment WHERE score IS NOT NULL ORDER BY ts").fetchall()
    except sqlite3.Error:
        return {}
    bucket = {}
    for asset, ts, score in rows:
        bucket.setdefault(asset, {})[str(ts)[:10]] = float(score)
    return {asset: sorted(days.items()) for asset, days in bucket.items()}


def _funding_series():
    path = DATA / "variants" / "trend_carry_unlock.db"
    rows = _read_rows(path, "SELECT value FROM state WHERE key='btc_funding'")
    if not rows or not rows[0][0]:
        return {}
    import json
    try:
        points = json.loads(rows[0][0]).get("points") or []
    except Exception:
        return {}
    series = []
    for row in points:
        try:
            series.append((str(row[0])[:10], funding_reading(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return {"BTC": series} if series else {}


def _insider_series():
    path = DATA / "variants" / "insider_rs_stocks.db"
    rows = _read_rows(path, "SELECT value FROM state WHERE key='insider_cache'")
    if not rows or not rows[0][0]:
        return {}
    import json
    try:
        tickers = (json.loads(rows[0][0]).get("tickers") or {})
    except Exception:
        return {}
    out = {}
    for sym, rec in tickers.items():
        if not rec.get("history_ok"):
            continue
        days = []
        for info in (rec.get("owners") or {}).values():
            days.extend(str(day)[:10] for day in (info.get("buys") or []))
        if len(days) < 3:
            continue
        out[sym] = sorted(set(days))
    return out


def insider_on(buy_days, day):
    """1 when a kept buy falls in the 180 days up to this day."""
    if not buy_days:
        return None
    return 1.0 if any(_within(raw, day, 180) for raw in buy_days) else 0.0


def _within(start, end, limit):
    try:
        from datetime import date
        a, b = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    except ValueError:
        return False
    gap = (b - a).days
    return 0 <= gap <= limit


def gather(con, news=None):
    """Side inputs already on disk. Missing files leave that signal out."""
    import newssent
    extras = {"routine": _sentiment_series(con) if con is not None else {}}
    fund = _funding_series()
    if fund:
        extras["funding"] = fund
    if news is None:
        news = newssent.by_asset()
    if news:
        extras["news"] = news
    insider = _insider_series()
    if insider:
        extras["insider_days"] = insider
    return extras


def _insider_pairs(buy_days, price_days):
    if not buy_days:
        return None
    return [(day, insider_on(buy_days, day)) for day in price_days]


def view_asset(closes, asset, extras):
    series = closes.get(asset) or []
    if len(series) < 80:
        return [], {}, None
    days = [d for d, _ in series]
    prices = [p for _, p in series]
    packed = {}
    for name in ("news", "routine", "funding", "unlock"):
        block = (extras or {}).get(name) or {}
        if asset in block:
            packed[name] = block[asset]
    buys = ((extras or {}).get("insider_days") or {}).get(asset)
    if buys:
        packed["insider"] = _insider_pairs(buys, days)
    rows, weights, score = edges_for(days, prices, packed)
    return rows, weights, score


def dashboard_view(con, closes):
    """What the Signals tab shows. closes is {asset: [(day, price), ...]}. No network."""
    import newssent
    extras = gather(con)
    assets = []
    for name in ("BTC", "VUSA", "VUSA.L", "EQQQ", "EQQQ.L"):
        if len(closes.get(name) or []) >= 80 and name not in assets:
            assets.append(name)
    stock = next((a for a in assets if a != "BTC"), None)
    btc_rows, _, btc_score = view_asset(closes, "BTC", extras) if "BTC" in assets else ([], {}, None)
    st_rows, _, st_score = view_asset(closes, stock, extras) if stock else ([], {}, None)
    by_name = {row["signal"]: row for row in btc_rows}
    stock_by = {row["signal"]: row for row in st_rows}
    rows = []
    for name in SIGNALS:
        b, s = by_name.get(name), stock_by.get(name)
        base = b or s or dict(signal=name, label=LABELS[name], blurb=BLURB[name])
        rows.append(dict(
            signal=name, label=base.get("label") or LABELS[name], blurb=base.get("blurb") or BLURB[name],
            btc=None if not b else b.get("value"), stock=None if not s else s.get("value"),
            ic=None if not b else b.get("confirm_ic"), train_ic=None if not b else b.get("train_ic"),
            kept=bool(b and b.get("kept")), weight=0.0 if not b else b.get("weight") or 0.0,
            stock_ic=None if not s else s.get("confirm_ic"), stock_kept=bool(s and s.get("kept")),
            stock_weight=0.0 if not s else s.get("weight") or 0.0,
            n=0 if not b else b.get("n") or 0,
        ))
    return dict(asset="BTC", stock=stock, composite=btc_score, stock_composite=st_score, rows=rows,
                headlines=newssent.latest(8),
                note=("A signal is used only when an older window and a later window agree on its rank "
                      "correlation with the next five daily returns, and neither correlation is tiny or perfect. "
                      "Each kept signal has the same weight. The sign is the later window's. News is scored "
                      "by a finance lexicon, hourly, outside this cycle."))


def book_snapshot(closes, con):
    """Composite the paper book reads. Same measurement as the Signals tab."""
    view = dashboard_view(con, closes)
    composite_map = {}
    if view.get("composite") is not None and "BTC" in (closes or {}):
        composite_map["BTC"] = view["composite"]
    stock = view.get("stock")
    if stock and view.get("stock_composite") is not None:
        composite_map[stock] = view["stock_composite"]
    kept = [row["signal"] for row in view["rows"] if row.get("kept") or row.get("stock_kept")]
    return dict(composite=composite_map, kept=kept, rows=view["rows"], composite_btc=view.get("composite"),
                stock=stock, stock_composite=view.get("stock_composite"))
