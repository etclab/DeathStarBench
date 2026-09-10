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
#   RESET_BETWEEN_RPS
#                 - off (default) | data | restart. Controls whether each RPS
#                   step starts from the state the previous one left behind, or
#                   from the state the arm was seeded into. See THE SWEEP IS
#                   CUMULATIVE below. 0/no are accepted as off, 1/yes as data.
#                     off      one continuous sweep: every step inherits the
#                              previous step's data, caches and (under HPA=1)
#                              fleet. This is the saturation curve.
#                     data     between steps, empty the datastores and re-seed
#                              them, so every step opens on the same dataset.
#                     restart  additionally roll every application deployment
#                              first, so the app and sidecar processes are
#                              fresh too. NEITHER MODE REINSTALLS THE MESH --
#                              istiod and the gateway are the thing under test
#                              and stay up for the whole arm.
#   RESET_QUIESCE - seconds to idle after a reset before the next wrk2 step
#                   (default 90). collect_metrics_sn.sh queries
#                   rate(...[1m]), so a step starting less than a minute after
#                   the re-seed charges the seed's CPU to itself.
#   MONGO_USER / MONGO_PASSWORD
#                 - credentials the reset uses to reach mongos on a sharded
#                   substrate (default root / password). Keep in sync with
#                   global.mongodb.sharding.svc in sn-mongodb-sharded.yaml.
#   HPA           - 1 to run the REPLICA-CHURN experiment: every autoscaled
#                   deployment starts at 1 and grows under load via
#                   scratch/yaml/sn-hpa.yaml, so the comparison is about how
#                   each mesh behaves during scale-up. Default 0 = fixed
#                   fleet (steady-state latency comparison).
#                   Use a monotonically increasing RPS_VALUES with this, and a
#                   longer DURATION (240s) so each step converges.
#   SETTLE_MIN     - HPA=1 only: minimum seconds after the HPAs are applied
#                   before the fleet is allowed to count as settled
#                   (default 420). Covers HPA reaction time plus the
#                   scaleDown stabilization window. See the note at that
#                   wait loop for why a bare at-floor check is not enough.
#   SETTLE_TIMEOUT - HPA=1 only: max seconds to wait for the fleet to reach
#                   the HPA floor before starting the sweep anyway with a
#                   warning (default 1200; raised if <= SETTLE_MIN).
#   GW_REPLICAS   - ingress gateway fleet size (default: 10)
#   ISTIOD_REPLICAS / ISTIOD_CPU / ISTIOD_MEM
#                 - istiod sizing forced onto the istio arm so it matches what
#                   the mazu operator overlays give istiod
#                   (default: 1 / 2000m / 2Gi)
#   ISTIOD_NODE   - hostname the istio arm's istiod is pinned to, so it runs on
#                   the same node as the mazu arms' istiod (default: node-1,
#                   the only control-plane node advertising the TPM). Must be a
#                   Ready node; checked at startup.
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
#   * THE SWEEP IS CUMULATIVE UNLESS YOU ASK OTHERWISE. With the default
#     RESET_BETWEEN_RPS=off the RPS values are one continuous run against one
#     application instance, and each step hands the next one:
#
#       DATA. mixed-workload.lua composes posts (~10% of its mix) and nothing
#         ever removes them. A 240 s step at 1000 RPS adds ~24k posts to a
#         dataset the seed left at ~10k, so by the time the 2000 RPS step runs
#         it is reading timelines an order of magnitude longer than the 100 RPS
#         step read. Dataset size is then a second variable moving in lockstep
#         with the one under test, and the two cannot be separated after the
#         fact.
#       CACHES. memcached and the redis timelines are warm, and warm in a way
#         that depends on what the previous step happened to touch.
#       FLEET (HPA=1 only). Replicas added at 1400 RPS are still there when the
#         1600 RPS step starts, and the scaleDown stabilization window keeps
#         them there. Under HPA=1 the ramp IS the experiment, so this is
#         deliberate -- but it does mean a step's fleet is a function of every
#         step before it.
#
#     That is the right shape for a saturation curve and the wrong shape for
#     comparing individual operating points, or for running the RPS values in
#     any order other than ascending. RESET_BETWEEN_RPS=data|restart restores
#     the post-seed state between steps instead. It is not free: the re-seed
#     costs a few minutes per step, and under HPA=1 every step then pays the
#     full SETTLE_MIN dwell again (~7 min at the default).
#
#     WITH A RESET, RPS_VALUES NEED NOT BE MONOTONIC -- and interleaving or
#     shuffling them is the cheapest test that the reset actually works, since
#     any surviving carryover shows up as an order effect.
#
#   * HOW THE DATASTORES ARE RESTORED, AND WHY NOT mongorestore. The reset
#     empties all three storage tiers and re-runs scripts/init_social_graph.py:
#
#       mongo      deleteMany({}) over the six db.collection pairs listed in
#                  templates/hooks/mongodb/configmap.yaml. DOCUMENTS, not the
#                  collections: dropping a collection takes its hashed shard
#                  key and index with it, and the only thing that puts those
#                  back is the post-install hook -- which helm will not run
#                  again. The arm would keep going, quietly, on a substrate it
#                  was not installed with.
#       redis      redis-cli flushall on every redis pod.
#       memcached  the pods are deleted. There is no client in the memcached
#                  image (upstream is debian-slim: no nc, and no bash either,
#                  so not even the /dev/tcp trick), mcrouter does not reliably
#                  broadcast flush_all, and nothing in this chart gives
#                  memcached a volume -- so a fresh process is by construction
#                  an empty cache. The cache MUST go with the databases:
#                  user-service answers registration out of memcached before it
#                  reads mongo, so a surviving entry makes the re-seed report
#                  "username already exists" against an empty user collection,
#                  and the arm silently continues with a half-populated graph.
#
#     A mongodump/mongorestore snapshot would be faster than re-seeding through
#     the API, and was rejected on three counts: mongorestore --drop recreates
#     the collection unsharded (the same trap as above); the database tools are
#     not present in every image this chart can be pinned to
#     (bitnamilegacy/mongodb-sharded vs library/mongo); and it restores mongo
#     ONLY, so redis and memcached would still need the API path to be
#     repopulated consistently with it. Re-seeding is the same code path, with
#     the same random.seed(1), that produced the state step 1 ran on, and it is
#     self-checking -- check_timelines runs again after every re-seed and its
#     verdict lands in timelines-<rps>.txt.
#
#   * HPA=1 MAKES THE FLEET A DEPENDENT VARIABLE. Read this before reading
#     anything into an HPA run.
#
#     HPA=1 asks "how does each mesh behave while the fleet grows under it".
#     It cannot cleanly answer that question, because the loop it closes runs
#     through the thing under test:
#
#         target RPS -> [mesh] -> delivered RPS -> app CPU -> HPA -> pods
#                          ^                                          |
#                          +------------------------------------------+
#
#     sn-hpa.yaml scales on APP-container CPU, and app CPU is a function of
#     the load that actually REACHES the app. A mesh that is slow delivers
#     less load, so its app containers do less work, so the HPA hands it FEWER
#     pods -- and the smaller fleet makes it slower still. The failing arm is
#     rewarded with a smaller fleet. That confound is documented at length in
#     scratch/yaml/sn-hpa.yaml under "APP CPU IS ONLY MESH-INDEPENDENT AT
#     EQUAL THROUGHPUT".
#
#     Measured on results/sn-strategies-09-03-26_203755, from 1400 RPS up, the
#     Mazu/Istio app-CPU ratio tracks the Mazu/Istio DELIVERED-THROUGHPUT ratio
#     to within ~2 points at every step (0.818/0.837 at 1400, 0.677/0.692 at
#     1600, 0.582/0.590 at 1800). The HPA was not mismeasuring anything -- it
#     was faithfully scaling on work that never arrived. Mazu's fleet flatlined
#     at 44-45 pods while Istio's climbed to 50.
#
#     SO AN HPA SWEEP COMPARES OPERATING POINTS THE ARMS DID NOT SHARE. It is
#     the right shape for "how does each mesh behave while the fleet grows
#     under it" and the wrong shape for attributing a latency or CPU gap to
#     the data plane, since the arms were not running on the same fleet. If
#     you want the autoscaling story told honestly -- "Mazu needed N% more
#     pods to hold the SLO" -- scale on OUTSTANDING-REQUEST DEPTH rather than
#     CPU or completion rate: queue depth RISES when a mesh chokes, so it
#     pushes the failing arm toward more pods instead of fewer. Run that as a
#     separate figure against an explicit SLO.
#
#     AN HPA SWEEP IS ALSO CUMULATIVE IN THE FLEET -- replicas added at 1400
#     RPS are still there at 1600 -- so RPS_VALUES has to ascend under HPA=1.
#
#   * MESH SIZING MUST MATCH ACROSS ARMS, on BOTH the gateway and istiod. The
#     mazu arms get theirs from scratch/yaml/istio-operator*.yaml;
#     `install-istio` applies the stock default profile, so the istio arm is
#     patched to match. Two separate knobs:
#
#       gateway  overlay pins 10 replicas with anti-control-plane affinity;
#                stock is an HPA of 1..5 with no affinity. Unpatched, the istio
#                baseline saturates its gateway and the comparison measures
#                gateway starvation rather than the data plane. Same reasoning
#                as bench1.5b.
#
#       istiod   overlay pins ONE replica (hpaSpec.maxReplicas: 1) with a 2000m
#                request, on a control-plane node; stock is an HPA of 1..5 at
#                80% CPU with a 500m request and no nodeSelector at all.
#                Unpatched, the istio arm can grow its control plane by 4
#                replicas, trips its HPA at 80% of 500m instead of 80% of
#                2000m, and lands istiod on a worker where it competes with the
#                application for CPU. The replica ceiling bites hardest under
#                HPA=1, where replica churn is itself an xDS-push workload --
#                istio would scale out of the pressure mazu is structurally
#                forbidden from escaping.
#
#     THE ISTIOD PATCH PINS BY HOSTNAME, NOT BY THE CONTROL-PLANE LABEL. The
#     two control-plane nodes are not interchangeable: node-0 carries the
#     NoSchedule control-plane taint, node-1 does not, so node-1 also runs
#     ordinary application workload while node-0 stays essentially idle. A
#     label selector picks "a control-plane node" and the istio arm was
#     observed landing on node-0 -- an idle node -- while the mazu TPM arms
#     have no choice but node-1 (the only one of the two advertising
#     tpm.boxboat.io/tpmrm). istiod CPU would then be compared across two
#     different neighbourhoods. ISTIOD_NODE (default node-1) closes that gap.
#     RES_DIR/istiod-node.txt records where it actually landed per arm --
#     check it before reading anything into an istiod CPU difference.
#
#     STILL ASYMMETRIC: the NON-TPM mazu arms (st4-AudUpd) install from
#     scratch/yaml/istio-operator.yaml, which selects on the control-plane
#     label and requests no TPM -- so their istiod can still land on either
#     node. Not in the default STRATEGIES set. If you add it, pin it there too.
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
RPS_VALUES=(${RPS_VALUES:-100 200 400 600 800 1000 1200 1400 1600 1800 2000})
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

