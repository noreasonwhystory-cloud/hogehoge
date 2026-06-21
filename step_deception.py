"""欺瞞前提インサイダー検知の手法一覧＋結果ページ(deception.html)を生成する。

入力: data/deception_candidates.json（パターン該当数）, data/deception_verdicts.json（workflow裁定）
出力: deception.html
"""
import os
import json
import html

import config

H = config.HERE


def esc(x):
    return html.escape(str(x)) if x is not None else ""


# 検討した欺瞞パターン（後でリストして、の本体）
PATTERNS = [
    ("A", "サイズ条件付きスキル", "大口は高勝率・小口はわざと負ける＝コピトレ撹乱。勝率を意図的に濁す",
     "大口/小口で勝率を2分割し、大口≥0.6 かつ 小口≤0.45 の乖離を検出", "wr_big / wr_small"),
    ("B", "非対称ペイオフ(低勝率高PnL)", "あえて勝率を5割以下に見せ、数発の大当たりで稼ぐ",
     "勝率≤0.45 かつ 黒字 かつ 上位3勝が全益の50%以上", "win_rate / top3_share"),
    ("C", "休眠→カタリスト覚醒", "普段は眠り、材料時だけ起きて正しい方向。利益が覚醒直後48hに集中",
     "14日以上の休眠明け48hに全益の40%以上が集中", "max_gap_d / burst_share"),
    ("D", "制御された負け", "負けは小さく速く(化粧)・勝ちは大きい。損失サイズが不自然に揃う",
     "勝ち建玉中央値÷負け建玉中央値 ≥ 2.5（負けを意図的に小さく）", "size_ratio"),
    ("E", "新鮮ウォレット単発大当たり", "使い捨て口座で一発当てて引上げ。短命・少取引・高実現",
     "活動≤21日 かつ クローズ≤15 かつ 実現≥$100k", "active_days / n_closes"),
    ("F", "デコイ→本命フリップ", "小さい逆張りを囮で見せ、直後に大きい本命へ反転",
     "同コイン逆方向が6h以内・先(小)負け→後(大)勝ちが3回以上", "flips"),
    ("G", "出金規律(勝ち逃げ)", "大勝直後にドローダウン前に出金。やめ時を知っている",
     "既存の💸出金疑い検知(PnL÷残高比)に統合済", "cashout_ratio"),
    ("H", "勝ち逃げ後の意図的低調", "注目を集めた後わざと不調期を作り記録を濁し、また高値更新",
     "累積益ピーク前後に小口での谷→新高値更新の系列を検出", "dip_then_new"),
]


def main():
    cand = json.load(open(f"{config.DATA_DIR}/deception_candidates.json", encoding="utf-8"))
    ver = json.load(open(f"{config.DATA_DIR}/deception_verdicts.json", encoding="utf-8"))
    summ = cand["summary"]

    # パターン表
    prows = ""
    cnt = {"A": "A_size_conditional", "B": "B_asymmetric", "C": "C_dormant_burst",
           "D": "D_controlled_loss", "E": "E_fresh_hit", "F": "F_decoy_flip",
           "G": "G_cashout", "H": "H_deliberate_dip"}
    for code, name, motive, how, feat in PATTERNS:
        key = cnt.get(code)
        n = summ.get(key)
        ndisp = f"{n}件" if n is not None else ("既存統合" if code == "G" else "0件")
        prows += f"""<tr><td class="pc">{code}</td><td><b>{esc(name)}</b></td><td>{esc(motive)}</td>
<td class="how">{esc(how)}</td><td><code>{esc(feat)}</code></td><td class="n">{ndisp}</td></tr>"""

    # 要監視2件
    def watchcard(x):
        a = x["address"]
        j, r = x["judge"], x["refute"]
        return f"""<div class="wc">
<div class="wch"><code>{esc(a)}</code> <span class="rs">残存疑い {r['residual_suspicion']}</span>
<a href="https://app.hyperliquid.xyz/explorer/address/{a}" target="_blank">HL</a></div>
<div class="wl"><b>第1判定</b> (欺瞞度{j['deception_score']} / {esc(j['primary_explanation'])}): {esc(j['key_evidence'])}</div>
<div class="wr2"><b>反証後</b>: {esc(r['refute_reason'])}</div></div>"""
    watch_html = "".join(watchcard(x) for x in ver["watch"]) or "<p>なし</p>"

    # 説明内訳
    from collections import Counter
    expl = Counter(a["judge"]["primary_explanation"] for a in ver["all"])
    EXJP = {"trend_following_riskmgmt": "損小利大の規律(良いトレード)", "regime_luck": "地合便乗・運",
            "market_maker": "MM/HFT(薄利多売)", "normal_trader": "普通のトレーダー",
            "scaling_strategy": "スケールイン(分割建て)", "insider_camouflage": "欺瞞疑い(第1判定)",
            "withdrawal": "出金", "unknown": "不明"}
    exrows = "".join(f"<tr><td>{esc(EXJP.get(k,k))}</td><td class='n'>{v}件</td></tr>"
                     for k, v in expl.most_common())

    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>欺瞞インサイダー検知（隠蔽手口別）</title><style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:28px;line-height:1.7;font-size:13.5px}}
