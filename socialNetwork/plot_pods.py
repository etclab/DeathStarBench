#!/usr/bin/env python3
"""Plot ready-pod counts from pods-<rps>-sum.csv files.

Produces three figures under <run-dir>:
  - plot_pod_totals.pdf:    8x2 line plot of total ready pods vs. index,
                            one subplot per rps, comparing strategies.
  - plot_pod_breakdown.pdf: 8x2 grouped stacked bars sampled every 10s
                            (plus the final index), per rps, with two bars
                            per bucket (one per strategy) stacked by
                            app-version.
  - plot_pod_scaling.pdf:   1x2 line plot, one subplot per strategy, with
                            all rps total curves overlaid colored by rps —
                            shows how scaling behavior varies with load.

Usage: ./plot_pods.py <benchmark-run-dir>
"""

import argparse
import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

STRATEGIES = ["istio", "st5-AttUpd"]
RPS_VALUES = [50, 100, 200, 300, 400, 500, 600, 700,
              800, 900, 1000, 1100, 1200, 1300, 1400, 1500]
# Subset of RPS_VALUES to overlay in the scaling figure (plot_pod_scaling.pdf).
# Edit this list to compare a different range of rps values.
SCALING_RPS_VALUES = [400, 500, 600, 700, 800]
APP_COLS = ["details-v1", "productpage-v1", "ratings-v1",
            "reviews-v1", "reviews-v2", "reviews-v3"]
SAMPLE_EVERY = 10  # seconds (== rows, since rows are 1s apart)


def load_sum(path: Path) -> dict[str, list[int]]:
    cols: dict[str, list[int]] = {k: [] for k in ["index", "total", *APP_COLS]}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            for k in cols:
                cols[k].append(int(row[k]))
    return cols


def plot_totals(run_dir: Path, out: Path) -> None:
    fig, axes = plt.subplots(8, 2, figsize=(12, 20))
    axes = axes.flatten()

    for i, rps in enumerate(RPS_VALUES):
        ax = axes[i]
        for strat in STRATEGIES:
            p = run_dir / strat / f"pods-{rps}-sum.csv"
            if not p.exists():
                print(f"  warn: missing {p}", file=sys.stderr)
                continue
            d = load_sum(p)
            ax.plot(d["index"], d["total"], label=strat, linewidth=1.2)
        ax.set_title(f"{rps} rps", fontsize=10)
        ax.set_xlabel("time (s)", fontsize=8)
        ax.set_ylabel("ready pods", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.7)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(STRATEGIES),
               bbox_to_anchor=(0.5, 1.0), fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    fig.savefig(out)
    print(f"wrote {out}")
    plt.close(fig)


