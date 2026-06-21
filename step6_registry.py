"""Step6: 77ウォレットを多角分析の位置づけ込みで永続台帳に蓄積する（upsert）。

入力: data/ranked.json（数値分類）, data/alt_assessment.json（多角再評価・任意）
出力: data/wallet_registry.json（永続・追記式）, registry.html（ビュー）

実行のたびに各ウォレットを upsert:
- 新規 → first_seen 付与
- 既存 → history にスナップショット追記・last_seen/現況更新・手動tags/notesは保持
使い方: python step6_registry.py
"""
import os
import json
import html
from datetime import datetime, timezone

import config
import tagging

REGISTRY = f"{config.DATA_DIR}/wallet_registry.json"

# workflow「協調レンズ」が検出した資金網クラスタ（投資判断でなく観測事実のタグ）
CLUSTER_A = {
    "0xefcace57934a753d66c48a31b880ba806d4c0869",
    "0x10b6f072fabe21bc72ffd77ff5d4414e6cef2980",
    "0xa351db10472a07059ea099e0581444e568bff894",
    "0xd67ca2c6f8bc84acf4fa4472b82a8740dc0a53ff",
}
CLUSTER_A_FUNDER = "0xdf747918"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def position_of(metric_cat, likelihood, tags, agreement=None, cashout_ratio=None):
    """ウォレットの「位置づけ」を個体ごとの独立評価で決める。
    協調クラスタ所属は position に使わず tags に留める（個別の素性で判定）。"""
    # 強いインサイダー疑惑は最優先
    if likelihood is not None and likelihood >= 0.45:
        return "インサイダー疑惑(要監視)"
    if likelihood is not None and likelihood >= 0.25:
        return "弱い疑惑(監視継続)"
    # 出金(hit-and-run)疑い: 大金を稼いで引き上げた層。majors成績に関係なく格上げし除外に埋もれさせない。
    if cashout_ratio and cashout_ratio >= config.CASHOUT_RATIO:
        return "💸 出金疑い(要監視)"
    if likelihood is not None:                 # 多角分析で再評価済み
        if metric_cat == "insider_suspect":
            return "偽陽性(数値疑惑→否定)"
        if metric_cat == "pro_trader":
            return "プロ格付け過大(要再検証)" if agreement == "disagree" else "プロトレーダー(本物)"
        return "除外/低優先"
    # 多角分析 未評価（数値分類のみ）
    if metric_cat == "pro_trader":
        return "プロトレーダー(未精査)"
    if metric_cat == "insider_suspect":
        return "要再検証(数値疑惑・未レビュー)"
    return "除外/低優先"


def load_registry():
    if os.path.exists(REGISTRY):
        return json.load(open(REGISTRY, encoding="utf-8"))
    return {"created_at": now_iso(), "run_count": 0, "wallets": {}}


