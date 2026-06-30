import sys, json, logging
sys.path.insert(0, ".")
from src import kalshi as ks
from src import polymarket as pm
log = logging.getLogger("p"); logging.basicConfig(level=logging.WARNING)
import requests
from collections import Counter
H={"User-Agent": pm.USER_AGENT}
def jget(url, **p):
    r=requests.get(url, params=p, headers=H, timeout=30)
    try: return r.json()
    except: return {}

GAME="26JUN30CIVNOR"; POLY_PREFIX="fifwc-civ-nor-2026-06-30"

# ---- KALSHI: every market for this game, with price ----
kal=[]
SERIES=["KXWCGAME","KXWCTOTAL","KXWCTEAMTOTAL","KXWCSPREAD","KXWC1HSPREAD","KXWC2HSPREAD",
        "KXWCBTTS","KXWC1HBTTS","KXWC2HBTTS","KXWCCORNERS","KXWCTCORNERS","KXWCSCORE",
        "KXWCGOAL","KXWCSOA","KXWC1H","KXWC2H","KXWCFTTS","KXWC1HTOTAL","KXWC2HTOTAL","KXWCADVANCE"]
B=ks.DEFAULT_BASE_URL
for s in SERIES:
    j=jget(B+"/markets", series_ticker=s, limit=1000)
    for m in j.get("markets",[]):
        if GAME not in m.get("ticker","").upper(): continue
        nb=m.get("no_bid_dollars"); ya=m.get("yes_ask_dollars") or (round(1-float(nb),3) if nb else None)
        kal.append({"type":s,"ticker":m.get("ticker"),
                    "title":m.get("yes_sub_title") or m.get("subtitle") or m.get("title"),
                    "yes_ask":ya})
json.dump(kal, open("_kalshi_game.json","w"), indent=1)

# ---- POLY: every sibling event's every market, with price ----
poly=[]
sibs=jget("https://gamma-api.polymarket.com/events", series_slug="soccer-fifwc", limit="2000", closed="false")
for e in (sibs if isinstance(sibs,list) else []):
    if not e.get("slug","").startswith(POLY_PREFIX): continue
    grp=e.get("title","").split(" - ")[-1]
    for m in e.get("markets",[]):
        op=m.get("outcomePrices"); oc=m.get("outcomes")
        try: prices=json.loads(op) if isinstance(op,str) else op
        except: prices=op
        poly.append({"group":grp,"slug":e.get("slug"),
                     "question":m.get("question") or m.get("groupItemTitle"),
                     "outcomes":oc,"prices":prices})
json.dump(poly, open("_poly_game.json","w"), indent=1)

print(f"KALSHI: {len(kal)} markets -> _kalshi_game.json")
print(f"POLY  : {len(poly)} markets -> _poly_game.json")
print("\nKALSHI types:", dict(Counter(k['type'] for k in kal)))
print("POLY groups :", dict(Counter(p['group'] for p in poly)))
