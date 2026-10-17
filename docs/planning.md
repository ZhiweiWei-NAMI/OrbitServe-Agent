# Forecast-driven rule planning and local policy

## Scope and replacement points

`forecast.forecast(history, contacts, horizon_s, candidate_workload=None,
source_kind="analytical_reference")` is an observed-state queue/energy model,
not a learned world model. `learned_wm` raises `NotImplementedError`; unknown
sources fail rather than silently changing implementation.

`planning.plan(task, assets, skills, forecast_graph)` produces a water analysis
DAG. `task.planner_mode="llm"` raises `NotImplementedError`. The implemented
mode is `rule`. `policy.choose_action(local, step, received_ads, forecast)` is
also rule-only; a `policy_mode="marl"` value raises `NotImplementedError`.
These checks are actual dispatch behavior, not only documentation.

All public values are JSON-compatible dictionaries. These three modules never
import the runtime, simulation scenarios, science outputs, or evaluation data.
The planner reads its public `configs/planning.json`, and accepts overrides in
`task.planning_config`; the worker policy performs no file or network I/O.

## Analytical reference

An observation contains `timestamp`, `nodes`, and optional asset/link metadata.
All times, including contact intervals, use seconds on the same run-monotonic
clock. Each node includes `node_id`, `capabilities`, `compute_pixels_s`,
`memory_bytes`, `storage_bytes`, `energy_j`, `queue_work_pixels`, and optional
`available_after_s`, `busy_until_s`, `energy_per_pixel_j`.

Observed values remain unchanged in `observed_snapshot`. For future time t,
service capacity is rate × max(0, t − available_time). The model drains known
queued work, limited by the available modeled energy. Queue endpoints, remaining
energy and spare compute capacity are evaluated over four horizon subdivisions
plus exact availability/queue-drain boundaries. Memory, storage and peak compute
rate remain explicitly static assumptions. The provider assumes no unobserved
arrivals; it does not predict runtime outage or congestion scripts.

Candidate workloads may be a node→pixel-count mapping, a `nodes` mapping, or a
list of `{node_id, compute_pixels}` records. They add explicitly conditioned work
to each named node's queue. The original argument is retained in `conditioning`.
The forecast records observation count/digest and `feedback_event_ids` supplied
by actual observations. Energy is a configurable estimate, not hardware power.
Uncertainty is explicitly `not_estimated`; no numerical confidence is invented.

Contacts are directed records with `source`, `destination`,
`throughput_bytes_s`, `latency_s`, and `windows=[[start_s,end_s], ...]` (or one
`start_s`/`end_s` pair). Source/destination aliases and bandwidth-byte-rate aliases
are accepted for integration. Overlapping windows within a record are merged;
only their intersection with the forecast horizon contributes predicted bytes.
Manual contact records default to `synthetic_contact_trace`. An observation or
contact missing necessary identity/rate fields fails explicitly.

`mark_expired(graph, now_s)` returns an explicitly marked copy without changing
its identity, issue time or cutoff. The planner rejects an expired graph. A
worker may still continue a previously issued plan using current observations;
its decision records whether the old forecast is expired.

## Grain selection and scientific invariants

Candidates are the original full grid, 128-pixel tiles and 64-pixel tiles.
The actual dimensions define edge windows, including non-square/odd grids.
Each candidate contains one `water_tile` step per half-open row/column window
and one `water_merge` depending on every tile. No overlap/halo is needed for
this per-pixel MNDWI calculation. The sum of window areas equals the original
AOI. No candidate changes pixel spacing, threshold, validity rules or coverage.

Configured memory accounting is 8 MiB operator base + 104 bytes per analyzed
pixel for a tile, and 8 MiB + 24 bytes per full-grid pixel for merge. The added 8 bytes per pixel account for two four-band display products. Final-product transfer/storage estimation uses 48 bytes per pixel to include the real overlay PNG, vectors and metadata as well as rasters. These are
explicit working-set reservations, not claims about measured Python RSS.
Compute work is the actual window area. Energy is work × configured J/pixel.
Storage includes inputs and estimated products. The runtime must independently
check and reserve its actual current modeled budget before accepting work.

For each step, the planner evaluates future segments and the received contact
forecast. It rejects missing capabilities, insufficient memory/storage/energy,
missing input/return contact capacity, and estimates outside the forecast
horizon. Feasible costs include contact wait, actual-metadata input bytes /
throughput, forecast queue wait, compute / rate, configured startup and result
return. Window gaps contribute waiting and partial contacts cannot carry more
bytes than their modeled capacity. If actual asset sizes are absent, the
explicit conservative raster estimate is 12 uint16 bands × original grid area.
The runtime currently sends complete case packages on first use; therefore the
planner charges whole-package input for every tile and assumes no cache savings.

