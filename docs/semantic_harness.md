# Semantic task harness: arbitrary agent DAGs, real pixels, explicit modeled resources

This addition leaves the V0 water-tile planner and four-node runtime unchanged.
It adds an executable interface for a real external agent to turn a natural-language
product request into typed pending tasks. It does **not** rebrand the old grain
selector or handcrafted fixtures as intelligent planning.

## What is executable

- `src/orbitserve/semantic.py`: system prompt, request export, JSON response
  validation, arbitrary DAG execution, explicit recipe baseline, API adapter.
- `semantic_ops.py`: 15 composable real-pixel operators. Only `load_bands` reads
  source imagery. Every later operator consumes declared, hash-verified artifact
  references; it cannot reopen the source case to bypass an edge.
- `semantic_quality.py`: separate product-contract checks reconstructed from the
  source samples. No reference answer or reference DAG is sent to the agent.
- `semantic_model.py`: a transparent, uncalibrated A/C discrete-event model.
- `configs/semantic_cases.json`: nine concrete cases across six families and an
  extra composed-product requirement, with fixed scientific contracts.
- `scripts/run_semantic.py`: export, fixture, import/execute and optional API call.
- `scripts/run_semantic_comparison.py`: three-arm runs and same-DAG model ablations.

A supplied plan is executed as supplied after validation. The executor does not
look up a graph by case ID, goal label, operator count or model rationale. Arbitrary
node IDs, forward-listed references, different legal branch orders and explicit
window/mosaic decomposition are supported. The deterministic `recipe()` function
is called only for the named non-agent baseline and fixture replay.

The current executor runs scientific tasks serially on one host. It is distinct
from V0's isolated four-node executor. The new semantic DAG has not been wired into
that older runtime. The A/C schedules in these results are simulated, not physical
semantic-task offloading measurements.

## Scientific task families and discriminating hypotheses

| Family | Required science/products | Meaningful difference | What is not established |
|---|---|---|---|
| Water area | Two-date MNDWI, common-valid change, JSON areas | Only needed bands and reductions; small requested final payload | A goal-aware fixed recipe can make the same choices |
| Water vectors + RGB | Same water definition, polygons, after RGB and areas | Additional vector/display branches and products are compulsory | An area-only shortcut does not meet quality |
| RGB quicklook | After B04/B03/B02, RGBA PNG | No before-date, water/vegetation index or SCL dependency | No semantic gain over an equally competent RGB recipe is assumed |
| NDVI decline | Two-date NDVI, common-valid pixel difference, raster + summary | Different spectral inputs, method and threshold semantics | Index decline is not validated vegetation loss |
| Missing data | Strict missing-B11 refusal, or explicitly authorized NDWI fallback | Permission changes feasibility and the allowed method | No acquisition or missing image is fabricated |
| Supplied NDVI/LST | Global full-AOI TVDI fit and valid threshold percentage | Larger products and a global fitting dependency constrain decomposition | The two different AOIs are not a pixelwise time series |

`water_area_mead` and `water_area_rgb_composition_mead` share the same goal label
and source data but require different output sets. A goal-only dispatch cannot
satisfy both. This composed fixture is an interface check, not held-out LLM
generalization evidence.

The water-area and water-vector cases use only B03/B11 and SCL for scientific
validity. NDVI uses B04/B08 and SCL. This is intentionally task-specific and differs
from V0's water bundle, which additionally required all five reflectance bands to
be finite. All arms of a semantic case share the same validity contract; V0's
numbers are not used as the semantic oracle. The display branch uses only RGB-band
validity and preserves nodata transparency. `load_bands` intersects finite support
across every named band in its bundle. Loading unrelated RGB bands into a water
bundle for reuse can therefore narrow its valid domain and fail the quality
contract; branch-specific source dependencies matter scientifically. Original 250×250 dimensions are used
unless the request explicitly sets `max_side`; resizing is display-only.

TVDI uses 100 equal NDVI bins, includes the maximum, fits global min/max temperature
edges with unweighted OLS, invalidates nonpositive spans and clips [0,1]. Splitting
cannot silently replace the global fit with tile-local fits. MODIS source products
are 250 m/1 km products on a 30 m export grid. No 30 m physical resolution is claimed.

## Plan interface

The exported request contains an actual system prompt plus a user context with:

- The natural-language instruction, required product roles and fixed parameters
- The visible asset catalog, source scales/offsets/times/hashes and target grid
- The typed operator catalog, concrete parameter schemas and response schema
- Current resource observations; optional forecast visibility for the WM arm

