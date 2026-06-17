"""個人インサイダー検出（別定義）: 利益集中度 ＋ 完璧エントリ。

往復(利確)を要求しない。発想:
  インサイダーの利益は「少数の異常に上手いデカ勝ち」に集中し、その大勝ちの建玉は
  『局所の底/天井の直前に大口で入る』。グラインド(薄利多売)やトレンド途中乗りと峻別する。

各ウォレットを HL約定からポジション単位(建て→フラット)に復元し:
  - 利益集中度: top1/top3/top5 トレードが総実現益に占める割合
  - 完璧エントリ: 上位益トレードの建値が、その局面の安値/高値からどれだけ近い位置か(0-1, 1=完璧)
  - 先取りリターン: 建ててから順方向にどれだけ動いたか
出力: data/insider_concentration.json
使い方: python insider_concentration.py [--limit N] [--positions A,B] [--maxpages 20]
"""
import sys
import json
import time
import argparse
from datetime import datetime, timezone
from collections import defaultdict

import config
import hl_client

MS_H = 3600 * 1000
NOW = int(time.time() * 1000)
EPS = 1e-9
CTX_BEFORE_H = 6           # 建玉時刻の何時間前から局面を見るか
CAND_DAYS = 420            # 足キャッシュの遡及日数


def fetch_fills(addr, max_pages):
    out, cur = [], 0
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
    seen, ded = set(), []
    for f in out:
        if f.get("tid") in seen:
            continue
        seen.add(f.get("tid")); ded.append(f)
    return ded


def reconstruct_positions(fills):
    """coinごとに 建て→フラット を1トレードに復元。"""
    byc = defaultdict(list)
    for f in fills:
        if f.get("coin") in config.COINS:
            byc[f["coin"]].append(f)
    trades = []
    for coin, fs in byc.items():
        fs.sort(key=lambda x: int(x["time"]))
        cur = None
        for f in fs:
            t = int(f["time"]); px = float(f["px"]); sz = float(f["sz"])
            signed = sz if f.get("side") == "B" else -sz
            before = float(f.get("startPosition", 0) or 0)
            after = before + signed
            cpnl = float(f.get("closedPnl", 0) or 0)
            if abs(before) < EPS and abs(after) > EPS:      # フラット→建て(新規)
                cur = {"coin": coin, "dir": "long" if after > 0 else "short",
                       "t_open": t, "entry_notional": 0.0, "entry_sz": 0.0,
                       "realized": 0.0, "t_close": t, "peak": abs(after)}
            if cur is None:
                # 取得窓の途中から始まった建玉(開始不明) → 簡易に開始
                cur = {"coin": coin, "dir": "long" if after >= 0 else "short",
                       "t_open": t, "entry_notional": 0.0, "entry_sz": 0.0,
                       "realized": 0.0, "t_close": t, "peak": abs(after)}
            # 建て増し(ポジ方向と同符号の約定)を entry に計上
            same_dir = (signed > 0 and cur["dir"] == "long") or (signed < 0 and cur["dir"] == "short")
            if same_dir:
                cur["entry_notional"] += px * sz
                cur["entry_sz"] += sz
            cur["realized"] += cpnl
            cur["t_close"] = t
            cur["peak"] = max(cur["peak"], abs(after))
            if abs(after) < EPS:                            # フラット→クローズ確定
                cur["entry_px"] = (cur["entry_notional"] / cur["entry_sz"]) if cur["entry_sz"] else px
                trades.append(cur); cur = None
        if cur is not None:                                 # 取得窓末で未クローズ
            cur["entry_px"] = (cur["entry_notional"] / cur["entry_sz"]) if cur["entry_sz"] else None
            cur["open_at_end"] = True
            trades.append(cur)
    return trades


