#!/bin/bash
#
# SocialNetwork (DeathStarBench): Istio vs Mazu strategy comparison.
#
# This is the SocialNetwork counterpart to run-benchmark1.5b-sweep.sh, which
# does the same comparison on Bookinfo. It mirrors that script's phase
# structure deliberately, so results are read the same way.
#
# The datastore topology is NOT the variable here -- it is the substrate. It
# is held FIXED across strategies so that a difference in the numbers is
# attributable to the mesh (Istio vs Mazu) and not to the storage tier.
# See run-socialnetwork-scale.sh for the experiment that picks the substrate.
#
# Usage:
#   ./run-socialnetwork-strategies.sh
#   STRATEGIES="istio" ./run-socialnetwork-strategies.sh
#   SUBSTRATE=baseline ./run-socialnetwork-strategies.sh
#
# Environment overrides:
#   STRATEGIES    - strategies to sweep (default: "st5-AttUpd istio")
#   SUBSTRATE     - all-correct | all | baseline   (default: all-correct)
#   RPS_VALUES    - wrk2 target rates (default: "100 200 400 600 800 1000 1200 1400")
#   DURATION      - seconds per RPS step (default: 120)
#   THREADS/CONNS - wrk2 -t / -c (default: 16 / 128)
#   WORKLOAD      - lua script (default: mixed-workload.lua)
#   GRAPH         - seed dataset (default: socfb-Reed98)
#   HPA           - 1 to run the REPLICA-CHURN experiment: every autoscaled
#                   deployment starts at 1 and grows under load via
#                   scratch/yaml/sn-hpa.yaml, so the comparison is about how
#                   each mesh behaves during scale-up. Default 0 = fixed
#                   fleet (steady-state latency comparison).
#                   Use a monotonically increasing RPS_VALUES with this, and a
#                   longer DURATION (240s) so each step converges.
#   GW_REPLICAS   - ingress gateway fleet size (default: 10)
#   RECREATE_TPM  - auto (default) | 1 | 0. auto recreates swtpm before every
#                   TPM strategy arm and skips it for arms that never touch the
#                   TPM (istio, st4-AudUpd). 1 forces it, 0 disables it.
#   TPM_JUMP_HOST - host to SSH into to reach the cluster nodes
#                   (default: the apiserver host from the current kubeconfig)
#   TPM_NODES     - nodes to recreate swtpm on (default: "node-0 node-1")
#   RESULTS_DIR   - output root
#   PROM_PORT     - local Prometheus port-forward (default: 9091)
#
# ---------------------------------------------------------------------------
# WHY THE SUBSTRATE IS WHAT IT IS
#
#   SUBSTRATE=all-correct  memcached (mcrouter x3 + 6 backends) and mongodb
#                          (3 mongos + 3 shards x2) are clustered; redis is
#                          left STANDALONE on purpose.
#
#   Redis Cluster is deliberately excluded even though the chart supports it.
#   HomeTimelineService and UserTimelineService both key on a bare user id
#   (src/HomeTimelineService/HomeTimelineHandler.h:150 and
#   src/UserTimelineService/UserTimelineHandler.h:168), and cluster mode points
#   every redis client at one shared service -- so the two timelines merge into
#   a single key and both endpoints start returning the same data. Measured:
#   100% Jaccard overlap under redis cluster vs 0% everywhere else. A mesh
#   comparison run on that substrate would be comparing meshes over a corrupt
#   application. See SOCIALNETWORK-ON-ISTIO.md.
#
#   SUBSTRATE=baseline     all datastores standalone. Simpler, and known to
#                          saturate around 900 RPS on this cluster.
#
# OTHER THINGS THAT WILL BITE YOU
#
#   * SEED AFTER INSTALL, ALWAYS. No datastore in any substrate has a
#     persistent volume, so anything that rolls a pod wipes the dataset and
#     the benchmark then measures empty timelines while returning HTTP 200.
#
#   * GATEWAY FLEET MUST MATCH ACROSS ARMS. The mazu arms get a fixed fleet of
#     GW_REPLICAS from scratch/yaml/istio-operator*.yaml. `install-istio`
#     applies the stock default profile, whose gateway HPA is 1..5 with no node
#     affinity -- so the istio arm is patched to match. Without this the istio
#     baseline saturates its gateway and the comparison measures gateway
#     starvation rather than the data plane. Same reasoning as bench1.5b.
#
#   * SWTPM STATE MUST BE RECREATED BEFORE EVERY TPM ARM. It is host state
#     (/tmp/myvtpm2 + /dev/tpmrm0 on the node), so nothing inside Kubernetes
#     resets it -- not `helm uninstall`, not `istioctl uninstall --purge`, not
#     reinstalling the mesh. A dirty TPM makes istiod's Key Curator panic:
#
#       error: can't initialize trinket: TPM_RC_NV_DEFINED       (index left over)
#       error: can't initialize trinket: TPM_RC_OBJECT_MEMORY    (contexts leaked)
#       counter attestation: <nil>
#       panic: nil pointer dereference -- counterAttestationToProto(0x0)
#
#     istiod then crash-loops, no sidecar ever registers, and all GW_REPLICAS
#     gateway pods sit at 0/1 until the install times out. Observed on
#     2026-08-14: two consecutive runs died this way and produced no data.
#
#     There is no scheduling escape: istiod is pinned to control-plane nodes and
#     the TPM profile requires tpm.boxboat.io/tpmrm, which only node-1 of the
#     two advertises -- so istiod always lands on the same node's TPM.
#
#   * HOOK PODS SURVIVE `helm uninstall` (no hook-delete-policy), and block the
#     next install with "pod already exists". Deleted explicitly below.
#
#   * THE MONGO HOOK RUNS INSIDE THE MESH. mtls.yaml applies a mesh-wide STRICT
#     PeerAuthentication, so setup-collection-sharding-hook cannot opt out of
#     injection the way the mcrouter hook does -- it has to reach mongos over
#     mTLS. Under Mazu that means its sidecar needs the RBE params like every
#     other pod, or it never registers, its mongo connect never succeeds,
#     setup_mongo() retries forever and helm blocks on the hook until the
#     20-minute timeout. templates/hooks/mongodb/post-install-hook.yaml now
#     picks up global.podAnnotations for exactly this reason.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YAML="${SCRIPT_DIR}/scratch/yaml"
CHART_DIR="${SCRIPT_DIR}/helm-chart/socialnetwork"
RELEASE="${RELEASE:-social-network}"
NAMESPACE="${NAMESPACE:-default}"

