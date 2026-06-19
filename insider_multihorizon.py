"""方向的中率を複数地平線(1h/4h/12h/24h/48h/72h)で測り直す。

4h一点固定では『遅効カタリスト型』(数日かけて効く)インサイダーを取りこぼす懸念への対処。
各エントリ(建玉)について、各地平線後の価格が建玉方向へ動いた割合を算出し:
  - 地平線ごとの的中率プロファイル(即効/遅効/トレンド便乗の判別)
  - 4hではコイン投げでも長地平線で高的中が持続する層を炙り出す
  - 大口エントリ限定の的中(確信ベットが当たるか)も併算
出力: data/insider_multihorizon.json
使い方: python insider_multihorizon.py [--scan-all] [--limit N]
"""
import json
import time
import bisect
import argparse
from datetime import datetime, timezone

import config
import hl_client
import hl_fills_cache as fc

MS_H = 3600 * 1000
NOW = int(time.time() * 1000)
CAND_DAYS = 800
HORIZONS = [1, 4, 12, 24, 48, 72]
LARGE = config.LARGE_TRADE_USD       # 大口エントリ閾値($100k)
MIN_OPENS = 20

_cache, _ts, _missing = {}, {}, set()


def get_ser(coin):
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
    ser = sorted([(int(x["t"]), float(x["c"])) for x in c])
    _cache[coin] = ser; _ts[coin] = [t for t, _ in ser]
    return ser


def price_at(coin, t):
    ser = get_ser(coin)
    if not ser:
        return None
    ts = _ts[coin]
    i = bisect.bisect_right(ts, t) - 1
    return ser[i][1] if i >= 0 else None


def fetch_fills(addr, max_pages=40):
    return fc.get_fills(addr, max_pages=max_pages)   # 永続キャッシュ＋増分取得(フル履歴)


def open_dir(d):
    d = (d or "").strip()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()
    return None


def analyze(addr, base_cache):
    fills = fetch_fills(addr)
    entries = []
    for f in fills:
        if f.get("coin") not in config.COINS:
            continue
        d = open_dir(f.get("dir"))
        if d not in ("long", "short"):
            continue
        entries.append({"coin": f["coin"], "dir": d, "t": int(f["time"]),
                        "notional": float(f["px"]) * float(f["sz"])})
    if len(entries) < MIN_OPENS:
        return {"address": addr, "n_opens": len(entries), "thin": True}

    big = [e for e in entries if e["notional"] >= LARGE]
    res = {"address": addr, "n_opens": len(entries), "n_big": len(big), "h": {}, "h_big": {}, "h_detr": {}}
    for H in HORIZONS:
        hit = tot = 0; bhit = btot = 0; detr_sum = 0.0; detr_n = 0
        base = base_cache.get(H, {})
        for e in entries:
            p0 = price_at(e["coin"], e["t"]); p1 = price_at(e["coin"], e["t"] + H * MS_H)
            if not p0 or not p1:
                continue
            raw = (p1 - p0) / p0
            up = raw > 0
            ok = (e["dir"] == "long" and up) or (e["dir"] == "short" and not up)
            tot += 1; hit += 1 if ok else 0
            # トレンド補正後の順方向リターン(地平線別ベースライン控除)
            sgn = 1 if e["dir"] == "long" else -1
            detr_sum += sgn * (raw - base.get(e["coin"], 0.0)); detr_n += 1
            if e["notional"] >= LARGE:
                btot += 1; bhit += 1 if ok else 0
        res["h"][H] = round(hit / tot, 3) if tot else None
        res["h_big"][H] = round(bhit / btot, 3) if btot else None
        res["h_detr"][H] = round(detr_sum / detr_n, 4) if detr_n else None
    # プロファイル: 最高的中の地平線, 4h→長地平線で改善するか
    valid = {H: v for H, v in res["h"].items() if v is not None}
    if valid:
        best_H = max(valid, key=valid.get)
        res["best_horizon"] = best_H
        res["best_acc"] = valid[best_H]
        h4 = res["h"].get(4)
        res["late_gain"] = round(max((valid.get(h, 0) for h in (24, 48, 72)), default=0) - (h4 or 0), 3)
    return res


