#!/usr/bin/env python3
"""Per-run step traces of ready pods vs time at a single RPS, per service.

For each service, draw all runs as faint step lines (pod count vs seconds since
window start), istio in blue, mazu in orange, with the per-second median run
overlaid as a bold line. This shows when each individual run scaled — unlike a
mean+band plot, the step transitions are preserved instead of smeared into
ramps.

Output: <sweep-dir>/pods_per_service_steps_<rps>.pdf

Usage: ./plot_pods_per_service_steps_rps.py <rps> [--sweep-dir DIR] [--out FILE]
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


def load_run(csv_path: Path) -> tuple[list[str], np.ndarray, np.ndarray] | None:
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        services = [c for c in (reader.fieldnames or []) if c not in SKIP_COLS]
        if not services:
            return None
        ts: list[int] = []
        rows: list[list[int]] = []
        for row in reader:
            try:
                ts.append(int(row["timestamp"]))
                rows.append([int(row[s]) for s in services])
            except (KeyError, ValueError):
                continue
    if not ts:
        return None
    t = np.array(ts) - ts[0]
    return services, t, np.array(rows, dtype=int)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rps", type=int)
    ap.add_argument("--sweep-dir", type=Path, default=Path.cwd())
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    sweep = args.sweep_dir
    run_dirs = sorted(p for p in sweep.glob("benchmark*") if p.is_dir())
    if not run_dirs:
        print(f"error: no benchmark* run dirs under {sweep}", file=sys.stderr)
        return 1

    # per_strat[strat] = (services, list of (t, arr))
    per_strat: dict[str, tuple[list[str], list[tuple[np.ndarray, np.ndarray]]]] = {}
    for strat in STRATEGIES:
        services_ref: list[str] | None = None
        runs: list[tuple[np.ndarray, np.ndarray]] = []
        for rd in run_dirs:
            loaded = load_run(rd / strat / f"pods-{args.rps}-sum.csv")
            if loaded is None:
                continue
            services, t, arr = loaded
            if services_ref is None:
                services_ref = services
            runs.append((t, arr))
        if services_ref and runs:
            per_strat[strat] = (services_ref, runs)

    if not per_strat:
        print("error: no data", file=sys.stderr)
        return 1

    # union of service names
    services_all: list[str] = []
    for services, _ in per_strat.values():
        for s in services:
            if s not in services_all:
                services_all.append(s)

    # drop services that never scale beyond 1 pod on either stack
    def ever_scales(svc: str) -> bool:
        for services, runs in per_strat.values():
            if svc not in services:
                continue
            j = services.index(svc)
            for _, arr in runs:
                if arr[:, j].max() >= 2:
                    return True
        return False

    services_all = [s for s in services_all if ever_scales(s)]

    n = len(services_all)
    ncols = 2
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(7.0 * ncols, 3.2 * nrows),
                             sharex=True, squeeze=False)

    for i, svc in enumerate(services_all):
        ax = axes[i // ncols][i % ncols]
        ymax = 1
        for strat, (services, runs) in per_strat.items():
            if svc not in services:
                continue
            j = services.index(svc)
            color = COLORS[strat]
            # faint per-run step lines
            stacked = []
            tmin_len = min(len(t) for t, _ in runs)
            for t, arr in runs:
                y = arr[:, j]
                ax.step(t, y, where="post", color=color, alpha=0.30, linewidth=1.0)
                stacked.append(y[:tmin_len])
                ymax = max(ymax, int(y.max()))
            # bold median trace
            med = np.median(np.vstack(stacked), axis=0)
            t_ref = runs[0][0][:tmin_len]
            ax.step(t_ref, med, where="post", color=color, linewidth=2.4,
                    label=f"{strat} (median of {len(runs)})")

        ax.set_title(svc)
        ax.set_ylabel("ready pods")
        ax.set_ylim(0.5, ymax + 0.7)
        ax.set_yticks(range(1, ymax + 1))
        ax.grid(True, alpha=0.3)

    for k in range(n, nrows * ncols):
        axes[k // ncols][k % ncols].axis("off")

    for c in range(ncols):
        axes[nrows - 1][c].set_xlabel("time since RPS window start (s)")

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels),
               bbox_to_anchor=(0.5, 1.0))
    fig.suptitle(
        f"Per-run pod scaling at {args.rps} rps "
        f"(faint lines = individual runs, bold = median)",
        y=1.03,
    )
    fig.tight_layout()

    out = args.out or sweep / f"pods_per_service_steps_{args.rps}.pdf"
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
