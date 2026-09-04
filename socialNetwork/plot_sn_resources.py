#!/usr/bin/env python3
"""Plot Istio-vs-Mazu CPU and memory from metrics_<rps>.json files.

The resource-cost counterpart to plot_sn_pods.py, for runs produced by
run-socialnetwork-strategies.sh. Reads the files written by
collect_metrics_sn.sh under <run-dir>/<strategy>/metrics_<rps>.json.

WHY `totals` AND NOT THE PER-POD MATRICES
  Each metrics file carries both a per-pod query_range matrix (cpu.<group> /
  memory.<group>) and a fleet-wide scalar (totals.cpu.<group> /
  totals.memory.<group>). Only the scalar is comparable ACROSS ARMS.

  Under HPA the pod count is a dependent variable: it differs between the two
  meshes and between RPS steps. An average over the per-pod vector therefore
  flatters whichever arm happened to run more pods -- the same fleet-wide
  burn spread over more replicas reads as a lower "per-pod" number even though
  the cluster is paying more. `totals` is the fleet-wide sum averaged over the
  benchmark window, i.e. what the cluster actually spent, which is the honest
  cross-arm number. (Same reasoning as the "WHY THE TOTALS BLOCK" section of
  collect_metrics_sn.sh.)

WHY BY COMPONENT AND NOT ONE AGGREGATE
  A single stacked total would bury the finding. Application CPU is
  essentially IDENTICAL across the two meshes -- same workload, same
  substrate, so the app tier must cost the same -- while the sidecar/proxy
  tier differs by several fold. Only a per-component breakdown shows that the
  delta is entirely mesh overhead and not the application doing more work.
  kube_apiserver is plotted too, but it is a CLUSTER-WIDE background cost and
  is not attributable to the workload the way the other four groups are.

Produces under <run-dir> (one figure per metric, PDF + PNG + .dat of each):
  - plot_sn_cpu.pdf / .png      fleet-wide CPU (cores), one subplot per
                                component, grouped bars per RPS step paired by
                                strategy.
  - plot_sn_memory.pdf / .png   same layout for working-set memory, converted
                                from bytes and labelled MiB or GiB per panel.
  - plot_sn_cpu.dat             the plotted numbers, tab-separated, one row per
    plot_sn_memory.dat          (RPS step, component), with a comment block
                                naming the source field and every transform.

Where the Mazu/Istio ratio is large it is annotated above the bar pair, since
that ratio is the headline number.

Component-per-subplot (rather than RPS-per-subplot) is deliberate: a 13-step
sweep becomes 13 slim bar pairs inside each of five panels, which stays
readable, whereas grouping five components inside each RPS step would put 10
bars in every group.

Strategies are discovered from the directory names rather than hardcoded;
known ones are relabelled (istio -> Istio, st5-AttUpd -> Mazu) and keep a
stable color across every plot in this repo family.

Missing strategy dirs, missing metrics files, empty/failed Prometheus results
and partially finished runs are all warned about on stderr and then skipped --
they are plotted as absent, never as zero. The script exits non-zero only when
there is nothing at all to plot.

Usage: ./plot_sn_resources.py <benchmark-run-dir>
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# Keep in sync with generate_plot_data.py / generate_timeseries_plots.py.
STRATEGY_DISPLAY = {"st5-AttUpd": "Mazu", "istio": "Istio"}

# Stable color per strategy so Istio and Mazu look the same in every figure
# of this family. Palette shared with generate_timeseries_plots.py.
STRATEGY_COLOR = {"istio": "#77AADD", "st5-AttUpd": "#EE8866"}
FALLBACK_COLORS = ["#EEDD88", "#44BB99", "#FFAABB", "#BBCC33", "#99DDFF"]

# Plot order, not discovery order: istio is the baseline so it goes first.
STRATEGY_ORDER = ["istio", "st5-AttUpd"]

COMPONENTS = ["app", "proxy", "istiod", "ingressgateway", "kube_apiserver"]
COMPONENT_TITLE = {
    "app": "app containers (workload)",
    "proxy": "istio-proxy sidecars",
    "istiod": "istiod (control plane)",
    "ingressgateway": "istio-ingressgateway",
    "kube_apiserver": "kube-apiserver (cluster-wide background)",
}

# Annotate the Mazu/Istio ratio only where it is worth reading: a 1.02x label
# on every app bar is noise, a 7x label on the proxy bar is the result.
RATIO_HI = 1.25
RATIO_LO = 1 / RATIO_HI


def display_name(strategy: str) -> str:
    return STRATEGY_DISPLAY.get(strategy, strategy)


def strategy_color(strategy: str, index: int) -> str:
    return STRATEGY_COLOR.get(strategy,
                              FALLBACK_COLORS[index % len(FALLBACK_COLORS)])


def order_strategies(strategies) -> list:
    """Known strategies first in a fixed order, then anything else, sorted."""
    known = [s for s in STRATEGY_ORDER if s in strategies]
    rest = sorted(s for s in strategies if s not in STRATEGY_ORDER)
    return known + rest


def scalar(vector) -> float | None:
    """Pull the scalar out of a Prometheus instant-vector response.

    collect_metrics_sn.sh substitutes {"status":"error", ... "result":[]} for
    any query that failed, so an empty result means MISSING, not zero -- return
    None and let the caller leave a gap rather than draw a bar at 0.
    """
    if not isinstance(vector, dict):
        return None
    result = vector.get("data", {}).get("result") or []
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def read_metrics(path: Path):
    """-> ({"cpu": {group: val|None}, "memory": {...}}, [warnings])."""
    warnings = []
    try:
        with path.open() as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{path}: unreadable ({exc})"]

    totals = doc.get("totals") or {}
    if not totals:
        return None, [f"{path}: no totals block"]

    out = {}
    for metric in ("cpu", "memory"):
        groups = totals.get(metric) or {}
        vals = {}
        for comp in COMPONENTS:
            if comp not in groups:
                warnings.append(f"{path}: totals.{metric}.{comp} absent")
                vals[comp] = None
                continue
            val = scalar(groups[comp])
            if val is None:
                warnings.append(
                    f"{path}: totals.{metric}.{comp} empty result "
                    f"(query failed at collection time) -- skipped")
            vals[comp] = val
        out[metric] = vals
    return out, warnings


def rps_key(label: str):
    """Sort numeric RPS steps numerically, park 'unknown' at the end."""
    return (0, int(label)) if label.isdigit() else (1, 0, label)


def collect(run_dir: Path):
    """-> ({rps_label: {strategy: {"cpu": {...}, "memory": {...}}}}, strategies)."""
    data = defaultdict(dict)
    strategies = []
    for strat_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        files = sorted(strat_dir.glob("metrics_*.json"))
        if not files:
            continue
        strategies.append(strat_dir.name)
        found = False
        for path in files:
            m = re.match(r"metrics_(.+)\.json$", path.name)
            rps = m.group(1) if m else path.stem
            metrics, warnings = read_metrics(path)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            if metrics is None:
                continue
            data[rps][strat_dir.name] = metrics
            found = True
        if not found:
            print(f"warning: {strat_dir}: no usable metrics_*.json",
                  file=sys.stderr)
    return data, strategies


def pick_unit(values, metric: str):
    """-> (divisor, unit label) for one panel's worth of values."""
    if metric == "cpu":
        return 1.0, "cores"
    peak = max((v for v in values if v is not None), default=0.0)
    if peak >= 1024 ** 3:
        return float(1024 ** 3), "GiB"
    return float(1024 ** 2), "MiB"


