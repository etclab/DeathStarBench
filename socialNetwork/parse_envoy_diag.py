#!/usr/bin/env python3
"""Reduce a run-2x-envoy-diag.sh result tree to one table per hypothesis.

collect_envoy_stats.sh writes raw Envoy admin dumps -- a few thousand stat
lines per pod per RPS step, times two snapshots, times two strategies. That is
the right thing to keep on disk and the wrong thing to read. This turns it into
the handful of numbers that actually separate the candidate explanations for
the Mazu throughput ceiling, and says which way each one came out.

The hypotheses, and the counter that decides each, are documented in
run-2x-envoy-diag.sh. In short:

    H1 blocked event loop      server.watchdog_miss / watchdog_mega_miss
    H2 thread-per-handshake    proxy thread count, sampled under load
    H3 handshake queueing      downstream_pre_cx_active, upstream_cx_connect_*
    H4 ext_authz cache misses  TokenReviews per completed request
    H5 cgroup CPU throttling   cpu.stat nr_throttled / throttled_usec

Counters are reported as post-minus-pre deltas over the load step. Gauges come
from the in-flight sampler, because a gauge read after the step has already
drained. Histograms report the cumulative P50/P99 the proxy itself computed.

Usage:
    python3 parse_envoy_diag.py results/envoy-diag-<date> [--pod-detail]
"""

import argparse
import json
import pathlib
import re
import statistics
import sys

# Envoy /stats text lines are "name: value". Names contain colons (cluster
# names embed "|"-separated ports and, for some listeners, ":"), so split on
# the LAST ": " rather than the first.
STAT_RE = re.compile(r"^(?P<name>.+?): (?P<value>.*)$")

# "P0(nan,1.5) P25(nan,3.0) ..." -- each bucket is (interval, cumulative). The
# interval value is nan whenever nothing landed in the flush window, which for
# a snapshot taken after load is most of the time; the cumulative value is the
# one that survives.
HIST_RE = re.compile(r"P([\d.]+)\(([^,]+),([^)]+)\)")


