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

# Get the moneyline event to read its gameId + series
sc,j = g("https://gamma-api.polymarket.com/events", slug="fifwc-civ-nor-2026-06-30")
ev = j[0] if isinstance(j,list) and j else {}
gameId = ev.get("gameId"); series = ev.get("seriesSlug")
print("gameId:", gameId, "| series:", series)

# METHOD A: list all events in the series, filter to this game's prefix
print("\n### A) events in series soccer-fifwc with civ-nor prefix ###")
sc,j = g("https://gamma-api.polymarket.com/events", series_slug=series, limit="2000", closed="false")
sibs=[]
if isinstance(j,list):
    sibs=[e for e in j if e.get("slug","").startswith("fifwc-civ-nor-2026-06-30")]
    print(f"  series returned {len(j)} events, {len(sibs)} match this game:")
    for e in sibs:
        print(f"     {e.get('slug'):50} | {len(e.get('markets',[])):2} markets | {e.get('title','')[:30]}")

# METHOD B: if series filter empty, brute a fuller suffix list
if not sibs:
    print("\n### B) fuller suffix brute ###")
    sfx=["","-total","-totals","-total-goals","-btts","-spread","-asian-handicap","-handicap",
         "-exact-score","-correct-score","-total-corners","-corners","-team-total","-civ-total",
         "-nor-total","-first-half","-1h","-1h-total","-1h-btts","-1h-spread","-second-half","-2h",
         "-first-team-to-score","-anytime-goalscorer","-goalscorer","-to-score","-shots","-assists",
         "-double-chance","-draw-no-bet","-clean-sheet","-win-to-nil","-half-time-full-time"]
    for s in sfx:
        sc,jj = g("https://gamma-api.polymarket.com/events", slug="fifwc-civ-nor-2026-06-30"+s)
        n=len(jj) if isinstance(jj,list) else 0
        if n: print(f"     {'fifwc-civ-nor-2026-06-30'+s:50} | {len(jj[0].get('markets',[])):2} markets")

# Show that each sibling market has order-book token IDs (depth) — the thing OddsPapi can't give
print("\n### sample: does a totals/corners market carry CLOB tokens (depth)? ###")
sc,j = g("https://gamma-api.polymarket.com/events", slug="fifwc-civ-nor-2026-06-30-total-corners")
if isinstance(j,list) and j:
    m=j[0].get("markets",[])[0]
    print("  market:", m.get("question") or m.get("groupItemTitle"))
    print("  outcomes:", m.get("outcomes"), "prices:", m.get("outcomePrices"))
    print("  clobTokenIds:", m.get("clobTokenIds"))
