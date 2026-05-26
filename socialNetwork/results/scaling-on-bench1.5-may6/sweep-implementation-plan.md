# Implementation plan — continuous HPA-on sweep script

Companion to [`sweep-design-discussion.md`](./sweep-design-discussion.md). The
design discussion explained *why* independent per-RPS runs are wrong for
benchmark 1.5b; this document specifies *what* gets built.

## Goal

Replace the per-RPS reinstall pattern (for the HPA-on case only) with a
single continuous sweep per strategy: install once, step through
`RPS_VALUES` with the pod fleet carrying over, sample pod count every 1 s
across the whole sweep.

The change is delivered as a **new dedicated script**,
`socialNetwork/run-benchmark1.5b-sweep.sh`. The existing
`socialNetwork/run-benchmark1.5.sh` (which still owns the keep-alive-off
1.5a path) is not modified.

## Files

### New
- `socialNetwork/run-benchmark1.5b-sweep.sh`

### Untouched
- `socialNetwork/run-benchmark1.5.sh`
- `socialNetwork/collect_metrics.sh`
- `socialNetwork/generate_dat.py`
- `socialNetwork/results/parse_15_data.py`
- `socialNetwork/summarize_pods.py`
- `socialNetwork/plot_pods.py`

All downstream scripts work as-is because the new script preserves the
per-RPS file naming contract (`${RPS}.txt`, `metrics_${RPS}.json`,
`pods-${RPS}.csv`).

## Script structure — `run-benchmark1.5b-sweep.sh`

Modeled on `run-benchmark1.5.sh` for familiarity (same helper sourcing,
`ISTIOCTL_PATH`, logging conventions, post-strategy plotting block), but
the per-strategy body is split into three phases. No `SCALE_ENABLED`
branch — this script is always the HPA-on sweep.

### Configuration

```
STRATEGIES=("istio" "st5-AttUpd")
RPS_VALUES=(50 100 200 300 400 500 600 700 800 900 1000 1100 1200)
DURATION=120
PROM_PORT=9091
RESULTS_DIR=results/benchmark1.5b-sweep-<date>
```

All override-able via env. `RPS_VALUES` must be monotonically increasing
(see design discussion — HPA scale-down stabilization is 5 min by default,
so a decreasing leg would contaminate the measurement).

### Phase A — setup (once per strategy)

1. Teardown previous bookinfo + istio (existing `uninstall-bf`,
   `remove-istio` and `kubectl wait --for=delete` calls from
   `run-benchmark1.5.sh:106-114`).
2. Reset kube-apiserver **once** (same block as
   `run-benchmark1.5.sh:117-125`).
3. Create TPMs on all nodes (same block as `run-benchmark1.5.sh:128-138`,
   including first-run `setup-tpm-all-nodes.sh` provisioning when `~/trinc`
   is missing on NODE0). `sleep 60` afterward.
4. Install Istio or Mazu, and for `st5-AttUpd` install the TPM device
   plugin / TPM pubkey configmap / TPM secret (same block as
   `run-benchmark1.5.sh:142-158`).
5. Apply HPA-on bookinfo manifests:
   `bookinfo-const-tpm.yaml` for `st5-AttUpd`, else `bookinfo-const.yaml`;
   plus `bf-gateway.yaml` and `bf-hpa.yaml`. No
   `bf-no-connection-reuse.yaml`.
6. Wait Ready: productpage / details / reviews / ratings / istiod /
   istio-ingressgateway.
7. `get_ingress_ip_port`; echo `INGRESS_IP:INGRESS_PORT`.
8. Kill stale port-forwards on `PROM_PORT`. Install Prometheus once; wait
   for Prometheus pod Ready; start the port-forward (`PF_PID`); poll
   `${PROM_URL}/-/ready` up to 30 × 5 s.
9. **Start a single continuous pod-poller** for the whole sweep:
   - Output: `${RES_DIR}/pods.csv`
   - Header: `timestamp,app,version,phase,ready`
   - 1 Hz `kubectl get pods -l 'app in
     (productpage,details,reviews,ratings)' -o jsonpath=…` (same query as
     `run-benchmark1.5.sh:243-246`).
   - PID captured into `POD_POLL_PID`.

