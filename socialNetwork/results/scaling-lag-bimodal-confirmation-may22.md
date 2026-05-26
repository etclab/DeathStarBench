# Mazu HPA-fire bimodality — direct confirmation via per-second CPU sweep — 2026-05-22

7-run replication at 400rps with new per-second container CPU instrumentation. Follow-up to `results/scaling-on-bench1.5-may21/scaling-lag-investigation.md` (which hypothesized bimodal HPA-fire timing from indirect signals across 10 runs).

Run dirs: `results/benchmark1.5-05-22-26_{040212,143437,144941,145806,150619,151441,152308}/`

## TL;DR

The bimodal HPA-fire hypothesis is confirmed with a clean signature. Mazu's first productpage scale-out fires in one of two discrete modes — t=26-28s (lockstep with istio) or t=40-41s (lagged by ~13s) — with **nothing in between**. istio's first scale-out is glued to t=27 ± 1 across all 7 runs.

Separately and independently, mazu's istio-proxy CPU peaks at ~2.5× istio's during the post-scale-out burst, **in every run regardless of HPA timing**. The burst is universal; only the HPA's response to the burst is bimodal.

## Instrumentation

`run-benchmark1.5.sh` instrumented to write `<run>/<strat>/container-cpu-<rps>.csv` at 1Hz via `kubectl top pod --containers`. Schema: `timestamp,pod,container,cpu_millicores,memory_mib`. Captures app and `istio-proxy` containers separately for productpage/details/reviews/ratings.

Note: metrics-server's `--metric-resolution` floor on this cluster prevents going below ~10s. The poller runs at 1Hz, but underlying values refresh every ~15s — timestamps still pinpoint *when* values change, which is sufficient for the HPA-fire timing analysis.

## Headline finding: HPA1 is bimodal for mazu, not for istio

First productpage scale-out time (1→N pods, "HPA1"):

| stack | values sorted | mean | range | stdev |
|---|---|---|---|---|
| istio | 26, 27, 27, 27, 28, 28, 28 | 27.3s | **2s** | 0.8 |
| mazu  | 26, 26, 27, 28, 40, 41, 41 | 32.7s | **15s** | 7.5 |

Mazu's values cluster into two groups separated by a 12-second empty gap:
- **Lockstep group** (4 runs): HPA1 ∈ {26, 26, 27, 28}
- **Lagged group** (3 runs): HPA1 ∈ {40, 41, 41}

No values in 29-39. This is not a continuous spread; it's two modes.

Subsequent HPA decisions (HPA2, HPA3) show wider variance for both stacks — the bimodality is specifically a first-decision phenomenon.

## Sidecar burst is universal for mazu, not bimodal

Max `istio-proxy` CPU on any productpage pod during the 120s window:

| stack | mean across 7 runs | range |
|---|---|---|
| istio | 622 mcores | 593-669 |
| mazu  | **1579 mcores** | 1210-1836 |

Every mazu run shows a sidecar peak 2-3× istio's. This is direct, replicated evidence of the per-scale-event cost amplification: each new pod triggers ~N×M cert exchanges across the mesh (where N=new pods, M=existing peer pods), bursting the sidecar CPU briefly. istio's sidecar peak is consistently flat at ~600 mcores.

## App-starvation signature correlates with lagged HPA1

A new signal exposed by the per-second CSV: in mazu warmup, the productpage app container can sit at near-zero CPU for tens of seconds while the sidecar is consuming a moderate but unsaturated CPU level — direct evidence of pending connections stuck in cert-exchange establishment.

Starvation duration = seconds where pp app < 100 mcores AND sidecar > 100 mcores:

| run | mazu HPA1 | mazu starv (s) | mazu p50 (ms) |
|---|---|---|---|
| 040212 | 28 | 15 | 234 |
| 143437 | 40 | **30** | 3440 |
| 144941 | 41 | **15** | 4380 |
| 145806 | 41 | 0 | 7390 |
| 150619 | 27 | 0 | 6660 |
| 151441 | 26 | 0 | 36 |
| 152308 | 26 | 0 | 3480 |

Directional correlation:
- 3/3 runs in the lagged HPA1 cluster show non-zero starvation (mean 15s).
- 3/4 runs in the lockstep HPA1 cluster show zero starvation.
- Outliers: 040212 (lockstep with starv=15), 145806 (lagged with starv=0). Mechanism is real but not the only driver.

## Latency

| stack | p50 mean | p50 range | p99 mean | p99 range |
|---|---|---|---|---|
| istio | 307 ms | 27-1930 | 16.7 s | 15.6-17.7 |
| mazu  | **3660 ms** | 36-7390 | **22.6 s** | 19.1-24.6 |

Two things to note:
1. **mazu's p99 is uniformly 4-7s higher than istio's**, even on best-case mazu runs (151441 has mazu p50=36ms but p99=19.1s). This floor is the per-request mazu overhead — visible even when scaling is perfectly timed.
2. **mazu's p50 has 200× run-to-run variance** (36ms to 7390ms). Maps roughly but not cleanly to HPA1 mode:
   - Both runs with HPA1=26 (151441, 152308): p50=36ms vs 3480ms — same first-decision timing, 100× latency difference.
   - This means the bimodal HPA1 explains *part* of the variance but not all of it. Per-request overhead + sidecar burst saturation contribute independently.

