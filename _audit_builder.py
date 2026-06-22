import json, glob, os
from collections import Counter

files = glob.glob("data/fills/*.json")
builder_coins = Counter()
distinct_builder_coins = Counter()
wb = set()
dom = 0
tot = 0
for fn in files:
    try:
        o = json.load(open(fn, encoding="utf-8"))
    except Exception:
        continue
    fills = o.get("fills", [])
    if not fills:
        continue
    tot += 1
    addr = os.path.basename(fn)[:-5]
    hb = False
    rb = 0.0
    ra = 0.0
    for f in fills:
        coin = f.get("coin") or ""
        cp = float(f.get("closedPnl", 0) or 0)
        ra += cp
        if ":" in coin:
            builder_coins[coin.split(":")[0] + ":*"] += 1
            distinct_builder_coins[coin] += 1
            hb = True
            rb += cp
    if hb:
        wb.add(addr)
        if abs(rb) > abs(ra) * 0.5 and abs(rb) > 10000:
            dom += 1

print("total_wallets", tot)
print("wallets_with_builder", len(wb))
print("wallets_builder_dominant_pnl(>50%,>10k)", dom)
print("builder_dex_prefixes", dict(builder_coins))
print("distinct_builder_coins", len(distinct_builder_coins))
print("top_builder_coins", distinct_builder_coins.most_common(15))
