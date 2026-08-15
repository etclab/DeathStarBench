#!/usr/bin/env python3
"""Summarize per-second pod counts from SocialNetwork pods-<rps>.csv files.

The SocialNetwork counterpart to summarize_pods.py. That script is hardcoded
to Bookinfo's (app, version) columns; run-socialnetwork-strategies.sh instead
polls the `service` label, so its rows are:

    timestamp,service,phase,ready

Unlike the Bookinfo version the service set is NOT hardcoded -- it is
discovered from the data, so this keeps working when the chart's service list
or the autoscaled subset changes.

For each pods-<rps>.csv under <run-dir>/<strategy>/, emit pods-<rps>-sum.csv
with one row per timestamp counting Running+Ready pods per service:

    index,timestamp,<service>...,total

and print a per-(strategy,rps) scale-up summary: the fleet at the start and
end of the step, the growth, and how long it took to reach its final size --
which is the number that actually separates the meshes under churn.

Usage: ./summarize_sn_pods.py <benchmark-run-dir>
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path


def load(src: Path):
    """-> (per-timestamp per-service ready counts, sorted service names)."""
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    services: set[str] = set()

    with src.open(newline="") as f:
        for row in csv.reader(f):
            if len(row) != 4:
                continue
            ts_s, service, phase, ready = row
            try:
                ts = int(ts_s)
            except ValueError:
                continue  # header
            # Register the timestamp even when nothing is ready, so a step
            # that starts from an empty fleet still has its leading samples.
            counts[ts]
            if not service:
                continue
            services.add(service)
            if phase == "Running" and ready == "True":
                counts[ts][service] += 1

    return counts, sorted(services)


def summarize(src: Path, dst: Path) -> tuple[int, int, int] | None:
    counts, services = load(src)
    if not counts:
        return None

    with dst.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "timestamp"] + services + ["total"])
        for idx, ts in enumerate(sorted(counts)):
            vals = [counts[ts][s] for s in services]
            w.writerow([idx, ts] + vals + [sum(vals)])

    order = sorted(counts)
    totals = [sum(counts[ts][s] for s in services) for ts in order]
    start, end = totals[0], totals[-1]
    # Seconds until the fleet first reaches its final size. With a 1 Hz poll
    # the index is the elapsed second.
    settle = next((i for i, t in enumerate(totals) if t >= end), len(totals) - 1)
    return start, end, settle


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    sources = sorted(p for p in run_dir.glob("*/pods-*.csv")
                     if not p.name.endswith("-sum.csv"))
    if not sources:
        print(f"error: no pods-*.csv files found under {run_dir}/*/", file=sys.stderr)
        return 1

    rows = []
    for src in sources:
        dst = src.with_name(src.stem + "-sum.csv")
        res = summarize(src, dst)
        if res is None:
            print(f"  warn: {src} has no usable samples", file=sys.stderr)
            continue
        start, end, settle = res
        m = re.search(r"pods-(\d+)\.csv$", src.name)
        rows.append((src.parent.name, int(m.group(1)) if m else -1, start, end, settle))

    rows.sort(key=lambda r: (r[1], r[0]))
    print(f"{'strategy':<12} {'rps':>6} {'pods_start':>11} {'pods_end':>9} "
          f"{'growth':>7} {'settle_s':>9}")
    for strat, rps, start, end, settle in rows:
        print(f"{strat:<12} {rps:>6} {start:>11} {end:>9} "
              f"{end - start:>+7} {settle:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
