#!/usr/bin/env python3
"""Sum envoy request counters per destination service for Istio vs Mazu.

Reads requests-<rps>.csv (raw envoy admin scrape) and reports:
  - inbound completions per destination service (sum across all server-side pods)
  - outbound completions per source service -> destination service

Counters are monotonically increasing; for each (pod, cluster) we take
last - first within the metrics_<rps>.json window. Pod restarts (counter
resets) are detected and the reset point's prior value is added.

Usage: python3 sum_requests_by_service.py <bench_dir> [--rps 400]
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

STRATEGIES = [("istio", "Istio"), ("st5-AttUpd", "Mazu")]
CLUSTER_RE = re.compile(r'cluster_name="(outbound\|\d+\|\|([^"]+?))"')
INBOUND_RE = re.compile(r'http_conn_manager_prefix="inbound_')


def deploy(pod):
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod


def short_dest(fqdn):
    # details.default.svc.cluster.local -> details
    return fqdn.split(".")[0]


def parse_value(metric_line):
    return int(metric_line.rsplit(" ", 1)[1].strip('"'))


def load(csv_path, ts_start, ts_end):
    # key = (pod, kind, target); kind = "out_<dest>" or "in"
    # store list of (ts, value) so we can detect resets
    series = defaultdict(list)
    with open(csv_path) as f:
        for row in csv.reader(f):
            if row[0] == "timestamp":
                continue
            ts = int(row[0])
            if ts < ts_start or ts > ts_end:
                continue
            pod, metric = row[1], row[2]
            if "_cluster_upstream_rq_completed" in metric:
                m = CLUSTER_RE.search(metric)
                if not m: continue
                dest = short_dest(m.group(2))
                if dest in ("xds-grpc", "prometheus_stats", "agent", "sds-grpc"):
                    continue
                key = (pod, "out", dest)
            elif "_http_downstream_rq_completed" in metric and INBOUND_RE.search(metric):
                key = (pod, "in", "self")
            else:
                continue
            try:
                series[key].append((ts, parse_value(metric)))
            except ValueError:
                continue
    return series


def counter_delta(samples):
    # samples: sorted by ts. handle resets (counter drops).
    samples.sort()
    if len(samples) < 2:
        return samples[-1][1] if samples else 0
    total = 0
    prev = samples[0][1]
    base = samples[0][1]
    for _, v in samples[1:]:
        if v < prev:
            total += prev - base  # finalize the segment
            base = v
        prev = v
    total += prev - base
    return total


def summarize(series):
    # roll up to (src_svc, kind, target)
    rollup = defaultdict(int)
    for (pod, kind, target), samples in series.items():
        src_svc = deploy(pod)
        rollup[(src_svc, kind, target)] += counter_delta(samples)
    return rollup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench_dir")
    ap.add_argument("--rps", default="400")
    args = ap.parse_args()
    bench = Path(args.bench_dir)

    print(f"=== {bench.name} (RPS={args.rps}) ===\n")

    for sdir, label in STRATEGIES:
        csv_path = bench / sdir / f"requests-{args.rps}.csv"
        md_path = bench / sdir / f"metrics_{args.rps}.json"
        if not csv_path.exists():
            print(f"-- {label}: missing {csv_path}"); continue
        md = json.load(open(md_path))["metadata"]
        series = load(csv_path, md["start_epoch"], md["end_epoch"])
        rollup = summarize(series)

        print(f"-- {label} (window {md['duration_seconds']}s) --")
        # inbound (requests this svc *served*)
        in_by_svc = defaultdict(int)
        for (src, kind, _), v in rollup.items():
            if kind == "in":
                in_by_svc[src] += v
        print(f"  {'service':<16} {'inbound_served':>15}")
        for svc in sorted(in_by_svc):
            print(f"  {svc:<16} {in_by_svc[svc]:>15,}")

        # outbound (requests this svc *sent* to each dest)
        out = defaultdict(int)  # (src,dest) -> n
        for (src, kind, dest), v in rollup.items():
            if kind == "out":
                out[(src, dest)] += v
        if out:
            print(f"\n  outbound calls (src -> dest):")
            for (src, dest), v in sorted(out.items()):
                print(f"    {src:<16} -> {dest:<16} {v:>12,}")
        print()


if __name__ == "__main__":
    main()
