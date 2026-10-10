"""Load AI-written sentiment JSON files from data/sentiment/ into SQLite (schema in README)."""
import json
from datetime import datetime
from common import SENT_DIR, db, iso, log

L = log("sentiment")

def valid(d):
    datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
    assert isinstance(d["asset"], str) and d["asset"]
    assert -1 <= float(d["score"]) <= 1 and 0 <= float(d["confidence"]) <= 1
    int(d.get("volume", 0)); assert isinstance(d.get("sources", []), list)

def main():
    con, n, bad = db(), 0, 0
    for sub in ("processed", "rejected"): (SENT_DIR / sub).mkdir(parents=True, exist_ok=True)
    for f in sorted(SENT_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            for d in data if isinstance(data, list) else [data]:
                valid(d)
                ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).isoformat()
                con.execute("INSERT OR REPLACE INTO sentiment VALUES(?,?,?,?,?,?,?,?,?)",
                            (ts, d["asset"].upper(), float(d["score"]), int(d.get("volume", 0)),
                             float(d["confidence"]), str(d.get("summary", ""))[:2000],
                             json.dumps(d.get("sources", [])), f.name, iso()))
                n += 1
            con.commit(); f.rename(SENT_DIR / "processed" / f.name)
        except Exception as e:
            bad += 1; L.error("rejected %s: %s", f.name, e); f.rename(SENT_DIR / "rejected" / f.name)
    print(f"[sentiment] ingested={n} rejected_files={bad}")

if __name__ == "__main__":
    main()
