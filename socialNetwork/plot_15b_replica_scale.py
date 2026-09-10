#!/usr/bin/env python3
"""Plot Istio vs Mazu across replica scales for a 1.5b replica-scale sweep.

Reads the tree written by run-benchmark1.5b-replica-scale-sweep.sh:

    <run-dir>/scale-1x/<strategy>/<rps>.txt      raw wrk2 output
    <run-dir>/scale-2x/<strategy>/<rps>.txt
    ...

and produces under <run-dir>:

  - plot_15b_replica_scale_latency.pdf/.png  one panel per scale, p50/p90/p99
        vs RPS, Istio against Mazu. All panels share one log y axis, which is
        the whole point: the 1x and 16x panels are only comparable if the axis
        does not silently rescale under each one.
  - plot_15b_replica_scale_p99.pdf/.png      one panel per strategy, p99 vs
        RPS with one line per replica scale. This is the "did replication
        help, and did it help both meshes equally" view -- the same numbers as
        the first figure, transposed, because reading a trend ACROSS panels is
        something eyes are bad at.
  - plot_15b_replica_scale_ratio.pdf/.png    Mazu/Istio p99 as a heatmap over
        (scale x RPS), annotated with the ratio. The headline number: does
        Mazu's overhead over Istio shrink, hold or grow as the fleet is
        replicated out.
  - plot_15b_replica_scale.dat               every number behind all three
        figures, tab-separated with a '#' header, so the run is checkable and
        re-plottable without this script.

WHY SHORTFALL IS DRAWN ALONGSIDE THE PERCENTILES
  Same reason as plot_sn_latency.py, and it matters MORE here because this
  sweep goes to 2000 RPS. wrk2's percentiles describe only the requests that
  came back. When the client cannot push the target rate -- saturated harness,
  saturated mesh, or a fleet too small for the offered load -- the steps that
  never left the client are simply absent from the histogram, so a step can
  report an excellent p99 while a third of the offered load never happened,
  with zero timeouts and zero non-2xx to show for it.

  So every step's achieved rate (requests / elapsed) is compared with its
  target, and any step short by more than FLAG_FRAC of the offered load is
  ringed in red in the latency figures and excluded from nothing -- it is
  still plotted, just marked. A ringed point is not a latency measurement you
  can quote on its own.

UNITS
  wrk2 emits us/ms/s AND m (it switches to minutes past ~60s, e.g. "0.95m").
  Everything is normalised to ms. Reading "1.14s" as a bare number puts the
  worst latency in a run at the best position on the axis -- a bug this repo
  has been bitten by before.

Scales and strategies are discovered from the directory names, so a partial
run (16x never finished, or a single-strategy run) plots whatever is on disk
and warns on stderr about the rest. Exits non-zero only when there is nothing
at all to plot.

Usage: ./plot_15b_replica_scale.py <benchmark-run-dir>
"""

import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import ScalarFormatter

# Stable across every plot in this repo family, so Istio and Mazu keep the same
# colour whichever script drew the figure (plot_sn_latency.py, plot_sn_pods.py,
# plot_sn_resources.py).
STRATEGY_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
STRATEGY_COLORS = {"istio": "#77AADD", "st5-AttUpd": "#EE8866"}
STRATEGY_ORDER = ["istio", "st5-AttUpd"]
FALLBACK_COLORS = ["#EEDD88", "#44BB99", "#FFAABB", "#BBCC33",
                   "tab:pink", "tab:olive", "tab:cyan"]

# Percentile -> (line style, marker). Colour separates meshes, stroke
# separates percentiles.
PCTS = [("p50", ":", "o"), ("p90", "--", "s"), ("p99", "-", "^")]

# A step is ringed once this share of the offered load never completed. 1% is
# well above run-to-run noise and well below the tens of percent seen when a
# sweep saturates.
FLAG_FRAC = 0.01
FLAG_LABEL = f"\u2265{FLAG_FRAC * 100:g}% of offered load never completed"

SCALE_DIR_RE = re.compile(r"^scale-(\d+)x$")
# wrk2 percentile lines: " 99.000%    1.14s " / " 50.000%   19.50ms".
# Alternation order matters: "us"/"ms" must be tried before bare "s"/"m".
PCT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)%\s+([\d.]+)\s*(us|ms|s|m)\s*$")
# " 28719 requests in 5.00m, 215.79MB read"
REQS_RE = re.compile(r"^\s*(\d+)\s+requests in\s+([\d.]+)\s*(us|ms|s|m)\b")
TIMEOUT_RE = re.compile(r"Socket errors:.*?\btimeout\s+(\d+)")
NON2XX_RE = re.compile(r"Non-2xx or 3xx responses:\s*(\d+)")

