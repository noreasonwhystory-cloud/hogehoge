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

    # workflowプロ精鋭検証(2レンズ)の elite/questionable を取り込む
    both_elite, any_elite, any_quest = set(), set(), set()
    try:
        wf = json.load(open(f"{config.DATA_DIR}/insider_pro_wf.json", encoding="utf-8"))["pros"]
        elites = []
        for p in wf:
            es = {e["address"].lower() for e in p.get("elite", [])}
            qs = {e["address"].lower() for e in p.get("questionable", [])}
            elites.append(es); any_elite |= es; any_quest |= qs
        if len(elites) >= 2:
            both_elite = elites[0] & elites[1]
    except FileNotFoundError:
        pass

    pros = [(k, e) for k, e in reg["wallets"].items() if e.get("position") in PRO_POS]
    for k, e in pros:
        x = hl.get(k, {})
        cur = e.get("current", {})
        realized = x.get("realized_pnl")
        if realized is None:
            realized = cur.get("total_pnl") or 0
        days = span_days(x) or e.get("trade_days") or 30
        nfills = x.get("n_fills") or 0
        active = e.get("active14")
        # robust成分（勝率・的中率は汚染指標ゆえ不使用）
        pnl_pts = min(max(realized, 0) / 5_000_000, 1.0)                  # 実現益(HL実測)
        cont_pts = min(days / 365, 1.0) * 0.6 + min(nfills / 3000, 1.0) * 0.4  # 継続性
        active_pts = 1.0 if active else 0.25                              # 現役か
        if k in both_elite:
            review_pts = 1.0                                             # 両レンズ精鋭
        elif k in any_quest:
            review_pts = 0.15                                            # 運/MM/サバイバーシップ疑い
        elif k in any_elite:
            review_pts = 0.7
        else:
            review_pts = 0.5
        score = round(0.45 * pnl_pts + 0.20 * cont_pts + 0.15 * active_pts + 0.20 * review_pts, 3)
        e["pro_score_old"] = e.get("pro_score")
        e["pro_score"] = score
        e["pro_realized"] = round(realized) if realized else None
        e["pro_span_days"] = days
        old_tier = e.get("pro_tier")
        tier = ("S" if score >= 0.65 else "A" if score >= 0.50 else
                "B" if score >= 0.38 else "C" if score >= 0.25 else "D")
        e["pro_tier_old"] = old_tier
        e["pro_tier"] = tier
        e["tags"] = sorted(set([t for t in e.get("tags", []) if not t.startswith("Tier-")])
                           | {f"Tier-{tier}"})

    json.dump(reg, open(f"{config.DATA_DIR}/wallet_registry.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    reg6.render_all(reg)

    ranked = sorted(pros, key=lambda kv: kv[1].get("pro_score", 0), reverse=True)
    from collections import Counter
    tiers = Counter(e["pro_tier"] for _, e in pros)
    old = Counter(e.get("pro_tier_old") for _, e in pros)
    print(f"プロ {len(pros)}件 robust再ランク。")
    print(f"  旧tier分布: {dict(old)}")
    print(f"  新tier分布: {dict(tiers)}")
    changed = [(k, e) for k, e in pros if e.get("pro_tier_old") != e["pro_tier"]]
    print(f"  tier変動: {len(changed)}件")
    print("=== 新Top12（robust）===")
    for k, e in ranked[:12]:
        lbl = (e.get("labels") or [""])[0] or "匿名"
        mv = f"{e.get('pro_tier_old')}→{e['pro_tier']}" if e.get('pro_tier_old') != e['pro_tier'] else e['pro_tier']
        act = "現役" if e.get("active14") else "停止"
        print(f"  [{mv}] score{e['pro_score']} 実現${(e.get('pro_realized') or 0):,} {act} {k[:10]}.. [{lbl[:18]}]")
    print("=== 大きく降格した例（旧S/A→低位）===")
    for k, e in pros:
        if e.get("pro_tier_old") in ("S", "A") and e["pro_tier"] in ("C", "D"):
            print(f"  {e['pro_tier_old']}→{e['pro_tier']} {k[:10]}.. (現役{e.get('active14')}/実現${(e.get('pro_realized') or 0):,})")


if __name__ == "__main__":
    main()
