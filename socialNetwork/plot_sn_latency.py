#!/usr/bin/env python3
"""Plot Istio-vs-Mazu request latency for a run-socialnetwork-strategies.sh sweep.

Produces under <run-dir>:
  - plot_sn_latency.pdf / .png   two stacked panels sharing the RPS axis:

    TOP    p50 / p90 / p99 response time vs target RPS. One colour per
           strategy (stable across every plot in this repo family), one line
           style per percentile.
    BOTTOM what those percentiles LEFT OUT: timed-out requests as a share of
           the offered load, grouped bars per strategy, with non-2xx overlaid.

WHY THE SECOND PANEL EXISTS -- read this before quoting a p99 from the top one.

TIMED-OUT REQUESTS ARE NOT IN WRK2'S LATENCY HISTOGRAM. wrk2 drops any request
that exceeds the socket timeout before it ever reaches the HdrHistogram, so
every percentile above describes only the requests that CAME BACK. A step can
therefore report an excellent p99 while a large fraction of the offered load
never completed at all, and non_2xx stays ~0 alongside it because a timeout is
not an HTTP status -- there is no response to carry a status code.

This is not hypothetical. In the very fixture this script was developed
against, BOTH arms at 100 RPS report a tidy-looking p99 (istio 201.98ms, Mazu
225.66ms) while the raw wrk2 output logs 4237 and 4281 timeouts respectively
against ~30000 offered requests. Roughly one request in seven vanished, and a
latency-only plot would have shown that step as the healthiest in the sweep.

It is load-bearing for the Istio-vs-Mazu comparison specifically: a mesh that
degrades by TIMING OUT rather than by returning errors scores equal-or-better
on every latency column. So the two panels are drawn together, share an x
axis, and any step where timeouts exceed FLAG_FRAC of the offered load is
shaded in both panels and ringed in red. You cannot read a percentile here
without seeing how much load it excludes.

Percentiles are read from the raw wrk2 output (<strategy>/<rps>.txt) because
summary.csv only carries p50 and p99; p90 is where the Mazu tail first opens
up. summary.csv supplies the step list, timeouts and non-2xx, and is used as a
latency fallback when a raw file is missing.

Unit handling on the raw files is load-bearing in its own right: wrk2 emits
us / ms / s AND m (it switches to minutes past ~60s, e.g. "0.95m"). Everything
is normalised to ms. Reading "1.14s" or "0.95m" as a bare number puts the
WORST latency in a run at the BEST position on the axis -- a bug this repo has
already been bitten by.

Strategies are discovered from the subdirectory names rather than hardcoded.
Missing strategy dirs, missing per-RPS files, sentinel failure rows and partial
runs all warn to stderr and plot whatever exists.

Usage: ./plot_sn_latency.py <benchmark-run-dir>
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

# Stable across every plot in this repo family, so Istio and Mazu keep the same
# colour whichever script drew the figure. The order also fixes bar/line order.
STRATEGY_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
# Paul-Tol palette, matching plot_sn_pods.py, plot_sn_resources.py and
# generate_timeseries_plots.py -- one color per arm across the whole repo.
STRATEGY_COLORS = {"istio": "#77AADD", "st5-AttUpd": "#EE8866"}
STRATEGY_ORDER = ["istio", "st5-AttUpd"]
FALLBACK_COLORS = ["#EEDD88", "#44BB99", "#FFAABB", "#BBCC33",
                   "tab:pink", "tab:olive", "tab:cyan"]

# Percentile -> (line style, marker). One style per percentile, so a reader
# separates strategies by colour and percentiles by stroke.
PCTS = [("p50", ":", "o"), ("p90", "--", "s"), ("p99", "-", "^")]

# A step is flagged -- shaded and ringed -- once this share of the offered load
# timed out. 1% is well above run-to-run noise and well below the ~14% seen in
# the fixture, so it separates "a few stragglers" from "the percentiles are
# describing a minority of the traffic".
FLAG_FRAC = 0.01

# wrk2 percentile lines: " 99.000%    1.14s " / " 50.000%   19.50ms".
# Alternation order matters: "us" and "ms" must be tried before bare "s"/"m".
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
    """Parse one raw wrk2 output file.

    -> {"pcts": {50.0: ms, ...}, "requests": int|None, "duration_s": float|None,
        "timeouts": int, "non_2xx": int}

    wrk2 OMITS the whole "Socket errors" line when nothing timed out, so an
    absent line means zero, not unknown. Same for "Non-2xx or 3xx responses".
    """
    out = {"pcts": {}, "requests": None, "duration_s": None,
           "timeouts": 0, "non_2xx": 0}
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        print(f"warning: cannot read {path}: {e}", file=sys.stderr)
        return out

    for line in text.splitlines():
        m = PCT_RE.match(line)
        if m:
            # The "Detailed Percentile spectrum" block below is 4 columns, so
            # it never matches this 2-column pattern. Only the summary
            # "Latency Distribution" block lands here.
            out["pcts"][float(m.group(1))] = to_ms(m.group(2), m.group(3))
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
    return out


def _num(s):
    """CSV cell -> float, or None if blank/non-numeric (sentinel rows)."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_summary(run_dir: Path) -> dict:
    """-> {(strategy, rps): row-dict}, skipping sentinel failure rows.

    On failure the runner appends rows whose rps_target is a status string and
    whose remaining columns are empty, e.g.
        st5-AttUpd,MESH_INSTALL_FAILED,,,,,,,
    Those carry no measurement, so they are reported and dropped. Older runs
    have an 8-column header with no `timeouts`; that column then reads as None
    and the timeout panel falls back to the raw wrk2 output.
    """
    path = run_dir / "summary.csv"
    data: dict[tuple[str, int], dict] = {}
    if not path.is_file():
        print(f"warning: no {path}; falling back to raw wrk2 output only",
              file=sys.stderr)
        return data

    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            strat = (row.get("strategy") or "").strip()
            rps = _num(row.get("rps_target"))
            if not strat:
                continue
            if rps is None:
                status = (row.get("rps_target") or "?").strip()
                print(f"warning: skipping sentinel row {strat},{status}",
                      file=sys.stderr)
                continue
            data[(strat, int(rps))] = {
                "rps_delivered": _num(row.get("rps_delivered")),
                "p50": _num(row.get("p50_ms")),
                "p99": _num(row.get("p99_ms")),
                "non_2xx": _num(row.get("non_2xx")),
                # Absent column (old 8-col header) -> None -> use the raw file.
                "timeouts": _num(row.get("timeouts")),
            }
    return data