# How long to wait, under HPA=1, for the seed-inflated fleet to shrink back to
# the HPA floor before starting the sweep. Must comfortably exceed the HPA's
# scaleDown stabilization window (kubernetes default 300s, and sn-hpa.yaml does
# not override it) plus the time the surplus takes to drain.
# SETTLE_MIN is the floor on that wait, not just a timeout: the fleet is still
# AT the HPA floor when the HPAs are applied (seeding runs before they exist),
# so an at-floor check is meaningless until the HPAs have had time to scale up
# AND to come back down. It must exceed the scaleDown stabilization window
# (kubernetes default 300s) plus the HPA's reaction time (~50s observed).
SETTLE_MIN=${SETTLE_MIN:-420}
SETTLE_TIMEOUT=${SETTLE_TIMEOUT:-1200}
# A timeout shorter than the minimum dwell would make the wait unsatisfiable.
if [[ "$SETTLE_TIMEOUT" -le "$SETTLE_MIN" ]]; then
    SETTLE_TIMEOUT=$(( SETTLE_MIN + 300 ))
fi

# Whether each RPS step starts from the state the previous one left behind
# (off, the continuous sweep) or from the state the arm was seeded into
# (data / restart). See THE SWEEP IS CUMULATIVE in the header for what
# actually carries over and what it costs to stop it.
#
# Validated here rather than at first use: this is a multi-hour unattended
# run, and a typo that silently fell through to "off" would produce a full set
# of results answering a different question than the one that was asked.
RESET_BETWEEN_RPS="${RESET_BETWEEN_RPS:-off}"
case "$RESET_BETWEEN_RPS" in
    0|no|none|off) RESET_BETWEEN_RPS=off ;;
    1|yes|data)    RESET_BETWEEN_RPS=data ;;
    restart)       ;;
    *) echo "ERROR: unknown RESET_BETWEEN_RPS='$RESET_BETWEEN_RPS' (expected off, data or restart)"; exit 1 ;;
esac

# Idle time between the end of a reset and the start of the next wrk2 step.
# NOT cosmetic: collect_metrics_sn.sh's CPU queries are rate(...[1m]), and
# that window reaches BACKWARDS from each sample -- so a step that starts less
# than 60s after the re-seed finishes has the seed's CPU inside its own first
# minute of samples. The re-seed is a heavy write workload, so that shows up
# as the step apparently opening at high CPU with no load to explain it.
RESET_QUIESCE=${RESET_QUIESCE:-90}

# Credentials for mongos on the sharded substrates. Defaults match
# global.mongodb.sharding.svc in scratch/yaml/sn-mongodb-sharded.yaml; the
# standalone substrate runs mongod without auth and ignores these.
MONGO_USER="${MONGO_USER:-root}"
MONGO_PASSWORD="${MONGO_PASSWORD:-password}"

# Keep in sync with hpaSpec min/maxReplicas in scratch/yaml/istio-operator.yaml
# and scratch/yaml/istio-operator-tpm.yaml (both currently 10).
GW_REPLICAS=${GW_REPLICAS:-10}

# istiod sizing, applied to the ISTIO arm to match what the Mazu operator
# overlays give istiod. Keep in sync with components.pilot.k8s in
# scratch/yaml/istio-operator.yaml and istio-operator-tpm.yaml.
ISTIOD_REPLICAS=${ISTIOD_REPLICAS:-1}
ISTIOD_CPU="${ISTIOD_CPU:-2000m}"
ISTIOD_MEM="${ISTIOD_MEM:-2Gi}"

