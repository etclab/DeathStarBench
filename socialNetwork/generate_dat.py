#!/usr/bin/env python3
"""
Generate gnuplot .dat files from Prometheus metrics JSON files.

Usage:
    python3 generate_dat.py <results_dir>

Reads all metrics_*.json files in <results_dir> and produces:
    <results_dir>/cpu.dat    - avg CPU rate (cores) per pod, per RPS
    <results_dir>/memory.dat - avg memory (bytes) per pod, per RPS

Each .dat file is tab-separated with one row per RPS level and one column
per pod/component. The header line starts with '#' (gnuplot comment).
"""

import json
import glob
import os
import sys


def deploy_name(pod_name):
    """Strip the replicaset hash and pod hash from a pod name.

    e.g. 'details-v1-6c48dbdbbd-fvv76' -> 'details-v1'
    """
    parts = pod_name.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod_name


def extract_pods(prom_response):
    """Extract {deploy_name: value} from a Prometheus instant query response."""
    results = prom_response.get("data", {}).get("result", [])
    out = {}
    for r in results:
        pod = r["metric"].get("pod", "unknown")
        value = r["value"][1]  # [timestamp, "value_string"]
        name = deploy_name(pod)
        if name in out:
            # Append increasing suffix to disambiguate duplicate deploy names
            i = 2
            while f"{name}-{i}" in out:
                i += 1
            name = f"{name}-{i}"
        out[name] = value
    return out


# Components in the metrics JSON, in the order they appear as column groups.
COMPONENTS = ["bookinfo", "istiod", "kube_apiserver"]


def generate_dat(metrics, metric_key, out_path):
    """Write a gnuplot .dat file for a given metric (cpu or memory).

    Args:
        metrics: list of parsed metrics JSON dicts, sorted by RPS
        metric_key: 'cpu' or 'memory'
        out_path: path to write the .dat file
    """
    # Discover all pod names per component across all RPS levels
    component_pods = {}
    for comp in COMPONENTS:
        all_pods = set()
        for m in metrics:
            all_pods.update(extract_pods(m[metric_key][comp]).keys())
        component_pods[comp] = sorted(all_pods)
        

    # Build ordered column list: RPS, then each component's pods
    columns = ["# RPS"]
    for comp in COMPONENTS:
        columns.extend(component_pods[comp])
        
    with open(out_path, "w") as f:
        f.write("\t".join(columns) + "\n")

        for m in metrics:
            rps = str(m["metadata"]["rps"])
            row = [rps]
            for comp in COMPONENTS:
                pod_values = extract_pods(m[metric_key][comp])
                row.extend(pod_values.get(p, "N/A") for p in component_pods[comp])
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

    # Load and sort by RPS
    metrics = []
    for path in files:
        with open(path) as f:
            metrics.append(json.load(f))
    metrics.sort(key=lambda m: float(m["metadata"]["rps"]))

    cpu_path = os.path.join(res_dir, "cpu.dat")
    mem_path = os.path.join(res_dir, "memory.dat")

    generate_dat(metrics, "cpu", cpu_path)
    generate_dat(metrics, "memory", mem_path)

    print(f"Generated {cpu_path} and {mem_path}")


if __name__ == "__main__":
    main()
