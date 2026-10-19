"""Overnight paper books. The main book's rules are not used here.

Each book has its own database and £200, split £100 crypto and £100 stocks.
Positions sit in the trend sleeve, so the 22:00 flatten never sees them.
A decision uses completed daily closes. Crypto history is Kraken's public daily OHLC.

best_of_plus is the exception: 15% of the book is the best_of day and risk engine
(that book holds overnight too) and the other 85% is the BTC / VUSA trend sleeve.
"""
import json
import math
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from common import ACCOUNTS, DATA, LON, ROOT, SCHEMA, cfg, ensure_sentiment_cols, iso, now

PURE = ("btc_trend", "btc_trend_vol", "majors_rotation", "btc_core_satellite", "rs_rotation_veto", "quiet_strength",
        "trend_carry_unlock", "insider_rs_stocks", "signal_engine")
BOOKS = PURE + ("best_of_plus",)
MAJORS = ("BTC", "ETH", "SOL")
STOCK_SYMS = ("VUSA", "VUSA.L", "EQQQ", "EQQQ.L")
# Liquid Kraken USD pairs. This is the top-30 universe for the rotation books.
# Pair names are Kraken's. A symbol that Kraken does not list is skipped.
PAIRS = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD", "ADA": "ADAUSD",
    "DOGE": "XDGUSD", "DOT": "DOTUSD", "AVAX": "AVAXUSD", "LINK": "LINKUSD", "LTC": "XLTCZUSD",
    "BCH": "BCHUSD", "ATOM": "ATOMUSD", "UNI": "UNIUSD", "AAVE": "AAVEUSD", "NEAR": "NEARUSD",
    "ALGO": "ALGOUSD", "FIL": "FILUSD", "ICP": "ICPUSD", "SAND": "SANDUSD", "MANA": "MANAUSD",
    "CRV": "CRVUSD", "GRT": "GRTUSD", "RUNE": "RUNEUSD", "APT": "APTUSD", "ARB": "ARBUSD",
    "OP": "OPUSD", "INJ": "INJUSD", "SUI": "SUIUSD", "PEPE": "PEPEUSD", "TIA": "TIAUSD",
}
HISTORY_FROM = 1483228800  # 2017-01-01 UTC
KRAKEN = "https://api.kraken.com/0/public/OHLC"
LABELS = {
    "btc_trend": "BTC trend",
    "btc_trend_vol": "BTC trend, vol sized",
    "majors_rotation": "Majors rotation",
    "btc_core_satellite": "BTC core + satellite",
    "rs_rotation_veto": "RS rotation, veto",
    "quiet_strength": "Quiet strength",
    "best_of_plus": "Best of plus trend",
    "trend_carry_unlock": "Trend, funding and unlocks",
    "insider_rs_stocks": "Insider relative strength",
    "signal_engine": 'News tone',
}
BLURB = {
    "btc_trend": "Crypto is 100% BTC when the daily close is above its 50-day average, otherwise cash. Stocks is the same rule on VUSA, or EQQQ if VUSA has no history. Checked once a day. Held through nights and weekends.",
    "btc_trend_vol": "The same trend book, sized at min(1, 40% / 30-day realised vol). A weight only moves when it has drifted more than 5 points, so a quiet day does not trade.",
    "majors_rotation": "Once a week, hold the best 28-day return among BTC, ETH and SOL that are above their own 50-day average. Cash when none are. The stock half stays in cash.",
    "btc_core_satellite": "85% BTC, put back when it drifts more than 5 points, plus 15% in one coin that has beaten BTC over 30 and 90 days and is above its 100-day average. Checked weekly. The satellite is held at least 5 days. The stock half stays in cash.",
    "rs_rotation_veto": "Once a week, the top 3 liquid coins by 4-week return minus BTC, among names above their 50-day average. A buzz change of 3 or more, or a 24h jump of 15% or more, is skipped. If none qualify, hold BTC. Cash when BTC itself is below its 50-day average.",
    "quiet_strength": "Names within 10% of their 30-day high, with buzz below the universe median and vol/mcap in the top quartile. Held for days to weeks. Sold on a 12% trail from the peak, or when buzz jumps to 3 or more after a 15% run-up. A 20% giveback trail is available and off.",
    "best_of_plus": "The best_of book (sentiment split, 15-minute entry watch, burst runner) may use only 15% of the £200. The other 85% follows the BTC / VUSA trend. Nothing flattens at 22:00. Core alt picks are off on this book.",
    "trend_carry_unlock": "Once a week, hold the strongest of BTC, ETH and SOL that is above its 50-day average, sized like the vol-target book. Crypto is halved when BTC perpetual funding is in the top tenth of the past year, or at 25% a year or more. An alt with more than 1% of supply unlocking within 30 days is left out. If that feed is missing, only BTC and ETH are held. A resize smaller than 10% of the position is skipped. Stocks follow the vol-sized VUSA trend. Held through nights and weekends. Unlock dates: Data: PatternPlus (https://pattern.plus).",
    "insider_rs_stocks": "Once a month, the stock half holds the strongest Trading 212 US and UK names by six-month return, above a long average. A name in the top half of that ranking is overweighted after an opportunistic open-market insider buy (the same person did not trade that calendar month in an earlier year). Routine filings are ignored. The crypto half stays in cash. Held through nights and weekends.",
    "signal_engine": 'Once a week, crypto is 100% BTC when the previous 7-day average GDELT tone for Bitcoin is positive, otherwise cash. No tone history means cash. Stocks stay cash.',
}


def opts(c=None):
    """Numbers for these books. Missing config keeps the defaults. The main book does not read this."""
    block = ((c or cfg()).get("strategies") or {})
    return dict(
        kraken_fee=float(block.get("kraken_fee_pct", 0.40)) / 100.0,
        stock_fee=float(block.get("stock_fee_pct", 0.15)) / 100.0,
        vol_target=float(block.get("vol_target", 0.40)),
        sma=int(block.get("sma_days", 50)),
        sat_sma=int(block.get("satellite_sma", 100)),
        drift=float(block.get("drift_pts", 5)) / 100.0,
        trail=float(block.get("quiet_trail_pct", 12)) / 100.0,
        giveback=bool(block.get("quiet_giveback", False)),
        crypto=float(block.get("crypto_gbp", 100)),
        stocks=float(block.get("stocks_gbp", 100)),
        min_trade=float(block.get("min_trade_gbp", 5)),
        cap=float(block.get("day_risk_cap_pct", 15)),
        trend_weight=float(block.get("trend_weight", 0.85)),
        funding_hot=float(block.get("carry_funding_pct", 25)) / 100.0,
        unlock_pct=float(block.get("carry_unlock_pct", 1)),
        min_pos_frac=float(block.get("carry_min_pos_frac", 10)) / 100.0,
        insider_window=int(block.get("insider_window_days", 180)),
        insider_batch=int(block.get("insider_per_day", 8)),
    )


def sma(vals, n):
    if len(vals) < n or n <= 0:
        return None
    window = vals[-n:]
    return sum(window) / float(n)


def ret_n(vals, n):
    if len(vals) <= n or n <= 0 or vals[-1 - n] <= 0:
        return None
    return vals[-1] / vals[-1 - n] - 1.0


def realised_vol(vals, n=30):
    """Annualised standard deviation of the last n daily simple returns."""
    if len(vals) < n + 1:
        return None
    window = vals[-(n + 1):]
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


def vol_weight(vals, target=0.40, n=30):
    """min(1, target / realised vol). No history, or a flat series, stays in cash's opposite: full size."""
    v = realised_vol(vals, n)
    if v is None or v <= 1e-8:
        return 1.0
    return min(1.0, float(target) / v)


def _px(series):
    return [p for _, p in series]


def _stock_name(closes, n):
    for sym in STOCK_SYMS:
        if len(closes.get(sym) or []) >= n:
            return sym
    return None


def _above(series, n):
    vals = _px(series)
    avg = sma(vals, n)
    return avg is not None and vals[-1] > avg