def collect(run_dir: Path) -> dict:
    """-> {strategy: {rps: step-dict}} merged from summary.csv and raw wrk2."""
    summary = read_summary(run_dir)
    steps: dict[str, dict[int, dict]] = defaultdict(dict)

    strat_dirs = sorted(p for p in run_dir.iterdir() if p.is_dir()) \
        if run_dir.is_dir() else []
    known = {s for s, _ in summary}
    for s in sorted(known - {p.name for p in strat_dirs}):
        print(f"warning: {run_dir/s}/ missing; using summary.csv only for {s}",
              file=sys.stderr)

    # Union of strategies seen on disk and in the summary, so a run that lost
    # its raw files still plots and vice versa.
    for strat in sorted({p.name for p in strat_dirs} | known):
        d = run_dir / strat
        rps_from_files = set()
        if d.is_dir():
            for p in d.glob("*.txt"):
                if p.stem.isdigit():
                    rps_from_files.add(int(p.stem))
        rps_from_csv = {r for s, r in summary if s == strat}
        if not rps_from_files and not rps_from_csv:
            continue

        for rps in sorted(rps_from_files | rps_from_csv):
            srow = summary.get((strat, rps), {})
            raw_path = d / f"{rps}.txt"
            if raw_path.is_file():
                raw = parse_wrk(raw_path)
            else:
                if rps in rps_from_csv:
                    print(f"warning: no {raw_path}; p90 unavailable for "
                          f"{strat} @ {rps} RPS", file=sys.stderr)
                raw = {"pcts": {}, "requests": None, "duration_s": None,
                       "timeouts": None, "non_2xx": None}

            step = {
                # Raw file first (it has p90); summary.csv as the fallback.
                "p50": raw["pcts"].get(50.0, srow.get("p50")),
                "p90": raw["pcts"].get(90.0),
                "p99": raw["pcts"].get(99.0, srow.get("p99")),
                "requests": raw["requests"],
                "duration_s": raw["duration_s"],
            }
            for k in ("timeouts", "non_2xx"):
                v = srow.get(k)
                if v is None:
                    v = raw[k]
                step[k] = None if v is None else int(v)

            # Offered load = target rate x measured step duration. This is the
            # honest denominator for "what fraction never came back": wrk2's
            # own request count already excludes the timed-out requests, so
            # using it alone would understate the loss.
            if step["duration_s"]:
                step["offered"] = rps * step["duration_s"]
            elif step["requests"] is not None and step["timeouts"]:
                step["offered"] = step["requests"] + step["timeouts"]
            else:
                step["offered"] = None

            to = step["timeouts"]
            step["timeout_frac"] = (to / step["offered"]
                                    if to is not None and step["offered"]
                                    else None)
            n2 = step["non_2xx"]
            step["non_2xx_frac"] = (n2 / step["offered"]
                                    if n2 is not None and step["offered"]
                                    else None)
            steps[strat][rps] = step
    return steps


