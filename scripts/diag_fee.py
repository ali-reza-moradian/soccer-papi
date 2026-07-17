import json, sys, requests
sys.path.insert(0, ".")
from src.executor.resolve import MarketData

t = json.load(open("data/genz/match_tree.json", encoding="utf-8"))
gid, g = next((k, v) for k, v in t["games"].items() if "ENGARG" in k)
md = MarketData()

def tok_of(n):
    return next((n[k] for k in n if "token" in k.lower() and n[k]), None)

picks = []
for n in g["nodes"]:
    mk, side = n.get("market_key", ""), n.get("side", "")
    if mk == "btts" and side in ("yes", "no"):
        picks.append((f"BTTS {side}", tok_of(n)))
    if mk.startswith("total_goals|0.5") and side == "over":
        picks.append(("Over 0.5 goals", tok_of(n)))

def dollar_walk(ladder, budget):
    sh = spent = 0.0
    for p, s in ladder:
        cost = p * s
        take = min(cost, budget - spent)
        if take <= 0: break
        sh += take / p; spent += take
    return sh, (spent / sh if sh else 0)

for label, tok in picks:
    if not tok: continue
    lad = md.poly_ask_ladder(tok)
    print("=" * 60)
    print(label, "| token", str(tok)[:14] + "...", "| top:", lad[:2])
    for amt in (100, 1000, 3000):
        sh, avg = dollar_walk(lad, amt)
        print(f"  ${amt:>5} -> NO-FEE to-win ${sh:,.2f}  (avg {avg*100:.2f}c)")
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"clob_token_ids": tok}, timeout=10).json()
        m = r[0] if isinstance(r, list) and r else r
        fees = {k: v for k, v in (m or {}).items() if "fee" in k.lower()}
        print("  gamma fee fields:", fees or "none exposed")
    except Exception as e:
        print("  gamma lookup failed:", e)
