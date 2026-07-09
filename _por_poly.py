import json, sys
sys.path.insert(0,".")
from src import polymarket as pm
import requests
H={"User-Agent":pm.USER_AGENT}
def g(u,**p):
    r=requests.get(u,params=p,headers=H,timeout=30)
    try:return r.json()
    except:return {}

print("=== search Poly events for portugal/spain by slug prefix guesses ===")
for slug in ["fifwc-por-esp-2026-07-06","fifwc-esp-por-2026-07-06",
             "fifwc-portugal-spain-2026-07-06","fifwc-spain-portugal-2026-07-06"]:
    j=g("https://gamma-api.polymarket.com/events", slug=slug)
    n=len(j) if isinstance(j,list) else 0
    print(f"  {slug:42} -> {n} events", (j[0].get('title') if n else ''))

print("=== broad: any open soccer event with portugal or spain in title ===")
sibs=g("https://gamma-api.polymarket.com/events", series_slug="soccer-fifwc", limit="2000", closed="false")
print("  total soccer-fifwc open events:", len(sibs) if isinstance(sibs,list) else 0)
for e in (sibs if isinstance(sibs,list) else []):
    t=(e.get("title","")+e.get("slug","")).lower()
    if "portugal" in t or "spain" in t or "-por-" in t or "-esp-" in t:
        print("   MATCH:", e.get("slug"), "|", e.get("title"), "|", len(e.get("markets",[])),"mk")

print("=== also try the gamma search endpoint ===")
for q in ["Portugal Spain"]:
    j=g("https://gamma-api.polymarket.com/public-search", q=q, limit_per_type="10")
    evs=(j.get("events") if isinstance(j,dict) else None) or []
    print(f"  search '{q}': {len(evs)} events")
    for e in evs[:6]:
        print("   ", e.get("slug"), "|", e.get("title"))