STRATEGIES=(${STRATEGIES:-"st5-AttUpd" "istio"})
SUBSTRATE="${SUBSTRATE:-all-correct}"
RPS_VALUES=(${RPS_VALUES:-100 200 400 600 800 1000 1200 1400})
DURATION=${DURATION:-120}
THREADS=${THREADS:-16}
CONNS=${CONNS:-128}
WORKLOAD="${WORKLOAD:-mixed-workload.lua}"
GRAPH="${GRAPH:-socfb-Reed98}"
PROM_PORT=${PROM_PORT:-9091}

# HPA=1 turns this into a replica-churn experiment: every autoscaled
# deployment starts at 1 and grows under load, so the comparison is about how
# each mesh behaves DURING scale-up rather than at a fixed fleet size. Off by
# default -- the fixed-fleet mode is the steady-state latency comparison.
HPA="${HPA:-0}"

# Keep in sync with hpaSpec min/maxReplicas in scratch/yaml/istio-operator.yaml
# and scratch/yaml/istio-operator-tpm.yaml (both currently 10).
GW_REPLICAS=${GW_REPLICAS:-10}

RESULTS_ROOT="${RESULTS_DIR:-${SCRIPT_DIR}/results/sn-strategies-$(date +%m-%d-%y_%H%M%S)}"
ISTIOCTL_PATH="$HOME/istio-1.24.0/bin/istioctl"

# --- TPM recreation -------------------------------------------------------
# This box is a jump host: it has kubectl access but no route to the cluster
# LAN, so swtpm is reset the way bench1.5b does it -- SSH to a node that is on
# that LAN, then hop to each target node from there. Defaulting the jump host
# to the kubeconfig's apiserver address keeps this working when the experiment
# is reprovisioned, instead of hardcoding a pcXXX name that goes stale.
RECREATE_TPM="${RECREATE_TPM:-auto}"
TPM_JUMP_HOST="${TPM_JUMP_HOST:-$(kubectl config view -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null | sed -E 's#^https?://##; s#:[0-9]+$##')}"
# istiod is the only component that requests the TPM device and it is pinned to
# the control-plane nodes, so these are the TPMs that actually matter.
TPM_NODES="${TPM_NODES:-node-0 node-1}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=10)

# --- Substrate definition --------------------------------------------------
# Mirrors the "all-correct" rung of run-socialnetwork-scale.sh exactly.
SUBSTRATE_FILES=()
SUBSTRATE_SETS=""
warn_redis_substrate=0
case "$SUBSTRATE" in
    all-correct)
        SUBSTRATE_FILES=(sn-images-legacy sn-memcached-cluster sn-mongodb-sharded)
        SUBSTRATE_SETS="mcrouter.statefulset.replicas=3,mcrouter.memcached.replicaCount=6,mongodb-sharded.mongos.replicaCount=3,mongodb-sharded.shards=3,mongodb-sharded.shardsvr.dataNode.replicaCount=2"
        ;;
    all)
        # Everything clustered, including redis. SEMANTICALLY BROKEN -- see the
        # substrate note in the header. Mirrors the "all" rung of
        # run-socialnetwork-scale.sh exactly.
        SUBSTRATE_FILES=(sn-images-legacy sn-memcached-cluster sn-mongodb-sharded sn-redis-cluster)
        SUBSTRATE_SETS="mcrouter.statefulset.replicas=3,mcrouter.memcached.replicaCount=6,mongodb-sharded.mongos.replicaCount=3,mongodb-sharded.shards=3,mongodb-sharded.shardsvr.dataNode.replicaCount=2,redis-cluster.cluster.nodes=6,redis-cluster.cluster.replicas=1"
        warn_redis_substrate=1
        ;;
    baseline)
        SUBSTRATE_FILES=()
        SUBSTRATE_SETS=""
        ;;
    *)
        echo "ERROR: unknown SUBSTRATE '$SUBSTRATE' (expected all-correct, all, or baseline)"; exit 1 ;;
