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
    """キャッシュ約定から全銘柄/majorsの真の実現損益＋realize月分布を返す(取得なし)。"""
    if not os.path.exists(f"{config.DATA_DIR}/fills/{addr}.json"):
        return None
    fl = fc.get_fills(addr, refresh=False)
    if not fl:
        return None
    real_all = real_maj = 0.0
    months = defaultdict(float)
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
    top_share = round(max(pos_m.values()) / tot_pos, 2) if tot_pos else None
    return {"real_all": round(real_all), "real_maj": round(real_maj),
            "n_pos_months": len(pos_m), "top_month_share": top_share}


def main():
    apply = "--apply" in sys.argv
    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    live = [(k, e) for k, e in W.items() if e.get("position") in LIVE]
    cached = [(k, e) for k, e in live if os.path.exists(f"{config.DATA_DIR}/fills/{k}.json")]
    print(f"生きた分類 {len(live)}件 / キャッシュ有 {len(cached)}件（未キャッシュ {len(live)-len(cached)}件はスキャン後に要再監査）")

    losers, fixed = [], 0
    for k, e in cached:
        t = true_realized(k)
        if not t:
            continue
        e["true_realized_all"] = t["real_all"]
        e["true_realized_maj"] = t["real_maj"]
        # net赤字＝生きた分類として不適格(プロ/弱疑惑/出金疑いは黒字が前提)
        if t["real_all"] < 0 and e["position"] in ("プロトレーダー(本物)", "プロトレーダー(未精査)",
                                                    "💸 出金疑い(要監視)", "弱い疑惑(監視継続)"):
            losers.append((k, e["position"], t))
            if apply:
                e["position"] = "除外/低優先"
                e["tags"] = sorted(set(e.get("tags", []) + ["再監査:真の実現赤字"]))
                e["notes_jp"] = (f"【真の実現損益で再監査({today})】キャッシュ約定の実現益合計は全銘柄"
                                 f"${t['real_all']:,}・majors${t['real_maj']:,}＝<b>通算赤字</b>。"
                                 f"台帳の記録PnLは過大/誤りだった。黒字前提の分類から除外へ降格。\n―――\n"
                                 + e.get("notes_jp", ""))
                fixed += 1

    print(f"\n=== 生きた分類だが真の実現が通算赤字＝誤分類 {len(losers)}件 ===")
    for k, pos, t in sorted(losers, key=lambda x: x[2]["real_all"]):
        print(f"  {k[:12]} [{pos[:14]}] 真の実現(全)${t['real_all']:,} majors${t['real_maj']:,}")

    if apply and fixed:
        json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        import step6_registry as reg6
        reg6.render_all(reg)
        import collections
        print(f"\n--apply: {fixed}件を除外へ降格・台帳再生成。新分布:",
              dict(collections.Counter(e["position"] for e in W.values())))
    elif losers:
        print("\n(--apply で除外へ降格する)")


if __name__ == "__main__":
    main()
