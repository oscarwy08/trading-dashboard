"""Shared helpers: paths, config, SQLite schema, logging."""
import logging, os, sqlite3, yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = Path(os.environ.get("TRADING_DB") or DATA / "trading.db")     # override only for tests/simulations
SENT_DIR = DATA / "sentiment"
SITE = ROOT / os.environ.get("SITE_DIR", "docs")     # published static site (GitHub Pages serves main:/docs)
KILL = ROOT / "KILL"                 # halts all trading (create the file in the repo root)
RISK_RESUME = ROOT / "RISK_RESUME"   # clears a RISK pot drawdown halt after review (trader deletes it once applied)
LON = ZoneInfo("Europe/London")
SLEEVES = ("core", "day", "risk")
# v6 (2026-10-10): two real-world accounts. Every pot has separate cash in each account (pot x account matrix):
#   crypto = Monad wallet (crypto trades: fee + gas), stocks = Trading 212 Invest (stock trades: spread + FX fee + stamp duty).
# A trade only ever uses cash of the account that matches its asset class; nothing moves between accounts automatically
# (moving money between Trading 212 and a wallet is manual in real life -> transfer_accounts.py).
ACCOUNTS = ("crypto", "stocks")
ACCT_LABEL = {"crypto": "Crypto account", "stocks": "Stocks account"}

def acct_of(klass):
    """Account an asset class settles in: stocks -> 'stocks' (Trading 212), everything else -> 'crypto' (Monad wallet)."""
    return "stocks" if klass == "stock" else "crypto"

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(filename=os.environ.get("TRADING_LOG") or ROOT / "logs" / "trading.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_NOW = None   # simulations set common._NOW to a UTC datetime to fake the clock

def log(name): return logging.getLogger(name)
def _merge(base, over):
    """Deep-merge a variant overlay onto config. Nested dicts combine; everything else is replaced."""
    out = dict(base or {})
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out

def cfg():
    """config.yaml, plus config/variants/<name>.yaml when TRADING_VARIANT is set (variant books only)."""
    base = yaml.safe_load((ROOT / "config.yaml").read_text())
    name = os.environ.get("TRADING_VARIANT")
    if not name:
        return base
    path = ROOT / "config" / "variants" / f"{name}.yaml"
    over = yaml.safe_load(path.read_text()) if path.exists() else {}
    return _merge(base, over or {})
def now(): return _NOW or datetime.now(timezone.utc).replace(microsecond=0)
def iso(dt=None): return (dt or now()).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices(ts TEXT, asset TEXT, klass TEXT, price REAL, high24 REAL, low24 REAL,
  vol24 REAL, mcap REAL, source TEXT, source_ts TEXT);   -- source_ts: the provider's last-updated time (not the fetch time)
CREATE INDEX IF NOT EXISTS ix_prices ON prices(asset, ts);
CREATE TABLE IF NOT EXISTS sentiment(ts TEXT, asset TEXT, score REAL, volume INTEGER, confidence REAL,
  summary TEXT, sources TEXT, file TEXT, ingested_at TEXT, UNIQUE(ts, asset));
CREATE TABLE IF NOT EXISTS signals(ts TEXT, sleeve TEXT, asset TEXT, momentum REAL, sentiment REAL, combined REAL,
  action TEXT, reason TEXT);
-- account tables (trades/positions/cash/equity) are in GBP; prices table stays raw USD.
-- sleeve = 'core' (long holds) or 'day' (intraday); a position belongs to one sleeve, an asset may be held in both.
-- value = qty*fill (GBP), cost = fee+slippage part, gas = simulated Monad network fee paid on top (GBP, from cash)
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT, sleeve TEXT, asset TEXT, klass TEXT, side TEXT,
  qty REAL, price REAL, fill REAL, value REAL, cost REAL, pnl REAL, reason TEXT, price_usd REAL, usd_gbp REAL, gas REAL);
CREATE TABLE IF NOT EXISTS positions(sleeve TEXT, asset TEXT, klass TEXT, qty REAL, entry REAL, cost REAL,
  opened TEXT, tranches INTEGER DEFAULT 1, PRIMARY KEY(sleeve, asset));
