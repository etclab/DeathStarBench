#!/usr/bin/env python3
"""
Analyze container-cpu-400.csv from each strategy directory.
For each service at each timestamp, sum total CPU and memory across all pods
(both app container and istio-proxy sidecar).
Outputs JSON and plots CPU usage growth per service over time.
"""

import csv
import json
import os
import re
import sys
from collections import defaultdict

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGIES = ["istio", "st5-AttUpd"]
CSV_FILENAME = "container-cpu-400.csv"


def extract_service(pod_name):
    """Extract service name (e.g., 'reviews-v3') from pod name like 'reviews-v3-5c49d9d5b8-rgnng'."""
    match = re.match(r"^(.*?-v\d+)-", pod_name)
    if match:
        return match.group(1)
    return pod_name.rsplit("-", 2)[0] if pod_name.count("-") >= 2 else pod_name


def parse_csv(filepath):
    """Parse CSV and return per-service, per-second aggregated data."""
    # service -> timestamp -> {cpu_total, mem_total, cpu_app, cpu_sidecar, mem_app, mem_sidecar}
    service_data = defaultdict(lambda: defaultdict(lambda: {
        "cpu_total": 0, "mem_total": 0,
        "cpu_app": 0, "cpu_sidecar": 0,
        "mem_app": 0, "mem_sidecar": 0,
    }))

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp"])
            pod = row["pod"]
            container = row["container"]
            cpu = int(row["cpu_millicores"])
            mem = int(row["memory_mib"])

            service = extract_service(pod)
            entry = service_data[service][ts]
            entry["cpu_total"] += cpu
            entry["mem_total"] += mem

            if container == "istio-proxy":
                entry["cpu_sidecar"] += cpu
                entry["mem_sidecar"] += mem
            else:
                entry["cpu_app"] += cpu
                entry["mem_app"] += mem

    return service_data


def build_json(service_data, strategy_name):
    """Convert service_data to a JSON-serializable structure."""
    result = {"strategy": strategy_name, "services": {}}
    for service in sorted(service_data.keys()):
        ts_dict = service_data[service]
        timeseries = []
        for ts in sorted(ts_dict.keys()):
            entry = ts_dict[ts]
            timeseries.append({
                "timestamp": ts,
                "cpu_total_millicores": entry["cpu_total"],
                "memory_total_mib": entry["mem_total"],
                "cpu_app_millicores": entry["cpu_app"],
                "cpu_sidecar_millicores": entry["cpu_sidecar"],
                "memory_app_mib": entry["mem_app"],
                "memory_sidecar_mib": entry["mem_sidecar"],
            })
        result["services"][service] = timeseries
    return result


def plot_cpu_growth(all_strategy_data):
    """Plot CPU usage growth per service, one subplot per strategy."""
    n_strategies = len(all_strategy_data)
    fig, axes = plt.subplots(n_strategies, 1, figsize=(12, 5 * n_strategies),
                             sharex=False, squeeze=False)

    for idx, (strategy_name, service_data) in enumerate(all_strategy_data.items()):
        ax = axes[idx, 0]
        for service in sorted(service_data.keys()):
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            cpu_values = [ts_dict[t]["cpu_total"] for t in timestamps]
            ax.plot(elapsed, cpu_values, label=service, linewidth=1.5)

        ax.set_title(f"CPU Usage per Service — {strategy_name}", fontsize=13)
        ax.set_xlabel("Time (seconds since start)")
        ax.set_ylabel("Total CPU (millicores)")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{int(x)}"))

    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "cpu_growth_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Plot saved to: {out_path}")
    plt.close()


STRATEGY_LABELS = {"istio": "Istio", "st5-AttUpd": "Mazu"}
STRATEGY_COLORS = {"istio": "#1f77b4", "st5-AttUpd": "#d62728"}


