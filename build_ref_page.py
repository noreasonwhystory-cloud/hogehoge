"""手動調査した参考10件(手動追加4＋当セッション照会6)を1ページに統合 → ref_watchlist.html。

各件: リーダーボード成績 + HL約定の勝率/実現益 + 現在の含み損 + Nansen資金源/正体。
台帳本体には入れない『参考リスト』。index から別ページとしてリンク。
"""
import json
import time
import html
from datetime import datetime, timezone
from collections import Counter

import config
import hl_client

NOW = int(time.time() * 1000)
MS_H = 3600 * 1000

MANUAL = [
    "0x9cc53c5af67fb83a16cc41f61e242bade875ab3d",
    "0x50b309f78e774a756a2230e1769729094cac9f20",
    "0xa864144d507da1f5a90aae0147b8cba6d93a21cb",
    "0x350e33a777d510616fbdb483d1de3b50d1edfcfb",
]
LOOKUP = [
    "0xa6ee1ed1ae80b8352603654b39f5e7b9bedd5078",
    "0x06bc596fb16734f7abc3a5996b580be932c2fb72",
    "0x0335a1387afc039bf30ba7bf620630121c33b797",
    "0xaea391e34ce73d90a6944a5a4997d19cdd6e3467",
    "0xa5d07658d8214f83620871dd4e293b1bd8678181",
    "0xc7be26aba75daba73d9f4c202a16fb5ca7abc238",
]
HL = "https://app.hyperliquid.xyz/explorer/address/{a}"
NS = "https://app.nansen.ai/profiler?address={a}"
HD = "https://hyperdash.info/trader/{a}"                # Hyperdash トレーダープロフィール
HS = "https://hypurrscan.io/address/{a}"               # Hypurrscan 建玉/約定
AX = "https://hyperscreener.asxn.xyz/profile/{a}"      # ASXN プロフィール


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def usd(x):
    try:
        return f"${x:,.0f}"
    except (TypeError, ValueError):
        return "—"


def fetch_fills(addr, max_pages=22):
    out, cur = [], 0
    for _ in range(max_pages):
        ch = hl_client._post_info({"type": "userFillsByTime", "user": addr, "startTime": cur, "endTime": NOW})
        if not ch:
            break
        out.extend(ch)
        if len(ch) < 2000:
            break
        last = ch[-1]["time"]
        if last <= cur:
            break
        cur = last + 1
    capped = len(out) >= max_pages * 2000
    seen, ded = set(), []
    for f in out:
        if f.get("tid") in seen:
            continue
        seen.add(f.get("tid")); ded.append(f)
    return ded, capped


def wr(fs):
    cl = [f for f in fs if abs(float(f.get("closedPnl", 0) or 0)) > 1e-9]
    w = sum(1 for f in cl if float(f["closedPnl"]) > 0)
    return (round(w / len(cl), 4) if cl else None, len(cl),
            round(sum(float(f.get("closedPnl", 0) or 0) for f in fs)))


def analyze(addr):
    fl, capped = fetch_fills(addr)
    maj = [f for f in fl if f.get("coin") in config.COINS]
    awr, acl, areal = wr(fl)
    mwr, mcl, mreal = wr(maj)
    st = hl_client.clearinghouse_state(addr) or {}
    acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
    bags = [(p["position"]["coin"], round(float(p["position"]["unrealizedPnl"])))
            for p in st.get("assetPositions", []) if float(p["position"]["unrealizedPnl"]) < 0]
    period = None
    if fl:
        t0 = min(int(f["time"]) for f in fl); t1 = max(int(f["time"]) for f in fl)
        period = (datetime.fromtimestamp(t0 / 1000, timezone.utc).strftime("%Y-%m-%d"),
                  datetime.fromtimestamp(t1 / 1000, timezone.utc).strftime("%Y-%m-%d"))
    coins = Counter(f["coin"] for f in fl).most_common(5)
    return {"all_wr": awr, "all_real": areal, "maj_wr": mwr, "maj_real": mreal,
            "n_fills": len(fl), "capped": capped, "account": round(acct),
            "bags": bags, "period": period, "coins": coins}


def lb_row(addr, rows):
    r = next((x for x in rows if (x.get("ethAddress") or "").lower() == addr), None)
    if not r:
        return {}
    d = {w: v for w, v in r["windowPerformances"]}
    return {"account": round(float(r["accountValue"])),
            "m_pnl": float(d["month"]["pnl"]), "m_roi": float(d["month"]["roi"]),
            "a_pnl": float(d["allTime"]["pnl"]), "a_roi": float(d["allTime"]["roi"]),
            "a_vlm": float(d["allTime"]["vlm"])}


def funding(addr, reg, nsx):
    e = reg.get(addr)
    if e and e.get("first_funders"):
        ff = e["first_funders"]; labels = e.get("labels") or []
    else:
        n = nsx.get(addr, {}); ff = n.get("first_funders", []); labels = n.get("labels", [])
    src = ", ".join((f.get("label") or (f.get("address") or "")[:10]) for f in ff[:4]) or "—"
    return src, (labels or [])


