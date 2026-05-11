#!/bin/bash
#
# Benchmark 1.5: Steady State (No Connection Reuse)
#
# - App: Bookinfo, HPA disabled (fixed replicas)
# - Connection reuse: Disabled (maxRequestsPerConnection: 1)
# - Measures: p50/p90/p95 latency + CPU/memory via Prometheus
# - What it measures: Per-connection TLS handshake cost. Every new connection
#   triggers doVerifyCertChain() -> gRPC to ext_authz -> TokenReview + regStore
#   lookup on both sides (mutual).
#
# Usage:
#   ./run-benchmark1.5.sh
#
# Environment overrides:
#   STRATEGIES   - space-separated list (default: all 5)
#   RPS_VALUES   - space-separated list (default: 50 100 150 200 250 300)
#   DURATION     - seconds per RPS level (default: 240)
#   RESULTS_DIR  - output directory (default: results/benchmark1.5-<date>)
#   PROM_PORT    - local port for Prometheus port-forward (default: 9091)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
STRATEGIES=(${STRATEGIES:-"istio" "st5-AttUpd"})

RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/benchmark1.5-$(date +%m-%d-%y_%H%M%S)}"
PROM_PORT=${PROM_PORT:-9091}
SCALE_ENABLED=${SCALE_ENABLED:-false}

if [[ "$SCALE_ENABLED" == "true" ]]; then
    # the below rps_values are used for benchmark when keep alive is on; 
    # scaling is on; cpu cores for istio-proxy and application container are set to 2
    # measures the cost of scaling
    # RPS_VALUES=(${RPS_VALUES:-50 100 200 300 400 500 600 700 800 900 1000})
    # RPS_VALUES=(${RPS_VALUES:-50 100 200 300 400 500})
    RPS_VALUES=(${RPS_VALUES:-50 100 200 300 400 500 600 700 800 900 1000 1100 1200})
    DURATION=${DURATION:-120}
else
    # the below rps_values are used for benchmark when keep alive is off; 
    # scaling is off; cpu cores for istio-proxy and application container are set to 2
    # measures the cost of repeated connection setup with fixed number of pods
    # RPS_VALUES=(${RPS_VALUES:-50 100 150 200 250 300 350 400 450 500})
    RPS_VALUES=(${RPS_VALUES:-100 200 300 400 500 600 700 800})
    DURATION=${DURATION:-120}
fi
ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