It excludes the recipe, expected node list, evaluation code, numerical reference
values, top-level evaluation notes and causal hypotheses. `case_id` is kept only
in the request envelope for bookkeeping, outside the model messages.

A successful response has `schema_version: 1`, `status: "ready"`, `nodes`,
`outputs` and a short `rationale`. Each node has `id`, `op`, `inputs` and `params`.
Input ports reference node IDs. Types come from the registry, not model assertions.
`allowed_nodes` and `not_before_s` optionally constrain the modeled schedule.
A genuine missing-data response has `status: "needs_data"`, explicit
`missing_assets`, and empty nodes/outputs.

Operators are `load_bands`, `select_band`, `index`, `threshold`, `difference`,
`water_change`, `area_summary`, `raster_summary`, `polygonize`, `rgb`, `overlay`,
`mosaic_raster`, `mosaic_mask`, `mosaic_change`, and `tvdi`.

The validator rejects unknown operators/parameters/ports, missing sources,
duplicate or unsafe IDs, undeclared references, cycles, orphan nodes that do not
contribute to any requested output, type mismatches, missing
output roles and invalid windows. Runtime rejects mismatched grids/windows and
phase order. Mosaics require exact nonoverlapping full-grid coverage; polygonizing
a mosaic avoids losing cross-window polygon topology. Artifact hashes are checked
before downstream reads. Quality gates then verify the actual requested products,
including masks, values, areas, threshold identities, polygon raster support,
geometry areas, RGB TIFF/PNG bytes and transparency.

## Three arms and honest interpretation

1. `non_agent`: deterministic, competent, goal- and product-aware composition.
2. `fixture_agent_only` / `external_agent_only`: supplied graph, local A model.
3. `fixture_agent_wm_offload` / `external_agent_wm_offload`: supplied graph,
   forecast-visible dynamic A/C model. The exported execution policy explicitly
   states which modeled nodes the selected arm can use.

Fixture mode intentionally replays the same valid recipe for the agent arms.
Thus its science, requested products and graph can tie the non-agent baseline.
This verifies adapters, contracts and scheduling comparisons; it is **not** a
measurement of model understanding, task-generation quality, token cost or agent
performance. Real comparisons require separately generated responses from the
actual model for each request/visibility arm.

`--model-controls` also evaluates local/current, local/forecast,
dynamic/current and dynamic/forecast on the **same** DAG and measured artifact
sizes. This separates modeled offloading and forecast effects without rerunning
science. The `normal` case is a generous connected control; `future_contact` has
the same initial observation but a declared near-future contact gap;
`busy_local` has a future local reservation. Parameter values, forecast/truth
identity, candidate evaluations and chosen schedules are saved. These are
hypothetical explanatory scenarios, not a learned world-model evaluation.
Model v2 also lets the current-only policy observe reservations already active at
its decision time, including the observed release; future unstarted reservations
remain hidden. The optional `environment_override` argument supplies a copied,
explicit shared world for controlled parameter sweeps; default behavior uses the
saved scenario definitions. Existing scientific products can be replayed through
a newer model, but those modeled records must retain their separate model version.

A current-only policy blocked on a closed contact waits for an actual environment
contact/resource event and then reevaluates the newly observed state. It is not
penalized with an artificial immediate failure simply because it has no forecast.

The per-node greedy scheduler is **not a globally optimal DAG scheduler**. In the
future-contact counterexample it can place early work on C, miss the return window,
and finish around 20 modeled seconds, while an all-local water-area graph finishes
in 1.8925 modeled seconds. A hand-authored imported plan with `allowed_nodes: ["A"]`
expresses that better decision and preserves science. Conversely, the declared
busy-local scenario permits an all-C manual witness at about 0.327 modeled seconds
versus 16.893 locally. These witnesses show an expressible theoretical opportunity,
not that an actual agent discovers it. No parameters were retuned to manufacture
an agent win. A real agent must account for the whole graph and return contacts,
not merely the cheapest next operator.

### Measurement boundaries

- `execution.tool_wall_s`: actual sum of operator elapsed time, including file I/O.
- `execution.wall_s`: whole local harness, including validation, scientific
  oracle, operator execution and model evaluation; it excludes API planning.
- `planning.wall_s` and `planning.usage`: actual API latency/usage only when a
  real request is made and its sidecar is supplied. Price is not assumed.
- `source_bytes_read`: legacy field name for the sum of full referenced source
  file sizes per load, including repeated windows. It is **not** measured disk
  I/O, compressed window bytes or decoded-memory bytes.
- Artifact sizes are exact data-file sizes, excluding `artifact.json`. The same
  convention feeds model transfer costs and requested-product volume. These are
  local file sizes, not observed network traffic.
