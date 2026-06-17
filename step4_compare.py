"""Step4: 「Hyperliquidだけ」vs「Nansen(Pro)を足す」の差別化を実データで詳説する HTML。

Pro化で得た実データ（premium正体ラベル・資金源・CEX到達・資金トレース・スマートマネー）を使い、
台帳から3カテゴリ（インサイダー/出金/プロ）の実例を自動選定して詳細な左右対比を作る。
出力: compare.html
"""
import os
import json
import html

import config

try:
    from step8_notes import FUNDER_TRACE
except Exception:
    FUNDER_TRACE = {}

HL_ADDR = "https://app.hyperliquid.xyz/explorer/address/{a}"
NANSEN = "https://app.nansen.ai/profiler?address={a}"


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def usd(x):
    try:
        return f"${x:,.0f}"
    except (TypeError, ValueError):
        return "—"


def pct(x):
    try:
        return f"{x*100:,.1f}%"
    except (TypeError, ValueError):
        return "—"


def load():
    reg = json.load(open(f"{config.DATA_DIR}/wallet_registry.json", encoding="utf-8"))["wallets"]
    ranked = {w["address"].lower(): w
              for w in json.load(open(f"{config.DATA_DIR}/ranked.json", encoding="utf-8"))["wallets"]}
    return reg, ranked


def hl_stats(addr, e, ranked):
    """HL側の素の数字（約定行動）。"""
    w = ranked.get(addr, {})
    hp = e.get("hl_profile", {})
    cur = e.get("current", {})
    roi = e.get("roi_alltime") or (e.get("lb_allTime") or {}).get("roi")
    nf = e.get("n_fills_14d") or hp.get("n_fills_recent")
    acct = w.get("account_value") or hp.get("account_value")
    return {
        "win_rate": cur.get("win_rate") if cur.get("win_rate") is not None else hp.get("win_rate"),
        "dir_accuracy": cur.get("dir_accuracy"),
        "total_pnl": cur.get("total_pnl"),
        "roi": roi, "n_fills": nf, "account_value": acct,
        "held": w.get("held_positions") or hp.get("held_positions") or [],
        "cashout_ratio": e.get("roi_alltime") and None or None,
    }


def richness(e):
    """“語れる”度合い: 正体ENS・資金トレース・クラスタを重視、取引相手は頭打ち。"""
    labels = e.get("labels") or []
    ens = sum(1 for l in labels if (".eth" in str(l) or "OpenSea" in str(l) or "Capital" in str(l)))
    score = len(labels) + ens * 2
    score += len(e.get("first_funders") or [])
    score += min(len(e.get("counterparties") or []), 3)
    if e["address"].lower() in FUNDER_TRACE:
        score += 4
    if "cluster-A" in (e.get("tags") or []):
        score += 3
    return score


def pick(reg, positions):
    """指定ポジションのうち Nansen データが最も豊富な1件を返す。"""
    cands = [e for e in reg.values() if e.get("position") in positions and e.get("nansen_checked")]
    if not cands:
        return None
    return max(cands, key=richness)


# Pro で初めて解ける問い（🔓）を含む能力マトリクス
MATRIX = [
    ("perp の全約定（時刻・価格・サイズ・方向）", True, True, "", "HLが一次ソース"),
    ("勝率・実現/含み損益・現在の建玉", True, True, "", "HLの fills/clearinghouse"),
    ("方向的中率・イベント先行度・保有時間", True, True, "", "HL足と突合"),
    ("このアドレスは誰か（premium正体ラベル）", False, True, "🔓Pro", "Abraxas Capital / ENS / OpenSea名 等"),
    ("資金の出どころ（First Funder）と1段上流", False, True, "🔓Pro", "related-wallets を再帰照会"),
    ("入金元が CEX か（Binance/Coinbase…）", False, True, "🔓Pro", "身元は取引所が握る、の判定"),
    ("同一主体の別ウォレット・協調クラスタ", False, True, "🔓Pro", "資金網の名寄せ"),
    ("オンチェーン取引相手（出金先・OTC）", False, True, "🔓Pro", "counterparties の in/out"),
    ("Smart Money / Fund 分類", False, True, "🔓Pro", "キュレーション済ラベル"),
    ("25+チェーン横断のポートフォリオ・PnL", False, True, "🔓Pro", "HL以外の全資産"),
]


def funder_line(e):
    ff = e.get("first_funders") or []
    if not ff:
        return "—"
    parts = []
    for f in ff[:3]:
        parts.append(esc(f.get("label") or (f.get("address") or "")[:12]))
    return ", ".join(p for p in parts if p)