# The node the istio arm's istiod is pinned to, BY HOSTNAME. A control-plane
# label is not specific enough: node-0 and node-1 are both control-plane but
# are not equivalent neighbourhoods (node-0 carries the NoSchedule taint and
# stays idle, node-1 does not and runs ordinary workload). The mazu TPM arms
# have no choice -- only node-1 of the two advertises tpm.boxboat.io/tpmrm --
# so the istio arm has to be pinned to the same host or the two istiods are
# measured under different neighbours. See the placement note in the header.
ISTIOD_NODE="${ISTIOD_NODE:-node-1}"

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
# The `reset` column says what state each step opened on, so a reader can tell
# a continuous sweep from a set of independent operating points WITHOUT having
# to know how the run was invoked -- and can spot the one step whose reset
# failed. Values: off (cumulative), seed (the first step of a reset run, whose
# state came from the arm's own install-and-seed), data / restart (this step
# was reset that way), FAILED:<mode> (a reset was attempted and did not
# complete -- that step is NOT independent of the one before it).
# Appended at the END: every consumer reads summary.csv with csv.DictReader,
# so a trailing column is invisible to the older ones.
#
# `fleet` says how the replica count for the step was DECIDED, which is the
# difference between two experiments that otherwise produce identical-looking
# rows. Values: fixed (the chart's pinned counts) or hpa (autoscaled -- the
# fleet is a DEPENDENT variable and a saturated arm gets fewer pods; see
# HPA=1 MAKES THE FLEET A DEPENDENT VARIABLE in the header).
echo "strategy,rps_target,rps_delivered,p50_ms,p99_ms,non_2xx,timeouts,timeline_overlap_pct,pods,reset,fleet" > "$SUMMARY"

mazu_echo "=== SocialNetwork: strategy comparison ==="
echo "Strategies: ${STRATEGIES[*]}"
echo "Substrate:  $SUBSTRATE"
echo "Workload:   $WORKLOAD @ ${RPS_VALUES[*]} RPS x ${DURATION}s (-t $THREADS -c $CONNS)"
echo "Gateway:    $GW_REPLICAS replicas per arm"
echo "istiod:     $ISTIOD_REPLICAS replica(s), requests cpu=$ISTIOD_CPU memory=$ISTIOD_MEM (both arms)"
echo "            pinned to $ISTIOD_NODE (istio arm; the TPM arms land there by device)"
case "$RESET_BETWEEN_RPS" in
    off)     echo "Reset:      off -- one CUMULATIVE sweep; every step inherits the previous one" ;;
    data)    echo "Reset:      data -- datastores wiped and re-seeded between RPS steps (quiesce ${RESET_QUIESCE}s)" ;;
    restart) echo "Reset:      restart -- app tier rolled, then datastores wiped and re-seeded, between RPS steps (quiesce ${RESET_QUIESCE}s)" ;;
esac
if [[ "$HPA" == "1" ]]; then
    echo "Fleet:      HPA -- autoscaled on app CPU. NOTE the fleet is then a"
    echo "            DEPENDENT variable and a saturated arm gets FEWER pods;"
    echo "            see HPA=1 MAKES THE FLEET A DEPENDENT VARIABLE in the header"
else
    echo "Fleet:      fixed at the chart's pinned replica counts"
fi
echo "Results:    $RESULTS_ROOT"

# Fail here rather than 600s into the arm. A hostname that is wrong, or a node
# that is NotReady, leaves istiod Pending forever -- which surfaces only as the
# gateway readiness timeout, with every gateway pod stuck at 0/1 because no
# sidecar can register. That looks exactly like the stale-TPM failure mode, so
# it is worth ruling out before a multi-hour run starts.
ISTIOD_NODE_READY=$(kubectl get node "$ISTIOD_NODE" \
    -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end}' 2>/dev/null)
if [[ "$ISTIOD_NODE_READY" != "True" ]]; then
    echo "ERROR: ISTIOD_NODE=$ISTIOD_NODE is not a Ready node (Ready=${ISTIOD_NODE_READY:-<missing>})."
    echo "       istiod would stay Pending and every gateway pod would sit at 0/1."
    kubectl get nodes -l node-role.kubernetes.io/control-plane 2>&1 | sed 's/^/       /'
    exit 1
fi

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

# ===========================================================================
# Per-step reset (RESET_BETWEEN_RPS). See THE SWEEP IS CUMULATIVE and HOW THE
# DATASTORES ARE RESTORED in the header for why any of this exists.
# ===========================================================================

# The six DeathStarBench databases, as db:collection. Copied from the
# collections_settings list in templates/hooks/mongodb/configmap.yaml -- which
# is the same list that gets a hashed shard key and its index, and is exactly
# why the wipe below deletes DOCUMENTS rather than dropping collections.
MONGO_COLLECTIONS=(media:media post:post social-graph:social-graph
                   url-shorten:url-shorten user:user user-timeline:user-timeline)

# Running pods carrying a label, or whose DeathStarBench `service` label
# matches an extended regex. Both filter on phase: a Terminating or Pending
# pod cannot be exec'd into, and a reset that tried would fail the whole step
# for a pod that was on its way out anyway.
pods_by_label() {
    kubectl get pods -n "$NAMESPACE" -l "$1" \
        -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\n"}{end}' 2>/dev/null
}
pods_by_service() {
    kubectl get pods -n "$NAMESPACE" -l service \
        -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels.service}{" "}{.status.phase}{"\n"}{end}' 2>/dev/null \
        | awk -v re="$1" '$3=="Running" && $2 ~ re {print $1}'
}

# `kubectl exec` needs -c on an injected pod. Pick the first container that is
# not the sidecar rather than assuming index 0: which end of the list the proxy
# is appended to is an injection implementation detail, not a contract, and it
# differs between the classic and native-sidecar injection paths.
app_container() {
    kubectl get pod -n "$NAMESPACE" "$1" \
        -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}' 2>/dev/null \
        | grep -vx 'istio-proxy' | head -1
}

# Empty the six collections, printing the deleted count per collection so the
# run log carries evidence that the wipe actually removed the previous step's
# writes rather than silently matching nothing.
#
# deleteMany({}), NOT drop(): see the header. A dropped collection comes back
# unsharded and unindexed and only the helm post-install hook restores that.
wipe_mongo() {
    local rc=0 js="" pair db coll p
    for pair in "${MONGO_COLLECTIONS[@]}"; do
        db="${pair%%:*}"; coll="${pair#*:}"
        js+="try{var r=db.getSiblingDB(\"$db\").getCollection(\"$coll\").deleteMany({});print(\"    $db.$coll removed \"+r.deletedCount);}catch(e){print(\"    $db.$coll ERROR \"+e);}"
    done

    # Two shapes, one statement. Sharded: a single mongos fronts all six
    # databases and wants the root credentials. Standalone: six separate
    # mongod pods with no auth, each holding exactly ONE of the six -- running
    # the whole statement against each is harmless, the other five simply do
    # not exist there, and it keeps this free of a substrate lookup table.
    #
    # `command -v mongosh` first because the two images disagree: MongoDB 8
    # (bitnamilegacy/mongodb-sharded) ships only mongosh, library/mongo:4.4.6
    # ships only the legacy mongo shell.
    local mongos
    mongos=$(pods_by_label 'app.kubernetes.io/component=mongos' | head -1)
    if [[ -n "$mongos" ]]; then
        echo "  mongo: emptying ${#MONGO_COLLECTIONS[@]} collections via mongos $mongos"
        kubectl exec -n "$NAMESPACE" "$mongos" -c "$(app_container "$mongos")" -- \
            sh -c "if command -v mongosh >/dev/null 2>&1; then MSH=mongosh; else MSH=mongo; fi; \
                   \$MSH --quiet -u '$MONGO_USER' -p '$MONGO_PASSWORD' \
                        --authenticationDatabase admin localhost:27017/admin \
                        --eval '$js'" || rc=1
    else
        local n=0
        for p in $(pods_by_service '-mongodb$'); do
            echo "  mongo: emptying collections in $p"
            kubectl exec -n "$NAMESPACE" "$p" -c "$(app_container "$p")" -- \
                sh -c "if command -v mongosh >/dev/null 2>&1; then MSH=mongosh; else MSH=mongo; fi; \
                       \$MSH --quiet localhost:27017/admin --eval '$js'" || rc=1
            n=$((n+1))
        done
        # No mongos AND no standalone mongod means the selector is wrong, not
        # that there is nothing to wipe. Failing loudly here is the difference
        # between a broken reset and a run that looks reset and is not.
        [[ "$n" -gt 0 ]] || { warn "reset: found no mongo pods to wipe"; rc=1; }
    fi
    return $rc
}

