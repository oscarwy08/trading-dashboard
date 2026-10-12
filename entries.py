"""Armed entries shared by the 5-minute cycle and the exit/entry watcher.

The cycle keeps scoring. For a setup it would buy, it records a row here: the
price that has to trade, an optional volume multiple, an expiry, the size, and
the same stop and target the cycle would store. The watcher reads those rows.

Recording a row does not spend cash. The main book's buys stay on the cycle.
Live buys from a trigger are only for the entry_watch variant, and only when
ENTRY_WATCH_SHADOW is off. That switch is separate from EXIT_WATCH_SHADOW.
"""
import json
from datetime import datetime, timedelta

from common import ACCOUNTS, LON, acct_start, iso, loan_net, usd_gbp

TRIGGER = "break_above"


def variant_db_path():
    """The only book the watcher may buy into. Main is shadow-logged, never bought here."""
    from variants import dir_path
    return dir_path() / "entry_watch.db"


def variant_books():
    """(name, path) for each paper book that has an overlay and a database file.

    The snapshot and manifest are not books. A name with no file yet is skipped.
    """
    from common import ROOT, cfg
    from variants import dir_path
    root = dir_path()
    names = list((cfg().get("variants") or {}).get("run") or [])
    out = []
    for name in names:
        path = root / f"{name}.db"
        if path.exists() and (ROOT / "config" / "variants" / f"{name}.yaml").exists():
            out.append((name, path))
    return out


def burst_strength(kind, B, range_frac=None):
    """Confidence breakout component at the moment the level is touched, not after the price has run."""
    B = B or {}
    mb = float(B.get("min_break_pct", 0.1)) / 100
    mx = float(B.get("max_extension_pct", 3)) / 100 or 1e-9
    if kind == "4h":
        rng = 0.0 if range_frac is None else float(range_frac)
        width = float(B.get("range_max_pct", 4)) / 100 or 1e-9
        raw = 0.45 + 0.25 * (1 - rng / width) + 0.2 * mb / mx
    else:
        raw = 0.7 + 0.3 * mb / mx
    return max(0.0, min(1.0, raw))


def _hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def _state(con, key):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return row[0]


def upsert(con, row):
    """Replace the open arm for this sleeve and asset. Filled rows stay, for the audit."""
    con.execute("DELETE FROM armed_entries WHERE sleeve=? AND asset=? AND status='armed'",
                (row["sleeve"], row["asset"]))
    snap = row.get("snap")
    if isinstance(snap, dict):
        snap = json.dumps(snap, separators=(",", ":"))
    con.execute(
        """INSERT INTO armed_entries(ts,expires,sleeve,sub,setup,asset,account,klass,trigger,level,level_max,
           volume_min,size_gbp,stop_pct,target_pct,conf,ev,reason,snap,status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'armed')""",
        (row.get("ts") or iso(), row["expires"], row["sleeve"], row.get("sub"), row.get("setup"), row["asset"],
         row["account"], row["klass"], row.get("trigger") or TRIGGER, float(row["level"]), row.get("level_max"),
         row.get("volume_min"), float(row["size_gbp"]), float(row["stop_pct"]), float(row["target_pct"]),
         row.get("conf"), row.get("ev"), row.get("reason"), snap))


def mark_filled(con, sleeve, asset, ts, price, by="cycle"):
    con.execute(
        """UPDATE armed_entries SET status='filled', filled_ts=?, filled_price=?, filled_by=?
           WHERE sleeve=? AND asset=? AND status='armed'""",
        (ts, price, by, sleeve, asset))


def expire(con, ts):
    con.execute("UPDATE armed_entries SET status='expired' WHERE status='armed' AND expires<=?", (ts,))


def settle_open(con, ts):
    """An arm whose position is already open must not fire again."""
    rows = con.execute("SELECT id, sleeve, asset FROM armed_entries WHERE status='armed'").fetchall()
    for r in rows:
        held = con.execute("SELECT 1 FROM positions WHERE sleeve=? AND asset=?", (r["sleeve"], r["asset"])).fetchone()
        if held:
            con.execute("UPDATE armed_entries SET status='filled', filled_ts=?, filled_by='position' WHERE id=?",
                        (ts, r["id"]))


def load_due(con, ts):
    return [dict(r) for r in con.execute(
        "SELECT * FROM armed_entries WHERE status='armed' AND expires>? ORDER BY sleeve, asset", (ts,))]


def snap_of(arm):
    raw = arm.get("snap")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _gas(con, c, klass):
    if klass == "stock":
        return 0.0
    from trader import gas_gbp
    return float(gas_gbp(con, c))


