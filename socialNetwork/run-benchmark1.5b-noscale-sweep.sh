#!/bin/bash
#
# Benchmark 1.5b (no-scale): HPA-disabled continuous RPS sweep
#
# Counterpart to run-benchmark1.5b-sweep.sh, but for the SCALE_ENABLED=false
# configuration of run-benchmark1.5.sh:
#   - App: Bookinfo with bookinfo-var(.tpm).yaml (fixed replicas, no HPA)
#   - DestinationRule: bf-no-connection-reuse.yaml (maxRequestsPerConnection=1),
#     applied only when CONN_REUSE=0 -- see that variable below.
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
#   BOOKINFO_YAML     - manifest applied for non-TPM strategies
#                       (default: scratch/yaml/bookinfo-var.yaml)
#   BOOKINFO_TPM_YAML - manifest applied for st5-AttUpd
#                       (default: scratch/yaml/bookinfo-var-tpm.yaml)
#                       Overriding these two is how run-benchmark1.5b-replica-scale-sweep.sh
#                       re-runs this sweep at 2x/4x/8x/16x replicas: it hands in
#                       copies of the stock manifests with the replica counts
#                       multiplied, so nothing else about the sweep changes.
#   READY_TIMEOUT     - per-Deployment readiness timeout for the Bookinfo fleet
#                       (default: 300s). A 16x fleet is ~96 app pods + sidecars
#                       and does not come up inside the stock 300s. Applied per
#                       Deployment, not to the fleet as a whole, so the
#                       wall-clock ceiling is this times the number of
#                       Deployments in the manifest (6 in the stock one).
#                       See wait_rollouts() for why this is now the ONLY clock
#                       that bounds the wait.
#   CONN_REUSE   - 0 (default) applies bf-no-connection-reuse.yaml, which pins
#                  maxRequestsPerConnection=1 on details/ratings/reviews so every
#                  request pays a fresh upstream connection. 1 leaves those
#                  DestinationRules off and lets Envoy pool connections the way
#                  it does by default.
#                  This is NOT a free knob: with reuse off, the per-request cost
#                  is dominated by connection establishment, which is the thing
#                  the replica-scale sweep varies the fan-out of -- so a sweep
#                  measured at CONN_REUSE=0 and one measured at CONN_REUSE=1 are
#                  answering different questions and must never be plotted on the
#                  same axes. The mode used is printed in the banner and is in
#                  sweep.log for every run.
#                  Either way the DestinationRules are deleted during teardown,
#                  because setup_social_network.sh uninstall-bf does not remove
#                  them and a stale one would silently survive into the next run.
#   LOG_DIR      - directory for this run's logs (default: RESULTS_DIR)
#   LOG_PREFIX   - filename prefix for them (default: none)
#   ENVOY_DIAG   - 1 turns on collect_envoy_stats.sh around every RPS step:
#                  an Envoy admin /stats snapshot before and after the step, a
#                  gauge sampler running alongside wrk2, and apiserver
#                  TokenReview/APF numbers for the window. Off by default
#                  because it costs a few kubectl execs per pod per step, which
#                  is noise at 2x and minutes at 16x.
#                  It only collects; it does not change what is measured. But
#                  the counters it wants have to EXIST, and the stock manifests'
#                  statsInclusionPrefixes annotation excludes server.* and
#                  ssl.* from ever being instantiated -- so a fleet deployed
#                  from an unmodified bookinfo-const*.yaml will report
#                  need_wider_stats=1 and the most useful rows will be blank.
#                  run-2x-envoy-diag.sh widens the annotation and sets this.
#
# Logs (all of them, so a run is never half-logged into a terminal that is
# later closed):
#   ${LOG_DIR}/${LOG_PREFIX}sweep.log     full transcript of this sweep
#   ${LOG_DIR}/${LOG_PREFIX}<strategy>.log  just that strategy's arm, a slice
#                                         of the transcript above
# LOG_DIR/LOG_PREFIX exist so run-benchmark1.5b-replica-scale-sweep.sh can point
# every nested sweep at ONE logs/ directory
# (logs/scale-4x-sweep.log, logs/scale-4x-istio.log, ...) instead of leaving ten
# run.log files scattered across ten result directories.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
STRATEGIES=(${STRATEGIES:-"st5-AttUpd" "istio"})
RPS_VALUES=(${RPS_VALUES:-100 200 300 400 500 600 700 800})
DURATION=${DURATION:-120}
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/benchmark1.5b-noscale-sweep-$(date +%m-%d-%y_%H%M%S)}"
PROM_PORT=${PROM_PORT:-9091}
BOOKINFO_YAML="${BOOKINFO_YAML:-${SCRIPT_DIR}/scratch/yaml/bookinfo-var.yaml}"
BOOKINFO_TPM_YAML="${BOOKINFO_TPM_YAML:-${SCRIPT_DIR}/scratch/yaml/bookinfo-var-tpm.yaml}"
READY_TIMEOUT=${READY_TIMEOUT:-300s}
CONN_REUSE=${CONN_REUSE:-0}
ENVOY_DIAG=${ENVOY_DIAG:-0}
LOG_DIR="${LOG_DIR:-$RESULTS_DIR}"
LOG_PREFIX="${LOG_PREFIX:-}"

