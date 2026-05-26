# Mazu vs Istio scaling lag investigation — 2026-05-22

Sweep dir: `results/scaling-on-bench1.5-may21/` (10 runs, RPS sweep 50…1200, HPA enabled).

## TL;DR

At **400 rps** mazu (`st5-AttUpd`) has visibly worse latency than istio. The cause is **not** lower mazu productpage capacity, **not** HPA misattribution, and **not** pod cold-start. It's **variance in HPA decision timing** for mazu: in some runs HPA fires at the same moment as istio, in others it fires ~15s later, while istio's HPA decision time is rock-steady across all runs.

Most plausible mechanism: mazu's async cert validation makes the sidecar's CPU profile **bimodal during warmup** — sometimes it spikes promptly (HPA fires on time), sometimes the async path defers work for ~15-30s before catching up in a burst (HPA fires late). This is hypothesis, not proven — see "Next steps" for what would settle it.

## Starting observation

From `pooled_intermediates/pooled_summary.txt`, the largest mazu vs istio gap (in both mean and stddev across runs) is at 400rps:

| stack | p50_pool | p50_mean | p50_std |
|---|---|---|---|
| istio | **43.8** | 1204.4 | 2491.9 |
| st5-AttUpd | **1588.3** | 2183.1 | 2945.9 |

The disparity between pooled-percentile and mean-of-per-run-percentile reveals:

- **istio is bimodal across runs**: pool p50 (43ms) much smaller than mean-of-run p50s (1204ms) means a few runs stayed healthy and dominate the pooled samples, while other runs collapsed. Some istio runs "escape" 400rps; others don't.
- **mazu is uniformly degraded**: pool p50 (1588ms) close to mean (2183ms) means every mazu run is in the bad regime.
- By 500rps both stacks are uniformly saturated (no escape for either).

## Hypothesis chain we walked

### H1: "Mazu's productpage scales slower than istio's, starving the entry-point service"

**Tested with**:
- `plot_pods_per_service_steps_rps.py 400` — per-run step traces of pod count per service.
- Direct numbers via inline scripts (see CPU/scaling tables below).

**Mean across 10 runs at 400rps for productpage-v1**:

| metric | istio | st5-AttUpd |
|---|---|---|
| time to first scale-out (≥2 pods), mean s | 33.8 | 34.7 |
| time to ≥3 pods | 36.9 | **36.7** |
| time to ≥4 pods | 68.0 | 75.1 |
| time-avg pods over the window | **3.69** | **3.69** |
| pod-seconds with ≥3 pods | 83.1 | 83.3 |
| end-of-window mean pods | 6.7 | **7.2** |

Mean view says: **productpage capacity is identical**, mazu actually ends with more pods. Hypothesis appears refuted at the mean.

But the mean was hiding the truth — averaging across 10 runs blurred the step transitions into smooth ramps.

### H2 (revised): "Per-run lag is real but variable" — confirmed

Single-run `pods-400-sum.csv` timelines (run `benchmark1.5-05-21-26_110416`) show clear lag:

- istio productpage: `t=0:1, t=31:3, t=60:4, t=61:5, t=106:7`
- mazu productpage: `t=0:1, t=46:2, t=47:3, t=91:4, t=92:5, t=107:7`

In *this* run, mazu was ~15s late to first scale-up and ~30s late to reach 4 pods. The lag is real per-run; averaging across 10 runs (with run-to-run timing variance ≥ the systematic offset) smears it out of view.

### H3: "Mazu pods aren't hitting HPA CPU threshold"

**Setup confirmed via YAMLs**:
- Deployment (`scratch/yaml/bookinfo-const.yaml`) is shared between stacks. productpage container request: `cpu: 1`, limit `cpu: 2`. proxyCPULimit: 2.
- HPA (`scratch/yaml/bf-hpa.yaml`) uses `type: ContainerResource` on BOTH `istio-proxy` AND `productpage` containers, `target: Utilization 70`, `selectPolicy: Max`. Whichever container saturates first triggers scale-up. scaleUp: `Pods +2 per 2s`, no stabilization window.
- Mazu is loaded **into** the `istio-proxy` container (mazu-config mounted at `/etc/mazu-config`, see `scratch/yaml/istio-operator.yaml`). No separate container. So the HPA's `container: istio-proxy` metric correctly accounts for mazu's CPU.

