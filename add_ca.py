"""手動指定アドレス(CA)を HL精査＋Nansen照会して台帳に追記(upsert)する。

ranked.json 由来でない手動追加用。tag「手動追加CA」付き。
使い方: python add_ca.py 0xaddr1 0xaddr2 ...
"""
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import Counter

import config
import hl_client
import nansen_client as nc
import step6_registry as reg6
import tagging

REGISTRY = f"{config.DATA_DIR}/wallet_registry.json"
MS_H = 3600 * 1000

# 個別所見（HL精査で判明済み・人手の注記）
NOTES = {
    "0x9cc53c5af67fb83a16cc41f61e242bade875ab3d": "苦戦中の高頻度BTC勢。allTime -10%。大型BTCショート含み損",
    "0x50b309f78e774a756a2230e1769729094cac9f20": "自動HFT/MM。月+$4.8M/ROI126%。約定が多すぎ履歴取得は頭打ち",
    "0xa864144d507da1f5a90aae0147b8cba6d93a21cb": "分散・超高レバ(BTC40x)の多資産プロ。allTime +143%",
    "0x350e33a777d510616fbdb483d1de3b50d1edfcfb": "HYPE中心の怪物プロ。allTime ROI 10,637%。現在9銘柄全空で含み損",
}


def ok(r):
    return isinstance(r, dict) and "_error" not in r


def lb_lookup(addr):
    lb = hl_client.download_leaderboard()
    for row in lb.get("leaderboardRows", []):
        if row.get("ethAddress", "").lower() == addr.lower():
            out = {"accountValue": float(row.get("accountValue", 0))}
            for n, p in row.get("windowPerformances", []):
                out[n] = {"pnl": float(p["pnl"]), "roi": float(p["roi"]), "vlm": float(p["vlm"])}
            return out
    return None


def hl_profile(addr):
    st = hl_client.clearinghouse_state(addr)
    ms = (st or {}).get("marginSummary", {})
    acct = float(ms.get("accountValue", 0) or 0)
    ntl = float(ms.get("totalNtlPos", 0) or 0)
    upnl = 0.0
    held = []
    for ap in (st or {}).get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        u = float(p.get("unrealizedPnl", 0) or 0)
        upnl += u
        held.append({"coin": p.get("coin"), "side": "long" if szi > 0 else "short",
                     "position_value": round(float(p.get("positionValue", 0) or 0)),
                     "unrealized_pnl": round(u)})
    end = int(time.time() * 1000)
    fills = hl_client.user_fills_by_time(addr, end - config.ANALYSIS_DAYS * 24 * MS_H, end)
    realized = sum(float(f.get("closedPnl", 0) or 0) for f in fills)
    closes = [f for f in fills if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
    wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)
    win_rate = round(wins / len(closes), 4) if closes else 0
    coins = Counter(f["coin"] for f in fills)
    # 保有時間
    durations, open_t = [], {}
    for f in sorted(fills, key=lambda x: int(x["time"])):
        c = f["coin"]; t = int(f["time"])
        signed = float(f["sz"]) * (1 if f.get("side") == "B" else -1)
        before = float(f.get("startPosition", 0) or 0); after = before + signed
        eps = 1e-9
        if (abs(before) >= eps and abs(after) < eps) or (before > eps > after) or (before < -eps < 0 and after > eps):
            if c in open_t:
                durations.append(t - open_t.pop(c))
        if abs(before) < eps <= abs(after):
            open_t[c] = t
    avg_hold = round(sum(durations) / len(durations) / MS_H, 2) if durations else None
    likely_mm = (len(closes) > config.MM_MAX_CLOSES or len(fills) > config.MM_MAX_FILLS)
    return {
        "account_value": round(acct), "leverage": round(ntl / acct, 2) if acct else 0,
        "held_positions": held, "n_fills_recent": len(fills), "n_closes_recent": len(closes),
        "realized_pnl": round(realized), "unrealized_pnl": round(upnl),
        "total_pnl": round(realized + upnl), "win_rate": win_rate, "avg_hold_h": avg_hold,
        "top_coins": dict(coins.most_common(5)), "likely_mm": likely_mm,
    }