wipe_redis() {
    local rc=0 p n=0
    # Standalone (the all-correct and baseline substrates): three plain
    # redis:6.2.4 pods -- home-timeline, user-timeline, social-graph -- no auth.
    for p in $(pods_by_service '-redis$'); do
        kubectl exec -n "$NAMESPACE" "$p" -c "$(app_container "$p")" -- \
            redis-cli flushall >/dev/null || rc=1
        n=$((n+1))
    done
    # SUBSTRATE=all: bitnami redis-cluster (usePassword: false). FLUSHALL is a
    # per-NODE command, not a cluster-wide one, so every pod gets it and the
    # replicas answer -READONLY. Their failure is expected and deliberately
    # not counted -- flushing the masters is what empties the keyspace.
    for p in $(pods_by_label 'app.kubernetes.io/name=redis-cluster'); do
        kubectl exec -n "$NAMESPACE" "$p" -c "$(app_container "$p")" -- \
            redis-cli flushall >/dev/null 2>&1 || true
        n=$((n+1))
    done
    echo "  redis: flushed $n pod(s)"
    [[ "$n" -gt 0 ]] || { warn "reset: found no redis pods to flush"; rc=1; }
    return $rc
}

# Delete the memcached pods. This is a wipe, not a restart for its own sake:
# nothing in this chart gives memcached a volume, so a fresh process is by
# construction an empty cache -- and there is no in-image client to ask more
# politely (upstream memcached is debian-slim: no nc, and no bash either, so
# not even the /dev/tcp trick), while mcrouter does not reliably broadcast
# flush_all across its pool.
#
# `delete pod`, not `rollout restart`: a StatefulSet rollout is serial by
# ordinal, so the six mcrouter backends would restart one at a time and each
# would wait out its sidecar registration. Deleting them together is the same
# outcome in a fraction of the wall clock, and mcrouter reconnects by DNS to
# StatefulSet pod names that do not change.
wipe_memcached() {
    local pods
    pods=$( { pods_by_service 'memcached'; pods_by_label 'app.kubernetes.io/name=memcached'; } | sort -u )
    [[ -n "$pods" ]] || { warn "reset: found no memcached pods to drop"; return 1; }
    echo "  memcached: dropping $(printf '%s\n' "$pods" | wc -l | tr -d ' ') pod(s) to empty the cache"
    printf '%s\n' "$pods" | xargs -r kubectl delete pod -n "$NAMESPACE" \
        --wait=false --ignore-not-found >/dev/null || return 1
    return 0
}

# Roll the application tier so every app process AND every sidecar starts
# fresh -- the extra that `restart` mode buys over `data`. It matters for this
# comparison specifically: Mazu's sidecar accumulates per-connection state that
# plain Istio's does not, so "the sidecar has already served 1.4M requests" is
# a difference between the arms that a data-only reset leaves in place.
#
# Datastores are excluded on purpose. They are wiped explicitly instead, and
# rolling them would be strictly worse: the sharded mongo tier would lose the
# per-collection shard keys the post-install hook set up, and helm will not run
# that hook again.
restart_app_pods() {
    local d rc=0
    local -a names=()
    while read -r d; do [[ -n "$d" ]] && names+=("$d"); done < <(
        kubectl get deploy -n "$NAMESPACE" -l service \
            -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
            | grep -Ev -- '-(mongodb|redis|memcached)$' || true )
    [[ "${#names[@]}" -gt 0 ]] || { warn "reset: found no application deployments to roll"; return 1; }
    echo "  rolling ${#names[@]} application deployment(s)"
    for d in "${names[@]}"; do
        kubectl rollout restart deploy "$d" -n "$NAMESPACE" >/dev/null || rc=1
    done
    # Restart them all first, then wait: rolling 20 deployments in series would
    # take twenty sidecar registrations end to end.
    for d in "${names[@]}"; do
        kubectl rollout status deploy "$d" -n "$NAMESPACE" --timeout=900s >/dev/null \
            || { warn "rollout of $d did not complete"; rc=1; }
    done
    return $rc
}

# Hand the autoscaled fleet back to its floor.
#
# THE HPAs HAVE TO BE DELETED, NOT JUST SCALED AGAINST. Kubernetes' scaleDown
# stabilization takes the MAXIMUM recommendation over its trailing window
# (default 300s, which sn-hpa.yaml does not override), so an HPA that has just
# watched a 1400 RPS step will undo a `kubectl scale` on its very next sync.
# Delete, scale down, re-apply: that is also what makes settle_hpa_floor's
# dwell mean the same thing before step 7 that it meant before step 1, since
# the dwell is measured from the moment the HPAs are (re-)applied.
hpa_reset_to_floor() {
    local targets t floor
    targets=$(kubectl get hpa -n "$NAMESPACE" \
        -o jsonpath='{range .items[*]}{.spec.scaleTargetRef.name}{" "}{.spec.minReplicas}{"\n"}{end}' 2>/dev/null \
        | awk 'NF==2')
    kubectl delete -f "${YAML}/sn-hpa.yaml" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
    printf '%s\n' "$targets" | while read -r t floor; do
        [[ -n "$t" ]] || continue
        kubectl scale deploy "$t" -n "$NAMESPACE" --replicas="${floor:-1}" >/dev/null 2>&1 || true
    done
    echo "  scaled $(printf '%s\n' "$targets" | grep -c . || true) autoscaled deployment(s) back to their floor"
}

# Seed the social graph through the public API.
#
# `set -o pipefail` is on, so a seeding failure -- or a `grep` that filters
# every line away -- would otherwise abort the whole sweep instead of just the
# arm or step that failed. Report it and let the caller decide.
seed_app() {
    local dir="$1" tag="${2:-seed}" ok=1
    mazu_echo "Seeding social graph from $GRAPH..."
    ( cd "$SCRIPT_DIR" && python3 scripts/init_social_graph.py \
        --graph="$GRAPH" --ip="$INGRESS_IP" --port="$INGRESS_PORT" --compose ) \
        > "$dir/${tag}.txt" 2>&1 || ok=0
    grep -Ev '^[0-9]+$' "$dir/${tag}.txt" | tail -8 || true
    [[ "$ok" == "1" ]]
}

# Sets OVERLAP, which becomes summary.csv's timeline_overlap_pct column.
check_overlap() {
    local dir="$1" tag="${2:-timelines}"
    mazu_echo "Checking timeline correctness..."
    check_timelines "$ADDR" "$dir/${tag}.txt"
    OVERLAP=$(sed -n 's/^OVERLAP_PCT=//p' "$dir/${tag}.txt" | tail -1)
    [[ -n "$OVERLAP" ]] || OVERLAP="nan"
}

wait_frontend() {
    local code="" i
    mazu_echo "Waiting for the frontend to answer..."
    for i in $(seq 1 60); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$ADDR/" || true)
        [[ "$code" == "200" ]] && return 0
        sleep 5
    done
    warn "gateway never returned 200 (last: ${code:-none})"
    return 1
}

