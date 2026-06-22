"""HL リアルタイム監視デーモン（GCP Compute Engine 常駐用）。

監視対象(プロ/弱い疑惑)のポジションの『オープン/クローズ/ドテン』を HL WebSocket で即検知し、
カテゴリ別の Discord チャンネルへ通知（分割約定は1ポジション=1通知に集約）。
現在の建玉は定期ポーリングしてライブページ(:PORT)で表示。各建玉に初回保有日時/追加回数/最新追加日時を併記。

【設計の核心（再構成パラダイム全廃・SSOT）】
- 建玉ネットの権威は **clearinghouseState.szi**（約定の積算再構成はしない＝ページ脱落で恒久ズレする為）。
- 建玉ライフサイクル(初回/追加/最新追加/最終約定)の権威は **newest-first userFills**。
  各fillが持つ `startPosition`(その約定直前の建玉)を使い、再構成せず導出(lifecycle_from_newest)。
- 表示値(POSITIONS/LIFE)は **pollのみが書く**。WSは通知(transition)専任で表示netを書かない。
  唯一の例外は LIFE.last_fill を WS が max() で前進させる(単調量ゆえ収束する)。

通知ルーティング(env): HOOK_INSIDER / HOOK_ELITE / HOOK_SOLID / HOOK_MID / HOOK_MURA / HOOK_THIN / HOOK_ALT
通知対象: 本物/alt/弱い疑惑(WS購読)。MMはDiscord通知せず=サイト表示のみ。
その他env: WATCH_PATH / PORT / POLL_SEC / LIFE_TTL / LIFE_PER_CYCLE
"""
import os
import json
import time
import html
import asyncio
from collections import defaultdict
from urllib.request import urlopen

import websockets
import aiohttp
from aiohttp import web

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
ASXN = "https://hyperscreener.asxn.xyz/profile/{a}"
WATCH_PATH = os.environ.get("WATCH_PATH", os.path.join(os.path.dirname(__file__), "data", "watch_addresses.json"))
PORT = int(os.environ.get("PORT", "8080"))
POLL_SEC = int(os.environ.get("POLL_SEC", "60"))
LIFE_TTL = int(os.environ.get("LIFE_TTL", "900"))            # ライフサイクル/クローズの再取得間隔(秒)
LIFE_PER_CYCLE = int(os.environ.get("LIFE_PER_CYCLE", "10"))  # 1巡あたりのuserFills取得上限(レート制御)

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
POSITIONS = {}    # addr -> {acct, pos:[{coin,long,szi,notional,upnl,entry}], t}   pollのみが書く
LIFE = {}         # (addr,coin) -> {net,open_ts,adds,last_add,last_fill,seeded}    pollが置換, WSはlast_fillのみmax
CLOSES = {}       # addr -> {"t": fetch_sec, "items":[{coin,long,pnl,time}]}        直近5クローズ
PREV_SZI = {}     # (addr,coin) -> 前回pollのszi   szi差分でのクローズ/ADL/清算検知用
LIFE_FETCH = {}   # addr -> 最後にuserFillsでLIFE/CLOSESを更新したts(sec)
LAST_EVT = {}     # (addr,coin) -> 最後に通知したts(sec)  WS/poll間のclose二重通知防止
STATE = {"started": int(time.time()), "ws": "init", "last_poll": 0, "events": 0}


def load_watch():
    if WATCH_PATH.startswith("http"):
        data = json.loads(urlopen(WATCH_PATH, timeout=30).read())
    else:
        data = json.load(open(WATCH_PATH, encoding="utf-8"))
    return {w["address"].lower(): w for w in data}


def hook_for(w):
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


JST = 9 * 3600  # 日本時間オフセット


def fmt(ts):
    return time.strftime("%m-%d %H:%M", time.gmtime(ts / 1000 + JST)) if ts else "—"


def _signed(f):
    """約定の符号付きサイズ。side B(買)=+ / A(売)=-。"""
    sz = float(f.get("sz", 0) or 0)
    return sz if f.get("side") == "B" else -sz


