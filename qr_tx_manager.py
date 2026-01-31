#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# ===== IMPORTANT: fix gpiozero backend BEFORE importing waveshare_epd =====
os.environ.setdefault("GPIOZERO_PIN_FACTORY", os.environ.get("GPIOZERO_PIN_FACTORY", "lgpio"))

# ---- local lib paths (project-local) ----
BASE_DIR = Path(__file__).resolve().parent
picdir = str(BASE_DIR / "pic")
libdir = str(BASE_DIR / "lib")
if os.path.exists(libdir):
    sys.path.append(libdir)

from waveshare_epd import epd2in7_V2 as epd2in7  # type: ignore
from PIL import Image, ImageDraw, ImageFont  # type: ignore
import qrcode  # type: ignore

logging.basicConfig(level=logging.INFO)

# ===== Config =====
NODE_ID = os.environ.get("NODE_ID", "node-t-8821")

NODE_BIN = os.environ.get("NODE_BIN", "node")
SEND_JS = os.environ.get("SEND_JS", "send_set_value.js")  # in BASE_DIR

CSV_FILENAME = os.environ.get("CSV_FILENAME", "qr_tx_log_spec.csv")

N_TRIALS = int(os.environ.get("N_TRIALS", "0"))  # 0 => infinite
DISPLAY_HOLD_SEC = int(os.environ.get("DISPLAY_HOLD_SEC", "20"))
SLEEP_BETWEEN_SEC = float(os.environ.get("SLEEP_BETWEEN_SEC", "0"))

SEND_FULL_PAYLOAD = os.environ.get("SEND_FULL_PAYLOAD", "1") == "1"
INCLUDE_TXHASH_IN_QR = os.environ.get("INCLUDE_TXHASH_IN_QR", "1") == "1"


@dataclass
class Timing:
    t0_ns: int
    t1_ns: int
    t2_ns: int

    @staticmethod
    def now_ns() -> int:
        return time.perf_counter_ns()

    @staticmethod
    def ns_to_ms(ns: int) -> float:
        return ns / 1e6

    @property
    def txhash_ms(self) -> float:
        return self.ns_to_ms(self.t1_ns - self.t0_ns)

    @property
    def display_ms(self) -> float:
        return self.ns_to_ms(self.t2_ns - self.t1_ns)

    @property
    def total_ms(self) -> float:
        return self.ns_to_ms(self.t2_ns - self.t0_ns)


def safe_text(s: str) -> str:
    """
    Some default fonts on Raspberry Pi can't render Japanese/Unicode,
    leading to UnicodeEncodeError inside Pillow. Convert to ASCII fallback.
    """
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return s.encode("ascii", "replace").decode("ascii")


def make_payload(node_id: str) -> Dict[str, Any]:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qr_id = uuid.uuid4().hex

    source_hash = {"node_id": node_id, "qr_id": qr_id, "timestamp": timestamp}
    source_string = json.dumps(source_hash, sort_keys=True)
    unique_id = hashlib.sha256(source_string.encode("utf-8")).hexdigest()

    return {
        "node_id": node_id,
        "name": "yama log e-paper",
        "description": "yama log QRe-paper",
        "unique_id": unique_id,
        "qr_id": qr_id,
        "timestamp": timestamp,
    }


def display_message(epd, font, message: str):
    message = safe_text(message)

    image = Image.new("1", (epd.width, epd.height), 255)
    draw = ImageDraw.Draw(image)

    # Pillow can still fail even after safe_text depending on font; guard it
    try:
        bbox = draw.textbbox((0, 0), message, font=font)
    except Exception:
        message = message.encode("ascii", "replace").decode("ascii")
        bbox = draw.textbbox((0, 0), message, font=font)

    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (epd.width - w) // 2
    y = (epd.height - h) // 2

    draw.text((x, y), message, font=font, fill=0)
    epd.display(epd.getbuffer(image))


