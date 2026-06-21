"""Step11: インサイダー検知に用いた全手法の体系カタログ → methods.html。

このプロジェクトで試した検知手法・汚染除去・オンチェーン文脈・マルチエージェント検証を
1ページに整理。各手法の「測るもの／閾値・手順／結果・弱点」を記す。
"""
import os
import json
from datetime import datetime, timezone
from collections import Counter

import config


def main():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    pc = Counter(e["position"] for e in reg.values())
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 行動シグナル系（HL約定から）
    behav = [
        ("① 高勝率×高方向的中（v0・最初の定義）",
         "方向的中率(エントリ4h後)≥80% かつ 勝率≥80% かつ クローズ≥5・エントリ≥10・黒字",
         "<b class='r'>廃止</b>。トレンド(下落相場のショート偏重)＋選択バイアス(勝ち玉だけ確定・負けは塩漬け)で汚染し、偽インサイダーを最上位に上げた。最も偽陽性を生む手法だった。"),
        ("② イベント先行（event lead）",
         f"急変(4h±{config.EVENT_MOVE_PCT}%)の{config.LEAD_WINDOW_H}時間前以内に ${config.LARGE_TRADE_USD:,}以上を同方向で建てた notional",
         "時刻の一致であって情報の証明でない。分割約定でN=1水増し。単独では使えず補助シグナル。"),
        ("③ 往復（round-trip）",
         "1つの急変イベントで「先行建て→急変後に同方向を利確」が成立＝1回",
         "利確を要求するため<u>保有型インサイダーを取りこぼす</u>。"),
        ("④ 反復（repetition）★中核",
         "別々の急変イベントで往復が成立した回数。分割約定は(銘柄,方向,1時間)で集約しN=1を排除。strict(6h/12h/$100k)〜loose(24h/48h/$25k)の3段＋正規化率",
         "偶然の一発と本物を分ける核。<b class='r'>実測=反復≥3級は該当ゼロ</b>。先行はことごとくN=1に化けた。"),
        ("⑤ conviction lift（regime頑健）",
         "各エントリの建玉後リターンをトレンド補正し、大口notional群の先取り − 小口群の先取り",
         "同一口座の大小比較ゆえトレンド相殺＝regime頑健。<b class='r'>観測上の最大+0.9%/4h＝ノイズ域</b>、3件は負。確信ベットの優位は皆無。"),
        ("⑥ 利益集中度＋完璧エントリ",
         "総勝ち益に占めるtop3割合(集中) ＋ 上位益トレードが建玉前24hレンジの底/天で入ったか(完璧0-1) ＋ 建玉後固定24hの伸び",
         "完璧でも<b class='r'>伸び1〜7%＝平均回帰スキャル</b>で大相場の先読みでない。※当初バグ(窓が保有期間を含み勝ちは必ず完璧に見える)を固定窓に修正。"),
        ("⑦ alt / 株perp 拡張",
         "⑥を全perp(alt・米国株perp含む)へ拡大。alt勝ち益比率も算出",
         "majorsは効率市場で情報優位が出にくいalt/株perpへ拡張。該当者も2026年5月の半導体セクター全体の上げ便乗等で説明(big_hit=0.0)。"),
        ("⑧ 含み損なし×高勝率×高的中×1週間+（塩漬け排除）",
         "勝率の最大汚染『負けを塩漬け』を、現在含み損が口座10%以下で排除した上での高勝率(≥0.70)・高的中(≥0.65)",
         "<b class='g'>塩漬けフィルタは有効：高勝率の117〜121件を「含み損で粉飾」として撃墜</b>。通過8件も方向分解で全て片方向regime便乗。"),
        ("⑨ 厳格AND版",
         "⑧ ＋ 両方向勝ち(L/S両dir≥0.6) ＋ 左右対称サイズ ＋ 複数月 ＋ 複数銘柄",
         "トレンド汚染も排除した最厳格条件。<b class='r'>全549件中 通過ゼロ</b>。"),
        ("⑩ 過去塩漬け履歴（段階タグ）",
         "約定から建玉を復元し、保有中の1h足closeで「含み損≥口座10%」が連続した最長日数。1/2/3/4/5/6/7日以上で段階タグ",
         "現在スナップでは見抜けぬ「過去に長期塩漬け→解消して今は綺麗」を炙る。プール内で7日以上塩漬けが16件。"),
        ("⑪ 複数地平線の方向的中（1〜72h）★突破口",
         "方向的中を1/4/12/24/48/72hで測りトレンド補正。4hは≈0でも24-72hで市場ドリフトを超える「遅効エッジ」を検出。時間/銘柄/方向の分散テストで篩う",
         "<b class='g'>4h固定が見落としていた遅効エッジを発見</b>。台帳549＋未照会1500件から、majors実現益も黒字＋局所ベースライン検証を通った<b>5件</b>を監視へ(当初6件→SOL地合便乗1件除外)。<br><b class='r'>教訓:「建玉後72hに順方向」≠「儲けた」</b>(分散通過18件中12件はmajors赤字の偽陽性)＝シグナルは実現損益と必ず組合せ。"),
        ("⑫ hit-and-run（集中kill→出金）",
         "all-coinで建玉を復元し、利益のtop3が70%以上＝単一銘柄(多くはalt)に集中して大勝ち→撤退",
         "典型的なインサイダー撤退パターン。出金疑い12件から<b>4件</b>を抽出(SOL 2025-10で$5.35M等)。altは情報非対称が大きく最有力の精査対象。"),
    ]

    # 汚染除去・補正
    fix = [
        ("方向的中/勝率の汚染特定",
         "① 市場トレンド(下落相場でショート偏重→自動的に高的中) ② クローズ選択バイアス(勝ち玉だけ確定・負けは塩漬け)。＝<u>勝率は判定に使ってはならない</u>"),
        ("N=1水増しの排除", "分割約定を(銘柄,方向,1時間)バケットで1回に集約。「一発当てた」と「反復して当てる」を区別。"),
        ("regime頑健化", "conviction lift(同一口座の大小比較)・detrend(地平線別の平均リターンを控除)でトレンド成分を除去。"),
        ("塩漬けの定量検出", "clearinghouseの未実現損益(現在)＋約定履歴の建玉復元(過去)の両面で含み損バッグを実測。"),
        ("エッジ≠利益", "挙動シグナル(完璧エントリ・遅効リターン)は単独では負け組も拾う。<b>必ず実現損益と組み合わせて</b>評価する。"),
    ]

    # Nansen（オンチェーン文脈）
    nansen = [
        ("資金源（First Funder）", "CEX直入金(身元残る) か ブリッジ/コントラクト経由(隠蔽) か。<b class='g'>容疑度と相関ありと判明</b>(疑わしいほどCEXを避ける)。"),
        ("協調クラスタ（cluster-A）", "同一資金元の名寄せ。だが共有元は<b class='r'>公開エアドロップ(約1万アドレス配布)と判明し否定</b>。「資金共有≠協調」。"),
        ("取引相手（counterparties）", "出金先・OTC接点・入出金額。撤退先の追跡。"),
        ("正体ラベル", "SmartMoney/機関/ENS等。<b class='r'>分類には使えない</b>(インサイダーもプロも大半が匿名)。"),
        ("出金疑い（cashout ratio）", "通算PnL ÷ 現在残高 が大(10倍超)＝稼いで引き上げ。⑫のhit-and-run判定と組合せ。"),
    ]

    # マルチエージェント検証（workflow）
    wf = [
        ("insider-pro-analysis（5レンズ）", "トレンド補正/協調/リスク/イベント先行/レッドチーム反証で77件を再評価。"),
        ("insider-recheck-v2（3レンズ・41体）", "反復性/基準率/資金網×個別判定。疑惑10件→<b class='r'>全件debunked</b>。"),
        ("insider-newdef-scrutiny（21体）", "新定義候補20件の個別精査→<b class='r'>昇格ゼロ</b>。"),
        ("clean-winrate-scrutiny（9体）", "塩漬け除外の高勝率8件のトレンド汚染精査→<b class='r'>全件トレンド汚染</b>。"),
    ]

    def rows3(items):
        return "".join(f"<tr><td class='m'>{m}</td><td>{d}</td><td>{r}</td></tr>" for m, d, r in items)

    def rows2(items):
        return "".join(f"<tr><td class='m'>{m}</td><td>{d}</td></tr>" for m, d in items)

    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>インサイダー検知 手法カタログ</title>