def verdict(a):
    """挙動からの一言分類。"""
    mwr, mreal = a.get("maj_wr"), a.get("maj_real")
    areal = a.get("all_real")
    if a["account"] < 5000 and areal is not None and areal < 0:
        return "破綻寄り（口座ほぼ消失）", "#ff5d6c"
    if mwr and mwr >= 0.6 and mreal is not None and mreal < 0:
        return "偽高勝率（高勝率でも赤字＝塩漬け/薄利多売）", "#ff8c42"
    if a["bags"]:
        return "含み損バッグ保有（塩漬け中）", "#ff8c42"
    if areal is not None and areal > 0:
        return "純黒字（但しMM/高回転の可能性）", "#3fb950"
    return "赤字/不明瞭", "#9aa3ad"


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    nsx = json.load(open(f"{config.DATA_DIR}/ten_ref_nansen.json", encoding="utf-8"))
    lb = json.load(open(f"{config.DATA_DIR}/leaderboard.json", encoding="utf-8"))
    rows = lb if isinstance(lb, list) else lb.get("leaderboardRows") or []

    cards = ""
    for group, addrs in [("手動追加（以前お渡しの4件）", MANUAL), ("当セッションで照会（6件）", LOOKUP)]:
        cards += f'<h2>{esc(group)}</h2>'
        for a in addrs:
            lbr = lb_row(a, rows)
            ana = analyze(a)
            src, labels = funding(a, reg, nsx)
            vtxt, vcol = verdict(ana)
            acct = lbr.get("account") or ana["account"]
            bagtxt = ", ".join(f"{c} {usd(u)}" for c, u in ana["bags"]) or "なし"
            coins = ", ".join(f"{c}×{n}" for c, n in ana["coins"])
            per = f"{ana['period'][0]}〜{ana['period'][1]}" if ana["period"] else "—"
            lbline = (f"月 {usd(lbr['m_pnl'])}({lbr['m_roi']*100:.0f}%) / 全期 <b>{usd(lbr['a_pnl'])}</b>({lbr['a_roi']*100:.0f}%) / 全期出来高 {usd(lbr['a_vlm'])}"
                      if lbr else "リーダーボード圏外")
            cards += f"""
<div class="case">
  <div class="ct"><code>{esc(a)}</code>
    <span class="lnk"><a href="{HL.format(a=a)}" target="_blank">HL</a> · <a href="{HD.format(a=a)}" target="_blank" title="Hyperdash">HD📊</a> · <a href="{HS.format(a=a)}" target="_blank" title="Hypurrscan">HS</a> · <a href="{AX.format(a=a)}" target="_blank" title="ASXN プロフィール">AX📈</a> · <a href="{NS.format(a=a)}" target="_blank">Nansen</a></span></div>
  <div class="vd" style="--c:{vcol}">▶ {esc(vtxt)}</div>
  <div class="kv">
    <span>正体ラベル</span><b>{esc('、'.join(labels) or '—（匿名）')}</b>
    <span>資金源(First Funder)</span><b>{esc(src)}</b>
    <span>口座残高</span><b>{usd(acct)}</b>
    <span>勝率（全 / majors）</span><b>{ana['all_wr']} / {ana['maj_wr']}</b>
    <span>実現損益（全 / majors）</span><b>{usd(ana['all_real'])} / {usd(ana['maj_real'])}</b>
    <span>含み損バッグ</span><b>{esc(bagtxt)}</b>
    <span>リーダーボード</span><b>{lbline}</b>
    <span>取得約定 / 期間</span><b>{ana['n_fills']}{'(上限到達)' if ana['capped'] else ''} / {per}</b>
    <span>主銘柄</span><b>{esc(coins)}</b>
  </div>
</div>"""

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>参考: 手動調査アドレス10件</title>
<style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:26px;line-height:1.6;max-width:1000px}}
h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:15px;margin:24px 0 8px;border-left:3px solid #7c5cff;padding-left:8px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:14px}} a{{color:#4ea1ff}} code{{font-size:12px}}
.case{{background:#171b22;border:1px solid #232a34;border-radius:10px;padding:12px 15px;margin-bottom:12px}}
.ct{{font-weight:700;font-size:13px;margin-bottom:6px}} .ct code{{color:#c9d1d9}} .lnk{{float:right;font-size:12px}} .lnk a{{text-decoration:none}}
.vd{{color:var(--c);font-weight:700;font-size:13px;margin-bottom:8px}}
.kv{{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:13px}} .kv span{{color:#8b949e}}
.note{{background:#171b22;border:1px solid #232a34;border-left:3px solid #ffb454;border-radius:8px;padding:10px 14px;font-size:13px;margin:10px 0}}
</style></head><body>
<h1>📌 参考: 手動調査アドレス 10件</h1>
<div class="sub">台帳本体（発掘＋分類）とは別に、手動で個別調査したアドレスの記録。<a href="index.html">← トップ</a>　生成 {gen}</div>
<div class="note">これらは台帳の発掘パイプラインを通っていない<b>手動照会の参考リスト</b>。多くが MM/高回転・赤字・塩漬けで、<b>方向で稼ぐインサイダー/プロのプロファイルではない</b>。判定はHL実測の挙動＋Nansen資金源に基づく。</div>
{cards}
</body></html>"""
    with open(f"{config.HERE}/ref_watchlist.html", "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"完了 → {config.HERE}/ref_watchlist.html （10件）")


if __name__ == "__main__":
    main()
