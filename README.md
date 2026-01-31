````markdown
# INJ-epaper-node

Raspberry Pi + Waveshare e-paper 上で **Injective testnet（injective-888）** の CosmWasm コントラクトへ `execute(set_value)` を送り、**txhash が得られた後に** QRコード＋付帯情報を e-paper に表示し、所要時間を CSV に記録する実験環境です。

---

## 目的（本仕様）

1. **QRに埋め込む情報（payload）を生成**（実験の「情報決定」）
2. **Node.js に payload を渡して Tx 送信を依頼**
3. **txhash が生成されたら**
4. **e-paperへ QR と付帯情報を表示**
5. これにかかる時間を **CSV に記録**

### 計測指標（CSV）
- `txhash_ms`：payload決定開始 → txhash取得まで
- `display_ms`：txhash取得 → e-paper表示完了まで（`epd.display` 完了時点）
- `total_ms`：payload決定開始 → 表示完了まで

---

## 構成（ざっくり）

- **Python (`qr_tx_manager.py`)**
  - payload生成
  - Node.js を呼び出して Tx 送信（stdin → stdout JSON）
  - txhash取得後に QR+情報を e-paper へ表示
  - CSVへ記録

- **Node.js (`send_set_value.js`)**
  - Injective TS SDK（`@injectivelabs/sdk-ts`）で署名・ブロードキャスト
  - `txhash`, `broadcast_ms` 等を JSON で返す

---

## 前提

### ハード
- Raspberry Pi
- Waveshare e-paper（本プロジェクトは `epd2in7_V2` を想定）

### ソフト
- Node.js v18 系（例：18.20.x）
- Python 3.11 系（Raspberry Pi OS Bookworm など）
- e-paper ドライバ（`waveshare_epd`）
- gpiozero バックエンド：**lgpio**
  - 実行時に `GPIOZERO_PIN_FACTORY=lgpio` を使用（rpigpio だと edge detect エラーになる場合あり）

---

## セットアップ

### 1) リポジトリ取得
```bash
git clone https://github.com/yanagihalab/INJ-epaper-node.git
cd INJ-epaper-node
````

### 2) Node.js 依存関係

```bash
npm install
```

> Node18 + CosmJS の依存で ESM/CJS が衝突する場合があるため、
> 必要に応じて `@scure/base` を 1.x に固定します（Node18向け）。

```bash
npm pkg set overrides."@scure/base"="1.2.6"
rm -rf node_modules package-lock.json
npm install
npm dedupe
```

### 3) `.env` を作成（Tx送信用 / query用）

**絶対に Git にコミットしないでください。**

```bash
cat > .env <<'EOF'
# Injective testnet
RPC=https://k8s.testnet.tm.injective.network:443
CHAIN_ID=injective-888

# Deployed CosmWasm contract address
CONTRACT=injxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Mnemonic for signer (testnet only)
MNEMONIC="word word word ..."

EOF
chmod 600 .env
```

### 4) Python 仮想環境（.venv）

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -U pip
pip install qrcode pillow matplotlib
```

### 5) OS依存（SPI/GPIO/BLAS 等）

```bash
sudo apt-get update
sudo apt-get install -y python3-spidev python3-rpi.gpio libopenblas0
```

> e-paper ドライバ側で `spidev`/`RPi.GPIO` を参照します。
> `.venv` が system site-packages を参照する必要があります（上の venv 作成手順で対応済み）。

### 6) waveshare_epd の配置

プロジェクト配下に `lib/waveshare_epd/` が必要です。

例（Waveshare公式 repo からコピーする場合）：

```bash
mkdir -p lib
# 例：手元の waveshare_epd を lib/ に配置
cp -a /path/to/waveshare_epd ./lib/waveshare_epd
```

---

## 主要スクリプト

### Tx送信用（Node.js）

* `send_set_value.js`

  * stdin: `{"value":"...","memo":"..."}`
  * stdout: `{"ok":true,"txhash":"...","broadcast_ms":...,...}`

### Query確認（Node.js）