- Model replay uses **completed-artifact sizes measured after scientific
  execution**, shared by every compared policy. This is offline counterfactual
  scheduling, not evidence that an online agent or WM predicts intermediate sizes.
- `modeled.completion_s`: uncalibrated simulated time until required products
  are back at A. There is **no separate A→G downlink** in this model. Never add
  modeled seconds to host wall time or present the result as measured speedup.
- The network model does not globally serialize concurrent link traffic or
  model packet loss. Resource profiles and perfect mock forecasts are explicit
  assumptions. A single smoke pass cannot establish statistical significance.

A correct missing-data response passes its contract but is counted separately
from completed scientific products. No fake zero-area result counts as success.

## Local use, without sharing an API key

Install the repository in your chosen Python environment and prepare or reuse
its real input data. Keep data and runs outside the checkout.

```bash
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps -e .
export ORBITSERVE_DATA_ROOT="$HOME/.cache/orbitserve/data"
python scripts/prepare_data.py --data-root "$ORBITSERVE_DATA_ROOT"
```

Export a self-contained model request and a clearly labeled replay fixture:

```bash
python scripts/run_semantic.py export-request \
  --case-id water_area_mead --arm agent_only --output requests/area.json
python scripts/run_semantic.py fixture \
  --case-id water_area_mead --output fixtures/area.json
```

Execute any separately prepared plan. No fixture fallback occurs if it is invalid:

```bash
python scripts/run_semantic.py run --case-id water_area_mead \
  --arm external_agent_only --plan responses/area.json --run-dir runs/area-import
```

For a compatible chat-completions endpoint, configure the API key **only in your
local environment**, with `ORBITSERVE_API_KEY`; do not put it in a JSON file,
command argument, report or commit. Set `ORBITSERVE_API_ENDPOINT` to the full HTTPS
chat-completions URL and `ORBITSERVE_API_MODEL` to the model name. A different key
variable can be selected with `--key-env`. Localhost HTTP is supported for local
adapters; remote plaintext HTTP and redirects are refused.

```bash
python scripts/run_semantic.py call-api --case-id water_area_mead \
  --arm agent_only --output plans/water_area_mead__normal__agent_only.json
python scripts/run_semantic.py call-api --case-id water_area_mead \
  --arm agent_wm_offload --output plans/water_area_mead__normal__agent_wm_offload.json
python scripts/run_semantic_comparison.py --mode external \
  --plans-dir plans --case-id water_area_mead --scenario normal \
  --run-root runs/real-agent-area --model-controls
```

The adapter sends `model`, `messages`, JSON-object response format and temperature
0 using the ordinary chat-completions interface. Provider-specific reasoning or
sampling APIs may need a small adapter extension; unsupported options fail
explicitly. It records the request, returned plan and a `.planning.json` sidecar
with latency and usage. Headers/keys are never persisted, and error bodies are
suppressed to avoid credential echoes. The comparison runner automatically reads
matching planning sidecars. Merely setting `mode: real_api` in an untrusted sidecar
is not independent proof of real inference; retain the original request/response
and provider evidence when reporting model results. No live provider request was
made during this implementation. A loopback fixture-server smoke test did verify
HTTP request shape, JSON response parsing and returned-plan validation with a
non-secret dummy value; it did not perform model inference or provider validation.

Requests for different resource scenarios should be exported/called with the
matching `--scenario`. The comparison runner requires scenario-qualified names:
`CASE_ID__SCENARIO__agent_only.json` and
`CASE_ID__SCENARIO__agent_wm_offload.json`. This prevents silently reusing a plan
generated for a different forecast context. It refuses missing external plans instead of substituting
fixtures. For full evaluation, generate both visibility-arm plans for every case,
repeat model trials, retain invalid plans and include planning latency/cost in any
end-to-end performance claim.

Run the replay smoke matrix, without any API credentials:

```bash
python scripts/run_semantic_comparison.py --mode fixtures --scenario normal \
  --run-root runs/fixture-normal --model-controls
```

Every run stores its actual plan, input contract, per-node hashed products,
execution timing, independent quality checks and modeled schedule. Run directories
must be empty; existing evidence is not overwritten. Suite manifests freeze source
hashes and stop if implementation changes mid-matrix.

## Modeled parameter-sweep follow-up

The [shared-world sweep protocol](semantic_world_sweep.md) describes the separate
factorial replay using the same real artifacts across execution controls. Its
statistics describe robustness across the declared modeled worlds. They do not
replace real-agent trials, repeated hardware measurements or prediction-error
calibration. Model v2's current-busy observation and environment-override behavior
were independently checked before that sweep.
