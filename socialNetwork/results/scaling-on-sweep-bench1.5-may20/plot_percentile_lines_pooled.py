#!/usr/bin/env python3
"""Pooled-histogram p50/p90/p99 latency vs rps, per strategy.

For each (strategy, rps) the 10 runs' wrk2 detailed percentile spectrums are
merged into a single histogram by summing per-bucket counts (recovered by
differencing the cumulative TotalCount column). Percentiles are then read off
the pooled CDF with linear interpolation.

This is the merge that HdrHistogram supports natively via add(): histograms
are additive, percentiles are not. Cross-run mean of per-run percentiles is
plotted alongside as a faint overlay so the divergence at the knee is visible.

References:
  - wrk2 README:                 https://github.com/giltene/wrk2
  - HdrHistogram (additivity):   https://hdrhistogram.github.io/HdrHistogram/
  - Wakart, "HdrHistogram: A better latency capture method":
      http://psy-lob-saw.blogspot.com/2015/02/hdrhistogram-better-latency-capture.html
  - Spectrum column format:      https://hdrhistogram.github.io/HdrHistogram/plotFiles.html
"""

import json
import re
import pprint
import statistics
from pathlib import Path

import matplotlib.pyplot as plt

BASE = Path(__file__).resolve().parent
STRATEGIES = ["istio", "st5-AttUpd"]
RPS_VALUES = [100, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000, 3200]
PERCENTILES = {"p50": 0.50, "p90": 0.90, "p99": 0.99}

STRATEGY_COLORS = {"istio": "#1f77b4", "st5-AttUpd": "#ff7f0e"}
PERCENTILE_STYLE = {
    "p50": {"linestyle": "-",  "marker": "o"},
    "p90": {"linestyle": "--", "marker": "s"},
    "p99": {"linestyle": ":",  "marker": "^"},
}

#
def parse_spectrum_with_counts(path: Path):
    """Return list of (value_ms, cumulative_count) from wrk2 detailed spectrum.

    Stops at the first separator/comment line after the table, which excludes
    the trailing summary block ("#[Mean    = ...", etc).
    """
    text = path.read_text()
    rows = []
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
        m = re.match(r"\s*([\d.]+)\s+([\d.]+)\s+(\d+)\s+[\d.infa]+", line)
        if m:
            rows.append((float(m.group(1)), float(m.group(2)), int(m.group(3))))
    return rows

#
def per_bucket_counts(rows):
    """Convert (value, percentile, cumulative_count) rows to (value, count) deltas.

    The HdrHistogram spectrum is decimating: many rows can collapse onto the
    same value bucket. We accumulate count deltas per distinct value so the
    pooled merge is a clean {value -> count} map.
    """
    counts = {}
    prev_cum = 0
    for v, _p, cum in rows:
        delta = cum - prev_cum
        if delta < 0:
            # malformed/non-monotonic row; skip but don't reset
            continue
        if delta > 0:
            counts[v] = counts.get(v, 0) + delta
        prev_cum = cum
    return counts

#
def merge_counts(per_run_counts):
    """Sum sparse {value -> count} dicts across runs into one dict."""
    pooled = {}
    for cmap in per_run_counts:
        for v, c in cmap.items():
            pooled[v] = pooled.get(v, 0) + c
    return pooled

#
def percentiles_from_pooled(pooled, targets):
    """Linear-interpolate percentile latencies from a pooled {value -> count} map."""
    if not pooled:
        return {name: None for name in targets}
    items = sorted(pooled.items())  # ascending value
    total = sum(c for _, c in items)

    cum_pairs = []  # (value, percentile_at_top_of_bucket)
    running = 0
    for v, c in items:
        running += c
        cum_pairs.append((v, running / total))

    out = {}
    for name, target in targets.items():
        out[name] = _interp(cum_pairs, target)
    return out

