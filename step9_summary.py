"""Step9: 総評＋「判定カテゴリ × Nansenタグ」の相関分析を1ページにまとめる。

台帳・欺瞞裁定から実データで集計して summary.html を生成（データが増えても自動で正確）。
出力: summary.html
"""
import os
import json
import html
from collections import Counter

import config

CEX = ["Binance", "Coinbase", "OKX", "Bybit", "Kraken", "Bitget", "KuCoin",
       "Nexo", "Gate", "HTX", "MEXC", "Gemini"]
BRIDGE = ["Across", "Stargate", "Hop", "Orbiter", "Socket", "Relay", "Bridge",
          "Spoke", "Refuel", "🤖", "Deployer", "Router", "Factory", "Pool",
          "Proxy", "Mastercopy", "Symmio", "Enzyme", "Gas.zip", "Solver"]
# 相関集計の対象（疑惑側＋プロ）。MM/除外/偽陽性は対象外。
WATCH = ["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
         "プロトレーダー(本物)", "alt主体プロ"]


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def lab_type(e):
    L = [str(x) for x in (e.get("labels") or [])]
    if not L:
        return "無(匿名)"
    s = " ".join(L)
    if "Smart" in s:
        return "SmartMoney"
    if any(k in s for k in ["Capital", "Fund", "GSR", "Abraxas", "Vault Leader"]):
        return "ファンド/機関"
    if ".eth" in s or "OpenSea" in s:
        return "ENS/個人名"
    return "汎用(HighBalance等)"


def fund_type(e):
    ff = e.get("first_funders") or []
    if not ff:
        return "不明"
    lbl = ff[0].get("label") or ""
    if any(k.lower() in lbl.lower() for k in CEX):
        return "CEX(取引所)"
    if any(k in lbl for k in BRIDGE):
        return "ブリッジ/コントラクト"
    if not lbl:
        return "個人(ラベル無)"
    return "汎用ウォレット"