def targets_for(book, closes, extra=None):
    """{asset: (account, weight of that account)}. extra carries buzz, mcap, vol and quiet state.

    Weights are the share of the crypto account or the stock account, not of the whole £200.
    A book that does not trade stocks leaves that account as cash.
    """
    extra = extra or {}
    o = extra.get("opts") or opts()
    n = int(o["sma"])
    out = {}
    btc = closes.get("BTC") or []
    if book in ("btc_trend", "btc_trend_vol", "best_of_plus"):
        if _above(btc, n):
            w = 1.0
            if book == "btc_trend_vol":
                w = vol_weight(_px(btc), o["vol_target"])
            out["BTC"] = ("crypto", w)
        stock = _stock_name(closes, n)
        if stock and _above(closes[stock], n):
            w = 1.0
            if book == "btc_trend_vol":
                w = vol_weight(_px(closes[stock]), o["vol_target"])
            out[stock] = ("stocks", w)
        return out
    if book == "majors_rotation":
        best, best_r = None, None
        for sym in MAJORS:
            series = closes.get(sym) or []
            if not _above(series, n):
                continue
            r = ret_n(_px(series), 28)
            if r is None:
                continue
            if best_r is None or r > best_r:
                best, best_r = sym, r
        if best:
            out[best] = ("crypto", 1.0)
        return out
    if book == "btc_core_satellite":
        out["BTC"] = ("crypto", 0.85)
        sat = _satellite(closes, extra.get("held"), o)
        if sat:
            out[sat] = ("crypto", 0.15)
        return out
    if book == "rs_rotation_veto":
        if not _above(btc, n):
            return out
        picks = _rs_picks(closes, extra.get("buzz") or {}, n)
        if not picks:
            out["BTC"] = ("crypto", 1.0)
            return out
        w = 1.0 / len(picks)
        for sym in picks:
            out[sym] = ("crypto", w)
        return out
    if book == "quiet_strength":
        names = _quiet_names(closes, extra)
        if not names:
            return out
        w = 1.0 / len(names)
        for sym in names:
            out[sym] = ("crypto", w)
        return out
    if book == "trend_carry_unlock":
        return _carry_targets(closes, extra, o, n)
    if book == "insider_rs_stocks":
        return _insider_targets(closes, extra)
    if book == "signal_engine":
        return _signal_targets(closes, extra, o, n)
    return out


def _satellite(closes, held, o):
    """One coin beating BTC over 30 and 90 days and above its 100-day average.

    held is (asset, days_held) or None. Inside the minimum 5 days the current
    coin stays even if a better one appeared. After that, the best qualifier wins.
    """
    btc = _px(closes.get("BTC") or [])
    b30, b90 = ret_n(btc, 30), ret_n(btc, 90)
    if b30 is None or b90 is None:
        return held[0] if held and held[1] < 5 else None
    best, best_x = None, None
    span = int(o["sat_sma"])
    for sym, series in closes.items():
        if sym in ("BTC",) or sym in STOCK_SYMS:
            continue
        vals = _px(series)
        if not _above(series, span):
            continue
        r30, r90 = ret_n(vals, 30), ret_n(vals, 90)
        if r30 is None or r90 is None or r30 <= b30 or r90 <= b90:
            continue
        score = r90 - b90
        if best_x is None or score > best_x:
            best, best_x = sym, score
    if held and held[1] < 5:
        return held[0]
    return best


def _rs_picks(closes, buzz, n):
    btc = _px(closes.get("BTC") or [])
    base = ret_n(btc, 28)
    if base is None:
        return []
    ranked = []
    for sym, series in closes.items():
        if sym in STOCK_SYMS:
            continue
        vals = _px(series)
        if not _above(series, n):
            continue
        jump = ret_n(vals, 1)
        if jump is not None and jump >= 0.15:
            continue
        b = buzz.get(sym)
        if b is not None and float(b) >= 3:
            continue
        r = ret_n(vals, 28)
        if r is None:
            continue
        ranked.append((r - base, sym))
    ranked.sort(reverse=True)
    return [sym for _, sym in ranked[:3]]