#
def _interp(cum_pairs, target):
    # (y-y0)/(x-x0) = (y1-y0)/(x1-x0)
    # x = x0+(x1-x0)*(y-y0)/(y1-y0)
    for i, (v, p) in enumerate(cum_pairs):
        if p >= target:
            if i == 0 or p == cum_pairs[i - 1][1]:
                return v
            v0, p0 = cum_pairs[i - 1]
            return v0 + (v - v0) * (target - p0) / (p - p0)
    return cum_pairs[-1][0]

#
def per_run_percentiles(rows, targets):
    """For overlay: compute p50/p90/p99 from a single run's spectrum (Value/Percentile)."""
    if not rows:
        return {name: None for name in targets}
    pts = [(v, p) for v, p, _ in rows]
    out = {}
    for name, target in targets.items():
        out[name] = _interp(pts, target)
    return out


def _pooled_cdf(pooled):
    """Return [[value, cumulative_percentile], ...] for inspection/plotting."""
    if not pooled:
        return []
    items = sorted(pooled.items())
    total = sum(c for _, c in items)
    cdf = []
    running = 0
    for v, c in items:
        running += c
        cdf.append([v, running / total])
    return cdf

#
def _sparse_to_pairs(cmap):
    """Sort {value -> count} and emit [[value, count], ...] for stable JSON."""
    return [[v, c] for v, c in sorted(cmap.items())]


def collect(intermediate_dir: Path):
    runs = sorted(
        d for d in BASE.iterdir() if d.is_dir() and d.name.startswith("benchmark")
    )
    if len(runs) != 10:
        print(f"warning: found {len(runs)} runs, expected 10")

    intermediate_dir.mkdir(exist_ok=True)

    per_run_counts_dump = {}   # rps -> strat -> run_dir -> [[value, count], ...]
    pooled_counts_dump = {}    # rps -> strat -> [[value, count], ...]
    pooled_cdf_dump = {}       # rps -> strat -> [[value, cumulative_pct], ...]
    per_run_pcts_dump = {}     # rps -> strat -> run_dir -> {p50, p90, p99}

    out = {}
    for rps in RPS_VALUES:
        out[str(rps)] = {}
        per_run_counts_dump[str(rps)] = {}
        pooled_counts_dump[str(rps)] = {}
        pooled_cdf_dump[str(rps)] = {}
        per_run_pcts_dump[str(rps)] = {}

        for strat in STRATEGIES:
            per_run_count_maps = []
            per_run_pcts = {p: [] for p in PERCENTILES}
            n_total_samples = 0

            per_run_counts_dump[str(rps)][strat] = {}
            per_run_pcts_dump[str(rps)][strat] = {}

            for run in runs:
                f = run / strat / f"{rps}.txt"
                if not f.exists():
                    continue
                # print(f"printing parsed spectrum for file: {f}")
                rows = parse_spectrum_with_counts(f)
                # print(rows)
                if not rows:
                    continue
                cmap = per_bucket_counts(rows)
                # print(cmap)
                per_run_count_maps.append(cmap)
                n_total_samples += sum(cmap.values())
                pr = per_run_percentiles(rows, PERCENTILES)
                for p, v in pr.items():
                    if v is not None:
                        per_run_pcts[p].append(v)

                per_run_counts_dump[str(rps)][strat][run.name] = _sparse_to_pairs(cmap)
                per_run_pcts_dump[str(rps)][strat][run.name] = pr

            pooled = merge_counts(per_run_count_maps)
            # print(f"\nmerged counts for: {rps}/{strat}")
            # pprint.pprint(pooled, indent=2)
            pooled_pcts = percentiles_from_pooled(pooled, PERCENTILES)

            # print(f"\npercentiles_from_pooled:")
            # pprint.pprint(pooled_pcts, indent=2)

            pooled_counts_dump[str(rps)][strat] = _sparse_to_pairs(pooled)
            pooled_cdf_dump[str(rps)][strat] = _pooled_cdf(pooled)

            mean_of_per_run = {}
            std_of_per_run = {}
            for p, vals in per_run_pcts.items():
                mean_of_per_run[p] = statistics.fmean(vals) if vals else None
                std_of_per_run[p] = (
                    statistics.stdev(vals) if len(vals) > 1 else (0.0 if vals else None)
                )

            out[str(rps)][strat] = {
                "n_runs": len(per_run_count_maps),
                "n_pooled_samples": n_total_samples,
                "pooled": pooled_pcts,
                "per_run_mean": mean_of_per_run,
                "per_run_std": std_of_per_run,
                "per_run_values": per_run_pcts,
            }

    _dump_json(intermediate_dir / "per_run_counts.json", per_run_counts_dump)
    _dump_json(intermediate_dir / "pooled_counts.json", pooled_counts_dump)
    _dump_json(intermediate_dir / "pooled_cdf.json", pooled_cdf_dump)
    _dump_json(intermediate_dir / "per_run_percentiles.json", per_run_pcts_dump)
    _dump_pooled_text(intermediate_dir / "pooled_summary.txt", out)

    return out


