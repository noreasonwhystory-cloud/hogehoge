"""Hyperliquid 公開 API ラッパ（認証不要）。レート制限＋指数バックオフ付き。"""
import time
import json
import os
import requests

import config

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


def _post_info(payload):
    """POST /info を叩く。429/5xx は指数バックオフでリトライ。"""
    for attempt in range(config.MAX_RETRIES):
        try:
            r = _session.post(config.HL_INFO, data=json.dumps(payload), timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                wait = (2 ** attempt) * 0.5
                time.sleep(wait)
                continue
            r.raise_for_status()
            time.sleep(config.HL_SLEEP)
            return r.json()
        except requests.RequestException as e:
            if attempt == config.MAX_RETRIES - 1:
                raise
            time.sleep((2 ** attempt) * 0.5)
    return None


def download_leaderboard(cache_path=None, max_age_h=12):
    """リーダーボード(~32MB)を取得。キャッシュがあれば再利用。"""
    cache_path = cache_path or os.path.join(config.DATA_DIR, "leaderboard.json")
    if os.path.exists(cache_path):
        age_h = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_h < max_age_h:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
    r = _session.get(config.HL_LEADERBOARD, timeout=120)
    r.raise_for_status()
    data = r.json()
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def user_fills_by_time(address, start_ms, end_ms):
    """期間内の約定を取得。HL は 1 リクエスト最大 2000 件・時系列でページング。"""
    out = []
    cur = start_ms
    while True:
        chunk = _post_info({
            "type": "userFillsByTime",
            "user": address,
            "startTime": cur,
            "endTime": end_ms,
        })
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 2000:
            break
        # 最後の fill の時刻+1ms から続行（重複は呼び出し側で tid 重複除去）
        last_t = chunk[-1]["time"]
        if last_t <= cur:
            break
        cur = last_t + 1
    # tid で重複除去
    seen, dedup = set(), []
    for f in out:
        if f.get("tid") in seen:
            continue
        seen.add(f.get("tid"))
        dedup.append(f)
    return dedup


def clearinghouse_state(address):
    """現在の建玉。"""
    return _post_info({"type": "clearinghouseState", "user": address})


def candles(coin, interval, start_ms, end_ms):
    """価格足。タイミング/イベント検出用。"""
    return _post_info({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval,
                "startTime": start_ms, "endTime": end_ms},
    })
