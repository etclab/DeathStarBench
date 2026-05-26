#!/usr/bin/env python3
"""Plot per-service ready-pod count vs time at a single RPS, mean +/- stddev across runs.

For each run under <sweep-dir>/benchmark*/<strategy>/pods-<rps>-sum.csv, read every
service column (everything except index/timestamp/total). For each (service, t_sec)
compute mean and stddev across the 10 runs and draw a line + shaded band, one
subplot per service, istio and st5-AttUpd overlaid.

Output: <sweep-dir>/pods_per_service_<rps>.pdf

Usage: ./plot_pods_per_service_rps.py <rps> [--sweep-dir DIR] [--out FILE]
"""

import argparse
import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

STRATEGIES = ["istio", "st5-AttUpd"]
COLORS = {"istio": "tab:blue", "st5-AttUpd": "tab:orange"}
SKIP_COLS = {"index", "timestamp", "total"}


def load_run(csv_path: Path) -> tuple[list[str], np.ndarray] | None:
    """Return (service_names, array shape (T, S)) for one run, t-zeroed by first sample."""
    services: list[str] | None = None
    rows: list[list[int]] = []
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        services = [c for c in (reader.fieldnames or []) if c not in SKIP_COLS]
        if not services:
            return None
        for row in reader:
            try:
                rows.append([int(row[s]) for s in services])
            except (KeyError, ValueError):
                continue
    if not rows:
        return None
    return services, np.array(rows, dtype=float)


def stack_runs(curves: list[np.ndarray]) -> np.ndarray:
    """Trim to shortest run, return (R, T, S)."""
    n = min(c.shape[0] for c in curves)
    return np.stack([c[:n] for c in curves], axis=0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rps", type=int, help="RPS window to plot (e.g. 400)")
    ap.add_argument("--sweep-dir", type=Path, default=Path.cwd(),
                    help="sweep directory containing benchmark* run dirs (default: cwd)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output PDF path (default: <sweep-dir>/pods_per_service_<rps>.pdf)")
    args = ap.parse_args()

    sweep = args.sweep_dir
    if not sweep.is_dir():
        print(f"error: {sweep} is not a directory", file=sys.stderr)
        return 1

    run_dirs = sorted(p for p in sweep.glob("benchmark*") if p.is_dir())
    if not run_dirs:
        print(f"error: no benchmark* run dirs under {sweep}", file=sys.stderr)
        return 1

    # collect curves per strategy, plus service name list
    per_strat: dict[str, tuple[list[str], np.ndarray]] = {}
    for strat in STRATEGIES:
        services_ref: list[str] | None = None
        curves: list[np.ndarray] = []
        for rd in run_dirs:
            csv_path = rd / strat / f"pods-{args.rps}-sum.csv"
            if not csv_path.is_file():
                continue
            loaded = load_run(csv_path)
            if loaded is None:
                continue
            services, arr = loaded
            if services_ref is None:
                services_ref = services
            elif services != services_ref:
                print(f"  warn: {csv_path} has different service columns, skipping",
                      file=sys.stderr)
                continue
            curves.append(arr)
        if not curves or services_ref is None:
            print(f"  warn: no data for {strat} at {args.rps} rps", file=sys.stderr)
            continue
        per_strat[strat] = (services_ref, stack_runs(curves))

    if not per_strat:
        print("error: nothing to plot", file=sys.stderr)
        return 1

    # union of service names (preserve order from first strategy)
    services_all: list[str] = []
    for services, _ in per_strat.values():
        for s in services:
            if s not in services_all:
                services_all.append(s)

    n = len(services_all)
    ncols = 3
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.0 * nrows),
                             sharex=True, squeeze=False)

    for i, svc in enumerate(services_all):
        ax = axes[i // ncols][i % ncols]
        for strat, (services, stacked) in per_strat.items():
            if svc not in services:
                continue
            j = services.index(svc)
            data = stacked[:, :, j]   # (R, T)
            mean = data.mean(axis=0)
            std = data.std(axis=0, ddof=1) if data.shape[0] > 1 else np.zeros_like(mean)
            t = np.arange(mean.shape[0])
            color = COLORS[strat]
            ax.plot(t, mean, color=color, linewidth=1.8,
                    label=f"{strat} (n={data.shape[0]})")
            ax.fill_between(t, mean - std, mean + std, color=color, alpha=0.2)
        ax.set_title(svc)
        ax.grid(True, alpha=0.3)
        ax.set_ylabel("ready pods")

    # hide any unused axes
    for k in range(n, nrows * ncols):
        axes[k // ncols][k % ncols].axis("off")

    for c in range(ncols):
        axes[nrows - 1][c].set_xlabel("time since RPS window start (s)")

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels),
               bbox_to_anchor=(0.5, 1.0))
    fig.suptitle(f"Per-service pod scaling at {args.rps} rps (mean +/- 1 stddev)", y=1.04)
    fig.tight_layout()

    out = args.out or sweep / f"pods_per_service_{args.rps}.pdf"
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
