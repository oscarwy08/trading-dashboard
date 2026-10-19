"""Parallel paper books. The main database is never written here.

Each name in config `variants.run` is a SQLite file under data/variants/, copied once from a snapshot
of the main book and then fed the same prices, sentiment, FX and collector screens. The trader runs
in a child process with TRADING_VARIANT set, so a crash or a rule change stays in that file.
Pre-trade quotes are not fetched again: TRADING_DB makes quotes.prefetch a no-op, and the rows the
main cycle just wrote are copied in before the child starts.
"""
import os, shutil, sqlite3, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

from common import DATA, DB, LON, ROOT, cfg, ensure_sentiment_cols, now

LABELS = {
    "ev_exit": "EV exit",
    "review_leftovers": "Review leftovers",
    "all_combined": "Both together",
    "burst_runner": "Burst runner",
    "everything": "Everything combined",
    "midnight_crypto": "Midnight crypto",
    "entry_watch": "Entry watch",
    "risk_giveback_15": "Risk giveback 15",
    "day_trail_arm": "Day trail arm",
    "day_ev_blend": "Day EV blend",
    "no_risk_clock": "No risk clock",
    "day_capital_caution": "Day capital caution",
    "partial_take": "Partial take",
    "sent_split": "Sentiment split",
    "best_of": "Best of",
    "btc_trend": "BTC trend",
    "btc_trend_vol": "BTC trend, vol sized",
    "majors_rotation": "Majors rotation",
    "btc_core_satellite": "BTC core + satellite",
    "rs_rotation_veto": "RS rotation, veto",
    "quiet_strength": "Quiet strength",
    "best_of_plus": "Best of plus trend",
}
BLURB = {
    "ev_exit": "EV exit is off. Live expected value rises when the trade falls, so it was selling winners between about +1% and +5%. This book was reset; sells before the reset are not counted. It follows the main book until the upside is taken from the current price and the chance is recomputed.",
    "review_leftovers": "Burst stop is 1.5x wider unless live EV is already below -0.4%. Core's 18:00 fallback tranche is half size when the coin is up more than 15% over 7 days or RSI is above 75. New Day, Risk and Core entries need the coin's own 3-day trend up. After 12:00 UK, Day and Core will not each buy the same coin.",
    "all_combined": "The review leftovers, on one book. The EV exit is off (live expected value was inverted) and this book was reset; sells before the reset are not counted. The burst runner is a separate book, not part of this one.",
    "burst_runner": "Same rules as the main book, except a Burst position that hits its target keeps running when the entry signal is still intact. The trail then gives back 20% of the peak gain. Each sell says sold at target or ran, with the extra gain and the giveback.",
    "everything": "The review leftovers and the burst runner, on one book. The EV exit is off (live expected value was inverted) and this book was reset; sells before the reset are not counted.",
    "midnight_crypto": "Same as the main book, except Day crypto entries run until 23:45 UK and those positions flatten at 00:00 UK. New crypto entries are allowed again from 00:00, and the daily loss budget still resets then. Stocks still flatten 15 minutes before their market closes. Copied from the same snapshot as the other books.",
    "entry_watch": "Same scoring as the main book. Setups the cycle would buy are also armed with a price trigger, a size, and the same stop and target. This is the only book the live watcher is allowed to buy when that trigger trades between cycles. The main book only logs those as WOULD BUY.",
    "risk_giveback_15": "Risk only. The trail still arms at +15% net. Once armed, it sells when the net gain falls to 65% of the peak net gain, and the 7-day exit waits while that trail is armed. The -12% stop stays. Not combined with the other paper books.",
    "day_trail_arm": "Day only. A trail arms only when 60% of the peak gain is at least the greater of 4x that trade's round-trip cost and 1x its 1-hour ATR. Once armed, a crossed trail sells. Stop, target, the burst 6-hour hold and the 22:00 flatten stay.",
    "day_ev_blend": "Day only. Each sub-strategy's expected value is its measured expectancy blended with the model: (n x measured + 10 x model) / (n + 10). Momentum and burst are measured apart. The Risk win is not changed on this book.",
    "no_risk_clock": "Risk only. The 24-hour re-entry clock is deleted. The next setup buys when its own confidence and expected value clear. No new-high gate and no stop-out cooldown.",
    "day_capital_caution": "Day only, per account. f is that account's Day loss today divided by 15% of that account's start-of-day Day equity, with no cap at 1. Momentum's threshold is its base (0.35, or 0.30 on MON, WLD and XRP) plus 0.30 x f. Burst uses the same f. Size is multiplied by (1 - min(f, 1)). The -15% halt on the whole Day pot stays.",
    "partial_take": "Day and Risk. At the trail arm, sell half and leave the rest on this book's trail. Buy that half back on a later dip only when a fresh expected value clears the hurdle. The fresh value uses a recomputed chance and the percent still available from the current price, not the inverted live EV. Orders in this split may be £1. Every other book keeps the £5 minimum. The sell records the extra fee of splitting one exit into two.",
    "sent_split": "Burst and Risk. A missing sentiment print scores 0.50, the same as a real neutral, instead of 0.25. A Risk dip needs the 48-hour uptrend; positive sentiment does not stand in for it. When price is up 15% or more over 24 hours (5% or more over 4 hours on a Day momentum entry) and sentiment is at least 0.30, or the buzz change is 3 or more, burst and Risk confidence lose 0.10 and that Day momentum entry ignores sentiment. A catalyst score, when the file has one, is used instead of the raw score. A quiet tape (buzz change under 1.2) with volume at least 2x and a confirmed breakout or bounce adds 0.05. Files without those fields score as before.",
    "best_of": "Sentiment split, the entry watcher and the burst runner, and nothing else. A missing sentiment print is neutral (0.50). A Risk dip needs the 48-hour uptrend. Hot run-up and quiet-tape adjustments are the sentiment-split numbers. Armed entries expire after 15 minutes. A burst that hits its target can keep running and then gives back 20% of the peak gain. The EV exit stays off.",
    "btc_trend": "Crypto is 100% BTC when the daily close is above its 50-day average, otherwise cash. Stocks is the same rule on VUSA, or EQQQ if VUSA has no history. Checked once a day. Held through nights and weekends.",
    "btc_trend_vol": "The same trend book, sized at min(1, 40% / 30-day realised vol). A weight only moves when it has drifted more than 5 points.",
    "majors_rotation": "Once a week, hold the best 28-day return among BTC, ETH and SOL that are above their own 50-day average. Cash when none are. The stock half stays in cash.",
    "btc_core_satellite": "85% BTC, put back when it drifts more than 5 points, plus 15% in one coin that has beaten BTC over 30 and 90 days and is above its 100-day average. The satellite is held at least 5 days.",
    "rs_rotation_veto": "Once a week, the top 3 by 4-week return minus BTC, above their 50-day average, skipping a buzz change of 3 or more or a 24h jump of 15% or more. BTC when none qualify. Cash when BTC is below its 50-day average.",
    "quiet_strength": "Within 10% of the 30-day high, buzz below the median, top quartile of volume over market cap. Sold on a 12% trail or into a hype spike. Held through nights and weekends.",
    "best_of_plus": "Sentiment split, the 15-minute entry watch and the burst runner may use 15% of the book. The other 85% is the BTC / VUSA trend. Nothing flattens at 22:00.",
}
INPUT_STATE = ("risk_screen", "day_excluded", "risk_only", "risk_only_stocks", "gas")
MARKET = (("prices", "ts"), ("sentiment", "ts"), ("fx", "ts"), ("daily_closes", "d"))