# One return to the post-seed state, run BETWEEN two RPS steps.
#
# THE ORDER IS NOT ARBITRARY:
#   1. drop the HPAs and scale back to the floor first, so nothing is being
#      resized while the datastores are emptied and so the roll below is not
#      fighting an autoscaler.
#   2. roll the app tier (restart mode only) BEFORE the wipe: a pod that is
#      still serving traffic would repopulate the caches from a database that
#      is halfway emptied.
#   3. empty all three storage tiers -- caches with the databases, always; see
#      the memcached note in the header for what a surviving cache entry does
#      to the re-seed.
#   4. re-seed through the API and re-check the timelines. The verdict is
#      recorded per step, so a re-seed that half-failed is visible in the
#      results rather than only in the log.
#   5. re-arm the HPAs and pay the settle dwell again (HPA=1 only).
#   6. quiesce, so the step's own rate(...[1m]) windows do not open on the
#      re-seed's CPU.
#
# Returns non-zero if any of that did not complete. The caller records that in
# summary.csv rather than skipping the step: a contaminated point that is
# LABELLED contaminated is still worth having, and dropping it silently would
# leave a gap in the curve with no explanation attached.
reset_between_steps() {
    local dir="$1" rps="$2" rc=0 t0
    t0=$(date +%s)
    mazu_echo "--- reset before ${rps} RPS (mode: $RESET_BETWEEN_RPS) ---"

    if [[ "$HPA" == "1" ]]; then
        hpa_reset_to_floor
    fi

    if [[ "$RESET_BETWEEN_RPS" == "restart" ]]; then
        restart_app_pods || rc=1
    fi

    wipe_memcached || rc=1
    wipe_mongo     || rc=1
    wipe_redis     || rc=1

    wait_ready 1800 || rc=1
    wait_frontend   || rc=1

    if [[ "$rc" == "0" ]]; then
        seed_app "$dir" "seed-${rps}" || rc=1
    else
        # Seeding into a cluster that is not fully up produces a partial graph
        # that LOOKS seeded. Better to leave it empty and let check_overlap say
        # so than to hand the step a dataset nobody can characterise.
        warn "skipping the re-seed: the cluster did not come back cleanly"
    fi
    check_overlap "$dir" "timelines-${rps}"

    if [[ "$HPA" == "1" ]]; then
        settle_hpa_floor "$dir" "hpa-floor-${rps}" || rc=1
    fi

    # The Prometheus port-forward has to survive the whole arm, and a reset run
    # makes an arm several times longer -- long enough that `kubectl
    # port-forward` dropping (it does: an apiserver restart, or the connection
    # simply going idle across a 15-minute reset) stops being theoretical.
    # Every step after that would collect nothing and say so only in a warning
    # buried in the log, while summary.csv kept filling in. Deliberately NOT
    # counted towards rc: this is the harness, not the state under test, and a
    # step with sound latency numbers and no CPU series should not be labelled
    # contaminated.
    if ! curl -sf "${PROM_URL}/-/ready" >/dev/null 2>&1; then
        warn "Prometheus port-forward is gone -- re-establishing it"
        kill "$PF_PID" 2>/dev/null || true
        kubectl port-forward -n istio-system svc/prometheus "${PROM_PORT}:9090" >/dev/null 2>&1 &
        PF_PID=$!
        sleep 5
        curl -sf "${PROM_URL}/-/ready" >/dev/null 2>&1 \
            || warn "Prometheus still unreachable at $PROM_URL -- metrics for the next steps will be empty"
    fi

    echo "  quiescing ${RESET_QUIESCE}s before the step"
    sleep "$RESET_QUIESCE"
    echo "  reset before ${rps} RPS took $(( $(date +%s) - t0 ))s (rc=$rc)"
    return "$rc"
}

