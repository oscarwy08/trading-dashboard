"""Hourly news scores. Not part of the trading cycle.

Free RSS only. Headlines are scored with a small finance lexicon, the same idea as
VADER, and blended with VADER when that package is already installed. A transformer
is not loaded: this box has 4 GB. Articles are matched to tracked coins and stocks,
duplicates are skipped, and rows land in data/news.db so the cycle never waits on them.
"""
import hashlib
import os
import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from common import DATA, ROOT, cfg

FEEDS = (
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("decrypt", "https://decrypt.co/feed"),
    ("yahoo", "https://finance.yahoo.com/news/rssindex"),
)
UA = "paper-trader"
# Words that would false-match a ticker. Tickers are only read when they appear in capitals.
STOP = {"ALL", "AND", "ARE", "BIG", "CAN", "FOR", "HAS", "INC", "IT", "LOW", "NEW", "NOT", "NOW", "OFF", "OLD",
        "ONE", "OUT", "SEE", "THE", "TOP", "USD", "GBP", "ETF", "CEO", "IPO", "ATH", "SEC", "FED"}
GOOD = ("surge", "soar", "rally", "jump", "gain", "beat", "bullish", "upgrade", "breakthrough", "approval",
        "adopt", "partnership", "buyback", "profit", "growth", "optimism", "inflow", "inflows", "record",
        "breakout", "approved", "wins", "win")
BAD = ("crash", "plunge", "slump", "bearish", "downgrade", "hack", "hacked", "exploit", "lawsuit", "ban",
       "banned", "fraud", "bankrupt", "bankruptcy", "selloff", "sell-off", "fear", "outflow", "outflows",
       "miss", "misses", "layoff", "layoffs", "investigation", "halt", "halted", "liquidat", "stolen",
       "collapse", "collapsed", "fine", "fined", "probe")
ALIASES = (
    ("bitcoin", "BTC"), ("ethereum", "ETH"), ("ether", "ETH"), ("solana", "SOL"), ("ripple", "XRP"),
    ("dogecoin", "DOGE"), ("cardano", "ADA"), ("avalanche", "AVAX"), ("chainlink", "LINK"),
    ("nvidia", "NVDA"), ("apple", "AAPL"), ("tesla", "TSLA"), ("microsoft", "MSFT"), ("amazon", "AMZN"),
    ("google", "GOOGL"), ("alphabet", "GOOGL"), ("meta", "META"), ("coinbase", "COIN"),
    ("microstrategy", "MSTR"), ("strategy inc", "MSTR"),
)
_WORD = re.compile(r"[a-z0-9][a-z0-9-]{1,}")
_TICK = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z]{3,5})(?:\.[A-Z]{1,2})?(?![A-Za-z0-9])")


def news_path():
    raw = os.environ.get("NEWS_DB")
    return Path(raw) if raw else DATA / "news.db"


def _connect(path=None):
    path = Path(path or news_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS news(
        id TEXT PRIMARY KEY, ts TEXT, source TEXT, title TEXT, url TEXT, asset TEXT, score REAL)""")
    return con


def _local(tag):
    return tag.rsplit("}", 1)[-1] if tag else ""


def parse_feed(text):
    """[{title, url}] from an RSS or Atom document. Empty when the XML is unreadable."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title, link = "", ""
        for child in list(el):
            name = _local(child.tag)
            if name == "title" and child.text:
                title = " ".join(child.text.split())
            elif name == "link":
                href = child.attrib.get("href") or (child.text or "")
                if href and not link:
                    link = href.strip()
        if title:
            out.append(dict(title=title, url=link))
    return out


def lexicon_score(title):
    """-1 to 1 from the finance word lists. No match is 0, not a guess."""
    words = _WORD.findall((title or "").lower())
    good = bad = 0
    for i, word in enumerate(words):
        flip = i > 0 and words[i - 1] in ("not", "no", "never")
        hit_g = any(word.startswith(w) for w in GOOD)
        hit_b = any(word.startswith(w) for w in BAD)
        if hit_g:
            bad += 1 if flip else 0
            good += 0 if flip else 1
        if hit_b:
            good += 1 if flip else 0
            bad += 0 if flip else 1
    if good + bad == 0:
        return 0.0
    return max(-1.0, min(1.0, (good - bad) / (good + bad)))


def _vader(title):
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except Exception:
        return None
    if not hasattr(_vader, "tool"):
        _vader.tool = SentimentIntensityAnalyzer()
    return float(_vader.tool.polarity_scores(title or "")["compound"])


def score_title(title):
    """Lexicon, averaged with VADER only when that package is already installed."""
    base = lexicon_score(title)
    other = _vader(title)
    if other is None:
        return base
    return max(-1.0, min(1.0, 0.5 * base + 0.5 * other))


def tracked_symbols():
    """Coins the books already know, plus US and UK names from the stock universe. No Australian listings."""
    syms = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "LTC", "BCH", "DOT", "UNI", "AAVE",
            "NEAR", "ATOM", "ARB", "OP", "SUI", "TIA", "PEPE"}
    uni = ((cfg().get("stocks") or {}).get("universe") or {})
    for block in uni.values():
        for ticker in block.get("tickers") or []:
            sym = str(ticker).strip().upper()
            if sym and not sym.endswith(".AX"):
                syms.add(sym)
    for ticker in (cfg().get("stocks") or {}).get("watchlist") or []:
        sym = str(ticker).strip().upper()
        if sym and not sym.endswith(".AX"):
            syms.add(sym)
    return syms


