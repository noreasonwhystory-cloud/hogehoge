"""定期実行用: HL公開APIだけで新規アドレスを発掘し、既存基準で分類して台帳に追加する。

Nansen不要。既存の部品を再利用:
 - hl_client.download_leaderboard … HL全トレーダーの成績(発掘母集団)
 - hl_fills_cache.get_fills        … 約定取得(新しい順・永続キャッシュ)
 - triage_untriaged の pro_grade/hft_tag … プロ品質・HFT回転速度グレード
 - deception_scan の features/patterns    … 欺瞞インサイダー候補フラグ
 - step6_registry.render_all              … HTML再生成

処理:
 1) leaderboard を更新し allTime PnL>=閾値 かつ 台帳未登録 を新規候補に
 2) 各候補の約定を取得→cache真値で指標算出
 3) 既存ロジックで分類: 高頻度MM / プロトレーダー(本物) / alt主体プロ / 除外
 4) 欺瞞8軸パターンに該当したら『欺瞞候補:要精査』タグ(後でworkflow裁定→弱い疑惑へ昇格)
 5) 台帳へ追記(既存は一切上書きしない)→auto_tags再計算→全ページ再描画
 6) data/discovery_log.jsonl に追加分を記録

使い方:
  python discover_and_classify.py                 # 既定(min=$30万・cap200)
  python discover_and_classify.py --min 1000000 --cap 50 --max-age 1
定期実行(タスクスケジューラ/cron)で回す想定。
"""
import os
import sys
import json
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone

import config
import hl_client
import hl_fills_cache as fc
import tagging
import deception_scan as ds
from triage_untriaged import pro_grade, hft_tag, QLAB

MAJ = set(config.COINS)


def arg(name, default, cast=float):
    if name in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(name) + 1])
        except Exception:
            pass
    return default


def at_pnl(row):
    for name, perf in row.get("windowPerformances", []):
        if name == "allTime":
            return float(perf.get("pnl", 0) or 0), float(perf.get("roi", 0) or 0)
    return 0.0, 0.0


def classify(rall, rmaj, cpm, nfills):
    """既存と同じ決定的分類。"""
    if cpm > 1500 or nfills > 3000:
        return "高頻度MM"
    if rall <= 0:
        return "除外/低優先"
    return "プロトレーダー(本物)" if (rmaj / rall if rall else 0) >= 0.3 else "alt主体プロ"


