# Independent original-checklist review (A06)

Status: PASS for the bounded V0 rule prototype after reopening the original requirements, correcting concrete omissions, and executing fresh evidence. This is not acceptance of a learned WM, LLM planner, MARL, real orbital geometry, or flight hardware.

Reviewed computational commit: `dc5fa469d08479c9d2a48cee850a542605e43aae`. Computational inventory SHA-256: `f2b7e0393e8670b1acd3cbbe91846ca4f779e31ad90680629d1f960851ffe1ce`. Native reviewer task: `/root/audit_original_orbitserve_checklist`.

## What was reopened and corrected

All three original supplied files were read in full, including the authoritative execution checklist and JSON dispatch/acceptance template. Earlier all-PASS summaries were not treated as runtime evidence: their full external data/run directories were absent in the reopened environment.

- E0 explicitly required viewing real overlays. Earlier maps contained categorical masks alone. Fresh products include correctly scaled B04/B03/B02 RGBA GeoTIFFs and transparent water/change layers over real imagery. Merge consumes received tile products, not a hidden raw-data read.
- D02 acquisition-time metadata was incomplete for Earth-Bench. Exact successful URL/hash receipt timestamps are now retained; date-only observations remain date-precision rather than invented instants. Sentinel times match pinned STAC and original crop receipts.
- Baseline harness calls omitted resource observations. Every state now records Linux process RSS high-water and CPU counters, explicitly separate from modeled satellite budgets.
- Pending jobs previously failed to reserve storage/energy against later admissions. The reproduced low-budget case could promise 1.5 J against 1 J and 120 bytes against 100 bytes. Current admission subtracts outstanding reservations; actual-run lifecycle verification independently reconstructs reservations, available capacity and one-time modeled energy debits.
- The README CLI installation was checked before the final freeze. Generated packaging metadata is excluded from the computational inventory; code/config/script hashes remain recorded.

Forty public input TIFFs were reacquired and verified. These are new acquisition and execution records, not recovered historical traces.

## Four exact README commands executed

The reviewer used a separate copied input cache and previously absent run root at `/workspace/shared/orbitserve-a06-readme-20261017`. All four commands exited 0:

```sh
orbitserve prepare-data --data-root "$ORBITSERVE_DATA_ROOT"
orbitserve nodes --work-dir "$ORBITSERVE_RUN_ROOT/node-check"
orbitserve run --data-root "$ORBITSERVE_DATA_ROOT" --run-root "$ORBITSERVE_RUN_ROOT" --case lake_mead
orbitserve accept --data-root "$ORBITSERVE_DATA_ROOT" --run-root "$ORBITSERVE_RUN_ROOT"
```

Environment variables pointed to the reviewer's `data` and `runs` subdirectories. The executable came from `/workspace/shared/orbitserve-venv/bin/orbitserve`, installed using the README instructions. Command text, stdout, stderr and exit codes are under `commands/`. Prepare-data validated all 40 cached TIFF hashes unchanged, matching the frozen originals, and read 0 new COG response-body bytes. This verifies the cache path; the separate restoration record verifies the fresh download.

Representative run: `20261017T002822Z-E1-dcfea0c7`. Independent full suite: `20261017T002828Z-acceptance-cbb560f2`, exit 0, **248 top-level checks PASS**. Full suite path: `/workspace/shared/orbitserve-a06-readme-20261017/runs/20261017T002828Z-acceptance-cbb560f2`. The lead's separate suite is `20261017T002642Z-acceptance-a649ed0d`; it is not substituted for the reviewer run.

## Independent scientific and visual evidence

The verifier imports none of the author science, forecast, planning, policy or runtime implementations. It decodes source pixels and scaling, reconstructs masks and indices, independently fits TVDI edges, counts areas, and rasterizes GeoJSON back onto the source grid. Fixed tolerances remain exact categorical equality, 1e-6 absolute continuous error, and 1e-6 m² area error.

