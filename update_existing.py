"""既存ウォレットの軽量差分更新（GCP CE 日次cron用）。

全履歴(12GBキャッシュ)を持たずに、各ウォレットのチェックポイント(upd_last_ms)以降の
新規約定だけHLから取得して更新する:
 - true_realized_all/maj（新規closedPnlを加算・二重計上なし）→ current.total_pnl
 - active_to / active14 / n_fills_14d（直近14日窓を毎回取得して正確に）
 - n_closes（新規クローズ数を加算）
 - 真の実現が通算赤字に転落した生分類は『除外/低優先』へ再分類
 - PnL/活動の auto_tags を最新化（品質グレード・頻度/銘柄タグは据え置き＝軽量）

使い方: python update_existing.py [--limit N]
"""
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import config
import hl_client
import tagging

MAJ = set(config.COINS)
DAY = 86_400_000
LIVE = {"プロトレーダー(本物)", "alt主体プロ", "弱い疑惑(監視継続)",
        "💸 出金疑い(要監視)", "インサイダー疑惑(要監視)"}


def ckpt_ms(e):
    """前回処理済みの最終約定時刻(ms)。無ければ active_to の翌日0時（既計上分の二重加算回避）。"""
    if e.get("upd_last_ms"):
        return int(e["upd_last_ms"])
    at = e.get("active_to")
    if at:
        try:
            dt = datetime.strptime(at, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000) + DAY
        except Exception:
            pass
    return 0


def fetch_since(addr, start, max_pages=8):
    out, cur, now = [], max(start, 0), int(time.time() * 1000)
    truncated = True   # max_pages分回り切ったら打ち切り(最終ページ2000件=以降が残ってる)
    for _ in range(max_pages):
        ch = hl_client._post_info({"type": "userFillsByTime", "user": addr,
                                   "startTime": cur, "endTime": now})
        if ch is None:   # 429/5xx枯渇=取得失敗。None(失敗)と[](成功0件)を区別し失敗はraise→呼元exceptでwallet skip(cp据え置き=ラチェット/PnL欠落を防ぐ)
            raise RuntimeError("HL userFillsByTime 取得失敗")
        if not ch:       # 成功で0件(=以降無し)
            truncated = False
            break
        out.extend(ch)
        if len(ch) < 2000:
            truncated = False
            break
        last = ch[-1]["time"]
        if last <= cur:
            truncated = False
            break
        cur = last + 1
    if truncated:
        print(f"  ⚠ {addr[:10]} fills打ち切り(max_pages={max_pages}=~{max_pages*2000}件超・14日窓過小の可能性)")
    seen, ded = set(), []
    for f in out:
        t = f.get("tid")
        if t in seen:
            continue
        seen.add(t)
        ded.append(f)
    return ded


