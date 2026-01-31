#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"
source .venv/bin/activate

# ===== 設定（必要ならここだけ編集）=====
SETS=5
N_TRIALS=100

# 1試行の表示維持秒（あなたの仕様では「txhash取得後に表示」。見せたいなら>0）
DISPLAY_HOLD_SEC=${DISPLAY_HOLD_SEC:-20}

# 試行間の追加待ち（表示時間とは別）
SLEEP_BETWEEN_SEC=${SLEEP_BETWEEN_SEC:-0}

# セット間の待ち秒
SLEEP_BETWEEN_SETS=${SLEEP_BETWEEN_SETS:-10}

# GPIO backend（lgpioで動作確認済み）
export GPIOZERO_PIN_FACTORY=lgpio
# ======================================

ts="$(date +%Y%m%d-%H%M%S)"
out_dir="runs_${ts}"
mkdir -p "$out_dir"

echo "[RUN] out_dir=$out_dir"
echo "[RUN] SETS=$SETS N_TRIALS=$N_TRIALS DISPLAY_HOLD_SEC=$DISPLAY_HOLD_SEC SLEEP_BETWEEN_SEC=$SLEEP_BETWEEN_SEC SLEEP_BETWEEN_SETS=$SLEEP_BETWEEN_SETS"

for set_id in $(seq 1 "$SETS"); do
  csv="${out_dir}/qr_tx_log_set${set_id}.csv"
  echo
  echo "===== SET ${set_id}/${SETS} START  csv=$csv ====="

  # セットごとにCSVを分けて保存
  CSV_FILENAME="$csv" \
  N_TRIALS="$N_TRIALS" \
  DISPLAY_HOLD_SEC="$DISPLAY_HOLD_SEC" \
  SLEEP_BETWEEN_SEC="$SLEEP_BETWEEN_SEC" \
  python3 qr_tx_manager.py

  echo "===== SET ${set_id}/${SETS} DONE ====="
  if [ "$set_id" -lt "$SETS" ]; then
    sleep "$SLEEP_BETWEEN_SETS"
  fi
done

echo
echo "[DONE] All sets completed. CSVs are in: $out_dir"
