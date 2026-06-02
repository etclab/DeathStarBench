# Pod Scaling Analysis Scripts

Documentation for the four Python analysis scripts in this directory
(`scaling-on-bench1.5-may21`). They analyze how many pods the autoscaler
provisions during the 120s load benchmark, comparing two strategies:

- **`st5-AttUpd`** — referred to as **"mazu"** (plotted in `tab:blue`)
- **`istio`** — baseline (plotted in `tab:orange`)

## Data layout (input)

```
scaling-on-bench1.5-may21/
├── benchmark1.5-05-20-26_235948/     # one of 10 benchmark RUN folders (benchmark1.5-*)
│   ├── istio/
│   │   ├── pods-50-sum.csv
│   │   ├── pods-100-sum.csv
│   │   ├── ...
│   │   └── pods-1200-sum.csv
│   └── st5-AttUpd/
│       └── pods-<rps>-sum.csv ...
├── benchmark1.5-05-21-26_020438/
└── ... (10 run folders total)
```

- **10 run folders** matching `benchmark1.5-*` (repeated trials).
- **2 strategy subfolders** per run: `istio` and `st5-AttUpd`.
- **13 RPS levels**: 50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200.

### `pods-<rps>-sum.csv` format

One row per second over the 120s benchmark. Header:

```
index,timestamp,details-v1,productpage-v1,ratings-v1,reviews-v1,reviews-v2,reviews-v3,total
```

- `index` — per-second offset (0, 1, 2, …, ~119). **Used to align across runs.**
- `timestamp` — unix epoch (not used by the scripts).
- `details-v1`, `productpage-v1`, `ratings-v1`, `reviews-v1`, `reviews-v2`,
  `reviews-v3` — **per-service** pod counts at that second.
- `total` — sum of all service pod counts at that second.

> Note: row counts vary slightly between runs (119 vs 120 rows). All scripts
> align by the `index` column and aggregate over whatever runs have data for
> each second, so this is handled gracefully.

### Common conventions across all three scripts

- Locate run folders via `glob` on `os.path.dirname(os.path.abspath(__file__))`
  — they work regardless of the current working directory.
- Aggregate across the 10 runs using **mean** and **sample standard deviation**
  (`statistics.stdev`, n−1 divisor; `0.0` when only one run is present).
- Matplotlib `Agg` (headless) backend; output to PDF.
- Missing CSV files are skipped gracefully.

---

## 1. `analyze_pod_totals.py`

**Question answered:** What is the *final* total pod count (across all services)
at the end of the 120s benchmark, and how does it vary by RPS?

**Method:** For each run / strategy / RPS, takes the `total` column of the
**last row** of `pods-<rps>-sum.csv` (the steady-state pod count at end of
benchmark). Then computes mean ± sample stddev across the 10 runs.

**No CLI args** — always processes all runs and all 13 RPS levels.

```bash
python3 analyze_pod_totals.py
```

**Outputs:**
| File | Contents |
|------|----------|
| `pod_totals_raw.csv` | One row per (strategy, rps, run): `strategy,rps,run,total_pods` |
| `pod_totals_summary.csv` | Per (strategy, rps): `strategy,rps,n_runs,mean_total_pods,stddev_total_pods` |
| `pod_totals_mazu_vs_istio.pdf` | Line chart: **x = RPS, y = mean final total pods**, one line per strategy, with ±1σ **error bars** |

Both CSVs are also echoed to stdout.

---

## 2. `analyze_pod_growth.py`

**Question answered:** How does the *total* pod count grow **over time
(per second)** during the benchmark, for a given RPS?

**Method:** For each run / strategy, reads the `total` column indexed by second
(`index`). Aligns the 10 runs by second and computes mean ± sample stddev of
`total` at each second.

**CLI:** optional positional `rps`. Omit to process **all 13** RPS levels.

```bash
python3 analyze_pod_growth.py 400     # just 400 RPS
python3 analyze_pod_growth.py         # all 13 RPS levels
```

**Outputs:**
| File | Contents |
|------|----------|
| `pod_growth_raw.csv` (all-RPS) **or** `pod_growth_raw_<rps>.csv` (single) | Long format: `strategy,rps,second,n_runs,mean_total_pods,stddev_total_pods` |
| `pod_growth_<rps>.pdf` (one per processed RPS) | Line chart: **x = second (0–~119), y = mean total pods**, one line per strategy, with ±1σ **shaded band** (`fill_between`, alpha 0.2) |

---

## 3. `analyze_pod_growth_per_service.py`

**Question answered:** Per-second pod growth **broken down by individual
service** — i.e. *which service is the scaling bottleneck?*

