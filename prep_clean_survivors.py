"""clean高勝率の通過8件を、トレンド汚染検査のため方向別・時期別に分解する。

dir_accuracy/win_rate が『下落相場でショート一辺倒』なだけかを暴く:
  - ロング/ショート別の件数・勝率・方向的中率(両方向で勝てるか)
  - 月別の取引・実現益分布(特定regimeに集中か)
  - 銘柄別の勝ち益
出力: data/wf_clean_survivors.json
"""
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

import config
import hl_client

MS_H = 3600 * 1000
NOW = int(time.time() * 1000)
CAND_DAYS = 420

ADDRS = [
    "0x72988778525f0ce15c5ac1804ac460606a987d6c",
    "0xfe4dec8543b0b9cec0c897a689f7af7bddb02192",
    "0x25dacd8a27eac9ad6eba5eb88e3b68f707eb1397",
    "0xdfee2d4f729723aed67eed4bfb7a998dbb4294c0",
    "0x02aa6015d2ba9992b88596d36bad706eeef41806",
    "0x1e5cf44f64a66dae56104ed91bf15f6147200ac1",
    "0xd46979f07f5d1e86ae2dcc5e6e0f3af5fe270471",
    "0x5198eff88dea32108605bcdb109a99dc397190d8",
]


def fetch_fills(addr, max_pages=20):
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
    return None


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


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    clean = {r["address"].lower(): r for r in
             json.load(open(f"{config.DATA_DIR}/insider_clean_winrate.json", encoding="utf-8"))["wallets"]}
    candle = {}
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", NOW - CAND_DAYS * 24 * MS_H, NOW) or []
        candle[coin] = sorted([(int(x["t"]), float(x["c"])) for x in c])

    rows = []
    for a in ADDRS:
        fills = fetch_fills(a)
        maj = [f for f in fills if f.get("coin") in config.COINS]
        # 方向別の的中(エントリ4h後)
        side = {"long": {"opens": 0, "hits": 0, "notional": 0.0},
                "short": {"opens": 0, "hits": 0, "notional": 0.0}}
        month_real = defaultdict(float)
        coin_real = defaultdict(float)
        for f in maj:
            cpnl = float(f.get("closedPnl", 0) or 0)
            mon = datetime.fromtimestamp(int(f["time"]) / 1000, timezone.utc).strftime("%Y-%m")
            month_real[mon] += cpnl
            coin_real[f["coin"]] += cpnl
            d = open_dir(f.get("dir"))
            if d in ("long", "short"):
                series = candle[f["coin"]]
                t = int(f["time"]); p0 = price_at(series, t); p1 = price_at(series, t + config.HIT_HORIZON_H * MS_H)
                side[d]["opens"] += 1
                side[d]["notional"] += float(f["px"]) * float(f["sz"])
                if p0 and p1:
                    moved = (p1 - p0) / p0
                    if (d == "long" and moved > 0) or (d == "short" and moved < 0):
                        side[d]["hits"] += 1
        for s in side.values():
            s["dir_acc"] = round(s["hits"] / s["opens"], 3) if s["opens"] else None
            s["notional"] = round(s["notional"])
        tot_open = side["long"]["opens"] + side["short"]["opens"]
        short_ratio = round(side["short"]["opens"] / tot_open, 3) if tot_open else None
        months = sorted(month_real.items())
        # 実現益が集中している月（regime偏りの指標）
        pos_months = {m: round(v) for m, v in months if v > 0}
        top_month = max(pos_months.items(), key=lambda x: x[1]) if pos_months else None
        total_real = sum(month_real.values())
        cm = clean.get(a.lower(), {})
        e = reg.get(a.lower(), {})
        rows.append({
            "address": a, "position": e.get("position"), "labels": e.get("labels") or [],
            "win_rate": cm.get("win_rate"), "dir_accuracy": cm.get("dir_accuracy"),
            "n_closes": cm.get("n_closes"), "active_days": cm.get("active_days"),
            "account_value": cm.get("account_value"), "realized_majors": cm.get("realized_majors"),
            "long": side["long"], "short": side["short"], "short_ratio": short_ratio,
            "both_dirs_win": bool(side["long"]["dir_acc"] and side["short"]["dir_acc"]
                                  and side["long"]["dir_acc"] >= 0.6 and side["short"]["dir_acc"] >= 0.6),
            "top_profit_month": top_month,
            "top_month_share": round(top_month[1] / total_real, 3) if (top_month and total_real > 0) else None,
            "month_realized": {m: round(v) for m, v in months},
            "coin_realized": {c: round(v) for c, v in sorted(coin_real.items(), key=lambda x: -abs(x[1]))[:5]},
        })
        print(f"{a[:10]} short率{short_ratio} L的中{side['long']['dir_acc']} S的中{side['short']['dir_acc']} "
              f"両方向勝ち{rows[-1]['both_dirs_win']} 最益月{top_month} 集中{rows[-1]['top_month_share']}")

    ctx = {
        "purpose": "clean高勝率(含み損バッグなし×高勝率×高的中×1週間+)の通過8件を、残るトレンド/regime汚染で精査",
        "note": "含み損バッグ(塩漬け)汚染は除去済。残る懸念は方向的中率のトレンド汚染=下落相場でショート一辺倒だと的中も勝率も高く出る。",
        "checks": {
            "short_ratio": "ショート比率。1に近い=ショート一辺倒=下落相場便乗の疑い",
            "both_dirs_win": "ロング・ショート両方で的中>=0.6か。両方勝てる=トレンド非依存=実力/情報優位寄り",
            "top_month_share": "実現益が単一月に集中する割合。高い=特定regime一発",
        },
    }
    json.dump({"context": ctx, "wallets": rows},
              open(f"{config.DATA_DIR}/wf_clean_survivors.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\nsaved data/wf_clean_survivors.json")


if __name__ == "__main__":
    main()
