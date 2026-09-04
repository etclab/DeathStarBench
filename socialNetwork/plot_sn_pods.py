#!/usr/bin/env python3
"""Plot SocialNetwork replica churn from pods-<rps>-sum.csv files.

The SocialNetwork counterpart to plot_pods.py. Run summarize_sn_pods.py first.

Produces under <run-dir>:
  - plot_sn_pod_totals.pdf   total ready pods vs elapsed seconds, one subplot
                             per RPS, one line per strategy. This is the
                             scale-up curve: how fast each mesh grows its
                             fleet, and where it settles.
  - plot_sn_pod_final.pdf    per-service fleet at the END of each step,
                             grouped bars paired by strategy. Shows WHERE the
                             extra replicas went, which is what distinguishes
                             "the mesh made everything heavier" from "one
                             service on the hot path saturated".

Each figure also gets a .dat of the exact series it drew (plot_sn_pod_*.dat),
tab-separated with a comment block naming the source file and the transform --
so a number can be checked or re-plotted without re-running the sweep.

Strategies are discovered from the directory names rather than hardcoded.

Usage: ./plot_sn_pods.py <benchmark-run-dir>
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# Labels, order and colors are shared with plot_sn_latency.py and
# plot_sn_resources.py so that Istio and Mazu are the same series, in the same
# color, in every plot a run directory produces. A reader flipping between the
# replica, latency and resource figures should never have to re-learn which
# line is which.
STRATEGY_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
STRATEGY_ORDER = ["istio", "st5-AttUpd"]
# Paul-Tol palette, matching generate_timeseries_plots.py and
# plot_sn_resources.py -- one color per arm across the whole repo.
STRATEGY_COLORS = {"istio": "#77AADD", "st5-AttUpd": "#EE8866"}


def label_of(strat: str) -> str:
    return STRATEGY_LABELS.get(strat, strat)


def color_of(strat: str):
    """None lets matplotlib fall back to its default cycle for unknown arms."""
    return STRATEGY_COLORS.get(strat)


def ordered(strats):
    """Known strategies in a fixed order, then anything else alphabetically."""
    known = [s for s in STRATEGY_ORDER if s in strats]
    return known + sorted(s for s in strats if s not in STRATEGY_ORDER)


def save(fig, out: Path) -> list:
    """Write both PDF (for papers) and PNG (viewable without a PDF reader)."""
    written = []
    for path in (out, out.with_suffix(".png")):
        fig.savefig(path, dpi=150 if path.suffix == ".png" else None,
                    bbox_inches="tight")
        written.append(path)
    return written


# Columns in pods-<rps>-sum.csv that are not per-service counts.
NON_SERVICE_COLUMNS = ("index", "timestamp", "total", "pending", "notready")


def read_sum(path: Path):
    """-> (elapsed seconds, totals, {service: final count})."""
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return [], [], {}
    idx = [int(r["index"]) for r in rows]
    totals = [int(r["total"]) for r in rows]
    # Every column that is not bookkeeping is a service. pending/notready are
    # bookkeeping -- summarize_sn_pods.py appends them after `total` so a step
    # the cluster could not schedule is visible in the record. Leaving them
    # out of this list is what keeps them out of the per-service plots.
    services = [k for k in rows[0] if k not in NON_SERVICE_COLUMNS]
    final = {s: int(rows[-1][s]) for s in services}
    return idx, totals, final


def collect(run_dir: Path):
    """-> {rps: {strategy: (idx, totals, final)}}"""
    data: dict[int, dict[str, tuple]] = defaultdict(dict)
    for p in sorted(run_dir.glob("*/pods-*-sum.csv")):
        m = re.search(r"pods-(\d+)-sum\.csv$", p.name)
        if not m:
            continue
        data[int(m.group(1))][p.parent.name] = read_sum(p)
    return data


def plot_totals(data, out: Path) -> None:
    rps_values = sorted(data)
    n = len(rps_values)
    ncols = min(2, n)
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 3.6 * nrows),
                             squeeze=False)
    for ax, rps in zip(axes.flat, rps_values):
        for strat in ordered(data[rps]):
            idx, totals, _ = data[rps][strat]
            if idx:
                ax.plot(idx, totals, label=label_of(strat),
                        color=color_of(strat), linewidth=1.6)
        ax.set_title(f"{rps} RPS")
        ax.set_xlabel("elapsed (s)")
        ax.set_ylabel("ready pods")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    for ax in axes.flat[n:]:
        ax.axis("off")
    fig.suptitle("Replica churn: ready pods during each load step")
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)


def plot_growth(data, out: Path) -> None:
    """Total fleet vs RPS, one line per strategy -- the headline growth curve.

    plot_totals() shows the SHAPE of each ramp but gives every step its own
    y-axis, so a 6-pod spread and a 108-pod spread look equally dramatic.
    plot_final() breaks the fleet down per service but only within one step.
    Neither answers the actual comparison question: across the whole sweep, how
    many replicas does each mesh need to carry the same offered load?

    Both the settled fleet (solid, end of step) and the peak reached during the
    step (dashed) are drawn: under HPA the two differ whenever a step is still
    ramping when its window closes, and a settled-only plot would hide an arm
    that overshot and came back down.
    """
    rps_values = sorted(data)
    strats = ordered({st for rps in data for st in data[rps]})
    fig, ax = plt.subplots(figsize=(8, 5))
    for strat in strats:
        xs = [r for r in rps_values if data[r].get(strat) and data[r][strat][1]]
        if not xs:
            continue
        final = [data[r][strat][1][-1] for r in xs]
        peak = [max(data[r][strat][1]) for r in xs]
        ax.plot(xs, final, marker="o", linewidth=1.8,
                color=color_of(strat), label=f"{label_of(strat)} (settled)")
        if peak != final:
            ax.plot(xs, peak, marker="^", linewidth=1.2, linestyle="--",
                    color=color_of(strat), alpha=0.7,
                    label=f"{label_of(strat)} (peak)")
    ax.set_xlabel("target RPS")
    ax.set_ylabel("ready pods")
    ax.set_title("Replica growth under load: total ready pods vs offered RPS")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)


def plot_final(data, out: Path) -> None:
    rps_values = sorted(data)
    n = len(rps_values)
    fig, axes = plt.subplots(n, 1, figsize=(12, 3.4 * n), squeeze=False)
    for ax, rps in zip(axes.flat, rps_values):
        strats = ordered(data[rps])
        services = sorted({s for st in strats for s in data[rps][st][2]
                           if data[rps][st][2].get(s)})
        if not services:
            ax.axis("off")
            continue
        x = np.arange(len(services))
        width = 0.8 / max(len(strats), 1)
        for i, strat in enumerate(strats):
            final = data[rps][strat][2]
            ax.bar(x + i * width - 0.4 + width / 2,
                   [final.get(s, 0) for s in services], width,
                   label=label_of(strat), color=color_of(strat))
        ax.set_xticks(x)
        ax.set_xticklabels(services, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("pods")
        ax.set_title(f"Fleet at end of {rps} RPS step")
        ax.grid(alpha=0.3, axis="y")
        ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, out)
    plt.close(fig)


# ===========================================================================
# .dat export
#
# Every figure above also writes the exact series it drew to a tab-separated
# .dat beside it, so a number can be checked -- or re-plotted in gnuplot --
# without re-running a multi-hour sweep or opening a PDF. Each file opens with
# a comment block naming the file the numbers came from and the transform
# applied to them. Absent samples are written N/A, never 0.
# ===========================================================================

# Shared by all three files below: they are all views of the same summarised
# poll, and the counting rule is the one thing a reader must not guess at.
DAT_SOURCE = """\
SOURCE     <run-dir>/<strategy>/pods-<rps>-sum.csv, written by
           summarize_sn_pods.py from the raw pods-<rps>.csv that
           run-socialnetwork-strategies.sh polls once a second for the whole
           load step. A pod counts toward `total` only when it is BOTH
           phase=Running AND ready=True at that sample, so pods that exist but
           are not yet serving traffic are excluded.