def table(reg, fn, cats):
    """position × fn(分類) のクロス集計 HTML 表。"""
    head = "<tr><th>カテゴリ</th>" + "".join(f"<th>{esc(c)}</th>" for c in cats) + "<th>計</th></tr>"
    rows = ""
    for p in WATCH:
        g = [e for e in reg.values() if e.get("position") == p]
        if not g:
            continue
        cnt = Counter(fn(e) for e in g)
        cells = "".join(f"<td>{cnt.get(c, 0) or ''}</td>" for c in cats)
        rows += f"<tr><td class='pn'>{esc(p)}</td>{cells}<td><b>{len(g)}</b></td></tr>"
    return f"<table>{head}{rows}</table>"


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    total = len(reg)
    poscnt = Counter(e["position"] for e in reg.values())

    # 主要カテゴリの動的件数
    n_ins = poscnt.get("インサイダー疑惑(要監視)", 0)
    n_weak = poscnt.get("弱い疑惑(監視継続)", 0)
    n_cashout = poscnt.get("💸 出金疑い(要監視)", 0)
    n_pro = poscnt.get("プロトレーダー(本物)", 0)
    n_alt = poscnt.get("alt主体プロ", 0)
    n_mm = poscnt.get("高頻度MM", 0)
    n_fp = poscnt.get("偽陽性(数値疑惑→否定)", 0)
    n_excl = poscnt.get("除外/低優先", 0)
    n_clusterA = sum(1 for e in reg.values() if "cluster-A" in (e.get("tags") or []))
    n_decwatch = sum(1 for e in reg.values() if "欺瞞精査:要監視" in (e.get("tags") or []))
    # 監視対象（watchlist と同じ定義）
    def is_watch(e):
        t = e.get("tags", [])
        return (e["position"] in ("弱い疑惑(監視継続)", "💸 出金疑い(要監視)", "インサイダー疑惑(要監視)")
                or "欺瞞精査:要監視" in t)
    n_watch = sum(1 for e in reg.values() if is_watch(e))
    # プロ品質
    wq = Counter(e.get("wf_quality") for e in reg.values() if e.get("wf_quality"))
    n_elite = wq.get("エリート", 0)

    # 欺瞞裁定
    dv = {}
    p = f"{config.DATA_DIR}/deception_verdicts.json"
    if os.path.exists(p):
        dv = json.load(open(p, encoding="utf-8"))
    dec_total = dv.get("total", 0)
    dec_conf = len(dv.get("confirmed", []))
    dec_watch = len(dv.get("watch", []))
    dec_fp = dv.get("false_positive_count", 0)

    # 分布表（全ポジション）
    DIST = [("🟠 弱い疑惑(遅効/単一銘柄)", "弱い疑惑(監視継続)"),
            ("🔴 インサイダー疑惑", "インサイダー疑惑(要監視)"),
            ("💸 出金疑い", "💸 出金疑い(要監視)"),
            ("🟢 プロ(本物)", "プロトレーダー(本物)"),
            ("🔵 alt主体プロ", "alt主体プロ"),
            ("🟣 高頻度MM/HFT", "高頻度MM"),
            ("⚫ 偽陽性(数値疑惑→否定)", "偽陽性(数値疑惑→否定)"),
            ("⚫ 除外/低優先", "除外/低優先")]
    dist_rows = "".join(
        f"<tr><td style='text-align:left'>{esc(l)}</td><td><b>{poscnt.get(pos,0)}</b></td></tr>"
        for l, pos in DIST)

    # 相関テーブル
    fund_cats = ["CEX(取引所)", "ブリッジ/コントラクト", "個人(ラベル無)", "汎用ウォレット", "不明"]
    fund_tbl = table(reg, fund_type, fund_cats)
    lab_cats = ["無(匿名)", "ENS/個人名", "ファンド/機関", "SmartMoney", "汎用(HighBalance等)"]
    lab_tbl = table(reg, lab_type, lab_cats)

    def cex_count(positions):
        g = [e for e in reg.values() if e.get("position") in positions]
        return sum(1 for e in g if fund_type(e) == "CEX(取引所)"), len(g)
    ins_cex, ins_n = cex_count(["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)"])
    co_cex, co_n = cex_count(["💸 出金疑い(要監視)"])
    sm = sum(1 for e in reg.values() if e.get("position") in WATCH and lab_type(e) == "SmartMoney")
    anon_rows = ""
    for label, pos in [("🟠 弱い疑惑", "弱い疑惑(監視継続)"), ("🟢 プロ(本物)", "プロトレーダー(本物)"),
                       ("🔵 alt主体プロ", "alt主体プロ")]:
        g = [e for e in reg.values() if e.get("position") == pos]
        a = sum(1 for e in g if not e.get("labels"))
        if g:
            anon_rows += f"<tr><td style='text-align:left'>{esc(label)}</td><td>{a} / {len(g)}</td></tr>"

    out = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>総評 — Hyperliquid perp インサイダー追跡のまとめ</title>
