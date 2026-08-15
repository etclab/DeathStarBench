# Running SocialNetwork (DeathStarBench) on Istio

Notes from getting `helm-chart/socialnetwork` running on the 18-node CloudLab
cluster, 2026-08-14. The short version: **the chart works unmodified**, but two
of its defaults make a healthy deployment look broken. Both are documented
below.

Companion files:

| File | Purpose |
| --- | --- |
| `run-socialnetwork-istio.sh` | Teardown → Istio → chart → seed → wrk2 sweep |
| `run-socialnetwork-scale.sh` | Progressive datastore scaling across topologies |
| `scratch/yaml/socialnetwork-bench-values.yaml` | Helm overrides that lift the throughput ceiling |
| `scratch/yaml/sn-images-legacy.yaml` | **Mandatory** image pins for every sharded/clustered arm |
| `scratch/yaml/sn-memcached-cluster.yaml` | mcrouter + memcached topology |
| `scratch/yaml/sn-mongodb-sharded.yaml` | mongos + config server + shards topology |
| `scratch/yaml/sn-redis-cluster.yaml` | Redis Cluster topology (see the warning inside) |
| `results/socialnetwork-fullrun-test/` | Output of the validating from-scratch run |

## Quick start

```bash
./run-socialnetwork-istio.sh                 # full run: install + seed + benchmark
SKIP_INSTALL=1 ./run-socialnetwork-istio.sh  # reuse the running deployment
STOCK_VALUES=1 ./run-socialnetwork-istio.sh  # stock chart defaults, no overrides

./run-socialnetwork-scale.sh --list          # show the scaling ladder
./run-socialnetwork-scale.sh                 # walk every rung
./run-socialnetwork-scale.sh baseline mc-3x3 # or just named ones
```

Useful knobs: `RPS_VALUES`, `DURATION`, `WORKLOAD`, `GRAPH`, `SKIP_SEED`,
`COMPOSE_POSTS`, `THREADS`, `CONNS`, `RESULTS_DIR`. See the header comment in
the script for the full list.

`run-socialnetwork-scale.sh` reuses the existing Istio install and only cycles
the chart release, so run `run-socialnetwork-istio.sh` at least once first.

## Does it start? Yes

56 pods, all `2/2 Running`, zero restarts. The application is functionally
correct end-to-end — register, follow, compose, and both timelines work with
full fan-out (user mentions resolved, media attached, URLs shortened, home
timelines populated from the follow graph).

Seeding `socfb-Reed98` with `--compose`:

```
Registering Users...   Succeeded: 962
Adding follows...      Succeeded: 37624
Composing posts...     Succeeded: 9424
```

Zero failures in any phase.

## The two things that make this chart look broken

### 1. The stock chart caps the whole application at ~200 RPS

`values.yaml` ships `global.resources.limits.cpu: 1000m` with
`global.replicas: 1`. Under load `nginx-thrift` pins at **exactly 1001m against
its 1000m limit** and gets CFS-throttled.

The trap is how this presents: **~21 s p50 latency with zero HTTP errors and no
crashes**. Nothing in `kubectl get pods` looks wrong, so it reads like a broken
or deadlocked application when it is purely CPU throttling.

Confirming it:

```bash
kubectl top pod <nginx-thrift-pod> --containers
# nginx-thrift   1001m   181Mi     <-- against a 1000m limit
```

On this cluster (18 × 32 vCPU = 576 vCPU, 64 GB/node) the defaults are wildly
conservative. `scratch/yaml/socialnetwork-bench-values.yaml` raises limits to
8 CPU / 8 Gi and scales the stateless services:

| | Stock | With overrides |
| --- | --- | --- |
| Delivered RPS (500 target) | 191 | 489 |
| p50 | 21.23 s | 18.30 ms |
| p99 | 37.58 s | 45.06 ms |