def cp_list(e):
    out = ""
    for c in (e.get("counterparties") or [])[:5]:
        lbl = c.get("label") or (c.get("address") or "")[:12]
        out += f"<li>{esc(lbl)} <span class='mut'>{usd(c.get('volume_usd'))}</span></li>"
    return out or "<li class='mut'>—</li>"


def why_block(e, ranked):
    """なぜこの判定（インサイダー/プロ/出金）なのか、決め手を平易に。"""
    a = e["address"].lower()
    w = ranked.get(a, {})
    pos = e.get("position", "")
    win = (e.get("current") or {}).get("win_rate")
    dir_ = (e.get("current") or {}).get("dir_accuracy")
    nclos = w.get("n_closes")
    elead = w.get("event_lead_notional") or 0
    leads = w.get("lead_examples") or []
    cluster = "cluster-A" in (e.get("tags") or [])
    reason = e.get("alt_reasoning") or e.get("alt_verdict") or ""
    pnl = (e.get("lb_allTime") or {}).get("pnl") or (w.get("lb_windows", {}).get("allTime") or {}).get("pnl")
    acct = w.get("account_value") or (e.get("hl_profile") or {}).get("account_value")
    ratio = e.get("roi_alltime") and None
    cr = w.get("cashout_ratio")
    pts = []

    if pos in ("インサイダー疑惑(要監視)", "弱い疑惑(監視継続)"):
        if elead > 0 or leads:
            ex = leads[0] if leads else None
            extra = (f"（例: {esc(ex.get('event_time'))} の急変{esc(ex.get('event_move_pct'))}%の直前に "
                     f"{esc(ex.get('coin'))} を {usd(ex.get('notional_usd'))} 先行建玉）") if ex else ""
            pts.append(f"<b>① 急変イベントの直前に大口を先行</b>{extra} → タイミングが良すぎ、偶然では説明しにくい。")
        if cluster:
            pts.append("<b>② 協調(検証で否定)</b>: 共有資金元（クラスタA）は<u>Monadの公開エアドロップ配布</u>＝誰でも受領可と判明。"
                       "私的な協調の証拠にはならず、これは<b>降格(弱疑惑)要因</b>。")
        pts.append("<b>判定の核</b>: 勝率の高さでなく<u>『動く前に』正しく賭けることを<a href='methodology.html'>反復</a>している</u>か。"
                   "1回だけ(N=1)なら偶然と区別できず弱疑惑止まり。")
        verdict = "👉 反復が確認できれば『プロの実力』では説明しきれずインサイダー疑惑。一発止まりなら弱疑惑。"
    elif pos in ("プロトレーダー(本物)", "プロトレーダー(未精査)"):
        if nclos:
            pts.append(f"<b>① 場数</b>: {nclos}回ものクローズで勝率{pct(win)}を<u>安定して</u>維持 → 一発の幸運でない。")
        pts.append(f"<b>② トレンド非依存</b>: 方向的中率{pct(dir_)}は下落相場のベタ(~85%)に頼らず、逆張りや両方向でも勝つ → 相場頼みでない実力。")
        pts.append("<b>③ イベント先行なし</b>: 急変直前の先行建玉は検出されず＝『先に知っていた』証拠はない。")
        pts.append("<b>判定の核</b>: <u>多数の取引×規律</u>で勝っている。タイミングの良さでなく腕。")
        verdict = "👉 だから情報優位（インサイダー）ではなく、純粋に上手いプロ。"
    elif pos == "💸 出金疑い(要監視)":
        pts.append(f"<b>① 稼いで引き上げ</b>: 通算 {usd(pnl)} 稼いだのに現在残高は {usd(acct)}"
                   + (f"（比 {cr:.0f}倍）" if cr else "") + " → 利益をほぼ外へ出した。")
        pts.append("<b>② 痕跡が薄い</b>: 直近の取引も少なく、稼いだら去る hit-and-run の形。")
        pts.append("<b>判定の核</b>: <u>『現在の口座規模』でなく『過去にいくら抜いたか』</u>で評価。残高が小さくても大物。")
        verdict = "👉 だから現役の成績では測れず、出金疑い（要追跡）。"
    else:
        pts, verdict = [], ""

    body = "".join(f"<li>{p}</li>" for p in pts)
    rj = f"<div class='rj'>多角分析の所見: {esc(reason)}</div>" if reason else ""
    return f"<div class='why'><div class='wt'>🔑 なぜこの判定か</div><ul>{body}</ul><div class='wv'>{verdict}</div>{rj}</div>"


