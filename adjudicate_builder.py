"""ビルダーperp(xyz:等)で稼いだ層を、majorsと同じ厳格ゲート(往復＋反復)で本判定する。

raw方向的中は薄商いで偽陽性が溢れるため使わない。insider_v2 の「イベント先行建て→急変後利確(往復)を
別イベントで反復」という厳格ロジックを builder coin にも適用し、強シグナルだけを抽出する。
 候補 = キャッシュ上 |ビルダーperp実現益| >= MIN_BUILDER のウォレット。
 出力 = data/builder_adjudicate.json（strict往復で生き残った候補＋全候補の指標）。
使い方: python adjudicate_builder.py [--min 50000]
"""
import os
import json
import time
import argparse
from datetime import datetime, timezone

import config
import hl_fills_cache as fc
import insider_v2 as v2

MS_H = 3600 * 1000
MAJ = set(config.COINS)
FILLS = os.path.join(config.DATA_DIR, "fills")


def builder_realized(fl):
    rmaj = sum(float(f.get("closedPnl", 0) or 0) for f in fl if f.get("coin") in MAJ)
    rperp = sum(float(f.get("closedPnl", 0) or 0) for f in fl if fc.is_perp_coin(f.get("coin")))
    return rmaj, rperp, rperp - rmaj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=50000, help="ビルダーperp実現益の下限(USD)")
    args = ap.parse_args()

    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    files = [f for f in os.listdir(FILLS) if f.endswith(".json")]
    print(f"キャッシュ {len(files)}件からビルダーperp実現益>=${args.min:,}の候補を抽出中…")

    cands, fills_map, all_coins = [], {}, set(config.COINS)
    for fn in files:
        a = fn[:-5]
        try:
            fl = json.load(open(os.path.join(FILLS, fn), encoding="utf-8")).get("fills", [])
        except Exception:
            continue
        if not fl:
            continue
        rmaj, rperp, rbuild = builder_realized(fl)
        # ビルダーperp固有銘柄(接頭辞付き)を実際に持つか
        has_builder = any(":" in (f.get("coin") or "") for f in fl)
        if not has_builder or abs(rbuild) < args.min:
            continue
        cands.append({"address": a, "rmaj": round(rmaj), "rperp": round(rperp), "rbuild": round(rbuild),
                      "position": reg.get(a, {}).get("position"), "wf_quality": reg.get(a, {}).get("wf_quality")})
        fills_map[a] = fl
        all_coins |= fc.scan_coins(fl)

    print(f"候補 {len(cands)}件 / 走査coin universe {len(all_coins)}銘柄 → イベント構築(4h candle)…")
    now = int(time.time() * 1000)
    events = v2.build_events(all_coins, now - 560 * 24 * MS_H, now)
    # ビルダーイベント数の内訳
    bev = sum(1 for e in events if ":" in e["coin"])
    print(f"急変イベント {len(events)}件(うちビルダー {bev}件)")

    for i, c in enumerate(cands, 1):
        try:
            r = v2.analyze(c["address"], events, fills=fills_map[c["address"]])
        except Exception as e:
            r = None
        if r:
            c["tiers"] = r["tiers"]
            c["coins"] = r.get("coins")
            for nm in ("strict", "medium", "loose"):
                c[f"rt_{nm}"] = r["tiers"][nm]["rt"]
                c[f"clust_{nm}"] = r["tiers"][nm]["large_clusters"]
        if i % 25 == 0:
            print(f"  {i}/{len(cands)} 裁定中…")

    # majorsと同じ厳格バー: medium往復>=2 かつ 大口クラスタ>=3（薄商いN=1を排除）
    strict = [c for c in cands if c.get("rt_medium", 0) >= 2 and c.get("clust_medium", 0) >= 3]
    strict.sort(key=lambda c: (c.get("rt_medium", 0), c.get("rt_strict", 0)), reverse=True)
    # 緩めの注目(往復>=1でもビルダー優勢)も別枠で記録
    watch = [c for c in cands if c.get("rt_strict", 0) >= 1 and c not in strict]

    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "min_builder": args.min,
           "n_candidates": len(cands), "n_strict": len(strict), "n_watch": len(watch),
           "strict": strict, "watch": watch, "all": cands}
    json.dump(out, open(f"{config.DATA_DIR}/builder_adjudicate.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print(f"\n=== 厳格往復(medium往復>=2 & 大口クラスタ>=3): {len(strict)}件 ===")
    for c in strict[:25]:
        print(f"  往復 strict{c.get('rt_strict')}/medium{c.get('rt_medium')} クラスタ{c.get('clust_medium')} "
              f"builder実現${c['rbuild']:,} {c['address'][:12]} [{(c.get('position') or '')[:14]}] coins={c.get('coins')}")
    print(f"\n=== 参考: strict往復>=1の注目層 {len(watch)}件（薄商い偽陽性の可能性込み）===")
    for c in sorted(watch, key=lambda c: -c.get("rt_strict", 0))[:15]:
        print(f"  strict往復{c.get('rt_strict')} builder実現${c['rbuild']:,} {c['address'][:12]} [{(c.get('position') or '')[:14]}]")
    print("\n→ strict層を workflow で敵対裁定する。出力: data/builder_adjudicate.json")


if __name__ == "__main__":
    main()
