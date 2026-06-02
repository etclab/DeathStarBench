#!/usr/bin/env python3
"""
Pending "pod-seconds" per service -- a single scalar per service/strategy that
quantifies the total scheduling/scaling delay during the benchmark.

The metric is the product of *pending time* and *number of pending pods*,
accumulated per second per service:

    pending_pod_seconds(service) = sum_over_seconds( pending_pods(service, s) * 1s )

Because the raw pods-<rps>.csv has one row per (pod, second) at a 1-second
cadence, each pending observation contributes exactly 1 pod-second. Summing the
per-second pending count over the whole benchmark therefore equals the area
under the pending-pods-vs-time curve (script analyze_pending_pods_per_service.py
plots that curve; this script integrates it to a single number). A pod is
"pending" at a second when its `ready` field is not "True".

This makes "how much longer does istio keep pods pending than mazu?" answerable
with one number per service, plus the mazu-vs-istio ratio/delta.

Pending pod-seconds blends two effects: how *slow* scheduling was, and how *many*
pods got scheduled (mazu provisions more replicas, so it racks up more pending
pod-seconds even if each pod schedules just as fast). To separate them this
script also reports the *mean pending time per pod*:

    pods_added                = peak_count - initial_count   (per service, run)
    mean_pending_time_per_pod = pending_pod_seconds / pods_added

i.e. the average seconds each newly-scheduled pod spent pending. Pods are not
individually identifiable in the raw data (only app/version), so we estimate the
number of pods that passed through the pending state from the count delta over
the run (counts ramp up roughly monotonically). The per-pod ratio is computed
per run, then averaged across runs (runs with no scaling, pods_added==0, are
skipped for the per-pod stat only).

CLI: optional positional `rps`, defaults to 400.

    python3 analyze_pending_pod_seconds_per_service.py        # 400 RPS
    python3 analyze_pending_pod_seconds_per_service.py 600

Outputs:
  - pending_pod_seconds_raw_<rps>.csv
        One row per (strategy, service, run): strategy,service,rps,run,
        pending_pod_seconds,pods_added,pending_time_per_pod
  - pending_pod_seconds_summary_<rps>.csv
        Per (strategy, service): strategy,service,rps,n_runs,
        mean_pending_pod_seconds,stddev_pending_pod_seconds,
        n_runs_scaled,mean_pending_time_per_pod,stddev_pending_time_per_pod
  - pending_pod_seconds_compare_<rps>.csv
        Per service: istio/mazu means, delta and ratio, for BOTH pending
        pod-seconds (volume+speed) and pending time per pod (speed only)
        -- the head-to-head quantification of the delay.
  - pending_pod_seconds_<rps>.pdf
        Grouped bar chart: x=service, one bar per strategy, height=mean pending
        pod-seconds with +/-1 sigma error bars.
  - pending_time_per_pod_<rps>.pdf
        Same chart for mean pending time per pod (replica-count normalized).
"""

import argparse
import csv
import glob
import os
import statistics

import matplotlib

matplotlib.use("Agg")  # headless / no display
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_GLOB = os.path.join(HERE, "benchmark1.5-*")
STRATEGIES = ["st5-AttUpd", "istio"]  # st5-AttUpd == "mazu"
STRATEGY_LABELS = {"st5-AttUpd": "mazu (st5-AttUpd)", "istio": "istio"}
STRATEGY_COLORS = {"st5-AttUpd": "tab:blue", "istio": "tab:orange"}

# Service plotting order. frontend aggregator first.
SERVICES = [
    "productpage-v1",
    "details-v1",
    "ratings-v1",
    "reviews-v1",
    "reviews-v2",
    "reviews-v3",
]

DEFAULT_RPS = 400


def metrics_by_service(csv_path):
    """
    Return dict[service] -> (pending_pod_seconds, pods_added) for one
    pods-<rps>.csv file.

    pending_pod_seconds = number of (pod, second) observations where the pod was
    not ready. With a 1-second sampling cadence this is exactly the integral of
    the pending-pod count over time (pod-seconds).

    pods_added = peak per-second total pod count - initial (first-second) total
    pod count, an estimate of how many pods the autoscaler created (and thus how
    many passed through the pending state) during the run. Pods are not
    individually identifiable, so this count-delta is the best available proxy.
    Every service seen in the file is present in the result.
    """
    pending = {}                    # svc -> pending_pod_seconds
    counts = {}                     # svc -> ts -> total pod count that second
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            svc = f"{row['app']}-{row['version']}"
            ts = int(row["timestamp"])
            pending.setdefault(svc, 0)
            counts.setdefault(svc, {})
            counts[svc][ts] = counts[svc].get(ts, 0) + 1
            if row["ready"].strip() != "True":
                pending[svc] += 1

    out = {}
    for svc, by_ts in counts.items():
        initial = by_ts[min(by_ts)]
        peak = max(by_ts.values())
        pods_added = max(peak - initial, 0)
        out[svc] = (pending[svc], pods_added)
    return out


