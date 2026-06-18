"""厳密版『含み損なし』判定: 約定履歴から建玉を復元し、過去に7日以上連続で口座の10%超の
含み損を抱え続けた時期があるか(=真の塩漬け)を足で判定する。

現在スナップショット(clearinghouseState)だけでなく履歴も見る:
  各建玉(建て→フラット)について、保有中の1h足closeで「含み損 >= 口座の10%」が
  7日(168h)以上連続したら『塩漬け履歴あり』=失格。
口座価値は現在値(clearinghouse)を基準に用いる(履歴の口座値はAPIで安価に取れぬための近似)。
勝率/方向的中(majors)も併算。出力: data/insider_clean_strict_bag.json
使い方: python insider_clean_strict_bag.py [--scan-all] [--limit N]
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
CAND_DAYS = 800
WIN_MIN, DIR_MIN, MIN_CLOSES, MIN_DAYS = 0.70, 0.65, 10, 7
BAG_PCT = 0.10            # 口座の10%超の含み損
BAG_HOURS = 168          # 7日(168h)以上連続で塩漬け

_cache, _missing = {}, set()


def get_hlc(coin):
    if coin in _cache:
        return _cache[coin]
    if coin in _missing:
        return None
    try:
        c = hl_client.candles(coin, "1h", NOW - CAND_DAYS * 24 * MS_H, NOW) or []
    except Exception:
        c = []
    if not c:
        _missing.add(coin); return None
    _cache[coin] = sorted([(int(x["t"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in c])
    return _cache[coin]


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


def hist_bag(trades, acct):
    """過去に7日以上連続で口座の10%超の含み損だった建玉があるか。"""
    if acct <= 0:
        return None
    thr = BAG_PCT * acct
    worst = None
    for tr in trades:
        if tr.get("entry_px") is None:
            continue
        hold_h = (tr["t_close"] - tr["t_open"]) / MS_H
        if hold_h < BAG_HOURS:        # そもそも7日未満保有はスキップ
            continue
        ser = get_hlc(tr["coin"])
        if not ser:
            continue
        e = tr["entry_px"]; sz = tr["peak"]
        run = 0; maxrun = 0; max_loss = 0.0
        for t, h, l, c in ser:
            if t < tr["t_open"] or t > tr["t_close"]:
                continue
            loss = (e - c) * sz if tr["dir"] == "long" else (c - e) * sz   # closeベースの含み損(>0=損)
            if loss >= thr:
                run += 1; maxrun = max(maxrun, run); max_loss = max(max_loss, loss)
            else:
                run = 0
        if maxrun >= BAG_HOURS:
            cand = {"coin": tr["coin"], "dir": tr["dir"],
                    "underwater_days": round(maxrun / 24, 1), "max_loss": round(max_loss),
                    "loss_pct_acct": round(max_loss / acct, 2),
                    "open": datetime.fromtimestamp(tr["t_open"] / 1000, timezone.utc).strftime("%Y-%m-%d"),
                    "open_at_end": tr.get("open_at_end", False)}
            if worst is None or cand["max_loss"] > worst["max_loss"]:
                worst = cand
    return worst


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
    return series[lo][3]   # close


def analyze(addr, majcandle):
    fills = fetch_fills(addr)
    maj = [f for f in fills if f.get("coin") in config.COINS]
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
        s = majcandle[f["coin"]]; t = int(f["time"])
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

    basic = bool(win_rate and dir_acc and win_rate >= WIN_MIN and dir_acc >= DIR_MIN
                 and len(closes) >= MIN_CLOSES and days >= MIN_DAYS)
    no_cur_bag = (cur_bag_ratio is not None and cur_bag_ratio <= BAG_PCT)
    worst = None
    if basic and no_cur_bag:                       # 高勝率候補だけ履歴塩漬けを深掘り(コスト節約)
        worst = hist_bag(reconstruct(fills), acct)
    no_hist_bag = (worst is None)
    return {
        "address": addr, "win_rate": win_rate, "dir_accuracy": dir_acc,
        "n_closes": len(closes), "active_days": days, "account_value": round(acct),
        "realized_majors": realized, "cur_bag_ratio": cur_bag_ratio,
        "no_cur_bag": no_cur_bag, "hist_bag": worst, "no_hist_bag": no_hist_bag,
        "basic_pass": basic,
        "qualifies_strict": bool(basic and no_cur_bag and no_hist_bag),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--scan-all", action="store_true")
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
    print(f"厳密塩漬け判定 対象 {len(targets)}件 (高勝率候補のみ履歴深掘り / 閾値=口座{int(BAG_PCT*100)}%×{BAG_HOURS//24}日)")

    majcandle = {}
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", NOW - CAND_DAYS * 24 * MS_H, NOW) or []
        majcandle[coin] = sorted([(int(x["t"]), 0.0, 0.0, float(x["c"])) for x in c])
        _cache[coin] = sorted([(int(x["t"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in c])

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, majcandle)
        except Exception as e:
            r = {"address": a, "error": str(e)[:80]}
        r["position"] = reg.get(a.lower(), {}).get("position")
        out.append(r)
        if i % 25 == 0:
            print(f"  {i}/{len(targets)} ... (足cache {len(_cache)}coin)")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    strict = [r for r in out if r.get("qualifies_strict")]
    strict.sort(key=lambda r: (r["win_rate"] or 0) * (r["dir_accuracy"] or 0), reverse=True)
    print(f"\n=== ★厳密通過（高勝率×高的中×現在含み損なし×過去にも7日塩漬け無し）: {len(strict)}件 ===")
    for r in strict:
        print(f"  勝率{r['win_rate']} 的中{r['dir_accuracy']} closes{r['n_closes']} {r['active_days']}日 "
              f"実現${r['realized_majors']:,} 口座${r['account_value']:,} {r['address'][:10]}.. [{r['position']}]")
    # 高勝率・現在バッグ無しだが『過去に塩漬け』で失格＝従来スナップショットでは見抜けなかった層
    histonly = [r for r in out if r.get("basic_pass") and r.get("no_cur_bag") and not r.get("no_hist_bag")]
    print(f"\n--- 現在は綺麗だが『過去に7日以上の塩漬け』で失格(スナップショットでは見抜けず): {len(histonly)}件 ---")
    for r in sorted(histonly, key=lambda r: -(r["hist_bag"]["max_loss"])):
        b = r["hist_bag"]
        print(f"  勝率{r['win_rate']} 過去塩漬け {b['coin']}{b['dir']} {b['underwater_days']}日 含み損${b['max_loss']:,}(口座{b['loss_pct_acct']}x) @{b['open']} {r['address'][:10]}..")


if __name__ == "__main__":
    main()
