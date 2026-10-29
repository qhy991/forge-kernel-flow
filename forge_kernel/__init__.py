"""One bounded Ralph worker with an independent deployment evaluator."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from hmz.flows import (
    Agent, AgentCollection, Budget, Env, EnvCollection, FilesEnvMixin,
    FlowParams, Permission, PermissionKind, ShellEnvMixin, flow,
)
from pydantic import ValidationError

from ._campaign import (
    Blocked, Contract, Implementation, Plan, Snapshot,
    Store, Turn, canonical, digest, evaluation, invalidated,
)
from ._executor import (
    REQUIRED_CAPABILITIES, Admission, Allowance, Analysis, Executor,
    Preflight, Probe, Restored, Seal,
)


class Worker(Agent):
    _permission = Permission(local=PermissionKind.ALL, user=PermissionKind.NONE,
                             system=PermissionKind.NONE, online=PermissionKind.NONE)


class Agents(AgentCollection):
    worker: Worker


class Workspace(Env, FilesEnvMixin):
    pass


class Evaluator(Env, ShellEnvMixin):
    pass


class Envs(EnvCollection):
    analysis: Workspace
    code: Workspace
    executor: Evaluator


class Params(FlowParams):
    contract: str
    campaign: str


def identities(agents, envs):
    worker = agents["worker"]
    return {"worker": {"role": worker.role, "harness": str(worker.harness),
                       "model": worker.model, "effort": worker.effort},
            "envs": {name: {"role": envs[name].role, "backend": str(envs[name].backend),
                            "provider": envs[name].provider, "workdir": str(envs[name].workdir)}
                     for name in ("analysis", "code", "executor")}}


async def activate(state, store, executor, worker, envs, ctx, schema, role, prompt):
    """A missing model result is unknown paid work, never an automatic retry."""
    if state.turn is None:
        history = digest(state.usage)
        receipt = await executor.exchange("allowance", {"history_digest": history,
                                           "known_cost": state.spent,
                                           "cost_limit": state.contract.model_cost_limit})
        allowance = Allowance.model_validate(receipt.payload)
        if allowance.history_digest != history:
            raise Blocked("allowance does not bind durable usage history")
        remaining = min(allowance.remaining_cost, state.contract.model_cost_limit - state.spent)
        if remaining <= 0:
            raise Blocked("model budget exhausted")
        executor.consume(receipt)
        state.turn = Turn(id=uuid4().hex, prompt=prompt, schema_name=schema.__name__, role=role,
                          allowance_receipt=receipt.request_id)
        store.save(state)
        before = ctx.usage
        try:
            session = await worker.spawn()
            result = await worker.run(prompt, session=session, env=envs[role],
                                      output_schema=schema, budget=Budget(cost=remaining, graceful=False))
        except Exception as error:
            after = ctx.usage
            state.usage.append({"action": state.turn.id, "cost": after.cost - before.cost,
                "output_tokens": after.output_tokens - before.output_tokens,
                "duration_seconds": (after.duration - before.duration).total_seconds(), "uncertain": True})
            raise Blocked(f"uncertain model turn {state.turn.id}: {type(error).__name__}") from error
        after = ctx.usage
        usage = {"action": state.turn.id, "cost": after.cost - before.cost,
                 "output_tokens": after.output_tokens - before.output_tokens,
                 "duration_seconds": (after.duration - before.duration).total_seconds()}
        result_data = {"result": result.model_dump(mode="json"), "usage": usage}
        store.write(f"turn-{state.turn.id}.json", result_data, immutable=True)
    turn = state.turn
    if turn.prompt != prompt or turn.schema_name != schema.__name__ or turn.role != role:
        raise Blocked("persisted model turn conflicts with phase")
    saved = store.root / f"turn-{turn.id}.json"
    if not saved.exists():
        raise Blocked(f"uncertain model turn {turn.id}; no automatic replay")
    record = json.loads(saved.read_text())
    result = schema.model_validate(record["result"])
    if not any(u["action"] == turn.id for u in state.usage):
        state.usage.append(record["usage"])
    return result


def worker_prompt(state, kind):
    common = {"task": state.task, "operator": state.contract.operator,
              "generation": state.generation, "analysis": state.analysis,
              "knowledge_snapshot": state.knowledge, "kind": kind,
              "analysis_evidence": state.analysis_evidence,
              "available_gates": [*state.contract.required_gates, "speedup"]}
    if kind == "architecture":
        common.update(previous_plan=state.plan.model_dump() if state.plan else None,
                      measured_stagnation=state.stagnation)
        instruction = "Design a decision DAG with an action, purpose, observed trigger, expected cost, measurable gate, and admitted evidence per decision. Give every decision an atomic group. Use only admitted analysis and knowledge."
    else:
        common.update(plan=state.plan.model_dump(), invalidated=state.invalidated)
        instruction = "Implement the whole plan in the code environment. Rebuild invalidated decisions and their atomic groups. Report completed IDs and optional knowledge proposal. Do not report timings or claim acceptance."
    common["instructions"] = Path(__file__).with_name("agent.md").read_text()
    common["task_instructions"] = state.contract.task_instructions
    common["measurements"] = [h for h in state.history if h["kind"] == "measurement"
                              and h["generation"] == state.generation]
    return instruction + "\n" + canonical(common)


@flow(agents=Agents, envs=Envs, params=Params, resumable=True)
async def forge_kernel(task, *, agents, envs, params, ctx):
    """Execute or resume one frozen campaign; return durable evidence and stop reasons."""
    contract = Contract.model_validate_json(Path(params.contract).read_bytes())
    with Store(Path(params.campaign)) as store:
        state = store.start(contract, task, identities(agents, envs))
        executor = Executor(store, state, envs["executor"])
        state.blocked = None
        try:
            while state.phase != "stop":
                phase = state.phase
                if phase == "preflight":
                    receipt = await executor.exchange("preflight", {"identities": state.identities,
                        "controller_path": str(store.root), "required": sorted(REQUIRED_CAPABILITIES)})
                    admission = Preflight.model_validate(receipt.payload)
                    if (admission.identities != state.identities or admission.controller_path != str(store.root)
                            or not REQUIRED_CAPABILITIES <= set(admission.capabilities)):
                        raise Blocked("required deployment capability or runtime identity is not attested")
                    state.seed = admission.seed
                    executor.consume(receipt)
                    state.phase = "analyze"
                elif phase == "analyze":
                    receipt = await executor.exchange("analyze", {"knowledge_snapshot": state.knowledge})
                    analysis = Analysis.model_validate(receipt.payload)
                    state.analysis = analysis.summary
                    state.analysis_evidence = [receipt.request_id, *analysis.evidence]
                    executor.consume(receipt)
                    state.phase = "plan"
                elif phase == "plan":
                    plan = await activate(state, store, executor, agents["worker"], envs, ctx,
                                          Plan, "analysis", worker_prompt(state, "architecture"))
                    admitted_evidence = set(state.analysis_evidence) | set(state.knowledge)
                    for event in state.history:
                        if event["kind"] == "measurement" and event["generation"] == state.generation:
                            admitted_evidence.update([event["receipt"], *event["result"]["evidence"]])
                    for decision in plan.decisions:
                        if decision.gate not in {*contract.required_gates, "speedup"} or not set(decision.evidence) <= admitted_evidence:
                            raise Blocked("plan gate or evidence is outside the admitted task")
                    state.invalidated = invalidated(state.plan, plan)
                    state.history.append({"kind": "plan", "generation": state.generation,
                                          "invalidated": state.invalidated, "plan": plan.model_dump()})
                    state.plan = plan
                    state.turn = None
                    state.stagnation = 0
                    state.phase = "implement"
                elif phase == "implement":
                    if state.attempts >= contract.attempts:
                        state.phase = "confirm" if state.best and state.best.gain > contract.noise else "stop"
                        if state.phase == "stop":
                            state.stopped = "attempt cap without a measured improvement"
                    else:
                        implementation = await activate(state, store, executor, agents["worker"], envs, ctx,
                            Implementation, "code", worker_prompt(state, "implementation"))
                        if set(implementation.completed_decisions) != {d.id for d in state.plan.decisions}:
                            raise Blocked("implementation did not complete every decision and atomic group")
                        state.implementation = implementation
                        state.turn = None
                        state.phase = "freeze"
                elif phase == "freeze":
                    receipt = await executor.exchange("freeze", {"plan": digest(state.plan),
                        "completed_decisions": state.implementation.completed_decisions,
                        "invalidated": state.invalidated, "attempt": state.attempts})
                    state.candidate = Snapshot.model_validate(receipt.payload)
                    executor.consume(receipt)
                    state.phase = "evaluate"
                elif phase == "evaluate":
                    receipt = await executor.exchange("evaluate", {"candidate": state.candidate.model_dump(),
                        "incumbent": state.incumbent.model_dump() if state.incumbent else None})
                    measured = evaluation(receipt.payload)
                    if measured.candidate != state.candidate:
                        raise Blocked("measurement candidate does not match immutable snapshot")
                    if measured.correct and state.incumbent and measured.incumbent_time is None:
                        raise Blocked("same-job incumbent timing missing")
                    state.attempts += 1
                    eligible = state.gates_pass(measured)
                    improved = eligible and (state.incumbent is None or
                        measured.incumbent_time / measured.candidate_time - 1 > contract.noise)
                    state.history.append({"kind": "measurement", "generation": state.generation,
                        "receipt": receipt.request_id, "result": measured.model_dump(),
                        "accepted": improved, "structural_only": improved and measured.gain <= contract.noise})
                    if improved:
                        state.incumbent = measured.candidate
                        state.best = measured
                        state.accepted_knowledge = state.implementation.knowledge
                        state.stagnation = 0
                    elif eligible:
                        state.stagnation += 1
                    executor.consume(receipt)
                    if improved and measured.gain >= contract.target_gain and measured.gain > contract.noise:
                        state.phase = "confirm"
                    elif improved:
                        state.phase = "implement"
                    else:
                        state.phase = "restore"
                elif phase == "restore":
                    accepted = state.incumbent or state.seed
                    source = accepted.source
                    receipt = await executor.exchange("restore", {"source": source,
                        "snapshot": accepted.model_dump()})
                    restored = Restored.model_validate(receipt.payload)
                    if restored.source != source:
                        raise Blocked("executor restored a different source")
                    executor.consume(receipt)
                    if state.attempts >= contract.attempts:
                        state.phase = "confirm" if state.best and state.best.gain > contract.noise else "stop"
                        if state.phase == "stop":
                            state.stopped = "attempt cap without a measured improvement"
                    else:
                        state.phase = "analyze" if state.stagnation >= contract.plateau else "implement"
                elif phase == "confirm":
                    receipt = await executor.exchange("confirm", {"candidate": state.incumbent.model_dump(),
                        "discovery_receipt": next(h["receipt"] for h in reversed(state.history)
                            if h["kind"] == "measurement" and h["accepted"])})
                    measured = evaluation(receipt.payload)
                    discovery_id = state.pending.payload["discovery_receipt"]
                    discovery = json.loads((store.root / f"receipt-{discovery_id}.json").read_text())
                    if (measured.candidate != state.incumbent or not measured.independent
                            or receipt.job == discovery["job"]):
                        raise Blocked("fresh confirmation identity or independence missing")
                    state.history.append({"kind": "confirmation", "generation": state.generation,
                                          "receipt": receipt.request_id, "result": measured.model_dump()})
                    executor.consume(receipt)
                    if not state.gates_pass(measured) or measured.gain <= contract.noise or measured.gain < contract.target_gain:
                        state.phase, state.stopped = "stop", "fresh confirmation did not pass gates, noise, and target"
                    else:
                        state.confirmation = receipt.request_id
                        state.phase = "probe" if state.accepted_knowledge else "seal"
                elif phase == "probe":
                    proposal = state.accepted_knowledge
                    receipt = await executor.exchange("probe", {"proposal": proposal.model_dump(),
                        "candidate": state.incumbent.model_dump(), "confirmation": state.confirmation})
                    probe = Probe.model_validate(receipt.payload)
                    state.probe_receipt = receipt.request_id
                    executor.consume(receipt)
                    if probe.certified and probe.independent and probe.source_safe and probe.scope == proposal.scope:
                        state.phase = "admit"
                    else:
                        state.history.append({"kind": "knowledge_rejected", "probe": state.probe_receipt})
                        state.phase = "seal"
                elif phase == "admit":
                    receipt = await executor.exchange("admit", {"probe_receipt": state.probe_receipt,
                        "proposal": state.accepted_knowledge.model_dump()})
                    admission = Admission.model_validate(receipt.payload)
                    if not admission.private or admission.probe_receipt != state.probe_receipt:
                        raise Blocked("knowledge admission is not private and probe-bound")
                    state.history.append({"kind": "knowledge_admitted", "generation": state.generation,
                                          "entry": admission.entry, "receipt": receipt.request_id})
                    executor.consume(receipt)
                    state.phase = "seal"
                elif phase == "seal":
                    receipt = await executor.exchange("seal", {"candidate": state.incumbent.model_dump(),
                                                               "confirmation": state.confirmation})
                    seal = Seal.model_validate(receipt.payload)
                    if seal.source != state.incumbent.source or seal.confirmation != state.confirmation:
                        raise Blocked("seal does not bind confirmed source")
                    state.history.append({"kind": "promotion", "generation": state.generation,
                        "source": seal.source, "reference": seal.reference,
                        "confirmation": seal.confirmation, "receipt": receipt.request_id})
                    executor.consume(receipt)
                    state.reference = seal.reference
                    state.phase = "next"
                elif phase == "next":
                    state.generation += 1
                    if state.generation >= contract.generations:
                        state.phase, state.stopped = "stop", "generation cap reached"
                    else:
                        state.knowledge = sorted(set(state.knowledge) | {h["entry"] for h in state.history if h["kind"] == "knowledge_admitted"})
                        state.attempts = state.stagnation = 0
                        state.plan = state.implementation = state.candidate = state.incumbent = state.best = None
                        state.confirmation = state.probe_receipt = state.accepted_knowledge = None
                        state.invalidated = []
                        state.phase = "preflight"
                store.save(state)
                ctx.state["campaign"] = {"id": state.id, "revision": state.revision}
        except (Blocked, ValidationError) as error:
            state.blocked = str(error)
            store.save(state)
        report = state.report()
        store.write("report.json", report)
        return report