def collect(rps):
    """dict[strategy][service] -> list of (pending_pod_seconds, pods_added)."""
    data = {s: {} for s in STRATEGIES}
    run_dirs = sorted(glob.glob(RUN_GLOB))
    if not run_dirs:
        raise SystemExit(f"No run folders matched {RUN_GLOB}")

    raw_rows = []  # (strategy, service, run, pending_pod_seconds, pods_added)
    for run_dir in run_dirs:
        run = os.path.basename(run_dir)
        for strategy in STRATEGIES:
            csv_path = os.path.join(run_dir, strategy, f"pods-{rps}.csv")
            if not os.path.isfile(csv_path):
                continue
            for svc, (pps, added) in metrics_by_service(csv_path).items():
                data[strategy].setdefault(svc, []).append((pps, added))
                raw_rows.append((strategy, svc, run, pps, added))
    return data, raw_rows


def summarize(data):
    """
    dict[strategy][service] -> dict with pending-pod-seconds stats (over all
    runs) and pending-time-per-pod stats (over runs that actually scaled).
    """
    summary = {s: {} for s in STRATEGIES}
    for strategy in STRATEGIES:
        for svc, vals in data[strategy].items():
            pps_vals = [pps for pps, _ in vals]
            n = len(pps_vals)
            mean_pps = statistics.mean(pps_vals)
            std_pps = statistics.stdev(pps_vals) if n > 1 else 0.0

            # Per-pod ratio only defined for runs where the autoscaler added
            # pods; otherwise pending_pod_seconds should be 0 with nothing to
            # normalize by.
            per_pod = [pps / added for pps, added in vals if added > 0]
            n_pod = len(per_pod)
            mean_pp = statistics.mean(per_pod) if n_pod else 0.0
            std_pp = statistics.stdev(per_pod) if n_pod > 1 else 0.0

            summary[strategy][svc] = {
                "n": n, "mean_pps": mean_pps, "std_pps": std_pps,
                "n_pod": n_pod, "mean_pp": mean_pp, "std_pp": std_pp,
            }
    return summary


def _stat(summary, strategy, svc, key):
    """Safe lookup; 0.0 when a strategy/service has no data."""
    return summary[strategy].get(svc, {}).get(key, 0.0)


def write_raw(rps, raw_rows, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "service", "rps", "run", "pending_pod_seconds",
                    "pods_added", "pending_time_per_pod"])
        for strategy, svc, run, pps, added in sorted(raw_rows):
            per_pod = f"{pps / added:.4f}" if added > 0 else ""
            w.writerow([strategy, svc, rps, run, pps, added, per_pod])
    print(f"[written] {path}")


def write_summary(rps, summary, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "service", "rps", "n_runs",
                    "mean_pending_pod_seconds", "stddev_pending_pod_seconds",
                    "n_runs_scaled", "mean_pending_time_per_pod",
                    "stddev_pending_time_per_pod"])
        for strategy in STRATEGIES:
            for svc in SERVICES:
                if svc not in summary[strategy]:
                    continue
                s = summary[strategy][svc]
                w.writerow([strategy, svc, rps, s["n"],
                            f"{s['mean_pps']:.4f}", f"{s['std_pps']:.4f}",
                            s["n_pod"], f"{s['mean_pp']:.4f}",
                            f"{s['std_pp']:.4f}"])
    print(f"[written] {path}")


def write_compare(rps, summary, path):
    """Head-to-head istio vs mazu per service (the delay quantification).

    Reports both pending pod-seconds (volume + speed) and pending time per pod
    (speed only, replica-count normalized).
    """
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "rps",
                    "istio_mean_pending_pod_seconds",
                    "mazu_mean_pending_pod_seconds",
                    "pps_delta_istio_minus_mazu", "pps_ratio_istio_over_mazu",
                    "istio_mean_pending_time_per_pod",
                    "mazu_mean_pending_time_per_pod",
                    "perpod_delta_istio_minus_mazu",
                    "perpod_ratio_istio_over_mazu"])
        i_pps_tot = m_pps_tot = 0.0
        for svc in SERVICES:
            m_pps = _stat(summary, "st5-AttUpd", svc, "mean_pps")
            i_pps = _stat(summary, "istio", svc, "mean_pps")
            m_pp = _stat(summary, "st5-AttUpd", svc, "mean_pp")
            i_pp = _stat(summary, "istio", svc, "mean_pp")
            i_pps_tot += i_pps
            m_pps_tot += m_pps
            w.writerow([
                svc, rps,
                f"{i_pps:.4f}", f"{m_pps:.4f}",
                f"{i_pps - m_pps:.4f}",
                f"{i_pps / m_pps:.4f}" if m_pps else "inf",
                f"{i_pp:.4f}", f"{m_pp:.4f}",
                f"{i_pp - m_pp:.4f}",
                f"{i_pp / m_pp:.4f}" if m_pp else "inf",
            ])
        # TOTAL row: pod-seconds sum meaningfully; per-pod does not (it is a
        # rate), so leave the per-pod columns blank on the total line.
        w.writerow([
            "TOTAL", rps,
            f"{i_pps_tot:.4f}", f"{m_pps_tot:.4f}",
            f"{i_pps_tot - m_pps_tot:.4f}",
            f"{i_pps_tot / m_pps_tot:.4f}" if m_pps_tot else "inf",
            "", "", "", "",
        ])
    print(f"[written] {path}")


