"""個人インサイダー検出 v2: 「イベント往復(動く前に建て→動いた後に利確)」の反復回数で評価。

workflowが暴いた弱点を克服:
 - 分割約定はイベント単位に集約（N=1水増しを排除）
 - 「往復」を要求＝トレンド便乗(持ち続ける)を自動排除
 - 別々の急変イベントでの反復回数を数える＝偶然1発を排除
出力: data/insider_v2.json
使い方: python insider_v2.py
"""
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

import config
import hl_client
import hl_fills_cache as fc
import hl_candle_cache as cc

MS_H = 3600 * 1000
LEAD = config.LEAD_WINDOW_H * MS_H        # 動く前 6h
EXIT = 12 * MS_H                          # 動いた後 12h以内に利確
LARGE = config.LARGE_TRADE_USD
# 除外/MM以外で稼ぎのある層を対象
TARGET_POS = {"インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
              "プロトレーダー(本物)", "Nansen候補(HL未検証)"}


def open_dir(d):
    d = (d or "").strip()
    if ">" in d:
        return d.split(">")[-1].strip().lower()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()
    return None


def build_events(coins, lo, hi):
    evs = []
    for coin in coins:
        try:
            c = cc.get_candles(coin, "4h", lo, hi)   # 永続candleキャッシュ(builderperp含む)
        except Exception:
            c = []
        s = sorted([(int(x["t"]), float(x["c"])) for x in (c or [])])
        for i in range(len(s) - 1):
            p0, p1 = s[i][1], s[i + 1][1]
            if p0 > 0 and abs((p1 - p0) / p0 * 100) >= config.EVENT_MOVE_PCT:
                evs.append({"coin": coin, "t0": s[i][0],
                            "dir": "long" if p1 > p0 else "short",
                            "pct": round((p1 - p0) / p0 * 100, 2)})
    return evs


# 緩和段階: (名前, lead時間h, exit時間h, 大口閾値USD)
TIERS = [("strict", 6, 12, 100_000), ("medium", 12, 24, 50_000), ("loose", 24, 48, 25_000)]


def analyze(addr, events, fills=None):
    if fills is None:
        fills = fc.get_fills(addr)
    coins = fc.scan_coins(fills)             # 実取引coin ∪ majors（builderperp含む）
    rel = [f for f in fills if f.get("coin") in coins]
    if not rel:
        return None
    opens = defaultdict(list)   # (coin,dir) -> [(t, notional)]
    closes = defaultdict(list)  # coin -> [(t, reduce_dir)]
    for f in rel:
        t = int(f["time"]); coin = f["coin"]; px = float(f["px"]); sz = float(f["sz"])
        d = open_dir(f.get("dir"))
        dd = (f.get("dir") or "")
        if d in ("long", "short"):
            opens[(coin, d)].append((t, px * sz))
        if "Close" in dd or "Reduce" in dd or ">" in dd:
            cd = "long" if "Long" in dd else ("short" if "Short" in dd else None)
            if cd:
                closes[coin].append((t, cd))

    def large_clusters(large):
        """大口の新規建玉を (coin,dir,1h) で束ねた“賭けの回数”（分割約定は1回に集約）。"""
        seen = set()
        for (coin, d), lst in opens.items():
            for t, notl in lst:
                if notl >= large:
                    seen.add((coin, d, t // MS_H))   # 1時間バケットで集約
        return len(seen)

    def rt_for(lead_h, exit_h, large):
        lead_ms, exit_ms = lead_h * MS_H, exit_h * MS_H
        rt = lead_only = 0
        detail = []
        for ev in events:
            coin, t0, d = ev["coin"], ev["t0"], ev["dir"]
            led = any(t0 - lead_ms <= t <= t0 and notl >= large for t, notl in opens.get((coin, d), []))
            if not led:
                continue
            exited = any(t0 <= t <= t0 + exit_ms and cd == d for t, cd in closes.get(coin, []))
            if exited:
                rt += 1
                if len(detail) < 6:
                    detail.append({"coin": coin, "dir": d, "pct": ev["pct"],
                                   "event": datetime.fromtimestamp(t0 / 1000, timezone.utc).strftime("%Y-%m-%d")})
            else:
                lead_only += 1
        return rt, lead_only, detail

    tiers = {}
    for name, lh, eh, lg in TIERS:
        rt, lo, det = rt_for(lh, eh, lg)
        nclust = large_clusters(lg)
        # 正規化: 大口の賭けのうち往復が占める率
        norm = round(rt / nclust, 4) if nclust else 0
        tiers[name] = {"rt": rt, "lead_only": lo, "large_clusters": nclust,
                       "norm_rate": norm, "detail": det}
    return {"address": addr, "majors": len(rel), "n_fills": len(rel),
            "coins": sorted({f.get("coin") for f in rel if f.get("coin")})[:12], "tiers": tiers,
            "rt_events": tiers["strict"]["rt"]}  # 後方互換(majors=判定対象fill数)


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    targets = [e["address"] for e in reg.values()
               if e.get("position") in TARGET_POS and "MM/HFT" not in e.get("tags", [])]
    print(f"個人インサイダー v2 検出対象: {len(targets)} 件")
    now = int(time.time() * 1000)
    # 各対象のfill(永続キャッシュ)を集め、走査対象 coin universe(builderperp含む)を作る
    wallet_fills, all_coins = {}, set(config.COINS)
    for a in targets:
        try:
            fl = fc.get_fills(a)
        except Exception:
            fl = []
        wallet_fills[a] = fl
        all_coins |= fc.scan_coins(fl)      # perp限定(スポット除外)・majors含む
    print(f"走査対象coin(majors+builder): {len(all_coins)} 銘柄")
    events = build_events(all_coins, now - 560 * 24 * MS_H, now)
    print(f"急変イベント: {len(events)} 件")

    out = []
    for i, a in enumerate(targets, 1):
        try:
            r = analyze(a, events, fills=wallet_fills.get(a))
        except Exception:
            r = None
        if r:
            out.append(r)
        if i % 25 == 0:
            print(f"  {i}/{len(targets)} ...")
    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "wallets": out},
              open(f"{config.DATA_DIR}/insider_v2.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # medium段階で正規化率ランキング（往復≥2 かつ 大口クラスタ≥3 で小サンプル除外）
    M = "medium"
    qualified = [r for r in out if r["tiers"][M]["rt"] >= 2 and r["tiers"][M]["large_clusters"] >= 3]
    qualified.sort(key=lambda r: r["tiers"][M]["norm_rate"], reverse=True)
    print(f"=== 頻度正規化ランキング(medium: 往復÷大口クラスタ, 往復≥2&クラスタ≥3) {len(qualified)}件 ===")
    print("  正規化率 = 大口の賭けのうち『イベント往復』が占める割合（高い=選別的に当てる）")
    for r in qualified[:20]:
        t = r["tiers"][M]
        print(f"  率{t['norm_rate']:.2f} (往復{t['rt']}/大口{t['large_clusters']}) majors{r['majors']} {r['address'][:12]}..")
    print("\n=== 参考: 生の往復≥3件数（出来高バイアスあり）===")
    for name, *_ in TIERS:
        n = sum(1 for r in out if r["tiers"][name]["rt"] >= 3)
        print(f"  {name}: {n}件")


if __name__ == "__main__":
    main()
