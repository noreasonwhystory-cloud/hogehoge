import json, os, glob, sys
from datetime import datetime, timezone

R=json.load(open('data/wallet_registry.json',encoding='utf-8'))
W=R['wallets']
reg=set(a.lower() for a in W.keys())

MAJORS={'BTC','ETH','SOL'}
cutoff_ms = int(datetime(2026,6,1,tzinfo=timezone.utc).timestamp()*1000)

files=glob.glob('data/fills/*.json')

rows=[]
for i,fp in enumerate(files):
    addr=os.path.basename(fp)[:-5].lower()
    try:
        d=json.load(open(fp,encoding='utf-8'))
    except Exception:
        continue
    fills=d.get('fills',[])
    maj=0.0; allp=0.0; last=0
    for f in fills:
        t=f.get('time',0)
        if t>last: last=t
        cp=f.get('closedPnl','0')
        try: cpf=float(cp or 0)
        except: cpf=0.0
        allp+=cpf
        if f.get('coin') in MAJORS:
            maj+=cpf
    rows.append((addr,maj,allp,last,addr in reg))

out=open('_verify_result.txt','w',encoding='utf-8')
def p(*a):
    s=' '.join(str(x) for x in a)
    out.write(s+'\n'); print(s)

p('fill files:', len(files))
cand=[r for r in rows if (not r[4]) and r[1]>500000 and r[3]>=cutoff_ms]
cand.sort(key=lambda r:-r[1])
p('=== non-registry majors>500k & last>=2026-06-01 ===')
p('count:', len(cand))
p('count >1M:', len([r for r in cand if r[1]>1_000_000]))
p('count >500k any-last (non-reg):', len([r for r in rows if (not r[4]) and r[1]>500000]))
p('--- top 10 ---')
for a,m,ap,l,ir in cand[:10]:
    dt=datetime.fromtimestamp(l/1000,tz=timezone.utc).strftime('%Y-%m-%d')
    p(f'{a} maj=${m:,.0f} all=${ap:,.0f} last={dt} in_reg={ir}')

# verify the 3 specific addresses cited
p('--- cited addresses ---')
cited=['0x71dfc07de32c2ebf1c4801f4b1c9e40b76d4a23d',
       '0x0ddf9bae2af4b874b96d287a5ad42eb47138a902',
       '0xb798aef79972ce8f73d47b9ebbcda6bbb7ec4fbf']
rowmap={r[0]:r for r in rows}
for c in cited:
    r=rowmap.get(c)
    if r:
        dt=datetime.fromtimestamp(r[3]/1000,tz=timezone.utc).strftime('%Y-%m-%d')
        p(f'{c} maj=${r[1]:,.0f} all=${r[2]:,.0f} last={dt} in_reg={r[4]}')
    else:
        p(f'{c} NOT FOUND in fills')
out.close()
print('WROTE _verify_result.txt')
