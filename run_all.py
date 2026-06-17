"""全段通し実行: discover -> enrich -> report。

    python run_all.py                # フル
    python run_all.py --limit 30     # 候補30件で軽く
"""
import argparse
import runpy
import sys

import config


def run(mod, argv):
    sys.argv = [mod] + argv
    runpy.run_module(mod, run_name="__main__")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=config.CANDIDATE_LIMIT)
    ap.add_argument("--top", type=int, default=config.ENRICH_TOP_K)
    args = ap.parse_args()

    print("===== Step1: 発掘 (Hyperliquid) =====")
    run("step1_discover", ["--limit", str(args.limit)])
    print("\n===== Step2: エンリッチ (Nansen REST) =====")
    run("step2_enrich", ["--top", str(args.top)])
    print("\n===== Step3: HTMLレポート =====")
    run("step3_report", [])
    print("\n===== Step6: 台帳に蓄積（upsert） =====")
    run("step6_registry", [])
    print("\n全段完了。report.html / registry.html を開いて確認せよ。")


if __name__ == "__main__":
    main()
