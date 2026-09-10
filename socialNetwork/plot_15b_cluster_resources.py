#!/usr/bin/env python3
"""Plot fleet-wide CPU and memory, Istio vs Mazu, for one replica scale of a
1.5b replica-scale sweep.

Reads the tree written by run-benchmark1.5b-replica-scale-sweep.sh:

    <run-dir>/scale-<N>x/<strategy>/metrics_<rps>.json   collect_metrics.sh
    <run-dir>/scale-<N>x/<strategy>/<rps>.txt            raw wrk2 output

and produces under <run-dir>, per selected scale:

  - plot_15b_cluster_cpu_<scales>.pdf/.png
  - plot_15b_cluster_memory_<scales>.pdf/.png
  - plot_15b_cluster_resources_<scales>.dat

WHY THIS EXISTS ALONGSIDE THE PER-SERVICE plot_15_cpu.gpi
  The gnuplot figure the sweep already emits per scale is a 100-bar histogram
  of PER-POD CPU, one panel per RPS step. At 16x that is 96 bars per panel and
  it answers the wrong question: whether any single sidecar is hot. The
  question a replica-scale sweep is actually asking is what the WHOLE FLEET
  costs, and how that cost moves with offered load -- one number per (mesh,
  RPS step), which is what this script plots.

WHAT "TOTAL" MEANS HERE, AND WHAT IS MISSING FROM IT
  collect_metrics.sh queries three groups, and the headline total is the sum
  over pods of the first two:

    sidecars   container="istio-proxy" in namespace default -- the mesh data
               plane, one container per app pod (96 of them at 16x)
    istiod     the control plane
    apiserver  kube-apiserver, drawn in the breakdown panel but NOT added into
               the headline total: it is a cluster-wide background cost that
               barely moves with load (~0.3 cores at every scale and every RPS
               in this run), so folding it in would add a constant offset that
               shrinks the visible difference between the arms. The .dat
               carries both totals so either can be quoted.

  APP CONTAINERS ARE NOT MEASURED AT ALL. collect_metrics.sh pins
  container="istio-proxy", so productpage/details/ratings/reviews CPU is
  absent from this run's metrics files. "Total cluster CPU" below therefore
  means TOTAL MESH CPU. That is the honest cross-arm number anyway -- the two
  arms run the same workload on the same substrate, so the app tier costs the
  same in both -- but it is not the machine's total, and must not be quoted as
  one.

WHY SUMMING PER-POD VECTORS IS THE RIGHT AGGREGATION
  Each group is a Prometheus instant vector with one sample per pod, already
  averaged over the benchmark window by the query
  (avg_over_time(rate(...)[dur:15s]) for CPU, avg_over_time(...[dur]) for
  memory). Summing gives what the fleet spent; averaging would flatter
  whichever arm ran more pods. Pod counts are equal per scale here, but the
  istiod group is not -- the Istio arm ran two istiod pods for the low RPS
  steps and one thereafter -- so the sum is what stays comparable.

WHY THE THIRD PANEL IS NORMALISED, AND WHY BY *ACHIEVED* RPS
  Total CPU rising with replica count is not by itself evidence of waste: at
  16x the fleet also serves far more load, because a 6-pod fleet saturates
  near 240 RPS and simply never delivers the higher steps. Charging 1x for
  2000 RPS it never served would manufacture a 10x efficiency gap out of
  nothing. So panel 3 divides fleet CPU by the rate wrk2 ACTUALLY completed
  (requests / elapsed), giving mCPU per 1000 req/s -- the number that answers
  "does spreading the same request rate over more replicas cost more CPU per
  request". Any step short of its offered load by >=1% is ringed, since a
  ringed step's ratio is built on a rate the fleet could not sustain.

  Memory has no per-request meaning -- it is a footprint, not a rate -- so its
  third panel normalises per sidecar instead, which is where a config/endpoint
  table that grows with fleet size would show up.

Scales, strategies and RPS steps are discovered from disk. Missing files,
failed Prometheus queries (collect_metrics.sh substitutes an empty result) and
partial runs are warned about on stderr and plotted as gaps, never as zeros.
Exits non-zero only when there is nothing at all to plot.

Usage: ./plot_15b_cluster_resources.py <benchmark-run-dir> [--scales 16x,1x]
       (default --scales 16x)
"""

