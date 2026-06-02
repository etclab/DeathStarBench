#!/usr/bin/env python3
"""
Analyze per-second *pending* pod counts across benchmark runs.

Motivation: investigating the mazu p50 latency spike at 400 RPS
(gnuplot_p50.dat *_pooled columns). Pending pods (containers that have been
scheduled but are not yet Ready) are a likely cause of latency spikes, since
requests routed to a not-yet-ready replica stall. This is *not* summarized in
the pods-<rps>-sum.csv files, so we read the raw pods-<rps>.csv instead.

For every benchmark run folder (benchmark1.5-*), every strategy (istio,
st5-AttUpd), and the requested RPS level, read pods-<rps>.csv. That file has
one row per (pod, second) with columns:

    timestamp,app,version,phase,ready

A pod is "pending" at a given second when ready != "True" (equivalently
phase == "Pending"). For each second we count the number of pending pods
across all services. Because there is no per-second index column, the unix
`timestamp` is normalized to a 0-based second offset (timestamp - min) so the
10 runs align even though they started at different wall-clock times.

Aligning across runs by that second offset, for each second we compute the
mean and sample standard deviation (n-1; 0 if only one run) of the pending pod
count. We also compute, per run, the time-averaged pending pod count over the
whole benchmark, then aggregate mean +/- stddev of that scalar across runs.

Outputs (default rps=400):
  - pending_pods_raw_<rps>.csv      long format per-second mean/stddev
  - pending_pods_<rps>.pdf          per-second pending curve, shaded +/-1 sigma
  - pending_pods_avg_<rps>.csv      per-run avg pending + across-run mean/stddev
  - pending_pods_avg_<rps>.pdf      bar chart of across-run mean +/-1 sigma
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

DEFAULT_RPS = 400


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------
def read_pending_by_second(csv_path):
    """
    Return dict[second:int] -> pending_count:int for one pods-<rps>.csv file.

    `second` is the 0-based offset (timestamp - min timestamp in the file).
    A pod counts as pending when its `ready` field is not "True". Every second
    that appears in the file is recorded, including seconds with 0 pending
    (where all observed pods are Ready), so the curve does not drop rows.
    """
    pending = {}        # raw_timestamp -> pending count
    timestamps = set()
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp"])
            timestamps.add(ts)
            ready = row["ready"].strip()
            if ready != "True":
                pending[ts] = pending.get(ts, 0) + 1

    if not timestamps:
        return {}
    base = min(timestamps)
    # Ensure every observed second has an entry (default 0 pending).
    return {ts - base: pending.get(ts, 0) for ts in timestamps}


def collect(rps):
    """
    Walk all runs/strategies for a given RPS and gather per-second pending
    counts, plus the per-run time-averaged pending count.

    Returns:
      per_second:  dict[strategy][second] -> list of pending counts across runs
      per_run_avg: dict[strategy] -> list of per-run mean pending counts
    """
    per_second = {s: {} for s in STRATEGIES}
    per_run_avg = {s: [] for s in STRATEGIES}
    run_dirs = sorted(glob.glob(RUN_GLOB))
    if not run_dirs:
        raise SystemExit(f"No run folders matched {RUN_GLOB}")

    for run_dir in run_dirs:
        for strategy in STRATEGIES:
            csv_path = os.path.join(run_dir, strategy, f"pods-{rps}.csv")
            if not os.path.isfile(csv_path):
                continue
            by_second = read_pending_by_second(csv_path)
            if not by_second:
                continue
            for second, count in by_second.items():
                per_second[strategy].setdefault(second, []).append(count)
            per_run_avg[strategy].append(statistics.mean(by_second.values()))
    return per_second, per_run_avg


def summarize_per_second(per_second):
    """dict[strategy] -> sorted list of (second, n, mean, stddev)."""
    summary = {}
    for strategy in STRATEGIES:
        rows = []
        for second in sorted(per_second[strategy]):
            counts = per_second[strategy][second]
            n = len(counts)
            mean = statistics.mean(counts)
            stddev = statistics.stdev(counts) if n > 1 else 0.0
            rows.append((second, n, mean, stddev))
        summary[strategy] = rows
    return summary


def summarize_per_run(per_run_avg):
    """dict[strategy] -> (n_runs, mean_across_runs, stddev_across_runs)."""
    summary = {}
    for strategy in STRATEGIES:
        vals = per_run_avg[strategy]
        n = len(vals)
        mean = statistics.mean(vals) if n else 0.0
        stddev = statistics.stdev(vals) if n > 1 else 0.0
        summary[strategy] = (n, mean, stddev)
    return summary


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_per_second_raw(rps, summary, raw_csv):
    with open(raw_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["strategy", "rps", "second", "n_runs",
             "mean_pending_pods", "stddev_pending_pods"]
        )
        for strategy in STRATEGIES:
            for second, n, mean, stddev in summary[strategy]:
                w.writerow(
                    [strategy, rps, second, n, f"{mean:.4f}", f"{stddev:.4f}"]
                )
    print(f"[written] {raw_csv}")


def write_per_run_summary(rps, per_run_avg, avg_summary, avg_csv):
    with open(avg_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["strategy", "rps", "n_runs",
             "mean_pending_across_runs", "stddev_pending_across_runs",
             "per_run_avg_pending"]
        )
        for strategy in STRATEGIES:
            n, mean, stddev = avg_summary[strategy]
            per_run = ";".join(f"{v:.4f}" for v in per_run_avg[strategy])
            w.writerow([strategy, rps, n, f"{mean:.4f}", f"{stddev:.4f}", per_run])
    print(f"[written] {avg_csv}")


def plot_per_second(rps, summary):
    out_pdf = os.path.join(HERE, f"pending_pods_{rps}.pdf")
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
        ax.plot(seconds, means, linewidth=2,
                label=STRATEGY_LABELS[strategy], color=color)
        ax.fill_between(seconds, lower, upper, alpha=0.2, color=color)

    ax.set_xlabel("Time into benchmark (s)")
    ax.set_ylabel("Pending pods (ready=False)")
    ax.set_title(
        f"Per-second pending pods at {rps} RPS: mazu (st5-AttUpd) vs istio"
    )
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


def plot_per_run_avg(rps, avg_summary):
    out_pdf = os.path.join(HERE, f"pending_pods_avg_{rps}.pdf")
    fig, ax = plt.subplots(figsize=(6, 5.5))

    labels = [STRATEGY_LABELS[s] for s in STRATEGIES]
    means = [avg_summary[s][1] for s in STRATEGIES]
    stds = [avg_summary[s][2] for s in STRATEGIES]
    colors = [STRATEGY_COLORS[s] for s in STRATEGIES]
    ns = [avg_summary[s][0] for s in STRATEGIES]

    x = range(len(STRATEGIES))
    ax.bar(x, means, yerr=stds, capsize=8, color=colors, alpha=0.85)
    for xi, m, n in zip(x, means, ns):
        ax.text(xi, m, f"{m:.2f}\n(n={n})", ha="center", va="bottom")

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Mean pending pods per second (avg over benchmark)")
    ax.set_title(
        f"Avg pending pods across {max(ns)} runs at {rps} RPS\n"
        "(error bar = +/-1 sample stddev across runs)"
    )
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Per-second pending-pod analysis across benchmark runs."
    )
    parser.add_argument(
        "rps", nargs="?", type=int, default=DEFAULT_RPS,
        help=f"RPS level to process (default {DEFAULT_RPS}).",
    )
    args = parser.parse_args()
    rps = args.rps

    per_second, per_run_avg = collect(rps)
    ps_summary = summarize_per_second(per_second)
    avg_summary = summarize_per_run(per_run_avg)

    write_per_second_raw(
        rps, ps_summary, os.path.join(HERE, f"pending_pods_raw_{rps}.csv")
    )
    write_per_run_summary(
        rps, per_run_avg, avg_summary,
        os.path.join(HERE, f"pending_pods_avg_{rps}.csv"),
    )
    plot_per_second(rps, ps_summary)
    plot_per_run_avg(rps, avg_summary)

    # Console summary
    print(f"\nPending-pod summary at {rps} RPS:")
    for strategy in STRATEGIES:
        n, mean, stddev = avg_summary[strategy]
        peak = max((r[2] for r in ps_summary[strategy]), default=0.0)
        print(f"  {STRATEGY_LABELS[strategy]:20s}  "
              f"n_runs={n}  avg_pending/s={mean:.3f} +/- {stddev:.3f}  "
              f"peak_mean_pending={peak:.2f}")


if __name__ == "__main__":
    main()