**Method:** Same per-second aggregation as script 2, but applied to each of the
6 per-service columns (`details-v1`, `productpage-v1`, `ratings-v1`,
`reviews-v1`, `reviews-v2`, `reviews-v3`) instead of `total`.

**CLI:** optional positional `rps`, **defaults to 400**.

```bash
python3 analyze_pod_growth_per_service.py        # defaults to 400 RPS
python3 analyze_pod_growth_per_service.py 600    # any RPS level
```

**Outputs:**
| File | Contents |
|------|----------|
| `pod_growth_per_service_raw_<rps>.csv` | Long format: `strategy,service,rps,second,n_runs,mean_pods,stddev_pods` |
| `pod_growth_per_service_<rps>.pdf` | **2×3 grid of subplots, one per service.** Each: x = second, y = mean pods, one line per strategy, ±1σ shaded band. **Y-axes are independent per subplot** so the bottleneck service is visible despite very different scales. |

---

## 4. `compare_pod_growth_two_runs.py`

**Question answered:** Are **two specific runs consistent** with each other? Unlike
scripts 2 & 3 (which aggregate a mean ±σ curve over *all* `benchmark1.5-*`
folders), this isolates **two named runs** and overlays them, reporting both
*total* pod growth and *per-service* growth in one pass.

**Method:** For each of the two runs / each strategy, reads `pods-<rps>-sum.csv`
indexed by second (`index`). Plots each run's raw per-second curve (no
aggregation, no error band) so run-to-run divergence is directly visible. Runs
are distinguished by color; in the per-service plot strategies are distinguished
by line style (mazu solid, istio dashed).

**CLI:** optional positional `run_a`, `run_b`, `rps`. Run tokens may be a full
folder name or just enough of the trailing timestamp to disambiguate (e.g.
`020438`). Defaults to runs `235948` and `020438` at **400 RPS**.

```bash
python3 compare_pod_growth_two_runs.py                       # 235948 vs 020438 @ 400
python3 compare_pod_growth_two_runs.py 235948 020438 800     # any two runs, any RPS
```

**Outputs:**
| File | Contents |
|------|----------|
| `compare_pod_growth_total_<runA>_vs_<runB>_<rps>.pdf` | 1×2 grid, one subplot per strategy, overlaying the two runs' **total pods/sec** (legend notes final & peak) |
| `compare_pod_growth_per_service_<runA>_vs_<runB>_<rps>.pdf` | 2×3 grid, one subplot per service; **run = color, strategy = line style** (mazu solid, istio dashed) |
| `compare_pod_growth_<runA>_vs_<runB>_<rps>.csv` | Long format: `strategy,service,run,rps,second,pods` (`service == "total"` for the total row) |

A console summary prints final/peak/start total pods per run and a per-service
peak-pods table.

---

## Findings so far (at 400 RPS, n=10 runs)

**Total pods (scripts 1 & 2):** mazu and istio track closely; both plateau
around ~18 (mazu) and ~16 (istio) pods. Ramp timing is similar; mazu ends
slightly higher.

**Per-service bottleneck (script 3), peak mean pods:**

| service | mazu | istio |
|---|---|---|
| **productpage-v1** | **7.20** | **6.70** |
| reviews-v3 | 3.30 | 2.40 |
| reviews-v2 | 3.20 | 2.70 |
| details-v1 | 1.90 | 1.60 |
| reviews-v1 | 1.70 | 1.70 |
| ratings-v1 | 1.00 | 1.00 |

- **`productpage-v1` is the bottleneck** — the frontend aggregator scales to
  ~7 pods (~2× any other service).
- `ratings-v1` never scales (flat at 1); `reviews-v1` is identical between
  strategies.
- mazu provisions slightly more replicas on every service that scales, with the
  largest gap on `reviews-v3` (3.30 vs 2.40).

---

## Quick reference

```bash
# Final total pods vs RPS (all runs, all RPS)
python3 analyze_pod_totals.py

# Total pod growth over time (one RPS, or all)
python3 analyze_pod_growth.py 400
python3 analyze_pod_growth.py

# Per-service pod growth over time (bottleneck analysis; default RPS=400)
python3 analyze_pod_growth_per_service.py 400

# Compare two specific runs (total + per-service overlay; default 235948 vs 020438 @ 400)
python3 compare_pod_growth_two_runs.py
python3 compare_pod_growth_two_runs.py 235948 020438 800
```

**Dependencies:** Python 3 + `matplotlib` (everything else is stdlib: `csv`,
`glob`, `os`, `statistics`, `argparse`).
