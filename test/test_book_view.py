"""Per-book dashboard files. Display only: a variant export is read-only and gets its own JSON."""
import json, os, sqlite3, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
os.environ["TRADING_DB"] = str(TMP / "main.db")
os.environ["TRADING_LOG"] = str(TMP / "t.log")
os.environ["TRADING_OFFLINE"] = "1"
os.environ["SITE_DIR"] = str(TMP / "site")
os.environ["VARIANTS_DIR"] = str(TMP / "variants")
os.environ["VARIANTS_MAIN_DB"] = str(TMP / "main.db")
os.environ.pop("TRADING_VARIANT", None)

import dashboard

fails = []
def check(cond, msg):
    print(("OK  " if cond else "FAIL") + " " + msg)
    if not cond: fails.append(msg)

check(dashboard.book_menu_label("all_combined") == "Both", "the menu calls all_combined Both")
check(dashboard.book_menu_label("burst_runner") == "Burst runner", "the menu names the burst runner")
check(dashboard.book_menu_label("ev_exit") == "EV exit", "the menu names EV exit")
check(dashboard.book_menu_label("review_leftovers") == "Review leftovers", "the menu names review leftovers")
check(dashboard.book_menu_label("main") == "Main (frozen)", "the menu names the frozen main book")
check(dashboard.book_file("main") == "data.json" and dashboard.book_file("ev_exit") == "data_ev_exit.json",
      "main stays data.json and a variant is data_<name>.json")
for bad in ("../evil", "EV", "data.json", "a/b", ""):
    try:
        dashboard.book_file(bad)
        check(False, f"{bad!r} must not be a file name")
    except ValueError:
        check(True, f"{bad!r} is rejected")

root = Path(os.environ["VARIANTS_DIR"])
root.mkdir()
names = ["ev_exit", "review_leftovers", "all_combined", "burst_runner"]
before = {}
for name in names:
    p = root / f"{name}.db"
    sqlite3.connect(p).close()
    before[name] = p.read_bytes()

site = Path(os.environ["SITE_DIR"])
site.mkdir()
(site / "data_retired.json").write_text("{}", encoding="utf-8")
(site / "notes.txt").write_text("keep", encoding="utf-8")

seen = []
def fake_build(con):
    path = con.execute("PRAGMA database_list").fetchone()[2] or ""
    if path.endswith("review_leftovers.db"):
        raise RuntimeError("boom")
    writable = True
    try:
        con.execute("CREATE TABLE book_view_probe(x)")
        con.execute("DROP TABLE book_view_probe")
    except sqlite3.OperationalError:
        writable = False
    seen.append((Path(path).name, writable))
    return {"generated": "t", "variants": {"enabled": True, "books": [
        {"name": "main", "label": "Main book"},
        {"name": "ev_exit", "label": "EV exit"},
        {"name": "all_combined", "label": "Both together"},
        {"name": "burst_runner", "label": "Burst runner"},
        {"name": "no_file", "label": "Not published"},
    ]}}

dashboard.build = fake_build
dashboard.main()

for name, raw in before.items():
    check((root / f"{name}.db").read_bytes() == raw, f"{name}.db bytes unchanged")
check(seen and seen[0][1] is True and all(w is False for _, w in seen[1:]),
      "only the main connection is writable")
check(not any(n == "review_leftovers.db" for n, _ in seen), "a failed export is not stamped")

published = ["data.json", "data_ev_exit.json", "data_all_combined.json", "data_burst_runner.json"]
check(not (site / "data_review_leftovers.json").exists(), "a failed book is not published")
check(not (site / "data_retired.json").exists(), "a retired data_*.json is deleted")
check((site / "notes.txt").read_text() == "keep", "unrelated files in the site dir stay")

payloads = {fn: json.loads((site / fn).read_text()) for fn in published}
catalog = payloads["data.json"]["books"]
check([c["name"] for c in catalog] == ["main", "ev_exit", "all_combined", "burst_runner"],
      "the catalog is main plus the books that exported")
check(catalog[0]["label"] == "Main (frozen)" and catalog[0]["variant"] is False and catalog[0]["file"] == "data.json",
      "main is marked as the frozen book")
check(next(c for c in catalog if c["name"] == "all_combined")["label"] == "Both", "catalog label for all_combined is Both")
check(next(c for c in catalog if c["name"] == "burst_runner")["label"] == "Burst runner", "catalog label names Burst runner")
check(all(p["books"] == catalog for p in payloads.values()), "every file carries the same catalog")
for fn, name in (("data.json", "main"), ("data_ev_exit.json", "ev_exit"), ("data_burst_runner.json", "burst_runner")):
    b = payloads[fn]["book"]
    check(b["name"] == name and b["variant"] is (name != "main") and b["label"] == dashboard.book_menu_label(name),
          f"{fn} names itself {name}")
check(any(b["name"] == "no_file" for b in payloads["data.json"]["variants"]["books"]),
      "the comparison list can name a book that has no file yet")
check(all(c["name"] != "no_file" for c in catalog), "the published catalog skips a book with no file")

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
