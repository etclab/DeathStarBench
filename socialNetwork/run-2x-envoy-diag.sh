#!/bin/bash
#
# Envoy-level diagnostic run at 2x replicas: Istio vs Mazu
#
# WHAT THIS IS FOR
#   results/benchmark1.5b-replica-scale-09-05-26_121802 says the Mazu arm has a
#   hard throughput ceiling that gets WORSE as the fleet grows -- 246 rps at 1x,
#   ~300 at 2x, ~65 at 4x, ~40 at 8x -- while its istio-proxy sits at 0.05-0.2
#   cores. Something is blocking, not computing. But that run collected only
#   cgroup CPU and memory, so which thing is blocking is not in the data, and
#   several explanations fit equally well:
#
#     H1  the Envoy worker's event loop is being blocked
#         rbe_validator.cc:254 calls pthread_join() from onVerificationComplete,
#         which runs on the dispatcher. A blocked worker freezes every
#         connection it owns.
#         DECIDED BY  server.watchdog_miss / server.watchdog_mega_miss.
#         Envoy raises these itself when a worker misses its watchdog for 200ms
#         / 1s. Nonzero is proof; zero rules H1 out outright.
#
#     H2  thread-per-handshake is the cost
#         doVerifyCertChain spawns one OS thread per validation
#         (rbe_validator.cc:164), default 8MB stack.
#         DECIDED BY  the proxy's live thread count, sampled during load. Flat
#         at concurrency plus Envoy's own handful means H2 is wrong; tracking
#         the handshake rate into the hundreds means it is right.
#
#     H3  handshakes are queueing in front of the transport socket
#         DECIDED BY  listener.*.downstream_pre_cx_active -- connections
#         accepted but not yet through the handshake -- together with
#         cluster.*.upstream_cx_connect_ms and upstream_cx_connect_fail.
#
#     H4  the ext_authz 1-second cache is missing, so every handshake becomes a
#         TokenReview and the queue is in kube-apiserver's APF, not in Envoy
#         DECIDED BY  TokenReviews per completed request, plus
#         apiserver_flowcontrol_current_inqueue_requests and the APF wait
#         histogram.
#
#     H5  the sidecar is simply CPU-throttled, and "low CPU" is a cgroup
#         artifact rather than idleness
#         DECIDED BY  cgroup cpu.stat nr_throttled / throttled_usec. This one
#         matters more than it looks: the replica-scale driver now pins
#         proxyCPU/proxyCPULimit at 1/1, and its own header warns to check for
#         throttling before reading a flat plateau as a fan-out effect.
#
#   None of this needs an Envoy rebuild. All five are readable from the admin
#   endpoint, /proc and Prometheus, which is what collect_envoy_stats.sh does.
#
# WHY 2x AND NOT 4x
#   2x is the smallest scale where Mazu and Istio actually diverge (Mazu 296 rps
#   against Istio 469, and p99 already 4.6s at 100 rps) while the mesh is still
#   functioning rather than collapsed. At 4x and beyond the Mazu arm is in
#   retry/timeout collapse, where every counter is saturated and none of them
#   discriminates -- the ceiling there is 65 / 40 / 50 rps at 4x / 8x / 16x,
#   which is not even monotonic in the fleet size. Diagnose the onset, not the
#   wreckage.
#
# WHAT IT RUNS
#   run-benchmark1.5b-replica-scale-sweep.sh restricted to SCALES=2, with
#   ENVOY_DIAG=1 and a widened statsInclusionPrefixes, so setup, teardown,
#   manifests and load generation are bit-for-bit the ones the 09-05 sweep used.
#   Nothing about the measurement path is special-cased for this run.
#
#   RPS_VALUES defaults to "100 400 800" rather than the full seven-step sweep:
#   100 is where Mazu is still meeting its target and the 4.6s p99 is the only
#   symptom, 400 is where Istio pulls ahead (397 vs 294), and 800 is past the
#   Mazu ceiling. Three steps at 120s keeps the run near an hour instead of
#   three, and the missing steps interpolate.
#
# TWO CAVEATS ABOUT COMPARING THIS TO THE 09-05 RUN
#   1. The sidecar is smaller now. That run's manifests carried
#      proxyCPULimit: "2" with no proxyCPU, so the request was defaulted up to
#      2 and Envoy chose concurrency=2. The current bookinfo-const*.yaml pins
#      1/1, so this run gets concurrency=1 -- one worker thread, where a single
#      blocking join is the entire proxy. That makes H1 and H5 easier to
#      observe, and it makes the throughput numbers NOT directly comparable to
#      the 09-05 table. Set SRC_YAML/SRC_TPM_YAML to the committed manifests
#      (git show HEAD:...) if you want the old pod shape back.
#   2. Connection reuse. The 09-05 run had it OFF -- its logs show
#      details-no-reuse / ratings-no-reuse / reviews-no-reuse applied -- so
#      every request paid three fresh handshakes and six RBE validations. The
#      replica-scale driver now defaults CONN_REUSE=1. This script keeps
#      CONN_REUSE=0 to reproduce the conditions that produced the collapse,
#      because a handshake-cost hypothesis cannot be tested on a fleet that
#      barely handshakes. Set CONN_REUSE=1 to see how much of it survives
#      normal pooling -- but plot the two separately.
#
# Usage:
#   ./run-2x-envoy-diag.sh
#   nohup ./run-2x-envoy-diag.sh > /dev/null 2>&1 &
#
#   RPS_VALUES=100 DURATION=60 ./run-2x-envoy-diag.sh     # quick smoke test
#   SCALES="1 2 4" ./run-2x-envoy-diag.sh                 # onset across scales
#
# Environment: everything run-benchmark1.5b-replica-scale-sweep.sh accepts.
# This script only changes the defaults.
#
# Reading the output:
#   python3 parse_envoy_diag.py results/envoy-diag-<date>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRIVER="${SCRIPT_DIR}/run-benchmark1.5b-replica-scale-sweep.sh"
[ -x "$DRIVER" ] || { echo "ERROR: $DRIVER is not executable" >&2; exit 1; }

RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/envoy-diag-$(date +%m-%d-%y_%H%M%S)}"

# Widened so the counters above are instantiated at all. "cluster" and "http"
# subsume the stock "cluster.outbound" and "http.inbound"; "server" and "ssl"
# are the two trees the stock value excluded, and they are where H1 and H3 live.
STATS_PREFIXES="${STATS_PREFIXES:-cluster,listener,http,server,ssl,rbe}"

export SCALES="${SCALES:-2}"
export RPS_VALUES="${RPS_VALUES:-100 400 800}"
export DURATION="${DURATION:-120}"
export STRATEGIES="${STRATEGIES:-st5-AttUpd istio}"
export CONN_REUSE="${CONN_REUSE:-0}"
# Exported explicitly rather than relied on to be inherited, so that the tag is
# still carried when this script is invoked from something that scrubs the
# environment. Empty means "use the strategy name", the historical behaviour.
export MAZU_TAG="${MAZU_TAG:-}"
export ENVOY_DIAG=1
export STATS_PREFIXES
export RESULTS_DIR
# The cross-scale plotter wants several scales and a full RPS sweep; neither
# holds here, and a failed plot at the end of an hour-long run is just noise.
export SKIP_PLOT="${SKIP_PLOT:-1}"

echo "=== Envoy diagnostic run ==="
echo "Scales:      ${SCALES}"
echo "RPS values:  ${RPS_VALUES}"
echo "Duration:    ${DURATION}s per step"
echo "Strategies:  ${STRATEGIES}"
echo "Conn reuse:  ${CONN_REUSE} (0 reproduces the 09-05 collapse conditions)"
echo "Mazu image:  docker.io/atosh502/proxyv2:${MAZU_TAG:-st5-AttUpd}${MAZU_TAG:+  (MAZU_TAG override)}"
echo "Stats:       ${STATS_PREFIXES}"
echo "Results:     ${RESULTS_DIR}"
echo
echo "Analyse with: python3 ${SCRIPT_DIR}/parse_envoy_diag.py ${RESULTS_DIR}"
echo

exec "$DRIVER"
