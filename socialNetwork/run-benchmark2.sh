#!/bin/bash
#
# Benchmark 2: Per-Operation Cost Measurement with Fortio
#
# - App: Fortio client/server (direct pod-to-pod HTTP)
# - Connection reuse: Disabled (maxRequestsPerConnection: 1)
# - Measures: per-operation latencies via inline Prometheus histograms
#   + CPU/memory via Prometheus
# - What it measures: Individual ext_authz operation costs (kc_fetch,
#   counter_attestation, rbe_proof, challenge_response, token_review)
#
# Usage:
#   ./run-benchmark2.sh
#
# Environment overrides:
#   STRATEGIES   - space-separated list (default: "istio" "st5-AttUpd")
#   RPS          - requests per second (default: 100)
#   DURATION     - seconds for the load run (default: 240)
#   RESULTS_DIR  - output directory (default: results/benchmark2-<date>)
#   PROM_PORT    - local port for Prometheus port-forward (default: 9091)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
STRATEGIES=(${STRATEGIES:-"istio" "st5-AttUpd"})
RPS=${RPS:-1000}
DURATION=${DURATION:-240}
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/benchmark2-$(date +%m-%d-%y_%H%M%S)}"
PROM_PORT=${PROM_PORT:-9091}
ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

source_setup() {
    eval "$(grep -A5 '^mazu_echo()' "$SCRIPT_DIR/setup_social_network.sh")"
}
source_setup

# --- Ensure istioctl is installed ---
if [ ! -x "$ISTIOCTL_PATH" ]; then
    mazu_echo "istioctl not found, installing..."
    cd "$HOME"
    curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh -
    cd -
fi

if [ ! -x "$ISTIOCTL_PATH" ]; then
    echo "ERROR: istioctl still not found at $ISTIOCTL_PATH after install attempt"
    exit 1
fi

# ensure istio-system namespace exists
if ! kubectl get namespace istio-system > /dev/null 2>&1; then
    mazu_echo "Creating istio-system namespace..."
    kubectl create namespace istio-system
fi

mazu_echo "=== Benchmark 2: Per-Operation Cost Measurement ==="
mazu_echo "Strategies: ${STRATEGIES[*]}"
mazu_echo "RPS: ${RPS}"
mazu_echo "Duration: ${DURATION}s"
mazu_echo "Results: ${RESULTS_DIR}"

mkdir -p "$RESULTS_DIR"

