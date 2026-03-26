#!/usr/bin/env python3
"""Parse benchmark 1.5 data and generate gnuplot .dat files.

Parses wrk2 output for latency and calls generate_dat.py for CPU/memory.

Usage:
    python3 parse_15_data.py <benchmark_dir>

Example:
    cd socialNetwork/results
    python3 parse_15_data.py benchmark1.5-03-25-26_134007
"""

import argparse
import re
import sys
from pathlib import Path

STRATEGIES = ['istio', 'st5-AttUpd']
STRATEGY_LABELS = {'istio': 'Istio', 'st5-AttUpd': 'Mazu'}

# Percentiles to extract from the wrk2 summary section
PERCENTILES = {
    'p50': '50.000%',
    'p90': '90.000%',
    'p99': '99.000%',
}


def parse_wrk2_latency(filepath):
    """Extract p50, p90, p99 latencies (in ms) from a wrk2 output file."""
    text = filepath.read_text()
    results = {}
    for label, pct_str in PERCENTILES.items():
        # Match lines like: " 50.000%   45.60ms"
        pattern = re.escape(pct_str) + r'\s+([\d.]+)([a-z]+)'
        match = re.search(pattern, text)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            # Convert to ms
            if unit == 's':
                value *= 1000
            elif unit == 'us':
                value /= 1000
            results[label] = value
        else:
            results[label] = None
    return results


def is_valid_result_file(filepath):
    """Check if file is a valid wrk2 result file."""
    name = filepath.name
    if '.failed_' in name or name == 'run.log':
        return False
    if not name.endswith('.txt'):
        return False
    return True


def parse_dat_file(filepath):
    """Parse a tab-separated .dat file (cpu.dat or memory.dat from generate_dat.py).

    Returns (components, rps_data) where:
        components: list of column names (excluding RPS)
        rps_data: dict of {rps: [values...]}
    """
    components = []
    rps_data = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line.startswith('# RPS'):
                # Header line
                parts = line.split('\t')
                components = parts[1:]  # skip "# RPS"
            elif line and not line.startswith('#'):
                parts = line.split('\t')
                rps = int(parts[0])
                values = [float(v) if v != 'N/A' else 0.0 for v in parts[1:]]
                rps_data[rps] = values
    return components, rps_data


def write_combined_resource_dat(bench_dir, metric):
    """Read {metric}.dat from each strategy and write a single combined file.

    Uses gnuplot index blocks (separated by double blank lines), one per RPS:
        # RPS = 20
        Service              Istio              Mazu
        details-v1           0.044              0.056
        ...

        <blank>
        <blank>
        # RPS = 40
        ...
    """
    strategy_data = {}
    components = None
    all_rps = set()

    for strategy in STRATEGIES:
        dat_path = bench_dir / strategy / f'{metric}.dat'
        if not dat_path.exists():
            print(f"Warning: {dat_path} not found, skipping...")
            continue
        comps, rps_data = parse_dat_file(dat_path)
        strategy_data[strategy] = rps_data
        if components is None:
            components = comps
        all_rps.update(rps_data.keys())

    if not components or not strategy_data:
        return

    sorted_rps = sorted(all_rps)
    out_path = bench_dir / f'{metric}_combined.dat'
    with open(out_path, 'w') as f:
        labels = [STRATEGY_LABELS.get(s, s) for s in STRATEGIES]
        for block_idx, rps in enumerate(sorted_rps):
            if block_idx > 0:
                f.write("\n\n")  # double blank line = new gnuplot index
            f.write(f"# RPS = {rps}\n")
            f.write(f"{'Service':<22} {labels[0]:<18} {labels[1]:<18}\n")
            for i, comp in enumerate(components):
                vals = []
                for strategy in STRATEGIES:
                    rps_data = strategy_data.get(strategy, {})
                    row = rps_data.get(rps, [])
                    val = row[i] if i < len(row) else 0.0
                    if metric == 'memory':
                        val = val / (1024 * 1024)  # bytes -> MB
                    vals.append(val)
                f.write(f"{comp:<22} {vals[0]:<18.6f} {vals[1]:<18.6f}\n")

    print(f"Wrote {out_path} ({len(sorted_rps)} blocks: {sorted_rps})")


def main():
    parser = argparse.ArgumentParser(description="Parse benchmark 1.5 data into gnuplot .dat files")
    parser.add_argument("benchmark_dir", help="Path to benchmark results directory")
    args = parser.parse_args()

    bench_dir = Path(args.benchmark_dir)
    if not bench_dir.is_dir():
        print(f"Error: {bench_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Generate latency.dat per strategy
    for strategy in STRATEGIES:
        strategy_dir = bench_dir / strategy
        if not strategy_dir.is_dir():
            print(f"Warning: {strategy_dir} not found, skipping...")
            continue

        rows = []
        for result_file in strategy_dir.iterdir():
            if not is_valid_result_file(result_file):
                continue
            qps = int(result_file.stem)
            latencies = parse_wrk2_latency(result_file)
            if all(v is not None for v in latencies.values()):
                rows.append((qps, latencies['p50'], latencies['p90'], latencies['p99']))
            else:
                print(f"Warning: could not parse all percentiles from {result_file}")

        rows.sort(key=lambda r: r[0])

        dat_path = strategy_dir / 'latency.dat'
        with open(dat_path, 'w') as f:
            f.write(f"# End-to-end latency for {STRATEGY_LABELS.get(strategy, strategy)}\n")
            f.write(f"# {'QPS':<10} {'p50(ms)':<12} {'p90(ms)':<12} {'p99(ms)':<12}\n")
            for qps, p50, p90, p99 in rows:
                f.write(f"  {qps:<10} {p50:<12.2f} {p90:<12.2f} {p99:<12.2f}\n")

        print(f"Wrote {dat_path} ({len(rows)} entries)")

    # Generate combined CPU and memory dat files
    write_combined_resource_dat(bench_dir, 'cpu')
    write_combined_resource_dat(bench_dir, 'memory')


if __name__ == "__main__":
    main()
