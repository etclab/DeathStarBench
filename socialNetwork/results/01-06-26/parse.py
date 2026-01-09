import argparse
from pathlib import Path
import re

# Percentile lookup strings in the detailed spectrum
PERCENTILE_MAP = {
    50: "0.500000",
    90: "0.900000",
    99: "0.990625"  # Close to 99%
}

LIST_OF_STRATEGIES = ['istio', 'st2-NIChaRes', 'st3-TokRev', 'st4-AudUpd', 'st5-AttUpd']
LIST_OF_RPS = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500]

def get_percentile(filename, lookup):
    """Extract latency value for a given percentile from wrk2 output."""
    with open(filename, "r") as file:
        for line in file:
            if lookup in line:
                parts = line.split()
                duration = parts[0].strip()
                # Convert from milliseconds to seconds
                return float(duration)/1000
        return 0

def convert_to_dat(files, filename='out.dat', percentile=99):
    """Convert parsed data to gnuplot .dat format."""
    rev_map = {}
    for strategy in files:
        for data in files[strategy]:
            if data not in rev_map:
                rev_map[data] = {}
            rev_map[data][strategy] = files[strategy][data]

    qps = LIST_OF_RPS
    with open(filename, 'w') as f:
        f.write(f"# p{percentile} latency (s) vs qps\n")
        f.write(f"{'# qps':<15} {'istio':<15} {'st2-NIChaRes':<15} {'st3-TokRev':<15} {'st4-AudUpd':<15} {'st5-AttUpd':<15}\n")

        for req in qps:
            req_str = f"{req}"
            row = f"{req_str:<15} "
            for strategy in LIST_OF_STRATEGIES:
                val = rev_map.get(req_str, {}).get(strategy, 'N/A')
                if isinstance(val, float):
                    row += f"{val:<15.3f} "
                else:
                    row += f"{val:<15} "
            f.write(row.rstrip() + "\n")

def get_cli_args():
    parser = argparse.ArgumentParser(description="Parse wrk2 output files and generate gnuplot data")
    parser.add_argument("-o", "--out", required=True, help="File to output data")
    parser.add_argument("-p", "--percentile", type=int, choices=[50, 99, 90], default=99,
                        help="Percentile to extract (50 or 99, default: 99)")
    args = parser.parse_args()
    return args

def is_valid_result_file(filepath):
    """Check if file is a valid result file (not a failed run or log)."""
    name = filepath.name
    # Skip failed runs and log files
    if '.failed_' in name or name == 'run.log':
        return False
    # Must be a .txt file
    if not name.endswith('.txt'):
        return False
    return True

if __name__ == "__main__":
    args = get_cli_args()

    percentile_str = PERCENTILE_MAP[args.percentile]

    # Local directories
    filepaths = LIST_OF_STRATEGIES
    files = {}

    for folder in filepaths:
        strategy_dir = Path(folder)
        strategy = strategy_dir.name
        if not strategy_dir.exists():
            print(f"Warning: Directory {folder} not found, skipping...")
            continue
        for rate_file in strategy_dir.iterdir():
            if rate_file.is_file() and is_valid_result_file(rate_file):
                qps = rate_file.stem
                if strategy not in files:
                    files[strategy] = {}
                files[strategy][qps] = get_percentile(rate_file, percentile_str)

    convert_to_dat(files, args.out, args.percentile)
    print(f"Output written to {args.out}")