#!/bin/bash
#
# SocialNetwork (DeathStarBench) on Istio -- progressive datastore scaling.
#
# Companion to run-socialnetwork-istio.sh. That script answers "does the
# chart run, and how fast". This one answers "does scaling the datastores
# move the ~950 RPS ceiling", by walking a list of topology configurations
# and benchmarking each one identically.
#
# Usage:
#   ./run-socialnetwork-scale.sh                        # run the default ladder
#   ./run-socialnetwork-scale.sh baseline mc-3x3        # run named configs only
#   CONFIGS="redis-6 redis-12" ./run-socialnetwork-scale.sh
#   ./run-socialnetwork-scale.sh --list                 # show known configs
#
# Environment overrides:
#   RESULTS_DIR   - output root (default: results/sn-scale-<date>)
#   RPS_VALUES    - wrk2 target rates       (default: "400 600 800 1000 1200")
#   DURATION      - seconds per wrk2 step   (default: 60)
#   THREADS/CONNS - wrk2 -t / -c            (default: 16 / 128)
#   WORKLOAD      - lua script              (default: mixed-workload.lua)
#   GRAPH         - seed dataset            (default: socfb-Reed98)
#   KEEP_RELEASE  - 1 to leave the last config installed when done
#   SKIP_BENCH    - 1 to install+seed+verify only, no wrk2 (for debugging)
#
# ---------------------------------------------------------------------------
# THINGS THAT WILL BITE YOU -- all learned the hard way, do not "clean up"
# without reading:
#
#  1. EVERY bitnami/* image tag these subcharts reference is 404 on Docker
#     Hub since Bitnami's 2025 catalogue purge. scratch/yaml/sn-images-legacy.yaml
#     repoints them at bitnamilegacy/*. It is applied to every non-baseline
#     config and is NOT optional -- without it you get ImagePullBackOff.
#
#  2. HOOK PODS ARE NOT GARBAGE COLLECTED. The chart's post-install hooks
#     (setup-mcrouter-configmap, setup-collection-sharding-hook,
#     redis-cluster-readiness-hook) carry no helm.sh/hook-delete-policy, so
#     they survive `helm uninstall`. Reinstalling then fails with "pod
#     already exists". teardown() deletes them explicitly.
#
#  3. THE READINESS WAIT MUST NOT FILTER ON THE `service` LABEL. The DSB
#     pods carry it; the bitnami statefulsets (mongodb-sharded, redis-cluster,
#     memcached, mcrouter) do not. run-socialnetwork-istio.sh waits on
#     -l service, which in these topologies returns Ready while the
#     datastores are still forming. This script waits on everything.
#
#  4. SEED AFTER INSTALL, ALWAYS. Persistence is deliberately disabled in
#     every arm (see the values files for why), so any roll wipes the data.
#
#  5. REDIS CLUSTER CORRUPTS THE TIMELINES. Home-timeline and user-timeline
#     both key on a bare user id and share one cluster, so the two merge.
#     check_timelines() measures the overlap and records it rather than
#     letting a silently-wrong run look clean. See sn-redis-cluster.yaml.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YAML="${SCRIPT_DIR}/scratch/yaml"
CHART_DIR="${SCRIPT_DIR}/helm-chart/socialnetwork"
RELEASE="${RELEASE:-social-network}"
NAMESPACE="${NAMESPACE:-default}"

BENCH_VALUES="${YAML}/socialnetwork-bench-values.yaml"
IMAGE_VALUES="${YAML}/sn-images-legacy.yaml"

RESULTS_ROOT="${RESULTS_DIR:-${SCRIPT_DIR}/results/sn-scale-$(date +%m-%d-%y_%H%M%S)}"
RPS_VALUES=(${RPS_VALUES:-400 600 800 1000 1200})
DURATION=${DURATION:-60}
THREADS=${THREADS:-16}
CONNS=${CONNS:-128}
WORKLOAD="${WORKLOAD:-mixed-workload.lua}"
GRAPH="${GRAPH:-socfb-Reed98}"

