#!/usr/bin/env python3
import os
import glob
import numpy as np
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_DIRS = sorted(glob.glob(os.path.join(BASE_DIR, "benchmark2-05-25-26_*")))
DAT_NAME = "plot_latency_breakdown_v2.dat"
OUT_DAT = os.path.join(BASE_DIR, "plot_latency_breakdown_v2_avg.dat")
OUT_PDF = "latency_breakdown_v2_avg.pdf"

COLUMNS = ["kc_fetch", "counter_att", "rbe_proof", "challenge_resp", "token_review", "e2e"]
LABELS = ["p50", "p90", "p95", "p99"]

all_data = {label: {col: [] for col in COLUMNS} for label in LABELS}
raw_lines = []

for run_dir in RUN_DIRS:
    dat_path = os.path.join(run_dir, DAT_NAME)
    if not os.path.isfile(dat_path):
        print(f"WARNING: missing {dat_path}, skipping")
        continue
    run_name = os.path.basename(run_dir)
    with open(dat_path, "r") as f:
        lines = f.readlines()
    raw_lines.append(f"# --- {run_name} ---")
    raw_lines.append(f"# {lines[0].rstrip()}")
    for line in lines[1:]:
        parts = line.strip().split("\t")
        if len(parts) < 7:
            continue
        label = parts[0]
        raw_lines.append(f"# {line.rstrip()}")
        if label not in LABELS:
            continue
        for i, col in enumerate(COLUMNS):
            all_data[label][col].append(float(parts[i + 1]))

print(f"Processed {len(RUN_DIRS)} runs\n")

print("=== Average ± Std Dev ===")
with open(OUT_DAT, "w") as f:
    f.write(f"# Individual values from {len(RUN_DIRS)} runs:\n")
    for rl in raw_lines:
        f.write(rl + "\n")
    f.write("#\n# === Averaged values ===\n")
    header = "Label\t" + "\t".join(COLUMNS)
    f.write(header + "\n")
    for label in LABELS:
        vals = []
        for col in COLUMNS:
            arr = np.array(all_data[label][col])
            avg = np.mean(arr)
            std = np.std(arr, ddof=1)
            vals.append(avg)
            print(f"  {label} {col:>16s}: {avg:8.2f} ± {std:5.2f}")
        row = label + "\t" + "\t".join(f"{v:.2f}" for v in vals)
        f.write(row + "\n")

print(f"\nWrote averaged data to {OUT_DAT}")

result = subprocess.run(["gnuplot", "plot_latency_breakdown_v2_avg.gpi"],
                        cwd=BASE_DIR, capture_output=True, text=True)
if result.returncode == 0:
    print(f"Generated {OUT_PDF}")
else:
    print(f"gnuplot error:\n{result.stderr}")
