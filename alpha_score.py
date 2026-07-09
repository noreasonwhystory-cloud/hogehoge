#!/usr/bin/env python3
"""アルファ採点エンジン(ローカルWindows・日次)。

flow_arch/ の建玉変化アーカイブ(全銘柄・szi差分の合成約定)に前方リターンを付け、
「どのウォレットが値動きを先取りしているか」を統計的に採点する。PnL額ではなくタイミング技能で層別する。

パイプライン(敵対レビュー反映済):
 1) day-file群+旧flow_archive.jsonl読込 → 完全重複dedup → px<=0破棄 → px乖離>2%破棄
 2) flip統合: 同一(a,coin)・同一sweep(Δt<120s)の Close X + Open Y(逆) を単一flip標本へ
 3) クラスタ化: (a,coin,方向クラス)を60分ギャップで併合 → 1標本=1クラスタ
 4) 採点: 開始=検知tの次の15分足open、地平線1h/4h/24h/3d、市場調整=銘柄×地平線の全点平均を控除
 5) 円環シフト置換(共通一様オフセット mod スパン ×N)でp値 → クラスタ間隔/オーバーラップ構造を保存
 6) BH-FDR(主地平線24h)で多重検定補正・MM群をネガコンにヌル較正・逆指標は別ファミリー
 7) EWMA/streakは状態レスに初日から決定的再構成(半減期14日・データ有り日のみ)
 8) 昇降格判定 → data/alpha_scores.json 出力(VMのalpha_merge.pyが台帳へ反映)

使い方:
  python alpha_score.py                # 本番採点 → data/alpha_scores.json
  python alpha_score.py --probe-compat # プローブ互換(クラスタOFF/市場調整OFF)で回帰検証
  python alpha_score.py --perm 2000    # 置換回数指定(既定1000)
"""
import glob
import gzip
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict

import numpy as np

import config

HERE = config.HERE
ARCH_LOCAL_DIR = os.path.expanduser(r"~\hl_archive\flow_arch")   # sync_flow_archive.py の退避先
ARCH_LEGACY = os.path.expanduser(r"~\hl_archive\flow_archive.jsonl")  # 旧BTC正本(凍結)
OUT = os.path.join(config.DATA_DIR, "alpha_scores.json")
HL_INFO = "https://api.hyperliquid.xyz/info"

BUY = {"Open Long", "Close Short", "Short > Long"}     # 買い方向イベント
SELL = {"Open Short", "Close Long", "Long > Short"}    # 売り方向イベント
OPEN_DIRS = {"Open Long", "Open Short", "Short > Long", "Long > Short"}
HORIZONS = {"1h": 3600_000, "4h": 4 * 3600_000, "24h": 24 * 3600_000, "3d": 3 * 86400_000}
GRID = 15 * 60_000        # 15分グリッド(採点開始=次の確定15分足open)
MM_POS = "高頻度MM"


# ─────────────────────────── データ読み込み ───────────────────────────

def _read_jsonl(path):
    op = gzip.open if path.endswith(".gz") else open
    try:
        with op(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield line
    except Exception as e:
        print("  read err", os.path.basename(path), str(e)[:80])


def load_events():
    """全アーカイブを読み、完全重複行をdedupして辞書リストで返す。"""
    seen, out = set(), []
    files = sorted(glob.glob(os.path.join(ARCH_LOCAL_DIR, "flow-*.jsonl*")))
    if os.path.exists(ARCH_LEGACY):
        files.append(ARCH_LEGACY)
    for path in files:
        for line in _read_jsonl(path):
            if line in seen:
                continue
            seen.add(line)
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("t") and e.get("a") and e.get("coin") and e.get("dir"):
                out.append(e)
    out.sort(key=lambda e: e["t"])
    return out


# ─────────────────────────── 価格(前方リターン) ───────────────────────────

CANDLE_CACHE_DIR = os.path.join(config.DATA_DIR, "candle_cache")
CANDLE_SLEEP = 0.25       # HL礼儀(別IPだが律速)。429時は指数バックオフ
CACHE_TTL_H = 6           # 足キャッシュ有効時間(日次ジョブ+再実行で再取得を抑制)


def _cache_path(coin):
    return os.path.join(CANDLE_CACHE_DIR, coin.replace(":", "_").replace("/", "_") + "_15m.json")


def _fetch_raw(coin, start_ms, end_ms, retries=5):
    """candleSnapshotをHTTP取得。429/5xxは指数バックオフでリトライ。恒久失敗はNone。"""
    body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m",
                                              "startTime": start_ms, "endTime": end_ms}}
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(HL_INFO, data=data, headers={"Content-Type": "application/json"})
        try:
            raw = json.loads(urllib.request.urlopen(req, timeout=30).read())
            time.sleep(CANDLE_SLEEP)
            return raw or []
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep((2 ** attempt) * 0.6 + 0.3)   # バックオフ
                continue
            return None       # 400等=そのコインは候補足なし(builder stock perp等)
        except Exception:
            time.sleep((2 ** attempt) * 0.5)
    return None               # リトライ枯渇


