"""One shared history for every signal book.

The seed in signaldata/history.json is the backfill. A newer point, when a
refresh succeeds, is kept in data/signal_cache.json. Both are read once.
data/signal_live.json is today's decision for all seventeen books, written
before the variant children start. A child reads that file and does not fetch.

A refresh runs only when a series is more than a day behind, once that day,
and it stops after a few seconds. The trading cycle does not score news. That
stays on the hourly timer.
"""
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import DATA, LON, ROOT

SEED = ROOT / "signaldata" / "history.json"
CACHE = DATA / "signal_cache.json"
LIVE = DATA / "signal_live.json"
ATTEMPTS = DATA / "signal_refresh.json"
KEYS = ("funding", "perp", "dvol", "supply", "nasdaq", "dxy", "gold", "addresses", "tone", "dominance")
# One pass, then stop. A missed source keeps yesterday's print. The rules already lag a day.
REFRESH_BUDGET_S = 8.0
UA = {"User-Agent": "paper-signal/1.0"}


def _load(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _points(raw, key):
    rows = raw.get(key) if isinstance(raw, dict) else None
    out = []
    for row in rows or []:
        try:
            out.append([str(row[0])[:10], float(row[1])])
        except (TypeError, ValueError, IndexError):
            continue
    out.sort()
    return out


def _merge(base, extra):
    by = {day: val for day, val in base}
    for day, val in extra:
        by[day] = val
    return [[day, by[day]] for day in sorted(by)]


def load_aux():
    """Seed plus any newer points. No network."""
    seed = _load(SEED)
    extra = _load(CACHE)
    return {key: _merge(_points(seed, key), _points(extra, key)) for key in KEYS}


def _today(today=None):
    if today is not None:
        return today if hasattr(today, "isoformat") and not isinstance(today, str) else datetime.fromisoformat(str(today)[:10]).date()
    import common
    return common.now().astimezone(LON).date()


def _offline():
    return bool(os.environ.get("TRADING_OFFLINE") or os.environ.get("SIGNAL_OFFLINE"))


def _get(url, timeout):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ms(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _day_ms(ms):
    return datetime.fromtimestamp(int(ms) / 1000.0, timezone.utc).date().isoformat()


def _funding(start):
    url = ("https://www.deribit.com/api/v2/public/get_funding_rate_history"
           f"?instrument_name=BTC-PERPETUAL&start_timestamp={_ms(start)}&end_timestamp={int(time.time()*1000)}")
    rows = _get(url, 2.5).get("result") or []
    daily = {}
    for row in rows:
        rate = row.get("interest_8h")
        mult = 3.0 * 365.0
        if rate is None:
            rate = row.get("interest_1h")
            mult = 24.0 * 365.0
        if rate is None or row.get("timestamp") is None:
            continue
        daily[_day_ms(row["timestamp"])] = float(rate) * mult
    return [[d, daily[d]] for d in sorted(daily) if d > start]


def _dvol(start):
    url = ("https://www.deribit.com/api/v2/public/get_volatility_index_data"
           f"?currency=BTC&start_timestamp={_ms(start)}&end_timestamp={int(time.time()*1000)}&resolution=86400")
    rows = (_get(url, 2.5).get("result") or {}).get("data") or []
    daily = {}
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            daily[_day_ms(row[0])] = float(row[4])
    return [[d, daily[d]] for d in sorted(daily) if d > start]


def _perp(start):
    url = ("https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
           f"?instrument_name=BTC-PERPETUAL&start_timestamp={_ms(start)}"
           f"&end_timestamp={int(time.time()*1000)}&resolution=1D")
    result = _get(url, 2.5).get("result") or {}
    out = []
    for tick, close in zip(result.get("ticks") or [], result.get("close") or []):
        if close and close > 0:
            day = _day_ms(tick)
            if day > start:
                out.append([day, float(close)])
    return out


def _yahoo(symbol, start):
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?period1={p1}&period2={int(time.time())}&interval=1d")
    result = (_get(url, 2.5).get("chart") or {}).get("result") or [{}]
    block = result[0] if result else {}
    stamps = block.get("timestamp") or []
    closes = ((block.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat()
        if day > start:
            out.append([day, float(close)])
    return out


def _stables(start):
    rows = _get("https://stablecoins.llama.fi/stablecoincharts/all", 2.5)
    out = []
    for row in rows or []:
        try:
            day = datetime.fromtimestamp(int(row["date"]), timezone.utc).date().isoformat()
            usd = float((row.get("totalCirculating") or {}).get("peggedUSD"))
        except (TypeError, ValueError, KeyError):
            continue
        if day > start and usd > 0:
            out.append([day, usd])
    return out


def _addresses(start):
    data = _get("https://api.blockchain.info/charts/n-unique-addresses?timespan=30days&format=json&sampled=true", 2.5)
    out = []
    for row in data.get("values") or []:
        try:
            day = datetime.fromtimestamp(int(row["x"]), timezone.utc).date().isoformat()
            val = float(row["y"])
        except (TypeError, ValueError, KeyError):
            continue
        if day > start:
            out.append([day, val])
    return out


def _dominance(start):
    end = int(time.time())
    begin = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    url = ("https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/historical"
           f"?format=chart&interval=1d&timeStart={begin}&timeEnd={end}")
    data = _get(url, 2.5)
    quotes = ((data.get("data") or {}).get("quotes") or [])
    out = []
    for row in quotes:
        try:
            day = str(row.get("timestamp") or "")[:10]
            val = float(((row.get("quote") or {}).get("2781") or row).get("btcDominance")
                        or row.get("btcDominance"))
        except (TypeError, ValueError, AttributeError):
            continue
        if len(day) == 10 and day > start:
            out.append([day, val])
    return out


def _tone(start):
    begin = start.replace("-", "") + "000000"
    end = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    q = urllib.parse.urlencode({
        "query": "bitcoin sourcelang:english",
        "mode": "timelinetone",
        "format": "json",
        "startdatetime": begin,
        "enddatetime": end,
    })
    data = _get("https://api.gdeltproject.org/api/v2/doc/doc?" + q, 2.5)
    timeline = ((data.get("timeline") or [{}])[0].get("data") or [])
    out = []
    for row in timeline:
        raw = str(row.get("date") or "")
        if len(raw) < 8 or row.get("value") is None:
            continue
        day = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        if day > start:
            out.append([day, float(row["value"])])
    return out


FETCH = {
    "funding": _funding,
    "perp": _perp,
    "dvol": _dvol,
    "supply": _stables,
    "nasdaq": lambda start: _yahoo("^IXIC", start),
    "dxy": lambda start: _yahoo("DX-Y.NYB", start),
    "gold": lambda start: _yahoo("GC=F", start),
    "addresses": _addresses,
    "tone": _tone,
    "dominance": _dominance,
}


def refresh(aux, today=None):
    """Append points newer than the cache. Returns (aux, how many series changed).

    Fresh series, and an offline process, make no requests. A source is asked
    once per day. A failure leaves the last print in place.
    """
    if _offline():
        return aux, 0
    today = _today(today)
    today_s = today.isoformat()
    stale_before = (today - timedelta(days=1)).isoformat()
    stale = [key for key in KEYS if not aux.get(key) or aux[key][-1][0] < stale_before]
    if not stale:
        return aux, 0
    tried = _load(ATTEMPTS)
    pending = [key for key in stale if tried.get(key) != today_s]
    if not pending:
        return aux, 0
    deadline = time.perf_counter() + REFRESH_BUDGET_S
    changed = {}
    for key in pending:
        if time.perf_counter() > deadline:
            break
        tried[key] = today_s
        start = aux[key][-1][0] if aux.get(key) else "2014-01-01"
        try:
            extra = FETCH[key](start) or []
        except Exception:
            extra = []
        if extra:
            aux[key] = _merge(aux[key], extra)
            changed[key] = extra
    ATTEMPTS.parent.mkdir(parents=True, exist_ok=True)
    ATTEMPTS.write_text(json.dumps({k: tried[k] for k in KEYS if k in tried}, separators=(",", ":")))
    if changed:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        # Only the new points. The seed stays in the repo and is not rewritten.
        prior = _load(CACHE)
        for key, extra in changed.items():
            prior[key] = _merge(_points(prior, key), extra)
        CACHE.write_text(json.dumps(prior, separators=(",", ":")))
    return aux, len(changed)


def _closes_from(con):
    out = {}
    if con is None:
        return out
    for asset in ("BTC", "VUSA", "VUSA.L", "EQQQ", "EQQQ.L"):
        try:
            rows = con.execute(
                "SELECT d, close FROM daily_closes WHERE asset=? AND close>0 ORDER BY d", (asset,)).fetchall()
        except sqlite3.Error:
            rows = []
        if rows:
            out[asset] = [(str(d)[:10], float(p)) for d, p in rows]
    return out


def read_closes():
    import common
    path = Path(common.DB)
    if not path.exists():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return _closes_from(con)
    finally:
        con.close()


def decide(closes, aux, today=None, only=None):
    """Today's composite or regime. No network. only limits the work to one book."""
    import signalbooks
    import signalcomb
    import signalhist
    today = _today(today).isoformat()
    books = {}
    tilt = signalbooks.TILT if only is None else {only: signalbooks.TILT[only]} if only in signalbooks.TILT else {}
    regime_names = signalbooks.REGIME if only is None else (
        {only: signalbooks.REGIME[only]} if only in signalbooks.REGIME else {})
    for name, mode in tilt.items():
        snap = signalcomb.book_snapshot(closes, None, mode=mode) if len(closes.get("BTC") or []) >= 80 else {}
        comp = snap.get("composite") or {}
        btc = comp.get("BTC")
        books[name] = dict(signal_kind="tilt", composite=comp,
                           note="tilt" if btc is None else f"tilt {btc:+.2f}")
    carry = {key: aux.get(key) or [] for key in KEYS}
    for name, kind in regime_names.items():
        series = signalhist.book_regime(closes, carry, kind)
        row = None
        for day, mapped in series.items():
            if day <= today and (row is None or day >= row[0]):
                row = (day, mapped)
        regime = row[1] if row else {}
        bits = []
        if regime.get("BTC"):
            bits.append("BTC")
        for key in ("VUSA", "EQQQ"):
            if regime.get(key):
                bits.append(key)
        books[name] = dict(signal_kind="regime", regime=regime, note=" ".join(bits) if bits else "cash")
    return books


def prepare(closes=None, today=None):
    """Refresh if needed, then write today's decisions. Returns seconds spent."""
    t0 = time.perf_counter()
    aux = load_aux()
    aux, n = refresh(aux, today)
    if closes is None:
        closes = read_closes()
    day = _today(today).isoformat()
    books = decide(closes, aux, today)
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    LIVE.write_text(json.dumps(dict(day=day, books=books), separators=(",", ":")))
    dt = time.perf_counter() - t0
    print(f"[signals] prepared {len(books)} books in {dt:.2f}s, refreshed {n}")
    return dt


def row_for(book, closes=None):
    """The decision a child applies. Reads the shared file. Does not fetch.

    A missing file falls back to the seed already on disk, still with no network.
    """
    import signalbooks
    today = _today().isoformat()
    try:
        payload = json.loads(LIVE.read_text())
    except (OSError, json.JSONDecodeError):
        payload = {}
    if payload.get("day") == today and book in (payload.get("books") or {}):
        return payload["books"][book]
    kind, _mode = signalbooks.kind(book)
    if kind is None:
        return {}
    # The child is offline. Compute this one book from the seed.
    os.environ.setdefault("SIGNAL_OFFLINE", "1")
    aux = load_aux()
    if closes is None:
        closes = {}
    return decide(closes, aux, only=book).get(book) or {}
