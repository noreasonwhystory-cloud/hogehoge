"""Step8: これまでの Nansen 結果を「猿でもわかる」平易な日本語で台帳に書き込む。

各ウォレットの notes_jp を生成（正体・お金の出どころ・取引相手・出金・1段上流）。
入力: data/wallet_registry.json（Nansen結果） + data/ranked.json（数値）
出力: registry に notes_jp を保存し、registry.html を再描画。
"""
import json
import config
import step6_registry as reg6

REG = f"{config.DATA_DIR}/wallet_registry.json"

CEX = ["Binance", "Coinbase", "OKX", "Bybit", "Kraken", "Bitget", "KuCoin",
       "Nexo", "Gate", "HTX", "Crypto.com", "MEXC", "Gemini"]
BRIDGE = ["Across", "Stargate", "Hop", "Orbiter", "Socket", "Relay", "Bridge",
          "Spoke", "Refuel", "deBridge", "Wormhole"]
CONTRACT = ["🤖", "Deployer", "Factory", "Router", "Pool", "Contract", "Vault",
            "Proxy", "Mastercopy", "Solver", "Gas.zip", "Symmio", "Enzyme"]

# 資金源を1段遡った結果（trace_funders.py で取得済・手で確定）
FUNDER_TRACE = {
    "0xefcace57934a753d66c48a31b880ba806d4c0869":
        "お金の出どころは Kromatika というプログラム(コントラクト)経由で、人の財布までは遡れなかった（足を消している）。",
    "0x10b6f072fabe21bc72ffd77ff5d4414e6cef2980":
        "お金は『お金持ち財布(0x9aa91e)』から来ていて、その財布はさらに Nexo(取引所)から入金され、Binance・Bybit・KuCoin と数百万ドル規模でやり取りしている。→ 実体は複数の取引所を使う大口の個人/組織。",
    "0xa351db10472a07059ea099e0581444e568bff894":
        "お金は Across という別チェーンからの『橋(ブリッジ)』を渡ってきており、橋の向こう側は別チェーンなのでこの先は辿れない（足を消している）。",
}


def usd(x):
    try:
        return f"${x:,.0f}"
    except (TypeError, ValueError):
        return "不明"


def classify_funder(label):
    s = label or ""
    if any(k.lower() in s.lower() for k in CEX):
        name = next((k for k in CEX if k.lower() in s.lower()), "取引所")
        return f"{name}(取引所)から入金 → 取引所が『この人が誰か』を知っている"
    if any(k.lower() in s.lower() for k in BRIDGE):
        return "別チェーンからの『橋(ブリッジ)』経由 → 足を消しており追いにくい"
    if any(k in s for k in CONTRACT):
        return "プログラム(コントラクト)経由 → 人の財布までは見えない"
    if ".eth" in s or "OpenSea" in s:
        return f"名前付きの個人財布（{s}）から"
    if s:
        return f"別の財布（{s}）から"
    return "不明な財布から"


def pos_oneliner(pos):
    return {
        "インサイダー疑惑(要監視)": "🔴 インサイダー（内部情報で先回り）が一番疑わしいウォレット。",
        "弱い疑惑(監視継続)": "🟠 ちょっと怪しい。様子見で監視するウォレット。",
        "💸 出金疑い(要監視)": "💰 大きく稼いで、お金をほぼ全部 外に引き出したっぽいウォレット。",
        "プロトレーダー(本物)": "🟢 ちゃんと実力で勝っている本物のプロ。",
        "プロトレーダー(未精査)": "🔵 たぶんプロ。まだ詳しく調べていない。",
        "プロ格付け過大(要再検証)": "🟣 プロ扱いだったが、実は無謀な賭けで稼いだだけかも。",
        "偽陽性(数値疑惑→否定)": "⚪ 数字だけ見ると怪しかったが、調べたらシロだった。",
        "高頻度HFT/MM(手動追加)": "⚙ 1日何千回も売買する自動bot/マーケットメイカー。手動で追加。",
        "要再検証(数値疑惑・未レビュー)": "⚪ 数字上は怪しいが未レビュー。",
        "除外/低優先": "・優先度は低い。",
    }.get(pos, pos)


def num_for(addr, e, ranked):
    """allTime PnL / 現在残高 / cashout比 を ranked か hl_profile から拾う。"""
    w = ranked.get(addr)
    if w:
        at = (w.get("lb_windows") or {}).get("allTime") or {}
        return at.get("pnl"), w.get("account_value"), w.get("cashout_ratio")
    hp = e.get("hl_profile") or {}
    at = e.get("lb_allTime") or {}
    acct = hp.get("account_value")
    pnl = at.get("pnl")
    ratio = round(pnl / acct, 1) if (pnl and acct) else None
    return pnl, acct, ratio


def make_note(addr, e, ranked):
    lines = []
    lines.append("【ひとことで】" + pos_oneliner(e.get("position", "")))

    # 正体
    labs = e.get("labels") or []
    if labs:
        lines.append("【正体】" + "、".join(labs) + "（Nansenが付けた名前/ラベル）")
    else:
        lines.append("【正体】名前は分からない（匿名）。")

    # お金の出どころ
    ff = e.get("first_funders") or []
    if ff:
        primary = ff[0]
        lines.append("【お金はどこから？】" + classify_funder(primary.get("label") or primary.get("address", "")))
    else:
        lines.append("【お金はどこから？】不明。")

    # 1段上流（追跡済みなら）
    if addr in FUNDER_TRACE:
        lines.append("【もう一段たどると】" + FUNDER_TRACE[addr])

    # 取引相手
    cps = e.get("counterparties") or []
    if cps:
        names = []
        for c in cps[:3]:
            lbl = c.get("label") or (c.get("address") or "")[:10]
            names.append(lbl)
        lines.append("【よく取引する相手】" + "、".join(n for n in names if n))

    # 出金の話
    pnl, acct, ratio = num_for(addr, e, ranked)
    if ratio and ratio >= 10:
        lines.append(f"【出金の話】今までに合計 {usd(pnl)} 稼いだのに、今の残高は {usd(acct)} しかない（=稼ぎをほぼ外に出した。比 {ratio:.0f}倍）。")

    # クラスタ
    if "cluster-A" in (e.get("tags") or []):
        lines.append("【グループ】同じ資金元から同時に動いた『協調クラスタA』の一員。")

    # 多角分析の判定（あれば）
    if e.get("alt_verdict"):
        lines.append("【詳しい判定】" + e["alt_verdict"])

    return "\n".join(lines)


def main():
    reg = json.load(open(REG, encoding="utf-8"))
    wallets = reg["wallets"]
    ranked = {w["address"].lower(): w
              for w in json.load(open(f"{config.DATA_DIR}/ranked.json", encoding="utf-8"))["wallets"]}

    n = 0
    for addr, e in wallets.items():
        # Nansen照会済み or 手動CA のものに説明を付ける
        if not (e.get("nansen_checked") or e.get("labels") or e.get("first_funders")):
            continue
        e["notes_jp"] = make_note(addr, e, ranked)
        n += 1

    with open(REG, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    reg6.render_all(reg)
    print(f"平易な説明(notes_jp)を {n} 件に記載 → registry.html 再描画完了")


if __name__ == "__main__":
    main()