# Apply the HPAs and block until the fleet is genuinely idle at their floor.
# Placement is the whole experiment. Seeding with init_social_graph.py
# --compose is a heavy write workload: with the HPAs already live it drove
# the fleet from 27 pods to 163 against a ceiling of 165 BEFORE the first
# wrk2 step, so the RPS sweep had no scaling range left and traced a flat
# line -- measuring nothing, which is the entire failure this mode exists
# to fix. Seed on the fixed floor, then hand the settled fleet to the HPAs
# so every replica added afterwards is attributable to benchmark load.
#
# Factored out of the arm setup so RESET_BETWEEN_RPS can pay the same dwell
# again between steps. Under a reset the fleet has to be handed back to the
# HPAs in the same condition it was in before the first step, or a "reset"
# run would still carry the previous step's replicas into the next one --
# which is most of what makes an HPA sweep cumulative in the first place.
settle_hpa_floor() {
    local dir="$1" tag="${2:-hpa-initial}"
    local HPA_APPLIED_AT UNKNOWN FLOOR_DEADLINE FLOOR_EARLIEST PEAK_ABOVE
    local NOW HPA_STATE SEEN ABOVE DWELL_LEFT i
    mazu_echo "Enabling HPAs (post-seed, pre-step)..."
    kubectl apply -f "${YAML}/sn-hpa.yaml" -n "$NAMESPACE" >/dev/null
    HPA_APPLIED_AT=$(date +%s)

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
    #
    # POLL FOR THE FLOOR -- DO NOT SLEEP A FIXED INTERVAL. sn-hpa.yaml
    # defines only a scaleUp behavior block, so kubernetes applies its
    # DEFAULT scaleDown stabilizationWindowSeconds of 300: for five minutes
    # after the seed inflates the fleet, it physically cannot shrink, no
    # matter how idle the cluster goes. The 120s sleep this replaces was
    # structurally incapable of outlasting that window.
    #
    # Measured on 2026-09-02 (istio, HPA=1): seeding drove the service tier
    # from its floor of 13 up to 37 pods. The sweep began 140s later with
    # the fleet still at 37, and it collapsed to 23 at t=210s INTO the first
    # RPS step -- a 14-pod scale-down in the middle of a measurement window,
    # caused by seed decay rather than by benchmark load. That step also
    # logged 4243 wrk2 timeouts against just 37 at FOUR TIMES the rate,
    # because connections were being dropped by pods terminating underneath
    # the load. The first data point of every HPA run was being corrupted.
    #
    # So wait until every autoscaled deployment is actually back at its
    # floor. Timing out here is a warning rather than a skip: a contaminated
    # first step is still worth collecting as long as it is labelled.
    # MINIMUM DWELL FIRST, THEN THE FLOOR CHECK -- THE ORDER IS THE WHOLE
    # POINT. Seeding runs BEFORE the HPAs exist, so the fleet is still
    # pinned at its 1-replica floor at the moment they are applied. A poll
    # that only asks "is everything at minReplicas?" therefore answers YES
    # on its first iteration, breaks instantly, and lets the sweep start
    # just as the HPAs begin reacting to the residual seed CPU. Measured on
    # 2026-09-02: that naive version logged zero above-floor samples, then
    # hpa-initial.txt showed 3 replicas at 50s of age and the sweep opened
    # on 54 pods -- indistinguishable from the 55 of the unfixed run, and a
    # SHORTER effective settle than the fixed sleep it replaced. The fleet
    # had not returned to the floor; it had not yet left it.
    #
    # So refuse to accept "at floor" until SETTLE_MIN has elapsed since the
    # HPAs were applied. That has to cover the HPA's reaction time (~50s
    # observed) plus the full scaleDown stabilization window (kubernetes
    # default 300s, which sn-hpa.yaml does not override), measured from the
    # last elevated recommendation rather than from the apply.
    echo "Waiting for the post-seed fleet to settle at the HPA floor..."
    # (Also runs between RPS steps under RESET_BETWEEN_RPS: "post-seed" is
    # accurate there too -- the reset re-seeds before re-arming the HPAs.)
    echo "  minimum dwell ${SETTLE_MIN}s, timeout ${SETTLE_TIMEOUT}s (from HPA apply)"
    FLOOR_DEADLINE=$(( HPA_APPLIED_AT + SETTLE_TIMEOUT ))
    FLOOR_EARLIEST=$(( HPA_APPLIED_AT + SETTLE_MIN ))
    PEAK_ABOVE=0
    while :; do
        NOW=$(date +%s)
        HPA_STATE=$(kubectl get hpa -n "$NAMESPACE" \
            -o jsonpath='{range .items[*]}{.status.currentReplicas}{" "}{.spec.minReplicas}{"\n"}{end}' 2>/dev/null \
            | awk 'NF==2')
        SEEN=$(printf '%s\n' "$HPA_STATE" | grep -c . || true)
        ABOVE=$(printf '%s\n' "$HPA_STATE" | awk '$1>$2' | wc -l | tr -d ' ')
        # SEEN is checked as well as ABOVE: a kubectl that returned nothing
        # (transient API error, or currentReplicas not yet populated on
        # freshly created HPAs) yields ABOVE=0, which would otherwise read
        # as "already at floor", break instantly, and silently reintroduce
        # the very contamination this loop exists to prevent. No data is
        # not the same as good data -- keep waiting instead.
        [[ "${ABOVE:-0}" -gt "$PEAK_ABOVE" ]] && PEAK_ABOVE="$ABOVE"
        if [[ "$NOW" -ge "$FLOOR_EARLIEST" && "${SEEN:-0}" -gt 0 && "${ABOVE:-1}" -eq 0 ]]; then
            echo "  fleet is at the HPA floor after $(( NOW - HPA_APPLIED_AT ))s (peak above-floor: ${PEAK_ABOVE})"
            break
        fi
        if [[ "$NOW" -ge "$FLOOR_DEADLINE" ]]; then
            warn "$ABOVE deployment(s) still above the HPA floor after ${SETTLE_TIMEOUT}s"
            warn "  the next RPS step will measure a fleet that is still shrinking"
            # jsonpath, not `get hpa --no-headers` + awk: with two metrics
            # the TARGETS column renders as "32%/70%, 2%/70%" -- one value
            # containing a SPACE, which shifts every column after it and
            # makes positional fields point at the wrong things.
            kubectl get hpa -n "$NAMESPACE" \
                -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.currentReplicas}{" "}{.spec.minReplicas}{"\n"}{end}' 2>/dev/null \
                | awk 'NF==3 && $2>$3 {print "    " $1 " at " $2 " (floor " $3 ")"}' || true
            break
        fi
        DWELL_LEFT=$(( FLOOR_EARLIEST > NOW ? FLOOR_EARLIEST - NOW : 0 ))
        echo "  $(date +%T) above-floor=${ABOVE}/${SEEN} dwell_left=${DWELL_LEFT}s"
        sleep 10
    done

    # currentReplicas reaching the floor means the SCALE-DOWN DECISION has
    # landed, not that the surplus pods are gone -- they are still
    # Terminating, and the 1 Hz poller counts them by label regardless of
    # phase. Give them a moment to actually drain so pods.csv opens on a
    # settled fleet.
    sleep 30
    kubectl get hpa -n "$NAMESPACE" > "$dir/${tag}.txt" 2>&1 || true
    kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
        | grep -Ecv "$READY_EXCLUDE" | sed 's/^/  fleet at step start: /'
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
            echo "$STRAT,TPM_RECREATE_FAILED,,,,,,,,," >> "$SUMMARY"
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
        echo "$STRAT,CONFIGMAP_FAILED,,,,,,,,," >> "$SUMMARY"; continue
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

            # Match the mazu arms' istiod sizing -- same reasoning as the
            # gateway fleet above, applied to the control plane.
            #
            # The stock default profile gives istiod an HPA of 1..5 at 80% CPU
            # and a 500m request; the operator overlays pin it to a single
            # replica with a 2000m request. Left alone, the istio arm can add
            # up to four more istiod replicas -- and trips its HPA at 80% of
            # 500m rather than 80% of 2000m, so it is ~4x more scale-happy per
            # absolute core. That matters most in HPA=1 mode, where replica
            # churn IS an xDS-push workload: the istio arm would grow its
            # control plane during the one experiment built to stress it while
            # mazu, capped at maxReplicas 1 by the overlay, structurally cannot.
            # The comparison would then measure control-plane headroom rather
            # than the data plane.
            #
            # Placement is mirrored too: stock istiod has no nodeSelector and
            # lands on a worker, where it competes with application pods for
            # CPU, while the mazu arms' istiod sits on a control-plane node.
            # Pinned BY HOSTNAME rather than by the control-plane label,
            # because the two control-plane nodes are not interchangeable --
            # see ISTIOD_NODE and the placement note in the header.
            kubectl -n istio-system patch hpa istiod --type=merge \
                -p "{\"spec\": {\"minReplicas\": ${ISTIOD_REPLICAS}, \"maxReplicas\": ${ISTIOD_REPLICAS}}}" 2>/dev/null || true

            # One patch for sizing AND placement, so istiod rolls once.
            #
            # --type=strategic, NOT merge: a JSON merge patch (RFC 7386) has no
            # notion of a list merge key, so `--type=merge` would REPLACE the
            # whole containers array with this one-field entry and the API
            # server rejects it with `containers[0].image: Required value`.
            # Strategic merge patches that list by name. (The gateway patch
            # above can use merge because affinity is a map, not a list.)
            #
            # `tolerations` HAS NO MERGE KEY -- it is an atomic list even under
            # a strategic patch, so whatever is written here REPLACES what the
            # deployment had. Stock istiod ships with cni.istio.io/not-ready,
            # which lets it start before istio-cni is ready; it is restated
            # below because omitting it would silently drop it.
            #
            # The control-plane toleration is kept even though the default
            # ISTIOD_NODE (node-1) is untainted: it costs nothing there, and
            # without it pointing ISTIOD_NODE at node-0 would leave istiod
            # Pending forever against the NoSchedule taint.
            ISTIOD_PATCH=$(cat <<EOF
{"spec": {"template": {"spec": {
  "containers": [{"name": "discovery", "resources": {"requests":
      {"cpu": "${ISTIOD_CPU}", "memory": "${ISTIOD_MEM}"}}}],
  "nodeSelector": {"kubernetes.io/hostname": "${ISTIOD_NODE}"},
  "tolerations": [
    {"key": "cni.istio.io/not-ready", "operator": "Exists"},
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
  ]
}}}}
EOF
            )
            kubectl -n istio-system patch deployment istiod --type=strategic -p "$ISTIOD_PATCH" \
                || warn "could not patch istiod sizing/placement -- arm will run with default-profile values"

            kubectl -n istio-system rollout status deployment/istio-ingressgateway --timeout=300s || true
            # The resource patch rolls istiod; wait for it before the app
            # install, so no pod is injected against a terminating istiod.
            kubectl -n istio-system rollout status deployment/istiod --timeout=300s || true
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
        echo "$STRAT,MESH_INSTALL_FAILED,,,,,,,,," >> "$SUMMARY"
        continue
    fi

    # rollout status, NOT `kubectl wait -l app=...` on pods. Both deployments
    # were just patched, so a pod-selector wait LISTs the pods once and then
    # blocks on whichever ones are still Terminating from the rollout -- they
    # never go Ready, they just disappear, and the wait sits there for its full
    # timeout with no output. Observed 2026-09-10: 10 minutes of dead time on
    # an istiod pod that had been 1/1 Running the entire time. rollout status
    # tracks the Deployment's own progress and cannot latch onto a doomed pod.
    kubectl rollout status deploy/istiod -n istio-system --timeout=600s || true
    kubectl rollout status deploy/istio-ingressgateway -n istio-system --timeout=600s || true

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
        echo "$STRAT,GATEWAY_NOT_READY,,,,,,,,," >> "$SUMMARY"
        continue
    fi
    echo "istio-ingressgateway: ${GW_READY} replicas Ready"

    # Which node istiod actually landed on, and with what sizing. Both arms
    # should now report ISTIOD_NODE -- the istio arm because it is pinned
    # there by hostname, the TPM mazu arms because it is the only
    # control-plane node advertising the TPM. Recorded per arm anyway: if the
    # two ever diverge, an istiod CPU difference is a neighbourhood
    # difference, not a mesh difference. See the placement note in the header.
    kubectl -n istio-system get pods -l app=istiod \
        -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,CPU_REQ:'.spec.containers[0].resources.requests.cpu',MEM_REQ:'.spec.containers[0].resources.requests.memory' \
        > "$RES_DIR/istiod-node.txt" 2>&1 || true
    cat "$RES_DIR/istiod-node.txt" || true

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
    if [[ "$HPA" == "1" ]]; then
        HELM_ARGS+=(-f "${YAML}/sn-hpa-values.yaml")
    fi

    [[ -n "$SUBSTRATE_SETS" ]] && HELM_ARGS+=(--set "$SUBSTRATE_SETS")

    echo "helm upgrade --install $RELEASE $CHART_DIR ${HELM_ARGS[*]}" | tee "$RES_DIR/helm-cmd.txt"
    if ! helm upgrade --install "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" \
            "${HELM_ARGS[@]}" --timeout 20m0s > "$RES_DIR/helm.log" 2>&1; then
        warn "helm install FAILED for $STRAT"; tail -30 "$RES_DIR/helm.log"
        echo "$STRAT,INSTALL_FAILED,,,,,,,,," >> "$SUMMARY"; continue
    fi

    wait_ready 1800 || { kubectl get pods -n "$NAMESPACE" -o wide > "$RES_DIR/pods.txt"; \
        echo "$STRAT,NOT_READY,,,,,,,,," >> "$SUMMARY"; continue; }

    kubectl get pods -n "$NAMESPACE" -o wide > "$RES_DIR/pods.txt"
    PODCOUNT=$(kubectl get pods -n "$NAMESPACE" --no-headers | grep -cv 'nfs-subdir' || true)

    get_ingress_ip_port
    ADDR="http://${INGRESS_IP}:${INGRESS_PORT}"
    echo "Ingress: ${INGRESS_IP}:${INGRESS_PORT}"

    wait_frontend || { echo "$STRAT,NO_FRONTEND,,,,,,,,," >> "$SUMMARY"; continue; }

    # ---- Seed (always after the last install) ----
    # Seeding is not optional -- without it the benchmark measures empty
    # timelines while returning 200 -- so treat a failure as a skip. This is
    # also the state RESET_BETWEEN_RPS restores between steps, which is why it
    # goes through seed_app() rather than being inlined here: the reset must
    # reproduce THIS, byte for byte, not an approximation of it.
    if ! seed_app "$RES_DIR" seed; then
        warn "seeding FAILED for $STRAT -- skipping this arm"
        echo "$STRAT,SEED_FAILED,,,,,,,,," >> "$SUMMARY"; continue
    fi

    check_overlap "$RES_DIR" timelines

    # ---- HPAs: applied LAST, after seeding, immediately before the sweep ----
    # Placement is the whole experiment -- seeding with the HPAs already live
    # consumes the entire scaling range before the first wrk2 step. The
    # reasoning, and the measurements behind the dwell, are with the function.
    if [[ "$HPA" == "1" ]]; then
        settle_hpa_floor "$RES_DIR" hpa-initial
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
    #
    # WHY THIS DOES NOT SELECT ON `service`. The DeathStarBench chart stamps a
    # `service` label via _baseDeployment.tpl, but the datastore tier does not
    # come from that chart -- mongodb-sharded, memcached and redis-cluster are
    # upstream Bitnami subcharts (helm-chart/socialnetwork/charts/*.tgz) that
    # label with app.kubernetes.io/*, and mcrouter uses a plain `app`. A
    # `-l 'service'` selector therefore returned the 46 application pods and
    # SILENTLY omitted all 19 datastore pods -- 29% of the running fleet, and
    # the half of it that holds the state. Measured on the 09-03 12:45 run: the
    # growth chart read 46 while summary.csv read 67 for the same fleet, and
    # neither number was wrong, they were just counting different things.
    #
    # So poll every pod and DERIVE the service name, most specific label first:
    #   1. `service`                       -- DeathStarBench app tier, as before
    #   2. app.kubernetes.io/name[-component]
    #                                      -- Bitnami. The component suffix
    #                                         matters: without it mongos,
    #                                         configsvr and shardsvr collapse
    #                                         into one "mongodb-sharded" bar
    #                                         and the shard fleet is unreadable.
    #   3. `app`                           -- mcrouter
    #   4. no labels at all                -- the setup-* helm hooks. Dropped:
    #                                         they are Completed Jobs, not
    #                                         serving capacity, and counting
    #                                         them is what made summary.csv's
    #                                         pods column read 67 instead of 65.
    # nfs-subdir-external-provisioner is dropped by name for the same reason it
    # is in READY_EXCLUDE: cluster infrastructure that predates the run and is
    # not part of the workload.
    #
    # A missing label yields an empty jsonpath field rather than a dropped one,
    # so the comma positions hold and awk can pick by column. The emitted CSV
    # schema is unchanged (timestamp,service,phase,ready), so
    # summarize_sn_pods.py -- which discovers the service set from the data --
    # needs no change to pick the datastore tier up.
    POD_CSV="$RES_DIR/pods.csv"
    echo "timestamp,service,phase,ready" > "$POD_CSV"
    (
        while true; do
            ts=$(date +%s)
            kubectl get pods -n "$NAMESPACE" \
                -o jsonpath='{range .items[*]}{.metadata.name}{","}{.metadata.labels.service}{","}{.metadata.labels.app}{","}{.metadata.labels['"'"'app\.kubernetes\.io/name'"'"']}{","}{.metadata.labels['"'"'app\.kubernetes\.io/component'"'"']}{","}{.status.phase}{","}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null \
              | awk -F, -v ts="$ts" 'NF>=7 && $1 !~ /^nfs-subdir-external-provisioner/ {
                    if ($2 != "")      svc = $2
                    else if ($4 != "") svc = ($5 != "" ? $4 "-" $5 : $4)
                    else if ($3 != "") svc = $3
                    else               next
                    print ts "," svc "," $6 "," $7
                }' >> "$POD_CSV" &
            sleep 1
        done
    ) &
    POD_POLL_PID=$!

    # ---------------- Phase B: RPS sweep ----------------
    #
    # RESET_TAG is what each step reports in summary.csv's `reset` column. The
    # first step of a reset run is tagged `seed` rather than the mode name:
    # its state came from the arm's own install-and-seed above, which is
    # precisely what every later reset reproduces, so calling it "data" would
    # claim a reset that never ran.
    if [[ "$RESET_BETWEEN_RPS" == "off" ]]; then RESET_TAG="off"; else RESET_TAG="seed"; fi
    STEP_INDEX=0

    for RPS in "${RPS_VALUES[@]}"; do
        STEP_INDEX=$(( STEP_INDEX + 1 ))

        # Reset BEFORE each step, never before the first: the arm has just
        # seeded, so step 1 already IS the state the reset restores, and a
        # trailing reset after the last step would be pure cost.
        #
        # A failed reset does not skip the step. It relabels it -- a data point
        # that is known-contaminated is more useful than a hole in the curve
        # with no explanation attached to it.
        if [[ "$RESET_BETWEEN_RPS" != "off" && "$STEP_INDEX" -gt 1 ]]; then
            if reset_between_steps "$RES_DIR" "$RPS"; then
                RESET_TAG="$RESET_BETWEEN_RPS"
            else
                RESET_TAG="FAILED:$RESET_BETWEEN_RPS"
                warn "reset before ${RPS} RPS did not complete -- this step is NOT"
                warn "  independent of the one before it; summary.csv says so in the reset column"
            fi
        fi

        mazu_echo "--- $STRAT @ ${RPS} RPS for ${DURATION}s ---"
        export RPS

        if [[ "$HPA" == "1" ]]; then
            FLEET_TAG="hpa"
        else
            FLEET_TAG="fixed"
        fi

        OUT_FILE="$RES_DIR/${RPS}.txt"
        BENCH_START=$(date +%s)

        "$WRK_BIN" -D exp -t "$THREADS" -c "$CONNS" -d "$DURATION" -L \
            -s "$LUA" "$ADDR" -R "$RPS" > "$OUT_FILE" 2>&1 || warn "wrk2 exited non-zero"

        BENCH_END=$(date +%s)
        grep -E '50\.000%|99\.000%|Requests/sec|Non-2xx|Socket errors' "$OUT_FILE" || true

        awk -F, -v s="$BENCH_START" -v e="$BENCH_END" 'NR==1 || ($1>=s && $1<=e)' \
            "$POD_CSV" > "$RES_DIR/pods-${RPS}.csv"

        # collect_metrics_sn.sh, not collect_metrics.sh: the latter collects
        # only the istio-proxy sidecar. Both containers are needed here, but
        # for opposite reasons -- sn-hpa.yaml now scales on the APP container
        # alone, so that is the control input and the reason a step's fleet is
        # the size it is, while the sidecar's CPU is the OUTCOME the whole
        # comparison exists to measure. See that script's header, and the
        # WHY APP CPU ONLY note in scratch/yaml/sn-hpa.yaml.
        "$SCRIPT_DIR/collect_metrics_sn.sh" "$RES_DIR" "$DURATION" "$BENCH_START" "$RPS" \
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

        # TIMED-OUT REQUESTS ARE NOT IN THE LATENCY HISTOGRAM. wrk2 drops any
        # request that exceeds the socket timeout before it reaches the
        # histogram, so P50/P99 above describe only the requests that came
        # BACK. A step can therefore report an excellent p99 while a large
        # fraction of the offered load never completed, and non_2xx stays ~0
        # because a timeout is not an HTTP status.
        #
        # Measured on 2026-09-02 (istio, HPA=1, 100 RPS): the summary row read
        # p99=220.03ms / non_2xx=6 -- which looks clean -- while the raw output
        # carried "timeout 4243" against a histogram of 27724 samples whose max
        # was 462ms. Roughly one request in seven had exceeded 2s and left no
        # trace in any recorded column.
        #
        # This is load-bearing for the Istio-vs-Mazu comparison specifically: a
        # mesh that degrades by TIMING OUT rather than by returning errors would
        # score equal-or-better on every column the summary previously had.
        #
        # wrk2 omits the whole "Socket errors" line when there are none, so a
        # clean step correctly yields 0.
        TIMEOUTS=$(awk '/Socket errors/{for(i=1;i<=NF;i++) if($i=="timeout") print $(i+1)}' \
            "$OUT_FILE" | tr -d ',')
        [[ -z "$TIMEOUTS" ]] && TIMEOUTS=0

        # Measure the fleet EVERY step, under HPA or not, and with the same
        # READY_EXCLUDE filter the poller now uses.
        #
        # This used to fall back to $PODCOUNT whenever HPA=0, which had two
        # problems. PODCOUNT is captured once before the sweep and filters only
        # on 'nfs-subdir', so it counts the Completed setup-* helm hooks as
        # though they were serving capacity -- that is why the 09-03 12:45 run
        # reported 67 on all 18 rows against a real fleet of 65. And a value
        # repeated from before the sweep cannot show a fixed-replica run
        # LOSING a pod mid-sweep, which is exactly when you want to know.
        # Re-querying costs one kubectl per step and makes the column agree
        # with pods.csv in both modes.
        STEP_PODS=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
            | grep -Ev "$READY_EXCLUDE" | wc -l | tr -d ' ')
        [[ -z "$STEP_PODS" || "$STEP_PODS" == "0" ]] && STEP_PODS="$PODCOUNT"

        echo "$STRAT,$RPS,${DELIVERED:-},$(to_ms "${P50:-0}"),$(to_ms "${P99:-0}"),$NON2XX,$TIMEOUTS,$OVERLAP,$STEP_PODS,$RESET_TAG,$FLEET_TAG" >> "$SUMMARY"
    done

    # ---------------- Phase C: per-strategy teardown ----------------
    kill "$POD_POLL_PID" 2>/dev/null || true
    wait "$POD_POLL_PID" 2>/dev/null || true
    kill "$PF_PID" 2>/dev/null || true
    "$SCRIPT_DIR/setup_social_network.sh" uninstall-prometheus || true

    mazu_echo "=== $STRAT complete at $(date) ==="
