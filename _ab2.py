import json, glob, os
from collections import Counter
# cross builder-fills vs registry classification + majors metric
reg = json.load(open("data/wallet_registry.json", encoding="utf-8"))["wallets"]
files = glob.glob("data/fills/*.json")
buf = {}  # addr -> builder realized
for fn in files:
    try: o=json.load(open(fn,encoding="utf-8"))
    except: continue
    addr=os.path.basename(fn)[:-5]
    rb=ra=0.0; nbf=0
    for f in o.get("fills",[]):
        coin=f.get("coin") or ""; cp=float(f.get("closedPnl",0) or 0); ra+=cp
        if ":" in coin and not coin.startswith("@"): rb+=cp; nbf+=1
    buf[addr]={"rb":rb,"ra":ra,"nbf":nbf}

pos_ct=Counter(); builder_pos=Counter(); insider_susp_builder=0
maj_zero_but_builder=0
for a,w in reg.items():
    b=buf.get(a)
    if not b or b["nbf"]==0: continue
    pos=w.get("position","?"); builder_pos[pos]+=1
    if w.get("metric_category")=="insider_suspect": insider_susp_builder+=1
    if (w.get("true_realized_maj") or 0)==0 and abs(b["rb"])>5000:
        maj_zero_but_builder+=1
print("registry wallets w/ builder fills, by position:", dict(builder_pos))
print("insider_suspect w/ builder fills:", insider_susp_builder)
print("majors_realized==0 but |builder realized|>5k:", maj_zero_but_builder)
print("registry total wallets:", len(reg))