def plot_metric(data, strategies, metric: str, out_paths) -> None:
    """One panel per component; grouped bars over RPS steps, per strategy."""
    rps_labels = sorted(data, key=rps_key)
    strategies = order_strategies(strategies)
    n_rps = len(rps_labels)

    ncols = 2
    nrows = (len(COMPONENTS) + ncols - 1) // ncols
    # Widen with the sweep so a 13-step run does not squash its bars, but cap
    # it so a 2-step fixture does not come out as a page of slabs.
    panel_w = min(11.0, max(5.5, 0.7 * n_rps + 3.0))
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(panel_w * ncols, 3.3 * nrows),
                             squeeze=False)

    x = np.arange(n_rps)
    legend_handles, legend_labels = [], []
    group_w = min(0.8, 0.28 * max(len(strategies), 1))
    width = group_w / max(len(strategies), 1)

    for ax, comp in zip(axes.flat, COMPONENTS):
        panel_vals = [data[r].get(s, {}).get(metric, {}).get(comp)
                      for r in rps_labels for s in strategies]
        divisor, unit = pick_unit(panel_vals, metric)

        drew = False
        for i, strat in enumerate(strategies):
            offset = i * width - group_w / 2 + width / 2
            xs, ys = [], []
            for j, rps in enumerate(rps_labels):
                val = data[rps].get(strat, {}).get(metric, {}).get(comp)
                if val is None:
                    continue
                xs.append(x[j] + offset)
                ys.append(val / divisor)
            if xs:
                ax.bar(xs, ys, width, label=display_name(strat),
                       color=strategy_color(strat, i), edgecolor="white",
                       linewidth=0.3)
                drew = True

        if not drew:
            ax.set_title(f"{COMPONENT_TITLE[comp]} -- no data", fontsize=10)
            ax.axis("off")
            continue

        # Ratio annotation: only meaningful with exactly the two known arms
        # present, and only worth ink when the arms actually differ.
        if "istio" in strategies and "st5-AttUpd" in strategies:
            top = ax.get_ylim()[1]
            for j, rps in enumerate(rps_labels):
                base = data[rps].get("istio", {}).get(metric, {}).get(comp)
                mazu = data[rps].get("st5-AttUpd", {}).get(metric, {}).get(comp)
                if not base or mazu is None:
                    continue
                ratio = mazu / base
                if RATIO_LO < ratio < RATIO_HI:
                    continue
                height = max(base, mazu) / divisor
                ax.text(x[j], height + 0.03 * top, f"{ratio:.1f}x",
                        ha="center", va="bottom", fontsize=7, color="#333333")
            ax.set_ylim(top=top * 1.18)

        ax.set_xticks(x)
        ax.set_xticklabels(rps_labels, rotation=45 if n_rps > 6 else 0,
                           ha="right" if n_rps > 6 else "center", fontsize=8)
        ax.set_xlabel("target RPS")
        ax.set_ylabel(f"{'CPU' if metric == 'cpu' else 'memory'} ({unit})")
        ax.set_title(COMPONENT_TITLE[comp], fontsize=10)
        ax.grid(alpha=0.3, axis="y")
        # Take the legend from the first panel that drew every strategy, so a
        # panel whose data is partly missing cannot produce a partial legend.
        handles, labels = ax.get_legend_handles_labels()
        if len(labels) > len(legend_labels):
            legend_handles, legend_labels = handles, labels

    # One shared legend in the unused grid slot when there is one -- five
    # copies of a two-entry legend is ink that could be bars. If the grid is
    # full, fall back to a legend on the first panel.
    spare = list(axes.flat[len(COMPONENTS):])
    for ax in spare:
        ax.axis("off")
    if legend_handles:
        if spare:
            spare[0].legend(legend_handles, legend_labels, loc="center",
                            fontsize=11, title="strategy", frameon=False)
        else:
            axes.flat[0].legend(legend_handles, legend_labels, fontsize=8)

    label = "CPU (cores)" if metric == "cpu" else "memory (working set)"
    fig.suptitle(f"Fleet-wide {label}, summed over all pods and averaged over "
                 f"each load step\n"
                 f"labels are Mazu/Istio ratios where they differ by >25%")
    fig.tight_layout()
    for out in out_paths:
        fig.savefig(out, dpi=150)
    plt.close(fig)