def label_of(strat: str) -> str:
    return STRATEGY_LABELS.get(strat, strat)


def order_strategies(steps: dict) -> list:
    """Known strategies first, in the pinned order; then anything else."""
    known = [s for s in STRATEGY_ORDER if s in steps]
    return known + sorted(s for s in steps if s not in STRATEGY_ORDER)


def color_of(strat: str, extras: list) -> str:
    if strat in STRATEGY_COLORS:
        return STRATEGY_COLORS[strat]
    return FALLBACK_COLORS[extras.index(strat) % len(FALLBACK_COLORS)]


def plot(steps: dict, run_dir: Path, outputs: list) -> None:
    strategies = order_strategies(steps)
    extras = [s for s in strategies if s not in STRATEGY_COLORS]
    rps_values = sorted({r for st in steps.values() for r in st})
    # Categorical x: keeps grouped bars readable and spaces an uneven sweep
    # (100/200/400/800/...) evenly instead of crushing the low end.
    x = np.arange(len(rps_values))

    fig, (ax_lat, ax_to) = plt.subplots(
        2, 1, # Grow with the sweep but cap it: past ~8 steps an uncapped width
        # flattens the panels into unreadable strips.
        figsize=(min(15.0, max(9.5, 1.0 * len(rps_values) + 7.5)), 9.0),
        sharex=True, gridspec_kw={"height_ratios": [2.1, 1.0]})

    # --- steps where the percentiles above exclude a meaningful share of load
    flagged = set()
    for strat in strategies:
        for rps, s in steps[strat].items():
            if s["timeout_frac"] is not None and s["timeout_frac"] >= FLAG_FRAC:
                flagged.add(rps)
    for rps in sorted(flagged):
        i = rps_values.index(rps)
        for ax in (ax_lat, ax_to):
            ax.axvspan(i - 0.5, i + 0.5, color="tab:red", alpha=0.08, zorder=0)

    # ------------------------------- top panel -------------------------------
    any_latency = False
    for strat in strategies:
        c = color_of(strat, extras)
        for key, ls, mk in PCTS:
            xs, ys = [], []
            for i, rps in enumerate(rps_values):
                s = steps[strat].get(rps)
                if s and s.get(key) is not None:
                    xs.append(i)
                    ys.append(s[key])
            if not xs:
                continue
            any_latency = True
            ax_lat.plot(xs, ys, ls, marker=mk, color=c, linewidth=1.7,
                        markersize=5, label=f"{label_of(strat)} {key}")

        # Ring the p99 of every flagged step: this specific number is computed
        # over a minority of the offered load.
        rx, ry = [], []
        for i, rps in enumerate(rps_values):
            s = steps[strat].get(rps)
            if (s and s.get("p99") is not None
                    and s["timeout_frac"] is not None
                    and s["timeout_frac"] >= FLAG_FRAC):
                rx.append(i)
                ry.append(s["p99"])
        if rx:
            ax_lat.scatter(rx, ry, s=190, facecolors="none",
                           edgecolors="tab:red", linewidths=1.8, zorder=5)

    ax_lat.set_yscale("log")
    ax_lat.set_ylabel("response time (ms, log scale)")
    ax_lat.grid(alpha=0.3, which="both")
    ax_lat.set_title("SocialNetwork latency: completed requests only",
                     fontsize=12)
    # Headroom so the legend and the timeout callouts clear the p99 curve,
    # and so the p50 line does not sit on the bottom spine.
    lo, hi = ax_lat.get_ylim()
    ax_lat.set_ylim(lo / 1.5, hi * 4.0)
    handles, labels = ax_lat.get_legend_handles_labels()
    if flagged:
        handles.append(plt.Line2D([], [], marker="o", markersize=11,
                                  markerfacecolor="none",
                                  markeredgecolor="tab:red", linestyle="none"))
        labels.append(f"p99 excludes >{FLAG_FRAC:.0%} of offered load")
    ax_lat.legend(handles, labels, fontsize=8, ncol=max(1, len(strategies)),
                  loc="upper left")

    # Spell out the loss on every flagged step. One callout per step, listing
    # every strategy in it -- per-strategy callouts collide when both arms are
    # flagged at the same rate, which is the common case.
    for i, rps in enumerate(rps_values):
        lines, anchor = [], None
        for strat in strategies:
            s = steps[strat].get(rps)
            if not s or s["timeout_frac"] is None or s["timeout_frac"] < FLAG_FRAC:
                continue
            lines.append(f"{label_of(strat)}: {s['timeout_frac']:.1%} "
                         f"({s['timeouts']:,} reqs)")
            if s.get("p99") is not None:
                anchor = max(anchor or 0, s["p99"])
        if not lines or anchor is None:
            continue
        # Callouts on the right-hand steps point leftwards, or they run off
        # the axes on a long sweep.
        right = i > (len(rps_values) - 1) / 2
        ax_lat.annotate(
            "timed out, absent from these percentiles\n" + "\n".join(lines),
            xy=(i, anchor), xytext=(-10 if right else 10, 34),
            textcoords="offset points",
            fontsize=7.5, color="tab:red",
            ha="right" if right else "left", va="bottom",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor="tab:red", alpha=0.9, linewidth=0.8),
            arrowprops=dict(arrowstyle="->", color="tab:red", lw=0.9))

    # ------------------------------ bottom panel -----------------------------
    # Narrow the group when the sweep has few steps, otherwise two bars fill
    # the whole slot and read as a single block.
    group_w = 0.8 if len(rps_values) > 2 else 0.55
    width = group_w / max(len(strategies), 1)
    any_completion = False
    for j, strat in enumerate(strategies):
        c = color_of(strat, extras)
        off = j * width - group_w / 2 + width / 2
        to_pct, n2_pct, labels_txt = [], [], []
        for rps in rps_values:
            s = steps[strat].get(rps)
            tf = s["timeout_frac"] if s else None
            nf = s["non_2xx_frac"] if s else None
            to_pct.append(0.0 if tf is None else tf * 100)
            n2_pct.append(0.0 if nf is None else nf * 100)
            if s and s.get("timeouts") is not None:
                any_completion = True
                labels_txt.append(f"{s['timeouts']:,}")
            else:
                labels_txt.append("n/a")
        bars = ax_to.bar(x + off, to_pct, width, color=c,
                         label=f"{label_of(strat)} timeouts")
        # non-2xx stacked on top: at fixture scale (10 of 120000) it is a hair
        # line, which is exactly the point -- the loss is timeouts, not statuses.
        ax_to.bar(x + off, n2_pct, width, bottom=to_pct, color=c,
                  hatch="///", edgecolor="white", linewidth=0.0, alpha=0.85,
                  label=f"{label_of(strat)} non-2xx")
        for b, txt, tp in zip(bars, labels_txt, to_pct):
            ax_to.annotate(txt, (b.get_x() + b.get_width() / 2, tp),
                           xytext=(0, 2), textcoords="offset points",
                           ha="center", fontsize=7, color=c)

    ax_to.axhline(FLAG_FRAC * 100, color="tab:red", linestyle="--",
                  linewidth=1.0)
    ax_to.text(0.995, FLAG_FRAC * 100, f" {FLAG_FRAC:.0%} of offered load",
               transform=ax_to.get_yaxis_transform(), ha="right", va="bottom",
               fontsize=7, color="tab:red")
    ax_to.set_ylabel("% of offered load\nthat never completed")
    ax_to.set_xlabel("target rate (RPS)")
    ax_to.set_xticks(x)
    ax_to.set_xticklabels([str(r) for r in rps_values])
    ax_to.grid(alpha=0.3, axis="y")
    ax_to.legend(fontsize=8, ncol=max(1, len(strategies)), loc="upper right")
    ax_to.set_title("What the percentiles above leave out "
                    "(bar labels = timeout count)", fontsize=10)
    top = max(ax_to.get_ylim()[1], FLAG_FRAC * 100 * 1.6)
    ax_to.set_ylim(0, top * 1.25)

    fig.text(0.01, 0.005,
             "wrk2 drops timed-out requests before its histogram, so the top "
             "panel describes only completed requests; a low p99 over a tall "
             "bar below is a step that mostly did not answer. "
             "Offered load = target RPS x step duration.",
             fontsize=7.5, color="0.3", ha="left", va="bottom", wrap=True)

    fig.tight_layout(rect=(0, 0.035, 1, 1))
    for out in outputs:
        fig.savefig(out, dpi=150)
    plt.close(fig)

    if not any_latency:
        print("warning: no latency percentiles found; top panel is empty",
              file=sys.stderr)
    if not any_completion:
        print("warning: no timeout data found (old summary.csv and no raw "
              "wrk2 output?); bottom panel is empty", file=sys.stderr)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <benchmark-run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    steps = collect(run_dir)
    plottable = {s: st for s, st in steps.items()
                 if any(v.get("p50") is not None or v.get("p99") is not None
                        or v.get("timeouts") is not None for v in st.values())}
    if not plottable:
        print(f"error: nothing to plot under {run_dir} (no summary.csv rows "
              f"and no <strategy>/<rps>.txt wrk2 output)", file=sys.stderr)
        return 1

    pdf_out = run_dir / "plot_sn_latency.pdf"
    png_out = run_dir / "plot_sn_latency.png"
    plot(plottable, run_dir, [pdf_out, png_out])

    for strat in order_strategies(plottable):
        for rps in sorted(plottable[strat]):
            s = plottable[strat][rps]
            fmt = lambda v: "n/a" if v is None else f"{v:.2f}ms"
            tf = ("n/a" if s["timeout_frac"] is None
                  else f"{s['timeout_frac']:.2%}")
            print(f"  {label_of(strat):<6} @ {rps:>5} RPS  "
                  f"p50={fmt(s['p50']):>10} p90={fmt(s['p90']):>10} "
                  f"p99={fmt(s['p99']):>11}  "
                  f"timeouts={s['timeouts']} ({tf} of offered)  "
                  f"non_2xx={s['non_2xx']}")
    print(f"wrote {pdf_out}")
    print(f"wrote {png_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
