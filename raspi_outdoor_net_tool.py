#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
raspi_outdoor_net_tool.py

Raspberry Pi friendly single-file tool for:
1) Measuring current link/network conditions
   - ping RTT / loss / mdev
   - HTTP(S) probe latency / timeout rate / payload goodput
   - optional iperf3 throughput
2) Emulating outdoor / unstable network conditions in Python
   - delay, jitter, loss, timeout, outage
3) Benchmarking existing HTTP or CLI workflows under emulated conditions
4) Deriving an emulation profile from measured metrics JSON

Dependencies:
- Standard library
- Optional: requests (recommended)
- Optional: iperf3 executable
- Linux ping executable (Raspberry Pi OS default)

Examples:
  # Measure the current network
  python3 raspi_outdoor_net_tool.py measure \
    --ping-host 1.1.1.1 \
    --ping-host 8.8.8.8 \
    --http-url https://httpbin.org/get \
    --http-runs 10 \
    --out metrics.json

  # Derive an emulation profile from measured metrics
  python3 raspi_outdoor_net_tool.py derive-profile \
    --metrics metrics.json \
    --out derived_profile.json

  # Run an HTTP benchmark under emulated conditions
  python3 raspi_outdoor_net_tool.py emulate-http \
    --url https://httpbin.org/post \
    --method POST \
    --json-body '{"hello":"world"}' \
    --profile derived_profile.json \
    --runs 20 \
    --out emu_http.json

  # Run a CLI command under emulated conditions
  python3 raspi_outdoor_net_tool.py emulate-cli \
    --cmd 'echo hello' \
    --profile derived_profile.json \
    --runs 10 \
    --out emu_cli.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import requests  # type: ignore
except ImportError:
    requests = None


def now_epoch() -> float:
    return time.time()


def monotonic() -> float:
    return time.monotonic()


