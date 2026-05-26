#!/usr/bin/env python3
"""Mean +/- stddev of p50/p90/p99 latency across 10 runs, vs rps, per strategy.

For each (strategy, rps) pair, pulls p50/p90/p99 from each of the 10 runs'
wrk2 detailed percentile spectrum, computes mean and stddev, and plots all
six (2 strategies x 3 percentiles) as lines with error bars on a single chart.
Also dumps the extracted values + summary stats to JSON.
"""

import json
import statistics
from pathlib import Path

import matplotlib.pyplot as plt
import pprint

from plot_percentile_boxplots import parse_detailed_spectrum, percentile_at

BASE = Path(__file__).resolve().parent
STRATEGIES = ["istio", "st5-AttUpd"]
RPS_VALUES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200]
PERCENTILES = {
    "p50": 0.50, 
    "p90": 0.90, 
    "p99": 0.99
}

STRATEGY_COLORS = {"istio": "#1f77b4", "st5-AttUpd": "#ff7f0e"}
PERCENTILE_STYLE = {
    "p50": {"linestyle": "-",  "marker": "o"},
    "p90": {"linestyle": "--", "marker": "s"},
    "p99": {"linestyle": ":",  "marker": "^"},
}


def collect():
    runs = sorted(
        d for d in BASE.iterdir() if d.is_dir() and d.name.startswith("benchmark")
    )
    # print(f"inside collect(): runs = \n{"\n".join([run.name for run in runs])}")
    if len(runs) != 10:
        print(f"warning: found {len(runs)} runs, expected 10")

    data = {str(rps): {s: {p: {"values": []} for p in PERCENTILES}
                       for s in STRATEGIES}
            for rps in RPS_VALUES}
    # print(f"\nprepared data map = ")
    # pprint.pprint(data, indent=2)

    for rps in RPS_VALUES:
        for strat in STRATEGIES:
            for run in runs:
                f = run / strat / f"{rps}.txt"
                if not f.exists():
                    continue
                pts = parse_detailed_spectrum(f)
                if not pts:
                    continue
                for pname, ptarget in PERCENTILES.items():
                    v = percentile_at(pts, ptarget)
                    # print(f"percentile value at {ptarget} is {v}")
                    if v is not None:
                        data[str(rps)][strat][pname]["values"].append(v)

    for rps_data in data.values():
        for strat_data in rps_data.values():
            for pname, pdata in strat_data.items():
                vals = pdata["values"]
                pdata["mean"] = statistics.fmean(vals) if vals else None
                pdata["std"] = (statistics.stdev(vals) if len(vals) > 1 else 0.0) if vals else None
                pdata["n"] = len(vals)

    return data, runs


def _draw_series(ax, data, strat, pname):
    xs, means, stds = [], [], []
    for rps in RPS_VALUES:
        d = data[str(rps)][strat][pname]
        if d["mean"] is None:
            continue
        xs.append(rps)
        means.append(d["mean"])
        stds.append(d["std"] or 0.0)

    style = PERCENTILE_STYLE[pname]
    ax.errorbar(
        xs, means, yerr=stds,
        color=STRATEGY_COLORS[strat],
        linestyle=style["linestyle"],
        marker=style["marker"],
        markersize=6,
        linewidth=1.6,
        capsize=3,
        label=f"{strat} {pname}",
    )


def _format_axes(ax, title):
    ax.set_xlabel("Target RPS")
    ax.set_ylabel("Latency (ms)")
    ax.set_yscale("log")
    ax.set_xticks(RPS_VALUES)
    ax.set_xticklabels([str(r) for r in RPS_VALUES], rotation=0, fontsize=8)
    ax.grid(True, which="both", axis="y", alpha=0.3)
    ax.grid(True, which="major", axis="x", alpha=0.2)
    ax.set_title(title)
    ax.legend(fontsize=9, loc="upper left")


def plot(data, out_pdf):
    fig, axes = plt.subplots(2, 2, figsize=(16, 11))

    for ax, pname in zip(axes.flat[:3], PERCENTILES):
        for strat in STRATEGIES:
            _draw_series(ax, data, strat, pname)
        _format_axes(ax, f"{pname} latency vs RPS (mean +/- stddev, n=10)")

    ax_all = axes.flat[3]
    for strat in STRATEGIES:
        for pname in PERCENTILES:
            _draw_series(ax_all, data, strat, pname)
    _format_axes(ax_all, "p50/p90/p99 combined (mean +/- stddev, n=10)")
    ax_all.legend(ncol=2, fontsize=9, loc="upper left")

    fig.tight_layout()
    fig.savefig(out_pdf)
    print(f"wrote {out_pdf}")


def main():
    data, _ = collect()
    # pprint.pprint(data, indent=2)

    out_json = BASE / "percentile_lines.json"
    out_json.write_text(json.dumps(data, indent=2))
    print(f"wrote {out_json}")

    plot(data, BASE / "percentile_lines.pdf")


if __name__ == "__main__":
    main()
