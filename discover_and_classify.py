"""定期実行用: HL公開APIだけで新規アドレスを発掘し、既存基準で分類して台帳に追加する(Phase3=多窓+イベント)。

発掘入口を allTime PnL 一本から4ソースへ拡張(掬い切ったら終わる網を多様化):
 S1 allTime PnL>=MIN            … 従来(長期の大物)
 S2 week   PnL>=LB_WEEK_MIN ∧ ROI>=LB_WEEK_ROI … 最近急に勝ち出した新興
 S3 month  PnL>=LB_MONTH_MIN    … 直近1ヶ月の勝ち組
 S4 day    PnL>=LB_DAY_MIN      … 建てっぱなし未実現の先行者(+急変イベント方向一致でflag)
 ※4窓とも同一leaderboard GET内=追加APIゼロ。

敵対レビュー反映:
 - 分類ラチェット対策: S2/S3/S4 で rall<=0 は『除外』登録せず data/pending_candidates.json へ退避(翌日再評価)。
 - fresh_whale判定: fills最古日でなく leaderboard窓整合(allTime vlm≒month vlm)でゲート。
 - fills空候補の無言消失禁止: clearinghouseを1回引き、S4×イベント方向一致×大玉なら fills_missing_lead で flagged。
 - flagged永続化: discovery_flagged.jsonl へ append(addr+date dedup)。週次レビュー前に消えない。
 - レート予算: fills取得は CAP_TOTAL 件まで(IP上限内)。超過候補は pending へ繰越。

使い方:
  python discover_and_classify.py [--min 500000] [--cap 150] [--dry-run]
"""
import os
import sys
import json
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta

import config
import hl_client
import hl_fills_cache as fc
import tagging
import deception_scan as ds
from triage_untriaged import pro_grade, hft_tag, QLAB

MAJ = set(config.COINS)
HOUR = 3600_000
PENDING_PATH = f"{config.DATA_DIR}/pending_candidates.json"
FLAGGED_PATH = f"{config.DATA_DIR}/discovery_flagged.jsonl"
REJECTS_PATH = f"{config.DATA_DIR}/event_rejects.json"


def arg(name, default, cast=float):
    if name in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(name) + 1])
        except Exception:
            pass
    return default


def windows(row):
    """windowPerformances を {window: {pnl,roi,vlm}} に整形。"""
    d = {}
    for item in row.get("windowPerformances", []):
        try:
            name, perf = item
        except Exception:
            continue
        d[name] = {"pnl": float(perf.get("pnl", 0) or 0), "roi": float(perf.get("roi", 0) or 0),
                   "vlm": float(perf.get("vlm", 0) or 0)}
    return d


def pick_source(w):
    """4窓から最優先ソースを1つ返す (source, score)。該当なしは None。"""
    at, wk, mo, dy = w.get("allTime", {}), w.get("week", {}), w.get("month", {}), w.get("day", {})
    if at.get("pnl", 0) >= pick_source.MIN:
        return "S1_allTime", at["pnl"]
    if wk.get("pnl", 0) >= config.LB_WEEK_MIN and wk.get("roi", 0) >= config.LB_WEEK_ROI:
        return "S2_week", wk["pnl"]
    if mo.get("pnl", 0) >= config.LB_MONTH_MIN:
        return "S3_month", mo["pnl"]
    if dy.get("pnl", 0) >= config.LB_DAY_MIN:
        return "S4_day", dy["pnl"]
    return None


def is_fresh_whale(w):
    """真の新規=活動が最近に集中(allTime vlm ≒ month vlm)。古参の間引き(allTime vlm>>month)は False。"""
    at, mo = w.get("allTime", {}), w.get("month", {})
    if not at.get("vlm") or not mo.get("vlm"):
        return False
    return (mo["vlm"] / at["vlm"]) >= config.FRESH_VLM_RATIO


def classify(rall, rmaj, cpm, nfills):
    if cpm > 1500 or nfills > 3000:
        return "高頻度MM"
    if rall <= 0:
        return "除外/低優先"
    return "プロトレーダー(本物)" if (rmaj / rall if rall else 0) >= 0.3 else "alt主体プロ"


def event_direction_hit(fl, events):
    """ウォレットの直近fillの建て方向が、直近イベント(EVENT_LEAD_H前まで)の方向と一致するか。
    一致すれば (coin, dir, event_t0) を返す=そのイベントを先取りした可能性。"""
    lead = config.EVENT_LEAD_H * HOUR
    for e in events:
        t0, coin, edir = int(e["t0"]), e["coin"], e["dir"]
        want_buy = (edir == "up")
        for f in fl:
            if f.get("coin") != coin:
                continue
            ft = int(f.get("time", 0))
            if t0 - lead <= ft <= t0:                       # イベント直前の窓
                side = f.get("side")                        # B=買い / A=売り
                is_buy = (side == "B")
                if is_buy == want_buy:
                    return coin, edir, t0
    return None