esac

# Borrow the two helpers from setup_social_network.sh without running the rest
# of it. Sliced brace-to-brace rather than with `grep -A5` as bench1.5b does:
# mazu_echo() is only 4 lines, so -A5 also drags in setup_social_network.sh's
# own `SCRIPT_DIR=$(dirname "${BASH_SOURCE[0]}")` and silently reassigns ours.
# It happens to land on the same value while both scripts sit in this
# directory, but it also means a failed grep leaves get_ingress_ip_port
# undefined with a zero exit status -- which then dies mid-arm, after the mesh
# and the whole application are already installed.
source_setup() {
    local f="$SCRIPT_DIR/setup_social_network.sh"
    [[ -f "$f" ]] || { echo "ERROR: not found: $f"; exit 1; }
    eval "$(sed -n '/^mazu_echo()/,/^}/p' "$f")"
    eval "$(sed -n '/^get_ingress_ip_port/,/^}/p' "$f")"
    for fn in mazu_echo get_ingress_ip_port; do
        declare -F "$fn" >/dev/null || { echo "ERROR: could not extract $fn() from $f"; exit 1; }
    done
}
source_setup
warn() { echo -e "\033[1;33mWARNING: $*\033[0m"; }

mkdir -p "$RESULTS_ROOT"
exec > >(tee -a "$RESULTS_ROOT/run.log") 2>&1

SUMMARY="$RESULTS_ROOT/summary.csv"
echo "strategy,rps_target,rps_delivered,p50_ms,p99_ms,non_2xx,timeline_overlap_pct,pods" > "$SUMMARY"

mazu_echo "=== SocialNetwork: strategy comparison ==="
echo "Strategies: ${STRATEGIES[*]}"
echo "Substrate:  $SUBSTRATE"
echo "Workload:   $WORKLOAD @ ${RPS_VALUES[*]} RPS x ${DURATION}s (-t $THREADS -c $CONNS)"
echo "Gateway:    $GW_REPLICAS replicas per arm"
echo "Results:    $RESULTS_ROOT"

# SUBSTRATE=all puts redis in cluster mode, which merges the home and user
# timelines onto one keyspace (see the substrate note in the header). The run
# is still a valid INFRASTRUCTURE measurement, but the application semantics
# are wrong and check_timelines() will report COLLIDED. Say so up front rather
# than leaving it to be discovered in the overlap column.
if [[ "$warn_redis_substrate" == "1" ]]; then
    warn "SUBSTRATE=all runs redis in CLUSTER mode."
    warn "  HomeTimelineService and UserTimelineService both key on a bare user id"
    warn "  and share one redis-cluster service, so the two timelines MERGE."
    warn "  Expect check_timelines to report VERDICT=COLLIDED with ~100% overlap."
    warn "  Throughput numbers remain meaningful as an infrastructure measurement;"
    warn "  the application semantics do NOT. Use SUBSTRATE=all-correct for a"
    warn "  semantically valid run."
fi

WRK_BIN="${SCRIPT_DIR}/../wrk2/wrk"
[[ -x "$WRK_BIN" ]] || "$SCRIPT_DIR/setup_social_network.sh" build-wrk2
LUA="${SCRIPT_DIR}/wrk2/scripts/social-network/${WORKLOAD}"
[[ -f "$LUA" ]] || { echo "ERROR: workload not found: $LUA"; exit 1; }

# Both meshes are installed through istioctl, and `remove-istio` calls it
# directly with no fallback -- fail here rather than three phases in.
if [[ ! -x "$ISTIOCTL_PATH" ]]; then
    mazu_echo "istioctl not found at $ISTIOCTL_PATH, installing..."
    ( cd "$HOME" && curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.24.0 TARGET_ARCH=x86_64 sh - )
fi
[[ -x "$ISTIOCTL_PATH" ]] || { echo "ERROR: istioctl still missing at $ISTIOCTL_PATH"; exit 1; }

# ---------------------------------------------------------------------------
teardown_app() {
    mazu_echo "Tearing down social-network..."
    # HPAs are applied outside the release, so helm does not own them. Left
    # behind they would immediately start resizing the NEXT arm's deployments
    # from its very first sync.
    kubectl delete -f "${YAML}/sn-hpa.yaml" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
    helm uninstall "$RELEASE" -n "$NAMESPACE" --wait --timeout 10m0s 2>/dev/null || true
    kubectl delete -f "$SCRIPT_DIR/kubernetes/istio-gateway.yaml" --ignore-not-found 2>/dev/null || true
    kubectl delete pod -n "$NAMESPACE" --ignore-not-found --wait=false \
        setup-mcrouter-configmap setup-collection-sharding-hook \
        redis-cluster-readiness-hook 2>/dev/null || true
    for sel in mongodb-sharded redis-cluster memcached; do
        kubectl delete pvc -n "$NAMESPACE" -l "app.kubernetes.io/name=$sel" \
            --ignore-not-found --wait=false 2>/dev/null || true
    done
    local deadline=$(( $(date +%s) + 600 ))
    while :; do
        local left
        left=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -cv 'nfs-subdir' || true)
        [[ "$left" -eq 0 ]] && break
        if [[ "$(date +%s)" -ge "$deadline" ]]; then
            # Do NOT just warn and move on. A pod that outlives this loop is
            # still counted by wait_ready, which then fails the whole arm 30
            # minutes later for a corpse belonging to the previous run --
            # observed with setup-mcrouter-configmap wedged in
            # ContainerCreating after an interrupted all-correct sweep.
            warn "$left pods still terminating after 600s -- force deleting"
            kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
                | grep -v 'nfs-subdir' | awk '{print $1}' \
                | xargs -r kubectl delete pod -n "$NAMESPACE" --force --grace-period=0 \
                    --ignore-not-found 2>/dev/null || true
            sleep 10
            left=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -cv 'nfs-subdir' || true)
            [[ "$left" -gt 0 ]] && warn "$left pods survived force delete"
            break
        fi
        sleep 5
    done
}

