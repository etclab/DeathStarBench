#!/bin/bash
#
# Collects CPU and memory metrics from Prometheus for a SocialNetwork
# benchmark run, aggregated PER POD.
#
# Usage:
#   ./collect_metrics_sn.sh <output_dir> <duration_seconds> <start_epoch> [rps]
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS AND NOT collect_metrics.sh
#
#   collect_metrics.sh collects ONLY container="istio-proxy" and files it under
#   a key named "bookinfo". Both are wrong here:
#
#     * SocialNetwork's HPAs (scratch/yaml/sn-hpa.yaml) scale on the MAX of the
#       app container's CPU and the istio-proxy sidecar's CPU. Collecting only
#       the sidecar means the metrics cannot say WHICH metric drove a scale-up
#       -- the central question of the HPA=1 replica-churn experiment.
#
#     * Its selector is namespace-scoped (namespace="default"), so it does in
#       fact match SocialNetwork pods -- it is not silently empty. But the
#       default namespace here holds the app tier AND the sidecars of the
#       mongodb / memcached / mcrouter / redis pods, all in one bucket.
#
#   collect_metrics.sh is NOT modified because run-benchmark1.sh,
#   run-benchmark1.5.sh, run-benchmark1.5b-sweep.sh and
#   run-benchmark1.5b-noscale-sweep.sh all call it, and generate_dat.py
#   hardcodes COMPONENTS = ["bookinfo", "istiod", "kube_apiserver"] against its
#   instant-vector schema.
#
# WHY `sum by (pod)`
#   The raw selectors return one series PER CONTAINER. A pod with more than one
#   app container therefore yields several series carrying the same `pod`
#   label, and generate_dat_with_app.py's extract_pod_series() de-duplicates
#   those by renaming the later ones to <name>-2, <name>-3 -- so a multi-container
#   pod silently turns into several fake "pods" whose values must not be added.
#   `sum by (pod)` collapses the containers of a pod into one series before the
#   data ever leaves Prometheus, which is both what the label claims and what
#   makes the per-pod numbers addable.
#
# WHY THE TOTALS BLOCK
#   Under HPA=1 the pod count is a dependent variable: it differs across RPS
#   steps AND across arms. Per-pod vectors are therefore not comparable between
#   arms at the same RPS -- an average over the vector flatters whichever arm
#   ran more pods. `totals` carries the fleet-wide sum, which is the number to
#   compare mazu against istio with.
#
# OUTPUT: <output_dir>/metrics_<rps>.json
#
#   cpu.<group> / memory.<group>
#       Prometheus query_range matrix, one series per pod, over the benchmark
#       window. Group names app/proxy/istiod/kube_apiserver are kept exactly as
#       generate_dat_with_app.py expects them, so that consumer works unchanged.
#
#   totals.cpu.<group> / totals.memory.<group>
#       Instant query, one scalar per group: the fleet-wide sum averaged over
#       the window.
#
#   Groups:
#     app             application containers in default ns (not istio-proxy)
#     proxy           istio-proxy sidecars in default ns
#     istiod          istiod in istio-system ns
#     ingressgateway  istio-ingressgateway pods in istio-system ns
#     kube_apiserver  kube-apiserver in kube-system ns
# ---------------------------------------------------------------------------

set -euo pipefail

OUT_DIR="${1:?Usage: $0 <output_dir> <duration_seconds> <start_epoch> [rps]}"
DURATION="${2:?Usage: $0 <output_dir> <duration_seconds> <start_epoch> [rps]}"
START_EPOCH="${3:?Usage: $0 <output_dir> <duration_seconds> <start_epoch> [rps]}"
RPS="${4:-unknown}"

END_EPOCH=$((START_EPOCH + DURATION))
STEP="${PROM_STEP:-15s}"

PROM_URL="${PROM_URL:-http://localhost:9091}"

mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/metrics_${RPS}.json"

# A query that fails must not abort the whole collection: under `set -e` a
# failed command substitution kills the script, and the caller then loses every
# other group for this RPS step over one transient 503. Fall back to an empty
# but well-formed Prometheus response so the JSON still parses.
EMPTY_MATRIX='{"status":"error","data":{"resultType":"matrix","result":[]}}'
EMPTY_VECTOR='{"status":"error","data":{"resultType":"vector","result":[]}}'

prom_query_range() {
    curl -s --fail -G "${PROM_URL}/api/v1/query_range" \
        --data-urlencode "query=$1" \
        --data-urlencode "start=${START_EPOCH}" \
        --data-urlencode "end=${END_EPOCH}" \
        --data-urlencode "step=${STEP}" \
        || echo "$EMPTY_MATRIX"
}

prom_query_instant() {
    curl -s --fail -G "${PROM_URL}/api/v1/query" \
        --data-urlencode "query=$1" \
        --data-urlencode "time=${END_EPOCH}" \
        || echo "$EMPTY_VECTOR"
}

