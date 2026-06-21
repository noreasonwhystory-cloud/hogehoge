"""監視の自動化: monitor.py を一定間隔で回し、live.html を更新して任意でGitHub Pagesへ反映する。

一度起動すれば常駐し、INTERVAL秒ごとに巡回→live.html更新→(--push時)git commit&push。
公開サイト(GitHub Pages)のライブ監視ページを自動で最新に保つ。

使い方:
  python monitor_loop.py                 # 5分毎にローカル巡回(live.html更新のみ)
  python monitor_loop.py --push          # 巡回毎に live.html/feed を commit&push
  python monitor_loop.py --interval 180 --push --watch all
  Ctrl+C で停止。

Windowsタスクスケジューラで常駐させる例(管理者PowerShell):
  schtasks /create /tn "hl-monitor" /tr "python C:\\...\\nansen\\monitor_loop.py --push" /sc onstart /ru SYSTEM
"""
import sys
import time
import subprocess
from datetime import datetime, timezone

import config

INTERVAL = 300
if "--interval" in sys.argv:
    try:
        INTERVAL = int(sys.argv[sys.argv.index("--interval") + 1])
    except Exception:
        pass
PUSH = "--push" in sys.argv
# monitor.py へ引き継ぐ引数(--watch all 等)
PASS = [a for a in sys.argv[1:] if a in ("--watch", "all", "min", "std")]


def run_once():
    subprocess.run([sys.executable, "monitor.py", *PASS], cwd=config.HERE)
    if PUSH:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "add", "live.html", "data/monitor_feed.jsonl", "data/monitor_state.json"],
                       cwd=config.HERE)
        r = subprocess.run(["git", "commit", "-q", "-m", f"chore: ライブ監視更新 {ts}UTC [skip ci]"],
                           cwd=config.HERE)
        if r.returncode == 0:
            subprocess.run(["git", "push", "-q"], cwd=config.HERE)


def main():
    print(f"監視ループ開始: 間隔{INTERVAL}秒 / push={'ON' if PUSH else 'OFF'} / 引数{PASS or '(標準)'}")
    while True:
        try:
            run_once()
        except Exception as ex:                 # 巡回失敗でループは止めない
            print(f"  巡回エラー(継続): {ex}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
