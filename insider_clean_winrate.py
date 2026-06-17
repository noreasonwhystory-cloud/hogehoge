"""個人インサイダー検出（別定義v-clean）: 含み損バッグなし × 高勝率 × 高方向的中率 × 1週間以上。

狙い: v0の高勝率の最大の汚染『勝ち玉だけ確定・負けは塩漬け(含み損放置)で分母から隠す』を直接潰す。
  → 現在 含み損のデカいバッグを抱えていない ことを条件に足せば、残る高勝率は塩漬け由来でない本物のエッジ。
  それが1週間以上持続し方向的中率も高ければ『建てた瞬間から逆行せずほぼ常に正しい』＝知っていた疑い。

含み損は clearinghouseState の建玉ごと unrealizedPnl で実測。win_rate/dir_accuracy は majors 約定から算出。
出力: data/insider_clean_winrate.json
使い方: python insider_clean_winrate.py [--limit N] [--positions ...] [--scan-all]
"""
import json
import time
import argparse
from datetime import datetime, timezone

import config
import hl_client

MS_H = 3600 * 1000
NOW = int(time.time() * 1000)
CAND_DAYS = 420
# 判定閾値
WIN_MIN = 0.70
DIR_MIN = 0.65
MIN_CLOSES = 10
MIN_DAYS = 7
BAG_MAX = 0.03          # 含み損合計が口座の3%超なら「塩漬けバッグあり」=失格


def fetch_fills(addr, max_pages):
    out, cur = [], 0
    for _ in range(max_pages):
        chunk = hl_client._post_info({"type": "userFillsByTime", "user": addr,
                                      "startTime": cur, "endTime": NOW})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 2000:
            break
        last = chunk[-1]["time"]
        if last <= cur:
            break
        cur = last + 1
    seen, ded = set(), []
    for f in out:
        if f.get("tid") in seen:
            continue
        seen.add(f.get("tid")); ded.append(f)
    return ded


def open_dir(d):
    d = (d or "").strip()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()
    return None


