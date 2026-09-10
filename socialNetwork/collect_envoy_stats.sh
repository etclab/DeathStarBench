#!/bin/bash
#
# Collects Envoy-, OS- and apiserver-level diagnostics for a benchmark step.
#
# WHY THIS EXISTS
#   collect_metrics.sh only records cgroup CPU/memory from cAdvisor. That is
#   enough to see THAT a sidecar is idle but not WHY, and the replica-scale
#   sweep left us with exactly that hole: at 4x-16x the Mazu istio-proxy sits
#   at 0.05-0.2 cores while wrk2 reports tens of seconds of latency, and
#   nothing in the collected data says where the time goes. These are the
#   counters that distinguish the candidate explanations, all of them readable
#   from outside the proxy -- no Envoy rebuild, no C++ changes.
#
# WHAT EACH MODE ANSWERS
#   snapshot   Envoy admin /stats, /clusters, /server_info per pod, plus the
#              proxy's thread count and cgroup throttle counters. Counters are
#              cumulative, so a snapshot before and after a load step gives the
#              delta for that step. The stats that matter:
#
#                server.watchdog_miss / watchdog_mega_miss
#                    Envoy's own event-loop stall detector: miss fires when a
#                    worker fails to touch its watchdog for 200ms, mega_miss at
#                    1s. Nonzero is direct proof the dispatcher is being
#                    blocked, which is what rbe_validator.cc:254 does when it
#                    calls pthread_join() from onVerificationComplete.
#                listener.*.downstream_pre_cx_active
#                    Connections accepted but not past the transport-socket
#                    handshake. This is the handshake backlog -- if async cert
#                    validation is the queue, it queues here and nowhere else.
#                ssl.handshake / ssl.connection_error / ssl.fail_verify_error
#                    Handshake volume and how many die. The RBE gRPC Check has
#                    a 5s deadline (rbe_validator.cc:207); a timeout surfaces
#                    as fail_verify_error, and each one costs a connection that
#                    the client then has to re-establish.
#                cluster.*.upstream_cx_connect_ms
#                    Histogram of upstream connect+handshake time. Separates
#                    "the handshake is slow" from "the request is slow".
#                cluster.*.upstream_cx_connect_fail / connect_timeout
#                cluster.*.upstream_rq_pending_active / pending_overflow
#                    Queueing in Envoy's own connection pools, so we can rule
#                    Envoy-side queue depth in or out rather than assuming.
#
#              server_info is collected because it reports `concurrency`, the
#              worker-thread count Envoy actually chose. That number bounds how
#              much a single blocking join can cost: at concurrency=1 one
#              blocked worker is the whole proxy.
#
#   sample     Gauges, sampled while load is running. Counters survive to the
#              post-snapshot; gauges do not, and the two most diagnostic
#              signals here are gauges:
#                - the proxy's live thread count, from /proc/<envoy>/status.
#                  doVerifyCertChain spawns one OS thread per handshake
#                  (rbe_validator.cc:164). If that is the cost, thread count
#                  tracks handshake rate instead of sitting flat near
#                  concurrency + Envoy's own handful.
#                - per-task names and context-switch counts, which show whether
#                  the worker (wrk:worker_N) is accumulating voluntary context
#                  switches, i.e. blocking rather than running.
#              Also samples cgroup cpu.stat, because a throttled container
#              reports LOW cpu usage while being frozen -- indistinguishable
#              from "idle" in cAdvisor, and a competing explanation for the
#              same evidence.
#
#   apiserver  Prometheus queries for TokenReview volume and latency and for
#              API Priority & Fairness queueing. The 1-second ext_authz cache
#              means the apiserver only sees traffic on cache misses, so
#              TokenReviews per request is a direct measurement of the cache
#              hit rate -- the thing that would explain why the collapse tracks
#              replica count. The default Prometheus install already scrapes
#              both apiserver endpoints (kubernetes-apiservers job), so this
#              covers the whole control plane, not just whichever instance
#              kubectl happens to reach.
#
# A NOTE ON statsInclusionPrefixes
#   The Bookinfo manifests set
#       sidecar.istio.io/statsInclusionPrefixes: "cluster.outbound,http.inbound,listener"
#   which becomes an Envoy stats_matcher inclusion_list. An inclusion_list is
#   not a display filter: stats outside it are never instantiated, so
#   server.watchdog_miss and the whole ssl.* tree do not exist in /stats and
#   cannot be recovered after the fact. run-2x-envoy-diag.sh widens the
#   annotation for its own generated manifests. If you point this script at a
#   fleet deployed from the stock manifests, expect those rows to be missing
#   and check need_wider_stats in the output before concluding anything.
#
# Usage:
#   ./collect_envoy_stats.sh snapshot  <out_dir> <tag>
#   ./collect_envoy_stats.sh sample    <out_dir> <tag> <duration_s> [interval_s]
#   ./collect_envoy_stats.sh apiserver <out_dir> <tag> <duration_s> <start_epoch>
#
# Environment:
#   APPS          space-separated app labels to scrape (default: the four
#                 Bookinfo apps)
#   SAMPLE_PODS   how many pods per app the sampler touches (default: 2). The
#                 sampler is one kubectl exec per pod per tick, so the full
#                 fleet is too slow to sample at 16x; snapshots still cover
#                 every pod.
#   SAMPLE_INTERVAL  seconds between sampler ticks (default: 5)
#   PROM_URL      Prometheus base URL (default: http://localhost:9091)
#   EXEC_PARALLEL how many kubectl execs to run at once (default: 8)