def baselines():
    """各地平線・各coinの平均リターン(=ベータ/ドリフト)。"""
    base = {H: {} for H in HORIZONS}
    for coin in config.COINS:
        ser = get_ser(coin)
        if not ser:
            continue
        ts = _ts[coin]
        for H in HORIZONS:
            rs = []
            step = max(len(ser) // 600, 1)
            for i in range(0, len(ser), step):
                t = ser[i][0]; p0 = ser[i][1]
                j = bisect.bisect_right(ts, t + H * MS_H) - 1
                if j >= 0 and p0:
                    rs.append((ser[j][1] - p0) / p0)
            base[H][coin] = sum(rs) / len(rs) if rs else 0.0
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--scan-all", action="store_true")
    ap.add_argument("--positions", default="プロトレーダー(本物),プロトレーダー(未精査),💸 出金疑い(要監視)")
    ap.add_argument("--out", default="insider_multihorizon.json")
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
    print(f"複数地平線 方向的中 対象 {len(targets)}件 / 地平線={HORIZONS}h")

    for coin in config.COINS:
        get_ser(coin)
    base_cache = baselines()
    print("ベースライン(平均リターン)算出済:", {H: {k: round(v, 4) for k, v in base_cache[H].items()} for H in (4, 24, 72)})

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, base_cache)
        except Exception as e:
            r = {"address": a, "error": str(e)[:80]}
        r["position"] = reg.get(a.lower(), {}).get("position")
        out.append(r)
        if i % 25 == 0:
            print(f"  {i}/{len(targets)} ...")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "horizons": HORIZONS, "wallets": out},
              open(f"{config.DATA_DIR}/{args.out}", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    ev = [r for r in out if r.get("h") and r.get("n_opens", 0) >= MIN_OPENS]
    # 遅効カタリスト型候補: 4hはコイン投げ(<0.55)だが長地平線(24-72h)で高的中(>=0.65)
    late = [r for r in ev if (r["h"].get(4) or 0) < 0.55
            and max((r["h"].get(h) or 0) for h in (24, 48, 72)) >= 0.65]
    print(f"\n=== 遅効カタリスト型候補（4h<0.55 だが24-72hで>=0.65）: {len(late)}件 ===")
    for r in sorted(late, key=lambda r: -r.get("late_gain", 0))[:15]:
        h = r["h"]
        print(f"  4h{h.get(4)} 12h{h.get(12)} 24h{h.get(24)} 48h{h.get(48)} 72h{h.get(72)} "
              f"(大口72h{r['h_big'].get(72)}) opens{r['n_opens']} {r['address'][:10]} [{r.get('position','')[:12]}]")
    # 全地平線で高的中(>=0.65)を維持＝持続的な方向エッジ
    allh = [r for r in ev if all((r["h"].get(h) or 0) >= 0.65 for h in HORIZONS)]
    print(f"\n=== 全地平線で的中>=0.65維持（持続的方向エッジ）: {len(allh)}件 ===")
    for r in allh:
        print(f"  {[r['h'].get(h) for h in HORIZONS]} opens{r['n_opens']} {r['address'][:10]} [{r.get('position','')[:12]}]")
    # 大口の的中が地平線で跳ねる層(確信ベットが遅効で効く)
    bigl = [r for r in ev if r.get("n_big", 0) >= 10 and (r["h_big"].get(4) or 0) < 0.55
            and max((r["h_big"].get(h) or 0) for h in (24, 48, 72)) >= 0.7]
    print(f"\n=== 大口確信ベットが遅効で当たる（大口4h<0.55→24-72h>=0.7, 大口>=10）: {len(bigl)}件 ===")
    for r in sorted(bigl, key=lambda r: -max((r['h_big'].get(h) or 0) for h in (24,48,72)))[:12]:
        print(f"  大口的中 4h{r['h_big'].get(4)} 24h{r['h_big'].get(24)} 48h{r['h_big'].get(48)} 72h{r['h_big'].get(72)} "
              f"n_big{r['n_big']} {r['address'][:10]} [{r.get('position','')[:12]}]")


if __name__ == "__main__":
    main()
