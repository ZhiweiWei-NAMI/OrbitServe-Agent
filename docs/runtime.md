# Four-node runtime

## Actual boundary

`run_distributed(plan, case_dir, run_dir, scenario='normal', config=None, forecast_graph=None)` launches
four separate Python worker processes. Node A alone receives a copy of the
case's declared assets and metadata. B and C initially contain no imagery. G
holds the initial plan and sends it to A over the same link broker used for all
other traffic. The actual ForecastGraph accompanies the plan. If not passed
explicitly, it must already exist at `run_dir/forecast.json`; identity must match
the plan. Policies price directed input and return contacts from this received
graph, never from an invented symmetric advertisement rate. A owns dependency
discovery and submission, not placement.

Each node imports its own rule policy. A considers only its own measurements and
received, timestamped neighbor advertisements. A bounded number of ready steps
can be outstanding concurrently. B and C independently accept or reject jobs
against their current capability, memory-workset budget, storage, energy, queue,
and concurrency budgets. Reserved memory includes accepted and waiting jobs.
Full queues are advertised unavailable. Decisions, advertisements, admissions,
request receipts, actual tool states and provenance are recorded.

The default A has insufficient workset memory for analysis. B and C have the
same scientific capability with different resource budgets and estimated
compute rates. G only has delivery capability. These are configuration values,
not node-selection branches in the policy. Increasing A's capacity permits the
same local admission and scientific harness path. The source never writes a
remote queue. The default maximum outstanding source steps is two, which allows
actual overlapping B/C work for a tiled DAG.

## Enforced filesystem, process and network isolation

The launcher uses `unshare --user --map-root-user --net` followed by bubblewrap
mount, PID, IPC and UTS namespaces. The network namespace has no usable
interfaces. The outer `unshare` route avoids a platform restriction on
bubblewrap's loopback-configuration netlink socket; it does not share the host
network. Landlock was probed and is unavailable on this execution platform.

Workers see their own writable storage at `/node`; independent host directories
are mounted at that same private path. They cannot see another node, the host
source data, evaluation/reference directories or the broker's output directory.
Only the interpreter, dependency trees, system libraries and four source files
are read-only mounted. The source allowlist is `__init__.py`, `worker_node.py`,
`policy.py`, and `science.py`. Hidden simulator scripts, node configurations,
planning and forecasting implementation are not mounted into workers.

Every run first executes the actual sandbox negative probes: host workspace and
raw data absent, peer path absent, source writes return EROFS, external TCP
connection returns ENETUNREACH, own-root writes succeed. A failed probe blocks
the run. `isolation.json`, worker mountinfo, actual externally observed worker
PIDs, namespace inode IDs and private-root mappings preserve evidence. PID 2 in
each worker is expected because each has its own PID namespace; host worker
PIDs are separately measured and distinct.

This is one-host process simulation, not physical heterogeneous satellites.
Workset and energy budgets are modeled admission limits, not claimed hardware
RSS or measured power. Accepted, queued, waiting-for-input and running jobs reserve their declared memory,
energy and storage estimates. Advertisements and admission subtract all pending
reservations, rather than spending the same future budget twice. Memory and
storage reservations are released at a terminal job state. Each attempted job
charges its configured energy estimate once, including a failed attempt; this is
an accounting model, not a power measurement. Observations expose reserved,
remaining and physically used values separately.

Storage admission is deliberately conservative: pending-work storage estimates
are added to measured current node-file bytes, even when part of a job's input
has arrived. Archive construction and incoming transfers separately check
actual disk-byte headroom, including the temporary coexistence of ZIP and
extracted members and other in-flight incoming reservations. Output files are
checked before artifact registration. These are application budget checks, not
filesystem quotas or a promise to bound transient OS allocation while an
operator writes; an exceeded budget fails the job rather than delivering it.

The E0 single-host harness records Linux process CPU times and peak RSS at every
job state. Those measurements are explicitly baseline-host process observations,
not fabricated satellite capacity or per-job peak-memory measurements.

There is no artificial scientific-compute sleep. Actual
operator duration and zero simulated extra compute delay are logged separately.

## Real bytes, resumability and control constraints