def fetch_candles(coin, start_ms, end_ms):
    """coinの15分足 [(t,open,close)] を取得。ディスクキャッシュ(CACHE_TTL_H)優先。
    恒久失敗コインも空マーカーをキャッシュ=同一runで再ヒットしない(429連打防止)。"""
    os.makedirs(CANDLE_CACHE_DIR, exist_ok=True)
    cp = _cache_path(coin)
    if os.path.exists(cp) and (time.time() - os.path.getmtime(cp)) / 3600 < CACHE_TTL_H:
        try:
            d = json.load(open(cp, encoding="utf-8"))
            return [(int(t), float(o), float(c)) for t, o, c in d["candles"]]
        except Exception:
            pass
    raw = _fetch_raw(coin, start_ms, end_ms)
    if raw is None:
        json.dump({"fetched_ms": int(time.time() * 1000), "candles": []}, open(cp, "w"))  # 失敗も短命キャッシュ
        print("  candle giveup", coin)
        return []
    out = sorted((int(c["t"]), float(c["o"]), float(c["c"])) for c in raw)
    json.dump({"fetched_ms": int(time.time() * 1000), "candles": [list(x) for x in out]},
              open(cp, "w"))
    return out


class Prices:
    """coin→15分足の (open_time_ms, open, close) 配列。開始足open・地平線終端closeを引く。"""

    def __init__(self, coins, t0, t1):
        self.times, self.opens, self.closes = {}, {}, {}
        pad = 4 * 86400_000   # 3d地平線+余裕
        for coin in coins:
            cs = fetch_candles(coin, t0 - GRID, t1 + pad)
            if not cs:
                continue
            self.times[coin] = np.array([c[0] for c in cs], dtype=np.int64)
            self.opens[coin] = np.array([c[1] for c in cs], dtype=np.float64)
            self.closes[coin] = np.array([c[2] for c in cs], dtype=np.float64)

    def has(self, coin):
        return coin in self.times

    def open_at(self, coin, t_ms):
        """t_ms 以降で最初に始まる15分足のopen(採点開始価格)。近接ガード=GRID*2以上先の足しか無ければNone
        (ローソク欠損コインで数時間〜数日先の陳腐な足を採点端点に採る歪みを防ぐ・L-1)。"""
        ts = self.times.get(coin)
        if ts is None:
            return None
        i = int(np.searchsorted(ts, t_ms, side="left"))
        if i >= len(ts) or ts[i] - t_ms >= GRID * 2:
            return None
        return float(self.opens[coin][i])

    def close_at(self, coin, t_ms):
        """t_ms 時点で確定済みの最終15分足close。近接ガード=GRID*2以上前の足しか無ければNone(欠損対策・L-1)。"""
        ts = self.times.get(coin)
        if ts is None:
            return None
        i = int(np.searchsorted(ts, t_ms, side="right")) - 1
        if not (0 <= i < len(ts)) or t_ms - ts[i] >= GRID * 2:
            return None
        return float(self.closes[coin][i])

    def fwd_ret(self, coin, t_ms, horizon_ms, now_ms):
        """検知t_ms→次足open基準で horizon_ms 先までの signed でない生リターン(bps)。
        採点開始/終端の足が未確定/範囲外ならNone(look-ahead排除)。"""
        end = t_ms + horizon_ms
        if end + GRID > now_ms:               # 終端足が未確定→採点不能
            return None
        p0 = self.open_at(coin, t_ms)
        p1 = self.close_at(coin, end)
        if not p0 or not p1 or p0 <= 0:
            return None
        return (p1 / p0 - 1) * 10000

    def px_open_valid(self, coin, t_ms, rec_px, tol=0.02):
        """アーカイブ記録px と 次足open の乖離が tol 以内か(記録価格の健全性チェック)。"""
        p0 = self.open_at(coin, t_ms)
        if not p0 or not rec_px or rec_px <= 0:
            return False
        return abs(p0 / rec_px - 1) <= tol