def plot_cpu_comparison_per_service(all_strategy_data):
    """One subplot per service comparing istio vs mazu CPU growth."""
    all_services = sorted(set(
        svc for sd in all_strategy_data.values() for svc in sd.keys()
    ))
    n_services = len(all_services)
    cols = 2
    rows = (n_services + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(14, 4 * rows), squeeze=False)

    for i, service in enumerate(all_services):
        ax = axes[i // cols, i % cols]
        for strategy, service_data in all_strategy_data.items():
            if service not in service_data:
                continue
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            cpu_values = [ts_dict[t]["cpu_total"] for t in timestamps]
            label = STRATEGY_LABELS.get(strategy, strategy)
            color = STRATEGY_COLORS.get(strategy, None)
            ax.plot(elapsed, cpu_values, label=label, linewidth=1.5, color=color)

        ax.set_title(service, fontsize=12)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("CPU (millicores)")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)

    # Hide unused subplots
    for j in range(n_services, rows * cols):
        axes[j // cols, j % cols].set_visible(False)

    fig.suptitle("CPU Growth: Istio vs Mazu per Service", fontsize=14, y=1.01)
    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "cpu_comparison_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Comparison plot saved to: {out_path}")
    plt.close()


def plot_app_cpu_growth(all_strategy_data):
    """Plot app container CPU usage per service, one subplot per strategy."""
    n_strategies = len(all_strategy_data)
    fig, axes = plt.subplots(n_strategies, 1, figsize=(12, 5 * n_strategies),
                             sharex=False, squeeze=False)

    for idx, (strategy_name, service_data) in enumerate(all_strategy_data.items()):
        ax = axes[idx, 0]
        for service in sorted(service_data.keys()):
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            values = [ts_dict[t]["cpu_app"] for t in timestamps]
            ax.plot(elapsed, values, label=service, linewidth=1.5)

        ax.set_title(f"App Container CPU per Service — {strategy_name}", fontsize=13)
        ax.set_xlabel("Time (seconds since start)")
        ax.set_ylabel("App CPU (millicores)")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "cpu_app_growth_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Plot saved to: {out_path}")
    plt.close()


def plot_app_cpu_comparison_per_service(all_strategy_data):
    """One subplot per service comparing istio vs mazu app CPU."""
    all_services = sorted(set(
        svc for sd in all_strategy_data.values() for svc in sd.keys()
    ))
    n_services = len(all_services)
    cols = 2
    rows = (n_services + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(14, 4 * rows), squeeze=False)

    for i, service in enumerate(all_services):
        ax = axes[i // cols, i % cols]
        for strategy, service_data in all_strategy_data.items():
            if service not in service_data:
                continue
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            values = [ts_dict[t]["cpu_app"] for t in timestamps]
            label = STRATEGY_LABELS.get(strategy, strategy)
            color = STRATEGY_COLORS.get(strategy, None)
            ax.plot(elapsed, values, label=label, linewidth=1.5, color=color)

        ax.set_title(service, fontsize=12)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("App CPU (millicores)")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)

    for j in range(n_services, rows * cols):
        axes[j // cols, j % cols].set_visible(False)

    fig.suptitle("App CPU Growth: Istio vs Mazu per Service", fontsize=14, y=1.01)
    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "cpu_app_comparison_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Comparison plot saved to: {out_path}")
    plt.close()


def plot_sidecar_cpu_growth(all_strategy_data):
    """Plot sidecar CPU usage per service, one subplot per strategy."""
    n_strategies = len(all_strategy_data)
    fig, axes = plt.subplots(n_strategies, 1, figsize=(12, 5 * n_strategies),
                             sharex=False, squeeze=False)

    for idx, (strategy_name, service_data) in enumerate(all_strategy_data.items()):
        ax = axes[idx, 0]
        for service in sorted(service_data.keys()):
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            values = [ts_dict[t]["cpu_sidecar"] for t in timestamps]
            ax.plot(elapsed, values, label=service, linewidth=1.5)

        ax.set_title(f"Sidecar CPU per Service — {strategy_name}", fontsize=13)
        ax.set_xlabel("Time (seconds since start)")
        ax.set_ylabel("Sidecar CPU (millicores)")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "cpu_sidecar_growth_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Plot saved to: {out_path}")
    plt.close()


