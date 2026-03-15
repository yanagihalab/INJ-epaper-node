#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"
source .venv/bin/activate

# ===== 設定（必要ならここだけ編集）=====
REPEATS_PER_PROFILE="${REPEATS_PER_PROFILE:-3}"
N_TRIALS="${N_TRIALS:-100}"

# 1試行の表示維持秒（txhash取得後に表示）
DISPLAY_HOLD_SEC="${DISPLAY_HOLD_SEC:-20}"

# 試行間の追加待ち（表示時間とは別）
SLEEP_BETWEEN_SEC="${SLEEP_BETWEEN_SEC:-0}"

# セット間の待ち秒
SLEEP_BETWEEN_SETS="${SLEEP_BETWEEN_SETS:-10}"

# プロファイル切替時の待ち秒
SLEEP_BETWEEN_PROFILES="${SLEEP_BETWEEN_PROFILES:-15}"

# GPIO backend
export GPIOZERO_PIN_FACTORY="${GPIOZERO_PIN_FACTORY:-lgpio}"

# 通信環境エミュレーション
NET_EMULATION="${NET_EMULATION:-1}"
NODE_SEND_TIMEOUT_SEC="${NODE_SEND_TIMEOUT_SEC:-60}"

# 送信内容
SEND_FULL_PAYLOAD="${SEND_FULL_PAYLOAD:-1}"
INCLUDE_TXHASH_IN_QR="${INCLUDE_TXHASH_IN_QR:-1}"

# 実験したい論理プロファイル名
PROFILES=(
  "baseline"
  "outdoor-normal"
  "outage-heavy"
)
# ======================================

resolve_profile_name() {
  local logical_name="$1"
  case "$logical_name" in
    baseline)
      echo "baseline"
      ;;
    outdoor-normal)
      echo "outdoor-normal"
      ;;
    outage-heavy)
      # qr_tx_manager.py 内蔵名に合わせる
      echo "outdoor-outage-heavy"
      ;;
    *)
      echo "$logical_name"
      ;;
  esac
}

ts="$(date +%Y%m%d-%H%M%S)"
root_dir="runs_profiles_${ts}"
mkdir -p "${root_dir}"

echo "[RUN] root_dir=${root_dir}"
echo "[RUN] REPEATS_PER_PROFILE=${REPEATS_PER_PROFILE} N_TRIALS=${N_TRIALS}"
echo "[RUN] DISPLAY_HOLD_SEC=${DISPLAY_HOLD_SEC} SLEEP_BETWEEN_SEC=${SLEEP_BETWEEN_SEC}"
echo "[RUN] SLEEP_BETWEEN_SETS=${SLEEP_BETWEEN_SETS} SLEEP_BETWEEN_PROFILES=${SLEEP_BETWEEN_PROFILES}"
echo "[RUN] NET_EMULATION=${NET_EMULATION} NODE_SEND_TIMEOUT_SEC=${NODE_SEND_TIMEOUT_SEC}"
echo "[RUN] PROFILES=${PROFILES[*]}"

for logical_profile in "${PROFILES[@]}"; do
  actual_profile="$(resolve_profile_name "${logical_profile}")"
  profile_dir="${root_dir}/${logical_profile}"
  mkdir -p "${profile_dir}"

  config_txt="${profile_dir}/run_config.txt"
  {
    echo "timestamp=${ts}"
    echo "logical_profile=${logical_profile}"
    echo "actual_profile=${actual_profile}"
    echo "REPEATS_PER_PROFILE=${REPEATS_PER_PROFILE}"
    echo "N_TRIALS=${N_TRIALS}"
    echo "DISPLAY_HOLD_SEC=${DISPLAY_HOLD_SEC}"
    echo "SLEEP_BETWEEN_SEC=${SLEEP_BETWEEN_SEC}"
    echo "SLEEP_BETWEEN_SETS=${SLEEP_BETWEEN_SETS}"
    echo "SLEEP_BETWEEN_PROFILES=${SLEEP_BETWEEN_PROFILES}"
    echo "GPIOZERO_PIN_FACTORY=${GPIOZERO_PIN_FACTORY}"
    echo "NET_EMULATION=${NET_EMULATION}"
    echo "NODE_SEND_TIMEOUT_SEC=${NODE_SEND_TIMEOUT_SEC}"
    echo "SEND_FULL_PAYLOAD=${SEND_FULL_PAYLOAD}"
    echo "INCLUDE_TXHASH_IN_QR=${INCLUDE_TXHASH_IN_QR}"
  } > "${config_txt}"

  echo
  echo "######## PROFILE=${logical_profile} (NET_PROFILE_NAME=${actual_profile}) ########"
  echo "[RUN] profile_dir=${profile_dir}"

  for set_id in $(seq 1 "${REPEATS_PER_PROFILE}"); do
    csv="${profile_dir}/qr_tx_log_set${set_id}.csv"
    log="${profile_dir}/qr_tx_log_set${set_id}.log"

    echo
    echo "===== PROFILE=${logical_profile} SET ${set_id}/${REPEATS_PER_PROFILE} START ====="
    echo "csv=${csv}"
    echo "log=${log}"

    CSV_FILENAME="${csv}" \
    N_TRIALS="${N_TRIALS}" \
    DISPLAY_HOLD_SEC="${DISPLAY_HOLD_SEC}" \
    SLEEP_BETWEEN_SEC="${SLEEP_BETWEEN_SEC}" \
    SEND_FULL_PAYLOAD="${SEND_FULL_PAYLOAD}" \
    INCLUDE_TXHASH_IN_QR="${INCLUDE_TXHASH_IN_QR}" \
    NET_EMULATION="${NET_EMULATION}" \
    NET_PROFILE_NAME="${actual_profile}" \
    NODE_SEND_TIMEOUT_SEC="${NODE_SEND_TIMEOUT_SEC}" \
    python3 qr_tx_manager.py 2>&1 | tee "${log}"

    echo "===== PROFILE=${logical_profile} SET ${set_id}/${REPEATS_PER_PROFILE} DONE ====="

    if [ "${set_id}" -lt "${REPEATS_PER_PROFILE}" ]; then
      sleep "${SLEEP_BETWEEN_SETS}"
    fi
  done

  echo "######## PROFILE=${logical_profile} DONE ########"

  last_profile="${PROFILES[${#PROFILES[@]}-1]}"
  if [ "${logical_profile}" != "${last_profile}" ]; then
    sleep "${SLEEP_BETWEEN_PROFILES}"
  fi
done

echo
echo "[DONE] All profile runs completed."
echo "[DONE] Outputs are in: ${root_dir}"
