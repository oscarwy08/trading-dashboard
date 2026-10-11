"""Data-freshness scores (pure functions, no network).

A price at full freshness must leave a setup's confidence unchanged: the freshness
weight scales the existing score, it does not join the weighted average. Sentiment
decays toward neutral (zero weight) by sentiment_max_age_h; hard gates keep the raw score.
"""
from datetime import datetime, timezone

def parse_dt(s):
    """Parse an ISO timestamp (CoinGecko 'Z' / fractional seconds included) to aware UTC. None if blank."""
    if not s: return None
    s = str(s).strip().replace("Z", "+00:00")
    if "." in s:
        pre, post = s.split(".", 1)
        for i, ch in enumerate(post):
            if ch in "+-":
                s = pre + post[i:]; break
        else:
            s = pre
    try: dt = datetime.fromisoformat(s)
    except ValueError: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt

def to_iso(s):
    dt = parse_dt(s)
    return dt.replace(microsecond=0).isoformat() if dt else None

def age_min(ts, now):
    """Minutes since a provider timestamp. None if it cannot be parsed."""
    dt = ts if isinstance(ts, datetime) else parse_dt(ts)
    if dt is None or now is None: return None
    if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 60.0)

def price_score(age, full_min, dead_min):
    """1 at or under full_min, 0 at or over dead_min, linear between. Unknown age scores 0."""
    if age is None: return 0.0
    full, dead = float(full_min), float(dead_min)
    if age <= full: return 1.0
    if dead <= full or age >= dead: return 0.0
    return (dead - age) / (dead - full)

def history_gaps(pts, now, window_h=8, gap_min=30):
    """How many holes longer than gap_min sit in the last window_h of (ts, price) points, plus a trailing hole up to now."""
    if not pts or now is None: return 0
    from datetime import timedelta
    cut = now - timedelta(hours=window_h)
    xs = [ts for ts, _ in pts if ts >= cut]
    n = sum(1 for a, b in zip(xs, xs[1:]) if (b - a).total_seconds() > gap_min * 60)
    if xs and (now - xs[-1]).total_seconds() > gap_min * 60: n += 1
    return n

def gap_score(n, window=8):
    """1 with a continuous history, falling to 0 after `window` holes."""
    return max(0.0, 1.0 - (n or 0) / float(window or 1))

def sentiment_factor(age_h, max_h=12):
    """1 at age 0, 0 at max_h (linear). None / negative age is fully stale."""
    if age_h is None: return 0.0
    max_h = float(max_h or 12)
    if max_h <= 0 or age_h >= max_h: return 0.0
    if age_h <= 0: return 1.0
    return 1.0 - age_h / max_h

def combine(price_fr, hist_fr, sent_fr=None):
    """Mean of the pieces we actually have. No sentiment row is not a penalty (it is already 'none')."""
    xs = [float(price_fr), float(hist_fr)]
    if sent_fr is not None: xs.append(float(sent_fr))
    return sum(xs) / len(xs)

def adjust_confidence(base, fr, wf, W):
    """conf = base * (W + wf*fr) / (W + wf). fr=1 leaves base unchanged (rounded to 3 d.p., same as the old weighted mean).
    Returns (confidence, freshness component 0-1 rounded to 2 d.p. for the mini-bar)."""
    fr = max(0.0, min(1.0, float(fr if fr is not None else 1)))
    wf, W = float(wf or 0), float(W or 0)
    if wf <= 0 or W <= 0: return round(base, 3), round(fr, 2)
    return round(base * (W + wf * fr) / (W + wf), 3), round(fr, 2)

def crossed_gap(prev_px, cur_px, level, minutes, overshoot_pp, gap_min=20, overshoot_min=0.8):
    """True when the previous price was still safe of `level` and this fill went through it,
    either because the snapshots are far apart or the overshoot is large (percentage points)."""
    if prev_px is None or cur_px is None or level is None: return False
    down = prev_px > level >= cur_px
    up = prev_px < level <= cur_px
    if not (down or up): return False
    return (minutes or 0) > gap_min or (overshoot_pp or 0) > overshoot_min
