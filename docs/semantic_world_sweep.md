# Semantic DAG world-sensitivity study

This fast study replays completed scientific DAGs and their measured artifact
sizes through a declared event model. It evaluates placement and forecast
mechanisms; no new real-model calls or host-performance repetitions are created.
The goal-aware recipe and fixture agent use identical graphs in this phase.

## Frozen design

- Seed `20261017`; 50 independent worlds in each of `normal`, `busy_local`, and
  `future_contact`, with independently spawned random streams per family.
- Three products within every world: water-change statistics, RGB-only, and
  water-change statistics plus polygons plus RGB. These are correlated repeated
  measurements within a world, not three additional independent worlds.
- Four controls share each exact DAG, artifact sizes, source placement, memory,
  compute parameters, network parameters and environmental truth: local/current,
  local/forecast, dynamic/current, dynamic/forecast. The current-only policy can
  observe already-active resource reservations and actual link changes, and can
  wait for a new observed event. It cannot inspect future unstarted events.
- Source A compute rate is log-uniform 250,000–1,000,000 declared work units/s;
  C/A compute ratio is log-uniform 0.5–12; bandwidth is log-uniform
  200,000–50,000,000 bytes/s. Thus remote execution is not always faster.
- Busy-world A reservation starts uniformly at 0.02–0.15 s and lasts uniformly
  10–60 s. Contact-world first open window lasts uniformly 0.01–0.2 s, followed
  by a uniformly 5–60 s closure. Normal worlds have neither perturbation.
- All controls have a 60 s deadline and 600 s model horizon. Required products
  returning to A define completion; no independent A-to-ground downlink is added.

The distributions are synthetic sensitivity assumptions, not estimates of
satellite operating conditions. Confidence intervals describe Monte Carlo
uncertainty under these declared distributions. They do not establish empirical
satellite performance or geophysical accuracy.

## Analysis

Effects are paired within world and product. Positive savings mean improvement.
For pooled percentages, first calculate each product's percentage saving, take
the equal-weight mean of the three products inside each world, then average the
50 world values. Absolute seconds are aggregated the same way. A fixed-seed,
5,000-resample percentile bootstrap resamples whole worlds to form 95% intervals.
Families are reported separately so a normal/busy benefit cannot hide a contact
failure. Separate contrasts isolate offloading without forecasts, incremental
forecasts with offloading available, their combined effect, and forecasts with
local execution only. Percentages from different contrasts must not be added.

Also report median completion, deadline success, and fractions improved or
worsened by more than 10%. The pooled median describes 150 product outcomes per
family/control; it does not imply 150 independent samples. Every attempt remains
in status/deadline denominators. If incomplete results arise, finite timing
comparisons are explicitly conditional, and excluded pairs/worlds and failure
rates must be reported. The released study had no incomplete model outcomes.

## Reproduce

After completing the scientific matrix, prepare the protocol and all worlds
before running any model outcome:

```sh
python scripts/run_semantic_world_sweep.py \
  --source-study /path/to/normal-final --run-root /path/to/new-sweep
# Inspect protocol.json, worlds.json, source_inputs.json and the frozen hashes.
python scripts/run_semantic_world_sweep.py --run-root /path/to/new-sweep --run
```

The driver refuses changed code, changed input science, resampling in an existing
root, and overwriting raw outcomes. It writes all 1,800 schedules to
`raw_results.jsonl`, flattened rows to `metrics.csv`, world-paired effects and
intervals to `effects.json`/`effects.csv`, and descriptive arm statistics to
`arm_summary.json`/`arm_summary.csv`. `protocol.json` records ranges, seeds,
analysis, source hashes and planned denominators before execution.

Original scientific results remain unchanged when a model is revised. The
separate `model-v2-replay` artifact links the 45 original scientific runs to 180
updated representative control schedules, using the same completed artifacts.
Do not relabel that model replay as another 45 scientific executions.