<style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:28px;line-height:1.65;max-width:1080px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:26px 0 8px;border-left:3px solid #4ea1ff;padding-left:8px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:14px}} a{{color:#4ea1ff}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}}
th,td{{border:1px solid #232a34;padding:8px 10px;text-align:left;vertical-align:top}}
th{{background:#10151c;font-size:12px}}
td.m{{font-weight:600;white-space:nowrap;min-width:230px}}
b.r{{color:#ff8893}} b.g{{color:#69d98a}} code{{background:#0b0f14;padding:1px 5px;border-radius:4px;font-size:12px}}
.box{{background:#171b22;border:1px solid #232a34;border-radius:10px;padding:12px 16px;margin:8px 0;font-size:13px}}
.concl{{border-left:4px solid #3fb950}}
ul{{margin:4px 0;padding-left:20px}}
</style></head><body>
<h1>🧪 インサイダー検知 手法カタログ</h1>
<div class="sub">このプロジェクトで「perpインサイダーを見極める」ために試した全手法の一覧。<a href="index.html">← トップ</a> ／ <a href="methodology.html">方法論</a> ／ <a href="summary.html">総評</a>　更新 {gen}</div>

<div class="box concl">
<b>全体の結論:</b> 行動シグナル6定義＋複数地平線で精査した結果、<b class='r'>確証ある個人インサイダーはゼロ</b>。
ただし4h固定で見落としていた<b>「遅効エッジ」黒字5件</b>を監視対象として残す(出金集中のhit-and-run候補は現在0件)。
現位置分布: プロ本物 {pc.get('プロトレーダー(本物)',0)} ／ 弱い疑惑(遅効エッジ) {pc.get('弱い疑惑(監視継続)',0)} ／ 出金疑い(hit-and-run) {pc.get('💸 出金疑い(要監視)',0)} ／ 除外 {pc.get('除外/低優先',0)}。
</div>

<h2>A. 行動シグナル系（HL約定から検出）</h2>
<table><tr><th>手法</th><th>測るもの／手順</th><th>結果・弱点</th></tr>{rows3(behav)}</table>

<h2>B. 汚染除去・補正（手法の信頼性を支える発見）</h2>
<table><tr><th>項目</th><th>内容</th></tr>{rows2(fix)}</table>

<h2>C. オンチェーン文脈（Nansen REST）</h2>
<table><tr><th>取得物</th><th>用途・知見</th></tr>{rows2(nansen)}</table>

<h2>D. マルチエージェント検証（workflow）</h2>
<table><tr><th>workflow</th><th>内容・結果</th></tr>{rows2(wf)}</table>

<h2>E. 支える基盤</h2>
<div class="box">
<ul>
<li><b>約定キャッシュ</b>(<code>hl_fills_cache.py</code>): HL約定を <code>data/fills/</code> に永続保存＋増分取得。再取得の撲滅＋HL間引き対策(一度貯めれば古いfillも永久保持)。現在1100件超・1100万約定超。</li>
<li><b>永続台帳</b>(<code>wallet_registry.json</code>): アドレス単位でupsert。位置・タグ・Nansenデータ・履歴を蓄積。</li>
<li><b>発掘の主軸=HL公開API</b>(無料・約定粒度)、<b>正体/資金源の補助=Nansen REST</b>(クレジット型・台帳に永続保存)。</li>
</ul>
</div>

<h2>最重要の教訓</h2>
<div class="box">
<ol>
<li><b>勝率を判定に使うな</b>：高勝率の大半は塩漬け(含み損放置)で粉飾。実現損益で裏取りせよ。</li>
<li><b>「一発」と「反復」を分けよ</b>：分割約定を集約しN=1水増しを排除。本物は別イベントで反復する。</li>
<li><b>単一地平線で見るな</b>：方向的中は1〜72hで測る。4h固定は遅効エッジを見落とす。</li>
<li><b>エッジ≠利益</b>：建玉後に順方向へ動いても儲けたとは限らぬ。シグナルは必ず実現損益と組合せ。</li>
<li><b>ラベルでなく行動で</b>：Nansenの肩書きでは分類できぬ。効くのは資金フローの追跡。</li>
<li><b>断定しない</b>：BTC/ETH/SOLは効率市場。すべて「人間レビュー用の疑い」であって証明ではない。</li>
</ol>
</div>
</body></html>"""
    out = os.path.join(config.HERE, "methods.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"完了 → {out}")


if __name__ == "__main__":
    main()
