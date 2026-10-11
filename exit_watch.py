"""Always-on exit watcher for open Day and Risk positions.

Entries stay on the 5-minute cycle. This process only sells, and only with the
same rules and the same fill as that cycle (exits.day_reason / risk_reason and
trader.apply_sell). It watches Kraken and Coinbase public websockets where the
coin is listed, and polls everything else about every 20 seconds. If a stream
drops it keeps running; the 5-minute cycle still exits.

The first deploy is shadow mode (EXIT_WATCH_SHADOW defaults to on). The watcher
tracks open positions and logs WOULD EXIT (trigger, price, time) to
data/exit_shadow.db. It does not write trades, cash, or positions. Review with
`python3 exit_shadow_report.py`, then set EXIT_WATCH_SHADOW=0 and restart to
sell for real.

Deploy on the VPS (Ubuntu, user bot; trading-cycle.timer already runs the cycle).
Do this only after the branch is on main:

    sudo cp deploy/trading-exit-watch.service /etc/systemd/system/
    # WorkingDirectory and ExecStart must be the clone run_cycle.sh uses
    # (the directory that contains this file and data/trading.db).
    # The unit sets EXIT_WATCH_SHADOW=1. Leave that until the report looks right.
    sudo systemctl daemon-reload
    sudo systemctl enable --now trading-exit-watch
    journalctl -u trading-exit-watch -f
    python3 exit_shadow_report.py
    # then add EXIT_WATCH_SHADOW=0 to /home/bot/.trading_env and:
    sudo systemctl restart trading-exit-watch

Creating KILL in that clone stops this watcher as well as the cycle. A quiet
stream is a warning, not a crash. Restart=on-failure is for a dead process.
"""
import base64, json, os, socket, sqlite3, ssl, struct, threading, time
from datetime import datetime, timedelta
from pathlib import Path

import daypot
import markets
import pricesrc
from common import KILL, LON, cfg, db, iso, log, now, usd_gbp
from exits import book_lock, day_flat_reason, day_reason, gap_tag, risk_reason, stock_flatten_reason
from trader import apply_sell, cost_pct, gas_gbp, history, hm, score_asset, sentiment, sget, sset

L = log("exit_watch")
REST_S = 20
QUIET_S = 45
LIVE_S = 60


def shadow_mode():
    """True: log WOULD EXIT and leave the book alone. This is the default.

    Set EXIT_WATCH_SHADOW=0 (or false/off/live) after the shadow report looks
    right. That is the only switch that lets this process write a sell.
    """
    raw = os.environ.get("EXIT_WATCH_SHADOW")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "live")


