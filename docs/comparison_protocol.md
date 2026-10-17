# Comparative prototype study (V0, stopped early)

**Delivered scope:** this original study was stopped when the user changed the
research priority. It contains 51 outcomes: 48 actual delivered results and 3
full-grid memory preflight infeasibilities. The remaining 111 planned outcomes
were canceled. Only first-repeat (`r1`) outcomes were executed; there are no
three-repeat estimates or confidence intervals. The 36-run supplementary study
was canceled before execution. Sections below describe the original planned
design unless explicitly stated otherwise.

The experiment driver is `scripts/run_comparison.py`. It compares actual local
scientific computation and real byte transfers under a synthetic application-byte
contact-capacity model. It does **not** measure flight hardware, ns-3 links, LLM
planning, MARL, or a learned world model.

## What decomposition means in this implementation

This study does not test semantic Agent task decomposition. The planner does
not read the natural-language question or invent a workflow from a goal. It
always constructs one or more `water_tile` steps followed by one `water_merge`.
Only tile windows and their count vary; the scientific operator family and
fork-join dependency pattern are fixed. The CLI also supplies a fixed two-tool
skill catalog. The narrow, implemented behavior is **template-internal data
partitioning**. Runtime selection then decides **where** each ready step runs.

A semantic planner would instead need to interpret the requested result, select
appropriate registered operators, construct and validate their input/output and
dependency contracts, and adapt the workflow to task requirements or observed
results. Such behavior need not use an LLM, but it is not present in this V0 planner.
The machine identifiers `agent_plan_static` and `agent_plan_dynamic` are arm
identifiers only; they are not evidence that this missing capability exists.
In V0 the semantic-planning objective was unimplemented. The subsequently added
[semantic harness](semantic_harness.md) accepts and executes arbitrary validated
typed DAGs, including external plans. Its real-API planning quality remains
unvalidated; explicit fixture replay is not evidence of model intelligence.

Source anchors: `planning.py:108-113,127-177,196-202`, `cli.py:81`, and
`worker_node.py:337-370`. Congestion gains in this study establish a node-selection
and offloading effect, not a semantic decomposition effect.

## Experimental controls

The main design crosses two factors:

- Predetermined 128-pixel windows versus the existing rule planner selecting
  original-grid, 128-pixel, or 64-pixel windows from analytical resource/contact
  forecasts.
- Fixed B execution versus the existing received-advertisement rule choosing a
  feasible execution node for each ready DAG step.

The first arm is called `no_agent_fixed` in machine-readable results. This label
means that neither grain nor placement is chosen adaptively. It still uses the
same isolated worker runtime, admission controls, transfer protocol, control
advertisements, and deterministic DAG builder. Fixed B entails A-to-B data
transfer; it does not mean zero physical offloading or execution on source A.
A has a 4 MiB modeled reservation budget and lacks the merge capability, so
forcing a purported no-offloading baseline onto A would be an invalid control.

The fixed128 baseline is intentionally feasible under every main-study resource
regime. A full-grid fixed-B sensitivity control additionally avoids hiding the
benefit of a well-configured fixed workflow. The planning candidate node set is
identical across arms; fixed placement is imposed only after grain generation by
setting every step's allowed nodes to B. The full-grid sensitivity is not in the
main 2×2 denominator.

Three public Sentinel-2 lake AOIs retain their original 250×250, 20 m grid, all
scientific validity rules, and MNDWI threshold zero. Four conditions are tested:
normal; B and C with actual configured 11 MiB admission budgets; an unannounced
25-second B admission gate; and 375,000 bytes/s directed links instead of
1,500,000 bytes/s. The gate is a synthetic perturbation, not measured CPU load.
The memory condition changes runtime budgets and observations together, rather
than using a paired-test forecast fixture. Every run starts in fresh isolated
node stores. All methods retain the same cache behavior.

The planned design assigned three repetitions per case-condition-arm; the
delivered study stopped during the first repetition. Cases and arms are run
sequentially with rotated ordering, never as competing simultaneous workloads.
The complete order is written to `protocol.json` before execution. Scientific
algorithms and decisions are deterministic; there is no algorithmic random
seed. Wall-time scheduling noise remains and is reported descriptively.

## Outcomes and failure accounting

`runs.jsonl` and `runs.csv` contain every attempted outcome, including infeasible
preflight controls. Ground-delivery time is the validated G delivery event on
the run monotonic clock. It excludes planner execution and shutdown; the
separate total-delivery metric adds measured forecast and DAG construction.
Undelivered or preflight-rejected runs have no fabricated delivery time.

Payload bytes sum each actual `chunk_bytes` once on each link. Serialized
control, transfer-begin headers, and ACK bytes are reported separately. Neither
metric includes stdio/base64 carrier expansion or claims physical radio-wire
measurement. Policy timers cover `choose_action` only, excluding observation
construction, logging, advertisements, and broker work. Planning timers cover
forecast and DAG construction, including the common builder in fixed arms.

The configured compute rates influence planning and node-selection estimates;
they do not throttle the scientific Python operators. All four workers execute
on a common Linux host. Memory and energy are modeled reservation/accounting
quantities, not measured satellite RSS or electrical power. Per-job elapsed
scientific time is measured, but it must not be interpreted as heterogeneous
hardware performance.

## Verification

Every delivered result undergoes independent pixel-level scientific
recomputation, input and received-product hashes, process-isolation probes,
capacity accounting, resource admission, dependency ordering, original-grid
coverage, and receipt-before-computation checks. Every recorded policy decision
is replayed from its recorded local state and received advertisements.

The original acceptance verifier is retained unchanged. Its
`DAG_not_preassigned` assertion is deliberately inapplicable to a fixed-placement
ablation. For static controls only, comparative acceptance replaces exactly that
assertion with checks that every allowed set is B and every actual execution is
on B. Both the original verdict and the disclosed comparative verdict are kept;
no scientific, transport, or resource check is waived.

A full-grid candidate that exceeds the 11 MiB modeled budget is explicitly
`PREFLIGHT_INFEASIBLE`, not a completed run, an execution timeout, or a zero-second
outcome. The robust fixed128 controls remain the primary comparison there.

## Interpretation boundaries

A rule planner can avoid an infeasible predefined grain and can reduce needless
subtasks when the original grid fits. A competent preconfigured fixed workflow
may achieve the same result. Dynamic assignment can avoid a received busy-node
advertisement, but can also duplicate complete case transfers and add overhead.
No method is assumed to win. Each worker's first use still receives the complete
case package; tiling does not itself crop network input. Different tile counts
also change provenance metadata size. These effects must be attributed to the
actual logs rather than described as pixel compression.

Classified areas are threshold-derived water-change candidates. Exact agreement
across arms and with the independent implementation is an implementation check,
not ground-truth flood accuracy. Small selected AOIs and the incomplete first repetition do not establish
population-wide or operational performance or support repeat-based confidence
intervals.

## Reproduction

Run from the repository with its declared dependencies and the prepared public
inputs:

```sh
python scripts/run_comparison.py --data-root /path/to/data --run-root /path/to/new-comparison
```

The initial protocol records source inventory, ordered runs and definitions.
A resumed driver rejects changes to the source inventory. Raw node outputs and
logs remain under `runs/`; group statistics, per-pair descriptive differences,
figures and unchanged representative scientific products are written separately.
Supplementary sensitivities must retain a separate protocol and disclose a
separate timing phase rather than claim contemporaneous pairing.