def plot_breakdown(run_dir: Path, out: Path) -> None:
    fig, axes = plt.subplots(8, 2, figsize=(14, 22))
    axes = axes.flatten()

    cmap = plt.get_cmap("tab10")
    app_colors = {a: cmap(i) for i, a in enumerate(APP_COLS)}
    bar_w = 0.4

    for i, rps in enumerate(RPS_VALUES):
        ax = axes[i]
        per_strat: dict[str, dict[str, list[int]]] = {}
        for strat in STRATEGIES:
            p = run_dir / strat / f"pods-{rps}-sum.csv"
            if not p.exists():
                print(f"  warn: missing {p}", file=sys.stderr)
                continue
            per_strat[strat] = load_sum(p)

        if not per_strat:
            ax.set_title(f"{rps} rps (no data)", fontsize=10)
            continue

        # Common sample points: indices that are multiples of SAMPLE_EVERY
        # and present in every strategy. Use index ranges per strategy and
        # intersect to the shorter one so bars line up.
        max_common = min(max(d["index"]) for d in per_strat.values())
        sample_idx = list(range(0, max_common + 1, SAMPLE_EVERY))
        if not sample_idx:
            ax.set_title(f"{rps} rps (too short)", fontsize=10)
            continue
        if sample_idx[-1] != max_common:
            sample_idx.append(max_common)

        x = np.arange(len(sample_idx))
        offsets = {STRATEGIES[0]: -bar_w / 2, STRATEGIES[1]: bar_w / 2}

        for strat, d in per_strat.items():
            # d["index"] is 0..N; we can index lists directly.
            bottom = np.zeros(len(sample_idx))
            xpos = x + offsets[strat]
            for app in APP_COLS:
                vals = np.array([d[app][k] for k in sample_idx])
                ax.bar(xpos, vals, width=bar_w, bottom=bottom,
                       color=app_colors[app], edgecolor="white", linewidth=0.2)
                bottom += vals

        ax.set_title(f"{rps} rps", fontsize=10)
        ax.set_xticks(x)
        ax.set_xticklabels([str(s) for s in sample_idx], fontsize=7)
        ax.set_xlabel("time (s)", fontsize=8)
        ax.set_ylabel("ready pods", fontsize=8)
        ax.tick_params(axis="y", labelsize=7)
        ax.grid(True, axis="y", linestyle=":", linewidth=0.5, alpha=0.7)

        # subplot-local annotation for which bar is which strategy
        ax.text(0.01, 0.98,
                f"L: {STRATEGIES[0]}   R: {STRATEGIES[1]}",
                transform=ax.transAxes, fontsize=7, va="top", ha="left",
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="0.8",
                          alpha=0.8))

    legend_handles = [plt.Rectangle((0, 0), 1, 1, color=app_colors[a]) for a in APP_COLS]
    fig.legend(legend_handles, APP_COLS, loc="upper center",
               ncol=len(APP_COLS), bbox_to_anchor=(0.5, 1.0), fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.975))
    fig.savefig(out)
    print(f"wrote {out}")
    plt.close(fig)


def plot_scaling(run_dir: Path, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), sharey=True)

    # rps curves distinguished by (color, marker). Lines stay solid so the
    # curves themselves are easy to read. Colors are distributed across the
    # selected rps values from the tab10 palette so each rps gets its own
    # color; markers cycle through 4 shapes for additional differentiation.
    rps_to_plot = SCALING_RPS_VALUES
    palette = plt.get_cmap("tab10").colors
    markers = ["*", "_", "x", "o"]
    style_for: dict[int, dict] = {}
    for i, rps in enumerate(rps_to_plot):
        style_for[rps] = dict(
            color=palette[i % len(palette)],
            marker=markers[i % len(markers)],
        )

    for ax, strat in zip(axes, STRATEGIES):
        for rps in rps_to_plot:
            p = run_dir / strat / f"pods-{rps}-sum.csv"
            if not p.exists():
                print(f"  warn: missing {p}", file=sys.stderr)
                continue
            d = load_sum(p)
            s = style_for[rps]
            ax.plot(d["index"], d["total"],
                    color=s["color"], linestyle="-",
                    marker=s["marker"], markersize=5, markevery=10,
                    linewidth=1.3, alpha=0.9,
                    label=f"{rps} rps")
        ax.set_title(strat, fontsize=11)
        ax.set_xlabel("time (s)", fontsize=9)
        ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.7)
    axes[0].set_ylabel("total ready pods", fontsize=9)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="center right", bbox_to_anchor=(1.0, 0.5),
               fontsize=8, ncol=1, frameon=True, title="rps", title_fontsize=9)
    fig.tight_layout(rect=(0, 0, 0.92, 1.0))
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out}")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()
    if not args.run_dir.is_dir():
        print(f"error: {args.run_dir} is not a directory", file=sys.stderr)
        return 1

    plot_totals(args.run_dir, args.run_dir / "plot_pod_totals.pdf")
    plot_breakdown(args.run_dir, args.run_dir / "plot_pod_breakdown.pdf")
    plot_scaling(args.run_dir, args.run_dir / "plot_pod_scaling.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