def _dump_json(path: Path, payload):
    path.write_text(json.dumps(payload, indent=2))
    print(f"wrote {path}")


def _fmt(v):
    return f"{v:.6f}" if v is not None else "NaN"


def _dump_gnuplot(base: Path, out):
    """Write whitespace-separated .dat files for gnuplot.

    One file per percentile, wide format: each strategy gets pooled/mean/std cols.
    Plus a combined file with every (strategy, percentile) pooled latency, and
    one per-strategy file with pooled p50/p90/p99 in fixed-width columns.
    Use NaN for missing values (gnuplot skips with `set datafile missing "NaN"`).
    """
    for pname in PERCENTILES:
        path = base / f"gnuplot_{pname}.dat"
        header_cols = ["rps"]
        for strat in STRATEGIES:
            header_cols += [f"{strat}_pooled", f"{strat}_mean", f"{strat}_std"]
        lines = ["# " + "  ".join(header_cols)]
        for rps in RPS_VALUES:
            row = [f"{rps}"]
            for strat in STRATEGIES:
                d = out[str(rps)][strat]
                row.append(_fmt(d["pooled"][pname]))
                row.append(_fmt(d["per_run_mean"][pname]))
                row.append(_fmt(d["per_run_std"][pname]))
            lines.append("  ".join(row))
        path.write_text("\n".join(lines) + "\n")
        print(f"wrote {path}")

    for strat in STRATEGIES:
        path = base / f"gnuplot_{strat}_latency.dat"
        lines = [
            f"# Pooled-histogram latency for {strat}",
            "# QPS        p50(ms)      p90(ms)      p99(ms)",
        ]
        for rps in RPS_VALUES:
            d = out[str(rps)][strat]["pooled"]
            row = f"  {rps:<10}"
            for pname in PERCENTILES:
                v = d[pname]
                row += f" {(v if v is not None else float('nan')):<12.2f}"
            lines.append(row)
        path.write_text("\n".join(lines) + "\n")
        print(f"wrote {path}")

    path = base / "gnuplot_combined.dat"
    header_cols = ["rps"]
    for strat in STRATEGIES:
        for pname in PERCENTILES:
            header_cols.append(f"{strat}_{pname}")
    lines = ["# " + "  ".join(header_cols)]
    for rps in RPS_VALUES:
        row = [f"{rps}"]
        for strat in STRATEGIES:
            for pname in PERCENTILES:
                row.append(_fmt(out[str(rps)][strat]["pooled"][pname]))
        lines.append("  ".join(row))
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}")


