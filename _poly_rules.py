import requests, json
H={"User-Agent":"Mozilla/5.0"}
j=requests.get("https://gamma-api.polymarket.com/events",
    params={"slug":"fifwc-prt-esp-2026-07-06-total-corners"}, headers=H, timeout=30).json()
ev=j[0] if isinstance(j,list) and j else {}
for m in ev.get("markets",[])[:3]:
    print("Q:", m.get("question"))
    print("DESC:", (m.get("description") or "")[:800])
    print("-"*60)
