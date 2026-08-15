#!/bin/bash
#
# SocialNetwork (DeathStarBench) on Istio -- install, seed and smoke test.
#
# Companion to run-benchmark1.5b-sweep.sh, but for the SocialNetwork
# microservice application from helm-chart/socialnetwork instead of Bookinfo.
# Istio-only for now: there is no mazu/TPM arm here yet.
#
# What it does, in order:
#   1. Tear down any previous social-network release and Istio install.
#   2. Install Istio (default profile) and enable sidecar injection on default.
#   3. Install the socialnetwork helm chart.
#   4. Wait for every pod to report Ready.
#   5. Seed the social graph (users + follows [+ posts]).
#   6. Run a wrk2 smoke test and save the output.
#
# Usage:
#   ./run-socialnetwork-istio.sh                 # full run
#   SKIP_INSTALL=1 ./run-socialnetwork-istio.sh  # reuse the running deployment
#   STOCK_VALUES=1 ./run-socialnetwork-istio.sh  # stock chart defaults, no overrides
#
# Environment overrides:
#   RESULTS_DIR    - output directory (default: results/socialnetwork-<date>)
#   VALUES_FILE    - helm values override
#                    (default: scratch/yaml/socialnetwork-bench-values.yaml)
#   STOCK_VALUES   - 1 to install with the chart's stock values.yaml only
#   SKIP_INSTALL   - 1 to skip teardown+install and just seed/benchmark
#   SKIP_SEED      - 1 to skip the social-graph seeding step
#   COMPOSE_POSTS  - 1 (default) to seed posts as well as users/follows
#   GRAPH          - dataset: socfb-Reed98 | ego-twitter | soc-twitter-follows-mun
#   RPS_VALUES     - space-separated wrk2 target rates (default: "400 600 800")
#   DURATION       - seconds per wrk2 step (default: 60)
#   THREADS/CONNS  - wrk2 -t / -c (default: 16 / 128)
#   WORKLOAD       - lua script name under wrk2/scripts/social-network
#                    (default: mixed-workload.lua)
#
# ---------------------------------------------------------------------------
# Two things that will bite you if you change this script:
#
#   * SEED AFTER INSTALL, ALWAYS. In the default (standalone) topology the
#     mongodb/redis/memcached pods have no PersistentVolume -- their data
#     lives in the container filesystem. Any helm upgrade that rolls those
#     pods silently wipes the dataset, and the benchmark then measures empty
#     timelines. That is why seeding is step 5 and not step 3.
#
#   * DO NOT USE read-home-timeline.lua UNDER LOAD. It ships with a
#     `response` hook that writes every status line, content-type and full
#     response body to stderr. It is a debugging aid, not a workload.
#     mixed-workload.lua (60% home timeline / 30% user timeline / 10%
#     compose) is the canonical DSB workload and has no such hook.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/socialnetwork-$(date +%m-%d-%y_%H%M%S)}"
VALUES_FILE="${VALUES_FILE:-${SCRIPT_DIR}/scratch/yaml/socialnetwork-bench-values.yaml}"
CHART_DIR="${SCRIPT_DIR}/helm-chart/socialnetwork"
RELEASE="${RELEASE:-social-network}"
NAMESPACE="${NAMESPACE:-default}"

GRAPH="${GRAPH:-socfb-Reed98}"
COMPOSE_POSTS="${COMPOSE_POSTS:-1}"
RPS_VALUES=(${RPS_VALUES:-400 600 800})
DURATION=${DURATION:-60}
THREADS=${THREADS:-16}
CONNS=${CONNS:-128}
WORKLOAD="${WORKLOAD:-mixed-workload.lua}"

ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

source_setup() {
    # Reuse the helpers from setup_social_network.sh without executing it.
    eval "$(grep -A5 '^mazu_echo()' "$SCRIPT_DIR/setup_social_network.sh")"
    eval "$(grep -A5 '^get_ingress_ip_port' "$SCRIPT_DIR/setup_social_network.sh")"
}
source_setup

# --- Ensure istioctl is installed ---
if [ ! -x "$ISTIOCTL_PATH" ]; then
    mazu_echo "istioctl not found, installing..."
    cd "$HOME"
    curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh -
    cd - > /dev/null
fi
if [ ! -x "$ISTIOCTL_PATH" ]; then
    echo "ERROR: istioctl still not found at $ISTIOCTL_PATH after install attempt"
    exit 1
fi