source_setup() {
    # Source the helper function from setup_social_network.sh without executing commands
    eval "$(grep -A5 '^mazu_echo()' "$SCRIPT_DIR/setup_social_network.sh")"
    eval "$(grep -A5 '^get_ingress_ip_port' "$SCRIPT_DIR/setup_social_network.sh")"
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

mazu_echo "=== Benchmark 1.5: Steady State (No Connection Reuse) ==="
mazu_echo "Strategies: ${STRATEGIES[*]}"
mazu_echo "RPS values: ${RPS_VALUES[*]}"
mazu_echo "Duration: ${DURATION}s per RPS level"
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
        
        echo "=== Benchmark 1.5 started at $(date) for $STRAT ==="

        # ---- RPS sweep ----
        for RPS in "${RPS_VALUES[@]}"; do
            echo "--- Running RPS=$RPS for ${DURATION}s ---"

            export RPS

            # ---- Teardown previous workload & Istio ----
            ${SCRIPT_DIR}/setup_social_network.sh uninstall-bf
            kubectl wait --for=delete pod -l app=details --timeout=300s 2>/dev/null || true
            kubectl wait --for=delete pod -l app=productpage --timeout=300s 2>/dev/null || true
            kubectl wait --for=delete pod -l app=ratings --timeout=300s 2>/dev/null || true
            kubectl wait --for=delete pod -l app=reviews --timeout=300s 2>/dev/null || true

            ${SCRIPT_DIR}/setup_social_network.sh remove-istio
            kubectl wait --for=delete pod -l app=istiod -n istio-system --timeout=300s 2>/dev/null || true
            kubectl wait --for=delete pod -l app=istio-ingressgateway -n istio-system --timeout=300s 2>/dev/null || true

            # delete kube-apiserver between runs and wait for all replacements to be Ready
            APISERVER_COUNT=$(kubectl -n kube-system get pods -l component=kube-apiserver --no-headers 2>/dev/null | wc -l)
            kubectl -n kube-system delete pods -l component=kube-apiserver --now --timeout=60s 2>/dev/null || true

            echo "API server count: $APISERVER_COUNT. Waiting for all kube-apiserver pods to become Ready..."
            echo "Waiting for all kube-apiserver pods to become Ready..."
            until [ "$(kubectl -n kube-system get pods -l component=kube-apiserver -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null | grep -c True)" -ge "$APISERVER_COUNT" ]; do
                sleep 5
            done
            echo "All kube-apiserver pods are Ready"

            # ---- Create fresh TPMs ----
            echo "Creating TPMs on all nodes..."
            NODE0="apoudel@c220g1-031111.wisc.cloudlab.us"
            SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            # On first run, clone trinc repo and set up TPM libs on all nodes
            if ! ssh $SSH_OPTS "$NODE0" 'test -d ~/trinc'; then
                echo "First run: setting up trinc/swtpm on all nodes..."
                ${SCRIPT_DIR}/dev/setup-tpm-all-nodes.sh -d apt.emulab.net apt033 apt030 apt029 apt036
            fi
            ssh $SSH_OPTS "$NODE0" 'for node in node-0 node-1 node-2 node-3 node-4 node-5; do ssh "$node" "~/trinc/swtpm-test/setup-tpm.sh create_tpm" & done; wait'
            echo "TPMs created on all nodes"

            sleep 60

            # ---- Install Istio / Mazu ----
            if [[ "$STRAT" == "istio" ]]; then
                ${SCRIPT_DIR}/dev/deploy-mazu-configmap.sh "$STRAT"
                ${SCRIPT_DIR}/dev/deploy-rbe-pp.sh

                ${SCRIPT_DIR}/setup_social_network.sh install-istio
            else
                ${SCRIPT_DIR}/dev/deploy-mazu-configmap.sh "$STRAT"
                ${SCRIPT_DIR}/dev/deploy-rbe-pp.sh

                if [[ "$STRAT" == "st5-AttUpd" ]]; then
                    ${SCRIPT_DIR}/dev/tpm/install-k8s-tpm-device.sh
                    ${SCRIPT_DIR}/dev/tpm/deploy-tpm-pubkey-configmap.sh
                    ${SCRIPT_DIR}/dev/tpm/deploy-tpm-secret.sh
                fi

                ${SCRIPT_DIR}/setup_social_network.sh install-mazu
            fi

            # ---- Install Bookinfo ----
            if [[ "$STRAT" == "st5-AttUpd" ]]; then
                if [[ "$SCALE_ENABLED" == "true" ]]; then
                    kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-const-tpm.yaml"
                else
                    kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-var-tpm.yaml"
                fi
            else
                if [[ "$SCALE_ENABLED" == "true" ]]; then
                    kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-const.yaml"
                else
                    kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-var.yaml"
                fi
            fi
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-gateway.yaml"
            if [[ "$SCALE_ENABLED" == "true" ]]; then
                kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-hpa.yaml"
            fi

            # ---- Disable connection reuse via DestinationRules ----
            # Forces maxRequestsPerConnection=1 so every request gets a new connection
            # Only apply when scaling is disabled (keep-alive off scenario)
            if [[ "$SCALE_ENABLED" != "true" ]]; then
                kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-no-connection-reuse.yaml"
            fi

            kubectl wait --for=condition=Ready pod -l app=details --timeout=300s
            kubectl wait --for=condition=Ready pod -l app=productpage --timeout=300s
            kubectl wait --for=condition=Ready pod -l app=ratings --timeout=300s
            kubectl wait --for=condition=Ready pod -l app=reviews --timeout=300s
            kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s
            kubectl wait --for=condition=Ready pod -l app=istio-ingressgateway -n istio-system --timeout=300s

            # ---- Fetch ingress ----
            get_ingress_ip_port
            echo "Ingress: ${INGRESS_IP}:${INGRESS_PORT}"

            # Kill any leftover port-forward on PROM_PORT
            lsof -ti :${PROM_PORT} | xargs -r kill 2>/dev/null || true
            sleep 5

            # Fresh Prometheus for each RPS run
            ${SCRIPT_DIR}/setup_social_network.sh uninstall-prometheus
            ${SCRIPT_DIR}/setup_social_network.sh install-prometheus

            # Wait until Prometheus pod is Running
            echo "Waiting for Prometheus pod to be Running..."
            kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus -n istio-system --timeout=300s

            # Start port-forward to Prometheus in background
            kubectl port-forward -n istio-system svc/prometheus ${PROM_PORT}:9090 &
            PF_PID=$!
            export PROM_URL="http://localhost:${PROM_PORT}"

            sleep 5

            # Wait until Prometheus is reachable via port-forward
            echo "Waiting for Prometheus to be reachable at ${PROM_URL}..."
            for i in $(seq 1 30); do
                if curl -sf "${PROM_URL}/-/ready" > /dev/null 2>&1; then
                    echo "Prometheus is reachable at ${PROM_URL}"
                    break
                fi
                if [ "$i" -eq 30 ]; then
                    echo "WARNING: Prometheus not reachable after 30s, metrics collection may fail"
                fi
                sleep 5
            done

            OUT_FILE="$RES_DIR/${RPS}.txt"

            echo "Running wrk2 with STRAT=$STRAT, RPS=$RPS, DURATION=${DURATION}s. Output: ${OUT_FILE}"

            # Record start time for Prometheus queries
            BENCH_START=$(date +%s)

            POD_POLL_PID=""
            if [[ "$SCALE_ENABLED" == "true" ]]; then
                POD_CSV="$RES_DIR/pods-${RPS}.csv"
                echo "timestamp,app,version,phase,ready" > "$POD_CSV"
                (
                    while true; do
                        ts=$(date +%s)
                        kubectl get pods -l 'app in (productpage,details,reviews,ratings)' \
                            -o jsonpath='{range .items[*]}{.metadata.labels.app}{","}{.metadata.labels.version}{","}{.status.phase}{","}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null \
                          | awk -v ts="$ts" 'NF{print ts","$0}' >> "$POD_CSV" &
                        sleep 1
                    done
                ) &
                POD_POLL_PID=$!
            fi

            ${SCRIPT_DIR}/../wrk2/wrk -D exp -t 16 -c 128 -d ${DURATION} -L \
                -s ${SCRIPT_DIR}/wrk2/scripts/social-network/read-productpage.lua \
                http://$INGRESS_IP:$INGRESS_PORT -R ${RPS} > "${OUT_FILE}"

            BENCH_END=$(date +%s)

            if [[ -n "$POD_POLL_PID" ]]; then
                kill "$POD_POLL_PID" 2>/dev/null || true
                wait "$POD_POLL_PID" 2>/dev/null || true
            fi

            echo "wrk2 results saved to ${OUT_FILE}"

            # Collect CPU/memory metrics from Prometheus
            ${SCRIPT_DIR}/collect_metrics.sh "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS" || \
                echo "WARNING: Metrics collection failed for RPS=$RPS"

            # Cleanup port-forward and Prometheus
            kill $PF_PID 2>/dev/null || true
            ${SCRIPT_DIR}/setup_social_network.sh uninstall-prometheus

            echo "--- RPS=$RPS complete ---"
        done

        # --- Generate gnuplot .dat files from collected metrics ---
        python3 "${SCRIPT_DIR}/generate_dat.py" "$RES_DIR" || \
            echo "WARNING: .dat file generation failed"

        echo "=== Benchmark 1.5 completed at $(date) for $STRAT ==="
    )
