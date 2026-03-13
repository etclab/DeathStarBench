#!/usr/bin/env python3
"""
Generate gnuplot .dat files from inline benchmark metrics JSON files.

Usage:
    python3 generate_inline_dat.py <results_dir>

Reads all inline_metrics_*.json files in <results_dir> and produces:
    <results_dir>/op_latency.dat    - per-operation percentiles (one row per op)
    <results_dir>/total_latency.dat - total ext_authz path percentiles
"""

import json
import glob
import os
import sys


# Known benchmark operations in display order
OPS = ["kc_fetch", "counter_attestation", "rbe_proof", "challenge_response", "token_review"]
QUANTILE_NAMES = ["p50", "p90", "p95", "p99"]


def extract_scalar(prom_response):
    """Extract a scalar value from a Prometheus instant query response.

    Returns the value string, or 'NaN' if no result.
    """
    if isinstance(prom_response, dict):
        results = prom_response.get("data", {}).get("result", [])
        if results:
            return results[0]["value"][1]
    return "NaN"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>", file=sys.stderr)
        sys.exit(1)

    res_dir = sys.argv[1]
    files = sorted(glob.glob(os.path.join(res_dir, "inline_metrics_*.json")))

    if not files:
        print(f"WARNING: No inline_metrics_*.json files found in {res_dir}", file=sys.stderr)
        sys.exit(0)

    # Load all metrics files (use the last one if multiple RPS levels)
    metrics_list = []
    for path in files:
        with open(path) as f:
            metrics_list.append(json.load(f))

    # Use the last collected metrics (or the single one for benchmark 2)
    metrics = metrics_list[-1]

    # --- op_latency.dat: one row per operation ---
    op_path = os.path.join(res_dir, "op_latency.dat")
    with open(op_path, "w") as f:
        header = ["# operation"] + QUANTILE_NAMES
        f.write("\t".join(header) + "\n")

        for op in OPS:
            op_data = metrics.get("op_latency", {}).get(op, {})
            row = [op]
            for qname in QUANTILE_NAMES:
                val = extract_scalar(op_data.get(qname, {}))
                row.append(val)
            f.write("\t".join(row) + "\n")

    # --- total_latency.dat: single row with percentiles ---
    total_path = os.path.join(res_dir, "total_latency.dat")
    with open(total_path, "w") as f:
        header = ["# metric"] + QUANTILE_NAMES
        f.write("\t".join(header) + "\n")

        total_data = metrics.get("total_latency", {})
        row = ["total_ext_authz"]
        for qname in QUANTILE_NAMES:
            val = extract_scalar(total_data.get(qname, {}))
            row.append(val)
        f.write("\t".join(row) + "\n")

    print(f"Generated {op_path} and {total_path}")


if __name__ == "__main__":
    main()
