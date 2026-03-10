#!/usr/bin/env python3
import argparse
import os
import re
import glob

parser = argparse.ArgumentParser(description="Parse wrk2 latency results into a gnuplot .dat file")
parser.add_argument("directory", help="Directory containing wrk2 .txt result files")
args = parser.parse_args()

dir_path = os.path.abspath(args.directory)
txt_files = glob.glob(os.path.join(dir_path, "*.txt"))

def parse_latency(value_str):
    """Parse latency value, converting to ms if needed."""
    m = re.match(r"([\d.]+)(ms|s|us|m)", value_str)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2)
    if unit == "m":
        return val * 60 * 1000
    elif unit == "s":
        return val * 1000
    elif unit == "us":
        return val / 1000
    return val

results = []
for f in txt_files:
    qps = int(os.path.basename(f).replace(".txt", ""))
    with open(f) as fh:
        content = fh.read()
    p50_m = re.search(r"50\.000%\s+([\d.]+(?:ms|s|us|m))", content)
    p90_m = re.search(r"90\.000%\s+([\d.]+(?:ms|s|us|m))", content)
    p99_m = re.search(r"99\.000%\s+([\d.]+(?:ms|s|us|m))", content)
    if p50_m and p90_m and p99_m:
        p50 = parse_latency(p50_m.group(1))
        p90 = parse_latency(p90_m.group(1))
        p99 = parse_latency(p99_m.group(1))
        if p50 is not None and p90 is not None and p99 is not None:
            results.append((qps, p50, p90, p99))

results.sort(key=lambda x: x[0])

out_path = os.path.join(dir_path, "latency.dat")
with open(out_path, "w") as fh:
    fh.write("# RPS(ms)\tp50\tp90\tp99\n")
    for qps, p50, p90, p99 in results:
        fh.write(f"{qps}\t{p50}\t{p90}\t{p99}\n")

print(f"Wrote {out_path}")