set -uo pipefail

# No braces in these messages: a '}' inside ${x:?...} closes the expansion
# early, which silently folded the rest of the usage string into $MODE.
MODE="${1:?Usage: $0 snapshot|sample|apiserver <out_dir> <tag> ...}"
OUT_DIR="${2:?missing out_dir}"
TAG="${3:?missing tag}"

APPS="${APPS:-details productpage ratings reviews}"
SAMPLE_PODS="${SAMPLE_PODS:-2}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-5}"
PROM_URL="${PROM_URL:-http://localhost:9091}"
EXEC_PARALLEL="${EXEC_PARALLEL:-8}"

# ---------------------------------------------------------------------------
# Pod discovery
# ---------------------------------------------------------------------------

# All pods for the configured apps that have an istio-proxy container. A pod
# that is not Running is skipped rather than failed on: during a 16x rollout
# some pods legitimately lag, and one of them must not abort the collection.
list_pods() {
    local app
    for app in $APPS; do
        kubectl get pods -l "app=${app}" \
            --field-selector=status.phase=Running \
            -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null
    done
}

# First $SAMPLE_PODS pods of each app, for the in-flight sampler.
list_sample_pods() {
    local app
    for app in $APPS; do
        kubectl get pods -l "app=${app}" \
            --field-selector=status.phase=Running \
            -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
            | head -n "$SAMPLE_PODS"
    done
}

# ---------------------------------------------------------------------------
# Talking to the proxy
# ---------------------------------------------------------------------------
#
# Two ways in, tried in this order:
#
#   pilot-agent request GET <path>
#       Always present in the istio-proxy container -- it is the same path
#       istioctl uses -- and needs no shell and no curl. This is the reliable
#       one and the only one used for admin endpoints.
#
#   sh -c '<snippet>'
#       Needed for /proc and cgroup reads, which are not exposed over the admin
#       API at all. Present on the Ubuntu-based proxyv2 images; absent on the
#       distroless variant. Probed once and reported, so a missing shell shows
#       up as an explicit note in the output instead of silently empty files.

admin_get() {
    local pod="$1" path="$2"
    kubectl exec "$pod" -c istio-proxy -- pilot-agent request GET "$path" 2>/dev/null
}

proxy_sh() {
    local pod="$1" snippet="$2"
    kubectl exec "$pod" -c istio-proxy -- sh -c "$snippet" 2>/dev/null
}

# Thread count, per-task names and context switches, and cgroup CPU accounting,
# in one exec. Emitted as flat key=value lines so the sampler output stays
# greppable without a parser.
#
# Envoy is not pid 1 in the container (pilot-agent is), so it is located by
# comm rather than assumed. cpu.stat is read from both the cgroup v2 and v1
# paths because the node layout is not guaranteed either way; whichever exists
# answers, the other is silent.
PROC_SNIPPET='
pid=""
for p in /proc/[0-9]*; do
  [ -r "$p/comm" ] || continue
  if [ "$(cat "$p/comm" 2>/dev/null)" = "envoy" ]; then pid="${p#/proc/}"; break; fi