# ---------------------------------------------------------------------------
# The configuration ladder.
#
# Format:  <name>|<description>|<extra -f files>|<extra --set overrides>
#
# Each rung changes ONE axis relative to the rung above it, so that a change
# in throughput can be attributed. Read top to bottom.
# ---------------------------------------------------------------------------
declare -a LADDER=(
  # Control. Single-instance datastores, scaled stateless tier. This is the
  # configuration that produced the ~950 RPS ceiling.
  "baseline|standalone datastores (control)||"

  # --- memcached axis -----------------------------------------------------
  # mcrouter routers x memcached backends. Router count is varied separately
  # from backend count because the chart defaults to ONE router pod fronting
  # every cache operation in the application.
  "mc-1x3|mcrouter x1, memcached x3|sn-memcached-cluster|mcrouter.statefulset.replicas=1,mcrouter.memcached.replicaCount=3"
  "mc-3x3|mcrouter x3, memcached x3|sn-memcached-cluster|mcrouter.statefulset.replicas=3,mcrouter.memcached.replicaCount=3"
  "mc-3x6|mcrouter x3, memcached x6|sn-memcached-cluster|mcrouter.statefulset.replicas=3,mcrouter.memcached.replicaCount=6"
  "mc-6x6|mcrouter x6, memcached x6|sn-memcached-cluster|mcrouter.statefulset.replicas=6,mcrouter.memcached.replicaCount=6"

  # --- mongodb axis -------------------------------------------------------
  # mongos routers x shards. Same reasoning: mongos defaults to 1.
  "mongo-1x3|mongos x1, 3 shards x2|sn-mongodb-sharded|mongodb-sharded.mongos.replicaCount=1,mongodb-sharded.shards=3,mongodb-sharded.shardsvr.dataNode.replicaCount=2"
  "mongo-3x3|mongos x3, 3 shards x2|sn-mongodb-sharded|mongodb-sharded.mongos.replicaCount=3,mongodb-sharded.shards=3,mongodb-sharded.shardsvr.dataNode.replicaCount=2"
  "mongo-3x6|mongos x3, 6 shards x2|sn-mongodb-sharded|mongodb-sharded.mongos.replicaCount=3,mongodb-sharded.shards=6,mongodb-sharded.shardsvr.dataNode.replicaCount=2"

  # --- redis axis ---------------------------------------------------------
  # nodes = masters + masters*replicas. SEMANTICALLY BROKEN, see note 5.
  "redis-6|redis cluster, 3 masters + 3 replicas|sn-redis-cluster|redis-cluster.cluster.nodes=6,redis-cluster.cluster.replicas=1"
  "redis-12|redis cluster, 6 masters + 6 replicas|sn-redis-cluster|redis-cluster.cluster.nodes=12,redis-cluster.cluster.replicas=1"

  # --- everything at once -------------------------------------------------
  "all|memcached + mongo + redis, all clustered|sn-memcached-cluster sn-mongodb-sharded sn-redis-cluster|mcrouter.statefulset.replicas=3,mcrouter.memcached.replicaCount=6,mongodb-sharded.mongos.replicaCount=3,mongodb-sharded.shards=3,mongodb-sharded.shardsvr.dataNode.replicaCount=2,redis-cluster.cluster.nodes=6,redis-cluster.cluster.replicas=1"

  # Same as "all" but without redis, i.e. the strongest configuration whose
  # application semantics are still CORRECT.
  "all-correct|memcached + mongo clustered, redis standalone|sn-memcached-cluster sn-mongodb-sharded|mcrouter.statefulset.replicas=3,mcrouter.memcached.replicaCount=6,mongodb-sharded.mongos.replicaCount=3,mongodb-sharded.shards=3,mongodb-sharded.shardsvr.dataNode.replicaCount=2"
)

mazu_echo() { echo -e "\n\033[1;36m[$(date +%T)] $*\033[0m"; }
warn()      { echo -e "\033[1;33mWARNING: $*\033[0m"; }

list_configs() {
    printf '%-14s %s\n' "CONFIG" "DESCRIPTION"
    for entry in "${LADDER[@]}"; do
        IFS='|' read -r name desc _ _ <<< "$entry"
        printf '%-14s %s\n' "$name" "$desc"
    done
}

if [[ "${1:-}" == "--list" ]]; then list_configs; exit 0; fi