mkdir -p "$RESULTS_DIR"
LOG_FILE="$RESULTS_DIR/run.log"
exec > >(tee -a "$LOG_FILE") 2>&1

mazu_echo "=== SocialNetwork on Istio ==="
mazu_echo "Results:  ${RESULTS_DIR}"
mazu_echo "Chart:    ${CHART_DIR}"
mazu_echo "Graph:    ${GRAPH} (compose posts: ${COMPOSE_POSTS})"
mazu_echo "Workload: ${WORKLOAD} @ ${RPS_VALUES[*]} RPS for ${DURATION}s each"

echo "=== started at $(date) ==="

# ========================================================
# Phase A: teardown + install
# ========================================================
if [[ "${SKIP_INSTALL:-0}" != "1" ]]; then

    # ---- Tear down any previous release ----
    mazu_echo "Uninstalling previous social-network release (if any)..."
    helm uninstall "$RELEASE" -n "$NAMESPACE" 2>/dev/null || true
    kubectl delete -f "$SCRIPT_DIR/kubernetes/istio-gateway.yaml" --ignore-not-found 2>/dev/null || true
    kubectl wait --for=delete pod -l service=nginx-thrift -n "$NAMESPACE" --timeout=300s 2>/dev/null || true

    # ---- Tear down Istio ----
    mazu_echo "Removing Istio..."
    "$SCRIPT_DIR/setup_social_network.sh" remove-istio || true
    kubectl wait --for=delete pod -l app=istiod -n istio-system --timeout=300s 2>/dev/null || true
    kubectl wait --for=delete pod -l app=istio-ingressgateway -n istio-system --timeout=300s 2>/dev/null || true

    # ---- Install Istio (default profile) + sidecar injection + STRICT mTLS ----
    mazu_echo "Installing Istio..."
    "$SCRIPT_DIR/setup_social_network.sh" install-istio

    kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=300s
    kubectl wait --for=condition=Ready pod -l app=istio-ingressgateway -n istio-system --timeout=300s

    # ---- Gateway + VirtualService routing "/" -> nginx-thrift:8080 ----
    mazu_echo "Applying gateway and virtual service..."
    kubectl apply -f "$SCRIPT_DIR/kubernetes/istio-gateway.yaml"

    # RBAC the mcrouter post-install hook needs. Harmless in the default
    # (standalone memcached) topology, required if you flip
    # global.memcached.cluster.enabled=true.
    kubectl apply -f "$SCRIPT_DIR/scratch/yaml/mcrouter-role.yaml"

    # ---- Install the chart ----
    HELM_ARGS=()
    if [[ "${STOCK_VALUES:-0}" == "1" ]]; then
        mazu_echo "Installing chart with STOCK values (expect a ~200 RPS ceiling)..."
    elif [[ -f "$VALUES_FILE" ]]; then
        mazu_echo "Installing chart with overrides from ${VALUES_FILE}..."
        HELM_ARGS+=(-f "$VALUES_FILE")
    else
        mazu_echo "WARNING: ${VALUES_FILE} not found, falling back to stock values"
    fi

    helm upgrade --install "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" \
        "${HELM_ARGS[@]}" --timeout 10m0s
fi

# ========================================================
# Phase B: wait for readiness
# ========================================================
# `kubectl wait` only covers pods that already exist, and the chart brings up
# ~30-56 pods over the course of a minute, so poll the whole set instead.
mazu_echo "Waiting for all social-network pods to become Ready..."
DEADLINE=$(( $(date +%s) + 900 ))
while :; do
    NOT_READY=$(kubectl get pods -n "$NAMESPACE" -l 'service' --no-headers 2>/dev/null \
        | grep -cv ' 2/2  *Running' || true)
    TOTAL=$(kubectl get pods -n "$NAMESPACE" -l 'service' --no-headers 2>/dev/null | wc -l)
    [ "${NOT_READY:-1}" -eq 0 ] && [ "${TOTAL:-0}" -gt 0 ] && break
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "ERROR: ${NOT_READY}/${TOTAL} pods still not Ready after 900s"
        kubectl get pods -n "$NAMESPACE" -o wide | grep -v ' 2/2  *Running' || true
        exit 1
    fi
    echo "  $(date +%T) not-ready=${NOT_READY}/${TOTAL}"
    sleep 10
done
mazu_echo "All ${TOTAL} social-network pods are Ready"
kubectl get pods -n "$NAMESPACE" -o wide > "$RESULTS_DIR/pods.txt"

