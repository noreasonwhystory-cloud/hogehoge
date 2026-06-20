"""欺瞞前提のインサイダー検知（従来手法と別軸）。

インサイダーは『勝ちすぎを隠す』ために、わざと負けたり勝率を濁したりする。
その“性格の悪い”手口を、キャッシュ約定から deterministic に特徴量化して容疑者を抽出する。
出力 data/deception_candidates.json は workflow が多角裁定する材料。

検知パターン（後述リストと一致）:
  A サイズ条件付きスキル（大口高勝率/小口わざと負け＝コピトレ撹乱）
  B 非対称ペイオフ（低勝率なのに高PnL＝数発の大当たり）
  C 休眠→カタリスト覚醒（眠り→材料時だけ正解、利益が覚醒直後に集中）
  D 制御された負け（負けは小さく揃い・勝ちは大きい＝損失を化粧）
  E 新鮮ウォレット単発大当たり（短命・少取引・高実現→引上げ）
  F デコイ→本命フリップ（小さい逆張り囮→直後に大きい本命へ反転）
  G 出金規律（大勝直後にDD前出金＝やめ時を知る）
  H 勝ち逃げ後の意図的低調（注目後わざと不調→記録を濁し再度高値更新）

使い方: python deception_scan.py
"""
import os
import json
import math
import statistics as st

import config

FILLS = os.path.join(config.DATA_DIR, "fills")
MAJ = set(config.COINS)
DAY = 86_400_000
HOUR = 3_600_000


def notional(f):
    try:
        return abs(float(f.get("sz", 0) or 0) * float(f.get("px", 0) or 0))
    except Exception:
        return 0.0


def cpnl(f):
    try:
        return float(f.get("closedPnl", 0) or 0)
    except Exception:
        return 0.0


