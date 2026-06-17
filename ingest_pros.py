"""nansen_candidates.json の「プロ寄り」候補（SmartMoney/機関）を台帳のプロ枠へ取り込む。

対象: ラベルが SmartMoney or 機関/ファンド の新規候補 → プロページ(pros.html)へ。
真プロトコルは取り込まない。インサイダー候補(個人)はここでは触らない。
使い方: python ingest_pros.py
"""
import json
from datetime import datetime, timezone

import config
import tagging
import step6_registry as reg6

REG = f"{config.DATA_DIR}/wallet_registry.json"
CAND = f"{config.DATA_DIR}/nansen_candidates.json"

INST = ["Galaxy", "GSR", "Abraxas", "Wintermute", "Jump", "DWF", "Amber",
        "Cumberland", "Capital", "Fund", "Flow Traders", "B2C2"]
PROTO = ["Liquidator", "HLP", "Collateral", "Deployer", "Bridge", "Spoke",
         ": Pool", "Router", "Factory", "🤖", "Proxy", "Mastercopy", "Vault"]


def kind(label):
    s = label or ""
    if any(k in s for k in PROTO):
        return None                       # プロトコルは取り込まない
    if any(k in s for k in INST):
        return "機関/ファンド(Nansen発)"
    if "Smart" in s:
        return "プロ候補(Nansen発・未精査)"
    return None                           # 個人候補はここでは対象外


def main():
    reg = json.load(open(REG, encoding="utf-8"))
    cands = json.load(open(CAND, encoding="utf-8"))["candidates"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    added = updated = 0
    by_pos = {}
    for c in cands:
        pos = kind(c.get("label"))
        if not pos:
            continue
        key = c["address"].lower()
        pnl = c.get("pnl"); roi = c.get("roi"); acct = c.get("account_value")
        ratio = c.get("ratio")
        label = c.get("label") or ""
        is_inst = pos.startswith("機関")
        tags = ["Nansen発見", "機関" if is_inst else "SmartMoney"]
        auto = tagging.derive_tags({"roi": roi, "pnl": pnl, "cashout_ratio": ratio,
                                    "labels": [label], "account_value": acct})
        note = ("【ひとことで】" + ("🏦 著名な機関/ファンド。インサイダーでなくプロの組織。"
                if is_inst else "🤓 Nansenが『常勝(Smart Money)』認定したトレーダー。たぶんプロ、未精査。") +
                f"\n【正体】{label}（Nansenラベル）" +
                f"\n【成績】通算PnL ${pnl:,.0f}・ROI {roi*100:,.0f}%・現在残高 ${acct:,.0f}" +
                ("\n【補足】残高が小さい＝稼ぎを引き上げ済の可能性（出金）。" if (acct or 0) < 50000 else ""))

        entry = reg["wallets"].get(key)
        if entry is not None:
            # 既存は position を上書きしない（インサイダー/出金疑い等の分類を守る）。SmartMoneyタグだけ付与。
            entry["tags"] = sorted(set(entry.get("tags", [])) | set(tags))
            updated += 1
            continue
        # 新規のみプロ枠で追加
        entry = {"address": c["address"], "first_seen": today, "times_seen": 1,
                 "last_seen": today, "tags": sorted(set(tags)), "notes": "", "history": []}
        reg["wallets"][key] = entry
        added += 1
        entry["position"] = pos
        entry["metric_category"] = "nansen-pro"
        entry["labels"] = [label] if label else []
        entry["roi_alltime"] = roi
        entry["nansen_checked"] = today
        entry["auto_tags"] = auto
        entry["notes_jp"] = note
        snap = {"date": today, "metric_category": "nansen-pro", "total_pnl": pnl,
                "win_rate": None, "dir_accuracy": None, "insider_likelihood": None,
                "avg_hold_h": None}
        entry["current"] = snap
        entry["history"] = [snap]
        by_pos[pos] = by_pos.get(pos, 0) + 1

    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(REG, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    reg6.render_all(reg)
    print(f"プロ枠へ取り込み: 新規{added} / 更新{updated}")
    for p, n in by_pos.items():
        print(f"  {p}: {n}")
    print(f"総登録: {len(reg['wallets'])} → pros.html 再描画")


if __name__ == "__main__":
    main()
