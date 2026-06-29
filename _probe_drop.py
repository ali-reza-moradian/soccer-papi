import sys, json, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("probe")
from src import polymarket as pm
names = json.load(open("data/cache/names.json", encoding="utf-8"))
by_fixture = names.get("by_fixture", names)

print("=== CACHE has", len(by_fixture), "fixtures ===")
for k,v in by_fixture.items():
    print(" ", k, "|", v.get("p1"), "vs", v.get("p2"), "| poly fields:",
          [kk for kk in v.keys() if "poly" in kk.lower() or "slug" in kk.lower() or "token" in kk.lower()])

client = pm.PolymarketClient()
events = pm.fetch_wc_events(client, by_fixture, log, depth_pricing=True, slippage_pct=2.0)
got = {getattr(e,"slug","") or getattr(e,"title","") for e in events}
print("\n=== fetch_wc_events BUILT", len(events), "events ===")
for e in events: print("  OK:", getattr(e,"slug",""))
print("\n=== MISSING (in cache, not built) ===")
for k,v in by_fixture.items():
    p1,p2 = v.get("p1",""), v.get("p2","")
    if not any((p1 or "x").split()[0].lower() in s.lower() or (p2 or "y").split()[0].lower() in s.lower() for s in got):
        print("  DROPPED:", p1, "vs", p2, "| full fixture record:")
        print("   ", json.dumps(v)[:600])
