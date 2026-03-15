#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import random
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

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

NODE_SEND_TIMEOUT_SEC = float(os.environ.get("NODE_SEND_TIMEOUT_SEC", "180"))

NET_EMULATION = os.environ.get("NET_EMULATION", "0") == "1"
NET_PROFILE_NAME = os.environ.get("NET_PROFILE_NAME", "").strip()
NET_PROFILE_PATH = os.environ.get("NET_PROFILE_PATH", "").strip()
NET_PROFILE_JSON = os.environ.get("NET_PROFILE_JSON", "").strip()
NET_SEED_OVERRIDE = os.environ.get("NET_SEED", "").strip()


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


@dataclass
class EmulationProfile:
    name: str = "baseline"
    base_rtt_ms: float = 80.0
    jitter_ms: float = 20.0
    uplink_kbps: float = 512.0
    downlink_kbps: float = 2048.0
    loss_prob: float = 0.0
    timeout_prob: float = 0.0
    outage_prob_per_call: float = 0.0
    outage_duration_min_s: float = 3.0
    outage_duration_max_s: float = 10.0
    seed: int = 42


@dataclass
class EmulationEvent:
    enabled: bool = False
    profile_name: str = ""
    seed: int = 0
    tx_bytes: int = 0
    rx_bytes: int = 0
    pre_delay_ms: float = 0.0
    post_delay_ms: float = 0.0
    outage_active: bool = False
    outage_started: bool = False
    timeout_injected: bool = False
    loss_injected: bool = False
    stage: str = ""
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "profile_name": self.profile_name,
            "seed": self.seed,
            "tx_bytes": self.tx_bytes,
            "rx_bytes": self.rx_bytes,
            "pre_delay_ms": round(self.pre_delay_ms, 3),
            "post_delay_ms": round(self.post_delay_ms, 3),
            "outage_active": self.outage_active,
            "outage_started": self.outage_started,
            "timeout_injected": self.timeout_injected,
            "loss_injected": self.loss_injected,
            "stage": self.stage,
            "note": self.note,
        }


class EmulatedNetworkError(ConnectionError):
    pass


class EmulatedTimeout(TimeoutError):
    pass


class NetworkEmulator:
    def __init__(self, profile: EmulationProfile):
        self.profile = profile
        self.rng = random.Random(profile.seed)
        self.outage_until = 0.0  # monotonic time

    def _now(self) -> float:
        return time.monotonic()

    def _one_way_delay_s(self) -> float:
        rtt_ms = max(
            0.0,
            self.profile.base_rtt_ms + self.rng.gauss(0.0, self.profile.jitter_ms),
        )
        return (rtt_ms / 1000.0) / 2.0

    def _serialization_delay_s(self, nbytes: int, kbps: float) -> float:
        if kbps <= 0:
            return 0.0
        return (nbytes * 8.0) / (kbps * 1000.0)

    def _check_outage(self, event: EmulationEvent) -> None:
        now = self._now()
        if now < self.outage_until:
            event.outage_active = True
            event.stage = "outage"
            event.note = f"outage in progress ({self.outage_until - now:.2f}s remaining)"
            raise EmulatedNetworkError(event.note)

        if self.profile.outage_prob_per_call > 0 and self.rng.random() < self.profile.outage_prob_per_call:
            dur = self.rng.uniform(
                self.profile.outage_duration_min_s,
                self.profile.outage_duration_max_s,
            )
            self.outage_until = now + dur
            event.outage_started = True
            event.outage_active = True
            event.stage = "outage"
            event.note = f"outage started ({dur:.2f}s)"
            raise EmulatedNetworkError(event.note)

    def _maybe_loss_or_timeout(self, timeout_s: float, event: EmulationEvent, stage: str) -> None:
        x = self.rng.random()

        if x < self.profile.loss_prob:
            event.loss_injected = True
            event.stage = stage
            event.note = "emulated packet loss / connection failure"
            raise EmulatedNetworkError(event.note)

        if x < (self.profile.loss_prob + self.profile.timeout_prob):
            event.timeout_injected = True
            event.stage = stage
            event.note = "emulated timeout"
            time.sleep(min(timeout_s, max(1.0, timeout_s * 0.8)))
            raise EmulatedTimeout(event.note)

    def before_request(self, tx_bytes: int, timeout_s: float) -> EmulationEvent:
        event = EmulationEvent(
            enabled=True,
            profile_name=self.profile.name,
            seed=self.profile.seed,
            tx_bytes=tx_bytes,
        )
        self._check_outage(event)

        delay_s = self._one_way_delay_s() + self._serialization_delay_s(tx_bytes, self.profile.uplink_kbps)
        event.pre_delay_ms = delay_s * 1000.0
        if delay_s > 0:
            time.sleep(delay_s)

        self._maybe_loss_or_timeout(timeout_s, event, "pre")
        return event

    def after_response(self, rx_bytes: int, timeout_s: float, event: EmulationEvent) -> None:
        event.rx_bytes = rx_bytes

        delay_s = self._one_way_delay_s() + self._serialization_delay_s(rx_bytes, self.profile.downlink_kbps)
        event.post_delay_ms = delay_s * 1000.0
        if delay_s > 0:
            time.sleep(delay_s)

        self._maybe_loss_or_timeout(timeout_s, event, "post")