def main():
    reg = load_registry()
    ranked = json.load(open(f"{config.DATA_DIR}/ranked.json", encoding="utf-8"))["wallets"]

    alt_path = f"{config.DATA_DIR}/alt_assessment.json"
    alt = {}
    if os.path.exists(alt_path):
        a = json.load(open(alt_path, encoding="utf-8"))
        for r in a.get("synthesis", {}).get("reassessments", []):
            alt[r["address"].lower()] = r

    run_date = today()
    new_n = upd_n = 0

    for w in ranked:
        addr = w["address"]
        key = addr.lower()
        a = alt.get(key)
        likelihood = a.get("insider_likelihood") if a else None

        snap = {
            "date": run_date,
            "metric_category": w.get("category"),
            "insider_score": w.get("insider_score"),
            "win_rate": w.get("win_rate"),
            "dir_accuracy": w.get("dir_accuracy"),
            "total_pnl": w.get("total_pnl"),
            "avg_hold_h": w.get("avg_hold_h"),
            "event_lead_notional": w.get("event_lead_notional"),
            "insider_likelihood": likelihood,
        }

        entry = reg["wallets"].get(key)
        if entry is None:
            entry = {
                "address": addr,
                "first_seen": run_date,
                "times_seen": 0,
                "tags": [],
                "notes": "",
                "history": [],
            }
            reg["wallets"][key] = entry
            new_n += 1
        else:
            upd_n += 1

        # タグ（観測事実）。手動タグは保持しつつ自動タグを補う
        tags = set(entry.get("tags", []))
        if addr.lower() in {c.lower() for c in CLUSTER_A}:
            tags.add("cluster-A")
            tags.add(f"funder:{CLUSTER_A_FUNDER}")
        entry["tags"] = sorted(tags)

        # 現況更新
        entry["last_seen"] = run_date
        entry["times_seen"] = entry.get("times_seen", 0) + 1
        entry["metric_category"] = w.get("category")
        entry["insider_likelihood"] = likelihood
        if a:
            entry["alt_verdict"] = a.get("alt_verdict")
            entry["agreement"] = a.get("agreement")
            entry["lenses_hit"] = a.get("lenses_hit", [])
            entry["alt_reasoning"] = a.get("reasoning")
        entry["position"] = position_of(w.get("category"), likelihood, entry["tags"],
                                        entry.get("agreement"), w.get("cashout_ratio"))
        # 多軸オートタグ
        win = w.get("lb_windows", {}) or {}
        at = win.get("allTime") or {}
        mo = win.get("month") or {}
        # 表示用: ROI(全期/月) と 直近14日の取引回数(majors約定数)
        entry["roi_alltime"] = at.get("roi")
        entry["roi_month"] = mo.get("roi")
        entry["n_fills_14d"] = w.get("n_fills")
        entry["auto_tags"] = tagging.derive_tags({
            "roi": at.get("roi"), "pnl": at.get("pnl"),
            "n_fills": w.get("n_fills"), "avg_hold_h": w.get("avg_hold_h"),
            "held": w.get("held_positions"), "pos_value": w.get("position_value"),
            "account_value": w.get("account_value"), "labels": entry.get("labels"),
            "cashout_ratio": w.get("cashout_ratio"),
        })
        entry["current"] = snap

        # 同日スナップショットは置換、それ以外は追記
        entry["history"] = [h for h in entry["history"] if h.get("date") != run_date]
        entry["history"].append(snap)

    reg["run_count"] = reg.get("run_count", 0) + 1
    reg["updated_at"] = now_iso()
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)

    render_all(reg)

    # サマリ
    from collections import Counter
    pos = Counter(e["position"] for e in reg["wallets"].values())
    print(f"台帳更新 → {REGISTRY}")
    print(f"  実行回数(run_count): {reg['run_count']}  総ウォレット: {len(reg['wallets'])}")
    print(f"  今回: 新規 {new_n} / 更新 {upd_n}")
    print("  ポジション内訳:")
    for p, c in pos.most_common():
        print(f"    {p}: {c}")


def esc(x):
    return html.escape(str(x)) if x is not None else ""


# ポジション表示順と色
POS_ORDER = ["インサイダー疑惑(要監視)", "弱い疑惑(監視継続)", "💸 出金疑い(要監視)",
             "プロトレーダー(本物)", "alt主体プロ", "高頻度MM", "プロトレーダー(未精査)",
             "偽陽性(数値疑惑→否定)", "除外/低優先"]
POS_COLOR = {
    "インサイダー疑惑(要監視)": "#ff5d6c", "弱い疑惑(監視継続)": "#ffb454",
    "💸 出金疑い(要監視)": "#f59e0b",
    "プロトレーダー(本物)": "#3fb950", "alt主体プロ": "#56b6c2",
    "高頻度MM": "#a78bfa", "プロトレーダー(未精査)": "#4ea1ff",
    "偽陽性(数値疑惑→否定)": "#7a8390", "除外/低優先": "#5c636d",
}
HL_ADDR = "https://app.hyperliquid.xyz/explorer/address/{a}"
NANSEN = "https://app.nansen.ai/profiler?address={a}"
HYPERDASH = "https://hyperdash.info/trader/{a}"        # トレーダープロフィール(PnL/建玉)
HYPURRSCAN = "https://hypurrscan.io/address/{a}"       # 建玉/約定/残高ビュー
ASXN = "https://hyperscreener.asxn.xyz/profile/{a}"  # ASXN(hyperscreener) ポートフォリオ/PnL分析


