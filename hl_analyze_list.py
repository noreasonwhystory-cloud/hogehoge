r"""指定アドレス群を Hyperliquid 約定で一括フォレンジック分析する。

Nansen発の出金候補(残高<$1k の個人)が「本当に取引で稼いだか」をHL側で検証:
  実現損益(closedPnl) / 勝率 / majors比 / 取引数 / 活動期間 / 平均保有 / 現在建玉
出力: data/hl_list_analysis.json （実現損益 降順）
使い方: python hl_analyze_list.py [--maxpages 8] [--limit N]
"""
import sys
import json
import time
import argparse
from datetime import datetime, timezone
from collections import Counter

import config
import hl_client

MS = 1000
NOW = int(time.time() * 1000)


def fetch_fills_capped(addr, max_pages):
    """userFillsByTime を最大 max_pages ページ(各2000)まで取得（高頻度の暴走防止）。"""
    out, cur, capped = [], 0, False
    for _ in range(max_pages):
        chunk = hl_client._post_info({"type": "userFillsByTime", "user": addr,
                                      "startTime": cur, "endTime": NOW})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 2000:
            break
        last = chunk[-1]["time"]
        if last <= cur:
            break
        cur = last + 1
    else:
        capped = True
    # tid 重複除去
    seen, ded = set(), []
    for f in out:
        if f.get("tid") in seen:
            continue
        seen.add(f.get("tid")); ded.append(f)
    return ded, capped


def analyze(addr, max_pages):
    fills, capped = fetch_fills_capped(addr, max_pages)
    st = hl_client.clearinghouse_state(addr)
    acct = float((st or {}).get("marginSummary", {}).get("accountValue", 0) or 0)
    if not fills:
        return {"address": addr, "n_fills": 0, "account_value": acct}
    closes = [f for f in fills if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
    wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)
    realized = sum(float(f.get("closedPnl", 0) or 0) for f in fills)
    maj = [f for f in fills if f.get("coin") in config.COINS]
    maj_real = sum(float(f.get("closedPnl", 0) or 0) for f in maj)
    t0 = min(int(f["time"]) for f in fills); t1 = max(int(f["time"]) for f in fills)
    coins = Counter(f["coin"] for f in fills)
    # 平均保有
    durations, opent = [], {}
    for f in sorted(fills, key=lambda x: int(x["time"])):
        c = f["coin"]; t = int(f["time"]); sz = float(f["sz"])
        signed = sz if f.get("side") == "B" else -sz
        before = float(f.get("startPosition", 0) or 0); after = before + signed
        eps = 1e-9
        if (abs(before) >= eps and abs(after) < eps):
            if c in opent:
                durations.append(t - opent.pop(c))
        elif abs(before) < eps and abs(after) >= eps:
            opent[c] = t
    avg_hold = round(sum(durations) / len(durations) / 3600000, 2) if durations else None
    return {
        "address": addr, "account_value": round(acct),
        "n_fills": len(fills), "capped": capped,
        "n_closes": len(closes), "win_rate": round(wins / len(closes), 4) if closes else None,
        "realized_pnl": round(realized), "majors_fills": len(maj),
        "majors_realized": round(maj_real),
        "majors_pct": round(len(maj) / len(fills), 2),
        "active_from": datetime.fromtimestamp(t0 / 1000, timezone.utc).strftime("%Y-%m-%d"),
        "active_to": datetime.fromtimestamp(t1 / 1000, timezone.utc).strftime("%Y-%m-%d"),
        "avg_hold_h": avg_hold, "top_coins": dict(coins.most_common(4)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--maxpages", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--candidates", action="store_true",
                    help="台帳の『Nansen候補(HL未検証)』(=元プロ枠に戻した383件)を対象にする")
    ap.add_argument("--out", default="hl_list_analysis.json")
    args = ap.parse_args()

    if args.candidates:
        reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
        targets = [{"address": e["address"], "label": (e.get("labels") or [""])[0],
                    "pnl": None, "account_value": None}
                   for e in reg.values() if e.get("position") == "Nansen候補(HL未検証)"]
        print(f"HL分析対象: {len(targets)} 件（Nansen候補・元プロ枠の383件）")
    else:
        d = json.load(open(f"{config.DATA_DIR}/nansen_candidates.json", encoding="utf-8"))["candidates"]
        PROTO = ["Liquidator", "HLP", "Collateral", "Deployer", "Bridge", "Spoke",
                 ": Pool", "Router", "Factory", "🤖", "Proxy", "Mastercopy", "Vault"]
        INST = ["Galaxy", "GSR", "Abraxas", "Wintermute", "Jump", "DWF", "Amber",
                "Cumberland", "Capital", "Fund", "Flow Traders", "B2C2"]

        def is_indiv(c):
            s = c.get("label") or ""
            return not (any(k in s for k in PROTO) or any(k in s for k in INST) or "Smart" in s)

        targets = [c for c in d if c.get("is_new") and is_indiv(c)
                   and (c.get("account_value") or 0) < 1000]
        print(f"HL分析対象: {len(targets)} 件（個人・残高<$1k）")
    if args.limit:
        targets = targets[:args.limit]

    results = []
    for i, c in enumerate(targets, 1):
        try:
            r = analyze(c["address"], args.maxpages)
        except Exception as e:
            r = {"address": c["address"], "error": str(e)[:60]}
        r["nansen_label"] = c.get("label")
        r["lb_pnl"] = c.get("pnl")
        results.append(r)
        if i % 100 == 0:
            print(f"  {i}/{len(targets)} ...")

    results.sort(key=lambda r: r.get("realized_pnl", 0) or 0, reverse=True)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "n": len(results), "wallets": results}
    path = f"{config.DATA_DIR}/{args.out}"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    have = [r for r in results if r.get("n_fills", 0) > 0]
    print(f"完了 → {path}")
    print(f"  約定取得できた: {len(have)}/{len(results)}")
    print("  実現損益トップ10（HL実測）:")
    for r in results[:10]:
        print(f"    実現${r.get('realized_pnl',0):>12,} 勝率{r.get('win_rate')} majors{r.get('majors_pct')} "
              f"{r.get('active_from')}〜{r.get('active_to')} {r['address'][:10]}.. [{(r.get('nansen_label') or '')[:22]}]")


if __name__ == "__main__":
    main()
