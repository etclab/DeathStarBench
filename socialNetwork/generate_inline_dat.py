#!/usr/bin/env python3
"""
Generate gnuplot .dat files from inline benchmark metrics JSON files.

Usage:
    python3 generate_inline_dat.py <results_dir>

Reads all inline_metrics_*.json files in <results_dir> and produces:
    <results_dir>/op_latency.dat    - per-operation percentiles (one row per op per app)
    <results_dir>/total_latency.dat - total ext_authz path percentiles (one row per app)
                                      plus fortio e2e latency and client+server sum
"""

import json
import glob
import os
import sys


# Known benchmark operations in display order
OPS = ["kc_fetch", "counter_attestation", "rbe_proof", "challenge_response", "token_review"]
QUANTILE_NAMES = ["p50", "p90", "p95", "p99"]
APPS = ["fortio-client", "fortio-server"]
FORTIO_PERCENTILES = [50, 90, 95, 99]


def extract_scalar(prom_response, app):
    """Extract a scalar value from a Prometheus instant query response.

    Finds the result whose 'app' label matches the given app name.
    Returns the value string, or 'NaN' if no matching result.
    """
    if isinstance(prom_response, dict):
        results = prom_response.get("data", {}).get("result", [])
        for r in results:
            if r.get("metric", {}).get("app") == app:
                return r["value"][1]
    return "NaN"


def fortio_percentile(dh, target_pct):
    """Look up a precomputed percentile from fortio's DurationHistogram.

    Returns the value in milliseconds, or 'NaN' if not available.
    Requires fortio to be invoked with -p flags for the desired percentiles.
    """
    for p in dh.get("Percentiles", []):
        if p["Percentile"] == target_pct:
            return str(p["Value"] * 1000)  # seconds -> ms
    return "NaN"


def load_fortio_latencies(res_dir, rps):
    """Load fortio e2e latencies from fortio_<rps>.json.

    Returns a dict mapping quantile names to value strings (in ms), or None.
    """
    fortio_path = os.path.join(res_dir, f"fortio_{rps}.json")
    if not os.path.exists(fortio_path):
        return None
    with open(fortio_path) as f:
        fortio = json.load(f)
    dh = fortio.get("DurationHistogram", {})
    return {qname: fortio_percentile(dh, pct)
            for qname, pct in zip(QUANTILE_NAMES, FORTIO_PERCENTILES)}


def detect_rps(res_dir):
    """Detect RPS from fortio_<rps>.json filenames in the results directory."""
    fortio_files = sorted(glob.glob(os.path.join(res_dir, "fortio_*.json")))
    if not fortio_files:
        return None
    # Use the last fortio file; extract RPS from filename
    basename = os.path.splitext(os.path.basename(fortio_files[-1]))[0]
    return basename.split("_", 1)[1]  # "fortio_100" -> "100"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>", file=sys.stderr)
        sys.exit(1)

    res_dir = sys.argv[1]
    files = sorted(glob.glob(os.path.join(res_dir, "inline_metrics_*.json")))

    # Load inline metrics if available (not present for istio baseline)
    metrics = None
    if files:
        metrics_list = []
        for path in files:
            with open(path) as f:
                metrics_list.append(json.load(f))
        metrics = metrics_list[-1]

    rps = None
    if metrics:
        rps = metrics.get("metadata", {}).get("rps", None)
    if rps is None:
        rps = detect_rps(res_dir)
    if rps is None:
        print(f"WARNING: No inline_metrics or fortio files found in {res_dir}", file=sys.stderr)
        sys.exit(0)

    # --- op_latency.dat: one row per operation per app (skip for istio baseline) ---
    if metrics:
        op_path = os.path.join(res_dir, "op_latency.dat")
        with open(op_path, "w") as f:
            header = ["# operation"] + QUANTILE_NAMES
            f.write("\t".join(header) + "\n")

            op_latency = metrics.get("op_latency", {})
            for app in APPS:
                for op in OPS:
                    op_data = op_latency.get(op, {})
                    row = [f"{op}/{app}"]
                    for qname in QUANTILE_NAMES:
                        val = extract_scalar(op_data.get(qname, {}), app)
                        row.append(val)
                    f.write("\t".join(row) + "\n")
        print(f"Generated {op_path}")

    # --- total_latency.dat: fortio e2e + per-app inline + sum ---
    total_path = os.path.join(res_dir, "total_latency.dat")
    with open(total_path, "w") as f:
        header = ["# metric"] + QUANTILE_NAMES
        f.write("\t".join(header) + "\n")

        # Fortio e2e latency (available for all strategies)
        fortio_lat = load_fortio_latencies(res_dir, rps)
        if fortio_lat:
            row = ["fortio_e2e"] + [fortio_lat[q] for q in QUANTILE_NAMES]
            f.write("\t".join(row) + "\n")

        # Per-app inline latencies and sum (only for Mazu strategies)
        if metrics:
            total_latency = metrics.get("total_latency", {})
            app_vals = {}
            for app in APPS:
                vals = {}
                row = [f"total_inline/{app}"]
                for qname in QUANTILE_NAMES:
                    val = extract_scalar(total_latency.get(qname, {}), app)
                    vals[qname] = val
                    row.append(val)
                app_vals[app] = vals
                f.write("\t".join(row) + "\n")

            # Sum of client + server inline latencies
            row = ["total_inline/sum"]
            for qname in QUANTILE_NAMES:
                try:
                    total = sum(float(app_vals[app][qname]) for app in APPS)
                    row.append(str(total))
                except (ValueError, KeyError):
                    row.append("NaN")
            f.write("\t".join(row) + "\n")

    print(f"Generated {total_path}")


if __name__ == "__main__":
    main()
