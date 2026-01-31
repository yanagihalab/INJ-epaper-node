#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from statistics import mean, median

import matplotlib

# GUI無し環境でも保存できるように
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _to_float(x: str) -> float | None:
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None
    # confirm_ms などで "timeout" が入る場合を弾く
    if s.lower() in {"timeout", "time_out"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_column(csv_path: Path, col: str) -> list[float]:
    vals: list[float] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise RuntimeError("CSV header not found.")
        if col not in reader.fieldnames:
            raise KeyError(f"Column '{col}' not found. Available: {reader.fieldnames}")

        for row in reader:
            v = _to_float(row.get(col, ""))
            if v is not None and math.isfinite(v):
                vals.append(v)

    return vals


def describe(vals: list[float]) -> dict[str, float]:
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    if n == 0:
        return {"n": 0}

    def pct(p: float) -> float:
        # linear interpolation percentile
        idx = (n - 1) * p
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return vals_sorted[lo]
        return vals_sorted[lo] + (vals_sorted[hi] - vals_sorted[lo]) * (idx - lo)

    return {
        "n": n,
        "min": vals_sorted[0],
        "p50": pct(0.50),
        "p95": pct(0.95),
        "max": vals_sorted[-1],
        "mean": mean(vals_sorted),
        "median": median(vals_sorted),
    }


def plot_hist(vals: list[float], title: str, xlabel: str, out_png: Path, bins: int = 30) -> None:
    if len(vals) == 0:
        raise RuntimeError(f"No numeric data to plot for: {title}")

    plt.figure()
    plt.hist(vals, bins=bins)
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel("count")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=200)
    plt.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Plot histogram(s) from timings CSV.")
    ap.add_argument("csv", type=str, help="Input CSV path (e.g., timings_100_run20snun4.csv)")
    ap.add_argument("--bins", type=int, default=30, help="Histogram bins (default: 30)")
    ap.add_argument("--outdir", type=str, default="plots", help="Output directory (default: plots)")
    ap.add_argument("--prefix", type=str, default="", help="Output file prefix (optional)")
    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    prefix = args.prefix

    # 1) broadcast_ms
    broadcast = read_column(csv_path, "broadcast_ms")
    stat_b = describe(broadcast)
    print("[broadcast_ms]", stat_b)

    out_b = outdir / f"{prefix}hist_broadcast_ms.png"
    plot_hist(
        broadcast,
        title=f"broadcast_ms histogram (n={stat_b.get('n', 0)})",
        xlabel="broadcast_ms (ms)",
        out_png=out_b,
        bins=args.bins,
    )
    print(f"[✓] wrote: {out_b}")

    # 2) confirm_ms（存在すれば）
    try:
        confirm = read_column(csv_path, "confirm_ms")
    except KeyError:
        confirm = []

    if len(confirm) > 0:
        stat_c = describe(confirm)
        print("[confirm_ms]", stat_c)

        out_c = outdir / f"{prefix}hist_confirm_ms.png"
        plot_hist(
            confirm,
            title=f"confirm_ms histogram (n={stat_c.get('n', 0)})",
            xlabel="confirm_ms (ms)",
            out_png=out_c,
            bins=args.bins,
        )
        print(f"[✓] wrote: {out_c}")
    else:
        print("[info] confirm_ms column not found or has no numeric data; skipped.")


if __name__ == "__main__":
    main()

