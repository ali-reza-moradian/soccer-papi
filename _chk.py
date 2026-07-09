import json
t=json.load(open("data/genz/match_tree.json"))["games"].get("26JUL06PORESP",{})
print("PORESP nodes:", len(t.get("nodes",[])))
