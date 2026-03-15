#!/usr/bin/env python3
"""
Consolidate per-strategy .dat files into plot-ready .dat files for gnuplot.

Reads cpu.dat, memory.dat, total_latency.dat, and op_latency.dat from each
strategy subfolder and produces:
  - plot_cpu.dat          (component x strategy CPU comparison)
  - plot_memory.dat       (component x strategy memory comparison, in MB)
  - plot_e2e_latency.dat  (percentile x strategy e2e latency comparison)
  - plot_latency_breakdown.dat  (percentile x ops stacked, for mazu strategy)

Usage:
    python3 generate_plot_data.py <results_dir> [strategy1 strategy2 ...]

If strategies are not specified, auto-detects subdirectories containing .dat files.
"""

import sys
import os
import csv

PERCENTILES = ["p50", "p90", "p95", "p99"]
PERCENTILE_COLS = {"p50": 0, "p90": 1, "p95": 2, "p99": 3}

# Operations in the order we want them stacked
OPS = ["kc_fetch", "counter_attestation", "rbe_proof", "challenge_response", "token_review"]
# Short names for the .dat header
OPS_SHORT = ["kc_fetch", "counter_att", "rbe_proof", "challenge_resp", "token_review"]
SIDES = ["fortio-client", "fortio-server"]


def read_dat(filepath):
    """Read a tab-separated .dat file, return header and rows."""
    rows = []
    header = None
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                # parse header columns
                header = line.lstrip("# ").split("\t")
                continue
            rows.append(line.split("\t"))
    return header, rows


def find_strategies(results_dir):
    """Auto-detect strategy subdirectories."""
    strats = []
    for name in sorted(os.listdir(results_dir)):
        subdir = os.path.join(results_dir, name)
        if os.path.isdir(subdir) and os.path.exists(os.path.join(subdir, "cpu.dat")):
            strats.append(name)
    return strats


def find_mazu_strategy(strategies):
    """Find the non-istio strategy (mazu)."""
    for s in strategies:
        if s != "istio":
            return s
    return None


def generate_cpu_dat(results_dir, strategies, rps):
    """Generate plot_cpu.dat: component comparison across strategies."""
    # Read each strategy's cpu.dat
    strat_data = {}
    all_components = []
    for strat in strategies:
        path = os.path.join(results_dir, strat, "cpu.dat")
        if not os.path.exists(path):
            print(f"  WARNING: {path} not found, skipping")
            continue
        header, rows = read_dat(path)
        # header: RPS, comp1, comp2, ...
        components = header[1:]
        # Find the row matching our RPS (or take first row)
        row = rows[0]  # benchmark2 typically has one RPS
        vals = {}
        for i, comp in enumerate(components):
            vals[comp] = row[i + 1]
            if comp not in all_components:
                all_components.append(comp)
        strat_data[strat] = vals

    outpath = os.path.join(results_dir, "plot_cpu.dat")
    with open(outpath, "w") as f:
        f.write("Component\t" + "\t".join(strategies) + "\n")
        for comp in all_components:
            vals = []
            for strat in strategies:
                vals.append(strat_data.get(strat, {}).get(comp, "NaN"))
            f.write(comp + "\t" + "\t".join(vals) + "\n")
    print(f"  Generated {outpath}")


def generate_memory_dat(results_dir, strategies, rps):
    """Generate plot_memory.dat: component comparison in MB."""
    strat_data = {}
    all_components = []
    for strat in strategies:
        path = os.path.join(results_dir, strat, "memory.dat")
        if not os.path.exists(path):
            print(f"  WARNING: {path} not found, skipping")
            continue
        header, rows = read_dat(path)
        components = header[1:]
        row = rows[0]
        vals = {}
        for i, comp in enumerate(components):
            # Convert bytes to MB
            try:
                val_mb = float(row[i + 1]) / (1024 * 1024)
                vals[comp] = f"{val_mb:.2f}"
            except (ValueError, IndexError):
                vals[comp] = "NaN"
            if comp not in all_components:
                all_components.append(comp)
        strat_data[strat] = vals

    outpath = os.path.join(results_dir, "plot_memory.dat")
    with open(outpath, "w") as f:
        f.write("Component\t" + "\t".join(strategies) + "\n")
        for comp in all_components:
            vals = []
            for strat in strategies:
                vals.append(strat_data.get(strat, {}).get(comp, "NaN"))
            f.write(comp + "\t" + "\t".join(vals) + "\n")
    print(f"  Generated {outpath}")


