"""Step7: registry の優先監視リストを Nansen REST で照会し、台帳に統合する。

対象: position が「除外/低優先」以外（cluster-A・疑惑・プロ・偽陽性）。
保存: 各エントリに labels / first_funders / counterparties / nansen_checked を付与し
      data/wallet_registry.json を更新、registry.html を再生成。
使い方: python step7_enrich_registry.py [--all] [--addr 0x..]
"""
import sys
import json
from datetime import datetime, timezone, timedelta

import config
import nansen_client as nc
import step6_registry as reg6

REGISTRY = f"{config.DATA_DIR}/wallet_registry.json"


def ok(r):
    return isinstance(r, dict) and "_error" not in r


def fetch_nansen(addr):
    out = {"labels": [], "first_funders": [], "counterparties": [], "nansen_errors": []}
    # ラベル
    for ch in config.ENRICH_CHAINS:
        r = nc.address_labels(addr, ch)
        if ok(r) and r.get("data"):
            out["labels"] = [l.get("label") or l.get("address_label") or str(l) for l in r["data"]]
            break
        if isinstance(r, dict) and "_error" in r:
            out["nansen_errors"].append({"field": "labels", "error": r["_error"]})
    # 関連ウォレット（資金源）
    for ch in config.ENRICH_CHAINS:
        r = nc.related_wallets(addr, ch)
        if ok(r) and r.get("data"):
            out["first_funders"] = [
                {"address": x.get("address"), "label": x.get("address_label"),
                 "relation": x.get("relation"), "time": x.get("block_timestamp")}
                for x in r["data"]
            ]
            break
        if isinstance(r, dict) and "_error" in r:
            out["nansen_errors"].append({"field": "related", "error": r["_error"]})
    # 取引相手（直近60日）
    to = datetime.now(timezone.utc).date()
    frm = to - timedelta(days=60)
    for ch in config.ENRICH_CHAINS:
        r = nc.counterparties(addr, ch, frm.isoformat(), to.isoformat())
        if ok(r) and r.get("data"):
            out["counterparties"] = [
                {"label": (", ".join(c["counterparty_address_label"])
                           if isinstance(c.get("counterparty_address_label"), list)
                           else c.get("counterparty_address_label")),
                 "address": c.get("counterparty_address"),
                 "volume_usd": c.get("total_volume_usd"),
                 "volume_in": c.get("volume_in_usd"),    # 相手→自分（入金）
                 "volume_out": c.get("volume_out_usd"),  # 自分→相手（出金先）
                 "count": c.get("interaction_count")}
                for c in r["data"][:10]
            ]
            break
        if isinstance(r, dict) and "_error" in r:
            out["nansen_errors"].append({"field": "counterparties", "error": r["_error"]})
    return out


def main():
    do_all = "--all" in sys.argv
    explicit = [a for a in sys.argv[1:] if a.startswith("0x")]

    reg = json.load(open(REGISTRY, encoding="utf-8"))
    wallets = reg["wallets"]

    WATCH = {"インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
             "プロトレーダー(本物)", "プロトレーダー(未精査)"}
    if explicit:
        targets = [k for k in wallets if k in {a.lower() for a in explicit}]
    elif do_all:
        targets = list(wallets)
    else:
        targets = [k for k, e in wallets.items() if e.get("position") in WATCH]
    # 既にエンリッチ済みは再取得しない（クレジット節約）。--refresh で強制再取得。
    if "--refresh" not in sys.argv and not explicit:
        before = len(targets)
        targets = [k for k in targets
                   if not (wallets[k].get("nansen_checked")
                           and (wallets[k].get("first_funders") or wallets[k].get("labels")))]
        if before != len(targets):
            print(f"  （エンリッチ済み {before - len(targets)} 件をスキップ。--refresh で再取得可）")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Nansen照会対象: {len(targets)} 件")

    def save():
        reg["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(REGISTRY, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)

    consec403 = 0
    updated = skipped = 0
    for i, key in enumerate(targets, 1):
        e = wallets[key]
        info = fetch_nansen(e["address"])
        is403 = any(str(x.get("error")) == "403" for x in info["nansen_errors"])
        got = info["labels"] or info["first_funders"] or info["counterparties"]
        if is403 and not got:
            # クレジット不足: 既存データは絶対に上書きしない（保持してスキップ）
            consec403 += 1
            skipped += 1
            print(f"  [{i}/{len(targets)}] {e['address'][:12]}.. ⚠403 スキップ(既存保持)")
            if consec403 >= 5:
                print("  ⚠ 403が連続。クレジット枯渇とみなし中断（ここまで逐次保存済み）。")
                break
            continue
        consec403 = 0
        # 取得できたフィールドのみ更新（空で良データを潰さない）
        if info["labels"]:
            e["labels"] = info["labels"]
        if info["first_funders"]:
            e["first_funders"] = info["first_funders"]
        if info["counterparties"]:
            e["counterparties"] = info["counterparties"]
        e["nansen_checked"] = today
        e["nansen_errors"] = info["nansen_errors"] or []
        updated += 1
        labs = ", ".join(info["labels"]) or "ラベル無"
        ff = ", ".join((f.get("label") or (f.get("address") or "")[:10]) for f in info["first_funders"]) or "—"
        print(f"  [{i}/{len(targets)}] {e['address'][:12]}.. [{labs}] 資金源:{ff}")
        if i % 25 == 0:        # 逐次保存（途中停止でも進捗が残る）
            save()
            print(f"    …{i}件まで保存")

    save()
    reg6.render_all(reg)
    print(f"完了 → {REGISTRY} / 更新{updated} / 403スキップ{skipped}（既存データは保持）")


if __name__ == "__main__":
    main()