For each water AOI the ground receives 14 TIFFs, GeoJSON, PNG and JSON. The two RGBA rasters are display products; the original 12 scientific rasters remain unchanged. RGB channels, alpha, grid, source URL/hash/scale identity and all three final map panels are independently reconstructed. All three real overlay maps were visually inspected, with the exact PNG hashes matched to the final ground-delivered products in `visual_delivery_evidence.json`. Changing one RGBA sample, one PNG sample, or one source hash in disposable copies makes the relevant check fail.

| AOI | Common-valid pixels | Gained m² | Lost m² | Unknown m² |
|---|---:|---:|---:|---:|
| Mead |51877|232000|722000|4249200|
| Powell |61341|66000|2073600|463600|
| Shasta |62228|44000|2013200|108800|

Each grid is 250×250 at 20 m; pixel area is 400 m². Earth-Bench TVDI fractions remain 28.07009226449461% and 14.936468942106828%. They concern supplied valid pixels, not a missing agricultural land-cover mask or validated drought accuracy. A fresh evaluator-only ablation on these inputs reproduces the reference 14.98% when the last fitting bin excludes its maximum: 671,566 above-threshold pixels (14.98184508219908%), versus the production 669,532 pixels (14.936468942106828%) when included. The 2,034-pixel difference is recorded in `reference_method_sensitivity.json`; output and tolerances were not tuned to the answer.

## Causal runtime evidence

- E2: identical observed state, source data and scientific settings; only future forecast fixture budgets differ. Executed whole run `20261017T002851Z-E2-whole-26d296e1` has one 250 px tile plus merge (2 steps). Tiled run `20261017T002856Z-E2-tiled-938d00ce` has four 128 px/boundary tiles plus merge (5 steps), with B/C execution. Every raster and statistic is equivalent. Formal E1 forecasts are analytical references, not fixtures.
- E3: saved plan and forecast are identical in the whole baseline and `20261017T002902Z-E3-e25a4a22`. The baseline executes tile/merge on B; the locally observed busy-B case executes both on C. No preassigned execution-node list changes.
- E4: in `20261017T002908Z-E4-4361e1e6`, ground closes at 0.149970496 s and remains closed until 12.149970496 s. The merge becomes ready at 2.275145321 s, starts at 2.633307246 s and succeeds at 2.890487604 s. This is a newly ready dependency-bearing operation during the outage, with no delivered ground control/data in the closed interval; delivery follows restoration.
- E5: in `20261017T002922Z-E5-3de0b42f`, a 1,243,848-byte real input ZIP pauses at 196,608 bytes when the link closes at 0.362717384 s. It resumes at the same retained offset after 1.862717384 s; no retransmitted payload is claimed. Chunk/source/destination hashes, shared-edge charges, unavailable partial files and execution order all pass.
- E6: `20261017T002930Z-E6-e1317fa9` runs in a previously absent independent-rerun root, with new job IDs/materialized products and the same source/input/plan. Scientific equivalence passes.

Actual namespace isolation, distinct worker processes, input-arrival-before-compute, per-node decisions, shared directed capacity and resource/capability effects were checked from files and correlated event records. Real elapsed times remain separate from synthetic contacts/resource/energy models.

## Boundaries and evidence index

Feedback imports actual completed-mission observations into a subsequent plan with an explicit clock rebase. Each mission starts fresh workers/resources; no persistent constellation-state claim is made. Water changes remain threshold-derived candidates without ground-truth classification accuracy. MODIS products supplied on 30 m grids retain their coarser native-information limits. Planner costs are estimates, not an optimality or speedup result.

`evidence/original_checklist_audit.json` maps A00–A17 to original clauses and current material evidence. `evidence/independent_review_summary.json` records source identity, commands, hashes and exact ground product paths. Full correlated runtime records remain in the external reviewer directory above. This review certifies the files actually inspected now; it does not claim that missing historical directories were recovered or that a separate portable archive is already published.