# ---- Fetch ingress ----
get_ingress_ip_port
if [[ -z "${INGRESS_IP:-}" || -z "${INGRESS_PORT:-}" ]]; then
    echo "ERROR: could not resolve the istio-ingressgateway address"
    kubectl -n istio-system get svc istio-ingressgateway
    exit 1
fi
ADDR="http://${INGRESS_IP}:${INGRESS_PORT}"
mazu_echo "Ingress: ${INGRESS_IP}:${INGRESS_PORT}"

# ---- Wait for the frontend to actually answer ----
mazu_echo "Waiting for nginx-thrift to serve through the gateway..."
for i in $(seq 1 60); do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$ADDR/" || true)
    [ "$CODE" = "200" ] && break
    if [ "$i" -eq 60 ]; then
        echo "ERROR: gateway never returned 200 (last code: ${CODE})"
        exit 1
    fi
    sleep 5
done
mazu_echo "Frontend is serving"

# ========================================================
# Phase C: seed the social graph
# ========================================================
# Must happen AFTER the last helm install -- see the note at the top of the
# file about the datastores having no persistent volume.
if [[ "${SKIP_SEED:-0}" != "1" ]]; then
    mazu_echo "Seeding social graph from ${GRAPH}..."
    SEED_ARGS=(--graph="$GRAPH" --ip="$INGRESS_IP" --port="$INGRESS_PORT")
    [[ "$COMPOSE_POSTS" == "1" ]] && SEED_ARGS+=(--compose)

    # init_social_graph.py resolves the dataset via a relative path.
    ( cd "$SCRIPT_DIR" && python3 scripts/init_social_graph.py "${SEED_ARGS[@]}" ) \
        | grep -Ev '^[0-9]+$' | tee "$RESULTS_DIR/seed.txt"

    if grep -q '^Failed:' "$RESULTS_DIR/seed.txt"; then
        echo "WARNING: some seeding requests failed -- see $RESULTS_DIR/seed.txt"
    fi
fi

# ---- Sanity-check that reads return real data ----
mazu_echo "Verifying timelines return data..."
{
    echo "--- user-timeline/read?user_id=5 ---"
    curl -sS --max-time 15 "$ADDR/wrk2-api/user-timeline/read?user_id=5&start=0&stop=2"
    echo; echo "--- home-timeline/read?user_id=5 ---"
    curl -sS --max-time 15 "$ADDR/wrk2-api/home-timeline/read?user_id=5&start=0&stop=2"
    echo
} > "$RESULTS_DIR/smoke.txt"
# Full payloads land in smoke.txt; a whole timeline page is far too much to
# dump into the run log, so only show the first line of each response here.
cut -c1-200 "$RESULTS_DIR/smoke.txt" | sed 's/$/ .../'

if [[ "${SKIP_SEED:-0}" != "1" ]] && grep -qx '{}' "$RESULTS_DIR/smoke.txt"; then
    echo "WARNING: a timeline came back empty. If pods rolled after seeding,"
    echo "         the datastores lost their data -- re-run the seeding step."
fi

# ========================================================
# Phase D: wrk2 load
# ========================================================
WRK_BIN="${SCRIPT_DIR}/../wrk2/wrk"
if [ ! -x "$WRK_BIN" ]; then
    mazu_echo "wrk2 not built, building..."
    "$SCRIPT_DIR/setup_social_network.sh" build-wrk2
fi

LUA="${SCRIPT_DIR}/wrk2/scripts/social-network/${WORKLOAD}"
if [ ! -f "$LUA" ]; then
    echo "ERROR: workload script not found: $LUA"
    exit 1
fi

for RPS in "${RPS_VALUES[@]}"; do
    OUT_FILE="$RESULTS_DIR/${RPS}.txt"
    mazu_echo "--- wrk2: ${WORKLOAD} @ ${RPS} RPS for ${DURATION}s -> ${OUT_FILE} ---"

    "$WRK_BIN" -D exp -t "$THREADS" -c "$CONNS" -d "$DURATION" -L \
        -s "$LUA" "$ADDR" -R "$RPS" > "$OUT_FILE" 2>&1

    grep -E '50\.000%|90\.000%|99\.000%|99\.900%|Requests/sec|Non-2xx|Socket errors|requests in' \
        "$OUT_FILE" || true
done

mazu_echo "=== complete at $(date). Results in ${RESULTS_DIR} ==="