def rules_of(c):
    """Flag values. Missing keys stay off, which is the main book's behaviour."""
    d = dict(ev_exit=False, ev_action=False, ev_exit_now_pct=-0.4, ev_exit_below_entry_pp=0.8, ev_exit_stop_mult=0.75,
             burst_stop_widen=False, burst_stop_mult=1.5, core_fallback_half=False, core_ext_7d_pct=15.0,
             core_ext_rsi=75.0, entry_own_trend=False, entry_trend_days=3, no_day_core_same_afternoon=False,
             afternoon_hour_uk=12, burst_runner=False, burst_runner_giveback_pct=20,
             midnight_crypto=False, midnight_entry_start_uk="00:00", midnight_entry_cutoff_uk="23:45",
             midnight_flatten_uk="00:00", entry_watch=False, entry_watch_expiry_min=15,
             risk_giveback_15=False, risk_giveback_keep_pct=65,
             day_trail_arm=False, day_trail_cost_x=4, day_trail_atr_x=1,
             day_ev_blend=False,
             no_risk_clock=False,
             day_capital_caution=False, day_caution_raise=0.30,
             risk_ev_legacy_win=False,
             partial_take=False, partial_min_gbp=1.0,
             sent_split=False, sent_hot=0.3, sent_overheat=0.10, sent_quiet_bonus=0.05,
             sent_runup_24h_pct=15.0, sent_runup_4h_pct=5.0, sent_buzz_hot=3.0,
             sent_buzz_quiet=1.2, sent_quiet_vol_x=2.0,
             hold_overnight=False, day_risk_cap_pct=0, core_off=False)
    for k, v in ((c or {}).get("variant_rules") or {}).items():
        if k in d and v is not None:
            d[k] = v
    return d


def active(R):
    return bool(R.get("ev_exit") or R.get("burst_stop_widen") or R.get("core_fallback_half")
                or R.get("entry_own_trend") or R.get("no_day_core_same_afternoon") or R.get("burst_runner")
                or R.get("midnight_crypto") or R.get("entry_watch")
                or R.get("risk_giveback_15") or R.get("day_trail_arm") or R.get("day_ev_blend")
                or R.get("no_risk_clock") or R.get("day_capital_caution") or R.get("partial_take")
                or R.get("sent_split"))


TABLE_BOOKS = ("risk_giveback_15", "day_trail_arm", "day_ev_blend", "no_risk_clock",
               "day_capital_caution", "partial_take", "sent_split", "best_of")


def blend_ev(model, n, exp, prior_n=10):
    """(n x measured expectancy + prior_n x model) / (n + prior_n). The model stands alone until a close exists."""
    model = float(model)
    if not n or exp is None:
        return round(model, 3)
    n, prior = float(n), float(prior_n)
    return round((n * float(exp) + prior * model) / (n + prior), 3)


def caution_f(pnl_frac, cap_pct):
    """That account's Day loss today / the daily-stop fraction. No cap at 1. A gain is 0."""
    if not cap_pct or pnl_frac is None or float(pnl_frac) >= 0:
        return 0.0
    return (-float(pnl_frac)) / (float(cap_pct) / 100.0)


def caution_threshold(base, f, raise_):
    """Base plus raise x f. f is not clipped at 1, so the hurdle keeps rising past the daily stop."""
    return round(min(0.99, float(base) + float(raise_) * max(0.0, float(f))), 3)


def caution_size_scale(f):
    """1 at no loss, 0 once f reaches 1. Size is multiplied by this. f above 1 does not go negative."""
    return max(0.0, 1.0 - min(max(0.0, float(f)), 1.0))


