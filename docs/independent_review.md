# Independent verification review (A06)

Status: PASS for independently executed E0–E6 checks, all four README entry points, and the additional root-requested fresh representative run. This bounded result supports the rule prototype only; the lead owns the complete A00–A17 delivery report. Verified source revision: `93fdaed`.

## Scope and independence

Reviewer owns only `src/orbitserve/verification.py`, `scripts/verify_run.py` and this review. The verifier does not import scientific, planning, forecast, policy or runtime implementation functions. It reads source imagery, asset metadata, materialized products and correlated execution evidence directly. Review of author source code is complementary evidence, not a substitute for files and execution.

Fixed before inspecting outputs:

- Categorical rasters: every pixel must agree exactly, including unknown/nodata.
- Continuous indices: absolute tolerance 0.000001, relative tolerance zero. Independent calculations are rounded to the specified float32 output storage before comparison. LST is likewise compared with its float32 representation; this does not claim microkelvin physical precision.
- Projected map-grid area: absolute tolerance 0.000001 square metres. Counts must be exact.
- Classification uses the original 250 by 250, 20 m projected grid, MNDWI greater than zero and common valid coverage. NDVI and NDWI are diagnostics.

## Scientific checks

Water validation reads each original asset, checks SHA-256 and grid metadata, decodes the metadata scale/offset once, calculates all retained indices independently and derives SCL/finite-value masks. It reconstructs both phase masks, common validity and all five change categories. It then recounts pixels and calculates projected pixel area from affine determinant and CRS unit conversion. GeoJSON changes are independently rasterized back to the source grid and must reproduce the gained/lost pixels. PNG files must decode.

TVDI validation checks actual NDVI/LST source bytes and physical scaling. It uses explicit interval predicates for 100 NDVI bins and scalar centered ordinary least-squares calculations for wet/dry envelopes. The implementation under review instead uses searchsorted, indexed reductions and polyfit. The independently calculated TVDI, validity and greater-than-0.75 categories must reproduce the outputs. No reference answer is used to choose numerical parameters.

Source hashes are checked against the pinned Hugging Face LFS SHA-256, or the Git blob SHA-1 for the small non-LFS question2 LST file, in addition to independent local-file SHA-256. Sentinel crop hashes and the bounded HTTP range receipt ledger are checked. Whole original COG hashes are not claimed because only required byte ranges were requested.

## Material defect found and corrected before formal acceptance

The Sentinel Earth Search catalog carried a declared reflectance offset of -0.1 while the item-level `earthsearch:boa_offset_applied` flag was true. Reapplying the offset would subtract a radiometric correction twice. Author A02 raised the discrepancy; A01 corrected the effective offset and preserved the original advertised metadata and source flag. A06 independently opened the provider maintainer's [offset explanation](https://github.com/Element84/earth-search/issues/9) and the [subsequent anomaly report](https://github.com/Element84/earth-search/issues/71).

Validation now requires explicit harmonization provenance and checks the retained flags against the original pinned STAC items. Effective zero offset is accepted only when the flag supports it. Old outputs with uncorrected metadata are not accepted as final scientific evidence. Merely reproducing the same erroneous scale arithmetic would not establish correctness.

## Execution evidence checks

- Four distinct host worker PIDs and PID/network/mount namespace IDs; actual negative isolation probes and per-worker mount evidence.
- Initial raw imagery only under A's private root. Each worker's mount exposes only its own private node data and read-only dependencies/source.
- Every transferred ZIP is independently hashed, its member hashes checked, and its destination files reopened and hashed. Chunk offsets and chunk byte hashes must exhaust the actual ZIP exactly.
- Every tool input must have a prior verified destination receipt, or a prior successful same-node producing tool with matching artifact/file hashes. This distinction permits genuine local dependency reuse without inventing a network receipt.
- Per-directed-edge capacity budgets and actual charge records are checked arithmetically, including cross-tick token credit, data bytes, serialized control bytes and transport acknowledgments. Completed-packet timestamps alone were insufficient to prove a token bucket's shared capacity; this was repaired by adding the actual charge ledger.
- Decisions are tied to actor processes and local observations. Every received neighbor/admission observation must trace to a prior delivered control message. Resource and capability rejection reasons are checked against their observed values and the actual chosen action.
- Job lifecycle order, materialized output hashes, dependency order, exact executed DAG coverage and per-worker concurrency are checked.
- Ground delivery requires a verified transfer, physical ground product files and another independent scientific recomputation.

## Experiment-specific gates

E2 requires identical observed state, input bytes and scientific constraints, a future-only forecast intervention, changed tile windows and actual DAG size, execution of both plans and equivalent products. E3 requires byte-equivalent plans/forecasts/inputs and an actual execution-node change following locally observed runtime perturbation. E4 requires a dependency-bearing step newly ready after disconnection to start and finish while ground is disconnected, with no delivered ground traffic during closure. E5 requires a nonzero partial offset, pause/resume at the same retained position, no closed-link charges and no consumer before verification. E6 requires a new run, root, jobs, materialized product timestamps, recorded successful command, matching source/input/plan and equivalent scientific products.

Feedback validation allows only the documented explicit rebase between separate run clocks. Original source timestamps and observation age are retained; non-time measurements must remain unchanged. The new forecast must be consumed by the subsequent actual plan.

## Executed reviewer checks

