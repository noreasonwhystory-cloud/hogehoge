"""未照会層から発掘した遅効エッジ(late_edge_new.json passers)を台帳へ追加。

各件: キャッシュ約定からHL実測(勝率/方向的中4h/実現益/期間)＋Nansen(資金源/正体)を付与し、
弱い疑惑(監視継続)＋遅効エッジ(24-72h)＋未照会発掘 タグで登録。
使い方: python add_late_edge.py
"""
import json
from datetime import datetime, timezone

import config
import hl_client
import hl_fills_cache as fc
import nansen_client as nc
import step6_registry as reg6

MS_H = 3600 * 1000


def od(x):
    x = (x or "").strip()
    return x.replace("Open", "").strip().lower() if x.startswith("Open") else None


def ok(r):
    return isinstance(r, dict) and "_error" not in r


def main():
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "late_edge_new.json"
    MIN_REAL = 50000   # majors実現益が黒字(>$5万)のもののみ追加(エッジ≠利益)
    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    passers = json.load(open(f"{config.DATA_DIR}/{src}", encoding="utf-8"))["passers"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # 4h的中用の足
    cc = {}
    import bisect
    now = int(__import__("time").time() * 1000)
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", now - 800 * 24 * MS_H, now) or []
        cc[coin] = sorted([(int(x["t"]), float(x["c"])) for x in c])

    def price_at(coin, t):
        s = cc.get(coin)
        if not s:
            return None
        ts = [x[0] for x in s]
        i = bisect.bisect_right(ts, t) - 1
        return s[i][1] if i >= 0 else None

    added = 0
    for p in passers:
        a = p["address"].lower()
        if a in W:
            continue
        fills = fc.get_fills(a, refresh=False)
        maj = [f for f in fills if f.get("coin") in config.COINS]
        closes = [f for f in maj if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
        wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)
        win_rate = round(wins / len(closes), 4) if closes else None
        realized = round(sum(float(f.get("closedPnl", 0) or 0) for f in maj))
        if realized <= MIN_REAL:        # 赤字/小利は追加しない(エッジ≠利益)
            continue
        # majors先物に焦点: 利益の主体がBTC/ETH/SOL先物でないと対象外(alt/ミーム除外)
        real_all = sum(float(f.get("closedPnl", 0) or 0) for f in fills if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9)
        if real_all > 0 and realized / real_all < 0.6:
            continue
        t0 = min(int(f["time"]) for f in maj); t1 = max(int(f["time"]) for f in maj)
        days = (datetime.fromtimestamp(t1 / 1000, timezone.utc) - datetime.fromtimestamp(t0 / 1000, timezone.utc)).days
        active14 = (now - t1) <= 14 * 24 * MS_H
        st = hl_client.clearinghouse_state(a) or {}
        acct = round(float(st.get("marginSummary", {}).get("accountValue", 0) or 0))
        # Nansen 資金源/正体
        labels, ff = [], []
        for ch in config.ENRICH_CHAINS:
            r = nc.address_labels(a, ch)
            if ok(r) and r.get("data"):
                labels = [l.get("label") or l.get("address_label") or str(l) for l in r["data"]]; break
        for ch in config.ENRICH_CHAINS:
            r = nc.related_wallets(a, ch)
            if ok(r) and r.get("data"):
                ff = [{"address": x.get("address"), "label": x.get("address_label"),
                       "relation": x.get("relation"), "time": x.get("block_timestamp")} for x in r["data"]]; break
        src = ", ".join((f.get("label") or (f.get("address") or "")[:12]) for f in ff[:3]) or "取得できず"
        rec = {"date": today, "metric_category": "hl-verified", "win_rate": win_rate,
               "dir_accuracy": None, "total_pnl": realized}
        W[a] = {
            "address": a, "first_seen": today, "last_seen": today, "times_seen": 1,
            "position": "弱い疑惑(監視継続)", "metric_category": "hl-verified",
            "labels": labels, "first_funders": ff,
            "current": rec, "history": [rec], "hl_checked": today, "nansen_checked": today,
            "active14": active14, "trade_days": days, "n_closes": len(closes),
            "realized_majors": realized, "account_value": acct,
            "mh_detr72": p["detr72"], "mh_detr24": p["detr24"], "mh_checked": today,
            "auto_tags": [],
            "tags": sorted({"遅効エッジ(24-72h)", "未照会発掘", "Nansen発見",
                            "取引あり(14d)" if active14 else "取引なし(14d)"}),
            "notes_jp": (f"【未照会層から遅効エッジ発掘({today})】台帳に未登録だった層をリーダーボードから抽出し複数地平線で精査。"
                         f"4h方向的中はコイン投げだが補正後リターンが 24h+{p['detr24']:.3f}/72h+{p['detr72']:.3f} と市場ドリフト超。"
                         f"{p['months']}ヶ月・銘柄{list(p['coins'])}・short率{p['short_ratio']}に分散しN=1/regimeでは説明困難。"
                         f"HL実測: 勝率{win_rate}・実現${realized:,}・{days}日・口座${acct:,}。"
                         f"正体「{(labels or ['匿名'])[0]}」 資金源={src}。実力スイングの可能性が高いが遅効positioningも否定できず監視。"),
        }
        added += 1
        print(f"  + {a[:10]} 補正72h+{p['detr72']:.3f} 勝率{win_rate} 実現${realized:,} 口座${acct:,} 資金源:{src}")

    json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    reg6.render_all(reg)
    print(f"\n台帳へ追加 {added}件 / 再生成済")


if __name__ == "__main__":
    main()