# Which configs to run: argv, else $CONFIGS, else the whole ladder.
if [[ $# -gt 0 ]]; then
    SELECTED=("$@")
elif [[ -n "${CONFIGS:-}" ]]; then
    SELECTED=(${CONFIGS})
else
    SELECTED=()
    for entry in "${LADDER[@]}"; do
        IFS='|' read -r name _ _ _ <<< "$entry"
        SELECTED+=("$name")
    done
fi

# Validate up front: a typo on config 7 of 12 should not surface an hour in.
for want in "${SELECTED[@]}"; do
    found=0
    for entry in "${LADDER[@]}"; do
        IFS='|' read -r name _ _ _ <<< "$entry"
        [[ "$name" == "$want" ]] && found=1 && break
    done
    if [[ $found -eq 0 ]]; then
        echo "ERROR: unknown config '$want'"; echo; list_configs; exit 1
    fi
done

mkdir -p "$RESULTS_ROOT"
exec > >(tee -a "$RESULTS_ROOT/run.log") 2>&1

SUMMARY="$RESULTS_ROOT/summary.csv"
echo "config,rps_target,rps_delivered,p50_ms,p99_ms,non_2xx,timeline_overlap_pct,pods" > "$SUMMARY"

mazu_echo "=== SocialNetwork progressive datastore scaling ==="
echo "Results:  $RESULTS_ROOT"
echo "Configs:  ${SELECTED[*]}"
echo "Workload: $WORKLOAD @ ${RPS_VALUES[*]} RPS x ${DURATION}s (-t $THREADS -c $CONNS)"

# --- Preflight -------------------------------------------------------------
WRK_BIN="${SCRIPT_DIR}/../wrk2/wrk"
if [[ ! -x "$WRK_BIN" ]]; then
    mazu_echo "wrk2 not built, building..."
    "$SCRIPT_DIR/setup_social_network.sh" build-wrk2
fi
LUA="${SCRIPT_DIR}/wrk2/scripts/social-network/${WORKLOAD}"
[[ -f "$LUA" ]] || { echo "ERROR: workload not found: $LUA"; exit 1; }

for f in "$BENCH_VALUES" "$IMAGE_VALUES"; do
    [[ -f "$f" ]] || { echo "ERROR: missing values file: $f"; exit 1; }
done

if ! kubectl get ns istio-system >/dev/null 2>&1; then
    echo "ERROR: Istio is not installed. Run ./run-socialnetwork-istio.sh first --"
    echo "       this script reuses the existing Istio install and only cycles"
    echo "       the social-network release."
    exit 1
fi

# RBAC for the mcrouter hook (no-op if already present).
kubectl apply -f "${YAML}/mcrouter-role.yaml" >/dev/null

get_ingress() {
    INGRESS_IP=$(kubectl -n istio-system get svc istio-ingressgateway \
        -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    INGRESS_PORT=$(kubectl -n istio-system get svc istio-ingressgateway \
        -o jsonpath='{.spec.ports[?(@.name=="http2")].port}' 2>/dev/null || true)
    [[ -z "$INGRESS_PORT" ]] && INGRESS_PORT=80
}

# ---------------------------------------------------------------------------
teardown() {
    mazu_echo "Tearing down previous release..."
    helm uninstall "$RELEASE" -n "$NAMESPACE" --wait --timeout 10m0s 2>/dev/null || true

    # See note 2: hook pods outlive the release and block the next install.
    kubectl delete pod -n "$NAMESPACE" --ignore-not-found --wait=false \
        setup-mcrouter-configmap setup-collection-sharding-hook \
        redis-cluster-readiness-hook 2>/dev/null || true

    # StatefulSet PVCs are never removed by helm. Persistence is disabled in
    # all arms so there should be none, but a stale PVC from an experiment
    # with persistence on would silently pin old data into the next run.
    for sel in mongodb-sharded redis-cluster memcached; do
        kubectl delete pvc -n "$NAMESPACE" -l "app.kubernetes.io/name=$sel" \
            --ignore-not-found --wait=false 2>/dev/null || true
    done

    # Wait for the pods to actually go away, otherwise the next install races
    # against terminating pods for node capacity.
    local deadline=$(( $(date +%s) + 600 ))
    while :; do
        local left
        left=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -Ev 'nfs-subdir|^NAME' | wc -l)
        [[ "$left" -eq 0 ]] && break
        [[ "$(date +%s)" -ge "$deadline" ]] && { warn "$left pods still terminating, continuing anyway"; break; }
        sleep 5
    done
}

# ---------------------------------------------------------------------------
wait_ready() {
    # Note 3: no label filter. Everything in the namespace must be Ready.
    local deadline=$(( $(date +%s) + ${1:-1200} ))
    mazu_echo "Waiting for all pods to become Ready..."
    while :; do
        local total not_ready
        total=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
                | grep -Ev 'nfs-subdir' | wc -l)
        # A pod is ready when its ready-count equals its container-count and
        # it is Running. Completed hook pods are excluded, not counted as bad.
        not_ready=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
                | grep -Ev 'nfs-subdir|Completed' \
                | awk '{split($2,a,"/"); if (a[1]!=a[2] || $3!="Running") c++} END{print c+0}')
        if [[ "$total" -gt 0 && "$not_ready" -eq 0 ]]; then
            echo "  all $total pods Ready"
            return 0
        fi
        if [[ "$(date +%s)" -ge "$deadline" ]]; then
            warn "timed out with $not_ready/$total pods not Ready"
            kubectl get pods -n "$NAMESPACE" --no-headers | grep -Ev 'nfs-subdir|Completed' \
                | awk '{split($2,a,"/"); if (a[1]!=a[2] || $3!="Running") print}'
            return 1
        fi
        echo "  $(date +%T) not-ready=${not_ready}/${total}"
        sleep 10
    done
}