def generate_e2e_latency_dat(results_dir, strategies):
    """Generate plot_e2e_latency.dat: percentile x strategy for fortio_e2e."""
    strat_data = {}
    for strat in strategies:
        path = os.path.join(results_dir, strat, "total_latency.dat")
        if not os.path.exists(path):
            print(f"  WARNING: {path} not found, skipping")
            continue
        header, rows = read_dat(path)
        # Find the fortio_e2e row
        for row in rows:
            if row[0] == "fortio_e2e":
                # columns: metric, p50, p90, p95, p99
                strat_data[strat] = {
                    "p50": row[1], "p90": row[2], "p95": row[3], "p99": row[4]
                }
                break

    outpath = os.path.join(results_dir, "plot_e2e_latency.dat")
    with open(outpath, "w") as f:
        f.write("Percentile\t" + "\t".join(strategies) + "\n")
        for p in PERCENTILES:
            vals = []
            for strat in strategies:
                vals.append(strat_data.get(strat, {}).get(p, "NaN"))
            f.write(p + "\t" + "\t".join(vals) + "\n")
    print(f"  Generated {outpath}")


def generate_latency_breakdown_dat(results_dir, mazu_strat):
    """Generate plot_latency_breakdown.dat for the mazu strategy.

    Format: one row per percentile, columns are:
    Label  kc_fetch(c)  counter_att(c)  rbe_proof(c)  challenge_resp(c)  token_review(c)
           kc_fetch(s)  counter_att(s)  rbe_proof(s)  challenge_resp(s)  token_review(s)  e2e
    """
    strat_dir = os.path.join(results_dir, mazu_strat)
    op_path = os.path.join(strat_dir, "op_latency.dat")
    total_path = os.path.join(strat_dir, "total_latency.dat")

    if not os.path.exists(op_path):
        print(f"  WARNING: {op_path} not found, skipping breakdown")
        return
    if not os.path.exists(total_path):
        print(f"  WARNING: {total_path} not found, skipping breakdown")
        return

    # Parse op_latency.dat: rows like "kc_fetch/fortio-client  p50  p90  p95  p99"
    _, op_rows = read_dat(op_path)
    # Build lookup: (op_name, side) -> {p50, p90, p95, p99}
    op_data = {}
    for row in op_rows:
        # row[0] is like "kc_fetch/fortio-client"
        parts = row[0].split("/")
        op_name = parts[0]
        side = parts[1] if len(parts) > 1 else "unknown"
        op_data[(op_name, side)] = {
            "p50": row[1], "p90": row[2], "p95": row[3], "p99": row[4]
        }

    # Parse total_latency.dat for fortio_e2e
    _, total_rows = read_dat(total_path)
    e2e = {}
    for row in total_rows:
        if row[0] == "fortio_e2e":
            e2e = {"p50": row[1], "p90": row[2], "p95": row[3], "p99": row[4]}
            break

    # Build header
    client_cols = [f"{s}" for s in OPS_SHORT]
    server_cols = [f"{s}" for s in OPS_SHORT]
    header = "Label\t" + "\t".join(client_cols) + "\t" + "\t".join(server_cols) + "\te2e"

    outpath = os.path.join(results_dir, "plot_latency_breakdown.dat")
    with open(outpath, "w") as f:
        f.write(header + "\n")
        for p in PERCENTILES:
            vals = [p]
            # Client ops
            for op in OPS:
                v = op_data.get((op, "fortio-client"), {}).get(p, "NaN")
                try:
                    vals.append(f"{float(v):.2f}")
                except ValueError:
                    vals.append("NaN")
            # Server ops
            for op in OPS:
                v = op_data.get((op, "fortio-server"), {}).get(p, "NaN")
                try:
                    vals.append(f"{float(v):.2f}")
                except ValueError:
                    vals.append("NaN")
            # e2e
            e2e_val = e2e.get(p, "NaN")
            try:
                vals.append(f"{float(e2e_val):.2f}")
            except ValueError:
                vals.append("NaN")
            f.write("\t".join(vals) + "\n")
    print(f"  Generated {outpath}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir> [strategy1 strategy2 ...]")
        sys.exit(1)

    results_dir = sys.argv[1]
    if not os.path.isdir(results_dir):
        print(f"ERROR: {results_dir} is not a directory")
        sys.exit(1)

    if len(sys.argv) > 2:
        strategies = sys.argv[2:]
    else:
        strategies = find_strategies(results_dir)

    if not strategies:
        print(f"ERROR: No strategy directories found in {results_dir}")
        sys.exit(1)

    print(f"Generating plot data for strategies: {strategies}")

    # Read RPS from any cpu.dat to get the RPS value
    rps = None
    for strat in strategies:
        cpu_path = os.path.join(results_dir, strat, "cpu.dat")
        if os.path.exists(cpu_path):
            _, rows = read_dat(cpu_path)
            if rows:
                rps = rows[0][0]
            break

    generate_cpu_dat(results_dir, strategies, rps)
    generate_memory_dat(results_dir, strategies, rps)
    generate_e2e_latency_dat(results_dir, strategies)

    mazu_strat = find_mazu_strategy(strategies)
    if mazu_strat:
        generate_latency_breakdown_dat(results_dir, mazu_strat)
    else:
        print("  No mazu strategy found, skipping latency breakdown")

    print("Plot data generation complete.")


if __name__ == "__main__":
    main()