**Only the stateless Thrift services and nginx frontends are scaled.** The
mongodb/redis/memcached charts in the `standalone` topology are single-instance
datastores behind one ClusterIP — raising their replica count would spray reads
and writes across independent, non-replicating instances and silently corrupt
results. To scale those, use the chart's sharded/cluster topologies
(`global.mongodb.sharding.enabled`, `global.redis.cluster.enabled`,
`global.memcached.cluster.enabled`).

### 2. The datastores have no persistent volume

In the default standalone topology the mongodb/redis/memcached pods keep their
data in the container filesystem — no PVC, no PV. **Any helm upgrade that rolls
those pods silently wipes the dataset**, and the benchmark then measures empty
timelines while still returning HTTP 200.

This is easy to walk into: apply the resource overrides after seeding and every
deployment rolls, taking the data with it.

> **Rule: seed after the last `helm install`/`upgrade`, never before.**

`run-socialnetwork-istio.sh` enforces the ordering and warns if a timeline comes
back `{}` after seeding.

## Smaller gotchas

- **`read-home-timeline.lua` is not a workload.** It ships with a `response`
  hook that writes every status line, content-type, and full response body to
  stderr. Fine for debugging, useless under load. Use `mixed-workload.lua`
  (60% home timeline / 30% user timeline / 10% compose) — the canonical DSB
  workload, and the script's default.
- **`init_social_graph.py` does not compose posts by default.** Without
  `--compose` you get users and follows only, and every timeline read returns
  `{}`. The script passes `--compose` unless `COMPOSE_POSTS=0`.
- **Istio sidecar injection is not a problem here.** The `nginx-thrift` and
  `media-frontend` init containers `git clone` from GitHub, which would fail if
  `istio-init` had already set up its iptables redirect with no Envoy running.
  Istio 1.24 *appends* `istio-init` after the user init containers, so the clone
  runs on an unmodified network path and completes in ~5 s. Verified directly,
  not assumed.
- The chart's `service-config.tpl` references a `write-home-timeline-service`
  that is not in `Chart.yaml`'s dependency list. It does not appear to matter —
  compose and home-timeline fan-out both work — but it is worth knowing about
  if compose ever starts misbehaving.

## Measured performance

Mixed workload, `-t 16 -c 128`, 60 s per step, with the overrides applied
(from `results/socialnetwork-fullrun-test/`):

| Target RPS | Delivered | p50 | p99 |
| --- | --- | --- | --- |
| 400 | 398 | 17.7 ms | 40.9 ms |
| 600 | 589 | 20.0 ms | 58.0 ms |
| 800 | 788 | 27.7 ms | 1.55 s |

Clean to ~600 RPS, degrading at 800. From a separate 45 s sweep, 1000 target
delivers 763 RPS at 5.17 s p50 — fully saturated. Read-only
(`read-user-timeline.lua`) tops out around 1440 RPS.

### Unresolved: the ~950 RPS ceiling

Throughput plateaus at roughly 950 RPS on the mixed workload and it is **not**
explained by the obvious causes:

- **Not CPU.** Aggregate application CPU stays under one core at the plateau;
  the hottest single container measured 273m. Nothing is near its limit.
- **Not client concurrency.** `-c 512` delivers 955 RPS versus 989 at `-c 128`.
  Adding connections does not help, so wrk2 is not the constraint.
- **Not the ingress gateway.** Gateway pods sit at 4–44m across 5 replicas.
- **Not run length.** 45 s → 989, 60 s → 872, 180 s → 834. It is a steady-state
  ceiling, not a warm-up artifact.

Something server-side is serializing. The leading suspects are a fixed-size
connection pool in the nginx lua layer (`GenericObjectPool`) and the
single-instance datastores. Note also that `worker_processes auto` in
`nginx.conf` sees the host's 32 CPUs, not the cgroup quota, so nginx spawns 32
workers regardless of the CPU limit.

If higher rates are needed, the path is probably the sharded/cluster datastore
topologies rather than more service replicas.

## Scaling the datastores

