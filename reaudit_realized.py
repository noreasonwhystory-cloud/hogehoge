"""全アドレスを『真の実現損益』(キャッシュ約定のclosedPnl合計)で再監査。

台帳の記録PnL(current.total_pnl)は取得時期・出所がまちまちで信頼できず、
建玉時帰属の集中度metric欠陥もあった。キャッシュ約定から realize ベースで再計算し:
 - 生きた分類(プロ/弱い疑惑/出金疑い)なのに真の実現が赤字＝誤分類を検出
 - realize月の分散で真のhit-and-run(短期集中→停止)を判定
HL取得は一切せず data/fills/ のキャッシュのみ使用（スキャンと無衝突）。
使い方: python reaudit_realized.py [--apply]   # --applyで net赤字の生分類を除外へ降格
"""
import os
import sys
import json
from datetime import datetime, timezone
from collections import defaultdict

import config
import hl_fills_cache as fc

LIVE = {"プロトレーダー(本物)", "プロトレーダー(未精査)", "弱い疑惑(監視継続)",
        "💸 出金疑い(要監視)", "偽陽性(数値疑惑→否定)"}


def true_realized(addr):
    """キャッシュ約定から真の実現損益・勝率・件数・活動spanを一括再計算(取得なし)。"""
    if not os.path.exists(f"{config.DATA_DIR}/fills/{addr}.json"):
        return None
    fl = fc.get_fills(addr, refresh=False)
    if not fl:
        return None
    real_all = real_maj = 0.0
    months = defaultdict(float)
    maj = [f for f in fl if f.get("coin") in config.COINS]
    closes_maj = [f for f in maj if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
    wins = sum(1 for f in closes_maj if float(f["closedPnl"]) > 0)
    for f in fl:
        cp = float(f.get("closedPnl", 0) or 0)
        if abs(cp) <= 1e-9:
            continue
        real_all += cp
        if f.get("coin") in config.COINS:
            real_maj += cp
        months[datetime.fromtimestamp(int(f["time"]) / 1000, timezone.utc).strftime("%Y-%m")] += cp
    pos_m = {m: v for m, v in months.items() if v > 0}
    tot_pos = sum(pos_m.values())
    t0 = min(int(f["time"]) for f in maj) if maj else None
    t1 = max(int(f["time"]) for f in maj) if maj else None
    now = int(__import__("time").time() * 1000)
    cut = now - 14 * 86400000
    rec = [f for f in fl if int(f["time"]) >= cut]       # 直近14日の約定(キャッシュ真値)
    return {"real_all": round(real_all), "real_maj": round(real_maj),
            "win_rate": round(wins / len(closes_maj), 4) if closes_maj else None,
            "n_closes": len(closes_maj), "n_fills": len(fl),
            "n_fills_14d": len(rec),
            "n_fills_14d_maj": sum(1 for f in rec if f.get("coin") in config.COINS),
            "active_days": round((t1 - t0) / 86400000, 1) if t0 else None,
            "active14": bool(rec),
            "n_pos_months": len(pos_m),
            "top_month_share": round(max(pos_m.values()) / tot_pos, 2) if tot_pos else None}


def tier_of(real_all):
    return ("S" if real_all >= 5_000_000 else "A" if real_all >= 2_000_000 else
            "B" if real_all >= 1_000_000 else "C" if real_all >= 300_000 else "D")


def main():
    apply = "--apply" in sys.argv
    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # キャッシュがある台帳ウォレット全部を対象に指標を真値へ正規化
    cached = [(k, e) for k, e in W.items() if os.path.exists(f"{config.DATA_DIR}/fills/{k}.json")]
    print(f"台帳 {len(W)}件 / キャッシュ有 {len(cached)}件を正規化（未キャッシュはスキャン後に差分処理）")

    normalized = losers = tier_chg = 0
    for k, e in cached:
        t = true_realized(k)
        if not t:
            continue
        # 全ウォレットの表示指標をキャッシュ起点の真値へ統一
        cur = e.setdefault("current", {})
        cur["win_rate"] = t["win_rate"]
        cur["total_pnl"] = t["real_maj"]           # majors実現益(closedPnl合計)に統一
        e["true_realized_all"] = t["real_all"]
        e["true_realized_maj"] = t["real_maj"]
        e["n_closes"] = t["n_closes"]
        e["n_fills_14d"] = t["n_fills_14d"]            # 取引数(14日)もキャッシュ真値に統一
        e["n_fills_14d_maj"] = t["n_fills_14d_maj"]
        e["active_days"] = t["active_days"]
        if t["active14"] is not None:
            e["active14"] = t["active14"]
        e["metric_normalized"] = today
        normalized += 1
        # 生きた分類だが真の実現が通算赤字＝誤分類→除外
        if t["real_all"] < 0 and e["position"] in ("プロトレーダー(本物)", "プロトレーダー(未精査)",
                                                    "💸 出金疑い(要監視)", "弱い疑惑(監視継続)"):
            losers += 1
            if apply:
                e["position"] = "除外/低優先"
                e["tags"] = sorted(set([x for x in e.get("tags", []) if not x.startswith("Tier-")] + ["再監査:真の実現赤字"]))
                e["notes_jp"] = (f"【真の実現で再監査({today})】実現益合計 全${t['real_all']:,}/majors"
                                 f"${t['real_maj']:,}＝通算赤字。記録PnLは過大/誤り。黒字前提の分類から除外へ。\n―――\n"
                                 + e.get("notes_jp", ""))
        # プロのTierを真の実現益(全銘柄)で再ランク
        if e.get("position") == "プロトレーダー(本物)" and t["real_all"] > 0:
            nt = tier_of(t["real_all"])
            if e.get("pro_tier") != nt:
                tier_chg += 1
            e["pro_tier"] = nt
            if apply:
                e["tags"] = sorted(set([x for x in e.get("tags", []) if not x.startswith("Tier-")] + [f"Tier-{nt}"]))

    print(f"指標正規化 {normalized}件 / 真の実現赤字で除外降格 {losers}件 / Tier変動 {tier_chg}件")
    if apply:
        json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        import step6_registry as reg6
        reg6.render_all(reg)
        import collections
        print("台帳再生成。新分布:", dict(collections.Counter(e["position"] for e in W.values())))
    else:
        print("(--apply で台帳へ反映)")


if __name__ == "__main__":
    main()