def plot_sidecar_cpu_comparison_per_service(all_strategy_data):
    """One subplot per service comparing istio vs mazu sidecar CPU."""
    all_services = sorted(set(
        svc for sd in all_strategy_data.values() for svc in sd.keys()
    ))
    n_services = len(all_services)
    cols = 2
    rows = (n_services + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(14, 4 * rows), squeeze=False)

    for i, service in enumerate(all_services):
        ax = axes[i // cols, i % cols]
        for strategy, service_data in all_strategy_data.items():
            if service not in service_data:
                continue
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            values = [ts_dict[t]["cpu_sidecar"] for t in timestamps]
            label = STRATEGY_LABELS.get(strategy, strategy)
            color = STRATEGY_COLORS.get(strategy, None)
            ax.plot(elapsed, values, label=label, linewidth=1.5, color=color)

        ax.set_title(service, fontsize=12)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Sidecar CPU (millicores)")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)

    for j in range(n_services, rows * cols):
        axes[j // cols, j % cols].set_visible(False)

    fig.suptitle("Sidecar CPU Growth: Istio vs Mazu per Service", fontsize=14, y=1.01)
    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "cpu_sidecar_comparison_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Comparison plot saved to: {out_path}")
    plt.close()


def plot_memory_growth(all_strategy_data):
    """Plot memory usage growth per service, one subplot per strategy."""
    n_strategies = len(all_strategy_data)
    fig, axes = plt.subplots(n_strategies, 1, figsize=(12, 5 * n_strategies),
                             sharex=False, squeeze=False)

    for idx, (strategy_name, service_data) in enumerate(all_strategy_data.items()):
        ax = axes[idx, 0]
        for service in sorted(service_data.keys()):
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            mem_values = [ts_dict[t]["mem_total"] for t in timestamps]
            ax.plot(elapsed, mem_values, label=service, linewidth=1.5)

        ax.set_title(f"Memory Usage per Service — {strategy_name}", fontsize=13)
        ax.set_xlabel("Time (seconds since start)")
        ax.set_ylabel("Total Memory (MiB)")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "memory_growth_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Plot saved to: {out_path}")
    plt.close()


def plot_memory_comparison_per_service(all_strategy_data):
    """One subplot per service comparing istio vs mazu memory growth."""
    all_services = sorted(set(
        svc for sd in all_strategy_data.values() for svc in sd.keys()
    ))
    n_services = len(all_services)
    cols = 2
    rows = (n_services + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(14, 4 * rows), squeeze=False)

    for i, service in enumerate(all_services):
        ax = axes[i // cols, i % cols]
        for strategy, service_data in all_strategy_data.items():
            if service not in service_data:
                continue
            ts_dict = service_data[service]
            timestamps = sorted(ts_dict.keys())
            t0 = timestamps[0]
            elapsed = [t - t0 for t in timestamps]
            mem_values = [ts_dict[t]["mem_total"] for t in timestamps]
            label = STRATEGY_LABELS.get(strategy, strategy)
            color = STRATEGY_COLORS.get(strategy, None)
            ax.plot(elapsed, mem_values, label=label, linewidth=1.5, color=color)

        ax.set_title(service, fontsize=12)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Memory (MiB)")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)

    for j in range(n_services, rows * cols):
        axes[j // cols, j % cols].set_visible(False)

    fig.suptitle("Memory Growth: Istio vs Mazu per Service", fontsize=14, y=1.01)
    plt.tight_layout()
    out_path = os.path.join(BASE_DIR, "memory_comparison_per_service.pdf")
    plt.savefig(out_path, bbox_inches="tight")
    print(f"Comparison plot saved to: {out_path}")
    plt.close()


def main():
    all_strategy_data = {}
    all_json = []

    for strategy in STRATEGIES:
        csv_path = os.path.join(BASE_DIR, strategy, CSV_FILENAME)
        if not os.path.exists(csv_path):
            print(f"WARNING: {csv_path} not found, skipping.")
            continue

        print(f"Parsing {strategy}...")
        service_data = parse_csv(csv_path)
        all_strategy_data[strategy] = service_data

        json_data = build_json(service_data, strategy)
        all_json.append(json_data)

    # Write JSON output
    json_path = os.path.join(BASE_DIR, "container_cpu_memory_summary.json")
    with open(json_path, "w") as f:
        json.dump(all_json, f, indent=2)
    print(f"JSON written to: {json_path}")

    # Plot
    plot_cpu_growth(all_strategy_data)
    plot_cpu_comparison_per_service(all_strategy_data)
    plot_app_cpu_growth(all_strategy_data)
    plot_app_cpu_comparison_per_service(all_strategy_data)
    plot_sidecar_cpu_growth(all_strategy_data)
    plot_sidecar_cpu_comparison_per_service(all_strategy_data)
    plot_memory_growth(all_strategy_data)
    plot_memory_comparison_per_service(all_strategy_data)


if __name__ == "__main__":
    main()
