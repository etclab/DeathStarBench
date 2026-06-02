#!/usr/bin/env python3
"""
Compare per-second pending pod counts between TWO specific benchmark runs at a
given RPS (default 400), for both strategies (mazu / st5-AttUpd and istio).

A "pending" pod is one whose `ready` field in pods-<rps>.csv is not "True"
(equivalently phase == "Pending"). For each run/strategy we count pending pods
per second, normalizing the unix `timestamp` to a 0-based second offset so the
two runs line up even though they started at different wall-clock times.

This is a run-to-run sanity check: are the two trials consistent, or did one
run see markedly more scheduling churn (more pending pods) at 400 RPS?

Usage:
    python3 compare_pending_pods_two_runs.py
    python3 compare_pending_pods_two_runs.py RUN_A RUN_B [rps]

RUN_A / RUN_B may be a full folder name or just enough of the trailing
timestamp to disambiguate (e.g. 020438). Defaults to the two runs
235948 and 020438 at 400 RPS.

Outputs:
  - compare_pending_<runA>_vs_<runB>_<rps>.pdf
        1x2 grid: one subplot per strategy, overlaying the two runs per-second.
  - compare_pending_<runA>_vs_<runB>_<rps>.csv
        Long format: strategy,run,rps,second,pending_pods
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


def read_pending_by_second(csv_path):
    """dict[second:int] -> pending_count for one pods-<rps>.csv file."""
    pending = {}
    timestamps = set()
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp"])
            timestamps.add(ts)
            if row["ready"].strip() != "True":
                pending[ts] = pending.get(ts, 0) + 1
    if not timestamps:
        return {}
    base = min(timestamps)
    return {ts - base: pending.get(ts, 0) for ts in timestamps}


def collect(runs, rps):
    """dict[run][strategy] -> sorted list of (second, pending_count)."""
    data = {}
    for run in runs:
        data[run] = {}
        for strategy in STRATEGIES:
            csv_path = os.path.join(HERE, run, strategy, f"pods-{rps}.csv")
            if not os.path.isfile(csv_path):
                print(f"[skip] missing {csv_path}")
                data[run][strategy] = []
                continue
            by_second = read_pending_by_second(csv_path)
            data[run][strategy] = sorted(by_second.items())
    return data


def write_csv(data, runs, rps, out_csv):
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "run", "rps", "second", "pending_pods"])
        for strategy in STRATEGIES:
            for run in runs:
                for second, count in data[run][strategy]:
                    w.writerow([strategy, run, rps, second, count])
    print(f"[written] {out_csv}")


def plot(data, runs, rps, out_pdf):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), sharey=True)
    for ax, strategy in zip(axes, STRATEGIES):
        for run, color in zip(runs, RUN_COLORS):
            rows = data[run][strategy]
            if not rows:
                continue
            seconds = [r[0] for r in rows]
            counts = [r[1] for r in rows]
            total = sum(counts)
            peak = max(counts)
            ax.plot(seconds, counts, linewidth=2, color=color,
                    label=f"{run}  (peak {peak}, sum {total})")
        ax.set_title(STRATEGY_LABELS[strategy])
        ax.set_xlabel("Time into benchmark (s)")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend()
    axes[0].set_ylabel("Pending pods (ready=False)")
    fig.suptitle(
        f"Pending pods at {rps} RPS: {runs[0]} vs {runs[1]}"
    )
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


def main():
    parser = argparse.ArgumentParser(
        description="Compare per-second pending pods between two runs."
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
    write_csv(data, runs, rps, os.path.join(HERE, f"compare_pending_{tag}.csv"))
    plot(data, runs, rps, os.path.join(HERE, f"compare_pending_{tag}.pdf"))

    # Console summary
    print("\nSummary (pending pod-seconds = sum over benchmark, peak/sec):")
    for strategy in STRATEGIES:
        print(f"  {STRATEGY_LABELS[strategy]}:")
        for run in runs:
            rows = data[run][strategy]
            total = sum(c for _, c in rows)
            peak = max((c for _, c in rows), default=0)
            secs = len(rows)
            avg = total / secs if secs else 0.0
            print(f"    {run}:  sum={total:4d}  peak={peak:2d}  "
                  f"avg/s={avg:.3f}  ({secs} s observed)")


if __name__ == "__main__":
    main()