def main():
    limit = None
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except Exception:
            pass
    P = f"{config.DATA_DIR}/wallet_registry.json"
    reg = json.load(open(P, encoding="utf-8"))
    W = reg["wallets"]
    now = int(time.time() * 1000)
    cut = now - 14 * DAY
    items = list(W.items())[:limit] if limit else list(W.items())
    upd = demoted = active = 0
    demotions = []   # この巡で降格した記録(data/demotions.jsonへ)
    repromotions = []   # 自動発掘×除外→黒字化した再昇格候補(data/repromote_queue.jsonへ)
    for i, (k, e) in enumerate(items):
        if i % 100 == 0:
            print(f"  {i}/{len(items)} 更新中…")
        cp = ckpt_ms(e)
        try:
            fills = fetch_since(k, min(cp, cut))    # 実現は>cpのみ加算・14日窓は必ず取得
        except Exception:
            continue
        if fills:
            na = sum(float(f.get("closedPnl", 0) or 0) for f in fills if int(f["time"]) > cp)
            nm = sum(float(f.get("closedPnl", 0) or 0) for f in fills
                     if int(f["time"]) > cp and f.get("coin") in MAJ)
            e["true_realized_all"] = round((e.get("true_realized_all") or 0) + na)
            e["true_realized_maj"] = round((e.get("true_realized_maj") or 0) + nm)
            e.setdefault("current", {})["total_pnl"] = e["true_realized_maj"]
            e["n_closes"] = (e.get("n_closes") or 0) + sum(
                1 for f in fills if int(f["time"]) > cp and abs(float(f.get("closedPnl", 0) or 0)) > 1e-9)
            maxt = max(int(f["time"]) for f in fills)
            e["upd_last_ms"] = maxt
            e["active_to"] = datetime.utcfromtimestamp(maxt / 1000).strftime("%Y-%m-%d")
            rec = [f for f in fills if int(f["time"]) >= cut]
            e["n_fills_14d"] = len(rec)
            e["n_fills_14d_maj"] = sum(1 for f in rec if f.get("coin") in MAJ)
            e["active14"] = maxt >= cut
            upd += 1
        else:
            # 14日窓が空(=休眠 or API成功で0件)。cpをラチェット前進させない=次巡でactive14恒久True化/PnL欠落を防ぐ。
            # API失敗は _post_info が raise→fetch_since raise→上のexcept continue でこの分岐に来ない(取得成功の0件のみ)。
            e["active14"] = cp >= cut          # cp=実last-fill(upd_last_ms) or active_to+1日。休眠は自然にFalse
            e["n_fills_14d"] = 0
            e["n_fills_14d_maj"] = 0
        if e["active14"]:
            active += 1
        # 真の実現が赤字転落した生分類は除外へ
        if (e.get("true_realized_all") or 0) <= 0 and e.get("position") in LIVE:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            from_pos, from_q = e.get("position"), e.get("wf_quality")
            tra = e.get("true_realized_all") or 0   # None安全(f-stringの:,がNoneでTypeError)
            e["demoted_at"] = today                 # 降格日(構造化)
            e["demoted_from"] = from_pos            # 降格前の区分
            e["demoted_from_q"] = from_q            # 降格前の品質
            demotions.append({"address": k, "date": today, "from": from_pos, "from_q": from_q,
                              "to": "除外/低優先", "reason": f"真の実現が通算赤字(${tra:,})に転落",
                              "first_seen": e.get("first_seen")})
            e["position"] = "除外/低優先"
            e["wf_quality"] = None
            e["notes_jp"] = (f"【差分更新({today})】真の実現が通算赤字"
                             f"(${tra:,})に転落→除外へ再分類。\n" + (e.get("notes_jp") or ""))
            demoted += 1
        # 再昇格キュー: 自動発掘で除外された後に実現が黒字化→再分類の入力へ(埋葬の一方向ラチェット解消)。
        # 自動flipはせず repromote_queue.json に積む(discover/裁定側で再評価=早く見つけた者を永久に埋めない)。
        elif (e.get("true_realized_all") or 0) > 0 and e.get("position") == "除外/低優先" \
                and e.get("metric_category") == "auto_discovered":
            repromotions.append({"address": k, "date": datetime.utcnow().strftime("%Y-%m-%d"),
                                 "rall": e.get("true_realized_all"), "rmaj": e.get("true_realized_maj"),
                                 "demoted_from": e.get("demoted_from"), "source": e.get("discovery_source"),
                                 "first_seen": e.get("first_seen")})
        # PnL/活動 auto_tags を最新化（頻度/銘柄/品質は据え置き）
        at = [t for t in e.get("auto_tags", [])
              if not (t.startswith("取引あり") or t.startswith("取引なし") or t.startswith("PnL:"))]
        pt = tagging.pnl_tier(e.get("true_realized_all"))
        if pt:
            at.append(pt)
        at.append("取引あり(14d)" if e["active14"] else "取引なし(14d)")
        e["auto_tags"] = at

    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(reg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 降格を data/demotions.json へ追記(既存とマージ・直近90日のみ保持・アドレス+日付でdedup)
    DP = f"{config.DATA_DIR}/demotions.json"
    try:
        prev = json.load(open(DP, encoding="utf-8"))
    except Exception:
        prev = []
    seen = {(r.get("address"), r.get("date")) for r in prev}
    for r in demotions:
        if (r["address"], r["date"]) not in seen:
            prev.append(r)
    cutoff = (datetime.utcnow().date() - timedelta(days=90)).isoformat()
    prev = [r for r in prev if (r.get("date") or "") >= cutoff]
    prev.sort(key=lambda r: r.get("date") or "", reverse=True)
    json.dump(prev, open(DP, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 再昇格キュー data/repromote_queue.json へ追記(既存マージ・addr+date dedup・直近90日)
    RP = f"{config.DATA_DIR}/repromote_queue.json"
    try:
        rprev = json.load(open(RP, encoding="utf-8"))
    except Exception:
        rprev = []
    rseen = {(r.get("address"), r.get("date")) for r in rprev}
    for r in repromotions:
        if (r["address"], r["date"]) not in rseen:
            rprev.append(r)
    rprev = [r for r in rprev if (r.get("date") or "") >= cutoff]
    rprev.sort(key=lambda r: r.get("date") or "", reverse=True)
    json.dump(rprev, open(RP, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"既存更新: 新規約定あり {upd}件 / 14日内アクティブ {active}件 / 赤字転落除外 {demoted}件 "
          f"/ 再昇格候補 {len(repromotions)}件 (対象{len(items)}) / demotions {len(prev)} / repromote {len(rprev)}")


if __name__ == "__main__":
    main()
