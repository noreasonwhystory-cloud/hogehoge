"""HL同期取引スキャン: 複数ウォレットが同一急変イベントの直前に揃って先行建玉したかを検出。

クレジット不要(HLのみ)。対象ウォレットの majors 約定を取り、共通の急変イベント(4h足±3%)に対し
各ウォレットの大口先行建玉(同方向・6h前以内)を集計。同一イベントに≥2ウォレット＝協調候補。
使い方: python sync_scan.py  （既定: 監視側=インサイダー/弱い/出金/候補）
"""
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

import config
import hl_client

MS_H = 3600 * 1000
LEAD = config.LEAD_WINDOW_H * MS_H
LARGE = config.LARGE_TRADE_USD
WATCH = {"インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
         "Nansen候補(HL未検証)"}


def open_dir(d):
    d = (d or "").strip()
    if ">" in d:
        return d.split(">")[-1].strip().lower()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()
    return None


def build_events(t_lo, t_hi):
    """対象期間の BTC/ETH/SOL 4h足から急変イベント(±EVENT_MOVE_PCT%/4h)を作る。"""
    events = []
    for coin in config.COINS:
        c = hl_client.candles(coin, "4h", t_lo, t_hi)
        s = sorted([(int(x["t"]), float(x["c"])) for x in (c or [])])
        for i in range(len(s) - 1):
            p0, p1 = s[i][1], s[i + 1][1]
            if p0 > 0 and abs((p1 - p0) / p0 * 100) >= config.EVENT_MOVE_PCT:
                events.append({"coin": coin, "t0": s[i][0],
                               "dir": "long" if p1 > p0 else "short",
                               "pct": round((p1 - p0) / p0 * 100, 2)})
    return events


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    targets = [(k, e) for k, e in reg.items() if e.get("position") in WATCH]
    print(f"同期スキャン対象: {len(targets)} 件（監視側）")

    now = int(time.time() * 1000)
    lo = now - 560 * 24 * MS_H        # 約18ヶ月
    events = build_events(lo, now)
    print(f"急変イベント: {len(events)} 件")
    ev_by = defaultdict(list)
    for e in events:
        ev_by[(e["coin"], e["dir"])].append(e)

    # 各ウォレットの大口先行建玉を、各イベントに割り当て
    hits = defaultdict(list)   # (coin,t0,dir) -> [(wallet,pos,entry_ms,notional)]
    for k, ent in targets:
        try:
            fills = hl_client.user_fills_by_time(ent["address"], lo, now)
        except Exception:
            continue
        for f in fills:
            if f.get("coin") not in config.COINS:
                continue
            d = open_dir(f.get("dir"))
            if d not in ("long", "short"):
                continue
            notl = float(f["px"]) * float(f["sz"])
            if notl < LARGE:
                continue
            t = int(f["time"])
            for ev in ev_by.get((f["coin"], d), []):
                if ev["t0"] - LEAD <= t <= ev["t0"]:
                    hits[(ev["coin"], ev["t0"], d)].append((k[:10], ent["position"], t, round(notl)))
                    break

    print("=== 同一イベントに複数ウォレットが先行（協調候補）===")
    found = 0
    for key, lst in sorted(hits.items(), key=lambda x: -len({w for w, _, _, _ in x[1]})):
        wallets = {w for w, _, _, _ in lst}
        if len(wallets) >= 2:
            found += 1
            coin, t0, d = key
            et = datetime.fromtimestamp(t0 / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"  {coin} {d} 急変@{et} ← {len(wallets)}ウォレット:")
            for w, p, t, n in sorted(lst, key=lambda z: z[2]):
                ts = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%m-%d %H:%M")
                print(f"      {w} [{p[:14]}] entry {ts} ${n:,}")
    if not found:
        print("  （同一イベントに複数ウォレットの先行: なし）")
    json.dump({k2: v for k2, v in (((f"{a}|{b}|{c}", lst) for (a, b, c), lst in hits.items()))},
              open(f"{config.DATA_DIR}/sync_hits.json", "w", encoding="utf-8"), ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
