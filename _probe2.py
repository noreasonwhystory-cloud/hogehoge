import json, requests
from collections import Counter
A = "0xd05808946809c180d190608e13f473db30aa8524"
URL = "https://api.hyperliquid.xyz/info"
S = requests.Session(); S.headers.update({"Content-Type":"application/json"})
def post(p):
    r = S.post(URL, data=json.dumps(p), timeout=30); r.raise_for_status(); return r.json()

dexs = post({"type":"perpDexs"})
names = [None] + [d.get("name") for d in dexs if d]

print("=== ALL PERP DEX NAMES ===", names)
print()
print("=== clearinghouseState per DEX ===")
for dx in names:
    p = {"type":"clearinghouseState","user":A}
    if dx: p["dex"] = dx
    try:
        st = post(p)
    except Exception as e:
        print(f"  dex={dx!r}: ERROR {e}"); continue
    aps = st.get("assetPositions", [])
    acct = st.get("marginSummary", {}).get("accountValue")
    poss = []
    for ap in aps:
        pos = ap.get("position", {})
        poss.append((pos.get("coin"), pos.get("szi"), pos.get("positionValue"), pos.get("unrealizedPnl")))
    print(f"  dex={dx!r}: accountValue={acct} positions={poss}")

print()
print("=== userFills (recent, all dex) coin prefixes ===")
fills = post({"type":"userFills","user":A})
print("n recent fills:", len(fills))
pref = Counter()
for f in fills:
    c = f.get("coin","")
    pref[c.split(":")[0]+":*" if ":" in c else c] += 1
print("coin breakdown:", dict(pref))
