#!/usr/bin/env python3
"""
Analyze total pods created across benchmark runs.

For every benchmark run folder (benchmark1.5-*), every strategy (istio,
st5-AttUpd), and every RPS level, read pods-<rps>-sum.csv and take the
`total` column of the LAST row -- i.e. the total number of pods that existed
at the end of the 120s benchmark.

Outputs:
  - Raw per-run data printed to stdout and written to pod_totals_raw.csv
  - Per-strategy / per-RPS average and standard deviation printed to stdout
    and written to pod_totals_summary.csv
  - A line chart (PDF) plotting mazu (st5-AttUpd) vs istio: mean total pods
    vs RPS, with +/- 1 stddev error bars.
"""

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

RAW_CSV = os.path.join(HERE, "pod_totals_raw.csv")
SUMMARY_CSV = os.path.join(HERE, "pod_totals_summary.csv")
OUTPUT_PDF = os.path.join(HERE, "pod_totals_mazu_vs_istio.pdf")


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------
def final_total_pods(sum_csv_path):
    """Return the `total` column from the last row of a pods-*-sum.csv file."""
    with open(sum_csv_path, newline="") as f:
        reader = csv.DictReader(f)
        last = None
        for row in reader:
            last = row
        if last is None:
            raise ValueError(f"No data rows in {sum_csv_path}")
        return int(last["total"])


def collect():
    """
    Walk all runs/strategies/rps and gather final pod totals.

    Returns: dict[strategy][rps] -> list of (run_name, total) tuples
    """
    data = {s: {} for s in STRATEGIES}
    run_dirs = sorted(glob.glob(RUN_GLOB))
    if not run_dirs:
        raise SystemExit(f"No run folders matched {RUN_GLOB}")

    for run_dir in run_dirs:
        run_name = os.path.basename(run_dir)
        for strategy in STRATEGIES:
            strat_dir = os.path.join(run_dir, strategy)
            if not os.path.isdir(strat_dir):
                continue
            for sum_csv in glob.glob(os.path.join(strat_dir, "pods-*-sum.csv")):
                base = os.path.basename(sum_csv)
                # pods-<rps>-sum.csv
                rps = int(base[len("pods-"):-len("-sum.csv")])
                total = final_total_pods(sum_csv)
                data[strategy].setdefault(rps, []).append((run_name, total))
    return data


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_raw(data):
    rows = []
    for strategy in STRATEGIES:
        for rps in sorted(data[strategy]):
            for run_name, total in sorted(data[strategy][rps]):
                rows.append((strategy, rps, run_name, total))

    with open(RAW_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "rps", "run", "total_pods"])
        w.writerows(rows)

    print("=== RAW DATA (final total pods at end of 120s) ===")
    print(f"{'strategy':<18}{'rps':>6}{'total':>8}  run")
    for strategy, rps, run_name, total in rows:
        print(f"{strategy:<18}{rps:>6}{total:>8}  {run_name}")
    print(f"\n[written] {RAW_CSV}")


def summarize(data):
    """Returns dict[strategy] -> sorted list of (rps, n, mean, stddev)."""
    summary = {}
    for strategy in STRATEGIES:
        rows = []
        for rps in sorted(data[strategy]):
            totals = [t for _, t in data[strategy][rps]]
            n = len(totals)
            mean = statistics.mean(totals)
            # sample stddev; 0 if only one run
            stddev = statistics.stdev(totals) if n > 1 else 0.0
            rows.append((rps, n, mean, stddev))
        summary[strategy] = rows
    return summary


def write_summary(summary):
    with open(SUMMARY_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "rps", "n_runs", "mean_total_pods", "stddev_total_pods"])
        for strategy in STRATEGIES:
            for rps, n, mean, stddev in summary[strategy]:
                w.writerow([strategy, rps, n, f"{mean:.4f}", f"{stddev:.4f}"])

    print("\n=== SUMMARY (avg +/- stddev across runs) ===")
    print(f"{'strategy':<18}{'rps':>6}{'n':>4}{'mean':>10}{'stddev':>10}")
    for strategy in STRATEGIES:
        for rps, n, mean, stddev in summary[strategy]:
            print(f"{strategy:<18}{rps:>6}{n:>4}{mean:>10.2f}{stddev:>10.2f}")
    print(f"\n[written] {SUMMARY_CSV}")


def plot(summary):
    fig, ax = plt.subplots(figsize=(9, 5.5))

    for strategy in STRATEGIES:
        rows = summary[strategy]
        rps_vals = [r[0] for r in rows]
        means = [r[2] for r in rows]
        stds = [r[3] for r in rows]
        ax.errorbar(
            rps_vals,
            means,
            yerr=stds,
            marker="o",
            capsize=3,
            linewidth=2,
            label=STRATEGY_LABELS[strategy],
            color=STRATEGY_COLORS[strategy],
        )

    ax.set_xlabel("Request rate (RPS)")
    ax.set_ylabel("Total pods at end of 120s benchmark")
    ax.set_title("Total pods created: mazu (st5-AttUpd) vs istio")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUTPUT_PDF)
    print(f"\n[written] {OUTPUT_PDF}")


def main():
    data = collect()
    write_raw(data)
    summary = summarize(data)
    write_summary(summary)
    plot(summary)


if __name__ == "__main__":
    main()
