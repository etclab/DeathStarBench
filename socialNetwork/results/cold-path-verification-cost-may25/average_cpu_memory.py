#!/usr/bin/env python3
import glob
import numpy as np
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
dat_files_cpu = sorted(glob.glob(os.path.join(base_dir, "benchmark2-*/plot_cpu.dat")))
dat_files_mem = sorted(glob.glob(os.path.join(base_dir, "benchmark2-*/plot_memory.dat")))

print(f"Found {len(dat_files_cpu)} CPU runs, {len(dat_files_mem)} memory runs")


def parse_dat(path):
    rows = {}
    components_order = []
    with open(path) as fh:
        lines = fh.readlines()
    for line in lines[1:]:
        parts = line.strip().split("\t")
        comp = parts[0]
        istio = float(parts[1])
        mazu = float(parts[2])
        rows[comp] = (istio, mazu)
        components_order.append(comp)
    return components_order, rows


def average_and_write(dat_files, out_dat, metric_label, value_label):
    components = []
    istio_vals = {}
    mazu_vals = {}
    per_run_data = []

    for f in dat_files:
        run_name = os.path.basename(os.path.dirname(f))
        comp_order, rows = parse_dat(f)
        if not components:
            components = comp_order
            for c in components:
                istio_vals[c] = []
                mazu_vals[c] = []
        for c in components:
            istio, mazu = rows[c]
            istio_vals[c].append(istio)
            mazu_vals[c].append(mazu)
        per_run_data.append((run_name, rows))

    out_path = os.path.join(base_dir, out_dat)
    with open(out_path, "w") as fh:
        fh.write(f"# Individual {metric_label} values from {len(dat_files)} runs:\n")
        for run_name, run_rows in per_run_data:
            fh.write(f"# --- {run_name} ---\n")
            fh.write(f"# Component\tIstio\tMazu\n")
            for c in components:
                istio, mazu = run_rows[c]
                fh.write(f"# {c}\t{istio}\t{mazu}\n")
        fh.write("#\n# === Averaged values ===\n")
        fh.write("Component\tIstio_mean\tIstio_std\tMazu_mean\tMazu_std\n")
        for c in components:
            im = np.mean(istio_vals[c])
            isd = np.std(istio_vals[c], ddof=1)
            mm = np.mean(mazu_vals[c])
            msd = np.std(mazu_vals[c], ddof=1)
            fh.write(f"{c}\t{im}\t{isd}\t{mm}\t{msd}\n")
            print(f"  {c}: Istio={im:.6f}±{isd:.6f}  Mazu={mm:.6f}±{msd:.6f}")

    print(f"Wrote {out_path}\n")


print("\n=== CPU ===")
average_and_write(dat_files_cpu, "plot_cpu.dat", "CPU", "cores")

print("=== Memory ===")
average_and_write(dat_files_mem, "plot_memory.dat", "Memory", "MB")