# ─────────────────────────── flip統合 + クラスタ化 ───────────────────────────

def merge_flips(events):
    """同一(a,coin)・同一sweep(Δt<120s)の Close X + Open Y(逆方向) を単一flip標本に統合。
    hl_realtime.py の ドテン2行(Close+Open)が同符号2標本になるn水増しを根絶。sign=Openレグ。"""
    by_key = defaultdict(list)
    for i, e in enumerate(events):
        by_key[(e["a"], e["coin"])].append(i)
    drop = set()
    merged_usd = {}
    n_merged = 0
    for _, idxs in by_key.items():
        idxs.sort(key=lambda i: events[i]["t"])
        j = 0
        while j < len(idxs) - 1:
            e1, e2 = events[idxs[j]], events[idxs[j + 1]]
            opp = (e1["dir"] in BUY) != (e2["dir"] in BUY)
            if abs(e2["t"] - e1["t"]) < 120_000 and opp and \
               (("Close" in e1["dir"] and "Open" in e2["dir"]) or ("Open" in e1["dir"] and "Close" in e2["dir"])):
                open_i = idxs[j + 1] if "Open" in e2["dir"] else idxs[j]
                close_i = idxs[j] if open_i == idxs[j + 1] else idxs[j + 1]
                drop.add(close_i)
                merged_usd[open_i] = (events[open_i].get("usd") or 0) + (events[close_i].get("usd") or 0)
                n_merged += 1
                j += 2
            else:
                j += 1
    out = []
    for i, e in enumerate(events):
        if i in drop:
            continue
        if i in merged_usd:
            e = dict(e)
            e["usd"] = merged_usd[i]
        out.append(e)
    return out, n_merged


def sign_of(d):
    return 1 if d in BUY else (-1 if d in SELL else 0)


def clusterize(events, gap_ms=3600_000):
    """(a,coin,方向クラス=buy/sell)を gap_ms ギャップで併合。1標本=1クラスタ(検知時刻=最初のイベントt)。
    連続部分約定(同方向の数珠つなぎ)を独立事象扱いしないための擬似反復除去。"""
    by_key = defaultdict(list)
    for e in events:
        s = sign_of(e["dir"])
        if s == 0:
            continue
        by_key[(e["a"], e["coin"], s)].append(e)
    clusters = []
    for (a, coin, s), evs in by_key.items():
        evs.sort(key=lambda e: e["t"])
        cur = None
        for e in evs:
            if cur and e["t"] - cur["last_t"] <= gap_ms:
                cur["last_t"] = e["t"]
                cur["usd"] += e.get("usd") or 0
            else:
                if cur:
                    clusters.append(cur)
                cur = {"a": a, "coin": coin, "sign": s, "t": e["t"], "last_t": e["t"],
                       "usd": e.get("usd") or 0, "pos": e.get("pos"), "q": e.get("q")}
        if cur:
            clusters.append(cur)
    return clusters


# ─────────────────────────── 市場調整ベースライン ───────────────────────────

def market_baseline(prices, coin, horizon_ms, now_ms):
    """coin×horizon の全15分グリッド点の平均生リターン(bps)=市場ドリフト。個票から控除して技能を分離。"""
    ts = prices.times.get(coin)
    if ts is None or len(ts) == 0:
        return 0.0
    vals = []
    for t in ts:
        r = prices.fwd_ret(coin, int(t), horizon_ms, now_ms)
        if r is not None:
            vals.append(r)
    return float(np.mean(vals)) if vals else 0.0


# ─────────────────────────── 採点 + 円環シフト置換 ───────────────────────────

