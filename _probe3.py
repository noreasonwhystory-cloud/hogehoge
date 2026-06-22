import json, os, requests
from collections import Counter
A = "0xd05808946809c180d190608e13f473db30aa8524"
URL = "https://api.hyperliquid.xyz/info"
S = requests.Session(); S.headers.update({"Content-Type":"application/json"})
def post(p):
    r = S.post(URL, data=json.dumps(p), timeout=30); r.raise_for_status(); return r.json()

# cached fills?
p = f"data/fills/{A.lower()}.json"
print("cached fills file exists:", os.path.exists(p))
if os.path.exists(p):
    o = json.load(open(p, encoding="utf-8"))
    fl = o.get("fills",[])
    pref = Counter()
    for f in fl: 
        c=f.get("coin",""); pref[c.split(":")[0]+":*" if ":" in c else c]+=1
    print("cached n:", len(fl), "breakdown:", dict(pref))

# registry entry?
reg = json.load(open("data/wallet_registry.json", encoding="utf-8"))
print("in registry:", A.lower() in reg["wallets"])

# funding history (userFunding all dex?)
import time
now=int(time.time()*1000); start=now-30*86400*1000
fund = post({"type":"userFunding","user":A,"startTime":start,"endTime":now})
print("userFunding n(30d):", len(fund))
fpref=Counter()
for f in fund:
    c=f.get("delta",{}).get("coin","")
    fpref[c.split(":")[0]+":*" if ":" in c else c]+=1
print("funding coin breakdown:", dict(fpref))

# open orders / TWAP (frontendOpenOrders main + per dex)
for dx in [None,"xyz"]:
    pp={"type":"frontendOpenOrders","user":A}
    if dx: pp["dex"]=dx
    oo=post(pp)
    print(f"frontendOpenOrders dex={dx!r}: n={len(oo)}", [ (o.get('coin'),o.get('orderType'),o.get('sz')) for o in oo[:5]])

# subaccounts
try:
    sub = post({"type":"subAccounts","user":A})
    print("subAccounts:", sub if not sub else len(sub))
except Exception as e:
    print("subAccounts err", e)
