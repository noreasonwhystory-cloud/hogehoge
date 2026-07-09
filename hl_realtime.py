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
その他env: WATCH_PATH / PORT / POLL_MIN_SEC / WEIGHT_PER_SEC / POLL_CONC / LIFE_TTL / LIFE_PER_CYCLE / WS_MAX_USERS
"""
import os
import json
import time
import html
import bisect
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
POLL_MIN_SEC = int(os.environ.get("POLL_MIN_SEC", "15"))     # 1巡の最小間隔(これより速くは回さない)
LIFE_TTL = int(os.environ.get("LIFE_TTL", "900"))            # ライフサイクル/クローズの再取得間隔(秒)
LIFE_PER_CYCLE = int(os.environ.get("LIFE_PER_CYCLE", "8"))   # 1巡あたりのuserFills取得上限(直列+weight20で重いので控えめ)
MM_LEARN_PER_CYCLE = int(os.environ.get("MM_LEARN_PER_CYCLE", "8"))  # 非notify(MM)のdex一回学習の1巡上限
# 並行ポーリング＋weightトークンバケット(秒粒度レート律速・burst歯止め・429自動減速)
WEIGHT_PER_SEC = float(os.environ.get("WEIGHT_PER_SEC", "16"))  # 建玉sweepのweight/秒(定常時)。充填中はflowへ譲り動的に下がる。429で自動半減
POLL_CONC = int(os.environ.get("POLL_CONC", "16"))           # 同時inflight上限(clearinghouseは高並行でも安全=実測56/s)
UF_CONC = int(os.environ.get("UF_CONC", "1"))                # userFills(weight20)はHLが並行を即429にする(実測)→直列化
W_CH = 2     # clearinghouseState weight
W_UF = 20    # userFills weight
W_CANDLE = 4  # candleSnapshot weight
CANDLE_TTL = int(os.environ.get("CANDLE_TTL", "8"))   # チャート足のインメモリ共有キャッシュTTL(秒)=サーバ掃引/再取得周期
CANDLE_POLL = int(os.environ.get("CANDLE_POLL", "4"))  # フロントのローソク再取得間隔(秒)=増分描画ゆえ短くても軽い(E2E遅延短縮)
CANDLE_DAYS = float(os.environ.get("CANDLE_DAYS", "3"))   # ローソク遡及日数(7→3で軽量化・1mは~5000本上限/高い足で更に遡及可)
CHART_COINS = ["BTC"]                                 # チャート対象(現在はBTCのみ・["BTC","ETH","SOL"]で復帰可)
CHART_INTERVALS = ["1m", "5m", "15m", "1h"]
_IV_MS = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000}

# ---- 複数取引所 spot CVD (TV方式=bar-polarity・7社を各公開API直叩き・wf_852c028e確定) ----
# TVは取引所の実taker買い/売りでなく、足の極性(close>open→+出来高/close<open→−)で出来高デルタを近似。
# 単位はUSD(quote)と枚(coin=base)の2モード。TVの±数百軸は枚スケールに一致するため既定=coin。
# quote(USD)直接取得=Binance/KuCoin/Bybit/OKX、base×終値換算=Bitfinex/Coinbase/Kraken。
CVD_TTL = int(os.environ.get("CVD_TTL", "30"))          # CVDの再取得間隔(秒)
CVD_UNIT_DEFAULT = os.environ.get("CVD_UNIT", "coin")   # coin(枚)|usd
CVD_DAYS = float(os.environ.get("CVD_DAYS", "2"))       # CVD遡及日数(各UTC0時で日次リセット・遡って見られる)
# ※1m足では取得元の制約で遡及が短い社あり(Kraken720本=12h・KuCoin/Bybit(Coinalyze)~1日)。5m/15m/1hなら全社CVD_DAYS分。
# ★米国GCPからは api.binance.com=451・api.bybit.com=403 でジオブロック。
#   Binanceは公開データミラー data-api.binance.vision(米国200・field7 quote付)、
#   BybitはCoinalyze経由(直叩きドメイン全403)で迂回する。
COINALYZE_KEY = os.environ.get("COINALYZE_KEY", "")     # VM ~/hl/hl.env のみ・Bybit迂回用
COINALYZE_URL = "https://api.coinalyze.net/v1/ohlcv-history"
SPOT_EXCH = [   # (ラベル, 色) 表示順。各社アダプタは下の fetch_*_bars
    ("Binance",  "#f3ba2f"),
    ("Bitfinex", "#16b157"),
    ("KuCoin",   "#26a17b"),
    ("Coinbase", "#1652f0"),
    ("Kraken",   "#7b68ee"),
    ("Bybit",    "#f7a600"),
    ("OKX",      "#cbd5e1"),
]
UA = {"User-Agent": "Mozilla/5.0"}   # Bitfinex/Coinbaseは無いと弾かれる

HOOKS = {   # BTC/ETH/SOL 用(既存チャンネル)
    "insider": os.environ.get("HOOK_INSIDER", ""),
    "エリート": os.environ.get("HOOK_ELITE", ""),
    "堅実": os.environ.get("HOOK_SOLID", ""),
    "中堅": os.environ.get("HOOK_MID", ""),
    "ムラあり": os.environ.get("HOOK_MURA", ""),
    "履歴薄/評価不能": os.environ.get("HOOK_THIN", ""),
    "alt主体": os.environ.get("HOOK_ALT", ""),
}
HOOKS_OTHER = {   # BTC/ETH/SOL 以外のコイン用(別チャンネル・env HOOK2_*)
    "insider": os.environ.get("HOOK2_INSIDER", ""),
    "エリート": os.environ.get("HOOK2_ELITE", ""),
    "堅実": os.environ.get("HOOK2_SOLID", ""),
    "中堅": os.environ.get("HOOK2_MID", ""),
    "ムラあり": os.environ.get("HOOK2_MURA", ""),
    "履歴薄/評価不能": os.environ.get("HOOK2_THIN", ""),
    "alt主体": os.environ.get("HOOK2_ALT", ""),
}
MAJORS = {"BTC", "ETH", "SOL"}   # 既存チャンネル対象。これ以外は HOOKS_OTHER へ
INSIDER_POS = {"弱い疑惑(監視継続)", "インサイダー疑惑(要監視)", "💸 出金疑い(要監視)"}

WATCH = {}
POSITIONS = {}    # addr -> {acct, pos:[{coin,long,szi,notional,upnl,entry}], t}   pollのみが書く
LIFE = {}         # (addr,coin) -> {net,open_ts,adds,last_add,last_fill,seeded}    pollが置換, WSはlast_fillのみmax
CLOSES = {}       # addr -> {"t": fetch_sec, "items":[{coin,long,pnl,time}]}        直近5クローズ
PREV_SZI = {}     # (addr,coin) -> 前回pollのszi   szi差分でのクローズ/ADL/清算検知用
SEEN_SZI = set()  # 過去sweepで取得成功済みのaddr。初観測=baseline巡(通知/flow抑止)→再起動/取得失敗後の初成功でのopen化け洪水を根絶
LIFE_FETCH = {}   # addr -> 最後にuserFillsでLIFE/CLOSESを更新したts(sec)
WALLET_DEXS = {}  # addr -> set(builder perp dex接頭辞)  HIP-3ビルダーperp(例 xyz:SPCX)の建玉照会用
LAST_EVT = {}     # (addr,coin) -> 最後に通知したts(sec)  WS/poll間のclose二重通知防止
MARK_LAST = {}    # coin -> 直近の有効mark価格(notional/szi)。全銘柄アーカイブのpx源=建玉個別markが無い時のフォールバック
CANDLE_MEM = {}   # (coin,interval) -> {"t": fetch_sec, "candles": [...]}  チャート足の共有キャッシュ
CANDLE_WANT = {}  # (coin,interval) -> last_request_sec  バックグラウンド更新対象(直近要求された足)
CVD_MEM = {}      # interval -> {"t": fetch_sec, "series": [{label,color,data}]}  取引所別CVDキャッシュ
CVD_WANT = {}     # interval -> last_request_sec
SESSION = None    # aiohttp ClientSession(main で設定・/candles ハンドラ用)
STATE = {"started": int(time.time()), "ws": "init", "last_poll": 0, "events": 0}

# ---- チャートアラート(設定タブ→Discord/Telegram) ----
ALERTS_PATH = os.environ.get("ALERTS_PATH", os.path.expanduser("~/hl/alerts.json"))  # VM専用・非git(秘密含む)
ALERT_PIN = os.environ.get("ALERT_PIN", "")     # 任意。設定時は /alerts/save,/test で一致必須
ALERT_CFG = {"channels": {"discord": "", "telegram": {"token": "", "chat_id": ""}}, "rules": []}
ALERT_STATE = {}  # rule_id -> {"prev": 値, "fired": last_fired_sec, "armed": bool}


def load_alerts():
    global ALERT_CFG
    try:
        with open(ALERTS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        ch = d.get("channels") or {}
        tg = ch.get("telegram") or {}
        ALERT_CFG = {"channels": {"discord": ch.get("discord", "") or "",
                                  "telegram": {"token": tg.get("token", "") or "",
                                               "chat_id": tg.get("chat_id", "") or ""}},
                     "rules": d.get("rules") or []}
    except Exception:
        pass   # 無ければ既定(空)のまま


def save_alerts(cfg):
    global ALERT_CFG
    ALERT_CFG = cfg
    try:
        os.makedirs(os.path.dirname(ALERTS_PATH) or ".", exist_ok=True)
        tmp = ALERTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ALERTS_PATH)   # atomic
    except Exception as e:
        print("save_alerts err", e)

# ---- 監視台帳 BTC約定フロー (FLOW) ----
FLOW_DAYS = float(os.environ.get("FLOW_DAYS", "2"))          # フロー遡及窓(日)
FLOW_WPS = float(os.environ.get("FLOW_WPS", "4"))            # flow専用バケットrate。充填中の合算=SWEEP_FILL(10)+4=14(上限~20の70%でマージン確保・18は edge で sweep が429する)
FLOW_FILL_TOTAL = float(os.environ.get("FLOW_FILL_TOTAL", "14"))  # 充填中の合算上限(sweepはこれ-FLOW_WPSに絞る)。回復目標もこの範囲ゆえ振動しない
assert FLOW_WPS <= 8, "FLOW_WPS過大"
FLOW_BATCH = int(os.environ.get("FLOW_BATCH", "20"))         # 1巡で取得する件数(flow専用バケット律速ゆえ飢餓しない)
FLOW_GAP = float(os.environ.get("FLOW_GAP", "0.05"))        # 各取得後の微小yield(秒)
FLOW_CYCLE_SLEEP = int(os.environ.get("FLOW_CYCLE_SLEEP", "1"))   # 巡間sleep(秒)
FLOW_REFRESH = int(os.environ.get("FLOW_REFRESH", "90000"))  # 起動時バックフィルのスキップ閾値(秒)。25h=日次再起動(cron 24h)を跨いでスキップ→773件の毎日再取得+throttleを回避。稼働中は前向きszi差分がcpを進めFLOWを維持(#15)。新規/fresh deploy(t=0)は通常バックフィル
FLOW_PAGE_MAX = int(os.environ.get("FLOW_PAGE_MAX", "3"))   # 1ウォレットのバックフィル最大ページ数(userFillsByTimeは[startTime,now]の最古2000を昇順返却ゆえ前進ページングで実時刻取得。窓内6000fillまでカバー・レート暴走防止に上限を絞る)
FLOW_TTL = int(os.environ.get("FLOW_TTL", "20"))           # /flow 集計キャッシュTTL(秒)
FLOW_STORE = os.environ.get("FLOW_STORE", os.path.expanduser("~/hl/flow.json"))  # 永続化(再起動で消えない・VM専用)
FLOW_SAVE_SEC = int(os.environ.get("FLOW_SAVE_SEC", "60"))  # FLOW保存間隔(秒)
STARTUP_MS = int(time.time() * 1000)   # 起動時刻。バックフィルは[floor,STARTUP_MS]=起動前の実約定のみ、前向きszi差分は起動後を担当し時間分離(二重計上回避)
# position(区分)→高レベルgroup(実position値。未知は other)
POS_GROUP = {
    "高頻度MM": "mm",
    "プロトレーダー(本物)": "pro",
    "alt主体プロ": "alt",
    "弱い疑惑(監視継続)": "insider",
    "インサイダー疑惑(要監視)": "insider",
    "💸 出金疑い(要監視)": "insider",
}
GROUP_LABEL = {"insider": "弱い疑惑/インサイダー", "pro": "プロ(本物)", "alt": "alt主体プロ", "mm": "高頻度MM"}
FLOW = {}      # addr -> {"cp":int_ms, "t":float, "miss":int, "fills":[(time_ms,dir,sz,px,tid)]}
FLOW_MEM = {}  # (groups,poss,quals,interval,unit) -> {"t":sec,"data":[...],"n":int}
_FLOW_DIRS = ("Open Long", "Close Short", "Open Short", "Close Long", "Short > Long", "Long > Short")

DEX_STORE = os.path.join(os.path.dirname(__file__), "data", "wallet_dexs.json")  # 学習済dexの永続化(再起動で消えない)


def load_dexs():
    """前回までに学習した使用dexを読み込む。再起動直後でも建玉あり/なしを即断定でき『取得中』の氾濫を防ぐ。"""
    try:
        d = json.load(open(DEX_STORE, encoding="utf-8"))
        for a, dx in d.items():
            WALLET_DEXS[a] = set(dx or [])
    except Exception:
        pass


def save_dexs():
    try:
        os.makedirs(os.path.dirname(DEX_STORE), exist_ok=True)
        json.dump({a: sorted(dx) for a, dx in WALLET_DEXS.items()},
                  open(DEX_STORE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def load_watch():
    if WATCH_PATH.startswith("http"):
        data = json.loads(urlopen(WATCH_PATH, timeout=30).read())
    else:
        data = json.load(open(WATCH_PATH, encoding="utf-8"))
    return {w["address"].lower(): w for w in data}


def _load_demotions(limit=40):
    """data/demotions.json(WATCH_PATHと同ディレクトリ)から直近の降格を読む。発掘パイプラインが書き出す。"""
    try:
        if WATCH_PATH.startswith("http"):
            return []
        p = os.path.join(os.path.dirname(WATCH_PATH), "demotions.json")
        return json.load(open(p, encoding="utf-8"))[:limit]
    except Exception:
        return []


def hook_for(w, coin):
    hooks = HOOKS if coin in MAJORS else HOOKS_OTHER   # BTC/ETH/SOL=既存ch / その他=別ch
    if w.get("position") in INSIDER_POS:
        return hooks.get("insider")
    return hooks.get(w.get("wf_quality"))


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


def fmt_s(ts):   # 約定/検知時刻用(秒精度)
    return time.strftime("%m-%d %H:%M:%S", time.gmtime(ts / 1000 + JST)) if ts else "—"


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
    # 同方向で40%以上縮小＝大幅な部分クローズは通知
    if abs(pre_net) > 1e-9 and 1e-9 < abs(post_net) <= abs(pre_net) * 0.6:
        return "reduce"
    # 同方向で40%以上増し玉＝大幅な追加建玉は通知(インサイダーのポジ構築シグナル)
    if abs(pre_net) > 1e-9 and abs(post_net) >= abs(pre_net) * 1.4:
        return "add"
    return None


async def notify_event(session, a, coin, trans, pre_net, post_net, cfills, notional=None):
    """ポジション遷移をDiscordへ。cfills=[] の場合は poll の szi差分検知(fills無きクローズ/ADL/清算)。"""
    w = WATCH.get(a, {})
    hook = hook_for(w, coin)
    if not hook:
        STATE["drop_no_hook"] = STATE.get("drop_no_hook", 0) + 1   # #9: 未知wf_quality/webhook未設定で無言ドロップした通知を/healthで可視化
        return
    lbl = w.get("label") or a[:10]
    pos = w.get("position", "")
    q = w.get("wf_quality")
    px = float(cfills[-1].get("px", 0) or 0) if cfills else 0.0
    realized = sum(float(f.get("closedPnl", 0) or 0) for f in cfills) if cfills else None
    detected = "" if cfills else "（建玉消滅を検知）"
    exec_ts = int(cfills[-1].get("time", 0) or 0) if cfills else int(time.time() * 1000)
    tline = (f"\n🕐 {'約定' if cfills else '検知'} {fmt_s(exec_ts)}"          # 約定=実fill時刻 / 検知=szi差分検知の現在時刻
             f"\n🔗 [ASXNで見る](https://hyperscreener.asxn.xyz/profile/{a})")  # ASXNプロフィール(建玉/PnL閲覧)
    if trans == "close":
        is_long = pre_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        title = f"🔴 {side} クローズ — {lbl}"
        desc = (f"**{coin} の{('ロング' if is_long else 'ショート')}を全クローズ**{detected}\n区分: {pos}"
                + (f"／質:{q}" if q else "")
                + (f"\n決済PnL ${realized:+,.0f}" if realized is not None else "")
                + tline + f"\n`{a}`")
        color = 0x8b949e
    elif trans == "reduce":
        is_long = post_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        pct = (1 - abs(post_net) / abs(pre_net)) * 100 if pre_net else 0
        remain = notional if notional is not None else abs(post_net) * px   # #10: poll検知(cfills=[])はpx=0ゆえnotional(現建玉残$)を優先。従来は常に「残$0」だった
        title = f"🟠 {side} 縮小 {pct:.0f}% — {lbl}"
        desc = (f"**{coin} の{('ロング' if is_long else 'ショート')}を{pct:.0f}%縮小**（残 ${remain:,.0f}）\n区分: {pos}"
                + (f"／質:{q}" if q else "")
                + (f"\n決済PnL ${realized:+,.0f}" if realized is not None else "")
                + tline + f"\n`{a}`")
        color = 0xffb454
    elif trans == "add":
        is_long = post_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        pct = (abs(post_net) / abs(pre_net) - 1) * 100 if pre_net else 0
        notion = notional if notional is not None else abs(post_net) * px
        title = f"🟢 {side} 増し玉 +{pct:.0f}% — {lbl}"
        desc = (f"**{coin} の{('ロング' if is_long else 'ショート')}を{pct:.0f}%増し玉**（建玉 ${notion:,.0f}）\n区分: {pos}"
                + (f"／質:{q}" if q else "") + tline + f"\n`{a}`")
        color = 0x3fb950 if is_long else 0xff5d6c
    else:
        is_long = post_net > 0
        side = "🟩ロング" if is_long else "🟥ショート"
        notion = notional if notional is not None else abs(post_net) * px   # poll検知時は建玉notionalを使用
        st = LIFE.get((a, coin), {})
        verb = "ドテン" if trans == "flip" else "エントリー"
        open_ts = st.get("open_ts") or (int(cfills[0]["time"]) if cfills else None)
        last_add = st.get("last_add") or (int(cfills[-1]["time"]) if cfills else None)
        adds = st.get("adds") or 1
        title = f"{'🔄' if trans=='flip' else '🟢'} {side} {verb} — {lbl}"
        life_line = (f"\n建玉: 初回 {fmt(open_ts)} ／ 追加{adds}回 ／ 最新 {fmt(last_add)}"
                     if st.get("open_ts") else "")   # LIFE実データがある時だけ(szi検知/userFills空は出さない)
        desc = (f"**{coin} を{('ロング' if is_long else 'ショート')}{verb}**"
                + (f"  ${notion:,.0f}" if notion else "") + f"\n区分: {pos}"
                + (f"／質:{q}" if q else "")
                + life_line + tline + f"\n`{a}`")
        color = (0x3fb950 if is_long else 0xff5d6c)
    STATE["events"] += 1
    await discord(session, hook, title, desc, color)


WS_MAX_USERS = int(os.environ.get("WS_MAX_USERS", "15"))   # HL実測: 1IPで追跡できるユーザは15が上限
# WS購読の優先度(低いほど優先): insider/弱い疑惑/出金 > エリート > 堅実 > 中堅 > その他
_WS_PRI = {"インサイダー疑惑(要監視)": 0, "弱い疑惑(監視継続)": 1, "💸 出金疑い(要監視)": 2}
_Q_PRI = {"エリート": 0, "堅実": 1, "中堅": 2, "ムラあり": 3, "alt主体": 4, "履歴薄/評価不能": 5}


def ws_priority(a):
    w = WATCH.get(a, {})
    return (_WS_PRI.get(w.get("position"), 3), _Q_PRI.get(w.get("wf_quality"), 9), a)


async def ws_loop(session):
    """WSは通知専任。表示値は書かない。HLは1IPで15ユーザまでしか追跡できない(実測)ため、
    最優先15件のみ userFills 購読。残りの notify層は poll の szi差分(~80秒)が安全網。"""
    notify_addrs = sorted([a for a, w in WATCH.items() if w.get("notify")], key=ws_priority)[:WS_MAX_USERS]
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=30, max_size=8 * 1024 * 1024) as ws:
                for a in notify_addrs:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": a}}))
                    await asyncio.sleep(0.02)
                STATE["ws"] = f"connected({len(notify_addrs)})"
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("channel") == "error":
                        STATE["ws_err"] = STATE.get("ws_err", 0) + 1   # 黙殺せず記録
                        continue
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
                        _le = LAST_EVT.get((a, coin))
                        if tr and ((not _le) or time.time() - _le[0] > NOTIFY_DEDUP or _le[1] != tr):  # poll先行の同一trは窓で抑制(二重防止・双方向対称)・別遷移は通す
                            LAST_EVT[(a, coin)] = (time.time(), tr)
                            await notify_event(session, a, coin, tr, pre, post, cf)
                        if tr in ("close", "flip"):   # #14: poll側と対称にLIFEを掃除(close→再openのライフサイクル汚染を防ぐ)
                            LIFE.pop((a, coin), None)
                    # FLOWはszi-delta(poll_one)が単一源ゆえWS userFills取込は無効(二重加算防止)。WS約定通知は従来通り。
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


class WeightBucket:
    """HL info の weight を秒粒度で律速するトークンバケット(burst歯止め)。rate は429で動的減速。"""
    def __init__(self, rate, cap):
        self.rate = rate
        self.cap = cap
        self.tokens = cap
        self.ts = time.time()

    async def acquire(self, w):
        while True:
            now = time.time()
            self.tokens = min(self.cap, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= w:
                self.tokens -= w           # ここまで await を挟まない=GIL原子的・二重消費なし
                return
            await asyncio.sleep(max(0.02, (w - self.tokens) / max(self.rate, 1e-6)))


# 容量は最大単発weight(userFills=20)以上必須(さもないと acquire(20) が永久に成立しない)。
# userFillsは UF_SEM=1 で直列化済ゆえバーストは抑えられる。clearinghouseの瞬間バーストは実測56/sでも安全。
BUCKET = WeightBucket(WEIGHT_PER_SEC, max(W_UF + WEIGHT_PER_SEC, 28))


async def hl_post(session, payload, weight):
    """weight律速付き POST /info。429/5xx は None を返し rate を半減(自動減速)。"""
    await BUCKET.acquire(weight)
    try:
        async with session.post(INFO_URL, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 429 or r.status >= 500:
                STATE["rate429"] = STATE.get("rate429", 0) + 1
                BUCKET.rate = max(4.0, BUCKET.rate * 0.5)       # 秒粒度で減速
                STATE["wps"] = round(BUCKET.rate, 1)
                return None
            return await r.json()
    except Exception:
        return None


# flow専用第2バケット: sweepのBUCKETと予算を物理分離し大weight(userFills=20)の飢餓を根絶。
# cap≧W_UF 必須(さもないと acquire(20) が永久不成立)。
FLOW_BUCKET = WeightBucket(FLOW_WPS, W_UF + 4)
# sweep BUCKETの回復目標(可変)。flow充填中は FLOW_FILL_TOTAL-FLOW_WPS に絞り、充填後は WEIGHT_PER_SEC へ戻す(監視鮮度復帰)。
SWEEP_RATE = WEIGHT_PER_SEC


async def flow_post(session, payload, weight):
    """flow専用バケット経由 POST /info。429時はHLがIP単位の合算予算ゆえ両バケットを同時減速。
    HLのuserFills weightは件数非依存の固定ゆえ追い課金はしない(過去の追い課金は人為的減速の原因だった)。"""
    await FLOW_BUCKET.acquire(weight)
    try:
        async with session.post(INFO_URL, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 429 or r.status >= 500:
                STATE["flow_429"] = STATE.get("flow_429", 0) + 1   # flow専用カウンタ(rate429=sweep専用と分離→poll_loopの回復判定がflowの429に巻き込まれない)
                FLOW_BUCKET.rate = max(2.0, FLOW_BUCKET.rate * 0.5)   # flow側だけ強く絞る。sweepのBUCKETは触らない(spiral防止)=sweepは自身のhl_post429で自律調整・監視鮮度を死守
                return None
            return await r.json()
    except Exception:
        return None


async def fetch_ch(session, a, dex=None):
    """clearinghouseState を取得し (acct, [position dict], {coin:szi}) を返す。dex指定でHIP-3ビルダーperp。"""
    body = {"type": "clearinghouseState", "user": a}
    if dex:
        body["dex"] = dex
    st = await hl_post(session, body, W_CH)
    if st is None:
        return 0.0, [], {}, False   # 取得失敗(429/timeout)=呼び元でlast-known-good維持(誤close/誤open洪水を防ぐ)。フラット成功と区別する
    acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
    ps, cur_szi = [], {}
    for ap in st.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        if abs(szi) < 1e-9:
            continue
        coin = p.get("coin")
        cur_szi[coin] = szi
        pv = float(p.get("positionValue", 0) or 0)
        ps.append({"coin": coin, "long": szi > 0, "szi": szi,
                   "notional": round(pv), "mark": (pv / abs(szi) if abs(szi) > 1e-9 else 0.0),
                   "upnl": round(float(p.get("unrealizedPnl", 0) or 0)), "entry": p.get("entryPx")})
    return acct, ps, cur_szi, True


async def _candle_window(session, coin, interval, start_ms, end_ms):
    """candleSnapshotを1窓(最大~5000本)取得→[{time秒,open,high,low,close}]。失敗時None。
    BUCKET経由でweight計上(sweepと同一律速)＝HL総負荷を一元管理し未計上バーストによる429を防ぐ。"""
    body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                              "startTime": start_ms, "endTime": end_ms}}
    raw = await hl_post(session, body, W_CANDLE)
    if raw is None:
        return None
    out = []
    for c in (raw or []):
        try:
            out.append({"time": int(c["t"]) // 1000, "open": float(c["o"]), "high": float(c["h"]),
                        "low": float(c["l"]), "close": float(c["c"])})
        except Exception:
            pass
    return out


async def fetch_candles(session, coin, interval, n=320):
    """candleSnapshotを取得→lightweight-charts形式。CANDLE_DAYS日分を遡って表示できる。
    HLは1窓~5000本上限ゆえ、初回(キャッシュ空)はページングで深く取得、以後は直近1窓だけ取得して既存にマージ
    (深い履歴を保持しつつHL負荷を1回/TTLに抑える)。現在形成中の足も含むためリアルタイム更新。"""
    key = (coin, interval)
    ent = CANDLE_MEM.get(key)
    if ent and time.time() - ent["t"] < CANDLE_TTL:
        return ent["candles"]
    ims = _IV_MS.get(interval, 60000)
    now = int(time.time() * 1000)
    target_start = now - int(CANDLE_DAYS * 86400 * 1000)
    win = 4900 * ims                                   # 1窓の最大スパン(~5000本上限)
    have = (ent or {}).get("candles", [])
    if have:                                           # 既存あり→直近1窓だけ更新(深い履歴は保持)
        windows = [(max(target_start, now - win), now)]
    else:                                              # 初回→ページングで CANDLE_DAYS 分まで遡及
        windows, end = [], now
        for _ in range(8):
            if end <= target_start:
                break
            st = max(target_start, end - win)
            windows.append((st, end))
            end = st - 1
    m = {c["time"]: c for c in have}                   # time秒→足。重複は新しい取得で上書き
    got = False
    for st, end in windows:
        w = await _candle_window(session, coin, interval, st, end)
        if w is None:
            continue
        got = True
        for c in w:
            m[c["time"]] = c
    if not got and not have:
        return []                                      # 全窓失敗かつ既存なし
    cutoff = target_start // 1000 - ims // 1000        # CANDLE_DAYS窓に収める(古い分はトリム)
    out = sorted((c for c in m.values() if c["time"] >= cutoff), key=lambda x: x["time"])
    CANDLE_MEM[key] = {"t": time.time(), "candles": out}
    if key == ("BTC", "1m") and out:      # #11: フォールバック用にBTC現値を保持(_btc_pxがCANDLE_MEM未取得時に使う)
        STATE["btc_px"] = float(out[-1]["close"])
    return out


async def candle_loop(session):
    """チャート足をバックグラウンドで定期更新(直近要求された(coin,interval)のみ)。
    /candles ハンドラはこのキャッシュを即返す=スイープと干渉せずブロックしない。"""
    for c in CHART_COINS:
        CANDLE_WANT[(c, "1m")] = time.time()      # 既定で 3コイン×1m は常時更新
    while True:
        now = time.time()
        for key in list(CANDLE_WANT.keys()):
            coin, interval = key
            if interval != "1m" and now - CANDLE_WANT[key] > 180:
                CANDLE_WANT.pop(key, None)         # 180秒要求の無い非1m足は更新対象から外す
                continue
            try:
                await fetch_candles(session, coin, interval)
            except Exception:
                pass
        await asyncio.sleep(CANDLE_TTL)


async def _get_json(session, url, headers=None):
    async with session.get(url, headers=headers or {},
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            raise RuntimeError(f"http {r.status}")
        return await r.json()

def _utc_midnight_s():
    now = int(time.time())
    return now - (now % 86400)   # epochはUTC基準ゆえ now%86400 = UTC0時からの経過秒

# 各社アダプタ: 正規化バー列 [(t秒, open, close, baseVol, quoteVol)] を昇順で返す。start_s以降を取得。
# TVと同じ「日次リセット」のため当日UTC0時(の少し前)から取得し、tick-ruleでCVDを積む。
async def fetch_coinalyze_bars(session, symbol, interval, start_s):
    """Coinalyze OHLCV(米国非ブロック)で正規化バー列。quote列なし→qv=base×終値。ジオブロック迂回用。"""
    if not COINALYZE_KEY:
        return []
    iv = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1hour"}.get(interval, "1min")
    url = f"{COINALYZE_URL}?symbols={symbol}&interval={iv}&from={start_s}&to={int(time.time())}"
    j = await _get_json(session, url, {"api_key": COINALYZE_KEY})
    hist = sorted((j[0].get("history", []) if j else []), key=lambda x: int(x["t"]))
    return [(int(c["t"]), float(c.get("o", 0) or 0), float(c.get("c", 0) or 0),
             float(c.get("v", 0) or 0), float(c.get("v", 0) or 0) * float(c.get("c", 0) or 0))
            for c in hist]

async def fetch_binance_bars(session, interval, start_s):
    iv = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h"}.get(interval, "1m")
    out, cur = [], start_s * 1000   # data-api.binance.vision=米国OK公開ミラー。昇順・最大1000/req
    for _ in range(8):              # 前方ページング。現在到達でlen<1000→break(無駄打ちなし)。1mで複数日に必要
        k = await _get_json(session, f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval={iv}&startTime={cur}&limit=1000")
        if not k:
            break
        out += [(int(x[0]) // 1000, float(x[1]), float(x[4]), float(x[5]), float(x[7])) for x in k]
        if len(k) < 1000:
            break
        cur = k[-1][0] + 1
    return out

async def fetch_bitfinex_bars(session, interval, start_s):
    iv = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h"}.get(interval, "1m")
    # sort=1で昇順・limit大。[mts,open,close,high,low,vol]
    k = await _get_json(session, f"https://api-pub.bitfinex.com/v2/candles/trade:{iv}:tBTCUSD/hist?limit=10000&sort=1&start={start_s*1000}", UA)
    return [(int(x[0]) // 1000, float(x[1]), float(x[2]), float(x[5]), float(x[5]) * float(x[2])) for x in k]

async def fetch_kucoin_bars(session, interval, start_s):
    iv = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1hour"}.get(interval, "1min")
    j = await _get_json(session, f"https://api.kucoin.com/api/v1/market/candles?type={iv}&symbol=BTC-USDT&startAt={start_s}&endAt={int(time.time())}")
    rows = (j or {}).get("data", [])   # 新→古。[time(秒),open,close,high,low,vol,turnover(USD)]
    return [(int(x[0]), float(x[1]), float(x[2]), float(x[5]), float(x[6])) for x in reversed(rows)]

async def fetch_coinbase_bars(session, interval, start_s):
    g = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(interval, 60)
    out, end = [], int(time.time())   # 300本/req上限ゆえ[start,end]窓を遡ってページング(~2日分まで)
    for _ in range(12):
        if end <= start_s:
            break
        st = max(start_s, end - 300 * g)
        k = await _get_json(session, f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={g}&start={st}&end={end}", UA)
        if not k:
            break
        out += [(int(x[0]), float(x[3]), float(x[4]), float(x[5]), float(x[5]) * float(x[4])) for x in k]
        end = st - 1
    out.sort(key=lambda r: r[0])   # [time,low,high,open,close,vol]
    return out

async def fetch_kraken_bars(session, interval, start_s):
    iv = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(interval, 1)
    # since以降を返す(最大720本・それ以前は遡れない=Kraken制約)。[time,open,high,low,close,vwap,volume,count]
    j = await _get_json(session, f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={iv}&since={start_s}")
    res = (j or {}).get("result", {})
    rows = next((v for kk, v in res.items() if kk != "last"), [])   # キー名 XXBTZUSD に化ける
    rows = rows[:-1]   # 最終足は未確定→除外
    return [(int(x[0]), float(x[1]), float(x[4]), float(x[6]), float(x[6]) * float(x[5])) for x in rows]  # vwap換算

async def fetch_bybit_bars(session, interval, start_s):
    # api.bybit.com/api.bytick.com は米国GCPで403→Coinalyze(sBTCUSDT.6=Bybit spot)経由で迂回
    return await fetch_coinalyze_bars(session, "sBTCUSDT.6", interval, start_s)

async def fetch_okx_bars(session, interval, start_s):
    bar = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H"}.get(interval, "1m")
    out, after = [], ""   # 300本/req上限・after=これより古い側へページング(~2日分まで)。[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
    for _ in range(12):
        url = f"https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar={bar}&limit=300"
        if after:
            url += f"&after={after}"
        j = await _get_json(session, url)
        d = (j or {}).get("data", [])
        if not d:
            break
        for x in d:
            if len(x) > 8 and x[8] == "0":   # 未確定足は除外
                continue
            out.append((int(x[0]) // 1000, float(x[1]), float(x[4]), float(x[5]), float(x[7])))
        oldest = int(d[-1][0])               # 新→古ゆえ末尾が最古
        after = str(oldest)
        if oldest // 1000 <= start_s:
            break
    out.sort(key=lambda r: r[0])
    return out

SPOT_FETCHERS = {
    "Binance": fetch_binance_bars, "Bitfinex": fetch_bitfinex_bars, "KuCoin": fetch_kucoin_bars,
    "Coinbase": fetch_coinbase_bars, "Kraken": fetch_kraken_bars, "Bybit": fetch_bybit_bars,
    "OKX": fetch_okx_bars,
}


def _cvd_tickrule(bars, unit):
    """TV方式: 符号=close vs 前足close(close>close[1]→+/<→−/==→0)。**各UTC0時で日次リセット**(複数日を遡って表示可)。
    unit=usd(quote)|coin(base)。"""
    data, cvd, prevc, prevday = [], 0.0, None, None
    for t, o, c, bv, qv in bars:
        day = int(t) // 86400          # UTC日
        if prevday is not None and day != prevday:
            cvd = 0.0                  # 日付が変わったら0にリセット(TVのDaily Reset)
        s = 0 if prevc is None else (1 if c > prevc else (-1 if c < prevc else 0))
        prevc = c
        prevday = day
        cvd += s * (qv if unit == "usd" else bv)
        data.append({"time": int(t), "value": round(cvd, 2 if unit == "usd" else 4)})
    return data


async def fetch_cvd(session, interval, unit="coin"):
    """7取引所のspot OHLCVを各公開API直叩きで並行取得し、TV「BTC Spot CVD(Daily Reset)」を再現。
    符号=前足終値比のtick-rule・各UTC0時で日次リセット・CVD_DAYS日分遡る。1社落ちても他は更新(失敗社は前回維持)。"""
    fstart = _utc_midnight_s() - int(CVD_DAYS) * 86400   # CVD_DAYS日前の0時から(古い日の起点も0時に揃う)
    fetchers = [SPOT_FETCHERS[lbl] for lbl, _ in SPOT_EXCH]
    results = await asyncio.gather(*[f(session, interval, fstart) for f in fetchers],
                                   return_exceptions=True)
    prev = {s["label"]: s for s in (CVD_MEM.get((interval, unit)) or {}).get("series", [])}
    series = []
    for (label, color), bars in zip(SPOT_EXCH, results):
        if isinstance(bars, Exception) or not bars:
            if label in prev:                      # 失敗社は前回維持(全停止しない)
                series.append(prev[label])
                continue
            bars = []
        # 各社のページング差で窓外まで返す社(OKXの逆ページングは粗い足で1ページ300本=窓を大幅超過)を
        # fstart でクリップ=全社の左端を揃える。tick-ruleは日次リセットゆえ窓開始=CVD_DAYS前0時で正。
        bars = [b for b in bars if b[0] >= fstart]
        series.append({"label": label, "color": color, "data": _cvd_tickrule(bars, unit)})
    CVD_MEM[(interval, unit)] = {"t": time.time(), "series": series}
    return series


async def cvd_loop(session):
    """spot CVDをバックグラウンドで定期更新(使用中interval×unitのみ)。/cvdはキャッシュ即返し。"""
    CVD_WANT[("1m", CVD_UNIT_DEFAULT)] = time.time()    # 既定
    while True:
        now = time.time()
        for key in list(CVD_WANT.keys()):
            iv, unit = key
            if now - CVD_WANT[key] > 300 and key != ("1m", CVD_UNIT_DEFAULT):
                CVD_WANT.pop(key, None)
                continue
            try:
                await fetch_cvd(session, iv, unit)
            except Exception:
                pass
        await asyncio.sleep(CVD_TTL)


# ===== 監視台帳 BTC約定フロー(FLOW) =====
def _flow_merge(ent, r, floor, touch=True):
    """userFillsByTime/WS約定を FLOW へ増分マージ。BTC4種+ドテンのみ・tidでdedup・窓外trim。
    touch=False(WS経由)は ent["t"](flow_loop充填済マーカー)を立てない=flow_loopの安全網(再接続ギャップ補填)を殺さない。"""
    have = ent["fills"]
    seen = {f[4] for f in have}        # tid集合(int・global一意。合成キーは衝突するため不可)
    mx = ent["cp"]
    for f in r:
        if f.get("coin") != "BTC":     # spot 'UBTC'等は coin!=BTC で除外
            continue
        d = f.get("dir")
        if d not in _FLOW_DIRS:        # TWAP親/清算等の未知dirは無視(else握りつぶし禁止)
            continue
        try:
            tid = int(f["tid"]); t = int(f["time"])
            s = float(f.get("sz", 0) or 0); p = float(f.get("px", 0) or 0)
        except Exception:
            continue
        if t > mx:
            mx = t
        if tid in seen:                # 同一ms inclusive再取得の重複を tid で除外
            continue
        seen.add(tid); have.append((t, d, s, p, tid))
    have.sort(key=lambda x: x[0])      # 昇順維持(同一ms挿入対策)
    cut = bisect.bisect_left([x[0] for x in have], floor)   # 窓外trim
    if cut > 0:
        del have[:cut]
    ent["cp"] = mx
    if touch:
        ent["t"] = time.time()


FLOW_SYN = [0]   # szi-delta合成約定のtidカウンタ(負値=実tidと衝突しない)

ARCH_COINS = os.environ.get("ARCH_COINS", "all")   # "all"=全銘柄をアーカイブ / "btc"=BTCのみ(即ロールバック用・hl.env 1行+restart)
FLOW_ARCH_DIR = os.environ.get("FLOW_ARCH_DIR", os.path.expanduser("~/hl/flow_arch"))  # 全銘柄永久ログの日次ローテ先(flow-YYYY-MM-DD.jsonl・JST日付)
# 旧 flow_archive.jsonl はBTC過去分の正本として凍結(新規書込はFLOW_ARCH_DIRへ)。
FLOW_ARCH_BUF = []   # 未書込みレコード(save周期でflush)


def _jst_date(t_ms):
    """UTC msから JST(UTC+9) の YYYY-MM-DD。日次ローテのファイル名用。"""
    return time.strftime("%Y-%m-%d", time.gmtime(t_ms / 1000 + 9 * 3600))


def _arch(a, coin, t_ms, d, sz, px):
    """検出した建玉変化を品質評価用の永久ログへ積む(trim無し)。ウォレットの区分/品質も時点情報として残す。
    px は低単価コイン(kPEPE等)も潰さないよう有効桁で保持。"""
    w = WATCH.get(a, {})
    FLOW_ARCH_BUF.append({"t": t_ms, "a": a, "coin": coin, "dir": d, "sz": round(sz, 6),
                          "px": float(f"{px:.8g}"), "usd": round(sz * px),
                          "pos": w.get("position"), "q": w.get("wf_quality")})
    if len(FLOW_ARCH_BUF) > 100000:   # #18: flush継続失敗時のメモリ膨張歯止め(e2-micro=10万で~20時間耐性・日次syncより長い)。古い方から落とし警告
        del FLOW_ARCH_BUF[:50000]
        STATE["arch_buf_drop"] = STATE.get("arch_buf_drop", 0) + 50000


def archive_flush():
    """FLOW_ARCH_BUF を JST日付の日次ファイルへ追記(append-only)。失敗してもバッファは保持し次回再試行。
    日境界を跨ぐバッチは各レコードのJST日付でファイルを振り分ける。"""
    if not FLOW_ARCH_BUF:
        return
    buf = FLOW_ARCH_BUF[:]
    try:
        os.makedirs(FLOW_ARCH_DIR, exist_ok=True)
        by_day = {}
        for r in buf:
            by_day.setdefault(_jst_date(r["t"]), []).append(r)
        for day, rows in by_day.items():
            path = os.path.join(FLOW_ARCH_DIR, f"flow-{day}.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        del FLOW_ARCH_BUF[:len(buf)]   # 書けた分だけ消す(間に追加された新規は残す)
    except Exception:
        STATE["arch_flush_err"] = STATE.get("arch_flush_err", 0) + 1   # #18: 黙殺せず/healthで可視化(ディスクフル/権限異常の早期検知)


def _btc_px():
    """BTC perp の現在価格(CANDLE_MEM最新足close・無ければSTATEのfallback)。"""
    ent = CANDLE_MEM.get(("BTC", "1m"))
    if ent and ent.get("candles"):
        return float(ent["candles"][-1]["close"])
    return float(STATE.get("btc_px", 0.0))


def _mark(a, coin):
    """ウォレットaのcoin建玉のmark価格(notional/szi)。ビルダー/HIP-3は価格帯が違うため個別に取る。
    有効値(>0)のみ返し無効なら次候補へ(notional=0の小口が0.0を返すのを防ぐ):
    個別建玉mark → 直近の任意ウォレットのcoin mark(MARK_LAST) → BTCのみグローバル(_btc_px)。"""
    for p in POSITIONS.get(a, {}).get("pos", []):
        if p.get("coin") == coin and abs(p.get("szi", 0) or 0) > 1e-9:
            m = p.get("mark") or (abs(float(p["notional"]) / float(p["szi"])) if p.get("szi") else 0.0)
            if m and m > 0:
                return float(m)
    m = MARK_LAST.get(coin, 0.0)
    if m and m > 0:
        return float(m)
    if coin == "BTC":
        return _btc_px()
    return 0.0


def flow_szi_event(a, prev, nowv, px=None, t_ms=None, coin="BTC"):
    """建玉のszi変化を合成約定として記録(clearinghouse由来=userFills不在/部分も確実に捕捉)。
    long増=Open Long/long減=Close Long/short増=Open Short/short減=Close Short/ドテンは両方。値=Δszi×価格(px省略時は現在mark)。
    coin=="BTC"(chart=True)のみ FLOW(チャート2日窓)・FLOW_SYN・cp前進・trim を実行=BTCの挙動は現行と同一。
    それ以外(alt)は _arch(永久アーカイブ)のみ=全銘柄記録。t_ms 指定時はその時刻でスタンプ。"""
    if abs(nowv - prev) < 1e-9:
        return
    if px is None:
        px = _mark(a, coin)   # #11: 個別mark(notional/szi)を第一候補=ビルダー(別価格帯)でも正確
    if px <= 0:
        STATE["arch_no_px"] = STATE.get("arch_no_px", 0) + 1   # 価格解決不能=dropして可視化(記録漏れの早期検知)
        return
    chart = (coin == "BTC")
    ent = FLOW.setdefault(a, {"cp": 0, "t": 0.0, "miss": 0, "fills": []}) if chart else None
    t = int(time.time() * 1000) if t_ms is None else int(t_ms)

    def add(d, sz):
        if abs(sz) <= 1e-9:
            return
        if chart:
            FLOW_SYN[0] -= 1
            ent["fills"].append((t, d, abs(sz), px, FLOW_SYN[0]))
        _arch(a, coin, t, d, abs(sz), px)   # 品質評価用の永久アーカイブへ(append-only・trim無し・前向き検出のみ=再起動でも重複しない)
    if prev >= 0 and nowv >= 0:
        d = nowv - prev
        add("Open Long" if d > 0 else "Close Long", d)
    elif prev <= 0 and nowv <= 0:
        d = nowv - prev
        add("Open Short" if d < 0 else "Close Short", d)
    elif prev > 0:                 # long→short ドテン
        add("Close Long", prev); add("Open Short", nowv)
    else:                          # short→long ドテン
        add("Close Short", prev); add("Open Long", nowv)
    if not chart:
        return
    ent["t"] = time.time()
    ent["cp"] = max(ent.get("cp", 0), t)   # #3二重計上根絶: cpを検知時刻へ前進→次回backfillの開始位置(max(floor,cp))がこの合成fillより後になり同一トレードを実fillで再取得しない
    floor = t - int(FLOW_DAYS * 86400 * 1000)
    fl = ent["fills"]
    while fl and fl[0][0] < floor:    # 窓外trim(時刻昇順に追記されるので先頭から)
        fl.pop(0)


def save_flow():
    """FLOW を永続化(再起動で消えない)。**取得済(t>0)は0件でも保存**=再起動で『充填済』マーカーが残り
    fill phaseの再churnを防ぐ(0件entryはfills空で軽量)。"""
    try:
        os.makedirs(os.path.dirname(FLOW_STORE) or ".", exist_ok=True)
        tmp = FLOW_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({a: {"cp": e["cp"], "t": e["t"], "fills": e["fills"]}
                       for a, e in FLOW.items() if e.get("t")}, f)
        os.replace(tmp, FLOW_STORE)
    except Exception as ex:
        print("save_flow", ex)


def load_flow():
    try:
        d = json.load(open(FLOW_STORE, encoding="utf-8"))
        for a, e in d.items():
            FLOW[a] = {"cp": e.get("cp", 0), "t": e.get("t", 0.0), "miss": 0,
                       "fills": [tuple(x) for x in e.get("fills", [])]}
    except Exception:
        pass


async def flow_backfill_one(session, a, ent, floor):
    """1ウォレットのBTC約定を窓内全件、実時刻で FLOW へ充填(前向きページング)。
    userFillsByTime は [startTime, now] の最古2000件を昇順返却ゆえ、窓内fillが2000超でも
    各バッチの最大time+1 を次 startTime にして len(batch)>=2000 の間ページング、_flow_merge が tid dedup。
    これで reconcile に頼らず窓内の個別約定が実時刻バケットへ分散する(エリート系の集約を解消)。
    窓外(floor未満)約定は _flow_merge が trim するため取り込まない=窓開始前の建玉ぶんのみ後段 reconcile が baseline 補完。"""
    start = max(floor, ent.get("cp", 0))   # 既存cp(=前回backfill完了STARTUP_MS or 前向きszi差分の最新検知時刻)から増分。cpより後だけ取るので合成fillとは時刻分離=二重計上しない
    pages = 0
    got_any = False       # 1ページでも取得成功
    completed = False     # 末尾到達=完全充填(途中失敗と区別)
    while pages < FLOW_PAGE_MAX and start < STARTUP_MS:   # 起動前[start,STARTUP_MS]のみ=起動後は前向きszi差分が担当(時間分離)
        async with FLOW_SEM:
            r = await flow_post(session, {"type": "userFillsByTime", "user": a, "startTime": start,
                                          "endTime": STARTUP_MS, "aggregateByTime": False}, W_UF)
        if r is None:
            STATE["flow_none"] = STATE.get("flow_none", 0) + 1
            break                          # 取得失敗=不完全(completed=Falseのまま→ent[t]立てず次回再試行)
        got_any = True
        STATE["flow_fetch"] = STATE.get("flow_fetch", 0) + 1
        if not r:
            completed = True; break        # 成功0件=末尾
        mx = 0
        for f in r:                        # バッチ最大time(BTC以外も含め全fillから=次窓の前進基準)
            try:
                ft = int(f["time"])
            except Exception:
                continue
            if ft > mx:
                mx = ft
        _flow_merge(ent, r, floor)         # BTC約定のみ実時刻でマージ・tid dedup・窓外trim
        ent["cp"] = max(ent.get("cp", 0), mx)   # #4a: 全fill mxでcp前進(_flow_mergeはBTCのみ前進ゆえBTC疎ウォレットで停滞するのを是正)
        pages += 1
        if len(r) < 2000 or mx <= 0:       # 末尾ページ(満杯未満)=これ以上の新しいfillなし
            completed = True; break
        nxt = mx                           # #4b: 同一ms分割の取りこぼし防止(mx+1だと同msの残fillを飛ばす)。dedupが重複吸収
        if nxt <= start:                   # 前進しない(単一ms>2000の稀ケース)=末尾扱い
            completed = True; break
        start = nxt
        await asyncio.sleep(FLOW_GAP)
    if completed:
        ent["cp"] = STARTUP_MS             # 完走=窓開始をSTARTUP_MSへ前進(次回再取得窓を縮小・以後は前向きszi差分がcpを進める)
        ent["t"] = time.time()             # 充填済マーカー(0件でも)。#4c: 途中失敗(completed=False)では立てず次回再取得
    return got_any


async def flow_loop(session):
    """ハイブリッド: 起動時に一度だけ userFills で過去FLOW_DAYS日をバックフィル(過去の出来高を復元)。
    完了後は前向きを poll_one(flow_szi_event)の szi差分が担い、ここは定期保存のみ。二重加算は時間境界で回避:
    バックフィル中は前向きszi差分をゲートOFF(PREV_SZIは進む)→完了で flow_backfilled=True にして前向きを有効化。
    userFills空のBTC保有者(エリート等=HLがfillを返さない)はバックフィルに乗らないため、現建玉を基準として一度だけ記録。"""
    global SWEEP_RATE
    await asyncio.sleep(20)            # 初回sweepでPREV_SZI/POSITIONSを揃えてから
    cand = [a for a, w in WATCH.items() if POS_GROUP.get(w.get("position"), "other") != "other"]
    floor = int(time.time() * 1000) - int(FLOW_DAYS * 86400 * 1000)
    now0 = time.time()
    targets = [a for a in cand if now0 - FLOW.get(a, {}).get("t", 0) > FLOW_REFRESH]  # 直近取得済(flow.json由来)はスキップ=再起動高速化
    if targets:                       # ── 過去FLOW_DAYS日バックフィル(未取得/期限切れのみ) ──
        SWEEP_RATE = max(8.0, FLOW_FILL_TOTAL - FLOW_WPS)   # 充填中はsweepを絞りflowへ帯域(合算18)
        if BUCKET.rate > SWEEP_RATE:
            BUCKET.rate = SWEEP_RATE
        FLOW_BUCKET.rate = FLOW_WPS
        done = 0
        for a in targets:
            ent = FLOW.setdefault(a, {"cp": 0, "t": 0.0, "miss": 0, "fills": []})
            await flow_backfill_one(session, a, ent, floor)   # 窓内全件・実時刻でページング充填(reconcileは残差のみ)
            done += 1
            if done % 50 == 0:        # 途中保存=中断しても次回は残りだけ
                save_flow()
            await asyncio.sleep(FLOW_GAP)
        SWEEP_RATE = WEIGHT_PER_SEC    # sweep定格16へ復帰(監視鮮度)
        BUCKET.rate = WEIGHT_PER_SEC
    # reconcile baseline は廃止: volチャートは「約定した瞬間のvol」だけを載せる。
    # 窓内の実約定=バックフィルが実時刻で取得済 / 前向き=poll_oneのszi差分(検知時刻)。
    # 窓開始前から持ち越した建玉(=窓内に約定タイミングが無い分)は載せない(net≠sziでOK)。
    STATE["flow_base"] = 0
    STATE["flow_backfilled"] = True   # 以後 poll_one の前向きszi差分が有効化
    save_flow()
    archive_flush()
    save_t = time.time()
    while True:                       # ── 以後は定期保存+永久アーカイブflush ──
        try:
            save_flow()
            archive_flush()
            if STATE.get("arch_flush_err", 0) > 0 and not STATE.get("arch_err_alerted"):
                STATE["arch_err_alerted"] = True   # 一度だけ通知(スパム防止)。ディスクフル/権限異常の早期検知
                hook = next((h for h in HOOKS.values() if h), "")
                if hook:
                    await discord(session, hook, "⚠️ アーカイブflush失敗",
                                  f"flow_arch書込が{STATE['arch_flush_err']}回失敗。VM ~/hl/flow_arch/ の空き/権限を確認",
                                  0xff0000)
        except Exception:
            pass
        await asyncio.sleep(FLOW_SAVE_SEC)


def _flow_facets():
    """UIチェックボックス用: group/区分/品質ごとのWATCH母数 + 充填進捗。"""
    g, pc, q = {}, {}, {}
    filled = 0
    for a, w in WATCH.items():
        grp = POS_GROUP.get(w.get("position"), "other")
        g[grp] = g.get(grp, 0) + 1
        pc[w.get("position")] = pc.get(w.get("position"), 0) + 1
        if w.get("wf_quality"):
            q[w["wf_quality"]] = q.get(w["wf_quality"], 0) + 1
        if FLOW.get(a, {}).get("t"):
            filled += 1
    return {"group": g, "position": pc, "quality": q, "filled": filled, "total": len(WATCH)}


def _flow_match(w, groups, poss, quals, mode="or"):
    """フィルタ判定。無選択=全件。mode=or: いずれかの軸に該当で対象(増やすほど広がる)。
    mode=and: 選択した各軸を全て満たす交差(例 区分=プロ AND 品質=エリート)。未選択の軸は条件にしない。"""
    if not (groups or poss or quals):
        return True
    g_ok = (not groups) or (POS_GROUP.get(w.get("position"), "other") in groups)
    p_ok = (not poss) or (w.get("position") in poss)
    q_ok = (not quals) or (w.get("wf_quality") in quals)
    if mode == "and":
        return g_ok and p_ok and q_ok
    return (bool(groups) and g_ok) or (bool(poss) and p_ok) or (bool(quals) and q_ok)


def _flow_aggregate(groups, poss, quals, interval, unit, mode="or"):
    """選択ウォレットのBTC fillを interval バケットへ ol/cs/os/cl 4分割集計。ドテンはside由来でエントリー側。"""
    ims = _IV_MS.get(interval, 60000)
    floor = int(time.time() * 1000) - int(FLOW_DAYS * 86400 * 1000)   # #16: 窓floorで集計を揃える(trimはイベント発生ウォレットのみ遅延実行ゆえ左端の母集団が不揃いだった)
    addrs = [a for a, w in WATCH.items() if _flow_match(w, groups, poss, quals, mode)]
    b = {}
    for a in addrs:
        for (t, d, s, p, _tid) in FLOW.get(a, {}).get("fills", []):
            if t < floor:                         # 窓外(遅延trim未実行分)は集計から除外
                continue
            bt = (t // ims) * ims // 1000          # バケット先頭=秒(既存candleと同形式)
            v = s * p if unit == "usd" else s
            slot = b.get(bt)
            if slot is None:
                slot = b[bt] = {"ol": 0.0, "cs": 0.0, "os": 0.0, "cl": 0.0}
            if d in ("Open Long", "Short > Long"):     slot["ol"] += v   # 買い・新規(濃緑)
            elif d == "Close Short":                   slot["cs"] += v   # 買い・クローズ(薄緑)
            elif d in ("Open Short", "Long > Short"):  slot["os"] += v   # 売り・新規(濃赤)
            elif d == "Close Long":                    slot["cl"] += v   # 売り・クローズ(薄赤)
    rnd = 2 if unit == "usd" else 4
    data = [{"time": t, "ol": round(s["ol"], rnd), "cs": round(s["cs"], rnd),
             "os": round(s["os"], rnd), "cl": round(s["cl"], rnd)} for t, s in sorted(b.items())]
    return data, len(addrs)


# ===== チャートアラート 判定エンジン =====
def _ema_py(vals, p):
    """EMA(チャートJS emaCalc と同式: 先頭シード, k=2/(p+1))。"""
    k = 2.0 / (p + 1)
    e = None
    out = []
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _bb_py(closes, period, sigma):
    """ボリンジャー: 中央=EMA(period), バンド=EMA±sigma·σ(母標準偏差・窓=period)。
    最新確定値 (mid, upper, lower) を返す(データ不足なら None)。チャートJSの renderOverlays と同式。"""
    n = len(closes)
    if n < period:
        return None
    ema = _ema_py(closes, period)
    win = closes[n - period:]
    m = sum(win) / period
    var = sum((x - m) ** 2 for x in win) / period
    sd = var ** 0.5
    mid = ema[-1]
    return mid, mid + sigma * sd, mid - sigma * sd


def _alert_closes(tf):
    """tf の最新ローソク close列(昇順)。未取得なら[]。アラート判定対象に登録もする。"""
    CANDLE_WANT[("BTC", tf)] = time.time()
    ent = CANDLE_MEM.get(("BTC", tf))
    return [c["close"] for c in (ent or {}).get("candles", [])]


def _alert_cvd_last(tf, unit, exchange):
    """tf×unit のCVDで指定取引所の最新値。未取得ならNone。判定対象に登録もする。"""
    CVD_WANT[(tf, unit)] = time.time()
    ent = CVD_MEM.get((tf, unit))
    for s in (ent or {}).get("series", []):
        if s["label"] == exchange and s.get("data"):
            return s["data"][-1]["value"]
    return None


async def telegram_send(session, token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        await session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": text},
                           timeout=aiohttp.ClientTimeout(total=15))
    except Exception:
        pass


async def fire_alert(session, text):
    """設定済みの全チャンネル(Discord/Telegram)へ送信。"""
    ch = ALERT_CFG.get("channels", {})
    hook = ch.get("discord", "")
    if hook:
        await discord(session, hook, "🔔 チャートアラート", text, 0x1f6feb)
    tg = ch.get("telegram", {})
    await telegram_send(session, tg.get("token", ""), tg.get("chat_id", ""), "🔔 " + text)


def _crossed(prev, cur, level, direction):
    """prev→cur が level を direction(up/down/both)に跨いだか。"""
    if prev is None:
        return False
    up = prev < level <= cur
    dn = prev > level >= cur
    return (up if direction == "up" else dn if direction == "down" else (up or dn))


def _fmt(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"


async def _eval_rule(session, r):
    """1ルールを評価し、発火条件成立かつクールダウン外なら通知。状態は ALERT_STATE[id]。"""
    rid = r.get("id")
    st = ALERT_STATE.setdefault(rid, {"prev": None, "fired": 0, "armed": True})
    tf = r.get("tf", "5m")
    typ = r.get("type")
    cd = float(r.get("cooldown_min", 15)) * 60
    now = time.time()

    cond = False        # 今この瞬間「条件成立状態」か(armed再武装用)
    trig = False        # 「発火イベント」か(クロス/タッチの瞬間)
    msg = ""

    if typ == "price":
        cl = _alert_closes(tf)
        if not cl:
            return
        cur, lv = cl[-1], float(r["level"])
        trig = _crossed(st["prev"], cur, lv, r.get("dir", "both"))
        cond = trig
        st["prev"] = cur
        if trig:
            ar = "上抜け" if cur >= lv else "下抜け"
            msg = f"BTC {tf}: 価格 {_fmt(lv)} を{ar} (現値 {_fmt(cur)})"
    elif typ == "bb":
        cl = _alert_closes(tf)
        bb = _bb_py(cl, int(r.get("period", 9)), int(r.get("sigma", 2)))
        if not bb:
            return
        mid, up, lo = bb
        cur = cl[-1]
        band = r.get("band", "both")
        hit_up = cur >= up and band in ("upper", "both")
        hit_lo = cur <= lo and band in ("lower", "both")
        cond = hit_up or hit_lo
        trig = cond and st["armed"]
        if trig:
            which = f"+{r.get('sigma',2)}σ({_fmt(up)})" if hit_up else f"-{r.get('sigma',2)}σ({_fmt(lo)})"
            msg = f"BTC {tf}: BB {which} にタッチ (現値 {_fmt(cur)})"
    elif typ == "cvd":
        unit = r.get("unit", "coin")
        v = _alert_cvd_last(tf, unit, r.get("exchange", "Binance"))
        if v is None:
            return
        thr = float(r["value"])
        op = r.get("op", ">")
        cond = (v > thr) if op == ">" else (v < thr)
        trig = cond and st["armed"]
        if trig:
            msg = f"BTC {tf}: {r.get('exchange')} CVD({unit}) {_fmt(v)} {op} {_fmt(thr)}"
    elif typ == "cvd_div":
        unit = r.get("unit", "coin")
        ex = r.get("exchange", "Binance")
        CVD_WANT[(tf, unit)] = time.time()
        cl = _alert_closes(tf)
        ent = CVD_MEM.get((tf, unit))
        ser = next((s for s in (ent or {}).get("series", []) if s["label"] == ex), None)
        bars = int(r.get("bars", 12))
        if not cl or not ser or not ser.get("data") or len(cl) <= bars or len(ser["data"]) <= bars:
            return
        dprice = cl[-1] - cl[-1 - bars]
        dcvd = ser["data"][-1]["value"] - ser["data"][-1 - bars]["value"]
        cond = (dprice > 0 and dcvd < 0) or (dprice < 0 and dcvd > 0)
        trig = cond and st["armed"]
        if trig:
            d = "価格↑ CVD↓ (弱気乖離)" if dprice > 0 else "価格↓ CVD↑ (強気乖離)"
            msg = f"BTC {tf}: {ex} CVDダイバージェンス {d} (直近{bars}本)"
    else:
        return

    if typ != "price":
        st["armed"] = not cond          # 条件が外れたら再武装(price はクロスで毎回判定)

    if trig and msg and (now - st["fired"]) >= cd:
        st["fired"] = now
        if typ != "price":
            st["armed"] = False
        await fire_alert(session, msg)
        STATE["alerts"] = STATE.get("alerts", 0) + 1


async def alert_loop(session):
    """有効ルールを ~12秒ごとに評価。必要な足/CVDは CANDLE_WANT/CVD_WANT 登録で鮮度維持。"""
    await asyncio.sleep(8)              # 起動直後はキャッシュ充填待ち
    while True:
        for r in list(ALERT_CFG.get("rules", [])):
            if not r.get("enabled"):
                continue
            try:
                await _eval_rule(session, r)
            except Exception:
                pass
        await asyncio.sleep(12)


NOTIFY_DEDUP = 90       # poll/WS間のイベント二重通知防止窓(秒)
NOTIFY_BIG_USD = int(os.environ.get("NOTIFY_BIG_USD", "100000"))  # %閾値(40%)未満でも建玉変化のnotionalがこれ以上なら通知(大口の部分決済/建て増しを拾う・MMはnotify=Falseで除外)
POLL_SEM = None         # clearinghouse同時inflight上限(main で生成)
UF_SEM = None           # userFills直列化(HLが並行を429にするため・main で生成)
FLOW_SEM = None         # flow専用userFills直列化(pollのUF_SEMと分離・同時userFillsは最大2)


async def poll_one(session, a, life_b, learn_b):
    """1ウォレットの建玉スナップショット＋LIFE/CLOSES＋szi差分通知。並行実行用(逐次sleepなし・律速はBUCKET)。"""
    async with POLL_SEM:
        w = WATCH.get(a, {})
        now_s = time.time()
        try:
            acct, ps, cur_szi, ok = await fetch_ch(session, a)     # メインperp dex(weight律速)
            if not ok:                                             # 取得失敗=last-known-good維持(誤close/誤open洪水を防ぐ・PREV_SZIもPOSITIONSも触らない)
                return
            # newest userFills(notify=TTL更新 / MM=未学習を一度だけ学習)。budgetは共有dict(await跨がず原子的)
            uf = None
            do_uf = False
            if w.get("notify") and life_b["n"] > 0 and now_s - LIFE_FETCH.get(a, 0) > LIFE_TTL:
                do_uf = True
                life_b["n"] -= 1
            elif (not w.get("notify")) and a not in WALLET_DEXS and learn_b["n"] > 0:
                do_uf = True
                learn_b["n"] -= 1
            if do_uf:
                async with UF_SEM:            # userFillsは直列(HLが並行を即429にする)
                    uf = await hl_post(session, {"type": "userFills", "user": a}, W_UF)
                if uf is not None:
                    WALLET_DEXS[a] = {c.split(":", 1)[0] for f in uf
                                      for c in [f.get("coin", "")] if ":" in c}
                    LIFE_FETCH[a] = time.time()
            # 既知のビルダーperp dex(xyz等)も照会して合算(weight律速・逐次sleep不要)
            for dex in sorted(WALLET_DEXS.get(a, ())):
                dacct, dps, dszi, _dok = await fetch_ch(session, a, dex=dex)
                if not _dok:              # #6: ビルダーdex取得失敗はメインと同じlast-known-good=その巡は全体スキップ(ビルダー建玉消失→偽close→偽open通知を防ぐ)
                    return
                acct += dacct
                ps.extend(dps)
                cur_szi.update(dszi)
            dex_checked = a in WALLET_DEXS
            POSITIONS[a] = {"acct": round(acct), "pos": ps, "t": int(time.time()), "dex_checked": dex_checked}
            for p in ps:                       # 全銘柄アーカイブのpx源: 有効mark(notional/szi)を直近値として種付け
                m = p.get("mark")
                if m and m > 0:
                    MARK_LAST[p["coin"]] = m
            # szi差分: 新規/ドテン/クローズを検知し notify層へ通知(WS非カバー84件の安全網)。
            # WS が直近通知済なら LAST_EVT 窓でdedup。建玉notionalは ps から引いて通知に載せる。
            notl = {p["coin"]: p["notional"] for p in ps}
            prev_coins = {c: v for (aa, c), v in PREV_SZI.items() if aa == a}
            seen = a in SEEN_SZI   # このウォレットを過去sweepで取得成功済みか。未取得(初観測)はbaseline巡=通知もflowも出さずPREV_SZI種付けのみ→再起動/取得失敗後の初成功で既存建玉がopen化けする洪水を根絶(flow_warm単独だと失敗先行ウォレットを取りこぼす)
            for coin in set(prev_coins) | set(cur_szi):
                prev = prev_coins.get(coin, 0.0)
                nowv = cur_szi.get(coin, 0.0)
                tr = transition(prev, nowv)   # open/close/flip/reduce(40%減)/add(40%増)。WS経路と同一ロジック
                if not tr and abs(nowv - prev) > 1e-9 and w.get("notify"):
                    # %閾値未満でも建玉変化のnotionalが大きい部分決済/建て増しは notify層のみ通知(大口の意味ある動き)。
                    pxm = _mark(a, coin)
                    if pxm > 0 and abs(nowv - prev) * pxm >= NOTIFY_BIG_USD:
                        tr = "add" if abs(nowv) > abs(prev) else "reduce"
                _le = LAST_EVT.get((a, coin))
                fresh = (not _le) or now_s - _le[0] > NOTIFY_DEDUP or _le[1] != tr  # 同一transの窓内再配信(WS+poll二重)のみ抑制。open→close→open等の別遷移は窓内でも通す=実イベント取りこぼし防止
                if tr and seen and w.get("notify") and fresh:
                    LAST_EVT[(a, coin)] = (now_s, tr)   # #13: await前に記録(WS側と同順)=Discord POST待機中に同一変化をWSが二重通知するのを防ぐ
                    await notify_event(session, a, coin, tr, prev, nowv, [], notional=notl.get(coin))
                if tr == "close":
                    LIFE.pop((a, coin), None)
                if seen and (ARCH_COINS == "all" or coin == "BTC"):    # 前向きszi差分=起動後の建玉変化を検知時刻に記録(通知と同源)。初観測はbaseline化で除外
                    try:                       # per-coin try: 1コインの例外でloop中断→下のPREV_SZI更新漏れ→次sweep二重発火、を防ぐ
                        flow_szi_event(a, prev, nowv, coin=coin)
                    except Exception:
                        STATE["flow_evt_err"] = STATE.get("flow_evt_err", 0) + 1
            for coin in [c for c in prev_coins if c not in cur_szi]:
                PREV_SZI.pop((a, coin), None)
            for coin, szi in cur_szi.items():
                PREV_SZI[(a, coin)] = szi
            SEEN_SZI.add(a)   # 取得成功(ok)後にマーク=次sweep以降は実変化を通知/記録
            # LIFE/CLOSES を同じ userFills から算出(メイン+ビルダー全建玉が ps に揃った後)
            if uf is not None:
                for p in ps:
                    lc = lifecycle_from_newest(uf, p["coin"], p["szi"])
                    if lc:
                        ex = LIFE.get((a, p["coin"]))
                        if ex and (ex.get("last_fill") or 0) > (lc["last_fill"] or 0):
                            lc["last_fill"] = ex["last_fill"]   # WS先行の最終約定は後退させない
                        LIFE[(a, p["coin"])] = lc
                CLOSES[a] = {"t": time.time(), "items": extract_closes(uf)}
                # FLOWはszi-delta単一源ゆえ piggyback(userFills)は無効(二重加算防止)
        except Exception:
            pass   # last-known-good: 一時失敗で POSITIONS を消さない(tも更新しない=鮮度で古さが見える)


async def poll_loop(session):
    """表示値の単一ライター。全ウォレットを並行スイープ(weightトークンバケットで律速・burst歯止め)。"""
    while True:
        t0 = time.time()
        err0 = STATE.get("rate429", 0)
        life_b = {"n": LIFE_PER_CYCLE}
        learn_b = {"n": MM_LEARN_PER_CYCLE}
        await asyncio.gather(*(poll_one(session, a, life_b, learn_b) for a in list(WATCH.keys())))
        STATE["last_poll"] = int(time.time())
        STATE["sweep_sec"] = round(time.time() - t0, 1)
        STATE["flow_warm"] = True   # 初回sweep完了後はPREV_SZIが揃う→以後のszi変化だけdirtyマーク(再起動洪水防止)
        # 429がこの巡で出なければ回復(出ていれば hl_post が既に半減済→据置)。回復目標はSWEEP_RATE(flow充填中は絞られる)。
        if STATE.get("rate429", 0) == err0:
            BUCKET.rate = SWEEP_RATE
        STATE["wps"] = round(BUCKET.rate, 1)
        save_dexs()
        await asyncio.sleep(max(1.0, POLL_MIN_SEC - (time.time() - t0)))


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
                plist = ("<span class=muted>建玉なし</span>" if d.get("dex_checked", True)
                         else "<span class=muted>⏳ 取得中…</span>")
            acct = f"${d['acct']:,}"
        # 鮮度バッジ(更新停滞)
        stale = ""
        if d and (now_s - d.get("t", 0) > 150):
            mins = int((now_s - d.get("t", 0)) / 60)
            stale = f"<span class=stale>⚠ {mins}分前</span>"
        actbadge = f"<span class='ab {'on' if w.get('active14') else 'off'}'>{act}</span>"
        fs = w.get("first_seen")
        fsbadge = f"<br><span class=muted>発掘 {esc(fs)}</span>" if fs else ""
        rows += (f"<tr data-f=\"{esc(ftok)}\"><td><b style='color:{col}'>{esc(w['position'])}</b><br>{qtag} {actbadge}{fsbadge}</td>"
                 f"<td>{esc(w['label'])}<br><code>{esc(a[:14])}…</code>"
                 f"<div class=lnk><a href='{ASXN.format(a=a)}' target=_blank>ASXN</a> "
                 f"<a href='https://app.hyperliquid.xyz/explorer/address/{a}' target=_blank>HL</a></div></td>"
                 f"<td>{acct}{stale}</td><td>{plist}</td><td>{closes_cell(a)}</td></tr>")

    demos = _load_demotions()
    if demos:
        drows = "".join(
            f"<tr><td>{esc(r.get('date') or '')}</td>"
            f"<td><b>{esc(r.get('from') or '')}</b>{('／' + esc(r.get('from_q')) if r.get('from_q') else '')}</td>"
            f"<td><code>{esc(r.get('address', '')[:14])}…</code> "
            f"<a href='{ASXN.format(a=r.get('address', ''))}' target=_blank>ASXN</a></td>"
            f"<td>{esc(r.get('first_seen') or '?')}</td><td class=muted>{esc(r.get('reason') or '')}</td></tr>"
            for r in demos)
        demo_html = (f"<details class=demo><summary>⬇ 最近の降格 {len(demos)}件（→除外/低優先）</summary>"
                     f"<table><tr><th>降格日</th><th>降格前(区分/品質)</th><th>ウォレット</th><th>発掘日</th><th>理由</th></tr>{drows}</table></details>")
    else:
        demo_html = ""

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
／ 日時は日本時間(JST)。net=clearinghouse szi・ライフサイクル=newest userFills(再構成しない)。エントリー/クローズ/ドテンはDiscordへ即通知(MM除く)。
／ <a href="/charts" style="color:#4ea1ff">📈 {'/'.join(CHART_COINS)} チャート →</a>　<a href="/alerts" style="color:#4ea1ff">🔔 アラート設定 →</a></div>
<div class="fb">
  <div><span class=gl>区分</span>{posbar}</div>
  <div><span class=gl>品質</span>{qbar}</div>
  <div><span class=gl>稼働</span>{actbar}</div>
  <div><span class=gl>方向</span>{dbar}<span id=cf>✕ 解除</span><span id=cnt></span></div>
</div>
<h2>📊 監視対象 全{len(WATCH)}件（建玉保有 {n_held}件 ／ 取得待ち {n_wait}件）</h2>
{demo_html}
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


async def handle_candles(request):
    """/candles?coin=BTC&interval=1m → lightweight-charts形式のローソク足JSON。"""
    coin = (request.query.get("coin") or "").upper()
    interval = request.query.get("interval") or "1m"
    if coin not in CHART_COINS or interval not in CHART_INTERVALS:
        return web.json_response({"error": "coin must be BTC/ETH/SOL, interval 1m/5m/15m/1h"}, status=400)
    CANDLE_WANT[(coin, interval)] = time.time()        # バックグラウンド更新対象に登録
    ent = CANDLE_MEM.get((coin, interval))
    if ent is not None:                                # キャッシュ即返し(HLを叩かずブロックしない)
        return web.json_response({"coin": coin, "interval": interval, "candles": ent["candles"]})
    try:                                               # 初回(未キャッシュ)のみその場で取得
        candles = await fetch_candles(SESSION, coin, interval)
    except Exception as e:
        return web.json_response({"error": str(e)[:80], "candles": []}, status=502)
    return web.json_response({"coin": coin, "interval": interval, "candles": candles})


async def handle_cvd(request):
    """/cvd?interval=1m&unit=coin|usd → 取引所別 spot CVD(bar-polarity累積) JSON。キャッシュ即返し。"""
    interval = request.query.get("interval") or "1m"
    unit = request.query.get("unit") or CVD_UNIT_DEFAULT
    if interval not in CHART_INTERVALS or unit not in ("coin", "usd"):
        return web.json_response({"error": "interval 1m/5m/15m/1h, unit coin/usd"}, status=400)
    CVD_WANT[(interval, unit)] = time.time()
    ent = CVD_MEM.get((interval, unit))
    if ent is not None:
        return web.json_response({"interval": interval, "unit": unit, "series": ent["series"]})
    try:
        series = await fetch_cvd(SESSION, interval, unit)
    except Exception as e:
        return web.json_response({"error": str(e)[:80], "series": []}, status=502)
    return web.json_response({"interval": interval, "unit": unit, "series": series or []})


async def handle_flow(request):
    """/flow?groups=&pos=&q=&interval=&unit= → 監視台帳BTC約定の4方向(ol/cs/os/cl)バケット集計+facets。"""
    q = request.query
    interval = q.get("interval") or "1m"
    unit = q.get("unit") or "coin"
    if interval not in CHART_INTERVALS or unit not in ("coin", "usd"):
        return web.json_response({"error": "interval 1m/5m/15m/1h, unit coin/usd"}, status=400)
    groups = frozenset(x for x in (q.get("groups") or "").split(",") if x)
    poss = frozenset(x for x in (q.get("pos") or "").split(",") if x)
    quals = frozenset(x for x in (q.get("q") or "").split(",") if x)
    mode = "and" if (q.get("mode") or "or") == "and" else "or"
    key = (groups, poss, quals, interval, unit, mode)
    ent = FLOW_MEM.get(key)
    if ent and time.time() - ent["t"] < FLOW_TTL:
        data, n = ent["data"], ent["n"]
    else:
        data, n = _flow_aggregate(groups, poss, quals, interval, unit, mode)
        FLOW_MEM[key] = {"t": time.time(), "data": data, "n": n}
    return web.json_response({"interval": interval, "unit": unit, "matched": n,
                              "facets": _flow_facets(), "data": data})


def render_charts():
    """BTC/ETH/SOL のリアルタイム・ローソク足チャート専用ページ。建玉マーカー重ねは後付け可(setMarkers)。"""
    ivbtn = "".join(f"<button class=iv data-iv=\"{iv}\">{iv}</button>" for iv in CHART_INTERVALS)
    cvd_on = "true"
    cvd_def = json.dumps([{"label": l, "color": c} for l, c in SPOT_EXCH])
    glabel = json.dumps(GROUP_LABEL, ensure_ascii=False)
    # 単一チャート(2ペイン)方式: ローソク=pane0 / CVD=pane1。時間軸・ズーム・クロスヘアが構造的に1つ。
    combined_html = (
        "<div class=card>"
        "<div class=ttl><span id=ttl_coin>BTC</span><span class=px id=px_BTC></span>"
        "<span style='margin-left:14px'>"
        "<button class=un data-un=\"coin\">枚</button>"
        "<button class=un data-un=\"usd\">USD</button></span></div>"
        "<div class=chwrap><div class=ch id=ch_combined></div>"
        "<button id=cvdtagbtn class=tagbtn>🏷 取引所タグ</button></div>"
        "<div style='color:#6e7681;font-size:10px;margin-top:4px'>"
        "下=spot CVD（取引所別・TV方式 前足終値比tick-rule・当日UTC0時で日次リセット・線右端のタグが取引所）。"
        "出典: 7取引所の公開API直叩き(Binance/Bitfinex/KuCoin/Coinbase/Kraken/Bybit/OKX)。"
        "TVは独自集約のため絶対値の完全一致は不可(符号/順序/形状を一致)。"
        "上=ローソク / 下=CVD、時間軸は最下部1本・ズーム完全連動</div></div>")
    return f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>HL チャート {'/'.join(CHART_COINS)}</title>
<script src="https://unpkg.com/lightweight-charts@5.0.7/dist/lightweight-charts.standalone.production.js"></script>
<style>
body{{font-family:system-ui,sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:16px;font-size:13px}}
h1{{font-size:18px;margin:0 0 6px}} a{{color:#4ea1ff;text-decoration:none}}
.bar{{margin:6px 0 12px}} .iv{{cursor:pointer;background:#0b0f14;color:#cbd5e1;border:1px solid #30363d;border-radius:8px;padding:3px 11px;margin-right:4px;font-size:12px}}
.iv.on{{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:700}}
.un{{cursor:pointer;background:#0b0f14;color:#cbd5e1;border:1px solid #30363d;border-radius:6px;padding:2px 9px;margin-right:3px;font-size:11px}}
.un.on{{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:700}}
.ovbar{{margin:2px 0 12px;font-size:12px;color:#cbd5e1;display:flex;flex-wrap:wrap;align-items:center;gap:2px}}
.ovbar b{{color:#8b949e;margin:0 4px 0 10px}}
.ovl{{margin:0 5px 0 0;cursor:pointer;white-space:nowrap}}
.ovp{{width:42px;background:#0b0f14;color:#cbd5e1;border:1px solid #30363d;border-radius:5px;padding:1px 4px;font-size:11px;margin-right:6px}}
.chwrap{{position:relative}}
.tagbtn{{position:absolute;left:8px;top:420px;z-index:5;cursor:pointer;background:rgba(11,15,20,.85);color:#cbd5e1;border:1px solid #30363d;border-radius:6px;padding:2px 8px;font-size:11px}}
.tagbtn.off{{color:#6e7681;opacity:.65}}
.grid{{display:flex;flex-wrap:wrap;gap:14px}}
.card{{background:#10151c;border:1px solid #232a34;border-radius:10px;padding:8px;flex:1 1 360px;min-width:320px}}
.ttl{{font-size:14px;font-weight:700;margin:2px 4px 6px}} .px{{font-weight:400;color:#8b949e;font-size:12px;margin-left:8px}}
/* ローソク(pane0)+CVD(pane1)を1枚のチャートに密着。隙間はペイン区切り線のみ=TV風 */
.ch{{height:640px}}</style></head><body>
<h1>📈 HL リアルタイムチャート（{' / '.join(CHART_COINS)}）　<a href="/">← 監視台帳</a>　<a href="/alerts">🔔 アラート設定</a></h1>
<div class=bar>足: {ivbtn}<span style="color:#8b949e;margin-left:10px">{CANDLE_TTL}秒ごと更新・現在足はライブ</span></div>
<div id=ovbar class=ovbar></div>
<div id=flowbar class=ovbar></div>
<div class=grid>{combined_html}</div>
<script>
const LWC=LightweightCharts;
const COINS={json.dumps(CHART_COINS)};
const COIN=COINS[0];                       // 単一チャート方式: 先頭(BTC)を採用
const CVD_ON={cvd_on};
const CVDDEF={cvd_def};
const CANDLE_H=440, FLOW_H=150, CVD_H=160;   // pane0/pane1/pane2 高さ配分
const P_CANDLE=0, P_FLOW=1, P_CVD=2;         // pane index定数(番号付け替え漏れ防止)
const GLABEL={glabel};
let IV=localStorage.getItem('chartiv')||'1m';
let UN=localStorage.getItem('cvdunit')||'{CVD_UNIT_DEFAULT}';
let chart=null, candle=null;               // 単一 IChartApi とローソク系列
const cvdSeries={{}};                      // ラベル→ISeriesApi (pane2)
const flowS={{}};                          // ol/cs/os/cl → HistogramSeries (pane1)
let FLOWSEL; try{{FLOWSEL=JSON.parse(localStorage.getItem('flowsel'))||{{g:[],p:[],q:[]}};}}catch(e){{FLOWSEL={{g:[],p:[],q:[]}};}}
let needFit=true;                          // 初回/足切替時に直近120本へ表示を合わせる
let candleMarkers=null;                    // 将来の建玉重ね(createSeriesMarkers)用ハンドル
let CANDLES=[];                            // 最新ローソク(オーバーレイ計算用)
// ── 描画増分化: 毎tickの全置換setData(ローソク+EMA/BB全点)を末尾update主体に。全置換は初回/足切替/多本ギャップ/定期回収のみ ──
const IVSEC={{'1m':60,'5m':300,'15m':900,'1h':3600}};
function ivsec(){{return IVSEC[IV]||60;}}
let needFullCandle=true;                   // 足切替/初回で全置換を強制
let prevLastT=null, tickN=0;               // ローソク末尾time / tick計数(定期full用)
function safeUpd(s,bar){{ try{{ s.update(bar); return true; }}catch(e){{ return false; }} }}  // time<lastはthrow→false→呼元がfull退避
// ── EMA(4本)+ボリンジャーバンド(±1〜3σ) オーバーレイ(pane0・サイトから設定/個別ON/OFF) ──
const EMA_COLORS=['#f5e441','#f0a13a','#f0564e','#a23bc4'];  // EMA1黄/EMA2橙/EMA3赤/EMA4紫
// ボリバン: 上バンド(+)=赤系で同色・下バンド(-)=緑系で同色。σが外側ほど透過(濃→淡)。
const BB_UP='#ff5d6c', BB_DN='#3fb950';
const BB_A=['E6','8C','4D'];   // σ1/σ2/σ3 のアルファ(8桁hex・約0.9/0.55/0.30)
const OVL_DEF={{ema:[{{on:true,p:9}},{{on:true,p:21}},{{on:false,p:50}},{{on:false,p:200}}],
               bb:{{p:20,s1:true,s2:true,s3:false}}}};
let OVL; try{{OVL=JSON.parse(localStorage.getItem('ovl'))||OVL_DEF;}}catch(e){{OVL=OVL_DEF;}}
let emaS=[]; const bbS={{}};               // 系列ハンドル
let CVDTAGS=localStorage.getItem('cvdtags')!=='0';   // CVD右端の取引所タグ表示(既定ON)
function applyCvdTags(){{ Object.keys(cvdSeries).forEach(label=>{{ const s=cvdSeries[label];
  try{{s.applyOptions({{lastValueVisible:true, title:CVDTAGS?label:''}});}}catch(e){{}} }});
  const b=document.getElementById('cvdtagbtn'); if(b) b.classList.toggle('off',!CVDTAGS); }}
function positionTagBtn(){{ try{{ const ps=chart.panes(); const b=document.getElementById('cvdtagbtn');
  if(ps[P_CANDLE]&&ps[P_FLOW]&&b) b.style.top=(ps[P_CANDLE].getHeight()+ps[P_FLOW].getHeight()+12)+'px'; }}catch(e){{}} }}
function emaCalc(vals,p){{const k=2/(p+1);let e=null;const o=[];for(let i=0;i<vals.length;i++){{e=(e===null)?vals[i]:vals[i]*k+e*(1-k);o.push(e);}}return o;}}
function bbCalc(vals,p){{const sma=[],sd=[];for(let i=0;i<vals.length;i++){{if(i<p-1){{sma.push(null);sd.push(null);continue;}}let s=0;for(let j=i-p+1;j<=i;j++)s+=vals[j];const m=s/p;let v=0;for(let j=i-p+1;j<=i;j++){{const dd=vals[j]-m;v+=dd*dd;}}sma.push(m);sd.push(Math.sqrt(v/p));}}return{{sma,sd}};}}

function build(){{
  const el=document.getElementById('ch_combined'); if(!el) return;
  chart=LWC.createChart(el,{{
    width:el.clientWidth, height:el.clientHeight||{440+150+160+12},
    layout:{{
      background:{{color:'#10151c'}}, textColor:'#cbd5e1',
      panes:{{separatorColor:'#232a34', separatorHoverColor:'rgba(120,140,170,0.25)', enableResize:true}}
    }},
    grid:{{vertLines:{{color:'#1c2230'}}, horzLines:{{color:'#1c2230'}}}},
    localization:{{timeFormatter:(t)=>{{const d=new Date((t+32400)*1000),p=n=>String(n).padStart(2,'0');return (d.getUTCMonth()+1)+'/'+d.getUTCDate()+' '+p(d.getUTCHours())+':'+p(d.getUTCMinutes());}}}},  // クロスヘア時刻=JST(UTC+9)
    // 時間軸はチャートに1つだけ=最下部に1本(下端=CVDペインの下)。上ペインに別目盛は出ない。軸ラベルもJST。
    timeScale:{{timeVisible:true, secondsVisible:false, borderColor:'#30363d', rightOffset:6,
      tickMarkFormatter:(t,tmt)=>{{const d=new Date((t+32400)*1000),p=n=>String(n).padStart(2,'0');return tmt<=2?((d.getUTCMonth()+1)+'/'+d.getUTCDate()):(p(d.getUTCHours())+':'+p(d.getUTCMinutes()));}}}},
    rightPriceScale:{{borderColor:'#30363d', minimumWidth:72}},
    // TV風操作:ホイールズーム/ドラッグパン/軸ドラッグ/ダブルクリックリセット
    handleScroll:{{mouseWheel:true, pressedMouseMove:true, horzTouchDrag:true, vertTouchDrag:true}},
    handleScale:{{mouseWheel:true, pinch:true,
      axisPressedMouseMove:{{time:true, price:true}}, axisDoubleClickReset:{{time:true, price:true}}}},
    kineticScroll:{{touch:true, mouse:true}},          // マウス慣性(既定false)を明示ON
    crosshair:{{mode:LWC.CrosshairMode.Magnet}}        // 単一chartなので十字線は全ペイン貫通
  }});
  // ローソク=pane0(既定)
  candle=chart.addSeries(LWC.CandlestickSeries,{{
    upColor:'#3fb950',downColor:'#ff5d6c',borderUpColor:'#3fb950',
    borderDownColor:'#ff5d6c',wickUpColor:'#3fb950',wickDownColor:'#ff5d6c'}});
  // EMA4本+BB7本 を pane0 に(初期は非表示、renderOverlaysで設定反映)
  for(let i=0;i<4;i++) emaS.push(chart.addSeries(LWC.LineSeries,
    {{color:EMA_COLORS[i],lineWidth:1,priceLineVisible:false,lastValueVisible:true,visible:false,
      crosshairMarkerVisible:false}}, 0));
  [['u1',BB_UP+BB_A[0]],['l1',BB_DN+BB_A[0]],['u2',BB_UP+BB_A[1]],['l2',BB_DN+BB_A[1]],
   ['u3',BB_UP+BB_A[2]],['l3',BB_DN+BB_A[2]]].forEach(([k,c])=>{{
    bbS[k]=chart.addSeries(LWC.LineSeries,{{color:c,lineWidth:1,priceLineVisible:false,
      lastValueVisible:true,visible:false,crosshairMarkerVisible:false}}, 0);
  }});
  // ── BTC約定フロー(pane1): 買い=正/売り=負・base0・同一priceScaleId 'flow'。
  //   スタックは自前累積。addSeries順=z順(後=手前)ゆえ『薄(累積大)→濃(累積小)』の順で追加し濃の底を手前に出す。
  flowS.cs=chart.addSeries(LWC.HistogramSeries,{{color:'#7fd1a0',base:0,priceScaleId:'right',
    priceFormat:{{type:'volume'}},lastValueVisible:false,priceLineVisible:false}}, P_FLOW);  // 買いクローズ(薄)・先
  flowS.ol=chart.addSeries(LWC.HistogramSeries,{{color:'#1b7a45',base:0,priceScaleId:'right',
    priceFormat:{{type:'volume'}},lastValueVisible:false,priceLineVisible:false}}, P_FLOW);  // 買い新規(濃)・後
  flowS.cl=chart.addSeries(LWC.HistogramSeries,{{color:'#e88888',base:0,priceScaleId:'right',
    priceFormat:{{type:'volume'}},lastValueVisible:false,priceLineVisible:false}}, P_FLOW);  // 売りクローズ(薄)・先
  flowS.os=chart.addSeries(LWC.HistogramSeries,{{color:'#a01b1b',base:0,priceScaleId:'right',
    priceFormat:{{type:'volume'}},lastValueVisible:false,priceLineVisible:false}}, P_FLOW);  // 売り新規(濃)・後
  try{{ flowS.ol.priceScale().applyOptions({{scaleMargins:{{top:0.05,bottom:0.05}}}}); }}catch(e){{}}
  try{{ flowS.ol.createPriceLine({{price:0,color:'#5a6270',lineWidth:1,lineStyle:2,axisLabelVisible:false}}); }}catch(e){{}}
  // CVD 7取引所=pane2(フローpaneをpane1に新設したため繰り下げ)。
  CVDDEF.forEach(d=>{{
    cvdSeries[d.label]=chart.addSeries(LWC.LineSeries,
      {{color:d.color,lineWidth:1,priceLineVisible:false,lastValueVisible:true,title:d.label}}, P_CVD);
  }});
  applyCvdTags(); positionTagBtn();
  const z0=Object.values(cvdSeries)[0];
  if(z0) z0.createPriceLine({{price:0,color:'#5a6270',lineWidth:1,lineStyle:2,axisLabelVisible:true,title:'0'}});
  try{{ const ps=chart.panes();
    if(ps[P_FLOW]) ps[P_FLOW].setHeight(FLOW_H);
    if(ps[P_CVD]) ps[P_CVD].setHeight(CVD_H); }}catch(e){{}}
  new ResizeObserver(()=>{{chart.applyOptions({{width:el.clientWidth, height:el.clientHeight}}); positionTagBtn();}}).observe(el);
}}
async function load(){{
  if(!chart||!candle) return;
  try{{
    const r=await fetch('/candles?coin='+COIN+'&interval='+IV);
    const j=await r.json(); const d=j.candles||[];
    if(!d.length) return;
    tickN++;
    const ims=ivsec(); const last=d[d.length-1];
    // 描画モード判定: 末尾足の時刻前進量 k で分岐(長さ差では窓スライドを検知不能ゆえ時刻基準)
    let mode='full';
    if(!needFullCandle && CANDLES.length>=2 && prevLastT!=null && (tickN%15!==0)){{
      const hasPrev=d.length>=2 && (d[d.length-2].time===prevLastT || last.time===prevLastT);
      if(hasPrev){{ const k=Math.round((last.time-prevLastT)/ims);
        if(k<=0) mode='tick';          // 同足内tick: 現在足のみ更新
        else if(k===1) mode='roll';    // ロール直後: 確定した旧足in-place + 新足append
        // k>=2(多本ギャップ) は full のまま(中間足はupdate到達不能)
      }}
    }}
    let ok=true;
    if(mode==='tick') ok=safeUpd(candle,last);
    else if(mode==='roll') ok=safeUpd(candle,d[d.length-2])&&safeUpd(candle,last);
    if(mode==='full'||!ok){{ candle.setData(d); mode='full'; }}   // 初回/足切替/ギャップ/update失敗の退避
    CANDLES=d; prevLastT=last.time; needFullCandle=false;
    renderOverlays(mode);                                          // 分岐外で無条件(凍結防止)
    if(needFit){{ needFit=false;   // 初回/足切替時のみ: 直近120本に合わせる
      try{{ chart.timeScale().setVisibleLogicalRange({{from:Math.max(0,d.length-120), to:d.length+6}}); }}catch(e){{}}
    }}
    const px=document.getElementById('px_'+COIN);                 // ライブ現値は毎poll無条件更新
    if(px){{ px.textContent='$'+last.close.toLocaleString(); px.style.color=last.close>=last.open?'#69d98a':'#ff8893'; }}
  }}catch(e){{}}
}}
async function loadCVD(){{
  if(!CVD_ON||!chart) return;
  try{{
    const r=await fetch('/cvd?interval='+IV+'&unit='+UN); const j=await r.json();
    (j.series||[]).forEach(s=>{{ if(cvdSeries[s.label]) cvdSeries[s.label].setData(s.data||[]); }});
    try{{ const ps=chart.panes(); if(ps[P_CVD]) ps[P_CVD].setHeight(CVD_H); }}catch(e){{}}
    positionTagBtn();
  }}catch(e){{}}
}}
async function loadFlow(){{
  if(!chart) return;
  try{{
    const qs='/flow?interval='+IV+'&unit='+UN+'&mode='+(FLOWSEL.mode||'or')
      +'&groups='+encodeURIComponent(FLOWSEL.g.join(','))
      +'&pos='+encodeURIComponent(FLOWSEL.p.join(','))
      +'&q='+encodeURIComponent(FLOWSEL.q.join(','));
    const r=await fetch(qs); const j=await r.json(); const d=j.data||[];
    // 自前スタック: 買い=正(薄=ol+cs を奥, 濃=ol を手前) / 売り=負(薄=-(os+cl) 奥, 濃=-os 手前)
    const aOL=[],aCS=[],aOS=[],aCL=[];
    d.forEach(x=>{{
      aCS.push({{time:x.time, value:x.ol+x.cs}});
      aOL.push({{time:x.time, value:x.ol}});
      aCL.push({{time:x.time, value:-(x.os+x.cl)}});
      aOS.push({{time:x.time, value:-x.os}});
    }});
    flowS.cs.setData(aCS); flowS.ol.setData(aOL); flowS.cl.setData(aCL); flowS.os.setData(aOS);
    try{{ const ps=chart.panes(); if(ps[P_FLOW]) ps[P_FLOW].setHeight(FLOW_H); }}catch(e){{}}
    renderFlowUI(j.facets, j.matched);
    positionTagBtn();
  }}catch(e){{}}
}}
function renderFlowUI(fac, matched){{
  const el=document.getElementById('flowbar'); if(!el||!fac) return;
  const g=fac.group||{{}}, qf=fac.quality||{{}};
  const chk=(t,val,lab,cnt,on)=>'<label class=ovl><input type=checkbox data-ft='+t
    +' data-v="'+encodeURIComponent(val)+'"'+(on?' checked':'')+'> '+lab+'<span class=tag>('+cnt+')</span></label>';
  const md=FLOWSEL.mode||'or';
  const GORDER=['pro','alt','insider','mm'];
  const QORDER=['エリート','堅実','中堅','ムラあり','alt主体','履歴薄/評価不能'];
  const ord=(obj,pref)=>pref.filter(k=>k in obj).concat(Object.keys(obj).filter(k=>!pref.includes(k)));
  let h='<b>BTC約定フロー</b><span class=tag>選択='+matched+'件 充填'+fac.filled+'/'+fac.total+'</span>'
    +'<br><b>絞り込み</b><button class=un data-md=or'+(md==='or'?' style="background:#1f6feb;color:#fff"':'')+'>OR(和)</button>'
    +'<button class=un data-md=and'+(md==='and'?' style="background:#1f6feb;color:#fff"':'')+'>AND(積)</button>'
    +'<br><b>区分</b>';
  ord(g,GORDER).forEach(k=>{{ h+=chk('g',k,(GLABEL[k]||k),g[k],FLOWSEL.g.includes(k)); }});
  h+='<br><b>品質</b>';
  ord(qf,QORDER).forEach(k=>{{ h+=chk('q',k,k,qf[k],FLOWSEL.q.includes(k)); }});
  h+='<br><span style="color:#8b949e">買い=緑(濃:新規ロング/薄:ショートクローズ)・売り=赤(濃:新規ショート/薄:ロングクローズ)</span>';
  el.innerHTML=h;
  el.querySelectorAll('input').forEach(inp=>inp.addEventListener('change',onFlowChange));
  el.querySelectorAll('button[data-md]').forEach(b=>b.onclick=()=>{{ FLOWSEL.mode=b.dataset.md;
    localStorage.setItem('flowsel',JSON.stringify(FLOWSEL)); loadFlow(); }});
}}
function onFlowChange(e){{
  const t=e.target.dataset.ft, v=decodeURIComponent(e.target.dataset.v), on=e.target.checked;
  const arr=(t==='g')?FLOWSEL.g:(t==='q')?FLOWSEL.q:FLOWSEL.p;
  const i=arr.indexOf(v);
  if(on&&i<0) arr.push(v); else if(!on&&i>=0) arr.splice(i,1);
  localStorage.setItem('flowsel',JSON.stringify(FLOWSEL));
  loadFlow();
}}
function setIV(iv){{IV=iv;localStorage.setItem('chartiv',iv); needFit=true; needFullCandle=true; prevLastT=null;  // 足切替=時間軸総入替→ローソク/オーバーレイ全置換
  document.querySelectorAll('.iv').forEach(b=>b.classList.toggle('on',b.dataset.iv===iv));
  load(); loadCVD(); loadFlow();}}
function setUN(un){{UN=un;localStorage.setItem('cvdunit',un);
  document.querySelectorAll('.un').forEach(b=>b.classList.toggle('on',b.dataset.un===un));
  loadCVD(); loadFlow();}}
// オーバーレイ(EMA/BB)を現在のCANDLESから毎回**全再計算**(チェーン状態を持たない=ドリフト源を断つ)し、
// mode で反映方式を切替: full=setData全点 / roll=末尾2点update(旧足確定+新足) / tick=末尾1点update。
// 過去足のEMA/BBは過去closeのみ依存で不変ゆえ末尾以外は触れなくてよい。mode省略/'full'は全置換(トグル時)。
function renderOverlays(mode){{
  if(!CANDLES.length||!emaS.length) return;
  mode=mode||'full';
  const times=CANDLES.map(c=>c.time), closes=CANDLES.map(c=>c.close); const n=times.length;
  // v: 値配列(null可)。full=null除去setData / roll,tick=末尾のみsafeUpd(null点は触らない・BB先頭p-1本のnull対策)
  const apply=(s,v)=>{{
    if(mode==='full'){{ const data=[]; for(let i=0;i<n;i++) if(v[i]!=null) data.push({{time:times[i],value:v[i]}}); s.setData(data); return; }}
    if(mode==='roll'&&n>=2&&v[n-2]!=null) safeUpd(s,{{time:times[n-2],value:v[n-2]}});
    if(v[n-1]!=null) safeUpd(s,{{time:times[n-1],value:v[n-1]}});
  }};
  OVL.ema.forEach((e,i)=>{{ const s=emaS[i]; if(!s) return;
    if(e.on){{ apply(s,emaCalc(closes,e.p)); s.applyOptions({{visible:true,title:'EMA'+e.p}}); }}
    else s.applyOptions({{visible:false}});
  }});
  const p1=OVL.ema[0].p;                      // BBは全てEMA1の期間: 中央=EMA1、σ窓もEMA1期間
  const ema1=emaCalc(closes,p1);
  const {{sd}}=bbCalc(closes,p1);
  const BBL={{u1:'+1σ',l1:'-1σ',u2:'+2σ',l2:'-2σ',u3:'+3σ',l3:'-3σ'}};
  const bline=(key,mult,on)=>{{ const s=bbS[key]; if(!s) return;
    if(on){{ const v=times.map((t,i)=>(sd[i]!=null)?(ema1[i]+mult*sd[i]):null); apply(s,v); s.applyOptions({{visible:true,title:BBL[key]}}); }}
    else s.applyOptions({{visible:false}});
  }};
  bline('u1',1,OVL.bb.s1); bline('l1',-1,OVL.bb.s1);
  bline('u2',2,OVL.bb.s2); bline('l2',-2,OVL.bb.s2);
  bline('u3',3,OVL.bb.s3); bline('l3',-3,OVL.bb.s3);
}}
// 設定バー(チェックボックス+期間入力)を生成
function buildOvUI(){{
  const el=document.getElementById('ovbar'); if(!el) return;
  let h='<b>EMA</b>';
  OVL.ema.forEach((e,i)=>{{ h+='<label class=ovl style="color:'+EMA_COLORS[i]+'"><input type=checkbox data-t=ema data-i='+i+(e.on?' checked':'')+'> EMA'+(i+1)+'</label>'
    +'<input type=number min=1 class=ovp data-t=emap data-i='+i+' value='+e.p+'>'; }});
  h+='<b>BB</b>';
  [['s1','±1σ'],['s2','±2σ'],['s3','±3σ']].forEach(([k,lb],i)=>{{
    h+='<label class=ovl><input type=checkbox data-t=bb data-k='+k+(OVL.bb[k]?' checked':'')
      +'> <span style="color:'+BB_UP+BB_A[i]+'">'+lb+'</span></label>'; }});
  el.innerHTML=h;
  el.querySelectorAll('input').forEach(inp=>inp.addEventListener('change',onOvChange));
}}
function onOvChange(e){{ const t=e.target.dataset.t;
  if(t==='ema') OVL.ema[+e.target.dataset.i].on=e.target.checked;
  else if(t==='emap') OVL.ema[+e.target.dataset.i].p=Math.max(1,parseInt(e.target.value)||1);
  else if(t==='bbp') OVL.bb.p=Math.max(2,parseInt(e.target.value)||20);
  else if(t==='bb') OVL.bb[e.target.dataset.k]=e.target.checked;
  localStorage.setItem('ovl',JSON.stringify(OVL)); renderOverlays();
}}
// ダブルクリックで全体表示(TVの『全体に戻す』)
function bindReset(){{
  const el=document.getElementById('ch_combined'); if(!el||!chart) return;
  el.addEventListener('dblclick',()=>{{ try{{chart.timeScale().fitContent();}}catch(e){{}} }});
}}
document.querySelectorAll('.iv').forEach(b=>b.onclick=()=>setIV(b.dataset.iv));
document.querySelectorAll('.un').forEach(b=>b.onclick=()=>setUN(b.dataset.un));
build(); buildOvUI(); bindReset();
document.getElementById('cvdtagbtn').onclick=()=>{{ CVDTAGS=!CVDTAGS;
  localStorage.setItem('cvdtags',CVDTAGS?'1':'0'); applyCvdTags(); }};
document.querySelectorAll('.un').forEach(b=>b.classList.toggle('on',b.dataset.un===UN));
setIV(IV);
setInterval(load, {CANDLE_POLL}*1000);
setInterval(loadCVD, {CVD_TTL}*1000);
setInterval(loadFlow, {FLOW_TTL}*1000);
</script></body></html>"""


