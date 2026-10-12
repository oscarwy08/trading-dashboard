"""Always-on watcher for Day and Risk exits, and for armed entries.

Exits use the same rules and the same fill as the cycle (exits.day_reason /
risk_reason and trader.apply_sell). Entries use the armed rows the cycle
writes (entries.py) and trader.apply_buy. One process, one systemd unit. It
watches Kraken and Coinbase public websockets where the coin is listed, and
polls everything else about every 20 seconds. If a stream drops it keeps
running; the 5-minute cycle still exits and still buys.

Two shadow switches, independent of each other. Both default to on.

EXIT_WATCH_SHADOW: log WOULD EXIT and do not sell. Set it to 0 to sell for real.
ENTRY_WATCH_SHADOW: log WOULD BUY and do not buy. Set it to 0 to buy, and only
on the entry_watch variant book. The main book never receives an entry from
this process. Every other paper book under data/variants/ is shadow-logged
too (WOULD EXIT and WOULD BUY), using that book's own rules, and is never
sold or bought by this process. Each shadow row is tagged with the book name.
WOULD EXIT and WOULD BUY both go to
~/exit_shadow/exit_shadow.db (EXIT_SHADOW_DB overrides that path), outside the
clone, so run_cycle.sh's git reset --hard does not delete it. Review with
`python3 exit_shadow_report.py`.

Deploy on the VPS (Ubuntu, user bot; trading-cycle.timer already runs the cycle).
Do this only after the branch is on main:

    sudo cp deploy/trading-exit-watch.service /etc/systemd/system/
    # WorkingDirectory and ExecStart must be the clone run_cycle.sh uses
    # (the directory that contains this file and data/trading.db).
    # The unit sets EXIT_WATCH_SHADOW=1 and ENTRY_WATCH_SHADOW=1. Leave both
    # until the report looks right.
    sudo systemctl daemon-reload
    sudo systemctl enable --now trading-exit-watch
    journalctl -u trading-exit-watch -f
    python3 exit_shadow_report.py
    # then add EXIT_WATCH_SHADOW=0 and, when entries should fill the variant,
    # ENTRY_WATCH_SHADOW=0 to /home/bot/.trading_env and:
    sudo systemctl restart trading-exit-watch

Creating KILL in that clone stops this watcher as well as the cycle. A quiet
stream is a warning, not a crash. Restart=on-failure is for a dead process.
"""
import base64, json, os, socket, sqlite3, ssl, struct, threading, time
from datetime import datetime, timedelta
from pathlib import Path

import daypot
import entries
import freshness
import markets
import riskpot
import pricesrc
import variants
from common import KILL, LON, cfg, db, iso, log, now, usd_gbp
from exits import book_lock, day_flat_reason, day_reason, dead_quote, gap_tag, risk_reason, stock_flatten_reason
from trader import apply_buy, apply_sell, cost_pct, gas_gbp, history, hm, score_asset, sentiment, sget, sset

L = log("exit_watch")
REST_S = 20
QUIET_S = 45
LIVE_S = 60


def _flag_on(name):
    """Unset or blank is on. 0 / false / no / off / live turns the shadow off."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "live")


def shadow_mode():
    """True: log WOULD EXIT and leave the book alone. This is the default.

    Set EXIT_WATCH_SHADOW=0 (or false/off/live) after the shadow report looks
    right. That is the only switch that lets this process write a sell.
    """
    return _flag_on("EXIT_WATCH_SHADOW")


def entry_shadow_mode():
    """True: log WOULD BUY and do not buy. This is the default, and it does not
    follow EXIT_WATCH_SHADOW.

    Set ENTRY_WATCH_SHADOW=0 to buy on the entry_watch variant only. The main
    book still only gets the WOULD BUY line.
    """
    return _flag_on("ENTRY_WATCH_SHADOW")


def config_for(book):
    """Config for one book. Main uses config.yaml. A variant merges its overlay.

    The environment is restored before this returns, so a later cfg() call is main again.
    """
    old = os.environ.get("TRADING_VARIANT")
    try:
        if book and book != "main":
            os.environ["TRADING_VARIANT"] = book
        else:
            os.environ.pop("TRADING_VARIANT", None)
        return cfg()
    finally:
        if old is None:
            os.environ.pop("TRADING_VARIANT", None)
        else:
            os.environ["TRADING_VARIANT"] = old


def _book_name(book):
    return book or "main"


def shadow_db_path():
    """Where WOULD EXIT rows are stored. Outside the clone by default.

    ~/exit_shadow/exit_shadow.db survives the cycle's `git reset --hard` (and
    `git add -A` cannot pick it up). EXIT_SHADOW_DB overrides the path.
    """
    override = os.environ.get("EXIT_SHADOW_DB")
    if override and str(override).strip():
        return Path(override).expanduser()
    return Path.home() / "exit_shadow" / "exit_shadow.db"


def _shadow_connect():
    path = shadow_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS would_exit (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        sleeve TEXT NOT NULL,
        asset TEXT NOT NULL,
        opened TEXT,
        trigger TEXT NOT NULL,
        price REAL NOT NULL,
        chg REAL,
        peak REAL,
        qty REAL,
        entry REAL,
        klass TEXT)""")
    _ensure_book_column(con, "would_exit")
    con.execute("CREATE TABLE IF NOT EXISTS shadow_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("""CREATE TABLE IF NOT EXISTS would_buy (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        sleeve TEXT NOT NULL,
        sub TEXT,
        setup TEXT,
        asset TEXT NOT NULL,
        armed TEXT,
        expires TEXT,
        trigger TEXT NOT NULL,
        level REAL,
        price REAL NOT NULL,
        size_gbp REAL,
        stop_pct REAL,
        target_pct REAL,
        klass TEXT,
        reason TEXT)""")
    _ensure_book_column(con, "would_buy")
    con.commit()
    return con


def _ensure_book_column(con, table):
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if "book" not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN book TEXT")
        con.execute(f"UPDATE {table} SET book='main' WHERE book IS NULL")


def _peaks_key(book):
    """Main keeps the original key so an existing shadow file still has its peaks."""
    return "peaks" if _book_name(book) == "main" else "peaks:" + _book_name(book)


def _shadow_peaks(shcon, book="main"):
    if shcon is None:
        return {"day": {}, "risk": {}}
    row = shcon.execute("SELECT value FROM shadow_state WHERE key=?", (_peaks_key(book),)).fetchone()
    if not row:
        return {"day": {}, "risk": {}}
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return {"day": {}, "risk": {}}
    data.setdefault("day", {})
    data.setdefault("risk", {})
    return data


def _exit_triggers(shcon, sleeve, asset, opened, book="main"):
    return [r[0] or "" for r in shcon.execute(
        """SELECT trigger FROM would_exit WHERE sleeve=? AND asset=? AND IFNULL(opened,'')=IFNULL(?,'')
           AND IFNULL(book,'main')=?""",
        (sleeve, asset, opened, _book_name(book))).fetchall()]


def _shadow_seen(shcon, sleeve, asset, opened, book="main"):
    return bool(_exit_triggers(shcon, sleeve, asset, opened, book))


def _shadow_closed(shcon, sleeve, asset, opened, book="main"):
    """A full exit was already logged. A half-sale does not close the shadow of the remainder."""
    return any(not t.startswith("partial take") for t in _exit_triggers(shcon, sleeve, asset, opened, book))


def _buy_line(shadow, ts, arm, gbp, book="main"):
    head = "WOULD BUY" if shadow else "BUY"
    tag = "" if _book_name(book) == "main" else f"[{_book_name(book)}] "
    vol = f" vol x{float(arm['volume_min']):g}" if arm.get("volume_min") is not None else ""
    return (f"{head} {tag}{arm['sleeve']} {arm['asset']} at {ts} trigger £{gbp:.6g} "
            f"(level £{float(arm['level']):.6g}{vol}) size £{float(arm['size_gbp']):.2f} "
            f"stop -{float(arm['stop_pct']):.1f}% target +{float(arm['target_pct']):.1f}% | {arm.get('reason') or ''}")


def _record_would_buy(shcon, ts, arm, gbp, book="main"):
    """First WOULD BUY for this armed row on this book. A later tick does not add another."""
    book = _book_name(book)
    if shcon.execute(
            """SELECT 1 FROM would_buy WHERE sleeve=? AND asset=? AND IFNULL(armed,'')=?
               AND IFNULL(book,'main')=? LIMIT 1""",
            (arm["sleeve"], arm["asset"], arm.get("ts"), book)).fetchone():
        return None
    shcon.execute(
        """INSERT INTO would_buy(ts,sleeve,sub,setup,asset,armed,expires,trigger,level,price,size_gbp,stop_pct,target_pct,klass,reason,book)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, arm["sleeve"], arm.get("sub"), arm.get("setup"), arm["asset"], arm.get("ts"), arm.get("expires"),
         arm.get("reason") or arm.get("trigger") or "break_above", arm.get("level"), gbp, arm.get("size_gbp"),
         arm.get("stop_pct"), arm.get("target_pct"), arm.get("klass"), arm.get("reason"), book))
    shcon.commit()
    line = _buy_line(True, ts, arm, gbp, book)
    return dict(sleeve=arm["sleeve"], asset=arm["asset"], price=gbp, reason=arm.get("reason"), log=line,
                shadow=True, ts=ts, level=arm.get("level"))