# --- Per-strategy loop ---
for STRAT in "${STRATEGIES[@]}"; do
    RES_DIR="${RESULTS_DIR}/${STRAT}"
    mkdir -p "$RES_DIR"
    LOG_FILE="$RES_DIR/run.log"

    (
        exec > >(tee -a "$LOG_FILE") 2>&1

        export STRAT
        export DURATION

        echo "=== Benchmark 2 started at $(date) for $STRAT ==="
        echo "--- Running RPS=$RPS for ${DURATION}s ---"

        # ---- Teardown previous fortio pods ----
        kubectl delete -f "$SCRIPT_DIR/scratch/yaml/fortio-no-connection-reuse.yaml" 2>/dev/null || true
        kubectl delete -f "$SCRIPT_DIR/scratch/yaml/fortio-tpm.yaml" 2>/dev/null || true
        kubectl delete -f "$SCRIPT_DIR/scratch/yaml/fortio.yaml" 2>/dev/null || true
        kubectl wait --for=delete pod -l app=fortio-server --timeout=300s 2>/dev/null || true
        kubectl wait --for=delete pod -l app=fortio-client --timeout=300s 2>/dev/null || true

        # ---- Teardown Istio ----
        ${SCRIPT_DIR}/setup_social_network.sh remove-istio
        kubectl wait --for=delete pod -l app=istiod -n istio-system --timeout=300s 2>/dev/null || true
        kubectl wait --for=delete pod -l app=istio-ingressgateway -n istio-system --timeout=300s 2>/dev/null || true

        # ---- Restart kube-apiserver ----
        APISERVER_COUNT=$(kubectl -n kube-system get pods -l component=kube-apiserver --no-headers 2>/dev/null | wc -l)
        kubectl -n kube-system delete pods -l component=kube-apiserver --now --timeout=60s 2>/dev/null || true

        echo "API server count: $APISERVER_COUNT. Waiting for all kube-apiserver pods to become Ready..."
        until [ "$(kubectl -n kube-system get pods -l component=kube-apiserver -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null | grep -c True)" -ge "$APISERVER_COUNT" ]; do
            sleep 3
        done
        echo "All kube-apiserver pods are Ready"

        # ---- Create fresh TPMs ----
        echo "Creating TPMs on all nodes..."
        NODE0="apoudel@c220g1-031111.wisc.cloudlab.us"
        SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        if ! ssh $SSH_OPTS "$NODE0" 'test -d ~/trinc'; then
            echo "First run: setting up trinc/swtpm on all nodes..."
            ${SCRIPT_DIR}/dev/setup-tpm-all-nodes.sh -d apt.emulab.net apt033 apt030 apt029 apt036
        fi
        ssh $SSH_OPTS "$NODE0" 'for node in node-0 node-1 node-2 node-3 node-4 node-5; do ssh "$node" "~/trinc/swtpm-test/setup-tpm.sh create_tpm" & done; wait'
        echo "TPMs created on all nodes"

        sleep 60s

        # ---- Install Istio / Mazu ----
        if [[ "$STRAT" == "istio" ]]; then
            ${SCRIPT_DIR}/dev/deploy-mazu-configmap.sh "$STRAT"
            ${SCRIPT_DIR}/dev/deploy-rbe-pp.sh

            ${SCRIPT_DIR}/setup_social_network.sh install-istio
        else
            MAZU_BENCHMARK_INLINE_ENABLED=true ${SCRIPT_DIR}/dev/deploy-mazu-configmap.sh "$STRAT"
            ${SCRIPT_DIR}/dev/deploy-rbe-pp.sh

            if [[ "$STRAT" == "st5-AttUpd" ]]; then
                ${SCRIPT_DIR}/dev/tpm/install-k8s-tpm-device.sh
                ${SCRIPT_DIR}/dev/tpm/deploy-tpm-pubkey-configmap.sh
                ${SCRIPT_DIR}/dev/tpm/deploy-tpm-secret.sh
            fi

            ${SCRIPT_DIR}/setup_social_network.sh install-mazu
        fi

        # ---- Deploy Fortio pods ----
        if [[ "$STRAT" == "st5-AttUpd" ]]; then
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/fortio-tpm.yaml"
        else
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/fortio.yaml"
        fi

        # ---- Disable connection reuse ----
        kubectl apply -f "$SCRIPT_DIR/scratch/yaml/fortio-no-connection-reuse.yaml"

        # ---- Wait for pods ready ----
        kubectl wait --for=condition=Ready pod -l app=fortio-server --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=fortio-client --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s

        # ---- Fresh Prometheus + port-forward ----
        # Kill any leftover port-forward on PROM_PORT
        lsof -ti :${PROM_PORT} | xargs -r kill 2>/dev/null || true
        sleep 1

        ${SCRIPT_DIR}/setup_social_network.sh uninstall-prometheus
        ${SCRIPT_DIR}/setup_social_network.sh install-prometheus

        echo "Waiting for Prometheus pod to be Running..."
        kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus -n istio-system --timeout=300s

        kubectl port-forward -n istio-system svc/prometheus ${PROM_PORT}:9090 &
        PF_PID=$!
        export PROM_URL="http://localhost:${PROM_PORT}"

        echo "Waiting for Prometheus to be reachable at ${PROM_URL}..."
        for i in $(seq 1 30); do
            if curl -sf "${PROM_URL}/-/ready" > /dev/null 2>&1; then
                echo "Prometheus is reachable at ${PROM_URL}"
                break
            fi
            if [ "$i" -eq 30 ]; then
                echo "WARNING: Prometheus not reachable after 30s, metrics collection may fail"
            fi
            sleep 1
        done

        # ---- Find fortio client pod ----
        FORTIO_CLIENT_POD=$(kubectl get pod -l app=fortio-client -o jsonpath='{.items[0].metadata.name}')
        echo "Fortio client pod: ${FORTIO_CLIENT_POD}"

        echo "Running fortio load with RPS=$RPS, DURATION=${DURATION}s"

        # Record start time for Prometheus queries
        BENCH_START=$(date +%s)

        # ---- Run fortio load (direct pod-to-pod, no ingress) ----
        kubectl exec "$FORTIO_CLIENT_POD" -c fortio-client -- \
            fortio load \
            -qps "$RPS" \
            -t "${DURATION}s" \
            -p "50,90,95,99" \
            -json /dev/stdout \
            -c 32 \
            -nocatchup \
            -uniform \
            http://fortio-server:8080/ > "$RES_DIR/fortio_${RPS}.json"

        BENCH_END=$(date +%s)

        echo "Fortio results saved to $RES_DIR/fortio_${RPS}.json"

        # ---- Collect CPU/memory metrics (range queries: app + proxy + istiod + apiserver) ----
        ${SCRIPT_DIR}/collect_metrics_with_app.sh "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS" || \
            echo "WARNING: CPU/memory metrics collection failed for RPS=$RPS"

        # ---- Collect inline benchmark histograms (Mazu strategies only) ----
        if [[ "$STRAT" != "istio" ]]; then
            ${SCRIPT_DIR}/collect_inline_metrics.sh "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS" || \
                echo "WARNING: Inline metrics collection failed for RPS=$RPS"
        fi

        # ---- Cleanup Prometheus ----
        kill $PF_PID 2>/dev/null || true
        ${SCRIPT_DIR}/setup_social_network.sh uninstall-prometheus

        # --- Generate .dat files ---
        python3 "${SCRIPT_DIR}/generate_dat_with_app.py" "$RES_DIR" || \
            echo "WARNING: CPU/memory .dat file generation failed"

        python3 "${SCRIPT_DIR}/generate_inline_dat.py" "$RES_DIR" || \
            echo "WARNING: Inline .dat file generation failed"

        echo "=== Benchmark 2 completed at $(date) for $STRAT ==="
    )
