# RPS sweep vs independent runs — design discussion

Context: this benchmark directory is for **scaling-on (HPA-enabled) benchmark 1.5b**
(see `../../../benchmark-plan.md`). Comparing baseline istio vs `st5-AttUpd`.

Current setup: each rps point is its own independent 120 s wrk2 run, repeated 10
times. Between runs HPA resets pods to baseline, so every measurement starts
cold.

## Observation that triggered the discussion

Plot `gnuplot_combined.dat` shows a non-monotonic p50 for istio: 800 rps spikes
to 4 s, 900 rps dips back to 2 s, then climbs again. Initially this looked like
a measurement artifact. With HPA in the loop it isn't — it's the mechanism the
benchmark is supposed to expose:

- 800 rps over-saturates the current pod count → p50 spikes.
- HPA reacts, spins new pods → at 900 rps the larger fleet absorbs the load and
  p50 falls back.
- After 1000 rps, load climbs faster than HPA can react → latency climbs again.

`st5-AttUpd` saturates ~300 rps earlier than istio (knee at ~700 rps vs ~800
rps) and never recovers, because its per-request verification cost is higher
and HPA has further to climb.

## Why independent runs are wrong for this benchmark

- 120 s per run is probably shorter than HPA's stabilization window (~15 s for
  scale-up decision plus scrape lag, longer to settle). Every measurement is
  catching a transient, not a new steady state.
- HPA resets between runs, so every rps point starts from baseline pod count
  regardless of where the previous point left off. The plot's x-axis (rps) is
  the only thing varying, but the y-axis is being served by a different fleet
  size at each point — and the fleet is changing *during* the measurement.
- It does not match what 1.5b is supposed to measure: realistic
  production-representative deployment where load builds and HPA reacts.

Independent runs remain the right design for **1.5a** (scaling off), where the
fleet is fixed and you want a clean steady-state characterization per rps.

## Proposed design: monotonic continuous sweep

Run a single continuous wrk2 sweep that steps through rps levels without
draining between steps. Capture pod count throughout.

### Refinements beyond "before and after pod count"

1. **Sample pod count continuously (every 5–10 s), not just at step
   boundaries.** Within a 120 s window at the knee, pod count changes
   mid-flight; the latency you measure is being served by a changing fleet.
   A timeseries of `(t, pod_count, target_rps)` lets you (a) plot pod count
   alongside latency on a shared time axis, or (b) slice the wrk2 detailed
   log into pre/post scale-up sub-windows for cleaner attribution.

2. **Make each step longer than HPA's stabilization window.** HPA defaults
   are ~15 s for the scale-up decision; with metrics-server scrape lag and
   pod startup, getting to a new steady state takes longer. Target **240–300 s
   per step at the knee region and beyond**. Early steps (100, 200 — far
   under saturation) can stay shorter; nothing is happening there.

3. **Monotonic increasing only — never decreasing.** HPA's scale-down
   stabilization window is 5 min by default. A sweep that goes up then down
   will measure the down-leg with pod count carried over from the up-leg,
   contaminating both. If a return leg is needed, drain to idle and start
   fresh.

### What not to lose

Independent-runs gave 10 repeats per rps, which is where the pooled-histogram
analysis (`plot_percentile_lines_pooled.py`) gets its statistical power. The
switch to continuous sweep must still do **N full sweeps** (e.g., 5–10
complete runs of the sweep) so per-rps variance is recoverable by pooling
across sweep repeats. Otherwise it's a single sample per rps and the variance
work disappears.

## Plots to produce

- **Top panel** — p50/p90/p99 latency vs rps. Still the headline.
- **Middle panel** — pod count vs rps. The capacity story: how many pods each
  scheme had to scale to in order to serve a given rps.
- **Bottom panel** — latency-per-pod or rps-per-pod vs rps. The efficiency
  story: this is the comparison that lets st5 win or lose on its actual merits
  (per-pod capacity), not on raw latency-vs-rps which conflates request cost
  with fleet size.

Annotate HPA scale-up events on the latency plot (vertical lines at rps where
pod count stepped) so the "dip" is self-documenting and a reader doesn't need
the surrounding prose to interpret it.

## Open questions / things to pin down

- HPA target threshold (CPU? requests/sec?) must be **identical** for istio
  and st5-AttUpd. Even small differences contaminate the comparison.
- Min/max replicas for HPA — should be wide enough that neither scheme hits
  the ceiling within the sweep, otherwise the comparison stops being about
  the scheme and becomes about the cap.
- Sweep step size and dwell time per step (see "240–300 s at the knee" above
  — finalize once HPA stabilization timing is measured on this cluster).
- Number of full-sweep repeats for pooling (5? 10?).
