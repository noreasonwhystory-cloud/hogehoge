"""HL行動検証(insider_verified.json)の結果を台帳へ振り分ける。

verdict で position を決定（ラベルでなく行動で分類）:
  insider-leaning → インサイダー疑惑(要監視)  ※的中率も併記し人間レビュー可能に
  pro             → プロトレーダー(本物)
  保留/?          → Nansen候補(HL未検証)（中立のまま）
補助データ: hl_list_analysis.json(実現益/残高/期間) + nansen_candidates.json(ラベル/LB_PnL)
使い方: python ingest_verified.py
"""
import json
from datetime import datetime, timezone

import config
import step6_registry as reg6


def usd(x):
    try:
        return f"${x:,.0f}"
    except (TypeError, ValueError):
        return "不明"


def main():
    ver = json.load(open(f"{config.DATA_DIR}/insider_verified.json", encoding="utf-8"))["wallets"]
    hl = {r["address"].lower(): r for r in
          json.load(open(f"{config.DATA_DIR}/hl_list_analysis.json", encoding="utf-8"))["wallets"]}
    nc = {c["address"].lower(): c for c in
          json.load(open(f"{config.DATA_DIR}/nansen_candidates.json", encoding="utf-8"))["candidates"]}
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def route(v):
        vd = v.get("verdict", "")
        if vd.startswith("insider"):
            return "インサイダー疑惑(要監視)", ["HL先行検出", "Nansen発見"]
        if vd.startswith("pro"):
            return "プロトレーダー(本物)", ["HL検証済プロ", "Nansen発見"]
        return "Nansen候補(HL未検証)", ["Nansen発見"]

    cnt = {}
    for v in ver:
        if v.get("majors", 0) == 0 or "error" in v:
            continue
        key = v["address"].lower()
        pos, tags = route(v)
        h = hl.get(key, {}); c = nc.get(key, {})
        label = (c.get("label") or "")
        el = v.get("event_lead_notional", 0); dacc = v.get("dir_accuracy"); win = v.get("win_rate")
        real = h.get("realized_pnl") or v.get("realized_pnl")
        # notes（行動の根拠を平易に・的中率も正直に）
        if pos.startswith("インサイダー"):
            one = ("🔴 急変イベントの直前に大口を先行建玉していた（HL実測）。インサイダー疑い。"
                   + (f" ただし方向的中率は{dacc:.0%}と高くなく、先行は出来高由来の偶然の可能性も残る（要人間レビュー）。"
                      if (dacc is not None and dacc < 0.6) else ""))
        elif pos.startswith("プロ"):
            one = "🟢 多数の取引で安定して稼ぎ、急変直前の先行建玉は検出されず＝実力のプロ（HL行動で確認）。"
        else:
            one = "⚪ HL行動では insider/pro を確定できず（保留）。"
        note = (f"【ひとことで】{one}"
                f"\n【HL実測】実現益{usd(real)}・勝率{win}・方向的中率{dacc}・急変先行{usd(el)}"
                f"\n【活動期間】{v.get('active_from')}〜{v.get('active_to')}（majors {v.get('majors')}約定）"
                f"\n【正体(Nansen)】{label or '匿名'}")

        entry = reg["wallets"].get(key)
        if entry is None:
            entry = {"address": v["address"], "first_seen": today, "times_seen": 0,
                     "tags": [], "notes": "", "history": []}
            reg["wallets"][key] = entry
        entry["last_seen"] = today
        entry["times_seen"] = entry.get("times_seen", 0) + 1
        entry["position"] = pos
        entry["metric_category"] = "hl-verified"
        entry["labels"] = [label] if label else []
        entry["roi_alltime"] = c.get("roi")
        entry["n_fills_14d"] = h.get("n_fills")
        entry["nansen_checked"] = entry.get("nansen_checked") or today
        entry["tags"] = sorted(set(entry.get("tags", [])) | set(tags))
        entry["notes_jp"] = note
        entry["current"] = {"date": today, "metric_category": "hl-verified",
                            "win_rate": win, "dir_accuracy": dacc, "total_pnl": real,
                            "event_lead_notional": el, "insider_likelihood": None,
                            "avg_hold_h": None}
        entry["lead_examples"] = v.get("lead_examples", [])
        cnt[pos] = cnt.get(pos, 0) + 1

    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(reg, open(f"{config.DATA_DIR}/wallet_registry.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    reg6.render_all(reg)
    print("台帳へ振り分け:")
    for p, n in cnt.items():
        print(f"  {p}: {n}")
    print(f"総登録: {len(reg['wallets'])}")


if __name__ == "__main__":
    main()
