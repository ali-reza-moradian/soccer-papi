import sys, json, logging
sys.path.insert(0, ".")
from src import polymarket as pm
log = logging.getLogger("p"); logging.basicConfig(level=logging.WARNING)
import requests
H={"User-Agent": pm.USER_AGENT}

def g(url, **p):
    r=requests.get(url, params=p, headers=H, timeout=30)
    try: return r.status_code, r.json()
    except: return r.status_code, r.text[:200]

# The match page slug is fifwc-civ-nor-2026-06-30. Try to pull that EVENT and see ALL its markets.
print("### try gamma events?slug=fifwc-civ-nor-2026-06-30 ###")
for slug in ["fifwc-civ-nor-2026-06-30","fifwc-civ-nor-2026-06-30-moneyline"]:
    sc,j = g("https://gamma-api.polymarket.com/events", slug=slug)
    print(f"  slug={slug} status={sc} ->", len(j) if isinstance(j,list) else type(j))
    if isinstance(j,list) and j:
        ev=j[0]; mk=ev.get("markets",[])
        print("   EVENT:", ev.get("title"), "| markets:", len(mk))
        for m in mk[:40]:
            print("     -", (m.get("question") or m.get("groupItemTitle") or m.get("slug") or "")[:60], "|", m.get("outcomes"))
        break

# Also try the events endpoint filtered by a slug prefix / search
print("\n### gamma /events?slug_contains or search for civ-nor ###")
sc,j = g("https://gamma-api.polymarket.com/events", limit=1000, tag_slug="soccer")
if isinstance(j,list):
    hits=[e for e in j if "civ" in (e.get("slug","")+e.get("title","")).lower() and "nor" in (e.get("slug","")+e.get("title","")).lower()]
    print("  soccer-tag events:", len(j), "| civ-nor hits:", len(hits))
    for e in hits[:5]:
        print("   ", e.get("slug"), "| markets:", len(e.get("markets",[])))

# And inspect how the scanner's own client pulls a single event by slug
print("\n### client.events_by_slug('fifwc-civ-nor-2026-06-30') ###")
client = pm.PolymarketClient()
try:
    ev = client.events_by_slug("fifwc-civ-nor-2026-06-30")
    print("  type:", type(ev).__name__)
    print("  json head:", json.dumps(ev)[:600])
except Exception as e:
    print("  err:", e)
