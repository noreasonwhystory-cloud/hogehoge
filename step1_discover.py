"""Step1: Hyperliquid で高勝率・高実現損益のウォレットを約定レベルで発掘する。

出力: data/ranked.json （insider_score 降順のウォレット成績）
使い方:
    python step1_discover.py            # フル（候補 CANDIDATE_LIMIT 件）
    python step1_discover.py --limit 3  # 先行検証（候補3件だけ）
"""
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

import config
import hl_client

MS_H = 3600 * 1000


def now_ms():
    return int(time.time() * 1000)


def get_window_perf(row, window):
    """leaderboardRow から指定窓の {pnl, roi, vlm} を返す。"""
    for name, perf in row.get("windowPerformances", []):
        if name == window:
            return perf
    return None


def build_candidates(limit):
    """リーダーボードから候補プールを作る。
    LB_REQUIRE_WINDOWS の全窓で pnl>0 かつ vlm>0（＝長期も直近も一貫して勝つ）を必須にする。"""
    lb = hl_client.download_leaderboard()
    rows = lb.get("leaderboardRows", [])
    cands = []
    for row in rows:
        try:
            acct = float(row.get("accountValue", 0))
        except (TypeError, ValueError):
            continue
        if acct < config.MIN_ACCOUNT_VALUE:
            continue
        # 必須窓すべてで黒字＆出来高あり
        windows = {}
        ok_all = True
        for win in config.LB_REQUIRE_WINDOWS:
            perf = get_window_perf(row, win)
            if not perf:
                ok_all = False
                break
            pnl = float(perf.get("pnl", 0))
            roi = float(perf.get("roi", 0))
            vlm = float(perf.get("vlm", 0))
            if pnl <= 0 or vlm <= 0:
                ok_all = False
                break
            windows[win] = {"pnl": pnl, "roi": roi, "vlm": vlm}
        if not ok_all:
            continue
        rank_perf = windows.get(config.LB_RANK_WINDOW) or list(windows.values())[0]
        cands.append({
            "address": row["ethAddress"],
            "account_value": acct,
            "lb_windows": windows,                 # 各窓の成績（一貫性の根拠）
            "lb_pnl": rank_perf["pnl"],             # 並び替え/正規化用（rank窓）
            "lb_roi": rank_perf["roi"],
        })
    # rank窓の指標(roi/pnl)降順で上位を候補化
    sort_key = "lb_roi" if config.LB_RANK_METRIC == "roi" else "lb_pnl"
    cands.sort(key=lambda c: c[sort_key], reverse=True)
    if limit:
        cands = cands[:limit]
    return cands


def fetch_price_data(start_ms, end_ms):
    """対象銘柄の 1h 足を取得し、価格lookup と 急変イベントを返す。"""
    price = {}   # coin -> sorted list of (t_ms, close)
    events = []  # {coin, t0, direction('long'/'short'), pct}
    for coin in config.COINS:
        c = hl_client.candles(coin, "1h", start_ms, end_ms + config.HIT_HORIZON_H * MS_H)
        series = sorted([(int(x["t"]), float(x["c"])) for x in (c or [])])
        price[coin] = series
        # 急変イベント検出: EVENT_WINDOW_H 時間で EVENT_MOVE_PCT% 以上
        w = config.EVENT_WINDOW_H
        for i in range(len(series) - w):
            t0, p0 = series[i]
            _, p1 = series[i + w]
            if p0 <= 0:
                continue
            pct = (p1 - p0) / p0 * 100
            if abs(pct) >= config.EVENT_MOVE_PCT:
                events.append({
                    "coin": coin,
                    "t0": t0,
                    "direction": "long" if pct > 0 else "short",
                    "pct": round(pct, 2),
                })
    return price, events


