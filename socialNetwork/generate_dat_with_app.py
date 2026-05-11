#!/usr/bin/env python3
"""
Generate gnuplot .dat files from Prometheus range-query metrics JSON files
produced by collect_metrics_with_app.sh.

Usage:
    python3 generate_dat_with_app.py <results_dir>

Reads all metrics_*.json in <results_dir> and writes:
  - cpu.dat / memory.dat
        Per-RPS averages (mean across the window) per pod, one column per pod.
        Column names are prefixed with the component group: e.g. "app/fortio-client",
        "proxy/fortio-client". Compatible with the existing generate_plot_data.py
        downstream pipeline (per-row component comparison across strategies).
  - cpu_app_timeseries.dat / cpu_proxy_timeseries.dat
  - memory_app_timeseries.dat / memory_proxy_timeseries.dat
        Per-pod time series for the *single-RPS* run (benchmark 2 only emits one
        metrics_*.json per strategy). Columns: time_offset_s, pod1, pod2, ...
        time_offset is seconds since metadata.start_epoch.

If a results dir contains multiple metrics_*.json files (e.g. an RPS sweep), the
averages .dat collapses them into one row per RPS, but only the first file's
time series is emitted (line plots over time only make sense for a single run).
"""

import json
import glob
import os
import sys
from statistics import mean

from generate_dat import deploy_name


# Component group key in the metrics JSON -> short label used as column prefix
COMPONENT_GROUPS = ["app", "proxy", "istiod", "kube_apiserver"]
TIMESERIES_GROUPS = ["app", "proxy"]


def _series_values(series):
    """Return numeric values from a Prometheus matrix `values` array, skipping NaNs."""
    out = []
    for _, v in series.get("values", []):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:
            continue
        out.append(f)
    return out


def _unique_label(base, taken):
    """Return `base`, or base-2 / base-3 / ... if base is already taken."""
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def extract_pod_series(prom_response):
    """Extract {deploy_name: [v1, v2, ...]} from a Prometheus query_range response."""
    out = {}
    for series in prom_response.get("data", {}).get("result", []):
        pod = series.get("metric", {}).get("pod", "unknown")
        name = _unique_label(deploy_name(pod), out)
        out[name] = _series_values(series)
    return out


def extract_pod_timeseries(prom_response, start_epoch):
    """Extract {deploy_name: [(t_offset, value), ...]} from a query_range response."""
    out = {}
    for series in prom_response.get("data", {}).get("result", []):
        pod = series.get("metric", {}).get("pod", "unknown")
        name = _unique_label(deploy_name(pod), out)
        points = []
        for ts, v in series.get("values", []):
            try:
                t = float(ts) - float(start_epoch)
                f = float(v)
            except (TypeError, ValueError):
                continue
            points.append((t, f))
        out[name] = points
    return out


def generate_avg_dat(metrics, metric_key, out_path):
    """Write per-RPS averages: one row per RPS, columns prefixed by component group."""
    # Discover all (group, pod) pairs across all RPS levels
    group_pods = {g: set() for g in COMPONENT_GROUPS}
    for m in metrics:
        for g in COMPONENT_GROUPS:
            group_pods[g].update(extract_pod_series(m[metric_key][g]).keys())
    group_pods = {g: sorted(pods) for g, pods in group_pods.items()}

    columns = ["# RPS"]
    for g in COMPONENT_GROUPS:
        for pod in group_pods[g]:
            # Use slash for app/proxy (per-pod), bare name for the singletons
            if g in TIMESERIES_GROUPS:
                columns.append(f"{g}/{pod}")
            else:
                columns.append(pod)

    with open(out_path, "w") as f:
        f.write("\t".join(columns) + "\n")
        for m in metrics:
            row = [str(m["metadata"]["rps"])]
            for g in COMPONENT_GROUPS:
                pod_series = extract_pod_series(m[metric_key][g])
                for pod in group_pods[g]:
                    vals = pod_series.get(pod, [])
                    row.append(f"{mean(vals):.6f}" if vals else "N/A")
            f.write("\t".join(row) + "\n")


def generate_timeseries_dat(metric, metric_key, group, out_path):
    """Write a per-pod time series for one (metric, component group)."""
    start = float(metric["metadata"]["start_epoch"])
    pod_points = extract_pod_timeseries(metric[metric_key][group], start)
    pods = sorted(pod_points.keys())

    if not pods:
        with open(out_path, "w") as f:
            f.write(f"time_offset_s\t(no pods in group '{group}')\n")
        return

    # Align all pods to the union of timestamps so each row is a sample at one t.
    timestamps = sorted({t for pts in pod_points.values() for t, _ in pts})
    pod_lookup = {p: dict(pts) for p, pts in pod_points.items()}

    # No '#' prefix on the header so gnuplot's columnheader() picks up pod names.
    with open(out_path, "w") as f:
        f.write("time_offset_s\t" + "\t".join(pods) + "\n")
        for t in timestamps:
            row = [f"{t:.0f}"]
            for p in pods:
                v = pod_lookup[p].get(t)
                row.append(f"{v:.6f}" if v is not None else "N/A")
            f.write("\t".join(row) + "\n")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>", file=sys.stderr)
        sys.exit(1)

    res_dir = sys.argv[1]
    files = sorted(glob.glob(os.path.join(res_dir, "metrics_*.json")))
    if not files:
        print(f"WARNING: No metrics_*.json files found in {res_dir}", file=sys.stderr)
        sys.exit(0)

    metrics = []
    for path in files:
        with open(path) as f:
            metrics.append(json.load(f))
    metrics.sort(key=lambda m: float(m["metadata"]["rps"]))

    cpu_path = os.path.join(res_dir, "cpu.dat")
    mem_path = os.path.join(res_dir, "memory.dat")
    generate_avg_dat(metrics, "cpu", cpu_path)
    generate_avg_dat(metrics, "memory", mem_path)
    print(f"Generated {cpu_path} and {mem_path}")

    # Time series only makes sense for a single run; if multiple, take the first.
    primary = metrics[0]
    for group in TIMESERIES_GROUPS:
        for metric_key, label in (("cpu", "cpu"), ("memory", "memory")):
            ts_path = os.path.join(res_dir, f"{label}_{group}_timeseries.dat")
            generate_timeseries_dat(primary, metric_key, group, ts_path)
            print(f"Generated {ts_path}")


if __name__ == "__main__":
    main()
