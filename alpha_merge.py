#!/usr/bin/env python3
"""alpha_scores.json(ローカルalpha_score.pyが生成→git push)を台帳へ反映(VM・run_discovery内)。

 - registry[wallet]["alpha"] に採点結果を書き戻す(表示用)
 - auto_tags に「タイミング:妙手/暫定妙手/逆指標」を冪等付与(既存タイミング:を剥がして付け直す)
 - 前回からの昇降格差分を ALERT_HOOK(env)へ1メッセージ集約通知(件数上限)
 - 全例外はトップレベルで捕捉→_alert+exit 0(run_stepのsys.exit(1)で発掘サイクル全体を殺さない)

採点はローカルWindows(alpha_score.py)専任。ここは反映のみ=HL API呼び出しゼロ。
"""
import json
import os
import sys
from datetime import datetime, timezone

import config

ALPHA_PATH = os.path.join(config.DATA_DIR, "alpha_scores.json")
REG_PATH = os.path.join(config.DATA_DIR, "wallet_registry.json")
TAGS = {"妙手": "タイミング:妙手", "暫定妙手": "タイミング:暫定妙手", "逆指標": "タイミング:逆指標"}
NOTIFY_CAP = 20   # 通知に載せる最大件数(regime切替日の一斉変化スパム対策)


def _alert(msg):
    print(msg)
    hook = os.environ.get("ALERT_HOOK")
    if hook:
        try:
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(
                hook, data=json.dumps({"content": msg[:1900]}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
        except Exception:
            pass


def _run():
    if not os.path.exists(ALPHA_PATH):
        print("alpha_scores.json 無し(まだ生成前/未push?)→skip")
        return
    payload = json.load(open(ALPHA_PATH, encoding="utf-8"))
    scores = payload.get("scores", {})
    meta = payload.get("meta", {})
    if not scores:
        print("alpha_scores.json にスコア無し→skip")
        return
    reg = json.load(open(REG_PATH, encoding="utf-8"))
    W = reg["wallets"]
    gen = meta.get("generated_ms")
    promoted, demoted, cleared = [], [], []
    n_written = 0
    for a, sc in scores.items():
        e = W.get(a)
        if not e:
            continue   # 台帳未登録(監視外)はスキップ
        prev_tag = (e.get("alpha") or {}).get("tag")
        new_tag = sc.get("tag")
        e["alpha"] = {"alpha24h": sc.get("alpha24h"), "n": sc.get("n"), "wr": sc.get("wr"),
                      "p24h": sc.get("p24h"), "ewma": sc.get("ewma"), "streak": sc.get("streak"),
                      "n_clusters": sc.get("n_clusters"), "tag": new_tag, "updated": gen}
        at = [t for t in e.get("auto_tags", []) if not t.startswith("タイミング:")]   # 冪等: 旧タイミング:剥がし
        if new_tag in TAGS:
            at.append(TAGS[new_tag])
        e["auto_tags"] = at
        n_written += 1
        if new_tag != prev_tag:
            if new_tag in ("妙手", "暫定妙手"):
                promoted.append((a, new_tag, sc))
            elif new_tag == "逆指標":
                demoted.append((a, new_tag, sc))
            elif prev_tag in TAGS and not new_tag:
                cleared.append((a, prev_tag))

    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = REG_PATH + ".tmp"
    json.dump(reg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, REG_PATH)
    print(f"alpha反映: {n_written}件書込 / 昇格{len(promoted)} 逆指標{len(demoted)} 取消{len(cleared)} "
          f"(regime={meta.get('regime')} span={meta.get('span_days')}d)")

    if promoted or demoted:
        lines = [f"🎯 タイミング採点更新 (regime={meta.get('regime')} / {meta.get('span_days')}d / "
                 f"MMネガコン偽発見={meta.get('mm_negcontrol_fdr10')}/{meta.get('mm_wallets')})"]
        for a, tag, sc in promoted[:NOTIFY_CAP]:
            lb = (W[a].get("labels") or [""])[0] or W[a].get("position")
            lines.append(f"🟢 {tag}: {a[:10]} {lb} α24h={sc.get('alpha24h')}bps "
                         f"n={sc.get('n')} wr={sc.get('wr')}% p={sc.get('p24h')}")
        for a, tag, sc in demoted[:NOTIFY_CAP]:
            lines.append(f"🔴 {tag}: {a[:10]} α24h={sc.get('alpha24h')}bps n={sc.get('n')}")
        extra = max(0, len(promoted) + len(demoted) - 2 * NOTIFY_CAP)
        if extra:
            lines.append(f"…他{extra}件")
        _alert("\n".join(lines))


def main():
    try:
        _run()
    except Exception as e:
        _alert(f"[alpha_merge] 失敗(発掘は継続): {type(e).__name__}: {str(e)[:200]}")
    sys.exit(0)   # 常に成功終了=run_stepで発掘サイクルを止めない


if __name__ == "__main__":
    main()