def price_at(series, t):
    lo, hi = 0, len(series) - 1
    if hi < 0 or t < series[0][0]:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if series[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    return series[lo][1]


def bags_from_state(st):
    """clearinghouseState から 口座価値・含み損合計・最悪バッグ・含み損建玉リスト。"""
    acct = float((st or {}).get("marginSummary", {}).get("accountValue", 0) or 0)
    underwater, total_unrl, worst = [], 0.0, 0.0
    for ap in (st or {}).get("assetPositions", []):
        p = ap.get("position", {})
        u = float(p.get("unrealizedPnl", 0) or 0)
        total_unrl += u
        if u < 0:
            underwater.append({"coin": p.get("coin"), "unrl": round(u),
                               "value": round(float(p.get("positionValue", 0) or 0))})
            worst = min(worst, u)
    loss = sum(-x["unrl"] for x in underwater)
    return {"account_value": round(acct), "total_unrealized": round(total_unrl),
            "underwater_loss": round(loss), "worst_bag": round(worst),
            "underwater": sorted(underwater, key=lambda x: x["unrl"])[:6],
            "bag_ratio": round(loss / acct, 3) if acct else None}


def analyze(addr, max_pages, candle):
    fills = fetch_fills(addr, max_pages)
    maj = [f for f in fills if f.get("coin") in config.COINS]
    if not maj:
        return {"address": addr, "no_majors": True}
    closes = [f for f in maj if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
    wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)
    win_rate = round(wins / len(closes), 4) if closes else None
    # 方向的中率(4h)
    hits = opens = 0
    for f in maj:
        d = open_dir(f.get("dir"))
        if d not in ("long", "short"):
            continue
        series = candle[f["coin"]]
        t = int(f["time"]); p0 = price_at(series, t); p1 = price_at(series, t + config.HIT_HORIZON_H * MS_H)
        if not p0 or not p1:
            continue
        opens += 1
        moved = (p1 - p0) / p0
        if (d == "long" and moved > 0) or (d == "short" and moved < 0):
            hits += 1
    dir_acc = round(hits / opens, 4) if opens else None
    t0 = min(int(f["time"]) for f in maj); t1 = max(int(f["time"]) for f in maj)
    days = round((t1 - t0) / (24 * MS_H), 1)
    st = hl_client.clearinghouse_state(addr)
    bags = bags_from_state(st)
    realized = round(sum(float(f.get("closedPnl", 0) or 0) for f in maj))

    clean = (bags["bag_ratio"] is not None and bags["bag_ratio"] <= BAG_MAX)
    qualifies = bool(clean and win_rate and dir_acc and win_rate >= WIN_MIN and dir_acc >= DIR_MIN
                     and len(closes) >= MIN_CLOSES and days >= MIN_DAYS)
    return {
        "address": addr, "win_rate": win_rate, "dir_accuracy": dir_acc,
        "n_closes": len(closes), "n_opens": opens, "active_days": days,
        "realized_majors": realized, **bags,
        "no_bag": clean, "qualifies": qualifies,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--maxpages", type=int, default=20)
    ap.add_argument("--positions", default="プロトレーダー(本物),プロトレーダー(未精査),💸 出金疑い(要監視)")
    ap.add_argument("--scan-all", action="store_true", help="台帳の全ウォレットを対象(広域探索)")
    ap.add_argument("--out", default="insider_clean_winrate.json")
    args = ap.parse_args()

    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    if args.scan_all:
        targets = [e["address"] for e in reg.values()]
    else:
        poss = set(p for p in args.positions.split(",") if p)
        targets = [e["address"] for e in reg.values()
                   if e.get("position") in poss or e.get("wf2_checked")]
    targets = list(dict.fromkeys(targets))
    if args.limit:
        targets = targets[:args.limit]
    print(f"clean高勝率 解析対象: {len(targets)} 件 "
          f"(条件: 勝率>={WIN_MIN} 的中>={DIR_MIN} closes>={MIN_CLOSES} {MIN_DAYS}日+ 含み損<=口座{int(BAG_MAX*100)}%)")

    candle = {}
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", NOW - CAND_DAYS * 24 * MS_H, NOW) or []
        candle[coin] = sorted([(int(x["t"]), float(x["c"])) for x in c])

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, args.maxpages, candle)
        except Exception as e:
            r = {"address": a, "error": str(e)[:80]}
        r["position"] = reg.get(a.lower(), {}).get("position")
        r["labels"] = reg.get(a.lower(), {}).get("labels") or []
        out.append(r)
        if i % 20 == 0:
            print(f"  {i}/{len(targets)} ...")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    qual = [r for r in out if r.get("qualifies")]
    qual.sort(key=lambda r: (r["win_rate"] or 0) * (r["dir_accuracy"] or 0), reverse=True)
    print(f"\n=== 条件通過（含み損バッグなし×高勝率×高的中×1週間+）: {len(qual)}件 ===")
    for r in qual:
        print(f"  勝率{r['win_rate']} 的中{r['dir_accuracy']} closes{r['n_closes']} {r['active_days']}日 "
              f"含み損率{r['bag_ratio']} 実現${r['realized_majors']:,} {r['address'][:10]}.. [{r['position']}]")
    # 高勝率だが塩漬けで失格、を対比表示
    dirty = [r for r in out if r.get("win_rate") and r["win_rate"] >= WIN_MIN and not r.get("no_bag")]
    print(f"\n--- 参考: 高勝率(>= {WIN_MIN})だが含み損バッグありで失格: {len(dirty)}件 ---")
    for r in sorted(dirty, key=lambda r: -(r.get("bag_ratio") or 0))[:10]:
        print(f"  勝率{r['win_rate']} 含み損率{r['bag_ratio']} 含み損${r.get('underwater_loss'):,} {r['address'][:10]}.. [{r['position']}]")


if __name__ == "__main__":
    main()
