import json
t = json.load(open("data/genz/match_tree.json"))["games"]["26JUN30CIVNOR"]
n = t["nodes"]
print("NODES:", len(n))
print()
print("=== EXACT SCORE — Kalshi label and Poly side must be the SAME scoreline ===")
for x in n:
    if x["market_type"]=="exact_score":
        print(f'  K_label={x.get("outcome_label",""):8} K_tk={x["kalshi_ticker"].split("-")[-1]:10} -> poly={x["poly_side"]}')
print()
print("=== SPREAD — same team both sides ===")
for x in n:
    if x["market_type"]=="spread":
        print(f'  side={x["side"]:12} line={x.get("line")} K={x["kalshi_ticker"].split("-")[-1]:8} -> poly={x["poly_side"]}')
