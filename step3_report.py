"""Step3: dossiers.json から HTML 単体レポートを生成する。

入力: data/dossiers.json （無ければ data/ranked.json のみで簡易版）
出力: report.html
"""
import os
import json
import html
from datetime import datetime, timezone

import config

HL_ADDR = "https://app.hyperliquid.xyz/explorer/address/{a}"
HYPURR = "https://hypurrscan.io/address/{a}"
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


def flags_for(hl, d):
    """容疑シグナルを箇条書き化（断定でなく根拠の提示）。"""
    fl = []
    if hl.get("likely_mm"):
        fl.append(f"⚠ MM/HFT 疑い（約定{hl.get('n_fills')}件・クローズ{hl.get('n_closes')}件）— スコア大幅減点済。インサイダーより自動マーケットメイクの可能性")
    if (hl.get("win_rate") or 0) >= 0.70 and (hl.get("n_closes") or 0) >= 20:
        fl.append(f"高勝率 {pct(hl['win_rate'])}（{hl['n_closes']}クローズ）")
    if (hl.get("dir_accuracy") or 0) >= 0.85 and (hl.get("n_opens") or 0) >= 20:
        fl.append(f"方向的中率が異常に高い {pct(hl['dir_accuracy'])}（{hl['n_opens']}エントリ）")
    if (hl.get("event_lead_notional") or 0) > 0:
        fl.append(f"急変イベント直前の大口先行エントリ {usd(hl['event_lead_notional'])}")
    if (hl.get("total_pnl") or 0) > 100_000:
        fl.append(f"majors実現＋含み益が大 {usd(hl['total_pnl'])}")
    if d and not d.get("counterparties") and not d.get("labels"):
        fl.append("オンチェーン footprint が希薄（資金源以外の痕跡が少ない＝新規/専用ウォレットの可能性）")
    if d and d.get("first_funders"):
        names = ", ".join(esc(f.get("address_label") or f.get("address", "")[:10]) for f in d["first_funders"][:3])
        fl.append(f"資金源(First Funder): {names}")
    return fl


