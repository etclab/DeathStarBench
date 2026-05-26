#!/usr/bin/env python3
import glob
import numpy as np
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
dat_files = sorted(glob.glob(os.path.join(base_dir, "benchmark2-*/plot_e2e_latency.dat")))

print(f"Found {len(dat_files)} benchmark2 runs")

percentiles = []
istio_vals = {}
mazu_vals = {}
per_run_data = []

for f in dat_files:
    run_name = os.path.basename(os.path.dirname(f))
    with open(f) as fh:
        lines = fh.readlines()
    run_rows = {}
    for line in lines[1:]:
        parts = line.strip().split("\t")
        pct = parts[0]
        istio = float(parts[1])
        mazu = float(parts[2])
        if pct not in istio_vals:
            percentiles.append(pct)
            istio_vals[pct] = []
            mazu_vals[pct] = []
        istio_vals[pct].append(istio)
        mazu_vals[pct].append(mazu)
        run_rows[pct] = (istio, mazu)
    per_run_data.append((run_name, run_rows))

out_path = os.path.join(base_dir, "plot_e2e_latency.dat")
with open(out_path, "w") as fh:
    fh.write(f"# Individual values from {len(dat_files)} runs:\n")
    for run_name, run_rows in per_run_data:
        fh.write(f"# --- {run_name} ---\n")
        fh.write("# Percentile\tIstio\tMazu\n")
        for pct in percentiles:
            istio, mazu = run_rows[pct]
            fh.write(f"# {pct}\t{istio:.2f}\t{mazu:.2f}\n")
    fh.write("#\n# === Averaged values ===\n")
    fh.write("Percentile\tIstio_mean\t\tIstio_std\t\t\tMazu_mean\t\tMazu_std\n")
    for pct in percentiles:
        im = np.mean(istio_vals[pct])
        isd = np.std(istio_vals[pct], ddof=1)
        mm = np.mean(mazu_vals[pct])
        msd = np.std(mazu_vals[pct], ddof=1)
        fh.write(f"{pct}\t\t{im}\t{isd}\t{mm}\t{msd}\n")
        print(f"{pct}: Istio={im:.3f}±{isd:.3f}  Mazu={mm:.3f}±{msd:.3f}")

print(f"\nWrote {out_path}")
