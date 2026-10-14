"""Load AI-written sentiment JSON files from data/sentiment/ into SQLite (schema in README).
v7.2: any asset is accepted - every CoinGecko symbol of the DAY crypto universe (state day_universe, ~100+ coins incl. trending
mid/small caps), the RISK screen, the stock universe, or a coin not priced yet (stored; used as soon as it is priced). The asset
may be given as the ticker (WLD, $WLD) or the CoinGecko id (worldcoin-wld) - ids are mapped to the ticker used here."""
import json
from datetime import datetime
from common import SENT_DIR, db, iso, log

L = log("sentiment")

def valid(d):
    datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
    assert isinstance(d["asset"], str) and d["asset"]
    assert -1 <= float(d["score"]) <= 1 and 0 <= float(d["confidence"]) <= 1
    int(d.get("volume", 0)); assert isinstance(d.get("sources", []), list)

def opt_float(d, key):
    """A number from an optional field, or None. A missing or non-numeric field does not reject the file."""
    if not isinstance(d, dict) or key not in d or d[key] is None or d[key] == "":
        return None
    try:
        return float(d[key])
    except (TypeError, ValueError):
        return None

def id_map(con):
    """CoinGecko id -> ticker (from the DAY universe + every coin ever priced, state cgid:SYM) and the known ticker set."""
    m, known = {}, set()
    for k, v in con.execute("SELECT key, value FROM state WHERE key LIKE 'cgid:%'"):
        sym = k[5:]; m[str(v).strip('"').lower()] = sym; known.add(sym)
    try:
        for sym, row in (json.loads((con.execute("SELECT value FROM state WHERE key='day_universe'").fetchone() or ["{}"])[0]).get("coins") or {}).items():
            m[str(row[0]).lower()] = sym; known.add(sym)
    except Exception: pass
    known |= {r[0] for r in con.execute("SELECT DISTINCT asset FROM prices")}
    return m, known

def norm_asset(a, m, known=()):
    a = a.strip().lstrip("$")
    return a.upper() if a.upper() in known or a.upper() == a else m.get(a.lower(), a.upper())   # tickers win; lower-case ids map

def main():
    con, n, bad = db(), 0, 0
    m, known = id_map(con); outside = set()
    for sub in ("processed", "rejected"): (SENT_DIR / sub).mkdir(parents=True, exist_ok=True)
    for f in sorted(SENT_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            for d in data if isinstance(data, list) else [data]:
                valid(d)
                ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).isoformat()
                d["asset"] = norm_asset(d["asset"], m, known)
                if d["asset"] not in known: outside.add(d["asset"])
                con.execute(
                    "INSERT OR REPLACE INTO sentiment"
                    "(ts,asset,score,volume,confidence,summary,sources,file,ingested_at,catalyst_score,buzz_change)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, d["asset"], float(d["score"]), int(d.get("volume", 0)),
                     float(d["confidence"]), str(d.get("summary", ""))[:2000],
                     json.dumps(d.get("sources", [])), f.name, iso(),
                     opt_float(d, "catalyst_score"), opt_float(d, "buzz_change")))
                n += 1
            con.commit(); f.rename(SENT_DIR / "processed" / f.name)
        except Exception as e:
            bad += 1; L.error("rejected %s: %s", f.name, e); f.rename(SENT_DIR / "rejected" / f.name)
    print(f"[sentiment] ingested={n} rejected_files={bad}" + (f" (not priced yet, kept for later: {', '.join(sorted(outside))})" if outside else ""))

if __name__ == "__main__":
    main()