import json
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import ScalarFormatter

# Same labels and colours as plot_15b_replica_scale.py / plot_sn_resources.py,
# so Istio and Mazu keep one identity across every figure in this family.
STRATEGY_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
STRATEGY_COLORS = {"istio": "#77AADD", "st5-AttUpd": "#EE8866"}
STRATEGY_ORDER = ["istio", "st5-AttUpd"]
FALLBACK_COLORS = ["#EEDD88", "#44BB99", "#FFAABB", "#BBCC33"]

# One line style per replica scale, so a multi-scale overlay stays readable
# with colour still meaning "which mesh".
SCALE_STYLES = ["-", "--", ":", "-.", (0, (3, 1, 1, 1))]

# Prometheus group -> (display name, is it part of the headline total).
GROUPS = [
    ("bookinfo", "istio-proxy sidecars", True),
    ("istiod", "istiod (control plane)", True),
    ("kube_apiserver", "kube-apiserver (background)", False),
]
GROUP_MARKERS = {"bookinfo": "o", "istiod": "s", "kube_apiserver": "^"}

# A step is ringed once this share of the offered load never completed --
# same threshold and same reasoning as plot_15b_replica_scale.py.
FLAG_FRAC = 0.01

SCALE_DIR_RE = re.compile(r"^scale-(\d+)x$")
METRICS_RE = re.compile(r"^metrics_(\d+)\.json$")
REQS_RE = re.compile(r"^\s*(\d+)\s+requests in\s+([\d.]+)\s*(us|ms|s|m)\b")
_UNIT_MS = {"us": 1e-3, "ms": 1.0, "s": 1e3, "m": 6e4}

MIB = 1024.0 ** 2
GIB = 1024.0 ** 3


def label_of(strat: str) -> str:
    return STRATEGY_LABELS.get(strat, strat)


def color_of(strat: str, extras: list) -> str:
    if strat in STRATEGY_COLORS:
        return STRATEGY_COLORS[strat]
    return FALLBACK_COLORS[extras.index(strat) % len(FALLBACK_COLORS)]


def order_strategies(strategies) -> list:
    known = [s for s in STRATEGY_ORDER if s in strategies]
    return known + sorted(s for s in strategies if s not in STRATEGY_ORDER)


def sum_group(metrics: dict, section: str, group: str, where: str):
    """Sum one Prometheus instant vector -> (total, pod_count).

    collect_metrics.sh writes {"status":"error", ..., "result":[]} for a query
    that failed, and a group can also be legitimately absent. Either way an
    empty vector means NOT MEASURED, so return (None, 0) and let the caller
    leave a gap -- a fleet that reported nothing did not burn zero cores.
    """
    body = metrics.get(section, {}).get(group)
    if not isinstance(body, dict):
        print(f"warning: {where}: no {section}.{group} block", file=sys.stderr)
        return None, 0
    result = body.get("data", {}).get("result") or []
    if not result:
        print(f"warning: {where}: {section}.{group} is empty "
              f"(status={body.get('status')!r})", file=sys.stderr)
        return None, 0
    total = 0.0
    n = 0
    for sample in result:
        try:
            total += float(sample["value"][1])
            n += 1
        except (KeyError, IndexError, TypeError, ValueError):
            print(f"warning: {where}: unparseable {section}.{group} sample",
                  file=sys.stderr)
    return (total, n) if n else (None, 0)