def price_at(series, t_ms):
    """t_ms 時点（直近で t<=t_ms の足の close）の価格。無ければ None。"""
    lo, hi, ans = 0, len(series) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t_ms:
            ans = series[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def open_direction(dir_str):
    """約定 dir から「新規/増し玉の結果方向」を返す。close等は None。"""
    d = (dir_str or "").strip()
    if ">" in d:                         # "Long > Short" 等の反転
        return d.split(">")[-1].strip().lower()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()  # long / short
    return None


def unrealized_majors(address):
    """現在建玉のうち majors の未実現損益・建玉価値を返す。"""
    st = hl_client.clearinghouse_state(address)
    upnl = 0.0
    pos_value = 0.0
    held = []
    for ap in (st or {}).get("assetPositions", []):
        p = ap.get("position", {})
        if p.get("coin") not in config.COINS:
            continue
        u = float(p.get("unrealizedPnl", 0) or 0)
        szi = float(p.get("szi", 0) or 0)        # +long / -short
        pv = float(p.get("positionValue", 0) or 0)
        upnl += u
        pos_value += pv
        held.append({
            "coin": p.get("coin"),
            "side": "long" if szi > 0 else "short",
            "unrealized_pnl": round(u, 2),
            "position_value": round(pv),
        })
    return round(upnl, 2), round(pos_value), held


def analyze_fills(fills, price, events):
    """majors のみの約定からウォレット成績を算出。"""
    realized = 0.0
    closes = wins = 0
    opens = hits = 0
    n_fills = len(fills)
    largest_win = 0.0
    event_lead_notional = 0.0
    lead_examples = []

    # イベントを (coin,direction)->[t0...] に整理
    ev_by = {}
    for e in events:
        ev_by.setdefault((e["coin"], e["direction"]), []).append(e)

    for f in fills:
        coin = f["coin"]
        px = float(f["px"])
        sz = float(f["sz"])
        t = int(f["time"])
        cpnl = float(f.get("closedPnl", 0) or 0)
        notional = px * sz

        # 実現損益・勝率（クローズfill = closedPnl が非ゼロ）
        if abs(cpnl) > 1e-9:
            realized += cpnl
            closes += 1
            if cpnl > 0:
                wins += 1
                largest_win = max(largest_win, cpnl)

        # 方向的中率（オープンfill）
        d = open_direction(f.get("dir"))
        if d in ("long", "short"):
            opens += 1
            fut = price_at(price.get(coin, []), t + config.HIT_HORIZON_H * MS_H)
            if fut is not None:
                if (d == "long" and fut > px) or (d == "short" and fut < px):
                    hits += 1

            # イベント先行度: 大口の正方向オープンが急変の直前窓にあるか
            if notional >= config.LARGE_TRADE_USD:
                for e in ev_by.get((coin, d), []):
                    if e["t0"] - config.LEAD_WINDOW_H * MS_H <= t <= e["t0"]:
                        event_lead_notional += notional
                        if len(lead_examples) < 5:
                            lead_examples.append({
                                "coin": coin, "dir": d,
                                "entry_time": datetime.fromtimestamp(t / 1000, timezone.utc)
                                    .strftime("%Y-%m-%d %H:%M"),
                                "notional_usd": round(notional),
                                "event_move_pct": e["pct"],
                                "event_time": datetime.fromtimestamp(e["t0"] / 1000, timezone.utc)
                                    .strftime("%Y-%m-%d %H:%M"),
                                "hash": f.get("hash"),
                            })
                        break

    win_rate = wins / closes if closes else 0.0
    dir_acc = hits / opens if opens else 0.0

    # 平均ポジション保有時間: 銘柄ごとに 0→非0(建て) … 非0→0(閉じ) の往復を計測
    durations = []
    pos_open_t = {}
    last_pos = {}
    for f in sorted(fills, key=lambda x: int(x["time"])):
        coin = f["coin"]
        t = int(f["time"])
        sz = float(f["sz"])
        signed = sz if f.get("side") == "B" else -sz
        before = float(f.get("startPosition", last_pos.get(coin, 0)) or 0)
        after = before + signed
        eps = 1e-9
        opened = abs(before) < eps and abs(after) >= eps
        closed = abs(before) >= eps and abs(after) < eps
        flipped = before > eps and after < -eps or before < -eps and after > eps
        if closed or flipped:
            if coin in pos_open_t:
                durations.append(t - pos_open_t.pop(coin))
        if opened or flipped:
            pos_open_t[coin] = t
        last_pos[coin] = after
    avg_hold_h = round(sum(durations) / len(durations) / 3600000, 2) if durations else None
    n_positions = len(durations)

    return {
        "n_fills": n_fills,
        "n_closes": closes,
        "n_opens": opens,
        "realized_pnl": round(realized, 2),
        "win_rate": round(win_rate, 4),
        "dir_accuracy": round(dir_acc, 4),
        "largest_win": round(largest_win, 2),
        "event_lead_notional": round(event_lead_notional),
        "lead_examples": lead_examples,
        "avg_hold_h": avg_hold_h,
        "n_positions": n_positions,
    }


def normalize(vals):
    """0..1 正規化（最大で割る）。"""
    m = max(vals) if vals else 0
    return [v / m if m > 0 else 0.0 for v in vals]


def classify(r):
    """ウォレットを insider_suspect / pro_trader / excluded に分類。(category, reason)。"""
    if r.get("likely_mm"):
        return "excluded", "MM/HFT（自動マーケットメイク・方向性ベットでない）"
    win = r.get("win_rate", 0)
    dir_ = r.get("dir_accuracy", 0)
    closes = r.get("n_closes", 0)
    opens = r.get("n_opens", 0)
    pnl = r.get("total_pnl", 0)
    # インサイダー疑惑: 高精度・高的中・十分なサンプル・黒字
    if (dir_ >= config.INSIDER_DIR and win >= config.INSIDER_WIN
            and closes >= config.INSIDER_MIN_CLOSES
            and opens >= config.INSIDER_MIN_OPENS and pnl > 0):
        return "insider_suspect", f"的中率{dir_:.0%}・勝率{win:.0%}と極めて高精度（{closes}クローズで一貫）"
    # プロ: 多数取引で持続的に黒字
    if (win >= config.PRO_WIN and dir_ >= config.PRO_DIR
            and closes >= config.PRO_MIN_CLOSES and pnl > 0):
        return "pro_trader", f"勝率{win:.0%}・的中率{dir_:.0%}を{closes}クローズで維持（持続的優位）"
    # それ以外: エッジ不明瞭 or サンプル不足 → 除外
    if pnl <= 0:
        return "excluded", "majors で黒字でない"
    if closes < config.INSIDER_MIN_CLOSES:
        return "excluded", f"サンプル不足（クローズ{closes}件のみ）"
    return "excluded", f"エッジ不明瞭（的中率{dir_:.0%}・勝率{win:.0%}が基準未満）"


def score_results(results):
    """results（各metrics付き）に insider_score と category を付与し降順ソートして返す。
    MM/HFT は強く減点し、正規化の基準からも除外して他ウォレットが潰れるのを防ぐ。"""
    if not results:
        return results
    for r in results:
        r["likely_mm"] = (r.get("n_closes", 0) > config.MM_MAX_CLOSES
                          or r.get("n_fills", 0) > config.MM_MAX_FILLS)
    npnl = normalize([0 if r["likely_mm"] else max(r.get("total_pnl", 0), 0) for r in results])
    nlead = normalize([0 if r["likely_mm"] else r.get("event_lead_notional", 0) for r in results])
    for r, p, l in zip(results, npnl, nlead):
        score = (config.W_REALIZED_PNL * p
                 + config.W_WIN_RATE * r.get("win_rate", 0)
                 + config.W_DIR_ACCURACY * r.get("dir_accuracy", 0)
                 + config.W_EVENT_LEAD * l)
        if r["likely_mm"]:
            score *= config.MM_PENALTY
        r["insider_score"] = round(score, 4)
        r["category"], r["category_reason"] = classify(r)
    results.sort(key=lambda r: r["insider_score"], reverse=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=config.CANDIDATE_LIMIT,
                    help="候補ウォレット数（検証時は小さく）")
    ap.add_argument("--rescore", action="store_true",
                    help="再フェッチせず data/ranked.json を再採点して保存")
    args = ap.parse_args()

    if args.rescore:
        d = json.load(open(f"{config.DATA_DIR}/ranked.json", encoding="utf-8"))
        d["wallets"] = score_results(d["wallets"])
        json.dump(d, open(f"{config.DATA_DIR}/ranked.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"再採点完了（{len(d['wallets'])} 件）")
        return

    end = now_ms()
    start = end - config.ANALYSIS_DAYS * 24 * MS_H

    print(f"[1/4] 候補プール抽出（必須窓{config.LB_REQUIRE_WINDOWS}で黒字, 上位{args.limit}）...")
    cands = build_candidates(args.limit)
    print(f"    候補 {len(cands)} 件")

    print("[2/4] 価格足・急変イベント取得...")
    price, events = fetch_price_data(start, end)
    print(f"    イベント {len(events)} 件検出")

    print(f"[3/4] 候補の約定取得＋解析（直近{config.ANALYSIS_DAYS}日, {config.COINS}）...")
    results = []
    for i, c in enumerate(cands, 1):
        try:
            fills = hl_client.user_fills_by_time(c["address"], start, end)
        except Exception as e:
            print(f"    [{i}/{len(cands)}] {c['address']} 取得失敗: {e}")
            continue
        majors = [f for f in fills if f.get("coin") in config.COINS]
        metrics = analyze_fills(majors, price, events)
        if metrics["n_fills"] == 0:
            continue
        try:
            upnl, pos_val, held = unrealized_majors(c["address"])
        except Exception:
            upnl, pos_val, held = 0.0, 0, []
        metrics["unrealized_pnl"] = upnl
        metrics["position_value"] = pos_val
        metrics["held_positions"] = held
        metrics["total_pnl"] = round(metrics["realized_pnl"] + upnl, 2)
        results.append({**c, **metrics})
        if i % 10 == 0 or args.limit <= 5:
            print(f"    [{i}/{len(cands)}] {c['address'][:10]}.. "
                  f"majors={metrics['n_fills']} realized={metrics['realized_pnl']} "
                  f"unreal={upnl} win={metrics['win_rate']} dir={metrics['dir_accuracy']}")

    print("[4/4] スコアリング...")
    results = score_results(results)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "coins": config.COINS, "require_windows": config.LB_REQUIRE_WINDOWS,
            "analysis_days": config.ANALYSIS_DAYS, "n_candidates": len(cands),
            "n_events": len(events),
        },
        "wallets": results,
    }
    path = f"{config.DATA_DIR}/ranked.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"完了 → {path}（解析 {len(results)} 件）")


if __name__ == "__main__":
    main()