def shadow_db_path():
    """Sibling of the paper book, unless EXIT_SHADOW_DB points somewhere else."""
    override = os.environ.get("EXIT_SHADOW_DB")
    if override:
        return Path(override)
    from common import DB
    return Path(DB).resolve().parent / "exit_shadow.db"


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
    con.execute("CREATE TABLE IF NOT EXISTS shadow_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.commit()
    return con


def _shadow_peaks(shcon):
    if shcon is None:
        return {"day": {}, "risk": {}}
    row = shcon.execute("SELECT value FROM shadow_state WHERE key='peaks'").fetchone()
    if not row:
        return {"day": {}, "risk": {}}
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return {"day": {}, "risk": {}}
    data.setdefault("day", {})
    data.setdefault("risk", {})
    return data


def _shadow_seen(shcon, sleeve, asset, opened):
    return shcon.execute(
        "SELECT 1 FROM would_exit WHERE sleeve=? AND asset=? AND IFNULL(opened,'')=IFNULL(?,'') LIMIT 1",
        (sleeve, asset, opened)).fetchone() is not None


def _would_line(ts, sleeve, asset, price, chg, peak, reason):
    peak_s = f" peak {peak * 100:+.1f}%" if peak is not None else ""
    return f"WOULD EXIT {sleeve} {asset} at {ts} trigger £{price:.6g} net {chg * 100:+.1f}%{peak_s} | {reason}"


def _record_would(shcon, ts, p, gbp, reason, chg, peak):
    """First WOULD EXIT for this open position. A later tick does not add another row."""
    if _shadow_seen(shcon, p["sleeve"], p["asset"], p.get("opened")):
        return None
    shcon.execute(
        "INSERT INTO would_exit(ts,sleeve,asset,opened,trigger,price,chg,peak,qty,entry,klass) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ts, p["sleeve"], p["asset"], p.get("opened"), reason, gbp, chg, peak, p.get("qty"), p.get("entry"), p.get("klass")))
    shcon.commit()
    line = _would_line(ts, p["sleeve"], p["asset"], gbp, chg, peak, reason)
    return dict(sleeve=p["sleeve"], asset=p["asset"], price=gbp, reason=reason, pnl=None,
                log=line, chg=chg, peak=peak, shadow=True, ts=ts)


def _lift_day_peak(md, asset, opened, state):
    sp = (state.get("day") or {}).get(asset)
    if not sp or sp.get("opened") != opened:
        return
    md["peak"] = max(float(md.get("peak") or 0), float(sp.get("peak") or 0))
    if sp.get("runner"):
        md["runner"] = True


def _store_shadow_peaks(shcon, dmeta, peaks, rows, state):
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
    shcon.execute("INSERT OR REPLACE INTO shadow_state VALUES('peaks', ?)",
                  (json.dumps({"day": day, "risk": risk}),))


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
    row = con.execute("""SELECT price FROM prices WHERE asset=? AND source!='backfill' AND ts<=?
                         ORDER BY ts DESC LIMIT 1""", (asset, iso(now() + timedelta(seconds=1)))).fetchone()
    return float(row[0]) * rate if row and rate else None


def _line(sleeve, asset, price, chg, peak, lat, reason):
    peak_s = f" peak {peak * 100:+.1f}%" if peak is not None else ""
    lat_s = f"{lat:.3f}s" if lat is not None else "n/a"
    return f"EXIT {sleeve} {asset} trigger £{price:.6g} net {chg * 100:+.1f}%{peak_s} latency {lat_s} | {reason}"


def evaluate(con, prices_gbp, recv_mono=None):
    """Sell open Day positions, then Risk, at these GBP prices. No entries.

    `prices_gbp` holds only fresh ticks. Flatten and max-hold may use the last
    stored price. A stop, trail, or target waits for a fresh tick; if the
    stream is quiet the 5-minute cycle still applies those. Returns exit records.
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


def _tick(con, prices_gbp, recv_mono, started):
    c = cfg(); t = now(); ts = iso(t)
    fx = usd_gbp(con, c["fx"]["max_age_h"])
    if not fx: return [], False
    rate = fx[0]
    D, st = c["day"], c["strategy"]
    RP = c.get("riskpot") or {}
    DM, DB = D["momentum"], D["burst"]
    SUBC = {"momentum": DM, "burst": DB}
    gas = gas_gbp(con, c)
    lt = t.astimezone(LON); mins = lt.hour * 60 + lt.minute; day = lt.date().isoformat()
    e0, flat = hm(D["entry_start_uk"]), hm(D["flatten_uk"])
    flat_now = mins >= flat or mins < e0
    rows = [dict(r) for r in con.execute("SELECT * FROM positions WHERE sleeve IN ('day','risk')")]
    rows.sort(key=lambda r: (0 if r["sleeve"] == "day" else 1, r["asset"]))
    dmeta = dict(sget(con, "day_meta") or {})
    peaks = dict(sget(con, "risk_peaks") or {})
    rmeta = dict(sget(con, "risk_meta") or {})
    exits, wrote = [], False
    shadow = shadow_mode()
    shcon = _shadow_connect() if shadow else None
    shadow_state = _shadow_peaks(shcon)
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
            rec = _record_would(shcon, ts, p, gbp, reason, chg, peak)
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
            if shadow and _shadow_seen(shcon, "day", a, p.get("opened")):
                continue
            live = a in prices_gbp
            gbp = prices_gbp[a] if live else _stored_gbp(con, a, rate)
            if gbp is None or not p["entry"]: continue
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
            if flat_now or opened_day != day:
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
            d = day_reason(p["entry"], gbp, p["opened"], md, SUBC[md["sub"]], st, comb, info, t, f_, thr, a)
            reason = d["reason"]
            if not live and not (reason or "").startswith("max hold"):
                md["peak"], md["runner"], reason = old_peak, old_runner, None
            if reason:
                if reason.startswith("stop-loss"):
                    reason = gap_tag(pts, p["entry"] * (1 - md["sl"] / 100) / (1 - f_), gbp, reason)
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
            if shadow and _shadow_seen(shcon, "risk", a, p.get("opened")):
                continue
            if p["klass"] == "stock" and not markets.is_open(a, t): continue
            live = a in prices_gbp
            gbp = prices_gbp[a] if live else _stored_gbp(con, a, rate)
            if gbp is None or not p["entry"]: continue
            q = _quote(con, a, p["klass"], gbp, rate)
            f_ = cost_pct(c, a, p["klass"], "SELL", q, p["qty"] * gbp)
            se = sentiment(con, a, st["sentiment_max_age_h"]) if live else None
            d = risk_reason(p["entry"], gbp, peaks.get(a), p["opened"], RP, se, t, f_)
            reason = d["reason"]
            if live and float(peaks.get(a) or 0) != d["peak"]:
                peaks[a] = d["peak"]; note_book()
            elif live:
                peaks[a] = d["peak"]
            if not live and not (reason or "").startswith("max hold"):
                reason = None
            if not reason: continue
            pts = history(con, a) if live else []
            if "stop-loss" in reason:
                reason = gap_tag(pts, p["entry"] * (1 - RP["stop_loss_pct"] / 100) / (1 - f_), gbp, reason)
            elif "trailing" in reason and d["peak"]:
                reason = gap_tag(pts, d["peak"] * (1 - RP["trail_pct"] / 100), gbp, reason)
            meta = rmeta.get(a) or {}
            if sell_one("risk", p, gbp, reason, meta.get("setup") or "breakout", meta.get("conf"), meta.get("snap"), d["chg"], d["pk_gain"]):
                peaks.pop(a, None); rmeta.pop(a, None)
                if not shadow: sset(con, f"risk_exit:{a}", ts)
        if shadow:
            _store_shadow_peaks(shcon, dmeta, peaks, rows, shadow_state)
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
            elif item["kind"] == "coingecko" and pricesrc.allow(con, c, now(), "live"):
                import collector
                cid = (item.get("route") or {}).get("id") or pricesrc._cgid(con, item["asset"])
                if not cid: continue
                d = collector.get(f"{collector.CG}/simple/price", {"ids": cid, "vs_currencies": "usd"}, tries=1, purpose="live")
                usd = ((d or {}).get(cid) or {}).get("usd")
                if usd: deliver(item["asset"], float(usd), "USD", "coingecko")
        except Exception as e:
            L.warning("REST %s failed (%s); the 5-min cycle still exits", item["asset"], e)


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
            rows = con.execute("SELECT sleeve, asset, klass FROM positions WHERE sleeve IN ('day','risk') ORDER BY sleeve, asset").fetchall()
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
        L.info("exit watcher up (live sells; Day then Risk; entries stay on the 5-min cycle)")
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
            con.close()
        except Exception:
            L.exception("tick failed; the 5-min cycle still exits")
        time.sleep(0.25)


if __name__ == "__main__":
    serve()