def render_qr_canvas(epd, font_info, font_main, payload_obj: Dict[str, Any], txhash: str) -> Image.Image:
    payload_for_qr = dict(payload_obj)
    if INCLUDE_TXHASH_IN_QR and txhash:
        payload_for_qr["txhash"] = txhash

    qr_payload = json.dumps(payload_for_qr, ensure_ascii=False, separators=(",", ":"))

    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=4, border=4)
    qr.add_data(qr_payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")

    canvas = Image.new("1", (epd.width, epd.height), 255)
    draw = ImageDraw.Draw(canvas)

    # Top info (ASCII only)
    draw.text((10, 5), "Node ID:", font=font_info, fill=0)
    draw.text((10, 21), safe_text(payload_obj["node_id"]), font=font_main, fill=0)

    draw.text((10, 47), "Timestamp:", font=font_info, fill=0)
    draw.text((10, 63), safe_text(payload_obj["timestamp"]), font=font_info, fill=0)

    qr_id = payload_obj["qr_id"]
    draw.text((10, 89), safe_text(f"(QR ID: {qr_id[8:]})"), font=font_info, fill=0)

    if txhash:
        short = txhash[-10:]
        draw.text((10, 105), safe_text(f"Tx: ..{short}"), font=font_info, fill=0)
    else:
        draw.text((10, 105), "Tx: (failed)", font=font_info, fill=0)

    # Main QR
    qr_size = 180
    qr_img_resized = qr_img.resize((qr_size, qr_size))
    qr_x = (epd.width - qr_size) // 2
    qr_y = epd.height - qr_size
    canvas.paste(qr_img_resized, (qr_x, qr_y))

    return canvas


def call_node_send(value: str, memo: str) -> Dict[str, Any]:
    inp = json.dumps({"value": value, "memo": memo}, ensure_ascii=False).encode("utf-8")

    p = subprocess.run(
        [NODE_BIN, SEND_JS],
        input=inp,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(BASE_DIR),
        timeout=180,
    )

    out = p.stdout.decode("utf-8", errors="replace").strip()
    err = p.stderr.decode("utf-8", errors="replace").strip()

    try:
        j = json.loads(out) if out else {"ok": False, "error": "empty stdout"}
    except Exception:
        j = {"ok": False, "error": f"stdout not json: {out[:200]}"}

    if err:
        j["stderr"] = err[:2000]
    return j


def append_csv(row: Dict[str, Any], csv_filename: str) -> None:
    path = Path(csv_filename)
    file_exists = path.exists()
    fieldnames = list(row.keys())
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        w.writerow(row)


def main():
    epd = None
    try:
        epd = epd2in7.EPD()
        epd.init()
        epd.Clear()

        # Fonts: if Font.ttc exists, it can render Unicode; otherwise default (ASCII safest)
        font_path = os.path.join(picdir, "Font.ttc")
        if os.path.exists(font_path):
            font_info = ImageFont.truetype(font_path, 14)
            font_main = ImageFont.truetype(font_path, 18)
            font_success = ImageFont.truetype(font_path, 22)
            font_error = ImageFont.truetype(font_path, 20)
        else:
            font_info = ImageFont.load_default()
            font_main = ImageFont.load_default()
            font_success = ImageFont.load_default()
            font_error = ImageFont.load_default()

        logging.info("SPEC LOOP: payload -> tx (txhash) -> display -> csv (Ctrl+C to stop)")
        logging.info(f"GPIOZERO_PIN_FACTORY={os.environ.get('GPIOZERO_PIN_FACTORY')}")
        logging.info(f"SEND_FULL_PAYLOAD={int(SEND_FULL_PAYLOAD)} INCLUDE_TXHASH_IN_QR={int(INCLUDE_TXHASH_IN_QR)}")

        trial = 0
        while True:
            trial += 1
            if N_TRIALS > 0 and trial > N_TRIALS:
                break

            # t0: payload 결정 시작
            t0 = Timing.now_ns()
            payload = make_payload(NODE_ID)
            qr_id = payload["qr_id"]
            unique_id = payload["unique_id"]

            if SEND_FULL_PAYLOAD:
                value_onchain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            else:
                value_onchain = unique_id

            memo = f"qr:{qr_id[:12]}"

            # Tx send (txhash acquired)
            display_message(epd, font_success, "Sending TX...")
            res = call_node_send(value_onchain, memo)
            t1 = Timing.now_ns()

            ok = bool(res.get("ok", False))
            txhash = str(res.get("txhash", "")) if ok else ""

            # Display AFTER txhash (spec)
            display_message(epd, font_success if ok else font_error, "TX OK - Display" if ok else "TX FAIL - Display")
            canvas = render_qr_canvas(epd, font_info, font_main, payload, txhash)
            epd.display(epd.getbuffer(canvas))
            t2 = Timing.now_ns()

            timing = Timing(t0_ns=t0, t1_ns=t1, t2_ns=t2)

            row = {
                "trial": trial,
                "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "node_id": payload["node_id"],
                "qr_id": qr_id,
                "unique_id": unique_id,
                "tx_ok": ok,
                "txhash": txhash,
                # Spec timings
                "txhash_ms": round(timing.txhash_ms, 3),
                "display_ms": round(timing.display_ms, 3),
                "total_ms": round(timing.total_ms, 3),
                # Node details
                "broadcast_ms_node": res.get("broadcast_ms", ""),
                "height": res.get("height", ""),
                "code": res.get("code", ""),
                "gasWanted": res.get("gasWanted", ""),
                "gasUsed": res.get("gasUsed", ""),
                "timestamp_chain": res.get("timestamp", ""),
                "value_len": len(value_onchain),
                "error": res.get("error", ""),
            }
            append_csv(row, CSV_FILENAME)

            logging.info(
                f"[{trial}] ok={ok} txhash_ms={row['txhash_ms']} display_ms={row['display_ms']} total_ms={row['total_ms']} txhash={txhash[-10:] if txhash else '-'}"
            )

            if DISPLAY_HOLD_SEC > 0:
                time.sleep(DISPLAY_HOLD_SEC)

            epd.Clear()
            if SLEEP_BETWEEN_SEC > 0:
                time.sleep(SLEEP_BETWEEN_SEC)

    except KeyboardInterrupt:
        logging.info("ctrl + c")
    finally:
        if epd is not None:
            try:
                epd.Clear()
                epd.sleep()
            except Exception:
                pass
        try:
            epd2in7.epdconfig.module_exit(cleanup=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