# Pods that must not gate application readiness:
#   nfs-subdir  - cluster infrastructure, not part of the release
#   Completed   - finished one-shot pods
#   Terminating - leftovers from a previous arm on their way out
#   the three post-install hooks - one-shot jobs, and helm has ALREADY blocked
#     on them by the time this runs, so a hook still present here is either
#     done or a corpse. Counting them cost a full arm once (see teardown_app).
READY_EXCLUDE='nfs-subdir|Completed|Terminating|setup-mcrouter-configmap|setup-collection-sharding-hook|redis-cluster-readiness-hook'

wait_ready() {
    local deadline=$(( $(date +%s) + ${1:-1500} ))
    mazu_echo "Waiting for all pods to become Ready..."
    while :; do
        local total not_ready
        total=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -Ecv "$READY_EXCLUDE" || true)
        not_ready=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -Ev "$READY_EXCLUDE" \
            | awk '{split($2,a,"/"); if (a[1]!=a[2] || $3!="Running") c++} END{print c+0}')
        if [[ "$total" -gt 0 && "$not_ready" -eq 0 ]]; then echo "  all $total pods Ready"; return 0; fi
        if [[ "$(date +%s)" -ge "$deadline" ]]; then
            warn "timed out with $not_ready/$total pods not Ready"
            kubectl get pods -n "$NAMESPACE" --no-headers | grep -Ev "$READY_EXCLUDE" \
                | awk '{split($2,a,"/"); if (a[1]!=a[2] || $3!="Running") print}'
            return 1
        fi
        echo "  $(date +%T) not-ready=${not_ready}/${total}"
        sleep 10
    done
}

# Home and user timelines should be largely disjoint. If they are not, the
# datastore keyspaces have merged and the run is measuring a broken app.
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
def pids(doc):
    return {p.get("post_id") for p in doc if isinstance(p, dict)} if isinstance(doc, list) else set()
ovs = []
for uid in (5, 42, 100, 300, 700):
    u = pids(get(f"/wrk2-api/user-timeline/read?user_id={uid}&start=0&stop=100"))
    h = pids(get(f"/wrk2-api/home-timeline/read?user_id={uid}&start=0&stop=100"))
    print(f"user {uid}: user-timeline={len(u)} posts, home-timeline={len(h)} posts")
    if u and h:
        ov = 100.0 * len(u & h) / len(u | h)
        ovs.append(ov)
        print(f"  jaccard overlap = {ov:.1f}%")
    elif not u and not h:
        print(f"  user {uid}: BOTH EMPTY -- seeding may not have taken")
if ovs:
    avg = sum(ovs)/len(ovs)
    print(f"OVERLAP_PCT={avg:.1f}")
    print("VERDICT=" + ("COLLIDED  timelines merged -- app semantics are WRONG" if avg > 50 else "OK  timelines are distinct"))
else:
    print("OVERLAP_PCT=nan"); print("VERDICT=UNKNOWN  could not compare")
PY
    grep -E 'OVERLAP_PCT|VERDICT' "$out" || true
}

# Recreate swtpm on the control-plane nodes. Mirrors run-benchmark1.5b-sweep.sh,
# which does this before every arm -- see the header for why it is mandatory.
# setup-tpm.sh create_tpm pkills swtpm_cuse, deletes /dev/tpmrm0 and the
# /tmp/myvtpm2 state dir, then re-runs swtpm_setup, which is what clears both
# the leftover NV index and the leaked object contexts.
recreate_tpms() {
    [[ -n "$TPM_JUMP_HOST" ]] || { warn "no TPM_JUMP_HOST and none derivable from kubeconfig"; return 1; }
    mazu_echo "Recreating swtpm on [$TPM_NODES] via $TPM_JUMP_HOST..."

    if ! ssh "${SSH_OPTS[@]}" "$TPM_JUMP_HOST" 'test -d ~/trinc'; then
        warn "~/trinc missing on $TPM_JUMP_HOST -- bootstrap it first with:"
        warn "  dev/setup-tpm-all-nodes.sh -d <domain> <node> [<node>...]"
        return 1
    fi

    # Parallel across nodes, as bench1.5b does. The inner ssh needs its own
    # host-key option: it runs on the jump host, not here.
    ssh "${SSH_OPTS[@]}" "$TPM_JUMP_HOST" \
        "for node in $TPM_NODES; do \
             ssh -o StrictHostKeyChecking=no -o BatchMode=yes \"\$node\" \
                 '~/trinc/swtpm-test/setup-tpm.sh create_tpm' & \
         done; wait" || { warn "swtpm recreation failed"; return 1; }

    # Not in bench1.5b, but needed: create_tpm deletes and recreates the
    # /dev/tpmrm0 inode, and the k8s-tpm-device DaemonSet holds a hostPath
    # mount of the old one. Without a restart the plugin keeps advertising a
    # device that no longer exists and istiod is admitted against a stale handle.
    if kubectl -n tpm-device get ds k8s-tpm-device >/dev/null 2>&1; then
        kubectl -n tpm-device rollout restart ds k8s-tpm-device >/dev/null
        kubectl -n tpm-device rollout status ds k8s-tpm-device --timeout=180s || \
            warn "k8s-tpm-device DaemonSet did not settle"
    fi

    sleep 60
    return 0
}

