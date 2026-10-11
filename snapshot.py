"""Render docs/snapshot.png (the Overview tab) with headless Chrome/Chromium if available; otherwise skip.
The page loads data.json via fetch(), which browsers block on file:// - so serve dashboard/ from a throwaway
loopback HTTP server (random port, read-only, no auth needed: lives only for this run) and give Chrome a
virtual-time budget long enough for data.json + the CDN chart library to load and the charts to draw."""
import shutil, subprocess, threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from common import SITE

exe = next((shutil.which(b) for b in ("chromium", "chromium-browser", "google-chrome") if shutil.which(b)), None)
if not exe:
    print("[snapshot] no headless browser found, skipped"); raise SystemExit(0)
DOC = SITE                      # docs/ (synced from the repo by pull_state.sh)
out = DOC / "snapshot.png"
tmp = DOC / ".snapshot.tmp.png"

class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def list_directory(self, path): self.send_error(404); return None

srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(DOC)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/index.html#overview"   # Overview tab
try:
    tmp.unlink(missing_ok=True)
    subprocess.run([exe, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--window-size=1400,1650",
                    "--force-device-scale-factor=1", "--run-all-compositor-stages-before-draw",
                    "--virtual-time-budget=15000", f"--screenshot={tmp}", url],
                   capture_output=True, timeout=120)
finally:
    srv.shutdown()
if tmp.exists() and tmp.stat().st_size > 10000:
    tmp.replace(out); print("[snapshot]", out)
else:
    tmp.unlink(missing_ok=True); print("[snapshot] failed (previous snapshot kept)")
