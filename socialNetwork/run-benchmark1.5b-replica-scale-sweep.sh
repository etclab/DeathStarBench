#!/bin/bash
#
# Benchmark 1.5b (no-scale): replica-scaling driver
#
# Runs the WHOLE of run-benchmark1.5b-noscale-sweep.sh once per replica
# multiplier, so the same Istio-vs-Mazu RPS sweep is measured against a 1x,
# 2x, 4x, 8x and 16x Bookinfo fleet, and then plots the five runs together.
#
# WHY A DRIVER AND NOT A NEW BENCHMARK
#   run-benchmark1.5b-noscale-sweep.sh already does everything that has to be
#   right here: teardown, kube-apiserver reset, fresh TPMs, mesh install,
#   Prometheus, the RPS sweep, sidecar-restart capture, per-run .dat/plots.
#   The ONLY thing that varies across scales is the replica count in the
#   Bookinfo manifest, so this script hands that script a copy of the stock
#   manifest with the replica counts multiplied (BOOKINFO_YAML /
#   BOOKINFO_TPM_YAML) and otherwise leaves it alone. Every scale is therefore
#   a full clean-slate run -- mesh reinstalled, TPMs recreated, Prometheus
#   restarted -- not a `kubectl scale` on a fleet that has already been under
#   load, which would carry warmed connection pools and sidecar state from the
#   previous scale into the next one.
#
#   The generated manifests are written INTO the results directory, not into
#   scratch/yaml, so each run carries the exact YAML it was measured with.
#
# WHAT "2x" MEANS
#   Every Deployment's replica count is multiplied, not set. The stock
#   manifest is 1 replica each for details-v1, ratings-v1, reviews-v1/v2/v3
#   and productpage-v1 -- 6 pods. So 2x is 12 pods, 16x is 96 pods, each with
#   an istio-proxy sidecar. HPA is off (this is the no-scale variant), so the
#   fleet is fixed for the whole of each scale's sweep.
#
# CAPACITY IS ON YOU
#   At 2 CPU per pod (1 app + 1 sidecar; see SRC_YAML below) a scale needs
#   2 * 6 * SCALE CPU of schedulable node, and a 32-CPU node seats 16 pods:
#
#       scale    app pods    CPU needed    % of a 480-CPU cluster
#       1x           6            12               3%
#       4x          24            48              10%
#       8x          48            96              20%
#       16x         96           192              40%
#       32x        192           384              80%
#       64x        384           768          does not fit
#
#   480 CPU is what this was developed against: 18 nodes of 32 CPU, minus the
#   control plane, minus one unreachable node, minus one excluded by node
#   affinity, leaves 15 usable workers. 32x fits at 80% with the reduced sidecar
#   reservation; it did NOT fit before that change (3 CPU per pod, 576 needed),
#   which is why the 09-05 run scheduled only 151 of 192 pods and left 42
#   Pending on "Insufficient cpu".
#
#   A scale the cluster cannot seat leaves its pods Pending, the rollout wait
#   times out, and that scale is recorded as failed and skipped -- the driver
#   keeps going and the plots are drawn from the scales that did run. Check
#   `kubectl describe node` for allocatable CPU before adding a scale.
#
# CONNECTION REUSE IS ON
#   The nested sweep's default is bf-no-connection-reuse.yaml
#   (maxRequestsPerConnection=1 on details/ratings/reviews), which makes every
#   request pay a fresh upstream connection. That is the wrong default for THIS
#   benchmark: the variable being swept is replica fan-out, and fan-out is
#   exactly what multiplies the number of distinct pod-to-pod connections. With
#   reuse off, scaling the fleet mostly scales handshakes, so the sweep measures
#   connection establishment rather than the mesh's steady-state data path, and
#   a mesh with a costlier handshake collapses at 4x for a reason that has
#   nothing to do with how it behaves in a normally-pooled deployment.
#   So this driver passes CONN_REUSE=1 and Envoy pools connections as it
#   normally would. Set CONN_REUSE=0 to get the old behaviour back -- but the
#   two are not comparable and must not be plotted together.
#
# RUNTIME
#   6 scales x 2 strategies x 7 RPS steps x DURATION, plus 12 full
#   teardown/install cycles. At the defaults that is ~2.8h of load and
#   realistically 6-9h wall clock. Run it under nohup/tmux.
#
# WHERE THE LOGS GO
#   Every log this run produces -- the driver's own progress, each nested
#   sweep's full transcript, and each strategy arm inside it -- lands in ONE
#   directory, <RESULTS_DIR>/logs:
#
#     logs/driver.log                 this script's progress and failures
#     logs/scale-1x-sweep.log         full transcript of the 1x nested sweep
#     logs/scale-1x-istio.log         just that sweep's Istio arm
#     logs/scale-1x-st5-AttUpd.log    just that sweep's Mazu arm
#     logs/scale-2x-...               and so on per scale
#
#   The per-strategy files are slices of their scale's transcript, not separate
#   streams, so scale-Nx-sweep.log is the one file to read for a whole scale.
#   Nothing is written to a log outside this directory, and nothing depends on
#   the invoking terminal being kept open -- a shell redirect on top is
#   optional, not the only record.
#
# Usage:
#   ./run-benchmark1.5b-replica-scale-sweep.sh
#   nohup ./run-benchmark1.5b-replica-scale-sweep.sh > /dev/null 2>&1 &
#
# Environment overrides:
#   SCALES       - space-separated replica multipliers (default: 1 2 4 8 16)
#   RPS_VALUES   - space-separated, MUST be monotonically increasing
#                  (default: 100 200 400 800 1200 1600 2000)
#   DURATION     - seconds per RPS step (default: 120)
#   STRATEGIES   - passed through (default: "st5-AttUpd istio")
#   RESULTS_DIR  - output root (default: results/benchmark1.5b-replica-scale-<date>)
#   PROM_PORT    - passed through (default: 9091)
#   SKIP_PLOT    - set to 1 to skip the final cross-scale plot
#   CONN_REUSE   - 1 (default here) leaves Envoy's connection pooling alone;
#                  0 restores bf-no-connection-reuse.yaml. See above.
#   SRC_YAML     - source Bookinfo manifest (default: scratch/yaml/bookinfo-const.yaml)
#   SRC_TPM_YAML - source Bookinfo TPM manifest (default: bookinfo-const-tpm.yaml)
#   ENVOY_DIAG   - 1 collects Envoy admin stats, proxy thread counts, cgroup
#                  throttle counters and apiserver TokenReview/APF numbers
#                  around every RPS step. Passed through to the nested sweep.
#                  Pair it with STATS_PREFIXES; on its own it will faithfully
#                  collect a fleet in which the interesting stats were never
#                  instantiated. See run-2x-envoy-diag.sh for the preset.
#   MAZU_TAG     - docker tag for the Mazu arm's pilot/proxyv2 images, when it
#                  differs from the strategy name. The strategy string
#                  "st5-AttUpd" gates the attestation ConfigMap, the TPM
#                  overlay and the TPM manifests, so a run against a rebuilt
#                  proxy must keep STRATEGIES as-is and set this instead of
#                  renaming the strategy. Recorded in the banner and scales.txt,
#                  because the results directory is named after the strategy and
#                  would otherwise not say which build produced it.
#   STATS_PREFIXES - replacement value for every
#                  sidecar.istio.io/statsInclusionPrefixes annotation in the
#                  generated manifests. Empty (default) leaves them alone.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Configuration ---
SCALES=(${SCALES:-1 2 4 8 16 32})
RPS_VALUES=(${RPS_VALUES:-100 200 400 800 1200 1600 2000})
DURATION=${DURATION:-120}
STRATEGIES="${STRATEGIES:-st5-AttUpd istio}"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/benchmark1.5b-replica-scale-$(date +%m-%d-%y_%H%M%S)}"
PROM_PORT=${PROM_PORT:-9091}
SKIP_PLOT=${SKIP_PLOT:-0}
# Overrides the nested sweep's default of 0. See "CONNECTION REUSE IS ON" above.
CONN_REUSE=${CONN_REUSE:-1}
ENVOY_DIAG=${ENVOY_DIAG:-0}
STATS_PREFIXES="${STATS_PREFIXES:-}"

