import json, requests, sys
sys.path.insert(0, ".")

t = json.load(open("data/genz/match_tree.json", encoding="utf-8"))
gid, g = next((k, v) for k, v in t["games"].items() if "ENGARG" in k)
toks = []
for n in g["nodes"]:
    if n.get("market_key") == "btts" and n.get("side") in ("yes", "no"):
        tok = next((n[k] for k in n if "token" in k.lower() and n[k]), None)
        if tok: toks.append((n["side"], str(tok)))

seen = set()
for side, tok in toks:
    if tok in seen: continue
    seen.add(tok)
    print("=" * 70); print("BTTS", side, "| token", tok[:16] + "...")
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"clob_token_ids": tok}, timeout=10).json()
        m = r[0] if isinstance(r, list) and r else (r or {})
        print(" GAMMA question:", m.get("question"))
        print(" GAMMA fee-ish fields:", {k: v for k, v in m.items() if "fee" in k.lower()} or "none")
        cid = m.get("conditionId") or m.get("condition_id")
        print(" conditionId:", cid)
        if cid:
            cm = requests.get("https://clob.polymarket.com/markets/" + cid, timeout=10).json()
            fees = {k: v for k, v in cm.items() if "fee" in k.lower()}
            print(" CLOB fee fields:", fees or "none")
            print(" CLOB misc:", {k: cm.get(k) for k in ("neg_risk", "minimum_tick_size", "is_50_50_outcome") if k in cm})
    except Exception as e:
        print(" lookup failed:", e)
