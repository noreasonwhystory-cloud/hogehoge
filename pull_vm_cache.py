"""VM(CE)が発掘で溜めた約定キャッシュをローカルの12GBキャッシュへ吸い上げ、VM側を空にする。

PC起動時に手動実行する想定。完全無料・ローカルに永久蓄積・VMディスクは肥大化しない。
 1) VMの ~/repo/data/fills を tar 圧縮
 2) ローカルへ scp して data/fills/ に展開(マージ)
 3) VM側の data/fills を削除(ディスク解放)

使い方: python pull_vm_cache.py
前提: gcloud 認証済み・VM名/zoneは下記定数。
"""
import os
import subprocess

import config

VM = "dlmm-bot-new"
ZONE = "us-central1-f"
REMOTE_FILLS = "/home/Matsuya131/repo/data/fills"
LOCAL_FILLS = os.path.join(config.DATA_DIR, "fills")
TGZ_LOCAL = os.path.join(config.DATA_DIR, "_vmfills.tgz")


def ssh(cmd):
    return subprocess.run(["gcloud", "compute", "ssh", VM, "--zone", ZONE, "--command", cmd],
                          capture_output=True, text=True)


def main():
    # VM側の件数確認＋tar
    r = ssh(f"ls {REMOTE_FILLS}/*.json 2>/dev/null | wc -l; "
            f"tar czf /tmp/vmfills.tgz -C {os.path.dirname(REMOTE_FILLS)} fills 2>/dev/null && echo TARDONE")
    out = (r.stdout or "").strip().splitlines()
    n = next((l for l in out if l.strip().isdigit()), "0")
    if not any("TARDONE" in l for l in out) or n == "0":
        print(f"VMキャッシュ: {n}件 — 取り込むものなし(または空)")
        return
    print(f"VMキャッシュ {n}件 を取り込み中…")
    # scp 圧縮ファイルをローカルへ
    subprocess.run(["gcloud", "compute", "scp", f"{VM}:/tmp/vmfills.tgz", TGZ_LOCAL, "--zone", ZONE])
    os.makedirs(LOCAL_FILLS, exist_ok=True)
    # data/ 直下に展開すると data/fills/*.json にマージされる
    subprocess.run(["tar", "xzf", TGZ_LOCAL, "-C", config.DATA_DIR])
    os.remove(TGZ_LOCAL)
    # VM側を解放
    ssh(f"rm -f {REMOTE_FILLS}/*.json /tmp/vmfills.tgz")
    total = len([f for f in os.listdir(LOCAL_FILLS) if f.endswith(".json")])
    print(f"完了: VMから{n}件を吸い上げ→ローカル統合(計{total}件) / VM側は解放")


if __name__ == "__main__":
    main()
