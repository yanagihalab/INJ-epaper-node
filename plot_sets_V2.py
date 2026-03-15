#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from statistics import mean, median

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def to_float(x: str) -> float | None:
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def percentile(vals: list[float], p: float) -> float:
    a = sorted(vals)
    n = len(a)
    if n == 0:
        return float("nan")
    idx = (n - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return a[lo]
    return a[lo] + (a[hi] - a[lo]) * (idx - lo)


def summarize(vals: list[float]) -> dict[str, float]:
    """
    Summary is computed on the *original unit* (ms in CSV).
    (We only change the plot axis unit when --plot_unit s is used.)
    """
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": min(vals),
        "p50": percentile(vals, 0.50),
        "p95": percentile(vals, 0.95),
        "max": max(vals),
        "mean": mean(vals),
        "median": median(vals),
    }


def scale_vals(vals: list[float], plot_unit: str) -> list[float]:
    """
    Convert plot data only.
    - 'ms': no-op
    - 's' : ms -> seconds
    """
    if plot_unit == "s":
        return [v / 1000.0 for v in vals]
    return vals

def metric_xlabel(metric: str, plot_unit: str = "ms") -> str:
    base = metric.replace("_ms", "")
    name = metric if plot_unit == "ms" else f"{base}_s"
    unit = "ms" if plot_unit == "ms" else "s"

    if metric == "txhash_ms":
        return f"{name} ({unit}): payload start -> txhash obtained"
    if metric == "display_ms":
        return f"{name} ({unit}): txhash obtained -> e-paper display done"
    if metric == "total_ms":
        return f"{name} ({unit}): payload start -> e-paper display done"
    return f"{name} ({unit})"
# 
# def metric_xlabel(metric: str, plot_unit: str = "ms") -> str:
#     """
#     Axis label used in plots.
#     CSV columns are ms (txhash_ms, display_ms, total_ms),
#     but when plotting in seconds, show *_s and unit (s).
#     """
#     base = metric.replace("_ms", "")
#     name = metric if plot_unit == "ms" else f"{base}_s"
#     unit = "ms" if plot_unit == "ms" else "s"
# 
#     if metric == "txhash_ms":
#         return f"{name} ({unit}): payload生成開始→Tx送信でtxhash取得まで"
#     if metric == "display_ms":
#         return f"{name} ({unit}): txhash取得→e-paper表示(epd.display完了)まで"
#     if metric == "total_ms":
#         return f"{name} ({unit}): payload生成開始→e-paper表示完了まで"
#     return f"{name} ({unit})"
# 
def read_csv_numeric(csv_path: Path, col: str) -> list[float]:
    out: list[float] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        if r.fieldnames is None or col not in r.fieldnames:
            raise KeyError(f"{csv_path}: column '{col}' not found. columns={r.fieldnames}")
        for row in r:
            v = to_float(row.get(col, ""))
            if v is not None and math.isfinite(v):
                out.append(v)
    return out


def read_csv_series(csv_path: Path, col: str, xcol: str = "trial") -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        if r.fieldnames is None or col not in r.fieldnames or xcol not in r.fieldnames:
            raise KeyError(f"{csv_path}: need columns '{xcol}' and '{col}'. columns={r.fieldnames}")
        for row in r:
            x = to_float(row.get(xcol, ""))
            y = to_float(row.get(col, ""))
            if x is not None and y is not None and math.isfinite(x) and math.isfinite(y):
                xs.append(x)
                ys.append(y)
    return xs, ys


def plot_hist(
    vals: list[float],
    title: str | None,
    xlabel: str,
    out_png: Path,
    bins: int,
    *,
    xlabel_fs: int = 12,
    ylabel_fs: int = 12,
    tick_fs: int = 10,
) -> None:
    plt.figure()
    plt.hist(vals, bins=bins)
    if title:
        plt.title(title)
    plt.xlabel(xlabel, fontsize=xlabel_fs)
    plt.ylabel("count", fontsize=ylabel_fs)
    plt.tick_params(axis="both", labelsize=tick_fs)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=200)
    plt.close()


def plot_box(
    data: list[list[float]],
    labels: list[str],
    title: str,
    xlabel: str,
    ylabel: str,
    out_png: Path,
) -> None:
    plt.figure()
    # matplotlib>=3.9 uses tick_labels, older uses labels
    try:
        plt.boxplot(data, tick_labels=labels, showfliers=True)  # matplotlib>=3.9
    except TypeError:
        plt.boxplot(data, labels=labels, showfliers=True)  # matplotlib<3.9
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=200)
    plt.close()


