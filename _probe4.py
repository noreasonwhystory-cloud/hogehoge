import json, requests, time
URL="https://api.hyperliquid.xyz/info"
S=requests.Session(); S.headers.update({"Content-Type":"application/json"})
def post(p):
    r=S.post(URL,data=json.dumps(p),timeout=30); r.raise_for_status(); return r.json()
now=int(time.time()*1000); start=now-7*86400*1000
for coin in ["BTC","xyz:SPCX","xyz:CL","SPCX","CL"]:
    try:
        c=post({"type":"candleSnapshot","req":{"coin":coin,"interval":"1h","startTime":start,"endTime":now}})
        print(f"candle {coin!r}: n={len(c) if c else 0}")
    except Exception as e:
        print(f"candle {coin!r}: ERR {e}")
# does alt_scan use config.COINS gate or all coins? check
import subprocess
