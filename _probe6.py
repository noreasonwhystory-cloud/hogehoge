import json, requests
# Is target wallet on the MAIN leaderboard? (discovery source)
lb = json.load(open("data/leaderboard.json", encoding="utf-8"))
rows = lb if isinstance(lb,list) else lb.get("leaderboardRows") or []
A="0xd05808946809c180d190608e13f473db30aa8524"
found=False
for r in rows:
    if (r.get("ethAddress") or "").lower()==A:
        found=True
        for n,p in r.get("windowPerformances",[]):
            if n=="allTime": print("target on main leaderboard, allTime pnl=", p.get("pnl"))
        break
print("target on MAIN leaderboard:", found, " total LB rows:", len(rows))
