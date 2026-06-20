"""欺瞞パターン容疑者の詳細ドシエを作り、workflow裁定の材料にする。

deception_candidates.json から各パターン上位＋合わせ技を厳選し、各ウォレットに:
 - 該当パターンとスコア
 - 勝敗の月次タイムライン（勝ち逃げ/意図的低調を目視できる）
 - 大勝top5 / 負けtop5（負けが小さく揃うか・altに逃がすかを見る）
 - leaderboard通算・台帳の既存分類
を付け、data/deception_dossiers.json に出す。
"""
import os
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

import config

FILLS = os.path.join(config.DATA_DIR, "fills")
MAJ = set(config.COINS)


def dt(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")


def ym(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m")


def cpnl(f):
    try:
        return float(f.get("closedPnl", 0) or 0)
    except Exception:
        return 0.0


def notional(f):
    try:
        return abs(float(f.get("sz", 0) or 0) * float(f.get("px", 0) or 0))
    except Exception:
        return 0.0


def build_dossier(F, reg, lb):
    addr = F["address"]
    p = os.path.join(FILLS, f"{addr}.json")
    if not os.path.exists(p):
        return None
    fills = sorted(json.load(open(p, encoding="utf-8")).get("fills", []), key=lambda f: int(f["time"]))
    closes = [f for f in fills if abs(cpnl(f)) > 1e-9]
    # 月次の勝敗（タイムライン）
    mon = defaultdict(lambda: [0, 0, 0.0, 0.0])  # win,loss,pnl,notional
    for f in closes:
        m = ym(f["time"]); pn = cpnl(f)
        mon[m][0 if pn > 0 else 1] += 1
        mon[m][2] += pn
        mon[m][3] += notional(f)
    timeline = [{"m": m, "win": v[0], "loss": v[1], "pnl": round(v[2]),
                 "avg_not": round(v[3] / max(v[0] + v[1], 1))} for m, v in sorted(mon.items())]
    # 大勝/負け top5
    cl = [{"coin": f.get("coin"), "dir": f.get("dir"), "date": dt(f["time"]),
           "not": round(notional(f)), "pnl": round(cpnl(f)), "maj": f.get("coin") in MAJ} for f in closes]
    top_wins = sorted([c for c in cl if c["pnl"] > 0], key=lambda c: -c["pnl"])[:5]
    top_losses = sorted([c for c in cl if c["pnl"] < 0], key=lambda c: c["pnl"])[:5]
    e = reg.get(addr, {})
    return {
        "address": addr,
        "patterns": F["patterns"],
        "stats": {k: F[k] for k in ("n_closes", "real_all", "real_maj", "win_rate",
                                    "active_days", "wr_big", "wr_small", "n_big", "n_small",
                                    "top3_share", "size_ratio", "med_win_not", "med_loss_not",
                                    "max_gap_d", "burst_share", "flips", "coins")},
        "lb_alltime": lb.get(addr),
        "registry_position": e.get("position"),
        "registry_quality": e.get("wf_quality"),
        "labels": e.get("labels"),
        "timeline": timeline,
        "top_wins": top_wins,
        "top_losses": top_losses,
    }


def main():
    o = json.load(open(os.path.join(config.DATA_DIR, "deception_candidates.json"), encoding="utf-8"))
    cand = o["candidates"]
    # 厳選: Aは全件・稀少パターン重視、ノイズ多のC/Hは上位のみ
    pick = {}
    take = {"A_size_conditional": 7, "B_asymmetric": 15, "C_dormant_burst": 8,
            "D_controlled_loss": 15, "F_decoy_flip": 15, "H_deliberate_dip": 8}
    for pat, k in take.items():
        for F in cand.get(pat, [])[:k]:
            pick[F["address"]] = F
    # 合わせ技は3パターン以上を全部
    for F in o.get("multi_pattern", []):
        if len(F["patterns"]) >= 3:
            pick[F["address"]] = F

    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    lbraw = json.load(open(f"{config.DATA_DIR}/leaderboard.json", encoding="utf-8"))
    rows = lbraw if isinstance(lbraw, list) else lbraw.get("leaderboardRows") or []
    lb = {}
    for r in rows:
        a = (r.get("ethAddress") or "").lower()
        if a:
            try:
                lb[a] = round(float({w: v for w, v in r["windowPerformances"]}["allTime"]["pnl"]))
            except Exception:
                pass

    doss = [d for F in pick.values() if (d := build_dossier(F, reg, lb))]
    json.dump(doss, open(f"{config.DATA_DIR}/deception_dossiers.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"ドシエ {len(doss)}件 → data/deception_dossiers.json")
    from collections import Counter
    pc = Counter()
    for d in doss:
        for p in d["patterns"]:
            pc[p] += 1
    print("内訳:", dict(pc))


if __name__ == "__main__":
    main()