# Tee everything from here on, BEFORE the istioctl bootstrap and the config
# banner: an istioctl download that fails, or a run whose banner says it used
# the wrong manifest, is exactly what you want in the log afterwards. The
# per-strategy tee below nests inside this one, so <strategy>.log is a slice of
# sweep.log rather than a separate stream. Same idiom as
# run-socialnetwork-strategies.sh.
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
exec > >(tee -a "${LOG_DIR}/${LOG_PREFIX}sweep.log") 2>&1

ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

source_setup() {
    # Source the helper functions from setup_social_network.sh without executing commands
    eval "$(grep -A5 '^mazu_echo()' "$SCRIPT_DIR/setup_social_network.sh")"
    eval "$(grep -A5 '^get_ingress_ip_port' "$SCRIPT_DIR/setup_social_network.sh")"
}
source_setup

# Wait for every Deployment in an applied manifest to finish rolling out.
#
# WHY NOT `kubectl wait --for=condition=Ready pod -l app=<svc>`
#   That was the first check here, and it is racy in two ways that only show up
#   on a large fleet. `kubectl wait` operates on the pods that match RIGHT NOW:
#   it does not wait for more to appear. So at 32x it returned "condition met"
#   for the 15 details pods that happened to exist and moved on, and then the
#   productpage wait ran before the scheduler had created any productpage pod at
#   all and died with "error: no matching resources found" -- which is how the
#   32x arm of an earlier run failed after ~40 minutes of setup, with nothing
#   wrong in the cluster.
#
# WHY NOT `kubectl rollout status`
#   That was the second check, and it is bounded by a clock this script does not
#   own. `rollout status` reports whatever the Deployment controller decided, and
#   the controller gives up after .spec.progressDeadlineSeconds (default 600s):
#   it sets Progressing=False/ProgressDeadlineExceeded and `rollout status` then
#   returns an error IMMEDIATELY, so --timeout= can never buy more than those
#   600s no matter how large READY_TIMEOUT is. That is how the 32x arm of the
#   09-05 19:50 run died: 191 of its 192 pods were Ready within 18s, one sidecar
#   needed a restart and came up at +620s, and the deadline tripped three seconds
#   before the fleet reached 32/32.
#
# So: poll the Deployment's own replica counts. That asks the same object
# `rollout status` asks, which keeps the property that made it safe over a pod
# selector -- Deployments exist the moment `kubectl apply` returns, so this
# cannot mistake "pods not created yet" for "nothing to wait for" -- while
# honouring READY_TIMEOUT and nothing else.
#
# run-benchmark1.5b-replica-scale-sweep.sh additionally stamps a matching
# progressDeadlineSeconds into the manifests it generates, so the controller's
# own verdict agrees with this wait instead of contradicting it.
#
# The names are read from the manifest rather than hard-coded so that editing
# bookinfo-*.yaml (adding a reviews-v4, renaming a service) cannot leave this
# silently waiting on the wrong set.
wait_rollouts() {
    local manifest="$1" deploys d name budget deadline now out fails
    local gen obs want upd ready last
    deploys=$(kubectl get -f "$manifest" -o name 2>/dev/null | grep '^deployment') || true
    if [ -z "$deploys" ]; then
        echo "ERROR: no Deployments found for $manifest" >&2
        return 1
    fi
    budget=${READY_TIMEOUT%s}
    echo "Waiting for rollouts (timeout ${budget}s each):"
    echo "$deploys" | sed 's/^/  /'
    for d in $deploys; do
        name=${d#deployment.apps/}
        deadline=$(( $(date +%s) + budget ))
        last=""
        fails=0
        while :; do
            # One call per poll. `or <field> 0` matters: a Deployment whose
            # controller has not written status yet has no .status.readyReplicas
            # at all, and an empty field would shift every later value under
            # `read` rather than reading as zero.
            if ! out=$(kubectl get "$d" -o go-template='{{.metadata.generation}} {{or .status.observedGeneration 0}} {{.spec.replicas}} {{or .status.updatedReplicas 0}} {{or .status.readyReplicas 0}}' 2>&1); then
                # A Deployment that is genuinely gone -- deleted underneath us,
                # or renamed in the manifest -- is never going to become ready,
                # and sitting out the whole budget for it throws away a scale
                # for something a single API call already knows. Tolerate a few
                # consecutive failures so a transient apiserver blip is not
                # fatal, then fail with whatever kubectl actually said.
                fails=$((fails + 1))
                if [ "$fails" -ge 5 ]; then
                    echo "ERROR: cannot read $d after ${fails} attempts: $out" >&2
                    return 1
                fi
                sleep 2
                continue
            fi
            fails=0
            read -r gen obs want upd ready <<<"$out"

            # observedGeneration >= generation means these counts describe the
            # spec we just applied and not the one before it.
            if [ -n "$want" ] && [ "$obs" -ge "$gen" ] \
               && [ "$upd" -eq "$want" ] && [ "$ready" -eq "$want" ]; then
                echo "deployment \"$name\" successfully rolled out (${ready}/${want} ready)"
                break
            fi

            now=$(date +%s)
            if [ "$now" -ge "$deadline" ]; then
                echo "ERROR: $d did not roll out within ${budget}s (${ready:-?}/${want:-?} ready)" >&2
                # The run is about to be recorded as FAILED, so leave enough in
                # the log to tell "cluster too small" from "image pull broke"
                # from "one sidecar is wedged".
                #
                # Filter on the READY column, not just the phase: the last
                # failure here was a pod that was Running the whole time and
                # simply never went Ready, which a `grep -v Running` dump hides
                # completely. Keep the events too -- they age out of the cluster
                # long before anyone reads this log.
                kubectl get pods -o wide 2>/dev/null \
                    | awk 'NR==1 { print; next }
                           { split($2, a, "/"); if (a[1] != a[2] || $3 != "Running") print }' \
                    | head -30 >&2 || true
                kubectl get events --sort-by=.lastTimestamp 2>/dev/null \
                    | tail -20 >&2 || true
                return 1
            fi

            # Only on change, so a 2000s wait does not write 1000 identical lines.
            if [ -n "$want" ] && [ "$ready" != "$last" ]; then
                echo "Waiting for deployment \"$name\" rollout to finish: ${ready} of ${want} replicas are ready..."
                last="$ready"
            fi
            sleep 2
        done
    done
}

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
mazu_echo "Manifests: ${BOOKINFO_YAML} (istio) / ${BOOKINFO_TPM_YAML} (st5-AttUpd)"
# Which proxy build the Mazu arm is measuring. Without this line two runs of the
# same strategy against different images are indistinguishable on disk, since
# the results directory is named after the strategy and not the tag.
mazu_echo "Mazu image tag: ${MAZU_TAG:-<strategy name>} (docker.io/atosh502/proxyv2:${MAZU_TAG:-st5-AttUpd})"
if [ "$CONN_REUSE" = "1" ]; then
    mazu_echo "Connection reuse: ENABLED (bf-no-connection-reuse.yaml NOT applied)"
else
    mazu_echo "Connection reuse: DISABLED (bf-no-connection-reuse.yaml, maxRequestsPerConnection=1)"
fi
if [ "$ENVOY_DIAG" = "1" ]; then
    mazu_echo "Envoy diagnostics: ENABLED (collect_envoy_stats.sh per RPS step)"
else
    mazu_echo "Envoy diagnostics: disabled (set ENVOY_DIAG=1 to collect)"
fi
mazu_echo "Results: ${RESULTS_DIR}"
mazu_echo "Logs: ${LOG_DIR}/${LOG_PREFIX}sweep.log (+ one per strategy)"

# --- Per-strategy loop ---
for STRAT in "${STRATEGIES[@]}"; do
    RES_DIR="${RESULTS_DIR}/${STRAT}"
    mkdir -p "$RES_DIR"
    LOG_FILE="${LOG_DIR}/${LOG_PREFIX}${STRAT}.log"

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
        # uninstall-bf does not touch these, and they are cluster-scoped state in
        # the default namespace, so without this an earlier CONN_REUSE=0 run
        # leaves maxRequestsPerConnection=1 in force for a CONN_REUSE=1 one.
        kubectl delete -f "$SCRIPT_DIR/scratch/yaml/bf-no-connection-reuse.yaml" \
            --ignore-not-found 2>/dev/null || true
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
        NODE0="apoudel@pc772.emulab.net"
        SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        if ! ssh $SSH_OPTS "$NODE0" 'test -d ~/trinc'; then
            echo "First run: setting up trinc/swtpm on all nodes..."
            ${SCRIPT_DIR}/dev/setup-tpm-all-nodes.sh -d apt.emulab.net pc772 pc860
        fi
        ssh $SSH_OPTS "$NODE0" 'for node in node-0 node-1; do ssh "$node" "~/trinc/swtpm-test/setup-tpm.sh create_tpm" & done; wait'
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

        # ---- Install Bookinfo (HPA-off; connection reuse per CONN_REUSE) ----
        if [[ "$STRAT" == "st5-AttUpd" ]]; then
            kubectl apply -f "$BOOKINFO_TPM_YAML"
        else
            kubectl apply -f "$BOOKINFO_YAML"
        fi
        kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-gateway.yaml"
        if [ "$CONN_REUSE" = "1" ]; then
            echo "CONN_REUSE=1: leaving connection pooling at the Envoy default"
        else
            kubectl apply -f "$SCRIPT_DIR/scratch/yaml/bf-no-connection-reuse.yaml"
        fi

        if [[ "$STRAT" == "st5-AttUpd" ]]; then
            wait_rollouts "$BOOKINFO_TPM_YAML"
        else
            wait_rollouts "$BOOKINFO_YAML"
        fi
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

            # ---- Envoy diagnostics: pre-step counter snapshot ----
            # Taken immediately before load so the post-step delta is this
            # step's traffic and not the previous step's tail. Failure to
            # collect must not abort the sweep: the latency/CPU numbers are
            # still the primary result.
            if [ "$ENVOY_DIAG" = "1" ]; then
                ${SCRIPT_DIR}/collect_envoy_stats.sh snapshot "$RES_DIR" "pre-${RPS}" || \
                    echo "WARNING: pre-${RPS} Envoy snapshot failed"
            fi

            BENCH_START=$(date +%s)

            # ---- Envoy diagnostics: gauge sampler, alongside wrk2 ----
            # downstream_pre_cx_active and the proxy's thread count are gauges:
            # they are the whole point of the exercise and they are gone by the
            # time the step ends, so they have to be sampled during it.
            SAMPLER_PID=""
            if [ "$ENVOY_DIAG" = "1" ]; then
                ${SCRIPT_DIR}/collect_envoy_stats.sh sample "$RES_DIR" "${RPS}" "$DURATION" &
                SAMPLER_PID=$!
            fi

            ${SCRIPT_DIR}/../wrk2/wrk -D exp -t 16 -c 128 -d ${DURATION} -L \
                -s ${SCRIPT_DIR}/wrk2/scripts/social-network/read-productpage.lua \
                http://$INGRESS_IP:$INGRESS_PORT -R ${RPS} > "${OUT_FILE}"

            BENCH_END=$(date +%s)

            echo "wrk2 results saved to ${OUT_FILE}"

            # ---- Envoy diagnostics: post-step snapshot + apiserver window ----
            if [ "$ENVOY_DIAG" = "1" ]; then
                # The sampler bounds itself by DURATION, so it has normally
                # already exited; wait rather than kill so a slow final tick
                # still lands on disk.
                [ -n "$SAMPLER_PID" ] && wait "$SAMPLER_PID" 2>/dev/null || true

                ${SCRIPT_DIR}/collect_envoy_stats.sh snapshot "$RES_DIR" "post-${RPS}" || \
                    echo "WARNING: post-${RPS} Envoy snapshot failed"
                ${SCRIPT_DIR}/collect_envoy_stats.sh apiserver "$RES_DIR" "${RPS}" \
                    "$DURATION" "$BENCH_START" || \
                    echo "WARNING: apiserver metrics collection failed for RPS=$RPS"
            fi

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
