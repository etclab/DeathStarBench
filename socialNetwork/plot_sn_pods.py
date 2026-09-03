#!/usr/bin/env python3
"""Plot SocialNetwork replica churn from pods-<rps>-sum.csv files.

The SocialNetwork counterpart to plot_pods.py. Run summarize_sn_pods.py first.

Produces under <run-dir>:
  - plot_sn_pod_totals.pdf   total ready pods vs elapsed seconds, one subplot
                             per RPS, one line per strategy. This is the
                             scale-up curve: how fast each mesh grows its
                             fleet, and where it settles.
  - plot_sn_pod_final.pdf    per-service fleet at the END of each step,
                             grouped bars paired by strategy. Shows WHERE the
                             extra replicas went, which is what distinguishes
                             "the mesh made everything heavier" from "one
                             service on the hot path saturated".

Strategies are discovered from the directory names rather than hardcoded.

Usage: ./plot_sn_pods.py <benchmark-run-dir>
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# Labels, order and colors are shared with plot_sn_latency.py and
# plot_sn_resources.py so that Istio and Mazu are the same series, in the same
# color, in every plot a run directory produces. A reader flipping between the
# replica, latency and resource figures should never have to re-learn which
# line is which.
STRATEGY_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
STRATEGY_ORDER = ["istio", "st5-AttUpd"]
# Paul-Tol palette, matching generate_timeseries_plots.py and
# plot_sn_resources.py -- one color per arm across the whole repo.
STRATEGY_COLORS = {"istio": "#77AADD", "st5-AttUpd": "#EE8866"}


def label_of(strat: str) -> str:
    return STRATEGY_LABELS.get(strat, strat)


def color_of(strat: str):
    """None lets matplotlib fall back to its default cycle for unknown arms."""
    return STRATEGY_COLORS.get(strat)


def ordered(strats):
    """Known strategies in a fixed order, then anything else alphabetically."""
    known = [s for s in STRATEGY_ORDER if s in strats]
    return known + sorted(s for s in strats if s not in STRATEGY_ORDER)


def save(fig, out: Path) -> list:
    """Write both PDF (for papers) and PNG (viewable without a PDF reader)."""
    written = []
    for path in (out, out.with_suffix(".png")):
        fig.savefig(path, dpi=150 if path.suffix == ".png" else None,
                    bbox_inches="tight")
        written.append(path)
    return written


def read_sum(path: Path):
    """-> (elapsed seconds, totals, {service: final count})."""
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return [], [], {}
    idx = [int(r["index"]) for r in rows]
    totals = [int(r["total"]) for r in rows]
    services = [k for k in rows[0] if k not in ("index", "timestamp", "total")]
    final = {s: int(rows[-1][s]) for s in services}
    return idx, totals, final


def collect(run_dir: Path):
    """-> {rps: {strategy: (idx, totals, final)}}"""
    data: dict[int, dict[str, tuple]] = defaultdict(dict)
    for p in sorted(run_dir.glob("*/pods-*-sum.csv")):
        m = re.search(r"pods-(\d+)-sum\.csv$", p.name)
        if not m:
            continue
        data[int(m.group(1))][p.parent.name] = read_sum(p)
    return data


def plot_totals(data, out: Path) -> None:
    rps_values = sorted(data)
    n = len(rps_values)
    ncols = min(2, n)
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 3.6 * nrows),
                             squeeze=False)
    for ax, rps in zip(axes.flat, rps_values):
        for strat in ordered(data[rps]):
            idx, totals, _ = data[rps][strat]
            if idx:
                ax.plot(idx, totals, label=label_of(strat),
                        color=color_of(strat), linewidth=1.6)
        ax.set_title(f"{rps} RPS")
        ax.set_xlabel("elapsed (s)")
        ax.set_ylabel("ready pods")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    for ax in axes.flat[n:]:
        ax.axis("off")
    fig.suptitle("Replica churn: ready pods during each load step")
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)


def plot_growth(data, out: Path) -> None:
    """Total fleet vs RPS, one line per strategy -- the headline growth curve.

    plot_totals() shows the SHAPE of each ramp but gives every step its own
    y-axis, so a 6-pod spread and a 108-pod spread look equally dramatic.
    plot_final() breaks the fleet down per service but only within one step.
    Neither answers the actual comparison question: across the whole sweep, how
    many replicas does each mesh need to carry the same offered load?

    Both the settled fleet (solid, end of step) and the peak reached during the
    step (dashed) are drawn: under HPA the two differ whenever a step is still
    ramping when its window closes, and a settled-only plot would hide an arm
    that overshot and came back down.
    """
    rps_values = sorted(data)
    strats = ordered({st for rps in data for st in data[rps]})
    fig, ax = plt.subplots(figsize=(8, 5))
    for strat in strats:
        xs = [r for r in rps_values if data[r].get(strat) and data[r][strat][1]]
        if not xs:
            continue
        final = [data[r][strat][1][-1] for r in xs]
        peak = [max(data[r][strat][1]) for r in xs]
        ax.plot(xs, final, marker="o", linewidth=1.8,
                color=color_of(strat), label=f"{label_of(strat)} (settled)")
        if peak != final:
            ax.plot(xs, peak, marker="^", linewidth=1.2, linestyle="--",
                    color=color_of(strat), alpha=0.7,
                    label=f"{label_of(strat)} (peak)")
    ax.set_xlabel("target RPS")
    ax.set_ylabel("ready pods")
    ax.set_title("Replica growth under load: total ready pods vs offered RPS")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)


def plot_final(data, out: Path) -> None:
    rps_values = sorted(data)
    n = len(rps_values)
    fig, axes = plt.subplots(n, 1, figsize=(12, 3.4 * n), squeeze=False)
    for ax, rps in zip(axes.flat, rps_values):
        strats = ordered(data[rps])
        services = sorted({s for st in strats for s in data[rps][st][2]
                           if data[rps][st][2].get(s)})
        if not services:
            ax.axis("off")
            continue
        x = np.arange(len(services))
        width = 0.8 / max(len(strats), 1)
        for i, strat in enumerate(strats):
            final = data[rps][strat][2]
            ax.bar(x + i * width - 0.4 + width / 2,
                   [final.get(s, 0) for s in services], width,
                   label=label_of(strat), color=color_of(strat))
        ax.set_xticks(x)
        ax.set_xticklabels(services, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("pods")
        ax.set_title(f"Fleet at end of {rps} RPS step")
        ax.grid(alpha=0.3, axis="y")
        ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    data = collect(run_dir)
    if not data:
        print(f"error: no pods-*-sum.csv under {run_dir}/*/ "
              f"(run summarize_sn_pods.py first)", file=sys.stderr)
        return 1

    totals_out = run_dir / "plot_sn_pod_totals.pdf"
    final_out = run_dir / "plot_sn_pod_final.pdf"
    growth_out = run_dir / "plot_sn_pod_growth.pdf"
    plot_totals(data, totals_out)
    plot_final(data, final_out)
    plot_growth(data, growth_out)
    for out in (growth_out, totals_out, final_out):
        print(f"wrote {out} (+ {out.with_suffix('.png').name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