SWEEP="${SCRIPT_DIR}/run-benchmark1.5b-noscale-sweep.sh"
# The -const manifests, not -var: -var reserves 6 CPU for the app container and
# another 6 for the sidecar (requests == limits), so a pod costs 12 CPU and a
# 32-CPU node seats exactly 2. That caps the cluster at ~30 pods and makes 8x
# (48 pods) and 16x (96) permanently unschedulable.
#
# -const requests 1 CPU for the app and 1 for the sidecar, so a pod costs 2 and
# a 32-CPU node seats 16.
#
# BOTH sidecar numbers are written out in the manifest on purpose. Up to the
# 09-05 run it set only sidecar.istio.io/proxyCPULimit: "2" and left the request
# unset, expecting the injector default of 100m. That is not what happens: a
# container with a CPU limit and no CPU request gets its request defaulted UP to
# the limit, so the sidecar quietly reserved 2 CPU, a pod cost 3 rather than the
# 1.1 this comment used to claim, and 32x became unschedulable. proxyCPU is now
# stated alongside proxyCPULimit so the reservation is explicit and cannot drift
# again; keep them together if you retune either.
#
# The pair is set to 1/1 rather than 2/2: Guaranteed QoS is preserved (request
# == limit, no oversubscription), and 32x fits. The cost is that the sidecar's
# CPU ceiling is halved, which matters most for the Mazu arm -- if Mazu plateaus
# at a suspiciously flat throughput across scales, check whether istio-proxy is
# being CPU-throttled before reading it as a fan-out effect.
#
# Override to bookinfo-var.yaml only for a small-scale run that needs the
# Guaranteed-QoS pod shape.
SRC_YAML="${SRC_YAML:-${SCRIPT_DIR}/scratch/yaml/bookinfo-const.yaml}"
SRC_TPM_YAML="${SRC_TPM_YAML:-${SCRIPT_DIR}/scratch/yaml/bookinfo-const-tpm.yaml}"