**Aggregate CPU at 400rps from `cpu.dat` (avg cores over 120s window, per replica)**:

| scenario | stack | n_active | sum cores | mean cores/pod | first-replica (pp) | max cores |
|---|---|---|---|---|---|---|
| 181553 (mazu lagged) | istio | 5 | 1.21 | 0.241 | 0.352 | 0.352 |
| 181553 (mazu lagged) | mazu | 5 | **2.39** | 0.479 | **0.462** | 0.771 |
| 144042 (lockstep)    | istio | 6 | 1.35 | 0.225 | 0.345 | 0.345 |
| 144042 (lockstep)    | mazu | 7 | 2.19 | 0.313 | 0.401 | 0.501 |

Key observations:
- Mazu's productpage burns ~1.6-2× the CPU istio's does at the same RPS (independent evidence of per-request overhead from async cert validation).
- **In the lagged run, mazu's first pp replica averaged 0.462 cores vs istio's 0.352** — mazu CPU was *higher*, not lower. So "CPU too low for HPA" is refuted directionally.
- Both stacks' app containers averaged below 0.7 cores (the 70% utilization threshold of a 1-core request), so **scale-up in both cases must have been triggered by the istio-proxy container metric**, not the app container.

⚠️ Caveat: `cpu.dat` is averaged over the full 120s window; we cannot read pre-scaling sub-window utilization from this file.

### H4: "The lag is in HPA decision time, not pod startup"

Compared two runs at 400rps for productpage:
- `benchmark1.5-05-21-26_162813` — mazu in lockstep with istio
- `benchmark1.5-05-21-26_181553` — mazu lagged

Tracked when pods **first appear** (any phase = HPA decision fired) vs when they become **ready**, using `pods-400.csv` (per-pod raw) and `pods-400-sum.csv` (ready counts):

| run | stack | t at first 1→3 trigger | t ready at 3 | startup gap |
|---|---|---|---|---|
| 162813 (lockstep) | istio | **28** | 31 | 3s |
| 162813 (lockstep) | mazu | **26** | 31 | 5s |
| 181553 (lagged) | istio | **28** | 31 | 3s |
| 181553 (lagged) | mazu | **41** | 46 | 5s |

Two clean facts:

1. **istio's HPA fires at t=28 in both runs** — no variance whatsoever.
2. **mazu's HPA fires at t=26 (162813) vs t=41 (181553)** — a 15-second swing between runs. In the lockstep run mazu was actually 2s *ahead* of istio.
3. **Pod startup time is consistent** at 3-5s create→ready for both stacks in both runs. The lag is *not* in pod cold-start, image pull, or scheduling.

Same pattern repeats at the next jump (3→5): istio at t=58 in both runs; mazu at t=56 (lockstep) vs t=86 (lagged). ~30s gap on the second decision.

**Since k8s, metrics-server, and HPA config are shared with istio (which is rock-steady), the variance has to come from mazu's actual CPU profile inside the istio-proxy container being bimodal during early warmup.**

## Working hypothesis (unproven)

Mazu's **async cert validation produces a bimodal sidecar CPU profile during the first ~45s of the 400rps window**:

- In some runs the async path executes work inline → sidecar CPU spikes promptly → HPA fires on time (or even slightly earlier than istio because mazu uses ~2× CPU per request once it does the work).
- In other runs the async path defers/buffers work → sidecar CPU stays below the 70% threshold for an extra 15-30s → HPA fires late → mazu plays catch-up and overshoots (which is why mazu ends with more pods than istio when it does scale, e.g. 7.2 vs 6.7 productpage end pods at 400rps in the mean).