def json_dump(obj: Any, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_rows_csv(rows: List[Dict[str, Any]], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write("")
        return

    fieldnames = sorted({k for row in rows for k in row.keys()})
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    idx = (len(xs) - 1) * p
    lo = int(math.floor(idx))
    hi = min(lo + 1, len(xs) - 1)
    frac = idx - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def summarize_floats(values: Sequence[float], unit: str) -> Dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {
            "count": 0,
            f"mean_{unit}": None,
            f"median_{unit}": None,
            f"p95_{unit}": None,
            f"p99_{unit}": None,
            f"min_{unit}": None,
            f"max_{unit}": None,
        }
    return {
        "count": len(vals),
        f"mean_{unit}": round(statistics.mean(vals), 6),
        f"median_{unit}": round(statistics.median(vals), 6),
        f"p95_{unit}": round(percentile(vals, 0.95) or 0.0, 6),
        f"p99_{unit}": round(percentile(vals, 0.99) or 0.0, 6),
        f"min_{unit}": round(min(vals), 6),
        f"max_{unit}": round(max(vals), 6),
    }


def summarize_ints(values: Sequence[int], unit: str) -> Dict[str, Any]:
    vals = [int(v) for v in values if v is not None]
    if not vals:
        return {
            "count": 0,
            f"mean_{unit}": None,
            f"median_{unit}": None,
            f"p95_{unit}": None,
            f"p99_{unit}": None,
            f"min_{unit}": None,
            f"max_{unit}": None,
        }
    return {
        "count": len(vals),
        f"mean_{unit}": round(statistics.mean(vals), 3),
        f"median_{unit}": round(statistics.median(vals), 3),
        f"p95_{unit}": round(percentile(vals, 0.95) or 0.0, 3),
        f"p99_{unit}": round(percentile(vals, 0.99) or 0.0, 3),
        f"min_{unit}": min(vals),
        f"max_{unit}": max(vals),
    }


def choose_existing_default_ping_binary() -> str:
    for name in ("ping", "/bin/ping", "/usr/bin/ping"):
        if shutil.which(name) or Path(name).exists():
            return name
    return "ping"


def parse_json_or_file(maybe_json_or_path: Optional[str], default: Any = None) -> Any:
    if maybe_json_or_path is None:
        return default
    s = maybe_json_or_path.strip()
    if not s:
        return default
    if s.startswith("{") or s.startswith("["):
        return json.loads(s)
    p = Path(s)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(s)


def shell_join_for_log(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


# =========================================================
# Measurement: ping / HTTP / iperf3
# =========================================================

def run_ping(host: str, count: int, interval_s: float, timeout_s: Optional[float] = None) -> Dict[str, Any]:
    ping_bin = choose_existing_default_ping_binary()
    cmd = [ping_bin, "-n", "-c", str(count), "-i", str(interval_s), host]
    tool_timeout = timeout_s
    if tool_timeout is None:
        tool_timeout = max(15.0, count * max(interval_s, 0.2) + 10.0)

    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=tool_timeout,
        check=False,
    )
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")

    tx = rx = None
    loss_percent = None
    rtt_min_ms = rtt_avg_ms = rtt_max_ms = rtt_mdev_ms = None

    m1 = re.search(
        r"(?P<tx>\d+)\s+packets transmitted,\s+"
        r"(?P<rx>\d+)\s+(?:packets )?received,.*?"
        r"(?P<loss>[0-9.]+)%\s+packet loss",
        text,
        flags=re.S,
    )
    if m1:
        tx = int(m1.group("tx"))
        rx = int(m1.group("rx"))
        loss_percent = float(m1.group("loss"))

    m2 = re.search(
        r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s+=\s+"
        r"(?P<min>[0-9.]+)/(?P<avg>[0-9.]+)/(?P<max>[0-9.]+)/(?P<mdev>[0-9.]+)\s+ms",
        text,
    )
    if m2:
        rtt_min_ms = float(m2.group("min"))
        rtt_avg_ms = float(m2.group("avg"))
        rtt_max_ms = float(m2.group("max"))
        rtt_mdev_ms = float(m2.group("mdev"))

    return {
        "kind": "ping",
        "host": host,
        "command": cmd,
        "command_str": shell_join_for_log(cmd),
        "returncode": cp.returncode,
        "transmitted": tx,
        "received": rx,
        "loss_percent": loss_percent,
        "rtt_min_ms": rtt_min_ms,
        "rtt_avg_ms": rtt_avg_ms,
        "rtt_max_ms": rtt_max_ms,
        "rtt_mdev_ms": rtt_mdev_ms,
        "raw_excerpt": text[-1000:],
    }


def _http_probe_requests(
    url: str,
    runs: int,
    timeout_s: float,
    method: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
) -> Dict[str, Any]:
    session = requests.Session()  # type: ignore[union-attr]
    header_ms: List[float] = []
    total_ms: List[float] = []
    body_bytes: List[int] = []
    statuses: List[int] = []
    errors: List[str] = []

    headers = headers or {}

    for _ in range(runs):
        t0 = time.perf_counter()
        try:
            if method == "HEAD":
                resp = session.head(url, timeout=timeout_s, allow_redirects=True, headers=headers)
                size = 0
            elif method == "POST":
                resp = session.post(url, timeout=timeout_s, data=body, headers=headers, allow_redirects=True, stream=True)
                size = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        size += len(chunk)
            else:
                resp = session.get(url, timeout=timeout_s, headers=headers, allow_redirects=True, stream=True)
                size = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        size += len(chunk)

            hdr = resp.elapsed.total_seconds() * 1000.0
            total = (time.perf_counter() - t0) * 1000.0

            header_ms.append(hdr)
            total_ms.append(total)
            body_bytes.append(size)
            statuses.append(resp.status_code)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    goodput_mbps: List[float] = []
    for nbytes, ms in zip(body_bytes, total_ms):
        if ms > 0:
            goodput_mbps.append((nbytes * 8.0) / (ms / 1000.0) / 1_000_000.0)

    return {
        "kind": "http",
        "url": url,
        "method": method,
        "runs": runs,
        "success": len(total_ms),
        "failures": len(errors),
        "success_rate": round(len(total_ms) / runs, 6) if runs > 0 else None,
        "status_codes": statuses,
        "header_latency": summarize_floats(header_ms, "ms"),
        "total_latency": summarize_floats(total_ms, "ms"),
        "body_size": summarize_ints(body_bytes, "bytes"),
        "goodput": summarize_floats(goodput_mbps, "mbps"),
        "errors": errors[:20],
    }


def _http_probe_urllib(
    url: str,
    runs: int,
    timeout_s: float,
    method: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
) -> Dict[str, Any]:
    header_ms: List[float] = []
    total_ms: List[float] = []
    body_bytes: List[int] = []
    statuses: List[int] = []
    errors: List[str] = []

    headers = headers or {}

    for _ in range(runs):
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url=url, method=method, headers=headers, data=body if method == "POST" else None)
            t1 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                t2 = time.perf_counter()
                data = b"" if method == "HEAD" else resp.read()
                status = getattr(resp, "status", None) or getattr(resp, "code", None) or 200

            hdr = (t2 - t1) * 1000.0
            total = (time.perf_counter() - t0) * 1000.0

            header_ms.append(hdr)
            total_ms.append(total)
            body_bytes.append(len(data))
            statuses.append(int(status))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    goodput_mbps: List[float] = []
    for nbytes, ms in zip(body_bytes, total_ms):
        if ms > 0:
            goodput_mbps.append((nbytes * 8.0) / (ms / 1000.0) / 1_000_000.0)

    return {
        "kind": "http",
        "url": url,
        "method": method,
        "runs": runs,
        "success": len(total_ms),
        "failures": len(errors),
        "success_rate": round(len(total_ms) / runs, 6) if runs > 0 else None,
        "status_codes": statuses,
        "header_latency": summarize_floats(header_ms, "ms"),
        "total_latency": summarize_floats(total_ms, "ms"),
        "body_size": summarize_ints(body_bytes, "bytes"),
        "goodput": summarize_floats(goodput_mbps, "mbps"),
        "errors": errors[:20],
    }


def probe_http(
    url: str,
    runs: int,
    timeout_s: float,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
) -> Dict[str, Any]:
    method = method.upper()
    if method not in {"GET", "HEAD", "POST"}:
        raise ValueError("HTTP method must be GET, HEAD, or POST")
    if requests is not None:
        return _http_probe_requests(url, runs, timeout_s, method, headers=headers, body=body)
    return _http_probe_urllib(url, runs, timeout_s, method, headers=headers, body=body)


def run_iperf3(server: str, duration_s: int, port: int, reverse: bool = False) -> Dict[str, Any]:
    if shutil.which("iperf3") is None:
        return {
            "kind": "iperf3",
            "server": server,
            "reverse": reverse,
            "skipped": True,
            "reason": "iperf3 not found in PATH",
        }

    cmd = ["iperf3", "-c", server, "-p", str(port), "-t", str(duration_s), "-J"]
    if reverse:
        cmd.append("-R")

    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=duration_s + 30,
        check=False,
    )

    if cp.returncode != 0:
        return {
            "kind": "iperf3",
            "server": server,
            "reverse": reverse,
            "skipped": False,
            "returncode": cp.returncode,
            "command": cmd,
            "command_str": shell_join_for_log(cmd),
            "stderr": (cp.stderr or "")[-1000:],
        }

    try:
        obj = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return {
            "kind": "iperf3",
            "server": server,
            "reverse": reverse,
            "skipped": False,
            "returncode": cp.returncode,
            "command": cmd,
            "command_str": shell_join_for_log(cmd),
            "error": f"json decode error: {e}",
        }

    end = obj.get("end", {})
    sent = end.get("sum_sent", {}) or {}
    recv = end.get("sum_received", {}) or {}
    return {
        "kind": "iperf3",
        "server": server,
        "reverse": reverse,
        "skipped": False,
        "command": cmd,
        "command_str": shell_join_for_log(cmd),
        "bits_per_second_sent": sent.get("bits_per_second"),
        "bits_per_second_received": recv.get("bits_per_second"),
        "bytes_sent": sent.get("bytes"),
        "bytes_received": recv.get("bytes"),
        "sender_retransmits": sent.get("retransmits"),
        "cpu_utilization_percent": end.get("cpu_utilization_percent"),
    }


