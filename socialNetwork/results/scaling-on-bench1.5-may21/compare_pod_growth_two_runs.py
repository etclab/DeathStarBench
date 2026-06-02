#!/usr/bin/env python3
"""
Compare per-second pod-growth curves between TWO specific benchmark runs at a
given RPS (default 400), for both strategies (mazu / st5-AttUpd and istio).

Unlike analyze_pod_growth.py (which aggregates a mean +/- stddev curve across
ALL benchmark1.5-* run folders), this script isolates two named runs and
overlays them so you can see run-to-run consistency. It reports both:

  1. TOTAL pod growth  -- the `total` column of pods-<rps>-sum.csv per second.
  2. PER-SERVICE growth -- each of the 6 service columns per second.

Rows are aligned across runs by the `index` column (0-based second offset).

Usage:
    python3 compare_pod_growth_two_runs.py
    python3 compare_pod_growth_two_runs.py RUN_A RUN_B [rps]

RUN_A / RUN_B may be a full folder name or just enough of the trailing
timestamp to disambiguate (e.g. 020438). Defaults to the two runs
235948 and 020438 at 400 RPS.

Outputs:
  - compare_pod_growth_total_<runA>_vs_<runB>_<rps>.pdf
        1x2 grid: one subplot per strategy, overlaying total pods/sec.
  - compare_pod_growth_per_service_<runA>_vs_<runB>_<rps>.pdf
        2x3 grid of services x (mazu solid / istio dashed), runs by color.
  - compare_pod_growth_<runA>_vs_<runB>_<rps>.csv
        Long format: strategy,service,run,rps,second,pods
        (service == "total" for the total row).
"""

import argparse
import csv
import glob
import os

import matplotlib

matplotlib.use("Agg")  # headless / no display
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_GLOB = os.path.join(HERE, "benchmark1.5-*")
STRATEGIES = ["st5-AttUpd", "istio"]  # st5-AttUpd == "mazu"
STRATEGY_LABELS = {"st5-AttUpd": "mazu (st5-AttUpd)", "istio": "istio"}
RUN_COLORS = ["tab:blue", "tab:red"]  # one color per run

SERVICES = [
    "details-v1",
    "productpage-v1",
    "ratings-v1",
    "reviews-v1",
    "reviews-v2",
    "reviews-v3",
]
# Columns we read per second; "total" is the sum the upstream tool computes.
COLUMNS = SERVICES + ["total"]

DEFAULT_RUNS = ["235948", "020438"]
DEFAULT_RPS = 400


def resolve_run(token):
    """Resolve a user token to exactly one benchmark run folder name."""
    candidates = sorted(
        os.path.basename(p) for p in glob.glob(RUN_GLOB)
        if token in os.path.basename(p)
    )
    if not candidates:
        raise SystemExit(f"No run folder matches '{token}'")
    if len(candidates) > 1:
        raise SystemExit(
            f"'{token}' is ambiguous, matches: {', '.join(candidates)}"
        )
    return candidates[0]


def read_sum_by_second(csv_path):
    """dict[second:int] -> dict[column:str] -> count for one *-sum.csv file."""
    out = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            second = int(row["index"])
            out[second] = {col: int(row[col]) for col in COLUMNS}
    return out


def collect(runs, rps):
    """dict[run][strategy] -> dict[second] -> {column: count}."""
    data = {}
    for run in runs:
        data[run] = {}
        for strategy in STRATEGIES:
            csv_path = os.path.join(HERE, run, strategy, f"pods-{rps}-sum.csv")
            if not os.path.isfile(csv_path):
                print(f"[skip] missing {csv_path}")
                data[run][strategy] = {}
                continue
            data[run][strategy] = read_sum_by_second(csv_path)
    return data


def series(data, run, strategy, column):
    """Return (seconds, values) for one run/strategy/column, sorted by second."""
    by_second = data[run][strategy]
    seconds = sorted(by_second)
    return seconds, [by_second[s][column] for s in seconds]


def write_csv(data, runs, rps, out_csv):
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "service", "run", "rps", "second", "pods"])
        for strategy in STRATEGIES:
            for column in COLUMNS:
                svc = "total" if column == "total" else column
                for run in runs:
                    seconds, vals = series(data, run, strategy, column)
                    for second, v in zip(seconds, vals):
                        w.writerow([strategy, svc, run, rps, second, v])
    print(f"[written] {out_csv}")


