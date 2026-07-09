import json, sys
sys.path.insert(0,".")
from src import kalshi as ks, polymarket as pm
import requests
H={"User-Agent":pm.USER_AGENT}
def g(u,**p):
    r=requests.get(u,params=p,headers=H,timeout=30)
    try:return r.json()
    except:return {}

print("=== KALSHI: does the PORESP event exist? ===")
B=ks.DEFAULT_BASE_URL
for s in ["KXWCCORNERS","KXWCGAME"]:
    j=g(B+"/markets", series_ticker=s, limit=1000)
    hits=[m for m in j.get("markets",[]) if "PORESP" in m.get("ticker","").upper() or ("POR" in m.get("ticker","").upper() and "ESP" in m.get("ticker","").upper())]
    print(f"  {s}: {len(hits)} PORESP markets", [m["ticker"] for m in hits[:3]])

print("=== POLY: does the Portugal-Spain sibling event exist? ===")
sibs=g("https://gamma-api.polymarket.com/events", series_slug="soccer-fifwc", limit="2000", closed="false")
por=[e for e in (sibs if isinstance(sibs,list) else []) if "por" in e.get("slug","").lower() and "esp" in e.get("slug","").lower()]
print(f"  {len(por)} Portugal-Spain sibling events:")
for e in por[:12]:
    print("   ", e.get("slug"), "|", len(e.get("markets",[])), "markets")
