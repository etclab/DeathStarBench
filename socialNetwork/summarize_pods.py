#!/usr/bin/env python3
"""Summarize per-second pod counts from benchmark pods-<rps>.csv files.

For each pods-<rps>.csv under <run-dir>/<strategy>/, emit pods-<rps>-sum.csv
with one row per timestamp counting Running+Ready pods per (app,version):

    index,timestamp,details-v1,productpage-v1,ratings-v1,reviews-v1,reviews-v2,reviews-v3,total

Usage: ./summarize_pods.py <benchmark-run-dir>
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

COLUMNS = [
    ("details", "v1"),
    ("productpage", "v1"),
    ("ratings", "v1"),
    ("reviews", "v1"),
    ("reviews", "v2"),
    ("reviews", "v3"),
]
HEADER = ["index", "timestamp"] + [f"{a}-{v}" for a, v in COLUMNS] + ["total"]


def summarize(src: Path, dst: Path) -> None:
    counts: dict[int, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
    unknown: set[tuple[str, str]] = set()

    with src.open(newline="") as f:
        for row in csv.reader(f):
            if len(row) != 5:
                continue
            ts_s, app, version, status, ready = row
            try:
                ts = int(ts_s)
            except ValueError:
                continue
            counts[ts]
            if status != "Running" or ready != "True":
                continue
            key = (app, version)
            if key not in COLUMNS:
                unknown.add(key)
                continue
            counts[ts][key] += 1

    if unknown:
        print(f"  warn: {src.name} has unknown (app,version) pairs ignored: {sorted(unknown)}",
              file=sys.stderr)

    with dst.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for idx, ts in enumerate(sorted(counts)):
            per = counts[ts]
            vals = [per[k] for k in COLUMNS]
            w.writerow([idx, ts] + vals + [sum(vals)])


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    sources = sorted(run_dir.glob("*/pods-*.csv"))
    sources = [p for p in sources if not p.name.endswith("-sum.csv")]
    if not sources:
        print(f"error: no pods-*.csv files found under {run_dir}/*/", file=sys.stderr)
        return 1

    for src in sources:
        dst = src.with_name(src.stem + "-sum.csv")
        summarize(src, dst)
        print(f"wrote {dst.relative_to(run_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
