#!/bin/bash
#
# One-shot scrape of envoy admin request counters from every bookinfo sidecar.
#
# Why: Mazu's custom istio-proxy image may not emit istio_requests_total via
# Prometheus, but base Envoy always exposes its own counters on the admin port
# (15000). Those counters work regardless of the stats filter chain.
#
# Usage: ./scrape-envoy-requests.sh <csv_path>
# Appends rows: timestamp,pod,metric_line   (metric_line is CSV-quoted)
# Filters to envoy_{cluster_upstream,http_downstream}_rq_completed.

set -uo pipefail

CSV="${1:?Usage: $0 <csv_path>}"
TS=$(date +%s)

# Refresh pod list each call so we capture pods that scaled in/out
pods=$(kubectl get pods -l 'app in (productpage,details,reviews,ratings)' \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)

[ -z "$pods" ] && exit 0

# Scrape pods in parallel. xargs -P caps concurrency so we don't flood apiserver.
echo "$pods" | xargs -P 16 -I {} bash -c '
    pod="$1"; ts="$2"; csv="$3";
    # Envoy admin is localhost-only on 15000. ?usedonly skips zero-valued metrics.
    out=$(kubectl exec "$pod" -c istio-proxy -- \
        curl -s --max-time 2 "http://localhost:15000/stats?format=prometheus&usedonly" 2>/dev/null)
    [ -z "$out" ] && exit 0
    echo "$out" \
      | grep -E "^envoy_(cluster_upstream_rq_completed|http_downstream_rq_completed)" \
      | awk -v ts="$ts" -v pod="$pod" "{
            gsub(/\"/, \"\\\"\\\"\");
            printf \"%s,%s,\\\"%s\\\"\\n\", ts, pod, \$0;
        }" >> "$csv"
' _ {} "$TS" "$CSV"
