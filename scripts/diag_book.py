import json, sys
sys.path.insert(0, ".")
from src.executor.resolve import MarketData
import src.bookmath as bm

t = json.load(open("data/genz/match_tree.json", encoding="utf-8"))
gid, g = next((k, v) for k, v in t["games"].items() if "ENGARG" in k)
print("game:", gid, "| tree nodes:", len(g["nodes"]))
md = MarketData()
for n in [x for x in g["nodes"] if x.get("market_key") == "btts"]:
    print("=" * 72)
    ident = {k: n[k] for k in n if any(s in k.lower() for s in ("ticker", "token", "side", "label", "question", "title")) and n[k]}
    print("NODE:", json.dumps(ident))
    kt, ks = n.get("kalshi_ticker"), (n.get("kalshi_side") or "YES")
    tok = next((n[k] for k in n if "token" in k.lower() and n[k]), None)
    if kt:
        raw = md._kalshi().orderbook(kt)
        ob = raw.get("orderbook_fp") or raw.get("orderbook") or raw
        print(" KALSHI RAW keys:", list(raw)[:4], "| yes head:", (ob.get("yes_dollars") or ob.get("yes") or [])[:3], "| no head:", (ob.get("no_dollars") or ob.get("no") or [])[:3])
        lad = md.kalshi_ask_ladder(kt, ks)
        w = bm.walk_book(lad, 5900)
        print(" KALSHI parsed top5:", lad[:5], "| levels:", len(lad))
        print("   walk 5900 sh -> avg %.4f  filled %.0f  cost $%.2f" % (w.avg_price, w.filled, w.cost))
    if tok:
        raw = md._poly().book(tok)
        print(" POLY RAW asks head:", (raw or {}).get("asks", [])[:3], "| bids head:", (raw or {}).get("bids", [])[:3])
        lad = md.poly_ask_ladder(tok)
        w = bm.walk_book(lad, 5900)
        print(" POLY parsed top5:", lad[:5], "| levels:", len(lad))
        print("   walk 5900 sh -> avg %.4f  filled %.0f  cost $%.2f" % (w.avg_price, w.filled, w.cost))