# --- Selectors -------------------------------------------------------------
# container!="POD" drops the pause container; container!="" drops the cgroup
# roll-up series cadvisor emits per pod, which would otherwise double-count
# every value in the sum.
APP_SEL='namespace="default", container!="istio-proxy", container!="POD", container!="", pod!~"nfs-subdir.*"'
PROXY_SEL='namespace="default", container="istio-proxy", pod!~"nfs-subdir.*"'
ISTIOD_SEL='namespace="istio-system", pod=~"istiod.*", container!="POD", container!=""'
GW_SEL='namespace="istio-system", pod=~"istio-ingressgateway.*", container!="POD", container!=""'
APISERVER_SEL='namespace="kube-system", pod=~"kube-apiserver.*", container!="POD", container!=""'

# Per-pod CPU time series (cores).
cpu_series() { prom_query_range "sum by (pod) (rate(container_cpu_usage_seconds_total{$1}[1m]))"; }

# Per-pod memory working set time series (bytes).
mem_series() { prom_query_range "sum by (pod) (container_memory_working_set_bytes{$1})"; }

# Fleet-wide totals, averaged across the window.
cpu_total() {
    prom_query_instant "sum(avg_over_time(sum by (pod) (rate(container_cpu_usage_seconds_total{$1}[1m]))[${DURATION}s:${STEP}]))"
}
mem_total() {
    prom_query_instant "sum(avg_over_time(sum by (pod) (container_memory_working_set_bytes{$1})[${DURATION}s:${STEP}]))"
}

echo "Collecting metrics from Prometheus (${PROM_URL})"
echo "  Window: $(date -d @"${START_EPOCH}" '+%Y-%m-%d %H:%M:%S') -> $(date -d @"${END_EPOCH}" '+%Y-%m-%d %H:%M:%S') (${DURATION}s, step=${STEP})"
echo "  Output: ${OUT_FILE}"

CPU_APP=$(cpu_series "$APP_SEL")
CPU_PROXY=$(cpu_series "$PROXY_SEL")
CPU_ISTIOD=$(cpu_series "$ISTIOD_SEL")
CPU_GW=$(cpu_series "$GW_SEL")
CPU_APISERVER=$(cpu_series "$APISERVER_SEL")

MEM_APP=$(mem_series "$APP_SEL")
MEM_PROXY=$(mem_series "$PROXY_SEL")
MEM_ISTIOD=$(mem_series "$ISTIOD_SEL")
MEM_GW=$(mem_series "$GW_SEL")
MEM_APISERVER=$(mem_series "$APISERVER_SEL")

CPU_APP_T=$(cpu_total "$APP_SEL")
CPU_PROXY_T=$(cpu_total "$PROXY_SEL")
CPU_ISTIOD_T=$(cpu_total "$ISTIOD_SEL")
CPU_GW_T=$(cpu_total "$GW_SEL")
CPU_APISERVER_T=$(cpu_total "$APISERVER_SEL")

MEM_APP_T=$(mem_total "$APP_SEL")
MEM_PROXY_T=$(mem_total "$PROXY_SEL")
MEM_ISTIOD_T=$(mem_total "$ISTIOD_SEL")
MEM_GW_T=$(mem_total "$GW_SEL")
MEM_APISERVER_T=$(mem_total "$APISERVER_SEL")

# `rps` is quoted: collect_metrics.sh emits it bare, so its documented default
# of "unknown" produces invalid JSON. Consumers parse it with int()/float().
cat > "$OUT_FILE" <<EOF
{
  "metadata": {
    "rps": "${RPS}",
    "duration_seconds": ${DURATION},
    "start_epoch": ${START_EPOCH},
    "end_epoch": ${END_EPOCH},
    "step": "${STEP}",
    "collected_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  },
  "cpu": {
    "app": ${CPU_APP},
    "proxy": ${CPU_PROXY},
    "istiod": ${CPU_ISTIOD},
    "ingressgateway": ${CPU_GW},
    "kube_apiserver": ${CPU_APISERVER}
  },
  "memory": {
    "app": ${MEM_APP},
    "proxy": ${MEM_PROXY},
    "istiod": ${MEM_ISTIOD},
    "ingressgateway": ${MEM_GW},
    "kube_apiserver": ${MEM_APISERVER}
  },
  "totals": {
    "cpu": {
      "app": ${CPU_APP_T},
      "proxy": ${CPU_PROXY_T},
      "istiod": ${CPU_ISTIOD_T},
      "ingressgateway": ${CPU_GW_T},
      "kube_apiserver": ${CPU_APISERVER_T}
    },
    "memory": {
      "app": ${MEM_APP_T},
      "proxy": ${MEM_PROXY_T},
      "istiod": ${MEM_ISTIOD_T},
      "ingressgateway": ${MEM_GW_T},
      "kube_apiserver": ${MEM_APISERVER_T}
    }
  }
}
EOF

# A file that parses but is empty everywhere means the port-forward or the
# scrape config is wrong -- say so now rather than at plotting time.
if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$OUT_FILE" 2>/dev/null; then
    echo "WARNING: ${OUT_FILE} is not valid JSON" >&2
elif ! grep -q '"result":\[{' "$OUT_FILE"; then
    echo "WARNING: every query returned an empty result -- check PROM_URL=${PROM_URL} and the scrape config" >&2
fi

echo "Metrics saved to ${OUT_FILE}"