A creates an uncompressed ZIP of a node-local artifact. It reads real bytes in
32 KiB chunks into its IPC output. The broker relays these bytes; it never opens
worker files. A destination writes only its private `.part` file. It checks the
archive SHA-256 and every extracted member's SHA-256, size and path before
registering a computable artifact. A service request can wait for its inputs,
but a job cannot run from a partial transfer. Locally produced artifacts instead
have same-node tool-success provenance and do not require a fictional transfer.

The broker owns one shared token bucket per directed edge. Every file chunk and
serialized control message consumes that same budget. The default application
rate is 1,500,000 bytes/s, with a 65,536-byte maximum accumulated burst. Base64 is
only the local pipe carrier; capacity counts decoded payload plus serialized
control bytes, explicitly not physical network framing overhead. `capacity_budget`
and `capacity_charge` events expose every actual token refill and charge,
including packet kind and edge, so acceptance can verify aggregate capacity
rather than infer it from packet delivery timestamps.

Plans, advertisements, requests, request receipts, completion receipts, transfer
headers, transport ACKs and final delivery receipts all use the gated broker.
Transport ACKs consume reverse-edge capacity before a subsequent chunk is
requested. Broker-to-worker source pulls are local transport flow control, not
application RPC or scheduling. Logs are a separate observation-only channel.

A link closure stops edge charges and deliveries. The receiver preserves its
validated contiguous partial offset. Resume continues at that byte offset;
`paused`/`resumed`/`chunk` records disclose retained and retransmitted bytes.
The transfer-outage test triggers only after a real input transfer has delivered
at least 15% and less than 100% of its bytes. No synthetic data products are used.

## Ownership and delivery

Each dispatch has a unique job ID and stable plan/step artifact identity. A never
submits a second active job for the same step. Retries follow explicit rejection;
accepted jobs are not duplicated. A caches only artifact possession learned from
receipts. Duplicate in-progress transfers of the same artifact to the same
node are coalesced. Dependency receipts and verified local result bytes jointly
make the next step ready. The merge receives local tile directories and does not
reread source imagery.

A satellite-produced final artifact is `partial` until G receives and verifies
it. G opens all mandatory GeoTIFFs, parses statistics and GeoJSON, verifies the
PNG, and checks all file hashes. Only G's actual successful validation changes
the returned status to `delivered`. `outputs_dir` points to G's received product
directory, not a copy of another node's output.

## Scenarios and evidence

All contacts are explicitly `synthetic_contact_trace` and run against the
monotonic wall clock at 1x. The forecast's known contact plan contains the public
windows. Hidden test disturbances are only applied by the broker when they
become actual environment state:

- `normal`: configured contacts and normal local queues.
- `busy_b`: B observes its own injected current queue/busy state; other nodes learn
  it only from gated advertisements or rejection receipts. The input DAG is
  unchanged.
- `ground_outage`: all G links close after A confirms the plan is installed, for 12 seconds.
  Satellite dependency discovery, new step admission and computation continue.
- `transfer_outage`: the first eligible actual A-to-compute partial transfer
  closes its bidirectional link for 1.5 seconds, then resumes.

`runtime_result.json` contains process records, actual result status, exact ground
output path, byte counts and final worker observations. Feedback observations
carry `source_run_id` and `feedback_event_ids` referring to real log events.
The append-only JSONL channels are `decisions`, `tool_calls`, `transfers`,
`node_events`, `control_messages`, `observations`, `plans` and `forecasts`.
The scientific and runtime acceptance verdicts are independently computed by
`verification.py`; runtime completion alone is not an acceptance claim.

## Commands

Install the project as documented in the main README, then:

```sh
python -m orbitserve nodes --work-dir "$RUN_ROOT/new-node-check"
python -m orbitserve run --data-root "$DATA_ROOT" --run-root "$RUN_ROOT" \
  --case lake_mead --scenario normal
python -m orbitserve run --data-root "$DATA_ROOT" --run-root "$RUN_ROOT" \
  --case lake_mead --scenario ground_outage --plan "$PLAN" --forecast "$FORECAST"
python -m orbitserve verify "$RUN_DIR"
```

A node check starts all four isolated workers, exchanges actual gated
advertisements and shuts them down cleanly; it does not fabricate imagery.
`get_job(run_dir, job_id)` returns the latest persisted harness state.
`default_config()` and `observed_snapshot()` provide the planner's initial
configuration and timestamped snapshot without hidden scenario state.
