#!/usr/bin/env python3
"""Plot Istio-vs-Mazu request latency for a run-socialnetwork-strategies.sh sweep.

Produces under <run-dir>:
  - plot_sn_latency.dat          the numbers behind both panels, tab-separated,
                                 with a comment block naming every source file
                                 and transform. Checkable without a PDF reader
                                 and re-plottable in gnuplot.
  - plot_sn_latency.pdf / .png   two stacked panels sharing the RPS axis:

    TOP    p50 / p90 / p99 response time vs target RPS. One colour per
           strategy (stable across every plot in this repo family), one line
           style per percentile.
    BOTTOM what those percentiles LEFT OUT: offered load that never completed,
           as a share of the offered load, grouped bars per strategy, with
           non-2xx overlaid.

WHY THE SECOND PANEL EXISTS -- read this before quoting a p99 from the top one.

UNCOMPLETED REQUESTS ARE NOT IN WRK2'S LATENCY HISTOGRAM. wrk2 drops any
request that exceeds the socket timeout before it ever reaches the
HdrHistogram, and a request the client never managed to send never existed to
begin with -- so every percentile above describes only the requests that CAME
BACK. A step can therefore report an excellent p99 while a large fraction of
the offered load never completed at all, and non_2xx stays ~0 alongside it
because neither failure carries an HTTP status.

This is not hypothetical, and it bites from BOTH ends of a sweep:

  low  RPS  wrk2's connection pool times out during warm-up. On the 09-03
            12:45 run both arms at 100 RPS report a tidy p99 (~205-210ms)
            while logging ~3400 timeouts against 24000 offered.
  high RPS  the harness saturates and simply cannot push the target rate. Same
            run at 1600 RPS: both arms delivered ~1270 RPS against 1600, so
            20-30% of the sweep never left the client -- with 0 timeouts and
            0 non-2xx, because nothing failed, it was never sent.

So the flag fires on SHORTFALL (offered - completed), not on wrk2's timeout
counter. That counter is per socket EVENT rather than per request, which makes
it both an unreliable magnitude -- completed + timeouts exceeded offered on the
100 RPS step above -- and blind to the saturation case entirely. Flagging on it
marked that run's two mildest steps and left its two most degraded ones clean.

It is load-bearing for the Istio-vs-Mazu comparison specifically: a mesh that
degrades by FAILING TO ANSWER rather than by returning errors scores
equal-or-better on every latency column. So the two panels are drawn together,
share an x axis, and any step whose shortfall exceeds FLAG_FRAC of the offered
load is shaded in both panels and ringed in red. You cannot read a percentile
here without seeing how much load it excludes.

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

# Vertical slots (axes fraction) for the flagged-step callouts, cycled in
# order. Three rows fit the band set_ylim reserves; a fourth would push the
# lowest row into the p99 curve on a flat sweep.
CALLOUT_SLOTS = (0.985, 0.86, 0.735)

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

            # SHORTFALL -- offered minus completed. This, not the timeout
            # count, is what the percentiles left out.
            #
            # Two independent reasons wrk2's timeout counter is the wrong
            # basis for the flag:
            #
            # 1. IT IS NOT A REQUEST COUNT. wrk2 increments it per socket
            #    timeout EVENT across the connection pool, so it can exceed
            #    the requests actually lost. On the 09-03 12:45 fixture,
            #    Istio at 100 RPS reported 22862 completed + 3412 timeouts
            #    against 24000 offered -- 2274 more than were ever sent. A
            #    "14.2% timed out" built on that denominator is not a
            #    fraction of anything.
            # 2. IT MISSES THE WORST STEPS ENTIRELY. When the harness cannot
            #    push the target rate the load simply never leaves the client:
            #    no socket times out, no HTTP status is returned, and the
            #    timeout counter stays at 0. Same fixture at 1600 RPS: both
            #    arms delivered ~1270 RPS against 1600 offered and lost 20-30%
            #    of the sweep, with 0 timeouts and 0 non-2xx -- so the old
            #    rule flagged the two mildest steps and left the two most
            #    degraded ones unmarked.
            #
            # offered - completed captures both, and it is the quantity the
            # bottom panel's axis has always claimed to show. Clamped at 0:
            # wrk2's own count can edge a few requests past target on a step
            # it fully served, and a negative loss is meaningless.
            if step["offered"] and step["requests"] is not None:
                step["shortfall"] = max(0, round(step["offered"] - step["requests"]))
                step["shortfall_frac"] = step["shortfall"] / step["offered"]
            else:
                step["shortfall"] = None
                step["shortfall_frac"] = None

            # What the flag and the bottom panel actually use. Prefer the
            # shortfall; fall back to the timeout count only when there is no
            # raw wrk2 file to supply a completed count (summary.csv alone),
            # so a partial run still flags rather than silently reading clean.
            if step["shortfall_frac"] is not None:
                step["loss_frac"] = step["shortfall_frac"]
                step["loss_n"] = step["shortfall"]
                step["loss_basis"] = "shortfall"
            elif step["timeout_frac"] is not None:
                step["loss_frac"] = step["timeout_frac"]
                step["loss_n"] = step["timeouts"]
                step["loss_basis"] = "timeouts"
            else:
                step["loss_frac"] = None
                step["loss_n"] = None
                step["loss_basis"] = None
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
            if s["loss_frac"] is not None and s["loss_frac"] >= FLAG_FRAC:
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
                    and s["loss_frac"] is not None
                    and s["loss_frac"] >= FLAG_FRAC):
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
    # Headroom so the legend and the loss callouts clear the p99 curve, and
    # so the p50 line does not sit on the bottom spine. The callouts are
    # parked in a reserved band across the top (see below), so the headroom
    # has to grow with how many rows that band needs -- on a saturating sweep
    # nearly every step flags, and 4x left the top rows outside the axes.
    lo, hi = ax_lat.get_ylim()
    n_slots = min(len(flagged), len(CALLOUT_SLOTS)) or 1
    ax_lat.set_ylim(lo / 1.5, hi * (3.6 ** n_slots))
    handles, labels = ax_lat.get_legend_handles_labels()
    if flagged:
        handles.append(plt.Line2D([], [], marker="o", markersize=11,
                                  markerfacecolor="none",
                                  markeredgecolor="tab:red", linestyle="none"))
        labels.append(f"p99 excludes >{FLAG_FRAC:.0%} of offered load")
    # Lower right, not upper left: the callout band now owns the top of the
    # axes, and on a saturating sweep the bottom-right is the one region no
    # curve reaches -- every percentile has already climbed by the last steps.
    ax_lat.legend(handles, labels, fontsize=8, ncol=max(1, len(strategies)),
                  loc="lower right")

    # Spell out the loss on every flagged step. One callout per step, listing
    # every strategy in it -- per-strategy callouts collide when both arms are
    # flagged at the same rate, which is the common case.
    #
    # Flagging on shortfall marks far more steps than flagging on timeouts did
    # (7 of 9 on the 09-03 fixture, against 2), and at that density the boxes
    # collide. Offsetting them in POINTS from each p99 point does not fix it:
    # on a saturating sweep p99 climbs two orders of magnitude across the
    # sweep, so a box pinned to a low-RPS point and a box pinned to a
    # high-RPS one land at unrelated heights and still overlap.
    #
    # So park the callouts in a reserved band at the TOP of the axes instead,
    # cycling through fixed slots, and let the leader line carry the eye back
    # down to the point. Boxes then cannot collide with each other or with the
    # data, whatever the sweep looks like. set_ylim above reserves the band.
    flag_seq = 0
    for i, rps in enumerate(rps_values):
        lines, anchor = [], None
        for strat in strategies:
            s = steps[strat].get(rps)
            if not s or s["loss_frac"] is None or s["loss_frac"] < FLAG_FRAC:
                continue
            # Name the mechanism, not just the size. A step that lost load to
            # timeouts and one that lost it because the client never reached
            # the target rate look identical in the bar height and call for
            # completely different follow-up.
            if s["loss_basis"] == "shortfall" and s["timeouts"]:
                why = f", {s['timeouts']:,} timed out"
            elif s["loss_basis"] == "shortfall":
                why = ", not offered"
            else:
                why = " timed out"
            lines.append(f"{label_of(strat)}: {s['loss_frac']:.1%} "
                         f"({s['loss_n']:,} reqs{why})")
            if s.get("p99") is not None:
                anchor = max(anchor or 0, s["p99"])
        if not lines or anchor is None:
            continue
        # Callouts on the right-hand steps point leftwards, or they run off
        # the axes on a long sweep.
        right = i > (len(rps_values) - 1) / 2
        slot = CALLOUT_SLOTS[flag_seq % len(CALLOUT_SLOTS)]
        flag_seq += 1
        # x in axes fraction, tracking the step so the leader line stays short,
        # but inset from the spines so a box at either end stays on the canvas.
        xf = min(0.97, max(0.03, (i + 0.5) / max(len(rps_values), 1)))
        ax_lat.annotate(
            "never completed, absent from these percentiles\n" + "\n".join(lines),
            xy=(i, anchor), xycoords="data", xytext=(xf, slot),
            textcoords="axes fraction",
            fontsize=7.5, color="tab:red",
            ha="right" if right else "left", va="top",
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
            tf = s["loss_frac"] if s else None
            nf = s["non_2xx_frac"] if s else None
            to_pct.append(0.0 if tf is None else tf * 100)
            n2_pct.append(0.0 if nf is None else nf * 100)
            if s and s.get("loss_n") is not None:
                any_completion = True
                labels_txt.append(f"{s['loss_n']:,}")
            else:
                labels_txt.append("n/a")
        bars = ax_to.bar(x + off, to_pct, width, color=c,
                         label=f"{label_of(strat)} never completed")
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
                    "(bar labels = requests offered but never completed)",
                    fontsize=10)
    top = max(ax_to.get_ylim()[1], FLAG_FRAC * 100 * 1.6)
    ax_to.set_ylim(0, top * 1.25)

    fig.text(0.01, 0.005,
             "Top panel describes only requests that completed. Bottom bars = "
             "offered load minus completed (timed out, or never sent because "
             "the client could not reach the target rate) -- a low p99 over a "
             "tall bar is a step that mostly did not answer. "
             "Offered load = target RPS x step duration; wrk2's timeout "
             "counter counts socket events, not requests, and is not the "
             "basis of the flag.",
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


# ===========================================================================
# .dat export
#
# The figure and this file are built from the same step dicts, so the .dat is
# the figure in text form -- not a re-derivation that can drift from it. The
# columns the figure does NOT draw (offered_reqs, completed_reqs, duration_s)
# are carried anyway: they are the denominators every percentage here rests
# on, and a reader checking a suspicious step needs them.
# ===========================================================================

LATENCY_DOC = f"""\
Data behind plot_sn_latency.pdf/.png -- both panels, one row per (strategy, RPS step).

