"""未照会層(リーダーボードに居るが台帳未登録)へ遅効エッジ検出を拡大。

母集団: allTime黒字>=閾値 & 月黒字 & 口座生存 & 台帳未登録 を allTime降順で上限N件。
各件を複数地平線(insider_multihorizon)で解析し、分散テスト(時間/銘柄/方向)を通過した
『埋もれた遅効エッジ』だけを抽出。約定はhl_fills_cacheで永続化。
出力: data/late_edge_new.json（通過者のみ＋全件サマリ）
使い方: python scan_late_edge_pool.py [--min 300000] [--limit 1500]
"""
import json
import argparse
from datetime import datetime, timezone
from collections import Counter

import config
import hl_fills_cache as fc
import insider_multihorizon as mh

MS_H = 3600 * 1000


def od(x):
    x = (x or "").strip()
    return x.replace("Open", "").strip().lower() if x.startswith("Open") else None


def distinct(addr, r):
    """時間/銘柄/方向に分散した遅効エッジか。"""
    def g(h):
        hd = r.get("h_detr") or {}
        return hd.get(h, hd.get(str(h)))
    if (g(72) or 0) < 0.03 or (g(24) or 0) <= 0 or r.get("n_opens", 0) < 100:
        return None
    fills = fc.get_fills(addr, refresh=False)
    ops = [f for f in fills if f.get("coin") in config.COINS and od(f.get("dir")) in ("long", "short")]
    if len(ops) < 50:
        return None
    months = Counter(datetime.fromtimestamp(int(f["time"]) / 1000, timezone.utc).strftime("%Y-%m") for f in ops)
    coins = Counter(f["coin"] for f in ops)
    sr = sum(1 for f in ops if od(f.get("dir")) == "short") / len(ops)
    topm = max(months.values()) / len(ops)
    nprofit_coins = len([c for c in coins if coins[c] >= 10])
    if len(months) >= 4 and topm < 0.5 and nprofit_coins >= 2 and 0.15 <= sr <= 0.85:
        return {"detr72": g(72), "detr24": g(24), "detr4": g(4), "short_ratio": round(sr, 2),
                "months": len(months), "top_month_share": round(topm, 2), "coins": dict(coins),
                "n_opens": r["n_opens"]}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=300000)
    ap.add_argument("--limit", type=int, default=1500)
    ap.add_argument("--out", default="late_edge_new.json")
    args = ap.parse_args()

    lb = json.load(open(f"{config.DATA_DIR}/leaderboard.json", encoding="utf-8"))
    rows = lb if isinstance(lb, list) else lb.get("leaderboardRows") or []
    reg = set(json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"].keys())

    pool = []
    for r in rows:
        a = (r.get("ethAddress") or "").lower()
        if not a or a in reg:
            continue
        try:
            d = {w: v for w, v in r["windowPerformances"]}
            at = float(d["allTime"]["pnl"]); mo = float(d["month"]["pnl"]); av = float(r["accountValue"])
        except Exception:
            continue
        if at >= args.min and mo > 0 and av >= 10000:   # 元の広い条件(MM/巨鯨も含め全件)
            pool.append((a, at))
    pool.sort(key=lambda x: -x[1])
    pool = pool[:args.limit]
    print(f"未照会プール {len(pool)}件 (allTime>=${args.min:,}, 最小allTime≈${pool[-1][1]:,.0f})")

    for coin in config.COINS:
        mh.get_ser(coin)
    base = mh.baselines()

    passers, allsum = [], []
    for i, (a, at) in enumerate(pool, 1):
        try:
            r = mh.analyze(a, base)        # fetch_fills=キャッシュ経由
            dd = distinct(a, r) if r.get("h_detr") else None
        except Exception as e:
            r = {"address": a, "error": str(e)[:60]}; dd = None
        allsum.append({"address": a, "allTime_pnl": at,
                       "detr72": (r.get("h_detr") or {}).get(72, (r.get("h_detr") or {}).get("72")) if r.get("h_detr") else None,
                       "n_opens": r.get("n_opens"), "pass": bool(dd)})
        if dd:
            dd["address"] = a; dd["allTime_pnl"] = at
            passers.append(dd)
            print(f"  ★通過 {a[:10]} 補正72h+{dd['detr72']:.3f} {dd['months']}ヶ月 銘柄{list(dd['coins'])} short率{dd['short_ratio']} allTime${at:,.0f}")
        if i % 50 == 0:
            print(f"  {i}/{len(pool)} ... 通過{len(passers)} (cache {fc.stats()['wallets']}w)")
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "passers": passers, "summary": allsum},
                      open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "passers": passers, "summary": allsum},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n完了: 解析{len(pool)} / ★分散通過の埋もれた遅効エッジ {len(passers)}件 → data/{args.out}")
    for p in sorted(passers, key=lambda x: -x["detr72"])[:20]:
        print(f"  補正72h+{p['detr72']:.3f} 24h+{p['detr24']:.3f} {p['months']}ヶ月 {p['short_ratio']} {list(p['coins'])} ${p['allTime_pnl']:,.0f} {p['address'][:10]}")


if __name__ == "__main__":
    main()