### Phase B — sweep (the RPS loop)

For each `RPS`:

1. `echo "--- Running RPS=$RPS for ${DURATION}s ---"`
2. `BENCH_START=$(date +%s)`
3. Run wrk2 exactly as today (`run-benchmark1.5.sh:252-254`): 16 threads,
   128 connections, `-D exp`, `-L`, `read-productpage.lua`. Output →
   `${RES_DIR}/${RPS}.txt`.
4. `BENCH_END=$(date +%s)`
5. Slice the spanning pod CSV into a per-RPS file so existing
   `summarize_pods.py` / `plot_pods.py` work unchanged:
   ```bash
   awk -F, -v s="$BENCH_START" -v e="$BENCH_END" \
       'NR==1 || ($1>=s && $1<=e)' \
       "${RES_DIR}/pods.csv" > "${RES_DIR}/pods-${RPS}.csv"
   ```
6. `collect_metrics.sh "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS"` —
   works unchanged; queries Prometheus by `[START_EPOCH, END_EPOCH)` so a
   single persistent Prometheus is fine.
7. `echo "--- RPS=$RPS complete ---"`

No teardown, reinstall, port-forward restart, or poller restart between
RPS steps. Fleet, prometheus, port-forward, and poller all carry over.

### Phase C — teardown (once per strategy)

1. Stop the spanning pod-poller (`kill "$POD_POLL_PID"; wait "$POD_POLL_PID"`).
2. `kill $PF_PID`.
3. `setup_social_network.sh uninstall-prometheus`.
4. `python3 "${SCRIPT_DIR}/generate_dat.py" "$RES_DIR"`.
5. Log completion.

### Post-strategy block (after both strategies)

Mirror `run-benchmark1.5.sh:285-306`:

- `python3 results/parse_15_data.py "$RESULTS_DIR"`
- copy `plot_15_e2e_latency.gpi`, `plot_15_cpu.gpi`, `plot_15_memory.gpi`,
  `style.gpi` into `$RESULTS_DIR`
- run the three gnuplot scripts
- `python3 summarize_pods.py "$RESULTS_DIR"`
- `python3 plot_pods.py "$RESULTS_DIR"`

## Verification

1. **Smoke test (short)**:
   ```bash
   STRATEGIES=istio RPS_VALUES="50 100" DURATION=30 \
       ./socialNetwork/run-benchmark1.5b-sweep.sh
   ```
   Confirm:
   - Setup logs (TPMs, istio install, bookinfo install, Prometheus
     install) appear exactly once before any `--- Running RPS=… ---` line.
   - `${RES_DIR}/istio/{50,100}.txt`, `pods-50.csv`, `pods-100.csv`, and a
     spanning `pods.csv` exist.
   - `pods.csv` timestamps are monotonically increasing and a superset of
     both per-RPS slice files.
   - `metrics_50.json` / `metrics_100.json` produced; `cpu.dat` /
     `memory.dat` generated.

2. **Full sweep**: run with default `RPS_VALUES` for both strategies.
   Confirm the e2e latency / cpu / memory gnuplot PDFs and the
   pod-readiness plots produce without error.

3. **HPA-dynamics check**: load `pods.csv`; replica count should be
   non-decreasing across RPS step transitions instead of resetting to
   baseline. The non-monotonic p50 dip at 900 rps in
   `gnuplot_combined.dat` should change shape because the fleet now
   carries over.

4. **No-regression on 1.5a**: `run-benchmark1.5.sh` is byte-for-byte
   unchanged; re-running the keep-alive-off path produces the prior
   layout.

## Out of scope (follow-ups noted in the design discussion)

- Step dwell time longer than 120 s in the knee region.
- N full-sweep repeats for statistical pooling.
- HPA target-metric / threshold parity audit between istio and
  `st5-AttUpd`.
- Min/max replica ceiling tuning to ensure neither scheme hits the cap
  inside the sweep.
