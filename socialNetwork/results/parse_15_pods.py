#!/usr/bin/env python3
"""Aggregate per-RPS total-ready-pod counts into a .dat file per strategy.

For each <run-dir>/<strategy>/pods-<rps>-sum.csv, compute the average and
maximum of the `total` column across the run window, and emit:

    <run-dir>/<strategy>/pods_active.dat

with columns:  QPS  avg_pods  max_pods

Usage: ./parse_15_pods.py <benchmark-run-dir>
"""

import csv
import re
import sys
from pathlib import Path

STRATEGIES = ["istio", "st5-AttUpd"]


def aggregate(strat_dir: Path) -> list[tuple[int, float, int]]:
    rows: list[tuple[int, float, int]] = []
    for p in sorted(strat_dir.glob("pods-*-sum.csv")):
        m = re.match(r"pods-(\d+)-sum\.csv$", p.name)
        if not m:
            continue
        rps = int(m.group(1))
        totals: list[int] = []
        with p.open(newline="") as f:
            for row in csv.DictReader(f):
                totals.append(int(row["total"]))
        if not totals:
            continue
        avg = sum(totals) / len(totals)
        rows.append((rps, avg, max(totals)))
    rows.sort(key=lambda r: r[0])
    return rows


def write_dat(strat: str, rows: list[tuple[int, float, int]], out: Path) -> None:
    with out.open("w") as f:
        f.write(f"# Total active (Running+Ready) pods for {strat}\n")
        f.write(f"# {'QPS':<10}{'avg':>12}{'max':>12}\n")
        for rps, avg, mx in rows:
            f.write(f"  {rps:<10}{avg:>12.2f}{mx:>12d}\n")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    for strat in STRATEGIES:
        strat_dir = run_dir / strat
        if not strat_dir.is_dir():
            print(f"  warn: {strat_dir} missing, skipping", file=sys.stderr)
            continue
        rows = aggregate(strat_dir)
        if not rows:
            print(f"  warn: no pods-*-sum.csv under {strat_dir}", file=sys.stderr)
            continue
        out = strat_dir / "pods_active.dat"
        write_dat(strat, rows, out)
        print(f"wrote {out.relative_to(run_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
