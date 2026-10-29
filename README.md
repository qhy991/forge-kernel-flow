# ForgeKernel

ForgeKernel is a bounded [Humanize](https://github.com/humanfia/humanize) flow for
kernel optimization campaigns. One Ralph-style worker takes fresh architecture
and coding turns. A deployment-owned executor freezes sources, measures them,
restores rejected changes, and confirms each promotion.

The repository includes a complete offline software demonstration. Its worker is
fake, its correctness checks run on CPU, and its timings are synthetic fixtures.
It provides no GPU results or performance acceptance. Native deployment requires
a task-specific executor with the capabilities in [the protocol](examples/protocol.md).

## Run the offline demonstration

Use Python 3.12 or newer. The package pins Humanize to the source revision used by
its actual-runtime tests.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m forge_kernel.demo --output /tmp/forge-kernel-demo
.venv/bin/python -m pytest -q
```

Choose a new output directory. The demonstration runs the public `hmz.flows.load`
entry through the runtime's `run_fake`, with explicit worker and environment
drivers. Every evaluator exchange invokes an external process with a fixed argv.
It completes two generations, writes `campaign/report.json`, and retains each
request, receipt, candidate snapshot, probe, and private fixture catalog entry.
The report starts with `OFFLINE SOFTWARE FIXTURE: NO GPU; NO PERFORMANCE ACCEPTANCE`.

The fixture executes only the tiny Python examples produced by its fake worker.
Do not use its capability declarations or synthetic timings to admit a native
hardware campaign or execute untrusted candidate code.

## Bind a native task

Prepare a frozen contract using [the fixture contract](examples/contract.json) as
a field reference. Replace its executable argv, operator, judge, protocol,
hardware, references, limits, issuer, and knowledge snapshot with your task's
values. `task_instructions` holds any task-specific instructions that must remain
fixed across resumes. Do not point the demo executor at a real kernel task.

The executor must enforce source isolation and protect the controller directory
from the worker, including across generations. Separate working directories,
`chmod`, fresh sessions, and the worker's permission declaration do not prove
isolation. Preflight must attest the actual runtime worker and environment
identities supplied in the request. Unsupported deployments stop before model
work.

The following command uses the pinned Humanize CLI. Replace the uppercase values
with deployment bindings before running it. Running this native command can use
paid model turns and the task's hardware allocation.

```bash
hmz exec -f /absolute/forge-kernel-flow/forge_kernel \
  -a worker=codex/MODEL:EFFORT \
  -e analysis=local/absolute/analysis \
  -e code=local/absolute/code \
  -e executor=local/absolute/trusted-executor \
  -p contract=/absolute/task.json,campaign=/absolute/controller-campaign \
  -p budget.cost=5,budget.duration=1h 'FROZEN TASK TEXT'
```

The native executor may need container or remote environment bindings instead
of local directories. Its immutable argv is the only trusted command the flow
runs. Every generation re-enters preflight before reference analysis or a model turn.
The adapter supplies a permitted coding seed and clears prior-generation sources.
Rejection restores that seed or a current-generation incumbent; it never copies
an opaque reference into the coding workspace. Model replies cannot select an evaluator or supply accepted timings.
[agent.md](forge_kernel/agent.md) is package data and is read into every worker
prompt. Workers also respect applicable workspace instructions; the flow never
writes workspace `AGENTS.md` or `agent.md` files.

## Resume a campaign

Invoke the same flow with the same task, contract, campaign directory, worker
identity, and environment identities. Humanize's journal is optional and is not
the campaign authority. The controller reads `campaign.json` under an exclusive
writer lock. Every replacement and immutable result file uses checked file and
directory `fsync`. Storage failures propagate.

A pending executor operation resumes with its exact full request and ID, even
when no native job ID was returned. The adapter must reconcile by request ID;
it cannot submit another effect. A receipt cached before a controller crash is
imported once. Confirmation, probes, admission, and sealing use the same path.

A model turn with no durable result is uncertain paid work. The controller blocks
instead of replaying it. Resolving that uncertainty requires an external audit;
this release does not expose a manual reconciliation command. Each new model
turn requires an executor allowance bound to the campaign and its durable usage
history. Known usage comes from actual runtime deltas. A fresh runtime's zero
usage does not reset the campaign budget.

## Read the result

`campaign.json` is the only mutable controller authority. `report.json` and the
runtime journal are projections. Immutable `request-*.json`, `exchange-*.json`, `receipt-*.json`, and `turn-*.json`
files preserve evidence. The report distinguishes the structural incumbent,
measured best, confirmation, promotion, terminal stop, and blocked pending work.

A correct initial implementation can become the structural incumbent below
reference parity. Only valid same-job stagnation counts toward reopening
architecture. Correctness failures restore the current-generation incumbent, or its permitted coding seed, without incrementing
stagnation. Infrastructure failures keep the request pending. A plan revision
invalidates changed decisions, their descendants, and entire touched atomic
groups. Required gates must all pass; deferred and unresolved gates cannot
promote a candidate.

Gain uses `reference_time / candidate_time - 1`; incumbent improvement uses the
same formula with the incumbent timing. Promotion requires a separate confirmation
job above both the frozen noise and
target thresholds, followed by a source-bound seal. The original reporting anchor
never changes. Knowledge proposals enter the next generation's pinned snapshot
only after an independent, source-safe probe and private admission. Probe failure
retains the negative result and does not fabricate a certified entry.

See [the executor protocol](examples/protocol.md) for the deployment contract and
[the tests](tests/test_flow.py) for recovery, budget, and gate examples. The software
checks do not establish native sandbox enforcement, GPU correctness, benchmark
quality, or a real optimization improvement.
