"""Task Scheduler / cron / GCP CE 用ランナー: 自動発掘(決定的部分)を実行し GitHub Pages へ push する。

Claude不要・headless・無料。git pull(最新台帳取得)→新規発掘→分類→台帳追記→watch_publish→再描画→push。
欺瞞候補(要精査)は data/discovery_flagged.json に貯まり、後でworkflow裁定する(Claude起動時)。

使い方: python run_discovery.py [--min 500000] [--cap 150]
GCP CE で日本時間0時(=UTC15:00)に実行する cron 例:
  0 15 * * * cd ~/repo && /usr/bin/python3 run_discovery.py --min 500000 --cap 150 >> ~/discovery.log 2>&1
"""
import os
import sys
import glob
import subprocess
from datetime import datetime, timezone

import config

HERE = config.HERE


def prune_cache():
    """約定キャッシュ(data/fills)を掃除。VMはキャッシュ保持不要ゆえ累積を防ぐ。
    ※ローカル(12GBキャッシュを使う環境)では実行しないよう DISCOVERY_PRUNE=1 の時だけ削除。"""
    if os.environ.get("DISCOVERY_PRUNE") != "1":
        return
    n = 0
    for f in glob.glob(os.path.join(HERE, "data", "fills", "*.json")):
        try:
            os.remove(f)
            n += 1
        except Exception:
            pass
    if n:
        print(f"約定キャッシュ掃除: {n}ファイル削除(ディスク累積防止)")


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE)


def main():
    extra = sys.argv[1:]
    # 最新の台帳を取得してから発掘（複数環境からの編集と協調）
    git("pull", "--rebase", "--autostash", "origin", "main")
    subprocess.run([sys.executable, "discover_and_classify.py", *extra], cwd=HERE)
    # 既存ウォレットを軽量差分更新（実現損益/最終取引日/active14/赤字転落の再分類）
    subprocess.run([sys.executable, "update_existing.py"], cwd=HERE)
    # 監視リストも最新化（リアルタイム監視デーモンが次回読込で拾えるよう）
    subprocess.run([sys.executable, "watch_publish.py"], cwd=HERE)
    # 全ページ再描画（差分更新を反映）
    import json as _json
    import step6_registry as _reg6
    _reg6.render_all(_json.load(open(f"{HERE}/data/wallet_registry.json", encoding="utf-8")))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    git("add", "-A")
    r = git("commit", "-q", "-m", f"chore: 自動発掘サイクル {ts}UTC [skip ci]")
    if r.returncode == 0:
        # 競合時は一度だけ rebase してから再push
        if git("push", "-q").returncode != 0:
            git("pull", "--rebase", "--autostash", "origin", "main")
            git("push", "-q")
        print("push完了")
    else:
        print("新規追加なし(commitなし)")
    prune_cache()      # VMのディスク累積防止（DISCOVERY_PRUNE=1の時のみ）


if __name__ == "__main__":
    main()