def render_html(reg, out="registry.html",
                title="perp ウォレット監視台帳（蓄積データ）", only=None, drop=None):
    wallets = list(reg["wallets"].values())
    if only:
        wallets = [w for w in wallets if w.get("position") in only]
    if drop:
        wallets = [w for w in wallets if w.get("position") not in drop]

    def sortkey(e):
        try:
            i = POS_ORDER.index(e["position"])
        except ValueError:
            i = len(POS_ORDER)
        return (i, -(e.get("insider_likelihood") or 0), -(e.get("pro_score") or 0))

    wallets.sort(key=sortkey)

    rows = ""
    for e in wallets:
        a = e["address"]
        cur = e.get("current", {})
        lik = e.get("insider_likelihood")
        liks = f"{lik:.2f}" if isinstance(lik, (int, float)) else "—"
        color = POS_COLOR.get(e["position"], "#5c636d")
        # ROI(全期) と 直近14日取引回数（手動CAは lb_allTime/hl_profile からフォールバック）
        roi_at = e.get("roi_alltime")
        if roi_at is None:
            roi_at = (e.get("lb_allTime") or {}).get("roi")
        roi_disp = f"{roi_at*100:,.0f}%" if isinstance(roi_at, (int, float)) else "—"
        # HL公式 通算PnL（leaderboard allTime）。全銘柄＋funding込みの「総額」。majors損益(キャッシュ実現)とは別物。
        lbat = e.get("lb_alltime")
        if isinstance(lbat, (int, float)):
            lb_disp = f"${lbat:,.0f}"
            mj = cur.get("total_pnl")
            if isinstance(mj, (int, float)) and lbat != 0:
                gap = lbat - mj
                if abs(gap) >= max(50000, abs(lbat) * 0.1):
                    lb_disp += f"<br><span class='muted'>差${gap:,.0f}<br>(alt/funding等)</span>"
        else:
            lb_disp = "—"
        # 全実現損益（キャッシュ全銘柄closedPnl合計）。旧Tierはこの金額のバケットだった→数値そのものを表示。
        ta = e.get("true_realized_all")
        ta_disp = f"${ta:,.0f}" if isinstance(ta, (int, float)) else "—"
        nf = e.get("n_fills_14d")
        if nf is None:
            nf = (e.get("hl_profile") or {}).get("n_fills_recent")
        nf_disp = f"{nf:,}" if isinstance(nf, (int, float)) else "—"
        # 取引期間（HLフル履歴の活動範囲）
        af, at2, td = e.get("active_from"), e.get("active_to"), e.get("trade_days")
        if af and at2:
            mo = f"{td//30}ヶ月" if (td and td >= 30) else (f"{td}日" if td else "")
            period_disp = f"{esc(af)}〜{esc(at2)}<br><span class='muted'>{mo}</span>"
        else:
            period_disp = "—"
        # 表示タグ = オートタグ + 手動/クラスタタグ（funder: は冗長／Tier-は全実現損益列へ置換ゆえ除外）
        all_tags = list(e.get("auto_tags", [])) + [t for t in e.get("tags", [])
                                                   if not t.startswith("funder:") and not t.startswith("Tier-")]
        # 品質(wf_quality)をタグ化＝フィルタ可能に（質:エリート/堅実/中堅…）
        if e.get("wf_quality") and f"質:{e['wf_quality']}" not in all_tags:
            all_tags.append(f"質:{e['wf_quality']}")
        tags = "".join(
            f"<span class='tag' style='--tc:{tagging.tag_color(t)}'>{esc(t)}</span>"
            for t in all_tags
        )
        data_tags = esc(" ".join(all_tags) + " " + e["position"])
        spark = " ".join(
            f"{h['date'][5:]}:{(h.get('insider_likelihood') if h.get('insider_likelihood') is not None else h.get('insider_score',0)):.2f}"
            for h in e.get("history", [])[-6:]
        )
        # Nansen 情報（照会済みなら表示）
        nansen_html = ""
        if e.get("nansen_checked"):
            labs = ", ".join(e.get("labels") or []) or "ラベル無"
            ff = ", ".join((f.get("label") or f.get("address", "")[:10]) for f in e.get("first_funders", [])) or "—"
            cps = ", ".join((c.get("label") or c.get("address", "")[:10]) for c in (e.get("counterparties") or [])[:3]) or "—"
            nansen_html = (f"<div class='nansen'>🔎 <b>{esc(labs)}</b>"
                           f" ／ 資金源: {esc(ff)} ／ 相手: {esc(cps)}"
                           f" <span class='muted'>({esc(e.get('nansen_checked'))})</span></div>")
        # 猿でもわかる平易な説明
        note_jp = e.get("notes_jp")
        note_html = ""
        if note_jp:
            note_html = "<div class='notejp'>" + esc(note_jp).replace("\n", "<br>") + "</div>"
        rowcls = "inact" if e.get("active14") is False else ""
        rows += f"""
<tr data-tags="{data_tags}" class="{rowcls}">
  <td><span class="pos" style="--c:{color}">{esc(e['position'])}</span></td>
  <td class="lik">{liks}</td>
  <td><code>{esc(a[:16])}…</code><div class="lnk"><a href="{HL_ADDR.format(a=a)}" target="_blank">HL</a> <a href="{HYPERDASH.format(a=a)}" target="_blank" title="Hyperdash トレーダープロフィール">HD📊</a> <a href="{HYPURRSCAN.format(a=a)}" target="_blank" title="Hypurrscan 建玉/約定">HS</a> <a href="{ASXN.format(a=a)}" target="_blank" title="ASXN ポートフォリオ/PnL">AX📈</a> <a href="{NANSEN.format(a=a)}" target="_blank">N</a></div></td>
  <td>{esc(cur.get('metric_category'))}</td>
  <td>{esc(round(cur.get('win_rate',0) or 0,2))}/{esc(round(cur.get('dir_accuracy',0) or 0,2))}</td>
  <td>{esc(f"${cur.get('total_pnl',0):,.0f}" if cur.get('total_pnl') is not None else '-')}</td>
  <td class="lbat">{lb_disp}</td>
  <td class="lbat">{ta_disp}</td>
  <td>{roi_disp}</td>
  <td>{nf_disp}</td>
  <td class="period">{period_disp}</td>
  <td>{esc(cur.get('avg_hold_h'))}</td>
  <td>{tags}</td>
  <td class="seen">{esc(e.get('times_seen'))}回<br><span class="muted">{esc(e.get('first_seen'))}→{esc(e.get('last_seen'))}</span></td>
  <td class="verdict">{note_html}{nansen_html}<div class="spark muted">{esc(spark)}</div></td>
</tr>"""

    from collections import Counter
    pos = Counter(e["position"] for e in wallets)
    chips = ""
    for p in POS_ORDER:
        c = pos.get(p)
        if not c:
            continue
        col = POS_COLOR.get(p, "#5c636d")
        chips += f"<span class='chip' style='--c:{col}'>{esc(p)}: {c}</span>"

    # フィルタバー: 全タグをカテゴリ別に集計して clickable chip 化
    tagcount = Counter()
    for e in wallets:
        at = list(e.get("auto_tags", [])) + [x for x in e.get("tags", [])
                                             if not x.startswith("funder:") and not x.startswith("Tier-")]
        if e.get("wf_quality") and f"質:{e['wf_quality']}" not in at:
            at.append(f"質:{e['wf_quality']}")
        for t in at:
            tagcount[t] += 1
    CAT_ORDER = ["位置", "品質", "活動", "ROI:", "PnL:", "頻度:", "保有:", "方向:",
                 "レバ:", "銘柄:", "ID:", "検証", "資金/出金", "区分", "他"]
    AXIS = ["ROI:", "PnL:", "頻度:", "保有:", "方向:", "レバ:", "銘柄:", "ID:"]
    def cat_of(t):
        if t.startswith("Tier-"):
            return "Tier"
        if t.startswith("質:"):
            return "品質"
        if t.startswith("取引あり") or t.startswith("取引なし"):
            return "活動"
        for c in AXIS:
            if t.startswith(c):
                return c
        if (t.startswith("WF:") or t.startswith("遅効エッジ")
                or t in ("HL先行検出", "HL検証済プロ", "稼ぎ確認・先行不明(要精査)",
                         "遅効シグナルだがmajors赤字")):
            return "検証"
        if (t.startswith("出金") or t.startswith("資金") or "資金源" in t
                or t.startswith("塩漬け") or t == "cluster-A" or t == "hit-and-run候補"):
            return "資金/出金"
        if t in ("MM/HFT", "HFT/MM", "HL履歴なし", "HL検証:非該当",
                 "未照会発掘", "Nansen発見", "小利/要再検証だった"):
            return "区分"
        return "他"
    groups = {}
    for t, c in tagcount.items():
        groups.setdefault(cat_of(t), []).append((t, c))
    groups["位置"] = [(p, pos[p]) for p in POS_ORDER if pos.get(p)]
    GLABEL = {"位置": "位置づけ", "品質": "品質(WF精査)", "活動": "活動(14日)",
              "ROI:": "ROI", "PnL:": "PnL", "頻度:": "頻度", "保有:": "保有",
              "方向:": "方向", "レバ:": "レバ", "銘柄:": "銘柄", "ID:": "正体",
              "検証": "検証/疑い", "資金/出金": "資金/出金", "区分": "区分", "他": "他"}
    filterbar = ""
    for c in CAT_ORDER:
        if c not in groups:
            continue
        items = sorted(groups[c], key=lambda x: -x[1])
        chipshtml = "".join(
            f"<span class='ft' style='--tc:{tagging.tag_color(t if c!='位置' else t)}' data-t=\"{esc(t)}\">{esc(t)} ({n})</span>"
            for t, n in items
        )
        filterbar += f"<div class='grp'><span class='glabel'>{GLABEL.get(c,c)}</span>{chipshtml}</div>"

    html_doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{font-family:system-ui,"Segoe UI",sans-serif;background:#0e1116;color:#e6edf3;margin:0;padding:24px;font-size:13px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:14px}}
.chips{{margin-bottom:14px}}
.chip,.tag{{display:inline-block;border-radius:10px;font-size:11px;padding:2px 8px;margin:2px}}
.chip{{background:#171b22;border-left:3px solid var(--c);color:#e6edf3}}
.tag{{background:color-mix(in srgb, var(--tc) 18%, #0b0f14);color:var(--tc);border:1px solid color-mix(in srgb, var(--tc) 40%, #0b0f14);font-size:10px}}
.filterbar{{background:#10151c;border:1px solid #232a34;border-radius:8px;padding:10px 12px;margin-bottom:14px}}
.filterbar .ft{{cursor:pointer;display:inline-block;border-radius:10px;font-size:11px;padding:2px 9px;margin:3px;border:1px solid color-mix(in srgb,var(--tc) 45%,#0b0f14);color:var(--tc);background:#0b0f14;user-select:none}}
.filterbar .ft.on{{background:var(--tc);color:#0b0f14;font-weight:700}}
.filterbar .grp{{margin:4px 0}} .filterbar .glabel{{color:#8b949e;font-size:11px;margin-right:6px;display:inline-block;width:54px}}
#clearf{{cursor:pointer;color:#ff8893;font-size:11px;margin-left:8px;text-decoration:underline}}
#cnt{{color:#8b949e;font-size:12px;margin-left:8px}}
table{{width:100%;border-collapse:collapse}}
th,td{{border:1px solid #232a34;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#10151c;position:sticky;top:0;font-size:11px}}
.pos{{font-weight:700;color:var(--c);font-size:12px}}
.lik{{font-weight:700;text-align:center}}
code{{background:#0b0f14;padding:1px 4px;border-radius:4px;font-size:11px}}
.lnk a{{color:#4ea1ff;text-decoration:none;font-size:10px;margin-right:4px}}
.muted{{color:#8b949e}} .seen{{font-size:11px}}
tr.inact td{{background:#1c1410}}  /* 直近14日 取引なし＝薄い琥珀の地色 */
tr.inact td:first-child{{box-shadow:inset 3px 0 #6b5535}}
.verdict{{font-size:11px;max-width:360px}}
.spark{{font-size:10px;margin-top:3px}}
.nansen{{font-size:11px;margin-top:4px;padding:3px 6px;background:#16142a;border-left:2px solid #7c5cff;border-radius:4px}}
.notejp{{font-size:12px;line-height:1.65;padding:7px 9px;background:#0f1b17;border-left:3px solid #3fb950;border-radius:5px;color:#d7e6dd}}
.verdict{{max-width:440px}}
</style></head><body>
<h1>{esc(title)}</h1>
<div class="sub">更新: {esc(reg.get('updated_at',''))} ／ 累計実行 {esc(reg.get('run_count'))}回 ／ 表示 {len(wallets)} ウォレット ／
<a href="index.html" style="color:#4ea1ff">トップ</a> ・ <a href="registry.html" style="color:#4ea1ff">インサイダー</a> ・ <a href="pros.html" style="color:#4ea1ff">プロ一覧</a></div>
<div class="sub" style="margin-top:-8px;font-size:11.5px">💡 <b>2つの損益は別物</b>: <b>majors損益</b>＝BTC/ETH/SOL約定のclosedPnl合計（取引のみ・高頻度勢は約定膨大で<u>過小</u>）／ <b>HL公式通算</b>＝HLリーダーボードの通算純損益（全銘柄＋funding込みの<u>総額・最も信頼できる</u>）。差分は alt・spot・funding。majors実力はmajors損益、総稼ぎはHL公式で見る。</div>
<div class="chips">{chips}</div>
<div class="filterbar">{filterbar}
  <div style="margin-top:6px"><span id="clearf">✕ フィルタ解除</span><span id="cnt"></span>
  <span class="muted" style="font-size:11px;margin-left:8px">※複数選択はAND（すべて満たす行）</span></div>
</div>
<table id="reg">
<tr><th>位置づけ</th><th>濃度</th><th>アドレス</th><th>数値分類</th><th>勝率/的中</th><th>majors損益<br><span style="font-weight:400;color:#8b949e">実現(取引)</span></th><th>HL公式通算<br><span style="font-weight:400;color:#8b949e">総額(全銘柄+funding)</span></th><th>全実現損益<br><span style="font-weight:400;color:#8b949e">全銘柄closedPnl(旧Tier)</span></th><th>ROI(全期)</th><th>取引数(14日)</th><th>取引期間(HL履歴)</th><th>保有h</th><th>タグ</th><th>観測</th><th>多角判定 / 履歴(直近)</th></tr>
{rows}
</table>
<script>
const sel=new Set();
const rows=[...document.querySelectorAll('#reg tr[data-tags]')];
const cnt=document.getElementById('cnt');
function apply(){{
  let shown=0;
  rows.forEach(r=>{{
    const tags=r.getAttribute('data-tags');
    const ok=[...sel].every(t=>tags.includes(t));
    r.style.display=ok?'':'none'; if(ok)shown++;
  }});
  cnt.textContent=sel.size? `絞り込み: ${{shown}} / ${{rows.length}} 件`:'';
}}
document.querySelectorAll('.ft').forEach(ft=>{{
  ft.addEventListener('click',()=>{{
    const t=ft.getAttribute('data-t');
    if(sel.has(t)){{sel.delete(t);ft.classList.remove('on');}}
    else{{sel.add(t);ft.classList.add('on');}}
    apply();
  }});
}});
document.getElementById('clearf').addEventListener('click',()=>{{
  sel.clear();document.querySelectorAll('.ft.on').forEach(x=>x.classList.remove('on'));apply();
}});
</script>
</body></html>"""

    path = os.path.join(config.HERE, out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_doc)


# プロ系ポジション（行動で検証した層・専用ページ）
PRO_POSITIONS = {"プロトレーダー(本物)", "alt主体プロ", "プロトレーダー(未精査)"}
# Nansenラベルで仮置きしただけの未検証候補（行動未分析）。ラベルでは分類しない方針ゆえ中立枠。
CANDIDATE_POSITIONS = {"Nansen候補(HL未検証)"}
# 除外/低優先（MM・ノイズ・履歴なし・偽陽性等）。別ページへ。
EXCLUDED_POSITIONS = {"除外/低優先", "偽陽性(数値疑惑→否定)"}
# 高頻度MM/HFT は専用ページ(mm.html)。監視台帳(インサイダー疑惑)からは除外する。
MM_POSITIONS = {"高頻度MM"}


def render_all(reg):
    """3ページ生成: メイン台帳(インサイダー/疑惑) / プロ / 除外・低優先。
    （Nansen候補は全件HL検証で振り分け済み＝枠消滅。万一再発生しても疑惑ページは汚さぬよう drop に残す）"""
    render_html(reg, out="registry.html",
                title="🔴 インサイダー（要監視）",
                drop=PRO_POSITIONS | CANDIDATE_POSITIONS | EXCLUDED_POSITIONS | MM_POSITIONS)
    render_html(reg, out="pros.html",
                title="プロ一覧（HL行動で検証した実力層・Vault運用者）", only=PRO_POSITIONS)
    render_html(reg, out="mm.html",
                title="高頻度MM / HFT（薄利多売・方向性でない・コピー不能）", only={"高頻度MM"})
    render_html(reg, out="excluded.html",
                title="除外・低優先（MM/ノイズ/履歴なし/偽陽性）", only=EXCLUDED_POSITIONS)


if __name__ == "__main__":
    main()