def day_arm_bar(R, rt_cost_pct, atr_pct):
    """Percent the locked share of the peak gain must clear before a Day trail may arm.

    None on the main book. Otherwise max(cost_x x round-trip cost, atr_x x 1-hour ATR).
    The lock is 60% of the peak when the trail gives back 40%.
    """
    if not R.get("day_trail_arm"):
        return None
    cost = 0.0 if rt_cost_pct is None else float(rt_cost_pct)
    atr = 0.0 if not atr_pct else float(atr_pct)
    return max(float(R.get("day_trail_cost_x", 4)) * cost, float(R.get("day_trail_atr_x", 1)) * atr)


def risk_lock_pct(R):
    """Percent the Risk trail locks when it first arms: (1 + arm) * (1 - trail) - 1.

    At +15% and a 10% give-back from that price this is 3.5%. It is the win a fresh
    expected value uses, measured from the current price rather than from the entry.
    """
    arm = float((R or {}).get("trail_after_pct", 15))
    trail = float((R or {}).get("trail_pct", 10))
    return (100.0 + arm) * (100.0 - trail) / 100.0 - 100.0


def fresh_ev(p, upside_pct, downside_pct, rt_cost_pct):
    """Expected value in % from the current price.

    Upside and downside are the percent distances from here (a new target and stop),
    not the residual to the prices stored at entry. p is recomputed, not the frozen
    entry chance. EV = p * upside - (1 - p) * downside - round-trip cost.
    The stored live EV does the opposite: it rises when the trade falls.
    """
    if p is None:
        p = 0.5
    p = max(0.0, min(1.0, float(p)))
    return round(float(p) * float(upside_pct) - (1.0 - float(p)) * float(downside_pct) - float(rt_cost_pct), 3)


def partial_half(qty, price, min_gbp=1.0):
    """(half quantity, half notional) when that half is at least min_gbp. Otherwise (None, None)."""
    if not qty or not price or float(price) <= 0:
        return None, None
    half = float(qty) / 2.0
    notional = half * float(price)
    if notional + 1e-9 < float(min_gbp):
        return None, None
    return half, notional


def split_extra_gbp(full_notional, half_notional, fee_full_frac, fee_half_frac, gas_gbp, klass):
    """Pounds two half-sells cost above one sell of the whole position.

    Percentage fees on the two halves sum to the fee on the whole. Crypto pays gas
    on each fill, so the second fill's gas is the extra. A stock has no gas.
    """
    gas_each = 0.0 if klass == "stock" else float(gas_gbp or 0.0)
    two = 2.0 * (float(half_notional) * float(fee_half_frac) + gas_each)
    one = float(full_notional) * float(fee_full_frac) + gas_each
    return round(two - one, 4)


def partial_reason(arm_pct, extra_gbp):
    return (f"partial take: sold half at the +{float(arm_pct):g}% arm; "
            f"extra fee of the split £{float(extra_gbp):.4f} (two half-sells versus one full sell)")


def buyback_ok(price, sold_px, ev, hurdle):
    """True on a later dip (price under the half-sale) when fresh EV clears the hurdle."""
    if price is None or sold_px is None or float(price) >= float(sold_px) - 1e-12:
        return False
    if ev is None:
        return False
    return float(ev) > float(hurdle)


def risk_on_clock(R, last_exit, t, cooldown_h):
    """True when the re-entry clock still blocks. no_risk_clock deletes it.

    There is no new-high gate and no separate cooldown after a stop.
    """
    if R.get("no_risk_clock") or not last_exit:
        return False
    hours = float(cooldown_h or 0)
    if hours <= 0:
        return False
    return datetime.fromisoformat(str(last_exit)) > t - timedelta(hours=hours)


def crypto_hours_to_flat(mins, flat_mins, midnight):
    """Hours until Day crypto is flattened.

    The main book uses flatten_uk (22:00). At or after that clock the hours are 0.
    Midnight crypto uses the next 00:00 UK. At exactly 00:00 a new entry has 24 hours,
    because yesterday's positions are flattened before the entry loop runs.
    """
    mins = int(mins)
    if midnight:
        left = (24 * 60 - mins) % (24 * 60)
        if left == 0:
            left = 24 * 60
        return left / 60.0
    return max(0.0, (int(flat_mins) - mins) / 60.0)


def crypto_entry_open(mins, start_mins, cutoff_mins):
    """True from start_mins inclusive until cutoff_mins exclusive. 00:00 is open; 23:45 is closed."""
    return int(start_mins) <= int(mins) < int(cutoff_mins)


def crypto_flatten_due(opened_day, today, flat_now, midnight):
    """Main book: the 22:00 / before-08:00 window, or a position opened on an earlier UK day.
    Midnight crypto: only a position opened on an earlier UK day (the 00:00 flatten).
    A position opened today stays open through 22:00 and through 23:45."""
    if midnight:
        return str(opened_day) != str(today)
    return bool(flat_now) or str(opened_day) != str(today)


EV_NOTE = ("EV exit is off. Live expected value rises when the trade falls, because the upside is the distance "
           "to the stored target and the chance is not recomputed, so a negative reading is a winner. "
           "ev_exit, all_combined and everything were reset; sells before the reset are not counted. "
           "The exit stays off until the upside is taken from the current price and the chance is recomputed.")


def ev_action(ev_now, entry_ev, R):
    """None, 'tighten', or 'exit'. Exit when EV now is below ev_exit_now_pct and (if an entry EV was
    stored) at least ev_exit_below_entry_pp worse than that entry EV. Tighten when EV now is negative
    but the exit line has not been crossed.

    Off unless both ev_exit and ev_action are on. ev_action stays off: the stored formula rises as the
    trade falls, so it must not sell or tighten a stop until the upside is taken from the current price
    and the chance is recomputed."""
    if not R.get("ev_exit") or not R.get("ev_action") or ev_now is None:
        return None
    line, gap = float(R["ev_exit_now_pct"]), float(R["ev_exit_below_entry_pp"])
    worse = entry_ev is None or float(ev_now) <= float(entry_ev) - gap
    if float(ev_now) < line and worse:
        return "exit"
    if float(ev_now) < 0:
        return "tighten"
    return None