CREATE TABLE IF NOT EXISTS cash(sleeve TEXT PRIMARY KEY, amount REAL);   -- v6: derived pot total (sum of acct_cash), kept for old readers
-- v6: the real cash ledger, one row per pot x account (account = 'crypto' Monad wallet | 'stocks' Trading 212)
CREATE TABLE IF NOT EXISTS acct_cash(sleeve TEXT, account TEXT, amount REAL, PRIMARY KEY(sleeve, account));
CREATE TABLE IF NOT EXISTS equity(ts TEXT, equity REAL, cash REAL, pos_value REAL, btc REAL, open_n INTEGER,
  usd_gbp REAL, core REAL, day REAL, core_cash REAL, day_cash REAL);
CREATE TABLE IF NOT EXISTS fx(ts TEXT, pair TEXT, rate REAL, source TEXT, alt_rate REAL, alt_source TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
-- cash moved between sleeves without trading (no fees); stats() rebases each sleeve's history by later transfers
CREATE TABLE IF NOT EXISTS transfers(ts TEXT, from_sleeve TEXT, to_sleeve TEXT, amount REAL, reason TEXT);
-- daily closes (USD) of the CORE stock universe, used for the weekly ranking (stockrank.py)
CREATE TABLE IF NOT EXISTS daily_closes(asset TEXT, d TEXT, close REAL, PRIMARY KEY(asset, d));
-- v7.3: temporary loans between pots INSIDE one account (never crypto<->stocks) for an exceptional setup. The lender keeps a
-- receivable, the borrower a liability: pot equity = cash + positions + lent - borrowed (outstanding = amount - repaid), so a
-- loan never shows as P&L; repaid (principal only) when the borrower's position closes; P&L stays with the borrower.
CREATE TABLE IF NOT EXISTS loans(id INTEGER PRIMARY KEY, ts TEXT, account TEXT, lender TEXT, borrower TEXT, sub TEXT, asset TEXT,
  amount REAL, repaid REAL DEFAULT 0, status TEXT DEFAULT 'open', repaid_ts TEXT, reason TEXT);
-- CoinGecko HTTP calls per UK day (the demo key is 10,000/month). pricesrc.allow reads this before a call.
CREATE TABLE IF NOT EXISTS cg_calls(day TEXT PRIMARY KEY, n INTEGER NOT NULL);
-- CoinGecko calls split by purpose (scan, meta, chart, fx, fallback, held, live) so the dashboard can show what spent the day.
CREATE TABLE IF NOT EXISTS cg_purpose(day TEXT, purpose TEXT, n INTEGER NOT NULL, PRIMARY KEY(day, purpose));
-- Armed entries: the cycle records a price trigger; the watcher fires it. Rows do not move cash.
CREATE TABLE IF NOT EXISTS armed_entries(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  expires TEXT NOT NULL,
  sleeve TEXT NOT NULL,
  sub TEXT,
  setup TEXT,
  asset TEXT NOT NULL,
  account TEXT NOT NULL,
  klass TEXT NOT NULL,
  trigger TEXT NOT NULL,
  level REAL NOT NULL,
  level_max REAL,
  volume_min REAL,
  size_gbp REAL NOT NULL,
  stop_pct REAL NOT NULL,
  target_pct REAL NOT NULL,
  conf REAL,
  ev REAL,
  reason TEXT,
  snap TEXT,
  status TEXT NOT NULL,
  filled_ts TEXT,
  filled_price REAL,
  filled_by TEXT);
CREATE INDEX IF NOT EXISTS ix_armed_open ON armed_entries(status, expires);
"""

def loans_open(con):
    """Outstanding loans: [dict(id, account, lender, borrower, sub, asset, amount, repaid, out)] (v7.3)."""
    try:
        return [dict(id=r[0], account=r[1], lender=r[2], borrower=r[3], sub=r[4], asset=r[5], amount=r[6], repaid=r[7] or 0.0, out=r[6] - (r[7] or 0.0), ts=r[8])
                for r in con.execute("SELECT id, account, lender, borrower, sub, asset, amount, repaid, ts FROM loans WHERE status='open' ORDER BY id")]
    except Exception: return []

def loan_net(con):
    """{(pot, account): lent - borrowed} of the outstanding loans (v7.3; 0 for cells without loans)."""
    out = {}
    for l in loans_open(con):
        out[(l["lender"], l["account"])] = out.get((l["lender"], l["account"]), 0.0) + l["out"]
        out[(l["borrower"], l["account"])] = out.get((l["borrower"], l["account"]), 0.0) - l["out"]
    return out

def funding(c=None):
    """v6 configured funding per pot x account, e.g. {'core': {'crypto': 50.0, 'stocks': 50.0}, ...} (config accounts.funding).
    Without that section: the pot's whole start in the crypto account (pre-v6 behaviour)."""
    c = c or cfg(); f = (c.get("accounts") or {}).get("funding")
    if f: return {k: {a: float((f.get(k) or {}).get(a, 0)) for a in ACCOUNTS} for k in SLEEVES}
    total, sp = float(c["starting_balance"]), c["sleeve_split_pct"]
    return {k: {"crypto": total * float(sp.get(k, 0)) / 100, "stocks": 0.0} for k in SLEEVES}

def start_cash(c=None):
    """Starting (= current baseline) GBP per sleeve, e.g. {'core': 100.0, 'day': 60.0, 'risk': 40.0}.
    After a TRANSFER rebalance (rebalance_pots.py) this equals the original start + net transfers, so sleeve
    returns are measured against the current split; earlier equity history is rebased (see transfer_adj)."""
    c = c or cfg()
    if (c.get("accounts") or {}).get("funding"): return {k: sum(v.values()) for k, v in funding(c).items()}
    total, sp = float(c["starting_balance"]), c["sleeve_split_pct"]
    return {k: total * float(sp.get(k, 0)) / 100 for k in SLEEVES}

def _state(con, key):
    import json
    r = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    try: return json.loads(r[0]) if r else None
    except Exception: return None

def acct_start(con, c=None):
    """v6 baseline (start) per pot x account: {'core': {'crypto': 50.0, 'stocks': 50.0}, ...}.
    = the opening balances fixed at the account split (state 'account_split'.opening; fresh accounts: config funding)
    + net later transfers that involve an account (pot moves within an account, settlement legs, manual account moves).
    The split's own TRANSFER rows (kind 'split') only re-label cash inside a pot and are not counted again."""
    sp = _state(con, "account_split"); c = c or cfg()
    base = {k: dict(v) for k, v in (sp["opening"] if sp else funding(c)).items()}
    since = sp["ts"] if sp else ""
    for r in con.execute("SELECT ts, from_sleeve, to_sleeve, amount, from_account, to_account, kind FROM transfers WHERE ts>? ORDER BY ts", (since,)):
        if r[6] == "split" or not r[4] or not r[5]: continue
        base.setdefault(r[1], {a: 0.0 for a in ACCOUNTS})[r[4]] -= r[3]
        base.setdefault(r[2], {a: 0.0 for a in ACCOUNTS})[r[5]] += r[3]
    return base

def transfer_adj_acct(con, account, sleeve=None):
    """Like transfer_adj but for an account (optionally one pot's slice of it): [(ts, net GBP in)] for account transfers
    after the split (kind != 'split'). Used to rebase account equity history so transfers are never shown as P&L."""
    sp = _state(con, "account_split"); since = sp["ts"] if sp else ""
    out = []
    for r in con.execute("SELECT ts, from_sleeve, to_sleeve, amount, from_account, to_account, kind FROM transfers WHERE ts>? ORDER BY ts", (since,)):
        if r[6] == "split" or not r[4]: continue
        inn = r[5] == account and (sleeve is None or r[2] == sleeve); out_ = r[4] == account and (sleeve is None or r[1] == sleeve)
        if inn != out_: out.append((r[0], r[3] if inn else -r[3]))
    return out

def move_cash(con, frm, to, amount, reason, kind):
    """Record a cash move (no fees, no P&L) between (pot, account) cells: frm/to = (sleeve, account).
    kind: 'split' (v6 one-off re-labelling inside a pot) | 'settle' (one-off pot settlement inside one account)
    | 'pot' (rebalance_pots.py: between pots, same account) | 'account' (transfer_accounts.py: manual move between accounts).
    Refuses to overdraw. Keeps same-day DAY loss-cap baselines neutral when a pot's total changes."""
    from datetime import datetime as _dt
    assert frm[0] in SLEEVES and to[0] in SLEEVES and frm[1] in ACCOUNTS and to[1] in ACCOUNTS and frm != to, (frm, to)
    amount = float(amount)
    have = (con.execute("SELECT amount FROM acct_cash WHERE sleeve=? AND account=?", frm).fetchone() or [0.0])[0]
    if amount <= 0: return 0.0
    if amount > have + 1e-9: raise SystemExit(f"{frm[0]}/{frm[1]} has only £{have:.4f} cash (asked £{amount:.4f}) - would need selling; not done")
    ts = iso()
    con.execute("INSERT OR IGNORE INTO acct_cash VALUES(?,?,0)", to)
    con.execute("UPDATE acct_cash SET amount=amount-? WHERE sleeve=? AND account=?", (amount, *frm))
    con.execute("UPDATE acct_cash SET amount=amount+? WHERE sleeve=? AND account=?", (amount, *to))
    con.execute("INSERT INTO transfers(ts,from_sleeve,to_sleeve,amount,reason,from_account,to_account,kind) VALUES(?,?,?,?,?,?,?,?)",
                (ts, frm[0], to[0], amount, reason, frm[1], to[1], kind))
    if frm[0] != to[0]:
        day = now().astimezone(LON).date().isoformat()
        for sl, d in ((frm[0], -amount), (to[0], amount)):
            k = f"day_start:{sl}:{day}"
            r = con.execute("SELECT value FROM state WHERE key=?", (k,)).fetchone()
            if r: con.execute("UPDATE state SET value=? WHERE key=?", (str(float(r[0]) + d), k))
    sync_pot_cash(con)
    return amount

def sync_pot_cash(con):
    """Mirror acct_cash into the legacy per-pot cash table (pot total = crypto + stocks)."""
    for sl in SLEEVES:
        v = con.execute("SELECT COALESCE(SUM(amount),0) FROM acct_cash WHERE sleeve=?", (sl,)).fetchone()[0]
        con.execute("INSERT OR REPLACE INTO cash VALUES(?,?)", (sl, v))

def transfer_adj(con, sleeve):
    """[(ts, net GBP into sleeve)] sorted by ts. A sleeve equity point at time t is rebased by adding the net
    transfers that happened AFTER t, so charts/drawdowns don't show cash moves as gains or losses."""
    rows = con.execute("SELECT ts, from_sleeve, to_sleeve, amount FROM transfers ORDER BY ts").fetchall()
    return [(r[0], (r[3] if r[2] == sleeve else 0) - (r[3] if r[1] == sleeve else 0)) for r in rows
            if sleeve in (r[1], r[2])]

def rebase(ts, adj):
    """Net transfers after ts (adj from transfer_adj)."""
    return sum(a for t, a in adj if t > ts)

def usd_gbp(con, max_age_h=None):
    """Latest stored USD->GBP rate as (rate, ts, source, stale) or None if never fetched."""
    r = con.execute("SELECT rate, ts, source FROM fx WHERE pair='USDGBP' AND rate>0 ORDER BY ts DESC LIMIT 1").fetchone()
    if not r: return None
    stale = max_age_h is not None and datetime.fromisoformat(r[1]) < now() - timedelta(hours=max_age_h)
    return r[0], r[1], r[2], stale

def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    if "gas" not in cols: con.execute("ALTER TABLE trades ADD COLUMN gas REAL")   # simulated Monad gas (GBP) per trade
    if "sub" not in cols: con.execute("ALTER TABLE trades ADD COLUMN sub TEXT")    # v7: DAY sub-strategy (momentum | burst)
    if "conf" not in cols: con.execute("ALTER TABLE trades ADD COLUMN conf REAL")  # v7: DAY burst confidence 0-1 / momentum signal score
    if "snap" not in cols: con.execute("ALTER TABLE trades ADD COLUMN snap TEXT")  # v7.7: entry confidence, components, p, target/loss %, costs, EV
    ecols = {r[1] for r in con.execute("PRAGMA table_info(equity)")}
    for col in ("risk", "risk_cash"):                                              # RISK pot (v4)
        if col not in ecols: con.execute(f"ALTER TABLE equity ADD COLUMN {col} REAL")
    for col, typ in (("crypto", "REAL"), ("stocks", "REAL"), ("crypto_cash", "REAL"), ("stocks_cash", "REAL"), ("accts", "TEXT")):   # v6 accounts
        if col not in ecols: con.execute(f"ALTER TABLE equity ADD COLUMN {col} {typ}")
    tcols = {r[1] for r in con.execute("PRAGMA table_info(transfers)")}
    for col in ("from_account", "to_account", "kind"):                             # v6: pot x account transfers
        if col not in tcols: con.execute(f"ALTER TABLE transfers ADD COLUMN {col} TEXT")
    pcols = {r[1] for r in con.execute("PRAGMA table_info(prices)")}
    if "source_ts" not in pcols: con.execute("ALTER TABLE prices ADD COLUMN source_ts TEXT")  # provider last-updated time
    return con