BUILTIN_PROFILES: Dict[str, EmulationProfile] = {
    "baseline": EmulationProfile(
        name="baseline",
        base_rtt_ms=61.359,
        jitter_ms=22.215,
        uplink_kbps=512.0,
        downlink_kbps=2048.0,
        loss_prob=0.0,
        timeout_prob=0.0,
        outage_prob_per_call=0.001,
        outage_duration_min_s=3.0,
        outage_duration_max_s=10.0,
        seed=42,
    ),
    "outdoor-normal": EmulationProfile(
        name="outdoor-normal",
        base_rtt_ms=140.0,
        jitter_ms=55.0,
        uplink_kbps=256.0,
        downlink_kbps=1024.0,
        loss_prob=0.005,
        timeout_prob=0.01,
        outage_prob_per_call=0.001,
        outage_duration_min_s=3.0,
        outage_duration_max_s=8.0,
        seed=42,
    ),
    "outdoor-tough": EmulationProfile(
        name="outdoor-tough",
        base_rtt_ms=220.0,
        jitter_ms=90.0,
        uplink_kbps=128.0,
        downlink_kbps=512.0,
        loss_prob=0.01,
        timeout_prob=0.02,
        outage_prob_per_call=0.003,
        outage_duration_min_s=5.0,
        outage_duration_max_s=12.0,
        seed=42,
    ),
    "outdoor-outage-heavy": EmulationProfile(
        name="outdoor-outage-heavy",
        base_rtt_ms=140.0,
        jitter_ms=55.0,
        uplink_kbps=256.0,
        downlink_kbps=1024.0,
        loss_prob=0.01,
        timeout_prob=0.02,
        outage_prob_per_call=0.01,
        outage_duration_min_s=5.0,
        outage_duration_max_s=15.0,
        seed=42,
    ),
    "severe-intermittent-connectivity": EmulationProfile(
        name="severe-intermittent-connectivity",
        base_rtt_ms=220.0,
        jitter_ms=90.0,
        uplink_kbps=128.0,
        downlink_kbps=512.0,
        loss_prob=0.03,
        timeout_prob=0.05,
        outage_prob_per_call=0.03,
        outage_duration_min_s=8.0,
        outage_duration_max_s=25.0,
        seed=42,
    ),
}


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


def _resolve_profile_source_text() -> str:
    if NET_PROFILE_PATH:
        return f"path:{NET_PROFILE_PATH}"
    if NET_PROFILE_JSON:
        return "json:NET_PROFILE_JSON"
    if NET_PROFILE_NAME:
        return f"name:{NET_PROFILE_NAME}"
    return "builtin:baseline"


def _maybe_resolve_profile_object(obj: Dict[str, Any]) -> Dict[str, Any]:
    if "profile" in obj and isinstance(obj["profile"], dict):
        return dict(obj["profile"])
    return dict(obj)


def _profile_from_mapping(mapping: Dict[str, Any]) -> EmulationProfile:
    data = _maybe_resolve_profile_object(mapping)

    outage_duration_s = data.get("outage_duration_s")
    if isinstance(outage_duration_s, (list, tuple)):
        outage_min = float(outage_duration_s[0]) if len(outage_duration_s) >= 1 else 3.0
        outage_max = float(outage_duration_s[1]) if len(outage_duration_s) >= 2 else 10.0
    else:
        outage_min = float(data.get("outage_duration_min_s", 3.0))
        outage_max = float(data.get("outage_duration_max_s", 10.0))

    profile = EmulationProfile(
        name=str(data.get("name", "custom-profile")),
        base_rtt_ms=float(data.get("base_rtt_ms", 80.0)),
        jitter_ms=float(data.get("jitter_ms", 20.0)),
        uplink_kbps=float(data.get("uplink_kbps", 512.0)),
        downlink_kbps=float(data.get("downlink_kbps", 2048.0)),
        loss_prob=float(data.get("loss_prob", 0.0)),
        timeout_prob=float(data.get("timeout_prob", 0.0)),
        outage_prob_per_call=float(data.get("outage_prob_per_call", 0.0)),
        outage_duration_min_s=outage_min,
        outage_duration_max_s=outage_max,
        seed=int(data.get("seed", 42)),
    )

    if profile.outage_duration_max_s < profile.outage_duration_min_s:
        profile.outage_duration_min_s, profile.outage_duration_max_s = (
            profile.outage_duration_max_s,
            profile.outage_duration_min_s,
        )

    if NET_SEED_OVERRIDE:
        profile.seed = int(NET_SEED_OVERRIDE)

    return profile


