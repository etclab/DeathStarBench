#!/usr/bin/env python3
import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec

STRATEGIES = ["istio", "st5-AttUpd"]
STRAT_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
STRAT_COLORS = {"istio": "tab:blue", "st5-AttUpd": "tab:orange"}
SKIP_COLS = {"index", "timestamp", "total"}

SVC_COLORS = {
    "details-v1":     "#77AADD",
    "productpage-v1": "#EE8866",
    "ratings-v1":     "#EEDD88",
    "reviews-v1":     "#FFAABB",
    "reviews-v2":     "#44BB99",
    "reviews-v3":     "#BBCC33",
}

base = Path(__file__).parent


def load(csv_path):
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        services = [c for c in (reader.fieldnames or []) if c not in SKIP_COLS]
        data = {s: [] for s in services}
        for row in reader:
            for s in services:
                data[s].append(int(row[s]))
    return services, {s: np.array(v) for s, v in data.items()}


all_data = {}
services = None
for strat in STRATEGIES:
    csv_path = base / strat / "pods-400-sum.csv"
    if not csv_path.exists():
        print(f"missing {csv_path}", file=sys.stderr)
        continue
    svcs, data = load(csv_path)
    all_data[strat] = data
    if services is None:
        services = svcs

n_svc = len(services)
svc_rows = (n_svc + 1) // 2  # 2 per row
total_rows = 1 + svc_rows     # 1 top row + service comparison rows

fig = plt.figure(figsize=(10, 3.8 * total_rows))
gs = GridSpec(total_rows, 2, figure=fig, hspace=0.45, wspace=0.3)

# --- Top row: all services per strategy ---
ax_istio = fig.add_subplot(gs[0, 0])
ax_mazu = fig.add_subplot(gs[0, 1], sharey=ax_istio)

for ax, strat in zip([ax_istio, ax_mazu], STRATEGIES):
    if strat not in all_data:
        continue
    data = all_data[strat]
    for svc in services:
        t = np.arange(len(data[svc]))
        ax.plot(t, data[svc], color=SVC_COLORS.get(svc, "gray"), linewidth=1.8, label=svc)
    ax.set_title(STRAT_LABELS[strat], fontsize=12)
    ax.set_xlabel("Time (s)")
    ax.grid(True, alpha=0.3)

ax_istio.set_ylabel("Ready Pods")
plt.setp(ax_mazu.get_yticklabels(), visible=False)
handles, labels = ax_istio.get_legend_handles_labels()
fig.legend(handles, labels, loc="upper center", ncol=n_svc,
           bbox_to_anchor=(0.5, 1.02), fontsize=9)

# --- Remaining rows: per-service comparison, 2 per row ---
for i, svc in enumerate(services):
    row = 1 + i // 2
    col = i % 2
    ax = fig.add_subplot(gs[row, col])
    for strat in STRATEGIES:
        if strat not in all_data:
            continue
        data = all_data[strat]
        t = np.arange(len(data[svc]))
        ax.plot(t, data[svc], color=STRAT_COLORS[strat], linewidth=1.8,
                label=STRAT_LABELS[strat])
    ax.set_title(svc, fontsize=10)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Ready Pods")
    ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    ax.grid(True, alpha=0.3)
    if i == 0:
        ax.legend(fontsize=8)

fig.suptitle("Per-Service Pod Count at 400 RPS", y=1.04, fontsize=14)

out = base / "pods_per_service_400.pdf"
fig.savefig(out, bbox_inches="tight")
print(f"wrote {out}")
