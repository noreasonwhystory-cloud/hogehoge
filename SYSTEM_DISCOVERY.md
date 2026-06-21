# 自動発掘システム（Nansen不要・HL公開APIのみ）

新規アドレスを定期的に発掘し、既存基準でインサイダー/プロ/MMに分類して台帳へ追加し続ける仕組み。

## パイプライン
```
HL leaderboard(全39,471件の成績)
  └→ allTime PnL>=閾値 かつ 台帳未登録 を新規候補に
       └→ 約定取得(hl_fills_cache・新しい順・永続)
            └→ cache真値で指標算出(実現益/勝率/回転/黒字月率/majors比)
                 └→ 既存の決定的分類:
                      ・高頻度MM   (回転>1500closes/月 or 約定>3000)  +HFT速度+品質
                      ・プロ(本物) (黒字 かつ majors比>=0.3)          +品質(エリート/堅実/中堅/ムラ/履歴薄)
                      ・alt主体プロ(黒字 かつ majors比<0.3)
                      ・除外       (実現赤字)
                 └→ 欺瞞8軸(deception_scan)該当は『欺瞞候補:要精査』タグ
                      └→ workflowで2段裁定(判定→反証)→確証のみ弱い疑惑へ昇格・否定は精査済タグ
            └→ 台帳へ追記(既存は不上書き)→auto_tags再計算→全HTML再描画→push
```

## スクリプト
| ファイル | 役割 | Claude要否 |
|---|---|---|
| `discover_and_classify.py` | 発掘+分類の決定的コア。`--min`(allTime PnL下限)`--cap`(1回上限)`--max-age`(LB鮮度) | 不要 |
| `run_discovery.py` | 上記+git push。タスクスケジューラ用 | 不要 |
| 欺瞞候補の裁定 | `data/discovery_flagged.json`を入力にworkflow裁定 | **要(workflow)** |

## 定期実行
### A. 決定的部分（headless・無料・恒久）= タスクスケジューラ
```
schtasks /create /tn "hl-discovery" /sc weekly /d MON /st 09:07 ^
  /tr "python C:\Users\Matsuya131\.gemini\antigravity\scratch\nansen\run_discovery.py --min 500000 --cap 150"
```
→ 毎週、新規アドレスを発掘・分類・台帳化・push。pro/MM/除外リストが自動で育つ。欺瞞候補は貯まる。

### B. インサイダー裁定（workflow・Claude起動時）= CronCreate
Claude Code 起動・idle時に発火し、`discovery_flagged.json`の欺瞞候補をworkflow裁定→確証を弱い疑惑へ昇格。
（CronCreateの recurring は7日で自動失効するため、継続するには再設定が要る）

## 出力
- `data/wallet_registry.json` … 台帳(追記)
- `data/discovery_log.jsonl` … 発掘履歴(日付/アドレス/分類)
- `data/discovery_flagged.json` … 欺瞞候補(裁定待ち)
- `registry.html / pros.html / mm.html / excluded.html` … 自動再描画

## 設計原則
- **Nansen不要**: 発掘・分類・検知すべてHL公開API(無料)とcache真値のみ。Nansenは正体付与の任意補助。
- **既存基準の再利用**: 分類閾値・品質グレード・欺瞞8軸は既存ロジックをそのまま使用(一貫性)。
- **既存は不上書き**: 発掘は新規のみ追加。手動分類・精査結果を壊さない。
- **断定しない**: 欺瞞候補は必ずworkflowの2段裁定(判定→反証)を経て、確証のみ昇格。
