"""キャッシュ全件を新しい順で取り直して現在化する（鮮度切れ解消）。

一括取得は手動一回限りゆえ日が経つと新規約定が漏れる。全 data/fills/*.json を
get_fills(refresh=True) で更新し、新規約定を得た件数＝鮮度切れの規模を報告する。
使い方: python refresh_all.py
"""
import os
import json
import time

import config
import hl_fills_cache as fc

D = os.path.join(config.DATA_DIR, "fills")


def main():
    files = sorted(f for f in os.listdir(D) if f.endswith(".json"))
    gained = same = err = 0
    total_new = 0
    t0 = time.time()
    for i, fn in enumerate(files):
        if i % 200 == 0:
            print(f"  {i}/{len(files)} 更新中… (新規取得 {gained}件 / 経過{int(time.time()-t0)}秒)")
        addr = fn[:-5]
        try:
            before = json.load(open(os.path.join(D, fn), encoding="utf-8")).get("n", 0)
        except Exception:
            before = 0
        try:
            fl = fc.get_fills(addr, refresh=True)
            after = len(fl)
            if after > before:
                gained += 1
                total_new += (after - before)
            else:
                same += 1
        except Exception:
            err += 1
    print(f"\n完了 {len(files)}件 / 経過 {int(time.time()-t0)}秒")
    print(f"  新規約定を得た(鮮度切れだった): {gained}件  追加約定 計{total_new:,}件")
    print(f"  変化なし: {same}件 / エラー: {err}件")


if __name__ == "__main__":
    main()