def parse_achieved(path: Path):
    """-> (achieved_rps, requests, elapsed_s) from raw wrk2 output.

    Only the summary "N requests in T" line is needed here; the percentiles
    are plot_15b_replica_scale.py's job. Units are normalised because wrk2
    switches to minutes past ~60s ("2.00m"), and a 120s run reported as 2.00
    would inflate the achieved rate 60-fold.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        print(f"warning: cannot read {path}: {e}", file=sys.stderr)
        return None, None, None
    for line in text.splitlines():
        m = REQS_RE.match(line)
        if m:
            reqs = int(m.group(1))
            elapsed = float(m.group(2)) * _UNIT_MS[m.group(3)] / 1000.0
            return (reqs / elapsed if elapsed else None), reqs, elapsed
    print(f"warning: no 'N requests in T' line in {path}", file=sys.stderr)
    return None, None, None


def collect(run_dir: Path, want_scales) -> dict:
    """-> {scale: {strategy: {rps: step}}} for everything on disk."""
    data = {}
    for child in sorted(run_dir.iterdir()):
        if not child.is_dir():
            continue
        m = SCALE_DIR_RE.match(child.name)
        if not m:
            continue
        scale = int(m.group(1))
        if want_scales and scale not in want_scales:
            continue

        per_strategy = {}
        for strat_dir in sorted(p for p in child.iterdir() if p.is_dir()):
            if strat_dir.name == "manifests":
                continue
            steps = {}
            for f in sorted(strat_dir.glob("metrics_*.json")):
                mm = METRICS_RE.match(f.name)
                if not mm:
                    continue
                rps = int(mm.group(1))
                where = f"{scale}x/{strat_dir.name}@{rps}"
                try:
                    metrics = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError) as e:
                    print(f"warning: {where}: cannot read {f}: {e}",
                          file=sys.stderr)
                    continue

                step = {"rps": rps, "cpu": {}, "memory": {}, "pods": {}}
                for group, _, _ in GROUPS:
                    cpu, n = sum_group(metrics, "cpu", group, where)
                    mem, _ = sum_group(metrics, "memory", group, where)
                    step["cpu"][group] = cpu
                    step["memory"][group] = mem
                    step["pods"][group] = n

                achieved, reqs, elapsed = parse_achieved(strat_dir / f"{rps}.txt")
                step["achieved_rps"] = achieved
                step["requests"] = reqs
                step["elapsed_s"] = elapsed
                # Offered load is the target rate over the time wrk2 actually
                # ran, not the configured duration: a step that ended early
                # must not be charged for load it was never asked to send.
                if reqs is not None and elapsed:
                    offered = rps * elapsed
                    step["shortfall_frac"] = max(0.0, (offered - reqs) / offered)
                else:
                    step["shortfall_frac"] = None
                step["start_epoch"] = metrics.get("metadata", {}).get("start_epoch")
                steps[rps] = step

            if steps:
                per_strategy[strat_dir.name] = steps
            else:
                print(f"warning: no metrics_<rps>.json under {strat_dir}",
                      file=sys.stderr)
        if per_strategy:
            data[scale] = per_strategy
        else:
            print(f"warning: scale {scale}x has no metrics, skipping",
                  file=sys.stderr)
    return data


def totals(step: dict, section: str):
    """Headline mesh total, and the same plus kube-apiserver.

    A group that was not measured makes the total None rather than silently
    dropping out of the sum -- a total missing its sidecar term is not a
    smaller total, it is not a total.
    """
    mesh = 0.0
    for group, _, in_total in GROUPS:
        if not in_total:
            continue
        v = step[section][group]
        if v is None:
            return None, None
        mesh += v
    api = step[section]["kube_apiserver"]
    return mesh, (None if api is None else mesh + api)


def style_rps_axis(ax, xs) -> None:
    """Log x with one labelled tick per swept RPS.

    The sweep is geometric-ish (100..2000); on a linear axis the four low
    steps collapse into one another and the pre-saturation behaviour -- the
    part that shows the fixed per-sidecar floor -- becomes unreadable.
    """
    ax.set_xscale("log")
    ax.set_xticks(xs)
    ax.get_xaxis().set_major_formatter(ScalarFormatter())
    ax.get_xaxis().set_minor_formatter(plt.NullFormatter())
    # Rotated because the top of a geometric sweep bunches up on a log axis:
    # horizontal labels render 1600 and 2000 as "16002000".
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    for lbl in ax.get_xticklabels():
        lbl.set_horizontalalignment("right")
    ax.set_xlabel("target RPS  (steps ran in this order)")


def headroom(ax) -> None:
    """Zero-based y with room above the top point for the legend.

    Memory sits in a narrow band well off zero, so the default top limit lands
    a hair above the highest sample and the upper-left legend then covers the
    first step of the upper line.
    """
    _, hi = ax.get_ylim()
    ax.set_ylim(0, hi * 1.18)


def ring_shortfall(ax, x, y, step) -> bool:
    """Ring a point whose step never delivered its offered load. -> ringed?"""
    sf = step["shortfall_frac"]
    if y is None or sf is None or sf < FLAG_FRAC:
        return False
    ax.plot([x], [y], marker="o", markersize=11, markerfacecolor="none",
            markeredgecolor="#CC3311", markeredgewidth=1.6, linestyle="none",
            zorder=5)
    return True


def series(steps: dict, rps_list, fn):
    """(xs, ys, steps) for the rps steps where fn(step) is not None."""
    xs, ys, sts = [], [], []
    for rps in rps_list:
        st = steps.get(rps)
        if st is None:
            continue
        v = fn(st)
        if v is None:
            continue
        xs.append(rps)
        ys.append(v)
        sts.append(st)
    return xs, ys, sts


def plot_metric(data: dict, run_dir: Path, section: str, suffix: str) -> list:
    """One 3-panel figure for CPU or memory. -> written paths."""
    is_cpu = section == "cpu"
    scales = sorted(data)
    strategies = order_strategies({s for per in data.values() for s in per})
    extras = [s for s in strategies if s not in STRATEGY_COLORS]
    rps_list = sorted({r for per in data.values()
                       for steps in per.values() for r in steps})

    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.0))
    ax_total, ax_break, ax_norm = axes
    ringed_any = False

    # --- panel 1: fleet total vs RPS -------------------------------------
    unit = "cores" if is_cpu else "GiB"
    div = 1.0 if is_cpu else GIB
    for si, scale in enumerate(scales):
        ls = SCALE_STYLES[si % len(SCALE_STYLES)]
        for strat in strategies:
            steps = data[scale].get(strat, {})
            xs, ys, sts = series(steps, rps_list,
                                 lambda st: totals(st, section)[0])
            if not xs:
                continue
            ys = [v / div for v in ys]
            tag = f"{label_of(strat)}" + (f" ({scale}x)" if len(scales) > 1 else "")
            ax_total.plot(xs, ys, linestyle=ls, marker="o", markersize=5,
                          color=color_of(strat, extras), label=tag, zorder=3)
            for x, y, st in zip(xs, ys, sts):
                ringed_any |= ring_shortfall(ax_total, x, y, st)
    ax_total.set_ylabel(f"fleet {'CPU' if is_cpu else 'memory'} ({unit})")
    ax_total.set_title(f"Total mesh {'CPU' if is_cpu else 'memory'}\n"
                       f"(istio-proxy sidecars + istiod)", fontsize=11)
    headroom(ax_total)
    ax_total.legend(fontsize=9, loc="upper left")

    # Ratio at the top step, the headline number of the whole figure.
    if len(scales) == 1 and len(strategies) == 2 and rps_list:
        base, other = strategies[0], strategies[1]
        top = rps_list[-1]
        b = data[scales[0]].get(base, {}).get(top)
        o = data[scales[0]].get(other, {}).get(top)
        if b and o:
            bv, ov = totals(b, section)[0], totals(o, section)[0]
            if bv and ov:
                ax_total.annotate(
                    f"{label_of(other)}/{label_of(base)} @ {top} RPS = "
                    f"{ov / bv:.2f}x",
                    xy=(0.97, 0.04), xycoords="axes fraction", fontsize=9,
                    ha="right",
                    bbox=dict(boxstyle="round,pad=0.3", fc="#F4F4F4",
                              ec="#BBBBBB"))

    # --- panel 2: per component ------------------------------------------
    unit2 = "cores" if is_cpu else "MiB"
    div2 = 1.0 if is_cpu else MIB
    for si, scale in enumerate(scales):
        ls = SCALE_STYLES[si % len(SCALE_STYLES)]
        for strat in strategies:
            steps = data[scale].get(strat, {})
            for group, _, _ in GROUPS:
                xs, ys, _ = series(steps, rps_list,
                                   lambda st, g=group: st[section][g])
                if not xs:
                    continue
                ax_break.plot(xs, [v / div2 for v in ys], linestyle=ls,
                              marker=GROUP_MARKERS[group], markersize=4,
                              color=color_of(strat, extras), alpha=0.85)
    ax_break.set_yscale("log")  # sidecars and istiod differ by ~3 decades
    ax_break.set_ylabel(f"{'CPU' if is_cpu else 'memory'} ({unit2}, log)")
    ax_break.set_title("Where it goes, by component\n"
                       "(colour = mesh, as panel 1)", fontsize=11)
    ax_break.legend(handles=[Line2D([], [], color="#555555",
                                    marker=GROUP_MARKERS[g], linestyle="-",
                                    markersize=5, label=name)
                             for g, name, _ in GROUPS],
                    fontsize=8, loc="best")

    # --- panel 3: normalised ---------------------------------------------
    if is_cpu:
        # mCPU per 1000 req/s ACTUALLY completed -- see the module docstring
        # on why the target rate would be dishonest here.
        def norm(st):
            mesh, _ = totals(st, "cpu")
            a = st["achieved_rps"]
            return None if (mesh is None or not a) else mesh * 1000.0 / a * 1000.0
        ax_norm.set_ylabel("mCPU per 1000 req/s completed")
        ax_norm.set_title("Cost per unit of delivered load\n"
                          "(flat = replication is free; rising = it is not)",
                          fontsize=11)
    else:
        def norm(st):
            v = st["memory"]["bookinfo"]
            n = st["pods"]["bookinfo"]
            return None if (v is None or not n) else v / n / MIB
        ax_norm.set_ylabel("memory per sidecar (MiB)")
        ax_norm.set_title("Per-sidecar footprint\n"
                          "(fleet memory / sidecar count)", fontsize=11)
    for si, scale in enumerate(scales):
        ls = SCALE_STYLES[si % len(SCALE_STYLES)]
        for strat in strategies:
            steps = data[scale].get(strat, {})
            xs, ys, sts = series(steps, rps_list, norm)
            if not xs:
                continue
            tag = f"{label_of(strat)}" + (f" ({scale}x)" if len(scales) > 1 else "")
            ax_norm.plot(xs, ys, linestyle=ls, marker="o", markersize=5,
                         color=color_of(strat, extras), label=tag, zorder=3)
            for x, y, st in zip(xs, ys, sts):
                ringed_any |= ring_shortfall(ax_norm, x, y, st)
    headroom(ax_norm)
    ax_norm.legend(fontsize=9)

    for ax in axes:
        style_rps_axis(ax, rps_list)
        ax.grid(True, which="major", axis="both", alpha=0.3)

    scale_txt = ", ".join(f"{s}x" for s in scales)
    pods = {data[s][st][r]["pods"]["bookinfo"]
            for s in scales for st in data[s] for r in data[s][st]}
    pod_txt = f"{min(pods)}" if len(pods) == 1 else f"{min(pods)}-{max(pods)}"
    fig.suptitle(
        f"{'CPU' if is_cpu else 'Memory'}: Istio vs Mazu at replica scale "
        f"{scale_txt}  ({pod_txt} sidecars)   {run_dir.name}",
        fontsize=13)

    notes = ["app containers are not measured by collect_metrics.sh "
             "(container=\"istio-proxy\" only)"]
    if ringed_any:
        notes.append(f"red ring: >={FLAG_FRAC * 100:g}% of offered load never "
                     f"completed at that step")
    fig.text(0.5, 0.005, "   |   ".join(notes), ha="center", fontsize=8,
             color="#555555")

    fig.tight_layout(rect=(0, 0.035, 1, 0.93))
    out = []
    for ext in ("pdf", "png"):
        p = run_dir / f"plot_15b_cluster_{section}_{suffix}.{ext}"
        fig.savefig(p, dpi=150)
        out.append(p)
    plt.close(fig)
    return out


DAT_DOC = f"""
Fleet-wide CPU and memory behind plot_15b_cluster_cpu_*.pdf and
plot_15b_cluster_memory_*.pdf, produced by plot_15b_cluster_resources.py.

