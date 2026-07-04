"""イベント検出(Phase3・S4=move-first逆引きの起点)。

config.COINS(BTC/ETH/SOL)の1h足を取得し、|1h ret|>=EVENT_1H_PCT または |4h ret|>=EVENT_4H_PCT の
急変を検出→data/events.jsonl へ追記(append・dedup)。discover_and_classify の S4 が
「このイベントの直前(EVENT_LEAD_H時間内)に正方向を建てた未登録ウォレット」を逆引きする材料。

dedupキー = coin + 方向 + t0の4h丸め(スライド窓で同一急変を別物として重複登録しない)。
alt銘柄は day窓PnLがcoin別に切れずS/N不足ゆえ v1 見送り(BTC/ETH/SOLのみ)。

使い方: python event_scan.py           # 単体でイベント追記
       discover_and_classify から try/except で縮退呼出(candles失敗時はS4スキップ・S1-S3続行)。
"""
import json
import os
import time

import config
import hl_client

EVENTS_PATH = os.path.join(config.DATA_DIR, "events.jsonl")
HOUR = 3600_000


def detect(coin, cs):
    """1h足配列(dict {t,o,h,l,c})から急変イベント[{coin,dir,t0,ret1h,ret4h}]を返す。"""
    cs = sorted(cs, key=lambda c: int(c["t"]))
    evs = []
    for i, c in enumerate(cs):
        o, cl, t = float(c["o"]), float(c["c"]), int(c["t"])
        r1 = (cl / o - 1) * 100 if o else 0.0
        r4 = None
        if i >= 3:
            o4 = float(cs[i - 3]["o"])
            r4 = (cl / o4 - 1) * 100 if o4 else 0.0
        big1 = abs(r1) >= config.EVENT_1H_PCT
        big4 = r4 is not None and abs(r4) >= config.EVENT_4H_PCT
        if big1 or big4:
            drv = r1 if big1 else r4
            evs.append({"coin": coin, "dir": "up" if drv > 0 else "down", "t0": t,
                        "ret1h": round(r1, 2), "ret4h": (round(r4, 2) if r4 is not None else None)})
    return evs


def merge_same(evs):
    """同coin同方向・4h以内に連続する急変を1イベント(最初のt0が代表)に併合。"""
    evs = sorted(evs, key=lambda e: (e["coin"], e["dir"], e["t0"]))
    out = []
    for e in evs:
        if out:
            p = out[-1]
            if p["coin"] == e["coin"] and p["dir"] == e["dir"] and e["t0"] - p["t0"] <= 4 * HOUR:
                # 併合: より大きい変化率を代表に残す
                if abs(e.get("ret4h") or e["ret1h"]) > abs(p.get("ret4h") or p["ret1h"]):
                    p["ret1h"], p["ret4h"] = e["ret1h"], e.get("ret4h")
                continue
        out.append(dict(e))
    return out


def _existing_keys():
    keys = set()
    if os.path.exists(EVENTS_PATH):
        with open(EVENTS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    keys.add((e["coin"], e["dir"], int(e["t0"]) // (4 * HOUR)))
                except Exception:
                    continue
    return keys


def scan(hours=48):
    """直近hours時間の1h足を取得しイベント検出→dedupして新規のみ返す(events.jsonlへは書かない)。"""
    now = int(time.time() * 1000)
    start = now - hours * HOUR
    found = []
    for coin in config.COINS:
        raw = hl_client.candles(coin, "1h", start, now)   # 失敗時 None/例外は呼び元で縮退
        cs = [{"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in (raw or [])]
        found.extend(detect(coin, cs))
    found = merge_same(found)
    seen = _existing_keys()
    fresh = [e for e in found if (e["coin"], e["dir"], e["t0"] // (4 * HOUR)) not in seen]
    return fresh


def append_events(evs):
    if not evs:
        return
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def recent_events(hours=None):
    """events.jsonl から直近イベントを読む(S4逆引き用)。hours指定で窓制限。"""
    out = []
    if not os.path.exists(EVENTS_PATH):
        return out
    cut = (int(time.time() * 1000) - hours * HOUR) if hours else 0
    with open(EVENTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if int(e.get("t0", 0)) >= cut:
                out.append(e)
    return out


def main():
    fresh = scan()
    append_events(fresh)
    print(f"イベント検出: 新規 {len(fresh)}件 → {EVENTS_PATH}")
    for e in fresh[:20]:
        print(f"  {e['coin']} {e['dir']} t0={e['t0']} 1h={e['ret1h']}% 4h={e['ret4h']}%")


if __name__ == "__main__":
    main()