def lifecycle_from_newest(uf, coin, szi):
    """newest-first userFills から coin の建玉ライフサイクルを startPosition 権威で導出（再構成しない）。
    戻り: dict(net, open_ts, adds, last_add, last_fill, seeded) または None。
    重要: APIが返すネイティブ newest-first 順をそのまま使う。time再ソートは禁止
          (同一msに最大239件のサブ約定があり、timeソートすると startPosition 連鎖が壊れる)。"""
    fs = [f for f in uf if f.get("coin") == coin]      # ネイティブ newest-first のまま
    if not fs:
        return None
    last_fill = int(fs[0].get("time", 0))              # 最終約定 = 最新fill時刻(真値)
    want = "Open Long" if szi > 0 else "Open Short"    # szi方向に一致する最新Open
    last_add = next((int(f["time"]) for f in fs if f.get("dir") == want), None)
    # 初回保有/追加回数: ネイティブ順の反転(=古い順, keyソートでない)で startPosition 連鎖を辿り
    # 最後のゼロクロス(=現建玉の起点)を求める。窓内に起点が無ければ open_ts=None(=14日以上前から保有)。
    chrono = fs[::-1]
    open_ts, adds, prev_end = None, 0, None
    for f in chrono:
        sp = float(f.get("startPosition", 0) or 0)
        end = sp + _signed(f)
        if prev_end is not None and abs(sp - prev_end) > 1e-6:
            open_ts, adds = None, 0                     # 連鎖断絶(窓境界/欠落) → 起点不明
        if abs(sp) < 1e-9 and abs(end) > 1e-9:         # ゼロ→建玉: 新規オープン
            open_ts, adds = int(f.get("time", 0)), 1
        elif abs(end) > abs(sp) + 1e-12:               # 同方向に積み増し
            adds += 1
        prev_end = end
    return {"net": szi, "open_ts": open_ts, "adds": (adds or None),
            "last_add": last_add or last_fill, "last_fill": last_fill, "seeded": True}


def extract_closes(fills, n=5):
    """約定群から直近のクローズ(決済)を新しい順に最大n件抽出。
    dir=Close Long → ロング決済 / Close Short → ショート決済。"""
    cl = [f for f in fills if f.get("dir") in ("Close Long", "Close Short")]
    cl.sort(key=lambda f: int(f.get("time", 0)), reverse=True)
    out = []
    for f in cl[:n]:
        try:
            pnl = float(f.get("closedPnl", 0) or 0)
        except Exception:
            pnl = 0.0
        out.append({"coin": f.get("coin"), "long": f.get("dir") == "Close Long",
                    "pnl": pnl, "time": int(f.get("time", 0))})
    return out


def transition(pre_net, post_net):
    if abs(pre_net) < 1e-9 and abs(post_net) > 1e-9:
        return "open"
    if abs(pre_net) > 1e-9 and abs(post_net) < 1e-9:
        return "close"
    if abs(pre_net) > 1e-9 and abs(post_net) > 1e-9 and (pre_net > 0) != (post_net > 0):
        return "flip"
    # 同方向で40%以上縮小＝大幅な部分クローズは通知（単なる追加/小幅利確は通知しない）
    if abs(pre_net) > 1e-9 and 1e-9 < abs(post_net) <= abs(pre_net) * 0.6:
        return "reduce"
    return None


