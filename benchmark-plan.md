# Benchmark Plan (CloudLab)

## Benchmark 1: Steady State (Connection Reuse)

- **App**: Bookinfo, scaling disabled
- **Connection reuse**: Enabled
- **RPS sweep**: 50, 100, 150, 200, 250, 300 (bookinfo struggles ~200-250)
- **Metrics**: p50, p90, p95 end-to-end latency
- **What it measures**: Amortized cost — only TokenReview + regStore lookup in `checkWithToken()`. All RBE proof verification, counter attestation, and challenge-response happen at startup via streaming preload.
- **Note**: TokenReview cache has 1s TTL, so low RPS may see more cache misses.

## Benchmark 1.5: Steady State (No Connection Reuse)

- **App**: Bookinfo, scaling disabled
- **Connection reuse**: Disabled
- **RPS sweep**: Same as benchmark 1
- **Metrics**: p50, p90, p95 end-to-end latency
- **What it measures**: Per-connection TLS handshake cost. Every new connection triggers `doVerifyCertChain()` → gRPC to ext_authz → TokenReview + regStore lookup on both sides (mutual). Compared against Istio's standard X.509 chain verification.

## Benchmark 2: Worst Case (All Operations in Request Path)

- **App**: Two pods (fortio client/server) for clean single-hop measurement. Optionally also bookinfo for the "real app" narrative.
- **Connection reuse**: Disabled
- **RPS**: 100
- **What it measures**: Simulates scaling behavior where services come online and must perform all verification per-request.
- **Approach**:
  1. First request fetches from KC and caches — discard from timing.
  2. Clear proof/attestation verification cache (keep raw registration data) so each request re-does verification.
  3. Time only the verification path — avoids KC round-trip latency (infrastructure noise, not scheme cost).
- **Operations timed per-request**:
  - RBE proof verification (BLS12-381 pairing checks via `circl`)
  - Counter attestation verification (TRINC signature check)
  - Challenge-response nonce computation (local crypto)
  - TokenReview API call

## Resource Usage (All Benchmarks)

Collected via Prometheus for all benchmarks (1, 1.5, and 2). Compare Mazu vs stock Istio.

### Pods to monitor

- All bookinfo pods (`istio-proxy` sidecar containers)
- `istiod` pod
- `kube-apiserver` pods (handles TokenReview requests — could be scaled independently, but still measure to quantify the load Mazu places on the API server)

### Metrics

- CPU usage (cores)
- Memory usage (RSS)
- Scraped via Prometheus throughout each benchmark run for easy extraction in later runs

## Cost Breakdown (Future — Requires Source Instrumentation)

Will instrument source code to measure per-operation costs in the request path. Not needed for the steady state benchmarks.

### Phases to time

- TokenReview duration
- FetchRegistration RPC time (only for first/uncached)
- Proof verification time (in `ProcessRegistration()`)
- Counter attestation verification time (in `ProcessRegistration()`)
- Challenge-response nonce computation time

## Key Design Decisions

- **Two pods vs bookinfo for benchmark 2**: Two pods gives clean single-hop measurement, easier to attribute costs. Bookinfo has deep call graph (productpage → details + reviews → ratings) where latency compounds. Use both: two-pod for precision, bookinfo for realism.
- **KC is colocated** in the same CloudLab cluster but not necessarily the same node. KC round-trip is cached after first fetch and excluded from per-request timing.
- **Connection reuse is the key variable**: With reuse, TLS handshake cost is amortized. Without reuse, every request pays the full validation cost.


## Backlog
- ensure `python3-pip`, `build-essential`, `helm` are installed
- ensure the test runner node has `kubectl` config set to connect to the `k8s` cluster
  - use the external ip of k8s node in `~/.kube/config`
- ensure `./setup_social_network.sh init-packages` is run
- ensure `./setup_social_network.sh build-wrk2` is run
- ensure test runner node can ssh into the individual nodes of the `k8s` cluster and setup tpm devices
- run the `./setup-tpm-all-nodes.sh` before you run the benchmarks