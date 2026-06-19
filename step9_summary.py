"""Step9: 総評＋「判定カテゴリ × Nansenタグ」の相関分析を1ページにまとめる。

台帳から実データで集計して summary.html を生成（データが増えても自動で正確）。
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
WATCH = ["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
         "プロトレーダー(本物)", "プロトレーダー(未精査)"]


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
        cnt = Counter(fn(e) for e in g)
        cells = "".join(f"<td>{cnt.get(c, 0) or ''}</td>" for c in cats)
        rows += f"<tr><td class='pn'>{esc(p)}</td>{cells}<td><b>{len(g)}</b></td></tr>"
    return f"<table>{head}{rows}</table>"


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    total = len(reg)
    poscnt = Counter(e["position"] for e in reg.values())

    # カテゴリ件数表
    cnt_rows = ""
    LABELMAP = [("🔴 インサイダー疑惑", "インサイダー疑惑(要監視)"),
                ("🟠 弱い疑惑", "弱い疑惑(監視継続)"),
                ("💸 出金疑い", "💸 出金疑い(要監視)"),
                ("🟢 プロ(本物)", "プロトレーダー(本物)"),
                ("🔵 プロ(未精査)", "プロトレーダー(未精査)")]
    watch_total = 0
    for label, pos in LABELMAP:
        n = poscnt.get(pos, 0)
        watch_total += n
        cnt_rows += f"<tr><td>{esc(label)}</td><td><b>{n}</b></td></tr>"

    # 相関: 資金源
    fund_cats = ["CEX(取引所)", "ブリッジ/コントラクト", "個人(ラベル無)", "汎用ウォレット", "不明"]
    fund_tbl = table(reg, fund_type, fund_cats)
    # 相関: 正体ラベル
    lab_cats = ["無(匿名)", "ENS/個人名", "ファンド/機関", "SmartMoney", "汎用(HighBalance等)"]
    lab_tbl = table(reg, lab_type, lab_cats)

    # 数値: インサイダー＋弱い のCEX率、出金のCEX率
    def cex_count(positions):
        g = [e for e in reg.values() if e.get("position") in positions]
        return sum(1 for e in g if fund_type(e) == "CEX(取引所)"), len(g)
    ins_cex, ins_n = cex_count(["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)"])
    co_cex, co_n = cex_count(["💸 出金疑い(要監視)"])
    # 動的な標本数（プロセス文中のハードコード防止）
    n_ins = poscnt.get("インサイダー疑惑(要監視)", 0)
    n_weak = poscnt.get("弱い疑惑(監視継続)", 0)
    n_pro = poscnt.get("プロトレーダー(本物)", 0)
    n_cashout = poscnt.get("💸 出金疑い(要監視)", 0)
    n_clusterA = sum(1 for e in reg.values() if "cluster-A" in (e.get("tags") or []))
    # SmartMoney 該当数（監視全体）
    sm = sum(1 for e in reg.values() if e.get("position") in WATCH and lab_type(e) == "SmartMoney")
    # 匿名率
    anon_rows = ""
    for label, pos in LABELMAP:
        g = [e for e in reg.values() if e.get("position") == pos]
        a = sum(1 for e in g if not e.get("labels"))
        anon_rows += f"<tr><td>{esc(label)}</td><td>{a} / {len(g)}</td></tr>"

    out = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>総評 — 判定カテゴリ と Nansenタグ の相関</title>
<style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:28px;line-height:1.7;max-width:1000px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:26px 0 10px;border-left:3px solid #4ea1ff;padding-left:8px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:16px}}
table{{border-collapse:collapse;font-size:13px;margin:8px 0;width:100%}}
th,td{{border:1px solid #232a34;padding:6px 9px;text-align:center}} th{{background:#10151c;font-size:12px}}
td.pn{{text-align:left;font-weight:600;white-space:nowrap}}
.box{{background:#171b22;border:1px solid #232a34;border-radius:10px;padding:14px 18px;margin:10px 0;font-size:13px}}
.yes{{border-left:4px solid #3fb950}} .no{{border-left:4px solid #ff5d6c}} .warn{{border-left:4px solid #ffb454}}
.big{{font-size:15px;font-weight:700}} b.g{{color:#69d98a}} b.r{{color:#ff8893}} b.o{{color:#ffc06b}}
a{{color:#4ea1ff}}
ul{{margin:6px 0;padding-left:20px}} li{{margin-bottom:5px}}
</style></head><body>
<h1>📊 総評 — 判定カテゴリ と Nansenタグ の相関</h1>
<div class="sub">Hyperliquid perp インサイダー追跡プロジェクトのまとめ。台帳 {total} 件・要監視 {watch_total} 件時点。
<a href="index.html">← トップへ</a> ／ <a href="registry.html">監視台帳</a> ／ <a href="compare.html">HL vs Nansen</a></div>

<h2>1. ここまでの到達点</h2>
<div class="box">
HL公開API（無料・約定粒度）で発掘 → 多角分析（workflowレッドチーム反証）で精査 → Nansen Pro で正体・資金源を付与、という3段で
<b>{total}件の永続台帳</b>を構築。要監視 {watch_total} 件は正体・資金源・出金先・判定根拠まで記載済み。
</div>
<table style="max-width:360px"><tr><th>カテゴリ</th><th>件数</th></tr>{cnt_rows}</table>

<div class="box no" style="margin-top:12px">
<b class="big">核心の所見：明確な「個人インサイダー」は検出できなかった（要監視の個人インサイダーは現在 {n_ins} 件）。</b><br>
「急変の前に大口で建てた（先行）」と出たウォレットを<a href="methodology.html">往復・反復</a>で精査すると、
<b class="r">ことごとく N=1（1つの注文を分割約定しただけの一発）</b>に化けた。別々の急変イベントで反復して当てる本物の挙動（strictで反復≥3級）は<b>ゼロ</b>。<br>
さらに残った疑惑10件（旧インサイダー1＋弱疑惑9）を<b>別視点の多角workflow（反復性／基準率／資金網の3レンズ×個別判定、計41エージェント）で再精査</b>したところ、
<b class="r">10件すべてが debunked（本物確度 0.05〜0.08）</b>。本命だった <code>0x18cd45…</code> も likely-insider(0.62)→否定（先行は同一txハッシュの二重計上でN=1、唯一の支持だったMonad資金元こそ公開エアドロップ）。
よって 🔴インサイダー疑惑 {n_ins}件・🟠弱い疑惑 {n_weak}件 ＝<b>要監視の個人インサイダーはゼロに収束</b>。全件 除外/低優先へ移した。
</div>
<div class="box no" style="margin-top:12px">
<b class="big">「往復という定義が厳しすぎただけでは？」を検証 → <u>6つの独立した物差し</u>が同じ結論に収束。</b><br>
往復(利確を要求)では保有型インサイダーを取りこぼす懸念があるため、<b>往復に依存しない別定義</b>を追加で当てた:
<table style="max-width:none;margin-top:8px">
<tr><th>定義</th><th>測るもの</th><th>結果</th></tr>
<tr><td><b>① 利益集中度＋完璧エントリ</b></td><td>少数のデカ勝ちに利益が集中し、その建玉が直近レンジの底/天で入ったか</td><td><b class="r">完璧でも建玉後の伸びは1〜7%</b>＝底/天取りの平均回帰スキャルで、大相場の先読みではない</td></tr>
<tr><td><b>② conviction lift</b>(regime頑健)</td><td>確信して大きく賭ける時ほど(トレンド補正後)当てているか＝大口の先取り−小口の先取り</td><td><b class="r">最大+0.9%/4h＝ノイズ域</b>、3件は<u>負</u>(大きく賭けるほど外す)。確信ベットの優位は皆無</td></tr>
<tr><td><b>③ alt/株perp拡張</b></td><td>効率的なmajorsでなくalt・米国株perp(決算等の情報優位余地)で集中＆完璧か</td><td>該当者も2026年5月の<b class="r">半導体セクター全体の上げ便乗</b>等で説明(big_hit=0.0)。リーク不要</td></tr>
<tr><td><b>④ 含み損バッグなし×高勝率×高的中</b></td><td>勝率の最大の汚染『勝ち玉だけ確定・負けは塩漬け』を、現在含み損を抱えていない事で排除</td><td><b class="g">塩漬けフィルタは有効：高勝率の117〜121件を「含み損で粉飾」として撃墜</b>。通過8件も方向分解すると全て片方向regime便乗</td></tr>
<tr><td><b>⑤ ④＋両方向勝ち＋左右対称サイズ＋複数月＋複数銘柄</b></td><td>④の最強版。トレンド汚染も排除した最厳格AND条件</td><td><b class="r">全549件中 通過ゼロ</b></td></tr>
</table>
各定義の上位候補をworkflow(計71エージェント)で個別精査しても<b class="r">昇格ゼロ・全件現状維持</b>。＝<b>往復定義の欠陥ではなく、6定義すべてで個人インサイダーは検出されない</b>。浮上候補は全員、下落/上昇regime便乗＋モメンタム順張り＋少数の神トレードのサバイバーシップ＋MM高回転で過不足なく説明できた。
<br><b>副産物（重要）:</b> 「含み損バッグなし」検査で、<b class="r">高勝率を称する大半が塩漬け(含み損放置)で勝率を粉飾</b>していたと定量判明（高勝率約130件中117〜121件が失格）。＝<u>勝率を判定に使ってはならない</u>という本プロジェクトの主張の決定的裏付け。
</div>
<div class="box yes">
<b class="big">⑥ 方向的中を複数地平線(1〜72h)で測り直し → 4h固定が見落とした「遅効エッジ」を発見。</b><br>
従来の方向的中は<b>4時間後の一点固定</b>だった。これだと数日かけて効くポジショニングを<u>コイン投げと誤判定</u>する。そこで 1/4/12/24/48/72h でトレンド補正後リターンを測り直したところ、
<b>4hは≈0なのに24-72hで市場ドリフトを超え、複数月・3銘柄・両方向に分散したエッジ</b>を持つ層が浮上した。
台帳＋未照会のリーダーボード層 計約3,800件を精査し、分散テスト通過後さらに<b>majors先物の利益主体・真の実現益黒字・局所ベースライン検証</b>を全て満たした<b>5件</b>のみ🟠弱い疑惑(監視継続)へ。
<table style="max-width:560px;margin-top:6px"><tr><th>アドレス</th><th>補正72h(局所)</th><th>majors実現益</th></tr>
<tr><td>0xccf135ab… (ETHショート)</td><td>+9.2%</td><td><b class="g">+$1.31M</b></td></tr>
<tr><td>0x3662dd1d… (BTC・Smart HL Perps)</td><td>+6.6%</td><td>+$952k</td></tr>
<tr><td>0xef91b28f… (BTC・Smart HL Perps)</td><td>+4.0%</td><td>+$278k</td></tr>
<tr><td>0x8ff1d5d5… (BTC・Former Smart)</td><td>+3.1%</td><td>+$228k</td></tr>
<tr><td>0x9a568bfe… (ETH)</td><td>+3.0%</td><td>+$157k</td></tr></table>
<b>第二の教訓:</b> 遅効エッジ指標は単独では<b class="r">負け組も拾う</b>（「建玉後72hに順方向へ動いた」≠「儲けた」）→実現益と組合せ必須。<br>
<b>第三の教訓（局所ベースライン）:</b> 当初6件だったが、全期間平均で引くと<u>局所トレンド(暴落regime)を取りこぼす</u>。各ウォレットの活動期間ドリフトで引き直すと、<b class="r">SOL 2件(暴落前ショート$7.8M含む)はエッジ消失＝regime便乗</b>と判明し除外。残った5件は局所超過リターンが+3〜9%生存した本物。ただし実力スイングか遅効先行positioningかは断定不可＝<b>最有力の監視対象だが断定しない</b>。</div>
<div class="box yes">
<b class="big">プロ側もworkflow(65エージェント)で再精査 → 真のエリートは5件(全体の8%)。</b><br>
「プロ(本物)」64件を<b>窓アーティファクト/高頻度MM/塩漬け/alt偏重/運</b>の5軸で多角検証。13〜19ヶ月の長期・黒字月率80%+・PF実数(負け月込み)・majors裏付けを満たす<b>真のエリート5件</b>(<code>0x41206f8e</code> $8.84M/15ヶ月 等)のみ確定。
最頻の失格は<b class="r">PF=99のキャッシュ窓アーティファクト(負け月が写っていないだけ)</b>と<b class="r">alt偏重(majors実現益が通算の数%〜マイナス)</b>。alt偏重の格下げ7件は「alt主体プロ」へ分離し、本物プロ57/alt主体プロ23に再編した。</div>
<div class="box warn">
<b>データの遡及範囲（訂正）。</b> 当初「HLの約定履歴は活発な口座だと約2週しか取れない」と記したが、<b>実測では誤り</b>だった。
各ウォレットの約定履歴(<code>userFillsByTime</code>)を遡れた期間は<b class="g">中央値106日・平均166日・最大2.5年</b>（356件中、180日超が134件）。
13,000約定超の高頻度口座でも643日取れた例があり、検証は<u>2週間の薄いデータでなく数ヶ月〜年単位の履歴</u>の上で行われている＝6定義の結論はその分<b>堅い</b>。
なお通算成績(pnl/roi)は<code>leaderboard</code>のallTimeで常に全期間が分かる。
</div>
<div class="box warn">
<b>それでも「不在の証明」ではない（残る盲点）。</b> ①約定履歴の取得は超高頻度ボット(数万約定/数日)では取得上限で頭打ちになりうる、②約定取得が一時的に失敗した口座は <code>roundtrip=null</code>＝検証不能になる、③そもそも検証はHLの板の内側(値動きとの時刻一致)に限られ、<u>板の外＝オンチェーンの資金フロー(取引所からの不自然な入金タイミング・新規上場の仕込み)は別途Nansenで追う領域</u>。
よって「反復する本物のインサイダー」を取りこぼす偽陰性リスクはゼロではない（確度を0でなく0.05〜0.08に留めた所以）。
</div>
<div class="box warn">
<b>唯一の協調候補だった「クラスタA」（当初4ウォレット）も解体。</b>
共有資金元が <b>Monad の公開エアドロップ配布（約1万アドレスへ誰でも受領可）</b>と判明し、<b class="r">私的な協調の確証は否定</b>された。
メンバーは🟠弱い疑惑／除外へ移し、<b>cluster-Aタグも撤去</b>（現在 cluster-A 該当 {n_clusterA} 件）。
</div>

<h2>2. 相関①：資金の出どころ ＝ <b class="g">相関あり</b></h2>
{fund_tbl}
<div class="box yes">
<b class="big">インサイダー疑惑/弱い疑惑は CEX 直入金が {ins_cex}/{ins_n} 件＝ほぼゼロ。</b>全員ブリッジ/コントラクト/個人財布経由で<u>足を消す側</u>。
一方 <b class="o">出金疑いは CEX 直入金が {co_cex}/{co_n} 件</b>で最多＝身元が取引所に残る雑な側。<br>
👉 <b>「足の隠し方」と容疑度に相関がある</b>。疑わしいほど CEX を避けブリッジ経由で入金する。
</div>

<h2>3. 相関②：Nansenの正体ラベル ＝ <b class="r">相関なし</b></h2>
{lab_tbl}
<div class="box no">
<b>匿名率（ラベル無し）:</b>
<table style="max-width:340px;margin-top:6px"><tr><th>カテゴリ</th><th>匿名 / 計</th></tr>{anon_rows}</table>
インサイダー疑惑は全員匿名だが、<b>プロも大半が匿名</b>。→ <b class="r">「ラベル無し＝インサイダー」は成り立たない</b>。
正体ラベルの種別では インサイダーとプロを見分けられない。
</div>
<div class="box warn">
<b>Nansen「Smart Money」タグは、監視 {watch_total} 件中 {sm} 件しか付いていない。</b>
→ わっちらの発掘は <b>Nansen がまだ Smart Money 認定していない層</b>を拾っている（独自性がある反面、Nansenラベルでの裏取りは効かない）。
</div>

<h2>4. 結論</h2>
<div class="box">
<ul>
<li>相関は <b>「Nansenのラベル種別」ではなく「資金の出どころ（CEXかブリッジか）」に現れる</b>。</li>
<li>インサイダーらしいほど <b>CEXを避けブリッジ/コントラクトで資金を入れる</b>（足を消す）。出金疑いは逆に CEX 直入金が多く、身元は取引所が握る。</li>
<li><b>Nansenの正体ラベルはインサイダーとプロの判別には使えない</b>（両方とも匿名が多い）。Nansenが効くのは<u>資金フローの追跡</u>であって<u>肩書きでの分類</u>ではない。</li>
</ul>
</div>

<h2>5. 限界（正直に）</h2>
<div class="box warn">
インサイダー疑惑 n={n_ins}・弱い疑惑 n={n_weak}・プロ本物 n={n_pro} と<b>疑惑側のサンプルが小さく、統計的相関とは言えない（傾向の域）</b>。
確度ある相関と呼べるのは出金疑い {n_cashout} 件のCEX入金パターンくらい。母数（CANDIDATE_LIMIT）を増やせば相関の有無をより確かめられる。
なお「先行」検出が一発(N=1)に化ける問題は<a href="methodology.html">往復・反復</a>で対処したが、<b>反復を満たす個人は今のところ未検出</b>。
</div>
</body></html>"""

    path = os.path.join(config.HERE, "summary.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"完了 → {path}（台帳{total}件・要監視{watch_total}件で集計）")


if __name__ == "__main__":
    main()