async def notify_event(session, a, coin, trans, pre_net, post_net, cfills):
    """ポジション遷移をDiscordへ。cfills=[] の場合は poll の szi差分検知(fills無きクローズ/ADL/清算)。"""
    w = WATCH.get(a, {})
    hook = hook_for(w)
    if not hook:
        return
    lbl = w.get("label") or a[:10]
    pos = w.get("position", "")
    q = w.get("wf_quality")
    px = float(cfills[-1].get("px", 0) or 0) if cfills else 0.0
    realized = sum(float(f.get("closedPnl", 0) or 0) for f in cfills) if cfills else None
    detected = "" if cfills else "（建玉消滅を検知）"
    if trans == "close":
        is_long = pre_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        title = f"🔴 {side} クローズ — {lbl}"
        desc = (f"**{coin} の{('ロング' if is_long else 'ショート')}を全クローズ**{detected}\n区分: {pos}"
                + (f"／質:{q}" if q else "")
                + (f"\n決済PnL ${realized:+,.0f}" if realized is not None else "")
                + f"\n`{a}`")
        color = 0x8b949e
    elif trans == "reduce":
        is_long = post_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        pct = (1 - abs(post_net) / abs(pre_net)) * 100 if pre_net else 0
        remain = abs(post_net) * px
        title = f"🟠 {side} 縮小 {pct:.0f}% — {lbl}"
        desc = (f"**{coin} の{('ロング' if is_long else 'ショート')}を{pct:.0f}%縮小**（残 ${remain:,.0f}）\n区分: {pos}"
                + (f"／質:{q}" if q else "")
                + (f"\n決済PnL ${realized:+,.0f}" if realized is not None else "")
                + f"\n`{a}`")
        color = 0xffb454
    else:
        is_long = post_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        notion = abs(post_net) * px
        st = LIFE.get((a, coin), {})
        verb = "ドテン" if trans == "flip" else "エントリー"
        open_ts = st.get("open_ts") or (int(cfills[0]["time"]) if cfills else None)
        last_add = st.get("last_add") or (int(cfills[-1]["time"]) if cfills else None)
        adds = st.get("adds") or 1
        title = f"{'🔄' if trans=='flip' else '🟢'} {side} {verb} — {lbl}"
        desc = (f"**{coin} を{('ロング' if is_long else 'ショート')}{verb}**"
                + (f"  ${notion:,.0f}" if notion else "") + f"\n区分: {pos}"
                + (f"／質:{q}" if q else "")
                + f"\n建玉: 初回 {fmt(open_ts)} ／ 追加{adds}回 ／ 最新 {fmt(last_add)}\n`{a}`")
        color = (0x3fb950 if is_long else 0xff5d6c)
    STATE["events"] += 1
    await discord(session, hook, title, desc, color)


async def ws_loop(session):
    """WSは通知専任。表示値(POSITIONS/LIFE.net/open_ts等)は書かない。stateless transition で判定。"""
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
                    fills = sorted(d.get("fills", []), key=lambda f: int(f.get("time", 0)))
                    if not fills:
                        continue
                    if d.get("isSnapshot"):
                        # スナップショットは状態種付けに使わない(最古2000件再生でTRACKを壊す旧バグの根)。
                        # net非依存で安全な直近クローズの初期表示にだけ使う。
                        new_cl = extract_closes(fills)
                        if new_cl and not CLOSES.get(a, {}).get("items"):
                            CLOSES[a] = {"t": time.time(), "items": new_cl}
                        continue
                    # ライブ: 銘柄ごとに startPosition から pre/post を確定(再構成・累積なし)
                    by_coin = defaultdict(list)
                    for f in fills:
                        by_coin[f.get("coin")].append(f)
                    for coin, cf in by_coin.items():
                        pre = float(cf[0].get("startPosition", 0) or 0)
                        post = float(cf[-1].get("startPosition", 0) or 0) + _signed(cf[-1])
                        t = int(cf[-1].get("time", 0))
                        # 唯一の例外: LIFE.last_fill を前進(単調max)。netやopen_tsは触らない。
                        ex = LIFE.get((a, coin))
                        if ex:
                            ex["last_fill"] = max(ex.get("last_fill") or 0, t)
                        else:
                            LIFE[(a, coin)] = {"net": post, "open_ts": None, "adds": None,
                                               "last_add": None, "last_fill": t, "seeded": False}
                        tr = transition(pre, post)
                        if tr:
                            LAST_EVT[(a, coin)] = time.time()
                            await notify_event(session, a, coin, tr, pre, post, cf)
                    # 直近クローズをリアルタイム反映（最大5・新しい順）
                    new_cl = extract_closes(fills)
                    if new_cl:
                        merged = new_cl + CLOSES.get(a, {}).get("items", [])
                        seen, items = set(), []
                        for c in sorted(merged, key=lambda x: x["time"], reverse=True):
                            k = (c["coin"], c["time"], round(c["pnl"], 4))
                            if k in seen:
                                continue
                            seen.add(k)
                            items.append(c)
                        CLOSES[a] = {"t": time.time(), "items": items[:5]}
        except Exception as e:
            STATE["ws"] = f"reconnecting: {type(e).__name__}"
            await asyncio.sleep(5)


