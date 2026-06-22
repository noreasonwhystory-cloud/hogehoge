"""厳密版『含み損』判定＋過去塩漬けの段階タグ付け。

約定履歴から建玉(建て→フラット)を復元し、保有中の1h足closeで
「含み損 >= 口座の10%」が連続した最長日数を求める。
段階タグ: 塩漬け:1日 / 2日 / 3日 / 4日 / 5日 / 6日 / 7日以上（最長連続塩漬け期間で1つ付与）。
口座価値は現在値(clearinghouse)を基準（履歴口座値はAPIで安価に取れぬための近似）。
勝率/方向的中(majors)も併算し『高勝率×高的中×現在含み損なし×過去にも7日塩漬け無し』を厳密通過とする。
出力: data/insider_clean_strict_bag.json  ＋  --apply-tags で台帳に塩漬けタグを書き込み再生成
使い方: python insider_clean_strict_bag.py [--scan-all] [--apply-tags] [--limit N]
"""
import json
import time
import bisect
import argparse
from datetime import datetime, timezone
from collections import defaultdict

import config
import hl_client
import hl_fills_cache as fc
import hl_candle_cache as cc

MS_H = 3600 * 1000
NOW = int(time.time() * 1000)
EPS = 1e-9
CAND_DAYS = 800
WIN_MIN, DIR_MIN, MIN_CLOSES, MIN_DAYS = 0.70, 0.65, 10, 7
BAG_PCT = 0.10           # 口座の10%超の含み損
MIN_SOAK_H = 24          # 1日(24h)未満の含み損は塩漬けと数えない

_cache, _ts, _missing = {}, {}, set()


def get_hlc(coin):
    if coin in _cache:
        return _cache[coin]
    if coin in _missing:
        return None
    try:
        c = cc.get_candles(coin, "1h", NOW - CAND_DAYS * 24 * MS_H, NOW) or []   # 永続candleキャッシュ(builder可)
    except Exception:
        c = []
    if not c:
        _missing.add(coin); return None
    ser = sorted([(int(x["t"]), float(x["c"])) for x in c])
    _cache[coin] = ser
    _ts[coin] = [t for t, _ in ser]
    return ser


def fetch_fills(addr, max_pages=25):
    out, cur = [], 0
    for _ in range(max_pages):
        ch = hl_client._post_info({"type": "userFillsByTime", "user": addr, "startTime": cur, "endTime": NOW})
        if not ch:
            break
        out.extend(ch)
        if len(ch) < 2000:
            break
        last = ch[-1]["time"]
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
    return None


def reconstruct(fills):
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
            before = float(f.get("startPosition", 0) or 0); after = before + signed
            if abs(before) < EPS and abs(after) > EPS:
                cur = {"coin": coin, "dir": "long" if after > 0 else "short", "t_open": t,
                       "en": 0.0, "ensz": 0.0, "t_close": t, "peak": abs(after)}
            if cur is None:
                cur = {"coin": coin, "dir": "long" if after >= 0 else "short", "t_open": t,
                       "en": 0.0, "ensz": 0.0, "t_close": t, "peak": abs(after)}
            same = (signed > 0 and cur["dir"] == "long") or (signed < 0 and cur["dir"] == "short")
            if same:
                cur["en"] += px * sz; cur["ensz"] += sz
            cur["t_close"] = t; cur["peak"] = max(cur["peak"], abs(after))
            if abs(after) < EPS:
                cur["entry_px"] = (cur["en"] / cur["ensz"]) if cur["ensz"] else px
                trades.append(cur); cur = None
        if cur is not None:
            cur["entry_px"] = (cur["en"] / cur["ensz"]) if cur["ensz"] else None
            cur["open_at_end"] = True
            trades.append(cur)
    return trades