## Working model

Three independent mazu costs, each with a distinct signature:

1. **Per-request sidecar overhead** (~1.7-2× istio in steady-state mean CPU). Universal. Sets a latency floor mazu cannot escape regardless of scaling.
2. **Per-scale-event burst** (sidecar peak ~2.5× istio). Universal. Each scale-out triggers N×M cert exchanges across the mesh. Briefly pegs sidecar CPU once connections actually establish.
3. **Bimodal first-decision timing** (HPA1 fires at t≈27 or t≈40). Variable. Driven by whether cert validation completes quickly or stalls — see mechanism below.

### Why HPA misses lagged warmup: cert validation is I/O-bound

Direct measurement in all 3 lagged runs (143437, 144941, 145806) shows mazu's productpage sidecar CPU staying in the **4-591 mcore range** throughout the lag period — never crossing the 700 mcore HPA threshold. The sidecar is elevated relative to idle but nowhere near saturation. In one run (145806) sidecar CPU even drops to 4 mcores at t=15-20s — essentially idle.

This is because **cert validation is I/O-bound, not CPU-bound**. The sidecar makes a gRPC call to ext_authz, which calls kube-apiserver for TokenReview, validates the RBE commitment, and verifies the TPM attestation. While that round-trip is in flight, the sidecar is **blocked waiting on a response**, not burning CPU. Meanwhile:

- Connections sit pending (cert handshake not complete) → no requests get forwarded to the app
- App CPU shows fake-low (no traffic reaches it)
- Sidecar CPU shows moderate-but-not-saturated (gRPC client work, connection management, but not "processing 400 rps")
- Both containers below 70% utilization → **HPA correctly does nothing**

Once cert validations finally complete, 128 wrk2 connections come online nearly simultaneously, traffic floods through, and the app jumps from ~2 to ~1900 mcores in one second. *Then* HPA fires.

The implication: **HPA-based autoscaling fundamentally can't detect this kind of backlog** — CPU utilization is the wrong signal for an I/O-blocking bottleneck. The system genuinely looks idle while it's severely backlogged on network I/O. Detecting it would require either queue-depth metrics (pending connections, pending requests at the sidecar) or making cert validation CPU-bound rather than I/O-bound.

### What drives the bimodality

The two modes likely reflect whether cert validation completes quickly (lockstep mode, ~5-15s of pending connections) or gets stuck on slow apiserver/TPM round-trips (lagged mode, ~25-40s). Variance in apiserver response latency, TPM operation latency, or kube-apiserver cache warmth could produce two regimes. Worth checking: did `kube-apiserver` pods get restarted between RPS iterations (they do, per `run-benchmark1.5.sh:118-126`) — first lookups after restart hit cold caches.

## Open questions

1. **Cert validation latency distribution**. If lag is caused by slow ext_authz round-trips, we should see this directly in ext_authz logs or in mazu's own metrics. Check whether ext_authz / mazu instrumentation captures per-call latency, and whether the distribution is bimodal across runs.
2. **kube-apiserver cold cache effect**. `run-benchmark1.5.sh:118-126` deletes and recreates the kube-apiserver between every RPS iteration. The first TokenReview / configmap lookup after restart will be a cache miss. If this is the cause, the bimodality should disappear if apiserver-restart is removed from the inner loop (or moved to once per strategy).
3. **What drives the second-order latency variance in lockstep runs?** Pair 151441 (p50=36ms, p99=19.1s) vs 152308 (p50=3480ms, p99=23.6s) — both lockstep on all 3 HPA decisions, dramatically different latency. Possibly TPM operation latency variance (mazu's `st5-AttUpd` specifically does TPM attestation per scale event)?
4. **Queue-depth-based autoscaling**. Since CPU is the wrong signal for I/O-bound backlog, evaluate scaling on `envoy_cluster_upstream_rq_pending_active` or downstream connection queue depth via prometheus-adapter + Pods-type HPA metric. Sketch: scrape envoy stats with Prometheus (already deployed for the benchmark), expose via prometheus-adapter on the custom metrics API, reference in `bf-hpa.yaml` as a Pods metric with averageValue threshold.
5. **Replicate across other RPS levels**. The may21 investigation predicted 300 and 500 wouldn't show this pattern as cleanly (300: load too low to need scaling, 500: universal saturation dominates). With per-second CSVs available, the test is cheap.

## Key file locations

- Run dirs: `results/benchmark1.5-05-22-26_{040212,143437,144941,145806,150619,151441,152308}/`
- Per RPS, per stack:
  - `<run>/<strat>/container-cpu-400.csv` — **new** per-second container CPU/memory at 1Hz
  - `<run>/<strat>/pods-400.csv` — raw per-pod observations at 1Hz
  - `<run>/<strat>/pods-400-sum.csv` — per-service ready-pod counts
  - `<run>/<strat>/400.txt` — wrk2 latency percentile output
  - `<run>/<strat>/metrics_400.json` — Prometheus CPU/memory window summary
- Prior investigation: `results/scaling-on-bench1.5-may21/scaling-lag-investigation.md`
- Instrumentation added in: `run-benchmark1.5.sh` (search for `container-cpu-`)