SOURCE     percentiles: <run-dir>/<strategy>/<rps>.txt, the raw wrk2 output
           ("Latency Distribution" block). summary.csv carries only p50 and
           p99, so p90 exists ONLY here; where a raw file is missing, p50/p99
           fall back to summary.csv and p90 is N/A.
           timeouts / non_2xx: summary.csv when the column is present, else the
           raw "Socket errors: ... timeout N" and "Non-2xx or 3xx responses: N"
           lines. wrk2 OMITS both lines entirely when the count is zero, so an
           absent line is read as 0 rather than as unknown.
TRANSFORM  wrk2 prints us / ms / s AND m -- it switches to minutes past ~60s,
           e.g. "0.95m" -- so every latency here is normalised to MILLISECONDS.
           offered_reqs = target RPS x the step duration wrk2 reports ("N
           requests in 4.00m"), NOT wrk2's own request count: that count
           already excludes the requests that timed out, and using it as the
           denominator would understate the loss. completed_reqs is wrk2's
           count, kept alongside for exactly that comparison.
           shortfall     = offered_reqs - completed_reqs, floored at 0. THIS
                           is the load the percentiles left out, and it is
                           what the bottom panel draws and the flag fires on.
                           It covers both ways a step loses traffic: requests
                           the server never answered, and requests the client
                           never managed to send.
           shortfall_pct = shortfall / offered_reqs x 100
           timeout_pct   = timeouts / offered_reqs x 100
           non_2xx_pct   = non_2xx  / offered_reqs x 100
           loss_basis    = which quantity the flag used: `shortfall` normally,
                           `timeouts` only where no raw wrk2 file supplied a
                           completed count.
           flagged       = yes once shortfall_pct >= {FLAG_FRAC:.0%} of the
                           offered load -- the steps the figure shades and
                           rings in red.
WARNING    timeouts IS NOT A REQUEST COUNT and is NOT the basis of the flag.
           wrk2 increments it per socket timeout EVENT across the connection
           pool, so timeout_pct can exceed shortfall_pct and even imply more
           lost requests than were ever offered -- on the 09-03 12:45 run,
           Istio at 100 RPS reported 22862 completed + 3412 timeouts against
           24000 offered. Read it as a symptom ("the losses were socket
           timeouts") and read shortfall_pct for the magnitude.
           The converse also holds: a step whose client could not reach the
           target rate loses load with timeouts=0 and non_2xx=0, which is why
           flagging on timeouts marked the two mildest steps of that run and
           left the two most degraded ones clean.
CAVEAT     THE PERCENTILES DESCRIBE COMPLETED REQUESTS ONLY. wrk2 drops a
           timed-out request before it reaches the histogram, so a step can
           report an excellent p99 while a large share of the offered load
           never came back -- and non_2xx stays ~0 beside it, because a
           timeout carries no HTTP status. Read p50/p90/p99 next to
           shortfall_pct on the same row, never on their own.
UNITS      latency in ms; *_pct in percent of offered load; counts in requests;
           duration_s in seconds.
"""


def write_dat(path: Path, doc: str, header: list, rows: list) -> Path:
    """Write one tab-separated gnuplot .dat: comment block, header, rows.

    The header line is '#'-prefixed (a gnuplot comment, matching
    generate_dat.py) so the file plots directly with no skip-row argument.
    None -> "N/A": a value we do not have must never be read as a measured 0 --
    the distinction between "no timeouts" and "no timeout data" is the whole
    point of the bottom panel.
    """
    with path.open("w") as f:
        for line in doc.strip("\n").splitlines():
            f.write(("# " + line).rstrip() + "\n")
        f.write("# " + "\t".join(str(h) for h in header) + "\n")
        for row in rows:
            f.write("\t".join("N/A" if v is None else str(v) for v in row) + "\n")
    return path


def write_latency_dat(steps: dict, out: Path) -> Path:
    def f(v, digits=2):
        return None if v is None else f"{v:.{digits}f}"

    header = ["strategy", "rps", "p50_ms", "p90_ms", "p99_ms",
              "shortfall", "shortfall_pct", "timeouts", "timeout_pct",
              "non_2xx", "non_2xx_pct", "offered_reqs", "completed_reqs",
              "duration_s", "loss_basis", "flagged"]
    rows = []
    for strat in order_strategies(steps):
        for rps in sorted(steps[strat]):
            s = steps[strat][rps]
            tf, lf = s["timeout_frac"], s["loss_frac"]
            rows.append([
                label_of(strat), rps,
                f(s["p50"]), f(s["p90"]), f(s["p99"]),
                s["shortfall"],
                f(None if s["shortfall_frac"] is None
                  else s["shortfall_frac"] * 100, 3),
                s["timeouts"],
                f(None if tf is None else tf * 100, 3),
                s["non_2xx"],
                f(None if s["non_2xx_frac"] is None
                  else s["non_2xx_frac"] * 100, 3),
                f(s["offered"], 0), s["requests"], f(s["duration_s"]),
                s["loss_basis"],
                # Blank rather than "no" when we cannot tell: an unflagged row
                # and an unmeasured one are different claims.
                None if lf is None else ("yes" if lf >= FLAG_FRAC else "no"),
            ])
    return write_dat(out, LATENCY_DOC, header, rows)


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
    dat_out = run_dir / "plot_sn_latency.dat"
    plot(plottable, run_dir, [pdf_out, png_out])
    # Same step dicts the figure was drawn from -- see ".dat export" above.
    write_latency_dat(plottable, dat_out)

    for strat in order_strategies(plottable):
        for rps in sorted(plottable[strat]):
            s = plottable[strat][rps]
            fmt = lambda v: "n/a" if v is None else f"{v:.2f}ms"
            lf = ("n/a" if s["loss_frac"] is None
                  else f"{s['loss_frac']:.2%}")
            flag = ("  <-- FLAGGED" if s["loss_frac"] is not None
                    and s["loss_frac"] >= FLAG_FRAC else "")
            print(f"  {label_of(strat):<6} @ {rps:>5} RPS  "
                  f"p50={fmt(s['p50']):>10} p90={fmt(s['p90']):>10} "
                  f"p99={fmt(s['p99']):>11}  "
                  f"never_completed={s['loss_n']} ({lf} of offered)  "
                  f"timeouts={s['timeouts']} non_2xx={s['non_2xx']}{flag}")
    print(f"wrote {pdf_out}")
    print(f"wrote {png_out}")
    print(f"wrote {dat_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