def worst_underwater(trades, acct):
    """全建玉で『含み損>=口座BAG_PCT%』が連続した最長期間(時間)を持つ建玉を返す。"""
    if acct <= 0:
        return None
    thr = BAG_PCT * acct
    best = None
    for tr in trades:
        if tr.get("entry_px") is None:
            continue
        if (tr["t_close"] - tr["t_open"]) < MIN_SOAK_H * MS_H:   # 1日未満保有はスキップ
            continue
        ser = get_hlc(tr["coin"])
        if not ser:
            continue
        ts = _ts[tr["coin"]]
        i0 = bisect.bisect_left(ts, tr["t_open"]); i1 = bisect.bisect_right(ts, tr["t_close"])
        e = tr["entry_px"]; sz = tr["peak"]
        run = maxrun = 0; max_loss = 0.0
        for t, c in ser[i0:i1]:
            loss = (e - c) * sz if tr["dir"] == "long" else (c - e) * sz
            if loss >= thr:
                run += 1; maxrun = max(maxrun, run); max_loss = max(max_loss, loss)
            else:
                run = 0
        if maxrun >= MIN_SOAK_H:
            cand = {"underwater_hours": maxrun, "underwater_days": round(maxrun / 24, 1),
                    "max_loss": round(max_loss), "loss_pct_acct": round(max_loss / acct, 2),
                    "coin": tr["coin"], "dir": tr["dir"],
                    "open": datetime.fromtimestamp(tr["t_open"] / 1000, timezone.utc).strftime("%Y-%m-%d"),
                    "open_at_end": tr.get("open_at_end", False)}
            if best is None or maxrun > best["underwater_hours"]:
                best = cand
    return best


def soak_tag(days):
    """最長連続塩漬け日数 → 段階タグ。"""
    if not days or days < 1:
        return None
    d = int(days)        # 切り捨て: 1.x→1日, 6.9→6日, 7+→7日以上
    if d >= 7:
        return "塩漬け:7日以上"
    return f"塩漬け:{d}日"


def price_at(series, t):
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


def analyze(addr):
    fills = fetch_fills(addr)
    coins = fc.scan_coins(fills)          # perp(builder含む) ∪ majors
    maj = [f for f in fills if f.get("coin") in coins]
    if not maj:
        return {"address": addr, "no_majors": True}
    closes = [f for f in maj if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
    wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)
    win_rate = round(wins / len(closes), 4) if closes else None
    hits = opens = 0
    for f in maj:
        d = open_dir(f.get("dir"))
        if d not in ("long", "short"):
            continue
        s = get_hlc(f["coin"]); t = int(f["time"])
        if not s:
            continue
        p0 = price_at(s, t); p1 = price_at(s, t + config.HIT_HORIZON_H * MS_H)
        if not p0 or not p1:
            continue
        opens += 1
        moved = (p1 - p0) / p0
        if (d == "long" and moved > 0) or (d == "short" and moved < 0):
            hits += 1
    dir_acc = round(hits / opens, 4) if opens else None
    t0 = min(int(f["time"]) for f in maj); t1 = max(int(f["time"]) for f in maj)
    days = round((t1 - t0) / (24 * MS_H), 1)
    st = hl_client.clearinghouse_state(addr) or {}
    acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
    cur_loss = sum(-float(p["position"]["unrealizedPnl"]) for p in st.get("assetPositions", [])
                   if float(p["position"]["unrealizedPnl"]) < 0)
    cur_bag_ratio = round(cur_loss / acct, 3) if acct else None
    realized = round(sum(float(f.get("closedPnl", 0) or 0) for f in maj))

    worst = worst_underwater(reconstruct(fills), acct)     # 全件で過去塩漬けを評価
    soak_days = worst["underwater_days"] if worst else 0
    tag = soak_tag(soak_days)
    basic = bool(win_rate and dir_acc and win_rate >= WIN_MIN and dir_acc >= DIR_MIN
                 and len(closes) >= MIN_CLOSES and days >= MIN_DAYS)
    no_cur_bag = (cur_bag_ratio is not None and cur_bag_ratio <= BAG_PCT)
    no_hist7 = (soak_days < 7)
    return {
        "address": addr, "win_rate": win_rate, "dir_accuracy": dir_acc,
        "n_closes": len(closes), "active_days": days, "account_value": round(acct),
        "realized_majors": realized, "cur_bag_ratio": cur_bag_ratio, "no_cur_bag": no_cur_bag,
        "soak_days": soak_days, "soak_tag": tag, "soak_detail": worst,
        "basic_pass": basic, "qualifies_strict": bool(basic and no_cur_bag and no_hist7),
    }