def _quiet_names(closes, extra):
    """Up to 5 names: near the 30-day high, quiet buzz, top quartile of vol/mcap."""
    buzz = extra.get("buzz") or {}
    ratio = extra.get("turnover") or {}
    readings = [float(v) for v in buzz.values() if v is not None]
    if not readings or not ratio:
        return []
    readings.sort()
    mid = readings[len(readings) // 2]
    ratios = sorted(float(v) for v in ratio.values() if v is not None)
    if not ratios:
        return []
    cut = ratios[int(len(ratios) * 0.75)]
    names = []
    for sym, series in closes.items():
        if sym in STOCK_SYMS or sym == "BTC":
            continue
        vals = _px(series)
        if len(vals) < 30:
            continue
        high = max(vals[-30:])
        if high <= 0 or vals[-1] < high * 0.90:
            continue
        b = buzz.get(sym)
        if b is None or float(b) >= mid:
            continue
        turn = ratio.get(sym)
        if turn is None or float(turn) < cut:
            continue
        names.append((float(turn), sym))
    names.sort(reverse=True)
    return [sym for _, sym in names[:5]]


def quiet_exit(price, peak, buzz, runup, trail=0.12, giveback=False):
    """True when a quiet-strength hold should be sold. peak is the best price since entry."""
    if price is None or peak is None or peak <= 0:
        return None
    if buzz is not None and float(buzz) >= 3 and runup is not None and float(runup) >= 0.15:
        return "sell into hype: buzz %.1f and run-up %.1f%%" % (float(buzz), float(runup) * 100)
    if price <= peak * (1 - trail):
        drop = (1 - price / peak) * 100
        return "12%% trail: price %.4g is %.1f%% under the peak %.4g" % (price, drop, peak)
    return None


def quiet_giveback_exit(entry, price, peak, giveback_pct=20):
    """Sell when the gain has given back this share of the peak gain. Off unless configured."""
    if not entry or entry <= 0 or not price or not peak:
        return None
    peak_gain = peak / entry - 1
    if peak_gain <= 0:
        return None
    gain = price / entry - 1
    if gain <= peak_gain * (1 - giveback_pct / 100.0):
        return "20%% giveback: kept %.1f%% of a %.1f%% peak gain" % (gain * 100, peak_gain * 100)
    return None


def _percentile(values, q):
    """Linear percentile. q is 0 to 1."""
    s = sorted(float(v) for v in values)
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    pos = min(1.0, max(0.0, q)) * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def funding_halve(points, hot=0.25, min_obs=30):
    """True when the latest annualised funding rate should cut crypto exposure in half.

    The last point is the live reading. It halves the book when it is at or above `hot`
    (25% a year) or strictly above the 90th percentile of the readings before it.
    A flat year does not halve on the percentile rule. No readings means no change.
    """
    if not points:
        return False
    latest = float(points[-1])
    if latest >= float(hot):
        return True
    prior = [float(x) for x in points[:-1]][-365:]
    if len(prior) < min_obs:
        return False
    p90 = _percentile(prior, 0.90)
    return p90 is not None and latest > p90


def halve_series(points, hot=0.25):
    """[(day, bool)] walking forward, so a replay never uses a future funding print."""
    out, hist = [], []
    for day, ann in points:
        hist.append(float(ann))
        out.append((day, funding_halve(hist, hot=hot)))
    return out


def allowed_from(known, hot):
    """BTC is always allowed. ETH is allowed unless it is unlocking. Other majors only when known and not unlocking."""
    known = set(known or [])
    hot = set(hot or [])
    allowed = {"BTC"}
    if "ETH" not in hot:
        allowed.add("ETH")
    for sym in MAJORS:
        if sym in known and sym not in hot:
            allowed.add(sym)
    return allowed


def parse_pattern_unlocks(payload, today, window=30, limit_pct=1.0):
    """PatternPlus rows: pct_circulating is already in percent. Sum the next `window` days.

    Returns (allowed set, known symbols) or None when the payload is not that feed.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    end = today + timedelta(days=window)
    known, total = set(), {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        known.add(sym)
        try:
            day = date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            continue
        if day < today or day > end:
            continue
        pct = row.get("pct_circulating")
        if pct is None:
            pct = row.get("pct_total")
        try:
            total[sym] = total.get(sym, 0.0) + float(pct)
        except (TypeError, ValueError):
            continue
    hot = {sym for sym, pct in total.items() if pct > float(limit_pct)}
    return allowed_from(known, hot), known


def parse_llama_emissions(payload, today, limit=0.01):
    """DeFiLlama emissions rows. Token amount over circulating supply, next 30 days.

    Returns (allowed, known) or None when this is not that shape. A PatternPlus
    payload is handed to that parser instead.
    """
    rows = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        rows = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("protocols")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    if "pct_circulating" in rows[0] or "symbol" in rows[0] and "date" in rows[0]:
        return parse_pattern_unlocks({"data": rows}, today, limit_pct=limit * 100.0)
    alias = {"BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "BTC": "BTC", "ETH": "ETH", "SOL": "SOL"}
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()
    end = start + 30 * 86400
    known, hot = set(), set()
    for row in rows:
        raw = str(row.get("token") or row.get("gecko_id") or row.get("name") or "").upper().strip()
        sym = alias.get(raw)
        supply = row.get("circSupply") or row.get("circulating") or row.get("supply")
        try:
            supply = float(supply)
        except (TypeError, ValueError):
            continue
        if not sym or supply <= 0:
            continue
        tokens = 0.0
        for ev in row.get("events") or []:
            ts = ev.get("timestamp")
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if ts > 10 ** 12:
                ts = ts / 1000.0
            if ts < start or ts > end:
                continue
            amt = ev.get("noOfTokens") if ev.get("noOfTokens") is not None else ev.get("amount")
            if isinstance(amt, list):
                amt = sum(float(x or 0) for x in amt)
            try:
                tokens += float(amt or 0)
            except (TypeError, ValueError):
                continue
        known.add(sym)
        if tokens / supply > float(limit):
            hot.add(sym)
    return allowed_from(known, hot), known


def _carry_ok(sym, extra):
    """BTC always. With no unlock feed, ETH is kept and every other alt is dropped."""
    if sym == "BTC":
        return True
    allowed = extra.get("unlock_allowed")
    if allowed is None:
        return sym == "ETH"
    return sym in allowed


def _carry_targets(closes, extra, o, n):
    """Best 28-day major above its 50-day average, sized by realised vol. Stocks stay on the vol trend."""
    best = None
    best_r = None
    best_series = None
    for sym in MAJORS:
        if not _carry_ok(sym, extra):
            continue
        series = closes.get(sym) or []
        if not _above(series, n):
            continue
        r = ret_n(_px(series), 28)
        if r is None:
            continue
        if best_r is None or r > best_r:
            best, best_r, best_series = sym, r, series
    out = {}
    if best_series is not None:
        w = vol_weight(_px(best_series), o["vol_target"])
        if extra.get("funding_halve"):
            w *= 0.5
        out[best] = ("crypto", w)
    stock = _stock_name(closes, n)
    if stock and _above(closes[stock], n):
        out[stock] = ("stocks", vol_weight(_px(closes[stock]), o["vol_target"]))
    return out


def _equity_universe():
    """Trading 212 US and UK names from the stock universe. Australian listings are left out."""
    names = []
    uni = ((cfg().get("stocks") or {}).get("universe") or {})
    for block in uni.values():
        for ticker in block.get("tickers") or []:
            sym = str(ticker).strip()
            if not sym or sym.upper().endswith(".AX"):
                continue
            names.append(sym)
    return list(dict.fromkeys(names))


def _insider_targets(closes, extra):
    """Top five by 126-day return, equal weight, with a 1.5 tilt for a recent opportunistic buy.

    A tilted name that is already in the top half but outside the top five is added.
    At most eight names. Weights are renormalised to the stock sleeve. Crypto stays cash.
    """
    insider = extra.get("insider") or {}
    universe = extra.get("universe")
    if universe is None:
        universe = _equity_universe()
    ranked = []
    for sym in universe:
        series = closes.get(sym) or []
        vals = _px(series)
        if len(vals) < 126:
            continue
        span = 200 if len(vals) >= 200 else 50
        if not _above(series, span):
            continue
        r = ret_n(vals, 126)
        if r is None:
            continue
        ranked.append((r, sym))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    if not ranked:
        return {}
    half = max(1, (len(ranked) + 1) // 2)
    weights = {}
    for _, sym in ranked[:5]:
        weights[sym] = 1.5 if insider.get(sym) else 1.0
    for _, sym in ranked[:half]:
        if len(weights) >= 8:
            break
        if sym in weights or not insider.get(sym):
            continue
        weights[sym] = 1.5
    total = sum(weights.values()) or 1.0
    return {sym: ("stocks", w / total) for sym, w in weights.items()}


def _signal_targets(closes, extra, o, n):
    """Full account when that day's regime is long. A missing regime is cash. There is no short.

    The 50-day trend is not a gate. This book is the timing rule, long or flat.
    """
    regime = extra.get("regime") or {}
    out = {}
    if regime.get("BTC") and closes.get("BTC"):
        out["BTC"] = ("crypto", 1.0)
    stock = None
    for sym in STOCK_SYMS:
        if closes.get(sym):
            stock = "VUSA" if sym.startswith("VUSA") else "EQQQ"
            break
    if stock and regime.get(stock) and _stock_name(closes, 2):
        out[_stock_name(closes, 2)] = ("stocks", 1.0)
    return out


def owner_is_routine(months, buy_month):
    """Cohen-Malloy-Pomorski routine trader: this person already traded that calendar month in an earlier year."""
    try:
        year_s, month = str(buy_month)[:7].split("-")
        year = int(year_s)
    except (TypeError, ValueError):
        return False
    for raw in months or []:
        text = str(raw)
        if len(text) < 7 or text[4] != "-":
            continue
        try:
            y = int(text[:4])
        except ValueError:
            continue
        if text[5:7] == month and y < year:
            return True
    return False


def insider_flags(cache, today, window_days=180):
    """{symbol: True} when a non-routine open-market buy falls inside the window and the history was complete enough."""
    flags = {}
    start = today - timedelta(days=int(window_days))
    for sym, rec in ((cache or {}).get("tickers") or {}).items():
        if not rec.get("history_ok"):
            continue
        hit = False
        for info in (rec.get("owners") or {}).values():
            months = info.get("months") or []
            for buy in info.get("buys") or []:
                try:
                    day = date.fromisoformat(str(buy)[:10])
                except ValueError:
                    continue
                if day < start or day > today:
                    continue
                if owner_is_routine(months, str(buy)[:7]):
                    continue
                hit = True
                break
            if hit:
                break
        if hit:
            flags[sym] = True
    return flags


def parse_form4(text):
    """Open-market and other coded transactions from one SEC Form 4 XML document."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    owners = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "rptOwnerName" and el.text and el.text.strip():
            owners.append(" ".join(el.text.split()).upper())
    owner = owners[0] if owners else ""
    out = []
    for tx in root.iter():
        kind = tx.tag.rsplit("}", 1)[-1]
        if kind not in ("nonDerivativeTransaction", "derivativeTransaction"):
            continue
        code = when = None
        for el in tx.iter():
            name = el.tag.rsplit("}", 1)[-1]
            if name == "transactionCode" and el.text and el.text.strip():
                code = el.text.strip().upper()
            if name == "transactionDate":
                for child in el.iter():
                    if child.tag.rsplit("}", 1)[-1] == "value" and child.text and child.text.strip():
                        when = child.text.strip()[:10]
        if code and when and owner:
            out.append(dict(owner=owner, code=code, date=when, purchase=kind == "nonDerivativeTransaction" and code == "P"))
    return out


def _note_trade(owners, owner, day, purchase):
    info = owners.setdefault(owner, {"months": [], "buys": []})
    ym = day[:7]
    if ym and ym not in info["months"]:
        info["months"].append(ym)
    if purchase and day not in info["buys"]:
        info["buys"].append(day)


def parse_finnhub_insiders(payload):
    """Finnhub insider-transactions rows into the same owner map Form 4 uses."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    owners = {}
    earliest = None
    if not isinstance(rows, list):
        return owners, earliest
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = " ".join(str(row.get("name") or "").split()).upper()
        day = str(row.get("transactionDate") or row.get("filingDate") or "")[:10]
        code = str(row.get("transactionCode") or "").upper()
        if not name or len(day) < 10:
            continue
        _note_trade(owners, name, day, code == "P")
        if earliest is None or day < earliest:
            earliest = day
    return owners, earliest


def due(book, last_day, today):
    """Daily books act once a calendar day. Weekly books act when the ISO week has changed."""
    if last_day is None:
        return True
    if book in ("majors_rotation", "btc_core_satellite", "rs_rotation_veto", "trend_carry_unlock"):
        return last_day.isocalendar()[:2] != today.isocalendar()[:2]
    if book == "insider_rs_stocks":
        return (last_day.year, last_day.month) != (today.year, today.month)
    return last_day != today


def band_for(book):
    """Vol size and the 85% core only trade after a 5 point drift. A flip to cash always trades."""
    if book in ("btc_trend_vol", "btc_core_satellite"):
        return 0.05
    return 0.0


def apply_targets(cash, pos, prices, targets, fees, min_trade, band=0.0, min_pos_frac=0.0):
    """Move holdings toward the target weights. Sells first. Returns trade rows (no timestamps).

    cash is {account: gbp}. pos is {asset: {qty, cost, account, peak}}.
    prices is {asset: gbp}. fees is {account: fraction per side} or a default via 'crypto'/'stocks'.
    """
    trades = []
    for account in ("crypto", "stocks"):
        held = {a: p for a, p in pos.items() if p.get("account") == account and p.get("qty", 0) > 1e-12}
        value = sum(p["qty"] * prices[a] for a, p in held.items() if a in prices and prices[a] > 0)
        equity = float(cash.get(account) or 0) + value
        if equity <= 0:
            continue
        want = {}
        for asset, (acct, weight) in targets.items():
            if acct == account and weight > 0 and prices.get(asset):
                want[asset] = float(weight) * equity
        # Sell what is no longer wanted, then trim overweights, then buy.
        for asset, p in list(held.items()):
            px = prices.get(asset)
            if not px:
                continue
            have = p["qty"] * px
            goal = want.get(asset, 0.0)
            gap = have - goal
            weight_gap = abs(have / equity - (goal / equity if equity else 0))
            if goal <= 0 or (gap > min_trade and weight_gap >= band - 1e-12):
                sell_gbp = have if goal <= 0 else min(have, gap)
                if sell_gbp < min_trade and goal > 0:
                    continue
                # A full exit is the whole position. A trim under min_pos_frac of the position waits.
                if goal > 0 and have > 0 and sell_gbp + 1e-9 < have * min_pos_frac:
                    continue
                _sell(cash, pos, trades, asset, sell_gbp, px, fees, account)
        for asset, goal in want.items():
            px = prices.get(asset)
            if not px:
                continue
            have = pos[asset]["qty"] * px if asset in pos else 0.0
            gap = goal - have
            if gap < min_trade:
                continue
            if have > 0 and gap + 1e-9 < have * min_pos_frac:
                continue
            if abs(gap) / equity < band - 1e-12 and have > 0:
                continue
            spend = min(gap, float(cash.get(account) or 0))
            if spend < min_trade:
                continue
            _buy(cash, pos, trades, asset, spend, px, fees, account)
    return trades


def _fee(fees, account):
    if isinstance(fees, dict):
        return float(fees.get(account, fees.get("crypto", 0.004)))
    return float(fees)


def _buy(cash, pos, trades, asset, spend, px, fees, account):
    rate = _fee(fees, account)
    qty = spend / (px * (1 + rate))
    fee = spend - qty * px
    if qty <= 0:
        return
    cash[account] = float(cash.get(account) or 0) - spend
    p = pos.get(asset)
    if p:
        p["qty"] += qty
        p["cost"] += spend
        p["peak"] = max(float(p.get("peak") or px), px)
    else:
        pos[asset] = dict(qty=qty, cost=spend, account=account, peak=px, entry=px * (1 + rate))
    trades.append(dict(side="BUY", asset=asset, account=account, qty=qty, price=px, value=spend, fee=fee, pnl=None,
                        reason="target weight"))


def _sell(cash, pos, trades, asset, gbp, px, fees, account):
    p = pos.get(asset)
    if not p or p["qty"] <= 0 or px <= 0:
        return
    rate = _fee(fees, account)
    qty = min(p["qty"], gbp / px)
    gross = qty * px
    fee = gross * rate
    proceeds = gross - fee
    frac = qty / p["qty"] if p["qty"] else 1
    basis = p["cost"] * frac
    pnl = proceeds - basis
    p["qty"] -= qty
    p["cost"] -= basis
    if p["qty"] <= 1e-10:
        pos.pop(asset, None)
    cash[account] = float(cash.get(account) or 0) + proceeds
    trades.append(dict(side="SELL", asset=asset, account=account, qty=qty, price=px, value=gross, fee=fee, pnl=pnl,
                        reason="target weight"))


def _asof_value(pairs, day, default=None):
    """Latest value whose day is on or before `day`. pairs is a sorted [(day, value)]."""
    if not pairs:
        return default
    lo, hi, best = 0, len(pairs) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if pairs[mid][0] <= day:
            best = pairs[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return default if best is None else best


def replay(book, closes, fees=None, min_trade=5.0, fx=1.0, carry=None):
    """Step one book across daily closes. closes is {asset: [(date, usd), ...]}.

    fx is GBP per 1 USD and is constant, so the return does not depend on it.
    The stock sleeve uses the stock fee. Crypto uses the Kraken fee.
    carry holds a precomputed funding series and an unlock allow-list. Nothing
    is fetched inside the day loop.
    """
    fees = fees or {"crypto": 0.004, "stocks": 0.0015}
    dates = sorted({d for series in closes.values() for d, _ in series})
    by = {a: {d: p for d, p in series} for a, series in closes.items()}
    cash = {"crypto": 100.0, "stocks": 100.0}
    pos = {}
    curve = []
    trades = []
    last = None
    held_sat = None
    sat_since = None
    peaks = {}
    o = opts()
    carry = carry or {}
    frac = o["min_pos_frac"] if book == "trend_carry_unlock" else 0.0
    regime_by_day = {}
    if book == "signal_engine":
        import signalhist
        regime_by_day = signalhist.book_regime(closes, carry)
    for i, day in enumerate(dates):
        today = datetime.fromisoformat(day).date()
        window = {}
        for asset, series in by.items():
            pts = [(d, p) for d, p in closes[asset] if d <= day]
            if pts:
                window[asset] = pts
        prices = {a: pts[-1][1] * fx for a, pts in window.items() if pts}
        extra = {"opts": o, "held": None}
        if book == "btc_core_satellite" and held_sat and sat_since:
            extra["held"] = (held_sat, (today - sat_since).days)
        if book == "trend_carry_unlock":
            extra["funding_halve"] = bool(_asof_value(carry.get("halve") or [], day, False))
            if carry.get("allowed") is not None:
                extra["unlock_allowed"] = set(carry["allowed"])
        if book == "insider_rs_stocks":
            extra["insider"] = carry.get("insider") or {}
            if carry.get("universe") is not None:
                extra["universe"] = carry["universe"]
        if book == "signal_engine":
            extra["regime"] = regime_by_day.get(day) or {}
        act = due(book, last, today)
        # Quiet-strength exits are checked every day, not only on the entry day.
        if book == "quiet_strength":
            for asset in list(pos):
                px = prices.get(asset)
                if not px:
                    continue
                peaks[asset] = max(peaks.get(asset, px), px)
                pos[asset]["peak"] = peaks[asset]
                why = quiet_exit(px, peaks[asset], None, ret_n(_px(window.get(asset) or []), 1), o["trail"], False)
                if o["giveback"]:
                    why = why or quiet_giveback_exit(pos[asset].get("entry"), px, peaks[asset])
                if why:
                    _sell(cash, pos, trades, asset, pos[asset]["qty"] * px, px, fees, pos[asset]["account"])
                    trades[-1]["reason"] = why
                    trades[-1]["day"] = day
                    peaks.pop(asset, None)
        if act:
            targets = targets_for(book if book != "best_of_plus" else "btc_trend", window, extra)
            if book == "btc_core_satellite":
                sat = next((a for a, (acct, w) in targets.items() if a != "BTC" and w > 0), None)
                if sat != held_sat:
                    held_sat = sat
                    sat_since = today if sat else None
            rows = apply_targets(cash, pos, prices, targets, fees, min_trade, band_for(book), frac)
            for row in rows:
                row["day"] = day
            trades.extend(rows)
            last = today
        for asset, p in pos.items():
            px = prices.get(asset)
            if px:
                peaks[asset] = max(peaks.get(asset, px), px)
                p["peak"] = peaks[asset]
        eq = sum(cash.values()) + sum(p["qty"] * prices[a] for a, p in pos.items() if a in prices)
        btc_px = prices.get("BTC")
        curve.append(dict(day=day, eq=eq, btc=btc_px))
    return dict(curve=curve, trades=trades, cash=cash, pos=pos)


def replay_stats(result, start=200.0):
    """Return, max drawdown, trade count, and excess versus buying and holding BTC."""
    curve = result.get("curve") or []
    if not curve:
        return dict(ret=None, max_dd=None, trades=0, excess_btc=None, end=None, btc_ret=None)
    end = curve[-1]["eq"]
    ret = end / start - 1
    peak, mdd = 0.0, 0.0
    for row in curve:
        peak = max(peak, row["eq"])
        if peak:
            mdd = min(mdd, row["eq"] / peak - 1)
    btc = [row["btc"] for row in curve if row.get("btc")]
    btc_ret = (btc[-1] / btc[0] - 1) if len(btc) >= 2 and btc[0] else None
    sells = [t for t in result["trades"] if t["side"] == "SELL"]
    return dict(ret=ret, max_dd=mdd, trades=len(result["trades"]), sells=len(sells),
                excess_btc=(None if btc_ret is None else ret - btc_ret), end=end, btc_ret=btc_ret,
                fees=sum(t.get("fee") or 0 for t in result["trades"]))


def report_table(rows):
    head = f"{'book':<22} {'return':>8} {'maxDD':>8} {'trades':>7} {'vs BTC':>8}"
    lines = [head]
    for row in rows:
        def pct(x):
            return "n/a" if x is None else f"{x * 100:+.1f}%"
        lines.append(f"{row['book']:<22} {pct(row.get('ret')):>8} {pct(row.get('max_dd')):>8} {row.get('trades', 0):>7} {pct(row.get('excess_btc')):>8}")
    return "\n".join(lines)


# --- Kraken history -------------------------------------------------------

def _http_json(url, params, timeout=30):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + "?" + query, headers={"User-Agent": "paper-trader"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_kraken_daily(pair, fetch=None, start=HISTORY_FROM, pause=1.1):
    """[(unix, close, base_volume), ...] from Kraken's public daily OHLC, walking forward from `start`."""
    fetch = fetch or _http_json
    since = int(start)
    out, seen = [], set()
    for _ in range(12):
        try:
            data = fetch(KRAKEN, {"pair": pair, "interval": 1440, "since": since})
        except Exception:
            break
        result = (data or {}).get("result") or {}
        rows = None
        for key, val in result.items():
            if key != "last" and isinstance(val, list):
                rows = val
                break
        if not rows:
            break
        fresh = []
        for row in rows:
            try:
                t, close, vol = int(row[0]), float(row[4]), float(row[6] or 0)
            except (TypeError, ValueError, IndexError):
                continue
            if close <= 0 or t in seen or t < since:
                continue
            seen.add(t)
            fresh.append((t, close, vol))
        if not fresh:
            break
        out.extend(fresh)
        since = fresh[-1][0] + 60
        if len(rows) < 720:
            break
        if pause:
            time.sleep(pause)
    out.sort()
    return out


def bars_to_days(bars):
    """Kraken (unix, close, vol) -> [(YYYY-MM-DD, close)]. The latest bar of a UTC date wins."""
    by = {}
    for t, close, _vol in bars:
        day = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
        by[day] = close
    return sorted(by.items())


def store_kraken_history(con, fetch=None, limit=3, pause=1.1):
    """Write daily closes for the next Kraken symbols that are not yet caught up.

    One call already returns about two years, which is enough for a 50-day and a
    100-day average. Further calls walk back to 2017. Returns how many symbols were stored.
    """
    raw = con.execute("SELECT value FROM state WHERE key='kraken_daily_done'").fetchone()
    try:
        done = set(json.loads(raw[0])) if raw and raw[0] else set()
    except Exception:
        done = set()
    todo = [sym for sym in PAIRS if sym not in done][:max(0, int(limit))]
    n = 0
    for sym in todo:
        bars = fetch_kraken_daily(PAIRS[sym], fetch=fetch, pause=pause)
        days = bars_to_days(bars)
        if not days:
            continue
        con.executemany("INSERT OR REPLACE INTO daily_closes VALUES(?,?,?)", [(sym, d, p) for d, p in days])
        done.add(sym)
        n += 1
    if todo:
        con.execute("INSERT OR REPLACE INTO state VALUES('kraken_daily_done',?)", (json.dumps(sorted(done)),))
    return n


def fetch_yahoo_daily(symbol="VUSA.L", timeout=30):
    """[(YYYY-MM-DD, usd_or_local close)] from Yahoo's chart. Empty when the call fails."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=10y"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
        stamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    except Exception:
        return []
    out = []
    for t, p in zip(stamps, closes):
        if not p or p <= 0:
            continue
        out.append((datetime.fromtimestamp(int(t), timezone.utc).date().isoformat(), float(p)))
    return out


def load_history(books=None, pause=0.6):
    """Fetch Kraken majors and the liquid list, plus VUSA, for a replay. Not written to the live database."""
    books = books or ("btc_trend",)
    need = set(MAJORS)
    if any(b in ("rs_rotation_veto", "btc_core_satellite", "quiet_strength") for b in books):
        need = set(PAIRS)
    elif "majors_rotation" in books:
        need = set(MAJORS)
    closes = {}
    for sym in sorted(need):
        bars = fetch_kraken_daily(PAIRS[sym], pause=pause)
        days = bars_to_days(bars)
        if days:
            closes[sym] = days
        elif pause:
            time.sleep(pause)
    yahoo = fetch_yahoo_daily("VUSA.L") or fetch_yahoo_daily("EQQQ.L")
    if yahoo:
        closes["VUSA" if yahoo and "VUSA" else "EQQQ"] = yahoo
        # The symbol we asked is the name we store. VUSA.L prices are GBP; the replay
        # uses them as the stock-sleeve price with fx 1 because both sides of a return
        # are in the same unit. Label it VUSA so the book can find it.
        closes["VUSA"] = yahoo
    return closes


def _get_json(url, timeout=8, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "paper-trader"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def parse_kraken_funding(payload):
    """Kraken hourly relative funding, kept as one annualised rate per UTC day (the last hour)."""
    rates = (payload or {}).get("rates") if isinstance(payload, dict) else None
    if not isinstance(rates, list) or not rates:
        return []
    daily = {}
    for row in sorted(rates, key=lambda r: str((r or {}).get("timestamp") or "")):
        if not isinstance(row, dict):
            continue
        day = str(row.get("timestamp") or "")[:10]
        rel = row.get("relativeFundingRate")
        if len(day) < 10 or rel is None:
            continue
        try:
            daily[day] = float(rel) * 24.0 * 365.0
        except (TypeError, ValueError):
            continue
    return sorted(daily.items())


def parse_binance_funding(rows):
    """Binance 8-hour fundingRate, one annualised rate per UTC day."""
    if isinstance(rows, dict):
        rows = rows.get("data") if isinstance(rows.get("data"), list) else None
    if not isinstance(rows, list) or not rows:
        return []
    daily = {}
    for row in sorted(rows, key=lambda r: (r or {}).get("fundingTime") or 0):
        if not isinstance(row, dict):
            continue
        try:
            ts = float(row.get("fundingTime"))
            ann = float(row.get("fundingRate")) * 3.0 * 365.0
        except (TypeError, ValueError):
            continue
        if ts > 10 ** 12:
            ts = ts / 1000.0
        daily[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = ann
    return sorted(daily.items())


def parse_deribit_funding(payload):
    """Deribit interest_8h (or interest_1h), one annualised rate per UTC day."""
    rows = payload.get("result") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        return []
    daily = {}
    for row in sorted(rows, key=lambda r: (r or {}).get("timestamp") or 0):
        if not isinstance(row, dict):
            continue
        rate = row.get("interest_8h")
        mult = 3.0 * 365.0
        if rate is None:
            rate = row.get("interest_1h")
            mult = 24.0 * 365.0
        try:
            ts = float(row.get("timestamp"))
            ann = float(rate) * mult
        except (TypeError, ValueError):
            continue
        if ts > 10 ** 12:
            ts = ts / 1000.0
        daily[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = ann
    return sorted(daily.items())


def fetch_funding_points(get=None):
    """One year of daily BTC funding. Kraken, then Binance, then Deribit. Empty when all three fail."""
    get = get or _get_json
    try:
        data = get("https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=PF_XBTUSD")
        pts = parse_kraken_funding(data)
        if len(pts) >= 30:
            return pts, "kraken"
    except Exception:
        pass
    try:
        start = int((time.time() - 370 * 86400) * 1000)
        rows, cursor = [], start
        for _ in range(2):
            chunk = get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime={cursor}&limit=1000")
            if not isinstance(chunk, list) or not chunk:
                break
            rows.extend(chunk)
            last = chunk[-1].get("fundingTime") if isinstance(chunk[-1], dict) else 0
            if len(chunk) < 1000 or not last:
                break
            cursor = int(last) + 1
        pts = parse_binance_funding(rows)
        if len(pts) >= 30:
            return pts, "binance"
    except Exception:
        pass
    try:
        end = int(time.time() * 1000)
        start = end - 370 * 86400 * 1000
        url = ("https://www.deribit.com/api/v2/public/get_funding_rate_history"
               f"?instrument_name=BTC-PERPETUAL&start_timestamp={start}&end_timestamp={end}")
        pts = parse_deribit_funding(get(url))
        if pts:
            return pts, "deribit"
    except Exception:
        pass
    return [], ""


def fetch_unlock_decision(today, get=None, limit_pct=1.0):
    """(allowed, known, source) or None. DeFiLlama, then Tokenomist if a key is set, then PatternPlus."""
    get = get or _get_json
    try:
        parsed = parse_llama_emissions(get("https://api.llama.fi/emissions"), today, limit=limit_pct / 100.0)
        if parsed and parsed[1]:
            return parsed[0], parsed[1], "defillama"
    except Exception:
        pass
    key = os.environ.get("TOKENOMIST_API_KEY") or ""
    if key:
        try:
            data = get("https://api.tokenomist.ai/v2/unlock/events",
                       headers={"User-Agent": "paper-trader", "x-api-key": key})
            parsed = parse_pattern_unlocks(data, today, limit_pct=limit_pct) or parse_llama_emissions(data, today, limit=limit_pct / 100.0)
            if parsed and parsed[1]:
                return parsed[0], parsed[1], "tokenomist"
        except Exception:
            pass
    try:
        parsed = parse_pattern_unlocks(get("https://pattern.plus/api/v1/unlocks.json"), today, limit_pct=limit_pct)
        if parsed:
            return parsed[0], parsed[1], "patternplus"
    except Exception:
        pass
    return None


def load_carry(today=None, get=None):
    """Funding and unlocks for a replay. One fetch of each, outside the day loop."""
    today = today or datetime.now(timezone.utc).date()
    o = opts()
    points, source = fetch_funding_points(get=get)
    decided = fetch_unlock_decision(today, get=get, limit_pct=o["unlock_pct"])
    return dict(halve=halve_series(points, hot=o["funding_hot"]),
                allowed=(decided[0] if decided else None),
                funding_source=source,
                unlock_source=(decided[2] if decided else ""),
                funding_days=len(points))


def replay_books(books, closes=None, pause=0.6, carry=None):
    """Replay books (or the names given) and return stat rows."""
    books = list(books)
    closes = closes or load_history(books, pause=pause)
    fees = {"crypto": opts()["kraken_fee"], "stocks": opts()["stock_fee"]}
    if carry is None and "trend_carry_unlock" in books:
        try:
            carry = load_carry()
        except Exception:
            carry = {}
    rows = []
    for name in books:
        result = replay(name, closes, fees=fees, min_trade=opts()["min_trade"],
                        carry=carry if name == "trend_carry_unlock" else None)
        row = replay_stats(result)
        row["book"] = name
        row["days"] = len(result["curve"])
        row["symbols"] = len(closes)
        if name == "trend_carry_unlock" and carry:
            row["funding_source"] = carry.get("funding_source") or ""
            row["unlock_source"] = carry.get("unlock_source") or ""
        rows.append(row)
    return rows


_LAST_SEC = 0.0
SEC_UA = "PaperTrader research github.com/oscarwy08/trading-dashboard"


def _sec_get(url, kind="json"):
    """SEC asks for a declared User-Agent and about 10 requests a second."""
    global _LAST_SEC
    wait = 0.12 - (time.time() - _LAST_SEC)
    if wait > 0:
        time.sleep(wait)
    headers = {"User-Agent": SEC_UA, "Accept": "application/xml,text/xml,*/*" if kind == "text" else "application/json"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", "replace")
    finally:
        _LAST_SEC = time.time()
    return raw if kind == "text" else json.loads(raw)


def _cik_map(payload):
    out = {}
    rows = payload.values() if isinstance(payload, dict) else (payload or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        cik = row.get("cik_str", row.get("cik"))
        if not ticker or cik is None:
            continue
        try:
            out[ticker] = f"{int(cik):010d}"
        except (TypeError, ValueError):
            continue
    return out


def _form4_filings(sub):
    recent = ((sub or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    acc = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    out = []
    for i, form in enumerate(forms):
        if str(form) != "4":
            continue
        if i >= len(dates) or i >= len(acc) or i >= len(docs):
            break
        doc = str(docs[i] or "")
        if not doc.lower().endswith(".xml"):
            continue
        out.append((str(dates[i]), str(acc[i]), doc))
    return out


def _choose_filings(filings, today, window_days, xml_cap=4):
    """Recent Form 4s, plus the same month in earlier years, capped so one ticker stays small.

    history_ok is false when a prior-year month still has filings we did not read.
    Those names stay on pure relative strength.
    """
    start = today - timedelta(days=int(window_days))
    parsed = []
    for day_s, accn, doc in filings:
        try:
            day = date.fromisoformat(str(day_s)[:10])
        except ValueError:
            continue
        parsed.append((day, accn, doc))
    recent = [row for row in parsed if start <= row[0] <= today]
    recent.sort(key=lambda row: row[0], reverse=True)
    chosen = list(recent[:2])
    prior_keys = {(y, day.month) for day, _, _ in chosen for y in range(day.year - 3, day.year)}
    by_month = {}
    for row in parsed:
        by_month.setdefault((row[0].year, row[0].month), []).append(row)
    have = {accn for _, accn, _ in chosen}
    for key in sorted(prior_keys):
        for row in by_month.get(key) or []:
            if len(chosen) >= xml_cap:
                break
            if row[1] not in have:
                chosen.append(row)
                have.add(row[1])
    history_ok = bool(chosen)
    for key in prior_keys:
        for row in by_month.get(key) or []:
            if row[1] not in have:
                history_ok = False
                break
    return chosen[:xml_cap], history_ok


def _form4_url(cik, accession, doc):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{str(accession).replace('-', '')}/{doc}"


def _us_names():
    return [sym for sym in _equity_universe() if "." not in sym]


def _refresh_edgar(cache, today, batch, get):
    cik_day = str(cache.get("cik_day") or "")
    try:
        stale = (today - date.fromisoformat(cik_day)).days >= 7
    except ValueError:
        stale = True
    started = time.perf_counter()
    if stale or not cache.get("cik"):
        mapped = _cik_map(get("https://www.sec.gov/files/company_tickers.json", "json"))
        want = {sym.upper() for sym in _us_names()}
        mapped = {k: v for k, v in mapped.items() if k in want}
        if mapped:
            cache["cik"] = mapped
            cache["cik_day"] = today.isoformat()
    cik = cache.get("cik") or {}
    tickers = cache.setdefault("tickers", {})
    window = opts()["insider_window"]

    def refreshed(sym):
        return (tickers.get(sym) or {}).get("refreshed") or ""

    todo = [sym for sym in _us_names() if sym.upper() in cik and refreshed(sym) != today.isoformat()]
    todo.sort(key=refreshed)
    for sym in todo[:batch]:
        if time.perf_counter() - started > 20:
            break
        rec = dict(tickers.get(sym) or {})
        owners = dict(rec.get("owners") or {})
        seen = set(rec.get("seen") or [])
        history_ok = False
        try:
            sub = get(f"https://data.sec.gov/submissions/CIK{cik[sym.upper()]}.json", "json")
            chosen, history_ok = _choose_filings(_form4_filings(sub), today, window, xml_cap=4)
            for _day, accn, doc in chosen:
                if accn in seen:
                    continue
                text = get(_form4_url(cik[sym.upper()], accn, doc), "text")
                for tx in parse_form4(text):
                    _note_trade(owners, tx["owner"], tx["date"], tx["purchase"])
                seen.add(accn)
        except Exception:
            history_ok = False
        tickers[sym] = {"refreshed": today.isoformat(), "owners": owners, "seen": sorted(seen),
                        "history_ok": history_ok, "source": "edgar"}
    cache["source"] = "edgar"


def _refresh_finnhub(cache, today, batch, get):
    key = os.environ.get("FINNHUB_API_KEY") or os.environ.get("FINNHUB_TOKEN") or ""
    tickers = cache.setdefault("tickers", {})
    start = (today - timedelta(days=365 * 4)).isoformat()

    def refreshed(sym):
        return (tickers.get(sym) or {}).get("refreshed") or ""

    todo = [sym for sym in _us_names() if refreshed(sym) != today.isoformat()]
    todo.sort(key=refreshed)
    started = time.perf_counter()
    for sym in todo[:batch]:
        if time.perf_counter() - started > 20:
            break
        owners, earliest, history_ok = {}, None, False
        try:
            url = ("https://finnhub.io/api/v1/stock/insider-transactions?symbol="
                   f"{urllib.parse.quote(sym)}&from={start}&to={today.isoformat()}&token={urllib.parse.quote(key)}")
            owners, earliest = parse_finnhub_insiders(get(url))
            if earliest:
                history_ok = (today - date.fromisoformat(earliest)).days >= 365 * 2
        except Exception:
            history_ok = False
        tickers[sym] = {"refreshed": today.isoformat(), "owners": owners, "history_ok": history_ok, "source": "finnhub"}
    cache["source"] = "finnhub"


def refresh_carry(con, today, get=None):
    """Funding and unlock caches, at most once a UTC day. Returns (halve, allowed).

    A failed fetch keeps yesterday's readings. With no readings, funding does not
    change the weight and unlocks fall back to BTC and ETH.
    """
    o = opts()
    day = today.isoformat()
    funding = _state(con, "btc_funding") or {}
    unlocks = _state(con, "unlocks") or {}
    if funding.get("fetched") != day:
        points, source = fetch_funding_points(get=get)
        if points:
            funding = {"fetched": day, "source": source, "points": [[d, round(float(v), 6)] for d, v in points]}
        else:
            funding = dict(funding)
            funding["fetched"] = day
            funding["error"] = "no funding feed"
        _set_state(con, "btc_funding", funding)
    if unlocks.get("fetched") != day:
        decided = fetch_unlock_decision(today, get=get, limit_pct=o["unlock_pct"])
        if decided:
            allowed, known, source = decided
            unlocks = {"fetched": day, "ok": True, "source": source,
                       "allowed": sorted(allowed), "known": sorted(known)}
        else:
            unlocks = dict(unlocks)
            unlocks["fetched"] = day
            unlocks["error"] = "no unlock feed"
            if not unlocks.get("ok"):
                unlocks["allowed"] = ["BTC", "ETH"]
                unlocks["known"] = []
        _set_state(con, "unlocks", unlocks)
    points = []
    for row in funding.get("points") or []:
        try:
            points.append(float(row[1]))
        except (TypeError, ValueError, IndexError):
            continue
    halve = funding_halve(points, hot=o["funding_hot"]) if points else False
    if unlocks.get("ok"):
        allowed = set(unlocks.get("allowed") or ["BTC", "ETH"])
    else:
        allowed = {"BTC", "ETH"}
    allowed.add("BTC")
    return halve, allowed


def refresh_insiders(con, today, get=None):
    """At most one batch of US names a day. Returns opportunistic-buy flags. A miss leaves the old cache."""
    o = opts()
    cache = _state(con, "insider_cache") or {}
    if cache.get("fetched") == today.isoformat():
        return insider_flags(cache, today, o["insider_window"])
    batch = max(1, int(o["insider_batch"]))
    try:
        if os.environ.get("FINNHUB_API_KEY") or os.environ.get("FINNHUB_TOKEN"):
            _refresh_finnhub(cache, today, batch, get or _get_json)
        else:
            _refresh_edgar(cache, today, batch, get or _sec_get)
        cache["error"] = ""
    except Exception as exc:
        text = str(exc)
        for env in ("FINNHUB_API_KEY", "FINNHUB_TOKEN", "TOKENOMIST_API_KEY"):
            secret = os.environ.get(env) or ""
            if secret and secret in text:
                text = text.replace(secret, "redacted")
        cache["error"] = text[:180]
    cache["fetched"] = today.isoformat()
    _set_state(con, "insider_cache", cache)
    return insider_flags(cache, today, o["insider_window"])


def _insider_ready(closes):
    for sym in _equity_universe():
        if len(closes.get(sym) or []) >= 126:
            return True
    return False


# --- live book -------------------------------------------------------------

def _connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    ensure_sentiment_cols(con)
    return con


def prepare_book(name, root=None):
    """Create a strategy book from the shared snapshot, then give it a flat £200 trend sleeve.

    Market rows (prices, closes, sentiment, fx) are copied forward from the main book
    on later cycles. The ledger is not. The main file is never written.
    """
    import variants
    root = Path(root or variants.dir_path())
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.db"
    if not path.exists():
        snap = variants.ensure_snapshot(root)
        variants._backup(snap, path)
        con = _connect(path)
        try:
            _blank_trend(con)
            con.commit()
        finally:
            con.close()
    variants._sync(variants.main_db(), path)
    return path


def _blank_trend(con):
    """Drop an inherited ledger and fund the trend sleeve £100 / £100."""
    for table in ("trades", "positions", "cash", "acct_cash", "equity", "signals", "transfers", "loans", "armed_entries"):
        try:
            con.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    o = opts()
    con.execute("INSERT INTO acct_cash VALUES('trend','crypto',?)", (o["crypto"],))
    con.execute("INSERT INTO acct_cash VALUES('trend','stocks',?)", (o["stocks"],))
    con.execute("INSERT OR REPLACE INTO state VALUES('strategy_book',?)", (json.dumps(dict(funded=True)),))


def allocate_best_of_plus(path):
    """Once: 85% of the book sits in the trend sleeve, 15% is split across day and risk.

    Core cash is moved to the trend sleeve. Later cycles leave the split alone.
    """
    con = _connect(path)
    try:
        if _state(con, "strategy_alloc"):
            return
        o = opts()
        total = o["crypto"] + o["stocks"]
        cap = total * o["cap"] / 100.0
        half = cap / 4.0  # day and risk, each account
        trend_c = (total - cap) / 2.0
        con.execute("DELETE FROM acct_cash")
        rows = [
            ("core", "crypto", 0.0), ("core", "stocks", 0.0),
            ("day", "crypto", half), ("day", "stocks", half),
            ("risk", "crypto", half), ("risk", "stocks", half),
            ("trend", "crypto", trend_c), ("trend", "stocks", trend_c),
        ]
        con.executemany("INSERT INTO acct_cash VALUES(?,?,?)", rows)
        try:
            from common import sync_pot_cash
            sync_pot_cash(con)
        except Exception:
            pass
        _set_state(con, "strategy_alloc", dict(ts=iso(), trend=trend_c, day_risk=half))
        con.commit()
    finally:
        con.close()


def _state(con, key):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _set_state(con, key, value):
    con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value)))


def _fx(con):
    row = con.execute("SELECT rate FROM fx WHERE pair='USDGBP' ORDER BY ts DESC LIMIT 1").fetchone()
    try:
        rate = float(row[0]) if row else None
    except (TypeError, ValueError):
        rate = None
    return rate if rate and rate > 0 else None


def _closes(con):
    out = {}
    for asset, day, close in con.execute("SELECT asset, d, close FROM daily_closes WHERE close>0 ORDER BY d"):
        out.setdefault(asset, []).append((day, float(close)))
    return out


def _buzz(con):
    out = {}
    try:
        rows = con.execute("""SELECT asset, buzz_change FROM sentiment WHERE buzz_change IS NOT NULL
                              ORDER BY ts""").fetchall()
    except sqlite3.OperationalError:
        return out
    for asset, buzz in rows:
        out[asset] = float(buzz)
    return out


def _turnover(con):
    """Latest vol24/mcap from the prices table, for the quiet-strength quartile."""
    out = {}
    rows = con.execute("""SELECT asset, vol24, mcap FROM (
                            SELECT asset, vol24, mcap, ROW_NUMBER() OVER (PARTITION BY asset ORDER BY ts DESC) rn
                            FROM prices WHERE mcap>0 AND vol24>0 AND source!='backfill')
                          WHERE rn=1""").fetchall()
    for asset, vol, mcap in rows:
        out[asset] = float(vol) / float(mcap)
    return out


def _gbp_prices(con, rate, closes):
    """Latest GBP price per asset. A missing print falls back to the last daily close.

    One indexed read of the newest row per asset. Scanning every stored print on every
    strategy book was a full sort of the price table, repeated seven times a cycle.
    """
    out = {}
    rows = con.execute("""SELECT asset, price FROM (
                            SELECT asset, price, ROW_NUMBER() OVER (PARTITION BY asset ORDER BY ts DESC) rn
                            FROM prices WHERE price>0 AND source!='backfill')
                          WHERE rn=1""").fetchall()
    for asset, price in rows:
        out[asset] = float(price) * rate
    for asset, series in closes.items():
        if asset not in out and series:
            out[asset] = series[-1][1] * rate
    return out


def _load_pos(con):
    cash = {"crypto": 0.0, "stocks": 0.0}
    for account, amount in con.execute("SELECT account, amount FROM acct_cash WHERE sleeve='trend'"):
        cash[account] = float(amount or 0)
    pos = {}
    for row in con.execute("SELECT asset, klass, qty, entry, cost FROM positions WHERE sleeve='trend'"):
        account = "stocks" if row["klass"] == "stock" else "crypto"
        pos[row["asset"]] = dict(qty=float(row["qty"]), cost=float(row["cost"]), account=account,
                                 entry=float(row["entry"]), peak=float(row["entry"]))
    return cash, pos


def _save(con, cash, pos, trades, ts, rate, peaks):
    con.execute("DELETE FROM positions WHERE sleeve='trend'")
    con.execute("DELETE FROM acct_cash WHERE sleeve='trend'")
    for account, amount in cash.items():
        con.execute("INSERT INTO acct_cash VALUES('trend',?,?)", (account, amount))
    for asset, p in pos.items():
        klass = "stock" if p["account"] == "stocks" else "crypto"
        con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                    ("trend", asset, klass, p["qty"], p["entry"], p["cost"], ts, 1))
    for row in trades:
        pnl = row.get("pnl")
        con.execute("""INSERT INTO trades(ts,sleeve,asset,klass,side,qty,price,fill,value,cost,pnl,reason,price_usd,usd_gbp,gas)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, "trend", row["asset"], "stock" if row["account"] == "stocks" else "crypto",
                     row["side"], row["qty"], row["price"], row["price"], row["value"], row.get("fee") or 0,
                     pnl, row.get("reason") or "target weight",
                     (row["price"] / rate) if rate else None, rate, 0))
    _mark(con, cash, pos, ts, rate, peaks)