def plot_series(
    xs: list[float],
    ys: list[float],
    title: str,
    xlabel: str,
    ylabel: str,
    out_png: Path,
) -> None:
    plt.figure()
    plt.plot(xs, ys)
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=200)
    plt.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Plot graphs from qr_tx_log_set*.csv")
    ap.add_argument("--indir", default=".", help="Directory containing csv files (default: current)")
    ap.add_argument("--pattern", default="qr_tx_log_set*.csv", help="Glob pattern (default: qr_tx_log_set*.csv)")
    ap.add_argument("--outdir", default="plots_sets", help="Output directory (default: plots_sets)")
    ap.add_argument("--bins", type=int, default=40, help="Histogram bins (default: 40)")
    ap.add_argument("--no_box", action="store_true", help="Disable boxplot output")
    ap.add_argument("--no_series", action="store_true", help="Disable timeseries plot output")
    ap.add_argument("--no_all", action="store_true", help="Disable aggregated(all-sets) histogram output")

    # Plot axis unit (convert plot only, summaries remain in ms)
    ap.add_argument(
        "--plot_unit",
        choices=["ms", "s"],
        default="ms",
        help="Plot axis unit (default: ms). Use 's' to show seconds on plots.",
    )

    # ALLSET only: label sizes
    ap.add_argument("--all_xlabel_fs", type=int, default=16, help="ALLSET xlabel fontsize (default: 16)")
    ap.add_argument("--all_ylabel_fs", type=int, default=16, help="ALLSET ylabel fontsize (default: 16)")
    ap.add_argument("--all_tick_fs", type=int, default=12, help="ALLSET tick fontsize (default: 12)")
    args = ap.parse_args()

    indir = Path(args.indir).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    csv_files = sorted(indir.glob(args.pattern))
    if not csv_files:
        raise SystemExit(f"No CSV files matched: {indir}/{args.pattern}")

    metrics = ["txhash_ms", "display_ms", "total_ms"]

    summary_rows: list[dict[str, object]] = []

    box_data: dict[str, list[list[float]]] = {m: [] for m in metrics}
    box_labels: list[str] = []

    all_vals_ms: dict[str, list[float]] = {m: [] for m in metrics}

    for csv_path in csv_files:
        label = csv_path.stem
        box_labels.append(label)

        for m in metrics:
            vals_ms = read_csv_numeric(csv_path, m)
            all_vals_ms[m].extend(vals_ms)

            stat = summarize(vals_ms)
            summary_rows.append({"set": label, "metric": m, **stat})

            vals_plot = scale_vals(vals_ms, args.plot_unit)

            out_png = outdir / f"{label}_hist_{m}.png"
            plot_hist(
                vals_plot,
                title=f"{label} {m} histogram (n={stat.get('n',0)})",
                xlabel=metric_xlabel(m, args.plot_unit),
                out_png=out_png,
                bins=args.bins,
            )
            print(f"[✓] wrote: {out_png}")

            box_data[m].append(vals_plot)

            if not args.no_series:
                xs, ys_ms = read_csv_series(csv_path, m, "trial")
                ys_plot = scale_vals(ys_ms, args.plot_unit)
                out_png2 = outdir / f"{label}_series_{m}.png"
                plot_series(
                    xs,
                    ys_plot,
                    title=f"{label} {m} timeseries",
                    xlabel="trial (1..N within this set)",
                    ylabel=metric_xlabel(m, args.plot_unit),
                    out_png=out_png2,
                )
                print(f"[✓] wrote: {out_png2}")

    if not args.no_box:
        for m in metrics:
            out_png = outdir / f"box_{m}_by_set.png"
            plot_box(
                box_data[m],
                box_labels,
                title=f"{m} by set (boxplot)",
                xlabel="set (qr_tx_log_setX)",
                ylabel=metric_xlabel(m, args.plot_unit),
                out_png=out_png,
            )
            print(f"[✓] wrote: {out_png}")

    if not args.no_all:
        for m in metrics:
            vals_plot = scale_vals(all_vals_ms[m], args.plot_unit)
            out_png = outdir / f"all_hist_{m}.png"
            plot_hist(
                vals_plot,
                title=None,
                xlabel=metric_xlabel(m, args.plot_unit),
                out_png=out_png,
                bins=args.bins,
                xlabel_fs=args.all_xlabel_fs,
                ylabel_fs=args.all_ylabel_fs,
                tick_fs=args.all_tick_fs,
            )
            print(f"[✓] wrote: {out_png}")

    summary_csv = outdir / "summary.csv"
    with summary_csv.open("w", encoding="utf-8", newline="") as f:
        fieldnames = ["set", "metric", "n", "min", "p50", "p95", "max", "mean", "median"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in summary_rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})
    print(f"[✓] wrote: {summary_csv}")

    if not args.no_all:
        all_summary_csv = outdir / "all_summary.csv"
        with all_summary_csv.open("w", encoding="utf-8", newline="") as f:
            fieldnames = ["metric", "n", "min", "p50", "p95", "max", "mean", "median"]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for m in metrics:
                stat = summarize(all_vals_ms[m])
                w.writerow({"metric": m, **stat})
        print(f"[✓] wrote: {all_summary_csv}")


if __name__ == "__main__":
    main()