def render_wallet(rank, d, hl_only=False):
    hl = d.get("hl", {})
    a = d["address"]
    win = hl.get("lb_windows", {}) or {}

    def wstat(name):
        w = win.get(name)
        if not w:
            return "—"
        return f"{usd(w.get('pnl'))} / {pct(w.get('roi'))}"
    labels = ", ".join(
        esc(l.get("label") or l.get("address_label") or l) if isinstance(l, dict) else esc(l)
        for l in (d.get("labels") or [])
    ) or "—"

    # 先行エントリ事例
    lead_rows = ""
    for ex in (hl.get("lead_examples") or []):
        lead_rows += (
            f"<tr><td>{esc(ex['coin'])}</td><td>{esc(ex['dir'])}</td>"
            f"<td>{esc(ex['entry_time'])}</td><td>{usd(ex['notional_usd'])}</td>"
            f"<td>{esc(ex['event_time'])}</td><td>{esc(ex['event_move_pct'])}%</td></tr>"
        )
    lead_tbl = (f"<table class='sub'><tr><th>銘柄</th><th>方向</th><th>エントリ時刻(UTC)</th>"
                f"<th>規模</th><th>急変時刻(UTC)</th><th>変化</th></tr>{lead_rows}</table>"
                if lead_rows else "<p class='muted'>該当なし</p>")

    # 建玉
    held = ""
    for h in (hl.get("held_positions") or []):
        held += (f"<li>{esc(h['coin'])} {esc(h['side'])} 建玉価値{usd(h['position_value'])} "
                 f"含み益{usd(h['unrealized_pnl'])}</li>")
    held = f"<ul>{held}</ul>" if held else "<span class='muted'>なし</span>"

    # 資金源・関連
    funders = ""
    for f in (d.get("first_funders") or []):
        lbl = esc(f.get("address_label") or "")
        funders += (f"<li><code>{esc(f.get('address',''))}</code> {lbl} "
                    f"<span class='muted'>({esc(f.get('relation',''))})</span></li>")
    funders = f"<ul>{funders}</ul>" if funders else "<span class='muted'>不明</span>"

    # 取引相手
    cps = ""
    for c in (d.get("counterparties") or [])[:8]:
        lbl = c.get("counterparty_address_label")
        lbl = ", ".join(lbl) if isinstance(lbl, list) else (lbl or "")
        cps += (f"<li>{esc(lbl) or esc(c.get('counterparty_address','')[:12])} "
                f"<span class='muted'>{usd(c.get('total_volume_usd'))}</span></li>")
    cps = f"<ul>{cps}</ul>" if cps else "<span class='muted'>なし/希薄</span>"

    flags = "".join(f"<li>{f}</li>" for f in flags_for(hl, d)) or "<li class='muted'>特筆シグナルなし</li>"

    if hl_only:
        cols_block = (f"<div class='cols2'>"
                      f"<div><b>現在の建玉(majors)</b>{held}</div>"
                      f"<div class='muted'><b>Nansen文脈（正体・資金源・取引相手）</b><br>"
                      f"次段 step2_enrich で付与（この資料は Hyperliquid 単独）</div></div>")
    else:
        cols_block = (f"<div class='cols'>"
                      f"<div><b>Nansenラベル</b><br>{labels}</div>"
                      f"<div><b>資金源・関連ウォレット</b>{funders}</div>"
                      f"<div><b>現在の建玉(majors)</b>{held}</div>"
                      f"<div><b>主な取引相手</b>{cps}</div></div>")

    cat = hl.get("category", "")
    cat_label = {"insider_suspect": "インサイダー疑惑", "pro_trader": "プロ",
                 "excluded": "除外"}.get(cat, cat)
    hold = hl.get("avg_hold_h")
    hold_disp = f"{hold:.1f}h" if isinstance(hold, (int, float)) else "—"

    return f"""
<div class="card">
  <div class="chead">
    <span class="rank">#{rank}</span>
    <span class="badge {cat}">{esc(cat_label)}</span>
    <span class="score">容疑度 {hl.get('insider_score','-')}</span>
    <code class="addr">{esc(a)}</code>
    <span class="links">
      <a href="{HL_ADDR.format(a=a)}" target="_blank">HL</a> ·
      <a href="{HYPURR.format(a=a)}" target="_blank">Hypurr</a> ·
      <a href="{NANSEN.format(a=a)}" target="_blank">Nansen</a>
    </span>
  </div>
  <div class="flags"><b>容疑シグナル（根拠）</b><ul>{flags}</ul></div>
  <div class="grid">
    <div><span class="k">勝率(majors)</span><span class="v">{pct(hl.get('win_rate'))}</span></div>
    <div><span class="k">方向的中率</span><span class="v">{pct(hl.get('dir_accuracy'))}</span></div>
    <div><span class="k">majors実現損益</span><span class="v">{usd(hl.get('realized_pnl'))}</span></div>
    <div><span class="k">majors含み損益</span><span class="v">{usd(hl.get('unrealized_pnl'))}</span></div>
    <div><span class="k">月次 PnL / ROI(全銘柄)</span><span class="v">{wstat('month')}</span></div>
    <div><span class="k">全期間 PnL / ROI</span><span class="v">{wstat('allTime')}</span></div>
    <div><span class="k">平均保有時間</span><span class="v">{hold_disp}</span></div>
    <div><span class="k">ポジション数 / 約定数</span><span class="v">{hl.get('n_positions','-')} / {hl.get('n_fills','-')}</span></div>
  </div>
  <div class="reason"><b>分類理由:</b> {esc(hl.get('category_reason',''))}</div>
  {cols_block}
  <div><b>急変イベント先行エントリ</b>{lead_tbl}</div>
</div>"""


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hl-only", action="store_true",
                    help="Hyperliquid単独レポート（Nansen文脈を出さない）")
    ap.add_argument("--out", default="report.html")
    args = ap.parse_args()

    dpath = f"{config.DATA_DIR}/dossiers.json"
    if (not args.hl_only) and os.path.exists(dpath):
        dossiers = json.load(open(dpath, encoding="utf-8"))["dossiers"]
    else:
        # ranked.json のみ（HL単独）
        ranked = json.load(open(f"{config.DATA_DIR}/ranked.json", encoding="utf-8"))
        dossiers = [{"address": w["address"], "hl": w, "labels": [],
                     "related_wallets": [], "first_funders": [], "counterparties": []}
                    for w in ranked["wallets"]]

    # 分類で振り分け
    def cat_of(d):
        return d.get("hl", {}).get("category", "excluded")

    insiders = [d for d in dossiers if cat_of(d) == "insider_suspect"]
    pros = [d for d in dossiers if cat_of(d) == "pro_trader"]
    excluded = [d for d in dossiers if cat_of(d) == "excluded"]

    def avg_hold(group):
        vals = [d["hl"].get("avg_hold_h") for d in group
                if isinstance(d["hl"].get("avg_hold_h"), (int, float))]
        return f"{sum(vals)/len(vals):.1f}h" if vals else "—"

    # 除外理由の内訳
    from collections import Counter
    exreasons = Counter()
    for d in excluded:
        reason = d["hl"].get("category_reason", "不明")
        key = reason.split("（")[0].split("(")[0]  # 理由の先頭で集計
        exreasons[key] += 1
    exbreak = "".join(f"<li>{esc(k)}: <b>{v}件</b></li>" for k, v in exreasons.most_common())

    # カード生成（インサイダー → プロ。除外は出さない）
    n = 0
    def render_group(group):
        nonlocal n
        out = ""
        for d in group:
            n += 1
            out += render_wallet(n, d, hl_only=args.hl_only)
        return out

    insider_cards = render_group(insiders)
    pro_cards = render_group(pros)

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode_note = ("Hyperliquid 単独（Nansen未連携）" if args.hl_only
                 else "Hyperliquid + Nansen")
    cards = (
        f"<div class='summary'>"
        f"<div class='sbox insider_suspect'><div class='snum'>{len(insiders)}</div>"
        f"<div>インサイダー疑惑<br><span class='muted'>平均保有 {avg_hold(insiders)}</span></div></div>"
        f"<div class='sbox pro_trader'><div class='snum'>{len(pros)}</div>"
        f"<div>プロトレーダー<br><span class='muted'>平均保有 {avg_hold(pros)}</span></div></div>"
        f"<div class='sbox excluded'><div class='snum'>{len(excluded)}</div>"
        f"<div>除外<br><span class='muted'>平均保有 {avg_hold(excluded)}</span></div></div>"
        f"</div>"
        f"<div class='exbox'><b>除外 {len(excluded)} 件の内訳:</b><ul>{exbreak}</ul></div>"
        f"<h2 class='sec'>🔴 インサイダー疑惑（{len(insiders)}件）</h2>"
        f"{insider_cards or '<p class=muted>該当なし</p>'}"
        f"<h2 class='sec'>🔵 プロトレーダー（{len(pros)}件）</h2>"
        f"{pro_cards or '<p class=muted>該当なし</p>'}"
    )

    htmlout = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hyperliquid perp インサイダー疑惑レポート (BTC/ETH/SOL)</title>