def _mark(con, cash, pos, ts, rate, peaks):
    # Prices already in GBP inside pos valuation: callers pass GBP prices via peaks only for the peak.
    # Revalue from the positions' last trade price is stale. The caller passes gbp on the position via a side map.
    gbp = peaks.get("_px") or {}
    crypto_val = sum(p["qty"] * gbp[a] for a, p in pos.items() if p["account"] == "crypto" and a in gbp)
    stock_val = sum(p["qty"] * gbp[a] for a, p in pos.items() if p["account"] == "stocks" and a in gbp)
    c_cash, s_cash = float(cash.get("crypto") or 0), float(cash.get("stocks") or 0)
    # Day/risk cash still counts for best_of_plus so the book equity is the whole £200.
    other = 0.0
    try:
        for (amount,) in con.execute("SELECT amount FROM acct_cash WHERE sleeve!='trend'"):
            other += float(amount or 0)
        for row in con.execute("SELECT asset, qty, klass FROM positions WHERE sleeve!='trend'"):
            px = gbp.get(row["asset"])
            if px:
                other += float(row["qty"]) * px
    except sqlite3.OperationalError:
        other = 0.0
    eq = c_cash + s_cash + crypto_val + stock_val + other
    btc = gbp.get("BTC")
    cols = {r[1] for r in con.execute("PRAGMA table_info(equity)")}
    row = dict(ts=ts, equity=eq, cash=c_cash + s_cash + other, pos_value=crypto_val + stock_val,
               btc=btc, open_n=len(pos), usd_gbp=rate, core=0, day=0, core_cash=0, day_cash=0,
               risk=0, risk_cash=0, crypto=c_cash + crypto_val, stocks=s_cash + stock_val,
               crypto_cash=c_cash, stocks_cash=s_cash, accts=None)
    use = [k for k in row if k in cols]
    ph = ",".join("?" * len(use))
    con.execute(f"INSERT INTO equity({','.join(use)}) VALUES({ph})", [row[k] for k in use])


