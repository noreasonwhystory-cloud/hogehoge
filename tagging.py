"""ウォレットを多軸で自動タグ付けする共通モジュール。

derive_tags(d) は正規化済み dict を受け、'カテゴリ:値' 形式のタグ配列を返す。
d のキー: roi(小数,1.0=100%) / pnl(USD) / n_fills / avg_hold_h /
          held(list[{coin,side}]) / pos_value / account_value / leverage(任意) /
          top_coins(dict) / labels(list)
"""


def roi_tier(roi):
    if roi is None:
        return None
    if roi >= 10:
        return "ROI:超高(>1000%)"
    if roi >= 1:
        return "ROI:高(100-1000%)"
    if roi >= 0.1:
        return "ROI:中(10-100%)"
    if roi >= 0:
        return "ROI:低(<10%)"
    return "ROI:赤字"


def pnl_tier(pnl):
    if pnl is None:
        return None
    if pnl >= 10_000_000:
        return "PnL:メガ(>$10M)"
    if pnl >= 1_000_000:
        return "PnL:大($1-10M)"
    if pnl >= 100_000:
        return "PnL:中($100k-1M)"
    if pnl >= 0:
        return "PnL:小(<$100k)"
    return "PnL:赤字"


def freq_tier(n_fills):
    if n_fills is None:
        return None
    if n_fills > 3000:
        return "頻度:HFT(>3000)"
    if n_fills >= 500:
        return "頻度:多(500+)"
    if n_fills >= 50:
        return "頻度:中(50+)"
    return "頻度:少(<50)"


def hold_tier(h):
    if h is None:
        return None
    if h < 2:
        return "保有:スキャルプ(<2h)"
    if h < 12:
        return "保有:短期(<12h)"
    if h < 48:
        return "保有:スイング(<48h)"
    return "保有:長期(>48h)"


def side_tag(held):
    if not held:
        return "方向:無/フラット"
    sides = {h.get("side") for h in held}
    if sides == {"short"}:
        return "方向:ショート"
    if sides == {"long"}:
        return "方向:ロング"
    return "方向:混在"


def lev_tag(d):
    lev = d.get("leverage")
    if lev is None:
        pv, acct = d.get("pos_value"), d.get("account_value")
        lev = (pv / acct) if (pv and acct) else None
    if lev is None or lev <= 0:
        return None
    if lev >= 10:
        return "レバ:高(>10x)"
    if lev >= 3:
        return "レバ:中(3-10x)"
    return "レバ:低(<3x)"


def coin_tag(held, top_coins):
    coins = []
    if top_coins:
        coins = list(top_coins.keys())
    elif held:
        coins = [h.get("coin") for h in held]
    coins = [c for c in coins if c]
    if not coins:
        return None
    uniq = list(dict.fromkeys(coins))
    if len(uniq) == 1:
        return f"銘柄:{uniq[0]}専"
    if len(uniq) >= 4:
        return "銘柄:マルチ"
    return f"銘柄:{uniq[0]}主"


def ens_tag(labels):
    for l in (labels or []):
        s = str(l)
        if ".eth" in s or "OpenSea" in s:
            return "ID:ENS有"
    return None


def derive_tags(d):
    """正規化 dict から多軸タグを生成。"""
    tags = []
    for t in (roi_tier(d.get("roi")), pnl_tier(d.get("pnl")),
              freq_tier(d.get("n_fills")), hold_tier(d.get("avg_hold_h")),
              side_tag(d.get("held")), lev_tag(d),
              coin_tag(d.get("held"), d.get("top_coins")), ens_tag(d.get("labels"))):
        if t:
            tags.append(t)
    return tags


# タグの色（カテゴリ接頭辞で決定）。registry.html 用。
def tag_color(tag):
    table = {
        "ROI:": "#c77dff", "PnL:": "#3fb950", "頻度:": "#4ea1ff",
        "保有:": "#2dd4bf", "方向:": "#ffb454", "レバ:": "#ff7d6c",
        "銘柄:": "#9aa3ad", "ID:": "#ffd24a", "cluster": "#ff5d6c",
        "手動": "#8b5cf6", "funder": "#5c636d",
    }
    for k, v in table.items():
        if tag.startswith(k):
            return v
    return "#5c636d"
