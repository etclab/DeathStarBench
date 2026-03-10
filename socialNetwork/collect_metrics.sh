#!/bin/bash
#
# Collects CPU and memory metrics from Prometheus for a benchmark run.
#
# Usage:
#   ./collect_metrics.sh <output_dir> <duration_seconds> <start_epoch>
#
# Queries Prometheus for:
#   - container_cpu_usage_seconds_total (rate over the run)
#   - container_memory_working_set_bytes (avg over the run)
#
# Targets: bookinfo pods (istio-proxy sidecar per pod), istiod, kube-apiserver
#
# Output: <output_dir>/metrics_<rps>.json  (one file per invocation)

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

OUT_DIR="${1:?Usage: $0 <output_dir> <duration_seconds> <start_epoch> [rps]}"
DURATION="${2:?Usage: $0 <output_dir> <duration_seconds> <start_epoch> [rps]}"
START_EPOCH="${3:?Usage: $0 <output_dir> <duration_seconds> <start_epoch> [rps]}"
RPS="${4:-unknown}"

END_EPOCH=$((START_EPOCH + DURATION))

# Prometheus endpoint (port-forward or ClusterIP)
PROM_URL="${PROM_URL:-http://localhost:9091}"

mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/metrics_${RPS}.json"

# Helper: query Prometheus range API and return the JSON result
prom_query() {
    local query="$1"
    curl -s --fail -G "${PROM_URL}/api/v1/query_range" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${START_EPOCH}" \
        --data-urlencode "end=${END_EPOCH}" \
        --data-urlencode "step=15s"
}

# Helper: query Prometheus instant API at end time
prom_query_instant() {
    local query="$1"
    curl -s --fail -G "${PROM_URL}/api/v1/query" \
        --data-urlencode "query=${query}" \
        --data-urlencode "time=${END_EPOCH}"
}

echo "Collecting metrics from Prometheus (${PROM_URL})"
echo "  Window: $(date -d @${START_EPOCH} '+%Y-%m-%d %H:%M:%S') -> $(date -d @${END_EPOCH} '+%Y-%m-%d %H:%M:%S') (${DURATION}s)"
echo "  Output: ${OUT_FILE}"

# --- CPU: avg rate over the benchmark window (instant query with subquery) ---
# Bookinfo pods – istio-proxy sidecar only, excluding nfs-subdir provisioner
CPU_BOOKINFO=$(prom_query_instant "avg_over_time(rate(container_cpu_usage_seconds_total{namespace=\"default\", container=\"istio-proxy\", pod!~\"nfs-subdir.*\"}[1m])[${DURATION}s:15s])")

# istiod (istio-system namespace)
CPU_ISTIOD=$(prom_query_instant "avg_over_time(rate(container_cpu_usage_seconds_total{namespace=\"istio-system\", pod=~\"istiod.*\", container!=\"POD\", container!=\"\"}[1m])[${DURATION}s:15s])")

# kube-apiserver
CPU_APISERVER=$(prom_query_instant "avg_over_time(rate(container_cpu_usage_seconds_total{namespace=\"kube-system\", pod=~\"kube-apiserver.*\", container!=\"POD\", container!=\"\"}[1m])[${DURATION}s:15s])")

# --- Memory: avg over the benchmark window ---
MEM_BOOKINFO=$(prom_query_instant "avg_over_time(container_memory_working_set_bytes{namespace=\"default\", container=\"istio-proxy\", pod!~\"nfs-subdir.*\"}[${DURATION}s])")

MEM_ISTIOD=$(prom_query_instant "avg_over_time(container_memory_working_set_bytes{namespace=\"istio-system\", pod=~\"istiod.*\", container!=\"POD\", container!=\"\"}[${DURATION}s])")

MEM_APISERVER=$(prom_query_instant "avg_over_time(container_memory_working_set_bytes{namespace=\"kube-system\", pod=~\"kube-apiserver.*\", container!=\"POD\", container!=\"\"}[${DURATION}s])")

# --- Assemble JSON output ---
cat > "$OUT_FILE" <<EOF
{
  "metadata": {
    "rps": ${RPS},
    "duration_seconds": ${DURATION},
    "start_epoch": ${START_EPOCH},
    "end_epoch": ${END_EPOCH},
    "collected_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  },
  "cpu": {
    "bookinfo": ${CPU_BOOKINFO},
    "istiod": ${CPU_ISTIOD},
    "kube_apiserver": ${CPU_APISERVER}
  },
  "memory": {
    "bookinfo": ${MEM_BOOKINFO},
    "istiod": ${MEM_ISTIOD},
    "kube_apiserver": ${MEM_APISERVER}
  }
}
EOF

echo "Metrics saved to ${OUT_FILE}"
