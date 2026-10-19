"""Exchange trading hours + holiday calendars for the stock side (no network, no dependencies).
Real brokers (Trading 212, IBKR, ...) only fill normal stock orders while the exchange is open, so paper stock trades
are restricted to the same regular sessions:
  US  (NYSE / Nasdaq / NYSE American; plain tickers)  09:30-16:00 America/New_York  (= 14:30-21:00 UK most of the year;
      the zoneinfo conversion handles the ~2-3 weeks a year when US and UK DST differ -> 13:30-20:00 UK)
      early close 13:00 NY on Jul 3, the day after Thanksgiving and Dec 24 (when they are trading days)
  LSE (incl. AIM; Yahoo suffix .L)                    08:00-16:30 Europe/London; early close 12:30 on Dec 24 / Dec 31
  ASX (Yahoo suffix .AX)                              10:00-16:00 Australia/Sydney; early close 14:10 on Dec 24 / Dec 31
No weekend or exchange-holiday sessions. Holiday rules are computed (Easter etc.), so no yearly list to maintain;
one-off closures (state funerals, ...) are not covered - add them to EXTRA_CLOSED if they happen."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

EXCH = {
    "US":  dict(tz=ZoneInfo("America/New_York"), open=time(9, 30), close=time(16, 0), early=time(13, 0), label="US (NYSE/Nasdaq)"),
    "LSE": dict(tz=ZoneInfo("Europe/London"), open=time(8, 0), close=time(16, 30), early=time(12, 30), label="London (LSE/AIM)"),
    "ASX": dict(tz=ZoneInfo("Australia/Sydney"), open=time(10, 0), close=time(16, 0), early=time(14, 10), label="Australia (ASX)"),
}
EXTRA_CLOSED = {"US": set(), "LSE": set(), "ASX": set()}     # ISO dates of unscheduled closures

def exchange(sym):
    s = sym.upper()
    return "LSE" if s.endswith(".L") else "ASX" if s.endswith(".AX") else "US"

def easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = divmod(b, 4); f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31; dy = (h + l - 7 * m + 114) % 31 + 1
    return date(y, mo, dy)

def nth_weekday(y, m, wd, n):          # n-th (1..) weekday wd (0=Mon) of the month; n=-1: last
    if n > 0:
        d = date(y, m, 1); d += timedelta(days=(wd - d.weekday()) % 7); return d + timedelta(weeks=n - 1)
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)

def _obs_us(d):                         # NYSE: Sat -> Fri, Sun -> Mon
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d

def holidays(ex, y):
    out = set(); E = easter(y)
    if ex == "US":
        nyd = date(y, 1, 1)
        if nyd.weekday() != 5: out.add(_obs_us(nyd))          # NYSE does not observe Jan 1 on the previous Friday
        out |= {nth_weekday(y, 1, 0, 3), nth_weekday(y, 2, 0, 3), E - timedelta(days=2), nth_weekday(y, 5, 0, -1),
                _obs_us(date(y, 6, 19)), _obs_us(date(y, 7, 4)), nth_weekday(y, 9, 0, 1), nth_weekday(y, 11, 3, 4),
                _obs_us(date(y, 12, 25))}
    elif ex == "LSE":
        nyd = date(y, 1, 1); out.add(nyd + timedelta(days=(7 - nyd.weekday()) % 7 if nyd.weekday() >= 5 else 0))
        out |= {E - timedelta(days=2), E + timedelta(days=1), nth_weekday(y, 5, 0, 1), nth_weekday(y, 5, 0, -1), nth_weekday(y, 8, 0, -1)}
        xm, bx = date(y, 12, 25), date(y, 12, 26)
        if xm.weekday() == 5: out |= {date(y, 12, 27), date(y, 12, 28)}
        elif xm.weekday() == 6: out |= {date(y, 12, 26), date(y, 12, 27)}
        elif xm.weekday() == 4: out |= {xm, date(y, 12, 28)}
        else: out |= {xm, bx}
    elif ex == "ASX":
        nyd = date(y, 1, 1); out.add(nyd + timedelta(days=(7 - nyd.weekday()) % 7 if nyd.weekday() >= 5 else 0))
        ad = date(y, 1, 26); out.add(ad + timedelta(days=(7 - ad.weekday()) % 7 if ad.weekday() >= 5 else 0))
        out |= {E - timedelta(days=2), E + timedelta(days=1), date(y, 4, 25), nth_weekday(y, 6, 0, 2)}
        xm = date(y, 12, 25)
        if xm.weekday() == 5: out |= {date(y, 12, 27), date(y, 12, 28)}
        elif xm.weekday() == 6: out |= {date(y, 12, 26), date(y, 12, 27)}
        elif xm.weekday() == 4: out |= {xm, date(y, 12, 28)}
        else: out |= {xm, date(y, 12, 26)}
    return out | {date.fromisoformat(x) for x in EXTRA_CLOSED.get(ex, ()) if x.startswith(str(y))}

_HC = {}
def is_trading_day(ex, d):
    if d.weekday() >= 5: return False
    k = (ex, d.year)
    if k not in _HC: _HC[k] = holidays(ex, d.year)
    return d not in _HC[k]

def _early(ex, d):
    if ex == "US":
        th = nth_weekday(d.year, 11, 3, 4)
        return d in (date(d.year, 7, 3), th + timedelta(days=1), date(d.year, 12, 24))
    return d in (date(d.year, 12, 24), date(d.year, 12, 31))

def session(ex, d):
    """(open_utc, close_utc) of the regular session on exchange-local date d, or None if closed that day."""
    if not is_trading_day(ex, d): return None
    X = EXCH[ex]; cl = X["early"] if _early(ex, d) else X["close"]
    return (datetime.combine(d, X["open"], X["tz"]).astimezone(timezone.utc), datetime.combine(d, cl, X["tz"]).astimezone(timezone.utc))

def _utc(t): return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

def is_open(sym_or_ex, t):
    ex = sym_or_ex if sym_or_ex in EXCH else exchange(sym_or_ex); t = _utc(t)
    s = session(ex, t.astimezone(EXCH[ex]["tz"]).date())
    return bool(s and s[0] <= t < s[1])

def mins_to_close(sym_or_ex, t):
    """Minutes until the current session closes (None if closed now)."""
    ex = sym_or_ex if sym_or_ex in EXCH else exchange(sym_or_ex); t = _utc(t)
    s = session(ex, t.astimezone(EXCH[ex]["tz"]).date())
    return (s[1] - t).total_seconds() / 60 if s and s[0] <= t < s[1] else None

def _sessions_between(ex, d0, d1):
    """Regular sessions from exchange-local date d0 through d1, inclusive."""
    out = []
    d = d0
    while d <= d1:
        s = session(ex, d)
        if s:
            out.append(s)
        d += timedelta(days=1)
    return out


def open_minutes(sym_or_ex, start, end):
    """Minutes between two times that fall inside a regular session.

    Overnight gaps, weekends, and exchange holidays count as zero. An early
    close ends that day at the early time. None if `start` cannot be read.
    A span longer than 30 exchange-local days is treated as far past any
    dead-quote window.
    """
    a, b = _as_utc(start), _as_utc(end)
    if a is None or b is None: return None
    if b <= a: return 0.0
    ex = sym_or_ex if sym_or_ex in EXCH else exchange(sym_or_ex)
    tz = EXCH[ex]["tz"]
    d, last = a.astimezone(tz).date(), b.astimezone(tz).date()
    if (last - d).days > 30: return 1e9
    total = 0.0
    for so, sc in _sessions_between(ex, d, last):
        lo, hi = max(so, a), min(sc, b)
        if hi > lo: total += (hi - lo).total_seconds() / 60.0
    return total


def open_minutes_many(sym_or_ex, starts, end):
    """open_minutes for many starts against one end. Sessions are built once.

    Scoring a stock used to walk the calendar again for every 15-minute bar.
    With a few dozen names, on every paper book, that is what pushed a cycle
    that was already near 14 minutes over the systemd cap once London opened.
    """
    b = _as_utc(end)
    ex = sym_or_ex if sym_or_ex in EXCH else exchange(sym_or_ex)
    tz = EXCH[ex]["tz"]
    parsed = [(s, _as_utc(s)) for s in starts]
    if b is None:
        return {s: None for s, _a in parsed}
    valid = [a for _s, a in parsed if a is not None and a < b]
    sessions = []
    if valid:
        d0 = min(a.astimezone(tz).date() for a in valid)
        d1 = b.astimezone(tz).date()
        if (d1 - d0).days <= 40:
            sessions = _sessions_between(ex, d0, d1)
    out = {}
    for key, a in parsed:
        if a is None or b is None:
            out[key] = None
            continue
        if b <= a:
            out[key] = 0.0
            continue
        if (b.astimezone(tz).date() - a.astimezone(tz).date()).days > 30:
            out[key] = 1e9
            continue
        total = 0.0
        for so, sc in sessions:
            if sc <= a or so >= b:
                continue
            lo, hi = max(so, a), min(sc, b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        out[key] = total
    return out

def _as_utc(t):
    if t is None: return None
    if isinstance(t, datetime): return _utc(t)
    s = str(t).strip().replace("Z", "+00:00")
    if "." in s:
        pre, post = s.split(".", 1)
        for i, ch in enumerate(post):
            if ch in "+-":
                s = pre + post[i:]; break
        else:
            s = pre
    try: dt = datetime.fromisoformat(s)
    except ValueError: return None
    return _utc(dt)

def last_close(sym_or_ex, t):
    """Most recent session close <= t (UTC)."""
    ex = sym_or_ex if sym_or_ex in EXCH else exchange(sym_or_ex); t = _utc(t)
    d = t.astimezone(EXCH[ex]["tz"]).date()
    for i in range(15):
        s = session(ex, d - timedelta(days=i))
        if s and s[1] <= t: return s[1]
    return None

def next_open(sym_or_ex, t):
    ex = sym_or_ex if sym_or_ex in EXCH else exchange(sym_or_ex); t = _utc(t)
    d = t.astimezone(EXCH[ex]["tz"]).date()
    for i in range(15):
        s = session(ex, d + timedelta(days=i))
        if s and s[0] > t: return s[0]
        if s and s[0] <= t < s[1]: return s[0]
    return None

def status(t):
    """{exchange: dict(open, label, next_open/closes epoch)} for the dashboard."""
    out = {}
    for ex, X in EXCH.items():
        o = is_open(ex, t); s = session(ex, _utc(t).astimezone(X["tz"]).date())
        out[ex] = dict(open=o, label=X["label"], closes=int(s[1].timestamp()) if o else None,
                       next_open=None if o else int(next_open(ex, t).timestamp()))
    return out

if __name__ == "__main__":      # quick self-check
    L = ZoneInfo("Europe/London")
    for ex in EXCH: print(ex, sorted(holidays(ex, 2026)))
    for s, d in (("NVDA", "2026-10-12"), ("NVDA", "2026-03-16"), ("NVDA", "2026-11-02"), ("RPI.L", "2026-10-12")):
        o, c = session(exchange(s), date.fromisoformat(d)); print(s, d, "UK", o.astimezone(L).strftime("%H:%M"), "-", c.astimezone(L).strftime("%H:%M"))
