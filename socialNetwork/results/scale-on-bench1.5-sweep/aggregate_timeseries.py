#!/usr/bin/env python3
"""Aggregate per-second ready-pod timeseries across the 10 sweep runs.

For each run's <strategy>/pods.csv, count Running+Ready pods per timestamp,
align to t=0 at each run's first sample, then compute the mean and stddev
across runs at each second.

Output: <sweep-dir>/pods_timeseries_summary_<strategy>.dat
        columns:  t_sec  mean_ready  stddev_ready  n_runs

Also writes rps_steps_summary.dat by averaging the istio step boundaries
across runs (each step's start time relative to t0).
"""

import csv
import math
import re
from collections import defaultdict
from pathlib import Path

SWEEP_DIR = Path(__file__).resolve().parent
STRATEGIES = ["istio", "st5-AttUpd"]


def build_run_timeseries(pods_csv: Path) -> dict[int, int]:
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
        return {}
    t0 = min(seen_ts)
    return {ts - t0: counts.get(ts, 0) for ts in seen_ts}


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
            next(r, None)
            for row in r:
                if len(row) >= 1:
                    try:
                        out.append((int(row[0]) - t0, rps))
                        break
                    except ValueError:
                        continue
    out.sort(key=lambda x: x[0])
    return out


def summarize_strategy(run_dirs: list[Path], strat: str) -> list[tuple[int, float, float, int]]:
    per_run_series: list[dict[int, int]] = []
    for run_dir in run_dirs:
        pods_csv = run_dir / strat / "pods.csv"
        if not pods_csv.is_file():
            print(f"  warn: {pods_csv} missing, skipping")
            continue
        per_run_series.append(build_run_timeseries(pods_csv))

    if not per_run_series:
        return []

    # Intersect to common time range so stats represent the same N at every t.
    t_min = max(min(s.keys()) for s in per_run_series)
    t_max = min(max(s.keys()) for s in per_run_series)

    rows: list[tuple[int, float, float, int]] = []
    for t in range(t_min, t_max + 1):
        vals = [s[t] for s in per_run_series if t in s]
        if not vals:
            continue
        n = len(vals)
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n if n > 0 else 0.0
        std = math.sqrt(var)
        rows.append((t, mean, std, n))
    return rows


def summarize_steps(run_dirs: list[Path], strat: str = "istio") -> list[tuple[float, int]]:
    by_rps: dict[int, list[int]] = defaultdict(list)
    for run_dir in run_dirs:
        strat_dir = run_dir / strat
        pods_csv = strat_dir / "pods.csv"
        if not pods_csv.is_file():
            continue
        t0 = pods_csv_t0(pods_csv)
        for t, rps in step_boundaries(strat_dir, t0):
            by_rps[rps].append(t)
    out: list[tuple[float, int]] = []
    for rps, ts in by_rps.items():
        out.append((sum(ts) / len(ts), rps))
    out.sort(key=lambda x: x[0])
    return out


def write_summary(rows: list[tuple[int, float, float, int]], out: Path, strat: str) -> None:
    with out.open("w") as f:
        f.write(f"# Total Running+Ready pods over sweep, aggregated across 10 runs ({strat})\n")
        f.write(f"# {'t_sec':<10}{'mean':>12}{'stddev':>12}{'n':>6}\n")
        for t, mean, std, n in rows:
            f.write(f"  {t:<10}{mean:>12.3f}{std:>12.3f}{n:>6d}\n")


def main() -> int:
    run_dirs = sorted(p for p in SWEEP_DIR.iterdir() if p.is_dir() and p.name.startswith("benchmark"))
    print(f"found {len(run_dirs)} run directories under {SWEEP_DIR.name}/")

    for strat in STRATEGIES:
        rows = summarize_strategy(run_dirs, strat)
        if not rows:
            print(f"  warn: no rows for {strat}")
            continue
        out = SWEEP_DIR / f"pods_timeseries_summary_{strat}.dat"
        write_summary(rows, out, strat)
        print(f"wrote {out.name} ({len(rows)} samples, n={rows[0][3]})")

    steps = summarize_steps(run_dirs, "istio")
    steps_out = SWEEP_DIR / "rps_steps_summary.dat"
    with steps_out.open("w") as f:
        f.write("# RPS step boundaries averaged across runs (from istio)\n")
        f.write(f"# {'t_sec':<10}{'rps':>8}\n")
        for t, rps in steps:
            f.write(f"  {t:<10.2f}{rps:>8d}\n")
    print(f"wrote {steps_out.name} ({len(steps)} steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
