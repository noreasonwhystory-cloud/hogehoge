"""HL リアルタイム監視デーモン（GCP Compute Engine 常駐用）。

監視対象(プロ/弱い疑惑)のポジションの『オープン/クローズ/ドテン』を HL WebSocket で即検知し、
カテゴリ別の Discord チャンネルへ通知（分割約定は1ポジション=1通知に集約）。
現在の建玉は定期ポーリングしてライブページ(:PORT)で表示。各建玉に初回保有日時/追加回数/最新追加日時を併記。

通知ルーティング(env): HOOK_INSIDER / HOOK_ELITE / HOOK_SOLID / HOOK_MID / HOOK_MURA / HOOK_THIN / HOOK_ALT
通知対象: 本物/alt/弱い疑惑(WS購読)。MMはDiscord通知せず=サイト表示のみ。
その他env: WATCH_PATH / PORT / POLL_SEC
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
CLOSES_TTL = int(os.environ.get("CLOSES_TTL", "1800"))        # 直近クローズの再取得間隔(秒)
CLOSES_PER_CYCLE = int(os.environ.get("CLOSES_PER_CYCLE", "20"))  # 1巡あたりのクローズ取得上限(レート制御)
SEED_TTL = int(os.environ.get("SEED_TTL", "1800"))            # 建玉ライフサイクルの全履歴再復元間隔(秒)

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
TRACK = {}   # (addr,coin) -> {net, open_ts, adds, last_ts}
SEEDED = {}  # addr -> last full-history seed ts(sec)  (プロ/弱い疑惑の建玉履歴を全約定から復元済)
CLOSES = {}  # addr -> {"t": fetch_sec, "items":[{coin,long,pnl,time}]}  直近5クローズ
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


def _apply(st, f):
    """約定1件を建玉状態に適用して新状態を返す（純関数・昇順で適用）。
    lol/los = 方向別の最新Open時刻(net再構成に依らずdirで確実に記録)。最新追加表示に使う。"""
    dirv = f.get("dir", "")
    try:
        sz = float(f.get("sz", 0) or 0)
    except Exception:
        return st
    t = int(f.get("time", 0))
    lol, los = st.get("lol"), st.get("los")
    lf = max(st.get("lf") or 0, t)               # 最終約定(売買問わず)
    if dirv == "Open Long":
        lol = max(lol or 0, t)
    elif dirv == "Open Short":
        los = max(los or 0, t)
    delta = sz if dirv in ("Open Long", "Close Short") else (-sz if dirv in ("Open Short", "Close Long") else 0)
    net = st["net"]
    new = net + delta if delta else net
    if delta and abs(net) < 1e-9 and abs(new) > 1e-9:
        base = {"net": new, "open_ts": t, "adds": 1, "last_ts": t}
    elif delta and abs(new) < 1e-9:
        return {"net": 0.0, "open_ts": None, "adds": 0, "last_ts": None,
                "lol": None, "los": None, "lf": lf}   # クローズで建玉系リセット(最終約定は保持)
    elif delta and (net > 0) != (new > 0):
        base = {"net": new, "open_ts": t, "adds": 1, "last_ts": t}
    elif delta and abs(new) > abs(net):
        base = {"net": new, "open_ts": st.get("open_ts") or t, "adds": st.get("adds", 0) + 1, "last_ts": t}
    else:
        base = dict(st)
        base["net"] = new
    base["lol"], base["los"], base["lf"] = lol, los, lf
    return base


def update_track(a, f):
    """WS約定で (addr,coin) の建玉ライフサイクルを更新（昇順で渡すこと）。"""
    coin = f.get("coin")
    key = (a, coin)
    TRACK[key] = _apply(TRACK.get(key, {"net": 0.0, "open_ts": None, "adds": 0, "last_ts": None}), f)


async def fetch_fills_full(session, addr, max_pages=60):
    """userFillsByTime を 0 から前方ページングして全約定を取得。
    戻り値 (fills, complete)。complete=False は max_pages で打ち切り＝全履歴未到達(高頻度勢)。"""
    out, cur, now, complete = [], 0, int(time.time() * 1000), False
    for _ in range(max_pages):
        try:
            async with session.post(INFO_URL, json={"type": "userFillsByTime", "user": addr,
                                                    "startTime": cur, "endTime": now},
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                ch = await r.json()
        except Exception:
            break
        if not ch:
            complete = True
            break
        out.extend(ch)
        if len(ch) < 2000:
            complete = True
            break
        last = int(ch[-1]["time"])
        if last <= cur:
            complete = True
            break
        cur = last + 1
    seen, ded = set(), []
    for f in out:
        tid = f.get("tid")
        if tid in seen:
            continue
        seen.add(tid)
        ded.append(f)
    return ded, complete


def compute_lifecycle(fills):
    """全約定から現在保有中の各建玉のライフサイクルを復元。coin->state(net>0のみ)。"""
    fills = sorted(fills, key=lambda f: int(f.get("time", 0)))
    state = {}
    for f in fills:
        coin = f.get("coin")
        st = state.get(coin, {"net": 0.0, "open_ts": None, "adds": 0, "last_ts": None})
        state[coin] = _apply(st, f)
    return {c: s for c, s in state.items() if abs(s["net"]) > 1e-9}


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
    w = WATCH.get(a, {})
    hook = hook_for(w)
    if not hook:
        return
    lbl = w.get("label") or a[:10]
    pos = w.get("position", "")
    q = w.get("wf_quality")
    px = float(cfills[-1].get("px", 0) or 0)
    if trans == "close":
        is_long = pre_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        realized = sum(float(f.get("closedPnl", 0) or 0) for f in cfills)
        title = f"🔴 {side} クローズ — {lbl}"
        desc = (f"**{coin} の{('ロング' if is_long else 'ショート')}を全クローズ**\n区分: {pos}"
                + (f"／質:{q}" if q else "") + f"\n決済PnL ${realized:+,.0f}\n`{a}`")
        color = 0x8b949e
    elif trans == "reduce":
        is_long = post_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        realized = sum(float(f.get("closedPnl", 0) or 0) for f in cfills)
        pct = (1 - abs(post_net) / abs(pre_net)) * 100 if pre_net else 0
        remain = abs(post_net) * px
        title = f"🟠 {side} 縮小 {pct:.0f}% — {lbl}"
        desc = (f"**{coin} の{('ロング' if is_long else 'ショート')}を{pct:.0f}%縮小**（残 ${remain:,.0f}）\n区分: {pos}"
                + (f"／質:{q}" if q else "") + f"\n決済PnL ${realized:+,.0f}\n`{a}`")
        color = 0xffb454
    else:
        is_long = post_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        notion = abs(post_net) * px
        st = TRACK.get((a, coin), {})
        verb = "ドテン" if trans == "flip" else "エントリー"
        last_add = (st.get("lol") if is_long else st.get("los")) or st.get("last_ts")
        title = f"{'🔄' if trans=='flip' else '🟢'} {side} {verb} — {lbl}"
        desc = (f"**{coin} を{('ロング' if is_long else 'ショート')}{verb}**  ${notion:,.0f}\n区分: {pos}"
                + (f"／質:{q}" if q else "")
                + f"\n建玉: 初回 {fmt(st.get('open_ts'))} ／ 追加{st.get('adds',1)}回 ／ 最新 {fmt(last_add)}\n`{a}`")
        color = (0x3fb950 if is_long else 0xff5d6c)
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
                    fills = sorted(d.get("fills", []), key=lambda f: int(f.get("time", 0)))
                    snap = d.get("isSnapshot")
                    if snap:
                        for f in fills:
                            update_track(a, f)             # 初期種付け（通知しない）
                        continue
                    # ライブ: 銘柄ごとに前後ネットを比較し、ポジション単位で1通知に集約
                    by_coin = defaultdict(list)
                    for f in fills:
                        by_coin[f.get("coin")].append(f)
                    for coin, cf in by_coin.items():
                        pre = TRACK.get((a, coin), {}).get("net", 0.0)
                        for f in cf:
                            update_track(a, f)
                        post = TRACK.get((a, coin), {}).get("net", 0.0)
                        tr = transition(pre, post)
                        if tr:
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
    while True:
        closes_budget = CLOSES_PER_CYCLE     # 1巡あたりの直近クローズ再取得上限
        now_s = time.time()
        for a in list(WATCH.keys()):
            try:
                async with session.post(INFO_URL, json={"type": "clearinghouseState", "user": a},
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                    st = await r.json()
                acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
                held = [ap.get("position", {}) for ap in st.get("assetPositions", [])
                        if abs(float(ap.get("position", {}).get("szi", 0) or 0)) >= 1e-9]
                # プロ/弱い疑惑の建玉ライフサイクルを全約定から権威的に復元(TTL毎に再復元)。
                # WSスナップショットは最古2000件しか種にできず初回/最新追加/最終約定がズレるため、
                # 全履歴(complete=True)を真実とし上書きする。open_ts有無でゲートしない(=必ず再復元)。
                w = WATCH.get(a, {})
                if w.get("notify") and held and (time.time() - SEEDED.get(a, 0) > SEED_TTL):
                    full, complete = await fetch_fills_full(session, a)
                    if complete:
                        lc = compute_lifecycle(full)
                        for p in held:
                            coin = p.get("coin")
                            stt = lc.get(coin)
                            if not stt:
                                continue
                            # 既存(WSで進んだ最新)と統合: 日時系はmaxで後退防止、net/初回/回数は全履歴を採用
                            ex = TRACK.get((a, coin), {})
                            for k in ("lf", "lol", "los", "last_ts"):
                                if ex.get(k) and (not stt.get(k) or ex[k] > stt[k]):
                                    stt[k] = ex[k]
                            TRACK[(a, coin)] = stt
                        SEEDED[a] = time.time()
                    else:
                        # 全履歴を取り切れない時は短時間で再挑戦(WS最新に委ねつつ)
                        SEEDED[a] = time.time() - SEED_TTL + 300
                ps = []
                for p in held:
                    szi = float(p.get("szi", 0) or 0)
                    coin = p.get("coin")
                    tr = TRACK.get((a, coin), {})
                    last_add = (tr.get("lol") if szi > 0 else tr.get("los")) or tr.get("last_ts")
                    ps.append({"coin": coin, "long": szi > 0, "notional": round(float(p.get("positionValue", 0) or 0)),
                               "upnl": round(float(p.get("unrealizedPnl", 0) or 0)), "entry": p.get("entryPx"),
                               "open_ts": tr.get("open_ts"), "adds": tr.get("adds"),
                               "last_ts": last_add, "last_fill": tr.get("lf")})
                POSITIONS[a] = {"acct": round(acct), "pos": ps, "t": int(time.time())}
                # 直近クローズ: TTL超過のものを巡回バジェット内で更新（newest userFills 1ページ）
                if closes_budget > 0 and now_s - CLOSES.get(a, {}).get("t", 0) > CLOSES_TTL:
                    closes_budget -= 1
                    try:
                        async with session.post(INFO_URL, json={"type": "userFills", "user": a},
                                                timeout=aiohttp.ClientTimeout(total=20)) as r2:
                            uf = await r2.json()
                        CLOSES[a] = {"t": time.time(), "items": extract_closes(uf or [])}
                    except Exception:
                        CLOSES[a] = {"t": now_s, "items": CLOSES.get(a, {}).get("items", [])}
            except Exception:
                pass
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


def render_page():
    order = ["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
             "プロトレーダー(本物)", "alt主体プロ", "高頻度MM"]
    # 監視対象の全ウォレットを表示（建玉ゼロでも行を出す）。建玉保有を上に、各区分内は建玉notional降順。
    allw = list(WATCH.keys())
    allw.sort(key=lambda a: (order.index(WATCH[a]["position"]) if WATCH[a]["position"] in order else 9,
                             0 if POSITIONS.get(a, {}).get("pos") else 1,
                             -sum(abs(p["notional"]) for p in POSITIONS.get(a, {}).get("pos", []))))
    n_held = sum(1 for a in allw if POSITIONS.get(a, {}).get("pos"))
    rows = ""
    facet_pos, facet_q, facet_act = {}, {}, {}      # 出現したファセット→件数
    for a in allw:
        w = WATCH[a]
        d = POSITIONS.get(a, {})
        pos = d.get("pos", [])
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
        plist = ""
        for p in pos:
            sd = "🟩ロング" if p["long"] else "🟥ショート"
            hist = (f"<div class=hist>初回 {fmt(p.get('open_ts'))} ／ 追加 {p.get('adds') if p.get('adds') is not None else '—'}回"
                    f" ／ 最新追加 {fmt(p.get('last_ts'))} ／ 最終約定 {fmt(p.get('last_fill'))}</div>") if p.get("open_ts") else f"<div class=hist>建玉履歴: WS取得待ち ／ 最終約定 {fmt(p.get('last_fill'))}</div>"
            plist += (f"<div class='p {'l' if p['long'] else 's'}'><b>{esc(p['coin'])} {sd}</b> ${p['notional']:,}"
                      f" 含み<span class={'g' if p['upnl']>=0 else 'r'}>{p['upnl']:+,}</span>{hist}</div>")
        if not plist:
            plist = "<span class=muted>建玉なし</span>"
        acct = f"${d['acct']:,}" if d.get("acct") is not None else "<span class=muted>—</span>"
        actbadge = f"<span class='ab {'on' if w.get('active14') else 'off'}'>{act}</span>"
        rows += (f"<tr data-f=\"{esc(ftok)}\"><td><b style='color:{col}'>{esc(w['position'])}</b><br>{qtag} {actbadge}</td>"
                 f"<td>{esc(w['label'])}<br><code>{esc(a[:14])}…</code>"
                 f"<div class=lnk><a href='{ASXN.format(a=a)}' target=_blank>ASXN</a> "
                 f"<a href='https://app.hyperliquid.xyz/explorer/address/{a}' target=_blank>HL</a></div></td>"
                 f"<td>{acct}</td><td>{plist}</td><td>{closes_cell(a)}</td></tr>")
    # フィルタチップ（区分・品質・稼働・方向）
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
.ab.on{{background:#16201c;border:1px solid #2a4636;color:#7fd6a8}} .ab.off{{background:#1a1d22;border:1px solid #30363d;color:#8b949e}}</style></head><body>
<h1>📡 HL リアルタイム監視（監視対象の全建玉）</h1>
<div class="sub">WS={esc(STATE['ws'])} ／ 監視{len(WATCH)}件(通知{sum(1 for w in WATCH.values() if w.get('notify'))}) ／ 建玉保有{n_held}件 ／
通知{STATE['events']} ／ 最終巡回 {time.strftime('%m-%d %H:%M:%S',time.gmtime(STATE['last_poll'] + JST)) if STATE['last_poll'] else '-'} JST ／ 30秒自動更新
／ 日時は日本時間(JST)。エントリー/クローズ/ドテンはDiscordへ即通知(MM除く)。</div>
<div class="fb">
  <div><span class=gl>区分</span>{posbar}</div>
  <div><span class=gl>品質</span>{qbar}</div>
  <div><span class=gl>稼働</span>{actbar}</div>
  <div><span class=gl>方向</span>{dbar}<span id=cf>✕ 解除</span><span id=cnt></span></div>
</div>
<h2>📊 監視対象 全{len(WATCH)}件（うち建玉保有 {n_held}件）</h2>
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
