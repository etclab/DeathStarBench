#!/usr/bin/env python3
"""
Per-second *pending* pod counts broken down by individual service, aggregated
across the 10 benchmark runs at a given RPS (default 400) -- i.e. *which
service is the source of the pending/scheduling churn?*

This is the per-service companion to analyze_pending_pods.py. A pod is
"pending" at a second when its `ready` field in pods-<rps>.csv is not "True"
(equivalently phase == "Pending"). The raw file has one row per (pod, second)
with columns: timestamp,app,version,phase,ready. A service is identified as
"<app>-<version>" (e.g. productpage-v1, reviews-v3).

For each run/strategy/service we count pending pods per second, normalizing the
unix `timestamp` to a 0-based second offset so the 10 runs align. Then for each
second we compute mean +/- sample stddev (n-1; 0 if one run) across runs.

CLI: optional positional `rps`, defaults to 400.

    python3 analyze_pending_pods_per_service.py        # 400 RPS
    python3 analyze_pending_pods_per_service.py 600

Outputs:
  - pending_pods_per_service_raw_<rps>.csv
        Long format: strategy,service,rps,second,n_runs,mean_pending,stddev_pending
  - pending_pods_per_service_<rps>.pdf
        2x3 grid of subplots, one per service. Each: x=second, y=mean pending
        pods, one line per strategy, +/-1 sigma shaded band. Y-axes independent
        per subplot so a low-volume service is still visible.
"""

import argparse
import csv
import glob
import os
import statistics

import matplotlib

matplotlib.use("Agg")  # headless / no display
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_GLOB = os.path.join(HERE, "benchmark1.5-*")
STRATEGIES = ["st5-AttUpd", "istio"]  # st5-AttUpd == "mazu"
STRATEGY_LABELS = {"st5-AttUpd": "mazu (st5-AttUpd)", "istio": "istio"}
STRATEGY_COLORS = {"st5-AttUpd": "tab:blue", "istio": "tab:orange"}

# Service plotting order (2x3 grid). frontend aggregator first.
SERVICES = [
    "productpage-v1",
    "details-v1",
    "ratings-v1",
    "reviews-v1",
    "reviews-v2",
    "reviews-v3",
]

DEFAULT_RPS = 400


def read_pending_by_service_second(csv_path):
    """
    Return dict[service] -> dict[second] -> pending_count for one
    pods-<rps>.csv file. Every observed second is recorded for every service
    that appears (default 0 pending) so curves do not drop rows.
    """
    pending = {svc: {} for svc in SERVICES}   # service -> raw_ts -> count
    timestamps = set()
    seen = set()                              # services actually observed
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp"])
            timestamps.add(ts)
            svc = f"{row['app']}-{row['version']}"
            if svc not in pending:
                pending[svc] = {}
            seen.add(svc)
            if row["ready"].strip() != "True":
                pending[svc][ts] = pending[svc].get(ts, 0) + 1

    if not timestamps:
        return {}
    base = min(timestamps)
    out = {}
    for svc in seen:
        out[svc] = {ts - base: pending[svc].get(ts, 0) for ts in timestamps}
    return out


def collect(rps):
    """dict[strategy][service][second] -> list of pending counts across runs."""
    data = {s: {svc: {} for svc in SERVICES} for s in STRATEGIES}
    run_dirs = sorted(glob.glob(RUN_GLOB))
    if not run_dirs:
        raise SystemExit(f"No run folders matched {RUN_GLOB}")

    for run_dir in run_dirs:
        for strategy in STRATEGIES:
            csv_path = os.path.join(run_dir, strategy, f"pods-{rps}.csv")
            if not os.path.isfile(csv_path):
                continue
            by_svc = read_pending_by_service_second(csv_path)
            for svc, by_second in by_svc.items():
                bucket = data[strategy].setdefault(svc, {})
                for second, count in by_second.items():
                    bucket.setdefault(second, []).append(count)
    return data


def summarize(data):
    """dict[strategy][service] -> sorted list of (second, n, mean, stddev)."""
    summary = {s: {} for s in STRATEGIES}
    for strategy in STRATEGIES:
        for svc in data[strategy]:
            rows = []
            for second in sorted(data[strategy][svc]):
                counts = data[strategy][svc][second]
                n = len(counts)
                mean = statistics.mean(counts)
                stddev = statistics.stdev(counts) if n > 1 else 0.0
                rows.append((second, n, mean, stddev))
            summary[strategy][svc] = rows
    return summary


def write_raw(rps, summary, raw_csv):
    with open(raw_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "service", "rps", "second", "n_runs",
                    "mean_pending_pods", "stddev_pending_pods"])
        for strategy in STRATEGIES:
            for svc in SERVICES:
                for second, n, mean, stddev in summary[strategy].get(svc, []):
                    w.writerow([strategy, svc, rps, second, n,
                                f"{mean:.4f}", f"{stddev:.4f}"])
    print(f"[written] {raw_csv}")


def plot(rps, summary):
    out_pdf = os.path.join(HERE, f"pending_pods_per_service_{rps}.pdf")
    fig, axes = plt.subplots(2, 3, figsize=(16, 9))
    axes = axes.flatten()

    for ax, svc in zip(axes, SERVICES):
        for strategy in STRATEGIES:
            rows = summary[strategy].get(svc, [])
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
        ax.set_title(svc)
        ax.set_xlabel("Time into benchmark (s)")
        ax.set_ylabel("Pending pods (ready=False)")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend(fontsize=8)

    fig.suptitle(
        f"Per-second pending pods by service at {rps} RPS: "
        "mazu (st5-AttUpd) vs istio (independent y-axes)",
        fontsize=13,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


def main():
    parser = argparse.ArgumentParser(
        description="Per-service per-second pending-pod analysis across runs."
    )
    parser.add_argument("rps", nargs="?", type=int, default=DEFAULT_RPS,
                        help=f"RPS level to process (default {DEFAULT_RPS}).")
    args = parser.parse_args()
    rps = args.rps

    data = collect(rps)
    summary = summarize(data)
    write_raw(rps, summary,
              os.path.join(HERE, f"pending_pods_per_service_raw_{rps}.csv"))
    plot(rps, summary)

    # Console summary: peak mean pending per service per strategy.
    print(f"\nPeak mean pending pods per service at {rps} RPS:")
    header = f"  {'service':16s}" + "".join(
        f"{STRATEGY_LABELS[s]:>22s}" for s in STRATEGIES)
    print(header)
    for svc in SERVICES:
        line = f"  {svc:16s}"
        for strategy in STRATEGIES:
            rows = summary[strategy].get(svc, [])
            peak = max((r[2] for r in rows), default=0.0)
            total = sum(r[2] for r in rows)
            line += f"{f'peak {peak:.2f} / sum {total:.1f}':>22s}"
        print(line)


if __name__ == "__main__":
    main()
