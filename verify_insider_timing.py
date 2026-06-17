"""イベント先行検証: 各ウォレットが『急変の直前に』建てたかを行動で測り insider/pro を確定。

各アドレスについて:
  1. majors約定を取得 → 活動期間 [t0,t1] を特定
  2. その期間の BTC/ETH/SOL 1h足を取得 → 急変イベント検出
  3. 各エントリの「4h後 方向的中率」と「急変直前(6h)の大口先行建玉(event_lead)」を算出
判定:
  insider-leaning … event_lead が大 or 的中率が極端高＋先行クラスタあり
  pro             … 勝率/的中は高いが event_lead ほぼ無し（実力・先読み無し）
出力: data/insider_verified.json
使い方: python verify_insider_timing.py [--src data/hl_classified.json] [--limit N]
"""
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import config
import hl_client
import step1_discover as s1

MS_H = 3600 * 1000


def analyze_wallet(addr):
    now = int(time.time() * 1000)
    fills = hl_client.user_fills_by_time(addr, 0, now)
    maj = [f for f in fills if f.get("coin") in config.COINS]
    if not maj:
        return {"address": addr, "majors": 0}
    t0 = min(int(f["time"]) for f in maj)
    t1 = max(int(f["time"]) for f in maj)
    # 活動期間の足を取得（銘柄ごと）
    price, events = {}, []
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", t0 - MS_H, t1 + config.HIT_HORIZON_H * MS_H)
        series = sorted([(int(x["t"]), float(x["c"])) for x in (c or [])])
        price[coin] = series
        w = config.EVENT_WINDOW_H
        for i in range(len(series) - w):
            p0 = series[i][1]; p1 = series[i + w][1]
            if p0 > 0 and abs((p1 - p0) / p0 * 100) >= config.EVENT_MOVE_PCT:
                events.append({"coin": coin, "t0": series[i][0],
                               "direction": "long" if p1 > p0 else "short",
                               "pct": round((p1 - p0) / p0 * 100, 2)})
    m = s1.analyze_fills(maj, price, events)
    # 判定
    el = m["event_lead_notional"]
    dir_acc = m["dir_accuracy"]
    win = m["win_rate"]
    if el >= config.LARGE_TRADE_USD and m["lead_examples"]:
        verdict = "insider-leaning(先行建玉あり)"
    elif dir_acc >= 0.85 and m["n_opens"] >= 20 and el > 0:
        verdict = "insider-leaning(高的中＋先行)"
    elif win >= 0.6 and m["n_closes"] >= 30 and el == 0:
        verdict = "pro(実力・先行なし)"
    else:
        verdict = "判定保留"
    return {"address": addr, "majors": len(maj),
            "active_from": datetime.fromtimestamp(t0/1000, timezone.utc).strftime("%Y-%m-%d"),
            "active_to": datetime.fromtimestamp(t1/1000, timezone.utc).strftime("%Y-%m-%d"),
            "n_events": len(events), "win_rate": win, "dir_accuracy": dir_acc,
            "event_lead_notional": el, "lead_examples": m["lead_examples"],
            "realized_pnl": m["realized_pnl"], "verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=f"{config.DATA_DIR}/hl_classified.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--addr", nargs="*", default=[])
    ap.add_argument("--position", default=None, help="台帳のこのpositionの全件を対象に")
    ap.add_argument("--out", default="insider_verified.json")
    args = ap.parse_args()

    if args.position:
        reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
        addrs = [e["address"] for e in reg.values() if e.get("position") == args.position]
    elif args.addr:
        addrs = args.addr
    else:
        import os
        if not os.path.exists(args.src):
            print(f"{args.src} が無い。先に classify_hl_list.py を実行せよ。")
            return
        data = json.load(open(args.src, encoding="utf-8"))["wallets"]
        addrs = [r["address"] for r in data if r.get("bucket", "").startswith("A")]
    if args.limit:
        addrs = addrs[:args.limit]
    print(f"イベント先行検証: {len(addrs)} 件")

    out = []
    for i, a in enumerate(addrs, 1):
        try:
            out.append(analyze_wallet(a))
        except Exception as e:
            out.append({"address": a, "error": str(e)[:60]})
        if i % 20 == 0:
            print(f"  {i}/{len(addrs)} ...")

    out.sort(key=lambda r: r.get("event_lead_notional", 0) or 0, reverse=True)
    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    from collections import Counter
    print("判定内訳:", dict(Counter(r.get("verdict", "?") for r in out)))
    print("--- insider-leaning 上位 ---")
    for r in out:
        if "insider" in r.get("verdict", ""):
            print(f"  先行${r.get('event_lead_notional',0):>10,} 的中{r.get('dir_accuracy')} "
                  f"{r.get('active_from')}〜{r.get('active_to')} {r['address'][:12]}..")
    print(f"保存 → {config.DATA_DIR}/insider_verified.json")


if __name__ == "__main__":
    main()
