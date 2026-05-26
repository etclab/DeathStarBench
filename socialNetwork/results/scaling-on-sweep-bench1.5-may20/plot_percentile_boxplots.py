#!/usr/bin/env python3
"""Per-rps box plots of wrk2 latency percentiles across 10 benchmark runs.

For each rps, draws 20 boxes (10 runs x 2 strategies) where each box is built
from that run's wrk2 percentile distribution. All rps values are tiled in a
7x2 multiplot.
"""

import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Patch

BASE = Path(__file__).resolve().parent
STRATEGIES = ["istio", "st5-AttUpd"]
RPS_VALUES = [100, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000, 3200]
COLORS = {"istio": "#1f77b4", "st5-AttUpd": "#ff7f0e"}

# 
def parse_detailed_spectrum(path: Path):
    """Return sorted list of (latency_ms, percentile) from wrk2 detailed spectrum."""
    # print(f"parse_detailed_spectrum running on path: {path}")
    
    text = path.read_text()
    points = []
    in_spectrum = False
    for line in text.splitlines():
        if "Detailed Percentile spectrum" in line:
            in_spectrum = True
            continue
        if not in_spectrum:
            continue
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("---"):
            break
        m = re.match(r"\s*([\d.]+)\s+([\d.]+)\s+\d+\s+[\d.inf]+", line)
        if m:
            # print(f"\nprinting all matched groups: {m.groups()}")
            points.append((float(m.group(1)), float(m.group(2))))
    return points

# 
def percentile_at(points, target_p):
    """Linear-interpolate latency at a given percentile from spectrum."""
    
    # print(f"inside percentile_at ")
    # print(f"points = {points}")
    # print(f"target percentile = {target_p}")
    # (y-y0)/(x-x0) = (y1-y0)/(x1-x0)
    # x = x0+(x1-x0)*(y-y0)/(y1-y0)
    if not points:
        return None
    for i, (v1, p1) in enumerate(points):
        if p1 >= target_p:
            if i == 0 or p1 == points[i - 1][1]:
                return v1
            v0, p0 = points[i - 1]
            return v0 + (v1 - v0) * (target_p - p0) / (p1 - p0)
    return points[-1][0]


def build_box_stats(path: Path):
    """Build a matplotlib bxp() stats dict from a wrk2 output file."""
    pts = parse_detailed_spectrum(path)
    if not pts:
        return None
    return {
        "med": percentile_at(pts, 0.50),
        "q1": percentile_at(pts, 0.25),
        "q3": percentile_at(pts, 0.75),
        "whislo": percentile_at(pts, 0.10),
        "whishi": percentile_at(pts, 0.90),
        "fliers": [
            percentile_at(pts, q)
            for q in (0.99, 0.999, 0.9999)
            if percentile_at(pts, q) is not None
        ],
    }


def main():
    runs = sorted(
        d for d in BASE.iterdir() if d.is_dir() and d.name.startswith("benchmark")
    )
    if len(runs) != 10:
        print(f"warning: found {len(runs)} runs, expected 10")

    fig, axes = plt.subplots(7, 2, figsize=(20, 30), constrained_layout=False)
    axes = axes.flatten()

    group_w = 3  # x-units per run group (room for 2 boxes + gap)
    all_stats_dump = {}

    for ax_idx, rps in enumerate(RPS_VALUES):
        ax = axes[ax_idx]
        stats_list, positions, box_colors = [], [], []
        rps_dump = {s: {} for s in STRATEGIES}

        for run_idx, run in enumerate(runs):
            for s_idx, strat in enumerate(STRATEGIES):
                f = run / strat / f"{rps}.txt"
                if not f.exists():
                    continue
                stats = build_box_stats(f)
                if stats is None:
                    continue
                stats_list.append(stats)
                positions.append(run_idx * group_w + s_idx)
                box_colors.append(COLORS[strat])
                rps_dump[strat][run.name] = stats

        all_stats_dump[str(rps)] = rps_dump

        if not stats_list:
            ax.set_title(f"RPS = {rps} (no data)")
            continue

        bp = ax.bxp(
            stats_list,
            positions=positions,
            widths=0.8,
            patch_artist=True,
            showfliers=True,
            flierprops={"marker": ".", "markersize": 3, "alpha": 0.6},
        )
        for patch, color in zip(bp["boxes"], box_colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.6)
            patch.set_edgecolor("black")

        ax.set_title(f"RPS = {rps}")
        ax.set_ylabel("Latency (ms)")
        ax.set_yscale("log")
        ax.set_xticks([i * group_w + 0.5 for i in range(len(runs))])
        ax.set_xticklabels([f"R{i+1}" for i in range(len(runs))], rotation=0)
        ax.grid(True, which="both", axis="y", alpha=0.3)

    for ax_idx in range(len(RPS_VALUES), len(axes)):
        axes[ax_idx].set_visible(False)

    legend = [
        Patch(facecolor=COLORS[s], alpha=0.6, edgecolor="black", label=s)
        for s in STRATEGIES
    ]
    fig.legend(handles=legend, loc="upper center", ncol=len(STRATEGIES),
               bbox_to_anchor=(0.5, 0.995), frameon=False, fontsize=12)
    fig.suptitle(
        "wrk2 latency percentile distribution per run "
        "(box: p25/p50/p75; whiskers: p10/p90; fliers: p99/p99.9/p99.99)",
        y=0.978, fontsize=11,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    out_pdf = BASE / "percentile_boxplots.pdf"
    fig.savefig(out_pdf)
    print(f"wrote {out_pdf}")

    out_json = BASE / "percentile_boxplots.json"
    out_json.write_text(json.dumps(all_stats_dump, indent=2))
    print(f"wrote {out_json}")


if __name__ == "__main__":
    main()