def plot_total(data, runs, rps, out_pdf):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), sharey=True)
    for ax, strategy in zip(axes, STRATEGIES):
        for run, color in zip(runs, RUN_COLORS):
            seconds, vals = series(data, run, strategy, "total")
            if not seconds:
                continue
            final = vals[-1]
            peak = max(vals)
            ax.plot(seconds, vals, linewidth=2, color=color,
                    label=f"{run.split('_')[-1]}  (final {final}, peak {peak})")
        ax.set_title(STRATEGY_LABELS[strategy])
        ax.set_xlabel("Time into benchmark (s)")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend()
    axes[0].set_ylabel("Total pods")
    fig.suptitle(
        f"Total pod growth at {rps} RPS: "
        f"{runs[0].split('_')[-1]} vs {runs[1].split('_')[-1]}"
    )
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


def plot_per_service(data, runs, rps, out_pdf):
    fig, axes = plt.subplots(2, 3, figsize=(15, 9), sharex=True)
    axes = axes.flatten()
    # mazu solid, istio dashed; run distinguished by color.
    style = {"st5-AttUpd": "-", "istio": "--"}

    handles, labels = [], []
    for idx, svc in enumerate(SERVICES):
        ax = axes[idx]
        for strategy in STRATEGIES:
            for run, color in zip(runs, RUN_COLORS):
                seconds, vals = series(data, run, strategy, svc)
                if not seconds:
                    continue
                line, = ax.plot(
                    seconds, vals, linewidth=2, color=color,
                    linestyle=style[strategy],
                    label=f"{STRATEGY_LABELS[strategy]} | {run.split('_')[-1]}",
                )
                if idx == 0:
                    handles.append(line)
                    labels.append(line.get_label())
        ax.set_title(svc)
        ax.set_xlabel("Time into benchmark (s)")
        ax.set_ylabel("Pods")
        ax.grid(True, linestyle="--", alpha=0.5)

    fig.suptitle(
        f"Per-service pod growth at {rps} RPS: "
        f"{runs[0].split('_')[-1]} vs {runs[1].split('_')[-1]} "
        f"(mazu solid, istio dashed)",
        fontsize=14,
    )
    if handles:
        fig.legend(handles, labels, loc="upper right", ncol=2)
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


def main():
    parser = argparse.ArgumentParser(
        description="Compare per-second pod growth between two runs."
    )
    parser.add_argument("run_a", nargs="?", default=DEFAULT_RUNS[0])
    parser.add_argument("run_b", nargs="?", default=DEFAULT_RUNS[1])
    parser.add_argument("rps", nargs="?", type=int, default=DEFAULT_RPS)
    args = parser.parse_args()

    runs = [resolve_run(args.run_a), resolve_run(args.run_b)]
    rps = args.rps
    print(f"Comparing runs: {runs[0]}  vs  {runs[1]}  at {rps} RPS\n")

    data = collect(runs, rps)

    tag = f"{runs[0].split('_')[-1]}_vs_{runs[1].split('_')[-1]}_{rps}"
    write_csv(data, runs, rps,
              os.path.join(HERE, f"compare_pod_growth_{tag}.csv"))
    plot_total(data, runs, rps,
               os.path.join(HERE, f"compare_pod_growth_total_{tag}.pdf"))
    plot_per_service(
        data, runs, rps,
        os.path.join(HERE, f"compare_pod_growth_per_service_{tag}.pdf"))

    # Console summary -------------------------------------------------------
    print("\nTOTAL pods (final = last second, peak = max over benchmark):")
    for strategy in STRATEGIES:
        print(f"  {STRATEGY_LABELS[strategy]}:")
        for run in runs:
            seconds, vals = series(data, run, strategy, "total")
            if not vals:
                print(f"    {run.split('_')[-1]}:  (no data)")
                continue
            print(f"    {run.split('_')[-1]}:  final={vals[-1]:3d}  "
                  f"peak={max(vals):3d}  start={vals[0]:3d}  "
                  f"({len(vals)} s observed)")

    print("\nPER-SERVICE peak pods:")
    header = "    {:<16}".format("service")
    for run in runs:
        for strategy in STRATEGIES:
            header += "{:>18}".format(
                f"{strategy.split('-')[0]}/{run.split('_')[-1]}")
    print(header)
    for svc in SERVICES:
        line = "    {:<16}".format(svc)
        for run in runs:
            for strategy in STRATEGIES:
                _, vals = series(data, run, strategy, svc)
                line += "{:>18}".format(max(vals) if vals else "-")
        print(line)


if __name__ == "__main__":
    main()
