#!/bin/bash
#
# Benchmark 1.5b: HPA-enabled continuous RPS sweep
#
# Why a separate script instead of branching run-benchmark1.5.sh:
#   The HPA-on case is fundamentally different from the keep-alive-off
#   fixed-fleet case. With HPA enabled, reinstalling the deployment between
#   RPS points resets the pod fleet to baseline every time, so 120 s wrk2
#   runs catch transients off a cold fleet instead of an HPA-driven steady
#   state. See:
#     results/scaling-on-bench1.5-may6/sweep-design-discussion.md
#     results/scaling-on-bench1.5-may6/sweep-implementation-plan.md
#
# This script installs the deployment ONCE per strategy and then sweeps
# RPS values back-to-back with the pod fleet carrying over. Pod count is
# sampled every 1 s across the whole sweep into a single pods.csv; per-RPS
# slices (pods-${RPS}.csv) are produced afterward so the existing
# summarize_pods.py / plot_pods.py keep working.
#
# Usage:
#   ./run-benchmark1.5b-sweep.sh
#
# Environment overrides:
#   STRATEGIES   - space-separated list (default: "istio st5-AttUpd")
#   RPS_VALUES   - space-separated list, MUST be monotonically increasing
#                  (default: 50 100 200 300 400 500 600 700 800 900 1000 1100 1200)
#   DURATION     - seconds per RPS step (default: 120)
#   RESULTS_DIR  - output directory (default: results/benchmark1.5b-sweep-<date>)
#   PROM_PORT    - local port for Prometheus port-forward (default: 9091)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
STRATEGIES=(${STRATEGIES:-"st5-AttUpd" "istio"})
# RPS_VALUES=(${RPS_VALUES:-50 100 200 300 400 500 600 700 800 900 1000 1100 1200})
# RPS_VALUES=(${RPS_VALUES:-100 200 400 600 800 1000 1200 1400 1600 1800 2000 2200 2400 2600 2800 3000 3200})
# RPS_VALUES=(${RPS_VALUES:-100 200 400 600 800 1000 1200 1400 1600 1800 2000})
# RPS_VALUES=(${RPS_VALUES:-100 200 400 800 1200 1600 2000 2400 2800 3200})
# RPS_VALUES=(${RPS_VALUES:-100 400 800 1200 1600 2000 2400 2800 3200 3600 4000})
RPS_VALUES=(${RPS_VALUES:-100 200 400 800 1000 1500 2000 2500 3000 3500 4000 4500 5000})
# RPS_VALUES=(${RPS_VALUES:-100 1000 2000 3000 4000})
# DURATION=${DURATION:-120}
DURATION=${DURATION:-240}
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/benchmark1.5b-sweep-$(date +%m-%d-%y_%H%M%S)}"
PROM_PORT=${PROM_PORT:-9091}

ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

# Ingress gateway fleet size. Keep in sync with hpaSpec min/maxReplicas in
# scratch/yaml/istio-operator.yaml and scratch/yaml/istio-operator-tpm.yaml --
# the mazu arms get it from there, the istio baseline is patched to match below.
GW_REPLICAS=${GW_REPLICAS:-10}

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

