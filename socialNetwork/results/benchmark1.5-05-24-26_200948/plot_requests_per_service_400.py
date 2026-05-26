#!/usr/bin/env python3
"""Compare per-service inbound request rate (req/s) between strategies.

Parses requests-400.csv from each strategy folder.  The CSV contains
cumulative Envoy counters scraped ~every second.  This script:
  1. Filters to envoy_http_downstream_rq_completed (inbound) — the actual
     requests handled by each pod.
  2. Computes instantaneous rate (Δcount / Δt) per pod.
  3. Sums rates across all replicas of the same service at each timestamp.
  4. Plots a per-service comparison between the two strategies.
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec

STRATEGIES = ["istio", "st5-AttUpd"]
STRAT_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
STRAT_COLORS = {"istio": "tab:blue", "st5-AttUpd": "tab:orange"}

SVC_ORDER = [
    "productpage-v1", "details-v1", "ratings-v1",
    "reviews-v1", "reviews-v2", "reviews-v3",
]

SVC_COLORS = {
    "productpage-v1": "#EE8866",
    "details-v1":     "#77AADD",
    "ratings-v1":     "#EEDD88",
    "reviews-v1":     "#FFAABB",
    "reviews-v2":     "#44BB99",
    "reviews-v3":     "#BBCC33",
}

INBOUND_PATTERN = re.compile(
    r'envoy_http_downstream_rq_completed\{http_conn_manager_prefix="inbound_'
)

base = Path(__file__).parent


def service_from_pod(pod_name: str) -> str:
    """Extract service name including version (e.g. reviews-v1).

    Pod names look like 'reviews-v2-57ccd5bd84-ws6pw'.  We keep
    '{name}-{version}' and strip the replicaset/pod hashes.
    """
    parts = pod_name.split("-")
    for i, p in enumerate(parts):
        if re.match(r"^v\d+$", p):
            return "-".join(parts[:i + 1])
    return parts[0]


def extract_count(metric_line: str) -> int:
    """Pull the trailing integer value from a metric line like '... 1234'."""
    return int(metric_line.rstrip('"').rsplit(" ", 1)[-1])


def load_cumulative(csv_path: Path):
    """Return {service: (time_s_array, cumulative_requests_array)}.

    Each pod's counter is normalized to start from 0 (subtract first reading),
    then all replicas of the same service are summed at each timestamp.
    """
    pod_ts = defaultdict(list)
    pod_val = defaultdict(list)

    with csv_path.open(newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            ts_str, pod, metric_line = row[0], row[1], row[2]
            if not INBOUND_PATTERN.search(metric_line):
                continue
            ts = int(ts_str)
            count = extract_count(metric_line)
            pod_ts[pod].append(ts)
            pod_val[pod].append(count)

    pod_baseline = {}
    for pod, vals in pod_val.items():
        pod_baseline[pod] = vals[0]

    svc_cumul = defaultdict(lambda: defaultdict(float))

    for pod, timestamps in pod_ts.items():
        svc = service_from_pod(pod)
        vals = pod_val[pod]
        base_val = pod_baseline[pod]
        for i, ts in enumerate(timestamps):
            svc_cumul[svc][ts] += vals[i] - base_val

    result = {}
    for svc, ts_count in svc_cumul.items():
        sorted_ts = sorted(ts_count.keys())
        t0 = sorted_ts[0]
        t_arr = np.array([t - t0 for t in sorted_ts], dtype=float)
        c_arr = np.array([ts_count[t] for t in sorted_ts], dtype=float)
        result[svc] = (t_arr, c_arr)

    return result


all_data = {}
for strat in STRATEGIES:
    csv_path = base / strat / "requests-400.csv"
    if not csv_path.exists():
        print(f"missing {csv_path}", file=sys.stderr)
        continue
    all_data[strat] = load_cumulative(csv_path)

services = SVC_ORDER
n_svc = len(services)
svc_rows = (n_svc + 1) // 2
total_rows = 1 + svc_rows

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
        if svc not in data:
            continue
        t, r = data[svc]
        ax.plot(t, r, color=SVC_COLORS.get(svc, "gray"), linewidth=1.4, label=svc)
    ax.set_title(STRAT_LABELS[strat], fontsize=12)
    ax.set_xlabel("Time (s)")
    ax.grid(True, alpha=0.3)

ax_istio.set_ylabel("Cumulative Requests")
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
        if strat not in all_data or svc not in all_data[strat]:
            continue
        t, r = all_data[strat][svc]
        ax.plot(t, r, color=STRAT_COLORS[strat], linewidth=1.4,
                label=STRAT_LABELS[strat])
    ax.set_title(svc, fontsize=10)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Cumulative Requests")
    ax.grid(True, alpha=0.3)
    if i == 0:
        ax.legend(fontsize=8)

fig.suptitle("Per-Service Cumulative Inbound Requests at 400 RPS", y=1.04, fontsize=14)

out = base / "requests_per_service_400.pdf"
fig.savefig(out, bbox_inches="tight")
print(f"wrote {out}")

# --- Print summary table ---
print("\n{:<15s}  {:>14s}  {:>14s}".format("Service", "Istio total", "Mazu total"))
print("-" * 47)
for svc in services:
    vals = []
    for strat in STRATEGIES:
        if strat in all_data and svc in all_data[strat]:
            _, c = all_data[strat][svc]
            vals.append(f"{c[-1]:.0f}")
        else:
            vals.append("n/a")
    print(f"{svc:<15s}  {vals[0]:>14s}  {vals[1]:>14s}")

# --- Write JSON with full time series + summary ---
import json

json_out = {}
for strat in STRATEGIES:
    label = STRAT_LABELS[strat]
    json_out[label] = {}
    if strat not in all_data:
        continue
    for svc in services:
        if svc not in all_data[strat]:
            continue
        t, r = all_data[strat][svc]
        json_out[label][svc] = {
            "time_s": t.tolist(),
            "cumulative_requests": [round(v, 2) for v in r.tolist()],
            "total_requests": round(float(r[-1]), 2),
        }

json_path = base / "requests_per_service_400.json"
with json_path.open("w") as f:
    json.dump(json_out, f, indent=2)
print(f"wrote {json_path}")