done
if [ -z "$pid" ]; then echo "envoy_pid=none"; else
  echo "envoy_pid=$pid"
  awk "/^Threads:/{print \"threads=\" \$2}" /proc/$pid/status
  awk "/^VmRSS:/{print \"vmrss_kb=\" \$2}" /proc/$pid/status
  vol=0; nonvol=0; nworker=0; nunnamed=0
  for t in /proc/$pid/task/*; do
    [ -r "$t/status" ] || continue
    v=$(awk "/^voluntary_ctxt_switches:/{print \$2}" "$t/status" 2>/dev/null)
    n=$(awk "/^nonvoluntary_ctxt_switches:/{print \$2}" "$t/status" 2>/dev/null)
    vol=$((vol + ${v:-0})); nonvol=$((nonvol + ${n:-0}))
    c=$(cat "$t/comm" 2>/dev/null)
    case "$c" in
      wrk:worker*) nworker=$((nworker + 1)) ;;
      envoy)       nunnamed=$((nunnamed + 1)) ;;
    esac
  done
  echo "vol_ctxt=$vol"
  echo "nonvol_ctxt=$nonvol"
  echo "tasks_named_worker=$nworker"
  echo "tasks_comm_envoy=$nunnamed"
fi
if [ -r /sys/fs/cgroup/cpu.stat ]; then
  sed "s/^/cg_/" /sys/fs/cgroup/cpu.stat | tr " " "="
elif [ -r /sys/fs/cgroup/cpu,cpuacct/cpu.stat ]; then
  sed "s/^/cg_/" /sys/fs/cgroup/cpu,cpuacct/cpu.stat | tr " " "="
fi
'

# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

do_snapshot() {
    local dir="${OUT_DIR}/envoy/${TAG}"
    mkdir -p "$dir"

    local pods
    pods=$(list_pods)
    if [ -z "$pods" ]; then
        echo "collect_envoy_stats: no Running pods for apps [${APPS}], nothing to snapshot"
        return 0
    fi

    echo "collect_envoy_stats: snapshot '${TAG}' -> ${dir} ($(echo "$pods" | wc -l) pods)"

    # One background job per pod, bounded by EXEC_PARALLEL. Serial collection
    # of 96 pods x 3 endpoints takes minutes, which would smear the snapshot
    # across a window long enough for the counters to move.
    local running=0 pod
    for pod in $pods; do
        (
            # NOT ?usedonly. That flag omits any counter still at zero, which
            # is exactly the case we have to be able to read: a stat absent
            # from the dump is then indistinguishable from a stat the proxy
            # never instantiated, and "watchdog_miss is zero" and
            # "watchdog_miss does not exist" are opposite conclusions about H1.
            # The full dump is ~4.7k lines against ~950, which is cheap enough.
            admin_get "$pod" 'stats'           > "${dir}/${pod}.stats"       || true
            admin_get "$pod" 'clusters?format=json' > "${dir}/${pod}.clusters.json" || true
            admin_get "$pod" 'server_info'     > "${dir}/${pod}.server_info" || true
            proxy_sh  "$pod" "$PROC_SNIPPET"   > "${dir}/${pod}.proc"        || true
        ) &
        running=$((running + 1))
        if [ "$running" -ge "$EXEC_PARALLEL" ]; then wait -n 2>/dev/null || wait; running=$((running - 1)); fi
    done
    wait

    # Record whether the fleet was deployed with a stats_matcher narrow enough
    # to have dropped the counters this whole exercise depends on. Checked
    # here, at collection time, because it is unrecoverable afterwards.
    local probe
    probe=$(cat "${dir}"/*.stats 2>/dev/null | grep -c '^server\.\|^ssl\.' || true)
    {
        echo "tag=${TAG}"
        echo "collected_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "pods=$(echo "$pods" | wc -l)"
        echo "server_and_ssl_stat_lines=${probe}"
        if [ "${probe:-0}" -eq 0 ]; then
            echo "need_wider_stats=1  # server.* and ssl.* absent: statsInclusionPrefixes excluded them"
        else
            echo "need_wider_stats=0"
        fi
        # Whether the /proc reads landed at all. A distroless proxyv2 image has
        # no shell, so PROC_SNIPPET produces nothing and the thread-count
        # evidence for H2 is simply unavailable -- worth saying once, here,
        # rather than leaving the analysis to infer it from empty columns.
        if grep -qs '^threads=' "${dir}"/*.proc; then
            echo "proc_readable=1"
        else
            echo "proc_readable=0  # no shell in istio-proxy: thread counts unavailable"
        fi
    } > "${dir}/_meta"

    cat "${dir}/_meta"
}

# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------

do_sample() {
    local duration="${4:?sample mode needs <duration_s>}"
    local interval="${5:-$SAMPLE_INTERVAL}"
    local dir="${OUT_DIR}/envoy/samples-${TAG}"
    mkdir -p "$dir"

    local pods
    pods=$(list_sample_pods)
    if [ -z "$pods" ]; then
        echo "collect_envoy_stats: no Running pods to sample"
        return 0
    fi

    echo "collect_envoy_stats: sampling $(echo "$pods" | wc -l) pods every ${interval}s for ${duration}s -> ${dir}"

    # Gauges only, and only the ones that cannot be reconstructed from the
    # before/after counter delta. Kept to a single filtered /stats call plus
    # one /proc read per pod per tick so a tick finishes well inside the
    # interval even at EXEC_PARALLEL pods wide.
    local gauge_filter='(downstream_pre_cx_active|downstream_cx_active|upstream_cx_active|upstream_rq_pending_active|upstream_rq_active|server\.(watchdog|total_connections|concurrency))'

    local deadline=$(( $(date +%s) + duration ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        local now
        now=$(date +%s)
        local running=0 pod
        for pod in $pods; do
            (
                {
                    echo "# t=${now}"
                    proxy_sh "$pod" "$PROC_SNIPPET"
                    admin_get "$pod" "stats?filter=${gauge_filter}"
                } >> "${dir}/${pod}.samples"
            ) &
            running=$((running + 1))
            if [ "$running" -ge "$EXEC_PARALLEL" ]; then wait -n 2>/dev/null || wait; running=$((running - 1)); fi
        done
        wait
        # Sleep only the remainder of the interval: a tick over a wide fleet
        # can itself take seconds, and adding a full interval on top would
        # silently stretch the sampling period past the load step.
        local spent=$(( $(date +%s) - now ))
        local rest=$(( interval - spent ))
        [ "$rest" -gt 0 ] && sleep "$rest"
    done

    echo "collect_envoy_stats: sampling '${TAG}' done"
}

# ---------------------------------------------------------------------------
# apiserver
# ---------------------------------------------------------------------------

do_apiserver() {
    local duration="${4:?apiserver mode needs <duration_s>}"
    local start_epoch="${5:?apiserver mode needs <start_epoch>}"
    local end_epoch=$(( start_epoch + duration ))
    local dir="${OUT_DIR}/envoy"
    mkdir -p "$dir"
    local out="${dir}/apiserver_${TAG}.json"

    q() {
        curl -s --fail -G "${PROM_URL}/api/v1/query" \
            --data-urlencode "query=$1" \
            --data-urlencode "time=${end_epoch}" 2>/dev/null || echo '{"status":"error"}'
    }

    # increase() over the load window rather than a rate: we want the absolute
    # number of TokenReviews the step cost, to divide by the requests wrk2
    # actually completed. Summed across instances so both apiservers count.
    local tr_total tr_p99 inflight apf_queue apf_wait apf_reject etcd_p99
    tr_total=$(q "sum(increase(apiserver_request_total{resource=\"tokenreviews\"}[${duration}s]))")
    tr_p99=$(q "histogram_quantile(0.99, sum by (le) (rate(apiserver_request_duration_seconds_bucket{resource=\"tokenreviews\"}[${duration}s])))")
    inflight=$(q "max_over_time(sum(apiserver_current_inflight_requests)[${duration}s:15s])")
    apf_queue=$(q "max_over_time(sum(apiserver_flowcontrol_current_inqueue_requests)[${duration}s:15s])")
    apf_wait=$(q "histogram_quantile(0.99, sum by (le) (rate(apiserver_flowcontrol_request_wait_duration_seconds_bucket[${duration}s])))")
    apf_reject=$(q "sum(increase(apiserver_flowcontrol_rejected_requests_total[${duration}s]))")
    etcd_p99=$(q "histogram_quantile(0.99, sum by (le) (rate(apiserver_request_duration_seconds_bucket{verb=\"POST\"}[${duration}s])))")

    cat > "$out" <<EOF
{
  "metadata": {
    "tag": "${TAG}",
    "duration_seconds": ${duration},
    "start_epoch": ${start_epoch},
    "end_epoch": ${end_epoch},
    "collected_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  },
  "tokenreview_count": ${tr_total},
  "tokenreview_p99_seconds": ${tr_p99},
  "apiserver_inflight_max": ${inflight},
  "apf_inqueue_max": ${apf_queue},
  "apf_wait_p99_seconds": ${apf_wait},
  "apf_rejected": ${apf_reject},
  "apiserver_post_p99_seconds": ${etcd_p99}
}
EOF
    echo "collect_envoy_stats: apiserver metrics -> ${out}"
}

# ---------------------------------------------------------------------------

case "$MODE" in
    snapshot)  do_snapshot ;;
    sample)    do_sample "$@" ;;
    apiserver) do_apiserver "$@" ;;
    *) echo "unknown mode '${MODE}' (want snapshot|sample|apiserver)" >&2; exit 2 ;;
esac
