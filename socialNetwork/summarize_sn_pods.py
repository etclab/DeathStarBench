#!/usr/bin/env python3
"""Summarize per-second pod counts from SocialNetwork pods-<rps>.csv files.

The SocialNetwork counterpart to summarize_pods.py. That script is hardcoded
to Bookinfo's (app, version) columns; run-socialnetwork-strategies.sh instead
emits a service name it derives per pod, so its rows are:

    timestamp,service,phase,ready

Unlike the Bookinfo version the service set is NOT hardcoded -- it is
discovered from the data, so this keeps working when the chart's service list
or the autoscaled subset changes.

That derivation matters for what these numbers COVER. The poller used to select
`-l 'service'`, which is the label the DeathStarBench chart stamps -- and which
the Bitnami subcharts (mongodb-sharded, memcached, redis-cluster) and mcrouter
do not carry. The datastore tier was therefore missing from every count: 46
pods reported against a real fleet of 65. It now derives the name from
`service`, then app.kubernetes.io/name[-component], then `app`, so the
datastore tier appears too -- as mongodb-sharded-{configsvr,mongos,shardsvr},
memcached and social-network-mcrouter. A run directory written before that
change has only the 46 application services and is not comparable pod-for-pod
with one written after it.

For each pods-<rps>.csv under <run-dir>/<strategy>/, emit pods-<rps>-sum.csv
with one row per timestamp counting Running+Ready pods per service:

    index,timestamp,<service>...,total,pending,notready

`total` counts only pods that are BOTH phase=Running AND ready=True, so it is
the serving fleet and its meaning is unchanged. The two trailing columns
account for every OTHER pod the poller emitted, so no pod is silently
dropped from the record:

    pending   phase=Pending -- not yet scheduled. A brief spike is normal:
              every new pod is Pending for a second or two between creation
              and binding, so a healthy step under a 2-pods/2s scaleUp policy
              shows a steady trickle. What is NOT normal is a SUSTAINED
              stall, which means the cluster ran out of schedulable CPU
              (~465 pods at 1.1 CPU/pod against 512 schedulable) and the HPA
              asked for a fleet that could not be built. Hence pend_stall_s
              below, which is what the warning triggers on -- max_pend alone
              fires on every healthy run and is worth nothing as an alarm.
    notready  everything else -- Running but not ready (still starting, or
              the sidecar never registered), plus Failed/Unknown. Sustained
              non-zero under Mazu is the signature of a sidecar that cannot
              attest.

Both matter more since maxReplicas doubled in scratch/yaml/sn-hpa.yaml: a
higher ceiling is only safe if the surplus can actually be scheduled, and a
ready-pod count alone cannot tell "the HPA did not ask" from "the HPA asked
and the cluster could not deliver".

It also prints a per-(strategy,rps) scale-up summary: the fleet at the start
and end of the step, the growth, how long it took to reach its final size --
the number that actually separates the meshes under churn -- the worst-case
pending/notready seen during the step, and the longest unbroken stretch of
seconds with at least one pod Pending.

Usage: ./summarize_sn_pods.py <benchmark-run-dir>
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path


# Seconds of unbroken Pending before a step is called starved rather than
# merely busy. Ordinary bind latency is 1-5s and the scaleUp policy adds pods
# every 2s, so a healthy step trickles; 30s of continuous Pending does not
# happen unless the scheduler has nowhere to put them.
PEND_STALL_ALARM_S = 30


def load(src: Path):
    """-> (ready counts per ts/service, pending per ts, notready per ts, services)."""
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pending: dict[int, int] = defaultdict(int)
    notready: dict[int, int] = defaultdict(int)
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
            pending[ts]
            notready[ts]
            if not service:
                continue
            services.add(service)
            # Three mutually exclusive buckets covering every labelled pod.
            # `ready` is empty rather than "False" for a pod young enough to
            # have no Ready condition yet, so test for serving explicitly and
            # let everything else fall through.
            if phase == "Running" and ready == "True":
                counts[ts][service] += 1
            elif phase == "Pending":
                pending[ts] += 1
            else:
                notready[ts] += 1

    return counts, pending, notready, sorted(services)


def summarize(src: Path, dst: Path) -> tuple[int, int, int, int, int, int] | None:
    counts, pending, notready, services = load(src)
    if not counts:
        return None

    order = sorted(counts)
    with dst.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "timestamp"] + services + ["total", "pending", "notready"])
        for idx, ts in enumerate(order):
            vals = [counts[ts][s] for s in services]
            w.writerow([idx, ts] + vals + [sum(vals), pending[ts], notready[ts]])

    totals = [sum(counts[ts][s] for s in services) for ts in order]
    start, end = totals[0], totals[-1]
    # Seconds until the fleet first reaches its final size. With a 1 Hz poll
    # the index is the elapsed second.
    settle = next((i for i, t in enumerate(totals) if t >= end), len(totals) - 1)
    # Peaks, not endpoints: a step that could not schedule for 90s and then
    # recovered still has a scheduling problem worth seeing. pend_stall is the
    # longest unbroken run of samples with anything Pending -- at 1 Hz that is
    # seconds, and it is the figure that separates ordinary bind latency from
    # a cluster that has run out of room.
    pend_stall = run = 0
    for ts in order:
        run = run + 1 if pending[ts] else 0
        pend_stall = max(pend_stall, run)
    return start, end, settle, max(pending[ts] for ts in order), pend_stall, \
        max(notready[ts] for ts in order)


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
        start, end, settle, max_pending, pend_stall, max_notready = res
        m = re.search(r"pods-(\d+)\.csv$", src.name)
        rows.append((src.parent.name, int(m.group(1)) if m else -1, start, end,
                     settle, max_pending, pend_stall, max_notready))

    rows.sort(key=lambda r: (r[1], r[0]))
    print(f"{'strategy':<12} {'rps':>6} {'pods_start':>11} {'pods_end':>9} "
          f"{'growth':>7} {'settle_s':>9} {'max_pend':>9} {'pend_stall_s':>13} "
          f"{'max_notrdy':>11}")
    for strat, rps, start, end, settle, max_pending, pend_stall, max_notready in rows:
        print(f"{strat:<12} {rps:>6} {start:>11} {end:>9} "
              f"{end - start:>+7} {settle:>9} {max_pending:>9} {pend_stall:>13} "
              f"{max_notready:>11}")

    # A step that could not schedule is not a slower step, it is a different
    # experiment: the HPA asked for a fleet the cluster could not build, so
    # its latency and CPU numbers describe a starved application rather than
    # the mesh under test. Say so loudly rather than leaving it in a column.
    starved = [(s_, r, ps) for s_, r, _, _, _, _, ps, _ in rows
               if ps >= PEND_STALL_ALARM_S]
    if starved:
        print(f"\nWARNING: pods sat Pending for >={PEND_STALL_ALARM_S}s -- the "
              "cluster could not schedule the fleet the HPA asked for.",
              file=sys.stderr)
        for s_, r, ps in starved:
            print(f"         {s_} @ {r} RPS: {ps}s", file=sys.stderr)
        print("         Treat these steps as invalid; lower maxReplicas in "
              "scratch/yaml/sn-hpa.yaml or shrink the substrate.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
