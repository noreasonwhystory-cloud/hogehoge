"""Nansen先行の発掘: Nansen perp-leaderboard から「怪しいCA」を抽出する。

学んだ signature を Nansen側で screen:
  高ROI × 高PnL × 現在残高が小さい(出金済み疑い) × 既存台帳に無い
さらに上位候補は資金源(related-wallets)を引き、ブリッジ/匿名なら容疑度UP・CEXなら追跡可。
出力: data/nansen_candidates.json
使い方: python nansen_discover.py [--days 30] [--minpnl 1000000] [--pages 6]
"""
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

import config
import nansen_client as nc

CEX = ["Binance", "Coinbase", "OKX", "Bybit", "Kraken", "Bitget", "KuCoin",
       "Nexo", "Gate", "HTX", "MEXC", "Gemini"]
BRIDGE = ["Across", "Stargate", "Hop", "Orbiter", "Socket", "Relay", "Bridge",
          "Spoke", "Refuel", "🤖", "Deployer", "Router", "Factory", "Pool", "deBridge"]


def perp_lb(date_from, date_to, page, per_page, order_by="roi", min_pnl=None):
    body = {"date": {"from": date_from, "to": date_to},
            "pagination": {"page": page, "per_page": per_page},
            "order_by": [{"field": order_by, "direction": "DESC"}]}
    if min_pnl:
        body["filters"] = {"total_pnl": {"min": min_pnl}}
    return nc._post("/perp-leaderboard", body)


def fund_class(label):
    s = label or ""
    if any(k.lower() in s.lower() for k in CEX):
        return "CEX(追跡可)"
    if any(k in s for k in BRIDGE):
        return "ブリッジ/コントラクト(隠蔽)"
    if not s:
        return "個人(ラベル無)"
    return "汎用ウォレット"


def first_funder(addr):
    for ch in config.ENRICH_CHAINS:
        r = nc.related_wallets(addr, ch)
        if isinstance(r, dict) and "_error" not in r and r.get("data"):
            f = r["data"][0]
            return f.get("address_label") or "", f.get("address") or ""
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--minpnl", type=float, default=1_000_000)
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--perpage", type=int, default=100)
    ap.add_argument("--checktop", type=int, default=15)
    args = ap.parse_args()

    to = datetime.now(timezone.utc).date()
    frm = to - timedelta(days=args.days)

    # 既存台帳（重複除外用）
    try:
        reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
        known = set(reg.keys())
    except Exception:
        known = set()

    print(f"Nansen perp-leaderboard 取得（直近{args.days}日, PnL≥${args.minpnl:,.0f}, {args.pages}ページ）...")
    rows = []
    for p in range(1, args.pages + 1):
        resp = perp_lb(frm.isoformat(), to.isoformat(), p, args.perpage, "roi", args.minpnl)
        if not (isinstance(resp, dict) and resp.get("data")):
            print(f"  page{p}: 取得失敗/終端 {str(resp)[:80]}")
            break
        rows.extend(resp["data"])
        time.sleep(config.NANSEN_SLEEP)
    print(f"  取得 {len(rows)} 行")

    # 個人でないラベル（vault/プロトコル/コントラクト）は除外
    EXCLUDE = ["Vault", "Collateral", "Deployer", "Pool", "Protocol", "Contract",
               "Factory", "🤖", "Router", "Bridge", "Spoke", "Mastercopy", "Proxy"]
    # screen: 怪しい signature
    cands = []
    excluded_nonperson = 0
    for r in rows:
        addr = (r.get("trader_address") or "").lower()
        if not addr:
            continue
        pnl = float(r.get("total_pnl") or 0)
        roi = float(r.get("roi") or 0)
        acct = float(r.get("account_value") or 0)
        ratio = pnl / acct if acct > 0 else None
        label = r.get("trader_address_label") or ""
        if any(k in label for k in EXCLUDE):       # vault/プロトコル等は個人でない→除外
            excluded_nonperson += 1
            continue
        is_new = addr not in known
        # 出金疑い: 残高が小さい or PnLが残高の10倍超。PnL(絶対額)で意味を担保。
        cashout = (acct < 50_000) or (ratio is not None and ratio >= 10)
        if pnl >= args.minpnl and cashout:
            cands.append({"address": r.get("trader_address"), "label": label,
                          "pnl": pnl, "roi": roi, "account_value": acct,
                          "ratio": round(ratio, 1) if ratio else None,
                          "is_new": is_new})
    # 新規優先 → PnL降順（残高0でROIが発散するためPnLで並べる）
    cands.sort(key=lambda c: (c["is_new"], c["pnl"]), reverse=True)
    print(f"  vault/プロトコル等を除外: {excluded_nonperson} 件")
    print(f"  怪しいsignature該当: {len(cands)} 件（うち新規 {sum(1 for c in cands if c['is_new'])}）")

    # 上位の資金源を引いて隠蔽度を判定
    print(f"  上位{args.checktop}件の資金源を照会...")
    for c in cands[:args.checktop]:
        lbl, faddr = first_funder(c["address"])
        c["funder_label"] = lbl
        c["fund_class"] = fund_class(lbl) if lbl is not None else "不明"

    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "params": {"days": args.days, "min_pnl": args.minpnl},
           "candidates": cands}
    path = f"{config.DATA_DIR}/nansen_candidates.json"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n=== Nansen発の怪しいCA 上位（新規★） ===")
    for c in cands[:args.checktop]:
        star = "★新規" if c["is_new"] else "既知"
        rs = f"{c['ratio']}x" if c["ratio"] else "残高0"
        print(f"  {star} ROI{c['roi']*100:>8,.0f}% PnL${c['pnl']:>11,.0f} 残高${c['account_value']:>10,.0f} 比{rs:>7} "
              f"資金源[{c.get('fund_class','?')}] {c['address'][:12]}.. [{c['label'] or '匿名'}]")
    print(f"\n保存 → {path}")


if __name__ == "__main__":
    main()