def ev_exit_reason(ev_now, entry_ev, R):
    line, gap = float(R["ev_exit_now_pct"]), float(R["ev_exit_below_entry_pp"])
    if entry_ev is None:
        return f"EV exit: EV now {float(ev_now):+.2f}% < {line:g}% (no entry EV stored)"
    return (f"EV exit: EV now {float(ev_now):+.2f}% < {line:g}% and "
            f"{float(entry_ev) - float(ev_now):.2f}pp below entry EV {float(entry_ev):+.2f}% (need {gap:g}pp)")


def _below_line(ev_now, R):
    return ev_now is not None and float(ev_now) < float(R["ev_exit_now_pct"])


def effective_stop_pct(stored, ev_now, R, kind):
    """Stop distance in percent (a positive number). The wider burst stop is not used once EV now is
    below ev_exit_now_pct. A negative EV that has not reached the exit line tightens the stop."""
    sl = float(stored)
    if kind == "burst" and R.get("burst_stop_widen") and not _below_line(ev_now, R):
        sl *= float(R["burst_stop_mult"])
    if ev_action(ev_now, None, R) == "tighten":
        # ev_action with no entry EV exits on the absolute line, so 'tighten' here is only the band
        # between 0 and that line. Apply it to the (possibly already widened) stop.
        if not _below_line(ev_now, R):
            sl *= float(R["ev_exit_stop_mult"])
    return sl


def ret_over_days(pts, days):
    """Percent change versus the last price at or before `days` ago. None without that history."""
    if not pts:
        return None
    t, p = pts[-1]
    old = [x for ts, x in pts if ts <= t - timedelta(days=days)]
    if not old or not old[-1]:
        return None
    return (p / old[-1] - 1) * 100


def trend_up(pts, days):
    r = ret_over_days(pts, days)
    if r is None:
        return None
    return r > 0


def trend_lookback_h(R):
    """Hours of prices the entry check needs. history()'s default is 72h, which ends exactly `entry_trend_days`
    before now. The last tick is a few minutes earlier than now, so that window has no price at or before
    (last tick - 3 days) and every entry would be blocked for missing history. Six extra hours covers the gap."""
    return int(R["entry_trend_days"]) * 24 + 6


def own_trend_block(pts, R):
    if not R.get("entry_own_trend"):
        return None
    days = int(R["entry_trend_days"])
    ok = trend_up(pts, days)
    if ok is None:
        return f"own {days}d trend: not enough history"
    if not ok:
        return f"own {days}d trend is not up"
    return None


def bought_this_afternoon(con, sleeve, asset, lt, hour):
    """True when `sleeve` bought `asset` today at or after `hour` UK. An older hold does not count."""
    if lt.hour < hour:
        return False
    row = con.execute("SELECT opened FROM positions WHERE sleeve=? AND asset=?", (sleeve, asset)).fetchone()
    if row and row[0]:
        d = datetime.fromisoformat(row[0]).astimezone(LON)
        if d.date() == lt.date() and d.hour >= hour:
            return True
    for (ts,) in con.execute("SELECT ts FROM trades WHERE sleeve=? AND asset=? AND side='BUY'", (sleeve, asset)):
        d = datetime.fromisoformat(ts).astimezone(LON)
        if d.date() == lt.date() and d.hour >= hour:
            return True
    return False


def entry_block(con, others, asset, pts, lt, R):
    """Reason to skip a new entry, or None. `others` is the sleeves that must not have bought today."""
    msg = own_trend_block(pts, R)
    if msg:
        return msg
    if R.get("no_day_core_same_afternoon"):
        for oth in others:
            if bought_this_afternoon(con, oth, asset, lt, int(R["afternoon_hour_uk"])):
                return f"{oth} already bought {asset} this afternoon"
    return None


def burst_target_action(breakout, volume_ok, rsi, rsi_max, sentiment_ok, market_ok, conf, threshold):
    """At a burst take-profit: keep the position only when the entry signal is still a buy.

    The same gates as a new burst entry: the breakout is still there, volume has not faded,
    RSI is under the entry cap, sentiment and the market are still acceptable, and confidence
    is still at least the entry threshold. Returns (keep, why).
    """
    fails = []
    if not breakout:
        fails.append("breakout no longer intact")
    if not volume_ok:
        fails.append("volume faded")
    if rsi is not None and float(rsi) >= float(rsi_max):
        fails.append(f"RSI {float(rsi):.0f} >= {float(rsi_max):g}")
    if not sentiment_ok:
        fails.append("sentiment below the entry line")
    if not market_ok:
        fails.append("market no longer supports it")
    shown = 0.0 if conf is None else float(conf)
    if conf is None or shown < float(threshold):
        fails.append(f"confidence {shown:.2f} < {float(threshold):.2f}")
    if fails:
        return False, "; ".join(fails)
    return True, f"confidence {shown:.2f} >= {float(threshold):.2f}"


def sold_at_target_note(why):
    return f"burst runner: sold at target ({why})"


def ran_note(chg, peak_gain, target_pct, giveback_pct):
    """Extra gain is the exit versus the target. Giveback is how far the exit is under the peak."""
    extra = float(chg) * 100 - float(target_pct)
    gave = (float(peak_gain) - float(chg)) * 100
    return (f"burst runner: ran, extra {extra:+.1f}pp vs target +{float(target_pct):.1f}%, "
            f"giveback {gave:.1f}pp ({float(giveback_pct):.0f}% of peak +{float(peak_gain)*100:.1f}%)")