async def handle_charts(request):
    return web.Response(text=render_charts(), content_type="text/html")


def _mask(s):
    s = s or ""
    return {"set": bool(s), "tail": s[-4:] if s else ""}


async def handle_alerts_config(request):
    """秘密は実値を返さない(set/末尾4のみ)。ルールはそのまま返す。"""
    ch = ALERT_CFG.get("channels", {})
    tg = ch.get("telegram", {})
    return web.json_response({
        "channels": {"discord": _mask(ch.get("discord", "")),
                     "telegram": {"token": _mask(tg.get("token", "")),
                                  "chat_id": tg.get("chat_id", "")}},   # chat_idは秘密でないので表示
        "rules": ALERT_CFG.get("rules", []),
        "pin_required": bool(ALERT_PIN),
        "exchanges": [l for l, _ in SPOT_EXCH],
    })


async def handle_alerts_save(request):
    if ALERT_PIN and request.headers.get("X-Pin", "") != ALERT_PIN:
        return web.json_response({"error": "pin"}, status=403)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    cur = ALERT_CFG
    inc = d.get("channels", {}) or {}
    itg = inc.get("telegram", {}) or {}
    # 秘密は空なら既存維持(消さない)。空でなければ更新。
    discord_url = inc.get("discord", "")
    discord_url = discord_url if discord_url else cur["channels"].get("discord", "")
    tok = itg.get("token", "")
    tok = tok if tok else cur["channels"].get("telegram", {}).get("token", "")
    chat = itg.get("chat_id", "")
    cfg = {"channels": {"discord": discord_url,
                        "telegram": {"token": tok, "chat_id": chat}},
           "rules": d.get("rules", cur.get("rules", []))}
    save_alerts(cfg)
    # 削除/変更されたルールの状態は破棄
    ids = {r.get("id") for r in cfg["rules"]}
    for k in [k for k in ALERT_STATE if k not in ids]:
        ALERT_STATE.pop(k, None)
    return web.json_response({"ok": True})


