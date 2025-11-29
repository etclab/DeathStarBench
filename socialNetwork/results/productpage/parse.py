from pathlib import Path

def get_percentile(filename, lookup):
    with open(filename, "r") as file:
        for line in file:
            if line.strip().startswith(lookup):
                parts = line.split()
                duration = parts[1].strip()
                
                if duration.endswith('us'):
                    duration = float(duration[:-2])/1000
                elif duration.endswith('ms'):
                    duration = float(duration[:-2])
                elif duration.endswith('s'):
                    duration = float(duration[:-1])*1000
                elif duration.endswith('m'):
                    duration = float(duration[:-1])*60*1000
                
                return duration
        # if not found
        return 0
    
def convert_to_dat(files, filename='out.dat'):
    rev_map = {}
    for strategy in files:
        for data in files[strategy]:
            if data not in rev_map:
                rev_map[data] = {}
            rev_map[data][strategy] = files[strategy][data]
    
    qps = [100, 250, 500, 750, 1000]
    with open(filename, 'w') as f:
        f.write("# p99 latency (ms) vs qps\n")
        f.write(f"{'# qps':<25} {'istio_mtls':<25} {'mazu_st2':<25} {'mazu_st3':<25} {'mazu_st4':<25} {'mazu_st5':<25}\n")

        for req in qps:
            req = f"{req}"
            f.write(f"{req:<25} {rev_map[req]['istio']:<25} {rev_map[req]['st2-NIChaRes']:<25} {rev_map[req]['st3-TokRev']:<25} {rev_map[req]['st4-AudUpd']:<25} {rev_map[req]['st5-AttUpd']:<25}\n")
            
            
    
if __name__ == "__main__":
    percentile_str = '99.000%'

    curr_dir = Path.cwd()
    print(f"enumerating files inside: {curr_dir}")

    files = {}
    for timestamp_dir in curr_dir.iterdir():
        if timestamp_dir.is_dir():
            for strategy_dir in timestamp_dir.iterdir():
                if strategy_dir.is_dir():
                    directory_name = strategy_dir.name.removesuffix('-60')
                    # read all the files
                    for rate_file in strategy_dir.iterdir():
                        if rate_file.is_file():
                            qps = rate_file.stem
                            if directory_name not in files:
                                files[directory_name] = {}
                            
                            files[directory_name][qps] = get_percentile(rate_file, percentile_str)
                           
    # print(files)
    convert_to_dat(files)