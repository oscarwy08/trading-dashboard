"""Variance premium.

Bollerslev, Tauchen and Zhou, Review of Financial Studies 2009: a high variance risk premium predicts higher returns.

Nothing in this module fetches the network. Missing side history leaves the book in cash.
"""
import bisect
import math
from datetime import date, timedelta

FREQ = 'W'
KIND = 'variance_premium'

def percentile(values, q):
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

class Tape:
    def __init__(self, points):
        clean = []
        for row in sorted(points or []):
            day, val = str(row[0])[:10], row[1]
            if len(day) < 10 or val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            if clean and clean[-1][0] == day:
                clean[-1] = [day, val]
            else:
                clean.append([day, val])
        self.days = [d for d, _ in clean]
        self.px = [v for _, v in clean]

    def __len__(self):
        return len(self.days)

    def index(self, day, inclusive):
        if inclusive:
            return bisect.bisect_right(self.days, day) - 1
        return bisect.bisect_left(self.days, day) - 1

    def asof(self, day, inclusive):
        i = self.index(day, inclusive)
        if i < 0:
            return None, None
        return self.days[i], self.px[i]

    def window(self, day, n, inclusive):
        i = self.index(day, inclusive)
        if i < 0 or i + 1 < n:
            return None
        return self.px[i + 1 - n:i + 1]

    def calendar_return(self, day, end_back, span, inclusive_end=True):
        """Close on or before day-end_back, over the close `span` calendar days earlier."""
        end_day = (date.fromisoformat(day) - timedelta(days=end_back)).isoformat()
        start_day = (date.fromisoformat(end_day) - timedelta(days=span)).isoformat()
        end_d, end_px = self.asof(end_day, inclusive_end)
        start_d, start_px = self.asof(start_day, True)
        if not end_px or not start_px or start_px <= 0:
            return None
        # The print has to sit near the date we asked for. A five-year gap is not a 126-day return.
        if (date.fromisoformat(end_day) - date.fromisoformat(end_d)).days > 7:
            return None
        if (date.fromisoformat(start_day) - date.fromisoformat(start_d)).days > 7:
            return None
        return end_px / start_px - 1.0

def shift(day, n):
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()

def rebalance(days, raw, freq):
    """Hold the last decision through the week or the month. Undefined days do not start the book."""
    out = {}
    held = None
    prev = None
    for day in days:
        d = date.fromisoformat(day)
        key = d.isocalendar()[:2] if freq == "W" else (d.year, d.month)
        val = raw.get(day)
        if held is None:
            if val is None:
                continue
            held = 1 if val else 0
            prev = key
            out[day] = held
            continue
        if key != prev and val is not None:
            held = 1 if val else 0
            prev = key
        out[day] = held
    return out

def _realised_var(btc, day, n=30):
    i = btc.index(day, True)
    if i < n:
        return None
    window = btc.px[i - n:i + 1]
    rets = []
    for a, b in zip(window, window[1:]):
        if a <= 0:
            return None
        rets.append(b / a - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    vol = math.sqrt(var) * math.sqrt(365.0)
    return vol * vol

def vrp_rows(btc, dvol):
    raw, score = {}, {}
    hist = []
    for day in btc.days:
        i = dvol.index(day, False)
        rv = _realised_var(btc, day)
        if i < 0 or rv is None:
            continue
        vrp = (dvol.px[i] / 100.0) ** 2 - rv
        if len(hist) >= 30:
            med = percentile(hist[-252:], 0.50)
            raw[day] = 1 if vrp > med else 0
            score[day] = vrp
        hist.append(vrp)
    return raw, score

def _carry(calendar, pos):
    """Last decision, held through days the other asset does not print."""
    last = None
    out = {}
    for day in calendar:
        if day in pos:
            last = int(pos[day])
        if last is not None:
            out[day] = last
    return out


def _raw(btc, carry):
    kind = KIND
    if kind in ("funding_contrarian", "funding_carry"):
        pts = carry.get("funding")
        if not pts:
            return None
        raw, _ = funding_rows(btc, Tape(pts), kind == "funding_carry")
        return raw
    if kind == "basis_contrarian":
        pts = carry.get("perp")
        if not pts:
            return None
        raw, _ = basis_rows(btc, Tape(pts), btc)
        return raw
    if kind == "dvol_fear":
        pts = carry.get("dvol")
        if not pts:
            return None
        raw, _ = dvol_rows(btc, Tape(pts))
        return raw
    if kind == "variance_premium":
        pts = carry.get("dvol")
        if not pts:
            return None
        raw, _ = vrp_rows(btc, Tape(pts))
        return raw
    if kind == "stablecoin":
        pts = carry.get("supply")
        if not pts:
            return None
        raw, _ = change_rows(btc, Tape(pts), 30, "up")
        return raw
    if kind == "macro":
        nq, dx = carry.get("nasdaq"), carry.get("dxy")
        if not nq or not dx:
            return None
        raw, _ = macro_rows(btc, Tape(nq), Tape(dx))
        return raw
    if kind == "tsmom":
        raw, _ = tsmom_rows(btc)
        return raw
    if kind == "gold":
        pts = carry.get("gold")
        if not pts:
            return None
        raw, _ = change_rows(btc, Tape(pts), 126, "down")
        return raw
    if kind == "onchain":
        pts = carry.get("addresses")
        if not pts:
            return None
        raw, _ = change_rows(btc, Tape(pts), 30, "up")
        return raw
    if kind == "gdelt":
        pts = carry.get("tone")
        if not pts:
            return None
        raw, _ = tone_rows(btc, Tape(pts))
        return raw
    if kind == "dominance":
        pts = carry.get("dominance")
        if not pts:
            return None
        raw, _ = change_rows(btc, Tape(pts), 30, "diff")
        return raw
    return None


def book_regime(closes, carry=None):
    """{day: {asset: 0 or 1}}. An explicit positions map is used as given."""
    carry = carry or {}
    if carry.get("positions"):
        return carry["positions"]
    btc_pts = closes.get("BTC") or []
    if len(btc_pts) < 5:
        return {}
    btc = Tape(btc_pts)
    raw = _raw(btc, carry)
    if not raw:
        return {}
    pos = rebalance(btc.days, raw, FREQ)
    if KIND not in ("tsmom", "macro"):
        return {day: {"BTC": int(v)} for day, v in pos.items()}
    stock_pts = None
    stock_name = None
    for name in ("VUSA", "VUSA.L", "EQQQ", "EQQQ.L"):
        if closes.get(name):
            stock_pts = closes[name]
            stock_name = "VUSA" if name.startswith("VUSA") else "EQQQ"
            break
    calendar = sorted(set(btc.days) | {d for d, _ in (stock_pts or [])})
    btc_held = _carry(calendar, pos)
    stock_held = {}
    if stock_pts:
        st = Tape(stock_pts)
        if KIND == "tsmom":
            sraw, _ = tsmom_rows(st)
        else:
            nq, dx = carry.get("nasdaq"), carry.get("dxy")
            sraw = macro_rows(st, Tape(nq), Tape(dx))[0] if nq and dx else {}
        stock_held = _carry(calendar, rebalance(st.days, sraw, FREQ)) if sraw else {}
    out = {}
    for day in calendar:
        row = {}
        if day in btc_held:
            row["BTC"] = btc_held[day]
        if stock_name and day in stock_held:
            row[stock_name] = stock_held[day]
        if row:
            out[day] = row
    return out