<style>
:root{{--bg:#0e1116;--card:#171b22;--mut:#8b949e;--acc:#4ea1ff;--warn:#ffb454;--ok:#3fb950}}
*{{box-sizing:border-box}}
body{{font-family:system-ui,"Segoe UI",sans-serif;background:var(--bg);color:#e6edf3;margin:0;padding:24px;line-height:1.55}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:18px}}
.disc{{background:#1f2530;border-left:3px solid var(--warn);padding:10px 14px;border-radius:6px;font-size:13px;color:#d7dde5;margin-bottom:20px}}
.card{{background:var(--card);border:1px solid #232a34;border-radius:10px;padding:16px;margin-bottom:16px}}
.chead{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}}
.rank{{font-weight:700;color:var(--acc)}}
.score{{background:#23303f;color:var(--warn);padding:2px 8px;border-radius:12px;font-size:12px;font-weight:700}}
.addr{{font-size:12px;color:#c9d1d9}}
.links a{{color:var(--acc);text-decoration:none;font-size:12px}}
.flags{{background:#1b2230;border-radius:8px;padding:8px 12px;font-size:13px;margin-bottom:12px}}
.flags ul{{margin:6px 0 0;padding-left:18px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}}
.grid div{{background:#10151c;border-radius:6px;padding:6px 8px}}
.k{{display:block;color:var(--mut);font-size:11px}}
.v{{display:block;font-size:15px;font-weight:600}}
.cols{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:12px;margin-bottom:10px}}
.cols ul{{margin:4px 0;padding-left:16px}}
table.sub{{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}}
table.sub th,table.sub td{{border:1px solid #2a323d;padding:4px 6px;text-align:left}}
.muted{{color:var(--mut)}}
code{{background:#0b0f14;padding:1px 4px;border-radius:4px}}
.cols2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px;margin-bottom:10px}}
.cols2 ul{{margin:4px 0;padding-left:16px}}
.badge{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px}}
.badge.insider_suspect{{background:#3a1620;color:#ff7088;border:1px solid #6b2233}}
.badge.pro_trader{{background:#16263a;color:#5ca8ff;border:1px solid #224a6b}}
.badge.excluded{{background:#23262b;color:#9aa3ad;border:1px solid #343a42}}
.reason{{font-size:12px;color:#c9d1d9;background:#10151c;border-radius:6px;padding:6px 10px;margin-bottom:10px}}
.summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px}}
.sbox{{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid #232a34;border-radius:10px;padding:14px 16px;font-size:13px}}
.sbox.insider_suspect{{border-left:4px solid #ff7088}}
.sbox.pro_trader{{border-left:4px solid #5ca8ff}}
.sbox.excluded{{border-left:4px solid #9aa3ad}}
.snum{{font-size:30px;font-weight:800}}
.exbox{{background:#171b22;border:1px solid #232a34;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:18px}}
.exbox ul{{margin:6px 0 0;padding-left:18px}}
h2.sec{{font-size:16px;margin:24px 0 10px;border-bottom:1px solid #232a34;padding-bottom:6px}}
@media(max-width:900px){{.grid,.cols,.cols2,.summary{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body>
<h1>Hyperliquid perp インサイダー疑惑ウォレット レポート（BTC / ETH / SOL）</h1>
<div class="sub">生成: {gen} ／ データ源: <b>{mode_note}</b> ／ 容疑度スコア降順 ／
母集団: {config.LB_REQUIRE_WINDOWS}両窓で黒字・{config.LB_RANK_WINDOW}の{config.LB_RANK_METRIC}降順 ／ 約定解析 直近{config.ANALYSIS_DAYS}日</div>
<div class="disc"><b>注意:</b> 本レポートは Hyperliquid 公開APIの約定・建玉と Nansen REST API の文脈情報から、
<b>統計的に不自然・タイミングが良すぎる perp 取引を「人間レビュー用」にスコア化・フラグ立て</b>したもの。
BTC/ETH/SOL は効率的市場ゆえ高勝率＝インサイダーとは限らず、<b>これはインサイダー取引の証明ではない</b>。
スコア・シグナルは調査の出発点として扱うこと。</div>
{cards}
</body></html>"""

    out = os.path.join(config.HERE, args.out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(htmlout)
    print(f"完了 → {out}（{len(dossiers)} 件 / {mode_note}）")


if __name__ == "__main__":
    main()
