"""監査指摘#4/#19/methodology の修正: 未トリアージの高額層を台帳化する。

cache(3844件)にあるのに台帳化されていない個体のうち、majors実現>=$50万(=majors先物スコープの高額層)を
既存と同じ決定的ロジックで分類(MM/本物プロ/alt主体プロ/除外)し、品質・HFT・指標・notesを付けて台帳へ追加する。
これで『確証ゼロ』結論が未トリアージ層を取りこぼしている穴を塞ぐ。
使い方: python triage_untriaged.py
"""
import os
import json
import time
from collections import defaultdict, Counter
from datetime import datetime

import config
import hl_fills_cache as fc
import hl_client

MAJ = set(config.COINS)
MIN_MAJ = 500_000


def pro_grade(months, posr, pf, artifact, real_all, real_maj, worst):
    # maj_share(majors比)はエリート必須条件から撤廃(perp取得監査 2026-06-22)。
    # builder主体でも長期・黒字月率・PF・規模が揃えばエリート。majors比は意味づけが逆向きだった。
    if months < 4 or artifact:
        return "履歴薄/評価不能"
    if posr < 0.6 or (real_all > 0 and abs(worst) > 0.4 * real_all):
        return "ムラあり"
    if months >= 10 and posr >= 0.8 and pf >= 3 and real_all >= 1_000_000:
        return "エリート"
    if months >= 6 and posr >= 0.7 and pf >= 2 and real_all > 0:
        return "堅実"
    if real_all > 0:
        return "中堅"
    return "ムラあり"


def hft_tag(cpm):
    if cpm >= 10000:
        return "HFT:超高速(10k+/月)"
    if cpm >= 3000:
        return "HFT:高速(3k-10k/月)"
    if cpm >= 1500:
        return "HFT:標準(1.5k-3k/月)"
    return "HFT:低速(〜1.5k/月)"


QLAB = {"プロトレーダー(本物)": "🟢プロ(本物)", "alt主体プロ": "🔵alt主体プロ",
        "高頻度MM": "🟣高頻度MM", "除外/低優先": "⚫除外/低優先"}


def main():
    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    reg_keys = set(W.keys())
    now = int(time.time() * 1000)
    cut = now - 14 * 86400 * 1000
    today = datetime.utcnow().strftime("%Y-%m-%d")

    added = Counter()
    files = [f for f in os.listdir(f"{config.DATA_DIR}/fills") if f.endswith(".json")]
    for fn in files:
        k = fn[:-5]
        if k in reg_keys:
            continue
        try:
            fl = json.load(open(f"{config.DATA_DIR}/fills/{fn}", encoding="utf-8")).get("fills", [])
        except Exception:
            continue
        if not fl:
            continue
        rmaj = round(sum(float(f.get("closedPnl", 0) or 0) for f in fl if f.get("coin") in MAJ))
        rperp = round(sum(float(f.get("closedPnl", 0) or 0) for f in fl if fc.is_perp_coin(f.get("coin"))))
        # perp実現益(majors+builder)で足切り=純ビルダーperpの高額層も精査キューへ(majors限定MIN_MAJの取りこぼし解消)
        if rperp < MIN_MAJ:
            continue
        rall = round(sum(float(f.get("closedPnl", 0) or 0) for f in fl))
        builder_dexs = sorted(hl_client.used_dexs_from_fills(fl))
        rbuild = rperp - rmaj   # ビルダーperp実現益
        closes = [f for f in fl if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
        ncl = len(closes)
        mon = defaultdict(float)
        for f in closes:
            mon[datetime.utcfromtimestamp(int(f["time"]) / 1000).strftime("%Y-%m")] += float(f["closedPnl"])
        months = len(mon)
        cpm = int(ncl / max(months, 1))
        posr = (sum(1 for v in mon.values() if v > 0) / months) if months else 0
        gw = sum(v for v in mon.values() if v > 0)
        gl = abs(sum(v for v in mon.values() if v < 0))
        pf = (gw / gl) if gl > 0 else 99
        worst = min(mon.values()) if mon else 0
        maj_share = (rmaj / rall) if rall > 0 else 0
        ts = [int(f["time"]) for f in fl]
        af = datetime.utcfromtimestamp(min(ts) / 1000).strftime("%Y-%m-%d")
        at = datetime.utcfromtimestamp(max(ts) / 1000).strftime("%Y-%m-%d")
        rec = [f for f in fl if int(f["time"]) >= cut]

        # 分類（既存と同じ決定的ロジック）
        is_mm = cpm > 1500 or len(fl) > 3000
        if is_mm:
            pos = "高頻度MM"
        elif rall <= 0:
            pos = "除外/低優先"
        elif maj_share >= 0.3:
            pos = "プロトレーダー(本物)"
        else:
            pos = "alt主体プロ"

        tags = ["未トリアージ層(監査追加)"]
        for dx in builder_dexs:
            tags.append(f"ビルダーperp:{dx}")
        if abs(rbuild) > abs(rmaj):
            tags.append("ビルダーperp主体")   # 実現益がmajorsよりビルダーperp優勢
        wq = None
        if pos == "高頻度MM":
            tags.append(hft_tag(cpm))
            wq = pro_grade(months, posr, pf, gl == 0, rall, rmaj, worst)
        elif pos == "プロトレーダー(本物)":
            wq = pro_grade(months, posr, pf, gl == 0, rall, rmaj, worst)
        elif pos == "alt主体プロ":
            wq = "alt主体"

        ql = ("質:" + wq) if wq else ""
        vtag = (f" / ビルダーperp実現${rbuild:,}({','.join(builder_dexs)})" if builder_dexs else "")
        head = (f"【現在の分類: {QLAB.get(pos, pos)}" + (f" / {ql}" if ql else "")
                + f" / perp実現${rperp:,}(majors${rmaj:,}{vtag}) / 最終取引{at}】")
        note = (head + f"\n【監査で追加】cacheにあったが未トリアージだった高額層(perp実現${rperp:,}/majors${rmaj:,}/全${rall:,})。"
                f"既存と同じ決定的ロジックで分類。回転{cpm}closes/月・黒字月率{posr:.0%}・履歴{months}ヶ月。"
                + ("ビルダーperp主体(majors比低=alt主体ラベルだが新興perp優位の可能性・要方向先読み精査)。" if abs(rbuild) > abs(rmaj) else ""))

        W[k] = {
            "address": k, "first_seen": today, "last_seen": today, "times_seen": 1,
            "position": pos, "wf_quality": wq, "metric_category": "triage_audit",
            "tags": sorted(tags), "mm_cpm": cpm if is_mm else None,
            "true_realized_all": rall, "true_realized_maj": rmaj,
            "current": {"win_rate": None, "total_pnl": rmaj, "dir_accuracy": None, "metric_category": "triage_audit"},
            "active_from": af, "active_to": at, "active14": bool(rec),
            "n_fills_14d": len(rec), "n_fills_14d_maj": sum(1 for f in rec if f.get("coin") in MAJ),
            "n_closes": ncl, "history": [], "notes_jp": note,
        }
        added[pos] += 1

    reg["updated_at"] = datetime.utcnow().isoformat() + "+00:00"
    json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"未トリアージ高額層を台帳化: {sum(added.values())}件  内訳={dict(added)}")
    print(f"台帳総数: {len(W)}")


if __name__ == "__main__":
    main()
