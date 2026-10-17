# OrbitServe-Agent

An explicitly bounded **prediction-interface-driven rule prototype** for remote-sensing workflows. An analytical reference forecaster projects observed resource queues and known synthetic contacts; the planner selects executable whole-AOI or tiled DAGs; node-local policies select execution locations; actual scientific operators consume real imagery; actual file bytes cross constrained links before ground delivery.

**Original-checklist V0 acceptance passed after re-audit on 2026-10-17.** The lead and independent reviewer each ran the full E0–E6 suite, exited 0, and passed 248 top-level checks. The re-audit added real imagery overlays, pending energy/storage reservations, baseline process-resource records and acquisition timestamps. See the [acceptance report](docs/acceptance_report.md) and [per-requirement evidence](evidence/acceptance_checklist.json). No learned model capability is implied.

## V0 architecture

1. Observed resource/data state and a declared contact plan enter ForecastProvider.
2. ForecastGraph describes future resource budgets and directed contact intervals.
3. The rule planner selects spatial granularity and emits a PlanDAG without fixed execution assignments.
4. Four real worker processes, A (source), B (primary analysis), C (alternate analysis), and G (ground), make local decisions and admission checks.
5. A link broker routes control messages and file bytes. It enforces synthetic contacts and aggregate per-edge capacity; it does not choose execution destinations.
6. The ground validates delivered products. Actual completion observations produce a new forecast consumed by a subsequent mission plan. This feedback experiment imports measured observations across fresh worker runs; it does not claim persistent constellation state. Ground-outage continuation is validated separately within one running mission.

V0 uses no learned WM, LLM planner or MARL. The semantic harness below adds an optional external-model API interface; real-model planning quality remains unvalidated. Unavailable V0 modes fail explicitly. Node hardware, resource budgets, contacts and energy use are modeled; this is not an in-orbit deployment or a calibrated orbital simulation.

## Install

Linux with Python 3.11+, `unshare` and `bubblewrap` is required for enforced worker isolation. This environment must permit unprivileged user, mount, PID and network namespaces. Workers fail closed if isolation is unavailable. No Docker or background service is required.

```bash
python -m venv ../orbitserve-venv
. ../orbitserve-venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip install -e . --no-deps
export ORBITSERVE_DATA_ROOT="$HOME/.cache/orbitserve/data"
export ORBITSERVE_RUN_ROOT="$HOME/.cache/orbitserve/runs"
```

## Four reproducible entry points

Prepare only the required public data, preserving source metadata and hashes:

```bash
orbitserve prepare-data --data-root "$ORBITSERVE_DATA_ROOT"
```

Start/check all four isolated workers, then shut them down:

```bash
orbitserve nodes --work-dir "$ORBITSERVE_RUN_ROOT/node-check"
```

Run a real four-node water-change mission (each run starts and stops its own workers):

```bash
orbitserve run --data-root "$ORBITSERVE_DATA_ROOT" --run-root "$ORBITSERVE_RUN_ROOT" --case lake_mead
```

Run the focused E0–E6 evidence suite:

```bash
orbitserve accept --data-root "$ORBITSERVE_DATA_ROOT" --run-root "$ORBITSERVE_RUN_ROOT"
```

For an independent scientific baseline, use `orbitserve baseline`. For perturbations, `orbitserve run --scenario busy_b`, `ground_outage`, or `transfer_outage`. A saved `--plan` and `--forecast` can be reused to demonstrate unchanged DAG behavior under a runtime event.

## Data and scientific scope

Public Earth-Bench question2 and question4 supply NDVI and LST products used for TVDI analysis. The upstream task text has inconsistencies documented in the acquisition record. These products do not contain the red/NIR bands needed to claim a new spectral NDVI computation.

Three projected 5 km × 5 km Sentinel-2 L2A shoreline AOIs each contain two observation dates and B02/B03/B04/B08/B11/SCL. Continuous reflectance and categorical quality information have explicit resampling semantics. Water-change candidates use a fixed MNDWI threshold on the common valid observation mask. Unknown pixels remain unknown. Projected areas are reported in square metres.

Results are GeoTIFF masks/indices, GeoJSON change candidates, a real RGB-plus-mask PNG overlay, and JSON statistics. Two georeferenced RGBA display products travel with tile outputs; the merge does not reopen original imagery. They demonstrate computational consistency, not externally validated flood-classification accuracy. See [acquisition](docs/data_acquisition.md), [science](docs/science_methods.md), [runtime](docs/runtime.md), and [planning](docs/planning.md).

## Semantic task harness and comparative studies

The [semantic harness](docs/semantic_harness.md) accepts a natural-language product
request and an arbitrary typed DAG from an external agent, validates its dataflow,
and executes real-pixel operators. [Nine case contracts](configs/semantic_cases.json)
cover water statistics, vectors plus RGB, RGB-only, NDVI decline, strict missing
data versus authorized fallback, TVDI, and composed products. Requests include a
tool catalog and scientific contracts, without supplying the reference DAG.
Use `scripts/run_semantic.py` for request export/API/import and
`scripts/run_semantic_comparison.py` for non-agent, agent-only and
agent+forecast+offloading comparisons. Local API setup is in the harness guide.

The delivered fixture demonstration passed 45/45 contracts: 42 produced scientific
results and 3 correct missing-data responses. The semantic scientific executor is
currently serial; its two-node resource/contact schedules are a separate model.
A [prespecified world-sensitivity study](docs/semantic_world_sweep.md), reproduced
by `scripts/run_semantic_world_sweep.py`, evaluated 1,800 same-DAG schedules over
150 sampled worlds. Busy-world combined modeled time savings averaged 95.68%
(world-cluster 95% CI 94.82–96.46%); contact-closure worlds also retained substantial
regressions. These intervals describe the declared synthetic distribution.
See the [sanitized result summary](evidence/semantic_study_summary.json) and
[stopped V0 comparison scope](docs/comparison_protocol.md). Complete datasets,
products, prompts, plans and run evidence belong in the
[project data and results folder](https://drive.google.com/drive/folders/1zqRXwZfPgHyBZBihPtFXalKdsulICg8T),
not in this repository.

## Repository boundary

This repository contains code, configurations, contracts, acquisition instructions, and small sanitized evidence summaries. It intentionally excludes raw imagery, derived binary imagery/maps, large catalogs, reference answers, credentials, environments and run directories. Running the documented acquisition command obtains public inputs independently. Full local run evidence is stored outside the repository and is not silently reused as a new run.

Each run records a run ID, command, source/config/input hashes, dependency versions, actual process and tool events, forecasts, plans, decisions, transfers, product hashes, and acceptance findings. A delivered state is emitted only after ground receipt and validation.

The separate `OrbitServe-Agent-evidence-20261017.zip` delivery preserves both
fresh full suites, acquisition records, actual generated products and targeted
failure proofs. Its [hash and inventory summary](evidence/evidence_bundle_summary.json)
identify the exact bundle. Inside it, replace `/workspace/shared/` in original
records with the archive-relative `shared/` prefix; the included manifest maps
and hashes every retained file. Raw source imagery is intentionally reacquired
through `prepare-data` rather than included in the delivery.