def parse_stats(path):
    """Envoy /stats text -> ({name: int}, {name: {percentile: float}})."""
    scalars, hists = {}, {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return scalars, hists
    for line in text.splitlines():
        m = STAT_RE.match(line.strip())
        if not m:
            continue
        name, raw = m.group("name"), m.group("value")
        if raw.startswith("P0("):
            buckets = {}
            for pct, _interval, cumulative in HIST_RE.findall(raw):
                try:
                    buckets[float(pct)] = float(cumulative)
                except ValueError:
                    pass
            if buckets:
                hists[name] = buckets
        else:
            try:
                scalars[name] = int(raw)
            except ValueError:
                pass
    return scalars, hists


def parse_samples(path):
    """A .samples file -> list of per-tick {key: value} dicts."""
    ticks, cur = [], None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ticks
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# t="):
            if cur:
                ticks.append(cur)
            cur = {"t": int(line[4:])}
            continue
        if cur is None:
            continue
        # The /proc snippet emits key=value; the admin endpoint emits
        # "name: value". Both land in the same tick.
        if "=" in line and ": " not in line:
            k, _, v = line.partition("=")
            try:
                cur[k] = int(v)
            except ValueError:
                cur[k] = v
        else:
            m = STAT_RE.match(line)
            if m:
                try:
                    cur[m.group("name")] = int(m.group("value"))
                except ValueError:
                    pass
    if cur:
        ticks.append(cur)
    return ticks


# Scopes that are not the data path. Every sidecar keeps clusters for istiod
# (xds/sds), the pilot-agent, and its own stats endpoint, and those carry their
# own ssl.handshake and upstream_cx_* counters. Summing them into the totals
# would mix control-plane connections -- which both arms make identically, at
# startup, a handful of times -- into the per-request handshake numbers that
# are the whole comparison. Excluded by name rather than by allow-listing the
# Bookinfo clusters so a renamed service does not silently vanish.
CONTROL_PLANE = (
    "xds-grpc", "sds-grpc", "cluster.agent;", "prometheus_stats",
    "istiod.istio-system", "istio-ingressgateway.istio-system",
    "kubernetes.default", "metrics-server", "kubernetes-dashboard",
    "dashboard-metrics-scraper", "coredns.kube-system", "webhook-service",
    "listener.admin",
)


def is_data_path(name):
    return not any(s in name for s in CONTROL_PLANE)


def sum_matching(scalars, *substrings):
    """Sum every data-path stat whose name contains all of `substrings`.

    Envoy names counters per listener and per cluster, and the fleet has one of
    each per service. Nothing here wants a specific cluster -- the question is
    always "how many across this proxy" -- so match on substring and add.

    Substrings are matched against the leaf too, because Envoy scopes the
    watchdog per thread: the counters are server.main_thread.watchdog_miss and
    server.worker_N.watchdog_miss, NOT server.watchdog_miss. Matching the
    latter finds nothing, which reads as "zero, H1 ruled out" when the truth is
    that the query was wrong.
    """
    return sum(v for k, v in scalars.items()
               if all(s in k for s in substrings) and is_data_path(k))


def hist_percentile(hists, substring, pct):
    """Max cumulative percentile across every histogram matching `substring`.

    Max, not mean: these are per-cluster histograms and the question is whether
    ANY upstream is slow to connect. Averaging a slow cluster against three
    fast ones hides exactly the case we are looking for.
    """
    vals = [b[pct] for k, b in hists.items()
            if substring in k and pct in b and b[pct] == b[pct]  # NaN != NaN
            and is_data_path(k)]
    return max(vals) if vals else None


def read_wrk(path):
    """Achieved throughput and latency percentiles from a wrk2 report."""
    out = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Requests/sec:"):
            out["rps"] = float(s.split()[-1])
        elif s.startswith("50.000%"):
            out["p50_ms"] = parse_wrk_time(s.split()[-1])
        elif s.startswith("99.000%"):
            out["p99_ms"] = parse_wrk_time(s.split()[-1])
        elif "requests in" in s and "read" in s:
            out["requests"] = int(s.split()[0])
        elif s.startswith("Socket errors:"):
            m = re.search(r"timeout (\d+)", s)
            if m:
                out["timeouts"] = int(m.group(1))
    return out


def parse_wrk_time(tok):
    """wrk2 prints '48.48ms', '26.00s', '1.12m'. Normalise to ms."""
    m = re.match(r"([\d.]+)(us|ms|s|m)$", tok)
    if not m:
        return None
    v, unit = float(m.group(1)), m.group(2)
    return v * {"us": 0.001, "ms": 1.0, "s": 1000.0, "m": 60000.0}[unit]


def collect_step(strat_dir, rps):
    """Everything known about one (strategy, RPS) step."""
    envoy = strat_dir / "envoy"
    pre_dir, post_dir = envoy / f"pre-{rps}", envoy / f"post-{rps}"
    step = {"rps_target": rps, "wrk": read_wrk(strat_dir / f"{rps}.txt")}

    # ---- counter deltas, per pod then aggregated ----
    pods = {}
    for post_path in sorted(post_dir.glob("*.stats")):
        pod = post_path.name[: -len(".stats")]
        post_s, post_h = parse_stats(post_path)
        pre_s, _ = parse_stats(pre_dir / f"{pod}.stats")
        if not post_s:
            continue

        def d(*subs):
            return sum_matching(post_s, *subs) - sum_matching(pre_s, *subs)

        pods[pod] = {
            "watchdog_miss": d("watchdog_miss"),
            "watchdog_mega_miss": d("watchdog_mega_miss"),
            "ssl_handshake": d("ssl.handshake"),
            "ssl_fail_verify": d("ssl.fail_verify_error"),
            "ssl_conn_error": d("ssl.connection_error"),
            "cx_connect_fail": d("upstream_cx_connect_fail"),
            "cx_connect_timeout": d("upstream_cx_connect_timeout"),
            "cx_total": d("upstream_cx_total"),
            "rq_pending_overflow": d("upstream_rq_pending_overflow"),
            "connect_p50_ms": hist_percentile(post_h, "upstream_cx_connect_ms", 50.0),
            "connect_p99_ms": hist_percentile(post_h, "upstream_cx_connect_ms", 99.0),
        }

        # concurrency lives under command_line_options, not at the top level:
        # /server_info is version + state + the full argv + Istio's node
        # metadata, which is why these files are ~86KB rather than a few
        # hundred bytes.
        info = post_dir / f"{pod}.server_info"
        if info.exists():
            try:
                blob = json.loads(info.read_text())
                pods[pod]["concurrency"] = (
                    blob.get("command_line_options", {}).get("concurrency")
                )
            except (json.JSONDecodeError, OSError):
                pass

    step["pods"] = pods
    meta = post_dir / "_meta"
    step["need_wider_stats"] = (
        "need_wider_stats=1" in meta.read_text() if meta.exists() else None
    )

    # ---- gauges and /proc, from the in-flight sampler ----
    threads, pre_cx, throttled, worker_tasks = [], [], [], []
    for spath in sorted((envoy / f"samples-{rps}").glob("*.samples")):
        ticks = parse_samples(spath)
        if not ticks:
            continue
        threads += [t["threads"] for t in ticks if isinstance(t.get("threads"), int)]
        worker_tasks += [t["tasks_named_worker"] for t in ticks
                         if isinstance(t.get("tasks_named_worker"), int)]
        for t in ticks:
            pcx = sum(v for k, v in t.items()
                      if isinstance(v, int) and "downstream_pre_cx_active" in k)
            pre_cx.append(pcx)
        # cpu.stat counters only rise, so the step's throttling is last-first
        # within this pod's own series.
        thr = [t["cg_nr_throttled"] for t in ticks
               if isinstance(t.get("cg_nr_throttled"), int)]
        if len(thr) >= 2:
            throttled.append(thr[-1] - thr[0])

    step["threads_max"] = max(threads) if threads else None
    step["threads_mean"] = round(statistics.mean(threads), 1) if threads else None
    step["workers"] = max(worker_tasks) if worker_tasks else None
    step["pre_cx_max"] = max(pre_cx) if pre_cx else None
    step["throttled_periods"] = sum(throttled) if throttled else None

    # ---- apiserver ----
    api_path = envoy / f"apiserver_{rps}.json"
    if api_path.exists():
        try:
            api = json.loads(api_path.read_text())
        except (json.JSONDecodeError, OSError):
            api = {}

        def scalar(key):
            res = (api.get(key) or {}).get("data", {}).get("result") or []
            try:
                return float(res[0]["value"][1])
            except (IndexError, KeyError, ValueError, TypeError):
                return None

        step["tokenreviews"] = scalar("tokenreview_count")
        step["tokenreview_p99_ms"] = (
            (scalar("tokenreview_p99_seconds") or 0) * 1000
            if scalar("tokenreview_p99_seconds") is not None else None
        )
        step["apf_inqueue_max"] = scalar("apf_inqueue_max")
        step["apf_wait_p99_ms"] = (
            (scalar("apf_wait_p99_seconds") or 0) * 1000
            if scalar("apf_wait_p99_seconds") is not None else None
        )
    return step


def agg(step, key):
    vals = [p.get(key) for p in step["pods"].values() if p.get(key) is not None]
    return sum(vals) if vals else None


def peak(step, key):
    vals = [p.get(key) for p in step["pods"].values() if p.get(key) is not None]
    return max(vals) if vals else None


def fmt(v, nd=0):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return f"{v:,}"


def report(results, pod_detail=False):
    for scale, strats in sorted(results.items()):
        print(f"\n{'=' * 100}\nscale {scale}\n{'=' * 100}")

        hdr = (f"{'strat':<12}{'rpsT':>6}{'rpsA':>8}{'p50ms':>9}{'p99ms':>10}"
               f"{'wdMiss':>8}{'wdMega':>8}{'thrMax':>8}{'preCX':>7}"
               f"{'hshake':>8}{'failVfy':>8}{'cxFail':>7}{'cnP99':>8}"
               f"{'TokRev':>8}{'TR/req':>8}{'apfQ':>6}{'throt':>7}")
        print(hdr)
        print("-" * len(hdr))

        for strat, steps in sorted(strats.items()):
            for rps, s in sorted(steps.items()):
                w = s["wrk"]
                tr, reqs = s.get("tokenreviews"), w.get("requests")
                tr_per_req = tr / reqs if tr and reqs else None
                print(
                    f"{strat:<12}{rps:>6}"
                    f"{fmt(w.get('rps'), 1):>8}"
                    f"{fmt(w.get('p50_ms'), 1):>9}"
                    f"{fmt(w.get('p99_ms'), 1):>10}"
                    f"{fmt(agg(s, 'watchdog_miss')):>8}"
                    f"{fmt(agg(s, 'watchdog_mega_miss')):>8}"
                    f"{fmt(s.get('threads_max')):>8}"
                    f"{fmt(s.get('pre_cx_max')):>7}"
                    f"{fmt(agg(s, 'ssl_handshake')):>8}"
                    f"{fmt(agg(s, 'ssl_fail_verify')):>8}"
                    f"{fmt(agg(s, 'cx_connect_fail')):>7}"
                    f"{fmt(peak(s, 'connect_p99_ms'), 1):>8}"
                    f"{fmt(tr, 0):>8}"
                    f"{fmt(tr_per_req, 2):>8}"
                    f"{fmt(s.get('apf_inqueue_max')):>6}"
                    f"{fmt(s.get('throttled_periods')):>7}"
                )

        if any(s.get("need_wider_stats") for st in strats.values() for s in st.values()):
            print("\n  WARNING: server.* / ssl.* were absent from at least one snapshot.")
            print("  The fleet was deployed with a statsInclusionPrefixes that excluded")
            print("  them, so wdMiss/wdMega/hshake/failVfy above are not zero -- they are")
            print("  missing. Re-run via run-2x-envoy-diag.sh, which widens the annotation.")

        verdicts(strats)

        if pod_detail:
            for strat, steps in sorted(strats.items()):
                for rps, s in sorted(steps.items()):
                    print(f"\n  -- {strat} @ {rps} rps, per pod --")
                    for pod, p in sorted(s["pods"].items()):
                        print(f"    {pod:<44} "
                              f"wd={fmt(p['watchdog_miss'])}/{fmt(p['watchdog_mega_miss'])} "
                              f"hs={fmt(p['ssl_handshake'])} "
                              f"fail={fmt(p['ssl_fail_verify'])} "
                              f"cxfail={fmt(p['cx_connect_fail'])} "
                              f"cnP99={fmt(p['connect_p99_ms'], 1)} "
                              f"conc={fmt(p.get('concurrency'))}")


def verdicts(strats):
    """State which hypothesis each column came out for, Mazu against Istio.

    Deliberately compares against the Istio arm rather than an absolute
    threshold: the same cluster, load generator and fleet shape run both arms
    minutes apart, so Istio is the control for everything except the cert
    validator. An absolute number would just be a guess about this hardware.
    """
    mazu = next((v for k, v in strats.items() if k != "istio"), None)
    istio = strats.get("istio")
    if not mazu or not istio:
        print("\n  (need both arms for a verdict; found: "
              f"{', '.join(sorted(strats))})")
        return

    def total(steps, key, how=agg):
        vals = [how(s, key) for s in steps.values()]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    def gauge(steps, key):
        vals = [s.get(key) for s in steps.values() if s.get(key) is not None]
        return max(vals) if vals else None

    def ratio(steps):
        tr = sum(s.get("tokenreviews") or 0 for s in steps.values())
        rq = sum(s["wrk"].get("requests") or 0 for s in steps.values())
        return tr / rq if tr and rq else None

    # An apiserver verdict cannot be read off the Mazu/Istio ratio. Istio does
    # essentially zero TokenReviews, so ANY Mazu traffic divides to a huge
    # multiple -- the first version of this function called H4 "SUPPORTED at
    # 268x" on 34 TokenReviews/s that the apiserver answered in 5ms with an
    # empty queue. What decides H4 is whether the apiserver is actually
    # STRESSED: requests waiting in an APF queue, or TokenReview latency large
    # enough to matter against a handshake. Judge it on those, and report the
    # ratio only as context.
    apf = gauge(mazu, "apf_inqueue_max") or 0
    tr_p99 = max((s.get("tokenreview_p99_ms") or 0) for s in mazu.values())
    tr_rate = max(
        (s.get("tokenreviews") or 0) / (s["wrk"].get("requests") or 1)
        for s in mazu.values()
    )

    print("\n  verdicts (Mazu vs Istio, summed/peaked over the RPS steps):")
    print(f"    H4 apiserver stress        APF in-queue peak {apf:.0f}, "
          f"TokenReview p99 {tr_p99:.1f}ms, {tr_rate:.2f} TR/request")
    if apf == 0 and tr_p99 < 50:
        print("      -> apiserver answered every TokenReview promptly and never "
              "queued one: NOT the bottleneck,")
        print("         regardless of how the volume compares to Istio's "
              "(which does none at all).")
    else:
        print("      -> apiserver is queueing or slow: H4 SUPPORTED.")

    rows = [
        ("H1 blocked event loop", "watchdog_miss + mega_miss",
         (total(mazu, "watchdog_miss") or 0) + (total(mazu, "watchdog_mega_miss") or 0),
         (total(istio, "watchdog_miss") or 0) + (total(istio, "watchdog_mega_miss") or 0)),
        ("H2 thread-per-handshake", "peak proxy threads",
         gauge(mazu, "threads_max"), gauge(istio, "threads_max")),
        ("H3 handshake queueing", "peak downstream_pre_cx_active",
         gauge(mazu, "pre_cx_max"), gauge(istio, "pre_cx_max")),
        ("H3 handshake queueing", "upstream_cx_connect_fail",
         total(mazu, "cx_connect_fail"), total(istio, "cx_connect_fail")),
        ("H5 cgroup throttling", "throttled periods",
         gauge(mazu, "throttled_periods"), gauge(istio, "throttled_periods")),
    ]
    for h, metric, m, i in rows:
        if m is None and i is None:
            call = "NOT MEASURED"
        elif not m and not i:
            call = "both zero -> ruled out"
        elif i and m and m > 2 * i:
            call = f"Mazu {m / i:.1f}x Istio -> SUPPORTED"
        elif m and not i:
            call = "Mazu only -> SUPPORTED"
        else:
            call = "comparable -> not the differentiator"
        print(f"    {h:<26} {metric:<30} "
              f"mazu={fmt(m, 2):>10}  istio={fmt(i, 2):>10}  {call}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_dir", type=pathlib.Path)
    ap.add_argument("--pod-detail", action="store_true",
                    help="also print per-pod counter deltas")
    args = ap.parse_args()

    if not args.results_dir.is_dir():
        sys.exit(f"not a directory: {args.results_dir}")

    results = {}
    for scale_dir in sorted(args.results_dir.glob("scale-*x")):
        strats = {}
        for strat_dir in sorted(p for p in scale_dir.iterdir() if p.is_dir()):
            if strat_dir.name == "manifests":
                continue
            # RPS steps are named by the wrk2 report the sweep writes.
            steps = {}
            for wrk_file in sorted(strat_dir.glob("[0-9]*.txt")):
                rps = int(wrk_file.stem)
                steps[rps] = collect_step(strat_dir, rps)
            if steps:
                strats[strat_dir.name] = steps
        if strats:
            results[scale_dir.name] = strats

    if not results:
        sys.exit(f"no scale-*x/<strategy>/<rps>.txt found under {args.results_dir}")

    report(results, pod_detail=args.pod_detail)


if __name__ == "__main__":
    main()