This also explains:
- Why mazu's latency at 400rps is uniformly bad (pool ≈ mean): even when HPA fires on time, mazu's per-request overhead is enough to keep latency elevated.
- Why istio is bimodal (pool ≪ mean): istio runs that scale early stay healthy; runs that scale late saturate. istio's per-request overhead is low enough that escaping the saturation knee is possible.

## Open questions / next steps

1. **The decisive test**: capture per-second `istio-proxy` container CPU for productpage-v1 during each RPS window in a future sweep. Either via a kubectl-top poller (similar to the existing pod-count poller in `dev/setup-tpm-all-nodes.sh` and `run-benchmark1.5.sh`) or by scraping cAdvisor. If lagged-run mazu shows visibly lower sidecar CPU during t=0..40s than lockstep-run mazu, the hypothesis holds.

2. **Cheap alternative**: see if any HPA observations are captured in run dirs. Did `kubectl describe hpa` or HPA events get logged anywhere? (didn't dig — worth a quick `find . -name "*hpa*"` and `grep -r "ObservedCPU"` in `benchmark1.5-05-21-26_*`).

3. **Statistical confirmation of the bimodal hypothesis**: plot per-run HPA-fire time (first `t` where `present > ready`) for productpage across all 10 runs, both stacks. Expected shape: istio is a tight cluster around t=28; mazu is a wider spread or two clusters around t=26 and t=41. Could be done with the existing pods-400.csv parsing logic in a new script.

4. **Correlate per-run latency with per-run HPA-fire time**. If lagged HPA = bad latency in that run, that nails the causal chain. Per-run p50/p99 numbers are in the `metrics_400.json` files.

5. **Check whether the lag exists at other RPS levels too** (we only looked at 400). The pool-vs-mean signature at 300rps and 500rps in `pooled_summary.txt` suggests:
   - 300rps: both stacks bimodal (pool ≪ mean), about equal latency — probably both have "lucky" runs that escape, lag may not matter
   - 500rps: pool ≥ mean for both — universal saturation, scaling lag probably stops mattering once everyone is overloaded
   - The 400rps observation may be the only RPS where mazu's lag is the dominant cause of the gap.

## Scripts added during this investigation

All in `results/`:

- `plot_pods_timeseries_rps.py <rps>` — total ready pods vs time at one RPS, per-run faint lines + mean.
- `plot_pods_per_service_rps.py <rps>` — per-service mean±stddev band (turned out to *hide* the per-run step structure, kept for reference).
- `plot_pods_per_service_steps_rps.py <rps>` — per-service per-run step traces with median (better for seeing scaling timing).
- `plot_pod_scale_time_rps.py <rps>` — scatter of per-run "time to reach N pods" per service (kept but harder to read than the step plot).

PDFs generated for 400rps:
- `pods_timeseries_400.pdf`
- `pods_per_service_400.pdf` — the mean+band one
- `pods_per_service_steps_400.pdf` — **the useful one**, shows the lag directly
- `pod_scale_time_400.pdf`

## Key file locations

- Run dirs: `results/scaling-on-bench1.5-may21/benchmark1.5-05-2*-26_*/`
- Per RPS, per stack:
  - `<run>/<strat>/pods-<rps>.csv` — raw per-pod observations (timestamp,app,version,phase,ready) at 1Hz
  - `<run>/<strat>/pods-<rps>-sum.csv` — per-service ready-pod counts at 1Hz with `total` column
  - `<run>/<strat>/cpu.dat` — per-replica CPU averaged over each RPS window (one row per RPS)
  - `<run>/<strat>/metrics_<rps>.json` — wrk2 latency percentiles for that RPS window
- HPA config: `scratch/yaml/bf-hpa.yaml`
- Deployment: `scratch/yaml/bookinfo-const.yaml`
- IstioOperator (mazu config injection): `scratch/yaml/istio-operator.yaml`
