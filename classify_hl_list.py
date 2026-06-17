"""HL実測フォレンジック結果(hl_list_analysis.json)を『行動』で仕分けする。

ラベルでなく行動(HL実測の実現損益・勝率・約定構造)で分類:
  A. 稼ぎ確認・要インサイダー検証 … 実現益が大きく勝率も妥当（次にevent-timing検証へ）
  B. MM/HFT(頭打ち)              … 約定が膨大で頭打ち＝自動売買
  C. ノイズ/赤字                 … 実現益が薄い or マイナス
さらに「Nansenの言うPnL vs HL実測実現益」の乖離を出す（LB益が取引由来かの検証）。
出力: data/hl_classified.json ＋ コンソール要約
使い方: python classify_hl_list.py
"""
import json
import config

SRC = f"{config.DATA_DIR}/hl_list_analysis.json"

MIN_REAL = 500_000      # 「稼ぎ確認」の実現益下限(USD)
MM_FILLS = 8000         # これ超＆頭打ち＝MM/HFT
MIN_WIN = 0.45          # 妥当な勝率下限


def bucket(r):
    nf = r.get("n_fills", 0) or 0
    real = r.get("realized_pnl", 0) or 0
    win = r.get("win_rate")
    capped = r.get("capped")
    if nf == 0:
        return "履歴なし"
    if capped and nf >= MM_FILLS:
        return "B: MM/HFT(頭打ち)"
    if real >= MIN_REAL and (win is None or win >= MIN_WIN):
        return "A: 稼ぎ確認・要インサイダー検証"
    if real <= 0:
        return "C: 赤字/ノイズ"
    return "C: 小利/ノイズ"


def main():
    import os
    if not os.path.exists(SRC):
        print(f"{SRC} がまだ無い（HL分析が実行中）。完了後に再実行せよ。")
        return
    data = json.load(open(SRC, encoding="utf-8"))["wallets"]
    for r in data:
        r["bucket"] = bucket(r)
        lb = r.get("lb_pnl") or 0
        real = r.get("realized_pnl") or 0
        # LB益が取引(実現)で説明できるか: 実現/LB の比率
        r["realized_vs_lb"] = round(real / lb, 2) if lb else None

    from collections import Counter
    cnt = Counter(r["bucket"] for r in data)
    print(f"HL分析済 {len(data)} 件の行動仕分け:")
    for b, n in cnt.most_common():
        print(f"  {b}: {n}")

    A = sorted([r for r in data if r["bucket"].startswith("A")],
               key=lambda r: r.get("realized_pnl", 0), reverse=True)
    print(f"\n=== A: 稼ぎ確認・要インサイダー検証 上位15（HL実測実現益）===")
    for r in A[:15]:
        rl = r.get("realized_vs_lb")
        flag = "  ⚠LB益の大半は取引外(funding/airdrop?)" if (rl is not None and rl < 0.3) else ""
        print(f"  実現${r.get('realized_pnl',0):>11,} 勝率{r.get('win_rate')} majors{r.get('majors_pct')} "
              f"LB比{rl} {r.get('active_from')}〜{r.get('active_to')} {r['address'][:10]}.. {flag}")

    json.dump({"wallets": data}, open(f"{config.DATA_DIR}/hl_classified.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n保存 → {config.DATA_DIR}/hl_classified.json")
    print(f"次: A群（{len(A)}件）に event-timing 検証をかけて insider/pro を最終判定。")


if __name__ == "__main__":
    main()
