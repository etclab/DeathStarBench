#!/bin/bash
#
# Benchmark 1.5b (no-scale): HPA-disabled continuous RPS sweep
#
# Counterpart to run-benchmark1.5b-sweep.sh, but for the SCALE_ENABLED=false
# configuration of run-benchmark1.5.sh:
#   - App: Bookinfo with bookinfo-var(.tpm).yaml (fixed replicas, no HPA)
#   - DestinationRule: bf-no-connection-reuse.yaml (maxRequestsPerConnection=1)
#
# Like 1.5b-sweep, the deployment is installed ONCE per strategy and then RPS
# values are swept back-to-back without re-creating the workload. Since HPA is
# off the fleet is fixed for the whole sweep, so no pod-count sampling or
# pod-readiness plots are produced.
#
# Usage:
#   ./run-benchmark1.5b-noscale-sweep.sh
#
# Environment overrides:
#   STRATEGIES   - space-separated list (default: "istio st5-AttUpd")
#   RPS_VALUES   - space-separated list, MUST be monotonically increasing
#                  (default: 100 200 300 400 500 600 700 800)
#   DURATION     - seconds per RPS step (default: 120)
#   RESULTS_DIR  - output directory (default: results/benchmark1.5b-noscale-sweep-<date>)
#   PROM_PORT    - local port for Prometheus port-forward (default: 9091)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
STRATEGIES=(${STRATEGIES:-"st5-AttUpd" "istio"})
RPS_VALUES=(${RPS_VALUES:-100 200 300 400 500 600 700 800})
DURATION=${DURATION:-120}
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/benchmark1.5b-noscale-sweep-$(date +%m-%d-%y_%H%M%S)}"
PROM_PORT=${PROM_PORT:-9091}

ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