SOAK_TAGS = {f"塩漬け:{d}日" for d in range(1, 7)} | {"塩漬け:7日以上"}


def apply_tags(out):
    """台帳の各アドレスに塩漬け段階タグを付与(既存の塩漬けタグは置換)。"""
    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    n = 0
    for r in out:
        e = W.get(r["address"].lower())
        if not e:
            continue
        tags = [t for t in e.get("tags", []) if t not in SOAK_TAGS]
        if r.get("soak_tag"):
            tags.append(r["soak_tag"]); n += 1
        e["tags"] = sorted(set(tags))
        if r.get("soak_detail"):
            e["soak_detail"] = r["soak_detail"]
    json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    import step6_registry as reg6
    reg6.render_all(reg)
    print(f"台帳に塩漬けタグ付与: {n}件 / 台帳再生成済")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--scan-all", action="store_true")
    ap.add_argument("--apply-tags", action="store_true")
    ap.add_argument("--positions", default="プロトレーダー(本物),プロトレーダー(未精査),💸 出金疑い(要監視)")
    ap.add_argument("--out", default="insider_clean_strict_bag.json")
    args = ap.parse_args()

    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    if args.scan_all:
        targets = [e["address"] for e in reg.values()]
    else:
        poss = set(p for p in args.positions.split(",") if p)
        targets = [e["address"] for e in reg.values() if e.get("position") in poss or e.get("wf2_checked")]
    targets = list(dict.fromkeys(targets))
    if args.limit:
        targets = targets[:args.limit]
    print(f"塩漬け段階判定 対象 {len(targets)}件 (閾値=口座{int(BAG_PCT*100)}%, 段階=1〜6日/7日以上)")

    for coin in config.COINS:        # majorsは先にキャッシュ
        get_hlc(coin)

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a)
        except Exception as e:
            r = {"address": a, "error": str(e)[:80]}
        r["position"] = reg.get(a.lower(), {}).get("position")
        out.append(r)
        if i % 25 == 0:
            print(f"  {i}/{len(targets)} ... (足cache {len(_cache)}coin)")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    from collections import Counter
    dist = Counter(r.get("soak_tag") for r in out if r.get("soak_tag"))
    print("\n=== 過去塩漬け 段階分布（最長連続・口座10%超）===")
    for d in [f"塩漬け:{x}日" for x in range(1, 7)] + ["塩漬け:7日以上"]:
        print(f"  {d}: {dist.get(d, 0)}件")
    print(f"  塩漬け無し: {sum(1 for r in out if not r.get('soak_tag') and not r.get('no_majors'))}件")
    strict = [r for r in out if r.get("qualifies_strict")]
    print(f"\n=== ★厳密通過（高勝率×高的中×現在含み損なし×過去7日塩漬け無し）: {len(strict)}件 ===")
    for r in sorted(strict, key=lambda r: (r["win_rate"] or 0) * (r["dir_accuracy"] or 0), reverse=True):
        print(f"  勝率{r['win_rate']} 的中{r['dir_accuracy']} {r['active_days']}日 実現${r['realized_majors']:,} "
              f"最長塩漬け{r['soak_days']}日 {r['address'][:10]}.. [{r['position']}]")

    if args.apply_tags:
        apply_tags(out)


if __name__ == "__main__":
    main()