_UNIT_MS = {"us": 1e-3, "ms": 1.0, "s": 1e3, "m": 6e4}


def to_ms(value: str, unit: str) -> float:
    """wrk2 duration -> milliseconds. See the module docstring on units."""
    return float(value) * _UNIT_MS[unit]


def parse_wrk(path: Path) -> dict:
    """Parse one raw wrk2 output file into a step dict.

    wrk2 OMITS the whole "Socket errors" line when nothing timed out, so an
    absent line means zero, not unknown. Same for "Non-2xx or 3xx responses".
    """
    out = {"p50": None, "p90": None, "p99": None,
           "requests": None, "duration_s": None, "timeouts": 0, "non_2xx": 0}
    pcts = {}
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        print(f"warning: cannot read {path}: {e}", file=sys.stderr)
        return out

    for line in text.splitlines():
        m = PCT_RE.match(line)
        if m:
            # The "Detailed Percentile spectrum" block below is 4 columns and
            # never matches this 2-column pattern; only the summary
            # "Latency Distribution" block lands here.
            pcts[float(m.group(1))] = to_ms(m.group(2), m.group(3))
            continue
        m = REQS_RE.match(line)
        if m:
            out["requests"] = int(m.group(1))
            out["duration_s"] = to_ms(m.group(2), m.group(3)) / 1000.0
            continue
        m = TIMEOUT_RE.search(line)
        if m:
            out["timeouts"] = int(m.group(1))
            continue
        m = NON2XX_RE.search(line)
        if m:
            out["non_2xx"] = int(m.group(1))

    for key, want in (("p50", 50.0), ("p90", 90.0), ("p99", 99.0)):
        out[key] = pcts.get(want)
    return out


def derive_load(step: dict, target_rps: int) -> dict:
    """Add offered / achieved / shortfall to a step, in place.

    offered  = target rate x the elapsed time wrk2 actually reports, NOT the
               configured DURATION: a step that ended early must not be
               charged for load it was never asked to send.
    shortfall= offered - completed, as a fraction. Flagging on this rather
               than on wrk2's timeout counter is deliberate -- that counter is
               per socket EVENT, not per request, and is blind to the
               saturation case where nothing failed because nothing was sent.
    """
    step["target_rps"] = target_rps
    reqs, dur = step["requests"], step["duration_s"]
    if reqs is None or not dur:
        step["offered"] = step["achieved_rps"] = step["shortfall_frac"] = None
        return step
    offered = target_rps * dur
    step["offered"] = offered
    step["achieved_rps"] = reqs / dur
    step["shortfall_frac"] = max(0.0, (offered - reqs) / offered) if offered else None
    return step


def collect(run_dir: Path) -> dict:
    """-> {scale: {strategy: {rps: step}}} for everything on disk."""
    data = {}
    scale_dirs = []
    for child in sorted(run_dir.iterdir()):
        if not child.is_dir():
            continue
        m = SCALE_DIR_RE.match(child.name)
        if m:
            scale_dirs.append((int(m.group(1)), child))
    if not scale_dirs:
        return data

    for scale, sdir in sorted(scale_dirs):
        per_strategy = {}
        for strat_dir in sorted(p for p in sdir.iterdir() if p.is_dir()):
            if strat_dir.name == "manifests":
                continue
            steps = {}
            for f in sorted(strat_dir.glob("*.txt")):
                if not f.stem.isdigit():
                    continue  # run.log, sidecar dumps, .failed_* markers
                step = derive_load(parse_wrk(f), int(f.stem))
                if step["p99"] is None and step["requests"] is None:
                    print(f"warning: no usable wrk2 data in {f}", file=sys.stderr)
                    continue
                steps[int(f.stem)] = step
            if steps:
                per_strategy[strat_dir.name] = steps
            else:
                print(f"warning: no wrk2 output under {strat_dir}", file=sys.stderr)
        if per_strategy:
            data[scale] = per_strategy
        else:
            print(f"warning: scale {scale}x has no results, skipping",
                  file=sys.stderr)
    return data


def label_of(strat: str) -> str:
    return STRATEGY_LABELS.get(strat, strat)


def order_strategies(data: dict) -> list:
    """Known strategies first in their canonical order, then anything else."""
    seen = []
    for per_strategy in data.values():
        for s in per_strategy:
            if s not in seen:
                seen.append(s)
    known = [s for s in STRATEGY_ORDER if s in seen]
    return known + sorted(s for s in seen if s not in known)


