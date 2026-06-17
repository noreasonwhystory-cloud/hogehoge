"""プロ(本物/未精査)を品質スコアでランク付けし tier(S/A/B/C/D) を付与。

スコア = HL実測の実現益(対数) + 勝率 + 継続性(活動期間/取引数)。
（方向的中率はトレンドバイアスがあるため低ウェイト）
台帳に pro_score / pro_tier を保存し、pros.html はスコア順に並ぶ。
使い方: python pro_ranking.py
"""
import json
from datetime import datetime

import config
import step6_registry as reg6

PRO_POS = {"プロトレーダー(本物)", "プロトレーダー(未精査)"}


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))
    hl = {}
    for fn in ["hl_list_analysis.json", "hl_cand_analysis.json"]:
        try:
            for x in json.load(open(f"{config.DATA_DIR}/{fn}", encoding="utf-8"))["wallets"]:
                hl[x["address"].lower()] = x
        except FileNotFoundError:
            pass

    def span_days(x):
        try:
            a = datetime.strptime(x["active_from"], "%Y-%m-%d")
            b = datetime.strptime(x["active_to"], "%Y-%m-%d")
            return max((b - a).days, 1)
        except Exception:
            return None

    pros = [(k, e) for k, e in reg["wallets"].items() if e.get("position") in PRO_POS]
    for k, e in pros:
        x = hl.get(k, {})
        cur = e.get("current", {})
        realized = x.get("realized_pnl")
        if realized is None:
            realized = cur.get("total_pnl") or 0
        win = x.get("win_rate")
        if win is None:
            win = cur.get("win_rate") or 0
        dacc = cur.get("dir_accuracy") or 0
        days = span_days(x) or 30
        nfills = x.get("n_fills") or 0
        # 各成分 0..1
        pnl_pts = min(max(realized, 0) / 5_000_000, 1.0)      # $5M で満点
        win_pts = win or 0
        cont_pts = min(days / 180, 1.0) * 0.5 + min(nfills / 2000, 1.0) * 0.5
        score = round(0.45 * pnl_pts + 0.30 * win_pts + 0.15 * cont_pts + 0.10 * dacc, 3)
        e["pro_score"] = score
        e["pro_realized"] = round(realized) if realized else None
        e["pro_span_days"] = days
        tier = ("S" if score >= 0.65 else "A" if score >= 0.50 else
                "B" if score >= 0.38 else "C" if score >= 0.25 else "D")
        e["pro_tier"] = tier
        e["tags"] = sorted(set([t for t in e.get("tags", []) if not t.startswith("Tier-")])
                           | {f"Tier-{tier}"})

    json.dump(reg, open(f"{config.DATA_DIR}/wallet_registry.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    reg6.render_all(reg)

    ranked = sorted(pros, key=lambda kv: kv[1].get("pro_score", 0), reverse=True)
    from collections import Counter
    tiers = Counter(e["pro_tier"] for _, e in pros)
    print(f"プロ {len(pros)}件 をランク付け。tier分布: {dict(tiers)}")
    print("=== 最強プロ Top15 ===")
    for k, e in ranked[:15]:
        x = hl.get(k, {})
        lbl = (e.get("labels") or [""])[0] or "匿名"
        print(f"  [{e['pro_tier']}] score{e['pro_score']} 実現${(e.get('pro_realized') or 0):,} "
              f"勝率{x.get('win_rate')} {x.get('active_from','?')}〜{x.get('active_to','?')} "
              f"{k[:10]}.. [{lbl[:20]}]")


if __name__ == "__main__":
    main()