The note above ends by guessing that the way past the ceiling is the
sharded/cluster datastore topologies rather than more service replicas. This
section is that experiment. `run-socialnetwork-scale.sh` walks a ladder of
configurations, changing **one axis per rung** so a throughput change can be
attributed, and benchmarking each rung identically.

```bash
./run-socialnetwork-scale.sh --list
```

Three things had to be sorted out before any of it would start.

### 1. Every Bitnami image the chart asks for is gone

This is the blocker, and it has nothing to do with DSB. The vendored subcharts
(`mongodb-sharded` 9.0.0, `redis-cluster` 8.1.4, `redis` 17.3.7, and the
`memcached` 5.15.8 subchart under `mcrouter`) pin image tags that Bitnami
**deleted from Docker Hub** when it moved its back catalogue to the
`bitnamilegacy` organisation in 2025. Checked against the registry API:

```
404  bitnami/mongodb-sharded:8.0.0-debian-12-r1
404  bitnami/redis-cluster:7.0.4-debian-11-r4
404  bitnami/redis:7.0.5-debian-11-r7
404  bitnami/memcached:1.6.12-debian-10-r23
200  bitnamilegacy/mongodb-sharded:8.0.0-debian-12-r1
200  bitnamilegacy/redis-cluster:7.0.4-debian-11-r4
```

So every cluster/sharded topology dies in `ImagePullBackOff` out of the box,
and the README's install commands cannot work as written. The standalone
topology is unaffected because it uses the plain upstream `redis`, `memcached`
and `mongo` images, not Bitnami's.

`scratch/yaml/sn-images-legacy.yaml` repoints them and is applied to every
non-baseline rung. Two wrinkles worth knowing:

- **`bitnami/bitnami-shell` is gone from every org**, `bitnamilegacy`
  included — the repository itself no longer exists. It is only referenced by
  `redis-cluster`'s `volumePermissions` and `sysctlImage`, both of which
  default to `enabled: false`, so nothing pulls it. Do not enable either
  without first finding a replacement.
- **`bitnamilegacy/memcached` has no `1.6.12-debian-10-r23` tag.** The legacy
  org only carries newer tags, so this one is pinned to `1.6.39-debian-12-r1`.
  That is an image version bump (1.6.12 → 1.6.39), not just a registry move.

### 2. Redis Cluster silently merges the home and user timelines

**This is the finding that matters most, and it invalidates the application
semantics of the redis arm.**

`service-config.tpl` points all four Redis clients at one shared
`<release>-redis-cluster` service. That is only sound if the services use
disjoint key spaces. They do not:

```
src/HomeTimelineService/HomeTimelineHandler.h:150
    pipe.zadd(std::to_string(follower_id), post_id_str, timestamp, ...)
src/UserTimelineService/UserTimelineHandler.h:168
    _redis_client_pool->zadd(std::to_string(user_id), std::to_string(post_id), ...)
```

Both use a **bare user id** as the sorted-set key, with no service prefix. In
the standalone topology that is harmless — they are separate Redis instances.
Collapsed onto one cluster, key `"123"` is simultaneously user 123's home
timeline (posts by people they follow) *and* user 123's own posts. The two
sets union together.

The failure is silent in exactly the way the resource-limit trap was: both
endpoints keep returning HTTP 200 with well-formed JSON, just with the wrong
contents. And because the merged sets are larger than either should be, read
latency is distorted too — so it is not merely a correctness problem that a
performance run can shrug off.

For contrast, the other two Redis clients *are* namespaced —
`SocialGraphService` uses `<id>:followers` / `<id>:followees` — so social-graph
is fine. Only home-timeline and user-timeline collide.

This cannot be fixed from values: the key prefixes are compiled into the
service binaries. So `run-socialnetwork-scale.sh` measures the damage instead
of ignoring it. After seeding, it reads both timelines for five users and
reports their Jaccard overlap, which lands in `summary.csv` next to every
throughput number. The two timelines should be nearly disjoint:

```
mc-3x3   user 5: user-timeline=3 posts, home-timeline=100 posts
         jaccard overlap = 0.0%
         VERDICT=OK  timelines are distinct
```

