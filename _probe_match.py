import sys, json, logging
sys.path.insert(0, ".")
from src import kalshi as ks
from src import polymarket as pm
log = logging.getLogger("p"); logging.basicConfig(level=logging.WARNING)
import requests
B = ks.DEFAULT_BASE_URL
sess = requests.Session()
def kget(path, **p):
    r = sess.get(B+path, params=p, timeout=30)
    try: return r.json()
    except: return {}

# ---- 1) Pull ALL Kalshi markets for CIV-NOR, grouped by series (market type) ----
GAME = "26JUN30CIVNOR"
SERIES = ["KXWCGAME","KXWCTOTAL","KXWCTEAMTOTAL","KXWCSPREAD","KXWC1HSPREAD","KXWC2HSPREAD",
          "KXWCBTTS","KXWC1HBTTS","KXWC2HBTTS","KXWCCORNERS","KXWCTCORNERS","KXWCSCORE",
          "KXWCGOAL","KXWCSOA","KXWC1H","KXWCFTTS","KXWC1HTOTAL","KXWC2HTOTAL"]
kalshi_by_type = {}
for s in SERIES:
    j = kget("/markets", series_ticker=s, limit=1000)
    rows=[]
    for m in j.get("markets",[]):
        if GAME not in m.get("ticker","").upper(): continue
        # price via *_dollars
        nb = m.get("no_bid_dollars"); 
        ya = m.get("yes_ask_dollars") or (round(1-float(nb),2) if nb else None)
        rows.append((m.get("yes_sub_title") or m.get("subtitle") or m.get("ticker"), ya))
    if rows: kalshi_by_type[s]=rows
print("### KALSHI market types for this game ###")
for s,rows in kalshi_by_type.items():
    print(f"  {s:16} {len(rows):3} outcomes   e.g. {rows[0][0][:40]} @ {rows[0][1]}")
print("  TOTAL Kalshi outcomes:", sum(len(r) for r in kalshi_by_type.values()))

# ---- 2) Pull Poly legs for the same game ----
names = json.load(open("data/cache/names.json", encoding="utf-8"))
by_fixture = names.get("by_fixture", names)
client = pm.PolymarketClient()
events = pm.fetch_wc_events(client, by_fixture, log, depth_pricing=True, slippage_pct=2.0)
def legs(e): return getattr(e,"legs",None) or []
poly_ev = None
for e in events:
    ts=[(getattr(l,"team","") or "").lower() for l in legs(e)]
    if any("ivor" in t or "civ" in t for t in ts) or any("norw" in t for t in ts):
        poly_ev=e; break
print("\n### POLY legs for this game ###")
if poly_ev:
    for l in legs(poly_ev):
        print(f"  {getattr(l,'team','?'):16} decimal={getattr(l,'decimal',None)}")
else:
    print("  CIV-NOR not in Poly events this fetch:", [getattr(e,'slug','') for e in events])
print("\nNOTE: Poly here only returns moneyline (H/D/A). To match totals/BTTS/etc we need Poly's other markets too.")
