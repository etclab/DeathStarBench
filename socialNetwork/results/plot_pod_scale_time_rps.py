#!/usr/bin/env python3
"""Per-run distribution of "time to reach N ready pods" at a single RPS.

For each service and each pod-count threshold N, scatter the per-run
time-to-reach across the 10 runs, istio vs st5-AttUpd side by side, with a
median bar. Runs that never reached N within the window are plotted at the
window length as open markers.

Output: <sweep-dir>/pod_scale_time_<rps>.pdf

Usage: ./plot_pod_scale_time_rps.py <rps> [--sweep-dir DIR] [--out FILE]
"""

import argparse
import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

STRATEGIES = ["istio", "st5-AttUpd"]
COLORS = {"istio": "tab:blue", "st5-AttUpd": "tab:orange"}
SKIP_COLS = {"index", "timestamp", "total"}
JITTER = {"istio": -0.18, "st5-AttUpd": +0.18}


def load_run(csv_path: Path) -> tuple[list[str], np.ndarray, np.ndarray] | None:
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        services = [c for c in (reader.fieldnames or []) if c not in SKIP_COLS]
        if not services:
            return None
        ts: list[int] = []
        rows: list[list[int]] = []
        for row in reader:
            try:
                ts.append(int(row["timestamp"]))
                rows.append([int(row[s]) for s in services])
            except (KeyError, ValueError):
                continue
    if not ts:
        return None
    t = np.array(ts) - ts[0]
    return services, t, np.array(rows)


def first_ge(t: np.ndarray, y: np.ndarray, k: int) -> float | None:
    mask = y >= k
    if not mask.any():
        return None
    return float(t[int(np.argmax(mask))])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rps", type=int)
    ap.add_argument("--sweep-dir", type=Path, default=Path.cwd())
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-n", type=int, default=7,
                    help="largest pod count threshold to consider (default 7)")
    args = ap.parse_args()

    sweep = args.sweep_dir
    run_dirs = sorted(p for p in sweep.glob("benchmark*") if p.is_dir())
    if not run_dirs:
        print(f"error: no benchmark* run dirs under {sweep}", file=sys.stderr)
        return 1

    # per_strat[strat][service] = list of (t array, y array per service column)
    per_strat: dict[str, dict[str, list[tuple[np.ndarray, np.ndarray]]]] = {
        s: {} for s in STRATEGIES
    }
    services_ref: list[str] | None = None
    window_len = 0

    for strat in STRATEGIES:
        for rd in run_dirs:
            loaded = load_run(rd / strat / f"pods-{args.rps}-sum.csv")
            if loaded is None:
                continue
            services, t, arr = loaded
            if services_ref is None:
                services_ref = services
            window_len = max(window_len, int(t[-1]))
            for j, svc in enumerate(services):
                per_strat[strat].setdefault(svc, []).append((t, arr[:, j]))

    if services_ref is None:
        print("error: no data loaded", file=sys.stderr)
        return 1

    # drop services that never scale beyond 1 pod in any run on either stack
    interesting: list[str] = []
    for svc in services_ref:
        m = max(
            (y.max() for s in STRATEGIES for _, y in per_strat[s].get(svc, []))
        )
        if m >= 2:
            interesting.append(svc)

    n = len(interesting)
    ncols = min(2, n)
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(6.5 * ncols, 3.4 * nrows),
                             sharey=False, squeeze=False)

    never_y = window_len + 5

    for i, svc in enumerate(interesting):
        ax = axes[i // ncols][i % ncols]
        # determine N range for this service
        max_n_here = 1
        for strat in STRATEGIES:
            for _, y in per_strat[strat].get(svc, []):
                max_n_here = max(max_n_here, int(y.max()))
        max_n_here = min(max_n_here, args.max_n)
        ns = list(range(2, max_n_here + 1))
        if not ns:
            ax.text(0.5, 0.5, "never scales", ha="center", va="center", transform=ax.transAxes)
            ax.set_title(svc)
            continue

        for strat in STRATEGIES:
            color = COLORS[strat]
            for N in ns:
                vals = [first_ge(t, y, N) for t, y in per_strat[strat].get(svc, [])]
                reached = [v for v in vals if v is not None]
                never = sum(1 for v in vals if v is None)
                x = N + JITTER[strat]
                rng = np.random.default_rng(N * 17 + (0 if strat == "istio" else 1))
                xj = x + rng.uniform(-0.06, 0.06, size=len(reached))
                ax.scatter(xj, reached, s=22, color=color, alpha=0.75,
                           edgecolors="none")
                if reached:
                    med = float(np.median(reached))
                    ax.plot([x - 0.12, x + 0.12], [med, med], color=color, linewidth=2.2)
                if never:
                    xj2 = x + rng.uniform(-0.06, 0.06, size=never)
                    ax.scatter(xj2, [never_y] * never, s=28, facecolors="none",
                               edgecolors=color, linewidths=1.2)

        ax.set_xticks(ns)
        ax.set_xlim(ns[0] - 0.5, ns[-1] + 0.5)
        ax.set_ylim(-3, never_y + 5)
        ax.axhline(window_len, color="grey", linewidth=0.6, linestyle=":")
        ax.text(ns[-1] + 0.3, never_y, "never", color="grey", fontsize=8,
                ha="right", va="center")
        ax.set_xlabel("ready pods threshold N")
        ax.set_ylabel("time to reach N (s)")
        ax.set_title(svc)
        ax.grid(True, alpha=0.3, axis="y")

    for k in range(n, nrows * ncols):
        axes[k // ncols][k % ncols].axis("off")

    handles = [plt.Line2D([], [], marker="o", linestyle="", color=COLORS[s], label=s)
               for s in STRATEGIES]
    fig.legend(handles=handles, loc="upper center", ncol=len(STRATEGIES),
               bbox_to_anchor=(0.5, 1.0))
    fig.suptitle(f"Per-run time-to-reach N ready pods at {args.rps} rps "
                 f"(median bar, n={len(run_dirs)} runs)", y=1.03)
    fig.tight_layout()

    out = args.out or sweep / f"pod_scale_time_{args.rps}.pdf"
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