def nansen_profile(addr):
    out = {"labels": [], "first_funders": [], "counterparties": []}
    for ch in config.ENRICH_CHAINS:
        r = nc.address_labels(addr, ch)
        if ok(r) and r.get("data"):
            out["labels"] = [l.get("label") or l.get("address_label") or str(l) for l in r["data"]]
            break
    for ch in config.ENRICH_CHAINS:
        r = nc.related_wallets(addr, ch)
        if ok(r) and r.get("data"):
            out["first_funders"] = [{"address": x.get("address"), "label": x.get("address_label"),
                                     "relation": x.get("relation"), "time": x.get("block_timestamp")}
                                    for x in r["data"]]
            break
    to = datetime.now(timezone.utc).date(); frm = to - timedelta(days=60)
    for ch in config.ENRICH_CHAINS:
        r = nc.counterparties(addr, ch, frm.isoformat(), to.isoformat())
        if ok(r) and r.get("data"):
            out["counterparties"] = [{"label": (", ".join(c["counterparty_address_label"])
                                                if isinstance(c.get("counterparty_address_label"), list)
                                                else c.get("counterparty_address_label")),
                                      "address": c.get("counterparty_address"),
                                      "volume_usd": c.get("total_volume_usd"),
                                      "count": c.get("interaction_count")} for c in r["data"][:10]]
            break
    return out


def main():
    import json
    addrs = [a for a in sys.argv[1:] if a.startswith("0x")]
    if not addrs:
        print("usage: python add_ca.py 0xaddr ...")
        return
    reg = json.load(open(REGISTRY, encoding="utf-8"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for a in addrs:
        key = a.lower()
        print(f"調査中: {a[:14]}..")
        hp = hl_profile(a)
        lb = lb_lookup(a) or {}
        np_ = nansen_profile(a)
        position = "高頻度HFT/MM(手動追加)" if hp["likely_mm"] else "高頻度プロ(手動追加)"
        snap = {"date": today, "metric_category": "manual-CA",
                "win_rate": hp["win_rate"], "total_pnl": hp["total_pnl"],
                "avg_hold_h": hp["avg_hold_h"], "insider_likelihood": None}
        entry = reg["wallets"].get(key)
        if entry is None:
            entry = {"address": a, "first_seen": today, "times_seen": 0, "tags": [], "notes": ""}
            reg["wallets"][key] = entry
        entry["last_seen"] = today
        entry["times_seen"] = entry.get("times_seen", 0) + 1
        entry["position"] = position
        entry["metric_category"] = "manual-CA"
        entry["insider_likelihood"] = None
        entry["alt_verdict"] = NOTES.get(key, "手動追加CA")
        tags = set(entry.get("tags", [])); tags.add("手動追加CA")
        if hp["likely_mm"]:
            tags.add("HFT/MM")
        entry["tags"] = sorted(tags)
        entry["hl_profile"] = hp
        entry["lb_month"] = lb.get("month"); entry["lb_allTime"] = lb.get("allTime")
        entry["labels"] = np_["labels"]; entry["first_funders"] = np_["first_funders"]
        entry["counterparties"] = np_["counterparties"]; entry["nansen_checked"] = today
        entry["held_positions"] = hp["held_positions"]
        entry["auto_tags"] = tagging.derive_tags({
            "roi": (lb.get("allTime") or {}).get("roi"),
            "pnl": (lb.get("allTime") or {}).get("pnl"),
            "n_fills": hp.get("n_fills_recent"), "avg_hold_h": hp.get("avg_hold_h"),
            "held": hp.get("held_positions"), "leverage": hp.get("leverage"),
            "top_coins": hp.get("top_coins"), "labels": np_.get("labels"),
        })
        entry["current"] = snap
        entry["history"] = [h for h in entry.get("history", []) if h.get("date") != today]
        entry["history"].append(snap)
        labs = ", ".join(np_["labels"]) or "ラベル無"
        print(f"  → {position} [{labs}] allTimeROI={lb.get('allTime',{}).get('roi','?')}")

    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    reg6.render_html(reg)
    print(f"追記完了 → {REGISTRY} / registry.html（総 {len(reg['wallets'])} 件）")


if __name__ == "__main__":
    main()