def color_of(strat: str, extras: list) -> str:
    if strat in STRATEGY_COLORS:
        return STRATEGY_COLORS[strat]
    return FALLBACK_COLORS[extras.index(strat) % len(FALLBACK_COLORS)]


def all_rps(data: dict) -> list:
    rps = set()
    for per_strategy in data.values():
        for steps in per_strategy.values():
            rps.update(steps.keys())
    return sorted(rps)


def y_limits(data: dict):
    """One (lo, hi) for every latency panel in every figure.

    Per-panel autoscaling would let a 16x panel and a 1x panel look alike
    while differing by an order of magnitude, which is exactly the comparison
    this run exists to make.
    """
    vals = [v for per_strategy in data.values()
            for steps in per_strategy.values()
            for step in steps.values()
            for v in (step["p50"], step["p90"], step["p99"])
            if v is not None and v > 0]
    if not vals:
        return None
    return min(vals) * 0.6, max(vals) * 1.8


def style_rps_axis(ax, xs) -> None:
    """Log x with one labelled tick per swept RPS.

    The sweep is geometric-ish (100..2000), so on a linear axis the low steps
    pile into one another -- "100200400" -- and the four low-load steps that
    carry the interesting pre-saturation behaviour become unreadable.
    """
    ax.set_xscale("log")
    ax.set_xticks(xs)
    ax.set_xticks([], minor=True)
    ax.get_xaxis().set_major_formatter(ScalarFormatter())
    ax.tick_params(axis="x", labelsize=8, rotation=45)
    ax.tick_params(axis="y", labelsize=8)


def save(fig, run_dir: Path, stem: str) -> list:
    outs = []
    for ext in ("pdf", "png"):
        out = run_dir / f"{stem}.{ext}"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        outs.append(out)
    plt.close(fig)
    return outs


def plot_latency_panels(data: dict, run_dir: Path) -> list:
    """One panel per replica scale: p50/p90/p99 vs RPS, Istio against Mazu."""
    scales = sorted(data)
    strategies = order_strategies(data)
    extras = [s for s in strategies if s not in STRATEGY_COLORS]
    ncols = min(len(scales), 3)
    nrows = (len(scales) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.6 * ncols, 3.9 * nrows),
                             sharex=True, sharey=True, squeeze=False)
    flat = [ax for row in axes for ax in row]
    lims = y_limits(data)
    xs = all_rps(data)

    # One legend for the figure, built from the first panel only -- every panel
    # draws the same series, so labelling them all would repeat each entry
    # once per scale.
    labelled = set()

    def once(key):
        """Return `key` as a legend label the first time it is asked for."""
        if key in labelled:
            return None
        labelled.add(key)
        return key

    for ax, scale in zip(flat, scales):
        first_panel = ax is flat[0]
        for strat in strategies:
            steps = data[scale].get(strat)
            if not steps:
                continue
            rps = sorted(steps)
            color = color_of(strat, extras)
            for pct, ls, marker in PCTS:
                ys = [steps[r][pct] for r in rps]
                if all(y is None for y in ys):
                    continue
                ax.plot(rps, ys, ls, marker=marker, color=color, markersize=4,
                        linewidth=1.8,
                        label=once(f"{label_of(strat)} {pct}")
                              if first_panel else None)
            # Ring the steps whose percentiles describe only part of the load.
            flagged = [r for r in rps
                       if (steps[r]["shortfall_frac"] or 0) >= FLAG_FRAC
                       and steps[r]["p99"] is not None]
            if flagged:
                ax.scatter(flagged, [steps[r]["p99"] for r in flagged],
                           s=110, facecolors="none", edgecolors="#CC3311",
                           linewidths=1.4, zorder=5,
                           label=once(FLAG_LABEL) if first_panel else None)
        ax.set_yscale("log")
        ax.set_title(f"{scale}x replicas", fontsize=11)
        ax.grid(True, which="both", axis="y", alpha=0.25)
        style_rps_axis(ax, xs)
        if lims:
            ax.set_ylim(*lims)

    for ax in flat[len(scales):]:
        ax.set_visible(False)
    for row in axes:
        row[0].set_ylabel("Latency (ms)")
    # sharex hides the tick labels on every panel that has one below it, so
    # the x label goes on the lowest VISIBLE panel of each column -- otherwise
    # a top-row panel carries an axis title over unlabelled ticks.
    for col in range(ncols):
        bottom = [r for r in range(nrows)
                  if r * ncols + col < len(scales)]
        if bottom:
            ax = axes[bottom[-1]][col]
            ax.set_xlabel("Target RPS")
            # sharex hides tick labels by GRID position, not by visibility, so
            # the last column of a partly-filled last row keeps its labels
            # hidden by a panel that was never drawn. Put them back.
            ax.xaxis.set_tick_params(labelbottom=True)

    handles, labels = flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, frameon=False,
               fontsize=9, bbox_to_anchor=(0.5, -0.06))
    fig.suptitle("Bookinfo (HPA off): latency vs load at each replica scale",
                 fontsize=13)
    fig.tight_layout(rect=(0, 0.01, 1, 0.96))
    return save(fig, run_dir, "plot_15b_replica_scale_latency")


