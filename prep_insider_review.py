"""インサイダー疑惑/弱疑惑の workflow再精査用に、往復・反復の新規計算 + 文脈を1ファイルに集約。

各対象に:
 - 台帳メトリクス/notes/funders/tags
 - insider_v2 の往復明細(strict/medium/loose): rt(反復) / lead_only / large_clusters / norm_rate / detail(往復イベント日)
 - 先行が「異なる日付/イベント」に何回分布するか（N=1判定: ユニークイベント数）
出力: data/wf_insider_review.json
"""
import json
import time
from datetime import datetime, timezone
from collections import Counter

import config
import insider_v2 as iv

TARGET = {"インサイダー疑惑(要監視)", "弱い疑惑(監視継続)"}
MS_H = 3600 * 1000


def lead_unique_events(addr, events):
    """先行(大口・lead窓内・同方向)が当たったユニークなイベント数を tier別に返す（N=1の暴露）。"""
    fills = iv.hl_client.user_fills_by_time(addr, 0, int(time.time() * 1000))
    maj = [f for f in fills if f.get("coin") in config.COINS]
    opens = {}
    for f in maj:
        d = iv.open_dir(f.get("dir"))
        if d in ("long", "short"):
            opens.setdefault((f["coin"], d), []).append((int(f["time"]), float(f["px"]) * float(f["sz"])))
    out = {}
    for name, lh, eh, lg in iv.TIERS:
        lead_ms = lh * MS_H
        hit_events = set()
        for ev in events:
            coin, t0, d = ev["coin"], ev["t0"], ev["dir"]
            if any(t0 - lead_ms <= t <= t0 and notl >= lg for t, notl in opens.get((coin, d), [])):
                hit_events.add((coin, t0))
        out[name] = len(hit_events)
    return out


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    targets = [(k, e) for k, e in reg.items() if e.get("position") in TARGET]
    print(f"対象 {len(targets)} 件")

    now = int(time.time() * 1000)
    events = iv.build_events(now - 560 * 24 * MS_H, now)
    print(f"急変イベント {len(events)} 件")

    rows = []
    for i, (k, e) in enumerate(targets, 1):
        cur = e.get("current", {})
        try:
            rt = iv.analyze(e["address"], events)
        except Exception as ex:
            rt = {"error": str(ex)[:60]}
        try:
            uniq = lead_unique_events(e["address"], events)
        except Exception:
            uniq = {}
        rows.append({
            "address": e["address"],
            "position": e.get("position"),
            "labels": e.get("labels") or [],
            "tags": [t for t in e.get("tags", []) if not t.startswith("funder:")],
            "metrics": {
                "win_rate": cur.get("win_rate"), "dir_accuracy": cur.get("dir_accuracy"),
                "total_pnl": cur.get("total_pnl"), "event_lead_notional": cur.get("event_lead_notional"),
                "trade_days": e.get("trade_days"), "active14": e.get("active14"),
            },
            "first_funders": [{"label": f.get("label"), "address": f.get("address"),
                               "time": f.get("time")} for f in (e.get("first_funders") or [])[:3]],
            "roundtrip": rt.get("tiers") if isinstance(rt, dict) else None,
            "lead_unique_events": uniq,   # 先行が当たったユニークイベント数（1ならN=1）
            "notes_jp": e.get("notes_jp", ""),
            "wf_prev_verdict": next((t for t in e.get("tags", []) if t.startswith("WF:")), None),
        })
        print(f"  [{i}/{len(targets)}] {e['address'][:12]}.. rt(strict)="
              f"{(rt.get('tiers',{}).get('strict',{}) or {}).get('rt') if isinstance(rt,dict) else '?'} "
              f"uniqLead(strict)={uniq.get('strict')}")

    ctx = {
        "purpose": "BTC/ETH/SOL perp(Hyperliquid)のインサイダー疑惑を別視点で再精査",
        "definitions": {
            "先行": "急変イベント(4h足±3%)のlead窓内に同方向で大口建玉。時刻一致であって情報優位の証明ではない",
            "往復(round-trip)": "1イベントで『先行建て→急変後に同方向を利確』が成立=1回",
            "反復(rt)": "別々の急変イベントで往復が成立した回数。分割約定は(銘柄,方向,1h)で集約しN=1水増しを排除",
            "lead_unique_events": "先行が当たったユニークなイベント数。1なら先行はN=1(一発)",
            "norm_rate": "往復回数 ÷ 大口の賭け総数。高いほど選別的に当てている",
            "tiers": "strict(6h/12h/$100k), medium(12h/24h/$50k), loose(24h/48h/$25k)",
        },
        "prior_finding": "これまで先行はことごとくN=1(分割約定の一発)に化け、反復≥3級は未検出。cluster-Aも公開エアドロップ判明で否定済み。",
        "n_events": len(events),
    }
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "context": ctx, "wallets": rows}
    path = f"{config.DATA_DIR}/wf_insider_review.json"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n保存 → {path}")
    print("反復(strict rt>=2)該当:", [r["address"][:10] for r in rows
          if (r.get("roundtrip") or {}).get("strict", {}).get("rt", 0) >= 2])


if __name__ == "__main__":
    main()
