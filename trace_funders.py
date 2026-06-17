"""指定ウォレットの資金源(First Funder)アドレスを1段上流へ遡る。

各 suspect の first_funders アドレスに対し Nansen で labels/related-wallets/counterparties を照会。
プロトコル/ブリッジ/CEX のコントラクトは「行き止まり」、通常ウォレットは「追跡可能」と判定。
使い方: python trace_funders.py 0xsuspect1 0xsuspect2 ...
"""
import sys
import json
from datetime import datetime, timezone, timedelta

import config
import nansen_client as nc

REG = f"{config.DATA_DIR}/wallet_registry.json"
DEADEND_KW = ["Protocol", "Bridge", "Spoke", "Deployer", "Router", "Factory",
              "Pool", "Contract", "Vault", ": Wallet", "Hot Wallet", "Solver",
              "Gas.zip", "Stargate", "Across", "Hop", "Socket", "Relay", "Symmio",
              "Binance", "Coinbase", "OKX", "Bybit", "Kraken", "Bitget"]


def ok(r):
    return isinstance(r, dict) and "_error" not in r


def is_deadend(label):
    return any(k.lower() in (label or "").lower() for k in DEADEND_KW)


def profile(addr):
    """funder の labels / 上流funder / counterparties を返す。"""
    out = {"labels": [], "up_funders": [], "counterparties": []}
    for ch in config.ENRICH_CHAINS:
        r = nc.address_labels(addr, ch)
        if ok(r) and r.get("data"):
            out["labels"] = [l.get("label") or l.get("address_label") or str(l) for l in r["data"]]
            break
    for ch in config.ENRICH_CHAINS:
        r = nc.related_wallets(addr, ch)
        if ok(r) and r.get("data"):
            out["up_funders"] = [{"address": x.get("address"), "label": x.get("address_label"),
                                  "relation": x.get("relation")} for x in r["data"][:6]]
            break
    to = datetime.now(timezone.utc).date(); frm = to - timedelta(days=120)
    for ch in config.ENRICH_CHAINS:
        r = nc.counterparties(addr, ch, frm.isoformat(), to.isoformat())
        if ok(r) and r.get("data"):
            out["counterparties"] = [{"label": (", ".join(c["counterparty_address_label"])
                                                 if isinstance(c.get("counterparty_address_label"), list)
                                                 else c.get("counterparty_address_label")),
                                      "vol": c.get("total_volume_usd")} for c in r["data"][:6]]
            break
    return out


def main():
    addrs = [a.lower() for a in sys.argv[1:] if a.startswith("0x")]
    reg = json.load(open(REG, encoding="utf-8"))["wallets"]
    for a in addrs:
        e = reg.get(a)
        if not e:
            print(f"■ {a[:12]}.. 台帳に無し"); continue
        print("=" * 76)
        print(f"■ suspect {a}  [{e.get('position')}]")
        funders = e.get("first_funders", [])
        if not funders:
            print("  資金源なし"); continue
        for f in funders[:4]:
            faddr = f.get("address"); flabel = f.get("label") or ""
            if not faddr:
                continue
            dead = is_deadend(flabel)
            mark = "⛔行き止まり(コントラクト/CEX)" if dead else "🔍追跡可(通常ウォレット)"
            print(f"\n  └ 資金源: {faddr}  [{flabel or '—'}]  {mark}")
            if dead:
                print("     → プロトコル/ブリッジ/CEX のため上流遡及は無意味（ここが実質終点）")
                continue
            p = profile(faddr)
            print(f"     正体: {', '.join(p['labels']) or '—'}")
            uf = "; ".join(f"{x.get('relation')}:{(x.get('label') or (x.get('address') or '')[:10])}" for x in p["up_funders"]) or "—"
            print(f"     さらに上流: {uf}")
            cp = "; ".join(f"{x['label'] or '?'}(${x['vol']:,.0f})" for x in p["counterparties"] if x.get('vol')) or "—"
            print(f"     主な取引相手: {cp}")
    print("=" * 76)


if __name__ == "__main__":
    main()
