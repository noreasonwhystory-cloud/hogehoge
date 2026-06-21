"""監査指摘の修正(データ層): notes_jpヘッダの古いPnL(符号反転含む)・日付鮮度・updated_atを
キャッシュ真値から一括再同期する。reauditがtotal_pnlは更新するがnotesヘッダを再生成しない穴を塞ぐ。
"""
import os
import re
import json
import time
from datetime import datetime, timezone

import config
import hl_fills_cache as fc

MAJ = set(config.COINS)
QLAB = {
    "プロトレーダー(本物)": "🟢プロ(本物)", "alt主体プロ": "🔵alt主体プロ",
    "高頻度MM": "🟣高頻度MM", "弱い疑惑(監視継続)": "🟠弱い疑惑",
    "💸 出金疑い(要監視)": "💸出金疑い", "偽陽性(数値疑惑→否定)": "⚫偽陽性",
    "除外/低優先": "⚫除外/低優先", "プロトレーダー(未精査)": "🔵プロ(未精査)",
    "インサイダー疑惑(要監視)": "🔴インサイダー疑惑",
}


def main():
    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    now = int(time.time() * 1000)
    cut = now - 14 * 86400 * 1000
    n = 0
    for k, e in W.items():
        if not os.path.exists(f"{config.DATA_DIR}/fills/{k}.json"):
            continue
        fl = fc.get_fills(k, refresh=False)
        if not fl:
            continue
        rall = round(sum(float(f.get("closedPnl", 0) or 0) for f in fl))
        rmaj = round(sum(float(f.get("closedPnl", 0) or 0) for f in fl if f.get("coin") in MAJ))
        ts = [int(f["time"]) for f in fl]
        af = datetime.utcfromtimestamp(min(ts) / 1000).strftime("%Y-%m-%d")
        at = datetime.utcfromtimestamp(max(ts) / 1000).strftime("%Y-%m-%d")
        rec = [f for f in fl if int(f["time"]) >= cut]
        e["true_realized_all"] = rall
        e["true_realized_maj"] = rmaj
        e.setdefault("current", {})["total_pnl"] = rmaj
        e["active_from"], e["active_to"] = af, at
        e["active14"] = bool(rec)
        e["n_fills_14d"] = len(rec)
        e["n_fills_14d_maj"] = sum(1 for f in rec if f.get("coin") in MAJ)
        ql = ("質:" + e["wf_quality"]) if e.get("wf_quality") else ""
        head = (f"【現在の分類: {QLAB.get(e['position'], e['position'])}"
                + (f" / {ql}" if ql else "")
                + f" / majors実現${rmaj:,} / 最終取引{at}】")
        body = [l for l in (e.get("notes_jp") or "").split("\n") if not l.startswith("【現在の分類")]
        e["notes_jp"] = head + "\n" + "\n".join(body).lstrip("\n")
        n += 1
    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 検証
    bad = flip = 0
    for e in W.values():
        first = (e.get("notes_jp") or "").split("\n")[0]
        m = re.search(r"majors実現\$([-\d,]+)", first)
        if m:
            disp = int(m.group(1).replace(",", ""))
            t = e.get("true_realized_maj") or 0
            if disp != t:
                bad += 1
            if (disp > 0) != (t > 0) and abs(t) > 100:
                flip += 1
    print(f"再同期 {n}件 / updated_at更新 / 再生成後ヘッダ不一致 {bad} 符号反転 {flip}")


if __name__ == "__main__":
    main()