LOGS_DIR="${RESULTS_DIR}/logs"
DRIVER_LOG="${LOGS_DIR}/driver.log"

# Written to the terminal AND to driver.log, rather than teeing this whole
# script: the nested sweeps do their own logging into the same directory, and
# a blanket tee here would duplicate every one of their lines into driver.log.
# So driver.log stays the short "what happened at the top level" file.
log() {
    local line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$line"
    [ -d "$LOGS_DIR" ] && echo "$line" >> "$DRIVER_LOG"
}

for f in "$SWEEP" "$SRC_YAML" "$SRC_TPM_YAML"; do
    [ -e "$f" ] || { echo "ERROR: missing $f" >&2; exit 1; }
done
[ -x "$SWEEP" ] || { echo "ERROR: $SWEEP is not executable" >&2; exit 1; }

# --- Manifest scaling -------------------------------------------------------
# Multiply every Deployment's `replicas:` by $factor. Matched on a line that is
# nothing but indentation + "replicas: <int>", which in these manifests is only
# ever the Deployment spec field. awk (not sed) because this is arithmetic on
# the existing value: `replicas: 3` at 4x must become 12, so a scale stays a
# MULTIPLIER even if the stock manifest is edited later to start above 1.
# Also stamps progressDeadlineSeconds onto every Deployment, because the
# Kubernetes default of 600s is a 6-pod number and it silently outranks our own
# wait: once the controller declares ProgressDeadlineExceeded, anything watching
# the rollout is told the Deployment failed, however much budget the sweep still
# had. A 192-pod fleet that takes one sidecar restart to converge lands just past
# 600s, which is exactly how the 32x arm of the 09-05 19:50 run was thrown away
# three seconds before it reached 32/32. Tying it to the same READY_TIMEOUT the
# sweep waits with keeps the controller and the harness on one clock.
#
# Also rewrites sidecar.istio.io/statsInclusionPrefixes when STATS_PREFIXES is
# set. That annotation becomes an Envoy stats_matcher inclusion_list, and an
# inclusion_list is not a display filter -- a stat outside it is never
# instantiated. The stock value ("cluster.outbound,http.inbound,listener")
# therefore means server.watchdog_miss and the entire ssl.* tree do not exist
# in the proxy at all, so no amount of scraping after the fact can recover
# them. Widening it here rather than in the checked-in manifests keeps the
# default fleet's stats footprint unchanged: only a run that asked for the
# diagnostics pays for them.
scale_yaml() {
    local src="$1" dst="$2" factor="$3" deadline="$4"
    awk -v f="$factor" -v dl="$deadline" -v sp="${STATS_PREFIXES:-}" '
        # Drop any deadline already in the source so the line below cannot
        # become a duplicate key.
        /^[[:space:]]*progressDeadlineSeconds:[[:space:]]*[0-9]+[[:space:]]*$/ { next }
        /^[[:space:]]*sidecar\.istio\.io\/statsInclusionPrefixes:/ {
            if (sp != "") {
                match($0, /^[[:space:]]*/)
                indent = substr($0, 1, RLENGTH)
                printf "%ssidecar.istio.io/statsInclusionPrefixes: \"%s\"\n", indent, sp
                next
            }
        }
        /^[[:space:]]*replicas:[[:space:]]*[0-9]+[[:space:]]*$/ {
            match($0, /^[[:space:]]*/)
            indent = substr($0, 1, RLENGTH)
            printf "%sreplicas: %d\n", indent, ($2 + 0) * f
            printf "%sprogressDeadlineSeconds: %d\n", indent, dl
            hits++
            next
        }
        { print }
        END { if (hits == 0) { print "no replicas: line matched" > "/dev/stderr"; exit 1 } }
    ' "$src" > "$dst"
}

