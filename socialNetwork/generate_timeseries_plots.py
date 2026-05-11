#!/usr/bin/env python3
"""
Render combined time-series PDFs (one line per pod x strategy) for benchmark 2.

Reads each strategy's <results_dir>/<strategy>/{cpu,memory}_{app,proxy}_timeseries.dat
files (produced by generate_dat_with_app.py) and writes 4 combined PDFs at the
parent results dir:

    cpu_app_timeseries.pdf
    cpu_proxy_timeseries.pdf
    memory_app_timeseries.pdf
    memory_proxy_timeseries.pdf

Each PDF overlays all strategies on a single axes. Same pod uses the same color
across strategies; strategies are distinguished by line style (solid vs dashed).

Usage:
    python3 generate_timeseries_plots.py <results_dir> [strategy1 strategy2 ...]
"""

import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


STRATEGY_DISPLAY = {"st5-AttUpd": "Mazu", "istio": "Istio"}
# Style per strategy. Order matches the typical run order; first strategy gets
# a dashed line, subsequent ones get solid (so Mazu's "growth" curve pops).
STRATEGY_STYLE = {"istio": "--", "st5-AttUpd": "-"}

# Tol-light palette (matches style.gpi)
PALETTE = [
    "#77AADD", "#EE8866", "#EEDD88", "#FFAABB",
    "#99DDFF", "#44BB99", "#BBCC33", "#AAAA00",
]

GROUPS = ["app", "proxy"]
METRICS = [
    # (metric_key, group, ylabel, scale_fn, title_suffix)
    ("cpu", "app", "CPU (cores)", lambda v: v, "application containers"),
    ("cpu", "proxy", "CPU (cores)", lambda v: v, "istio-proxy sidecars"),
    ("memory", "app", "Memory (MB)", lambda v: v / (1024 * 1024), "application containers"),
    ("memory", "proxy", "Memory (MB)", lambda v: v / (1024 * 1024), "istio-proxy sidecars"),
]


def display_name(strat):
    return STRATEGY_DISPLAY.get(strat, strat)


def line_style(strat, idx):
    return STRATEGY_STYLE.get(strat, "-" if idx > 0 else "--")


def read_timeseries(path):
    """Return (pods, times, {pod: [values]}). Missing values become None.

    Header is the first non-blank line: 'time_offset_s\tpod1\tpod2\t...'
    """
    if not os.path.exists(path):
        return [], [], {}
    with open(path) as f:
        lines = [l.rstrip("\n") for l in f if l.strip()]
    if not lines:
        return [], [], {}
    header = lines[0].split("\t")
    pods = header[1:]
    times = []
    series = {p: [] for p in pods}
    for line in lines[1:]:
        cols = line.split("\t")
        try:
            t = float(cols[0])
        except ValueError:
            continue
        times.append(t)
        for i, p in enumerate(pods):
            try:
                series[p].append(float(cols[i + 1]))
            except (ValueError, IndexError):
                series[p].append(None)
    return pods, times, series


def assign_pod_colors(all_pods):
    """Stable color per pod across all strategies."""
    return {pod: PALETTE[i % len(PALETTE)] for i, pod in enumerate(sorted(all_pods))}


def render_plot(results_dir, strategies, metric_key, group, ylabel, scale_fn, title_suffix):
    fig, ax = plt.subplots(figsize=(7, 4.5))

    # First pass: discover pods across strategies for stable color assignment
    per_strategy = {}
    all_pods = set()
    for strat in strategies:
        path = os.path.join(results_dir, strat, f"{metric_key}_{group}_timeseries.dat")
        pods, times, series = read_timeseries(path)
        per_strategy[strat] = (pods, times, series)
        all_pods.update(pods)

    if not all_pods:
        plt.close(fig)
        print(f"  SKIP: no data for {metric_key}/{group}")
        return

    color_map = assign_pod_colors(all_pods)

    # Build the JSON dump in lockstep with the plotted lines so the file matches
    # the figure exactly (post-scaling, post-strategy-filter).
    dump = {
        "metric": metric_key,
        "group": group,
        "ylabel": ylabel,
        "x_label": "time_offset_seconds_since_benchmark_start",
        "source": {
            strat: os.path.relpath(
                os.path.join(results_dir, strat, f"{metric_key}_{group}_timeseries.dat"),
                results_dir,
            )
            for strat in strategies
        },
        "strategies": {},
    }

    for s_idx, strat in enumerate(strategies):
        pods, times, series = per_strategy[strat]
        if not pods:
            continue
        ls = line_style(strat, s_idx)
        strat_dump = {
            "display_name": display_name(strat),
            "linestyle": ls,
            "times": times,
            "pods": {},
        }
        for pod in pods:
            ys = [scale_fn(v) if v is not None else None for v in series[pod]]
            ax.plot(
                times,
                ys,
                linestyle=ls,
                linewidth=1.8,
                color=color_map[pod],
                label=f"{pod} ({display_name(strat)})",
            )
            strat_dump["pods"][pod] = {"color": color_map[pod], "values": ys}
        dump["strategies"][strat] = strat_dump

    ax.set_xlabel("Time (seconds since benchmark start)")
    ax.set_ylabel(ylabel)
    ax.set_title(f"{metric_key.upper()} usage over time - {title_suffix}")
    ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.6)
    ax.legend(loc="best", fontsize=9, frameon=True)

    out_path = os.path.join(results_dir, f"{metric_key}_{group}_timeseries.pdf")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f"  Generated {out_path}")

    json_path = os.path.join(results_dir, f"{metric_key}_{group}_timeseries.json")
    with open(json_path, "w") as f:
        json.dump(dump, f, indent=2)
    print(f"  Generated {json_path}")


def find_strategies(results_dir):
    out = []
    for name in sorted(os.listdir(results_dir)):
        sub = os.path.join(results_dir, name)
        if os.path.isdir(sub) and any(
            os.path.exists(os.path.join(sub, f"{m}_{g}_timeseries.dat"))
            for m in ("cpu", "memory") for g in GROUPS
        ):
            out.append(name)
    return out


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir> [strategy1 strategy2 ...]", file=sys.stderr)
        sys.exit(1)

    results_dir = sys.argv[1]
    strategies = sys.argv[2:] if len(sys.argv) > 2 else find_strategies(results_dir)
    if not strategies:
        print(f"ERROR: no strategy subdirs with timeseries data in {results_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Rendering combined time-series plots for: {strategies}")
    for metric_key, group, ylabel, scale_fn, title_suffix in METRICS:
        render_plot(results_dir, strategies, metric_key, group, ylabel, scale_fn, title_suffix)


if __name__ == "__main__":
    main()
