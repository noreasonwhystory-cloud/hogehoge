"""Step2: Nansen REST API で上位容疑ウォレットに正体・資金源・取引相手を付与する。

入力: data/ranked.json   出力: data/dossiers.json
HL アドレスは EVM アドレスなので arbitrum(HLブリッジ)→ethereum の順に照会。
"""
import json
import argparse
from datetime import datetime, timezone, timedelta

import config
import nansen_client as nc


def ok(resp):
    return isinstance(resp, dict) and "_error" not in resp


def first_nonempty(address, fn):
    """ENRICH_CHAINS を順に試し、data が空でない最初の結果(chain, data, err)を返す。
    エラー（クレジット不足/レート制限等）は握り潰さず err に載せて返す。"""
    last_err = None
    for chain in config.ENRICH_CHAINS:
        resp = fn(address, chain)
        if ok(resp):
            data = resp.get("data", [])
            if data:
                return chain, data, None
        elif isinstance(resp, dict) and "_error" in resp:
            last_err = resp  # 403クレジット不足/429等
    return None, [], last_err


def enrich_one(address):
    dossier = {"address": address}
    errors = []

    # ラベル（正体）
    chain, labels, err = first_nonempty(address, nc.address_labels)
    dossier["labels"] = labels
    dossier["labels_chain"] = chain
    if err:
        errors.append(("labels", err))

    # 関連ウォレット（First Funder = 資金源・同一主体）
    chain, rel, err = first_nonempty(address, nc.related_wallets)
    dossier["related_wallets"] = rel
    dossier["related_chain"] = chain
    if err:
        errors.append(("related_wallets", err))
    dossier["first_funders"] = [
        r for r in rel if str(r.get("relation", "")).lower().find("funder") >= 0
    ]

    # 取引相手（直近30日）
    to = datetime.now(timezone.utc).date()
    frm = to - timedelta(days=30)
    cp_data = []
    cp_chain = None
    for ch in config.ENRICH_CHAINS:
        resp = nc.counterparties(address, ch, frm.isoformat(), to.isoformat())
        if ok(resp) and resp.get("data"):
            cp_data = resp["data"]
            cp_chain = ch
            break
    dossier["counterparties"] = cp_data[:15]
    dossier["counterparties_chain"] = cp_chain

    dossier["nansen_errors"] = [{"field": f, "error": e.get("_error"),
                                 "body": e.get("_body", "")[:120]} for f, e in errors]
    return dossier


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=config.ENRICH_TOP_K)
    args = ap.parse_args()

    ranked = json.load(open(f"{config.DATA_DIR}/ranked.json", encoding="utf-8"))
    wallets = ranked["wallets"][:args.top]
    print(f"Nansen エンリッチ対象: {len(wallets)} 件")

    dossiers = []
    for i, w in enumerate(wallets, 1):
        d = enrich_one(w["address"])
        # HL 成績とマージ
        d["hl"] = {k: w.get(k) for k in (
            "insider_score", "lb_pnl", "lb_roi", "account_value",
            "realized_pnl", "unrealized_pnl", "total_pnl",
            "win_rate", "dir_accuracy", "n_fills", "n_closes", "n_opens",
            "event_lead_notional", "lead_examples", "held_positions", "likely_mm",
        )}
        dossiers.append(d)
        nlab = len(d["labels"]); nrel = len(d["related_wallets"])
        funders = ", ".join(f.get("address_label", "?") for f in d["first_funders"][:3])
        warn = ""
        if d["nansen_errors"]:
            codes = {str(e["error"]) for e in d["nansen_errors"]}
            warn = f"  ⚠ Nansenエラー {codes}（クレジット不足の可能性）"
        print(f"  [{i}/{len(wallets)}] {w['address'][:12]}.. "
              f"labels={nlab} related={nrel} funders=[{funders}]{warn}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dossiers": dossiers,
    }
    path = f"{config.DATA_DIR}/dossiers.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"完了 → {path}")


if __name__ == "__main__":
    main()
