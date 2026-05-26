#!/usr/bin/env python3
"""Sum total CPU usage by service (across replicas) for Istio vs Mazu.

Reads container-cpu-<rps>.csv for both strategies in a benchmark run,
groups by service (deploy name) and reports:
  - Total cores (app + sidecar), time-averaged across the scrape window
  - Breakdown: app-only and istio-proxy sidecar-only
  - Also prints per-service replica counts (max observed concurrent)

Usage:
    python3 sum_cpu_by_service.py <benchmark_dir> [--rps 400]
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

STRATEGIES = [("istio", "Istio"), ("st5-AttUpd", "Mazu")]


def deploy_name(pod):
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod


def load(csv_path, ts_start=None, ts_end=None):
    """Return:
      per_ts_service[(ts, svc)] = {'app': cores, 'proxy': cores}
      pods_per_ts_service[(ts, svc)] = set of pod names
    """
    per_ts = defaultdict(lambda: {"app": 0.0, "proxy": 0.0})
    pods_per_ts = defaultdict(set)
    timestamps = set()
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp"])
            if ts_start is not None and ts < ts_start:
                continue
            if ts_end is not None and ts > ts_end:
                continue
            pod = row["pod"]
            container = row["container"]
            cpu_m = float(row["cpu_millicores"])
            svc = deploy_name(pod)
            key = (ts, svc)
            bucket = "proxy" if container == "istio-proxy" else "app"
            per_ts[key][bucket] += cpu_m / 1000.0  # cores
            pods_per_ts[key].add(pod)
            timestamps.add(ts)
    return per_ts, pods_per_ts, sorted(timestamps)


def summarize(csv_path, ts_start=None, ts_end=None):
    per_ts, pods_per_ts, timestamps = load(csv_path, ts_start, ts_end)
    services = sorted({svc for (_, svc) in per_ts.keys()})
    n_ts = len(timestamps)

    # Sum per service across all timestamps -> divide by n_ts = avg instantaneous total cores
    out = {}
    for svc in services:
        app_sum = 0.0
        proxy_sum = 0.0
        max_replicas = 0
        for ts in timestamps:
            entry = per_ts.get((ts, svc), {"app": 0.0, "proxy": 0.0})
            app_sum += entry["app"]
            proxy_sum += entry["proxy"]
            r = len(pods_per_ts.get((ts, svc), set()))
            if r > max_replicas:
                max_replicas = r
        out[svc] = {
            "app_cores_avg": app_sum / n_ts,
            "proxy_cores_avg": proxy_sum / n_ts,
            "total_cores_avg": (app_sum + proxy_sum) / n_ts,
            "max_replicas": max_replicas,
        }
    return out, n_ts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("benchmark_dir")
    p.add_argument("--rps", default="400")
    args = p.parse_args()

    bench = Path(args.benchmark_dir)
    results = {}
    for sdir, label in STRATEGIES:
        csv_path = bench / sdir / f"container-cpu-{args.rps}.csv"
        metrics_path = bench / sdir / f"metrics_{args.rps}.json"
        if not csv_path.exists():
            print(f"missing: {csv_path}")
            continue
        ts_start = ts_end = None
        if metrics_path.exists():
            md = json.load(open(metrics_path)).get("metadata", {})
            ts_start = md.get("start_epoch")
            ts_end = md.get("end_epoch")
        summary, n_ts = summarize(csv_path, ts_start, ts_end)
        results[label] = (summary, n_ts)

    # Print comparison table
    all_services = sorted({s for (summary, _) in results.values() for s in summary})

    print(f"\n=== Run: {bench.name}  (RPS={args.rps}) ===")
    for label, (_, n_ts) in results.items():
        print(f"  {label}: {n_ts} scrape samples")

    header = f"{'Service':<16} | {'Istio total':>11} {'app':>7} {'proxy':>7} {'repl':>4} || {'Mazu total':>11} {'app':>7} {'proxy':>7} {'repl':>4} || {'Δ total':>8} {'Δ %':>7}"
    print()
    print(header)
    print("-" * len(header))
    grand = {"Istio": [0.0, 0.0, 0.0], "Mazu": [0.0, 0.0, 0.0]}
    for svc in all_services:
        row = [svc]
        vals = {}
        for label in ("Istio", "Mazu"):
            summary = results.get(label, ({}, 0))[0]
            s = summary.get(svc, {"app_cores_avg": 0, "proxy_cores_avg": 0, "total_cores_avg": 0, "max_replicas": 0})
            vals[label] = s
            grand[label][0] += s["app_cores_avg"]
            grand[label][1] += s["proxy_cores_avg"]
            grand[label][2] += s["total_cores_avg"]
        i = vals["Istio"]; m = vals["Mazu"]
        delta = m["total_cores_avg"] - i["total_cores_avg"]
        pct = (delta / i["total_cores_avg"] * 100) if i["total_cores_avg"] > 1e-9 else float("nan")
        print(f"{svc:<16} | {i['total_cores_avg']:>11.4f} {i['app_cores_avg']:>7.4f} {i['proxy_cores_avg']:>7.4f} {i['max_replicas']:>4} || "
              f"{m['total_cores_avg']:>11.4f} {m['app_cores_avg']:>7.4f} {m['proxy_cores_avg']:>7.4f} {m['max_replicas']:>4} || "
              f"{delta:>+8.4f} {pct:>+6.1f}%")

    print("-" * len(header))
    gi = grand["Istio"]; gm = grand["Mazu"]
    d = gm[2] - gi[2]
    pct = (d / gi[2] * 100) if gi[2] > 1e-9 else float("nan")
    print(f"{'TOTAL (cores)':<16} | {gi[2]:>11.4f} {gi[0]:>7.4f} {gi[1]:>7.4f} {'':>4} || "
          f"{gm[2]:>11.4f} {gm[0]:>7.4f} {gm[1]:>7.4f} {'':>4} || {d:>+8.4f} {pct:>+6.1f}%")


if __name__ == "__main__":
    main()
