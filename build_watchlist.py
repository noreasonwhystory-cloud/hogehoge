"""現在の監視対象を1枚に集約する名簿ページ(watchlist.html)を生成する。

各ページに散在していた監視対象(遅効エッジ・欺瞞要監視・出金疑い等)を横断で集める。
リアルタイムの建玉は live.html、ここは「誰を・なぜ追っているか」の静的ロスター。
"""
import os
import json
import html

import config

H = config.HERE
HLX = "https://app.hyperliquid.xyz/explorer/address/{a}"
HD = "https://hyperdash.info/trader/{a}"
HS = "https://hypurrscan.io/address/{a}"
AX = "https://hyperscreener.asxn.xyz/portfolio?address={a}"


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def reason(e):
    """監視理由をtags/positionから1行で。"""
    tags = e.get("tags", [])
    bits = []
    if "局所検証済(真の遅効エッジ)" in tags:
        bits.append("遅効エッジ(局所検証済)")
    if "欺瞞精査:要監視" in tags:
        bits.append("欺瞞精査で残疑")
    if any(t.startswith("単一銘柄アルファ") for t in tags):
        bits.append("単一銘柄アルファ")
    if e["position"] == "💸 出金疑い(要監視)":
        bits.append("出金(hit-and-run)疑い")
    return " / ".join(bits) or e["position"]


def main():
    W = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]

    def is_watch(e):
        t = e.get("tags", [])
        return (e["position"] in ("弱い疑惑(監視継続)", "💸 出金疑い(要監視)", "インサイダー疑惑(要監視)")
                or "欺瞞精査:要監視" in t or "局所検証済(真の遅効エッジ)" in t)

    items = sorted([e for e in W.values() if is_watch(e)],
                   key=lambda e: -(e.get("current", {}).get("total_pnl") or 0))

    rows = ""
    for e in items:
        a = e["address"]
        cur = e.get("current", {})
        rm = cur.get("total_pnl")
        rmd = f"${rm:,.0f}" if isinstance(rm, (int, float)) else "—"
        lb = e.get("lb_alltime")
        lbd = f"${lb:,.0f}" if isinstance(lb, (int, float)) else "—"
        wr = cur.get("win_rate")
        wrd = f"{wr:.2f}" if isinstance(wr, (int, float)) else "—"
        # notes_jp 先頭2行(現状ヘッダ+精査結論)を要約に
        nj = (e.get("notes_jp") or "").split("\n")
        summary = " ".join(s for s in nj[:3] if not s.startswith("―"))[:240]
        tags = "".join(f"<span class='tag'>{esc(t)}</span>" for t in e.get("tags", [])
                       if not t.startswith("funder:"))
        nf = e.get("n_fills_14d")
        nfd = f"{nf:,}" if isinstance(nf, (int, float)) else "—"
        nfm = e.get("n_fills_14d_maj")
        nfsub = f"<div class='sub2'>majors {nfm:,}</div>" if isinstance(nfm, (int, float)) else ""
        rows += f"""<tr>
<td><b>{esc(reason(e))}</b><div class="pos">{esc(e['position'])}</div></td>
<td><code>{esc(a[:18])}…</code><div class="lnk"><a href="{HLX.format(a=a)}" target="_blank">HL</a> <a href="{HD.format(a=a)}" target="_blank" title="Hyperdash">HD📊</a> <a href="{HS.format(a=a)}" target="_blank" title="Hypurrscan">HS</a> <a href="{AX.format(a=a)}" target="_blank" title="ASXN ポートフォリオ">AX📈</a></div></td>
<td class="num">{rmd}</td><td class="num">{lbd}</td><td class="num">{wrd}</td>
<td class="num">{nfd}{nfsub}</td>
<td class="per">{esc(e.get('active_from'))}〜{esc(e.get('active_to'))}</td>
<td class="sm">{tags}<div class="note">{esc(summary)}</div></td>
</tr>"""

    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>現在の監視対象（集約）</title><style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:26px;font-size:13px}}
h1{{font-size:21px;margin:0 0 5px}} .sub{{color:#8b949e;font-size:13px;margin-bottom:16px}} a{{color:#4ea1ff;text-decoration:none}}
table{{border-collapse:collapse;width:100%;max-width:1180px}}
th,td{{border:1px solid #232a34;padding:8px 10px;text-align:left;vertical-align:top}} th{{background:#10151c;font-size:11px}}
.pos{{color:#8b949e;font-size:11px;margin-top:3px}} code{{background:#0b0f14;padding:1px 5px;border-radius:4px;font-size:11px}}
.lnk a{{font-size:10px}} .num{{text-align:right;white-space:nowrap}} .per{{font-size:11px;color:#9aa3ad;white-space:nowrap}}
.sm{{max-width:520px}} .tag{{display:inline-block;background:#16201c;border:1px solid #2a4636;color:#7fd6a8;border-radius:9px;font-size:10px;padding:1px 7px;margin:1px}}
.note{{font-size:11px;color:#9aa3ad;margin-top:5px;line-height:1.55}} .sub2{{font-size:10px;color:#8b949e}}
.kpi{{display:flex;gap:12px;margin:12px 0}} .kc{{background:#171b22;border:1px solid #232a34;border-radius:9px;padding:10px 16px}}
.kc .v{{font-size:22px;font-weight:700;color:#ffb454}} .kc .l{{font-size:11px;color:#8b949e}}
</style></head><body>
<h1>🎯 現在の監視対象（集約名簿）</h1>
<div class="sub">各ページに散在する監視対象を横断で集約。確証インサイダーは未検出ゆえ、残るのは「残存疑い」層のみ。
リアルタイムの建玉・新規アクションは <a href="live.html">📡 ライブ監視</a>。 <a href="index.html">トップ</a></div>
<div class="kpi"><div class="kc"><div class="v">{len(items)}</div><div class="l">監視対象 合計</div></div></div>
<table>
<tr><th>監視理由</th><th>アドレス</th><th>majors実現</th><th>HL公式通算</th><th>勝率</th><th>取引数(14日)</th><th>取引期間</th><th>タグ / 精査メモ</th></tr>
{rows}</table>
<div class="sub" style="margin-top:14px;font-size:11.5px">※ いずれも「確証」ではなく人間レビュー用の残存疑い。遅効エッジ＝4h固定では見えぬ24-72hの局所検証済の黒字スイング、
欺瞞要監視＝隠蔽手口8軸の裁定で良性説に解消しきれなかった残疑、単一銘柄アルファ＝特定銘柄(@107等)への情報優位の可能性。</div>
</body></html>"""
    open(os.path.join(H, "watchlist.html"), "w", encoding="utf-8").write(doc)
    print(f"watchlist.html 生成 — 監視対象 {len(items)}件")


if __name__ == "__main__":
    main()
