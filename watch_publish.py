"""リアルタイム監視デーモン用の監視リストを台帳から書き出す。

除外/低優先・偽陽性 以外（プロ本物/alt主体/高頻度MM/弱い疑惑）を対象に
{address,label,position,notify} を data/watch_addresses.json へ。
notify=True は『エントリー/クローズを即通知』する層(本物/alt/弱い疑惑)。MMはサイト表示のみ(大口は別途検知)。
GCPデーモンはこのJSONを(raw GitHub等で)取得して購読する。
"""
import json
import config

EXCLUDE = {"除外/低優先", "偽陽性(数値疑惑→否定)", "プロトレーダー(未精査)"}
NOTIFY = {"プロトレーダー(本物)", "alt主体プロ", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
          "インサイダー疑惑(要監視)"}


def main():
    W = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    out = []
    for k, e in W.items():
        pos = e.get("position")
        if pos in EXCLUDE:
            continue
        labels = e.get("labels") or []
        label = (labels[0] if labels else "") or pos
        al = e.get("alpha") or {}
        out.append({"address": k, "label": str(label)[:40], "position": pos,
                    "wf_quality": e.get("wf_quality"), "notify": pos in NOTIFY,
                    "active14": bool(e.get("active14")), "first_seen": e.get("first_seen"),
                    "alpha_tag": al.get("tag"), "alpha24h": al.get("alpha24h")})
    out.sort(key=lambda w: (not w["notify"], w["position"]))
    json.dump(out, open(f"{config.DATA_DIR}/watch_addresses.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    n_notify = sum(1 for w in out if w["notify"])
    print(f"watch_addresses.json 書き出し: {len(out)}件 (通知対象 {n_notify} / サイトのみ {len(out)-n_notify})")


if __name__ == "__main__":
    main()