done

# --- Generate latency .dat files and combined CPU/memory .dat files ---
python3 "${SCRIPT_DIR}/results/parse_15_data.py" "$RESULTS_DIR" || \
    echo "WARNING: parse_15_data.py failed"

# --- Copy gnuplot scripts into results directory ---
cp "${SCRIPT_DIR}/results/plot_15_e2e_latency.gpi" "$RESULTS_DIR/" || true
cp "${SCRIPT_DIR}/results/plot_15_cpu.gpi" "$RESULTS_DIR/" || true
cp "${SCRIPT_DIR}/results/plot_15_memory.gpi" "$RESULTS_DIR/" || true
cp "${SCRIPT_DIR}/results/style.gpi" "$RESULTS_DIR/" || true

# --- Generate plots ---
(
    cd "$RESULTS_DIR"
    gnuplot plot_15_e2e_latency.gpi || echo "WARNING: plot_15_e2e_latency.gpi failed"
    gnuplot plot_15_cpu.gpi || echo "WARNING: plot_15_cpu.gpi failed"
    gnuplot plot_15_memory.gpi || echo "WARNING: plot_15_memory.gpi failed"
)

# --- Summarize and plot per-second pod-readiness data ---
python3 "${SCRIPT_DIR}/summarize_pods.py" "$RESULTS_DIR" || \
    echo "WARNING: summarize_pods.py failed"
python3 "${SCRIPT_DIR}/plot_pods.py" "$RESULTS_DIR" || \
    echo "WARNING: plot_pods.py failed"

echo "=== All benchmark 1.5 runs complete. Results in ${RESULTS_DIR} ==="
