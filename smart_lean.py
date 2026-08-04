#!/usr/bin/env python3
"""毎朝「スマート層の傾き」観察要約を Discord へ投稿(VM ~/hl/ で日次cron)。

★重要な位置づけ: これは**観察材料**であって自動発注シグナルではない。
EA研究(project_nansen_ea_research)で「機械的追随は手数料負け=トリガー不可」を証明済。
本要約は人間が複数材料の一つとして見る参考に留め、message末尾に免責を必ず付す。

集計: flow_arch(全銘柄szi差分アーカイブ)直近LEAN_DAYS日の
 スマート層(プロ本物/alt主体プロ/弱い疑惑・MM除外)の coin別 net signed usd(買+/売-)+買売イベント数。
 MM の net で「混雑」も算出(逆張り注意材料)。
品質フィルタ: coinあたり smart イベント>=MIN_EVENTS のみ(薄いノイズ除外)。メジャーは常時表示。

秘密: webhookは ~/hl/hl.env のみ(スクリプトが自前パース)。優先 LEAN_HOOK→HOOK_INSIDER→無ければ投稿せずログ。
使い方: python smart_lean.py [--dry-run]
"""
import glob
import gzip
import json
import os
import sys
import time
import urllib.request

HL_DIR = os.path.expanduser("~/hl")
ARCH_DIR = os.path.join(HL_DIR, "flow_arch")
ENV_PATH = os.path.join(HL_DIR, "hl.env")
SMART = {"プロトレーダー(本物)", "alt主体プロ", "弱い疑惑(監視継続)"}
MAJORS = ["BTC", "ETH", "SOL"]
BUY = {"Open Long", "Close Short", "Short > Long"}
LEAN_DAYS = int(os.environ.get("LEAN_DAYS", "3"))
MIN_EVENTS = int(os.environ.get("LEAN_MIN_EVENTS", "3"))
TOP_ALT = int(os.environ.get("LEAN_TOP_ALT", "8"))


def read_env(path):
    """hl.env を KEY=VALUE でパース(cronはenvを継がないため自前で読む)。"""
    d = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return d


def iter_lines(path):
    op = gzip.open if path.endswith(".gz") else open
    try:
        with op(path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line
    except Exception:
        return


def aggregate(days):
    """直近 days 日を集計 → (smart_net{coin:usd}, smart_cnt{coin:[buy,sell]}, mm_net{coin:usd})。"""
    floor = time.time() * 1000 - days * 86400 * 1000
    smart_net, mm_net = {}, {}
    smart_cnt = {}
    files = sorted(glob.glob(os.path.join(ARCH_DIR, "flow-*.jsonl")) +
                   glob.glob(os.path.join(ARCH_DIR, "flow-*.jsonl.gz")))[-(days + 2):]
    for p in files:
        for line in iter_lines(p):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("t", 0) < floor:
                continue
            usd = e.get("usd", 0) or 0
            sgn = 1 if e.get("dir") in BUY else -1
            coin = e.get("coin")
            pos = e.get("pos")
            if pos in SMART:
                smart_net[coin] = smart_net.get(coin, 0) + sgn * usd
                c = smart_cnt.setdefault(coin, [0, 0])
                c[0 if sgn > 0 else 1] += 1
            elif pos == "高頻度MM":
                mm_net[coin] = mm_net.get(coin, 0) + sgn * usd
    return smart_net, smart_cnt, mm_net


def _line(coin, net, cnt):
    b, s = cnt
    tot = b + s
    arrow = "🟢買い" if net > 0 else "🔴売り"
    conv = ""
    if tot >= MIN_EVENTS:
        r = b / tot if tot else 0.5
        if r >= 0.8 or r <= 0.2:
            conv = " ★強い確信"
    return f"`{coin:<9}` {arrow} 純${net:>+12,.0f} (買{b}/売{s}){conv}"


def build_message(smart_net, smart_cnt, mm_net):
    d = time.strftime("%m/%d", time.gmtime(time.time() + 9 * 3600))
    out = [f"**🧭 スマート層の傾き 観察 ({d} JST・直近{LEAN_DAYS}日)**",
           "*※プロ/alt主体プロ/弱い疑惑の建玉szi差分の純フロー(買い+/売り-)。MM除外。*", ""]
    # メジャーは常時表示
    out.append("__メジャー__")
    for coin in MAJORS:
        if coin in smart_net:
            out.append(_line(coin, smart_net[coin], smart_cnt.get(coin, [0, 0])))
        else:
            out.append(f"`{coin:<9}` — (直近スマート建玉変化なし)")
    # alt: イベント数>=MIN_EVENTS かつ非メジャーを|net|降順
    alts = [(c, v) for c, v in smart_net.items()
            if c not in MAJORS and sum(smart_cnt.get(c, [0, 0])) >= MIN_EVENTS]
    alts.sort(key=lambda x: -abs(x[1]))
    if alts:
        out.append("")
        out.append(f"__alt 上位(イベント{MIN_EVENTS}件以上)__")
        for coin, v in alts[:TOP_ALT]:
            out.append(_line(coin, v, smart_cnt.get(coin, [0, 0])))
    # MM混雑(逆張り注意)
    mm = sorted(mm_net.items(), key=lambda x: -abs(x[1]))[:3]
    if mm:
        out.append("")
        out.append("__MM混雑(片側殺到=逆張り注意)__")
        for coin, v in mm:
            out.append(f"`{coin:<9}` {'買偏' if v > 0 else '売偏'} 純${v:>+15,.0f}")
    # 免責(必須)
    out.append("")
    out.append("> ⚠ **観察材料であり自動発注シグナルではない。** 機械的追随は手数料負け(検証済)。"
               "単一レジームの写像ゆえ重みは低く・薄いaltは板薄でノイズ・自分のテーゼに添える程度に。")
    return "\n".join(out)


def post(hook, content):
    try:
        req = urllib.request.Request(
            hook, data=json.dumps({"content": content[:1990]}).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "smart-lean/1.0"})  # 既定Python-urllibはDiscord/CFが403ゆえUA必須
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print("post失敗:", str(e)[:150])
        return False


def main():
    dry = "--dry-run" in sys.argv
    smart_net, smart_cnt, mm_net = aggregate(LEAN_DAYS)
    if not smart_net and not mm_net:
        print("直近スマート/MMフローなし→skip")
        return
    msg = build_message(smart_net, smart_cnt, mm_net)
    if dry:
        print(msg)
        return
    env = read_env(ENV_PATH)
    hook = env.get("LEAN_HOOK") or env.get("HOOK_INSIDER") or os.environ.get("LEAN_HOOK", "")
    if not hook:
        print("webhook未設定(LEAN_HOOK/HOOK_INSIDER)→投稿せずログのみ\n" + msg)
        return
    ok = post(hook, msg)
    print(f"投稿{'成功' if ok else '失敗'} ({'LEAN_HOOK' if env.get('LEAN_HOOK') else 'HOOK_INSIDER'})")


if __name__ == "__main__":
    main()