def _stamp_open(con, arm, ts, price, snap):
    """The same day_meta / risk_meta the cycle writes, so the next cycle sees the position."""
    asset = arm["asset"]
    if arm["sleeve"] == "day":
        meta = dict(sget(con, "day_meta") or {})
        row = dict(sub=arm.get("sub") or "momentum", tp=float(arm["target_pct"]), sl=float(arm["stop_pct"]),
                   atr=(snap or {}).get("atr"), peak=price, opened=ts, conf=arm.get("conf"), ev=arm.get("ev"), snap=snap)
        if arm.get("sub") == "burst" and arm.get("setup"):
            row["kind"] = arm["setup"]
        meta[asset] = row
        sset(con, "day_meta", meta)
        return
    peaks = dict(sget(con, "risk_peaks") or {})
    peaks[asset] = price
    rmeta = dict(sget(con, "risk_meta") or {})
    rmeta[asset] = dict(conf=arm.get("conf"), ev=arm.get("ev"), opened=ts,
                        setup=arm.get("setup") or arm.get("sub"), why=arm.get("reason"), snap=snap)
    sset(con, "risk_peaks", peaks)
    sset(con, "risk_meta", rmeta)


def _entry_tick(con, prices_gbp, live, book="main", c=None):
    """Shadow-log (live False) or buy (live True) armed entries whose trigger has traded.

    A missing live tick does not fire. Flatten, cash, session and the open-slot
    checks are entries.skip_reason. Live buys are only for the entry_watch book.
    Every other book, including main, only logs WOULD BUY.
    """
    book = _book_name(book)
    if live and book != "entry_watch":
        live = False
    c = c or cfg()
    t = now()
    ts = iso(t)
    fx = usd_gbp(con, c["fx"]["max_age_h"])
    if not fx or fx[3]:
        return []
    rate = fx[0]
    try:
        arms = entries.load_due(con, ts)
    except sqlite3.OperationalError:
        return []
    shcon = None if live else _shadow_connect()
    out = []
    gas = gas_gbp(con, c)
    try:
        for arm in arms:
            asset = arm["asset"]
            if asset not in prices_gbp:
                continue
            gbp = prices_gbp[asset]
            if entries.skip_reason(con, c, arm, gbp, t, rate, prices_gbp):
                continue
            if not live:
                rec = _record_would_buy(shcon, ts, arm, gbp, book)
                if rec:
                    L.info(rec["log"])
                    out.append(rec)
                continue
            q = _quote(con, asset, arm["klass"], gbp, rate)
            snap = entries.snap_of(arm)
            done = apply_buy(con, c, ts, rate, gas, arm["sleeve"], asset, arm["klass"], arm["size_gbp"], gbp,
                             arm.get("reason") or "live trigger", q, sub=arm.get("sub"), conf=arm.get("conf"), snap=snap)
            if not done:
                continue
            entries.mark_filled(con, arm["sleeve"], asset, ts, gbp, "watch")
            _stamp_open(con, arm, ts, gbp, done.get("snap"))
            con.execute(
                "INSERT INTO signals(ts,sleeve,asset,momentum,sentiment,combined,action,reason) VALUES(?,?,?,?,?,?,?,?)",
                (ts, arm["sleeve"], asset, None, None, arm.get("conf"), "BUY", arm.get("reason") or "live trigger"))
            line = _buy_line(False, ts, arm, gbp, book)
            L.info(line)
            out.append(dict(sleeve=arm["sleeve"], asset=asset, price=gbp, reason=arm.get("reason"), log=line,
                            shadow=False, ts=ts))
        return out
    finally:
        if shcon is not None:
            shcon.close()


def act_entries(con, prices_gbp, recv_mono=None, live=False, book="main", c=None):
    """One entry pass. live=False never writes the book. live=True buys under that book's lock.

    live=True is ignored unless book is entry_watch. Variant shadow logs never buy.
    """
    del recv_mono
    book = _book_name(book)
    if live and book != "entry_watch":
        live = False
    if KILL.exists():
        L.info("KILL file present - watcher halted, no entries")
        return []
    if not live:
        return _entry_tick(con, prices_gbp, live=False, book=book, c=c)
    try:
        path = con.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        path = None
    try:
        with book_lock(timeout=2, db_path=path):
            con.execute("BEGIN IMMEDIATE")
            try:
                rows = _entry_tick(con, prices_gbp, live=True, book=book, c=c)
                con.commit()
                return rows
            except Exception:
                con.rollback()
                raise
    except TimeoutError:
        L.info("book busy (the cycle is in it); this tick leaves the entry to that run")
        return []


def _would_line(ts, sleeve, asset, price, chg, peak, reason, book="main"):
    peak_s = f" peak {peak * 100:+.1f}%" if peak is not None else ""
    tag = "" if _book_name(book) == "main" else f"[{_book_name(book)}] "
    return f"WOULD EXIT {tag}{sleeve} {asset} at {ts} trigger £{price:.6g} net {chg * 100:+.1f}%{peak_s} | {reason}"


