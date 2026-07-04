# 発掘フラグの週次手動レビュー手順(Nansen深掘り)

自動発掘(discover_and_classify.py)が立てるフラグは**断定せず**、週次で人間+Nansen MCPが裁定する。
Nansen MCP は Claude のローカルセッションからのみ叩ける(定期自動化はしない=HL公開APIで組んだ発掘とは別レイヤ)。

## 入力ファイル
- `data/discovery_flagged.jsonl` — append-only(addr+date dedup)。週次レビューの主対象。
  - `kind: "deception"` … 欺瞞8軸該当。手仕舞い偽装・往復等の疑い。
  - `kind: "fills_missing_lead"` … S4(day窓)候補で userFills が空だが clearinghouse に急変イベント方向一致の大玉建玉。
    HLがfillを返さない経路(サブ垢/vault等)で先取りした可能性。品質計算不能ゆえ**自動採用しない**=必ず手動確認。
- `data/pending_candidates.json` — rall<=0 やレート繰越の候補。翌日以降 haveに無ければ自然に再候補化。
- `data/repromote_queue.json` — 自動発掘で除外後に実現黒字化した再昇格候補。
- `data/events.jsonl` — 検出済み急変イベント(S4逆引きの根拠)。

## レビュー観点(Nansen MCP)
1. `address_related_addresses` / `address_counterparties` … サブ垢クラスタ・入金元の共通性(1人が複数アドレス)。
2. `smart_traders_and_funds_perp_trades` … 該当イベント前後の建玉タイミングを別ソースで裏取り。
3. `address_portfolio` / `address_historical_balances` … 資金規模・出金パターン(hit-and-run)。
4. 欺瞞候補は往復反復/6定義/複数地平線/欺瞞8軸で従来同様に多面裁定(個人インサイダー未検出の収束結論を尊重)。

## 判定→反映
- 昇格に値する → wallet_registry.json の position/wf_quality を手動更新(承認制・断定表現は避け「疑惑(要監視)」止まり)。
- 否定 → flagged を resolved 扱い(次回レビューで除外)。
- インサイダー認定のみLLM可・分類はコード判定の分担は維持。

## タイミング採点(Phase2)との関係
- `alpha_score.py` が全ウォレットにタイミング前方リターンを付ける(タイミング:妙手/暫定妙手/逆指標)。
- 発掘フラグ(先取りの"状況証拠")× アルファ採点(先取りの"統計的裏付け")の**両方**が揃う候補を最優先で深掘りする。