Source: <run-dir>/scale-<N>x/<strategy>/metrics_<rps>.json, written by
collect_metrics.sh, summed over the pods in each Prometheus instant vector.
  cpu_*     avg_over_time(rate(container_cpu_usage_seconds_total[1m])[dur:15s])
            in cores. sidecars = container="istio-proxy", namespace default.
  mem_*     avg_over_time(container_memory_working_set_bytes[dur]), reported
            here in MiB (1 MiB = 1048576 B).
App containers are NOT in any column: the collector pins
container="istio-proxy", so every "total" here is a MESH total, not the
machine's.

columns
scale          replica multiplier of the app deployment
strategy       Istio (baseline) or Mazu (st5-AttUpd)
target_rps     the step's offered rate, and the sweep's step order in time
achieved_rps   requests / elapsed as reported by wrk2 -- what was DELIVERED
shortfall_pct  100 * (offered - completed) / offered, offered = target x elapsed
flagged        shortfall_pct >= {FLAG_FRAC * 100:g}; the fleet could not sustain the offered
               rate, so per-request columns for that step are not quotable
sidecars       number of istio-proxy pods summed into the sidecar columns
cpu_sidecars   cores, summed over sidecars
cpu_istiod     cores, control plane
cpu_apiserver  cores, kube-apiserver -- cluster-wide background, excluded from
               cpu_mesh_total (see the script docstring)