def main():
    MIN = arg("--min", 300_000)
    CAP = int(arg("--cap", 200))
    MAXAGE = arg("--max-age", 1)
    now = int(time.time() * 1000)
    cut = now - 14 * 86400 * 1000
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lb = hl_client.download_leaderboard(max_age_h=MAXAGE)
    rows = lb if isinstance(lb, list) else lb.get("leaderboardRows") or []

    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    have = set(W.keys())

    # 新規候補: allTime PnL>=MIN かつ 未登録。PnL降順で cap 件
    cand = []
    for r in rows:
        a = (r.get("ethAddress") or "").lower()
        if not a or a in have:
            continue
        pnl, roi = at_pnl(r)
        if pnl >= MIN:
            cand.append((a, pnl, roi))
    cand.sort(key=lambda x: -x[1])
    cand = cand[:CAP]
    print(f"新規候補 {len(cand)}件 (allTime PnL>=${MIN:,.0f}, 上限{CAP})")

    added = Counter()
    flagged = []
    log_recs = []
    for i, (a, lbpnl, roi) in enumerate(cand):
        if i % 25 == 0:
            print(f"  {i}/{len(cand)} 分類中…")
        try:
            fl = fc.get_fills(a)            # 取得+キャッシュ
        except Exception:
            continue
        if not fl:
            continue
        closes = [f for f in fl if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
        ncl = len(closes)
        rmaj = round(sum(float(f["closedPnl"]) for f in closes if f.get("coin") in MAJ))
        rall = round(sum(float(f["closedPnl"]) for f in closes))
        mon = defaultdict(float)
        for f in closes:
            mon[datetime.utcfromtimestamp(int(f["time"]) / 1000).strftime("%Y-%m")] += float(f["closedPnl"])
        months = len(mon)
        cpm = int(ncl / max(months, 1))
        posr = (sum(1 for v in mon.values() if v > 0) / months) if months else 0
        gw = sum(v for v in mon.values() if v > 0)
        gl = abs(sum(v for v in mon.values() if v < 0))
        pf = (gw / gl) if gl > 0 else 99
        worst = min(mon.values()) if mon else 0
        ts = [int(f["time"]) for f in fl]
        af = datetime.utcfromtimestamp(min(ts) / 1000).strftime("%Y-%m-%d")
        at = datetime.utcfromtimestamp(max(ts) / 1000).strftime("%Y-%m-%d")
        rec = [f for f in fl if int(f["time"]) >= cut]

        pos = classify(rall, rmaj, cpm, len(fl))
        tags = [f"自動発掘({today})"]
        wq = None
        if pos == "高頻度MM":
            tags.append(hft_tag(cpm))
            wq = pro_grade(months, posr, pf, gl == 0, rall, rmaj, worst)
        elif pos == "プロトレーダー(本物)":
            wq = pro_grade(months, posr, pf, gl == 0, rall, rmaj, worst)
        elif pos == "alt主体プロ":
            wq = "alt主体"

        # 欺瞞インサイダー候補フラグ(既存8軸特徴量)
        F = ds.features(a, fl)
        dec_hits = ds.patterns(F) if F else {}
        if dec_hits:
            tags.append("欺瞞候補:要精査")
            flagged.append({"address": a, "patterns": dec_hits, "rmaj": rmaj, "rall": rall})

        ql = ("質:" + wq) if wq else ""
        head = (f"【現在の分類: {QLAB.get(pos, pos)}" + (f" / {ql}" if ql else "")
                + f" / majors実現${rmaj:,} / 最終取引{at}】")
        note = (head + f"\n【自動発掘({today})】HLリーダーボード(allTime ${lbpnl:,.0f})から発掘し既存基準で分類。"
                f"回転{cpm}closes/月・黒字月率{posr:.0%}・履歴{months}ヶ月。"
                + ("欺瞞8軸該当: " + ",".join(dec_hits) + "→要精査。" if dec_hits else ""))

        W[a] = {
            "address": a, "first_seen": today, "last_seen": today, "times_seen": 1,
            "position": pos, "wf_quality": wq, "metric_category": "auto_discovered",
            "tags": sorted(tags), "mm_cpm": cpm if pos == "高頻度MM" else None,
            "roi_alltime": roi, "lb_alltime": round(lbpnl),
            "true_realized_all": rall, "true_realized_maj": rmaj, "n_closes": ncl,
            "current": {"win_rate": None, "total_pnl": rmaj, "dir_accuracy": None, "metric_category": "auto_discovered"},
            "active_from": af, "active_to": at, "active14": bool(rec),
            "n_fills_14d": len(rec), "n_fills_14d_maj": sum(1 for f in rec if f.get("coin") in MAJ),
            "history": [],
            "auto_tags": [t for t in (tagging.pnl_tier(rall), tagging.freq_tier(len(fl))) if t]
                         + ([tagging.coin_tag(None, {c: n for c, n in Counter(f.get("coin") for f in fl).most_common()})] if fl else [])
                         + ["取引あり(14d)" if rec else "取引なし(14d)"],
            "notes_jp": note,
        }
        added[pos] += 1
        log_recs.append({"date": today, "address": a, "position": pos, "wf_quality": wq,
                         "rmaj": rmaj, "rall": rall, "flagged": bool(dec_hits)})

    if not added:
        print("新規追加なし。")
        return

    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    import step6_registry as reg6
    reg6.render_all(reg)
    with open(f"{config.DATA_DIR}/discovery_log.jsonl", "a", encoding="utf-8") as f:
        for r in log_recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 欺瞞候補は別ファイルへ(workflow裁定の入力)
    if flagged:
        json.dump(flagged, open(f"{config.DATA_DIR}/discovery_flagged.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    print(f"\n台帳へ追加 {sum(added.values())}件  内訳={dict(added)}")
    print(f"欺瞞候補(要精査)フラグ: {len(flagged)}件 → data/discovery_flagged.json")
    print(f"台帳総数: {len(W)} / log: data/discovery_log.jsonl")


if __name__ == "__main__":
    main()
