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

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(filename=os.environ.get("TRADING_LOG") or ROOT / "logs" / "trading.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_NOW = None   # simulations set common._NOW to a UTC datetime to fake the clock

def log(name): return logging.getLogger(name)
def cfg(): return yaml.safe_load((ROOT / "config.yaml").read_text())
def now(): return _NOW or datetime.now(timezone.utc).replace(microsecond=0)
def iso(dt=None): return (dt or now()).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices(ts TEXT, asset TEXT, klass TEXT, price REAL, high24 REAL, low24 REAL,
  vol24 REAL, mcap REAL, source TEXT);
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
CREATE TABLE IF NOT EXISTS cash(sleeve TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS equity(ts TEXT, equity REAL, cash REAL, pos_value REAL, btc REAL, open_n INTEGER,
  usd_gbp REAL, core REAL, day REAL, core_cash REAL, day_cash REAL);
CREATE TABLE IF NOT EXISTS fx(ts TEXT, pair TEXT, rate REAL, source TEXT, alt_rate REAL, alt_source TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
-- cash moved between sleeves without trading (no fees); stats() rebases each sleeve's history by later transfers
CREATE TABLE IF NOT EXISTS transfers(ts TEXT, from_sleeve TEXT, to_sleeve TEXT, amount REAL, reason TEXT);
-- daily closes (USD) of the CORE stock universe, used for the weekly ranking (stockrank.py)
CREATE TABLE IF NOT EXISTS daily_closes(asset TEXT, d TEXT, close REAL, PRIMARY KEY(asset, d));
"""

def start_cash(c=None):
    """Starting (= current baseline) GBP per sleeve, e.g. {'core': 100.0, 'day': 60.0, 'risk': 40.0}.
    After a TRANSFER rebalance (rebalance_pots.py) this equals the original start + net transfers, so sleeve
    returns are measured against the current split; earlier equity history is rebased (see transfer_adj)."""
    c = c or cfg(); total, sp = float(c["starting_balance"]), c["sleeve_split_pct"]
    return {k: total * float(sp.get(k, 0)) / 100 for k in SLEEVES}

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
    ecols = {r[1] for r in con.execute("PRAGMA table_info(equity)")}
    for col in ("risk", "risk_cash"):                                              # RISK pot (v4)
        if col not in ecols: con.execute(f"ALTER TABLE equity ADD COLUMN {col} REAL")
    return con