def fallback_fraction(ret_7d, rsi, R):
    """1.0, or 0.5 when the 18:00 fallback is on and the coin is extended."""
    if not R.get("core_fallback_half"):
        return 1.0
    extended = ((ret_7d is not None and ret_7d > float(R["core_ext_7d_pct"]))
                or (rsi is not None and rsi > float(R["core_ext_rsi"])))
    return 0.5 if extended else 1.0


def extended_note(ret_7d, rsi, R):
    bits = []
    if ret_7d is not None and ret_7d > float(R["core_ext_7d_pct"]):
        bits.append(f"7d {ret_7d:+.0f}% > {float(R['core_ext_7d_pct']):g}%")
    if rsi is not None and rsi > float(R["core_ext_rsi"]):
        bits.append(f"RSI {rsi:.0f} > {float(R['core_ext_rsi']):g}")
    return ", ".join(bits)


def _opt_float(row, key):
    """A stored number, or None when the column or the value is absent."""
    if row is None:
        return None
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return None
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return None
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sent_score(row, R):
    """Sentiment score for one book.

    The main book uses the raw score. sent_split uses catalyst_score when the row
    has one, and the raw score when the file did not carry that field.
    """
    raw = _opt_float(row, "score")
    if not (R or {}).get("sent_split"):
        return raw
    cat = _opt_float(row, "catalyst_score")
    return raw if cat is None else cat


def buzz_of(row):
    """buzz_change, or None when the file did not carry it."""
    return _opt_float(row, "buzz_change")


def price_runup_pct(pts, hours):
    """Percent change versus the last price at or before `hours` ago. None without that print."""
    if not pts:
        return None
    t, p = pts[-1]
    old = [x for ts, x in pts if ts <= t - timedelta(hours=float(hours))]
    if not old or not old[-1]:
        return None
    return (p / old[-1] - 1) * 100


def _hot(sent, buzz, R):
    """True when sentiment is at least sent_hot, or buzz_change is at least sent_buzz_hot.

    A missing score does not fire the sentiment half. A missing buzz does not fire the buzz half.
    """
    hot_sent = sent is not None and float(sent) >= float((R or {}).get("sent_hot", 0.3))
    hot_buzz = buzz is not None and float(buzz) >= float((R or {}).get("sent_buzz_hot", 3))
    return hot_sent or hot_buzz


def confidence_adjust(conf, R, runup_pct, sent, buzz, volume, confirmed):
    """(confidence, note). The main book returns the confidence unchanged.

    On sent_split, a 24h run-up at or above sent_runup_24h_pct together with a hot
    sentiment score or a hot buzz change subtracts sent_overheat. A quiet buzz
    (below sent_buzz_quiet), volume at least sent_quiet_vol_x, and a confirmed
    breakout or bounce adds sent_quiet_bonus. The two can both apply. The result
    stays inside 0 to 1.
    """
    if conf is None or not (R or {}).get("sent_split"):
        return conf, None
    delta = 0.0
    notes = []
    line = float(R.get("sent_runup_24h_pct", 15))
    if runup_pct is not None and float(runup_pct) >= line and _hot(sent, buzz, R):
        cut = float(R.get("sent_overheat", 0.10))
        delta -= cut
        notes.append(f"overheat -{cut:.2f}")
    quiet_line = float(R.get("sent_buzz_quiet", 1.2))
    vol_line = float(R.get("sent_quiet_vol_x", 2))
    if (buzz is not None and float(buzz) < quiet_line and volume is not None
            and float(volume) >= vol_line and confirmed):
        add = float(R.get("sent_quiet_bonus", 0.05))
        delta += add
        notes.append(f"quiet strength +{add:.2f}")
    if not delta:
        return conf, None
    return round(max(0.0, min(1.0, float(conf) + delta)), 3), "; ".join(notes)


def momentum_weight(weight, pts, sent, buzz, R):
    """Day momentum sentiment weight for one entry.

    The main book returns `weight`. sent_split returns 0 when the 4h run-up is at
    least sent_runup_4h_pct and sentiment or buzz is hot.
    """
    if not (R or {}).get("sent_split"):
        return weight
    run = price_runup_pct(pts, 4)
    if run is None or run < float(R.get("sent_runup_4h_pct", 5)):
        return weight
    if _hot(sent, buzz, R):
        return 0.0
    return weight


def momentum_blend(momentum, weight, sent):
    """Combined Day score. A weight of 0 leaves the momentum score alone."""
    if momentum is None:
        return None
    w = float(weight or 0)
    s = 0.0 if sent is None else float(sent)
    return round((1.0 - w) * float(momentum) + w * s, 3)


def dir_path():
    return Path(os.environ.get("VARIANTS_DIR") or DATA / "variants")


def main_db():
    return Path(os.environ.get("VARIANTS_MAIN_DB") or DB)


