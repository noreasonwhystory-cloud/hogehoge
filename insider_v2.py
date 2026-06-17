"""個人インサイダー検出 v2: 「イベント往復(動く前に建て→動いた後に利確)」の反復回数で評価。

workflowが暴いた弱点を克服:
 - 分割約定はイベント単位に集約（N=1水増しを排除）
 - 「往復」を要求＝トレンド便乗(持ち続ける)を自動排除
 - 別々の急変イベントでの反復回数を数える＝偶然1発を排除
出力: data/insider_v2.json
使い方: python insider_v2.py
"""
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

import config
import hl_client

MS_H = 3600 * 1000
LEAD = config.LEAD_WINDOW_H * MS_H        # 動く前 6h
EXIT = 12 * MS_H                          # 動いた後 12h以内に利確
LARGE = config.LARGE_TRADE_USD
# 除外/MM以外で稼ぎのある層を対象
TARGET_POS = {"インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
              "プロトレーダー(本物)", "Nansen候補(HL未検証)"}


def open_dir(d):
    d = (d or "").strip()
    if ">" in d:
        return d.split(">")[-1].strip().lower()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()
    return None


def build_events(lo, hi):
    evs = []
    for coin in config.COINS:
        c = hl_client.candles(coin, "4h", lo, hi)
        s = sorted([(int(x["t"]), float(x["c"])) for x in (c or [])])
        for i in range(len(s) - 1):
            p0, p1 = s[i][1], s[i + 1][1]
            if p0 > 0 and abs((p1 - p0) / p0 * 100) >= config.EVENT_MOVE_PCT:
                evs.append({"coin": coin, "t0": s[i][0],
                            "dir": "long" if p1 > p0 else "short",
                            "pct": round((p1 - p0) / p0 * 100, 2)})
    return evs


def analyze(addr, events):
    fills = hl_client.user_fills_by_time(addr, 0, int(time.time() * 1000))
    maj = [f for f in fills if f.get("coin") in config.COINS]
    if not maj:
        return None
    # 銘柄別に時系列のopen/closeを用意
    opens = defaultdict(list)   # (coin,dir) -> [t...]  大口の新規
    closes = defaultdict(list)  # coin -> [(t, side_reduce_dir)]
    for f in maj:
        t = int(f["time"]); coin = f["coin"]; px = float(f["px"]); sz = float(f["sz"])
        d = open_dir(f.get("dir"))
        dd = (f.get("dir") or "")
        if d in ("long", "short") and px * sz >= LARGE:
            opens[(coin, d)].append(t)
        if "Close" in dd or "Reduce" in dd or ">" in dd:
            # クローズ/減玉。その時点で減らした方向（=元の建玉方向）を記録
            cd = "long" if "Long" in dd else ("short" if "Short" in dd else None)
            if cd:
                closes[coin].append((t, cd))
    rt_events = 0          # 往復(前に建て+後に利確)が成立した distinct イベント数
    lead_events = 0        # 前に建てただけ(利確未確認)
    rt_detail = []
    for ev in events:
        coin, t0, d = ev["coin"], ev["t0"], ev["dir"]
        # 動く前6h以内に同方向で大口建て
        led = any(t0 - LEAD <= t <= t0 for t in opens.get((coin, d), []))
        if not led:
            continue
        # 動いた後12h以内に同方向建玉を利確(減らした)
        exited = any(t0 <= t <= t0 + EXIT and cd == d for t, cd in closes.get(coin, []))
        if exited:
            rt_events += 1
            if len(rt_detail) < 8:
                rt_detail.append({"coin": coin, "dir": d, "pct": ev["pct"],
                                  "event": datetime.fromtimestamp(t0 / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")})
        else:
            lead_events += 1
    return {"address": addr, "majors": len(maj),
            "rt_events": rt_events, "lead_only_events": lead_events,
            "rt_detail": rt_detail}


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    targets = [e["address"] for e in reg.values()
               if e.get("position") in TARGET_POS and "MM/HFT" not in e.get("tags", [])]
    print(f"個人インサイダー v2 検出対象: {len(targets)} 件")
    now = int(time.time() * 1000)
    events = build_events(now - 560 * 24 * MS_H, now)
    print(f"急変イベント: {len(events)} 件")

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, events)
        except Exception:
            r = None
        if r:
            out.append(r)
        if i % 25 == 0:
            print(f"  {i}/{len(targets)} ...")
    out.sort(key=lambda r: (r["rt_events"], r["lead_only_events"]), reverse=True)
    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/insider_v2.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("=== 往復(round-trip)反復 上位 ===")
    for r in out[:20]:
        if r["rt_events"] >= 1:
            print(f"  往復{r['rt_events']}回 先行のみ{r['lead_only_events']}回 majors{r['majors']} {r['address'][:12]}..")
    strong = [r for r in out if r["rt_events"] >= 3]
    print(f"\n本命(往復≥3回): {len(strong)} 件")


if __name__ == "__main__":
    main()