def features(addr, fills):
    fills = sorted(fills, key=lambda f: int(f["time"]))
    closes = [f for f in fills if abs(cpnl(f)) > 1e-9]
    if len(closes) < 5:
        return None
    n = len(closes)
    pnls = [cpnl(f) for f in closes]
    nots = [notional(f) for f in closes]
    real_all = sum(pnls)
    real_maj = sum(cpnl(f) for f in closes if f.get("coin") in MAJ)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n
    t0, t1 = int(closes[0]["time"]), int(closes[-1]["time"])
    active_days = max(1, (t1 - t0) // DAY)

    med_not = st.median(nots) if nots else 0
    # サイズ2分割（大口=中央値以上 / 小口=未満）
    big = [(p, nt) for p, nt in zip(pnls, nots) if nt >= med_not]
    small = [(p, nt) for p, nt in zip(pnls, nots) if nt < med_not]
    def wr(b):
        return (sum(1 for p, _ in b if p > 0) / len(b)) if b else None
    wr_big, wr_small = wr(big), wr(small)
    sum_big = sum(p for p, _ in big)
    sum_small = sum(p for p, _ in small)

    # top3勝ち集中
    pos_sorted = sorted([p for p in pnls if p > 0], reverse=True)
    top3 = sum(pos_sorted[:3])
    top3_share = (top3 / real_all) if real_all > 0 else 0

    # 勝ち/負けのnotional分布
    win_nots = [nt for p, nt in zip(pnls, nots) if p > 0]
    loss_nots = [nt for p, nt in zip(pnls, nots) if p < 0]
    med_win_not = st.median(win_nots) if win_nots else 0
    med_loss_not = st.median(loss_nots) if loss_nots else 0
    size_ratio = (med_win_not / med_loss_not) if med_loss_not > 0 else 0

    # 休眠ギャップ → 覚醒直後48hの利益集中
    gaps = [(int(closes[i]["time"]) - int(closes[i-1]["time"])) for i in range(1, n)]
    max_gap_d = (max(gaps) / DAY) if gaps else 0
    burst_pnl = 0.0
    for i in range(1, n):
        gap = int(closes[i]["time"]) - int(closes[i-1]["time"])
        if gap >= 7 * DAY:                       # 7日以上の休眠明け
            wake = int(closes[i]["time"])
            burst_pnl += sum(cpnl(f) for f in closes
                             if wake <= int(f["time"]) <= wake + 2 * DAY and cpnl(f) > 0)
    burst_share = (burst_pnl / real_all) if real_all > 0 else 0

    # デコイ→本命フリップ: 同コイン逆方向が6h以内、先(小)が負け→後(大)が勝ち
    flips = 0
    for i in range(1, n):
        a, b = closes[i-1], closes[i]
        if a.get("coin") == b.get("coin") and a.get("dir") != b.get("dir"):
            if int(b["time"]) - int(a["time"]) <= 6 * HOUR:
                if cpnl(a) < 0 and cpnl(b) > 0 and notional(b) > notional(a) * 1.5:
                    flips += 1

    # 勝ち逃げ後の意図的低調: 累積益のピーク後に小口で連敗→新高値更新
    cum, peak, dip_then_new = 0.0, 0.0, False
    cums = []
    for p in pnls:
        cum += p
        cums.append(cum)
    if cums:
        gmax = max(cums)
        pk_i = cums.index(gmax)
        # ピーク前にも谷→回復があるか（注目後に濁して再上昇）
        for j in range(5, len(cums) - 5):
            window_prev = max(cums[:j])
            if cums[j] < window_prev * 0.85 and max(cums[j:]) > window_prev * 1.05:
                seg = nots[max(0, j-5):j+5]
                if seg and (st.median(seg) < med_not * 0.6):
                    dip_then_new = True
                    break

    return {
        "address": addr, "n_closes": n, "n_closes_maj": sum(1 for f in closes if f.get("coin") in MAJ),
        "real_all": round(real_all), "real_maj": round(real_maj), "win_rate": round(win_rate, 3),
        "active_days": active_days, "last_time": t1,
        "wr_big": round(wr_big, 3) if wr_big is not None else None,
        "wr_small": round(wr_small, 3) if wr_small is not None else None,
        "n_big": len(big), "n_small": len(small),
        "sum_big": round(sum_big), "sum_small": round(sum_small),
        "top3_share": round(top3_share, 3),
        "med_win_not": round(med_win_not), "med_loss_not": round(med_loss_not),
        "size_ratio": round(size_ratio, 2),
        "max_gap_d": round(max_gap_d, 1), "burst_share": round(burst_share, 3),
        "flips": flips, "dip_then_new": dip_then_new,
        "avg_loss": round(st.mean(losses)) if losses else 0,
        "avg_win": round(st.mean(wins)) if wins else 0,
        "coins": sorted({f.get("coin") for f in closes if f.get("coin") in MAJ}),
    }


def patterns(F):
    """各特徴量から欺瞞パターン該当を判定（複数該当可）。スコア付き。"""
    hits = {}
    rm, ra, n = F["real_maj"], F["real_all"], F["n_closes"]
    # A サイズ条件付きスキル
    if (F["wr_big"] is not None and F["wr_small"] is not None
            and F["n_big"] >= 10 and F["n_small"] >= 10
            and F["wr_big"] >= 0.6 and F["wr_small"] <= 0.45
            and F["sum_big"] > 0 and rm > 30000):
        hits["A_size_conditional"] = round((F["wr_big"] - F["wr_small"]) * math.log10(max(rm, 10)), 3)
    # B 非対称ペイオフ
    if F["win_rate"] <= 0.45 and ra >= 100000 and F["top3_share"] >= 0.5 and n >= 15:
        hits["B_asymmetric"] = round(ra * F["top3_share"] / 1e6, 3)
    # C 休眠→覚醒
    if F["max_gap_d"] >= 14 and F["burst_share"] >= 0.4 and ra >= 50000:
        hits["C_dormant_burst"] = round(F["burst_share"] * math.log10(max(ra, 10)), 3)
    # D 制御された負け
    if (F["size_ratio"] >= 2.5 and len(F["coins"]) >= 1 and rm > 50000
            and F["med_loss_not"] > 0 and n >= 20):
        hits["D_controlled_loss"] = round(F["size_ratio"] * math.log10(max(rm, 10)), 3)
    # E 新鮮ウォレット単発大当たり
    if F["active_days"] <= 21 and n <= 15 and ra >= 100000:
        hits["E_fresh_hit"] = round(ra / max(n, 1) / 1e5, 3)
    # F デコイ→本命フリップ
    if F["flips"] >= 3 and rm > 30000:
        hits["F_decoy_flip"] = round(F["flips"] * math.log10(max(rm, 10)), 3)
    # H 勝ち逃げ後の意図的低調
    if F["dip_then_new"] and ra >= 100000 and n >= 30:
        hits["H_deliberate_dip"] = round(math.log10(max(ra, 10)), 3)
    return hits


def main():
    files = [fn for fn in os.listdir(FILLS) if fn.endswith(".json")]
    allF, by_pat = [], {}
    for i, fn in enumerate(files):
        if i % 500 == 0:
            print(f"  {i}/{len(files)} 処理中…")
        try:
            o = json.load(open(os.path.join(FILLS, fn), encoding="utf-8"))
        except Exception:
            continue
        fills = o.get("fills", [])
        if not fills:
            continue
        F = features(fn[:-5], fills)
        if not F:
            continue
        hits = patterns(F)
        if hits:
            F["patterns"] = hits
            allF.append(F)
            for p in hits:
                by_pat.setdefault(p, []).append(F)

    # 各パターン上位を整理
    out = {"summary": {}, "candidates": {}}
    for p, lst in by_pat.items():
        lst.sort(key=lambda f: -f["patterns"][p])
        out["summary"][p] = len(lst)
        out["candidates"][p] = lst[:30]
    # 複数パターン該当（合わせ技＝より怪しい）
    multi = sorted([f for f in allF if len(f["patterns"]) >= 2],
                   key=lambda f: -len(f["patterns"]))
    out["multi_pattern"] = multi[:40]

    json.dump(out, open(os.path.join(config.DATA_DIR, "deception_candidates.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n=== 欺瞞パターン該当数 ===")
    for p in sorted(out["summary"]):
        print(f"  {p}: {out['summary'][p]}件")
    print(f"  複数該当(合わせ技): {len(multi)}件")
    print(f"→ data/deception_candidates.json")


if __name__ == "__main__":
    main()