- Real Earth-Bench question2 and question4 independently recomputed successfully: 28.07009226449461% and 14.936468942106828% of valid TVDI pixels exceed 0.75, respectively. These are supplied-scene descriptive percentages, not agricultural-mask statistics or validated drought accuracy.
- All five final-scope cases were independently recomputed. All three water whole/tile products agree, including independently rasterized GeoJSON geometries. Mead: common valid 51,877; gained 580; lost 1,805. Powell: common valid 61,341; gained 165; lost 5,184. Shasta: common valid 62,228; gained 110; lost 5,033. Pixel area is 400 m² in each. All three categorical quicklooks were opened and visually checked.
- Focused negative checks in disposable output copies changed one reported count and one output category pixel. The scientific verifier rejected each mutation, and product comparison rejected the corrupted classification. Evidence: `/workspace/shared/orbitserve-runs/a06-negative-checks/verifier_mutation_checks.json`.
- Source inspection confirms scientific calculations do not read reference answers; the merge reads task metadata and received tile products. Forecast/policy do not import runtime/science/data. The broker does not call the policy or science operators.

## README command verification

The exact README prepare-data command was executed against a separate copy of the valid input cache, with the documented environment variables set to that copied data root and a fresh reviewer run root. It exited zero. All 40 input TIFF SHA-256 hashes were unchanged before/after, and the original frozen inputs were unchanged. No new COG response body bytes were required. This verifies the cached-acquisition path, not a second cold download. Evidence: `/workspace/shared/orbitserve-runs/a06-readme-20261016T1342/prepare_proof.json`, `prepare.stdout.log`, `prepare.stderr.log` and `prepare.exit_code`. The nodes command, fresh run command and full accept command subsequently all exited zero. Logs and exit-code files are in the same reviewer root. The fresh README run is `20261016T134418Z-E1-928861f6`. The independently executed suite is `20261016T134508Z-acceptance-0a1a15b6`; its acceptance JSON contains 168 top-level PASS checks and no FAIL/BLOCKED checks, including nested scientific/process/transfer evidence.


## Terminal experiment evidence

Reviewer suite: `/workspace/shared/orbitserve-runs/a06-readme-20261016T1342/20261016T134508Z-acceptance-0a1a15b6`.

- E0: both Earth-Bench cases and all three AOIs independently recomputed.
- E1: all three AOIs delivered through the four-worker harness and agreed with E0.
- E2: unchanged observed state and science, but future-only memory budgets selected one 250-pixel tile plus merge (2 DAG steps) versus four 128-pixel/boundary tiles plus merge (5 steps). Both executed and all raster categories agreed exactly.
- E3: the same saved plan ran tile and merge on B normally and on C with B busy. Products agreed.
- E4: ground closed at 0.177229 s and reopened at 12.177229 s. The dependency-bearing merge became ready at 2.252331 s, started at 2.576710 s and succeeded at 2.803620 s, entirely during the outage. Ground control/data did not pass during closure.
- E5: a real 1,254,142-byte input archive paused at byte 196,608 and resumed at that exact retained offset after the 1.5 s closure. No retransmitted payload bytes were claimed or observed. Source/archive/chunk/destination hashes and all capacities were independently checked; no consumer started early.
- E6: a fresh subprocess run in a previously absent output root produced new job IDs and execution traces with the same plan, code and inputs, and equivalent products.

The separately requested post-suite command also exited zero:

`orbitserve run --data-root /workspace/shared/orbitserve-data --run-root /workspace/shared/orbitserve-runs/root-requested-rerun --case lake_mead`

New run ID: `20261016T134554Z-E1-1c4e8ac1`; actual wall time 5.019809 s. Ground products are under that run's `nodes/G/artifacts/9055e24c758feb60ea1f0d8a`. Independent runtime verification and direct comparison with the lead's formal E0 both passed. Command stdout, stderr, exit code and complete independent recheck are in `/workspace/shared/orbitserve-runs/root-requested-rerun-command/`.

## Earth-Bench reference discrepancy resolved

Evaluation references were consulted only by A06 after the operators and independent recomputation were already fixed. Question2 rounds to its reference 28.07%. Question4's reference 14.98% is reproduced by the [pinned upstream method](https://github.com/opendatalab/Earth-Agent/blob/ca1dfaef750654331308a434966d307f1728fd90/agent/tools/Index.py#L923), which excludes maximum NDVI from the last fitting bin. A controlled independent ablation gave 14.98184508219908% (671,566 pixels) with that exclusion and 14.936468942106828% (669,532 pixels) when the maximum is included. The difference is 2,034 pixels, or 0.04537614 percentage points. Float32 versus float64 did not change either count. Production outputs and tolerances were not altered to match the answer. Evidence: `reference_method_sensitivity.json` in the reviewer root.

## Claim limits

This is an analytical-reference forecast and rule-planning/rule-execution prototype. No learned world model, LLM planner or MARL is implemented or validated. The four processes share one host; compute roles/resources, contacts, link capacities and energy are models, not space-qualified hardware or orbital-geometry validation. Feedback is measured observation import between separate missions; each new mission starts fresh workers/resources. It does not establish a persistent constellation. Planner costs are conservative serial estimates, not optimal-schedule claims. Water changes are threshold-derived candidates without ground-truth accuracy labels. MODIS NDVI/LST source information is much coarser than the supplied 30 m grid and has differing temporal support.
