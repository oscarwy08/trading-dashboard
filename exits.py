"""Exit decisions shared by the 5-minute cycle and the always-on watcher.

The numbers (stop, trail, target, max hold, giveback) come from the config the
cycle already uses. This module does not change them. A reason string here is
the same string the cycle writes on a sell.
"""
import hashlib, time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import freshness


def dead_quote(age_min, dead_min):
    """A stored quote older than dead_min must not stop, trail, or flatten.

    Unknown age is dead. The position is unpriced until a quote inside the window arrives,
    and that first fresh print is the one that can sell.
    """
    if age_min is None:
        return True
    return float(age_min) > float(dead_min)


def gap_tag(pts, level, cur, reason):
    """Same gap note the cycle appends when the fill jumps through a level."""
    if not pts or len(pts) < 2 or not level or not reason:
        return reason
    t0, p0 = pts[-2]
    mins = (pts[-1][0] - t0).total_seconds() / 60.0
    over = abs(cur - level) / level * 100 if level else 0
    if freshness.crossed_gap(p0, cur, level, mins, over) and "[gap fill]" not in reason:
        return reason + " [gap fill]"
    return reason


def stock_flatten_reason(mins_to_close, exchange):
    if mins_to_close is None:
        return "flatten: market already closed - at the last (closing) price"
    return f"flatten {int(mins_to_close)} min before the {exchange} close (flat overnight)"


def day_flat_reason(mins, flat, e0, flatten_uk, entry_start_uk, fresh):
    why = (f"flatten {flatten_uk} UK (flat overnight)" if mins >= flat else
           f"flatten: overnight/before {entry_start_uk} UK" if mins < e0 else
           "flatten: carried from previous UK day")
    return why + ("" if fresh else f" [last price, after {flatten_uk}]")


def day_reason(entry, price, opened, md, sub_cfg, st, comb, info, now, fee_frac, buy_thr, asset, stop_pct=None,
               giveback_pct=None, defer_burst_target=False, arm_floor_pct=None):
    """Day stop / trail / target / max-hold / signal. Mutates md['peak'] and maybe md['runner'].

    Returns reason (or None), the runner note, and the gain figures the hold line uses.
    `price` and `entry` are GBP. The gain is net of the sell-side cost, same as the cycle.
    `stop_pct` overrides the stored stop distance when a variant book tightens or widens it.
    `giveback_pct` overrides the trail giveback once a burst is already a runner.
    `defer_burst_target` leaves a burst that has just hit its target for the caller (the
    burst-runner book). The watcher does not pass it, so a burst still sells at the target.
    `arm_floor_pct` replaces the fixed arm: the trail arms only when the locked share of the
    peak gain (percent) is at least this. None keeps trail_after_pct.
    """
    md["peak"] = max(float(md.get("peak") or 0), price)
    chg = price * (1 - fee_frac) / entry - 1
    pkg = md["peak"] * (1 - fee_frac) / entry - 1
    if giveback_pct is not None and md.get("runner"):
        gb = float(giveback_pct) / 100
    else:
        gb = float(sub_cfg.get("runner_giveback_pct", 20) if md.get("runner") else sub_cfg["trail_giveback_pct"]) / 100
    lock_pct = pkg * (1 - gb) * 100
    if arm_floor_pct is None:
        trail_on = pkg >= float(sub_cfg["trail_after_pct"]) / 100
    else:
        trail_on = lock_pct + 1e-9 >= float(arm_floor_pct)
    lvl = pkg * (1 - gb)
    held_h = (now - datetime.fromisoformat(opened)).total_seconds() / 3600
    sl = float(md["sl"] if stop_pct is None else stop_pct)
    reason = None
    note = None
    at_target = False
    if chg <= -sl / 100:
        reason = f"stop-loss {chg*100:.1f}% <= -{sl:.1f}% (ATR-scaled)"
        if abs(sl - float(md["sl"])) > 1e-9:
            reason += f" [stored -{float(md['sl']):.1f}%]"
    elif trail_on and chg <= lvl:
        reason = f"trailing stop {chg*100:+.1f}% (peak +{pkg*100:.1f}%, gives back {gb*100:.0f}% of the gain)"
    elif chg >= md["tp"] / 100 and not md.get("runner"):
        if md.get("sub") == "momentum" and sub_cfg.get("runner") and comb is not None and comb >= buy_thr:
            md["runner"] = True
            note = (f"[day] RUNNER {asset}: target +{md['tp']:.1f}% reached at {chg*100:+.1f}% with signal {comb} still a buy"
                    f" - trailing {float(sub_cfg.get('runner_giveback_pct', 20)):.0f}% of the gain")
        elif defer_burst_target and md.get("sub") == "burst":
            at_target = True
        else:
            reason = f"take-profit {chg*100:.1f}% >= {md['tp']:.1f}% (ATR-scaled)"
    elif md.get("sub") == "burst" and held_h >= float(sub_cfg["max_hold_h"]):
        reason = f"max hold {sub_cfg['max_hold_h']}h reached ({held_h:.1f}h), {chg*100:+.1f}%"
    elif md.get("sub") == "momentum" and comb is not None and comb <= st["sell_threshold"]:
        reason = f"signal {comb} <= {st['sell_threshold']} ({info})"
    return dict(reason=reason, runner=note, chg=chg, pkg=pkg, trail_on=trail_on, lvl=lvl, gb=gb,
                held_h=held_h, at_target=at_target)


