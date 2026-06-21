"""HL リアルタイム監視デーモン（GCP Compute Engine 常駐用）。

監視対象(プロ/MM/弱い疑惑)のポジション エントリー/クローズを HL WebSocket で即検知し、
カテゴリ別の Discord チャンネルへ通知。現在の建玉は定期ポーリングしてライブページ(:PORT)で確認。

通知ルーティング(env):
 HOOK_INSIDER … 弱い疑惑/インサイダー/出金疑い
 HOOK_ELITE/SOLID/MID/MURA/THIN/ALT … wf_quality(エリート/堅実/中堅/ムラあり/履歴薄/alt主体)別
通知方針:
 - 本物/alt主体プロ・弱い疑惑 … WS userFills を購読し Open/Close を即通知(方向ロング/ショート明示)
 - 高頻度MM … 約定膨大ゆえ毎fill通知せず、ポーリングで大口ポジ変動(>MM_NOTIFY_MIN)のみ通知

その他env: WATCH_PATH / PORT / POLL_SEC / MM_NOTIFY_MIN
"""
import os
import json
import time
import html
import asyncio
from urllib.request import urlopen

import websockets
import aiohttp
from aiohttp import web

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
WATCH_PATH = os.environ.get("WATCH_PATH", os.path.join(os.path.dirname(__file__), "data", "watch_addresses.json"))
PORT = int(os.environ.get("PORT", "8080"))
POLL_SEC = int(os.environ.get("POLL_SEC", "60"))
MM_NOTIFY_MIN = float(os.environ.get("MM_NOTIFY_MIN", "500000"))

# カテゴリ別 Discord webhook
HOOKS = {
    "insider": os.environ.get("HOOK_INSIDER", ""),
    "エリート": os.environ.get("HOOK_ELITE", ""),
    "堅実": os.environ.get("HOOK_SOLID", ""),
    "中堅": os.environ.get("HOOK_MID", ""),
    "ムラあり": os.environ.get("HOOK_MURA", ""),
    "履歴薄/評価不能": os.environ.get("HOOK_THIN", ""),
    "alt主体": os.environ.get("HOOK_ALT", ""),
}
INSIDER_POS = {"弱い疑惑(監視継続)", "インサイダー疑惑(要監視)", "💸 出金疑い(要監視)"}

WATCH = {}
POSITIONS = {}
FEED = []
PREV = {}
STATE = {"started": int(time.time()), "ws": "init", "last_poll": 0, "events": 0}


def load_watch():
    if WATCH_PATH.startswith("http"):
        data = json.loads(urlopen(WATCH_PATH, timeout=30).read())
    else:
        data = json.load(open(WATCH_PATH, encoding="utf-8"))
    return {w["address"].lower(): w for w in data}


def hook_for(w):
    """ウォレットの分類から通知先チャンネルを決める。"""
    if w.get("position") in INSIDER_POS:
        return HOOKS.get("insider")
    return HOOKS.get(w.get("wf_quality"))


async def discord(session, hook, title, desc, color):
    if not hook:
        return
    try:
        await session.post(hook, json={"embeds": [{"title": title[:240], "description": desc[:3500], "color": color}]},
                           timeout=aiohttp.ClientTimeout(total=15))
    except Exception:
        pass


def label_of(a):
    w = WATCH.get(a, {})
    return (w.get("label") or a[:10]), w.get("position", "")


def side_jp(dirv):
    """dir(例 'Open Long'/'Close Short')→ (アクション, 方向, 絵文字, 色)。"""
    action = "エントリー" if dirv.startswith("Open") else "クローズ"
    is_long = "Long" in dirv
    side = "🟩ロング" if is_long else "🟥ショート"
    # 色: エントリー=方向色 / クローズ=灰
    if action == "エントリー":
        color = 0x3fb950 if is_long else 0xff5d6c
    else:
        color = 0x8b949e
    return action, side, color