def _px(con, asset, rate, live):
    if asset in live:
        return float(live[asset])
    row = con.execute(
        """SELECT price FROM prices WHERE asset=? AND source!='backfill' AND price>0 ORDER BY ts DESC LIMIT 1""",
        (asset,)).fetchone()
    if not row or not rate:
        return None
    return float(row[0]) * float(rate)


def _day_equity(con, rate, live):
    eq = 0.0
    for (amt,) in con.execute("SELECT amount FROM acct_cash WHERE sleeve='day'"):
        eq += float(amt or 0)
    for ac in ACCOUNTS:
        eq += float(loan_net(con).get(("day", ac), 0.0))
    for asset, qty in con.execute("SELECT asset, qty FROM positions WHERE sleeve='day'"):
        px = _px(con, asset, rate, live)
        if px is not None:
            eq += float(qty) * px
    return eq


def _day_free(con, c, sub, account, rate, live):
    """Cash the sub-strategy can still spend, same ledger the cycle uses."""
    import daypot
    start = acct_start(con, c).get("day") or {}
    cash = {ac: 0.0 for ac in ACCOUNTS}
    for ac, amt in con.execute("SELECT account, amount FROM acct_cash WHERE sleeve='day'"):
        cash[ac] = float(amt or 0)
    meta = _state(con, "day_meta") or {}
    positions = []
    for asset, klass, qty, cost in con.execute("SELECT asset, klass, qty, cost FROM positions WHERE sleeve='day'"):
        px = _px(con, asset, rate, live)
        sub_p = (meta.get(asset) or {}).get("sub") or "momentum"
        acct = "stocks" if klass == "stock" else "crypto"
        positions.append(dict(sub=sub_p, acct=acct, cost=float(cost or 0),
                              value=(float(qty) * px) if px is not None else float(cost or 0)))
    split = _state(con, "account_split")
    since = split.get("ts", "") if isinstance(split, dict) else ""
    borrowed, lent = {}, {}
    from common import loans_open
    for l in loans_open(con):
        if l["borrower"] == "day":
            borrowed[(l.get("sub") or "momentum", l["account"])] = borrowed.get((l.get("sub") or "momentum", l["account"]), 0.0) + l["out"]
        if l["lender"] == "day":
            lent[l["account"]] = lent.get(l["account"], 0.0) + l["out"]
    led = daypot.sub_ledger(start, cash, positions, daypot.sub_realized(con, since),
                            daypot.sub_shares(c["day"]), ACCOUNTS, borrowed=borrowed, lent=lent)
    cell = (led.get(account) or {}).get(sub) or {}
    return float(cell.get("free") or 0.0)


def _open_n(con, arm):
    if arm["sleeve"] == "risk":
        return con.execute("SELECT COUNT(*) FROM positions WHERE sleeve='risk'").fetchone()[0]
    meta = _state(con, "day_meta") or {}
    n = 0
    for asset, klass in con.execute("SELECT asset, klass FROM positions WHERE sleeve='day'"):
        sub = (meta.get(asset) or {}).get("sub") or "momentum"
        acct = "stocks" if klass == "stock" else "crypto"
        if arm["sub"] == "burst":
            if sub == "burst" and acct == arm["account"]:
                n += 1
        elif sub != "burst":
            n += 1
    return n


def _volume_ok(con, arm, t):
    if arm.get("volume_min") is None:
        return True
    import daypot
    if arm["klass"] == "stock":
        from trader import sget
        vol = daypot.stock_vol_ratio(con, arm["asset"], t, sget)
    else:
        vol = daypot.crypto_vol_ratio(con, arm["asset"], t)
    return vol is not None and float(vol) + 1e-9 >= float(arm["volume_min"])