async def handle_alerts_test(request):
    if ALERT_PIN and request.headers.get("X-Pin", "") != ALERT_PIN:
        return web.json_response({"error": "pin"}, status=403)
    await fire_alert(SESSION, "テスト通知: アラート配信は正常です。")
    return web.json_response({"ok": True})


def render_alerts():
    exch = json.dumps([l for l, _ in SPOT_EXCH])
    ivs = json.dumps(CHART_INTERVALS)
    return f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>アラート設定</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:16px;font-size:13px}}
h1{{font-size:18px;margin:0 0 10px}} a{{color:#4ea1ff;text-decoration:none}}
h2{{font-size:14px;margin:18px 0 8px;color:#8b949e}}
.card{{background:#10151c;border:1px solid #232a34;border-radius:10px;padding:12px;margin-bottom:14px;max-width:880px}}
input,select{{background:#0b0f14;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 7px;font-size:12px;margin:2px 4px 2px 0}}
input[type=number]{{width:90px}} .url{{width:420px;max-width:90%}}
button{{cursor:pointer;background:#1f6feb;color:#fff;border:0;border-radius:7px;padding:6px 14px;font-size:13px;margin-right:8px}}
button.sec{{background:#21262d;color:#cbd5e1;border:1px solid #30363d}}
.rule{{border:1px solid #232a34;border-radius:8px;padding:8px;margin:8px 0;background:#0d1218}}
.muted{{color:#6e7681;font-size:11px}} label{{margin-right:8px}}
table{{border-collapse:collapse}} .del{{background:#5a2230;color:#ffb3b3;border:1px solid #30363d;padding:3px 8px;border-radius:6px}}
.tag{{font-size:11px;color:#8b949e;margin-left:6px}}
</style></head><body>
<h1>🔔 チャートアラート設定　<a href="/charts">← チャートへ</a>　<a href="/">台帳へ</a></h1>

<div class=card>
<h2>配信先</h2>
<div>Discord Webhook URL: <input id=discord class=url placeholder="https://discord.com/api/webhooks/..."> <span id=discord_s class=tag></span></div>
<div style="margin-top:6px">Telegram Bot Token: <input id=tg_token class=url placeholder="123456:ABC-..."> <span id=tg_token_s class=tag></span></div>
<div style="margin-top:6px">Telegram chat_id: <input id=tg_chat placeholder="123456789"></div>
<div class=muted style="margin-top:6px">Telegram: @BotFather で /newbot → token取得。chat_idは @userinfobot にメッセージか、Botに話しかけて api.telegram.org/bot&lt;token&gt;/getUpdates の chat.id。秘密は保存後マスク表示(末尾4桁)・空欄保存で既存維持。</div>
<div id=pinrow style="margin-top:6px;display:none">PIN: <input id=pin type=password placeholder="保存に必要"> <span class=muted>ALERT_PIN設定時のみ</span></div>
</div>

<div class=card>
<h2>アラート条件 <button class=sec onclick="addRule()">+ ルール追加</button></h2>
<div id=rules></div>
</div>

<div>
<button onclick="save()">💾 保存</button>
<button class=sec onclick="test()">テスト送信</button>
<span id=msg class=tag></span>
</div>
<div class=muted style="margin-top:10px;max-width:880px">⚠ このページは認証なしHTTP。秘密(webhook/token)はサーバ(VM ~/hl/alerts.json)にのみ保存しブラウザへ実値は返しません。24h常駐daemonがサーバ側で判定し発火します。発火後は cooldown_min 沈黙＋条件が外れるまで再発火しません。</div>

<script>
const EXCH={exch}, IVS={ivs};
let RULES=[], NEXTID=1;
function opt(arr,sel){{return arr.map(v=>'<option'+(v==sel?' selected':'')+'>'+v+'</option>').join('');}}
function ruleHTML(r){{
  const tf='足:<select data-f=tf>'+opt(IVS,r.tf)+'</select>';
  const en='<label><input type=checkbox data-f=enabled'+(r.enabled?' checked':'')+'> 有効</label>';
  const cd='cooldown分:<input type=number data-f=cooldown_min value="'+(r.cooldown_min||15)+'">';
  const ty='種別:<select data-f=type>'+opt(['price','bb','cvd','cvd_div'],r.type)+'</select>';
  let f='';
  if(r.type==='price') f='水準:<input type=number data-f=level value="'+(r.level||0)+'"> 方向:<select data-f=dir>'+opt(['both','up','down'],r.dir||'both')+'</select>';
  else if(r.type==='bb') f='期間(EMA):<input type=number data-f=period value="'+(r.period||9)+'"> σ:<select data-f=sigma>'+opt(['1','2','3'],String(r.sigma||2))+'</select> バンド:<select data-f=band>'+opt(['both','upper','lower'],r.band||'both')+'</select>';
  else if(r.type==='cvd') f='取引所:<select data-f=exchange>'+opt(EXCH,r.exchange||'Binance')+'</select> 単位:<select data-f=unit>'+opt(['coin','usd'],r.unit||'coin')+'</select> <select data-f=op>'+opt(['>','<'],r.op||'>')+'</select> 閾値:<input type=number data-f=value value="'+(r.value||0)+'">';
  else if(r.type==='cvd_div') f='取引所:<select data-f=exchange>'+opt(EXCH,r.exchange||'Binance')+'</select> 単位:<select data-f=unit>'+opt(['coin','usd'],r.unit||'coin')+'</select> 本数:<input type=number data-f=bars value="'+(r.bars||12)+'">';
  return '<div class=rule data-id="'+r.id+'">'+en+' '+ty+' '+tf+' '+f+' '+cd+' メモ:<input data-f=note value="'+(r.note||'').replace(/"/g,'&quot;')+'"> <button class=del onclick="delRule('+r.id+')">削除</button></div>';
}}
function readRules(){{
  const out=[];
  document.querySelectorAll('#rules .rule').forEach(el=>{{
    const r={{id:+el.dataset.id}};
    el.querySelectorAll('[data-f]').forEach(inp=>{{ const f=inp.dataset.f;
      if(inp.type==='checkbox') r[f]=inp.checked;
      else if(inp.type==='number') r[f]=parseFloat(inp.value);
      else r[f]=inp.value; }});
    out.push(r);
  }});
  return out;
}}
function paint(){{ document.getElementById('rules').innerHTML=RULES.map(ruleHTML).join('');
  document.querySelectorAll('#rules [data-f=type]').forEach(s=>s.addEventListener('change',()=>{{
    RULES=readRules(); paint(); }})); }}
function addRule(){{ RULES=readRules(); RULES.push({{id:NEXTID++,type:'price',tf:'5m',enabled:true,dir:'both',cooldown_min:15,level:0}}); paint(); }}
function delRule(id){{ RULES=readRules().filter(r=>r.id!==id); paint(); }}
async function load(){{
  const j=await (await fetch('/alerts/config')).json();
  document.getElementById('pinrow').style.display=j.pin_required?'block':'none';
  const ds=j.channels.discord, ts=j.channels.telegram.token;
  document.getElementById('discord_s').textContent=ds.set?('設定済 …'+ds.tail):'未設定';
  document.getElementById('tg_token_s').textContent=ts.set?('設定済 …'+ts.tail):'未設定';
  document.getElementById('tg_chat').value=j.channels.telegram.chat_id||'';
  RULES=j.rules||[]; NEXTID=Math.max(0,...RULES.map(r=>r.id||0))+1; paint();
}}
function payload(){{
  const p={{channels:{{discord:document.getElementById('discord').value,
    telegram:{{token:document.getElementById('tg_token').value,chat_id:document.getElementById('tg_chat').value}}}},
    rules:readRules()}};
  return p;
}}
function hdr(){{ const h={{'Content-Type':'application/json'}}; const pin=document.getElementById('pin');
  if(pin&&pin.value) h['X-Pin']=pin.value; return h; }}
async function save(){{
  const r=await fetch('/alerts/save',{{method:'POST',headers:hdr(),body:JSON.stringify(payload())}});
  const m=document.getElementById('msg'); m.textContent=r.ok?'保存しました':'保存失敗('+r.status+')';
  if(r.ok){{ document.getElementById('discord').value=''; document.getElementById('tg_token').value=''; load(); }}
}}
async function test(){{
  const r=await fetch('/alerts/test',{{method:'POST',headers:hdr()}});
  document.getElementById('msg').textContent=r.ok?'テスト送信しました(Discord/Telegram確認)':'送信失敗('+r.status+')';
}}
load();
</script></body></html>"""


async def handle_alerts_page(request):
    return web.Response(text=render_alerts(), content_type="text/html")


async def main():
    global WATCH, POLL_SEM, UF_SEM, FLOW_SEM, SESSION
    WATCH = load_watch()
    load_dexs()          # 学習済dexを復元→再起動直後でも建玉あり/なしを即断定
    load_alerts()        # アラート設定(VM ~/hl/alerts.json)を復元
    load_flow()          # BTCフロー(VM ~/hl/flow.json)を復元→再起動で消えない
    POLL_SEM = asyncio.Semaphore(POLL_CONC)   # clearinghouse同時inflight上限
    UF_SEM = asyncio.Semaphore(UF_CONC)       # userFills直列化
    FLOW_SEM = asyncio.Semaphore(1)           # flow専用userFills直列化(pollと分離)
    await asyncio.sleep(3)                    # 再起動オーバーラップ回避(旧プロセスがHLを叩き終えるのを待つ)
    async with aiohttp.ClientSession() as session:
        SESSION = session                     # /candles ハンドラ用
        app = web.Application()
        app.router.add_get("/", handle_index)
        app.router.add_get("/health", handle_health)
        app.router.add_get("/candles", handle_candles)
        app.router.add_get("/charts", handle_charts)
        app.router.add_get("/cvd", handle_cvd)
        app.router.add_get("/flow", handle_flow)
        app.router.add_get("/alerts", handle_alerts_page)
        app.router.add_get("/alerts/config", handle_alerts_config)
        app.router.add_post("/alerts/save", handle_alerts_save)
        app.router.add_post("/alerts/test", handle_alerts_test)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        print(f"started: watch={len(WATCH)} port={PORT}")

        async def _supervise(name, fn):   # #5: 1つのloopが例外死してもdaemon全体を落とさず個別に再起動(flow_loopのバックフィル区間は無防備だった)
            while True:
                try:
                    await fn(session)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[supervise] {name} died: {e!r} → 30s後に再起動")
                    STATE[name + "_deaths"] = STATE.get(name + "_deaths", 0) + 1
                    await asyncio.sleep(30)
        await asyncio.gather(
            _supervise("ws", ws_loop), _supervise("poll", poll_loop), _supervise("candle", candle_loop),
            _supervise("cvd", cvd_loop), _supervise("alert", alert_loop), _supervise("flow", flow_loop))


if __name__ == "__main__":
    asyncio.run(main())
