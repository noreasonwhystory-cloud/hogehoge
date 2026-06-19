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
    """キャッシュ＋増分。最新userFillsを必ず取り、隙間は前方で埋め『既取得と被ったら停止』。dedupe済を返す。
    HLは古い順APIしか無いため『最新を確実に＋重複なく隙間を埋める』方式で新しい側を取りこぼさない。"""
    addr = addr.lower()
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached, last = _load(addr)
    if not (refresh or not cached):
        return cached

    have = {f.get("tid") for f in cached}
    recent = _recent(addr)                       # 最新2000(新しい側を保証)
    have_recent = {f.get("tid") for f in recent}
    new = list(recent)
    # キャッシュ最終時刻の翌から前方へ隙間を埋め、最新2000と被った時点で停止(=連結完了)
    cur = (last + 1) if cached else 0
    for _ in range(max_pages):
        chunk = _fetch_page(addr, cur)
        if not chunk:
            break
        new.extend(chunk)
        if any(f.get("tid") in have_recent or f.get("tid") in have for f in chunk):
            break                                 # 既取得と被った→これ以上は重複ゆえ停止
        if len(chunk) < PAGE:
            break
        nx = chunk[-1]["time"] + 1
        if nx <= cur:
            break
        cur = nx

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


def _fetch_page(addr, start):
    now = int(time.time() * 1000)
    return hl_client._post_info({"type": "userFillsByTime", "user": addr,
                                 "startTime": start, "endTime": now}) or []


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
