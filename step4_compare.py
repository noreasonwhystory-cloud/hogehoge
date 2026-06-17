"""Step4: 「Hyperliquidだけ」vs「Nansenを足す」の差別化を視覚化した HTML を生成する。

出力: compare.html
- 概念マトリクス（各問いに HL / Nansen が答えられるか）
- 実例ウォレットの左右対比（左=HLのみ＝正体不明 / 右=Nansen付与＝解錠）
実例の Nansen 側は dossiers.json に生データがあればそれを使い、
クレジット枯渇等で無ければ初回取得済みの代表例にフォールバックする。
"""
import os
import json
import html

import config

HL_ADDR = "https://app.hyperliquid.xyz/explorer/address/{a}"
NANSEN = "https://app.nansen.ai/profiler?address={a}"


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def usd(x):
    try:
        return f"${x:,.0f}"
    except (TypeError, ValueError):
        return "-"


def pct(x):
    try:
        return f"{x*100:.1f}%"
    except (TypeError, ValueError):
        return "-"


# 能力マトリクス: (問い, HLで分かるか, Nansenで分かるか, 補足)
MATRIX = [
    ("perp の全約定（時刻・価格・サイズ・方向）", True, True, "HLが一次ソース・約定粒度"),
    ("勝率・実現/含み損益・建玉", True, True, "HLの clearinghouseState/fills"),
    ("方向的中率・イベント先行度", True, True, "HL足と突合して算出"),
    ("このアドレスは誰か（ラベル）", False, True, "Fund/CEX/著名エンティティ"),
    ("資金の出所（First Funder）", False, True, "related-wallets"),
    ("同一主体の別ウォレット（名寄せ）", False, True, "クラスタリング"),
    ("オンチェーンの取引相手（CEX入金元/OTC）", False, True, "counterparties"),
    ("対象トークンの現物仕込み（25+チェーン）", False, True, "token transfers/flows"),
    ("HL以外の資産・横断PnL", False, True, "portfolio/pnl"),
]

# クレジット枯渇時のフォールバック実例（初回取得済みの実データ）
FALLBACK = {
    "address": "0xaf0fdd39e5d92499b0ed9f68693da99c0ec1e92e",
    "labels": ["High Activity"],
    "related_count": 10,
    "first_funder": "First Funder（ラベル無の関連ウォレット）",
    "counterparties": ["High Activity", "Token Millionaire", "High Activity",
                       "High Activity", "Token Millionaire"],
    "note": "※この Nansen 側データは初回取得時のもの（現在キーはクレジット枯渇で再取得不可）",
}


def pick_example():
    """ranked.json から HL成績、dossiers.json から Nansen文脈を取得。
    Nansen情報が空ならフォールバックを使う。"""
    ranked = json.load(open(f"{config.DATA_DIR}/ranked.json", encoding="utf-8"))["wallets"]
    dpath = f"{config.DATA_DIR}/dossiers.json"
    dossiers = (json.load(open(dpath, encoding="utf-8"))["dossiers"]
                if os.path.exists(dpath) else [])
    dmap = {d["address"]: d for d in dossiers}

    # Nansen情報が豊富な最上位を探す
    live = None
    for w in ranked:
        d = dmap.get(w["address"])
        if d and (d.get("labels") or d.get("related_wallets") or d.get("counterparties")):
            live = (w, d)
            break

    if live:
        w, d = live
        nansen = {
            "address": w["address"],
            "labels": [(l.get("label") or l.get("address_label") or l)
                       if isinstance(l, dict) else l for l in d.get("labels", [])],
            "related_count": len(d.get("related_wallets", [])),
            "first_funder": ", ".join(
                (f.get("address_label") or f.get("address", "")[:12]) for f in d.get("first_funders", [])
            ) or "—",
            "counterparties": [
                ", ".join(c["counterparty_address_label"]) if isinstance(c.get("counterparty_address_label"), list)
                else (c.get("counterparty_address_label") or c.get("counterparty_address", "")[:12])
                for c in d.get("counterparties", [])[:5]
            ],
            "note": "",
        }
        return w, nansen, False

    # フォールバック: 該当HL成績 + 初回Nansenデータ
    w = next((x for x in ranked if x["address"] == FALLBACK["address"]), ranked[0])
    return w, FALLBACK, True


