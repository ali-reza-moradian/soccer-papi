import sys, json, logging
sys.path.insert(0, ".")
from src import polymarket as pm
from src import bookmath as bm
log = logging.getLogger("p"); logging.basicConfig(level=logging.WARNING)

STAKE = 10000.0
WANT  = "germany"

names = json.load(open("data/cache/names.json", encoding="utf-8"))
by_fixture = names.get("by_fixture", names)
client = pm.PolymarketClient()
events = pm.fetch_wc_events(client, by_fixture, log, depth_pricing=True, slippage_pct=2.0)
def legs(e): return getattr(e,"legs",None) or getattr(e,"outcomes",None) or []
def key(e): return getattr(e,"slug","") or getattr(e,"title","") or ""
print(f"{len(events)} events built:", [key(e) for e in events])

for e in events:
    if not any(WANT in (getattr(l,"team","") or "").lower() for l in legs(e)): continue
    print("="*64); print("MATCH:", key(e), f"  | stake ${STAKE:,.0f}")
    for leg in legs(e):
        t=getattr(leg,"team","?"); dec=getattr(leg,"decimal",None); lim=getattr(leg,"limit",None); tok=getattr(leg,"yes_token","")
        print(f"  {t}:")
        if dec:
            print(f"     BOT decimal odds = {dec:.4f}  (implied price {1/dec:.4f} = {100/dec:.1f}c)")
            print(f"     BOT says: ${STAKE:,.0f} stake -> ${STAKE*dec:,.2f} TOTAL return  (profit ${STAKE*dec-STAKE:,.2f})")
            print(f"     BOT fillable limit on this leg = ${(lim or 0):,.0f}" + ("   !! limit < stake, bot would cap the stake" if (lim or 0) < STAKE else ""))
        else:
            print("     BOT decimal = None")
        bk=client.book(tok); asks=bk.get("asks") if isinstance(bk,dict) else None
        L=sorted(((float(a["price"]),float(a["size"])) for a in (asks or [])),key=lambda x:x[0])
        if L:
            w=bm.walk_book(L,STAKE); ap=w["avg_price"] if isinstance(w,dict) else getattr(w,"avg_price",None)
            fu=w.get("filled_usd") if isinstance(w,dict) else getattr(w,"filled_usd",None)
            if ap:
                print(f"     REAL book: avg fill {ap:.4f} ({100*ap:.1f}c) = {1/ap:.4f} odds -> ${STAKE*(1/ap):,.2f} TOTAL return")
            if fu is not None and fu < STAKE-0.5:
                print(f"     REAL book: only ${fu:,.0f} of ${STAKE:,.0f} actually fills")
        else:
            print("     REAL book: EMPTY")