def entry_precision(coin, tr, candle_cache):
    """大勝ちトレードの建値が局面の底/天井にどれだけ近いか。1=完璧, 0=最悪。"""
    series = candle_cache.get(coin)
    if not series or tr.get("entry_px") is None:
        return None
    lo_t = tr["t_open"] - CTX_BEFORE_H * MS_H
    hi_t = tr["t_close"]
    win = [(t, h, l) for (t, h, l) in series if lo_t <= t <= hi_t]
    if len(win) < 2:
        return None
    hi = max(h for _, h, _ in win); lo = min(l for _, _, l in win)
    if hi - lo < EPS:
        return None
    e = tr["entry_px"]
    pctile = (e - lo) / (hi - lo)          # 0=安値, 1=高値
    if tr["dir"] == "long":
        perfect = 1 - pctile               # 安値で買うほど完璧
        fwd = (hi - e) / e                 # 建値からの順方向最大上昇
    else:
        perfect = pctile                   # 高値で売るほど完璧
        fwd = (e - lo) / e
    return {"perfect": round(perfect, 3), "entry_pctile": round(pctile, 3),
            "fwd_favorable": round(fwd, 4),
            "event_date": datetime.fromtimestamp(tr["t_open"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")}


def analyze(addr, max_pages, candle_cache):
    fills = fetch_fills(addr, max_pages)
    if not fills:
        return None
    trades = reconstruct_positions(fills)
    wins = [t for t in trades if t["realized"] > 0]
    total_real = sum(t["realized"] for t in trades)
    total_win = sum(t["realized"] for t in wins)
    if total_win <= 0:
        return {"address": addr, "n_trades": len(trades), "total_realized": round(total_real),
                "no_profit": True}
    wins.sort(key=lambda t: t["realized"], reverse=True)
    base = total_win                       # 総勝ち益を分母に(0-1)。「勝ち益のうち上位が占める割合」
    top1 = wins[0]["realized"] / base
    top3 = sum(t["realized"] for t in wins[:3]) / base
    top5 = sum(t["realized"] for t in wins[:5]) / base
    # 純益効率: 純益÷総勝ち益。低い=勝ちを負けで打ち消すグラインド/MM、高い=効率的な少数kill
    net_eff = round(total_real / total_win, 3)
    # 上位益トレードの完璧エントリ精度
    top_detail = []
    for tr in wins[:5]:
        prec = entry_precision(tr["coin"], tr, candle_cache)
        top_detail.append({"coin": tr["coin"], "dir": tr["dir"],
                           "realized": round(tr["realized"]),
                           "entry_px": round(tr["entry_px"], 4) if tr.get("entry_px") else None,
                           "open_at_end": tr.get("open_at_end", False),
                           **(prec or {})})
    precs = [d["perfect"] for d in top_detail if d.get("perfect") is not None]
    fwds = [d["fwd_favorable"] for d in top_detail if d.get("fwd_favorable") is not None]
    return {
        "address": addr, "n_trades": len(trades), "n_wins": len(wins),
        "total_realized": round(total_real), "total_win": round(total_win),
        "top1_share": round(top1, 3), "top3_share": round(top3, 3), "top5_share": round(top5, 3),
        "net_efficiency": net_eff,
        "mean_top_perfect": round(sum(precs) / len(precs), 3) if precs else None,
        "max_top_perfect": round(max(precs), 3) if precs else None,
        "mean_top_fwd": round(sum(fwds) / len(fwds), 4) if fwds else None,
        "top_trades": top_detail,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--maxpages", type=int, default=20)
    ap.add_argument("--positions", default="プロトレーダー(本物),プロトレーダー(未精査),💸 出金疑い(要監視)")
    ap.add_argument("--include-wf2", action="store_true", default=True,
                    help="別視点再精査済(旧疑惑10件)も含める")
    ap.add_argument("--out", default="insider_concentration.json")
    args = ap.parse_args()

    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    poss = set(p for p in args.positions.split(",") if p)
    targets = []
    for e in reg.values():
        if e.get("position") in poss or (args.include_wf2 and e.get("wf2_checked")):
            targets.append(e["address"])
    targets = list(dict.fromkeys(targets))
    if args.limit:
        targets = targets[:args.limit]
    print(f"利益集中度+完璧エントリ 解析対象: {len(targets)} 件")

    # 足キャッシュ(1h, 各coin)
    candle_cache = {}
    start = NOW - CAND_DAYS * 24 * MS_H
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", start, NOW) or []
        candle_cache[coin] = sorted([(int(x["t"]), float(x["h"]), float(x["l"])) for x in c])
    print(f"足キャッシュ: " + ", ".join(f"{k}={len(v)}" for k, v in candle_cache.items()))

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, args.maxpages, candle_cache)
        except Exception as e:
            r = {"address": a, "error": str(e)[:80]}
        if r:
            r["position"] = reg.get(a.lower(), {}).get("position")
            r["labels"] = reg.get(a.lower(), {}).get("labels") or []
            out.append(r)
        if i % 20 == 0:
            print(f"  {i}/{len(targets)} ...")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ランキング: 集中度が高く(top3>=0.6) かつ 完璧エントリ(mean_top_perfect>=0.7) を上位に
    scored = [r for r in out if r.get("top3_share") is not None and r.get("mean_top_perfect") is not None
              and r.get("n_trades", 0) >= 3]
    scored.sort(key=lambda r: (r["top3_share"] * r["mean_top_perfect"]), reverse=True)
    print(f"\n=== 候補(集中度×完璧エントリ) 上位15 / 評価可能 {len(scored)}件 ===")
    print("  top3=利益集中度, perfect=上位益トレードの建値精度(1=底/天で建てた), fwd=順方向の伸び")
    for r in scored[:15]:
        print(f"  集中{r['top3_share']:.2f} 完璧{r['mean_top_perfect']:.2f} 伸び{r.get('mean_top_fwd')} "
              f"trades{r['n_trades']} 実現${r['total_realized']:,} {r['address'][:10]}.. [{r['position']}]")


if __name__ == "__main__":
    main()