source_setup() {
    # Source the helper functions from setup_social_network.sh without executing commands
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

mazu_echo "=== Benchmark 1.5b (no-scale): HPA-disabled continuous RPS sweep ==="
mazu_echo "Strategies: ${STRATEGIES[*]}"
mazu_echo "RPS values: ${RPS_VALUES[*]}"
mazu_echo "Duration: ${DURATION}s per RPS step"
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

        echo "=== Benchmark 1.5b (no-scale) sweep started at $(date) for $STRAT ==="

        # ========================================================
        # Phase A: setup (once per strategy)
        # ========================================================

        # ---- Teardown previous workload & Istio ----
        ${SCRIPT_DIR}/setup_social_network.sh uninstall-bf
        kubectl wait --for=delete pod -l app=details --timeout=300s 2>/dev/null || true
        kubectl wait --for=delete pod -l app=productpage --timeout=300s 2>/dev/null || true
        kubectl wait --for=delete pod -l app=ratings --timeout=300s 2>/dev/null || true
        kubectl wait --for=delete pod -l app=reviews --timeout=300s 2>/dev/null || true

        ${SCRIPT_DIR}/setup_social_network.sh remove-istio
        kubectl wait --for=delete pod -l app=istiod -n istio-system --timeout=300s 2>/dev/null || true
        kubectl wait --for=delete pod -l app=istio-ingressgateway -n istio-system --timeout=300s 2>/dev/null || true

        # ---- Reset kube-apiserver once and wait for replacements to be Ready ----
        APISERVER_COUNT=$(kubectl -n kube-system get pods -l component=kube-apiserver --no-headers 2>/dev/null | wc -l)
        kubectl -n kube-system delete pods -l component=kube-apiserver --now --timeout=60s 2>/dev/null || true

        echo "API server count: $APISERVER_COUNT. Waiting for all kube-apiserver pods to become Ready..."
        until [ "$(kubectl -n kube-system get pods -l component=kube-apiserver -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null | grep -c True)" -ge "$APISERVER_COUNT" ]; do
            sleep 5
        done
        echo "All kube-apiserver pods are Ready"

        # ---- Create fresh TPMs on all nodes ----
        echo "Creating TPMs on all nodes..."
        NODE0="apoudel@pc781.emulab.net"
        SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
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

        # ---- Install Bookinfo (HPA-off / no-connection-reuse variant) ----
        if [[ "$STRAT" == "st5-AttUpd" ]]; then
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-var-tpm.yaml"
        else
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-var.yaml"
        fi
        kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-gateway.yaml"
        kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-no-connection-reuse.yaml"

        kubectl wait --for=condition=Ready pod -l app=details --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=productpage --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=ratings --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=reviews --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=istio-ingressgateway -n istio-system --timeout=300s

        # ---- Fetch ingress ----
        get_ingress_ip_port
        echo "Ingress: ${INGRESS_IP}:${INGRESS_PORT}"

        # ---- Prometheus (once per strategy) ----
        lsof -ti :${PROM_PORT} | xargs -r kill 2>/dev/null || true
        sleep 5

        ${SCRIPT_DIR}/setup_social_network.sh uninstall-prometheus
        ${SCRIPT_DIR}/setup_social_network.sh install-prometheus

        echo "Waiting for Prometheus pod to be Running..."
        kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus -n istio-system --timeout=300s

        kubectl port-forward -n istio-system svc/prometheus ${PROM_PORT}:9090 &
        PF_PID=$!
        export PROM_URL="http://localhost:${PROM_PORT}"

        sleep 5

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

        # ========================================================
        # Phase B: sweep (RPS loop, no teardown between steps)
        # ========================================================
        for RPS in "${RPS_VALUES[@]}"; do
            echo "--- Running RPS=$RPS for ${DURATION}s ---"
            export RPS

            OUT_FILE="$RES_DIR/${RPS}.txt"
            echo "Running wrk2 with STRAT=$STRAT, RPS=$RPS, DURATION=${DURATION}s. Output: ${OUT_FILE}"

            BENCH_START=$(date +%s)

            ${SCRIPT_DIR}/../wrk2/wrk -D exp -t 16 -c 128 -d ${DURATION} -L \
                -s ${SCRIPT_DIR}/wrk2/scripts/social-network/read-productpage.lua \
                http://$INGRESS_IP:$INGRESS_PORT -R ${RPS} > "${OUT_FILE}"

            BENCH_END=$(date +%s)

            echo "wrk2 results saved to ${OUT_FILE}"

            # Collect CPU/memory metrics from Prometheus for the window
            ${SCRIPT_DIR}/collect_metrics.sh "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS" || \
                echo "WARNING: Metrics collection failed for RPS=$RPS"

            # ---- Capture sidecar logs for any Bookinfo pod that has restarted ----
            # Dumps current + previous istio-proxy logs and `kubectl describe pod`
            # for each pod whose istio-proxy restartCount > 0. Run after every RPS
            # step so the next step's restart doesn't clobber `--previous` evidence.
            LOG_DIR="$RES_DIR/sidecar-logs-${RPS}"
            mkdir -p "$LOG_DIR"
            for app in details productpage ratings reviews; do
                while IFS=$'\t' read -r pod restarts; do
                    [ -z "$pod" ] && continue
                    if [ "${restarts:-0}" -gt 0 ]; then
                        echo "  capturing logs for $pod (istio-proxy restarts=$restarts)"
                        kubectl logs "$pod" -c istio-proxy            > "$LOG_DIR/${pod}.current.log" 2>&1 || true
                        kubectl logs "$pod" -c istio-proxy --previous > "$LOG_DIR/${pod}.previous.log" 2>&1 || true
                        kubectl describe pod "$pod"                   > "$LOG_DIR/${pod}.describe.txt" 2>&1 || true
                    fi
                done < <(kubectl get pods -l app=$app \
                    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[?(@.name=="istio-proxy")].restartCount}{"\n"}{end}')
            done

            echo "--- RPS=$RPS complete ---"
        done

        # ========================================================
        # Phase C: teardown (once per strategy)
        # ========================================================

        # Stop Prometheus port-forward and uninstall Prometheus
        kill $PF_PID 2>/dev/null || true
        ${SCRIPT_DIR}/setup_social_network.sh uninstall-prometheus

        # Generate gnuplot .dat files from collected metrics
        python3 "${SCRIPT_DIR}/generate_dat.py" "$RES_DIR" || \
            echo "WARNING: .dat file generation failed"

        echo "=== Benchmark 1.5b (no-scale) sweep completed at $(date) for $STRAT ==="
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

echo "=== All benchmark 1.5b (no-scale) sweep runs complete. Results in ${RESULTS_DIR} ==="
