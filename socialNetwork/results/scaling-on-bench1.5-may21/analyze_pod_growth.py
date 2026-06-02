#!/usr/bin/env python3
"""
Analyze per-second pod-growth curves across benchmark runs.

For every benchmark run folder (benchmark1.5-*), every strategy (istio,
st5-AttUpd), and every RPS level, read pods-<rps>-sum.csv. Each file has one
row per second (the `index` column, 0..~119) and a `total` column giving the
total number of pods at that second over the 120s benchmark.

Aligning across the 10 runs by the `index` column, for each second compute the
mean and sample standard deviation (n-1; 0 if only one run) of `total`. This
characterizes the per-second pod-growth curve for each strategy.

Outputs:
  - Long-format raw CSV pod_growth_raw.csv with columns
    strategy,rps,second,n_runs,mean_total_pods,stddev_total_pods
    (named pod_growth_raw_<rps>.csv when a single RPS is requested).
  - Per-RPS line chart (PDF) pod_growth_<rps>.pdf plotting second vs mean
    total pods for mazu (st5-AttUpd) and istio, with a +/- 1 stddev shaded
    band around each mean line.
"""

import argparse
import csv
import glob
import os
import statistics

import matplotlib

matplotlib.use("Agg")  # headless / no display
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
RUN_GLOB = os.path.join(HERE, "benchmark1.5-*")
STRATEGIES = ["st5-AttUpd", "istio"]  # st5-AttUpd == "mazu"
STRATEGY_LABELS = {"st5-AttUpd": "mazu (st5-AttUpd)", "istio": "istio"}
STRATEGY_COLORS = {"st5-AttUpd": "tab:blue", "istio": "tab:orange"}

ALL_RPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200]


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------
def read_totals_by_second(sum_csv_path):
    """Return dict[second:int] -> total:int for one pods-*-sum.csv file."""
    out = {}
    with open(sum_csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            second = int(row["index"])
            out[second] = int(row["total"])
    return out


def collect(rps):
    """
    Walk all runs/strategies for a given RPS and gather per-second totals.

    Returns: dict[strategy][second] -> list of total values across runs.
    """
    data = {s: {} for s in STRATEGIES}
    run_dirs = sorted(glob.glob(RUN_GLOB))
    if not run_dirs:
        raise SystemExit(f"No run folders matched {RUN_GLOB}")

    for run_dir in run_dirs:
        for strategy in STRATEGIES:
            sum_csv = os.path.join(run_dir, strategy, f"pods-{rps}-sum.csv")
            if not os.path.isfile(sum_csv):
                continue
            per_second = read_totals_by_second(sum_csv)
            for second, total in per_second.items():
                data[strategy].setdefault(second, []).append(total)
    return data


def summarize(data):
    """
    Returns dict[strategy] -> sorted list of (second, n, mean, stddev).
    """
    summary = {}
    for strategy in STRATEGIES:
        rows = []
        for second in sorted(data[strategy]):
            totals = data[strategy][second]
            n = len(totals)
            mean = statistics.mean(totals)
            stddev = statistics.stdev(totals) if n > 1 else 0.0
            rows.append((second, n, mean, stddev))
        summary[strategy] = rows
    return summary


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_raw(per_rps_summary, raw_csv):
    """per_rps_summary: dict[rps] -> dict[strategy] -> list of (second,n,mean,std)."""
    with open(raw_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["strategy", "rps", "second", "n_runs",
             "mean_total_pods", "stddev_total_pods"]
        )
        for rps in sorted(per_rps_summary):
            summary = per_rps_summary[rps]
            for strategy in STRATEGIES:
                for second, n, mean, stddev in summary[strategy]:
                    w.writerow(
                        [strategy, rps, second, n,
                         f"{mean:.4f}", f"{stddev:.4f}"]
                    )
    print(f"[written] {raw_csv}")


def plot(rps, summary):
    out_pdf = os.path.join(HERE, f"pod_growth_{rps}.pdf")
    fig, ax = plt.subplots(figsize=(9, 5.5))

    for strategy in STRATEGIES:
        rows = summary[strategy]
        if not rows:
            continue
        seconds = [r[0] for r in rows]
        means = [r[2] for r in rows]
        stds = [r[3] for r in rows]
        lower = [m - s for m, s in zip(means, stds)]
        upper = [m + s for m, s in zip(means, stds)]
        color = STRATEGY_COLORS[strategy]
        ax.plot(
            seconds,
            means,
            linewidth=2,
            label=STRATEGY_LABELS[strategy],
            color=color,
        )
        ax.fill_between(seconds, lower, upper, alpha=0.2, color=color)

    ax.set_xlabel("Time into benchmark (s)")
    ax.set_ylabel("Total pods")
    ax.set_title(
        f"Per-second pod growth at {rps} RPS: mazu (st5-AttUpd) vs istio"
    )
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Per-second pod-growth analysis across benchmark runs."
    )
    parser.add_argument(
        "rps",
        nargs="?",
        type=int,
        default=None,
        help="RPS level to process. Omit to process all 13 RPS levels.",
    )
    args = parser.parse_args()

    if args.rps is not None:
        rps_list = [args.rps]
        raw_csv = os.path.join(HERE, f"pod_growth_raw_{args.rps}.csv")
    else:
        rps_list = ALL_RPS
        raw_csv = os.path.join(HERE, "pod_growth_raw.csv")

    per_rps_summary = {}
    for rps in rps_list:
        data = collect(rps)
        summary = summarize(data)
        per_rps_summary[rps] = summary
        plot(rps, summary)

    write_raw(per_rps_summary, raw_csv)


if __name__ == "__main__":
    main()