def print_table(data, strategies, metric: str) -> None:
    """Echo the plotted numbers so a run can be checked without opening a PDF."""
    strategies = order_strategies(strategies)
    unit = "cores" if metric == "cpu" else "GiB"
    div = 1.0 if metric == "cpu" else float(1024 ** 3)
    print(f"\n{metric} ({unit}), fleet-wide totals:")
    header = f"  {'rps':>7} {'component':<16}" + "".join(
        f" {display_name(s):>10}" for s in strategies)
    if "istio" in strategies and "st5-AttUpd" in strategies:
        header += f" {'ratio':>7}"
    print(header)
    for rps in sorted(data, key=rps_key):
        for comp in COMPONENTS:
            vals = [data[rps].get(s, {}).get(metric, {}).get(comp)
                    for s in strategies]
            if all(v is None for v in vals):
                continue
            row = f"  {rps:>7} {comp:<16}" + "".join(
                f" {'n/a':>10}" if v is None else f" {v / div:>10.3f}"
                for v in vals)
            if "istio" in strategies and "st5-AttUpd" in strategies:
                base = data[rps].get("istio", {}).get(metric, {}).get(comp)
                mazu = data[rps].get("st5-AttUpd", {}).get(metric, {}).get(comp)
                row += (f" {mazu / base:>6.2f}x"
                        if base and mazu is not None else f" {'-':>7}")
            print(row)


# ===========================================================================
# .dat export
#
# Written from the same `data` dict the bars are drawn from, so the file is
# the figure in text form rather than a second derivation that can drift.
# Unlike the figure, the unit is fixed for the whole file (see below): a panel
# free to pick MiB or GiB is right for reading one component, wrong for
# comparing two columns in a text file.
# ===========================================================================

# Memory is stored in bytes and plotted per-panel in MiB or GiB; the .dat is
# MiB throughout so every row stays directly comparable.
DAT_MEM_DIV = float(1024 ** 2)