def _bar_chart(rps, summary, mean_key, std_key, ylabel, title, out_pdf):
    x = range(len(SERVICES))
    width = 0.38
    fig, ax = plt.subplots(figsize=(11, 6))
    for i, strategy in enumerate(STRATEGIES):
        means = [_stat(summary, strategy, svc, mean_key) for svc in SERVICES]
        errs = [_stat(summary, strategy, svc, std_key) for svc in SERVICES]
        offset = (i - (len(STRATEGIES) - 1) / 2) * width
        ax.bar([xi + offset for xi in x], means, width,
               yerr=errs, capsize=4, color=STRATEGY_COLORS[strategy],
               label=STRATEGY_LABELS[strategy])
    ax.set_xticks(list(x))
    ax.set_xticklabels(SERVICES, rotation=20, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_pdf)
    plt.close(fig)
    print(f"[written] {out_pdf}")


def plot(rps, summary):
    _bar_chart(
        rps, summary, "mean_pps", "std_pps",
        "Pending pod-seconds (pending pods integrated over time)",
        f"Total pending pod-seconds per service at {rps} RPS: "
        "mazu (st5-AttUpd) vs istio (mean +/- 1 sigma over runs)",
        os.path.join(HERE, f"pending_pod_seconds_{rps}.pdf"))
    _bar_chart(
        rps, summary, "mean_pp", "std_pp",
        "Mean pending time per pod (s) [pod-seconds / pods added]",
        f"Mean pending time per scheduled pod at {rps} RPS: "
        "mazu (st5-AttUpd) vs istio (replica-count normalized)",
        os.path.join(HERE, f"pending_time_per_pod_{rps}.pdf"))


def main():
    parser = argparse.ArgumentParser(
        description="Pending pod-seconds (pending pods integrated over time) "
                    "per service across runs.")
    parser.add_argument("rps", nargs="?", type=int, default=DEFAULT_RPS,
                        help=f"RPS level to process (default {DEFAULT_RPS}).")
    args = parser.parse_args()
    rps = args.rps

    data, raw_rows = collect(rps)
    summary = summarize(data)
    write_raw(rps, raw_rows,
              os.path.join(HERE, f"pending_pod_seconds_raw_{rps}.csv"))
    write_summary(rps, summary,
                  os.path.join(HERE, f"pending_pod_seconds_summary_{rps}.csv"))
    write_compare(rps, summary,
                  os.path.join(HERE, f"pending_pod_seconds_compare_{rps}.csv"))
    plot(rps, summary)

    # Console summary 1: pending pod-seconds (volume + speed).
    print(f"\nMean pending pod-seconds per service at {rps} RPS "
          f"(volume + speed; higher = more pod-time spent pending):")
    print(f"  {'service':16s}{'mazu':>12s}{'istio':>12s}"
          f"{'delta(i-m)':>14s}")
    istio_total = mazu_total = 0.0
    for svc in SERVICES:
        mazu = _stat(summary, "st5-AttUpd", svc, "mean_pps")
        istio = _stat(summary, "istio", svc, "mean_pps")
        istio_total += istio
        mazu_total += mazu
        print(f"  {svc:16s}{mazu:>12.1f}{istio:>12.1f}{istio - mazu:>14.1f}")
    print(f"  {'TOTAL':16s}{mazu_total:>12.1f}{istio_total:>12.1f}"
          f"{istio_total - mazu_total:>14.1f}")
    if mazu_total:
        print(f"  -> istio = {istio_total / mazu_total:.2f}x mazu's pending "
              f"pod-seconds ({istio_total - mazu_total:+.1f} pod-seconds).")

    # Console summary 2: pending time per pod (speed only, normalized).
    print(f"\nMean pending time PER POD per service at {rps} RPS "
          f"(speed only; replica-count normalized):")
    print(f"  {'service':16s}{'mazu(s)':>12s}{'istio(s)':>12s}"
          f"{'delta(i-m)':>14s}")
    for svc in SERVICES:
        mazu = _stat(summary, "st5-AttUpd", svc, "mean_pp")
        istio = _stat(summary, "istio", svc, "mean_pp")
        tag = "" if (mazu or istio) else "   (no scaling)"
        print(f"  {svc:16s}{mazu:>12.1f}{istio:>12.1f}"
              f"{istio - mazu:>14.1f}{tag}")
    print("\n  Per-pod = pending_pod_seconds / (peak - initial pod count), "
          "averaged over runs that scaled.")


if __name__ == "__main__":
    main()
