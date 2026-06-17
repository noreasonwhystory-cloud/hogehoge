"""全ウォレットに「直近14日 取引あり/なし」を付与（active14フラグ＋タグ）。

active_to があればそれで判定。無ければ HL userFillsByTime(直近14日) で実確認（HLのみ・無料）。
タグ: 取引あり(14d) / 取引なし(14d)。行の背景色は active14=False で変わる(step6側)。
"""
import json
import time
from datetime import datetime, timezone, timedelta

import config
import hl_client
import step6_registry as reg6

REG = f"{config.DATA_DIR}/wallet_registry.json"


def main():
    reg = json.load(open(REG, encoding="utf-8"))
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=14)).isoformat()
    now = int(time.time() * 1000)
    lo = now - 14 * 24 * 3600 * 1000

    fetched = 0
    for k, e in reg["wallets"].items():
        at = e.get("active_to")
        if at:
            active = at >= cutoff
        else:
            try:
                f = hl_client.user_fills_by_time(e["address"], lo, now)
            except Exception:
                f = None
            if f:
                active = True
                t1 = max(int(x["time"]) for x in f)
                e["active_to"] = datetime.fromtimestamp(t1 / 1000, timezone.utc).strftime("%Y-%m-%d")
                e.setdefault("active_from", e["active_to"])
            else:
                active = False
            fetched += 1
        e["active14"] = active
        tags = [t for t in e.get("tags", []) if not t.startswith("取引あり") and not t.startswith("取引なし")]
        tags.append("取引あり(14d)" if active else "取引なし(14d)")
        e["tags"] = sorted(set(tags))

    json.dump(reg, open(REG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    reg6.render_all(reg)
    from collections import Counter
    c = Counter("あり" if e.get("active14") else "なし" for e in reg["wallets"].values())
    print(f"活動判定完了（HL追加取得 {fetched}件）: 直近14日 取引あり {c['あり']} / なし {c['なし']}")


if __name__ == "__main__":
    main()