def run(name, path=None):
    """One cycle for a strategy book. best_of_plus only rebalances the trend sleeve (the trader already ran)."""
    import variants
    path = Path(path or (variants.dir_path() / f"{name}.db"))
    book = "btc_trend" if name == "best_of_plus" else name
    con = _connect(path)
    try:
        rate = _fx(con)
        if not rate:
            print(f"[strategy] {name} skipped (no GBP rate)")
            return
        closes = _closes(con)
        if book == "insider_rs_stocks":
            if not _insider_ready(closes):
                print(f"[strategy] {name} waiting for 126 stock closes")
                return
        elif len(closes.get("BTC") or []) < 50 and book != "quiet_strength":
            print(f"[strategy] {name} waiting for 50 BTC closes ({len(closes.get('BTC') or [])} stored)")
            return
        o = opts()
        cash, pos = _load_pos(con)
        state = _state(con, "strategy_state") or {}
        peaks = state.get("peaks") or {}
        for asset, p in pos.items():
            if asset in peaks:
                p["peak"] = peaks[asset]
        gbp = _gbp_prices(con, rate, closes)
        today = now().astimezone(LON).date()
        last = None
        if state.get("last_day"):
            last = datetime.fromisoformat(state["last_day"]).date()
        extra = {"opts": o, "buzz": _buzz(con), "turnover": _turnover(con),
                 "held": None}
        note = ""
        if book == "trend_carry_unlock":
            halve, allowed = refresh_carry(con, today)
            extra["funding_halve"] = halve
            extra["unlock_allowed"] = allowed
            note = " funding " + ("halved" if halve else "full")
        elif book == "insider_rs_stocks":
            flags = refresh_insiders(con, today)
            extra["insider"] = flags
            note = f" insider {len(flags)}"
        elif book == "signal_engine":
            try:
                import signalhist
                regime = signalhist.book_regime(closes, {})
                extra["regime"] = regime.get(today.isoformat()) or {}
                _set_state(con, "signal_live", dict(btc=int(extra["regime"].get("BTC") or 0),
                                                    regime=extra["regime"]))
                note = " BTC" if extra["regime"].get("BTC") else " cash"
            except Exception:
                extra["regime"] = {}
                note = " cash"
        if book == "btc_core_satellite" and state.get("satellite"):
            since = datetime.fromisoformat(state["satellite"]["since"]).date()
            extra["held"] = (state["satellite"]["asset"], (today - since).days)
        trades = []
        if book == "quiet_strength":
            for asset in list(pos):
                px = gbp.get(asset)
                if not px:
                    continue
                peaks[asset] = max(float(peaks.get(asset) or px), px)
                runup = ret_n(_px(closes.get(asset) or []), 1)
                why = quiet_exit(px, peaks[asset], extra["buzz"].get(asset), runup, o["trail"], False)
                if o["giveback"]:
                    why = why or quiet_giveback_exit(pos[asset].get("entry"), px, peaks[asset])
                if why:
                    _sell(cash, pos, trades, asset, pos[asset]["qty"] * px, px, _fees(o), pos[asset]["account"])
                    trades[-1]["reason"] = why
                    peaks.pop(asset, None)
        acted = False
        if due(book, last, today) or book == "quiet_strength":
            # Quiet entries are weekly; exits already ran. Use the weekly clock for new names.
            entry_due = due("majors_rotation" if book == "quiet_strength" else book, last, today)
            if book != "quiet_strength" or entry_due:
                targets = targets_for(book, closes, extra)
                frac = o["min_pos_frac"] if book == "trend_carry_unlock" else 0.0
                trades += apply_targets(cash, pos, gbp, targets, _fees(o), o["min_trade"], band_for(book), frac)
                acted = True
                if book == "btc_core_satellite":
                    sat = next((a for a, (acct, w) in targets.items() if a != "BTC"), None)
                    prev = (state.get("satellite") or {}).get("asset")
                    if sat and sat == prev:
                        pass
                    elif sat:
                        state["satellite"] = dict(asset=sat, since=today.isoformat())
                    elif not (extra.get("held") and extra["held"][1] < 5):
                        state.pop("satellite", None)
        if acted:
            state["last_day"] = today.isoformat()
        for asset, p in pos.items():
            if asset in gbp:
                peaks[asset] = max(float(peaks.get(asset) or gbp[asset]), gbp[asset])
        state["peaks"] = {a: peaks[a] for a in pos if a in peaks}
        _save(con, cash, pos, trades, iso(), rate, {"_px": gbp, **{a: peaks.get(a) for a in pos}})
        _set_state(con, "strategy_state", state)
        con.commit()
        eq = con.execute("SELECT equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        line = f"[strategy] {name} trades {len(trades)}"
        if eq:
            line += f" equity £{eq[0]:.2f}"
        print(line + note)
    finally:
        con.close()


def _fees(o):
    return {"crypto": o["kraken_fee"], "stocks": o["stock_fee"]}


def comparison_rows():
    """One row per book the dashboard lists, old variants and these books, sorted by excess versus BTC."""
    import variants
    from dashboard import BOOK_MENU
    rep = variants.report()
    rows = []
    for book in rep.get("books") or []:
        if book.get("error"):
            rows.append(dict(name=book["name"], label=book.get("label") or book["name"], error=book["error"],
                             excess_btc=None))
            continue
        rows.append(_row_from_book(book))
    rows.sort(key=lambda r: (r.get("excess_btc") is None, -(r.get("excess_btc") or 0)))
    return rows


def _row_from_book(book):
    curve = book.get("curve") or []
    btc_ret = None
    # The variant report's curve is equity only. Excess uses the book's own equity.btc when we can open it.
    excess = None
    vusa = None
    fees = None
    pf = None
    holdings = []
    try:
        import variants
        path = variants.main_db() if book["name"] == "main" else variants.dir_path() / f"{book['name']}.db"
        if Path(path).exists():
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                excess, btc_ret, vusa, fees, pf, holdings = _extras(con, book.get("ret"))
            finally:
                con.close()
    except Exception:
        pass
    ret = None if book.get("ret") is None else book["ret"] / 100.0
    return dict(
        name=book["name"], label=book.get("label") or book["name"], blurb=book.get("blurb") or "",
        equity=book.get("equity"), ret=ret, excess_btc=excess, excess_vusa=vusa,
        max_dd=None if book.get("max_dd") is None else book["max_dd"] / 100.0,
        trades=book.get("trades"), pf=pf, fees=fees, holdings=holdings,
        curve=curve, btc_ret=btc_ret,
    )


def _extras(con, ret_pct):
    """Excess versus BTC and versus VUSA, fees, profit factor, and current holdings."""
    eq = con.execute("SELECT ts, equity, btc FROM equity ORDER BY ts").fetchall()
    btc_ret = None
    if eq and eq[0]["btc"] and eq[-1]["btc"]:
        btc_ret = eq[-1]["btc"] / eq[0]["btc"] - 1
    ret = None if ret_pct is None else ret_pct / 100.0
    excess = None if ret is None or btc_ret is None else ret - btc_ret
    vusa_ret = _close_ret(con, ("VUSA", "VUSA.L", "EQQQ", "EQQQ.L"))
    excess_vusa = None if ret is None or vusa_ret is None else ret - vusa_ret
    fees = con.execute("SELECT COALESCE(SUM(cost),0)+COALESCE(SUM(gas),0) FROM trades").fetchone()[0]
    sells = [float(r[0] or 0) for r in con.execute("SELECT pnl FROM trades WHERE side='SELL'")]
    wins = sum(x for x in sells if x > 0)
    losses = sum(x for x in sells if x < 0)
    if not sells:
        pf = None
    elif losses == 0:
        pf = None if wins == 0 else 99.0
    else:
        pf = wins / abs(losses)
    holdings = []
    for row in con.execute("SELECT sleeve, asset, qty, cost FROM positions ORDER BY cost DESC"):
        holdings.append(dict(sleeve=row["sleeve"], asset=row["asset"], qty=row["qty"]))
    return excess, btc_ret, excess_vusa, round(float(fees or 0), 2), (None if pf is None else round(pf, 2)), holdings


def _close_ret(con, names):
    for name in names:
        rows = con.execute("SELECT d, close FROM daily_closes WHERE asset=? AND close>0 ORDER BY d", (name,)).fetchall()
        if len(rows) >= 2 and rows[0][1]:
            return rows[-1][1] / rows[0][1] - 1
    return None
