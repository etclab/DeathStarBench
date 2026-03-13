#!/bin/bash
#
# Collects inline benchmark histograms from Prometheus for Mazu per-operation
# cost measurement.
#
# Usage:
#   ./collect_inline_metrics.sh <output_dir> <duration_seconds> <start_epoch> [rps]
#
# Queries Prometheus for:
#   - mazu_benchmark_op_latency_ms (per-operation latencies)
#   - mazu_benchmark_total_latency_ms (total ext_authz path latency)
#
# Output: <output_dir>/inline_metrics_<rps>.json

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
OUT_FILE="$OUT_DIR/inline_metrics_${RPS}.json"

# Helper: query Prometheus instant API at end time
prom_query_instant() {
    local query="$1"
    curl -s --fail -G "${PROM_URL}/api/v1/query" \
        --data-urlencode "query=${query}" \
        --data-urlencode "time=${END_EPOCH}"
}

echo "Collecting inline benchmark metrics from Prometheus (${PROM_URL})"
echo "  Window: $(date -d @${START_EPOCH} '+%Y-%m-%d %H:%M:%S') -> $(date -d @${END_EPOCH} '+%Y-%m-%d %H:%M:%S') (${DURATION}s)"
echo "  Output: ${OUT_FILE}"

# Known benchmark_op label values
OPS=("kc_fetch" "counter_attestation" "rbe_proof" "challenge_response" "token_review")
QUANTILES=("0.5" "0.9" "0.95" "0.99")
QUANTILE_NAMES=("p50" "p90" "p95" "p99")

# --- Per-operation latencies ---
# Build JSON for each operation at each quantile
OP_JSON="{"
first_op=true
for op in "${OPS[@]}"; do
    if [ "$first_op" = true ]; then
        first_op=false
    else
        OP_JSON+=","
    fi
    OP_JSON+="\"${op}\": {"
    first_q=true
    for i in "${!QUANTILES[@]}"; do
        q="${QUANTILES[$i]}"
        qname="${QUANTILE_NAMES[$i]}"
        result=$(prom_query_instant "histogram_quantile(${q}, rate(mazu_benchmark_op_latency_ms_bucket{benchmark_op=\"${op}\"}[${DURATION}s]))")
        if [ "$first_q" = true ]; then
            first_q=false
        else
            OP_JSON+=","
        fi
        OP_JSON+="\"${qname}\": ${result}"
    done
    OP_JSON+="}"
done
OP_JSON+="}"

# --- Total ext_authz path latency ---
TOTAL_JSON="{"
first_q=true
for i in "${!QUANTILES[@]}"; do
    q="${QUANTILES[$i]}"
    qname="${QUANTILE_NAMES[$i]}"
    result=$(prom_query_instant "histogram_quantile(${q}, rate(mazu_benchmark_total_latency_ms_bucket[${DURATION}s]))")
    if [ "$first_q" = true ]; then
        first_q=false
    else
        TOTAL_JSON+=","
    fi
    TOTAL_JSON+="\"${qname}\": ${result}"
done
TOTAL_JSON+="}"

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
  "op_latency": ${OP_JSON},
  "total_latency": ${TOTAL_JSON}
}
EOF

echo "Inline metrics saved to ${OUT_FILE}"