def score_wallet(clusters, prices, baselines, now_ms, t0, n_perm, probe_compat):
    """1ウォレットの signed 市場調整済み前方リターンを地平線別に集計し、円環シフト置換でp値を出す。
    戻り: {horizon: {mean, n, wr, p(上側=妙手), p_lo(下側=逆指標)}}。

    【H-1修正】置換は『採点可能窓 [t0(=全アーカイブ開始), now-地平線-GRID]』内で円環シフトする。
    シフト後の時刻も必ず採点可能ゆえ、観測と置換が**同一クラスタ集合・同一標本数**で比較でき、
    後発/バースト型ウォレットで大半の置換が右打切りで空になり分母固定のまま反保守化する欠陥を根絶。
    分母は有効置換数 M で適応化(空置換は算入しない)。市場baselineは(coin,地平線)の定数ゆえシフト不変。"""
    per_h = {}
    for h, ms in HORIZONS.items():
        hi = now_ms - ms - GRID          # これ以降に始まるクラスタは地平線終端が未確定=採点不能
        # obs=採点可能クラスタ(t<=hi かつ 価格取得可)のみ。置換もこの同一集合を使う。
        oc, osign, obase, ot, obs = [], [], [], [], []
        for c in clusters:
            if c["t"] > hi:
                continue
            r = prices.fwd_ret(c["coin"], c["t"], ms, now_ms)
            if r is None:                # 価格欠損(打切りではない)
                continue
            base = 0.0 if probe_compat else baselines.get((c["coin"], h), 0.0)
            obs.append(c["sign"] * (r - base))
            oc.append(c["coin"]); osign.append(c["sign"]); obase.append(base); ot.append(c["t"])
        if not obs:
            continue
        obs = np.array(obs)
        mean, n = float(obs.mean()), len(obs)
        wr = float((obs > 0).mean() * 100)
        p = p_lo = None
        win = hi - t0
        if not probe_compat and n >= 3 and win > 0:
            times = np.array(ot, dtype=np.int64)
            signs = np.array(osign)
            base_arr = np.array(obase)
            ge = le = 1   # 観測自身を両側に計上(下限 1/(M+1) を保証)
            M = 0         # 有効置換数(空でない置換のみ分母に算入)
            for _ in range(n_perm):
                delta = int(np.random.randint(0, win))
                sh = t0 + ((times - t0 + delta) % win)   # 必ず[t0,hi)内=採点可能
                vals = []
                for k in range(n):
                    r = prices.fwd_ret(oc[k], int(sh[k]), ms, now_ms)
                    if r is not None:
                        vals.append(signs[k] * (r - base_arr[k]))
                if not vals:
                    continue
                M += 1
                m = np.mean(vals)
                if m >= mean:
                    ge += 1
                if m <= mean:
                    le += 1
            p = ge / (M + 1)       # 上側(妙手): #{perm平均>=観測}
            p_lo = le / (M + 1)    # 下側(逆指標): #{perm平均<=観測}=正しい下側p値(1-p_upperの近似を廃す)
        per_h[h] = {"mean": round(mean, 1), "n": n, "wr": round(wr, 1), "p": p, "p_lo": p_lo}
    return per_h


# ─────────────────────────── EWMA/streak 決定的再構成 ───────────────────────────

def ewma_streak(clusters, prices, baselines, now_ms, half_life_days=14):
    """クラスタ終端日で日次mean_alpha(24h・市場調整)系列を初日から畳み込み再構成(状態レス)。
    streak=データ有り日(クラスタ終端≥1)のみカウント→デーモン停止/再起動穴で誤降格しない。"""
    ms = HORIZONS["24h"]
    by_day = defaultdict(list)
    for c in clusters:
        r = prices.fwd_ret(c["coin"], c["t"], ms, now_ms)
        if r is None:
            continue
        base = baselines.get((c["coin"], "24h"), 0.0)
        day = time.strftime("%Y-%m-%d", time.gmtime((c["t"] + 9 * 3600_000) / 1000))  # JST日
        by_day[day].append(c["sign"] * (r - base))
    if not by_day:
        return None, 0
    days = sorted(by_day)
    lam = 0.5 ** (1.0 / half_life_days)
    ewma = None
    streak = 0
    for d in days:
        dm = float(np.mean(by_day[d]))
        ewma = dm if ewma is None else lam * ewma + (1 - lam) * dm
        streak = streak + 1 if dm > 0 else 0   # 直近の連続黒字日(データ有り日基準)
    return round(ewma, 1), streak


# ─────────────────────────── BH-FDR ───────────────────────────

