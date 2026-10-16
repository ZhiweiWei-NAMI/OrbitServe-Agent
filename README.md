# OrbitServe-Agent

An explicitly bounded **prediction-interface-driven rule prototype** for remote-sensing workflows. An analytical reference forecaster projects observed resource queues and known synthetic contacts; the planner selects executable whole-AOI or tiled DAGs; node-local policies select execution locations; actual scientific operators consume real imagery; actual file bytes cross constrained links before ground delivery.

**V0 local E0–E6 acceptance passed on 2026-10-16.** The lead's full suite exited 0 and the independent validator passed 168 suite checks. See the [acceptance report](docs/acceptance_report.md) and [machine-readable evidence](evidence/acceptance_summary.json). This is a rule prototype; no learned model capability is implied.

## Architecture

1. Observed resource/data state and a declared contact plan enter ForecastProvider.
2. ForecastGraph describes future resource budgets and directed contact intervals.
3. The rule planner selects spatial granularity and emits a PlanDAG without fixed execution assignments.
4. Four real worker processes, A (source), B (primary analysis), C (alternate analysis), and G (ground), make local decisions and admission checks.
5. A link broker routes control messages and file bytes. It enforces synthetic contacts and aggregate per-edge capacity; it does not choose execution destinations.
6. The ground validates delivered products. Actual completion observations produce a new forecast consumed by a subsequent mission plan. This feedback experiment imports measured observations across fresh worker runs; it does not claim persistent constellation state. Ground-outage continuation is validated separately within one running mission.

Learning-based WM, LLM planning and MARL are **not implemented or validated**. Unavailable modes must fail explicitly. Node hardware, resource budgets, contacts and energy use are modeled; this is not an in-orbit deployment or a calibrated orbital simulation.

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

Results are GeoTIFF masks/indices, GeoJSON change candidates, a PNG map, and JSON statistics. They demonstrate computational consistency, not externally validated flood-classification accuracy. See [acquisition](docs/data_acquisition.md), [science](docs/science_methods.md), [runtime](docs/runtime.md), and [planning](docs/planning.md).

## Repository boundary

This repository contains code, configurations, contracts, acquisition instructions, and small sanitized evidence summaries. It intentionally excludes raw imagery, derived binary imagery/maps, large catalogs, reference answers, credentials, environments and run directories. Running the documented acquisition command obtains public inputs independently. Full local run evidence is stored outside the repository and is not silently reused as a new run.

Each run records a run ID, command, source/config/input hashes, dependency versions, actual process and tool events, forecasts, plans, decisions, transfers, product hashes, and acceptance findings. A delivered state is emitted only after ground receipt and validation.