h1{{font-size:22px;margin:0 0 6px}} h2{{font-size:16px;margin:26px 0 10px;color:#cbd5e1;border-bottom:1px solid #232a34;padding-bottom:6px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:18px}} a{{color:#4ea1ff;text-decoration:none}}
table{{border-collapse:collapse;width:100%;max-width:1100px;margin:6px 0}}
th,td{{border:1px solid #232a34;padding:8px 10px;text-align:left;vertical-align:top;font-size:12.5px}}
th{{background:#10151c}} .pc{{font-weight:700;color:#ff8893;text-align:center}}
.how{{color:#9aa3ad;font-size:11.5px}} .n{{text-align:center;color:#ffb454;font-weight:700;white-space:nowrap}}
code{{background:#0b0f14;padding:1px 5px;border-radius:4px;font-size:11px;color:#7ee0c0}}
.kpi{{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}}
.kc{{background:#171b22;border:1px solid #232a34;border-radius:10px;padding:12px 18px;min-width:120px}}
.kc .v{{font-size:24px;font-weight:700}} .kc .l{{font-size:11px;color:#8b949e}}
.kc.zero .v{{color:#3fb950}} .kc.watch .v{{color:#ffb454}} .kc.fp .v{{color:#8b949e}}
.box{{background:#10151c;border:1px solid #232a34;border-radius:10px;padding:14px 18px;max-width:1080px;margin:10px 0}}
.wc{{background:#1c1812;border:1px solid #3a2f1a;border-left:3px solid #ffb454;border-radius:8px;padding:12px 14px;margin:10px 0;max-width:1080px}}
.wch{{font-size:13px;margin-bottom:6px}} .rs{{color:#ffb454;font-weight:700;margin:0 8px}}
.wl{{font-size:12px;color:#d7e0c8;margin:5px 0}} .wr2{{font-size:11.5px;color:#9aa3ad;margin-top:5px}}
.concl{{background:#0f1b17;border-left:3px solid #3fb950;border-radius:6px;padding:12px 16px;max-width:1080px}}
</style></head><body>
<h1>🎭 欺瞞インサイダー検知（隠蔽手口別）</h1>
<div class="sub">インサイダーは「勝ちすぎを隠す」ため、わざと負けたり勝率を濁したりする。その“性格の悪い”手口を
従来手法（往復/反復/遅効エッジ等）とは別軸で、行動から検出→workflowで多角裁定した。
<a href="index.html">トップ</a> ・ <a href="methods.html">手法カタログ</a></div>

<h2>検討した8つの隠蔽パターン</h2>
<div class="sub" style="margin-bottom:6px">全キャッシュ3,844ウォレット・3,031万約定から deterministic に特徴量化して各パターン該当を抽出。</div>
<table><tr><th>#</th><th>パターン</th><th>インサイダーの動機(なぜこうするか)</th><th>検知ロジック</th><th>主な特徴量</th><th>該当</th></tr>
{prows}</table>

<h2>裁定結果（容疑者89件 × 2段：欺瞞判定→敵対的反証）</h2>
<div class="kpi">
<div class="kc zero"><div class="v">0</div><div class="l">欺瞞インサイダー濃厚</div></div>
<div class="kc watch"><div class="v">{len(ver['watch'])}</div><div class="l">要監視(残疑あり)</div></div>
<div class="kc fp"><div class="v">{ver['false_positive_count']}</div><div class="l">偽陽性(良性で説明)</div></div>
</div>
<div class="box"><b>偽陽性87件の良性説明の内訳</b>（第1判定の主因）
<table style="max-width:420px;margin-top:8px"><tr><th>良性の正体</th><th>件数</th></tr>{exrows}</table>
<div style="font-size:11.5px;color:#8b949e;margin-top:8px">※「制御された負け」の極端例(size_ratio 37 等)も、地合プーリングの偽陽性・分割利確の小closedPnl・休眠明けの隔離churnで説明され、カモフラージュ配置の証拠は出なかった。</div></div>

<h2>要監視に残った2件（断定せず・残存疑いのみ）</h2>
{watch_html}

<h2>結論</h2>
<div class="concl">欺瞞前提で“性格の悪い”手口（わざと負ける・勝率を濁す・休眠覚醒・損失化粧・デコイ反転・勝ち逃げ）を
8軸で追ったが、<b>確証ある欺瞞インサイダーはゼロ</b>。従来6定義＋複数地平線＋遅効エッジの精査と<b>完全に収束</b>した。
要監視は単一銘柄アルファ寄りの2件のみで、いずれも「隠す意図(deliberate noise)」の立証には至らず。
判定核: <b>欺瞞と言えるのは「優位そのものが不自然」かつ「それを隠すノイズ」が併存する場合のみ</b>——
損小利大やスケールインや地合便乗といった“良いトレードの癖”は欺瞞ではない、という弁別で偽陽性を排除した。</div>
</body></html>"""
    open(os.path.join(H, "deception.html"), "w", encoding="utf-8").write(doc)
    print("deception.html 生成")


if __name__ == "__main__":
    main()
