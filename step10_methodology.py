"""Step10: 方法論・読み方ガイド(methodology.html)を生成。閾値はconfigから反映。

「先行」の判定中身・限界、分類の考え方(行動で決めラベルでは決めない)、
勝率が基準でない理由、資金源の相関、用語集 を1ページに。
"""
import os
import config

C = config


def main():
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>方法論・読み方ガイド — どう判定しているか / 何を証明しないか</title>
<style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:28px;line-height:1.75;max-width:920px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:26px 0 10px;border-left:3px solid #4ea1ff;padding-left:8px}}
h3{{font-size:14px;margin:16px 0 6px;color:#9fb4d8}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:16px}}
.box{{background:#171b22;border:1px solid #232a34;border-radius:10px;padding:14px 18px;margin:10px 0;font-size:13.5px}}
.warn{{border-left:4px solid #ffb454}} .ok{{border-left:4px solid #3fb950}} .bad{{border-left:4px solid #ff5d6c}}
ol,ul{{margin:6px 0;padding-left:22px}} li{{margin-bottom:6px}}
code{{background:#0b0f14;padding:1px 5px;border-radius:4px;font-size:12px}}
.eg{{background:#0f1b17;border-left:3px solid #3fb950;border-radius:5px;padding:8px 12px;font-size:13px;margin:6px 0}}
table{{border-collapse:collapse;font-size:13px;width:100%;margin:8px 0}}
th,td{{border:1px solid #232a34;padding:6px 9px;text-align:left}} th{{background:#10151c}}
a{{color:#4ea1ff}} b.r{{color:#ff8893}} b.g{{color:#69d98a}}
</style></head><body>
<h1>方法論・読み方ガイド</h1>
<div class="sub">このプロジェクトが「インサイダー疑惑 / プロ」をどう判定しているか、そして<b>何を証明していないか</b>。
<a href="index.html">トップ</a> ・ <a href="summary.html">総評</a> ・ <a href="registry.html">台帳</a></div>

<div class="box warn"><b>大前提:</b> 本ツールの出力は <b>「統計的に不自然・タイミングが良すぎる取引を “人間レビュー用” にフラグ立てした疑い」</b>であって、
<b class="r">インサイダー取引の証明ではない</b>。「疑惑(要監視)」は容疑であり有罪判定ではない。</div>

<h2>1. 全体の流れ（行動で分類・ラベルでは分類しない）</h2>
<div class="box">
<ol>
<li><b>発掘</b>: Hyperliquid公開API（無料・約定粒度）で高成績ウォレットを集める。</li>
<li><b>行動分析</b>: 各ウォレットの<b>実際の約定</b>から「実現損益・勝率・方向的中率・先行建玉・保有時間」を算出。</li>
<li><b>分類</b>: 上記の<b>行動</b>でインサイダー疑惑/プロ/除外を決める。</li>
<li><b>補助</b>: Nansenで正体・資金源を付与（あくまで参考タグ。<b>ラベルで分類は決めない</b>——理由は<a href="summary.html">総評</a>参照）。</li>
</ol>
</div>

<h2>2. 「先行(せんこう)」とは何か — 判定の中身</h2>
<div class="box">
「先行建玉」＝ <b>価格が動く前に、動く方向へ大口を建てた</b> という<b>時刻と方向の一致</b>を機械的に拾ったもの。手順:
<ol>
<li><b>急変イベントの定義</b>: BTC/ETH/SOLの1時間足で「<b>{C.EVENT_WINDOW_H}時間で±{C.EVENT_MOVE_PCT}%以上</b>」動いた区間を急変とし、その<b>開始時刻 t0</b> と方向を記録。</li>
<li><b>先行の判定</b>: そのウォレットの新規建玉のうち、<b>規模 ${C.LARGE_TRADE_USD:,}以上</b>・<b>t0 の{C.LEAD_WINDOW_H}時間前〜t0</b>・<b>イベントと同方向</b> を満たすものを「先行」と数える。</li>
<li><b>方向的中率</b>: 各エントリの<b>{C.HIT_HORIZON_H}時間後</b>に価格が建玉方向へ動いた割合（先読みの“質”の指標）。</li>
</ol>
<div class="eg">例: ETHを18:31に$20万ショート → 22:00から-4%下落。＝下落の3.5時間前に下落方向へ大口 → 「先行」と判定。</div>
</div>

<h2>3. 「先行」が<b class="r">証明しないこと</b>（最重要）</h2>
<div class="box bad">
先行は<b>相関（タイミングの一致）であって、因果でも内部情報の証拠でもない</b>。同じパターンは次でも起きる:
<ul>
<li><b>偶然</b>: 毎日大量に売買するウォレットは、確率的にどこかで急変の前に建てている。</li>
<li><b>トレンド便乗</b>: 下落相場でショート偏重なら、下落イベントに自動的に方向一致する。</li>
<li><b>自己実現</b>: 大口が建てたこと自体が価格を動かした可能性。</li>
<li><b>恣意的な閾値</b>: {C.EVENT_WINDOW_H}h/{C.EVENT_MOVE_PCT}%/{C.LEAD_WINDOW_H}h/${C.LARGE_TRADE_USD:,} は本ツールが決めた値。変えれば結果も変わる。</li>
</ul>
</div>

<h2>4. だから単独では使わない — 偽陽性対策</h2>
<div class="box ok">
「先行」を3つで補強し、偶然・便乗を排除する:
<ul>
<li><b>方向的中率</b>: 低い(≈ランダム)なら先行は偶然 → 降格。実際に的中9〜13%の2件を偽陽性として除外した。</li>
<li><b>反復性</b>: 1回でなく繰り返し先行しているか。</li>
<li><b>協調</b>: 同じ<b>私的</b>資金元の仲間と<b>同一イベント・同時刻・同方向</b>（＝cluster-Aの決め手）。<br>
  ※ Gas.zip/Binance等の<b>公共サービス共有はノイズ</b>（誰でも使う）で協調の証拠にならない。</li>
</ul>
最有力のcluster-A 2件が強いのは「先行 ＋ 的中{int(C.INSIDER_DIR*100)}%超 ＋ 協調」が<b>揃った</b>から。
</div>

<h2>5. 「往復」と「反復」 — 一発と本物を分ける核心</h2>
<div class="box">
「先行」を1回拾っただけでは偶然と区別できない。そこで<b>「往復(ラウンドトリップ)」</b>と<b>「反復」</b>で裏を取る。
<h3>① 往復(ラウンドトリップ) = 1イベントに対する判定</h3>
ある急変イベントについて、<b>両方</b>を満たすと「往復1回」:
<ul>
<li><b>先行</b>: 急変の<b>前</b>(lead窓内)に、正しい方向へ<b>大口</b>で建てた</li>
<li><b>利確</b>: 急変の<b>後</b>(exit窓内)に、その方向を<b>手仕舞った</b></li>
</ul>
＝「建てる→急変→抜ける」がワンセット。情報で入って出尽くしで逃げる、インサイダーの典型挙動。
<h3>② 反復 = 往復が成立した「別々のイベント」の数</h3>
ここが核心。<b class="r">同じ1イベントを何度も数えない</b>。分割約定は <code>(銘柄, 方向, 1時間バケット)</code> で束ねて<b>1回に集約</b>し、N=1の水増しを排除する。
判定は3段階の窓で行う:
<table>
<tr><th>段階</th><th>lead窓(前)</th><th>exit窓(後)</th><th>大口閾値</th></tr>
<tr><td>strict</td><td>6h</td><td>12h</td><td>$100,000</td></tr>
<tr><td>medium</td><td>12h</td><td>24h</td><td>$50,000</td></tr>
<tr><td>loose</td><td>24h</td><td>48h</td><td>$25,000</td></tr>
</table>
<b>正規化率(norm_rate) = 往復回数 ÷ 大口の賭け総数</b>。「大口で賭けたうち何割がイベント往復に化けたか」。高いほど“狙って当てている”。
<div class="eg"><b>判別の要点</b><br>
・往復が<b>1イベント分だけ</b> → 偶然・一発の好タイミングで説明できる（N=1）<br>
・往復が<b>複数イベントで反復</b> → 偶然では説明しにくい ＝ インサイダー疑い濃厚<br>
インサイダー認定の目安は「strict で<b>反復≥3</b>」級。</div>
</div>
<div class="box bad">
<b>実例 — なぜ反復が要るか（0xbcc7…）:</b> 先行$1.43Mと出て一見有望だったが、先行例を分解すると
<b>全て同一ハッシュ・同一時刻(2026-02-05 20:24)・同一イベント</b>＝<b class="r">1つの注文を分割約定しただけ</b>。
集約すると反復は実質1。勝率も49%。よって「一発当てた」に過ぎず<b>弱疑惑(監視継続)止まり</b>とした。
これまで検出された“怪しい先行”は精査すると<b>ことごとくN=1(一発)</b>に化けた、というのが本プロジェクトの一貫した所見。
</div>

<h2>6. 勝率は判定基準ではない</h2>
<div class="box warn">
勝率が高くても意味がない場合が多い:
<ul>
<li><b>勝ち玉だけ確定・負けは塩漬け</b>すれば勝率100%は簡単に作れる（実現益に負けが出ない）。</li>
<li>下落相場でショート偏重なら自然に勝率が上がる。</li>
</ul>
実際、<a href="registry.html">除外</a>には<b>勝率0.9以上が61件</b>含まれる（実現益が小さい/MM/履歴なし等の別理由で除外）。
見るべきは「<b>実際にいくら実現したか・MMでないか・先行があるか</b>」という行動の質。
</div>

<h2>7. 分類の定義（閾値）</h2>
<table>
<tr><th>分類</th><th>条件（行動）</th></tr>
<tr><td>🔴 インサイダー疑惑</td><td>急変直前の先行建玉 ＋ 高い方向的中率（{int(C.INSIDER_DIR*100)}%超）／または協調。＝動く前に正しく賭けた疑い</td></tr>
<tr><td>💸 出金疑い</td><td>通算PnL ÷ 現在残高 が {C.CASHOUT_RATIO}倍以上（大きく稼いで引き上げた hit-and-run）</td></tr>
<tr><td>🟢 プロ(本物)</td><td>多数の取引で安定黒字・先行なし（実力）。HL実測の実現益で裏取り</td></tr>
<tr><td>⚙ MM/HFT（除外）</td><td>約定が膨大（クローズ&gt;{C.MM_MAX_CLOSES:,} or 約定&gt;{C.MM_MAX_FILLS:,}）＝自動売買</td></tr>
</table>

<h2>8. 資金源の相関（実データで判明）</h2>
<div class="box">
インサイダー疑惑ほど <b>CEX(取引所)からの直接入金を避け、ブリッジ/コントラクト経由</b>で資金を入れる（＝足を消す）。
逆にプロ/出金疑いは CEX 直入金が多く、身元は取引所が握る。
<b>Nansenが効くのは「資金フローの追跡」であって「肩書き(ラベル)での分類」ではない。</b>
</div>

<h2>9. 構造的な限界</h2>
<div class="box bad">
<ul>
<li><b>銘柄</b>: 先行・的中の検証は BTC/ETH/SOL のみ。alt中心のウォレットは検証しきれない。</li>
<li><b>HL約定の保持</b>: 活発なウォレットは直近~2週しか約定が取れない（古い分は間引かれる）。</li>
<li><b>Nansenラベル</b>: 実名特定ではなく挙動分類。CEX入金の先の本人は取引所しか知らない。</li>
<li><b>サンプル</b>: インサイダー疑惑は少数。統計的断定でなく「傾向・要レビュー」。</li>
</ul>
</div>

<div class="sub" style="margin-top:18px">要するに: <b>「先行」＝『動く前の{C.LEAD_WINDOW_H}時間以内に動く方向へ大口を建てた』というルールに当てはまっただけ</b>。
疑いの入口であり、的中率・反復・協調・資金源で裏を取って初めて意味を持つ。それでも証明ではなく「要監視」。</div>
</body></html>"""
    out = os.path.join(C.HERE, "methodology.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"完了 → {out}")


if __name__ == "__main__":
    main()
