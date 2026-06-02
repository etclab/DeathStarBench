#!/usr/bin/env python3
"""
Analyze per-second, PER-SERVICE pod-growth curves across benchmark runs.

For every benchmark run folder (benchmark1.5-*), every strategy (istio,
st5-AttUpd), and a chosen RPS level, read pods-<rps>-sum.csv. Each file has one
row per second (the `index` column, 0..~119) and one column per service giving
that service's pod count at that second over the 120s benchmark.

The per-service pod-count columns are details-v1, productpage-v1, ratings-v1,
reviews-v1, reviews-v2, reviews-v3 (the `total` column is their sum).

Aligning across the 10 runs by the `index` column, for each second and each
service compute the mean and sample standard deviation (n-1; 0 if only one
run) of that service's pod count. This characterizes the per-service
pod-growth curve for each strategy, making the bottleneck service visible.

Outputs:
  - Long-format raw CSV pod_growth_per_service_raw_<rps>.csv with columns
    strategy,service,rps,second,n_runs,mean_pods,stddev_pods
  - A per-RPS grid chart (PDF) pod_growth_per_service_<rps>.pdf -- one subplot
    per service plotting second vs mean pod count for mazu (st5-AttUpd) and
    istio, with a +/- 1 stddev shaded band around each mean line. Y-axes are
    independent per subplot so the bottleneck service is visible.
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

# Per-service pod-count columns (NOT `total`, which is their sum).
SERVICES = [
    "details-v1",
    "productpage-v1",
    "ratings-v1",
    "reviews-v1",
    "reviews-v2",
    "reviews-v3",
]

DEFAULT_RPS = 400


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------
def read_pods_by_second(sum_csv_path):
    """
    Return dict[second:int] -> dict[service:str] -> pods:int for one
    pods-*-sum.csv file.
    """
    out = {}
    with open(sum_csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            second = int(row["index"])
            out[second] = {svc: int(row[svc]) for svc in SERVICES}
    return out


def collect(rps):
    """
    Walk all runs/strategies for a given RPS and gather per-second,
    per-service pod counts.

    Returns: dict[strategy][service][second] -> list of pod-count values
    across runs.
    """
    data = {s: {svc: {} for svc in SERVICES} for s in STRATEGIES}
    run_dirs = sorted(glob.glob(RUN_GLOB))
    if not run_dirs:
        raise SystemExit(f"No run folders matched {RUN_GLOB}")

    for run_dir in run_dirs:
        for strategy in STRATEGIES:
            sum_csv = os.path.join(run_dir, strategy, f"pods-{rps}-sum.csv")
            if not os.path.isfile(sum_csv):
                continue
            per_second = read_pods_by_second(sum_csv)
            for second, svc_counts in per_second.items():
                for svc in SERVICES:
                    data[strategy][svc].setdefault(second, []).append(
                        svc_counts[svc]
                    )
    return data


def summarize(data):
    """
    Returns dict[strategy][service] -> sorted list of (second, n, mean, stddev).
    """
    summary = {s: {} for s in STRATEGIES}
    for strategy in STRATEGIES:
        for svc in SERVICES:
            rows = []
            for second in sorted(data[strategy][svc]):
                counts = data[strategy][svc][second]
                n = len(counts)
                mean = statistics.mean(counts)
                stddev = statistics.stdev(counts) if n > 1 else 0.0
                rows.append((second, n, mean, stddev))
            summary[strategy][svc] = rows
    return summary


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_raw(rps, summary, raw_csv):
    """summary: dict[strategy][service] -> list of (second, n, mean, std)."""
    with open(raw_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["strategy", "service", "rps", "second", "n_runs",
             "mean_pods", "stddev_pods"]
        )
        for strategy in STRATEGIES:
            for svc in SERVICES:
                for second, n, mean, stddev in summary[strategy][svc]:
                    w.writerow(
                        [strategy, svc, rps, second, n,
                         f"{mean:.4f}", f"{stddev:.4f}"]
                    )
    print(f"[written] {raw_csv}")


def plot(rps, summary):
    out_pdf = os.path.join(HERE, f"pod_growth_per_service_{rps}.pdf")
    fig, axes = plt.subplots(2, 3, figsize=(15, 9), sharex=True)
    axes = axes.flatten()

    legend_handles = None
    legend_labels = None

    for idx, svc in enumerate(SERVICES):
        ax = axes[idx]
        for strategy in STRATEGIES:
            rows = summary[strategy][svc]
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

        ax.set_title(svc)
        ax.set_xlabel("Time into benchmark (s)")
        ax.set_ylabel("Pods")
        ax.grid(True, linestyle="--", alpha=0.5)
        # Y-axes intentionally independent per subplot so the bottleneck
        # service is visible (services scale to very different counts).
        if legend_handles is None:
            legend_handles, legend_labels = ax.get_legend_handles_labels()

    fig.suptitle(
        f"Per-service pod growth at {rps} RPS: mazu (st5-AttUpd) vs istio",
        fontsize=14,
    )
    if legend_handles:
        fig.legend(
            legend_handles,
            legend_labels,
            loc="upper right",
            ncol=2,
        )
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Per-second, per-service pod-growth analysis across "
                    "benchmark runs."
    )
    parser.add_argument(
        "rps",
        nargs="?",
        type=int,
        default=DEFAULT_RPS,
        help=f"RPS level to process (default: {DEFAULT_RPS}).",
    )
    args = parser.parse_args()
    rps = args.rps

    raw_csv = os.path.join(HERE, f"pod_growth_per_service_raw_{rps}.csv")

    data = collect(rps)
    summary = summarize(data)
    write_raw(rps, summary, raw_csv)
    plot(rps, summary)


if __name__ == "__main__":
    main()
