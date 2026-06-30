import sys, json, logging
sys.path.insert(0, ".")
from src import polymarket as pm
log = logging.getLogger("p"); logging.basicConfig(level=logging.WARNING)
import requests
H={"User-Agent": pm.USER_AGENT}
def g(url, **p):
    r=requests.get(url, params=p, headers=H, timeout=30)
    try: return r.status_code, r.json()
    except: return r.status_code, r.text[:150]

base = "fifwc-civ-nor-2026-06-30"

# 1) Brute the obvious sibling slug suffixes
print("### sibling slug probe ###")
suffixes = ["","-total","-totals","-over-under","-ou","-btts","-both-teams-to-score","-spread",
            "-handicap","-correct-score","-score","-exact-score","-corners","-total-corners",
            "-1h","-first-half","-2h","-halftime","-team-total","-civ-total","-nor-total",
            "-first-team-to-score","-anytime-goalscorer","-goalscorer","-shots","-assists"]
found=[]
for sfx in suffixes:
    slug=base+sfx
    sc,j = g("https://gamma-api.polymarket.com/events", slug=slug)
    n = len(j) if isinstance(j,list) else 0
    if n:
        mk=j[0].get("markets",[])
        print(f"  {slug:48} -> {len(mk)} markets")
        found.append(slug)

# 2) The real way: list ALL events whose slug STARTS WITH the game prefix
print("\n### all events with slug prefix (the proper discovery) ###")
# gamma supports ?slug= exact; try the events list and filter by ticker/slug prefix
for endpoint,params in [
    ("https://gamma-api.polymarket.com/events", {"limit":"1000","closed":"false","tag_slug":"soccer"}),
    ("https://gamma-api.polymarket.com/events", {"limit":"1000","closed":"false","series_slug":"fifa-world-cup"}),
]:
    sc,j = g(endpoint, **params)
    if isinstance(j,list):
        hits=[e for e in j if (e.get("slug","")).startswith("fifwc-civ-nor")]
        print(f"  {endpoint} {params} -> {len(j)} events, {len(hits)} civ-nor:")
        for e in hits: print("     ", e.get("slug"), "|", len(e.get("markets",[])), "markets |", e.get("title"))

# 3) Inspect the moneyline event for a 'series'/'group' link that points to siblings
print("\n### does the moneyline event reference a series/group? ###")
sc,j = g("https://gamma-api.polymarket.com/events", slug=base)
if isinstance(j,list) and j:
    ev=j[0]
    for k in ["series","seriesSlug","groupSlug","series_id","tags","ticker","negRiskMarketID","parentEvent"]:
        if k in ev: print(f"  {k}:", json.dumps(ev[k])[:200])
    print("  all top-level keys:", list(ev.keys()))