done

# --- Generate consolidated plot data and PDFs ---
echo "Generating consolidated plot data..."
python3 "${SCRIPT_DIR}/generate_plot_data.py" "$RESULTS_DIR" "${STRATEGIES[@]}" || \
    echo "WARNING: Plot data generation failed"

echo "Rendering combined CPU/memory time-series PDFs (matplotlib)..."
python3 "${SCRIPT_DIR}/generate_timeseries_plots.py" "$RESULTS_DIR" "${STRATEGIES[@]}" || \
    echo "WARNING: Time-series plot generation failed"

echo "Copying gnuplot scripts and generating PDFs..."
for gpi in plot_cpu.gpi plot_memory.gpi plot_e2e_latency.gpi plot_latency_breakdown.gpi plot_latency_breakdown_v2.gpi; do
    cp "${SCRIPT_DIR}/results/${gpi}" "$RESULTS_DIR/" 2>/dev/null || true
done
(
    cd "$RESULTS_DIR"
    for gpi in plot_cpu.gpi plot_memory.gpi plot_e2e_latency.gpi plot_latency_breakdown.gpi plot_latency_breakdown_v2.gpi; do
        if [ -f "$gpi" ]; then
            gnuplot "$gpi" 2>/dev/null && echo "  Generated PDF from $gpi" || \
                echo "  WARNING: gnuplot failed for $gpi"
        fi
    done
)

echo "=== All benchmark 2 runs complete. Results in ${RESULTS_DIR} ==="
