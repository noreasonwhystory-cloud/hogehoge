#!/usr/bin/env python3
"""flow_arch/ 日次ファイル(flow-YYYY-MM-DD.jsonl[.gz])を GCP VM からローカル(GCP外)へ退避・マージ。

VM側はJST日次のappend-onlyログ。ローカルは日別に行dedup+時刻昇順で蓄積するので、
VMが消えても/gzip化(30日後)されてもローカルに残り、複数回pullしても重複しない。
旧 flow_archive.jsonl(BTC過去分・凍結=新規書込なし)は対象外(別途同期済み)。

【v2の要点】単一ファイルの日付固定pullを廃止し、VM側の全日次ファイル(.gz含む)を列挙して
「ローカル未取得の日付 + 直近2日(成長中)」を全てpull。1日でもsyncを欠かしても取りこぼさない。

使い方: python sync_flow_archive.py   (ローカルPCで実行)
Windowsタスクスケジューラに登録すれば定期自動退避。"""
import datetime
import gzip
import json
import os
import subprocess
import tempfile

VM = "dlmm-bot-new"
ZONE = "us-central1-f"
VM_ARCH_DIR = "/home/Matsuya131/hl/flow_arch"
LOCAL_DIR = os.path.expanduser(r"~\hl_archive")            # GCP外のローカル永久保管先
LOCAL_ARCH_DIR = os.path.join(LOCAL_DIR, "flow_arch")      # VM構造をミラー(日別ファイル)


def _run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)  # Windowsは.cmdラッパゆえshell経由


def _recent_jst(n=2):
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    return {(now - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)}


def _date_of(basename):
    # flow-2026-07-04.jsonl / flow-2026-07-04.jsonl.gz -> 2026-07-04
    if basename.startswith("flow-") and len(basename) >= 15:
        return basename[5:15]
    return None


def _read_lines(path):
    """行を読む(.gzは自動解凍)。ファイル無し/読取失敗は空。"""
    if not os.path.exists(path):
        return []
    op = gzip.open if path.endswith(".gz") else open
    try:
        with op(path, "rt", encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f]
    except Exception as e:
        print("  read err", os.path.basename(path), str(e)[:80])
        return []


def _merge_day(local_merged, vm_tmp):
    """既存local + VM を行dedup+時刻昇順でマージ。VM側のパース不能行(torn)のみ捨て、
    既存localは温存。行数が既存を下回るなら破損疑いで書換え中止。原子的に置換。"""
    seen, rows, old_count = set(), [], 0
    for line in _read_lines(local_merged):        # 既存local(必ず温存)
        if not line or line in seen:
            continue
        seen.add(line)
        old_count += 1
        try:
            t = json.loads(line).get("t", 0)
        except Exception:
            t = 0
        rows.append((t, line))
    for line in _read_lines(vm_tmp):              # VM側(torn行はjson.loadsで弾く)
        if not line or line in seen:
            continue
        try:
            t = json.loads(line).get("t", 0)
        except Exception:
            continue
        seen.add(line)
        rows.append((t, line))
    rows.sort(key=lambda x: x[0])
    if len(rows) < old_count:
        print(f"  ⚠ マージ後 {len(rows)}行 < 既存 {old_count}行=破損疑い→温存(書換え中止)")
        return None
    tmp_out = local_merged + ".tmp"
    with open(tmp_out, "w", encoding="utf-8") as f:
        for _t, line in rows:
            f.write(line + "\n")
    os.replace(tmp_out, local_merged)
    return len(rows), len(rows) - old_count


def main():
    os.makedirs(LOCAL_ARCH_DIR, exist_ok=True)
    r = _run(f'gcloud compute ssh {VM} --zone {ZONE} --command "ls -1 {VM_ARCH_DIR}/flow-*.jsonl* 2>/dev/null"')
    if r.returncode != 0:
        print("ls failed (VM未作成/権限?):", (r.stderr or r.stdout).strip()[:200])
        return
    remote = [l.strip() for l in r.stdout.splitlines() if l.strip().endswith((".jsonl", ".gz"))]
    if not remote:
        print("VM側に日次ファイルなし(まだ生成前?)")
        return
    recent = _recent_jst(2)
    pulled = 0
    for rf in remote:
        base = os.path.basename(rf)
        date = _date_of(base)
        if not date:
            continue
        local_merged = os.path.join(LOCAL_ARCH_DIR, f"flow-{date}.jsonl")
        if os.path.exists(local_merged) and date not in recent:
            continue   # 取得済みの過去日はスキップ(直近2日=成長中は常に再pull)
        tmp = os.path.join(tempfile.gettempdir(), "sync_" + base)
        if os.path.exists(tmp):
            os.remove(tmp)
        rr = _run(f'gcloud compute scp "{VM}:{rf}" "{tmp}" --zone {ZONE}')
        if rr.returncode != 0 or not os.path.exists(tmp):
            print("  scp失敗", base, (rr.stderr or rr.stdout).strip()[:120])
            continue
        res = _merge_day(local_merged, tmp)
        if res:
            pulled += 1
            print(f"  synced {base}: {res[0]}行 (+{res[1]})")
    print(f"完了: {pulled}ファイル同期 → {LOCAL_ARCH_DIR}")


if __name__ == "__main__":
    main()