def _backup(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close(); s.close()


def reset_map(c=None):
    """Books whose database is recopied from the current main book when the generation changes.

    A missing stamp on an existing file counts as a change, so the first cycle after this
    generation is published drops the old sells. A file created after that is copied from
    the shared snapshot and stamped, and is not reset again.
    """
    c = c or cfg()
    raw = ((c.get("variants") or {}).get("reset") or {})
    return {str(k): str(v) for k, v in raw.items() if v}


def _read_reset(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM state WHERE key='variant_reset'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row or not row[0]:
        return None
    try:
        import json
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _stamp_reset(path, generation, ts=None, equity=None):
    import json
    con = sqlite3.connect(path)
    try:
        con.execute("INSERT OR REPLACE INTO state VALUES('variant_reset',?)",
                    (json.dumps(dict(generation=generation, ts=ts, equity=equity)),))
        con.commit()
    finally:
        con.close()


def _equity_now(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return None if not row else row[0]


def ensure_snapshot(root=None):
    """Copy the main book once. Later cycles keep this file so every variant shares the start."""
    root = root or dir_path()
    root.mkdir(parents=True, exist_ok=True)
    snap = root / "_snapshot.db"
    man = root / "manifest.json"
    if snap.exists() and man.exists():
        return snap
    src = main_db()
    _backup(src, snap)
    import json
    con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT equity, ts FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        con.close()
    man.write_text(json.dumps(dict(created=now().isoformat(), source=str(src),
                                   equity=None if not row else row[0], equity_ts=None if not row else row[1]), indent=2))
    return snap


def _sync(src_path, dst_path):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    try:
        ensure_sentiment_cols(dst)
        for table, col in MARKET:
            scols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            dcols = {r[1] for r in dst.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in scols if c in dcols]
            if not cols or col not in cols:
                continue
            mx = dst.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
            q = f"SELECT {','.join(cols)} FROM {table}"
            args = ()
            if mx:
                q += f" WHERE {col}>?"
                args = (mx,)
            rows = src.execute(q, args).fetchall()
            if rows:
                ph = ",".join("?" * len(cols))
                dst.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})", rows)
        for key in INPUT_STATE:
            row = src.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            if row:
                dst.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, row[0]))
        dst.commit()
    finally:
        dst.close(); src.close()


def prepare(name, root=None):
    root = root or dir_path()
    snap = ensure_snapshot(root)
    path = root / f"{name}.db"
    gen = reset_map().get(name)
    if path.exists() and gen:
        stored = _read_reset(path) or {}
        if stored.get("generation") != gen:
            path.unlink()
            _backup(main_db(), path)
            _stamp_reset(path, gen, now().isoformat(), _equity_now(path))
    if not path.exists():
        shutil.copy(snap, path)
        if gen:
            _stamp_reset(path, gen, None, None)
    _sync(main_db(), path)
    return path


def _trade_child(name):
    """common.DB is bound when this process imported common, which is before TRADING_DB exists.
    Point that global at the variant file, then run the trader. A strategy book runs strategies.py
    instead, so it does not inherit the main book's entries or the 22:00 flatten."""
    import common
    import strategies
    os.environ["TRADING_VARIANT"] = name
    if name in strategies.PURE:
        path = strategies.prepare_book(name)
        common.DB = path
        os.environ["TRADING_DB"] = str(path)
        os.environ.setdefault("TRADING_LOG", str(ROOT / "logs" / f"variant-{name}.log"))
        strategies.run(name, path)
        return
    path = prepare(name)
    common.DB = path
    os.environ["TRADING_DB"] = str(path)
    os.environ.setdefault("TRADING_LOG", str(ROOT / "logs" / f"variant-{name}.log"))
    if name == "best_of_plus":
        strategies.allocate_best_of_plus(path)
    import trader
    trader.main()
    if name == "best_of_plus":
        strategies.run(name, path)


