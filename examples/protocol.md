# Executor protocol reference

The controller appends two argv elements to the frozen `executor_argv`: the
literal `exchange` and one canonical JSON request. No shell evaluates the JSON.
The executable returns exit status zero and one JSON receipt on stdout. Stderr is
diagnostic only. Each raw status, stdout, stderr, or transport failure is retained in a protected
exchange artifact. Nonzero status, malformed JSON, a binding mismatch, or a changed
native job retains the pending request and blocks the campaign.

The executable and its environment are a trusted deployment boundary. The
worker must not modify either, the contract, or controller files. Receipts are
authenticated by that channel and protected storage, not by a model assertion.
There is no second signing service or pricing estimator.

## Request and receipt records

`forge_kernel._campaign.Request` and `Receipt` are the canonical schemas. All
models reject extra fields. A request contains `id`, `campaign`, the canonical
contract SHA-256, `generation`, `op`, current `reference`, original
`reporting_anchor`, `judge`, `protocol`, `hardware`, and operation `payload`.

The controller commits the full request before the first exchange. Repeating an
ID with different bytes is a protocol error. Repeating an ID with identical bytes
must look up and reconcile the original effect, even if its native job ID was
not returned. The executor must commit intent before submitting hardware work.
It must not equate a missing native job ID with permission to submit another job.

A receipt has these fields:

```json
{
  "request_id": "the exact request ID",
  "request_digest": "SHA-256 of canonical request JSON",
  "issuer": "the frozen deployment issuer",
  "status": "final",
  "job": "stable native effect ID",
  "payload": {}
}
```

`status` is `final`, `pending`, or `unknown`. A pending or unknown response with a
known job uses its stable ID on every subsequent reply. Before a native ID exists,
return a nonzero transport result or use a stable adapter effect ID as `job` for
its entire lifetime. Do not later replace that ID. The full request digest binds
every frozen field, candidate, incumbent, proposal, and confirmation reference.

Canonical JSON uses sorted keys, compact separators, UTF-8, default JSON ASCII
escaping, and disallows NaN and infinity. `canonical` and `digest` in `_campaign.py`
implement the encoding. The controller archives a valid final receipt before
applying it. A final receipt is immutable; correcting an invalid final protocol
response requires a deployment audit, not changing archived evidence.

## Operations

| Operation | Input payload | Final payload |
| --- | --- | --- |
| `preflight` | actual worker/environment `identities`, `controller_path`, required capability names | `capabilities`, exact `identities`, exact `controller_path`, permitted coding `seed` snapshot, nonempty `evidence` |
| `allowance` | `history_digest`, `known_cost`, frozen `cost_limit` | matching `history_digest`, finite nonnegative `remaining_cost`, nonempty `evidence` |
| `analyze` | pinned `knowledge_snapshot` | source-safe reference `summary`, nonempty `evidence` |
| `freeze` | plan digest, complete decision IDs, invalidated IDs, attempt index | immutable `source` identity and `artifact` reference |
| `evaluate` | immutable `candidate`, optional `incumbent` | `Measurement` below |
| `restore` | current-generation accepted `source` and snapshot, or its permitted coding seed | matching `source`, nonempty `evidence` |
| `confirm` | accepted candidate and discovery receipt ID | independent `Measurement` in a new native job |
| `probe` | scoped proposal, accepted candidate, confirmation receipt | `certified`, `independent`, `source_safe`, exact `scope`, nonempty `evidence` |
| `admit` | certified `probe_receipt` and exact proposal | immutable `entry`, matching `probe_receipt`, `private: true`, nonempty `evidence` |
| `seal` | confirmed candidate and confirmation receipt | next `reference`, matching `source`, matching `confirmation`, nonempty `evidence` |

Every operation uses the same request-ID reconciliation rule. Probe certification
must independently test the proposed mechanism and minimal example within the
stated scope; simply re-running candidate correctness is insufficient for native
certification. A failed probe is retained and cannot enter the private catalog.

`Measurement` contains the exact candidate snapshot, `correct`, per-name `gates`,
positive finite `candidate_time` and `reference_time`, `incumbent_time` when an
incumbent exists, `same_job: true`, `independent`, and nonempty raw `evidence`
references. Gates are `pass`, `fail`, `unresolved`, or `deferred`. An incorrect or compile-failed candidate instead returns `Failure`: the candidate,
`correct: false`, gates, a failure `reason`, nonempty evidence, and independence.
It contains no timing fields. Only valid measured results count toward stagnation.
Gain is `reference_time / candidate_time - 1`; improvement over the incumbent uses
`incumbent_time / candidate_time - 1`. Timing units and estimator belong to the frozen protocol. The adapter measures all comparison
sources in the same job and preserves raw evidence; a model-written float is
never a measurement. Confirmation must use a different job from discovery.

`allowance` is the deployment's verified remaining campaign allowance, including
known reservations and spending outside this process. Its request binds the
campaign and durable usage history. It is not permission to reset an exhausted
budget. The controller passes the smaller of this allowance and the frozen
remaining cost to the runtime. An uncertain model turn blocks before another
allowance or paid turn. Runtime usage deltas record known history; they do not
resolve missing or disputed billing.

## Required capabilities

The canonical set is `REQUIRED_CAPABILITIES` in `_executor.py`: source isolation,
controller protection, request-ID reconciliation, immutable snapshots, restoration,
same-job comparison, fresh confirmation, source-bound seals, independent probes,
private catalog admission, and budget authority. Preflight must bind these
properties to the actual supplied runtime identities and controller path. The
adapter must verify enforcement, not blindly echo the request. Preflight runs for
each generation and supplies a permitted coding seed. The adapter must remove
previous-generation source access before admitting that generation. Restoration
must never expose an opaque reference source to the coding workspace. An unavailable
capability blocks the campaign before model work.

## Executable offline example

`python -m forge_kernel.demo` creates an exact contract with the active Python
executable and invokes this exchange as a subprocess for every operation:

```text
/absolute/python -m forge_kernel.demo --executor-root /absolute/output/executor exchange CANONICAL_JSON
```

Its source is `fixture_exchange` in `forge_kernel/demo.py`. It preserves one
transactional receipt per request and demonstrates cache recovery. Only its fake
worker and small CPU examples are supported. Capability attestations and timing
values are software fixtures. This command is not a native deployment adapter.