def _dump_pooled_text(path: Path, out):
    """Plain-text headline table: one row per (rps, strategy)."""
    lines = [
        "# pooled-histogram percentiles vs per-run mean+/-std",
        f"# {'rps':>5} {'strat':<12} {'n_runs':>6} {'n_samples':>10}"
        f" {'p50_pool':>10} {'p50_mean':>10} {'p50_std':>9}"
        f" {'p90_pool':>10} {'p90_mean':>10} {'p90_std':>9}"
        f" {'p99_pool':>10} {'p99_mean':>10} {'p99_std':>9}",
    ]
    for rps in RPS_VALUES:
        for strat in STRATEGIES:
            d = out[str(rps)][strat]
            row = [f"{rps:>7}", f"{strat:<12}", f"{d['n_runs']:>6}", f"{d['n_pooled_samples']:>10}"]
            for p in PERCENTILES:
                pool = d["pooled"][p]
                mean = d["per_run_mean"][p]
                std = d["per_run_std"][p]
                row.append(f"{(pool if pool is not None else float('nan')):>10.3f}")
                row.append(f"{(mean if mean is not None else float('nan')):>10.3f}")
                row.append(f"{(std if std is not None else float('nan')):>9.3f}")
            lines.append(" ".join(row))
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}")


def _xy_pooled(data, strat, pname):
    xs, ys = [], []
    for rps in RPS_VALUES:
        v = data[str(rps)][strat]["pooled"][pname]
        if v is None:
            continue
        xs.append(rps)
        ys.append(v)
    return xs, ys


def _xy_mean_std(data, strat, pname):
    xs, means, stds = [], [], []
    for rps in RPS_VALUES:
        d = data[str(rps)][strat]
        m = d["per_run_mean"][pname]
        s = d["per_run_std"][pname]
        if m is None:
            continue
        xs.append(rps)
        means.append(m)
        stds.append(s if s is not None else 0.0)
    return xs, means, stds


def _draw(ax, data, strat, pname, show_overlay):
    style = PERCENTILE_STYLE[pname]
    color = STRATEGY_COLORS[strat]

    xs, ys = _xy_pooled(data, strat, pname)
    ax.plot(
        xs, ys,
        color=color,
        linestyle=style["linestyle"],
        marker=style["marker"],
        markersize=6,
        linewidth=2.0,
        label=f"{strat} {pname} (pooled)",
    )

    if show_overlay:
        xs, means, stds = _xy_mean_std(data, strat, pname)
        ax.errorbar(
            xs, means, yerr=stds,
            color=color,
            linestyle=style["linestyle"],
            marker=style["marker"],
            markersize=4,
            linewidth=1.0,
            alpha=0.35,
            capsize=3,
            label=f"{strat} {pname} (mean+/-std)",
        )


def _format_axes(ax, title):
    ax.set_xlabel("Target RPS")
    ax.set_ylabel("Latency (ms)")
    ax.set_yscale("log")
    ax.set_xticks(RPS_VALUES)
    ax.set_xticklabels([str(r) for r in RPS_VALUES], rotation=0, fontsize=8)
    ax.grid(True, which="both", axis="y", alpha=0.3)
    ax.grid(True, which="major", axis="x", alpha=0.2)
    ax.set_title(title)
    ax.legend(fontsize=8, loc="upper left")


def plot(data, out_pdf):
    fig, axes = plt.subplots(2, 2, figsize=(16, 11))

    for ax, pname in zip(axes.flat[:3], PERCENTILES):
        for strat in STRATEGIES:
            _draw(ax, data, strat, pname, show_overlay=True)
        _format_axes(ax, f"{pname} latency vs RPS (pooled hist; faint = per-run mean+/-std)")

    ax_all = axes.flat[3]
    for strat in STRATEGIES:
        for pname in PERCENTILES:
            _draw(ax_all, data, strat, pname, show_overlay=False)
    _format_axes(ax_all, "p50/p90/p99 combined (pooled histogram)")
    ax_all.legend(ncol=2, fontsize=8, loc="upper left")

    fig.tight_layout()
    fig.savefig(out_pdf)
    print(f"wrote {out_pdf}")


def main():
    intermediate_dir = BASE / "pooled_intermediates"
    data = collect(intermediate_dir)

    out_json = BASE / "percentile_lines_pooled.json"
    out_json.write_text(json.dumps(data, indent=2))
    print(f"wrote {out_json}")

    _dump_gnuplot(BASE, data)

    plot(data, BASE / "percentile_lines_pooled.pdf")


if __name__ == "__main__":
    main()