def main():
    hl, nansen, is_fallback = pick_example()
    a = hl["address"]

    # マトリクス行
    rows = ""
    for q, h, n, note in MATRIX:
        hc = "<span class='yes'>✓</span>" if h else "<span class='no'>✕</span>"
        nc = "<span class='yes'>✓</span>" if n else "<span class='no'>✕</span>"
        only = "nansen-only" if (n and not h) else ""
        rows += (f"<tr class='{only}'><td class='q'>{esc(q)}</td>"
                 f"<td class='c'>{hc}</td><td class='c'>{nc}</td>"
                 f"<td class='note'>{esc(note)}</td></tr>")

    cps_hl = "<li class='unknown'>取引相手 → <b>不明</b>（生の0xしか見えない）</li>"
    cps_n = "".join(f"<li>{esc(c)}</li>" for c in nansen["counterparties"]) or "<li class='muted'>希薄</li>"
    labels_n = ", ".join(esc(l) for l in nansen["labels"]) or "—"
    fallback_note = (f"<div class='fbnote'>{esc(nansen.get('note',''))}</div>"
                     if is_fallback and nansen.get("note") else "")

    out_html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hyperliquid だけ vs Nansen を足す — 差別化</title>
<style>
:root{{--bg:#0e1116;--card:#171b22;--mut:#8b949e;--hl:#f7a440;--ns:#7c5cff;--ok:#3fb950;--no:#6b7280;--line:#232a34}}
*{{box-sizing:border-box}}
body{{font-family:system-ui,"Segoe UI",sans-serif;background:var(--bg);color:#e6edf3;margin:0;padding:26px;line-height:1.6}}
h1{{font-size:21px;margin:0 0 4px}}
h2{{font-size:16px;margin:26px 0 10px;border-left:3px solid var(--ns);padding-left:8px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:8px}}
.legend{{font-size:12px;color:var(--mut);margin-bottom:14px}}
.hlchip{{color:var(--hl);font-weight:700}} .nschip{{color:var(--ns);font-weight:700}}

/* レイヤー図 */
.layers{{display:flex;flex-direction:column;gap:8px;margin:10px 0 4px}}
.layer{{border-radius:10px;padding:12px 16px;border:1px solid var(--line)}}
.layer.l-ns{{background:linear-gradient(90deg,#1c1633,#171b22);border-color:#3a2d6b}}
.layer.l-hl{{background:linear-gradient(90deg,#2a1f10,#171b22);border-color:#5c4318}}
.layer b{{font-size:14px}} .layer .d{{color:var(--mut);font-size:12px}}

/* マトリクス */
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{border:1px solid var(--line);padding:7px 10px;text-align:left}}
th{{background:#10151c;font-size:12px}}
td.c{{text-align:center;font-weight:700;width:90px}}
th.hl{{color:var(--hl)}} th.ns{{color:var(--ns)}}
.yes{{color:var(--ok)}} .no{{color:var(--no)}}
td.q{{font-weight:600}} td.note{{color:var(--mut);font-size:12px}}
tr.nansen-only{{background:#191436}}
tr.nansen-only td.q::before{{content:"🔓 ";}}

/* 左右対比 */
.vs{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px}}
.pane{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}}
.pane.hl{{border-top:4px solid var(--hl)}}
.pane.ns{{border-top:4px solid var(--ns)}}
.pane h3{{margin:0 0 4px;font-size:15px}}
.pane .tag{{font-size:11px;color:var(--mut);margin-bottom:10px}}
.kv{{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;font-size:13px;margin-bottom:10px}}
.kv .k{{color:var(--mut)}}
ul{{margin:4px 0;padding-left:18px;font-size:13px}}
li.unknown{{color:#e0697a;list-style:none;margin-left:-18px}}
li.unlocked{{color:#c3b5ff}}
.block{{margin-top:10px}} .block b{{font-size:13px}}
.muted{{color:var(--mut)}}
code{{background:#0b0f14;padding:1px 5px;border-radius:4px;font-size:12px}}
.fbnote{{background:#2a2410;border-left:3px solid var(--hl);padding:8px 12px;border-radius:6px;font-size:12px;margin:10px 0}}
.foot{{margin-top:22px;font-size:12px;color:var(--mut);border-top:1px solid var(--line);padding-top:12px}}
a{{color:#4ea1ff;text-decoration:none}}
@media(max-width:820px){{.vs{{grid-template-columns:1fr}}}}
</style></head><body>

<h1>Hyperliquid だけ <span class="muted">vs</span> Nansen を足す — 何が違うか</h1>
<div class="sub">perp インサイダー追跡における「行動データ」と「正体・資金源データ」の差別化</div>
<div class="legend"><span class="hlchip">■ Hyperliquid 公開API</span>＝無料・約定粒度の<b>行動</b>　／
<span class="nschip">■ Nansen REST API</span>＝<b>正体・資金源・オンチェーン文脈</b>（クレジット消費）</div>

<h2>2つのデータ層</h2>
<div class="layers">
  <div class="layer l-ns"><b class="nschip">Nansen 層 — 「誰が・どこの金で・他に何を」</b>
    <div class="d">ラベル / First Funder（資金源） / 関連ウォレット名寄せ / 取引相手 / 25+チェーンのオンチェーン仕込み / 横断PnL</div></div>
  <div class="layer l-hl"><b class="hlchip">Hyperliquid 層 — 「何をどう取引し、どれだけ勝ったか」</b>
    <div class="d">全約定（時刻/価格/サイズ/方向/実現損益） / 建玉・含み損益 / 勝率・方向的中率・イベント先行度</div></div>
</div>
<div class="sub">発掘は HL 層だけで完結する。Nansen 層は<b>HL の0xアドレスに正体と文脈を被せる</b>──これが単独では決して埋まらない差分。</div>

<h2>能力マトリクス（🔓 = Nansen で初めて解ける問い）</h2>
<table>
<tr><th>問い</th><th class="hl">HLだけ</th><th class="ns">+Nansen</th><th>補足</th></tr>
{rows}
</table>

<h2>実例で見る差分 — <code>{esc(a[:14])}…</code></h2>
<div class="sub">容疑度スコア <b>{hl.get('insider_score')}</b>（勝率{pct(hl.get('win_rate'))} / 方向的中率{pct(hl.get('dir_accuracy'))}）の要レビュー候補。</div>
{fallback_note}
<div class="vs">
  <div class="pane hl">
    <h3 class="hlchip">Hyperliquid だけ</h3>
    <div class="tag">無料・即時・約定粒度。だが「行動」しか見えない</div>
    <div class="kv">
      <span class="k">アドレス</span><span><code>{esc(a)}</code></span>
      <span class="k">勝率(majors)</span><span>{pct(hl.get('win_rate'))}</span>
      <span class="k">方向的中率</span><span>{pct(hl.get('dir_accuracy'))}</span>
      <span class="k">majors損益</span><span>{usd(hl.get('total_pnl'))}</span>
      <span class="k">週間PnL(全銘柄)</span><span>{usd(hl.get('lb_pnl'))}</span>
      <span class="k">約定数</span><span>{hl.get('n_fills','-')}</span>
    </div>
    <div class="block"><b>この先が見えない:</b>
      <ul>
        <li class="unknown">このアドレスは誰か → <b>不明</b></li>
        <li class="unknown">資金の出所 → <b>不明</b></li>
        <li class="unknown">同一主体の別ウォレット → <b>不明</b></li>
        {cps_hl}
      </ul>
    </div>
  </div>
  <div class="pane ns">
    <h3 class="nschip">+ Nansen を足すと</h3>
    <div class="tag">同じアドレスに正体・資金源・取引相手が乗る</div>
    <div class="block"><b>🔓 ラベル（正体）:</b> {labels_n}</div>
    <div class="block"><b>🔓 資金源(First Funder):</b> {esc(nansen['first_funder'])}</div>
    <div class="block"><b>🔓 関連ウォレット（名寄せ）:</b> {nansen['related_count']} 件</div>
    <div class="block"><b>🔓 主な取引相手:</b>
      <ul>{cps_n}</ul>
    </div>
    <div class="block"><a href="{NANSEN.format(a=a)}" target="_blank">→ Nansen プロファイラで開く</a></div>
  </div>
</div>

<div class="foot">
結論: <b class="hlchip">HL</b> が「<b>不自然に勝っている行動</b>」を炙り出し、<b class="nschip">Nansen</b> が「<b>それが誰で・どこの金か</b>」を与える。
インサイダー疑惑は両者が揃って初めて<b>「身元のある資金源 × 異常な成績」</b>として立ち上がる。<br>
HL リンク: <a href="{HL_ADDR.format(a=a)}" target="_blank">explorer</a> ／ 本比較は調査ツールの実データに基づく。
</div>
</body></html>"""

    out = os.path.join(config.HERE, "compare.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(f"完了 → {out}（実例: {a[:14]}… / fallback={is_fallback}）")


if __name__ == "__main__":
    main()
