#!/usr/bin/env python3
"""Plot ready-pod count vs time at a single RPS, across all runs in a sweep.

For each run under <sweep-dir>/benchmark*/<strategy>/pods-<rps>-sum.csv, plot
the `total` column against seconds since that window's first sample. Draws
one faint line per run and a bold mean per strategy, both strategies on the
same axes. Output: <sweep-dir>/pods_timeseries_<rps>.pdf

Usage: ./plot_pods_timeseries_rps.py <rps> [--sweep-dir DIR] [--out FILE]
"""

import argparse
import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

STRATEGIES = ["istio", "st5-AttUpd"]
COLORS = {"istio": "tab:blue", "st5-AttUpd": "tab:orange"}


def load_run(csv_path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    ts: list[int] = []
    tot: list[int] = []
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts.append(int(row["timestamp"]))
                tot.append(int(row["total"]))
            except (KeyError, ValueError):
                continue
    if not ts:
        return None
    t = np.array(ts) - ts[0]
    return t, np.array(tot)


def mean_curve(curves: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    # align on shortest run; data is 1 Hz so indices map directly
    n = min(len(t) for t, _ in curves)
    stacked = np.vstack([y[:n] for _, y in curves])
    t = curves[0][0][:n]
    return t, stacked.mean(axis=0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rps", type=int, help="RPS window to plot (e.g. 400)")
    ap.add_argument("--sweep-dir", type=Path, default=Path.cwd(),
                    help="sweep directory containing benchmark* run dirs (default: cwd)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output PDF path (default: <sweep-dir>/pods_timeseries_<rps>.pdf)")
    args = ap.parse_args()

    sweep = args.sweep_dir
    if not sweep.is_dir():
        print(f"error: {sweep} is not a directory", file=sys.stderr)
        return 1

    run_dirs = sorted(p for p in sweep.glob("benchmark*") if p.is_dir())
    if not run_dirs:
        print(f"error: no benchmark* run dirs under {sweep}", file=sys.stderr)
        return 1

    fig, ax = plt.subplots(figsize=(8, 4.5))

    for strat in STRATEGIES:
        curves: list[tuple[np.ndarray, np.ndarray]] = []
        for rd in run_dirs:
            csv_path = rd / strat / f"pods-{args.rps}-sum.csv"
            if not csv_path.is_file():
                continue
            c = load_run(csv_path)
            if c is None:
                continue
            curves.append(c)
        if not curves:
            print(f"  warn: no data for {strat} at {args.rps} rps", file=sys.stderr)
            continue

        color = COLORS[strat]
        for t, y in curves:
            ax.plot(t, y, color=color, alpha=0.25, linewidth=0.8)
        tm, ym = mean_curve(curves)
        ax.plot(tm, ym, color=color, linewidth=2.0, label=f"{strat} (mean of {len(curves)})")

    ax.set_xlabel("time since RPS window start (s)")
    ax.set_ylabel("ready pods (Running+Ready, total)")
    ax.set_title(f"Pod scaling at {args.rps} rps")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.tight_layout()

    out = args.out or sweep / f"pods_timeseries_{args.rps}.pdf"
    fig.savefig(out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