"""

GROWTH_DOC = f"""\
Data behind plot_sn_pod_growth.pdf/.png -- total ready pods vs offered RPS.

{DAT_SOURCE}\
TRANSFORM  settled = the LAST `total` sample of the step, i.e. the fleet the
           step ended with. peak = max(`total`) over the step. Under HPA the
           two differ whenever a step was still ramping when its window
           closed, so both are kept -- a settled-only number hides an arm that
           overshot and came back down.
UNITS      ready pods, summed over every service in the chart.
LAYOUT     one row per RPS step; one settled/peak column pair per strategy.
"""

TOTALS_DOC = f"""\
Data behind plot_sn_pod_totals.pdf/.png -- ready pods over time inside each step.

{DAT_SOURCE}\
TRANSFORM  none beyond that per-sample count: one column per (RPS step,
           strategy), one row per sample, values copied straight from `total`.
           elapsed_s is the sample index; the poll runs at 1 Hz, so it is also
           seconds since the step's polling began.
           Steps do not all have the same number of samples, so short columns
           are padded with N/A rather than repeating their last value -- a
           flat tail there would read as a settled fleet that was never
           observed.
UNITS      ready pods, summed over every service.
LAYOUT     one row per elapsed second; one column per (RPS step, strategy).
"""

FINAL_DOC = f"""\
Data behind plot_sn_pod_final.pdf/.png -- per-service fleet at the END of each step.

