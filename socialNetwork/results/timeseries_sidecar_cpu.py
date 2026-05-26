#!/usr/bin/env python3
"""Per-service istio-proxy CPU time series (Istio vs Mazu) for one benchmark run.

For each strategy and service, emits at each scrape timestamp (within the 120s
benchmark window from metrics_<rps>.json):
    t_rel, sidecar_cores_sum, app_cores_sum, n_replicas, sidecar_cores_per_replica

Output: <bench>/sidecar_ts_<strategy>.dat (one file per strategy)
        with one gnuplot block per service (separated by blank lines, indexed by service name).

Usage:
    python3 timeseries_sidecar_cpu.py <bench_dir> [--rps 400]
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

STRATEGIES = [("istio", "Istio"), ("st5-AttUpd", "Mazu")]
SERVICES = ["details-v1", "productpage-v1", "ratings-v1",
            "reviews-v1", "reviews-v2", "reviews-v3"]


def deploy_name(pod):
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod


def build_ts(csv_path, ts_start, ts_end):
    # per_ts[ts][svc] = {'app': cores, 'proxy': cores, 'pods': set()}
    per_ts = defaultdict(lambda: defaultdict(lambda: {"app": 0.0, "proxy": 0.0, "pods": set()}))
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            ts = int(row["timestamp"])
            if ts < ts_start or ts > ts_end:
                continue
            pod = row["pod"]
            svc = deploy_name(pod)
            container = row["container"]
            cpu_cores = float(row["cpu_millicores"]) / 1000.0
            bucket = "proxy" if container == "istio-proxy" else "app"
            per_ts[ts][svc][bucket] += cpu_cores
            per_ts[ts][svc]["pods"].add(pod)
    return per_ts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench_dir")
    ap.add_argument("--rps", default="400")
    args = ap.parse_args()
    bench = Path(args.bench_dir)

    for sdir, label in STRATEGIES:
        csv_path = bench / sdir / f"container-cpu-{args.rps}.csv"
        md_path = bench / sdir / f"metrics_{args.rps}.json"
        if not csv_path.exists() or not md_path.exists():
            print(f"skip {sdir}: missing inputs")
            continue
        md = json.load(open(md_path))["metadata"]
        ts_start, ts_end = md["start_epoch"], md["end_epoch"]

        per_ts = build_ts(csv_path, ts_start, ts_end)
        timestamps = sorted(per_ts.keys())

        out_path = bench / f"sidecar_ts_{sdir}.dat"
        with open(out_path, "w") as f:
            f.write(f"# Strategy: {label}\n")
            f.write(f"# window: {ts_start}..{ts_end} ({ts_end - ts_start}s)\n")
            f.write("# Each block = one service (gnuplot 'index' by service name below).\n")
            for i, svc in enumerate(SERVICES):
                if i > 0:
                    f.write("\n\n")
                f.write(f"# service: {svc}\n")
                f.write(f"# {'t_rel':<6} {'proxy_sum':>10} {'app_sum':>9} {'replicas':>9} {'proxy_per_rep':>14} {'app_per_rep':>12}\n")
                for ts in timestamps:
                    entry = per_ts[ts].get(svc, {"app": 0.0, "proxy": 0.0, "pods": set()})
                    n = len(entry["pods"])
                    proxy_per = entry["proxy"] / n if n else 0.0
                    app_per = entry["app"] / n if n else 0.0
                    f.write(f"  {ts - ts_start:<6} {entry['proxy']:>10.4f} {entry['app']:>9.4f} {n:>9d} {proxy_per:>14.4f} {app_per:>12.4f}\n")
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