def _record_would(shcon, ts, p, gbp, reason, chg, peak, book="main", qty=None):
    """First WOULD EXIT for this open position on this book. A later tick does not add another row.

    A half-sale is logged once and does not block the later exit of the remainder.
    """
    book = _book_name(book)
    triggers = _exit_triggers(shcon, p["sleeve"], p["asset"], p.get("opened"), book)
    is_partial = str(reason or "").startswith("partial take")
    if is_partial:
        if any(t.startswith("partial take") for t in triggers):
            return None
    elif any(not t.startswith("partial take") for t in triggers):
        return None
    use_qty = p.get("qty") if qty is None else qty
    shcon.execute(
        "INSERT INTO would_exit(ts,sleeve,asset,opened,trigger,price,chg,peak,qty,entry,klass,book) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, p["sleeve"], p["asset"], p.get("opened"), reason, gbp, chg, peak, use_qty, p.get("entry"), p.get("klass"), book))
    shcon.commit()
    line = _would_line(ts, p["sleeve"], p["asset"], gbp, chg, peak, reason, book)
    return dict(sleeve=p["sleeve"], asset=p["asset"], price=gbp, reason=reason, pnl=None,
                log=line, chg=chg, peak=peak, shadow=True, ts=ts)


def _lift_day_peak(md, asset, opened, state):
    sp = (state.get("day") or {}).get(asset)
    if not sp or sp.get("opened") != opened:
        return
    md["peak"] = max(float(md.get("peak") or 0), float(sp.get("peak") or 0))
    if sp.get("runner"):
        md["runner"] = True


def _store_shadow_peaks(shcon, dmeta, peaks, rows, state, book="main"):
    """Remember peaks the watcher has seen, without writing day_meta or risk_peaks."""
    day = dict(state.get("day") or {})
    risk = dict(state.get("risk") or {})
    open_day = {p["asset"]: p for p in rows if p["sleeve"] == "day"}
    open_risk = {p["asset"]: p for p in rows if p["sleeve"] == "risk"}
    for asset, md in dmeta.items():
        held = open_day.get(asset)
        if not held:
            continue
        day[asset] = {"peak": md.get("peak"), "runner": bool(md.get("runner")), "opened": held["opened"]}
    for asset, pk in peaks.items():
        held = open_risk.get(asset)
        if not held:
            continue
        risk[asset] = {"peak": pk, "opened": held["opened"]}
    # Drop a peak that belonged to a position which has since been closed in the book.
    day = {a: v for a, v in day.items() if a in open_day and v.get("opened") == open_day[a]["opened"]}
    risk = {a: v for a, v in risk.items() if a in open_risk and v.get("opened") == open_risk[a]["opened"]}
    shcon.execute("INSERT OR REPLACE INTO shadow_state VALUES(?, ?)",
                  (_peaks_key(book), json.dumps({"day": day, "risk": risk})))


def kraken_symbol(route):
    """XBTUSD + quote USD -> XBT/USD, which is what the Kraken websocket v2 ticker wants."""
    pair, quote = (route or {}).get("pair") or "", (route or {}).get("quote") or ""
    if not pair or not quote or "-" in pair or "/" in pair or not pair.endswith(quote):
        return None
    return pair[:-len(quote)] + "/" + quote


def watch_plan(rows, routes):
    """Split open positions into a Kraken socket, a Coinbase socket, and REST."""
    kraken, coinbase, rest = [], [], []
    for r in rows:
        asset, klass = r["asset"], r["klass"]
        if klass == "stock":
            rest.append(dict(asset=asset, kind="stock")); continue
        route = (routes or {}).get(asset) or {}
        src = route.get("src")
        if src == "kraken" and kraken_symbol(route):
            kraken.append(dict(asset=asset, symbol=kraken_symbol(route), quote=route.get("quote") or "USD"))
        elif src == "coinbase" and route.get("pair"):
            coinbase.append(dict(asset=asset, symbol=route["pair"], quote=route.get("quote") or "USD"))
        elif src == "kyber":
            rest.append(dict(asset=asset, kind="kyber", route=route))
        else:
            rest.append(dict(asset=asset, kind="coingecko", route=route))
    return dict(kraken=kraken, coinbase=coinbase, rest=rest)


def kraken_ticks(msg):
    if not isinstance(msg, dict) or msg.get("channel") != "ticker":
        return []
    out = []
    for row in msg.get("data") or []:
        if not isinstance(row, dict) or row.get("last") in (None, ""): continue
        try: out.append((row.get("symbol"), float(row["last"])))
        except (TypeError, ValueError): continue
    return [(s, p) for s, p in out if s]


def coinbase_tick(msg):
    if not isinstance(msg, dict) or msg.get("type") != "ticker" or msg.get("price") in (None, ""):
        return None
    try: return msg.get("product_id"), float(msg["price"])
    except (TypeError, ValueError): return None


def to_gbp(price, quote, fx):
    """fx is GBP per 1 USD, the rate stored in table fx."""
    usd = pricesrc.to_usd(price, quote, fx)
    if usd is None or not fx or float(fx) <= 0: return None
    return usd * float(fx)


def _impact(con, asset, q):
    screen = sget(con, "risk_screen") or {}
    for d in screen.get("top") or []:
        if d.get("a") == asset and d.get("dex") is not None:
            q["impact_pct"] = d.get("dex")
    eco = sget(con, "monad_eco") or {}
    dex = eco.get("dex") if isinstance(eco, dict) else None
    if not dex: return
    ids = {d.get("id"): d.get("a") for d in (screen.get("top") or []) if d.get("id")}
    for cid, dx in dex.items():
        if ids.get(cid) == asset and isinstance(dx, dict) and dx.get("loss_pct") is not None:
            q["impact_pct"] = dx["loss_pct"]


def _quote(con, asset, klass, gbp, rate):
    row = con.execute("""SELECT vol24, mcap FROM prices WHERE asset=? AND source!='backfill' AND ts<=?
                         ORDER BY ts DESC LIMIT 1""", (asset, iso(now() + timedelta(seconds=1)))).fetchone()
    q = dict(klass=klass, price=gbp, price_usd=(gbp / rate if rate else None),
             vol24=(row["vol24"] if row else None), mcap=(row["mcap"] if row else None), fresh=True)
    _impact(con, asset, q)
    return q


def _stored_gbp(con, asset, rate):
    row = con.execute("""SELECT price, source_ts, ts FROM prices WHERE asset=? AND source!='backfill' AND ts<=?
                         ORDER BY ts DESC LIMIT 1""", (asset, iso(now() + timedelta(seconds=1)))).fetchone()
    if not row or not rate:
        return None, None
    return float(row[0]) * rate, freshness.age_min(row["source_ts"] or row["ts"], now())


def _line(sleeve, asset, price, chg, peak, lat, reason):
    peak_s = f" peak {peak * 100:+.1f}%" if peak is not None else ""
    lat_s = f"{lat:.3f}s" if lat is not None else "n/a"
    return f"EXIT {sleeve} {asset} trigger £{price:.6g} net {chg * 100:+.1f}%{peak_s} latency {lat_s} | {reason}"


def _pos_ev(c, meta, price, asset, klass, q):
    """(EV now, entry EV) from the snapshot stored at the buy. None without a stored probability."""
    snap = (meta or {}).get("snap") or {}
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except (TypeError, ValueError):
            snap = {}
    p_hit, tgt, loss, entry = snap.get("p"), snap.get("target"), snap.get("loss"), snap.get("entry")
    if p_hit is None or tgt is None or loss is None or not entry or not price:
        return None, snap.get("ev")
    cost = cost_pct(c, asset, klass, "SELL", q) * 100
    evn = daypot.ev_now(p_hit, price, float(entry) * (1 + float(tgt) / 100.0),
                        float(entry) * (1 - float(loss) / 100.0), cost)
    return evn, snap.get("ev")


def _burst_keep(con, c, asset, klass, gbp, t):
    """Same gates as a new burst entry, used when a burst_runner book hits its target."""
    D, st = c["day"], c["strategy"]
    DB = D["burst"]
    pts = history(con, asset)
    brk = daypot.breakout(con, asset, pts, DB)
    vol = daypot.crypto_vol_ratio(con, asset, t) if klass != "stock" else daypot.stock_vol_ratio(con, asset, t, sget)
    r_ = daypot.rsi_h(pts)
    se = sentiment(con, asset, st["sentiment_max_age_h"])
    hist = {asset: pts}
    if klass != "stock":
        hist["BTC"] = history(con, "BTC")
    mok, reg, _info = daypot.market_regime(con, klass, asset, hist, t, DB, sget, markets.exchange)
    kind = brk.get("kind")
    ss = daypot.setup_stats(con, kind or "24h", DB, t)
    sent = None if not se else se["score"]
    conf, _parts = daypot.confidence(DB, brk.get("strength") or 0, vol, sent, reg, ss["p"], freshness=1)
    thr = daypot.threshold(DB, 0.0, D["daily_loss_cap_pct"])
    vx = float(DB["volume_x"])
    vol_ok = not ((vol is not None and vol < vx) or (vol is None and (DB.get("require_volume") or kind != "24h")))
    sent_ok = not (se and se["score"] < float(DB["sentiment_min"]))
    return variants.burst_target_action(bool(kind), vol_ok, r_, DB["rsi_max"], sent_ok, bool(mok), conf, thr)


def shadow_evaluate(con, prices_gbp, recv_mono=None, book="main", c=None):
    """WOULD EXIT for one book. Never writes that book, even when exits are live on main."""
    if KILL.exists():
        return []
    c = c or config_for(book)
    _exits, _wrote = _tick(con, prices_gbp, recv_mono or {}, time.monotonic(), c=c, book=book, force_shadow=True)
    return _exits


def evaluate(con, prices_gbp, recv_mono=None):
    """Sell open Day positions, then Risk, at these GBP prices. No entries.

    `prices_gbp` holds only fresh ticks. A stored quote older than price_dead_min
    is unpriced: it must not stop, trail, or flatten. The first fresh tick is the
    one that can sell. Max-hold still uses a stored quote inside that window.
    Returns exit records.
    """
    if KILL.exists():
        L.info("KILL file present - watcher halted, no exits")
        return []
    recv_mono = recv_mono or {}
    started = time.monotonic()
    try:
        with book_lock(timeout=2):
            con.execute("BEGIN IMMEDIATE")
            try:
                exits, wrote = _tick(con, prices_gbp, recv_mono, started)
                # Shadow never commits the book, even if a peak was edited in memory.
                if wrote and not shadow_mode(): con.commit()
                else: con.rollback()
                return exits
            except Exception:
                con.rollback()
                raise
    except TimeoutError:
        L.info("book busy (the cycle is in it); this tick leaves the exit to that run")
        return []


def _tick(con, prices_gbp, recv_mono, started, c=None, book="main", force_shadow=False):
    c = c or cfg()
    book = _book_name(book)
    R = variants.rules_of(c)
    t = now(); ts = iso(t)
    fx = usd_gbp(con, c["fx"]["max_age_h"])
    if not fx: return [], False
    rate = fx[0]
    D, st = c["day"], c["strategy"]
    RP = c.get("riskpot") or {}
    DM, DB = D["momentum"], D["burst"]
    SUBC = {"momentum": DM, "burst": DB}
    gas = gas_gbp(con, c)
    dead_min = float(c["risk"].get("price_dead_min", 180))
    lt = t.astimezone(LON); mins = lt.hour * 60 + lt.minute; day = lt.date().isoformat()
    e0, flat = hm(D["entry_start_uk"]), hm(D["flatten_uk"])
    flat_now = mins >= flat or mins < e0
    rows = [dict(r) for r in con.execute("SELECT * FROM positions WHERE sleeve IN ('day','risk')")]
    rows.sort(key=lambda r: (0 if r["sleeve"] == "day" else 1, r["asset"]))
    dmeta = dict(sget(con, "day_meta") or {})
    peaks = dict(sget(con, "risk_peaks") or {})
    rmeta = dict(sget(con, "risk_meta") or {})
    exits, wrote = [], False
    shadow = True if force_shadow else shadow_mode()
    shcon = _shadow_connect() if shadow else None
    shadow_state = _shadow_peaks(shcon, book)
    if shadow:
        for p in rows:
            if p["sleeve"] != "risk": continue
            sp = (shadow_state.get("risk") or {}).get(p["asset"])
            if sp and sp.get("opened") == p["opened"]:
                peaks[p["asset"]] = max(float(peaks.get(p["asset"]) or 0), float(sp.get("peak") or 0))

    def latency(asset):
        mark = recv_mono.get(asset)
        return max(0.0, time.monotonic() - (mark if mark is not None else started))

    def note_book():
        """A book write. Shadow keeps the change in memory only; the caller rolls it back."""
        nonlocal wrote
        if not shadow:
            wrote = True

    def sell_one(sleeve, p, gbp, reason, sub, conf, snap, chg, peak):
        if shadow:
            rec = _record_would(shcon, ts, p, gbp, reason, chg, peak, book)
            if rec:
                L.info(rec["log"])
                exits.append(rec)
            return True
        q = _quote(con, p["asset"], p["klass"], gbp, rate)
        done = apply_sell(con, c, ts, rate, gas, sleeve, p["asset"], p["klass"], p["qty"], gbp, reason, q,
                          sub=sub, conf=conf, snap=snap)
        if done is None: return False
        note_book()
        line = _line(sleeve, p["asset"], gbp, chg, peak, latency(p["asset"]), reason)
        L.info(line)
        con.execute("INSERT INTO signals(ts,sleeve,asset,momentum,sentiment,combined,action,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (ts, sleeve, p["asset"], None, None, None, "SELL", f"{reason}; P&L £{done['pnl']:+.2f}"))
        exits.append(dict(sleeve=sleeve, asset=p["asset"], price=gbp, reason=reason, pnl=done["pnl"], log=line, chg=chg, peak=peak))
        return True

    try:
        for p in rows:
            if p["sleeve"] != "day": continue
            a = p["asset"]
            if shadow and _shadow_closed(shcon, "day", a, p.get("opened"), book):
                continue
            live = a in prices_gbp
            if live:
                gbp, age = prices_gbp[a], 0.0
            else:
                gbp, age = _stored_gbp(con, a, rate)
            if gbp is None or not p["entry"]: continue
            if not live and dead_quote(age, dead_min):
                continue
            md = dmeta.get(a) or {}
            sub = md.get("sub", "momentum")
            tag = sub + (f" {md['kind']}" if md.get("kind") else "")
            if p["klass"] == "stock":
                cl = markets.mins_to_close(a, t)
                if cl is None or cl <= int(D.get("stock_flatten_before_close_min", 15)):
                    why = stock_flatten_reason(cl, markets.exchange(a))
                    if sell_one("day", p, gbp, f"[{tag}] {why}", sub, md.get("conf", md.get("score")), md.get("snap"), 0.0, None):
                        dmeta.pop(a, None)
                        if not shadow: sset(con, f"day_exit:{a}", ts)
                    continue
            opened_day = datetime.fromisoformat(p["opened"]).astimezone(LON).date().isoformat()
            if R.get("midnight_crypto") and p["klass"] != "stock":
                if not variants.crypto_flatten_due(opened_day, day, flat_now, True):
                    pass
                else:
                    why = day_flat_reason(mins, 0, 0, str(R["midnight_flatten_uk"]), str(R["midnight_flatten_uk"]), live)
                    q = _quote(con, a, p["klass"], gbp, rate)
                    f_ = cost_pct(c, a, p["klass"], "SELL", q, p["qty"] * gbp)
                    chg = gbp * (1 - f_) / p["entry"] - 1
                    if sell_one("day", p, gbp, f"[{tag}] {why}", sub, md.get("conf", md.get("score")), md.get("snap"), chg, None):
                        dmeta.pop(a, None)
                        if not shadow: sset(con, f"day_exit:{a}", ts)
                    continue
            elif flat_now or opened_day != day:
                why = day_flat_reason(mins, flat, e0, D["flatten_uk"], D["entry_start_uk"], live)
                q = _quote(con, a, p["klass"], gbp, rate)
                f_ = cost_pct(c, a, p["klass"], "SELL", q, p["qty"] * gbp)
                chg = gbp * (1 - f_) / p["entry"] - 1
                if sell_one("day", p, gbp, f"[{tag}] {why}", sub, md.get("conf", md.get("score")), md.get("snap"), chg, None):
                    dmeta.pop(a, None)
                    if not shadow: sset(con, f"day_exit:{a}", ts)
                continue
            if not md:
                pts = history(con, a)
                at_ = daypot.atr_pct(pts, int((D.get("atr") or {}).get("bars", 14)), int((D.get("atr") or {}).get("min_bars", 4)))
                tp_, sl_ = daypot.levels(DM["take_profit"], DM["stop"], at_)
                md = dmeta[a] = dict(sub="momentum", tp=round(tp_, 3), sl=round(sl_, 3), atr=round(at_, 3) if at_ else None,
                                     peak=max(gbp, p["entry"]), opened=p["opened"], legacy=True)
                note_book()
            if shadow:
                _lift_day_peak(md, a, p["opened"], shadow_state)
            if md.get("sub") not in SUBC: continue
            q = _quote(con, a, p["klass"], gbp, rate)
            f_ = cost_pct(c, a, p["klass"], "SELL", q, p["qty"] * gbp)
            if live:
                _m, _s, comb, info, _rs, pts = score_asset(con, a, st, t)
            else:
                comb, info, pts = None, "", []
            thr = st["focus_buy_threshold"] if a in (st.get("focus_assets") or []) else st["buy_threshold"]
            old_peak, old_runner = md.get("peak"), md.get("runner")
            stop_over, ev_reason, giveback, defer = None, None, None, False
            if R.get("ev_exit") or (md.get("sub") == "burst" and R.get("burst_stop_widen")):
                evn, eev = _pos_ev(c, md, gbp, a, p["klass"], q)
                if variants.ev_action(evn, eev, R) == "exit":
                    ev_reason = variants.ev_exit_reason(evn, eev, R)
                else:
                    stop_over = variants.effective_stop_pct(md["sl"], evn, R, md.get("sub") or "day")
            if md.get("runner") and md.get("sub") == "burst" and R.get("burst_runner"):
                giveback = float(R.get("burst_runner_giveback_pct", 20))
            defer = bool(R.get("burst_runner"))
            snap = md.get("snap") if isinstance(md.get("snap"), dict) else {}
            if isinstance(md.get("snap"), str):
                try:
                    snap = json.loads(md["snap"])
                except (TypeError, ValueError):
                    snap = {}
            rt_here = snap.get("costs") if isinstance(snap, dict) else None
            if rt_here is None:
                rt_here = (cost_pct(c, a, p["klass"], "BUY", q, p["qty"] * gbp) + f_) * 100
            bar = variants.day_arm_bar(R, rt_here, md.get("atr"))
            if stop_over is None and giveback is None and not defer and bar is None:
                d = day_reason(p["entry"], gbp, p["opened"], md, SUBC[md["sub"]], st, comb, info, t, f_, thr, a)
            else:
                d = day_reason(p["entry"], gbp, p["opened"], md, SUBC[md["sub"]], st, comb, info, t, f_, thr, a,
                               stop_over, giveback, defer_burst_target=defer, arm_floor_pct=bar)
            reason = ev_reason or d["reason"]
            if d.get("at_target") and not ev_reason and defer:
                keep, why_b = _burst_keep(con, c, a, p["klass"], gbp, t)
                if keep:
                    md["runner"] = True
                    reason = None
                else:
                    reason = (f"take-profit {d['chg']*100:.1f}% >= {md['tp']:.1f}% (ATR-scaled); "
                              f"{variants.sold_at_target_note(why_b)}")
            if not live and not (reason or "").startswith("max hold"):
                md["peak"], md["runner"], reason = old_peak, old_runner, None
            if shadow and not reason and live and R.get("partial_take") and d.get("trail_on") and not md.get("partial"):
                half, _n = variants.partial_half(p["qty"], gbp, float(R.get("partial_min_gbp", 1)))
                if half:
                    full_n = float(p["qty"]) * gbp
                    extra = variants.split_extra_gbp(full_n, full_n / 2.0, f_, f_, gas, p["klass"])
                    why = variants.partial_reason(SUBC[md["sub"]]["trail_after_pct"], extra)
                    tag = md.get("sub", "momentum") + (f" {md['kind']}" if md.get("kind") else "")
                    rec = _record_would(shcon, ts, p, gbp, f"[{tag}] {why}", d["chg"], d["pkg"], book, qty=half)
                    if rec:
                        L.info(rec["log"])
                        exits.append(rec)
            part = md.get("partial") or {}
            if shadow and not reason and live and R.get("partial_take") and part and not part.get("bought"):
                subc = SUBC[md["sub"]]
                ss = daypot.sell_record(con, "day", md.get("sub") or "momentum", t, float(subc.get("record_days", 14)))
                p_ = daypot.hit_p(md.get("conf", md.get("score")), ss, int(subc.get("ev_prior_n", 10)), int(subc.get("ev_conf_full_n", 30)))
                if p["klass"] == "stock":
                    mleft = markets.mins_to_close(a, t)
                    h = 0.0 if mleft is None else max(0.0, (mleft - int(D.get("stock_flatten_before_close_min", 15))) / 60.0)
                else:
                    h = max(0.0, (flat - mins) / 60.0)
                if md.get("sub") == "burst" and subc.get("max_hold_h"):
                    h = min(h, float(subc["max_hold_h"]))
                wt = daypot.flatten_p(h, float(D.get("ev_flatten_floor", 0.40)), float(D.get("ev_flatten_full_h", 6)))
                notion = float(part["qty"]) * gbp
                rt = (cost_pct(c, a, p["klass"], "BUY", q, notion) + cost_pct(c, a, p["klass"], "SELL", q, notion)) * 100
                evb = daypot.day_ev(p_, md.get("tp") or 0, md.get("sl") or 0, rt, wt)
                if variants.buyback_ok(gbp, part.get("px"), evb, float(subc.get("ev_min_pct", 0.3))):
                    arm = dict(sleeve="day", asset=a, ts=part.get("ts") or p.get("opened"), sub=md.get("sub"),
                               setup=md.get("kind"), klass=p["klass"], level=float(part.get("px") or gbp),
                               size_gbp=notion, stop_pct=md.get("sl") or 0, target_pct=md.get("tp") or 0,
                               reason=f"buyback half: dip below the partial sale; fresh EV {evb:+.2f}%")
                    rec = _record_would_buy(shcon, ts, arm, gbp, book)
                    if rec:
                        L.info(rec["log"])
                        exits.append(rec)
            if reason:
                if reason.startswith("stop-loss"):
                    sl_used = float(md["sl"] if stop_over is None else stop_over)
                    reason = gap_tag(pts, p["entry"] * (1 - sl_used / 100) / (1 - f_), gbp, reason)
                elif reason.startswith("trailing"):
                    lvl_px = p["entry"] * (1 + d["pkg"] * (1 - d["gb"])) / (1 - f_)
                    reason = gap_tag(pts, lvl_px, gbp, reason)
                tag = md.get("sub", "momentum") + (f" {md['kind']}" if md.get("kind") else "")
                if sell_one("day", p, gbp, f"[{tag}] {reason}", md.get("sub"), md.get("conf", md.get("score")), md.get("snap"), d["chg"], d["pkg"]):
                    dmeta.pop(a, None)
                    if not shadow: sset(con, f"day_exit:{a}", ts)
            elif live and (md.get("peak") != old_peak or md.get("runner") != old_runner):
                note_book()
        if not shadow:
            held_day = {k: v for k, v in dmeta.items()
                        if con.execute("SELECT 1 FROM positions WHERE sleeve='day' AND asset=?", (k,)).fetchone()}
            if held_day != (sget(con, "day_meta") or {}):
                sset(con, "day_meta", held_day); wrote = True

        for p in rows:
            if p["sleeve"] != "risk": continue
            a = p["asset"]
            if shadow and _shadow_closed(shcon, "risk", a, p.get("opened"), book):
                continue
            if p["klass"] == "stock" and not markets.is_open(a, t): continue
            live = a in prices_gbp
            if live:
                gbp, age = prices_gbp[a], 0.0
            else:
                gbp, age = _stored_gbp(con, a, rate)
            if gbp is None or not p["entry"]: continue
            if not live and dead_quote(age, dead_min):
                continue
            q = _quote(con, a, p["klass"], gbp, rate)
            f_ = cost_pct(c, a, p["klass"], "SELL", q, p["qty"] * gbp)
            se = sentiment(con, a, st["sentiment_max_age_h"]) if live else None
            stop_over, ev_reason = None, None
            if R.get("ev_exit"):
                evn, eev = _pos_ev(c, rmeta.get(a) or {}, gbp, a, p["klass"], q)
                if variants.ev_action(evn, eev, R) == "exit":
                    ev_reason = variants.ev_exit_reason(evn, eev, R)
                else:
                    stop_over = variants.effective_stop_pct(float(RP["stop_loss_pct"]), evn, R, "risk")
            keep = float(R["risk_giveback_keep_pct"]) if R.get("risk_giveback_15") else None
            if ev_reason is None and stop_over is None and keep is None:
                d = risk_reason(p["entry"], gbp, peaks.get(a), p["opened"], RP, se, t, f_)
            else:
                d = risk_reason(p["entry"], gbp, peaks.get(a), p["opened"], RP, se, t, f_,
                                float(RP["stop_loss_pct"]) if stop_over is None else stop_over, keep)
            reason = ev_reason or d["reason"]
            if live and float(peaks.get(a) or 0) != d["peak"]:
                peaks[a] = d["peak"]; note_book()
            elif live:
                peaks[a] = d["peak"]
            if not live and not (reason or "").startswith("max hold"):
                reason = None
            if shadow and not reason and live and R.get("partial_take") and d.get("trail_on") and not (rmeta.get(a) or {}).get("partial"):
                half, _n = variants.partial_half(p["qty"], gbp, float(R.get("partial_min_gbp", 1)))
                if half:
                    full_n = float(p["qty"]) * gbp
                    extra = variants.split_extra_gbp(full_n, full_n / 2.0, f_, f_, gas, p["klass"])
                    why = variants.partial_reason(RP["trail_after_pct"], extra)
                    rec = _record_would(shcon, ts, p, gbp, why, d["chg"], d["pk_gain"], book, qty=half)
                    if rec:
                        L.info(rec["log"])
                        exits.append(rec)
            part = (rmeta.get(a) or {}).get("partial") or {}
            if shadow and not reason and live and R.get("partial_take") and part and not part.get("bought"):
                meta = rmeta.get(a) or {}
                ss = riskpot.setup_stats(con, RP, t, sub=meta.get("setup"))
                p_ = daypot.hit_p(meta.get("conf"), ss, int(RP.get("ev_prior_n", 10)), int(RP.get("ev_conf_full_n", 30)))
                notion = float(part["qty"]) * gbp
                rt = (cost_pct(c, a, p["klass"], "BUY", q, notion) + cost_pct(c, a, p["klass"], "SELL", q, notion)) * 100
                evb = variants.fresh_ev(p_, variants.risk_lock_pct(RP), float(RP.get("stop_loss_pct", 12)), rt)
                if variants.buyback_ok(gbp, part.get("px"), evb, float(RP.get("ev_min_pct", 0.3))):
                    arm = dict(sleeve="risk", asset=a, ts=part.get("ts") or p.get("opened"), sub=meta.get("setup") or "breakout",
                               setup=meta.get("setup") or "breakout", klass=p["klass"], level=float(part.get("px") or gbp),
                               size_gbp=notion, stop_pct=float(RP.get("stop_loss_pct", 12)),
                               target_pct=variants.risk_lock_pct(RP),
                               reason=f"buyback half: dip below the partial sale; fresh EV {evb:+.2f}%")
                    rec = _record_would_buy(shcon, ts, arm, gbp, book)
                    if rec:
                        L.info(rec["log"])
                        exits.append(rec)
            if not reason: continue
            pts = history(con, a) if live else []
            if "stop-loss" in reason:
                sl_used = float(RP["stop_loss_pct"]) if stop_over is None else float(stop_over)
                reason = gap_tag(pts, p["entry"] * (1 - sl_used / 100) / (1 - f_), gbp, reason)
            elif "trailing" in reason and d["peak"]:
                reason = gap_tag(pts, d["peak"] * (1 - RP["trail_pct"] / 100), gbp, reason)
            meta = rmeta.get(a) or {}
            if sell_one("risk", p, gbp, reason, meta.get("setup") or "breakout", meta.get("conf"), meta.get("snap"), d["chg"], d["pk_gain"]):
                peaks.pop(a, None); rmeta.pop(a, None)
                if not shadow: sset(con, f"risk_exit:{a}", ts)
        if shadow:
            _store_shadow_peaks(shcon, dmeta, peaks, rows, shadow_state, book)
            shcon.commit()
            return exits, False
        held = {a for (a,) in con.execute("SELECT asset FROM positions WHERE sleeve='risk'")}
        still_p = {k: v for k, v in peaks.items() if k in held}
        still_m = {k: v for k, v in rmeta.items() if k in held}
        if still_p != (sget(con, "risk_peaks") or {}):
            sset(con, "risk_peaks", still_p); wrote = True
        if still_m != (sget(con, "risk_meta") or {}):
            sset(con, "risk_meta", still_m); wrote = True
        return exits, wrote
    finally:
        if shcon is not None:
            shcon.close()


def _ws_connect(url, timeout):
    rest = url.split("://", 1)[1]
    host, _, path = rest.partition("/")
    path = "/" + path
    port = 443
    if ":" in host:
        host, port_s = host.rsplit(":", 1); port = int(port_s)
    raw = socket.create_connection((host, port), timeout)
    ss = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    ss.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = ss.recv(4096)
        if not chunk: raise ConnectionError("websocket handshake closed")
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        raise ConnectionError("websocket handshake rejected")
    ss.settimeout(timeout)
    return ss, buf


def _ws_send(ss, text, opcode=0x1):
    data = text.encode() if isinstance(text, str) else text
    mask = os.urandom(4)
    n = len(data)
    hdr = bytearray([0x80 | opcode])
    if n < 126: hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126); hdr.extend(struct.pack(">H", n))
    else:
        hdr.append(0x80 | 127); hdr.extend(struct.pack(">Q", n))
    hdr.extend(mask)
    hdr.extend(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    ss.sendall(hdr)


def _ws_recv(ss, buf):
    while True:
        if len(buf) < 2:
            chunk = ss.recv(4096)
            if not chunk: return None, None, buf
            buf += chunk
            continue
        b0, b1 = buf[0], buf[1]
        opcode, ln, i = b0 & 0x0F, b1 & 0x7F, 2
        if ln == 126:
            if len(buf) < 4: buf += ss.recv(4096); continue
            ln, i = struct.unpack(">H", buf[2:4])[0], 4
        elif ln == 127:
            if len(buf) < 10: buf += ss.recv(4096); continue
            ln, i = struct.unpack(">Q", buf[2:10])[0], 10
        masked = b1 & 0x80
        need = i + (4 if masked else 0) + ln
        if len(buf) < need:
            chunk = ss.recv(max(4096, need - len(buf)))
            if not chunk: return None, None, buf
            buf += chunk
            continue
        if masked:
            mask = buf[i:i + 4]; i += 4
            payload = bytes(b ^ mask[j % 4] for j, b in enumerate(buf[i:i + ln]))
        else:
            payload = buf[i:i + ln]
        buf = buf[i + ln:]
        if opcode == 9:
            _ws_send(ss, payload, opcode=0xA); continue
        if opcode in (1, 8):
            return opcode, payload, buf


def _ws_loop(name, url, subscribe, parse, deliver, stop):
    delay = 1
    while not stop.is_set():
        try:
            ss, buf = _ws_connect(url, 25)
            _ws_send(ss, subscribe())
            L.info("%s stream up", name)
            delay = 1
            while not stop.is_set():
                op, payload, buf = _ws_recv(ss, buf)
                if op is None or op == 8: break
                if not payload: continue
                try: msg = json.loads(payload.decode())
                except Exception: continue
                for asset, raw, quote in parse(msg):
                    deliver(asset, raw, quote, name.lower())
        except Exception as e:
            L.warning("%s stream dropped (%s); REST fallback, the 5-min cycle still exits", name, e)
        if stop.wait(delay): return
        delay = min(30, delay * 2)


def _rest_once(con, c, plan, rate, deliver):
    import requests
    ts = iso()
    for item in plan["kraken"]:
        try:
            pair = item["symbol"].replace("/", "")
            r = requests.get(pricesrc.KRAKEN + "/Ticker", params={"pair": pair}, timeout=8)
            route = {item["asset"]: dict(pair=pair, quote=item["quote"], norm=pricesrc.kraken_norm(pair))}
            rows = pricesrc.parse_kraken_ticker(r.json(), route, rate, ts)
            usd = (rows.get(item["asset"]) or {}).get("usd")
            if usd: deliver(item["asset"], usd, "USD", "kraken-rest")
        except Exception as e:
            L.warning("Kraken REST %s failed (%s); the 5-min cycle still exits", item["asset"], e)
    for item in plan["coinbase"]:
        try:
            r = requests.get(f"{pricesrc.COINBASE}/products/{item['symbol']}/ticker", timeout=8)
            deliver(item["asset"], float(r.json()["price"]), item["quote"], "coinbase-rest")
        except Exception as e:
            L.warning("Coinbase REST %s failed (%s); the 5-min cycle still exits", item["asset"], e)
    for item in plan["rest"]:
        try:
            if item["kind"] == "stock":
                import collector
                _s, price, meta, _v = collector.stock_quote(item["asset"])
                if not price or not meta: continue
                factor = collector.usd_factor(con, meta.get("currency"))
                if factor: deliver(item["asset"], price * factor, "USD", "yahoo")
            elif item["kind"] == "kyber":
                import collector
                usd = collector._kyber_usd(c, item.get("route") or {})
                if usd: deliver(item["asset"], usd, "USD", "kyber")
            elif item["kind"] == "coingecko":
                import collector
                usd, src = None, "coingecko"
                cid = (item.get("route") or {}).get("id") or pricesrc._cgid(con, item["asset"])
                if cid and pricesrc.allow(con, c, now(), "live"):
                    own = collector._BUDGET_CON is None
                    if own: collector.bind_budget(con, c)
                    try:
                        d = collector.get(f"{collector.CG}/simple/price", {"ids": cid, "vs_currencies": "usd"}, tries=1, purpose="live")
                    finally:
                        if own: collector.bind_budget(None, None)
                    raw = ((d or {}).get(cid) or {}).get("usd")
                    if raw: usd = float(raw)
                if not usd:
                    rows = pricesrc.backup_quotes(con, c, [item["asset"]], collector.get, ts, limit=1)
                    row = rows.get(item["asset"]) or {}
                    if row.get("usd"): usd, src = float(row["usd"]), row.get("source") or "geckoterminal"
                if usd: deliver(item["asset"], usd, "USD", src)
        except Exception as e:
            L.warning("REST %s failed (%s); the 5-min cycle still exits", item["asset"], e)


def books_to_watch(main_con):
    """Open positions and armed entries across main and every variant, one row per symbol.

    The price socket is shared. A coin held on two books is fetched once.
    """
    rows, seen = [], set()
    cons = [("main", main_con)]
    extra = []
    for name, path in entries.variant_books():
        try:
            vcon = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
            vcon.row_factory = sqlite3.Row
            extra.append(vcon)
            cons.append((name, vcon))
        except Exception as e:
            L.warning("%s not readable for prices (%s)", name, e)
    try:
        for _name, con in cons:
            for r in entries.watched_rows(con, iso()):
                if r["asset"] in seen:
                    continue
                seen.add(r["asset"])
                rows.append(r)
    finally:
        for vcon in extra:
            vcon.close()
    return rows


def _streams(rate_box, seen, deliver, stop):
    """Sockets for listed coins. REST for the rest, and for a socket that has gone quiet."""
    ws_stop = threading.Event()
    threads, last_key = [], None
    warned = {}
    while not stop.is_set():
        try:
            con = db(); c = cfg()
            fx = usd_gbp(con)
            rate_box["fx"] = fx[0] if fx else None
            rows = books_to_watch(con)
            plan = watch_plan(rows, pricesrc.routes_of(con))
            key = (tuple((x["asset"], x["symbol"]) for x in plan["kraken"]),
                   tuple((x["asset"], x["symbol"]) for x in plan["coinbase"]))
            if key != last_key:
                ws_stop.set()
                for th in threads: th.join(timeout=2)
                ws_stop = threading.Event(); threads = []; last_key = key
                if plan["kraken"]:
                    by = {x["symbol"]: x for x in plan["kraken"]}
                    def sub_k(symbols=list(by)):
                        return json.dumps({"method": "subscribe", "params": {"channel": "ticker", "symbol": symbols}})
                    def parse_k(msg, by=by):
                        out = []
                        for sym, px in kraken_ticks(msg):
                            hit = by.get(sym)
                            if hit: out.append((hit["asset"], px, hit["quote"]))
                        return out
                    threads.append(threading.Thread(target=_ws_loop, args=("Kraken", "wss://ws.kraken.com/v2", sub_k, parse_k, deliver, ws_stop), daemon=True))
                if plan["coinbase"]:
                    byc = {x["symbol"]: x for x in plan["coinbase"]}
                    def sub_c(ids=list(byc)):
                        return json.dumps({"type": "subscribe", "product_ids": ids, "channels": ["ticker"]})
                    def parse_c(msg, byc=byc):
                        hit = coinbase_tick(msg)
                        if not hit: return []
                        prod, px = hit
                        row = byc.get(prod)
                        return [(row["asset"], px, row["quote"])] if row else []
                    threads.append(threading.Thread(target=_ws_loop, args=("Coinbase", "wss://ws-feed.exchange.coinbase.com", sub_c, parse_c, deliver, ws_stop), daemon=True))
                for th in threads: th.start()
            now_m = time.monotonic()
            stale = dict(kraken=[], coinbase=[], rest=list(plan["rest"]))
            for item in plan["kraken"]:
                age = now_m - seen.get(item["asset"], 0)
                if item["asset"] not in seen or age > QUIET_S:
                    if item["asset"] in seen and age > QUIET_S and now_m - warned.get(item["asset"], 0) > QUIET_S:
                        L.warning("stream quiet for %s; REST fallback, the 5-min cycle still exits", item["asset"])
                        warned[item["asset"]] = now_m
                    stale["kraken"].append(item)
            for item in plan["coinbase"]:
                age = now_m - seen.get(item["asset"], 0)
                if item["asset"] not in seen or age > QUIET_S:
                    if item["asset"] in seen and age > QUIET_S and now_m - warned.get(item["asset"], 0) > QUIET_S:
                        L.warning("stream quiet for %s; REST fallback, the 5-min cycle still exits", item["asset"])
                        warned[item["asset"]] = now_m
                    stale["coinbase"].append(item)
            if rate_box["fx"] and (stale["kraken"] or stale["coinbase"] or stale["rest"]):
                _rest_once(con, c, stale, rate_box["fx"], deliver)
            con.close()
        except Exception as e:
            L.warning("stream supervisor: %s", e)
        if stop.wait(REST_S): break
    ws_stop.set()


def _shadow_variants(fresh, recv):
    """WOULD EXIT and WOULD BUY for every variant book, using that book's rules.

    No variant is sold here. entry_watch is the only book that can buy, and only
    when ENTRY_WATCH_SHADOW is off. Prices are the tick already fetched for main.
    """
    for name, path in entries.variant_books():
        c = config_for(name)
        try:
            vcon = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
            vcon.row_factory = sqlite3.Row
        except Exception as e:
            L.warning("%s shadow skipped (%s)", name, e)
            continue
        try:
            for e in shadow_evaluate(vcon, fresh, recv, book=name, c=c):
                print(e["log"], flush=True)
            for e in act_entries(vcon, fresh, recv, live=False, book=name, c=c):
                print(e["log"], flush=True)
        except Exception:
            L.exception("%s shadow tick failed; that book's cycle still trades", name)
        finally:
            vcon.close()
        if name == "entry_watch" and not entry_shadow_mode():
            wcon = sqlite3.connect(path, timeout=30)
            wcon.row_factory = sqlite3.Row
            try:
                for e in act_entries(wcon, fresh, recv, live=True, book=name, c=c):
                    print(e["log"], flush=True)
            except Exception:
                L.exception("entry_watch live buy failed; the cycle still buys")
            finally:
                wcon.close()


def serve():
    latest, guard = {}, threading.Lock()
    rate_box, seen, stop = {"fx": None}, {}, threading.Event()

    def deliver(asset, raw, quote, src):
        gbp = to_gbp(raw, quote, rate_box["fx"])
        if gbp is None: return
        with guard:
            latest[asset] = dict(gbp=gbp, mono=time.monotonic(), src=src)
            seen[asset] = latest[asset]["mono"]

    threading.Thread(target=_streams, args=(rate_box, seen, deliver, stop), daemon=True).start()
    if shadow_mode():
        L.info("exit watcher up in shadow mode (%s). It logs WOULD EXIT and does not write trades. "
               "Set EXIT_WATCH_SHADOW=0 to sell for real.", shadow_db_path())
    else:
        L.info("exit watcher up (live sells; Day then Risk)")
    if entry_shadow_mode():
        L.info("entry watcher shadow (%s). It logs WOULD BUY and does not buy. "
               "Set ENTRY_WATCH_SHADOW=0 to buy on the entry_watch variant only.", shadow_db_path())
    else:
        L.info("entry watcher live on %s only. The main book still only logs WOULD BUY.", entries.variant_db_path())
    while not stop.is_set():
        if KILL.exists():
            time.sleep(5); continue
        with guard:
            fresh = {a: v["gbp"] for a, v in latest.items() if time.monotonic() - v["mono"] <= LIVE_S}
            recv = {a: v["mono"] for a, v in latest.items()}
        try:
            con = db()
            for e in evaluate(con, fresh, recv):
                print(e["log"], flush=True)
            # Main entries are always a shadow log, even after ENTRY_WATCH_SHADOW=0.
            for e in act_entries(con, fresh, recv, live=False):
                print(e["log"], flush=True)
            con.close()
            _shadow_variants(fresh, recv)
        except Exception:
            L.exception("tick failed; the 5-min cycle still exits and still buys")
        time.sleep(0.25)


if __name__ == "__main__":
    serve()