def load_emulation_profile() -> Optional[EmulationProfile]:
    if not NET_EMULATION:
        return None

    if NET_PROFILE_JSON:
        return _profile_from_mapping(json.loads(NET_PROFILE_JSON))

    if NET_PROFILE_PATH:
        path = Path(NET_PROFILE_PATH)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        with path.open("r", encoding="utf-8") as f:
            return _profile_from_mapping(json.load(f))

    if NET_PROFILE_NAME:
        key = NET_PROFILE_NAME.strip()
        if key in BUILTIN_PROFILES:
            profile = EmulationProfile(**BUILTIN_PROFILES[key].__dict__)
            if NET_SEED_OVERRIDE:
                profile.seed = int(NET_SEED_OVERRIDE)
            return profile
        raise ValueError(f"unknown NET_PROFILE_NAME: {key}")

    profile = EmulationProfile(**BUILTIN_PROFILES["baseline"].__dict__)
    if NET_SEED_OVERRIDE:
        profile.seed = int(NET_SEED_OVERRIDE)
    return profile


def call_node_send(
    value: str,
    memo: str,
    emulator: Optional[NetworkEmulator],
    send_timeout_sec: float,
) -> Dict[str, Any]:
    inp_obj = {"value": value, "memo": memo}
    inp = json.dumps(inp_obj, ensure_ascii=False).encode("utf-8")
    event = EmulationEvent()

    env = os.environ.copy()
    env["NODE_BROADCAST_TIMEOUT_SEC"] = env.get("NODE_BROADCAST_TIMEOUT_SEC", str(send_timeout_sec))

    try:
        if emulator is not None:
            event = emulator.before_request(tx_bytes=len(inp), timeout_s=send_timeout_sec)
    except EmulatedTimeout as e:
        return {
            "ok": False,
            "error": str(e),
            "error_type": "EmulatedTimeout",
            "subprocess_returncode": "not-run",
            "emulation": event.as_dict(),
        }
    except EmulatedNetworkError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_type": "EmulatedNetworkError",
            "subprocess_returncode": "not-run",
            "emulation": event.as_dict(),
        }

    try:
        p = subprocess.run(
            [NODE_BIN, SEND_JS],
            input=inp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(BASE_DIR),
            timeout=send_timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        event.stage = event.stage or "subprocess"
        event.note = event.note or "node subprocess timeout"
        return {
            "ok": False,
            "error": f"node subprocess timeout after {send_timeout_sec}s",
            "error_type": "SubprocessTimeout",
            "subprocess_returncode": "timeout",
            "emulation": event.as_dict(),
        }

    out = p.stdout.decode("utf-8", errors="replace").strip()
    err = p.stderr.decode("utf-8", errors="replace").strip()
    rx_bytes = len(p.stdout) + len(p.stderr)

    try:
        j = json.loads(out) if out else {"ok": False, "error": "empty stdout", "error_type": "EmptyStdout"}
    except Exception:
        j = {"ok": False, "error": f"stdout not json: {out[:200]}", "error_type": "InvalidJsonStdout"}

    if err:
        j["stderr"] = err[:2000]

    if emulator is not None:
        try:
            emulator.after_response(rx_bytes=rx_bytes, timeout_s=send_timeout_sec, event=event)
        except EmulatedTimeout as e:
            return {
                "ok": False,
                "error": str(e),
                "error_type": "EmulatedTimeout",
                "subprocess_returncode": p.returncode,
                "node_result_ok": j.get("ok", False),
                "node_txhash_hint": j.get("txhash", ""),
                "emulation": event.as_dict(),
            }
        except EmulatedNetworkError as e:
            return {
                "ok": False,
                "error": str(e),
                "error_type": "EmulatedNetworkError",
                "subprocess_returncode": p.returncode,
                "node_result_ok": j.get("ok", False),
                "node_txhash_hint": j.get("txhash", ""),
                "emulation": event.as_dict(),
            }

    j["subprocess_returncode"] = p.returncode
    j["emulation"] = event.as_dict()
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
    emulator: Optional[NetworkEmulator] = None
    profile: Optional[EmulationProfile] = None

    try:
        if NET_EMULATION:
            profile = load_emulation_profile()
            if profile is not None:
                emulator = NetworkEmulator(profile)

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
        logging.info(f"NODE_SEND_TIMEOUT_SEC={NODE_SEND_TIMEOUT_SEC}")
        if profile is not None:
            logging.info(
                "NET_EMULATION=1 profile=%s source=%s rtt_ms=%.3f jitter_ms=%.3f up_kbps=%.1f down_kbps=%.1f loss=%.4f timeout=%.4f outage_prob=%.4f seed=%d",
                profile.name,
                _resolve_profile_source_text(),
                profile.base_rtt_ms,
                profile.jitter_ms,
                profile.uplink_kbps,
                profile.downlink_kbps,
                profile.loss_prob,
                profile.timeout_prob,
                profile.outage_prob_per_call,
                profile.seed,
            )
        else:
            logging.info("NET_EMULATION=0")

        trial = 0
        while True:
            trial += 1
            if N_TRIALS > 0 and trial > N_TRIALS:
                break

            # t0: payload generation start
            t0 = Timing.now_ns()
            payload = make_payload(NODE_ID)
            qr_id = payload["qr_id"]
            unique_id = payload["unique_id"]

            if SEND_FULL_PAYLOAD:
                value_onchain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            else:
                value_onchain = unique_id

            memo = f"qr:{qr_id[:12]}"

            display_message(epd, font_success, "Sending TX...")
            res = call_node_send(
                value=value_onchain,
                memo=memo,
                emulator=emulator,
                send_timeout_sec=NODE_SEND_TIMEOUT_SEC,
            )
            t1 = Timing.now_ns()

            ok = bool(res.get("ok", False))
            txhash = str(res.get("txhash", "")) if ok else ""

            display_message(epd, font_success if ok else font_error, "TX OK - Display" if ok else "TX FAIL - Display")
            canvas = render_qr_canvas(epd, font_info, font_main, payload, txhash)
            epd.display(epd.getbuffer(canvas))
            t2 = Timing.now_ns()

            timing = Timing(t0_ns=t0, t1_ns=t1, t2_ns=t2)
            emu = res.get("emulation", {}) if isinstance(res.get("emulation"), dict) else {}

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
                "sender": res.get("sender", ""),
                "contract": res.get("contract", ""),
                "network": res.get("network", ""),
                "value_len": len(value_onchain),
                "subprocess_returncode": res.get("subprocess_returncode", ""),
                "error_type": res.get("error_type", ""),
                "error": res.get("error", ""),
                "stderr": res.get("stderr", ""),
                # Emulation details
                "net_emulation": int(profile is not None),
                "net_profile_source": _resolve_profile_source_text() if profile is not None else "",
                "net_profile_name": emu.get("profile_name", profile.name if profile else ""),
                "net_seed": emu.get("seed", profile.seed if profile else ""),
                "net_base_rtt_ms": profile.base_rtt_ms if profile else "",
                "net_jitter_ms": profile.jitter_ms if profile else "",
                "net_uplink_kbps": profile.uplink_kbps if profile else "",
                "net_downlink_kbps": profile.downlink_kbps if profile else "",
                "net_loss_prob": profile.loss_prob if profile else "",
                "net_timeout_prob": profile.timeout_prob if profile else "",
                "net_outage_prob_per_call": profile.outage_prob_per_call if profile else "",
                "net_outage_duration_min_s": profile.outage_duration_min_s if profile else "",
                "net_outage_duration_max_s": profile.outage_duration_max_s if profile else "",
                "net_tx_bytes": emu.get("tx_bytes", ""),
                "net_rx_bytes": emu.get("rx_bytes", ""),
                "net_pre_delay_ms": emu.get("pre_delay_ms", ""),
                "net_post_delay_ms": emu.get("post_delay_ms", ""),
                "net_outage_active": emu.get("outage_active", ""),
                "net_outage_started": emu.get("outage_started", ""),
                "net_timeout_injected": emu.get("timeout_injected", ""),
                "net_loss_injected": emu.get("loss_injected", ""),
                "net_stage": emu.get("stage", ""),
                "net_note": emu.get("note", ""),
            }
            append_csv(row, CSV_FILENAME)

            logging.info(
                "[%d] ok=%s txhash_ms=%.3f display_ms=%.3f total_ms=%.3f txhash=%s error_type=%s net_stage=%s pre_ms=%s post_ms=%s",
                trial,
                ok,
                row["txhash_ms"],
                row["display_ms"],
                row["total_ms"],
                txhash[-10:] if txhash else "-",
                row["error_type"] or "-",
                row["net_stage"] or "-",
                row["net_pre_delay_ms"],
                row["net_post_delay_ms"],
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