done

# ===========================================================================
# Phase D: analysis
#
# BEST-EFFORT BY DESIGN. Every raw artifact is already on disk by the time
# this runs, so a plotting failure must never fail a multi-hour sweep -- each
# step warns and the next one still runs. Re-run any of them by hand against
# the run root afterwards.
#
# ORDER MATTERS: summarize_sn_pods.py writes the pods-<rps>-sum.csv files that
# plot_sn_pods.py consumes. plot_sn_pods.py does NOT read the raw
# pods-<rps>.csv, so running it first produces nothing.
#
# All four take the RUN ROOT, not a per-strategy directory, and discover the
# arms from its subdirectories -- so a sweep in which one arm was skipped
# still plots the arm that ran, and adding a third strategy needs no change.
#
# Each plotter writes a .dat beside every figure holding the exact series it
# drew: tab-separated, '#'-commented header (so gnuplot reads it directly),
# missing samples as N/A rather than 0. Every file opens with a block naming
# the artifact its numbers came from and the transform applied -- the unit
# normalisation, the denominator, why one field was used and its neighbour was
# not. That block is the point: it is what lets someone check a surprising bar
# months later, off this machine, without re-reading the plotting code.
mazu_echo "Generating comparison plots..."

python3 "$SCRIPT_DIR/summarize_sn_pods.py" "$RESULTS_ROOT" \
    || warn "summarize_sn_pods.py failed -- plot_sn_pods.py will have nothing to read"

