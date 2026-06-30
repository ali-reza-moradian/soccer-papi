import sys, json, logging
sys.path.insert(0, ".")
from src import kalshi as ks
log = logging.getLogger("p"); logging.basicConfig(level=logging.WARNING)
import requests
client = ks.KalshiClient(); B = ks.DEFAULT_BASE_URL
sess = client.session if hasattr(client,"session") else requests.Session()
def get(path, **p):
    r = sess.get(B+path, params=p, timeout=30)
    try: return r.status_code, r.json()
    except: return r.status_code, r.text[:300]

def fnum(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None,"",): 
            try: return float(v)
            except: pass
    return None

def kalshi_price(tk):
    sc,j = get(f"/markets/{tk}")
    m = j.get("market",{}) if isinstance(j,dict) else {}
    # yes_ask = best price to BUY yes. Kalshi: yes_ask = 1 - no_bid ; yes_bid = 1 - no_ask
    no_bid = fnum(m,"no_bid_dollars"); no_ask = fnum(m,"no_ask_dollars")
    yes_ask = fnum(m,"yes_ask_dollars")
    if yes_ask is None and no_bid is not None: yes_ask = round(1.0-no_bid,2)
    yes_bid = fnum(m,"yes_bid_dollars")
    if yes_bid is None and no_ask is not None: yes_bid = round(1.0-no_ask,2)
    last = fnum(m,"last_price_dollars")
    # depth available to BUY yes at/near yes_ask = the no_dollars ladder size
    sc,ob = get(f"/markets/{tk}/orderbook")
    obj = ob.get("orderbook_fp",{}) if isinstance(ob,dict) else {}
    nod = obj.get("no_dollars") or []
    depth = sum(float(q) for _,q in nod[:5]) if nod else 0
    return yes_ask, yes_bid, last, depth

print("KALSHI moneyline — CIV vs NOR\n")
mp = {"Norway":"KXWCGAME-26JUN30CIVNOR-NOR","Tie":"KXWCGAME-26JUN30CIVNOR-TIE","Ivory Coast":"KXWCGAME-26JUN30CIVNOR-CIV"}
ssum=0
for name,tk in mp.items():
    ya,yb,last,depth = kalshi_price(tk)
    if ya: ssum += 1.0/(1.0/ya)  # implied prob = ya
    print(f"  {name:14} yes_ask={ya}  yes_bid={yb}  last={last}  decimal={1/ya if ya else None:.3f}  depth~${depth:,.0f}")
imp = sum(ya for ya,_,_,_ in [kalshi_price(tk) for tk in mp.values()] if ya)
print(f"\n  Kalshi implied-prob sum (yes_ask): {imp:.4f}  -> {'ARB <1!' if imp<1 else 'no arb, vig '+format((imp-1)*100,'.1f')+'%'}")
