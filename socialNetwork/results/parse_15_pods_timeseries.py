#!/usr/bin/env python3
"""Build per-second ready-pod timeseries across the whole sweep.

For each <run-dir>/<strategy>/pods.csv (the 1 Hz spanning poller output),
count Running+Ready pods per timestamp (the pods actually serving traffic)
and emit:

    <run-dir>/<strategy>/pods_timeseries.dat   columns:  t_sec  ready_pods

Step boundaries (RPS transitions) are derived from each per-RPS slice
(pods-<rps>.csv) using its first timestamp, emitted as:

    <run-dir>/<strategy>/rps_steps.dat         columns:  t_sec  rps

Both files use seconds relative to the first sample in pods.csv.

Usage: ./parse_15_pods_timeseries.py <benchmark-run-dir>
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

STRATEGIES = ["istio", "st5-AttUpd"]


def build_timeseries(pods_csv: Path) -> list[tuple[int, int]]:
    counts: dict[int, int] = defaultdict(int)
    seen_ts: set[int] = set()
    with pods_csv.open(newline="") as f:
        for row in csv.reader(f):
            if len(row) != 5:
                continue
            ts_s, _app, _ver, status, ready = row
            try:
                ts = int(ts_s)
            except ValueError:
                continue
            seen_ts.add(ts)
            if status == "Running" and ready == "True":
                counts[ts] += 1
    if not seen_ts:
        return []
    t0 = min(seen_ts)
    return [(ts - t0, counts.get(ts, 0)) for ts in sorted(seen_ts)]


def step_boundaries(strat_dir: Path, t0: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for p in sorted(strat_dir.glob("pods-*.csv")):
        if p.name.endswith("-sum.csv"):
            continue
        m = re.match(r"pods-(\d+)\.csv$", p.name)
        if not m:
            continue
        rps = int(m.group(1))
        with p.open(newline="") as f:
            r = csv.reader(f)
            next(r, None)  # header
            for row in r:
                if len(row) >= 1:
                    try:
                        out.append((int(row[0]) - t0, rps))
                        break
                    except ValueError:
                        continue
    out.sort(key=lambda x: x[0])
    return out


def pods_csv_t0(pods_csv: Path) -> int:
    with pods_csv.open(newline="") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) == 5:
                try:
                    return int(row[0])
                except ValueError:
                    continue
    return 0


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
        pods_csv = strat_dir / "pods.csv"
        if not pods_csv.is_file():
            print(f"  warn: {pods_csv} missing, skipping", file=sys.stderr)
            continue

        ts = build_timeseries(pods_csv)
        out = strat_dir / "pods_timeseries.dat"
        with out.open("w") as f:
            f.write(f"# Ready pods (Running+Ready, actually serving) over sweep for {strat}\n")
            f.write(f"# {'t_sec':<10}{'total':>8}\n")
            for t, n in ts:
                f.write(f"  {t:<10}{n:>8d}\n")
        print(f"wrote {out.relative_to(run_dir)} ({len(ts)} samples)")

        t0 = pods_csv_t0(pods_csv)
        steps = step_boundaries(strat_dir, t0)
        out_steps = strat_dir / "rps_steps.dat"
        with out_steps.open("w") as f:
            f.write(f"# RPS step boundaries (t = window start) for {strat}\n")
            f.write(f"# {'t_sec':<10}{'rps':>8}\n")
            for t, rps in steps:
                f.write(f"  {t:<10}{rps:>8d}\n")
        print(f"wrote {out_steps.relative_to(run_dir)} ({len(steps)} steps)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