#   plot_sn_pods       replica growth: total fleet vs RPS per arm, the per-step
#                      ramps, and where the extra replicas went per service
#   plot_sn_latency    p50/p90/p99 vs RPS, PLUS the share of offered load that
#                      never completed. The two panels are deliberately one
#                      figure: wrk2 drops timed-out requests before its
#                      histogram, so a percentile here describes only the
#                      requests that came back and must not be read alone.
#   plot_sn_resources  fleet-wide CPU and memory per component (app / proxy /
#                      istiod / gateway). Per-component because the headline is
#                      that app CPU is identical across meshes while the proxy
#                      tier is not -- a single aggregate would bury that.
for plotter in plot_sn_pods plot_sn_latency plot_sn_resources; do
    python3 "$SCRIPT_DIR/${plotter}.py" "$RESULTS_ROOT" || warn "${plotter}.py failed"
done

mazu_echo "=== DONE ==="
echo "Summary: $SUMMARY"
column -s, -t < "$SUMMARY"
echo
echo "Plots in $RESULTS_ROOT (pdf + png, with the plotted numbers in .dat):"
for f in plot_sn_pod_growth plot_sn_pod_totals plot_sn_pod_final \
         plot_sn_latency plot_sn_cpu plot_sn_memory; do
    [[ -f "$RESULTS_ROOT/$f.pdf" ]] || continue
    # The .dat is listed only when it exists: an older plotter, or one that
    # failed after saving the figure, still leaves a usable PDF.
    if [[ -f "$RESULTS_ROOT/$f.dat" ]]; then
        echo "  $f.pdf  ($f.dat)"
    else
        echo "  $f.pdf"
    fi
done