cpu_mesh_total cpu_sidecars + cpu_istiod  <- the headline total
cpu_with_api   cpu_mesh_total + cpu_apiserver
mcpu_per_krps  1000 * cpu_mesh_total / achieved_rps * 1000, i.e. mCPU spent
               per 1000 req/s actually completed
mem_* columns  the same decomposition in MiB
mem_per_car    mem_sidecars / sidecars, MiB per istio-proxy container
t_rel_min      minutes from the first step of the run to this step's window
               start (metrics metadata.start_epoch), for reading the sweep as
               a timeline rather than as an RPS axis
N/A            not measured. Never read it as a zero.
"""


def write_dat(path: Path, header: list, rows: list) -> Path:
    """Tab-separated gnuplot .dat: comment block, '#'-prefixed header, rows."""
    with path.open("w") as f:
        for line in DAT_DOC.strip("\n").splitlines():
            f.write(("# " + line).rstrip() + "\n")
        f.write("# " + "\t".join(str(h) for h in header) + "\n")
        for row in rows:
            f.write("\t".join("N/A" if v is None else str(v) for v in row) + "\n")
    return path


def write_resources_dat(data: dict, out: Path) -> Path:
    header = ["scale", "strategy", "target_rps", "achieved_rps",
              "shortfall_pct", "flagged", "sidecars",
              "cpu_sidecars", "cpu_istiod", "cpu_apiserver",
              "cpu_mesh_total", "cpu_with_api", "mcpu_per_krps",
              "mem_sidecars_mib", "mem_istiod_mib", "mem_apiserver_mib",
              "mem_mesh_total_mib", "mem_with_api_mib", "mem_per_car_mib",
              "t_rel_min"]
    rows = []
    epochs = [st["start_epoch"] for per in data.values()
              for steps in per.values() for st in steps.values()
              if st["start_epoch"]]
    t0 = min(epochs) if epochs else None

    def r(v, nd):
        return None if v is None else round(v, nd)

    for scale in sorted(data):
        for strat in order_strategies(data[scale]):
            steps = data[scale][strat]
            for rps in sorted(steps):
                st = steps[rps]
                cpu_mesh, cpu_api_total = totals(st, "cpu")
                mem_mesh, mem_api_total = totals(st, "memory")
                sf = st["shortfall_frac"]
                a = st["achieved_rps"]
                n = st["pods"]["bookinfo"]
                mem_car = st["memory"]["bookinfo"]
                mib = lambda v: None if v is None else v / MIB
                rows.append([
                    f"{scale}x", label_of(strat), rps, r(a, 1),
                    None if sf is None else round(sf * 100, 3),
                    None if sf is None else ("yes" if sf >= FLAG_FRAC else "no"),
                    n or None,
                    r(st["cpu"]["bookinfo"], 4), r(st["cpu"]["istiod"], 4),
                    r(st["cpu"]["kube_apiserver"], 4),
                    r(cpu_mesh, 4), r(cpu_api_total, 4),
                    None if (cpu_mesh is None or not a)
                    else round(cpu_mesh * 1e6 / a, 1),
                    r(mib(st["memory"]["bookinfo"]), 1),
                    r(mib(st["memory"]["istiod"]), 1),
                    r(mib(st["memory"]["kube_apiserver"]), 1),
                    r(mib(mem_mesh), 1), r(mib(mem_api_total), 1),
                    None if (mem_car is None or not n)
                    else round(mem_car / n / MIB, 1),
                    None if (t0 is None or not st["start_epoch"])
                    else round((st["start_epoch"] - t0) / 60.0, 1),
                ])
    return write_dat(out, header, rows)


def parse_args(argv):
    run_dir, scales = None, {16}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--scales":
            i += 1
            if i >= len(argv):
                return None, None, "--scales needs a value"
            raw = argv[i]
            if raw.strip().lower() == "all":
                scales = set()  # empty = take everything on disk
            else:
                try:
                    scales = {int(t.strip().rstrip("xX"))
                              for t in raw.split(",") if t.strip()}
                except ValueError:
                    return None, None, f"cannot parse --scales {raw!r}"
        elif a.startswith("--"):
            return None, None, f"unknown option {a}"
        elif run_dir is None:
            run_dir = Path(a)
        else:
            return None, None, f"unexpected argument {a}"
        i += 1
    if run_dir is None:
        return None, None, "missing <benchmark-run-dir>"
    return run_dir, scales, None


def main() -> int:
    run_dir, want_scales, err = parse_args(sys.argv)
    if err:
        print(f"error: {err}\n"
              f"usage: {sys.argv[0]} <benchmark-run-dir> "
              f"[--scales 16x[,1x,...]|all]", file=sys.stderr)
        return 2
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    data = collect(run_dir, want_scales)
    if not data:
        asked = ", ".join(f"{s}x" for s in sorted(want_scales)) or "any scale"
        print(f"error: no scale-<N>x/<strategy>/metrics_<rps>.json under "
              f"{run_dir} for {asked}", file=sys.stderr)
        return 1

    suffix = "-".join(f"{s}x" for s in sorted(data))
    outs = []
    outs += plot_metric(data, run_dir, "cpu", suffix)
    outs += plot_metric(data, run_dir, "memory", suffix)
    outs.append(write_resources_dat(
        data, run_dir / f"plot_15b_cluster_resources_{suffix}.dat"))

    for scale in sorted(data):
        for strat in order_strategies(data[scale]):
            for rps in sorted(data[scale][strat]):
                st = data[scale][strat][rps]
                cpu, _ = totals(st, "cpu")
                mem, _ = totals(st, "memory")
                a = st["achieved_rps"]
                sf = st["shortfall_frac"]
                n = st["pods"]["bookinfo"]
                f2 = lambda v, u: "n/a" if v is None else f"{v:.2f}{u}"
                per = ("n/a" if (cpu is None or not a)
                       else f"{cpu * 1e6 / a:.0f}")
                flag = "  <-- could not sustain offered rate" \
                    if sf is not None and sf >= FLAG_FRAC else ""
                print(f"  {scale:>3}x  {label_of(strat):<6} @ {rps:>5} RPS  "
                      f"achieved={('n/a' if a is None else f'{a:7.1f}')}  "
                      f"cpu={f2(cpu, ' cores'):>12}  "
                      f"mem={f2(None if mem is None else mem / GIB, ' GiB'):>10}  "
                      f"mCPU/krps={per:>6}  "
                      f"mem/sidecar="
                      f"{f2(None if (st['memory']['bookinfo'] is None or not n) else st['memory']['bookinfo'] / n / MIB, ' MiB'):>10}"
                      f"{flag}")
    for o in outs:
        print(f"wrote {o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
