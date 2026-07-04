#!/usr/bin/env python3
"""日次(ローカルWindows): alpha_score.py 実行 → alpha_scores.json を VM ~/repo へ push(scp+commit)。

Windowsタスクスケジューラ 9:20 で実行(9:00 の sync_flow_archive.py の後=最新アーカイブで採点)。
VM側は次の run_discovery(JST0:00)の git操作で拾い、alpha_merge.py が台帳へ反映する(遅効15hラグ許容)。
秘密は動かさない・HL叩くのは alpha_score.py の足取得のみ(別IP・キャッシュ優先)。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VM, ZONE = "dlmm-bot-new", "us-central1-f"


def run(cmd, shell=False):
    # encoding明示=Windows既定cp932でのUTF-8日本語デコード失敗を防ぐ
    return subprocess.run(cmd, shell=shell, cwd=HERE, capture_output=True,
                          encoding="utf-8", errors="replace")


def main():
    r = run([sys.executable, "alpha_score.py"])
    print((r.stdout or "")[-2000:])
    if r.returncode != 0:
        print("alpha_score 失敗:", (r.stderr or "")[-500:])
        sys.exit(1)
    local = os.path.join(HERE, "data", "alpha_scores.json")
    if not os.path.exists(local):
        print("alpha_scores.json が生成されていない")
        sys.exit(1)
    scp = f'gcloud compute scp "{local}" "{VM}:/home/Matsuya131/repo/data/alpha_scores.json" --zone {ZONE}'
    r2 = run(scp, shell=True)   # gcloudは.cmd/.ps1ラッパゆえshell経由(sync_flow_archive.pyと同流儀)
    if r2.returncode != 0:
        print("scp 失敗:", (r2.stderr or r2.stdout)[-300:])
        sys.exit(1)
    commit = (f'gcloud compute ssh {VM} --zone {ZONE} --command '
              f'"cd ~/repo && git add data/alpha_scores.json && git commit -q -m alpha-update || true"')
    run(commit, shell=True)     # コミット無し(差分ゼロ)は || true で無害
    print("alpha_scores.json を VM ~/repo/data へ push 完了")


if __name__ == "__main__":
    main()
