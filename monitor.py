"""監視対象(弱い疑惑＝遅効エッジ／プロのエリート・堅実)のリアルタイム監視。

HL公開API(無料・無認証)で各対象の最新約定＋現在建玉を取得し:
 - 前回監視以降の新規アクション(建て/決済)を検出し data/monitor_feed.jsonl に追記
 - 現在の建玉(銘柄/方向/含み損益)とライブ状況を live.html に出力
増分キャッシュ(最新優先)ゆえ毎回軽量。cron/スケジュール or ループで定期実行する想定。
使い方: python monitor.py        # 1回分の巡回
        python monitor.py --watch all   # プロ全部も対象に
"""
import os
import sys
import json
import time
import html as H
from datetime import datetime, timezone

import config
import hl_client
import hl_fills_cache as fc

FEED = f"{config.DATA_DIR}/monitor_feed.jsonl"
STATE = f"{config.DATA_DIR}/monitor_state.json"


def watch_set(mode):
    W = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    sel = []
    for k, e in W.items():
        pos = e.get("position")
        wq = e.get("wf_quality")
        tags = e.get("tags", [])
        if "局所検証済(真の遅効エッジ)" in tags:
            sel.append((k, "🟠遅効エッジ", e))
        elif "欺瞞精査:要監視" in tags:
            sel.append((k, "🎭欺瞞要監視", e))
        elif pos == "プロトレーダー(本物)" and wq == "エリート":
            sel.append((k, "🟢エリート", e))
        elif pos == "プロトレーダー(本物)" and wq == "堅実" and mode != "min":
            sel.append((k, "🟢堅実", e))
        elif mode == "all" and pos in ("プロトレーダー(本物)", "alt主体プロ"):
            sel.append((k, "プロ", e))
    return sel


def positions(addr):
    st = hl_client.clearinghouse_state(addr) or {}
    acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
    out = []
    for ap in st.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        if abs(szi) < 1e-9:
            continue
        out.append({"coin": p.get("coin"), "dir": "L" if szi > 0 else "S",
                    "value": round(float(p.get("positionValue", 0) or 0)),
                    "entry": p.get("entryPx"), "upnl": round(float(p.get("unrealizedPnl", 0) or 0))})
    return acct, out


def main():
    mode = "all" if "--watch" in sys.argv and "all" in sys.argv else "std"
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    now = datetime.now(timezone.utc)
    ws = watch_set(mode)
    print(f"監視対象 {len(ws)}件 巡回 {now.strftime('%Y-%m-%d %H:%M')}UTC")

    rows, newacts = [], 0
    feed = open(FEED, "a", encoding="utf-8")
    for k, role, e in ws:
        try:
            fills = fc.get_fills(k, max_pages=4, refresh=True)   # 増分(最新優先)で軽量
        except Exception:
            fills = fc.get_fills(k, refresh=False)
        maj = sorted(fills, key=lambda f: int(f["time"]))
        last_seen = state.get(k, 0)
        fresh = [f for f in maj if int(f["time"]) > last_seen]
        if maj:
            state[k] = max(int(f["time"]) for f in maj)
        # 新規アクションをfeedへ
        for f in fresh[-20:]:
            act = {"t": int(f["time"]), "addr": k, "role": role, "coin": f.get("coin"),
                   "dir": f.get("dir"), "px": f.get("px"), "sz": f.get("sz"),
                   "closedPnl": f.get("closedPnl"),
                   "when": datetime.fromtimestamp(int(f["time"]) / 1000, timezone.utc).strftime("%m-%d %H:%M")}
            feed.write(json.dumps(act, ensure_ascii=False) + "\n"); newacts += 1
        try:
            acct, pos = positions(k)
        except Exception:
            acct, pos = 0, []
        last_t = max((int(f["time"]) for f in maj), default=0)
        rows.append({"addr": k, "role": role, "acct": acct, "pos": pos,
                     "last": datetime.fromtimestamp(last_t / 1000, timezone.utc).strftime("%m-%d %H:%M") if last_t else "-",
                     "fresh": len(fresh), "label": (e.get("labels") or [""])[0]})
    feed.close()
    json.dump(state, open(STATE, "w", encoding="utf-8"))

    # live.html
    def posfmt(pos):
        if not pos:
            return "<span class=mut>建玉なし(フラット)</span>"
        return " ".join(f"<span class='p {('l' if p['dir']=='L' else 's')}'>{H.escape(p['coin'])}{p['dir']} ${p['value']:,}"
                        f"(<span class={'g' if p['upnl']>=0 else 'r'}>{p['upnl']:+,}</span>)</span>" for p in pos)
    rows.sort(key=lambda r: (r["role"], -r["acct"]))
    body = "".join(
        f"<tr class='{'act' if r['fresh'] else ''}'><td>{r['role']}</td><td><code>{r['addr'][:14]}…</code><br><span class=mut>{H.escape(r['label'][:22])}</span></td>"
        f"<td>${r['acct']:,}</td><td>{posfmt(r['pos'])}</td><td>{r['last']}{' 🆕'+str(r['fresh']) if r['fresh'] else ''}</td></tr>"
        for r in rows)
    doc = f"""<!doctype html><html lang=ja><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=120><title>ライブ監視</title><style>
body{{font-family:system-ui,sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:22px;font-size:13px}}
h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:12px}} a{{color:#4ea1ff}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #232a34;padding:7px 9px;text-align:left;vertical-align:top}} th{{background:#10151c}}
code{{font-size:11px}} .mut{{color:#8b949e;font-size:11px}} tr.act td{{background:#16142a}}
.p{{display:inline-block;border-radius:8px;padding:1px 7px;margin:1px;font-size:11px}} .p.l{{background:#0f2a1a}} .p.s{{background:#2a1015}}
.g{{color:#69d98a}} .r{{color:#ff8893}}</style></head><body>
<h1>📡 ライブ監視 — 遅効エッジ／プロ</h1>
<div class="sub">HL公開APIで巡回。120秒毎に自動リロード。最終巡回 {now.strftime('%Y-%m-%d %H:%M:%S')}UTC ／ 新規アクション {newacts}件 ／ <a href=index.html>トップ</a>
　※建玉・含み損益はリアルタイム、🆕は前回巡回以降の新規約定数。</div>
<table><tr><th>区分</th><th>アドレス</th><th>口座</th><th>現在の建玉(含み損益)</th><th>最終約定</th></tr>{body}</table>
</body></html>"""
    open(f"{config.HERE}/live.html", "w", encoding="utf-8").write(doc)
    print(f"  新規アクション {newacts}件 / live.html 更新 / feed追記 {FEED}")


if __name__ == "__main__":
    main()
