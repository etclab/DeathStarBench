#!/usr/bin/env python3
"""
Total pending "pod-seconds" per strategy, swept over every RPS level.

For each benchmark run / strategy / RPS, the total pending pod-seconds is the
number of (pod, second) observations in pods-<rps>.csv where the pod was not
ready. Because the raw CSV samples at a 1-second cadence with one row per
(pod, second), each non-ready observation contributes exactly 1 pod-second, so
this count IS the integral of the pending-pod count over the whole benchmark --
summed across ALL services (no per-service breakdown here).

A pod is "pending" at a second when its `ready` field is not "True".

This gives a single number per (strategy, rps, run): how much total pod-time was
spent waiting to become ready. Aggregated across the 10 runs (mean / stddev /
min / max) per (strategy, rps), it answers "how much longer does istio keep pods
pending than mazu?" with one value per RPS level.

Strategies:
    st5-AttUpd  ==  "mazu"
    istio       ==  baseline

CLI: no arguments. Always processes all runs and all RPS levels.

    python3 analyze_pending_pod_seconds_totals.py

Outputs (written next to this script):
  - pending_pod_seconds_totals.json
        Structured summary. Top level keyed by strategy, then by rps:
        {"per_run": {run: total}, "values": [...], "n_runs": N,
         "mean": ..., "stddev": ..., "min": ..., "max": ..., "sum": ...}
        Plus a "meta" block (strategy labels, rps levels, run list).
  - pending_pod_seconds_totals_raw.csv
        One row per (strategy, rps, run): strategy,rps,run,
        total_pending_pod_seconds
"""

import csv
import glob
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_GLOB = os.path.join(HERE, "benchmark1.5-*")
STRATEGIES = ["st5-AttUpd", "istio"]  # st5-AttUpd == "mazu"
STRATEGY_LABELS = {"st5-AttUpd": "mazu", "istio": "istio"}
RPS_LEVELS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200]


def total_pending_pod_seconds(csv_path):
    """Count (pod, second) observations where the pod was not ready, across all
    services. With 1-second sampling this equals total pending pod-seconds."""
    pending = 0
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["ready"].strip() != "True":
                pending += 1
    return pending


def collect():
    """dict[strategy][rps] -> dict[run] -> total_pending_pod_seconds, plus the
    flat list of raw rows and the sorted run names actually seen."""
    data = {s: {r: {} for r in RPS_LEVELS} for s in STRATEGIES}
    raw_rows = []  # (strategy, rps, run, total)
    seen_runs = set()

    run_dirs = sorted(glob.glob(RUN_GLOB))
    if not run_dirs:
        raise SystemExit(f"No run folders matched {RUN_GLOB}")

    for run_dir in run_dirs:
        run = os.path.basename(run_dir)
        for strategy in STRATEGIES:
            for rps in RPS_LEVELS:
                csv_path = os.path.join(run_dir, strategy, f"pods-{rps}.csv")
                if not os.path.isfile(csv_path):
                    continue
                total = total_pending_pod_seconds(csv_path)
                data[strategy][rps][run] = total
                raw_rows.append((strategy, rps, run, total))
                seen_runs.add(run)
    return data, raw_rows, sorted(seen_runs)


def summarize(data):
    """Per (strategy, rps): per-run map + aggregate stats across runs."""
    out = {}
    for strategy in STRATEGIES:
        out[strategy] = {}
        for rps in RPS_LEVELS:
            per_run = data[strategy][rps]
            values = [per_run[r] for r in sorted(per_run)]
            n = len(values)
            out[strategy][str(rps)] = {
                "per_run": per_run,
                "values": values,
                "n_runs": n,
                "mean": statistics.mean(values) if n else 0.0,
                "stddev": statistics.stdev(values) if n > 1 else 0.0,
                "min": min(values) if n else 0,
                "max": max(values) if n else 0,
                "sum": sum(values),
            }
    return out


def across_rps(summary, focus_rps=400):
    """Per strategy: the average pending pod-seconds across all RPS levels (the
    mean of the per-RPS means), and how the focus_rps level compares to it."""
    out = {}
    for strategy in STRATEGIES:
        per_rps_means = [summary[strategy][str(r)]["mean"] for r in RPS_LEVELS]
        avg = statistics.mean(per_rps_means)
        at_focus = summary[strategy][str(focus_rps)]["mean"]
        out[strategy] = {
            "avg_across_rps": avg,
            "stddev_across_rps": statistics.stdev(per_rps_means),
            "focus_rps": focus_rps,
            "at_focus_rps": at_focus,
            "focus_minus_avg": at_focus - avg,
            "focus_over_avg": (at_focus / avg) if avg else float("inf"),
        }
    return out


def write_raw(raw_rows, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "rps", "run", "total_pending_pod_seconds"])
        for strategy, rps, run, total in sorted(raw_rows):
            w.writerow([strategy, rps, run, total])
    print(f"[written] {path}")


def write_json(summary, across, runs, path):
    payload = {
        "meta": {
            "metric": "total_pending_pod_seconds",
            "description": "(pod, second) observations with ready != True, "
                           "summed over all services; == pending-pod count "
                           "integrated over the benchmark (1s cadence).",
            "strategy_labels": STRATEGY_LABELS,
            "rps_levels": RPS_LEVELS,
            "runs": runs,
            "n_runs": len(runs),
        },
        "data": summary,
        "across_rps": across,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"[written] {path}")


def main():
    data, raw_rows, runs = collect()
    summary = summarize(data)
    across = across_rps(summary, focus_rps=400)

    write_raw(raw_rows, os.path.join(HERE, "pending_pod_seconds_totals_raw.csv"))
    write_json(summary, across, runs,
               os.path.join(HERE, "pending_pod_seconds_totals.json"))

    # Console summary: mean total pending pod-seconds per RPS, mazu vs istio.
    print(f"\nMean total pending pod-seconds across {len(runs)} runs "
          f"(all services summed):")
    print(f"  {'rps':>6s}{'mazu':>12s}{'istio':>12s}"
          f"{'delta(m-i)':>14s}{'ratio(m/i)':>12s}")
    for rps in RPS_LEVELS:
        mazu = summary["st5-AttUpd"][str(rps)]["mean"]
        istio = summary["istio"][str(rps)]["mean"]
        ratio = f"{mazu / istio:.2f}x" if istio else "inf"
        print(f"  {rps:>6d}{mazu:>12.1f}{istio:>12.1f}"
              f"{mazu - istio:>14.1f}{ratio:>12s}")

    # Console summary: average across all RPS levels vs the 400 RPS value.
    focus = 400
    print(f"\nAverage pending pod-seconds across all {len(RPS_LEVELS)} RPS "
          f"levels vs {focus} RPS:")
    print(f"  {'strategy':10s}{'avg(all rps)':>14s}{f'@{focus}rps':>12s}"
          f"{'delta':>12s}{'ratio(400/avg)':>16s}")
    for strategy in STRATEGIES:
        a = across[strategy]
        print(f"  {STRATEGY_LABELS[strategy]:10s}{a['avg_across_rps']:>14.1f}"
              f"{a['at_focus_rps']:>12.1f}{a['focus_minus_avg']:>+12.1f}"
              f"{a['focus_over_avg']:>15.2f}x")


if __name__ == "__main__":
    main()
