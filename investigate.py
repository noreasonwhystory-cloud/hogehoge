"""指定アドレスを Hyperliquid 公開APIだけで精査する（全銘柄）。

使い方: python investigate.py 0xaddr1 0xaddr2 ...
"""
import sys
import time
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import config
import hl_client

MS_H = 3600 * 1000
HEX40 = re.compile(r"^0x[0-9a-fA-F]{40}$")


def now_ms():
    return int(time.time() * 1000)


def fmt_t(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def lb_lookup(addr):
    """キャッシュ済リーダーボードから該当行の各窓成績を返す。"""
    try:
        lb = hl_client.download_leaderboard()
    except Exception:
        return None
    for row in lb.get("leaderboardRows", []):
        if row.get("ethAddress", "").lower() == addr.lower():
            out = {"accountValue": float(row.get("accountValue", 0))}
            for name, perf in row.get("windowPerformances", []):
                out[name] = {"pnl": float(perf["pnl"]), "roi": float(perf["roi"]), "vlm": float(perf["vlm"])}
            return out
    return None


def open_dir(d):
    d = (d or "").strip()
    if ">" in d:
        return d.split(">")[-1].strip().lower()
    if d.startswith("Open"):
        return d.replace("Open", "").strip().lower()
    return None


def investigate(addr):
    print("=" * 78)
    print(f"■ {addr}")
    if not HEX40.match(addr):
        print("  ⚠ 不正なアドレス形式（16進40桁でない）。スキップ。")
        return
    # 1) 現在の建玉
    st = hl_client.clearinghouse_state(addr)
    ms = (st or {}).get("marginSummary", {})
    acct = float(ms.get("accountValue", 0) or 0)
    ntl = float(ms.get("totalNtlPos", 0) or 0)
    lev = (ntl / acct) if acct else 0
    print(f"  口座評価額: ${acct:,.0f}  建玉総額: ${ntl:,.0f}  実効レバレッジ: {lev:.2f}x")
    positions = (st or {}).get("assetPositions", [])
    if positions:
        print("  現在の建玉:")
        for ap in positions:
            p = ap.get("position", {})
            szi = float(p.get("szi", 0) or 0)
            print(f"    - {p.get('coin'):6s} {'LONG' if szi>0 else 'SHORT':5s} "
                  f"建玉${float(p.get('positionValue',0) or 0):,.0f} "
                  f"含み損益${float(p.get('unrealizedPnl',0) or 0):,.0f} "
                  f"entry@{p.get('entryPx')} lev{p.get('leverage',{}).get('value','?')}x")
    else:
        print("  現在の建玉: なし（フラット）")

    # 2) 約定（取れる範囲・全銘柄）
    end = now_ms()
    fills = hl_client.user_fills_by_time(addr, 0, end)
    if not fills:
        print("  約定履歴: 取得できず（または無し）")
    else:
        t0 = min(int(f["time"]) for f in fills)
        t1 = max(int(f["time"]) for f in fills)
        span_d = (t1 - t0) / (24 * MS_H)
        coins = Counter(f["coin"] for f in fills)
        realized = sum(float(f.get("closedPnl", 0) or 0) for f in fills)
        closes = [f for f in fills if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
        wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)
        win_rate = wins / len(closes) if closes else 0
        # 売買バイアス
        buy = sum(float(f["sz"]) * float(f["px"]) for f in fills if f.get("side") == "B")
        sell = sum(float(f["sz"]) * float(f["px"]) for f in fills if f.get("side") == "A")
        # 平均保有時間
        durations = []
        open_t = {}
        for f in sorted(fills, key=lambda x: int(x["time"])):
            c = f["coin"]; t = int(f["time"])
            signed = float(f["sz"]) * (1 if f.get("side") == "B" else -1)
            before = float(f.get("startPosition", 0) or 0)
            after = before + signed
            eps = 1e-9
            if (abs(before) >= eps and abs(after) < eps) or (before > eps and after < -eps) or (before < -eps and after > eps):
                if c in open_t:
                    durations.append(t - open_t.pop(c))
            if abs(before) < eps and abs(after) >= eps or (before > eps and after < -eps) or (before < -eps and after > eps):
                open_t[c] = t
        avg_hold = (sum(durations) / len(durations) / MS_H) if durations else None

        print(f"  約定履歴: {len(fills)}件  期間 {fmt_t(t0)} 〜 {fmt_t(t1)}（{span_d:.1f}日, HL保持分のみ）")
        print(f"    銘柄: {dict(coins.most_common(8))}")
        print(f"    実現損益(closedPnl計): ${realized:,.0f}  クローズ{len(closes)}件 勝率{win_rate:.0%}")
        print(f"    売り${sell:,.0f} / 買い${buy:,.0f}  → {'ショート' if sell>buy else 'ロング'}寄り")
        print(f"    平均保有: {f'{avg_hold:.1f}h' if avg_hold else '—'}（往復{len(durations)}回）")

    # 3) リーダーボード順位
    lb = lb_lookup(addr)
    if lb:
        m = lb.get("month", {}); a = lb.get("allTime", {})
        print(f"  リーダーボード: month PnL ${m.get('pnl',0):,.0f}/ROI {m.get('roi',0):.0%}  "
              f"allTime PnL ${a.get('pnl',0):,.0f}/ROI {a.get('roi',0):.0%}")
    else:
        print("  リーダーボード: 該当なし（口座$100k未満や対象外の可能性）")


def main():
    addrs = sys.argv[1:]
    if not addrs:
        print("usage: python investigate.py 0xaddr ...")
        return
    for a in addrs:
        investigate(a)
    print("=" * 78)


if __name__ == "__main__":
    main()
