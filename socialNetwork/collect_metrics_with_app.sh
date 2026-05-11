#!/bin/bash
#
# Collects CPU and memory metrics from Prometheus for a benchmark run, capturing
# BOTH the application container and the istio-proxy sidecar of pods in the
# default namespace.
#
# Uses query_range so the output preserves the full time series across the
# benchmark window (step matches Prometheus scrape_interval = 15s; finer steps
# would just duplicate samples).
#
# Usage:
#   ./collect_metrics_with_app.sh <output_dir> <duration_seconds> <start_epoch> [rps]
#
# Output: <output_dir>/metrics_<rps>.json with component groups:
#   app             - application containers in default ns (everything that is
#                     not istio-proxy / POD)
#   proxy           - istio-proxy sidecars in default ns
#   istiod          - istiod in istio-system ns
#   kube_apiserver  - kube-apiserver in kube-system ns
#
# Each component value is a Prometheus query_range matrix response with one
# series per pod. Downstream tooling can either average the series for bar
# charts or plot the series directly for line charts.

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

prom_query_range() {
    local query="$1"
    curl -s --fail -G "${PROM_URL}/api/v1/query_range" \
        --data-urlencode "query=${query}" \
        --data-urlencode "start=${START_EPOCH}" \
        --data-urlencode "end=${END_EPOCH}" \
        --data-urlencode "step=${STEP}"
}

echo "Collecting metrics from Prometheus (${PROM_URL})"
echo "  Window: $(date -d @${START_EPOCH} '+%Y-%m-%d %H:%M:%S') -> $(date -d @${END_EPOCH} '+%Y-%m-%d %H:%M:%S') (${DURATION}s, step=${STEP})"
echo "  Output: ${OUT_FILE}"

# --- CPU: per-pod rate time series over the benchmark window ---
CPU_APP=$(prom_query_range "rate(container_cpu_usage_seconds_total{namespace=\"default\", container!=\"istio-proxy\", container!=\"POD\", container!=\"\", pod!~\"nfs-subdir.*\"}[1m])")

CPU_PROXY=$(prom_query_range "rate(container_cpu_usage_seconds_total{namespace=\"default\", container=\"istio-proxy\", pod!~\"nfs-subdir.*\"}[1m])")

CPU_ISTIOD=$(prom_query_range "rate(container_cpu_usage_seconds_total{namespace=\"istio-system\", pod=~\"istiod.*\", container!=\"POD\", container!=\"\"}[1m])")

CPU_APISERVER=$(prom_query_range "rate(container_cpu_usage_seconds_total{namespace=\"kube-system\", pod=~\"kube-apiserver.*\", container!=\"POD\", container!=\"\"}[1m])")

# --- Memory: per-pod working-set gauge over the benchmark window ---
MEM_APP=$(prom_query_range "container_memory_working_set_bytes{namespace=\"default\", container!=\"istio-proxy\", container!=\"POD\", container!=\"\", pod!~\"nfs-subdir.*\"}")

MEM_PROXY=$(prom_query_range "container_memory_working_set_bytes{namespace=\"default\", container=\"istio-proxy\", pod!~\"nfs-subdir.*\"}")

MEM_ISTIOD=$(prom_query_range "container_memory_working_set_bytes{namespace=\"istio-system\", pod=~\"istiod.*\", container!=\"POD\", container!=\"\"}")

MEM_APISERVER=$(prom_query_range "container_memory_working_set_bytes{namespace=\"kube-system\", pod=~\"kube-apiserver.*\", container!=\"POD\", container!=\"\"}")

cat > "$OUT_FILE" <<EOF
{
  "metadata": {
    "rps": ${RPS},
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
    "kube_apiserver": ${CPU_APISERVER}
  },
  "memory": {
    "app": ${MEM_APP},
    "proxy": ${MEM_PROXY},
    "istiod": ${MEM_ISTIOD},
    "kube_apiserver": ${MEM_APISERVER}
  }
}
EOF

echo "Metrics saved to ${OUT_FILE}"