{DAT_SOURCE}\
TRANSFORM  the LAST row of each pods-<rps>-sum.csv, kept split per service
           instead of summed. This is where the replicas counted by
           plot_sn_pod_growth actually went.
           A service appears for a step only when at least one arm ended that
           step with a non-zero count -- the same filter the figure applies,
           so the rows here are exactly the bars there.
UNITS      ready pods, per service.
LAYOUT     one row per (RPS step, service); one column per strategy.
"""


def write_dat(path: Path, doc: str, header: list, rows: list) -> Path:
    """Write one tab-separated gnuplot .dat: comment block, header, rows.

    The header line is '#'-prefixed (a gnuplot comment, matching
    generate_dat.py) so the file plots directly with no skip-row argument.
    None -> "N/A": a sample we do not have must never be read as a measured 0.
    """
    with path.open("w") as f:
        for line in doc.strip("\n").splitlines():
            f.write(("# " + line).rstrip() + "\n")
        f.write("# " + "\t".join(str(h) for h in header) + "\n")
        for row in rows:
            f.write("\t".join("N/A" if v is None else str(v) for v in row) + "\n")
    return path


def write_growth_dat(data, out: Path) -> Path:
    strats = ordered({st for rps in data for st in data[rps]})
    header = ["rps"]
    for s in strats:
        header += [f"{label_of(s)}_settled", f"{label_of(s)}_peak"]
    rows = []
    for rps in sorted(data):
        row = [rps]
        for s in strats:
            entry = data[rps].get(s)
            totals = entry[1] if entry else None
            row += [totals[-1], max(totals)] if totals else [None, None]
        rows.append(row)
    return write_dat(out, GROWTH_DOC, header, rows)


def write_totals_dat(data, out: Path) -> Path:
    # Built in plot order so the columns read left-to-right the way the
    # subplots do.
    series = {}
    for rps in sorted(data):
        for strat in ordered(data[rps]):
            totals = data[rps][strat][1]
            if totals:
                series[(rps, strat)] = totals
    header = ["elapsed_s"] + [f"{rps}rps_{label_of(s)}" for rps, s in series]
    rows = []
    for i in range(max((len(v) for v in series.values()), default=0)):
        rows.append([i] + [v[i] if i < len(v) else None
                           for v in series.values()])
    return write_dat(out, TOTALS_DOC, header, rows)


def write_final_dat(data, out: Path) -> Path:
    strats = ordered({st for rps in data for st in data[rps]})
    header = ["rps", "service"] + [label_of(s) for s in strats]
    rows = []
    for rps in sorted(data):
        present = [s for s in strats if data[rps].get(s)]
        services = sorted({svc for s in present for svc in data[rps][s][2]
                           if data[rps][s][2].get(svc)})
        for svc in services:
            rows.append([rps, svc] + [data[rps][s][2].get(svc) if data[rps].get(s)
                                      else None for s in strats])
    return write_dat(out, FINAL_DOC, header, rows)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    data = collect(run_dir)
    if not data:
        print(f"error: no pods-*-sum.csv under {run_dir}/*/ "
              f"(run summarize_sn_pods.py first)", file=sys.stderr)
        return 1

    totals_out = run_dir / "plot_sn_pod_totals.pdf"
    final_out = run_dir / "plot_sn_pod_final.pdf"
    growth_out = run_dir / "plot_sn_pod_growth.pdf"
    plot_totals(data, totals_out)
    plot_final(data, final_out)
    plot_growth(data, growth_out)

    # The same series in text form -- see the ".dat export" section above.
    write_growth_dat(data, growth_out.with_suffix(".dat"))
    write_totals_dat(data, totals_out.with_suffix(".dat"))
    write_final_dat(data, final_out.with_suffix(".dat"))

    for out in (growth_out, totals_out, final_out):
        print(f"wrote {out} (+ {out.with_suffix('.png').name}"
              f" + {out.with_suffix('.dat').name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