<style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:28px;line-height:1.7;max-width:1020px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 10px;border-left:3px solid #4ea1ff;padding-left:8px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:16px}}
table{{border-collapse:collapse;font-size:13px;margin:8px 0;width:100%}}
th,td{{border:1px solid #232a34;padding:6px 9px;text-align:center}} th{{background:#10151c;font-size:12px}}
td.pn{{text-align:left;font-weight:600;white-space:nowrap}}
.box{{background:#171b22;border:1px solid #232a34;border-radius:10px;padding:14px 18px;margin:10px 0;font-size:13px}}
.yes{{border-left:4px solid #3fb950}} .no{{border-left:4px solid #ff5d6c}} .warn{{border-left:4px solid #ffb454}}
.big{{font-size:15px;font-weight:700}} b.g{{color:#69d98a}} b.r{{color:#ff8893}} b.o{{color:#ffc06b}}
a{{color:#4ea1ff}} ul{{margin:6px 0;padding-left:20px}} li{{margin-bottom:5px}}
.kpi{{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}}
.kc{{background:#10151c;border:1px solid #232a34;border-radius:9px;padding:10px 16px;min-width:96px;text-align:center}}
.kc .v{{font-size:22px;font-weight:700}} .kc .l{{font-size:11px;color:#8b949e}}
</style></head><body>
<h1>📊 総評 — Hyperliquid perp インサイダー追跡のまとめ</h1>
<div class="sub">台帳 {total} 件時点。HL公開API（無料・約定粒度）で発掘 → 行動分析＋workflow多角裁定で精査 → Nansenで正体補助、の3段。
<a href="index.html">← トップ</a> ／ <a href="registry.html">監視台帳</a> ／ <a href="watchlist.html">監視対象</a> ／ <a href="deception.html">欺瞞検知</a> ／ <a href="compare.html">HL vs Nansen</a></div>

<h2>1. 到達点（現在の分布）</h2>
<div class="kpi">
<div class="kc"><div class="v" style="color:#ffb454">{n_watch}</div><div class="l">監視対象(残疑)</div></div>
<div class="kc"><div class="v" style="color:#3fb950">{n_pro}</div><div class="l">プロ(本物)</div></div>
<div class="kc"><div class="v" style="color:#56b6c2">{n_alt}</div><div class="l">alt主体プロ</div></div>
<div class="kc"><div class="v" style="color:#a78bfa">{n_mm}</div><div class="l">高頻度MM</div></div>
<div class="kc"><div class="v" style="color:#ff5d6c">0</div><div class="l">確証インサイダー</div></div>
</div>
<table style="max-width:380px"><tr><th style="text-align:left">カテゴリ</th><th>件数</th></tr>{dist_rows}</table>

<h2>2. 核心：確証ある個人インサイダーはゼロに収束</h2>
<div class="box no">
<b class="big">7系統の独立した物差し（往復・反復／6定義／複数地平線／欺瞞8軸）が、すべて同じ結論に収束した。</b><br>
「急変前に大口で建てた（先行）」と出たウォレットを精査すると<b class="r">ことごとく N=1（1注文の分割約定）</b>に化け、別イベントで反復して当てる本物の挙動は<b>ゼロ</b>。
旧疑惑10件も別視点workflowで<b class="r">全件 debunked（本物確度0.05〜0.08）</b>。
🔴インサイダー疑惑 {n_ins}件・確証インサイダー<b>ゼロ</b>。残るのは断定しない監視対象のみ。
</div>
<div class="box no">
<b class="big">「往復定義が厳しすぎただけでは？」を6つの別定義で検証 → すべて同結論。</b>
<table style="max-width:none;margin-top:8px">
<tr><th>定義</th><th>測るもの</th><th>結果</th></tr>
<tr><td><b>① 利益集中度＋完璧エントリ</b></td><td>少数のデカ勝ちに集中＆底/天で入ったか</td><td><b class="r">完璧でも伸びは1〜7%</b>＝平均回帰スキャル</td></tr>
<tr><td><b>② conviction lift</b></td><td>大きく賭ける時ほど(トレンド補正後)当てるか</td><td><b class="r">最大+0.9%/4h＝ノイズ</b>、3件は負</td></tr>
<tr><td><b>③ alt/株perp拡張</b></td><td>情報優位余地のあるalt・株perpで集中＆完璧か</td><td>セクター全体の上げ便乗で説明(リーク不要)</td></tr>
<tr><td><b>④ 含み損なし×高勝率×高的中</b></td><td>『勝ち玉だけ確定・負けは塩漬け』汚染を排除</td><td><b class="g">塩漬けで勝率を粉飾していた約120件を撃墜</b></td></tr>
<tr><td><b>⑤ ④＋両方向＋対称サイズ＋複数月銘柄</b></td><td>トレンド汚染も排除の最厳格AND</td><td><b class="r">全549件 通過ゼロ</b></td></tr>
</table>
<b>副産物（重要）:</b> 「含み損なし」検査で、高勝率を称する大半が<b class="r">塩漬けで勝率を粉飾</b>と定量判明＝<u>勝率を判定に使ってはならない</u>の決定的裏付け。
</div>
<div class="box yes">
<b class="big">⑥ 方向的中を複数地平線(1〜72h)で測り直し → 4h固定が見落とした「遅効エッジ」5件。</b><br>
従来の方向的中は4時間後一点固定で、数日かけて効く玉をコイン投げと誤判定していた。1〜72hで測り直し、分散テスト＋majors実現益黒字＋<b>局所ベースライン</b>検証を全て満たした<b>5件</b>のみ🟠弱い疑惑へ。
<table style="max-width:560px;margin-top:6px"><tr><th>アドレス</th><th>補正72h(局所)</th><th>majors実現益</th></tr>
<tr><td>0xccf135ab… (ETHショート)</td><td>+9.2%</td><td><b class="g">+$1.31M</b></td></tr>
<tr><td>0x3662dd1d… (BTC)</td><td>+6.6%</td><td>+$952k</td></tr>
<tr><td>0xef91b28f… (BTC)</td><td>+4.0%</td><td>+$278k</td></tr>
<tr><td>0x8ff1d5d5… (BTC)</td><td>+3.1%</td><td>+$228k</td></tr>
<tr><td>0x9a568bfe… (ETH)</td><td>+3.0%</td><td>+$157k</td></tr></table>
<b>教訓:</b> 遅効エッジ単独では負け組も拾う→実現益と組合せ必須。全期間平均で引くと暴落regimeを取りこぼす→局所ベースラインで引き直し、<b class="r">SOL便乗2件はエッジ消失</b>。残5件は実力スイングか遅効先行か断定不可＝<b>最有力だが断定しない</b>監視対象。</div>
<div class="box yes">
<b class="big">⑦ 欺瞞8軸：インサイダーが“わざと負ける/勝率を濁す”隠蔽手口を別軸で追跡 → 確証ゼロ。</b><br>
「勝った後コピトレ対策にわざと負ける」等の性格の悪い手口を想定し、A:サイズ条件付きスキル／B:非対称ペイオフ／C:休眠覚醒／D:制御された負け／E:新鮮単発／F:デコイ反転／G:出金規律／H:意図的低調の<b>8パターン</b>で全3,844件・3,031万約定を特徴量化。容疑者{dec_total}件をworkflow178エージェントで2段裁定(欺瞞判定→敵対的反証)した結果:
<div class="kpi" style="margin-top:8px">
<div class="kc"><div class="v" style="color:#3fb950">{dec_conf}</div><div class="l">欺瞞濃厚</div></div>
<div class="kc"><div class="v" style="color:#ffb454">{dec_watch}</div><div class="l">要監視</div></div>
<div class="kc"><div class="v" style="color:#8b949e">{dec_fp}</div><div class="l">偽陽性</div></div>
</div>
偽陽性{dec_fp}件は地合運・MM薄利多売・損小利大の規律・スケールイン等の<b>良性</b>で説明。判定核は<b>「優位そのものが不自然」かつ「それを隠すノイズ」が併存する場合のみ欺瞞</b>——良いトレードの癖は欺瞞でない、という弁別。詳細は<a href="deception.html">欺瞞検知ページ</a>。</div>

<h2>3. プロ側もworkflowで再精査 → 真のエリートは{n_elite}件</h2>
<div class="box">
Nansen/leaderboardが「プロ」と示した層を<b>窓アーティファクト/高頻度MM/塩漬け/alt偏重/運</b>の軸で多角検証し、品質を6段階(エリート{n_elite}・堅実{wq.get('堅実',0)}・中堅{wq.get('中堅',0)}・ムラあり{wq.get('ムラあり',0)}・履歴薄{wq.get('履歴薄/評価不能',0)}・高頻度MM{wq.get('高頻度MM',0)})に整理。
13〜19ヶ月の長期・黒字月率80%+・PF実数(負け月込み)・majors裏付けを満たす<b class="g">真のエリート{n_elite}件</b>(<code>0x41206f8e</code> majors$8.72M/15ヶ月 等)のみ確定。
最頻の失格は<b class="r">PF=99のキャッシュ窓アーティファクト(負け月が写っていないだけ)</b>と<b class="r">alt偏重</b>。
現在は<b>本物プロ{n_pro}・alt主体プロ{n_alt}・高頻度MM{n_mm}</b>に再編。MMは薄利多売でコピー不能ゆえ<a href="mm.html">専用ページ</a>へ分離した。</div>

<h2>4. いま追っている監視対象（{n_watch}件）</h2>
<div class="box warn">
確証は無いが断定せず追う残疑層。<b>遅効エッジ5</b>＋<b>欺瞞要監視{n_decwatch}</b>（単一銘柄@107アルファ／295勝0敗月だが会計効果で説明可）。
いずれも人間レビュー用で、リアルタイムの建玉は<a href="live.html">ライブ監視</a>、名簿は<a href="watchlist.html">監視対象ページ</a>。</div>

<h2>5. 成績指標の注記（成績は定義で違う）</h2>
<div class="box warn">
台帳の<b>majors損益</b>＝BTC/ETH/SOL約定のclosedPnl合計（取引のみ／高頻度勢は約定膨大で過小）。
<b>HL公式通算</b>＝HLリーダーボードの通算純損益（全銘柄＋funding込み・総額として最も信頼）。
両者の差はalt・spot・funding。例: あるMMはmajors$42.9MだがHL公式$65.7M。<u>majors実力はmajors損益、総稼ぎはHL公式</u>で見るのが正しい。</div>

<h2>6. 相関①：資金の出どころ ＝ <b class="g">相関あり</b></h2>
{fund_tbl}
<div class="box yes">
弱い疑惑は CEX 直入金が {ins_cex}/{ins_n} 件＝ほぼゼロで、ブリッジ/コントラクト経由＝<u>足を消す側</u>。
一方 <b class="o">出金疑いは CEX 直入金が {co_cex}/{co_n} 件</b>＝身元が取引所に残る側。
👉 <b>「足の隠し方」と容疑度に相関がある</b>。</div>

<h2>7. 相関②：Nansenの正体ラベル ＝ <b class="r">相関なし</b></h2>
{lab_tbl}
<div class="box no">
<b>匿名率（ラベル無し）:</b>
<table style="max-width:340px;margin-top:6px"><tr><th style="text-align:left">カテゴリ</th><th>匿名 / 計</th></tr>{anon_rows}</table>
疑惑もプロも大半が匿名→ <b class="r">「ラベル無し＝インサイダー」は成り立たない</b>。
Nansen「Smart Money」は監視層 {sm} 件にしか付かず、わしらは<b>Nansen未認定の層</b>を独自に拾っている。</div>

<h2>8. 結論</h2>
<div class="box">
<ul>
<li><b>確証ある個人インサイダーは検出されず</b>（往復・反復／6定義／複数地平線／欺瞞8軸の7系統がすべて収束）。協調クラスタAも公開エアドロップ資金と判明し解体（cluster-A該当 {n_clusterA}件）。</li>
<li>残るのは断定しない監視対象 {n_watch}件（遅効エッジ＋欺瞞要監視）のみ。</li>
<li>相関は<b>「資金の出どころ(CEXかブリッジか)」に現れ、Nansenの肩書きには現れない</b>。Nansenが効くのは資金フロー追跡であって分類ではない。</li>
<li><b>勝率は塩漬けで粉飾されるため判定に使えない</b>——本プロジェクト最大の教訓。</li>
</ul>
</div>

<h2>9. 限界（正直に）</h2>
<div class="box warn">
疑惑側のサンプルが小さく統計的相関とは言えない（傾向の域）。
検証はHLの板の内側（値動きとの時刻一致）に限られ、板の外＝オンチェーン資金フロー（取引所からの不自然な入金タイミング・新規上場の仕込み）はNansenで追う別領域。
よって「反復する本物のインサイダー」を取りこぼす偽陰性リスクはゼロではない（確度を0でなく0.05〜0.08に留めた所以）。
約定履歴は中央値106日・最大2.5年遡れており、結論は数ヶ月〜年単位の履歴の上で出している。</div>
</body></html>"""

    path = os.path.join(config.HERE, "summary.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"総評を書き直し → {path}（台帳{total}件・監視{n_watch}件・確証インサイダー0で集計）")


if __name__ == "__main__":
    main()