* `query.js`

  * `.env` の `RPC` と `CONTRACT` を参照して `ping/get_value` を実行

### 本仕様ループ（Python）

* `qr_tx_manager.py`

  * payload生成 → Tx送信 → txhash取得 → 表示 → CSV記録

---

## 実行方法

### 1) Query疎通確認

```bash
node query.js
```

### 2) 単発 execute（set_value）

```bash
node exec_set_value.js
```

### 3) 本仕様（payload→txhash→表示→CSV）

**lgpio バックエンド固定が重要です。**

```bash
source .venv/bin/activate
export GPIOZERO_PIN_FACTORY=lgpio
python3 qr_tx_manager.py
```

環境変数で制御できます：

* `N_TRIALS`：回数（0=無限）
* `DISPLAY_HOLD_SEC`：表示維持秒
* `SLEEP_BETWEEN_SEC`：試行間追加待ち
* `SEND_FULL_PAYLOAD`：on-chain に payload(JSON) を送る(1) / unique_idのみ送る(0)
* `INCLUDE_TXHASH_IN_QR`：QRに txhash を含める(1)/含めない(0)
* `CSV_FILENAME`：出力CSV名

例：100回だけ回す

```bash
N_TRIALS=100 DISPLAY_HOLD_SEC=20 python3 qr_tx_manager.py
```

---

## 100回 × 5セット 実験

### 実験実行

`run_5sets.sh` が **100回×5セット**を連続実行し、セットごとにCSVを分けて保存します。

```bash
./run_5sets.sh
```

出力例：

* `runs_YYYYmmdd-HHMMSS/qr_tx_log_set1.csv`
* …
* `runs_YYYYmmdd-HHMMSS/qr_tx_log_set5.csv`

---

## グラフ描画（セット別＋統合）

### セット別・時系列・箱ひげ・summary.csv

```bash
source .venv/bin/activate
python3 plot_sets.py \
  --indir runs_YYYYmmdd-HHMMSS \
  --pattern "qr_tx_log_set*.csv" \
  --outdir runs_YYYYmmdd-HHMMSS/plots_all \
  --bins 40
```

* 各セット×各指標（`txhash_ms`, `display_ms`, `total_ms`）のヒストグラム/時系列
* セット比較の箱ひげ
* `summary.csv`（set×metricの統計）
* `all_hist_*.png`（全セット統合ヒストグラム）
* `all_summary.csv`（全セット統合統計）

---

## よくあるトラブル

### 1) `GPIO busy`

別プロセス（スタートアップで起動しているQR表示スクリプト等）が GPIO を掴んでいる可能性があります。
対象プロセスを停止してください。

### 2) `Failed to add edge detection`

`GPIOZERO_PIN_FACTORY=lgpio` を指定してください。

```bash
export GPIOZERO_PIN_FACTORY=lgpio
```

### 3) 日本語表示で `UnicodeEncodeError`

フォントが日本語非対応の場合があります。
現状はASCII表示にフォールバックする実装になっています。
日本語表示したい場合は `pic/Font.ttc` に CJK対応フォントを配置してください。

---

## ディレクトリ構成（例）

* `qr_tx_manager.py`：本仕様実験ループ（Python）
* `send_set_value.js`：Tx送信（Node）
* `query.js`：query疎通確認（Node/CosmJS）
* `run_5sets.sh`：100回×5セット実験
* `plot_sets.py`：CSVからグラフ生成（セット別＋統合）
* `lib/waveshare_epd/`：Waveshareドライバ
* `runs_*/`：実験出力（CSV）
* `plots*/`：グラフ出力（PNG）

---

## セキュリティ注意

* `.env` に mnemonic を置きます。**必ず `.gitignore` 対象**にしてください。
* mainnet ではなく testnet 前提です（injective-888）。

---

```

必要なら、この README に **「コントラクトデプロイ手順（store/instantiate）」「コントラクト要件（ExecuteMsg/QueryMsg）」**も追記して、完全再現できる形に拡張します。
::contentReference[oaicite:0]{index=0}
```