# Total app pods a manifest asks for, for the log line and the run manifest.
total_replicas() {
    awk '/^[[:space:]]*replicas:[[:space:]]*[0-9]+[[:space:]]*$/ { n += $2 } END { print n + 0 }' "$1"
}

mkdir -p "$RESULTS_DIR" "$LOGS_DIR"
SUMMARY="${RESULTS_DIR}/scales.txt"

log "=== Benchmark 1.5b replica-scale sweep ==="
log "Scales:      ${SCALES[*]} (x stock replicas)"
log "RPS values:  ${RPS_VALUES[*]}"
log "Duration:    ${DURATION}s per RPS step"
log "Strategies:  ${STRATEGIES}"
log "Conn reuse:  ${CONN_REUSE} (1 = Envoy default pooling, 0 = maxRequestsPerConnection=1)"
log "Mazu image:  docker.io/atosh502/proxyv2:${MAZU_TAG:-st5-AttUpd}${MAZU_TAG:+  (MAZU_TAG override)}"
log "Envoy diag:  ${ENVOY_DIAG}${STATS_PREFIXES:+  (statsInclusionPrefixes -> ${STATS_PREFIXES})}"
log "Results:     ${RESULTS_DIR}"
log "Logs:        ${LOGS_DIR} (driver.log + scale-<N>x-*.log)"

{
    echo "# Benchmark 1.5b replica-scale sweep"
    echo "# started      $(date)"
    echo "# scales       ${SCALES[*]}"
    echo "# rps_values   ${RPS_VALUES[*]}"
    echo "# duration_s   ${DURATION}"
    echo "# strategies   ${STRATEGIES}"
    echo "# source_yaml  ${SRC_YAML} / ${SRC_TPM_YAML}"
    echo "# conn_reuse   ${CONN_REUSE}"
    echo "# mazu_image   docker.io/atosh502/proxyv2:${MAZU_TAG:-st5-AttUpd}"
    printf "# %-8s %-12s %-14s %s\n" "scale" "app_replicas" "ready_timeout" "status"
} > "$SUMMARY"

