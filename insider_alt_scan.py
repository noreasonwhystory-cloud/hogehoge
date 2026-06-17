"""個人インサイダー検出（別定義C）: alt/全perpへ拡張した 利益集中度＋完璧エントリ。

majors(BTC/ETH/SOL)は効率市場で情報優位が出にくい。インサイダーの本場は alt/新規perp。
そこで対象銘柄をウォレットが触る『全perp』へ拡張し、
  - どの銘柄で大勝ちしているか(alt比率)
  - その大勝ちトレードが局所の底/天井の直前に大口で入っているか(完璧エントリ)
を測る。利確不要・往復不要。
出力: data/insider_alt_scan.json
使い方: python insider_alt_scan.py [--limit N] [--maxpages 20]
"""
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
CTX_BEFORE_H = 6
CAND_DAYS = 420
MAJORS = set(config.COINS)

_cache = {}
_missing = set()


def get_series(coin):
    """coinの1h(t,h,l)をlazy取得・キャッシュ。取得不能はNone。"""
    if coin in _cache:
        return _cache[coin]
    if coin in _missing:
        return None
    try:
        c = hl_client.candles(coin, "1h", NOW - CAND_DAYS * 24 * MS_H, NOW) or []
    except Exception:
        c = []
    if not c:
        _missing.add(coin)
        return None
    _cache[coin] = sorted([(int(x["t"]), float(x["h"]), float(x["l"])) for x in c])
    return _cache[coin]


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
    """全coinで 建て→フラット を1トレードに復元。"""
    byc = defaultdict(list)
    for f in fills:
        if f.get("coin"):
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
            if abs(before) < EPS and abs(after) > EPS:
                cur = {"coin": coin, "dir": "long" if after > 0 else "short", "t_open": t,
                       "entry_notional": 0.0, "entry_sz": 0.0, "realized": 0.0, "t_close": t, "peak": abs(after)}
            if cur is None:
                cur = {"coin": coin, "dir": "long" if after >= 0 else "short", "t_open": t,
                       "entry_notional": 0.0, "entry_sz": 0.0, "realized": 0.0, "t_close": t, "peak": abs(after)}
            same = (signed > 0 and cur["dir"] == "long") or (signed < 0 and cur["dir"] == "short")
            if same:
                cur["entry_notional"] += px * sz
                cur["entry_sz"] += sz
            cur["realized"] += cpnl
            cur["t_close"] = t
            cur["peak"] = max(cur["peak"], abs(after))
            if abs(after) < EPS:
                cur["entry_px"] = (cur["entry_notional"] / cur["entry_sz"]) if cur["entry_sz"] else px
                trades.append(cur); cur = None
        if cur is not None:
            cur["entry_px"] = (cur["entry_notional"] / cur["entry_sz"]) if cur["entry_sz"] else None
            cur["open_at_end"] = True
            trades.append(cur)
    return trades


def entry_precision(tr):
    series = get_series(tr["coin"])
    if not series or tr.get("entry_px") is None:
        return None
    lo_t = tr["t_open"] - CTX_BEFORE_H * MS_H
    win = [(t, h, l) for (t, h, l) in series if lo_t <= t <= tr["t_close"]]
    if len(win) < 2:
        return None
    hi = max(h for _, h, _ in win); lo = min(l for _, _, l in win)
    if hi - lo < EPS:
        return None
    e = tr["entry_px"]; pctile = (e - lo) / (hi - lo)
    perfect = (1 - pctile) if tr["dir"] == "long" else pctile
    fwd = ((hi - e) / e) if tr["dir"] == "long" else ((e - lo) / e)
    return {"perfect": round(perfect, 3), "fwd_favorable": round(fwd, 4),
            "event_date": datetime.fromtimestamp(tr["t_open"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")}


def analyze(addr, max_pages):
    fills = fetch_fills(addr, max_pages)
    if not fills:
        return None
    trades = reconstruct_positions(fills)
    wins = [t for t in trades if t["realized"] > 0]
    total_win = sum(t["realized"] for t in wins)
    total_real = sum(t["realized"] for t in trades)
    if total_win <= 0:
        return {"address": addr, "n_trades": len(trades), "no_profit": True}
    wins.sort(key=lambda t: t["realized"], reverse=True)
    alt_win = sum(t["realized"] for t in wins if t["coin"] not in MAJORS)
    top = wins[:5]
    detail = []
    for tr in top:
        prec = entry_precision(tr)
        detail.append({"coin": tr["coin"], "is_alt": tr["coin"] not in MAJORS, "dir": tr["dir"],
                       "realized": round(tr["realized"]), "open_at_end": tr.get("open_at_end", False),
                       **(prec or {})})
    precs = [d["perfect"] for d in detail if d.get("perfect") is not None]
    return {
        "address": addr, "n_trades": len(trades), "n_wins": len(wins),
        "total_realized": round(total_real), "total_win": round(total_win),
        "alt_win_share": round(alt_win / total_win, 3),
        "top1_share": round(wins[0]["realized"] / total_win, 3),
        "top3_share": round(sum(t["realized"] for t in wins[:3]) / total_win, 3),
        "net_efficiency": round(total_real / total_win, 3),
        "mean_top_perfect": round(sum(precs) / len(precs), 3) if precs else None,
        "top_coins_win": list(dict.fromkeys(t["coin"] for t in wins[:5])),
        "top_trades": detail,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--maxpages", type=int, default=20)
    ap.add_argument("--positions", default="プロトレーダー(本物),プロトレーダー(未精査),💸 出金疑い(要監視)")
    ap.add_argument("--out", default="insider_alt_scan.json")
    args = ap.parse_args()

    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    poss = set(p for p in args.positions.split(",") if p)
    targets = list(dict.fromkeys(
        [e["address"] for e in reg.values() if e.get("position") in poss or e.get("wf2_checked")]))
    if args.limit:
        targets = targets[:args.limit]
    print(f"alt拡張(全perp 集中度+完璧エントリ) 解析対象: {len(targets)} 件")

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, args.maxpages)
        except Exception as e:
            r = {"address": a, "error": str(e)[:80]}
        if r:
            r["position"] = reg.get(a.lower(), {}).get("position")
            r["labels"] = reg.get(a.lower(), {}).get("labels") or []
            out.append(r)
        if i % 20 == 0:
            print(f"  {i}/{len(targets)} ... (足cache {len(_cache)} coin)")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # alt中心で大勝ち集中＆完璧エントリの層を上位に
    scored = [r for r in out if r.get("mean_top_perfect") is not None and r.get("n_trades", 0) >= 3]
    scored.sort(key=lambda r: (r["alt_win_share"] * r["top3_share"] * r["mean_top_perfect"]), reverse=True)
    print(f"\n=== alt集中×完璧エントリ 上位15 / 評価可能{len(scored)}件 ===")
    print("  alt比={alt勝ち益÷総勝ち益} 集中=top3 完璧=上位益建値精度")
    for r in scored[:15]:
        print(f"  alt比{r['alt_win_share']:.2f} 集中{r['top3_share']:.2f} 完璧{r['mean_top_perfect']:.2f} "
              f"実現${r['total_realized']:,} 銘柄{r['top_coins_win']} {r['address'][:10]}.. [{r['position']}]")


if __name__ == "__main__":
    main()