DAT_DOC = """\
Data behind plot_sn_{stem}.pdf/.png -- one row per (RPS step, component).

SOURCE     <run-dir>/<strategy>/metrics_<rps>.json, written by
           collect_metrics_sn.sh: the `totals.{metric}.<component>` block, a
           Prometheus instant vector holding ONE fleet-wide scalar per
           component, already averaged over that load step's window.
TRANSFORM  the scalar is taken as-is{conv}
           The per-pod `{metric}.<component>` query_range matrices in the same
           file are deliberately NOT used: under HPA the pod count is a
           dependent variable that differs between arms and between steps, so
           a per-pod average flatters whichever arm ran more replicas even
           though the cluster is paying more. The fleet-wide sum is the only
           cross-arm-comparable number. (Same reasoning as the "WHY THE TOTALS
           BLOCK" section of collect_metrics_sn.sh.)
           ratio = Mazu / Istio, and is N/A unless both arms measured that
           step and the Istio value is non-zero.
           A component whose Prometheus query failed at collection time comes
           back as an empty result and is written N/A -- never as 0.
UNITS      {units}
NOTE       kube_apiserver is a CLUSTER-WIDE background cost. It is reported for
           completeness but, unlike the other four components, is not
           attributable to this workload.
LAYOUT     one row per (RPS step, component); one column per strategy, then
           the ratio. Rows where no arm has a value are omitted.
"""


def write_dat(path: Path, doc: str, header: list, rows: list) -> Path:
    """Write one tab-separated gnuplot .dat: comment block, header, rows.

    The header line is '#'-prefixed (a gnuplot comment, matching
    generate_dat.py) so the file plots directly with no skip-row argument.
    None -> "N/A", keeping a failed query distinct from a measured zero the
    same way the figure keeps a gap distinct from a zero-height bar.
    """
    with path.open("w") as f:
        for line in doc.strip("\n").splitlines():
            f.write(("# " + line).rstrip() + "\n")
        f.write("# " + "\t".join(str(h) for h in header) + "\n")
        for row in rows:
            f.write("\t".join("N/A" if v is None else str(v) for v in row) + "\n")
    return path


def write_metric_dat(data, strategies, metric: str, out: Path) -> Path:
    strategies = order_strategies(strategies)
    if metric == "cpu":
        div, digits = 1.0, 6
        units, conv = "CPU in cores.", "."
    else:
        div, digits = DAT_MEM_DIV, 2
        units = "memory (working set) in MiB."
        conv = (", then converted from bytes to MiB\n"
                "           (/1024^2). The figure picks MiB or GiB per panel;\n"
                "           this file is MiB throughout, so every row stays\n"
                "           directly comparable.")

    header = ["rps", "component"] + [display_name(s) for s in strategies] \
        + ["ratio_mazu_over_istio"]
    rows = []
    for rps in sorted(data, key=rps_key):
        for comp in COMPONENTS:
            vals = [data[rps].get(s, {}).get(metric, {}).get(comp)
                    for s in strategies]
            if all(v is None for v in vals):
                continue
            base = data[rps].get("istio", {}).get(metric, {}).get(comp)
            mazu = data[rps].get("st5-AttUpd", {}).get(metric, {}).get(comp)
            ratio = mazu / base if base and mazu is not None else None
            rows.append([rps, comp]
                        + [None if v is None else f"{v / div:.{digits}f}"
                           for v in vals]
                        + [None if ratio is None else f"{ratio:.3f}"])
    doc = DAT_DOC.format(stem="cpu" if metric == "cpu" else "memory",
                         metric=metric, units=units, conv=conv)
    return write_dat(out, doc, header, rows)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    data, strategies = collect(run_dir)
    if not data or not strategies:
        print(f"error: no usable metrics_*.json under {run_dir}/*/ "
              f"(run collect_metrics_sn.sh first)", file=sys.stderr)
        return 1

    missing = [s for s in STRATEGY_ORDER if s not in strategies]
    if missing:
        print(f"warning: no metrics for {', '.join(missing)} -- plotting "
              f"{', '.join(display_name(s) for s in strategies)} only",
              file=sys.stderr)
    if len(data) < 3:
        print(f"note: only {len(data)} RPS step(s) present "
              f"({', '.join(sorted(data, key=rps_key))}); the comparison is "
              f"thin until the full sweep has run", file=sys.stderr)

    written = []
    for metric, stem in (("cpu", "plot_sn_cpu"), ("memory", "plot_sn_memory")):
        if all(v is None
               for rps in data for s in data[rps]
               for v in data[rps][s].get(metric, {}).values()):
            print(f"warning: no {metric} totals anywhere -- skipping {stem}",
                  file=sys.stderr)
            continue
        outs = [run_dir / f"{stem}.pdf", run_dir / f"{stem}.png"]
        plot_metric(data, strategies, metric, outs)
        # Same values as the bars -- see the ".dat export" section above.
        dat = write_metric_dat(data, strategies, metric,
                               run_dir / f"{stem}.dat")
        written.extend(outs + [dat])
        print_table(data, strategies, metric)

    if not written:
        print("error: nothing to plot", file=sys.stderr)
        return 1

    print()
    for out in written:
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
