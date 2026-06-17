"""Step5: マルチエージェント workflow の多角再評価結果を HTML 資料にする。

入力: workflow 出力JSON（result.lenses / result.synthesis を含む）
出力: data/alt_assessment.json（永続コピー）, alt_view.html
使い方: python step5_altview.py <workflow_output.json>
"""
import os
import sys
import json
import html

import config

HL_ADDR = "https://app.hyperliquid.xyz/explorer/address/{a}"
NANSEN = "https://app.nansen.ai/profiler?address={a}"


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def load_result(path):
    raw = json.load(open(path, encoding="utf-8"))
    # workflow出力は {summary, result:{...}} 形式。result が無ければそのものを使う
    return raw.get("result", raw)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else f"{config.DATA_DIR}/alt_assessment.json"
    res = load_result(src)
    lenses = res.get("lenses", [])
    synth = res.get("synthesis", {})
    reasses = synth.get("reassessments", [])
    headlines = synth.get("headline_findings", [])
    critique = synth.get("method_critique", "")

    # 永続コピー
    with open(f"{config.DATA_DIR}/alt_assessment.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    # insider_likelihood 降順
    reasses = sorted(reasses, key=lambda r: r.get("insider_likelihood", 0), reverse=True)

    catjp = {"insider_suspect": "インサイダー疑惑", "pro_trader": "プロ", "excluded": "除外"}
    agjp = {"disagree": "不一致(覆した)", "partial": "部分", "agree": "一致"}

    def row(r):
        a = r["address"]
        lik = r.get("insider_likelihood", 0)
        ag = r.get("agreement", "")
        lenses_hit = ", ".join(esc(x) for x in r.get("lenses_hit", []))
        bar = int(lik * 100)
        barcolor = "#ff5d6c" if lik >= 0.45 else ("#ffb454" if lik >= 0.25 else "#4ea1ff")
        return f"""
<div class="rcard ag-{esc(ag)}">
  <div class="rhead">
    <span class="lik" style="--c:{barcolor}">{lik:.2f}</span>
    <code class="addr">{esc(a)}</code>
    <span class="orig">{catjp.get(r.get('original_category'),'')}</span>
    <span class="arrow">→</span>
    <span class="ag ag-{esc(ag)}">{agjp.get(ag, ag)}</span>
    <span class="links"><a href="{HL_ADDR.format(a=a)}" target="_blank">HL</a> · <a href="{NANSEN.format(a=a)}" target="_blank">Nansen</a></span>
  </div>
  <div class="likbar"><span style="width:{bar}%;background:{barcolor}"></span></div>
  <div class="verdict">{esc(r.get('alt_verdict',''))}</div>
  <div class="rmeta">指摘レンズ: {lenses_hit or '—'}</div>
  <div class="reason">{esc(r.get('reasoning',''))}</div>
</div>"""

    rows = "".join(row(r) for r in reasses)
    hl_items = "".join(f"<li>{esc(h)}</li>" for h in headlines)
    lens_items = "".join(
        f"<div class='lens'><b>{esc(l.get('key'))}</b>"
        f"<div class='lsum'>{esc(l.get('summary'))}</div></div>"
        for l in lenses
    )
    critique_html = esc(critique).replace("\n", "<br>")

    out_html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>77ウォレット マルチエージェント別視点再評価</title>
<style>
:root{{--bg:#0e1116;--card:#171b22;--mut:#8b949e;--line:#232a34;--acc:#4ea1ff}}
*{{box-sizing:border-box}}
body{{font-family:system-ui,"Segoe UI",sans-serif;background:var(--bg);color:#e6edf3;margin:0;padding:26px;line-height:1.6}}
h1{{font-size:21px;margin:0 0 4px}}
h2{{font-size:16px;margin:24px 0 10px;border-left:3px solid var(--acc);padding-left:8px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:16px}}
.disc{{background:#1f2530;border-left:3px solid #ffb454;padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:18px}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin-bottom:16px;font-size:13px}}
.box ul{{margin:6px 0;padding-left:20px}} .box li{{margin-bottom:8px}}
.lens{{margin-bottom:10px}} .lsum{{color:#cdd6df;font-size:12.5px;margin-top:2px}}
.rcard{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px;border-left:4px solid #4ea1ff}}
.rcard.ag-disagree{{border-left-color:#ff5d6c}}
.rcard.ag-partial{{border-left-color:#ffb454}}
.rcard.ag-agree{{border-left-color:#3fb950}}
.rhead{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px}}
.lik{{font-weight:800;font-size:18px;color:var(--c)}}
.addr{{font-size:12px;color:#c9d1d9;background:#0b0f14;padding:1px 5px;border-radius:4px}}
.orig{{color:var(--mut)}} .arrow{{color:var(--mut)}}
.ag{{font-weight:700;font-size:12px;padding:1px 8px;border-radius:10px}}
.ag-disagree{{color:#ff8893}} .ag-partial{{color:#ffc06b}} .ag-agree{{color:#69d98a}}
.links{{margin-left:auto}} .links a{{color:var(--acc);text-decoration:none;font-size:12px}}
.likbar{{height:5px;background:#0b0f14;border-radius:3px;margin:7px 0;overflow:hidden}}
.likbar span{{display:block;height:100%}}
.verdict{{font-weight:600;margin:4px 0;font-size:13.5px}}
.rmeta{{color:var(--mut);font-size:11.5px;margin-bottom:4px}}
.reason{{font-size:12.5px;color:#cdd6df}}
code{{font-family:ui-monospace,monospace}}
.legend{{font-size:12px;color:var(--mut);margin-bottom:10px}}
b.red{{color:#ff8893}}
</style></head><body>
<h1>77ウォレット マルチエージェント「別視点」再評価</h1>
<div class="sub">5つの独立レンズ（トレンド補正 / 協調クラスタ / リスク / イベント先行性 / レッドチーム反証）→ 統合 ／ 元の数値分類との突合</div>
<div class="disc"><b>要旨:</b> 数値分類(勝率・的中率)は <b class="red">下落トレンドとクローズ選択バイアスで汚染</b>されており、
最高スコアの偽インサイダーを生んでいた。別視点（特に<b>オンチェーン資金源の追跡＝協調レンズ</b>）が、数値には現れない
<b>同一資金元×同時刻×大口の先行ショート</b>を検出し、真の疑惑を2件に絞り込んだ。確信度は中程度に留めるべき。</div>

<h2>最重要の所見</h2>
<div class="box"><ul>{hl_items}</ul></div>

<h2>ウォレット別 再評価（インサイダー濃度 降順）</h2>
<div class="legend">枠色: <b style="color:#ff8893">赤=数値分類を覆した</b> / <b style="color:#ffc06b">橙=部分</b> / <b style="color:#69d98a">緑=一致</b>。数値=多角分析後のインサイダー濃度(0-1)。</div>
{rows}

<h2>5レンズの所見</h2>
<div class="box">{lens_items}</div>

<h2>既存手法の弱点と多角化の効果（統合エージェントの講評）</h2>
<div class="box">{critique_html}</div>

</body></html>"""

    out = os.path.join(config.HERE, "alt_view.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(f"完了 → {out}（再評価 {len(reasses)} 件 / レンズ {len(lenses)}）")


if __name__ == "__main__":
    main()