to_ms() {
    case "$1" in
        *us) echo "scale=4; ${1%us}/1000" | bc ;;
        *ms) echo "${1%ms}" ;;
        # wrk2 switches to MINUTES past ~60s ("0.95m"). Without this case the
        # value falls through to the catch-all and is written verbatim into a
        # millisecond column, where it parses as 0.95 -- 60000x too small, and
        # sorts as the BEST latency in the table when it is in fact the worst.
        *m)  echo "scale=4; ${1%m}*60000" | bc ;;
        *s)  echo "scale=4; ${1%s}*1000" | bc ;;
        *)   echo "$1" ;;
    esac
}

# ===========================================================================
for STRAT in "${STRATEGIES[@]}"; do
    export STRAT
    RES_DIR="$RESULTS_ROOT/$STRAT"
    mkdir -p "$RES_DIR"

    mazu_echo "############ STRATEGY: $STRAT (substrate: $SUBSTRATE) ############"
    echo "=== started at $(date) ==="

    # ---------------- Phase A: setup ----------------
    teardown_app

    mazu_echo "Removing Istio..."
    "$SCRIPT_DIR/setup_social_network.sh" remove-istio || true
    kubectl wait --for=delete pod -l app=istiod -n istio-system --timeout=300s 2>/dev/null || true
    kubectl wait --for=delete pod -l app=istio-ingressgateway -n istio-system --timeout=300s 2>/dev/null || true

    # Reset the API server between arms, as bench1.5b does -- Mazu's attestation
    # path leans on the API server, and a warm one carries state across arms.
    APISERVER_COUNT=$(kubectl -n kube-system get pods -l component=kube-apiserver --no-headers 2>/dev/null | wc -l)
    kubectl -n kube-system delete pods -l component=kube-apiserver --now --timeout=60s 2>/dev/null || true
    echo "Waiting for $APISERVER_COUNT kube-apiserver pods to become Ready..."
    until [ "$(kubectl -n kube-system get pods -l component=kube-apiserver \
        -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null \
        | grep -c True)" -ge "$APISERVER_COUNT" ]; do sleep 5; done
    echo "All kube-apiserver pods are Ready"

    # ---- TPM state ----
    # Must happen after `remove-istio` (nothing is holding /dev/tpmrm0) and
    # before the mesh install (istiod initialises the trinket on startup).
    # A dirty TPM is fatal to this arm, not cosmetic, so skip the arm rather
    # than install a mesh whose istiod is guaranteed to crash-loop.
    NEEDS_TPM=0
    case "$RECREATE_TPM" in
        1)    NEEDS_TPM=1 ;;
        0)    NEEDS_TPM=0 ;;
        auto) [[ "$STRAT" == *AttUpd* ]] && NEEDS_TPM=1 ;;
        *)    warn "unknown RECREATE_TPM='$RECREATE_TPM', treating as auto"
              [[ "$STRAT" == *AttUpd* ]] && NEEDS_TPM=1 ;;
    esac

    if [[ "$NEEDS_TPM" == "1" ]]; then
        if ! recreate_tpms; then
            warn "swtpm recreation FAILED for $STRAT -- skipping this arm"
            echo "$STRAT,TPM_RECREATE_FAILED,,,,,," >> "$SUMMARY"
            continue
        fi
    else
        echo "Skipping swtpm recreation for $STRAT (does not use the TPM)"
    fi

    # ---- Install the mesh ----
    # deploy-rbe-pp.sh / deploy-mazu-configmap.sh / deploy-tpm-secret.sh all
    # write into istio-system BEFORE istioctl runs -- the mazu arms' istiod and
    # gateway mount those configMaps from the operator overlay. `remove-istio`
    # above may have taken the namespace with it, so recreate it here.
    kubectl get namespace istio-system >/dev/null 2>&1 || kubectl create namespace istio-system

    # A mesh install failure must not take the other arm down with it -- this is
    # an unattended multi-hour run, and the Mazu arm is new for SocialNetwork.
    # These two are part of that: they end in a bare `kubectl create`, so under
    # `set -e` a transient API-server error here would abort every remaining arm.
    MESH_OK=1
    "$SCRIPT_DIR/dev/deploy-mazu-configmap.sh" "$STRAT" || MESH_OK=0
    "$SCRIPT_DIR/dev/deploy-rbe-pp.sh" || MESH_OK=0
    if [[ "$MESH_OK" != "1" ]]; then
        # No point installing a mesh whose sidecars would block on a missing
        # configMap for the full istioctl timeout.
        warn "mazu configMaps FAILED for $STRAT -- skipping this arm"
        echo "$STRAT,CONFIGMAP_FAILED,,,,,," >> "$SUMMARY"; continue
    fi

    if [[ "$STRAT" == "istio" ]]; then
        "$SCRIPT_DIR/setup_social_network.sh" install-istio || MESH_OK=0

        # Match the mazu arms' fixed gateway fleet -- see the header note.
        # Only if the install actually landed: patching a deployment that was
        # never created fails, and under `set -e` that would kill the sweep
        # instead of skipping this arm.
        if [[ "$MESH_OK" == "1" ]]; then
            kubectl -n istio-system patch deployment istio-ingressgateway --type=merge -p '{
              "spec": {"template": {"spec": {"affinity": {"nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                  {"matchExpressions": [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}
                  ]}
                ]}
              }}}}}
            }' || MESH_OK=0
            kubectl -n istio-system patch hpa istio-ingressgateway --type=merge \
                -p "{\"spec\": {\"minReplicas\": ${GW_REPLICAS}, \"maxReplicas\": ${GW_REPLICAS}}}" 2>/dev/null || true
            kubectl -n istio-system rollout status deployment/istio-ingressgateway --timeout=300s || true
        fi
    else
        if [[ "$STRAT" == "st5-AttUpd" ]]; then
            "$SCRIPT_DIR/dev/tpm/install-k8s-tpm-device.sh" || MESH_OK=0
            "$SCRIPT_DIR/dev/tpm/deploy-tpm-pubkey-configmap.sh" || MESH_OK=0
            "$SCRIPT_DIR/dev/tpm/deploy-tpm-secret.sh" || MESH_OK=0
        fi
        "$SCRIPT_DIR/setup_social_network.sh" install-mazu || MESH_OK=0
    fi

    if [[ "$MESH_OK" != "1" ]]; then
        warn "mesh install FAILED for $STRAT -- skipping this arm"
        kubectl get pods -n istio-system -o wide > "$RES_DIR/istio-pods.txt" 2>/dev/null || true
        echo "$STRAT,MESH_INSTALL_FAILED,,,,,," >> "$SUMMARY"
        continue
    fi

    kubectl wait --for=condition=Ready pod -l app=istiod -n istio-system --timeout=600s || true
    kubectl wait --for=condition=Ready pod -l app=istio-ingressgateway -n istio-system --timeout=600s || true

    # Skip this arm rather than exiting: a gateway that never comes up is
    # usually an arm-specific problem (e.g. istiod crash-looping on stale TPM
    # state in the Mazu arms), and it must not take the other arm down with it.
    echo "Waiting for ${GW_REPLICAS} ready istio-ingressgateway replicas..."
    GW_OK=0
    for i in $(seq 1 60); do
        GW_READY=$(kubectl -n istio-system get deploy istio-ingressgateway \
            -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
        [ "${GW_READY:-0}" -ge "$GW_REPLICAS" ] && { GW_OK=1; break; }
        sleep 5
    done
    if [[ "$GW_OK" != "1" ]]; then
        warn "only ${GW_READY:-0}/${GW_REPLICAS} gateway replicas Ready -- skipping $STRAT"
        kubectl get pods -n istio-system -o wide > "$RES_DIR/istio-pods.txt" 2>/dev/null || true
        kubectl logs -n istio-system -l app=istiod --tail=100 > "$RES_DIR/istiod.log" 2>/dev/null || true
        kubectl logs -n istio-system -l app=istiod --previous --tail=100 > "$RES_DIR/istiod-previous.log" 2>/dev/null || true
        echo "$STRAT,GATEWAY_NOT_READY,,,,,," >> "$SUMMARY"
        continue
    fi
    echo "istio-ingressgateway: ${GW_READY} replicas Ready"

    # ---- Install the application (AFTER the mesh, so every pod gets a sidecar) ----
    kubectl apply -f "$SCRIPT_DIR/kubernetes/istio-gateway.yaml"
    kubectl apply -f "${YAML}/mcrouter-role.yaml" >/dev/null

    HELM_ARGS=(-f "${YAML}/socialnetwork-bench-values.yaml")
    for f in "${SUBSTRATE_FILES[@]}"; do HELM_ARGS+=(-f "${YAML}/${f}.yaml"); done

    # Mazu sidecar annotations. WITHOUT THESE EVERY APPLICATION POD HANGS AT
    # 1/2 Running with "RBE registration not yet confirmed by Key Curator" --
    # the app container is fine, the sidecar just never registers because the
    # RBE public params are not mounted into it. The Bookinfo manifests carry
    # the same annotations inline; this chart takes them via podAnnotations.
    # TPM strategies additionally need the tpm-pubkey configMap, which
    # dev/tpm/deploy-tpm-pubkey-configmap.sh creates only for those arms --
    # so the non-TPM file must be used elsewhere or pods block on a missing
    # configMap.
    if [[ "$STRAT" == *AttUpd* ]]; then
        HELM_ARGS+=(-f "${YAML}/sn-mazu-annotations-tpm.yaml")
    else
        HELM_ARGS+=(-f "${YAML}/sn-mazu-annotations.yaml")
    fi

    # HPA mode: drop every autoscaled deployment to 1 replica so the fleet has
    # somewhere to grow from. Must be layered AFTER the bench values, whose
    # pinned replica counts it overrides.
    [[ "$HPA" == "1" ]] && HELM_ARGS+=(-f "${YAML}/sn-hpa-values.yaml")

    [[ -n "$SUBSTRATE_SETS" ]] && HELM_ARGS+=(--set "$SUBSTRATE_SETS")

    echo "helm upgrade --install $RELEASE $CHART_DIR ${HELM_ARGS[*]}" | tee "$RES_DIR/helm-cmd.txt"
    if ! helm upgrade --install "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" \
            "${HELM_ARGS[@]}" --timeout 20m0s > "$RES_DIR/helm.log" 2>&1; then
        warn "helm install FAILED for $STRAT"; tail -30 "$RES_DIR/helm.log"
        echo "$STRAT,INSTALL_FAILED,,,,,," >> "$SUMMARY"; continue
    fi

    wait_ready 1800 || { kubectl get pods -n "$NAMESPACE" -o wide > "$RES_DIR/pods.txt"; \
        echo "$STRAT,NOT_READY,,,,,," >> "$SUMMARY"; continue; }

    kubectl get pods -n "$NAMESPACE" -o wide > "$RES_DIR/pods.txt"
    PODCOUNT=$(kubectl get pods -n "$NAMESPACE" --no-headers | grep -cv 'nfs-subdir' || true)

    get_ingress_ip_port
    ADDR="http://${INGRESS_IP}:${INGRESS_PORT}"
    echo "Ingress: ${INGRESS_IP}:${INGRESS_PORT}"

    mazu_echo "Waiting for the frontend to answer..."
    CODE=""
    for i in $(seq 1 60); do
        CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$ADDR/" || true)
        [[ "$CODE" == "200" ]] && break
        sleep 5
    done
    [[ "$CODE" != "200" ]] && { warn "gateway never returned 200 (last: $CODE)"; \
        echo "$STRAT,NO_FRONTEND,,,,,," >> "$SUMMARY"; continue; }

    # ---- Seed (always after the last install) ----
    # `set -o pipefail` is on, so a seeding failure (or a `grep` that filters
    # every line away) would otherwise abort the whole sweep instead of just
    # this arm. Seeding is not optional -- without it the benchmark measures
    # empty timelines while returning 200 -- so treat a failure as a skip.
    mazu_echo "Seeding social graph from $GRAPH..."
    SEED_OK=1
    ( cd "$SCRIPT_DIR" && python3 scripts/init_social_graph.py \
        --graph="$GRAPH" --ip="$INGRESS_IP" --port="$INGRESS_PORT" --compose ) \
        > "$RES_DIR/seed.txt" 2>&1 || SEED_OK=0
    grep -Ev '^[0-9]+$' "$RES_DIR/seed.txt" | tail -8 || true
    if [[ "$SEED_OK" != "1" ]]; then
        warn "seeding FAILED for $STRAT -- skipping this arm"
        echo "$STRAT,SEED_FAILED,,,,,," >> "$SUMMARY"; continue
    fi

    mazu_echo "Checking timeline correctness..."
    check_timelines "$ADDR" "$RES_DIR/timelines.txt"
    OVERLAP=$(sed -n 's/^OVERLAP_PCT=//p' "$RES_DIR/timelines.txt" | tail -1); [[ -z "$OVERLAP" ]] && OVERLAP="nan"

    # ---- HPAs: applied LAST, after seeding, immediately before the sweep ----
    # Placement is the whole experiment. Seeding with init_social_graph.py
    # --compose is a heavy write workload: with the HPAs already live it drove
    # the fleet from 27 pods to 163 against a ceiling of 165 BEFORE the first
    # wrk2 step, so the RPS sweep had no scaling range left and traced a flat
    # line -- measuring nothing, which is the entire failure this mode exists
    # to fix. Seed on the fixed floor, then hand the settled fleet to the HPAs
    # so every replica added afterwards is attributable to benchmark load.
    if [[ "$HPA" == "1" ]]; then
        mazu_echo "Enabling HPAs (post-seed, pre-sweep)..."
        kubectl apply -f "${YAML}/sn-hpa.yaml" -n "$NAMESPACE" >/dev/null

        # An HPA reporting <unknown> never scales, and the run would silently
        # measure a fixed fleet. metrics-server needs a few scrape intervals
        # before ContainerResource targets resolve.
        echo "Waiting for HPA metrics to resolve..."
        for i in $(seq 1 30); do
            UNKNOWN=$(kubectl get hpa -n "$NAMESPACE" --no-headers 2>/dev/null \
                | grep -c '<unknown>' || true)
            [[ "$UNKNOWN" -eq 0 ]] && break
            sleep 10
        done
        [[ "${UNKNOWN:-1}" -eq 0 ]] || warn "$UNKNOWN HPAs still report <unknown> targets"

        # Let the seeding CPU spike decay before the first step, so the opening
        # fleet reflects idle rather than the tail of the seed workload.
        echo "Letting post-seed CPU settle..."
        sleep 120
        kubectl get hpa -n "$NAMESPACE" > "$RES_DIR/hpa-initial.txt" 2>&1 || true
        kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -Ecv "$READY_EXCLUDE" | sed 's/^/  fleet at sweep start: /'
    fi

    # ---- Prometheus ----
    lsof -ti :${PROM_PORT} | xargs -r kill 2>/dev/null || true
    sleep 5
    "$SCRIPT_DIR/setup_social_network.sh" uninstall-prometheus || true
    "$SCRIPT_DIR/setup_social_network.sh" install-prometheus
    kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=prometheus -n istio-system --timeout=300s
    kubectl port-forward -n istio-system svc/prometheus ${PROM_PORT}:9090 &
    PF_PID=$!
    export PROM_URL="http://localhost:${PROM_PORT}"
    sleep 5
    for i in $(seq 1 30); do
        curl -sf "${PROM_URL}/-/ready" >/dev/null 2>&1 && { echo "Prometheus reachable"; break; }
        [ "$i" -eq 30 ] && warn "Prometheus not reachable, metrics may fail"
        sleep 5
    done

    # ---- Spanning pod poller (1 Hz) ----
    POD_CSV="$RES_DIR/pods.csv"
    echo "timestamp,service,phase,ready" > "$POD_CSV"
    (
        while true; do
            ts=$(date +%s)
            kubectl get pods -n "$NAMESPACE" -l 'service' \
                -o jsonpath='{range .items[*]}{.metadata.labels.service}{","}{.status.phase}{","}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null \
              | awk -v ts="$ts" 'NF{print ts","$0}' >> "$POD_CSV" &
            sleep 1
        done
    ) &
    POD_POLL_PID=$!

    # ---------------- Phase B: RPS sweep ----------------
    for RPS in "${RPS_VALUES[@]}"; do
        mazu_echo "--- $STRAT @ ${RPS} RPS for ${DURATION}s ---"
        export RPS
        OUT_FILE="$RES_DIR/${RPS}.txt"
        BENCH_START=$(date +%s)

        "$WRK_BIN" -D exp -t "$THREADS" -c "$CONNS" -d "$DURATION" -L \
            -s "$LUA" "$ADDR" -R "$RPS" > "$OUT_FILE" 2>&1 || warn "wrk2 exited non-zero"

        BENCH_END=$(date +%s)
        grep -E '50\.000%|99\.000%|Requests/sec|Non-2xx|Socket errors' "$OUT_FILE" || true

        awk -F, -v s="$BENCH_START" -v e="$BENCH_END" 'NR==1 || ($1>=s && $1<=e)' \
            "$POD_CSV" > "$RES_DIR/pods-${RPS}.csv"

        "$SCRIPT_DIR/collect_metrics.sh" "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS" \
            || warn "Metrics collection failed for RPS=$RPS"

        # Fleet size at the END of this step, plus the HPA's own view. The
        # 1 Hz pods.csv slice has the shape of the ramp; this is the endpoint
        # and the reason for it (current vs target utilization).
        if [[ "$HPA" == "1" ]]; then
            kubectl get hpa -n "$NAMESPACE" > "$RES_DIR/hpa-${RPS}.txt" 2>&1 || true
            kubectl get deploy -n "$NAMESPACE" -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas --no-headers \
                > "$RES_DIR/deploy-${RPS}.txt" 2>&1 || true
        fi

        DELIVERED=$(awk '/Requests\/sec/{print $2}' "$OUT_FILE")
        P50=$(awk '/ 50\.000%/{print $2}' "$OUT_FILE")
        P99=$(awk '/ 99\.000%/{print $2}' "$OUT_FILE")
        NON2XX=$(awk '/Non-2xx/{print $NF}' "$OUT_FILE"); [[ -z "$NON2XX" ]] && NON2XX=0

        # Under HPA the fleet is the dependent variable, so report it per step
        # rather than the constant captured before the sweep.
        STEP_PODS="$PODCOUNT"
        [[ "$HPA" == "1" ]] && STEP_PODS=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -Ev "$READY_EXCLUDE" | wc -l | tr -d ' ')

        echo "$STRAT,$RPS,${DELIVERED:-},$(to_ms "${P50:-0}"),$(to_ms "${P99:-0}"),$NON2XX,$OVERLAP,$STEP_PODS" >> "$SUMMARY"
    done

    # ---------------- Phase C: per-strategy teardown ----------------
    kill "$POD_POLL_PID" 2>/dev/null || true
    wait "$POD_POLL_PID" 2>/dev/null || true
    kill "$PF_PID" 2>/dev/null || true
    "$SCRIPT_DIR/setup_social_network.sh" uninstall-prometheus || true

    mazu_echo "=== $STRAT complete at $(date) ==="
done

mazu_echo "=== DONE ==="
echo "Summary: $SUMMARY"
column -s, -t < "$SUMMARY"