def render_case(e, ranked, title, color):
    a = e["address"]
    s = hl_stats(a.lower(), e, ranked)
    labels = "、".join(esc(l) for l in (e.get("labels") or [])) or "—（ラベル無＝匿名）"
    trace = FUNDER_TRACE.get(a.lower())
    held = "".join(
        f"<li>{esc(h.get('coin'))} {esc(h.get('side'))} {usd(h.get('position_value'))}（含み {usd(h.get('unrealized_pnl'))}）</li>"
        for h in s["held"][:6]) or "<li class='mut'>フラット</li>"
    cluster = ("クラスタA（※共有資金元=公開エアドロップと判明し協調は否定）"
               if "cluster-A" in (e.get("tags") or []) else "—")

    return f"""
<div class="case">
  <div class="ctitle" style="--c:{color}">{esc(title)} <code>{esc(a)}</code>
    <span class="lnk"><a href="{HL_ADDR.format(a=a)}" target="_blank">HL</a> · <a href="{NANSEN.format(a=a)}" target="_blank">Nansen</a></span></div>
  <div class="vs">
    <div class="pane hl">
      <h4>① Hyperliquid だけ <span class="mut">＝行動しか見えない</span></h4>
      <div class="kv">
        <span>アドレス</span><b><code>{esc(a[:20])}…</code></b>
        <span>勝率 / 方向的中率</span><b>{pct(s['win_rate'])} / {pct(s['dir_accuracy'])}</b>
        <span>majors損益</span><b>{usd(s['total_pnl'])}</b>
        <span>ROI(全期)</span><b>{pct(s['roi'])}</b>
        <span>取引数(14日)</span><b>{esc(s['n_fills'])}</b>
        <span>現在残高</span><b>{usd(s['account_value'])}</b>
      </div>
      <div class="held"><b>現在の建玉</b><ul>{held}</ul></div>
      <div class="unknown">ここから先は <b>HL では永遠に不明</b>:
        <ul><li>このアドレスは誰か → ❓</li><li>お金の出どころ → ❓</li><li>入金元が取引所か → ❓</li><li>別ウォレットとの繋がり → ❓</li></ul></div>
    </div>
    <div class="pane ns">
      <h4>② ＋ Nansen Pro <span class="mut">＝正体・お金の流れが乗る</span></h4>
      <div class="block"><b>🔓 正体（premiumラベル）</b><br>{labels}</div>
      <div class="block"><b>🔓 お金の出どころ（First Funder）</b><br>{funder_line(e)}</div>
      {f'<div class="block trace"><b>🔓 もう一段たどると</b><br>{esc(trace)}</div>' if trace else ''}
      <div class="block"><b>🔓 取引相手（出金先・OTC候補）</b><ul>{cp_list(e)}</ul></div>
      <div class="block"><b>🔓 協調クラスタ</b> {esc(cluster)}</div>
    </div>
  </div>
  {why_block(e, ranked)}
  <div class="note"><b>まとめ:</b> {esc(e.get('notes_jp','').replace(chr(10),' / '))}</div>
</div>"""


def main():
    reg, ranked = load()
    cases = []
    ins = pick(reg, {"インサイダー疑惑(要監視)", "弱い疑惑(監視継続)"})
    if ins:
        cases.append((ins, "🔴 インサイダー疑惑", "#ff5d6c"))
    co = pick(reg, {"💸 出金疑い(要監視)"})
    if co:
        cases.append((co, "💸 出金疑い", "#f59e0b"))
    pro = pick(reg, {"プロトレーダー(本物)", "プロトレーダー(未精査)"})
    if pro:
        cases.append((pro, "🟢 プロトレーダー", "#3fb950"))

    case_html = "".join(render_case(e, ranked, t, c) for e, t, c in cases)

    rows = ""
    for q, h, n, badge, note in MATRIX:
        hc = "<span class='yes'>✓</span>" if h else "<span class='no'>✕</span>"
        nc = "<span class='yes'>✓</span>" if n else "<span class='no'>✕</span>"
        only = "nsonly" if (n and not h) else ""
        rows += (f"<tr class='{only}'><td class='q'>{esc(q)} {('<span class=pro>'+badge+'</span>') if badge else ''}</td>"
                 f"<td class='c'>{hc}</td><td class='c'>{nc}</td><td class='note'>{esc(note)}</td></tr>")

    # Pro で実際に得られた premium 正体ラベルの実例（台帳から収集）
    real_labels = []
    for e in reg.values():
        for l in (e.get("labels") or []):
            if (".eth" in str(l) or "OpenSea" in str(l) or "Capital" in str(l)
                    or "Fund" in str(l)) and str(l) not in real_labels:
                real_labels.append(str(l))
    real_labels = real_labels[:14]
    labels_html = "".join(f"<span class='tag'>{esc(l)}</span>" for l in real_labels)
    n_enriched = sum(1 for e in reg.values() if e.get("nansen_checked"))
    n_funded = sum(1 for e in reg.values() if e.get("first_funders"))

    out = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hyperliquid だけ vs Nansen Pro — 差別化（実データ詳説）</title>
