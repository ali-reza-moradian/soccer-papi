import sys, json, logging
sys.path.insert(0, ".")
from src import polymarket as pm
log = logging.getLogger("p"); logging.basicConfig(level=logging.WARNING)
import requests
# Pull the WC events straight from gamma and show ALL markets per event (not just moneyline)
r = requests.get("https://gamma-api.polymarket.com/events",
                 params={"closed":"false","limit":"500","tag_slug":"world-cup"},
                 headers={"User-Agent": pm.USER_AGENT}, timeout=30)
events = r.json()
print(f"{len(events)} world-cup events from gamma\n")

# find a CIV-NOR style match event (has 'vs' and two teams, not a tournament market)
def is_match(ev):
    t=(ev.get("title") or "").lower()
    return " vs " in t or any(x in t for x in ["ivory","norway","mexico","ecuador","france","sweden"])
cands=[e for e in events if is_match(e)]
print("match-like events:", [e.get("title") for e in cands][:12], "\n")

ev = cands[0] if cands else events[0]
print("="*70); print("EVENT:", ev.get("title"), "| slug:", ev.get("slug"))
mkts = ev.get("markets",[])
print("markets in this event:", len(mkts))
for m in mkts:
    q = (m.get("question") or m.get("groupItemTitle") or m.get("slug") or "")[:55]
    # gamma markets carry outcomes + outcomePrices (JSON strings) + clobTokenIds
    op = m.get("outcomePrices"); oc = m.get("outcomes")
    print(f"  - {q:55} | outcomes={oc} prices={op}")
