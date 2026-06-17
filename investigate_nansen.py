"""指定アドレスの正体を Nansen REST で割る（ラベル/資金源/取引相手）。

使い方: python investigate_nansen.py 0xaddr1 0xaddr2 ...
"""
import sys
from datetime import datetime, timezone, timedelta

import config
import nansen_client as nc


def ok(r):
    return isinstance(r, dict) and "_error" not in r


def first_nonempty(addr, fn):
    for ch in config.ENRICH_CHAINS:
        r = fn(addr, ch)
        if ok(r) and r.get("data"):
            return ch, r["data"], None
        if isinstance(r, dict) and "_error" in r:
            err = r
    return None, [], locals().get("err")


def main():
    addrs = sys.argv[1:]
    to = datetime.now(timezone.utc).date()
    frm = to - timedelta(days=60)
    for a in addrs:
        print("=" * 78)
        print(f"■ {a}")
        # ラベル
        ch, labels, err = first_nonempty(a, nc.address_labels)
        if err:
            print(f"  ⚠ Nansenエラー {err.get('_error')}: {err.get('_body','')[:80]}")
        names = []
        for l in labels:
            names.append(l.get("label") or l.get("address_label") or str(l))
        print(f"  ラベル({ch}): {', '.join(names) if names else '—（ラベル無し）'}")

        # 関連ウォレット（資金源）
        ch, rel, _ = first_nonempty(a, nc.related_wallets)
        if rel:
            print(f"  関連ウォレット({ch}): {len(rel)}件")
            for r in rel[:8]:
                print(f"    - {r.get('relation','?'):14s} {r.get('address','')[:18]}.. "
                      f"[{r.get('address_label','')}] {r.get('block_timestamp','')}")
        else:
            print("  関連ウォレット: —")

        # 取引相手（直近60日）
        cp_data, cp_ch = [], None
        for c in config.ENRICH_CHAINS:
            r = nc.counterparties(a, c, frm.isoformat(), to.isoformat())
            if ok(r) and r.get("data"):
                cp_data, cp_ch = r["data"], c
                break
        if cp_data:
            print(f"  取引相手({cp_ch}, 直近60日): {len(cp_data)}件")
            for c in cp_data[:8]:
                lbl = c.get("counterparty_address_label")
                lbl = ", ".join(lbl) if isinstance(lbl, list) else (lbl or "")
                print(f"    - {lbl or c.get('counterparty_address','')[:18]:24s} "
                      f"${c.get('total_volume_usd',0):,.0f} ({c.get('interaction_count','?')}回)")
        else:
            print("  取引相手: —（オンチェーン痕跡希薄）")
    print("=" * 78)


if __name__ == "__main__":
    main()