FAILED=()

for SCALE in "${SCALES[@]}"; do
    if ! [[ "$SCALE" =~ ^[0-9]+$ ]] || [ "$SCALE" -lt 1 ]; then
        echo "ERROR: SCALES entry '$SCALE' is not a positive integer" >&2
        exit 1
    fi

    SCALE_DIR="${RESULTS_DIR}/scale-${SCALE}x"
    MANIFEST_DIR="${SCALE_DIR}/manifests"
    mkdir -p "$MANIFEST_DIR"

    # The stock 300s is a 6-pod number. Rolling out 96 pods plus 96 sidecars
    # (image pulls, TPM device plugin, sidecar injection) does not finish in
    # it, and a wait that expires kills the whole scale for no good reason.
    # Computed before the manifests so it can be baked into them as
    # progressDeadlineSeconds as well -- see scale_yaml().
    READY_SECONDS=$((300 + 60 * SCALE))
    READY_TIMEOUT="${READY_SECONDS}s"

    SCALED_YAML="${MANIFEST_DIR}/bookinfo-var.yaml"
    SCALED_TPM_YAML="${MANIFEST_DIR}/bookinfo-var-tpm.yaml"
    scale_yaml "$SRC_YAML"     "$SCALED_YAML"     "$SCALE" "$READY_SECONDS"
    scale_yaml "$SRC_TPM_YAML" "$SCALED_TPM_YAML" "$SCALE" "$READY_SECONDS"

    PODS=$(total_replicas "$SCALED_YAML")

    log "--- scale ${SCALE}x: ${PODS} app pods, ready timeout ${READY_TIMEOUT} ---"
    log "    manifests: ${SCALED_YAML}"

    STATUS=ok
    if ! env \
            RESULTS_DIR="$SCALE_DIR" \
            STRATEGIES="$STRATEGIES" \
            RPS_VALUES="${RPS_VALUES[*]}" \
            DURATION="$DURATION" \
            PROM_PORT="$PROM_PORT" \
            BOOKINFO_YAML="$SCALED_YAML" \
            BOOKINFO_TPM_YAML="$SCALED_TPM_YAML" \
            READY_TIMEOUT="$READY_TIMEOUT" \
            CONN_REUSE="$CONN_REUSE" \
            ENVOY_DIAG="$ENVOY_DIAG" \
            LOG_DIR="$LOGS_DIR" \
            LOG_PREFIX="scale-${SCALE}x-" \
            "$SWEEP"; then
        STATUS="FAILED"
        FAILED+=("${SCALE}x")
        # Deliberately not fatal: a scale that the cluster cannot seat (see the
        # capacity table in the header) must not throw away the scales that
        # already ran, and the
        # plotter draws whatever is on disk.
        log "WARNING: scale ${SCALE}x failed -- continuing with the remaining scales"
    fi

    printf "  %-8s %-12s %-14s %s\n" "${SCALE}x" "$PODS" "$READY_TIMEOUT" "$STATUS" >> "$SUMMARY"
    log "--- scale ${SCALE}x ${STATUS} ---"
done

echo "# finished $(date)" >> "$SUMMARY"

# --- Cross-scale plot -------------------------------------------------------
if [ "$SKIP_PLOT" != "1" ]; then
    log "Generating cross-scale Istio-vs-Mazu plots..."
    python3 "${SCRIPT_DIR}/plot_15b_replica_scale.py" "$RESULTS_DIR" 2>&1 \
        | tee -a "$DRIVER_LOG" \
        || log "WARNING: plot_15b_replica_scale.py failed"
fi

log "=== Replica-scale sweep complete. Results in ${RESULTS_DIR} ==="
tee -a "$DRIVER_LOG" < "$SUMMARY"

if [ ${#FAILED[@]} -gt 0 ]; then
    log "Scales that failed: ${FAILED[*]}"
    exit 1
fi