The selected score is a conservative serial sum of minimum feasible step costs.
This is a grain comparison estimate, not a distributed placement schedule; it
may overestimate execution if the runtime overlaps work or reuses received
inputs. Per-step hypothetical node/segment costs, all candidate peak working
sets, total intermediate bytes and completion estimates remain in `selection`.
The planner does not promise an optimal runtime makespan.

Every step has capability, memory/energy/storage/work estimates, input references,
dependencies, and `allowed_nodes`. At least two currently observed nodes must
really provide each analysis capability. The default allowed set includes all
observed nodes so runtime decisions explicitly show resource/capability rejection;
it does not override those constraints. There is no `assigned_node` field.
`owner_node`, `submission_owner`, and `result_owner` refer to the source's right
to submit/track the task, not the node that executes it.

The merger receives dependency products as node-local `params.tile_dirs`, which
the runtime fills after complete hash-verified receipt. It does not read original
imagery. Delivery means G has all required GeoTIFF, GeoJSON, PNG and JSON files
and their validated bytes; producing a file on a satellite is not delivery.

## E2 paired fixture

`paired_test_forecasts(history, contacts, horizon_s=120)` returns `(ample,
constrained)` with `source_kind="test_fixture"`. Both retain identical
`observed_snapshot`, conditioning, topology and science inputs. Only future
analysis memory budgets change (and forecast identity/fixture metadata).
The ample budget is bounded by observed physical capacity; the constrained
budget is 11 MiB for analysis nodes initially having at least 16 MiB. A's
4 MiB source budget is left unchanged.

For a 250×250 grid, the full tile requires 14,888,608 bytes, a 128×128 tile
requires 10,092,544 bytes, and merge requires 9,888,608 bytes. Thus the constrained
forecast makes whole processing infeasible while four edge-aware tiles and
merge fit. The same general planner chooses the lowest-cost feasible grain;
it never inspects `fixture_name` or a scenario flag. The two plans still need
actual execution and independent scientific comparison for E2 acceptance.

## Local execution policy

Every worker must call `choose_action` in its own process. The inputs are its
own current resource state, a ready step, timestamped advertisements actually
received over links, and a previously received forecast view. The policy never
uses `node_forecasts` to obtain a remote node's current resources or queue.
Only the newest received advertisement from each neighbor is considered.
Missing timestamps, advertisements older than 30 seconds, and future-dated
advertisements beyond clock tolerance are rejected. The age bound is configurable
in `local_observation.advertisement_max_age_s`.

Ads may carry `transfer_rate_bytes_s`, `contact_wait_s`, and optional reverse-link
`return_rate_bytes_s`, `return_wait_s`. Without these summaries, received edge
forecasts estimate input and return opportunities. A link estimate is not an
admission guarantee. Observed busy time and queued work produce a queue-delay
estimate; costs sum wait + transfer + queue + compute + required return.

The chosen result is `local`, `request` or `wait`, with destination and complete
candidate details. Capability, allowed-node, memory, storage, energy, service,
contact, age and deadline failures are explicit. `request` sets
`admission_required=true`; the sender cannot inject work into a neighbor queue.
The destination independently checks its current state before executing or
queuing. A normal faster B can win; a newly received B-busy advertisement can
make C cheaper with the exact same PlanDAG. The policy contains no B preference
or scenario check. Ground G's capability and source A's memory are actual
rejection reasons, not labels included solely for display.

## Feedback integration

After actual tool/transfer completion, collect the timestamped resource
observations emitted by workers and receipt event IDs; do not synthesize them
from a scenario name. Use those observations as a new history entry and call
`forecast`. Record the new forecast and its cutoff/feedback event IDs. Call
`plan` for the next planning request with this new object and persist its
`forecast_id`. A valid acceptance chain contains actual completion → observation
→ new forecast → consumed plan, with monotonic event times. Merely producing
an unused feedback forecast does not pass the feedback requirement.

## Author checks and acceptance boundary

The initial metadata smoke test used a 250×250 task and four timestamped node
metadata records. It verified whole vs four-tile DAGs with identical observed
snapshots; nonfixture analytical output; A memory and G capability rejection;
and B→C selection after a received B queue increase. This demonstrates module
behavior only. Real-AOI W05, end-to-end policy process invocation, E2 scientific
equivalence and actual feedback acceptance must be established by integration
run artifacts and independent review, not inferred from the smoke result.