mazu_echo "=== Benchmark 1.5b: HPA-enabled continuous RPS sweep ==="
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

        echo "=== Benchmark 1.5b sweep started at $(date) for $STRAT ==="

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
        NODE0="apoudel@pc841.emulab.net"
        SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        if ! ssh $SSH_OPTS "$NODE0" 'test -d ~/trinc'; then
            echo "First run: setting up trinc/swtpm on all nodes..."
            ${SCRIPT_DIR}/dev/setup-tpm-all-nodes.sh -d emulab.net pc829 pc841
        fi
        ssh $SSH_OPTS "$NODE0" 'for node in node-0 node-1; do ssh "$node" "~/trinc/swtpm-test/setup-tpm.sh create_tpm" & done; wait'
        echo "TPMs created on all nodes"

        sleep 60

        # ---- Install Istio / Mazu ----
        if [[ "$STRAT" == "istio" ]]; then
            ${SCRIPT_DIR}/dev/deploy-mazu-configmap.sh "$STRAT"
            ${SCRIPT_DIR}/dev/deploy-rbe-pp.sh

            ${SCRIPT_DIR}/setup_social_network.sh install-istio

            # install-istio applies the stock default profile, which gives the
            # gateway an HPA of 1..5 and no node affinity. The mazu arms get a
            # fixed fleet of GW_REPLICAS pods kept off the control-plane nodes
            # (scratch/yaml/istio-operator*.yaml). Match that here -- otherwise
            # the baseline saturates its gateway at high RPS and the comparison
            # measures gateway starvation instead of the data plane.
            kubectl -n istio-system patch deployment istio-ingressgateway --type=merge -p '{
              "spec": {"template": {"spec": {"affinity": {"nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                  {"matchExpressions": [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}
                  ]}
                ]}
              }}}}}
            }'
            kubectl -n istio-system patch hpa istio-ingressgateway --type=merge \
                -p "{\"spec\": {\"minReplicas\": ${GW_REPLICAS}, \"maxReplicas\": ${GW_REPLICAS}}}"
            kubectl -n istio-system rollout status deployment/istio-ingressgateway --timeout=300s
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

        # ---- Install Bookinfo (HPA-on variant) ----
        if [[ "$STRAT" == "st5-AttUpd" ]]; then
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-const-tpm.yaml"
        else
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bookinfo-const.yaml"
        fi
        kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-gateway.yaml"
        kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-hpa.yaml"

        kubectl wait --for=condition=Ready pod -l app=details --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=productpage --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=ratings --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=reviews --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s
        kubectl wait --for=condition=Ready pod -l app=istio-ingressgateway -n istio-system --timeout=300s

        # The wait above only covers the pods that exist at that moment, so hold
        # until the full gateway fleet is Ready in both arms.
        echo "Waiting for ${GW_REPLICAS} ready istio-ingressgateway replicas..."
        for i in $(seq 1 60); do
            GW_READY=$(kubectl -n istio-system get deploy istio-ingressgateway \
                -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
            [ "${GW_READY:-0}" -ge "$GW_REPLICAS" ] && break
            if [ "$i" -eq 60 ]; then
                echo "ERROR: only ${GW_READY:-0}/${GW_REPLICAS} istio-ingressgateway replicas Ready after 300s"
                exit 1
            fi
            sleep 5
        done
        echo "istio-ingressgateway: ${GW_READY} replicas Ready"

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

        # ---- Start the spanning pod-poller (1 Hz, runs for the whole sweep) ----
        POD_CSV="$RES_DIR/pods.csv"
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

            # Slice the spanning pods.csv into a per-RPS file for the
            # [BENCH_START, BENCH_END] window so summarize_pods.py /
            # plot_pods.py keep working unchanged.
            awk -F, -v s="$BENCH_START" -v e="$BENCH_END" \
                'NR==1 || ($1>=s && $1<=e)' \
                "$POD_CSV" > "$RES_DIR/pods-${RPS}.csv"

            # Collect CPU/memory metrics from Prometheus for the window
            ${SCRIPT_DIR}/collect_metrics.sh "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS" || \
                echo "WARNING: Metrics collection failed for RPS=$RPS"

            echo "--- RPS=$RPS complete ---"
        done

        # ========================================================
        # Phase C: teardown (once per strategy)
        # ========================================================

        # Stop the spanning pod-poller
        kill "$POD_POLL_PID" 2>/dev/null || true
        wait "$POD_POLL_PID" 2>/dev/null || true

        # Stop Prometheus port-forward and uninstall Prometheus
        kill $PF_PID 2>/dev/null || true
        ${SCRIPT_DIR}/setup_social_network.sh uninstall-prometheus

        # Generate gnuplot .dat files from collected metrics
        python3 "${SCRIPT_DIR}/generate_dat.py" "$RES_DIR" || \
            echo "WARNING: .dat file generation failed"

        echo "=== Benchmark 1.5b sweep completed at $(date) for $STRAT ==="
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

echo "=== All benchmark 1.5b sweep runs complete. Results in ${RESULTS_DIR} ==="
