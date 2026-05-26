# Plan: queue-depth-based autoscaling for mazu — 2026-05-22

## Motivation

The may22 sweep (`results/scaling-lag-bimodal-confirmation-may22.md`) showed that mazu's lagged-mode warmup is invisible to CPU-based HPA: during the lag window, the istio-proxy container stays at 4-591 mcores (below the 700 mcore HPA threshold) because cert validation is **I/O-bound, not CPU-bound** — the sidecar blocks on ext_authz/TokenReview/TPM round-trips rather than burning CPU. HPA correctly does nothing because both containers are under threshold; the system genuinely looks idle while it's severely backlogged.

To detect this kind of backlog we need a metric that grows with **pending work**, not CPU. Envoy already tracks pending requests waiting for upstream connections — exposing this to HPA via the Kubernetes custom metrics API is the standard path.

## Goal

Replace (or augment) the current ContainerResource-on-CPU triggers in `scratch/yaml/bf-hpa.yaml` with a Pods-type metric on `envoy_cluster_upstream_rq_pending_active`, so HPA can fire on connection backlog during mazu warmup before it manifests as CPU saturation.

## Non-goals

- Not redesigning the HPA from scratch — keeping CPU metrics as fallback (`selectPolicy: Max`).
- Not changing scaleUp behavior (`+2 pods / 2s`, no stabilization) — that's already aggressive enough.
- Not solving mazu's per-request overhead — this plan only addresses scaling-lag detection.

## Approach

Three-layer pipeline (only the middle layer is new):

```
envoy (already exposing :15090/stats/prometheus)
   ↓
Prometheus (already deployed by run-benchmark1.5.sh)
   ↓
prometheus-adapter (NEW — exposes envoy metric on custom metrics API)
   ↓
HPA (existing bf-hpa.yaml, new Pods metric added)
```

## Pre-test (cheap, do this first)

Before installing prometheus-adapter, verify the metric actually captures the mazu warmup backlog. If `envoy_cluster_upstream_rq_pending_active` doesn't spike for mazu during the lag period, the whole pipeline is pointless.

1. Add a per-second poller for envoy stats to `run-benchmark1.5.sh`, similar to the existing `container-cpu-<rps>.csv` writer.
2. Poller hits each pod's sidecar admin endpoint (`kubectl exec <pod> -c istio-proxy -- curl -s localhost:15090/stats?filter=upstream_rq_pending_active`) and writes `timestamp,pod,cluster,pending_requests` rows.
3. Run 1× at 400rps with `SCALE_ENABLED=true`.
4. **Check**: does mazu's productpage sidecar show elevated pending_requests during t=0..HPA1 in lagged runs? Does istio's stay near zero?
5. **Decision gate**:
   - If yes → proceed to implementation.
   - If no → investigate what other envoy stats capture the cert-pending state (`upstream_cx_connecting`? `downstream_cx_active` vs `cx_active`?). Possibly need to instrument mazu directly with a `pending_cert_validations` gauge before this is feasible.

## Pre-test outcome (2026-05-22) — failed for envoy-native metrics

Ran the pre-test with a per-second envoy poller added to `run-benchmark1.5.sh` (see the `ENVOY_POLL_PID` block, writes `<run>/<strat>/envoy-pending-<rps>.csv` schema `timestamp,pod,metric,scope,value`). First attempts only captured `xds-grpc` because istio's default stats filter suppresses per-app-cluster and per-listener stats; resolved by adding `sidecar.istio.io/statsInclusionRegexps: ".*upstream_rq.*,.*upstream_cx.*,.*downstream_cx.*,.*downstream_pre_cx.*,.*downstream_rq.*"` to every deployment in `bookinfo-const.yaml` and `bookinfo-const-tpm.yaml`. After that the CSV does carry app cluster scopes (`outbound|9080||details.default.svc.cluster.local`, etc.) and inbound listener scopes (`0.0.0.0_15006`).

Run dir: `results/benchmark1.5-05-22-26_163840/`. HPA1 on this run: istio=+29s, mazu=+46s.

Productpage-pod peak values in the t=0..HPA1 window:

