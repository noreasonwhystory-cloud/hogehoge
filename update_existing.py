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
    for _ in range(max_pages):
        ch = hl_client._post_info({"type": "userFillsByTime", "user": addr,
                                   "startTime": cur, "endTime": now})
        if not ch:
            break
        out.extend(ch)
        if len(ch) < 2000:
            break
        last = ch[-1]["time"]
        if last <= cur:
            break
        cur = last + 1
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
            e["upd_last_ms"] = max(cp, now - DAY)
            e["active14"] = cp >= cut
            e["n_fills_14d"] = 0
            e["n_fills_14d_maj"] = 0
        if e["active14"]:
            active += 1
        # 真の実現が赤字転落した生分類は除外へ
        if (e.get("true_realized_all") or 0) <= 0 and e.get("position") in LIVE:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            from_pos, from_q = e.get("position"), e.get("wf_quality")
            e["demoted_at"] = today                 # 降格日(構造化)
            e["demoted_from"] = from_pos            # 降格前の区分
            e["demoted_from_q"] = from_q            # 降格前の品質
            demotions.append({"address": k, "date": today, "from": from_pos, "from_q": from_q,
                              "to": "除外/低優先", "reason": f"真の実現が通算赤字(${e.get('true_realized_all'):,})に転落",
                              "first_seen": e.get("first_seen")})
            e["position"] = "除外/低優先"
            e["wf_quality"] = None
            e["notes_jp"] = (f"【差分更新({today})】真の実現が通算赤字"
                             f"(${e.get('true_realized_all'):,})に転落→除外へ再分類。\n" + (e.get("notes_jp") or ""))
            demoted += 1
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
    print(f"既存更新: 新規約定あり {upd}件 / 14日内アクティブ {active}件 / 赤字転落除外 {demoted}件 (対象{len(items)}) / demotions.json {len(prev)}件")


if __name__ == "__main__":
    main()