def map_assets(title, universe=None):
    """Symbols this headline is about. A ticker counts only in capitals, so ordinary words do not match."""
    universe = universe or tracked_symbols()
    found = []
    low = (title or "").lower()
    for phrase, sym in ALIASES:
        if phrase in low and sym in universe and sym not in found:
            found.append(sym)
    for match in _TICK.findall(title or ""):
        sym = match.upper()
        if sym in STOP or sym not in universe or sym in found:
            continue
        found.append(sym)
    # London names are written NVDA.L in the universe and NVDA in a headline.
    extra = []
    for sym in found:
        london = sym + ".L"
        if london in universe and london not in found:
            extra.append(london)
    return found + extra


def _ident(url, title):
    key = (url or "").strip().lower() or " ".join((title or "").lower().split())
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def store_items(rows, path=None, now=None):
    """Insert new article-asset rows. Returns how many were new."""
    now = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    con = _connect(path)
    n = 0
    try:
        for row in rows:
            assets = row.get("assets") or [""]
            base = _ident(row.get("url"), row.get("title"))
            for asset in assets:
                ident = base + ":" + (asset or "-")
                cur = con.execute("INSERT OR IGNORE INTO news VALUES(?,?,?,?,?,?,?)",
                                  (ident, now, row.get("source") or "", row.get("title") or "",
                                   row.get("url") or "", asset or "", float(row.get("score") or 0)))
                n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        con.commit()
    finally:
        con.close()
    return n


def _fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def run(feeds=None, fetch=None, path=None, universe=None, pause=0.3):
    """One hourly pass. A dead feed is skipped. Nothing here is called from the trading cycle."""
    feeds = feeds or FEEDS
    fetch = fetch or _fetch
    universe = universe or tracked_symbols()
    rows = []
    errors = []
    for name, url in feeds:
        try:
            items = parse_feed(fetch(url))[:40]
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        for item in items:
            rows.append(dict(source=name, title=item["title"], url=item.get("url") or "",
                             score=score_title(item["title"]), assets=map_assets(item["title"], universe) or [""]))
        if pause:
            time.sleep(pause)
    added = store_items(rows, path=path)
    print(f"[news] {added} new rows from {len(rows)} headlines" + (f"; {'; '.join(errors)}" if errors else ""))
    return added


def latest(limit=12, path=None):
    path = Path(path or news_path())
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [dict(ts=ts, source=src, title=title, url=url, asset=asset, score=score)
                for ts, src, title, url, asset, score in con.execute(
                    "SELECT ts, source, title, url, asset, score FROM news ORDER BY ts DESC LIMIT ?", (int(limit),))]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def by_asset(path=None):
    """{symbol: [(day, score), ...]} mean score per UTC day. Empty when the file is missing."""
    path = Path(path or news_path())
    if not path.exists():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT ts, asset, score FROM news WHERE asset != '' AND score IS NOT NULL").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    bucket = {}
    for ts, asset, score in rows:
        day = str(ts)[:10]
        bucket.setdefault(asset, {}).setdefault(day, []).append(float(score))
    out = {}
    for asset, days in bucket.items():
        out[asset] = sorted((day, sum(vals) / len(vals)) for day, vals in days.items())
    return out


if __name__ == "__main__":
    raise SystemExit(0 if run() >= 0 else 1)