async def poll_loop(session):
    """表示値の単一ライター。clearinghouseStateでPOSITIONS(net=szi)、newest userFillsでLIFE/CLOSES。"""
    while True:
        life_budget = LIFE_PER_CYCLE
        now_s = time.time()
        for a in list(WATCH.keys()):
            w = WATCH.get(a, {})
            try:
                async with session.post(INFO_URL, json={"type": "clearinghouseState", "user": a},
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                    st = await r.json()
                acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
                ps, cur_szi = [], {}
                for ap in st.get("assetPositions", []):
                    p = ap.get("position", {})
                    szi = float(p.get("szi", 0) or 0)
                    if abs(szi) < 1e-9:
                        continue
                    coin = p.get("coin")
                    cur_szi[coin] = szi
                    ps.append({"coin": coin, "long": szi > 0, "szi": szi,
                               "notional": round(float(p.get("positionValue", 0) or 0)),
                               "upnl": round(float(p.get("unrealizedPnl", 0) or 0)), "entry": p.get("entryPx")})
                POSITIONS[a] = {"acct": round(acct), "pos": ps, "t": int(time.time())}
                # szi差分: 建玉の消滅/反転を検知(fills無きADL/清算の安全網)。WS直近通知が無ければclose通知。
                prev_coins = {c: v for (aa, c), v in PREV_SZI.items() if aa == a}
                for coin, prev in prev_coins.items():
                    nowv = cur_szi.get(coin, 0.0)
                    if abs(prev) > 1e-9 and (abs(nowv) < 1e-9 or (prev > 0) != (nowv > 0)):
                        if w.get("notify") and now_s - LAST_EVT.get((a, coin), 0) > POLL_SEC * 2:
                            await notify_event(session, a, coin, "close", prev, nowv, [])
                            LAST_EVT[(a, coin)] = now_s
                        if abs(nowv) < 1e-9:
                            LIFE.pop((a, coin), None)
                for coin in [c for c in prev_coins if c not in cur_szi]:
                    PREV_SZI.pop((a, coin), None)
                for coin, szi in cur_szi.items():
                    PREV_SZI[(a, coin)] = szi
                # LIFE/CLOSES を newest userFills 1本で更新(notify層・建玉あり・TTL・巡回バジェット)
                if w.get("notify") and ps and life_budget > 0 and now_s - LIFE_FETCH.get(a, 0) > LIFE_TTL:
                    life_budget -= 1
                    try:
                        async with session.post(INFO_URL, json={"type": "userFills", "user": a},
                                                timeout=aiohttp.ClientTimeout(total=20)) as r2:
                            uf = await r2.json()
                        for p in ps:
                            lc = lifecycle_from_newest(uf or [], p["coin"], p["szi"])
                            if lc:
                                ex = LIFE.get((a, p["coin"]))
                                if ex and (ex.get("last_fill") or 0) > (lc["last_fill"] or 0):
                                    lc["last_fill"] = ex["last_fill"]   # WS先行の最終約定は後退させない
                                LIFE[(a, p["coin"])] = lc
                        CLOSES[a] = {"t": time.time(), "items": extract_closes(uf or [])}
                        LIFE_FETCH[a] = time.time()
                    except Exception:
                        pass
            except Exception:
                pass   # last-known-good: 一時失敗で POSITIONS を消さない(tも更新しない=鮮度で古さが見える)
            await asyncio.sleep(0.12)
        STATE["last_poll"] = int(time.time())
        await asyncio.sleep(POLL_SEC)


def esc(x):
    return html.escape(str(x)) if x is not None else ""


PCOL = {"プロトレーダー(本物)": "#3fb950", "alt主体プロ": "#56b6c2", "高頻度MM": "#a78bfa",
        "弱い疑惑(監視継続)": "#ffb454", "💸 出金疑い(要監視)": "#f59e0b", "インサイダー疑惑(要監視)": "#ff5d6c"}


def closes_cell(a):
    """直近クローズ(最大5)のセルHTML。"""
    items = CLOSES.get(a, {}).get("items", [])
    if not items:
        return "<span class=muted>—</span>"
    out = ""
    for c in items:
        sd = "🟩L" if c["long"] else "🟥S"
        cls = "g" if c["pnl"] >= 0 else "r"
        out += (f"<div class=cl>{esc(c['coin'])} {sd} "
                f"<span class={cls}>${c['pnl']:+,.0f}</span> "
                f"<span class=muted>{fmt(c['time'])}</span></div>")
    return out


def _hist_html(a, p):
    """1建玉のライフサイクル行HTML。LIFE(poll権威)から組み立て、未取得/窓外を誠実に表示。"""
    life = LIFE.get((a, p["coin"]))
    lf = (life or {}).get("last_fill")
    if not life or not life.get("seeded"):
        return f"<div class=hist>履歴取得中 ／ 最終約定 {fmt(lf)}</div>"
    adds = life.get("adds")
    addtxt = f"{adds}回(直近14日)" if adds is not None else "—"
    if life.get("open_ts"):
        head = f"初回 {fmt(life['open_ts'])} ／ 追加 {addtxt}"
    else:
        head = f"14日以上前から保有 ／ 直近14日 追加 {addtxt}"
    return (f"<div class=hist>{head} ／ 最新追加 {fmt(life.get('last_add'))}"
            f" ／ 最終約定 {fmt(lf)}</div>")


def render_page():
    order = ["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
             "プロトレーダー(本物)", "alt主体プロ", "高頻度MM"]
    # 監視対象の全ウォレットを表示。建玉保有=上, 未取得(保有可能性あり)=中, 確定建玉ゼロ=下。
    def band(a):
        d = POSITIONS.get(a)
        if d is None:
            return 1            # 未取得(取得中) — 沈めない
        return 0 if d.get("pos") else 2
    allw = list(WATCH.keys())
    allw.sort(key=lambda a: (order.index(WATCH[a]["position"]) if WATCH[a]["position"] in order else 9,
                             band(a),
                             -sum(abs(p["notional"]) for p in POSITIONS.get(a, {}).get("pos", []))))
    n_held = sum(1 for a in allw if POSITIONS.get(a, {}).get("pos"))
    n_wait = sum(1 for a in allw if a not in POSITIONS)
    rows = ""
    facet_pos, facet_q, facet_act = {}, {}, {}
    now_s = time.time()
    for a in allw:
        w = WATCH[a]
        d = POSITIONS.get(a)
        pos = (d or {}).get("pos", [])
        col = PCOL.get(w["position"], "#8b949e")
        q = w.get("wf_quality")
        qtag = f"<span class=qt>質:{esc(q)}</span>" if q else ""
        act = "14日以内" if w.get("active14") else "14日超"
        dirs = set("ロング" if p["long"] else "ショート" for p in pos)
        facet_pos[w["position"]] = facet_pos.get(w["position"], 0) + 1
        facet_act[act] = facet_act.get(act, 0) + 1
        if q:
            facet_q[q] = facet_q.get(q, 0) + 1
        ftok = (f"区分:{w['position']} " + (f"質:{q} " if q else "")
                + f"稼働:{act} " + " ".join("方向:" + x for x in dirs))
        # 建玉セル: 3状態(未取得/建玉ゼロ/保有)
        if d is None:
            plist = "<span class=muted>⏳ 取得中…</span>"
            acct = "<span class=muted>⏳</span>"
        else:
            plist = ""
            for p in pos:
                sd = "🟩ロング" if p["long"] else "🟥ショート"
                plist += (f"<div class='p {'l' if p['long'] else 's'}'><b>{esc(p['coin'])} {sd}</b> ${p['notional']:,}"
                          f" 含み<span class={'g' if p['upnl']>=0 else 'r'}>{p['upnl']:+,}</span>{_hist_html(a, p)}</div>")
            if not plist:
                plist = "<span class=muted>建玉なし</span>"
            acct = f"${d['acct']:,}"
        # 鮮度バッジ(更新停滞)
        stale = ""
        if d and (now_s - d.get("t", 0) > POLL_SEC * 2):
            mins = int((now_s - d.get("t", 0)) / 60)
            stale = f"<span class=stale>⚠ {mins}分前</span>"
        actbadge = f"<span class='ab {'on' if w.get('active14') else 'off'}'>{act}</span>"
        rows += (f"<tr data-f=\"{esc(ftok)}\"><td><b style='color:{col}'>{esc(w['position'])}</b><br>{qtag} {actbadge}</td>"
                 f"<td>{esc(w['label'])}<br><code>{esc(a[:14])}…</code>"
                 f"<div class=lnk><a href='{ASXN.format(a=a)}' target=_blank>ASXN</a> "
                 f"<a href='https://app.hyperliquid.xyz/explorer/address/{a}' target=_blank>HL</a></div></td>"
                 f"<td>{acct}{stale}</td><td>{plist}</td><td>{closes_cell(a)}</td></tr>")

    def chips(items, prefix):
        return "".join(f"<span class=ft data-t=\"{prefix}:{esc(k)}\">{esc(k)} ({v})</span>"
                       for k, v in sorted(items.items(), key=lambda x: -x[1]))
    posbar = chips(facet_pos, "区分")
    qbar = chips(facet_q, "質")
    actbar = chips(facet_act, "稼働")
    dbar = ("<span class=ft data-t=\"方向:ロング\">ロング</span>"
            "<span class=ft data-t=\"方向:ショート\">ショート</span>")
    return f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=30>
<title>HL リアルタイム監視</title><style>
body{{font-family:system-ui,sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:20px;font-size:13px}}
h1{{font-size:19px;margin:0 0 4px}} h2{{font-size:15px;margin:18px 0 8px;color:#cbd5e1}}
.sub{{color:#8b949e;font-size:12px;margin-bottom:10px}}
.fb{{background:#10151c;border:1px solid #232a34;border-radius:8px;padding:8px 10px;margin-bottom:12px;max-width:1180px}}
.fb .gl{{color:#8b949e;font-size:11px;margin-right:6px;display:inline-block;width:42px}}
.ft{{cursor:pointer;display:inline-block;border-radius:10px;font-size:11px;padding:2px 9px;margin:2px;border:1px solid #30363d;color:#cbd5e1;background:#0b0f14;user-select:none}}
.ft.on{{background:#1f6feb;color:#fff;font-weight:700;border-color:#1f6feb}}
#cf{{cursor:pointer;color:#ff8893;font-size:11px;margin-left:8px;text-decoration:underline}} #cnt{{color:#8b949e;font-size:11px;margin-left:8px}}
table{{border-collapse:collapse;width:100%;max-width:1180px}} th,td{{border:1px solid #232a34;padding:6px 9px;text-align:left;vertical-align:top}}
th{{background:#10151c;font-size:11px}} code{{font-size:10px;color:#8b949e}}
.qt{{display:inline-block;background:#16201c;border:1px solid #2a4636;color:#7fd6a8;border-radius:8px;font-size:10px;padding:1px 6px}}
.lnk a{{color:#4ea1ff;text-decoration:none;font-size:10px;margin-right:6px}}
.p{{border-radius:8px;padding:4px 8px;margin:3px 0;font-size:11px}} .p.l{{background:#0f2a1a}} .p.s{{background:#2a1015}}
.hist{{color:#9aa3ad;font-size:10px;margin-top:2px}} .g{{color:#69d98a}} .r{{color:#ff8893}}
.muted{{color:#6e7681;font-size:11px}}
.cl{{font-size:10px;color:#cbd5e1;white-space:nowrap;margin:1px 0}}
.ab{{display:inline-block;border-radius:8px;font-size:10px;padding:1px 6px;margin-top:2px}}
.ab.on{{background:#16201c;border:1px solid #2a4636;color:#7fd6a8}} .ab.off{{background:#1a1d22;border:1px solid #30363d;color:#8b949e}}
.stale{{display:inline-block;margin-left:6px;color:#f0a020;font-size:10px}}</style></head><body>
<h1>📡 HL リアルタイム監視（監視対象の全建玉）</h1>
<div class="sub">WS={esc(STATE['ws'])} ／ 監視{len(WATCH)}件(通知{sum(1 for w in WATCH.values() if w.get('notify'))}) ／ 建玉保有{n_held}件 ／ 取得待ち{n_wait}件 ／
通知{STATE['events']} ／ 最終巡回 {time.strftime('%m-%d %H:%M:%S',time.gmtime(STATE['last_poll'] + JST)) if STATE['last_poll'] else '-'} JST ／ 30秒自動更新
／ 日時は日本時間(JST)。net=clearinghouse szi・ライフサイクル=newest userFills(再構成しない)。エントリー/クローズ/ドテンはDiscordへ即通知(MM除く)。</div>
<div class="fb">
  <div><span class=gl>区分</span>{posbar}</div>
  <div><span class=gl>品質</span>{qbar}</div>
  <div><span class=gl>稼働</span>{actbar}</div>
  <div><span class=gl>方向</span>{dbar}<span id=cf>✕ 解除</span><span id=cnt></span></div>
</div>
<h2>📊 監視対象 全{len(WATCH)}件（建玉保有 {n_held}件 ／ 取得待ち {n_wait}件）</h2>
<table id=tbl><tr><th>区分/品質/稼働</th><th>ウォレット</th><th>口座</th><th>建玉（ロング/ショート・含み損益・初回/追加回数/最新追加）</th><th>直近クローズ(5件)</th></tr>{rows or '<tr><td colspan=5>監視対象なし</td></tr>'}</table>
<script>
const sel=new Set(JSON.parse(localStorage.getItem('hlfilt')||'[]'));
const rows=[...document.querySelectorAll('#tbl tr[data-f]')];
const cnt=document.getElementById('cnt');
function apply(){{let s=0;rows.forEach(r=>{{const f=r.getAttribute('data-f');const ok=[...sel].every(t=>f.includes(t));r.style.display=ok?'':'none';if(ok)s++;}});cnt.textContent=sel.size?`絞込: ${{s}}/${{rows.length}}`:'';}}
document.querySelectorAll('.ft').forEach(c=>{{const t=c.getAttribute('data-t');if(sel.has(t))c.classList.add('on');c.onclick=()=>{{if(sel.has(t)){{sel.delete(t);c.classList.remove('on');}}else{{sel.add(t);c.classList.add('on');}}localStorage.setItem('hlfilt',JSON.stringify([...sel]));apply();}};}});
document.getElementById('cf').onclick=()=>{{sel.clear();localStorage.removeItem('hlfilt');document.querySelectorAll('.ft.on').forEach(x=>x.classList.remove('on'));apply();}};
apply();
</script>
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
        print(f"started: watch={len(WATCH)} port={PORT}")
        await asyncio.gather(ws_loop(session), poll_loop(session))


if __name__ == "__main__":
    asyncio.run(main())