<style>
:root{{--bg:#0e1116;--card:#171b22;--mut:#8b949e;--hl:#f7a440;--ns:#7c5cff;--line:#232a34;--ok:#3fb950}}
*{{box-sizing:border-box}}
body{{font-family:system-ui,"Segoe UI",sans-serif;background:var(--bg);color:#e6edf3;margin:0;padding:26px;line-height:1.6}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:26px 0 10px;border-left:3px solid var(--ns);padding-left:8px}}
h4{{margin:0 0 8px;font-size:14px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:14px}}
.mut{{color:var(--mut)}} code{{font-family:ui-monospace,monospace;font-size:.92em}}
.pro{{background:#2a1f4d;color:#b9a3ff;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:4px}}
.layers{{display:flex;flex-direction:column;gap:8px;margin:8px 0}}
.layer{{border-radius:10px;padding:12px 16px;border:1px solid var(--line)}}
.layer.ns{{background:linear-gradient(90deg,#1c1633,#171b22);border-color:#3a2d6b}}
.layer.hl{{background:linear-gradient(90deg,#2a1f10,#171b22);border-color:#5c4318}}
.callout{{background:#16142a;border:1px solid #3a2d6b;border-radius:10px;padding:12px 16px;font-size:13px;margin-bottom:10px}}
.tag{{display:inline-block;background:#0b0f14;border:1px solid #3a2d6b;color:#b9a3ff;border-radius:9px;font-size:11px;padding:2px 8px;margin:2px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{border:1px solid var(--line);padding:7px 10px;text-align:left}} th{{background:#10151c;font-size:12px}}
td.c{{text-align:center;font-weight:700;width:80px}} th.hl{{color:var(--hl)}} th.ns{{color:var(--ns)}}
.yes{{color:var(--ok)}} .no{{color:#6b7280}} td.q{{font-weight:600}} td.note{{color:var(--mut);font-size:12px}}
tr.nsonly{{background:#191436}}
.case{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:18px}}
.ctitle{{font-weight:700;color:var(--c);margin-bottom:10px;font-size:15px}}
.ctitle code{{color:#c9d1d9;font-size:12px}} .lnk{{float:right;font-size:12px}} .lnk a{{color:#4ea1ff;text-decoration:none}}
.vs{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.pane{{border-radius:10px;padding:12px 14px;font-size:13px}}
.pane.hl{{background:#10151c;border-top:3px solid var(--hl)}} .pane.ns{{background:#120f22;border-top:3px solid var(--ns)}}
.kv{{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin-bottom:8px}} .kv span{{color:var(--mut)}}
.held ul,.unknown ul,.block ul{{margin:3px 0;padding-left:18px}}
.unknown{{margin-top:8px;color:#e0697a}} .unknown b{{color:#ff8893}}
.block{{margin-bottom:8px}} .block.trace{{background:#0f1b17;border-left:3px solid var(--ok);padding:6px 9px;border-radius:5px}}
.note{{margin-top:10px;font-size:12px;color:#cdd6df;border-top:1px solid var(--line);padding-top:8px}}
.why{{margin-top:12px;background:#10151c;border:1px solid var(--line);border-radius:8px;padding:10px 13px}}
.why .wt{{font-weight:700;font-size:13px;margin-bottom:5px}}
.why ul{{margin:4px 0;padding-left:18px;font-size:12.5px;line-height:1.6}}
.why u{{text-decoration:underline;text-underline-offset:2px}}
.why .wv{{margin-top:6px;font-weight:700;font-size:13px;color:#ffd86b}}
.why .rj{{margin-top:6px;font-size:11.5px;color:var(--mut)}}
table.guide td.g1{{color:#ff8893;font-weight:700;white-space:nowrap}}
table.guide td.g2{{color:#69d98a;font-weight:700;white-space:nowrap}}
table.guide td.g3{{color:#ffc06b;font-weight:700;white-space:nowrap}}
@media(max-width:820px){{.vs{{grid-template-columns:1fr}}}}
</style></head><body>

<h1>Hyperliquid だけ <span class="mut">vs</span> Nansen Pro — 差別化（実データ詳説）</h1>
<div class="sub">perp インサイダー追跡における「行動データ」と「正体・資金フローデータ」の違い。Pro化で得た実データで解説。</div>

<h2>2つのデータ層</h2>
<div class="layers">
  <div class="layer ns"><b style="color:var(--ns)">Nansen Pro 層 — 「誰が・どこの金で・他に何を」</b>
    <div class="mut">premium正体ラベル / First Funder（資金源）と上流 / CEX到達判定 / 協調クラスタ名寄せ / 取引相手(出金先) / Smart Money分類 / 25+チェーン横断</div></div>
  <div class="layer hl"><b style="color:var(--hl)">Hyperliquid 層 — 「何をどう取引し、どれだけ勝ったか」</b>
    <div class="mut">全約定（時刻/価格/サイズ/方向/実現損益） / 建玉・含み損益 / 勝率・方向的中率・ROI・取引回数</div></div>
</div>

<h2>🔓 Pro で解禁されたこと（実証済み）</h2>
<div class="callout">
  ① <b>premium正体ラベル</b>＝生の0xに実体名が付く（下が台帳で実際に取れた例）:<br>{labels_html}<br><br>
  ② <b>Nansen照会で {n_enriched} 件をエンリッチ</b>済み（うち資金源 First Funder を {n_funded} 件で特定）。<br>
  ③ <b>資金源の再帰トレース</b> → 入金元をもう一段遡り、CEX(取引所)に到達するか判定。<br>
  ④ <b>レート 20req/秒・500req/分</b> で再実行が速い。
</div>

<h2>能力マトリクス（🔓Pro = Pro で初めて解ける問い）</h2>
<table>
<tr><th>問い</th><th class="hl">HLだけ</th><th class="ns">+Nansen Pro</th><th>補足</th></tr>
{rows}
</table>

<h2>判定基準 — 何で見分けるか（ここが肝）</h2>
<table class="guide">
<tr><th>種別</th><th>決め手（他と何が違う）</th><th>見ている主なデータ</th></tr>
<tr><td class="g1">🔴 インサイダー疑惑</td><td><b>タイミングが良すぎる＋それを<u>反復</u>する。</b>急変イベントの直前に大口を先行し、<u>別々のイベントで往復(建て→急変→利確)を繰り返す</u>のが核。<b>1回だけの先行(N=1)は偶然と区別できず<a href="registry.html">🟠弱い疑惑</a>止まり</b>。勝率の高さは見ない。</td><td>イベント先行度・<a href="methodology.html">往復/反復</a>回数・先行エントリ事例・多角分析の濃度</td></tr>
<tr><td class="g2">🟢 プロトレーダー</td><td><b>場数と規律。</b>多数の取引で勝率を<u>安定維持</u>＋トレンド非依存（逆張り/両方向でも勝つ）。イベント直前の先行は<u>無い</u>。＝知っていたのでなく上手い。</td><td>クローズ回数・勝率の継続性・方向的中率がベタ超えか・規律</td></tr>
<tr><td class="g3">💸 出金疑い</td><td><b>稼いで即引き上げ。</b>通算利益÷現在残高が大（10倍超）。残高が小さくても過去に大きく抜いた hit-and-run。</td><td>allTime PnL ÷ 現在残高・直近取引の少なさ</td></tr>
</table>

<h2>実例で見る差分（台帳の実データ・Nansenデータ量上位を自動選定）</h2>
{case_html or '<p class="mut">該当なし</p>'}

<div class="sub" style="margin-top:20px">結論: <b style="color:var(--hl)">HL</b> が「不自然に勝っている行動」を炙り出し、<b style="color:var(--ns)">Nansen Pro</b> が「それが誰で・どこの金で・どこへ消えたか」を与える。両者が揃って初めてインサイダー疑惑が立ち上がる。CEXに当たれば身元は取引所が握り、ブリッジ/コントラクト経由なら意図的な隠蔽——という読み分けも Pro のラベルで可能になった。</div>
</body></html>"""

    path = os.path.join(config.HERE, "compare.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"完了 → {path}（実例 {len(cases)} 件 / premiumラベル例 {len(real_labels)} 個）")


if __name__ == "__main__":
    main()