def skip_reason(con, c, arm, price_gbp, t, rate, live=None):
    """None when this arm may be bought at price_gbp. A string is the check that blocked it.

    The same gates the cycle uses at a buy: trigger, chase cap, volume, session,
    flatten, loss cap, cooldown, open slots, and cash. No loan from this path.
    """
    live = live or {}
    if price_gbp is None:
        return "no live price"
    level = float(arm["level"])
    if float(price_gbp) + 1e-12 < level:
        return "price below the trigger"
    cap = arm.get("level_max")
    if cap is not None and float(price_gbp) > float(cap) + 1e-9:
        return "past the chase cap"
    if not _volume_ok(con, arm, t):
        return "volume condition not met"
    if con.execute("SELECT 1 FROM positions WHERE sleeve=? AND asset=?", (arm["sleeve"], arm["asset"])).fetchone():
        return "already held"
    fx = usd_gbp(con, (c.get("fx") or {}).get("max_age_h"))
    if not fx or fx[3]:
        return "stale FX rate"
    D = c["day"]
    lt = t.astimezone(LON)
    mins = lt.hour * 60 + lt.minute
    if arm["klass"] == "stock":
        import markets
        if not markets.is_open(arm["asset"], t):
            return "market closed"
        left = markets.mins_to_close(arm["asset"], t)
        lim = 15 if arm["sleeve"] == "risk" else int(D.get("stock_entry_stop_before_close_min", 30))
        if left is not None and left <= lim:
            return "too close to the close"
    if arm["sleeve"] == "day":
        import variants
        R = variants.rules_of(c)
        # Midnight crypto keeps its own entry clock. Every other book, including main, uses flatten_uk.
        if arm["klass"] != "stock" and R.get("midnight_crypto"):
            if not variants.crypto_entry_open(mins, _hm(R["midnight_entry_start_uk"]), _hm(R["midnight_entry_cutoff_uk"])):
                return "outside the day session"
        else:
            e0, e1, flat = _hm(D["entry_start_uk"]), _hm(D["entry_cutoff_uk"]), _hm(D["flatten_uk"])
            if mins >= flat or mins < e0 or not (e0 <= mins < e1):
                return "outside the day session"
        day = lt.date().isoformat()
        if _state(con, f"day_halt:{day}") is not None:
            return "day loss cap"
        start_row = con.execute("SELECT value FROM state WHERE key=?", (f"day_start:day:{day}",)).fetchone()
        if start_row and start_row[0] not in (None, ""):
            try:
                start = float(_state(con, f"day_start:day:{day}"))
            except (TypeError, ValueError):
                start = None
            if start:
                eq = _day_equity(con, rate, live)
                if eq / start - 1 <= -float(D["daily_loss_cap_pct"]) / 100:
                    return "day loss cap"
        hours, key = None, None
        if arm.get("sub") == "burst":
            hours = float(D["burst"]["cooldown_h"])
            key = f"day_exit:{arm['asset']}"
        if hours and key:
            lx = _state(con, key)
            if lx and datetime.fromisoformat(str(lx)) > t - timedelta(hours=hours):
                return "cooldown"
        cap_n = int(D["burst"]["max_open_per_account"] if arm.get("sub") == "burst" else D["momentum"]["max_open"])
        if _open_n(con, arm) >= cap_n:
            return "max open positions"
        free = _day_free(con, c, arm.get("sub") or "momentum", arm["account"], rate, live)
        gas = _gas(con, c, arm["klass"])
        row = con.execute("SELECT amount FROM acct_cash WHERE sleeve='day' AND account=?", (arm["account"],)).fetchone()
        cash = float(row[0]) if row else 0.0
        need = float(arm["size_gbp"])
        if free + 1e-9 < need or cash + 1e-9 < need + gas:
            return "short of cash"
        floor = float(D["burst"].get("cash_floor_gbp", 4)) if arm.get("sub") == "burst" else float(c["risk"]["min_trade_size"])
        if need + 1e-9 < floor:
            return "below the minimum trade"
        return None
    # risk
    if _state(con, "riskpot_halt"):
        return "risk drawdown halt"
    RP = c.get("riskpot") or {}
    lx = _state(con, f"risk_exit:{arm['asset']}")
    if lx and datetime.fromisoformat(str(lx)) > t - timedelta(hours=float(RP.get("cooldown_h", 24))):
        return "cooldown"
    if _open_n(con, arm) >= int(RP.get("max_open", 5)):
        return "max open positions"
    gas = _gas(con, c, arm["klass"])
    row = con.execute("SELECT amount FROM acct_cash WHERE sleeve='risk' AND account=?", (arm["account"],)).fetchone()
    cash = float(row[0]) if row else 0.0
    need = float(arm["size_gbp"])
    mint = float(c["risk"]["min_trade_size"])
    if need + 1e-9 < mint:
        return "below the minimum trade"
    if cash + 1e-9 < need + gas:
        return "short of cash"
    return None


def watched_rows(con, ts):
    """Assets the price streams should follow: open Day and Risk positions, plus armed entries."""
    rows = [dict(r) for r in con.execute(
        "SELECT sleeve, asset, klass FROM positions WHERE sleeve IN ('day','risk')")]
    try:
        rows += [dict(r) for r in con.execute(
            "SELECT sleeve, asset, klass FROM armed_entries WHERE status='armed' AND expires>?", (ts,))]
    except Exception:
        pass
    out, seen = [], set()
    for r in rows:
        if r["asset"] in seen:
            continue
        seen.add(r["asset"])
        out.append(r)
    return out
