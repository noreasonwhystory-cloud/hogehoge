"""HL約定(userFillsByTime)の永続キャッシュ＋増分取得。

各検出器が毎回HLから全約定を取り直していた無駄を解消する共有ローダ。
 - data/fills/{address}.json に約定を保存。
 - 2回目以降は『前回取得した最終時刻より後』だけを取得して追記（増分）。
 - 一度貯めればHL側が古いfillを間引いても、こちらは永久保持＝間引き対策にもなる。

使い方:
  import hl_fills_cache as fc
  fills = fc.get_fills(addr)            # キャッシュ優先＋差分更新
  fills = fc.get_fills(addr, refresh=False)  # キャッシュのみ(取得しない)
"""
import os
import json
import time

import config
import hl_client

CACHE_DIR = os.path.join(config.DATA_DIR, "fills")
PAGE = 2000


def _path(addr):
    return os.path.join(CACHE_DIR, f"{addr.lower()}.json")


def _load(addr):
    p = _path(addr)
    if not os.path.exists(p):
        return [], 0
    try:
        o = json.load(open(p, encoding="utf-8"))
        return o.get("fills", []), int(o.get("last_time", 0) or 0)
    except Exception:
        return [], 0


def _fetch(addr, start, max_pages):
    out, cur, now = [], start, int(time.time() * 1000)
    for _ in range(max_pages):
        ch = hl_client._post_info({"type": "userFillsByTime", "user": addr,
                                   "startTime": cur, "endTime": now})
        if not ch:
            break
        out.extend(ch)
        if len(ch) < PAGE:
            break
        last = ch[-1]["time"]
        if last <= cur:
            break
        cur = last + 1
    return out


def _recent(addr):
    """userFills(最新最大2000件)。高頻度でページング上限に達しても最新を取りこぼさぬ保険。"""
    try:
        return hl_client._post_info({"type": "userFills", "user": addr}) or []
    except Exception:
        return []


def get_fills(addr, max_pages=40, refresh=True):
    """キャッシュを読み、refresh時は前回最終時刻以降を増分取得＋最新userFillsをマージ。dedupe済の全約定を返す。"""
    addr = addr.lower()
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached, last = _load(addr)
    new = []
    if refresh or not cached:
        start = (last + 1) if cached else 0
        new = _fetch(addr, start, max_pages)
        new += _recent(addr)        # 最新2000を必ず合流(高頻度の取りこぼし＝古い窓固定を防ぐ)
    if not new:
        return cached
    seen, merged = set(), []
    for f in cached + new:
        tid = f.get("tid")
        if tid in seen:
            continue
        seen.add(tid); merged.append(f)
    merged.sort(key=lambda f: int(f["time"]))
    lt = max((int(f["time"]) for f in merged), default=last)
    json.dump({"address": addr, "fetched_at": int(time.time() * 1000),
               "last_time": lt, "n": len(merged), "fills": merged},
              open(_path(addr), "w", encoding="utf-8"), ensure_ascii=False)
    return merged


def stats():
    if not os.path.isdir(CACHE_DIR):
        return {"wallets": 0, "fills": 0}
    n = fills = 0
    for fn in os.listdir(CACHE_DIR):
        if fn.endswith(".json"):
            n += 1
            try:
                fills += json.load(open(os.path.join(CACHE_DIR, fn), encoding="utf-8")).get("n", 0)
            except Exception:
                pass
    return {"wallets": n, "fills": fills}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        fl = get_fills(sys.argv[1])
        print(f"{sys.argv[1]}: {len(fl)} fills cached")
    print("cache stats:", stats())