def risk_reason(entry, price, peak, opened, rp, sent, now, fee_frac, stop_pct=None, keep_pct=None):
    """Risk stop / trail / max-hold / sentiment. `peak` is the stored peak price (GBP).
    `stop_pct` overrides the config stop when a variant book has tightened it.
    `keep_pct` replaces the price trail: once armed at trail_after_pct, sell when the net gain
    falls to this share of the peak net gain. The 7-day exit then waits while the trail is armed."""
    pk = max(float(peak or 0), price)
    chg = price * (1 - fee_frac) / entry - 1
    pk_gain = pk * (1 - fee_frac) / entry - 1
    held_d = (now - datetime.fromisoformat(opened)).total_seconds() / 86400
    trail_on = pk_gain >= rp["trail_after_pct"] / 100
    stop = float(rp["stop_loss_pct"] if stop_pct is None else stop_pct)
    if chg <= -stop / 100:
        reason = (f"risk stop-loss {chg*100:.1f}% <= -{rp['stop_loss_pct']}%" if abs(stop - float(rp["stop_loss_pct"])) < 1e-9
                  else f"risk stop-loss {chg*100:.1f}% <= -{stop:.1f}% (tightened from -{rp['stop_loss_pct']}%)")
    elif keep_pct is not None and trail_on and chg <= pk_gain * float(keep_pct) / 100.0:
        reason = (f"trailing stop {chg*100:+.1f}% (peak +{pk_gain*100:.1f}%, "
                  f"keeps {float(keep_pct):.0f}% of the gain)")
    elif keep_pct is None and trail_on and pk and price <= pk * (1 - rp["trail_pct"] / 100):
        reason = f"trailing take-profit: {(price / pk - 1) * 100:.1f}% from peak (peak +{pk_gain*100:.1f}%), now {chg*100:+.1f}%"
    elif held_d >= rp["max_hold_days"] and not (keep_pct is not None and trail_on):
        reason = f"max hold {rp['max_hold_days']} days reached ({held_d:.1f} d), {chg*100:+.1f}%"
    elif sent and sent["score"] <= rp["sentiment_exit"] and sent["confidence"] >= 0.6:
        reason = f"sentiment exit {sent['score']:+.2f} (conf {sent['confidence']:.2f})"
    else:
        reason = None
    if trail_on and keep_pct is not None:
        trail_bit = f", lock +{pk_gain * float(keep_pct):.1f}%"
    elif trail_on:
        trail_bit = f", trailing stop £{pk * (1 - rp['trail_pct'] / 100):.5g}"
    else:
        trail_bit = ""
    hold = f"open {chg*100:+.1f}%, peak +{pk_gain*100:.1f}%, {held_d:.1f} d" + trail_bit
    return dict(reason=reason, peak=pk, chg=chg, pk_gain=pk_gain, trail_on=trail_on, hold=hold)


@contextmanager
def book_lock(timeout=120, db_path=None):
    """Exclusive lock for one paper book. The cycle holds it for the whole run.
    The watcher holds it only while it decides and writes one tick. A second
    writer waits, then sees the position already open or already gone.

    db_path defaults to the process book. The entry watcher passes the variant
    file so a fill there does not take the main book's lock.
    """
    import fcntl
    from common import DB
    target = Path(db_path) if db_path else Path(DB)
    path = Path("/tmp") / ("trading-book-" + hashlib.sha1(str(target.resolve()).encode()).hexdigest()[:16] + ".lock")
    fh = open(path, "a+")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                fh.close()
                raise TimeoutError(f"paper book lock busy ({path})")
            time.sleep(0.05)
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
