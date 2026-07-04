"""共通設定。すべての閾値・パラメータはここで一元管理する。"""
import os

# ---- API ----
# Nansen API キーは git 管理外。優先: 環境変数 NANSEN_API_KEY → local_settings.py（gitignore対象）。
NANSEN_API_KEY = os.environ.get("NANSEN_API_KEY", "")
if not NANSEN_API_KEY:
    try:
        import local_settings  # gitignore済・キーを直書きするローカル専用ファイル
        NANSEN_API_KEY = getattr(local_settings, "NANSEN_API_KEY", "")
    except ImportError:
        pass
NANSEN_BASE = "https://api.nansen.ai/api/v1"
HL_INFO = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# ---- 対象 ----
COINS = ["BTC", "ETH", "SOL"]          # 追跡対象 perp 銘柄

# ---- 候補プール（リーダーボード・複数窓併用） ----
# 下記すべての窓で「pnl>0 かつ vlm>0」を満たす＝長期も直近も一貫して勝つ層に絞る
LB_REQUIRE_WINDOWS = ["month", "allTime"]   # day / week / month / allTime から選ぶ
LB_RANK_WINDOW = "month"                     # 並び替え基準の窓（month=直近の異常勝ち重視）
LB_RANK_METRIC = "roi"                       # roi=効率重視(インサイダー的) / pnl=規模重視
CANDIDATE_LIMIT = 150                       # 候補ウォレット数
# 出金済み(hit-and-run)容疑も拾うため口座下限を低めに。代わりに「実際に稼いだ証拠」をPnL下限で担保。
MIN_ACCOUNT_VALUE = 10_000                  # 現在の口座評価額の下限(USD)。低い=出金済みも残す
MIN_LB_PNL = 50_000                         # allTime PnL の下限(USD)。大きく稼いだ実績を必須に
# 出金(hit-and-run)検出: allTime PnL ÷ 現在残高 が高い=稼いで引き出した。
# month ROI 上位とは別枠で、比が高い順に下記件数を必ず解析対象へ含める。
CASHOUT_RATIO = 10                          # PnL÷残高 がこれ以上で「出金疑い」
CASHOUT_INCLUDE = 60                        # 出金疑いを比の高い順に最大何件 解析へ追加するか

# ---- 約定解析 ----
# 注: HLは古い約定を間引くため、活発なウォレットの fill 履歴は実質~2週が上限（API仕様）。
# ここを大きくしてもデータは返らない。成績サンプルは上の複数窓(月次/全期間)で担保する。
ANALYSIS_DAYS = 14                      # 直近何日の約定を解析するか（フォレンジック層）
HIT_HORIZON_H = 4                       # 方向的中判定の地平線(時間)
LARGE_TRADE_USD = 100_000              # 「大口」エントリ閾値(USD)

# ---- イベント検出（補助シグナル） ----
EVENT_WINDOW_H = 4                       # この時間幅で
EVENT_MOVE_PCT = 3.0                     # この%以上動いたら急変イベント
LEAD_WINDOW_H = 6                        # イベント開始の何時間前を「先行」とみなすか

# ---- Nansen エンリッチ ----
ENRICH_TOP_K = 20                        # 上位何件を Nansen で深掘りするか
ENRICH_CHAINS = ["arbitrum", "ethereum"]  # profiler 照会チェーン（HLブリッジ→ETH）

# ---- レート制限 ----
HL_SLEEP = 0.15                          # HL info リクエスト間隔(秒)
NANSEN_SLEEP = 0.4                       # Nansen リクエスト間隔(秒)
MAX_RETRIES = 4

# ---- MM/HFT 除外（誤検出対策） ----
MM_MAX_CLOSES = 1500     # クローズ数がこれ超→MM/HFT疑い
MM_MAX_FILLS = 3000      # 約定数がこれ超→MM/HFT疑い
MM_PENALTY = 0.15        # MM疑いウォレットのスコア乗数

# ---- ウォレット分類（インサイダー疑惑 / プロ / 除外） ----
# インサイダー疑惑: 少数精鋭で的中率・勝率とも極端に高い（情報先読み型）
INSIDER_DIR = 0.80          # 方向的中率の下限
INSIDER_WIN = 0.80          # 勝率の下限
INSIDER_MIN_CLOSES = 5      # 最低クローズ数（一発屋を除外）
INSIDER_MIN_OPENS = 10      # 最低エントリ数（的中率の信頼性担保）
# プロトレーダー: 取引量が多く一貫して黒字（持続的優位）
PRO_WIN = 0.55
PRO_DIR = 0.55
PRO_MIN_CLOSES = 20         # 多数取引で優位を証明

# ---- スコア重み（初版・検証でチューニング） ----
W_REALIZED_PNL = 0.30                    # 実現損益(正規化)
W_WIN_RATE = 0.25                        # 勝率
W_DIR_ACCURACY = 0.25                    # 方向的中率
W_EVENT_LEAD = 0.20                      # イベント先行度(正規化)

# ---- Phase3 発掘拡張(多窓leaderboard + イベント逆引き + 新規大玉) ----
# 発掘窓の多様化: allTime一本(掬い切ったら終わり)に week/month/day 窓を足し「最近急に勝ち出した」層を早期捕捉。
LB_WEEK_MIN = 150_000       # week窓 PnL下限(USD・新興勝ち組)
LB_WEEK_ROI = 0.30          # week窓 ROI下限(効率で足切り)
LB_MONTH_MIN = 300_000      # month窓 PnL下限(USD)
LB_DAY_MIN = 75_000         # day窓 PnL下限(USD・建てっぱなし未実現の先行者はday窓に出る)
# イベント逆引き(S4): 急変の直前に正しく仕込んだ未登録者を探す起点
EVENT_1H_PCT = 1.5          # 1h急変閾値(%)
EVENT_4H_PCT = 3.0          # 4h急変閾値(%)
EVENT_LEAD_H = 6            # イベント確定の何時間前までを「先行」とみなすか
# 新規アドレス判定(fresh_whale): fills間引きで最古fill日は当てにならない→leaderboard窓整合でゲート
FRESH_VLM_RATIO = 0.9       # allTime vlm ÷ month vlm がこれ以上=活動が最近に集中=真の新規
FRESH_NOTIONAL = 200_000    # 新規アドレスが同方向・数分以内に建てた合算がこれ以上=大玉シグネチャ
# レート予算(IP上限1200w/分・userFillsByTime=w20直列の現実に合わせ控えめ・超過分はpendingで翌日繰越)
CAP_TOTAL = 50              # 1サイクルで fills 取得する候補の総上限
CAP_PER_SOURCE = 30         # 1ソース(S1-S4)あたりの候補上限

# ---- パス ----
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
