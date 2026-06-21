# HL リアルタイム監視デーモン — GCP Compute Engine デプロイ手順

監視対象(プロ/MM/弱い疑惑)のエントリー/クローズを **HL WebSocket で即検知 → Discord 通知**し、
現在の建玉を **ライブページ(:8080)** で確認できる常駐デーモン。PCレス・Nansen不要・e2-micro無料枠。

## 構成
```
[GCP e2-micro 常駐]
  hl_realtime.py
   ├ WS購読(通知対象89件) ──Open/Close検知──▶ Discord webhook(即通知)
   ├ clearinghouseState ポーリング(全302件,60秒) ──▶ ライブ建玉ページ :8080
   └ MM大口ポジ変動(>$50万)のみ別途通知
  watch_addresses.json … 監視リスト(発掘パイプラインが更新→ここへ配布)
```

## 1. Discord webhook を用意
対象チャンネル → 設定 → 連携サービス → ウェブフック → 新規 → URLをコピー。

## 2. GCP で e2-micro を作成（Always Free）
無料枠は **us-west1 / us-central1 / us-east1 のいずれかの e2-micro 1台**。
```
gcloud compute instances create hl-monitor \
  --machine-type=e2-micro --zone=us-west1-b \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=15GB
# ライブページ用にポート開放(自分のIPに絞ると安全)
gcloud compute firewall-rules create allow-hl-monitor \
  --allow=tcp:8080 --source-ranges=YOUR.IP.ADDR.0/32
```

## 3. コードを配置
```
gcloud compute ssh hl-monitor --zone=us-west1-b
sudo apt update && sudo apt install -y python3-pip git
git clone https://github.com/noreasonwhystory-cloud/hogehoge.git hl   # 監視リスト同梱リポ
cd hl/nansen   # ※ディレクトリ構成に合わせて調整
pip3 install -r requirements_realtime.txt --break-system-packages
```
※ watch_addresses.json はリポに含めるか、raw URL を `WATCH_PATH` に指定:
`https://raw.githubusercontent.com/noreasonwhystory-cloud/hogehoge/main/nansen/data/watch_addresses.json`

## 4. systemd サービス化（常駐・自動再起動）
`/etc/systemd/system/hl-monitor.service`:
```
[Unit]
Description=HL realtime monitor
After=network-online.target

[Service]
WorkingDirectory=/home/USER/hl/nansen
Environment=DISCORD_WEBHOOK=https://discord.com/api/webhooks/XXXX/YYYY
Environment=WATCH_PATH=https://raw.githubusercontent.com/noreasonwhystory-cloud/hogehoge/main/nansen/data/watch_addresses.json
Environment=PORT=8080
Environment=POLL_SEC=60
Environment=MM_NOTIFY_MIN=500000
ExecStart=/usr/bin/python3 hl_realtime.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```
sudo systemctl daemon-reload && sudo systemctl enable --now hl-monitor
sudo journalctl -u hl-monitor -f      # ログ確認
```

## 5. 確認
- 起動時に Discord へ「🚀 起動」通知が来る
- `http://<VMの外部IP>:8080/` で現在の建玉＋直近イベントが見える(30秒自動更新)
- 監視対象がエントリー/クローズすると即 Discord 通知

## 監視リストの更新
発掘パイプライン(`discover_and_classify.py`)で台帳が増えたら、ローカルで
`python watch_publish.py` → `git push`。デーモンは再起動時(or 定期再読込実装で)最新を取得。

## コスト
e2-micro 1台＝**Always Free**(無料枠内)。HL公開API・Discord webhook も無料。Nansen不要。
※無料枠は1台のみ。複数VMを動かすと[[nansenurl GCPコスト]]のように課金されるので注意。