`memcached` cluster mode was checked the same way and is **safe** — its four
caches use genuinely disjoint keys (`post_id` as a bare int64,
`<username>:user_id` and `<username>:login`, shortened-url strings, media
names). MongoDB sharding is safe too, because each service gets its own
database and `mongos` routes by `db.collection`.

### 3. The cluster topologies ship with a single router in front of everything

Both replacements introduce a proxy tier that defaults to **one pod**:

- `mcrouter.statefulset.replicas` defaults to 1
- `mongodb-sharded.mongos.replicaCount` defaults to 1

In the standalone topology each service talks to its own datastore, so there
is no shared choke point. Switching to a cluster topology at chart defaults
funnels *every* cache or database operation in the application through a
single pod — which can easily be slower than the standalone setup it replaced.
The ladder therefore varies router count and backend count as **separate
axes** (`mc-1x3` vs `mc-3x3`, `mongo-1x3` vs `mongo-3x3`) rather than assuming
that "clustered" means "faster".

### What "scaling memcached" actually buys you

Worth being precise, because the obvious reading is wrong. The route config
the chart installs (`templates/hooks/mcrouter/configmap.yaml`) sets
`operation_policies` for `add`/`delete`/`get`/`set` to `AllFastestRoute` —
write to every backend, read the latest. That is **replication, not
sharding**: every memcached pod holds a full copy, so adding backends buys
read throughput and redundancy, *not* cache capacity. The
`default_policy: PoolRoute|A` would shard, but every operation the application
actually issues is covered by an `operation_policy` override, so it never
applies.

### Environment notes specific to these arms

- **Persistence is kept off every arm — but Redis needs a different lever.**
  The charts default to PVCs, and the only StorageClass here is NFS-backed
  (`nfs-subdir-external-provisioner`). Running mongod's journal or Redis fsync
  over NFS would dominate the measurement and make the arms incomparable to
  the standalone baseline, which kept data in the container filesystem.
  `mongodb-sharded` and `memcached` honour `persistence.enabled: false`.
  **`redis-cluster` 8.1.4 does not have that key at all** — its
  `volumeClaimTemplates` block is unconditional, so setting it is silently
  ignored and every node still gets an 8Gi NFS PVC. Since the volume cannot
  be removed, the write path is neutralised instead, with
  `redis.useAOFPersistence: "no"` (it defaults to `"yes"`, i.e. an fsync to
  NFS on every write) plus `save ""` to disable RDB snapshots. The PVCs are
  then provisioned and sit empty.
  **This means the "seed after the last helm upgrade" rule still applies.**
- **The chart's post-install hooks are already Istio-aware.** All three
  (`setup-mcrouter-configmap`, `setup-collection-sharding-hook`,
  `redis-cluster-readiness-hook`) POST to `localhost:15020/quitquitquit` when
  they finish, so the sidecar exits and the hook pod can reach `Succeeded`
  instead of hanging forever under STRICT mTLS. This is the usual reason helm
  hooks hang in a mesh, and it is already handled here — do not "fix" it.
- **Hook pods are not garbage collected.** They carry no
  `helm.sh/hook-delete-policy`, so they survive `helm uninstall` and the next
  install fails with "pod already exists". The scale script deletes them
  explicitly during teardown.
- **Do not wait on `-l service`.** The DSB pods carry that label; the Bitnami
  statefulsets do not. `run-socialnetwork-istio.sh`'s readiness loop filters on
  it, which in these topologies reports Ready while the datastores are still
  forming. The scale script waits on every pod in the namespace.

## Environment

- Kubernetes 1.29.10, 18 nodes (2 control-plane), Calico, 32 vCPU / 64 GB each
- Istio 1.24.0, default profile, sidecar injection on `default`, STRICT mTLS
- Ingress via LoadBalancer, port 80 → `nginx-thrift:8080`
- Helm 3.21.4; all chart dependencies vendored under `charts/` (no repo access
  needed for the default standalone topology)