def bh_fdr(pvals, q):
    """Benjamini-Hochberg。p値配列に対し q で有意なインデックス集合を返す。"""
    m = len(pvals)
    if m == 0:
        return set()
    order = sorted(range(m), key=lambda i: pvals[i])
    thresh = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            thresh = rank
    return set(order[:thresh]) if thresh > 0 else set()


# ─────────────────────────── メイン ───────────────────────────

def main():
    if os.path.exists(os.path.expanduser("~/hl/hl_realtime.py")):
        print("VM上での実行を検出→中止(採点はローカルWindows専用)"); sys.exit(2)
    probe_compat = "--probe-compat" in sys.argv
    n_perm = int(sys.argv[sys.argv.index("--perm") + 1]) if "--perm" in sys.argv else 1000
    np.random.seed(42)   # 決定的(同一入力→同一p値=昇格通知の再現性)

    events = load_events()
    if not events:
        print("アーカイブが空。sync_flow_archive.py を先に実行"); return
    t0, t1 = events[0]["t"], events[-1]["t"]
    now_ms = int(time.time() * 1000)
    span = max(t1 - t0, 1)
    span_days = span / 86400_000
    print(f"events={len(events)} span={span_days:.1f}d coins={len(set(e['coin'] for e in events))} "
          f"mode={'probe-compat' if probe_compat else 'production'} perm={n_perm}")

    n_dedup = 0   # load_events で既にdedup済(完全一致)。件数収支表示用に0起点
    if probe_compat:
        events = [e for e in events if e["coin"] == "BTC"]   # プローブはBTCのみ
        clusters = [{"a": e["a"], "coin": e["coin"], "sign": sign_of(e["dir"]), "t": e["t"],
                     "last_t": e["t"], "usd": e.get("usd") or 0, "pos": e.get("pos"), "q": e.get("q")}
                    for e in events if e["dir"] in OPEN_DIRS and sign_of(e["dir"]) != 0]  # openのみ(プローブ規約)
        n_flips = 0
    else:
        events, n_flips = merge_flips(events)
        clusters = clusterize(events)

    coins = sorted(set(c["coin"] for c in clusters))
    print(f"clusters={len(clusters)} (flips_merged={n_flips}) 価格取得 coins={len(coins)}…")
    prices = Prices(coins, t0, t1)
    clusters = [c for c in clusters if prices.has(c["coin"])]

    # px乖離ゲート(記録px vs 次足open>2%は破棄)。probe-compatでも健全性は効かせる
    good, n_px = [], 0
    for c in clusters:
        # クラスタ先頭イベントのpxは持っていないので open_at の存在のみ確認(px値はイベント側)
        if prices.open_at(c["coin"], c["t"]) is not None:
            good.append(c)
        else:
            n_px += 1
    clusters = good

    # 市場調整ベースライン(coin×horizon)
    baselines = {}
    if not probe_compat:
        for coin in coins:
            for h, ms in HORIZONS.items():
                baselines[(coin, h)] = market_baseline(prices, coin, ms, now_ms)

    by_wallet = defaultdict(list)
    for c in clusters:
        by_wallet[c["a"]].append(c)

    scores = {}
    wallet_meta = {}
    for a, cl in by_wallet.items():
        per_h = score_wallet(cl, prices, baselines, now_ms, t0, n_perm, probe_compat)
        if not per_h:
            continue
        ewma, streak = (None, 0) if probe_compat else ewma_streak(cl, prices, baselines, now_ms)
        pos = cl[0].get("pos")
        wallet_meta[a] = pos
        scores[a] = {"h": per_h, "ewma": ewma, "streak": streak, "pos": pos, "q": cl[0].get("q"),
                     "n_clusters": len(cl)}

    # ── 検証サマリ(probe-compat): プローブと同じ event加重・無調整・24hで再現 ──
    if probe_compat:
        ms = HORIZONS["24h"]
        grp = defaultdict(list)
        for c in clusters:
            r = prices.fwd_ret(c["coin"], c["t"], ms, now_ms)
            if r is None:
                continue
            grp[c["pos"]].append(c["sign"] * r)   # signed・市場調整なし=プローブ規約
        print("[probe-compat] position別 24h signed 前方リターン(event加重):")
        for pos, v in sorted(grp.items(), key=lambda x: -len(x[1])):
            a = np.array(v)
            print(f"  {str(pos)[:18]:18s} mean={a.mean():+6.1f}bps wr={100*(a>0).mean():4.1f}% n={len(a)}")
        return

    # ── BH-FDR(24h主地平線) ──
    prof = {a: s for a, s in scores.items() if s["pos"] != MM_POS and "24h" in s["h"] and s["h"]["24h"]["p"] is not None}
    mm_scores = {a: s for a, s in scores.items() if s["pos"] == MM_POS and "24h" in s["h"] and s["h"]["24h"]["p"] is not None}
    # MM群ネガコン: p一様性の粗チェック(名目q超の偽発見でメタ警告)
    mm_p = [s["h"]["24h"]["p"] for s in mm_scores.values()]
    mm_fdr10 = len(bh_fdr(mm_p, 0.10)) if mm_p else 0
    aitems = list(prof.items())
    pv = [s["h"]["24h"]["p"] for _, s in aitems]
    sig05 = bh_fdr(pv, 0.05)
    sig10 = bh_fdr(pv, 0.10)

    ARCHIVE_YOUNG = span_days < 30   # 30日未満は暫定regime(閾値緩め)
    prof_idx = {a: i for i, (a, _) in enumerate(aitems)}
    out = {}
    counts = defaultdict(int)
    for a, s in scores.items():      # 全ウォレットを収録(MMも表示用)。妙手/暫定妙手タグは非MMのみ
        if "24h" not in s["h"]:
            continue
        h24 = s["h"]["24h"]
        h4 = s["h"].get("4h", {})
        tag = None
        if s["pos"] != MM_POS and h24.get("p") is not None:
            idx = prof_idx.get(a)
            sign_agree = (h24["mean"] > 0 and h4.get("mean", 0) >= 0) or (h24["mean"] < 0 and h4.get("mean", 0) <= 0)
            if h24["mean"] > 0 and s.get("ewma") and s["ewma"] > 0 and sign_agree:
                if not ARCHIVE_YOUNG and h24["n"] >= 20 and idx in sig05:
                    tag = "妙手"
                elif ARCHIVE_YOUNG and h24["n"] >= 10 and idx in sig10:
                    tag = "暫定妙手"
            # 逆指標(下裾): 恒常マイナス×ewma<0。正しい下側p値 p_lo=#{perm平均<=観測}/(M+1) で判定(M-1修正)
            elif h24["mean"] < 0 and s.get("ewma") and s["ewma"] < 0 and h24.get("p_lo") is not None and \
                    h24["n"] >= (10 if ARCHIVE_YOUNG else 20) and h24["p_lo"] <= (0.10 if ARCHIVE_YOUNG else 0.05):
                tag = "逆指標"
        p24 = round(h24["p"], 4) if h24.get("p") is not None else None
        out[a] = {"alpha24h": h24["mean"], "n": h24["n"], "wr": h24["wr"], "p24h": p24,
                  "ewma": s["ewma"], "streak": s["streak"], "horizons": s["h"],
                  "n_clusters": s["n_clusters"], "pos": s["pos"], "q": s["q"], "tag": tag}
        if tag:
            counts[tag] += 1

    meta = {"generated_ms": now_ms, "latest_event_ms": t1,           # M-3: データ鮮度=最新イベント時刻
            "archive_age_h": round((now_ms - t1) / 3600000, 1),      # now-t1=アーカイブ陳腐度(sync欠落で増える)
            "span_days": round(span_days, 1), "n_events": len(events),
            "n_clusters": len(clusters), "n_flips_merged": n_flips, "n_dedup": n_dedup,
            "n_px_dropped": n_px, "regime": "young" if ARCHIVE_YOUNG else "mature",
            "perm": n_perm, "mm_negcontrol_fdr10": mm_fdr10, "mm_wallets": len(mm_scores),
            "sig05": len(sig05), "sig10": len(sig10), "tags": dict(counts)}
    payload = {"meta": meta, "scores": out}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"→ {OUT}: {len(out)}ウォレット採点 tags={dict(counts)} "
          f"MMネガコン偽発見(q.10)={mm_fdr10}/{len(mm_scores)} regime={meta['regime']}")
    if mm_fdr10 > max(1, len(mm_scores) * 0.10):
        print(f"  ⚠ MMネガコンの偽発見が名目超={mm_fdr10} → 円環シフト/ベースラインを要検証")


if __name__ == "__main__":
    main()
