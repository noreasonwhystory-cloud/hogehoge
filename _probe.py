import json, requests
A = "0xd05808946809c180d190608e13f473db30aa8524"
URL = "https://api.hyperliquid.xyz/info"
S = requests.Session()
S.headers.update({"Content-Type":"application/json"})
def post(p):
    r = S.post(URL, data=json.dumps(p), timeout=30)
    r.raise_for_status()
    return r.json()

# 1) perpDexs metadata
dexs = post({"type":"perpDexs"})
print("=== perpDexs meta ===")
print(json.dumps(dexs, indent=2)[:2000])
names = []
for d in dexs:
    if d is None:
        names.append(None)
    else:
        names.append(d.get("name"))
print("DEX NAMES:", names)
