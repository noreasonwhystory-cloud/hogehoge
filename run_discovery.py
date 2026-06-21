"""Task Scheduler / cron 用ランナー: 自動発掘(決定的部分)を実行し、結果をGitHub Pagesへpushする。

Claude不要・完全headless・無料。新規アドレスの発掘→既存基準で分類(MM/本物/alt/除外)→台帳化→再描画→push。
欺瞞候補(要精査)は data/discovery_flagged.json に貯まり、後でworkflow裁定する(Claude起動時)。

使い方: python run_discovery.py [--min 500000] [--cap 150]
Windowsタスクスケジューラ例(毎週月曜09:07):
  schtasks /create /tn "hl-discovery" /sc weekly /d MON /st 09:07 /tr "python C:\\...\\nansen\\run_discovery.py"
"""
import sys
import subprocess
from datetime import datetime, timezone

import config


def main():
    extra = sys.argv[1:]
    subprocess.run([sys.executable, "discover_and_classify.py", *extra], cwd=config.HERE)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    subprocess.run(["git", "add", "-A"], cwd=config.HERE)
    r = subprocess.run(["git", "commit", "-q", "-m", f"chore: 自動発掘サイクル {ts}UTC [skip ci]"], cwd=config.HERE)
    if r.returncode == 0:
        subprocess.run(["git", "push", "-q"], cwd=config.HERE)
        print("push完了")
    else:
        print("新規追加なし(commitなし)")


if __name__ == "__main__":
    main()
