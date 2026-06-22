import json, requests, time
from datetime import datetime
URL="https://api.hyperliquid.xyz/info"
S=requests.Session(); S.headers.update({"Content-Type":"application/json"})
def post(p):
    r=S.post(URL,data=json.dumps(p),timeout=30); r.raise_for_status(); return r.json()
A="0xd05808946809c180d190608e13f473db30aa8524"
# full history from t=0
now=int(time.time()*1000)
chunk=post({"type":"userFillsByTime","user":A,"startTime":0,"endTime":now})
print("first-page n (from t=0):", len(chunk))
if chunk:
    ts=[int(f["time"]) for f in chunk]
    print("oldest in page:", datetime.utcfromtimestamp(min(ts)/1000).strftime("%Y-%m-%d"),
          "newest:", datetime.utcfromtimestamp(max(ts)/1000).strftime("%Y-%m-%d"))
    bld=[f for f in chunk if ":" in (f.get("coin") or "") and not (f.get("coin") or "").startswith("@")]
    print("builder fills in first page:", len(bld))
    if bld:
        bts=[int(f["time"]) for f in bld]
        print("oldest builder fill in page:", datetime.utcfromtimestamp(min(bts)/1000).strftime("%Y-%m-%d"))
