#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
export GPIOZERO_PIN_FACTORY=lgpio
exec python3 qr_tx_manager.py