async def emit_fill(session, a, f):
    dirv = f.get("dir", "")
    if not (dirv.startswith("Open") or dirv.startswith("Close")):
        return
    w = WATCH.get(a, {})
    hook = hook_for(w)
    if not hook:
        return
    lbl, pos = label_of(a)
    coin, sz, px = f.get("coin"), f.get("sz"), f.get("px")
    pnl = float(f.get("closedPnl", 0) or 0)
    notion = abs(float(sz or 0) * float(px or 0))
    action, side, color = side_jp(dirv)
    is_long = "Long" in dirv
    title = f"{'🟢' if action=='エントリー' else '🔴'} {side} {action} — {lbl}"
    desc = (f"**{coin} を {side.replace('🟩','').replace('🟥','')} {action}**  ${notion:,.0f}\n"
            f"区分: {pos}\nsz {sz} @ {px}"
            + (f"  決済PnL ${pnl:+,.0f}" if abs(pnl) > 1e-9 else "") + f"\n`{a}`")
    FEED.insert(0, {"t": int(f.get("time", 0)), "label": lbl, "pos": pos, "action": action,
                    "side": "ロング" if is_long else "ショート", "long": is_long,
                    "coin": coin, "notion": round(notion), "pnl": round(pnl)})
    del FEED[300:]
    STATE["events"] += 1
    await discord(session, hook, title, desc, color)


async def ws_loop(session):
    notify_addrs = [a for a, w in WATCH.items() if w.get("notify")]
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=30, max_size=8 * 1024 * 1024) as ws:
                for a in notify_addrs:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": a}}))
                    await asyncio.sleep(0.02)
                STATE["ws"] = f"connected({len(notify_addrs)})"
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("channel") != "userFills":
                        continue
                    d = m.get("data", {})
                    a = (d.get("user") or "").lower()
                    if d.get("isSnapshot"):
                        continue
                    for f in d.get("fills", []):
                        await emit_fill(session, a, f)
        except Exception as e:
            STATE["ws"] = f"reconnecting: {type(e).__name__}"
            await asyncio.sleep(5)