def worker_count():
    """How many variant books to trade at once.

    Each book is its own database and already refuses a second price fetch
    (TRADING_OFFLINE). VARIANTS_WORKERS overrides. The default is at most 4,
    and never more cores than the machine has.
    """
    raw = os.environ.get("VARIANTS_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, min(4, os.cpu_count() or 2))


def _run_one(name):
    """Trade one book in a child process. Returns (name, seconds, status, detail)."""
    one = time.perf_counter()
    env = os.environ.copy()
    env["TRADING_VARIANT"] = name
    env["VARIANTS_DIR"] = str(dir_path())
    env["VARIANTS_MAIN_DB"] = str(main_db())
    env["TRADING_OFFLINE"] = "1"   # the child reads prices the main cycle already stored
    env.pop("TRADING_DB", None)
    try:
        r = subprocess.run([sys.executable, str(ROOT / "variants.py"), "--trade", name],
                           cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return name, time.perf_counter() - one, "timeout", ""
    except Exception as e:
        return name, time.perf_counter() - one, "error", str(e)
    dt = time.perf_counter() - one
    tail = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("[trader") or ln.startswith("[strategy")]
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return name, dt, "failed", (err[-1] if err else "")
    return name, dt, "ok", (tail[-1] if tail else "")


def run_all():
    """After the main trader. Failures are printed and skipped. Returns wall-clock seconds.

    Books run together, up to worker_count() at a time. The snapshot is taken
    first so the children do not copy it at the same moment. Rules are the
    overlay for that book, the same as a serial run.
    """
    from concurrent.futures import ThreadPoolExecutor
    c = cfg()
    block = c.get("variants") or {}
    names = list(block.get("run") or [])
    if not block.get("enabled") or not names:
        print("[variants] off")
        return 0.0
    runnable = []
    for name in names:
        if not (ROOT / "config" / "variants" / f"{name}.yaml").exists():
            print(f"[variants] {name} has no overlay, skipped")
            continue
        runnable.append(name)
    if not runnable:
        print("[variants] off")
        return 0.0
    ensure_snapshot()
    t0 = time.perf_counter()
    n_workers = min(worker_count(), len(runnable))
    results = {}
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [ex.submit(_run_one, name) for name in runnable]
        for fut in futs:
            name, dt, status, detail = fut.result()
            results[name] = (dt, status, detail)
    parts = []
    for name in runnable:
        dt, status, detail = results[name]
        if status == "timeout":
            print(f"[variants] {name} timed out after 180s")
            parts.append(f"{name} timeout")
        elif status == "error":
            print(f"[variants] {name} failed to start: {detail}")
            parts.append(f"{name} error")
        elif status == "failed":
            print(f"[variants] {name} failed in {dt:.1f}s" + (f": {detail}" if detail else ""))
            parts.append(f"{name} failed {dt:.1f}s")
        else:
            print(f"[variants] {name} {dt:.1f}s" + (f" | {detail}" if detail else ""))
            parts.append(f"{name} {dt:.1f}s")
    total = time.perf_counter() - t0
    print(f"[variants] total {total:.1f}s on {n_workers} workers ({', '.join(parts)})")
    return total


def _curve(con, since, start=None):
    from dashboard import partial_equity_point
    rows = con.execute("SELECT ts, equity FROM equity WHERE ts>=? ORDER BY ts", (since or "",)).fetchall()
    rows = [r for r in rows if not partial_equity_point(r[0], r[1], rows, start)]
    if len(rows) > 400:
        step = (len(rows) - 1) / 399
        rows = [rows[min(int(i * step), len(rows) - 1)] for i in range(400)]
    out, seen = [], set()
    for r in rows:
        try:
            t = int(datetime.fromisoformat(r[0]).timestamp())
        except Exception:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(dict(t=t, eq=round(r[1], 2)))
    return out


def _since(con, since, start_equity):
    """Comparison figures from the shared snapshot. Lifetime stats would hide a new rule under the identical history."""
    from dashboard import stats
    st = stats(con)
    base = start_equity if start_equity is not None else st["start"]
    if not since:
        return dict(equity=st["equity"], start=base, pnl=st["equity"] - base, trades=st["trades"], closed=st["closed"],
                    win_rate=st["win_rate"], max_dd=st["max_dd"], realized=st["realized"])
    sells = [r[0] for r in con.execute("SELECT pnl FROM trades WHERE side='SELL' AND ts>?", (since,))]
    nt = con.execute("SELECT COUNT(*) FROM trades WHERE side IN ('BUY','SELL') AND ts>?", (since,)).fetchone()[0]
    from dashboard import curve_drawdown
    pts = [(r[0], r[1]) for r in con.execute("SELECT ts, equity FROM equity WHERE ts>=? ORDER BY ts", (since or "",))]
    mdd = curve_drawdown(pts, base)
    return dict(equity=st["equity"], start=base, pnl=st["equity"] - base, trades=nt, closed=len(sells),
                win_rate=(sum((x or 0) > 0 for x in sells) / len(sells)) if sells else None,
                max_dd=mdd, realized=sum(x or 0 for x in sells))


def sleeve_table(con):
    """Momentum, burst and risk rows in the Day sub-strategy table shape.

    Closed-trade columns come from the same stats the Day and Risk pages use.
    Budget is that sleeve's equity (cash + open positions). Open P&L is value minus cost.
    """
    from dashboard import day_sub_stats, risk_stats
    c = cfg()
    lt = now().astimezone(LON)
    day = day_sub_stats(con, lt)
    risk = risk_stats(con, lt).get("all:all") or {}
    rate = None
    try:
        from common import usd_gbp
        fx = usd_gbp(con, (c.get("fx") or {}).get("max_age_h", 12))
        rate = fx[0] if fx else None
    except Exception:
        rate = None
    px = {}
    try:
        for asset, price in con.execute(
                """SELECT asset, price FROM (
                     SELECT asset, price, ROW_NUMBER() OVER (PARTITION BY asset ORDER BY ts DESC, rowid DESC) rn
                     FROM prices WHERE source!='backfill') WHERE rn=1"""):
            px[asset] = price
    except Exception:
        px = {}
    import json as _json
    try:
        dmeta = _json.loads((con.execute("SELECT value FROM state WHERE key='day_meta'").fetchone() or ["{}"])[0]) or {}
    except Exception:
        dmeta = {}
    open_day = {"momentum": 0.0, "burst": 0.0}
    open_risk = 0.0
    cost_day = {"momentum": 0.0, "burst": 0.0}
    val_day = {"momentum": 0.0, "burst": 0.0}
    cost_risk = val_risk = 0.0
    for r in con.execute("SELECT sleeve, asset, klass, qty, cost FROM positions"):
        usd = px.get(r["asset"])
        val = (r["qty"] * usd * rate) if (usd is not None and rate) else (r["cost"] or 0)
        pnl = (val or 0) - (r["cost"] or 0)
        if r["sleeve"] == "day":
            sb = (dmeta.get(r["asset"]) or {}).get("sub") or "momentum"
            if sb not in open_day:
                sb = "momentum"
            open_day[sb] += pnl
            cost_day[sb] += r["cost"] or 0
            val_day[sb] += val or 0
        elif r["sleeve"] == "risk":
            open_risk += pnl
            cost_risk += r["cost"] or 0
            val_risk += val or 0
    cash = {}
    try:
        for sl, ac, amt in con.execute("SELECT sleeve, account, amount FROM acct_cash"):
            cash[(sl, ac)] = amt or 0
    except Exception:
        cash = {}
    risk_cash = sum(v for (sl, _), v in cash.items() if sl == "risk")
    day_budget = {"momentum": None, "burst": None}
    try:
        import daypot
        from common import acct_start, loans_open
        base = acct_start(con, c)
        sh = daypot.sub_shares(c["day"])
        since = ""
        try:
            raw = con.execute("SELECT value FROM state WHERE key='account_split'").fetchone()
            since = (_json.loads(raw[0]) or {}).get("ts", "") if raw else ""
        except Exception:
            since = ""
        held = []
        for r in con.execute("SELECT asset, klass, qty, cost FROM positions WHERE sleeve='day'"):
            sb = (dmeta.get(r["asset"]) or {}).get("sub") or "momentum"
            usd = px.get(r["asset"])
            val = (r["qty"] * usd * rate) if (usd is not None and rate) else (r["cost"] or 0)
            ac = "stocks" if r["klass"] == "stock" else "crypto"
            held.append(dict(sub=sb if sb in ("momentum", "burst") else "momentum", acct=ac, cost=r["cost"] or 0, value=val or 0))
        real = daypot.sub_realized(con, since)
        LO = loans_open(con)
        L = daypot.sub_ledger(
            {ac: base["day"][ac] for ac in ("crypto", "stocks")},
            {ac: cash.get(("day", ac), 0.0) for ac in ("crypto", "stocks")},
            held, real, sh, ("crypto", "stocks"),
            borrowed={(sb, ac): sum(l["out"] for l in LO if l["borrower"] == "day" and l["account"] == ac and l["sub"] == sb)
                      for sb in ("momentum", "burst") for ac in ("crypto", "stocks")},
            lent={ac: sum(l["out"] for l in LO if l["lender"] == "day" and l["account"] == ac) for ac in ("crypto", "stocks")})
        for sb in ("momentum", "burst"):
            day_budget[sb] = sum(L[ac][sb]["equity"] for ac in ("crypto", "stocks"))
    except Exception:
        day_budget = {"momentum": None, "burst": None}

    def row(label, st, budget, open_pnl):
        return dict(
            strategy=label,
            budget=None if budget is None else round(budget, 2),
            trades=st.get("trades") or 0,
            win_rate=st.get("win_rate"),
            avg_win=st.get("avg_win"),
            avg_loss=st.get("avg_loss"),
            pf=st.get("pf"),
            pnl=st.get("pnl"),
            open_pnl=round(open_pnl, 2),
            fees=st.get("fees"),
            fees_closed=st.get("fees_closed"),
            fees_open=st.get("fees_open"),
            exp_gbp=st.get("exp_gbp"),
            exp_pct=st.get("exp_pct"),
        )

    rows = []
    for sb, label in (("momentum", "Momentum"), ("burst", "Burst")):
        st = (day.get(sb) or {}).get("all:all") or {}
        rows.append(row(label, st, day_budget[sb], open_day[sb]))
    rows.append(row("Risk", risk, risk_cash + val_risk, open_risk))
    return rows


def _book(con, name, label, blurb, since, start_equity, reset=None):
    from dashboard import setup_scorecard
    note = None
    if reset and reset.get("ts"):
        since = reset["ts"]
        if reset.get("equity") is not None:
            start_equity = reset["equity"]
        note = "Reset " + str(reset["ts"])[:16].replace("T", " ") + " UTC. Sells before this reset are not counted."
        blurb = (blurb + " " + note).strip()
    st = _since(con, since, start_equity)
    return dict(
        name=name, label=label, blurb=blurb,
        equity=round(st["equity"], 2), start=round(st["start"], 2),
        pnl=round(st["pnl"], 2),
        ret=round((st["equity"] / st["start"] - 1) * 100, 3) if st["start"] else None,
        trades=st["trades"], closed=st["closed"],
        win_rate=None if st["win_rate"] is None else round(st["win_rate"] * 100, 1),
        max_dd=round(st["max_dd"] * 100, 3),
        realized=round(st["realized"], 2),
        scorecard=setup_scorecard(con, since),
        curve=_curve(con, since, st["start"]),
        table=sleeve_table(con) if name in TABLE_BOOKS else None,
        reset_note=note,
    )


def report():
    """Dashboard payload. Missing books are an empty list, not an error."""
    import json
    c = cfg()
    block = c.get("variants") or {}
    root = dir_path()
    man = {}
    if (root / "manifest.json").exists():
        try:
            man = json.loads((root / "manifest.json").read_text())
        except Exception:
            man = {}
    books = []
    since = man.get("equity_ts") or man.get("created")
    if (root / "_snapshot.db").exists() and main_db().exists():
        con = sqlite3.connect(f"file:{main_db()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            books.append(_book(con, "main", "Main book", "Frozen rules. This is the book the cycle commits.", since, man.get("equity")))
        except Exception as e:
            books.append(dict(name="main", label="Main book", error=str(e)))
        finally:
            con.close()
    for name in list(block.get("run") or []):
        path = root / f"{name}.db"
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            stored = _read_reset(path)
            books.append(_book(con, name, LABELS.get(name, name), BLURB.get(name, ""), since, man.get("equity"),
                               stored if name in reset_map() else None))
        except Exception as e:
            books.append(dict(name=name, label=LABELS.get(name, name), error=str(e)))
        finally:
            con.close()
    return dict(enabled=bool(block.get("enabled")), since=since, start_equity=man.get("equity"),
                ev_note=EV_NOTE, books=books)


def _book_files_present():
    """True when a snapshot or a variant database is already on disk."""
    root = dir_path()
    if (root / "_snapshot.db").exists():
        return True
    for name in ((cfg().get("variants") or {}).get("run") or []):
        if (root / f"{name}.db").exists():
            return True
    return False


def summary_line():
    rep = report()
    if not rep["books"]:
        if not rep["enabled"]:
            return "VARIANTS off"
        if _book_files_present():
            return "VARIANTS present but none could be read"
        return "VARIANTS not started yet"
    bits = []
    for b in rep["books"]:
        if b.get("error"):
            bits.append(f"{b['label']} error")
            continue
        wr = "n/a" if b["win_rate"] is None else f"{b['win_rate']:.0f}%"
        bits.append(f"{b['label']} £{b['equity']:.2f} (P&L £{b['pnl']:+.2f}, win {wr}, max DD {b['max_dd']:.1f}%)")
    since = (rep.get("since") or "")[:16].replace("T", " ")
    return "VARIANTS since " + (since or "?") + ": " + " | ".join(bits)


def main(argv):
    if len(argv) >= 3 and argv[1] == "--trade":
        _trade_child(argv[2])
        return
    run_all()


if __name__ == "__main__":
    main(sys.argv)