def plot_p99_by_scale(data: dict, run_dir: Path) -> list:
    """One panel per strategy: p99 vs RPS, one line per replica scale."""
    scales = sorted(data)
    strategies = order_strategies(data)
    fig, axes = plt.subplots(1, len(strategies),
                             figsize=(5.2 * len(strategies), 4.0),
                             sharex=True, sharey=True, squeeze=False)
    flat = [ax for row in axes for ax in row]
    lims = y_limits(data)
    xs = all_rps(data)
    # Light -> dark with increasing replica count, so "more replicas" reads as
    # "darker" in both panels without needing the legend.
    cmap = plt.cm.get_cmap("viridis")
    shades = {s: cmap(i / max(1, len(scales) - 1) * 0.85)
              for i, s in enumerate(scales)}

    for ax, strat in zip(flat, strategies):
        for scale in scales:
            steps = data[scale].get(strat)
            if not steps:
                continue
            rps = sorted(steps)
            ys = [steps[r]["p99"] for r in rps]
            if all(y is None for y in ys):
                continue
            ax.plot(rps, ys, "-", marker="o", markersize=4, linewidth=1.8,
                    color=shades[scale], label=f"{scale}x")
            flagged = [r for r in rps
                       if (steps[r]["shortfall_frac"] or 0) >= FLAG_FRAC
                       and steps[r]["p99"] is not None]
            if flagged:
                ax.scatter(flagged, [steps[r]["p99"] for r in flagged],
                           s=110, facecolors="none", edgecolors="#CC3311",
                           linewidths=1.4, zorder=5)
        ax.set_yscale("log")
        ax.set_title(label_of(strat), fontsize=12,
                     color=STRATEGY_COLORS.get(strat, "black"))
        ax.set_xlabel("Target RPS")
        style_rps_axis(ax, xs)
        ax.grid(True, which="both", axis="y", alpha=0.25)
        if lims:
            ax.set_ylim(*lims)
    flat[0].set_ylabel("p99 latency (ms)")
    flat[0].legend(title="replicas", fontsize=8, title_fontsize=8, ncol=2)
    fig.suptitle("p99 latency as the fleet is replicated out "
                 f"(red ring: {FLAG_LABEL})", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return save(fig, run_dir, "plot_15b_replica_scale_p99")


def plot_ratio(data: dict, run_dir: Path) -> list:
    """Mazu/Istio p99 over (scale x RPS). The headline comparison.

    Drawn only when both arms ran; a one-arm sweep has no ratio to show.
    """
    strategies = order_strategies(data)
    if len(strategies) < 2:
        print("note: fewer than two strategies present, skipping the ratio "
              "heatmap", file=sys.stderr)
        return []
    base, other = strategies[0], strategies[1]

    scales = sorted(data)
    xs = all_rps(data)
    grid = np.full((len(scales), len(xs)), np.nan)
    for i, scale in enumerate(scales):
        for j, rps in enumerate(xs):
            a = data[scale].get(base, {}).get(rps, {}).get("p99")
            b = data[scale].get(other, {}).get(rps, {}).get("p99")
            if a and b:
                grid[i, j] = b / a

    if np.all(np.isnan(grid)):
        print("note: no scale has both arms at the same RPS, skipping the "
              "ratio heatmap", file=sys.stderr)
        return []

    fig, ax = plt.subplots(figsize=(1.0 * len(xs) + 3.2, 0.75 * len(scales) + 2.4))
    # Centred on 1.0 so "no overhead" is the neutral colour and the two
    # directions are visually distinct.
    finite = grid[np.isfinite(grid)]
    span = max(abs(np.log2(finite)).max(), 0.2)
    im = ax.imshow(np.log2(grid), cmap="coolwarm", vmin=-span, vmax=span,
                   aspect="auto")
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels(xs)
    ax.set_yticks(range(len(scales)))
    ax.set_yticklabels([f"{s}x" for s in scales])
    ax.set_xlabel("Target RPS")
    ax.set_ylabel("Replica scale")
    ax.set_title(f"{label_of(other)} / {label_of(base)} p99 latency\n"
                 f"(1.00 = parity, >1 = {label_of(other)} slower)", fontsize=11)
    for i in range(len(scales)):
        for j in range(len(xs)):
            v = grid[i, j]
            ax.text(j, i, "n/a" if not np.isfinite(v) else f"{v:.2f}",
                    ha="center", va="center", fontsize=8,
                    color="black" if not np.isfinite(v)
                    or abs(np.log2(v)) < span * 0.6 else "white")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("log2 ratio")
    fig.tight_layout()
    return save(fig, run_dir, "plot_15b_replica_scale_ratio")


DAT_DOC = f"""\
Istio-vs-Mazu latency across replica scales.
Written by plot_15b_replica_scale.py; the numbers behind
plot_15b_replica_scale_latency / _p99 / _ratio.

source        <run-dir>/scale-<N>x/<strategy>/<rps>.txt (raw wrk2 output)
scale         replica multiplier over the stock Bookinfo manifest
              (stock = 6 app pods, so 4x = 24 app pods, HPA off)
p50/p90/p99   wrk2 "Latency Distribution", normalised to ms from us/ms/s/m
offered       target_rps * elapsed_s (wrk2's own elapsed, not the configured
              step duration)
completed     wrk2 "N requests in ..."
achieved_rps  completed / elapsed_s
shortfall_pct 100 * (offered - completed) / offered -- load that never
              completed and is therefore ABSENT from the percentiles above
flagged       shortfall_pct >= {FLAG_FRAC * 100:g}; a flagged step's percentiles
              describe only part of the offered load and cannot be quoted alone
N/A           not measured. Never read it as a zero.
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


def write_scale_dat(data: dict, out: Path) -> Path:
    header = ["scale", "strategy", "target_rps", "p50_ms", "p90_ms", "p99_ms",
              "offered", "completed", "achieved_rps", "shortfall_pct",
              "timeouts", "non_2xx", "flagged"]
    rows = []

    def f(v, nd=2):
        return None if v is None else round(v, nd)

    for scale in sorted(data):
        for strat in order_strategies({scale: data[scale]}):
            steps = data[scale].get(strat, {})
            for rps in sorted(steps):
                s = steps[rps]
                sf = s["shortfall_frac"]
                rows.append([
                    f"{scale}x", label_of(strat), rps,
                    f(s["p50"]), f(s["p90"]), f(s["p99"]),
                    f(s["offered"], 0), s["requests"], f(s["achieved_rps"], 1),
                    None if sf is None else round(sf * 100, 3),
                    s["timeouts"], s["non_2xx"],
                    None if sf is None else ("yes" if sf >= FLAG_FRAC else "no"),
                ])
    return write_dat(out, DAT_DOC, header, rows)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    data = collect(run_dir)
    if not data:
        print(f"error: no scale-<N>x/<strategy>/<rps>.txt results under "
              f"{run_dir}", file=sys.stderr)
        return 1

    outs = []
    outs += plot_latency_panels(data, run_dir)
    outs += plot_p99_by_scale(data, run_dir)
    outs += plot_ratio(data, run_dir)
    outs.append(write_scale_dat(data, run_dir / "plot_15b_replica_scale.dat"))

    for scale in sorted(data):
        for strat in order_strategies({scale: data[scale]}):
            for rps in sorted(data[scale].get(strat, {})):
                s = data[scale][strat][rps]
                fmt = lambda v: "n/a" if v is None else f"{v:.2f}ms"
                sf = s["shortfall_frac"]
                sfs = "n/a" if sf is None else f"{sf:.2%}"
                flag = "  <-- FLAGGED" if sf is not None and sf >= FLAG_FRAC else ""
                print(f"  {scale:>3}x  {label_of(strat):<6} @ {rps:>5} RPS  "
                      f"p50={fmt(s['p50']):>10} p90={fmt(s['p90']):>10} "
                      f"p99={fmt(s['p99']):>11}  "
                      f"never_completed={sfs:>7} of offered  "
                      f"timeouts={s['timeouts']} non_2xx={s['non_2xx']}{flag}")
    for o in outs:
        print(f"wrote {o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
