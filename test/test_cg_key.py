"""CoinGecko demo key: header only when COINGECKO_API_KEY is set, only on api.coingecko.com, never logged."""
import io, logging, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("COINGECKO_API_KEY", None)
import collector

os.environ.pop("COINGECKO_API_KEY", None)
h = collector.cg_headers({"Accept": "application/json"})
assert "x-cg-demo-api-key" not in h and h["Accept"] == "application/json", "unset key adds no header"
assert "x-cg-demo-api-key" not in collector.S.headers, "the session default must not carry the key"

os.environ["COINGECKO_API_KEY"] = "  demo-secret-key  "
h = collector.cg_headers()
assert h.get("x-cg-demo-api-key") == "demo-secret-key", "set key is stripped and sent"
os.environ["COINGECKO_API_KEY"] = "   "
assert "x-cg-demo-api-key" not in collector.cg_headers(), "blank key is the same as unset"

seen = {}
class R:
    status_code = 200
    headers = {}
    def json(self): return {"bitcoin": {"usd": 1}}
    def raise_for_status(self): pass

def fake_get(url, params=None, headers=None, timeout=20):
    seen["url"] = url; seen["headers"] = dict(headers or {}); return R()

os.environ["COINGECKO_API_KEY"] = "demo-secret-key"
collector.S.get = fake_get
assert collector.get(collector.CG + "/simple/price", {"ids": "bitcoin"}, tries=1)["bitcoin"]["usd"] == 1
assert seen["url"].startswith("https://api.coingecko.com/api/v3")
assert seen["headers"].get("x-cg-demo-api-key") == "demo-secret-key", "CoinGecko call sends the demo header"
assert collector.CG == "https://api.coingecko.com/api/v3"

seen.clear()
assert collector.get("https://aggregator-api.kyberswap.com/monad/api/v1/routes", headers={"x-client-id": "paper-trader"}, tries=1)
assert "x-cg-demo-api-key" not in seen["headers"] and seen["headers"].get("x-client-id") == "paper-trader", "other hosts do not get the key"

buf = io.StringIO()
handler = logging.StreamHandler(buf); handler.setLevel(logging.DEBUG)
collector.L.addHandler(handler); collector.L.setLevel(logging.DEBUG)
def boom(*a, **k):
    raise RuntimeError("network down")
collector.S.get = boom
assert collector.get(collector.CG + "/ping", tries=1) is None
text = buf.getvalue()
assert "demo-secret-key" not in text and "x-cg-demo-api-key" not in text, "a failed call does not log the key"
collector.L.removeHandler(handler)
os.environ.pop("COINGECKO_API_KEY", None)
print("[test_cg_key] OK")