def measure_network(args: argparse.Namespace) -> Dict[str, Any]:
    headers = parse_json_or_file(args.http_headers, default={}) or {}
    json_body = parse_json_or_file(args.http_json_body, default=None)

    body_bytes: Optional[bytes] = None
    if json_body is not None:
        body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers = dict(headers)
        headers.setdefault("Content-Type", "application/json; charset=utf-8")

    result: Dict[str, Any] = {
        "tool": "raspi_outdoor_net_tool",
        "mode": "measure",
        "timestamp_epoch": now_epoch(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else None,
        "ping": [],
        "http": [],
        "iperf": [],
    }

    for host in args.ping_host:
        row = run_ping(host, args.ping_count, args.ping_interval, timeout_s=args.ping_timeout)
        result["ping"].append(row)

    for url in args.http_url:
        row = probe_http(url, args.http_runs, args.http_timeout, args.http_method, headers=headers, body=body_bytes)
        result["http"].append(row)

    if args.iperf_server:
        result["iperf"].append(run_iperf3(args.iperf_server, args.iperf_seconds, args.iperf_port, reverse=False))
        result["iperf"].append(run_iperf3(args.iperf_server, args.iperf_seconds, args.iperf_port, reverse=True))

    return result


# =========================================================
# Emulation profile + emulation runtime
# =========================================================

@dataclass
class OutdoorProfile:
    name: str = "outdoor-normal"
    base_rtt_ms: float = 180.0
    jitter_ms: float = 80.0
    uplink_kbps: float = 512.0
    downlink_kbps: float = 2048.0
    loss_prob: float = 0.01
    timeout_prob: float = 0.02
    outage_prob_per_call: float = 0.005
    outage_duration_min_s: float = 3.0
    outage_duration_max_s: float = 10.0
    seed: int = 42

    @classmethod
    def from_any(cls, obj: Optional[Dict[str, Any]]) -> "OutdoorProfile":
        if not obj:
            return cls()
        return cls(
            name=str(obj.get("name", cls.name)),
            base_rtt_ms=float(obj.get("base_rtt_ms", cls.base_rtt_ms)),
            jitter_ms=float(obj.get("jitter_ms", cls.jitter_ms)),
            uplink_kbps=float(obj.get("uplink_kbps", cls.uplink_kbps)),
            downlink_kbps=float(obj.get("downlink_kbps", cls.downlink_kbps)),
            loss_prob=float(obj.get("loss_prob", cls.loss_prob)),
            timeout_prob=float(obj.get("timeout_prob", cls.timeout_prob)),
            outage_prob_per_call=float(obj.get("outage_prob_per_call", cls.outage_prob_per_call)),
            outage_duration_min_s=float(obj.get("outage_duration_min_s", cls.outage_duration_min_s)),
            outage_duration_max_s=float(obj.get("outage_duration_max_s", cls.outage_duration_max_s)),
            seed=int(obj.get("seed", cls.seed)),
        )

    def validate(self) -> None:
        if self.base_rtt_ms < 0 or self.jitter_ms < 0:
            raise ValueError("base_rtt_ms and jitter_ms must be >= 0")
        if self.uplink_kbps <= 0 or self.downlink_kbps <= 0:
            raise ValueError("uplink_kbps and downlink_kbps must be > 0")
        for name in ("loss_prob", "timeout_prob", "outage_prob_per_call"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.outage_duration_min_s < 0 or self.outage_duration_max_s < self.outage_duration_min_s:
            raise ValueError("invalid outage duration range")


class EmulatedNetworkError(ConnectionError):
    pass


class EmulatedTimeout(TimeoutError):
    pass


class OutdoorLinkEmulator:
    def __init__(self, profile: OutdoorProfile):
        profile.validate()
        self.profile = profile
        self.rng = random.Random(profile.seed)
        self.outage_until = 0.0

    def _now(self) -> float:
        return monotonic()

    def _maybe_start_or_continue_outage(self) -> None:
        current = self._now()
        if current < self.outage_until:
            remain = self.outage_until - current
            raise EmulatedNetworkError(f"emulated outage in progress ({remain:.2f}s remaining)")

        if self.rng.random() < self.profile.outage_prob_per_call:
            dur = self.rng.uniform(self.profile.outage_duration_min_s, self.profile.outage_duration_max_s)
            self.outage_until = current + dur
            raise EmulatedNetworkError(f"emulated outage started ({dur:.2f}s)")

    def _sample_one_way_delay_s(self) -> float:
        rtt_ms = max(0.0, self.profile.base_rtt_ms + self.rng.gauss(0.0, self.profile.jitter_ms))
        return (rtt_ms / 1000.0) / 2.0

    def _serialization_delay_s(self, nbytes: int, kbps: float) -> float:
        return (nbytes * 8.0) / (kbps * 1000.0)

    def _maybe_loss_or_timeout(self, timeout_s: float) -> None:
        x = self.rng.random()
        if x < self.profile.loss_prob:
            raise EmulatedNetworkError("emulated packet loss / connection failure")
        if x < self.profile.loss_prob + self.profile.timeout_prob:
            time.sleep(min(timeout_s, max(1.0, timeout_s * 0.8)))
            raise EmulatedTimeout("emulated timeout")

    def before_request(self, tx_bytes: int, timeout_s: float) -> Dict[str, float]:
        self._maybe_start_or_continue_outage()
        delay = self._sample_one_way_delay_s() + self._serialization_delay_s(tx_bytes, self.profile.uplink_kbps)
        time.sleep(delay)
        self._maybe_loss_or_timeout(timeout_s)
        return {"pre_delay_s": delay}

    def after_response(self, rx_bytes: int, timeout_s: float) -> Dict[str, float]:
        delay = self._sample_one_way_delay_s() + self._serialization_delay_s(rx_bytes, self.profile.downlink_kbps)
        time.sleep(delay)
        self._maybe_loss_or_timeout(timeout_s)
        return {"post_delay_s": delay}


def load_profile(profile_arg: Optional[str]) -> OutdoorProfile:
    if not profile_arg:
        return OutdoorProfile()
    obj = parse_json_or_file(profile_arg, default={}) or {}
    if isinstance(obj, dict) and "profile" in obj and isinstance(obj["profile"], dict):
        obj = obj["profile"]
    profile = OutdoorProfile.from_any(obj)
    profile.validate()
    return profile


# =========================================================
# Emulated HTTP benchmark
# =========================================================

def http_request_once(
    url: str,
    method: str,
    timeout_s: float,
    headers: Optional[Dict[str, str]],
    body_bytes: Optional[bytes],
) -> Tuple[int, int, float]:
    method = method.upper()
    headers = headers or {}
    if requests is not None:
        session = requests.Session()  # type: ignore[union-attr]
        t0 = time.perf_counter()
        if method == "HEAD":
            resp = session.head(url, timeout=timeout_s, allow_redirects=True, headers=headers)
            status = resp.status_code
            size = 0
        elif method == "POST":
            resp = session.post(url, timeout=timeout_s, headers=headers, data=body_bytes, allow_redirects=True)
            status = resp.status_code
            size = len(resp.content or b"")
        else:
            resp = session.get(url, timeout=timeout_s, headers=headers, allow_redirects=True)
            status = resp.status_code
            size = len(resp.content or b"")
        elapsed = time.perf_counter() - t0
        return status, size, elapsed

    req = urllib.request.Request(url=url, method=method, headers=headers, data=body_bytes if method == "POST" else None)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = b"" if method == "HEAD" else resp.read()
        status = getattr(resp, "status", None) or getattr(resp, "code", None) or 200
    elapsed = time.perf_counter() - t0
    return int(status), len(data), elapsed


def emulate_http(args: argparse.Namespace) -> Dict[str, Any]:
    profile = load_profile(args.profile)
    emu = OutdoorLinkEmulator(profile)

    headers = parse_json_or_file(args.headers, default={}) or {}
    json_body = parse_json_or_file(args.json_body, default=None)

    body_bytes: Optional[bytes] = None
    if json_body is not None:
        body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers = dict(headers)
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    elif args.body_text is not None:
        body_bytes = args.body_text.encode("utf-8")

    rows: List[Dict[str, Any]] = []
    lat_ms_ok: List[float] = []

    tx_bytes = len(body_bytes or b"")
    for i in range(1, args.runs + 1):
        t0 = time.perf_counter()
        row: Dict[str, Any] = {
            "run": i,
            "ok": False,
            "status_code": None,
            "error_type": None,
            "error": None,
            "tx_bytes": tx_bytes,
            "rx_bytes": None,
            "app_elapsed_ms": None,
            "total_elapsed_ms": None,
            "pre_delay_ms": None,
            "post_delay_ms": None,
        }
        try:
            pre = emu.before_request(tx_bytes=tx_bytes, timeout_s=args.timeout)
            status, rx_bytes, app_elapsed_s = http_request_once(
                url=args.url,
                method=args.method,
                timeout_s=args.timeout,
                headers=headers,
                body_bytes=body_bytes,
            )
            post = emu.after_response(rx_bytes=rx_bytes, timeout_s=args.timeout)
            total_ms = (time.perf_counter() - t0) * 1000.0
            row.update({
                "ok": True,
                "status_code": status,
                "rx_bytes": rx_bytes,
                "app_elapsed_ms": round(app_elapsed_s * 1000.0, 6),
                "total_elapsed_ms": round(total_ms, 6),
                "pre_delay_ms": round(pre["pre_delay_s"] * 1000.0, 6),
                "post_delay_ms": round(post["post_delay_s"] * 1000.0, 6),
            })
            lat_ms_ok.append(total_ms)
        except Exception as e:
            row.update({
                "error_type": type(e).__name__,
                "error": str(e),
                "total_elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 6),
            })
        rows.append(row)

    return {
        "tool": "raspi_outdoor_net_tool",
        "mode": "emulate-http",
        "timestamp_epoch": now_epoch(),
        "url": args.url,
        "method": args.method.upper(),
        "profile": asdict(profile),
        "runs": args.runs,
        "timeout_s": args.timeout,
        "summary": {
            "success": sum(1 for r in rows if r["ok"]),
            "failures": sum(1 for r in rows if not r["ok"]),
            "success_rate": round(sum(1 for r in rows if r["ok"]) / len(rows), 6) if rows else None,
            "status_codes": sorted({r["status_code"] for r in rows if r["status_code"] is not None}),
            "latency_total": summarize_floats(lat_ms_ok, "ms"),
        },
        "rows": rows,
    }


# =========================================================
# Emulated CLI benchmark
# =========================================================

def run_cli_once(cmd: Sequence[str], timeout_s: float) -> Tuple[int, int, float, str, str]:
    t0 = time.perf_counter()
    cp = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    elapsed = time.perf_counter() - t0
    rx_bytes = len((cp.stdout or "").encode("utf-8")) + len((cp.stderr or "").encode("utf-8"))
    return cp.returncode, rx_bytes, elapsed, cp.stdout or "", cp.stderr or ""


def emulate_cli(args: argparse.Namespace) -> Dict[str, Any]:
    profile = load_profile(args.profile)
    emu = OutdoorLinkEmulator(profile)

    cmd = shlex.split(args.cmd)
    rows: List[Dict[str, Any]] = []
    lat_ms_ok: List[float] = []

    tx_bytes = sum(len(part.encode("utf-8")) for part in cmd)
    for i in range(1, args.runs + 1):
        t0 = time.perf_counter()
        row: Dict[str, Any] = {
            "run": i,
            "ok": False,
            "returncode": None,
            "error_type": None,
            "error": None,
            "tx_bytes": tx_bytes,
            "rx_bytes": None,
            "app_elapsed_ms": None,
            "total_elapsed_ms": None,
            "pre_delay_ms": None,
            "post_delay_ms": None,
            "stdout_excerpt": None,
            "stderr_excerpt": None,
        }
        try:
            pre = emu.before_request(tx_bytes=tx_bytes, timeout_s=args.timeout)
            rc, rx_bytes, app_elapsed_s, out, err = run_cli_once(cmd=cmd, timeout_s=args.timeout)
            post = emu.after_response(rx_bytes=rx_bytes, timeout_s=args.timeout)
            total_ms = (time.perf_counter() - t0) * 1000.0
            row.update({
                "ok": True,
                "returncode": rc,
                "rx_bytes": rx_bytes,
                "app_elapsed_ms": round(app_elapsed_s * 1000.0, 6),
                "total_elapsed_ms": round(total_ms, 6),
                "pre_delay_ms": round(pre["pre_delay_s"] * 1000.0, 6),
                "post_delay_ms": round(post["post_delay_s"] * 1000.0, 6),
                "stdout_excerpt": out[:200],
                "stderr_excerpt": err[:200],
            })
            lat_ms_ok.append(total_ms)
        except Exception as e:
            row.update({
                "error_type": type(e).__name__,
                "error": str(e),
                "total_elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 6),
            })
        rows.append(row)

    return {
        "tool": "raspi_outdoor_net_tool",
        "mode": "emulate-cli",
        "timestamp_epoch": now_epoch(),
        "cmd": cmd,
        "cmd_str": args.cmd,
        "profile": asdict(profile),
        "runs": args.runs,
        "timeout_s": args.timeout,
        "summary": {
            "success": sum(1 for r in rows if r["ok"]),
            "failures": sum(1 for r in rows if not r["ok"]),
            "success_rate": round(sum(1 for r in rows if r["ok"]) / len(rows), 6) if rows else None,
            "returncodes": sorted({r["returncode"] for r in rows if r["returncode"] is not None}),
            "latency_total": summarize_floats(lat_ms_ok, "ms"),
        },
        "rows": rows,
    }


# =========================================================
# Derive profile from measured metrics JSON
# =========================================================

def derive_profile_from_metrics(metrics: Dict[str, Any], name: str = "derived-profile") -> OutdoorProfile:
    ping_rows = metrics.get("ping", []) or []
    http_rows = metrics.get("http", []) or []
    iperf_rows = metrics.get("iperf", []) or []

    rtt_avgs = [row.get("rtt_avg_ms") for row in ping_rows if row.get("rtt_avg_ms") is not None]
    rtt_mdevs = [row.get("rtt_mdev_ms") for row in ping_rows if row.get("rtt_mdev_ms") is not None]
    losses = [row.get("loss_percent") for row in ping_rows if row.get("loss_percent") is not None]

    failure_rates = []
    for row in http_rows:
        runs = row.get("runs") or 0
        failures = row.get("failures") or 0
        if runs > 0:
            failure_rates.append(float(failures) / float(runs))

    up_kbps_candidates = []
    down_kbps_candidates = []
    for row in iperf_rows:
        if row.get("skipped"):
            continue
        if row.get("reverse"):
            bps = row.get("bits_per_second_received") or row.get("bits_per_second_sent")
            if bps:
                down_kbps_candidates.append(float(bps) / 1000.0)
        else:
            bps = row.get("bits_per_second_sent") or row.get("bits_per_second_received")
            if bps:
                up_kbps_candidates.append(float(bps) / 1000.0)

    if not up_kbps_candidates and not down_kbps_candidates:
        http_mbps = []
        for row in http_rows:
            gp = row.get("goodput", {})
            mean_mbps = gp.get("mean_mbps")
            if mean_mbps:
                http_mbps.append(float(mean_mbps))
        if http_mbps:
            approx_kbps = statistics.mean(http_mbps) * 1000.0
            down_kbps_candidates.append(max(128.0, approx_kbps))
            up_kbps_candidates.append(max(64.0, approx_kbps / 4.0))

    base_rtt_ms = statistics.mean(rtt_avgs) if rtt_avgs else 180.0
    jitter_ms = statistics.mean(rtt_mdevs) if rtt_mdevs else max(20.0, base_rtt_ms * 0.3)
    loss_prob = (statistics.mean(losses) / 100.0) if losses else 0.01
    timeout_prob = statistics.mean(failure_rates) if failure_rates else 0.02
    uplink_kbps = statistics.mean(up_kbps_candidates) if up_kbps_candidates else 512.0
    downlink_kbps = statistics.mean(down_kbps_candidates) if down_kbps_candidates else 2048.0
    outage_prob_per_call = min(0.2, max(0.001, timeout_prob * 0.5 + loss_prob * 0.25))
    outage_duration_min_s = 3.0
    outage_duration_max_s = 10.0 if timeout_prob < 0.1 else 20.0

    profile = OutdoorProfile(
        name=name,
        base_rtt_ms=round(base_rtt_ms, 3),
        jitter_ms=round(jitter_ms, 3),
        uplink_kbps=round(max(32.0, uplink_kbps), 3),
        downlink_kbps=round(max(64.0, downlink_kbps), 3),
        loss_prob=round(min(max(loss_prob, 0.0), 1.0), 6),
        timeout_prob=round(min(max(timeout_prob, 0.0), 1.0), 6),
        outage_prob_per_call=round(outage_prob_per_call, 6),
        outage_duration_min_s=outage_duration_min_s,
        outage_duration_max_s=outage_duration_max_s,
        seed=42,
    )
    profile.validate()
    return profile


PRESET_PROFILES: Dict[str, Dict[str, Any]] = {
    "good": {
        "name": "good",
        "base_rtt_ms": 50.0,
        "jitter_ms": 10.0,
        "uplink_kbps": 5000.0,
        "downlink_kbps": 20000.0,
        "loss_prob": 0.001,
        "timeout_prob": 0.002,
        "outage_prob_per_call": 0.0,
        "outage_duration_min_s": 1.0,
        "outage_duration_max_s": 2.0,
        "seed": 42,
    },
    "normal": {
        "name": "normal",
        "base_rtt_ms": 120.0,
        "jitter_ms": 30.0,
        "uplink_kbps": 2000.0,
        "downlink_kbps": 8000.0,
        "loss_prob": 0.005,
        "timeout_prob": 0.01,
        "outage_prob_per_call": 0.002,
        "outage_duration_min_s": 2.0,
        "outage_duration_max_s": 5.0,
        "seed": 42,
    },
    "bad": {
        "name": "bad",
        "base_rtt_ms": 250.0,
        "jitter_ms": 120.0,
        "uplink_kbps": 256.0,
        "downlink_kbps": 1024.0,
        "loss_prob": 0.02,
        "timeout_prob": 0.03,
        "outage_prob_per_call": 0.02,
        "outage_duration_min_s": 5.0,
        "outage_duration_max_s": 15.0,
        "seed": 42,
    },
    "outage-heavy": {
        "name": "outage-heavy",
        "base_rtt_ms": 400.0,
        "jitter_ms": 200.0,
        "uplink_kbps": 128.0,
        "downlink_kbps": 512.0,
        "loss_prob": 0.05,
        "timeout_prob": 0.08,
        "outage_prob_per_call": 0.05,
        "outage_duration_min_s": 10.0,
        "outage_duration_max_s": 30.0,
        "seed": 42,
    },
}


def flatten_measure_for_csv(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in result.get("ping", []):
        rows.append({
            "section": "ping",
            "target": row.get("host"),
            "loss_percent": row.get("loss_percent"),
            "rtt_avg_ms": row.get("rtt_avg_ms"),
            "rtt_mdev_ms": row.get("rtt_mdev_ms"),
            "rtt_min_ms": row.get("rtt_min_ms"),
            "rtt_max_ms": row.get("rtt_max_ms"),
            "returncode": row.get("returncode"),
        })
    for row in result.get("http", []):
        hdr = row.get("header_latency", {})
        tot = row.get("total_latency", {})
        gp = row.get("goodput", {})
        rows.append({
            "section": "http",
            "target": row.get("url"),
            "runs": row.get("runs"),
            "success": row.get("success"),
            "failures": row.get("failures"),
            "success_rate": row.get("success_rate"),
            "header_mean_ms": hdr.get("mean_ms"),
            "header_p95_ms": hdr.get("p95_ms"),
            "total_mean_ms": tot.get("mean_ms"),
            "total_p95_ms": tot.get("p95_ms"),
            "goodput_mean_mbps": gp.get("mean_mbps"),
        })
    for row in result.get("iperf", []):
        rows.append({
            "section": "iperf3",
            "target": row.get("server"),
            "reverse": row.get("reverse"),
            "bps_sent": row.get("bits_per_second_sent"),
            "bps_received": row.get("bits_per_second_received"),
            "retransmits": row.get("sender_retransmits"),
            "skipped": row.get("skipped"),
        })
    return rows


def flatten_emulation_summary_for_csv(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = result.get("rows", []) or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append({
            "run": row.get("run"),
            "ok": row.get("ok"),
            "status_code": row.get("status_code"),
            "returncode": row.get("returncode"),
            "error_type": row.get("error_type"),
            "tx_bytes": row.get("tx_bytes"),
            "rx_bytes": row.get("rx_bytes"),
            "app_elapsed_ms": row.get("app_elapsed_ms"),
            "total_elapsed_ms": row.get("total_elapsed_ms"),
            "pre_delay_ms": row.get("pre_delay_ms"),
            "post_delay_ms": row.get("post_delay_ms"),
        })
    return out


def print_measure_summary(result: Dict[str, Any]) -> None:
    print("=== PING ===")
    if not result.get("ping"):
        print("(none)")
    for row in result.get("ping", []):
        print(
            f"{row['host']}: loss={row.get('loss_percent')}% avg={row.get('rtt_avg_ms')}ms mdev={row.get('rtt_mdev_ms')}ms"
        )

    print("\n=== HTTP ===")
    if not result.get("http"):
        print("(none)")
    for row in result.get("http", []):
        hdr = row.get("header_latency", {})
        tot = row.get("total_latency", {})
        print(
            f"{row['url']}: success={row.get('success')}/{row.get('runs')} hdr_mean={hdr.get('mean_ms')}ms hdr_p95={hdr.get('p95_ms')}ms total_mean={tot.get('mean_ms')}ms total_p95={tot.get('p95_ms')}ms"
        )

    print("\n=== IPERF3 ===")
    if not result.get("iperf"):
        print("(none)")
    for row in result.get("iperf", []):
        if row.get("skipped"):
            print(f"{row.get('server')}: skipped ({row.get('reason')})")
        else:
            print(
                f"{row.get('server')} reverse={row.get('reverse')}: sent={row.get('bits_per_second_sent')}bps recv={row.get('bits_per_second_received')}bps retrans={row.get('sender_retransmits')}"
            )


def print_emulation_summary(result: Dict[str, Any]) -> None:
    summary = result.get("summary", {})
    lat = summary.get("latency_total", {})
    print(f"mode        : {result.get('mode')}")
    print(f"success     : {summary.get('success')}")
    print(f"failures    : {summary.get('failures')}")
    print(f"success_rate: {summary.get('success_rate')}")
    print(f"mean_ms     : {lat.get('mean_ms')}")
    print(f"p95_ms      : {lat.get('p95_ms')}")
    print(f"p99_ms      : {lat.get('p99_ms')}")


def add_common_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", required=True, help="output JSON file path")
    p.add_argument("--csv-out", default=None, help="optional CSV summary path")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Raspberry Pi single-file tool for network measurement and outdoor-network emulation."
    )
    sub = ap.add_subparsers(dest="subcmd", required=True)

    p = sub.add_parser("measure", help="measure current link/network conditions")
    p.add_argument("--ping-host", action="append", default=[], help="can be specified multiple times")
    p.add_argument("--ping-count", type=int, default=20)
    p.add_argument("--ping-interval", type=float, default=0.5)
    p.add_argument("--ping-timeout", type=float, default=None)
    p.add_argument("--http-url", action="append", default=[], help="can be specified multiple times")
    p.add_argument("--http-runs", type=int, default=10)
    p.add_argument("--http-timeout", type=float, default=10.0)
    p.add_argument("--http-method", choices=["GET", "HEAD", "POST"], default="GET")
    p.add_argument("--http-headers", default=None, help="JSON string or path to JSON file")
    p.add_argument("--http-json-body", default=None, help="JSON string or path to JSON file")
    p.add_argument("--iperf-server", default=None)
    p.add_argument("--iperf-port", type=int, default=5201)
    p.add_argument("--iperf-seconds", type=int, default=10)
    add_common_output_args(p)

    p = sub.add_parser("print-presets", help="print built-in profile presets and exit")
    p.add_argument("--preset", default=None, choices=sorted(PRESET_PROFILES.keys()))
    p.add_argument("--out", default=None)

    p = sub.add_parser("derive-profile", help="derive an emulation profile from a metrics JSON")
    p.add_argument("--metrics", required=True, help="path to measure-mode JSON")
    p.add_argument("--name", default="derived-profile")
    add_common_output_args(p)

    p = sub.add_parser("emulate-http", help="run HTTP benchmark under an emulated network profile")
    p.add_argument("--url", required=True)
    p.add_argument("--method", choices=["GET", "HEAD", "POST"], default="GET")
    p.add_argument("--headers", default=None, help="JSON string or path to JSON file")
    p.add_argument("--json-body", default=None, help="JSON string or path to JSON file")
    p.add_argument("--body-text", default=None, help="plain text request body")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--profile", default=None, help="profile JSON string or path to JSON file, or preset name")
    add_common_output_args(p)

    p = sub.add_parser("emulate-cli", help="run CLI benchmark under an emulated network profile")
    p.add_argument("--cmd", required=True, help="shell-like command string, e.g. 'echo hello'")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--profile", default=None, help="profile JSON string or path to JSON file, or preset name")
    add_common_output_args(p)

    return ap


def do_print_presets(args: argparse.Namespace) -> int:
    obj = PRESET_PROFILES[args.preset] if args.preset else PRESET_PROFILES
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[OK] wrote: {args.out}")
    else:
        print(text)
    return 0


def do_measure(args: argparse.Namespace) -> int:
    if not args.ping_host and not args.http_url and not args.iperf_server:
        print("Specify at least one of --ping-host / --http-url / --iperf-server", file=sys.stderr)
        return 2

    result = measure_network(args)
    json_dump(result, args.out)
    if args.csv_out:
        write_rows_csv(flatten_measure_for_csv(result), args.csv_out)

    print_measure_summary(result)
    print(f"\n[OK] wrote JSON: {args.out}")
    if args.csv_out:
        print(f"[OK] wrote CSV : {args.csv_out}")
    return 0


def do_derive_profile(args: argparse.Namespace) -> int:
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    profile = derive_profile_from_metrics(metrics, name=args.name)
    result = {
        "tool": "raspi_outdoor_net_tool",
        "mode": "derive-profile",
        "timestamp_epoch": now_epoch(),
        "profile": asdict(profile),
        "source_metrics": args.metrics,
    }
    json_dump(result, args.out)
    if args.csv_out:
        write_rows_csv([asdict(profile)], args.csv_out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[OK] wrote JSON: {args.out}")
    if args.csv_out:
        print(f"[OK] wrote CSV : {args.csv_out}")
    return 0


def do_emulate_http(args: argparse.Namespace) -> int:
    if args.profile in PRESET_PROFILES:
        args.profile = json.dumps(PRESET_PROFILES[args.profile], ensure_ascii=False)
    result = emulate_http(args)
    json_dump(result, args.out)
    if args.csv_out:
        write_rows_csv(flatten_emulation_summary_for_csv(result), args.csv_out)
    print_emulation_summary(result)
    print(f"\n[OK] wrote JSON: {args.out}")
    if args.csv_out:
        print(f"[OK] wrote CSV : {args.csv_out}")
    return 0


def do_emulate_cli(args: argparse.Namespace) -> int:
    if args.profile in PRESET_PROFILES:
        args.profile = json.dumps(PRESET_PROFILES[args.profile], ensure_ascii=False)
    result = emulate_cli(args)
    json_dump(result, args.out)
    if args.csv_out:
        write_rows_csv(flatten_emulation_summary_for_csv(result), args.csv_out)
    print_emulation_summary(result)
    print(f"\n[OK] wrote JSON: {args.out}")
    if args.csv_out:
        print(f"[OK] wrote CSV : {args.csv_out}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcmd == "print-presets":
        return do_print_presets(args)
    if args.subcmd == "measure":
        return do_measure(args)
    if args.subcmd == "derive-profile":
        return do_derive_profile(args)
    if args.subcmd == "emulate-http":
        return do_emulate_http(args)
    if args.subcmd == "emulate-cli":
        return do_emulate_cli(args)

    parser.error("unknown subcommand")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
