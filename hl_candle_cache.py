"""HL価格足(candleSnapshot)の永続キャッシュ＋欠損区間のみ取得。

各インサイダー検出器が銘柄ごとに毎回 candle を取り直す無駄を解消する共有ローダ。
ビルダー配備perp(coin名 'xyz:SPCX' 等)の candle も取得・キャッシュできる（フル接頭辞必須）。
 - data/candles/{interval}__{safe_coin}.json に candle を保存（safe_coin = coin の ':' '/' を '_' 化）。
 - 2回目以降は要求区間のうち未キャッシュの頭/尾だけ取得して追記（API再取得を最小化）。

使い方:
  import hl_candle_cache as cc
  ser = cc.get_candles('xyz:SPCX', '1h', start_ms, end_ms)   # [{'t':ms,'c':close,...}, ...] (t昇順)
  ser = cc.get_candles('BTC', '1h', start_ms, end_ms, refresh=False)  # キャッシュのみ
"""
import os
import json
import time

import config
import hl_client

CACHE_DIR = os.path.join(config.DATA_DIR, "candles")
PAGE = 5000   # HL candleSnapshot の1レスポンス上限

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                "1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000, "1d": 86_400_000}


def _ims(interval):
    return _INTERVAL_MS.get(interval, 3_600_000)


def _safe(coin):
    return coin.replace(":", "_").replace("/", "_").replace("\\", "_")


def _path(coin, interval):
    return os.path.join(CACHE_DIR, f"{interval}__{_safe(coin)}.json")


def _load(coin, interval):
    p = _path(coin, interval)
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p, encoding="utf-8")).get("candles", [])
    except Exception:
        return []


def _save(coin, interval, candles):
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump({"coin": coin, "interval": interval, "n": len(candles),
               "fetched_at": int(time.time() * 1000), "candles": candles},
              open(_path(coin, interval), "w", encoding="utf-8"), ensure_ascii=False)


def _fetch_range(coin, interval, start, end):
    """[start,end] を candleSnapshot で前方ページング取得（昇順前提・上限PAGEで継続）。"""
    ims = _ims(interval)
    out, cur = [], int(start)
    for _ in range(60):
        try:
            ch = hl_client.candles(coin, interval, cur, int(end)) or []
        except Exception:
            break
        if not ch:
            break
        out.extend(ch)
        last_t = int(ch[-1]["t"])
        if len(ch) < PAGE or last_t + ims > end:
            break
        nx = last_t + ims
        if nx <= cur:
            break
        cur = nx
    return out


def get_candles(coin, interval, start_ms, end_ms, refresh=True):
    """要求区間の candle を t昇順で返す。未キャッシュの頭/尾のみ取得して追記。"""
    start_ms, end_ms = int(start_ms), int(end_ms)
    cached = _load(coin, interval)
    cmin = cached[0]["t"] if cached else None
    cmax = cached[-1]["t"] if cached else None
    need = []
    if not cached:
        need.append((start_ms, end_ms))
    else:
        if start_ms < cmin:
            need.append((start_ms, cmin))
        if end_ms > cmax:
            need.append((cmax, end_ms))
    if refresh and need:
        new = []
        for s, e in need:
            new.extend(_fetch_range(coin, interval, s, e))
        if new:
            by_t = {int(c["t"]): c for c in cached}
            for c in new:
                by_t[int(c["t"])] = c
            cached = [by_t[t] for t in sorted(by_t)]
            _save(coin, interval, cached)
    return [c for c in cached if start_ms <= int(c["t"]) <= end_ms]


def stats():
    if not os.path.isdir(CACHE_DIR):
        return {"series": 0}
    return {"series": sum(1 for f in os.listdir(CACHE_DIR) if f.endswith(".json"))}


if __name__ == "__main__":
    import sys
    coin = sys.argv[1] if len(sys.argv) > 1 else "xyz:SPCX"
    now = int(time.time() * 1000)
    s = get_candles(coin, "1h", now - 30 * 24 * 3600_000, now)
    print(f"{coin}: {len(s)} candles (1h, 30d)  cache={stats()}")