async def poll_loop(session):
    while True:
        for a in list(WATCH.keys()):
            try:
                async with session.post(INFO_URL, json={"type": "clearinghouseState", "user": a},
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                    st = await r.json()
                acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
                ps, cur = [], {}
                for ap in st.get("assetPositions", []):
                    p = ap.get("position", {})
                    szi = float(p.get("szi", 0) or 0)
                    if abs(szi) < 1e-9:
                        continue
                    notion = float(p.get("positionValue", 0) or 0)
                    coin = p.get("coin")
                    ps.append({"coin": coin, "long": szi > 0, "notional": round(notion),
                               "upnl": round(float(p.get("unrealizedPnl", 0) or 0)), "entry": p.get("entryPx")})
                    cur[coin] = notion if szi > 0 else -notion
                POSITIONS[a] = {"acct": round(acct), "pos": ps, "t": int(time.time())}
                w = WATCH.get(a, {})
                if w.get("position") == "高頻度MM":
                    prev = PREV.get(a, {})
                    for coin, sig in cur.items():
                        d = sig - prev.get(coin, 0)
                        if abs(d) >= MM_NOTIFY_MIN:
                            side = "🟩ロング" if sig > 0 else "🟥ショート"
                            await discord(session, hook_for(w), f"🟣 {w.get('label', a[:10])} [MM大口]",
                                          f"{coin} {side} 建玉変動 ${abs(d):,.0f}（現在 ${abs(sig):,.0f}）\n`{a}`", 0xa78bfa)
                    PREV[a] = cur
            except Exception:
                pass
            await asyncio.sleep(0.12)
        STATE["last_poll"] = int(time.time())
        await asyncio.sleep(POLL_SEC)


def esc(x):
    return html.escape(str(x)) if x is not None else ""


PCOL = {"プロトレーダー(本物)": "#3fb950", "alt主体プロ": "#56b6c2", "高頻度MM": "#a78bfa",
        "弱い疑惑(監視継続)": "#ffb454", "💸 出金疑い(要監視)": "#f59e0b", "インサイダー疑惑(要監視)": "#ff5d6c"}


def render_page():
    order = ["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
             "プロトレーダー(本物)", "alt主体プロ", "高頻度MM"]
    held = [(a, POSITIONS[a]) for a in WATCH if POSITIONS.get(a, {}).get("pos")]
    held.sort(key=lambda x: (order.index(WATCH[x[0]]["position"]) if WATCH[x[0]]["position"] in order else 9,
                             -sum(abs(p["notional"]) for p in x[1]["pos"])))
    rows = ""
    for a, d in held:
        w = WATCH[a]
        col = PCOL.get(w["position"], "#8b949e")
        pos_html = " ".join(
            f"<span class='p {'l' if p['long'] else 's'}'>{esc(p['coin'])} {'🟩ロング' if p['long'] else '🟥ショート'} ${p['notional']:,}"
            f"(<span class={'g' if p['upnl']>=0 else 'r'}>{p['upnl']:+,}</span>)</span>" for p in d["pos"])
        rows += (f"<tr><td><b style='color:{col}'>{esc(w['position'])}</b>"
                 + (f"<br><span class=mut>質:{esc(w.get('wf_quality'))}</span>" if w.get('wf_quality') else "")
                 + f"</td><td>{esc(w['label'])}<br><code>{esc(a[:14])}…</code></td>"
                 f"<td>${d['acct']:,}</td><td>{pos_html}</td></tr>")
    feed = ""
    for e in FEED[:60]:
        em = "🟢" if e["action"] == "エントリー" else "🔴"
        sd = "🟩ロング" if e["long"] else "🟥ショート"
        t = time.strftime("%m-%d %H:%M", time.gmtime(e["t"] / 1000)) if e["t"] else ""
        feed += (f"<tr><td>{t}</td><td>{em} {esc(e['label'])}</td><td>{sd} {esc(e['action'])}</td>"
                 f"<td>{esc(e['coin'])}</td><td>${e['notion']:,}</td><td>{('$'+format(e['pnl'],'+,')) if e['pnl'] else ''}</td></tr>")
    return f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=30>
<title>HL リアルタイム監視</title><style>
body{{font-family:system-ui,sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:20px;font-size:13px}}
h1{{font-size:19px;margin:0 0 4px}} h2{{font-size:15px;margin:20px 0 8px;color:#cbd5e1}}
.sub{{color:#8b949e;font-size:12px;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%;max-width:1100px}} th,td{{border:1px solid #232a34;padding:6px 9px;text-align:left;vertical-align:top}}
th{{background:#10151c;font-size:11px}} code{{font-size:10px;color:#8b949e}} .mut{{color:#8b949e;font-size:10px}}
.p{{display:inline-block;border-radius:8px;padding:1px 7px;margin:1px;font-size:11px}} .p.l{{background:#0f2a1a}} .p.s{{background:#2a1015}}
.g{{color:#69d98a}} .r{{color:#ff8893}}</style></head><body>
<h1>📡 HL リアルタイム監視（現在ポジション）</h1>
<div class="sub">WS={esc(STATE['ws'])} ／ 監視{len(WATCH)}件(通知{sum(1 for w in WATCH.values() if w.get('notify'))}) ／
通知{STATE['events']} ／ 最終巡回 {time.strftime('%H:%M:%S',time.gmtime(STATE['last_poll'])) if STATE['last_poll'] else '-'}UTC ／ 30秒自動更新</div>
<h2>🔔 最近のエントリー/クローズ（直近60）</h2>
<table><tr><th>時刻</th><th>ウォレット</th><th>方向/動作</th><th>銘柄</th><th>規模</th><th>決済PnL</th></tr>{feed or '<tr><td colspan=6>まだイベントなし(WS購読中)</td></tr>'}</table>
<h2>📊 現在の建玉（{len(held)}件が保有中・ロング/ショート明示）</h2>
<table><tr><th>区分</th><th>ウォレット</th><th>口座</th><th>建玉(含み損益)</th></tr>{rows or '<tr><td colspan=4>建玉なし</td></tr>'}</table>
</body></html>"""


async def handle_index(request):
    return web.Response(text=render_page(), content_type="text/html")


async def handle_health(request):
    return web.json_response(STATE)


async def main():
    global WATCH
    WATCH = load_watch()
    async with aiohttp.ClientSession() as session:
        app = web.Application()
        app.router.add_get("/", handle_index)
        app.router.add_get("/health", handle_health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        await discord(session, HOOKS.get("insider"), "🚀 HLリアルタイム監視 起動",
                      f"監視{len(WATCH)}件 / 通知対象{sum(1 for w in WATCH.values() if w.get('notify'))}件 / ロング・ショート明示・カテゴリ別通知", 0x4ea1ff)
        print(f"started: watch={len(WATCH)} port={PORT}")
        await asyncio.gather(ws_loop(session), poll_loop(session))


if __name__ == "__main__":
    asyncio.run(main())
