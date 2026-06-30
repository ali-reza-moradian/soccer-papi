import json
t = json.load(open("data/genz/match_tree.json"))["games"]["26JUN30CIVNOR"]
from collections import Counter
n = t["nodes"]
print("NODES:", len(n))
print("types:", dict(Counter(x["market_type"] for x in n)))
print("unmatched:", len(t.get("unmatched", [])))
print()
print("=== CORNERS (Kalshi 9 must pair Poly Over @ line 8.5, not 9.5) ===")
for x in n:
    if x["market_type"] in ("corners","team_corners"):
        print(f'  {x["side"]:8} line={x.get("line")} K={x["kalshi_ticker"].split("-")[-1]:8} polyside={x["poly_side"]}')
print()
print("=== EXACT SCORE (Kalshi CIV2NOR1 must pair Poly CIV 2-1, not NOR 2-1) ===")
for x in n:
    if x["market_type"]=="exact_score":
        print(f'  label={x.get("outcome_label","")[:34]:34} K={x["kalshi_ticker"].split("-")[-1]:10} polyside={x["poly_side"]}')
print()
print("=== SPREAD (Kalshi 'wins by >1.5' must pair Poly (-1.5) same team) ===")
for x in n:
    if x["market_type"]=="spread":
        print(f'  {x["side"]:8} line={x.get("line")} K={x["kalshi_ticker"].split("-")[-1]:8} polyside={x["poly_side"]}')
print()
print("=== TOTALS sanity (Kalshi Over 2.5 -> Poly Over line 2.5) ===")
for x in n:
    if x["market_type"]=="total_goals":
        print(f'  {x["side"]:8} line={x.get("line")} K={x["kalshi_ticker"].split("-")[-1]:6} polyside={x["poly_side"]}')
