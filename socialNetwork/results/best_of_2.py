#!/usr/bin/env python3
"""Merge two benchmark1.5 runs into a best-of-2 result folder.

For each strategy (istio, st5-AttUpd) and each QPS level, picks the better
row from the two source latency.dat files using validity rules:
  - intra_valid:  p50 < p90 < p99
  - mono_valid:   each percentile >= the previously chosen row's percentile
A row is "valid" iff intra_valid AND mono_valid. Selection per QPS:
  - both valid     -> smaller p50 (tiebreak p90, p99)
  - one valid      -> that one
  - neither valid  -> drop monotonicity, re-evaluate with intra_valid only;
                      if still neither, skip the QPS row.

Then copies plot_15_e2e_latency.gpi and style.gpi from run1 and runs gnuplot.

  python3 results/best_of_2.py \
      --output-dir results/best_of_2_benchmark1.5 \
      --benchmark-type no_scale \
      results/<run1> results/<run2>
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

STRATEGIES = ['istio', 'st5-AttUpd']
STRATEGY_LABELS = {'istio': 'Istio', 'st5-AttUpd': 'Mazu'}


def parse_latency_dat(path):
    """Return {qps: (p50, p90, p99)} from a latency.dat file."""
    rows = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            qps = int(parts[0])
            rows[qps] = (float(parts[1]), float(parts[2]), float(parts[3]))
    return rows


def intra_valid(row):
    p50, p90, p99 = row
    return p50 < p90 < p99


def mono_valid(row, prev):
    if prev is None:
        return True
    return all(c >= p for c, p in zip(row, prev))


def pick_smaller(a, b):
    """Pick the row with smaller p50, tiebreak p90 then p99."""
    return a if a <= b else b


def choose_row(qps, row1, row2, prev):
    """Pick the best row for this QPS or return None to skip."""
    candidates = [r for r in (row1, row2) if r is not None]
    if not candidates:
        return None

    valid = [r for r in candidates if intra_valid(r) and mono_valid(r, prev)]
    if len(valid) == 2:
        return pick_smaller(valid[0], valid[1])
    if len(valid) == 1:
        return valid[0]

    # Fallback: drop monotonicity, require only intra_valid.
    fallback = [r for r in candidates if intra_valid(r)]
    if len(fallback) == 2:
        return pick_smaller(fallback[0], fallback[1])
    if len(fallback) == 1:
        return fallback[0]

    return None


def merge_strategy(run1_dir, run2_dir, out_dir, strategy):
    src1 = run1_dir / strategy / 'latency.dat'
    src2 = run2_dir / strategy / 'latency.dat'
    if not src1.exists() or not src2.exists():
        print(f"Warning: missing {strategy}/latency.dat in one of the runs, skipping",
              file=sys.stderr)
        return

    rows1 = parse_latency_dat(src1)
    rows2 = parse_latency_dat(src2)
    all_qps = sorted(set(rows1) | set(rows2))

    chosen = []
    prev = None
    for qps in all_qps:
        picked = choose_row(qps, rows1.get(qps), rows2.get(qps), prev)
        if picked is None:
            print(f"  {strategy} qps={qps}: skipped (no valid row)")
            continue
        chosen.append((qps, picked))
        prev = picked

    out_strategy_dir = out_dir / strategy
    out_strategy_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_strategy_dir / 'latency.dat'
    label = STRATEGY_LABELS.get(strategy, strategy)
    with open(out_path, 'w') as f:
        f.write(f"# End-to-end latency for {label}\n")
        f.write(f"# {'QPS':<10} {'p50(ms)':<12} {'p90(ms)':<12} {'p99(ms)':<12}\n")
        for qps, (p50, p90, p99) in chosen:
            f.write(f"  {qps:<10} {p50:<12.2f} {p90:<12.2f} {p99:<12.2f}\n")
    print(f"Wrote {out_path} ({len(chosen)} rows)")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output-dir', required=True,
                        help="Best-of-2 output root, e.g. results/best_of_2_benchmark1.5")
    parser.add_argument('--benchmark-type', required=True,
                        help="Subfolder under output-dir, e.g. no_scale or scale")
    parser.add_argument('run1', help="First benchmark run directory")
    parser.add_argument('run2', help="Second benchmark run directory")
    args = parser.parse_args()

    run1 = Path(args.run1)
    run2 = Path(args.run2)
    out_dir = Path(args.output_dir) / args.benchmark_type

    for d in (run1, run2):
        if not d.is_dir():
            print(f"Error: {d} is not a directory", file=sys.stderr)
            sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    for strategy in STRATEGIES:
        merge_strategy(run1, run2, out_dir, strategy)

    for gpi in ('plot_15_e2e_latency.gpi', 'style.gpi'):
        src = run1 / gpi
        if not src.exists():
            print(f"Error: {src} not found in run1", file=sys.stderr)
            sys.exit(1)
        shutil.copy2(src, out_dir / gpi)
        print(f"Copied {src.name}")

    print(f"Running gnuplot in {out_dir}")
    subprocess.run(['gnuplot', 'plot_15_e2e_latency.gpi'],
                   cwd=out_dir, check=True)
    print(f"Wrote {out_dir / 'plot_15_e2e_latency.pdf'}")


if __name__ == '__main__':
    main()
