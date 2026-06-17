"""個人インサイダー検出（別定義A）: 大口エントリの先見性 = conviction lift。

発想: インサイダーは「確信のある時だけ大きく賭ける」。
  各エントリ(建玉)の建値からH時間後の順方向リターンをトレンド補正し、
  『大口の賭け(上位notional)が小口より systematically 当たっているか』を測る。
  → conviction_lift = 上位notionalの先取りリターン − 下位notionalの先取りリターン(同一口座・同一期間ゆえトレンド相殺)。

利確を要求しない(エントリだけ見る)・往復不要・保有型も捕捉。
出力: data/insider_conviction.json
使い方: python insider_conviction.py [--limit N] [--horizon 4,24]
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
CAND_DAYS = 420


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


def open_dir(d):
    d = (d or "").strip()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()
    return None   # 建て増し以外(Close/Reduce)は対象外


def price_at(series, t):
    """t 以前の最後の close。"""
    lo, hi = 0, len(series) - 1
    if hi < 0 or t < series[0][0]:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if series[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    return series[lo][1]


def baseline(series, H):
    """期間中の H時間リターンの平均(=ベータ/ドリフト)。"""
    rs = []
    step = max(len(series) // 500, 1)
    for i in range(0, len(series), step):
        t = series[i][0]
        p0 = series[i][1]; p1 = price_at(series, t + H * MS_H)
        if p0 and p1:
            rs.append((p1 - p0) / p0)
    return sum(rs) / len(rs) if rs else 0.0


def analyze(addr, max_pages, candle, base_cache, horizons):
    fills = fetch_fills(addr, max_pages)
    entries = []
    for f in fills:
        coin = f.get("coin")
        if coin not in config.COINS:
            continue
        d = open_dir(f.get("dir"))
        if d not in ("long", "short"):
            continue
        entries.append({"coin": coin, "dir": d, "t": int(f["time"]),
                        "notional": float(f["px"]) * float(f["sz"])})
    if len(entries) < 8:
        return {"address": addr, "n_entries": len(entries), "thin": True}

    out = {"address": addr, "n_entries": len(entries)}
    for H in horizons:
        det = []
        for e in entries:
            series = candle[e["coin"]]
            p0 = price_at(series, e["t"]); p1 = price_at(series, e["t"] + H * MS_H)
            if not p0 or not p1:
                continue
            raw = (p1 - p0) / p0
            sgn = 1 if e["dir"] == "long" else -1
            detr = sgn * (raw - base_cache[(e["coin"], H)])   # トレンド補正後・順方向
            det.append((e["notional"], detr))
        if len(det) < 8:
            continue
        det.sort(key=lambda x: x[0])               # notional昇順
        q = max(len(det) // 4, 1)
        small = det[:q]; big = det[-q:]

        def wmean(xs):
            sw = sum(n for n, _ in xs)
            return sum(n * r for n, r in xs) / sw if sw else 0.0
        big_fwd = wmean(big); small_fwd = wmean(small)
        # 上位decileの的中率(トレンド補正後>0)
        dec = max(len(det) // 10, 1)
        top = sorted(det, key=lambda x: x[0], reverse=True)[:dec]
        hit = sum(1 for _, r in top if r > 0) / len(top)
        out[f"h{H}"] = {
            "big_fwd": round(big_fwd, 4), "small_fwd": round(small_fwd, 4),
            "conviction_lift": round(big_fwd - small_fwd, 4),
            "big_hit": round(hit, 3), "n": len(det),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--maxpages", type=int, default=20)
    ap.add_argument("--horizon", default="4,24")
    ap.add_argument("--positions", default="プロトレーダー(本物),プロトレーダー(未精査),💸 出金疑い(要監視)")
    ap.add_argument("--out", default="insider_conviction.json")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizon.split(",")]

    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    poss = set(p for p in args.positions.split(",") if p)
    targets = list(dict.fromkeys(
        [e["address"] for e in reg.values() if e.get("position") in poss or e.get("wf2_checked")]))
    if args.limit:
        targets = targets[:args.limit]
    print(f"conviction lift 解析対象: {len(targets)} 件 / horizons={horizons}")

    candle = {}
    base_cache = {}
    start = NOW - CAND_DAYS * 24 * MS_H
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", start, NOW) or []
        candle[coin] = sorted([(int(x["t"]), float(x["c"])) for x in c])
        for H in horizons:
            base_cache[(coin, H)] = baseline(candle[coin], H)
    print("足:", ", ".join(f"{k}={len(v)}" for k, v in candle.items()),
          "| baseline:", {k: round(v, 4) for k, v in base_cache.items()})

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, args.maxpages, candle, base_cache, horizons)
        except Exception as e:
            r = {"address": a, "error": str(e)[:80]}
        r["position"] = reg.get(a.lower(), {}).get("position")
        r["labels"] = reg.get(a.lower(), {}).get("labels") or []
        out.append(r)
        if i % 20 == 0:
            print(f"  {i}/{len(targets)} ...")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "horizons": horizons, "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    H0 = horizons[0]
    scored = [r for r in out if r.get(f"h{H0}") and r[f"h{H0}"]["n"] >= 12]
    scored.sort(key=lambda r: r[f"h{H0}"]["conviction_lift"], reverse=True)
    print(f"\n=== conviction lift({H0}h) 上位15 / 評価可能{len(scored)}件 ===")
    print("  lift>0=大口ほど(トレンド補正後)先取りで当てている / big_hit=上位decileの的中")
    for r in scored[:15]:
        h = r[f"h{H0}"]
        print(f"  lift{h['conviction_lift']:+.4f} 大口{h['big_fwd']:+.4f} 小口{h['small_fwd']:+.4f} "
              f"的中{h['big_hit']} n{h['n']} {r['address'][:10]}.. [{r['position']}]")


if __name__ == "__main__":
    main()
