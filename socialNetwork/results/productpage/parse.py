import argparse
from pathlib import Path

def get_percentile(filename, lookup):
    with open(filename, "r") as file:
        for line in file:
            if lookup in line:
                parts = line.split()
                duration = parts[0].strip()
                
                return float(duration)/1000
        return 0
    
def convert_to_dat(files, filename='out.dat'):
    rev_map = {}
    for strategy in files:
        for data in files[strategy]:
            if data not in rev_map:
                rev_map[data] = {}
            rev_map[data][strategy] = files[strategy][data]
    
    qps = [1000, 2000, 4000, 8000, 12000, 16000, 24000, 32000]
    with open(filename, 'w') as f:
        f.write("# p99 latency (ms) vs qps\n")
        f.write(f"{'# qps':<25} {'istio_mtls':<25} {'mazu_st2':<25} {'mazu_st3':<25} {'mazu_st4':<25} {'mazu_st5':<25}\n")

        for req in qps:
            req = f"{req}"
            f.write(f"{req:<25} {rev_map[req]['istio-120']:<25} {rev_map[req]['st2-NIChaRes-120']:<25} {rev_map[req]['st3-TokRev-120']:<25} {rev_map[req]['st4-AudUpd-120']:<25} {rev_map[req]['st5-AttUpd-120']:<25}\n")
            
def get_cli_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--out", required=True, help="File to output data")
    args = parser.parse_args()
    return args
    
if __name__ == "__main__":
    args = get_cli_args()
    
    # percentile_str = '99.000%'
    percentile_str = "0.990625"

    filepaths = ["20251216_085444/st2-NIChaRes-120", 
                 "20251216_095813/istio-120", 
                 "20251216_175512/st3-TokRev-120",
                 "20251218_083046/st4-AudUpd-120",
                 "20251218_141820/st5-AttUpd-120"]
    files = {}
    
    for folder in filepaths:
        strategy_dir = Path(folder)
        strategy = strategy_dir.name
        for rate_file in strategy_dir.iterdir():
            if rate_file.is_file():
                qps = rate_file.stem
                if strategy not in files:
                    files[strategy] = {}
                files[strategy][qps] = get_percentile(rate_file, percentile_str)
                           
    # print(files)
    convert_to_dat(files, args.out)