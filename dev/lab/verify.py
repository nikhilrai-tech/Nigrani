"""Checks every dashboard page and alert rule of the running lab against real exporter data.

Exit code 1 if a page is unavailable or a panel is empty. Rules never affect the exit code (the host/container
rules need node-exporter/cAdvisor and show NO DATA in this lab).
"""
import json, sys, urllib.request

import os
B = os.environ.get("NIGRANI_URL", "http://localhost:8091")
H = {"X-Requested-With": "nigrani", "Content-Type": "application/json"}

def get(path):
    return json.load(urllib.request.urlopen(urllib.request.Request(B + path, headers=H), timeout=60))

def post(path, body):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(), headers=H, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))

bad = 0
print("== PAGES")
for p in get("/api/pages"):
    page = get(f"/api/pages/{p['id']}?range=15m")
    if not page["available"]:
        print(f"  [{p['id']}] NOT AVAILABLE  (requires {p['requires']})"); bad += 1; continue
    print(f"  [{p['id']}] available")
    for pn in page["panels"]:
        n = len(pn["series"])
        flag = "ok " if n and not pn["error"] else "BAD"
        if flag == "BAD": bad += 1
        extra = pn["error"] or (", ".join(f"{s['name']}={next((v for _, v in reversed(s['points']) if v is not None), None)}" for s in pn["series"][:3]))
        print(f"     {flag} {pn['title']:<32} series={n}  {extra[:90]}")

print("== RULES (expression returns data right now?)")
for r in get("/api/rules"):
    res = post("/api/rules/test", {k: r[k] for k in ("name", "expr", "direction", "warn", "crit", "for_seconds", "unit")})
    flag = "ok " if res["count"] else "NO DATA"
    if r.get("error"): flag = "ERROR"
    print(f"  {flag:<8} {r['id']:<22} series={res['count']}  {r.get('error') or ''}")

sys.exit(1 if bad else 0)