| metric @ scope | istio | mazu | why useless |
|---|---|---|---|
| `listener_downstream_pre_cx_active` @ `0.0.0.0_15006` | 6 (+0s only) | 4 (+0s only) | transient single-sample blip; 0 for entire rest of warmup |
| `cluster_upstream_rq_pending_active` @ details | 19 (+0s only) | 32 (+0s only) | same — single-sample blip |
| `listener_downstream_cx_active` @ `0.0.0.0_15006` | 128 | 128 | saturates at wrk2's `-c 128`; no headroom for queue signal |
| `cluster_upstream_cx_active` @ details | 46 | 58 | 1.3× gap, but mazu's value already high at t=0 — not a lag indicator |
| `cluster_upstream_cx_connecting` | — | — | not emitted by this envoy build at all |
| `listener_cx_active` − `http_cx_active` @ 15006 | 0 | 0 | listener saturates at the same rate as http filter chain |

**Structural reason**: cert validation happens *inside* the TLS handshake — by the time the next 1Hz sample lands, the handshake has either completed (incremented `cx_active`) or failed (incremented `cx_destroy`). There is no envoy gauge for "TLS handshake currently blocked on ext_authz callback". `pre_cx_active` is the closest envoy has, and it is transient. Compounding this, with `-c 128` wrk2 connections the inbound listener saturates at 128 from t=+1s and stays there, so any connection-level queue gauge loses signal once the workload is up.

**Caveat on representativeness**: this run was a slow-but-CPU-visible warmup, not the lagged-mode (CPU stuck <600m for 30s) documented in [`scaling-lag-bimodal-confirmation-may22.md`](scaling-lag-bimodal-confirmation-may22.md). Mazu's productpage CPU climbed through 811m@+18s and 2622m@+33s here. To rule envoy metrics out fully, repeat instrumentation across a 7-run sweep at 400rps to catch the lagged-mode runs (3/7 in the may22 sweep) and confirm the metrics stay flat there too.

### Implications for this plan