# ---------------------------------------------------------------------------
# Note 5. Home timeline and user timeline should be largely DISJOINT: one is
# other people's posts, the other is your own. If the redis keyspaces have
# collided, the two responses converge on the same merged set.
check_timelines() {
    local addr="$1" out="$2"
    python3 - "$addr" > "$out" 2>&1 <<'PY'
import json, sys, urllib.request

addr = sys.argv[1]

def get(path):
    try:
        with urllib.request.urlopen(addr + path, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        return {"__error__": str(e)}

def post_ids(doc):
    if not isinstance(doc, list):
        return set()
    return {p.get("post_id") for p in doc if isinstance(p, dict)}

overlaps, checked = [], 0
for uid in (5, 42, 100, 300, 700):
    ut = get(f"/wrk2-api/user-timeline/read?user_id={uid}&start=0&stop=100")
    ht = get(f"/wrk2-api/home-timeline/read?user_id={uid}&start=0&stop=100")
    u, h = post_ids(ut), post_ids(ht)
    print(f"user {uid}: user-timeline={len(u)} posts, home-timeline={len(h)} posts")
    if not u or not h:
        if not u and not h:
            print(f"  user {uid}: BOTH EMPTY -- seeding may not have taken")
        continue
    checked += 1
    ov = 100.0 * len(u & h) / len(u | h)
    overlaps.append(ov)
    print(f"  jaccard overlap = {ov:.1f}%")

if overlaps:
    avg = sum(overlaps) / len(overlaps)
    print(f"OVERLAP_PCT={avg:.1f}")
    if avg > 50:
        print("VERDICT=COLLIDED  home and user timelines are serving the same "
              "data -- the redis keyspaces have merged. Throughput numbers are "
              "still meaningful as an infrastructure measurement, but the "
              "application semantics are WRONG.")
    else:
        print("VERDICT=OK  timelines are distinct")
else:
    print("OVERLAP_PCT=nan")
    print("VERDICT=UNKNOWN  could not compare (empty or failed responses)")
PY
    grep -E 'OVERLAP_PCT|VERDICT' "$out" || true
}

# ---------------------------------------------------------------------------
run_config() {
    local name="$1" desc="$2" files="$3" sets="$4"
    local dir="$RESULTS_ROOT/$name"
    mkdir -p "$dir"

    mazu_echo "############ CONFIG: $name -- $desc ############"

    teardown

    local args=(-f "$BENCH_VALUES")
    if [[ -n "$files" ]]; then
        # Any non-baseline config pulls in bitnami subcharts, so the legacy
        # image pins become mandatory (note 1).
        args+=(-f "$IMAGE_VALUES")
        for f in $files; do args+=(-f "${YAML}/${f}.yaml"); done
    fi
    [[ -n "$sets" ]] && args+=(--set "$sets")

    echo "helm upgrade --install $RELEASE $CHART_DIR ${args[*]}" | tee "$dir/helm-cmd.txt"

    if ! helm upgrade --install "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" \
            "${args[@]}" --timeout 20m0s > "$dir/helm.log" 2>&1; then
        warn "helm install FAILED for $name -- see $dir/helm.log"
        tail -30 "$dir/helm.log"
        # Hook logs are where the real cause usually is.
        for h in setup-mcrouter-configmap setup-collection-sharding-hook redis-cluster-readiness-hook; do
            kubectl logs "$h" -n "$NAMESPACE" > "$dir/hook-$h.log" 2>/dev/null && \
                { echo "--- $h ---"; tail -20 "$dir/hook-$h.log"; }
        done
        echo "$name,INSTALL_FAILED,,,,,," >> "$SUMMARY"
        return 1
    fi
    tail -5 "$dir/helm.log"

    if ! wait_ready 1500; then
        kubectl get pods -n "$NAMESPACE" -o wide > "$dir/pods.txt"
        echo "$name,NOT_READY,,,,,," >> "$SUMMARY"
        return 1
    fi

    kubectl get pods -n "$NAMESPACE" -o wide > "$dir/pods.txt"
    # Counted from a headerless query -- pods.txt keeps its header for humans.
    local podcount
    podcount=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
        | grep -cv 'nfs-subdir' || true)

    for h in setup-mcrouter-configmap setup-collection-sharding-hook redis-cluster-readiness-hook; do
        kubectl logs "$h" -n "$NAMESPACE" > "$dir/hook-$h.log" 2>/dev/null || true
    done

    # ---- ingress ----
    get_ingress
    [[ -z "$INGRESS_IP" ]] && { warn "no ingress IP"; echo "$name,NO_INGRESS,,,,,," >> "$SUMMARY"; return 1; }
    local addr="http://${INGRESS_IP}:${INGRESS_PORT}"

    mazu_echo "Waiting for the frontend to answer through the gateway..."
    local code=""
    for i in $(seq 1 60); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$addr/" || true)
        [[ "$code" == "200" ]] && break
        sleep 5
    done
    [[ "$code" != "200" ]] && { warn "gateway never returned 200 (last: $code)"; echo "$name,NO_FRONTEND,,,,,," >> "$SUMMARY"; return 1; }

    # ---- seed (note 4: always after the last install) ----
    mazu_echo "Seeding social graph from $GRAPH..."
    ( cd "$SCRIPT_DIR" && python3 scripts/init_social_graph.py \
        --graph="$GRAPH" --ip="$INGRESS_IP" --port="$INGRESS_PORT" --compose ) \
        2>&1 | grep -Ev '^[0-9]+$' | tee "$dir/seed.txt" | tail -8

    # ---- correctness ----
    mazu_echo "Checking timeline correctness..."
    check_timelines "$addr" "$dir/timelines.txt"
    local overlap
    overlap=$(sed -n 's/^OVERLAP_PCT=//p' "$dir/timelines.txt" | tail -1)
    [[ -z "$overlap" ]] && overlap="nan"

    if [[ "${SKIP_BENCH:-0}" == "1" ]]; then
        echo "$name,SKIPPED,,,,,${overlap},${podcount}" >> "$SUMMARY"
        return 0
    fi

    # ---- benchmark ----
    for rps in "${RPS_VALUES[@]}"; do
        local out="$dir/${rps}.txt"
        mazu_echo "wrk2: $name @ ${rps} RPS for ${DURATION}s"
        "$WRK_BIN" -D exp -t "$THREADS" -c "$CONNS" -d "$DURATION" -L \
            -s "$LUA" "$addr" -R "$rps" > "$out" 2>&1 || warn "wrk2 exited non-zero"

        grep -E '50\.000%|99\.000%|Requests/sec|Non-2xx|Socket errors' "$out" || true

        # wrk2 prints latency as "12.34ms" / "1.23s" / "123.45us"; normalise to ms.
        local delivered p50 p99 non2xx
        delivered=$(awk '/Requests\/sec/{print $2}' "$out")
        p50=$(awk '/ 50\.000%/{print $2}' "$out")
        p99=$(awk '/ 99\.000%/{print $2}' "$out")
        non2xx=$(awk '/Non-2xx/{print $NF}' "$out"); [[ -z "$non2xx" ]] && non2xx=0
        to_ms() {
            case "$1" in
                *us) echo "scale=4; ${1%us}/1000" | bc ;;
                *ms) echo "${1%ms}" ;;
                *s)  echo "scale=4; ${1%s}*1000" | bc ;;
                *)   echo "$1" ;;
            esac
        }
        echo "$name,$rps,${delivered:-},$(to_ms "${p50:-0}"),$(to_ms "${p99:-0}"),$non2xx,$overlap,$podcount" >> "$SUMMARY"
    done
}

# ---------------------------------------------------------------------------
FAILED=()
for want in "${SELECTED[@]}"; do
    for entry in "${LADDER[@]}"; do
        IFS='|' read -r name desc files sets <<< "$entry"
        if [[ "$name" == "$want" ]]; then
            run_config "$name" "$desc" "$files" "$sets" || FAILED+=("$name")
            break
        fi
    done
done

# The last configuration is left running so it can be inspected. Set
# KEEP_RELEASE=0 to tear it down and hand the nodes back.
if [[ "${KEEP_RELEASE:-1}" == "0" ]]; then
    teardown
fi

mazu_echo "=== DONE ==="
echo "Summary: $SUMMARY"
column -s, -t < "$SUMMARY"
if [[ ${#FAILED[@]} -gt 0 ]]; then
    warn "configs that did not complete: ${FAILED[*]}"
fi