def szi_direction_hit(a, events):
    """fills空ウォレットの現建玉szi方向が直近イベント方向と一致し大玉か。fills_missing_lead判定用。"""
    try:
        st = hl_client.clearinghouse_state(a) or {}
    except Exception:
        return None
    for ap in st.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        pv = float(p.get("positionValue", 0) or 0)
        coin = p.get("coin")
        if abs(szi) < 1e-9 or pv < config.FRESH_NOTIONAL:
            continue
        for e in events:
            if e["coin"] == coin and ((e["dir"] == "up") == (szi > 0)):
                return {"coin": coin, "dir": e["dir"], "notional": round(pv), "event_t0": int(e["t0"])}
    return None


def load_pending():
    try:
        return json.load(open(PENDING_PATH, encoding="utf-8"))
    except Exception:
        return []


def save_pending(rows):
    tmp = PENDING_PATH + ".tmp"
    json.dump(rows, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, PENDING_PATH)


def append_flagged(recs):
    """discovery_flagged.jsonl へ append(addr+date dedup・read-merge-write。全置換しない)。"""
    if not recs:
        return
    seen = set()
    if os.path.exists(FLAGGED_PATH):
        with open(FLAGGED_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    seen.add((e.get("address"), e.get("date")))
                except Exception:
                    continue
    with open(FLAGGED_PATH, "a", encoding="utf-8") as f:
        for r in recs:
            if (r.get("address"), r.get("date")) in seen:
                continue
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    pick_source.MIN = arg("--min", config.MIN_LB_PNL)
    CAP = int(arg("--cap", config.CANDIDATE_LIMIT))
    MAXAGE = arg("--max-age", 1)
    DRY = "--dry-run" in sys.argv
    now = int(time.time() * 1000)
    cut = now - 14 * 86400 * 1000
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 急変イベント(S4逆引き材料)。失敗しても S1-S3 は続行(縮退)。
    events = []
    try:
        import event_scan
        event_scan.append_events(event_scan.scan())
        events = event_scan.recent_events(hours=72)
    except Exception as e:
        print(f"  event_scan 縮退(S4方向一致は無効): {str(e)[:80]}")

    lb = hl_client.download_leaderboard(max_age_h=MAXAGE)
    rows = lb if isinstance(lb, list) else lb.get("leaderboardRows") or []

    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    have = set(W.keys())

    # 4ソース候補(未登録のみ・最優先source1つ)
    picked = {}
    for r in rows:
        a = (r.get("ethAddress") or "").lower()
        if not a or a in have:
            continue
        w = windows(r)
        src = pick_source(w)
        if src:
            picked[a] = {"source": src[0], "score": src[1], "w": w}
    # source毎に上限、その後ラウンドロビンで交互配置=fills予算を全sourceへ行き渡らせる。
    # 優先順は recent movers 先(S4/S2/S3)→S1最後: S1のallTime巨大群(多くは登録済/fills間引きで空)に
    # 予算を食われてS4(イベント先取り候補)が飢えるのを防ぐ。同source内はscore降順。
    by_src = defaultdict(list)
    for a, v in picked.items():
        by_src[v["source"]].append((a, v))
    ORDER_PREF = ["S4_day", "S2_week", "S3_month", "S1_allTime"]
    pools = {s: sorted(by_src.get(s, []), key=lambda x: -x[1]["score"])[:config.CAP_PER_SOURCE]
             for s in ORDER_PREF}
    ordered = []
    while any(pools.values()) and len(ordered) < CAP:
        for s in ORDER_PREF:
            if pools[s]:
                ordered.append(pools[s].pop(0))
                if len(ordered) >= CAP:
                    break
    cap_fills = min(CAP, config.CAP_TOTAL)
    print(f"候補 {len(ordered)}件 (内訳={ {s: len(by_src[s]) for s in by_src} }) / fills取得上限={cap_fills} / events={len(events)}")

    added = Counter()
    flagged = []
    pending_new = []
    log_recs = []
    reject_keys = set()
    try:
        reject_keys = set(tuple(x) for x in json.load(open(REJECTS_PATH, encoding="utf-8")))
    except Exception:
        pass

    fetched = 0
    for a, v in ordered:
        source, w = v["source"], v["w"]
        fresh = is_fresh_whale(w)
        if fetched >= cap_fills:               # レート予算超過→pendingへ繰越(翌日再評価)
            pending_new.append({"address": a, "source": source, "date": today, "reason": "rate_cap繰越"})
            continue
        fetched += 1
        try:
            fl = fc.get_fills(a)
        except Exception:
            pending_new.append({"address": a, "source": source, "date": today, "reason": "fills取得失敗"})
            continue

        if not fl:
            # fills空=無言でcontinueしない。S4×イベント方向一致×大玉なら fills_missing_lead で flagged。
            if source == "S4_day" and events:
                hit = szi_direction_hit(a, events)
                if hit and (a, hit["event_t0"]) not in reject_keys:
                    flagged.append({"address": a, "date": today, "kind": "fills_missing_lead",
                                    "source": source, **hit})
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
        posr = (sum(1 for x in mon.values() if x > 0) / months) if months else 0
        gw = sum(x for x in mon.values() if x > 0)
        gl = abs(sum(x for x in mon.values() if x < 0))
        pf = (gw / gl) if gl > 0 else 99
        worst = min(mon.values()) if mon else 0
        ts = [int(f["time"]) for f in fl]
        af = datetime.utcfromtimestamp(min(ts) / 1000).strftime("%Y-%m-%d")
        at = datetime.utcfromtimestamp(max(ts) / 1000).strftime("%Y-%m-%d")
        rec = [f for f in fl if int(f["time"]) >= cut]

        pos = classify(rall, rmaj, cpm, len(fl))

        # 分類ラチェット対策: S2/S3/S4 で実現赤字は『除外』登録せず pending へ(埋葬回避・翌日再評価)
        if pos == "除外/低優先" and source != "S1_allTime":
            pending_new.append({"address": a, "source": source, "date": today,
                                "reason": f"rall<=0(${rall:,})→翌日再評価", "rall": rall})
            continue

        ev_hit = event_direction_hit(fl, events) if events else None

        tags = [f"自動発掘({today})", f"発掘源:{source}"]
        if fresh:
            tags.append("新規ウォレット")
        if ev_hit:
            tags.append("イベント先行")
        wq = None
        if pos == "高頻度MM":
            tags.append(hft_tag(cpm))
            wq = pro_grade(months, posr, pf, gl == 0, rall, rmaj, worst)
        elif pos == "プロトレーダー(本物)":
            wq = pro_grade(months, posr, pf, gl == 0, rall, rmaj, worst)
        elif pos == "alt主体プロ":
            wq = "alt主体"

        F = ds.features(a, fl)
        dec_hits = ds.patterns(F) if F else {}
        if dec_hits:
            tags.append("欺瞞候補:要精査")
            flagged.append({"address": a, "date": today, "kind": "deception",
                            "patterns": dec_hits, "rmaj": rmaj, "rall": rall, "source": source})

        ql = ("質:" + wq) if wq else ""
        head = (f"【現在の分類: {QLAB.get(pos, pos)}" + (f" / {ql}" if ql else "")
                + f" / majors実現${rmaj:,} / 最終取引{at}】")
        note = (head + f"\n【自動発掘({today})/{source}】"
                + (f"新規ウォレット。" if fresh else "")
                + (f"急変イベント({ev_hit[0]} {ev_hit[1]})を先取り。" if ev_hit else "")
                + f"回転{cpm}closes/月・黒字月率{posr:.0%}・履歴{months}ヶ月。"
                + ("欺瞞8軸該当: " + ",".join(dec_hits) + "→要精査。" if dec_hits else ""))

        W[a] = {
            "address": a, "first_seen": today, "last_seen": today, "times_seen": 1,
            "position": pos, "wf_quality": wq, "metric_category": "auto_discovered",
            "discovery_source": source, "fresh_whale": fresh, "event_lead": bool(ev_hit),
            "tags": sorted(tags), "mm_cpm": cpm if pos == "高頻度MM" else None,
            "roi_alltime": w.get("allTime", {}).get("roi", 0), "lb_alltime": round(w.get("allTime", {}).get("pnl", 0)),
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
        log_recs.append({"date": today, "address": a, "position": pos, "wf_quality": wq, "source": source,
                         "rmaj": rmaj, "rall": rall, "fresh": fresh, "event_lead": bool(ev_hit),
                         "flagged": bool(dec_hits)})

    if DRY:
        outdir = os.path.join(config.HERE, "dry_run_out")
        os.makedirs(outdir, exist_ok=True)
        json.dump({"added": dict(added), "n_candidates": len(ordered), "fetched": fetched,
                   "flagged": flagged, "pending_new": pending_new, "log": log_recs},
                  open(os.path.join(outdir, "discover_dryrun.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"[dry-run] 追加見込 {sum(added.values())}件 内訳={dict(added)} / flagged {len(flagged)} / "
              f"pending {len(pending_new)} → {outdir}/discover_dryrun.json (台帳は変更せず)")
        return

    # pending 永続化(既存とマージ・addr+date dedup)
    pend = load_pending()
    pseen = {(r.get("address"), r.get("date")) for r in pend}
    for r in pending_new:
        if (r["address"], r["date"]) not in pseen:
            pend.append(r)
    cutoff = (datetime.utcnow().date() - timedelta(days=90)).isoformat()
    pend = [r for r in pend if (r.get("date") or "") >= cutoff][-5000:]
    save_pending(pend)
    append_flagged(flagged)

    if not added:
        print(f"新規追加なし。pending {len(pending_new)} / flagged {len(flagged)}")
        return

    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = P + ".tmp"
    json.dump(reg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, P)
    import step6_registry as reg6
    reg6.render_all(reg)
    with open(f"{config.DATA_DIR}/discovery_log.jsonl", "a", encoding="utf-8") as f:
        for r in log_recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n台帳へ追加 {sum(added.values())}件  内訳={dict(added)}")
    print(f"flagged(要精査) {len(flagged)}件 → {FLAGGED_PATH} / pending {len(pending_new)}件 → {PENDING_PATH}")
    print(f"台帳総数: {len(W)} / log: data/discovery_log.jsonl")


if __name__ == "__main__":
    main()