1. **Decision gate failed** — steps 1-7 below are paused for envoy-native metrics.
2. **Pivot to mazu-side instrumentation** (originally open question #1): add a `pending_cert_validations` gauge to mazu, incremented at `doVerifyCertChain()` entry and decremented on ext_authz response. Scrape via Prometheus → prometheus-adapter → HPA. The remainder of this plan's Prometheus/adapter/HPA wiring still applies; only the source metric changes from `envoy_cluster_upstream_rq_pending_active` to the mazu gauge.
3. Before pivoting, **run the 7-sweep** with the current instrumentation to confirm envoy metrics stay flat on a real lagged-mode run.

Analysis scripts left in run dir: `benchmark1.5-05-22-26_163840/{survey.py,analyze.py,timeseries.py}`.

## Implementation steps (assuming pre-test passes)

### Step 1 — verify envoy metric is scraped by Prometheus

```bash
kubectl port-forward -n istio-system svc/prometheus 9091:9090 &
curl -s "http://localhost:9091/api/v1/query?query=envoy_cluster_upstream_rq_pending_active" | jq
```

If empty, check Prometheus's scrape config — the benchmark's Prometheus install may need the sidecar `:15090/stats/prometheus` endpoint added.

### Step 2 — install prometheus-adapter

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus-adapter prometheus-community/prometheus-adapter \
  -n istio-system \
  --set prometheus.url=http://prometheus.istio-system.svc \
  --set prometheus.port=9090 \
  --set metricsRelistInterval=10s
```

Note: 10s relist interval is aggressive — matches the metrics-server cadence. Default is 30s which would dominate detect-to-scale latency.

### Step 3 — configure the metric exposure rule

Create `dev/prometheus-adapter-values.yaml`:

```yaml
rules:
  custom:
  - seriesQuery: 'envoy_cluster_upstream_rq_pending_active{namespace!="",pod!=""}'
    resources:
      overrides:
        namespace: {resource: "namespace"}
        pod:       {resource: "pod"}
    name:
      matches: "envoy_cluster_upstream_rq_pending_active"
      as: "envoy_pending_requests"
    metricsQuery: 'sum(<<.Series>>{<<.LabelMatchers>>}) by (<<.GroupBy>>)'
```

Apply via `helm upgrade ... -f dev/prometheus-adapter-values.yaml ...`.

### Step 4 — verify the custom metric is reachable

```bash
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/default/pods/*/envoy_pending_requests"
```

Should return one entry per productpage/details/reviews/ratings pod with numeric values.

### Step 5 — pick a threshold

Scrape `envoy_pending_requests` from a known-healthy 400rps istio run (during steady-state, post-warmup). Set HPA target to ~2-3× that value. Starting point: 10 (will tune from observation).

### Step 6 — update `scratch/yaml/bf-hpa.yaml`

Add a Pods-type metric to each service's HPA, keeping the two ContainerResource metrics. `selectPolicy: Max` means whichever signal fires first wins:

```yaml
- type: Pods
  pods:
    metric:
      name: envoy_pending_requests
    target:
      type: AverageValue
      averageValue: "10"
```

### Step 7 — integrate with benchmark teardown

`run-benchmark1.5.sh` tears down and reinstalls a lot between RPS iterations (uninstall-bf, remove-istio, delete kube-apiserver, fresh Prometheus). prometheus-adapter has a cluster-scoped APIService registration that needs to survive these resets. Two options:

- **A**: install prometheus-adapter once, outside the inner loop. APIService stays registered. New Prometheus per-RPS-iter still works because we set the prometheus.url to the service name.
- **B**: install/uninstall adapter per iteration. Cleaner state but slow and adds APIService churn.

Recommend **A**. Add adapter install to the existing "First run" section (alongside trinc/swtpm setup).

## Risks

1. **The metric may not capture cert-pending state.** Pre-test guards against this.
2. **Adapter scrape latency** could blunt the win. `metricsRelistInterval=10s` + Prometheus scrape interval (default 15s) means detect-to-HPA-input ≈ 25s worst case. HPA's own 15s sync period adds more. Net: best case ~10s improvement vs CPU; worst case no improvement.
3. **Threshold tuning is iterative.** Start at 10, observe scale-up cadence, adjust. Too low → oscillation. Too high → no improvement over CPU.
4. **APIService teardown collision.** If benchmark teardown deletes the istio-system namespace, prometheus-adapter's APIService becomes orphaned and breaks future installs until manually cleaned up. Check `kubectl get apiservice v1beta1.custom.metrics.k8s.io` if metrics suddenly stop appearing.
5. **Doesn't address the underlying issue.** This makes HPA *detect* the bottleneck faster, but mazu's I/O-blocking cert validation is still the root cause. A future fix would make cert validation faster (apiserver caching, TPM batching) or async-but-CPU-visible.

## Validation

Re-run the 7× 400rps sweep with queue-depth HPA enabled. Expected outcomes:

- **HPA1 distribution narrows for mazu** — bimodality disappears or compresses. Lagged-mode runs (currently t=40-41s) should fire closer to istio's t=27s baseline because the pending-request backlog crosses threshold while cert validation is still in flight.
- **App-starvation duration shrinks** — with more pods available earlier, the first connections that complete cert validation get distributed across more pods.
- **p50 latency variance narrows** — if scaling timing was the dominant cause of mazu's p50 spread (36ms to 7390ms), that spread should compress.
- **p99 floor stays put** — per-request overhead is unchanged by scaling improvements. Expect mazu p99 still ~19-22s.

Failure modes to watch for:
- Oscillation: HPA scales up, pending requests drain to zero, HPA scales down, pending requests spike again. Mitigate by adding `scaleDown.stabilizationWindowSeconds` if observed.
- Over-provisioning: HPA scales to maxReplicas before the load actually warrants it. Mitigate by raising the averageValue threshold.

## Rollback

Revert `bf-hpa.yaml` to current CPU-only configuration. prometheus-adapter can stay installed (no harm) or be uninstalled with `helm uninstall prometheus-adapter -n istio-system`. APIService cleanup: `kubectl delete apiservice v1beta1.custom.metrics.k8s.io` if it lingers.

## Open questions to resolve before starting

1. Is mazu itself emitting a metric for pending cert validations? If yes, that's a much more direct signal than `envoy_cluster_upstream_rq_pending_active`. Worth checking mazu's instrumentation before settling on the envoy metric.
2. Does Prometheus's current scrape config already cover sidecar `:15090`? If not, pre-test step 1 will reveal it.
3. Should this change be gated to mazu only (via separate HPAs per strategy)? Current `bf-hpa.yaml` is shared between istio and mazu — adding the queue-depth metric affects both. Likely benign for istio (its queue depth stays near zero) but worth verifying.

## File locations

- Investigation note: `results/scaling-lag-bimodal-confirmation-may22.md`
- Current HPA: `scratch/yaml/bf-hpa.yaml`
- Benchmark driver: `run-benchmark1.5.sh`
- Per-second container CPU CSVs: `results/benchmark1.5-05-22-26_*/<strat>/container-cpu-400.csv`
