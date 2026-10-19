"""v7.1 consistency check of the dashboard export (docs/data.json): every total shown on the site must equal the sum of its
parts, with the same definitions everywhere (Overview / Portfolio / account cards / pot tabs / Day tab sub-strategies).
Usage: python3 test/check_data.py [path-or-URL-to-data.json]   (default docs/data.json). Exit 1 + list of failures if any.
Tolerances: EXACT = half a penny (figures are built from the rounded parts, so they must match to the penny);
ROUND = 1.5p for identities that mix independently rounded numbers (realised + unrealised vs value - start)."""
import json, sys, urllib.request
from pathlib import Path
EXACT, ROUND = 0.005, 0.015
fails, n = [], [0]

def load(src):
    if src.startswith("http"):
        with urllib.request.urlopen(src, timeout=30) as r: return json.load(r)
    return json.loads(Path(src).read_text())

def eq(a, b, what, tol=EXACT):
    n[0] += 1; a = a or 0; b = b or 0
    if abs(a - b) > tol + 1e-9: fails.append(f"{what}: {a!r} != {b!r} (diff {a - b:+.4f})")

def main(D):
    ACC, POTS = ("crypto", "stocks"), [k for k in ("core", "day", "risk", "trend") if k in D["sleeves"]]
    Z, A, S, P = D["sleeves"], D.get("accounts") or {}, D["stats"], D.get("positions") or []
    for p in P:
        if p.get("value") is not None: eq(p["pnl"], p["value"] - p["cost"], f"position {p['sleeve']}/{p['asset']} P&L = value - cost")
    for k in POTS:
        z = Z[k]
        for ac in ACC:
            c = z["accts"][ac]; ps = [p for p in P if p["sleeve"] == k and p["acct"] == ac]; w = f"{k}/{ac}"
            eq(c["equity"], c["cash"] + c["invested"] + c.get("lent", 0) - c.get("borrowed", 0), f"{w} value = cash + invested + lent - borrowed")
            eq(c["invested"], sum(p["value"] or 0 for p in ps), f"{w} invested = sum of its positions")
            eq(c["unrealized"], sum(p["pnl"] or 0 for p in ps), f"{w} unrealised = sum of its positions' P&L")
            eq(c["open_n"], len(ps), f"{w} open count")
            eq(c["pnl"], c["equity"] - c["start"], f"{w} P&L = value - start")
            eq(c["pnl"], (c["realized"] or 0) + (c["unrealized"] or 0), f"{w} P&L = realised + unrealised", ROUND)
        for f in ("equity", "cash", "invested", "start", "pnl", "unrealized", "realized", "open_n", "fees"):
            if f in z: eq(z[f], sum(z["accts"][ac][f] or 0 for ac in ACC), f"{k} {f} = crypto + stocks")
        eq(z["equity"], z["cash"] + z["invested"] + z.get("lent", 0) - z.get("borrowed", 0), f"{k} value = cash + invested + lent - borrowed")
    for ac, a in A.items():
        for f, g in (("equity", "equity"), ("cash", "cash"), ("invested", "invested"), ("start", "start"), ("pnl", "pnl"), ("unrealized", "unrealized"), ("fees_total", "fees")):
            eq(a[f], sum(Z[k]["accts"][ac][g] or 0 for k in POTS), f"account {ac} {f} = sum of its pot cells")
        eq(a["open_n"], sum(1 for p in P if p["acct"] == ac), f"account {ac} open count")
    for f in ("equity", "cash", "invested", "pnl", "unrealized", "realized"):
        if f in S: eq(S[f], sum(Z[k][f] or 0 for k in POTS), f"total {f} = sum of pots")
    if A: eq(S["equity"], sum(a["equity"] for a in A.values()), "total value = sum of accounts")
    eq(S["equity"], S["cash"] + S["invested"], "total value = cash + invested (loans between pots cancel out)")
    def split_ok(bd, headline, what):
        n[0] += 1
        if not isinstance(bd, dict) or "open" not in bd or "closed" not in bd:
            fails.append(f"{what}: missing open/closed breakdown"); return
        if "fees" in bd: fails.append(f"{what}: fees are already inside open and closed, not a third component")
        eq((bd.get("open") or 0) + (bd.get("closed") or 0), headline, what)
    if S.get("pnl") is not None: split_ok(S.get("pnl_breakdown"), S["pnl"], "total P&L = open positions + closed trades")
    if S.get("day_pnl") is not None: split_ok(S.get("day_pnl_breakdown"), S["day_pnl"], "today's P&L = open positions + closed trades")
    for k in POTS:
        split_ok(Z[k].get("pnl_breakdown"), Z[k]["pnl"], f"{k} P&L = open positions + closed trades")
        for ac in ACC: split_ok(Z[k]["accts"][ac].get("pnl_breakdown"), Z[k]["accts"][ac]["pnl"], f"{k}/{ac} P&L = open positions + closed trades")
    for ac, a in A.items(): split_ok(a.get("pnl_breakdown"), a["pnl"], f"account {ac} P&L = open positions + closed trades")
    for ac in ACC:   # v7.3 loans: what pots lent = what pots borrowed inside each account; = the outstanding loans list
        lt_, bw_ = sum(Z[k]["accts"][ac].get("lent", 0) for k in POTS), sum(Z[k]["accts"][ac].get("borrowed", 0) for k in POTS)
        eq(lt_, bw_, f"{ac} loans: lent = borrowed")
        eq(bw_, sum(l["out"] for l in D.get("loans") or [] if l["status"] == "open" and l["account"] == ac), f"{ac} loans = outstanding loans list", 0.011)
        cap_ = float((D.get("loan_rules") or {}).get("max_outstanding_pct", 20)) / 100 * (D.get("accounts") or {}).get(ac, {}).get("start", 0)
        n[0] += 1
        if bw_ > cap_ + 0.011: fails.append(f"{ac} loans outstanding {bw_} > {cap_:.2f} (max_outstanding_pct of the account start)")
    for l in D.get("loans") or []:
        if l["status"] == "open" and l["lender"] == l["borrower"]: fails.append(f"loan {l['id']} lender = borrower")
    eq(sum(p["value"] or 0 for p in P), S["invested"], "Overview invested = sum of all positions")
    if S.get("day_start"): eq(S["day_pnl"], S["equity"] - S["day_start"], "Overview today's P&L = value - midnight value")
    if "start" in S: eq(S["pnl"], S["equity"] - S["start"], "total P&L = value - start")
    # ---- Day tab ----
    d = Z.get("day")
    if d:
        if d.get("day_start"): eq(d["day_pnl"], d["equity"] - d["day_start"], "Day today's P&L = value - midnight value")
        if d.get("day_pnl") is not None: split_ok(d.get("day_pnl_breakdown"), d["day_pnl"], "Day today's P&L = open positions + closed trades")
        L = d.get("ledger") or {}
        if not L: fails.append("sleeves.day.ledger missing")
        subs = [s for s in ("momentum", "burst") if s in (L.get("all") or {})]
        for ac in ACC:
            if ac not in L: continue
            c, x = d["accts"][ac], L[ac]
            eq(sum(x[s]["budget"] for s in subs), c["equity"], f"day/{ac} momentum + burst budget = Day value")
            eq(sum(x[s]["cash"] for s in subs), c["cash"], f"day/{ac} momentum + burst cash = Day cash")
            eq(sum(x[s]["free"] for s in subs), c["cash"], f"day/{ac} momentum + burst free = Day cash (no negative sub cash)")
            eq(sum(x[s]["value"] for s in subs), c["invested"], f"day/{ac} momentum + burst positions = Day invested")
            eq(sum(x[s].get("owed", 0) for s in subs), c.get("borrowed", 0), f"day/{ac} sub loans owed = Day borrowed")
            eq(sum(x[s].get("lent", 0) for s in subs), c.get("lent", 0), f"day/{ac} sub loans lent = Day lent")
            eq(sum(x[s]["start"] for s in subs), c["start"], f"day/{ac} sub starts = Day start")
            eq(sum(x[s]["open_n"] for s in subs), c["open_n"], f"day/{ac} sub open counts")
            eq(abs(x.get("residual") or 0), 0, f"day/{ac} ledger residual", 0.01)
            for s in subs:
                y = x[s]; ps = [p for p in P if p["sleeve"] == "day" and p["acct"] == ac and p.get("sub", "momentum") == s]; w = f"day/{ac}/{s}"
                eq(y["budget"], y["cash"] + y["value"] + y.get("lent", 0) - y.get("owed", 0), f"{w} budget = cash + positions + lent - owed")
                eq(y["value"], sum(p["value"] or 0 for p in ps), f"{w} positions value = sum of its positions")
                eq(y["open_pnl"], sum(p["pnl"] or 0 for p in ps), f"{w} open P&L = sum of its positions' P&L")
                eq(y["pnl"], y["budget"] - y["start"], f"{w} P&L = budget - start")
                eq(y["pnl"], y["realized"] + y["open_pnl"], f"{w} P&L = closed + open", ROUND)
                if y["free"] > y["cash"] + EXACT: fails.append(f"{w} free {y['free']} > cash {y['cash']}")
                st = (d.get("subs") or {}).get(s, {}).get(f"all:{ac}") or {}
                if st: eq(st.get("pnl"), y["realized"], f"{w} closed P&L (table) = ledger realised", 0.006)
        for s in subs:
            for f in ("budget", "cash", "free", "value", "start", "open_n", "pnl", "open_pnl", "realized", "owed", "lent"):
                eq(L["all"][s].get(f, 0), sum(L[ac][s].get(f, 0) for ac in ACC if ac in L), f"day/all/{s} {f} = crypto + stocks")
        ST = d.get("subs") or {}
        for s, m in ST.items():
            for key, v in m.items():
                if "fees_open" in v: eq(v["fees"], v["fees_closed"] + v["fees_open"], f"day sub {s} {key} fees = closed + open buys", 0.0002)
            for per in ("all", "today"):
                if f"{per}:all" in m:
                    for f in ("trades", "pnl", "fees"):
                        eq(m[f"{per}:all"][f], sum(m[f"{per}:{ac}"][f] or 0 for ac in ACC), f"day sub {s} {per} {f} = crypto + stocks", 0.0002)
        if ST: eq(sum(ST[s]["all:all"]["fees"] for s in ST), d.get("fees", sum(d["accts"][ac]["fees"] for ac in ACC)), "day sub fees = Day pot fees", 0.01)
    # ---- Risk tab (v7.4 Day-style) ----
    r = Z.get("risk")
    if r and "stats" in r:
        R = r.get("rules") or {}; RS = r["stats"]
        for key, v in RS.items():
            eq(v["fees"], v["fees_closed"] + v["fees_open"], f"risk {key} fees = closed + open buys", 0.0002)
        for per in ("all", "today"):
            for f in ("trades", "pnl", "fees"):
                eq(RS[f"{per}:all"][f], sum(RS[f"{per}:{ac}"][f] or 0 for ac in ACC), f"risk {per} {f} = crypto + stocks", 0.0002)
        eq(RS["all:all"]["fees"], r.get("fees", sum(r["accts"][ac]["fees"] for ac in ACC)), "risk stats fees = Risk pot fees", 0.01)
        eq(RS["all:all"]["pnl"], r["realized"], "risk closed P&L (table) = Risk realised", 0.011)
        eq(r["unrealized"], sum(p["pnl"] or 0 for p in P if p["sleeve"] == "risk"), "risk open P&L = sum of its positions' P&L")
        b0, b1, cap = R.get("conf_threshold"), R.get("conf_at_stop"), R.get("cap") or 30
        thr = r.get("threshold")
        if isinstance(thr, (int, float)) and b0 is not None:
            frac = min(1.0, max(0.0, -(r.get("dd_pct") or 0) / cap))
            eq(thr, min(0.99, b0 + (b1 - b0) * frac), "risk threshold = base + (top - base) x drawdown / stop", 0.002)
        for x in r.get("cands") or []:
            w = f"risk cand {x.get('a')}"
            if "conf" not in x: continue
            n[0] += 1
            if x.get("setup") not in (None, "", "breakout", "dip"): fails.append(f"{w} setup {x.get('setup')!r} is not breakout or dip")
            if not (0 <= x["conf"] <= 1) or any(not (0 <= v <= 1) for v in (x.get("parts") or {}).values()): fails.append(f"{w} confidence/components outside 0-1")
            Wt = (R.get("dip_weights") if x.get("setup") == "dip" else R.get("weights")) or {}
            if Wt and x.get("parts"):
                core = {k: v for k, v in Wt.items() if k != "freshness"}
                if core and all(k in x["parts"] for k in core):
                    base = sum(core[k] * x["parts"][k] for k in core) / sum(core.values())
                    wf = float(Wt.get("freshness") or 0); Wsum = sum(core.values()); fr = x["parts"].get("freshness", 1)
                    expect = base if wf <= 0 else base * (Wsum + wf * fr) / (Wsum + wf)
                    eq(x["conf"], expect, f"{w} confidence = components, scaled by data freshness", 0.006)
            if x.get("bought"):
                n[0] += 1
                if isinstance(thr, (int, float)) and x["conf"] < thr - 1e-9: fails.append(f"{w} bought below the threshold ({x['conf']} < {thr})")
                if x.get("ev") is None or x["ev"] <= (R.get("ev_min") or 0): fails.append(f"{w} bought with EV {x.get('ev')} <= {R.get('ev_min')}")
                if x["bought"] < (R.get("min_trade") or 5) - 0.01 or x["bought"] > (R.get("max_pos") or 8) + 0.01: fails.append(f"{w} size £{x['bought']} outside min trade .. max position")
                if x.get("block"): fails.append(f"{w} bought but blocked: {x['block']}")
            elif not x.get("block"): fails.append(f"{w} neither bought nor blocked")
        for p in P:
            if p["sleeve"] == "risk" and p.get("conf") is not None and not (0 <= p["conf"] <= 1): fails.append(f"risk position {p['asset']} confidence {p['conf']}")
    def ev_now_of(p, now, tgt, stop, cost):
        if not all(isinstance(v, (int, float)) for v in (p, now, tgt, stop)) or now <= 0: return None
        p = max(0.0, min(1.0, float(p)))
        up = max(0.0, (float(tgt) / float(now) - 1) * 100)
        dn = max(0.0, (1 - float(stop) / float(now)) * 100)
        return round(p * up - (1 - p) * dn - float(cost or 0), 3)
    for p in P:
        if p.get("sleeve") in ("day", "risk") and isinstance(p.get("ev_now"), (int, float)) and p.get("now") and p.get("ev_tgt") and p.get("ev_stop"):
            eq(p["ev_now"], ev_now_of(p.get("ev_p") if p.get("ev_p") is not None else 0.5, p["now"], p["ev_tgt"], p["ev_stop"], p.get("ev_cost")),
               f"{p['sleeve']} {p['asset']} EV now", 0.011)
    for t in D.get("trades") or []:
        if t.get("side") in ("BUY", "SELL") and t.get("sleeve") in ("day", "risk") and isinstance(t.get("ev_now"), (int, float)) and t.get("ev_px") and t.get("ev_tgt") and t.get("ev_stop"):
            eq(t["ev_now"], ev_now_of(t.get("ev_p") if t.get("ev_p") is not None else 0.5, t["ev_px"], t["ev_tgt"], t["ev_stop"], t.get("ev_cost")),
               f"order {t.get('id')} {t.get('asset')} EV now", 0.011)
    return fails

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "docs" / "data.json")
    try: main(load(src))
    except (KeyError, TypeError) as e: fails.append(f"data.json is missing a reconciliation field: {e!r} (old export?)")
    print(f"[check_data] {src}: {n[0]} reconciliation checks, {len(fails)} failed")
    for f in fails: print("  FAIL", f)
    sys.exit(1 if fails else 0)
