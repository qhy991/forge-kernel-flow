"""Offline software demonstration. NO GPU and NO performance acceptance.

The fake worker writes tiny Python reductions. The independent CPU fixture checks
outputs; timing values are deterministic test data, not measured performance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from hmz.flows import Budget, load
from hmz.runtime.flowing.fakes import FakeAgentDriver, FakeEnvDriver, run_fake

from ._campaign import Request, Store, canonical, digest
from ._executor import REQUIRED_CAPABILITIES


LABEL = "OFFLINE SOFTWARE FIXTURE: NO GPU; NO PERFORMANCE ACCEPTANCE"


def setup(root: Path, *, generations=2, attempts=3, plateau=2, config=None):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("analysis", "code", "executor"):
        (root / name).mkdir(exist_ok=True)
    fixture = root / "executor"
    fixture.chmod(0o700)
    (fixture / "config.json").write_text(canonical(config or {}))
    contract = {"operator": {"name": "sum", "shape": [4], "precision": "float64", "domain": "finite CPU fixture vectors"},
                "reference": "fixture-reference-v0", "reporting_anchor": "fixture-reference-v0",
                "judge": "fixture-cpu-correctness-v1", "protocol": "synthetic-ticks-v1",
                "hardware": "NO-GPU-fixture", "executor_argv": [sys.executable, "-m", "forge_kernel.demo", "--executor-root", str(fixture)],
                "issuer": "offline-fixture", "required_gates": ["correctness", "state"],
                "noise": 0.02, "target_gain": 0.05, "generations": generations,
                "attempts": attempts, "plateau": plateau, "model_cost_limit": 2.0,
                "knowledge_snapshot": []}
    path = root / "contract.json"
    path.write_text(json.dumps(contract, indent=2) + "\n")
    return path


def fixture_exchange(root: Path, request: dict):
    """Idempotent fixture executor. Production deployments must replace this command."""
    request = Request.model_validate(request)
    with Store(root) as store:
        request_file = f"request-{request.id}.json"
        store.write(request_file, request, immutable=True)
        receipt_cache = root / f"effect-{request.id}.json"
        if receipt_cache.exists():
            return json.loads(receipt_cache.read_text())
        config = json.loads((root / "config.json").read_text())
        state_path = root / "fixture.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"evaluations": 0, "sources": {}, "reference_ticks": 100.0, "effects": [], "receipts": {}}
        if request.id in state["receipts"]:
            receipt = state["receipts"][request.id]
            store.write(receipt_cache.name, receipt, immutable=True)
            return receipt
        op, payload = request.op, request.payload
        evidence = [LABEL, f"fixture://{request.id}"]
        if op == "preflight":
            seed_code = "# permitted empty coding seed\n"
            source = hashlib.sha256(seed_code.encode()).hexdigest()
            artifact = f"snapshot-{source}.json"
            store.write(artifact, {"code": seed_code}, immutable=True)
            state["sources"].setdefault(source, {"artifact": artifact, "ticks": None})
            (root.parent / "code" / "kernel.py").write_text(seed_code)
            out = {"seed": {"source": source, "artifact": str(root / artifact)},"capabilities": sorted(REQUIRED_CAPABILITIES - set(config.get("missing_capabilities", []))),
                   "identities": payload["identities"], "controller_path": payload["controller_path"], "evidence": evidence}
        elif op == "allowance":
            out = {"history_digest": payload["history_digest"],
                   "remaining_cost": config.get("remaining_cost", payload["cost_limit"] - payload["known_cost"]), "evidence": evidence}
        elif op == "analyze":
            out = {"summary": "Compute a finite-vector sum. Reference source is opaque. Fixture uses synthetic timing ticks.", "evidence": evidence}
        elif op == "freeze":
            code = (root.parent / "code" / "kernel.py").read_text()
            source = hashlib.sha256(code.encode()).hexdigest()
            artifact = f"snapshot-{source}.json"
            store.write(artifact, {"code": code}, immutable=True)
            state["sources"].setdefault(source, {"artifact": artifact, "ticks": None})
            out = {"source": source, "artifact": str(root / artifact)}
        elif op in {"evaluate", "confirm"}:
            snapshot = payload["candidate"]
            code = json.loads((root / f"snapshot-{snapshot['source']}.json").read_text())["code"]
            namespace = {}
            try:
                exec(compile(code, "fixture-candidate", "exec"), namespace)
                vectors = [[1., 2., 3., 4.], [-1., 0., 2., 3.], [0., 0., 0., 0.]]
                correct = all(math.isclose(namespace["kernel"](v), math.fsum(v), rel_tol=1e-12, abs_tol=1e-12) for v in vectors)
                failure_reason = "CPU correctness check failed"
            except Exception as error:
                correct, failure_reason = False, f"CPU candidate failed: {type(error).__name__}"
            overrides = config.get("evaluations", [])
            if op == "evaluate":
                index = state["evaluations"]
                override = overrides[index] if index < len(overrides) else {}
                state["evaluations"] += 1
            else:
                override = config.get("confirmation", {})
            ticks = override.get("candidate_time", state["reference_ticks"] * 0.8)
            incumbent = payload.get("incumbent")
            incumbent_time = state["sources"][incumbent["source"]]["ticks"] if incumbent else None
            state["sources"][snapshot["source"]]["ticks"] = ticks
            out = {"candidate": snapshot, "correct": correct and override.get("correct", True),
                   "gates": override.get("gates", {"correctness": "pass", "state": "pass"}),
                   "candidate_time": ticks, "reference_time": state["reference_ticks"],
                   "incumbent_time": incumbent_time, "same_job": True,
                   "independent": op == "confirm", "evidence": evidence}
            if not out["correct"]:
                out = {"candidate": snapshot, "correct": False,
                       "gates": {"correctness": "fail", "state": "unresolved"},
                       "reason": failure_reason, "independent": op == "confirm", "evidence": evidence}
        elif op == "restore":
            source = payload["source"]
            code = json.loads((root / state["sources"][source]["artifact"]).read_text())["code"]
            (root.parent / "code" / "kernel.py").write_text(code)
            out = {"source": source, "evidence": evidence}
        elif op == "probe":
            proposal = payload["proposal"]
            scope = proposal["scope"]
            namespace = {}
            exec(compile(proposal["minimal_example"], "independent-probe", "exec"), namespace)
            certified = namespace["kernel"]([1., 2., 3., 4.]) == 10.
            out = {"certified": certified, "independent": True, "source_safe": True,
                   "scope": scope, "evidence": evidence}
            out.update(config.get("probe", {}))
        elif op == "admit":
            entry = "private-fixture://" + digest(payload)
            store.write(f"catalog-{digest(payload)}.json", payload, immutable=True)
            out = {"entry": entry, "probe_receipt": payload["probe_receipt"], "private": True, "evidence": evidence}
        elif op == "seal":
            source = payload["candidate"]["source"]
            state["reference_ticks"] = state["sources"][source]["ticks"]
            out = {"reference": "sealed-fixture:" + source, "source": source,
                   "confirmation": payload["confirmation"], "evidence": evidence}
        else:
            raise ValueError(f"unsupported fixture operation: {op}")
        receipt = {"request_id": request.id, "request_digest": digest(request), "issuer": "offline-fixture",
                   "status": "final", "job": "fixture-job:" + request.id, "payload": out}
        state["effects"].append({"request": request.id, "op": op})
        state["receipts"][request.id] = receipt
        store.write("fixture.json", state)
        store.write(receipt_cache.name, receipt, immutable=True)
        return receipt


def fake_worker(root: Path):
    implementations = 0
    def reply(prompt, *, output_schema, session):
        nonlocal implementations
        data = json.loads(prompt.split("\n", 1)[1])
        if output_schema.__name__ == "Plan":
            return {"rationale": "Use one CPU reduction for the frozen vector contract.",
                    "decisions": [{"id": "sum", "action": f"sum revision {data['measured_stagnation']}",
                                   "purpose": "Preserve finite-vector reduction semantics",
                                   "trigger": "Admitted reference analysis or measured stagnation",
                                   "cost": "One CPU reduction; no extra memory allocation",
                                   "gate": "speedup", "evidence": [data["analysis_evidence"][0]],
                                   "depends_on": [], "atomic_group": "reduction"}]}
        implementations += 1
        code = f"# generation {data['generation']}; implementation {implementations}\ndef kernel(values):\n    return sum(values)\n"
        (root / "code" / "kernel.py").write_text(code)
        return {"summary": "Implemented CPU fixture reduction.", "completed_decisions": ["sum"],
                "knowledge": {"mechanism": "finite vector reduction", "scope": "CPU fixture only",
                              "minimal_example": "def kernel(values):\n    return sum(values)\n"}}
    return FakeAgentDriver(reply=reply, cost=0.01, output_tokens=10, name="fixture-worker")


async def run_demo(root: Path, *, subprocess_executor=True, worker=None, exchange=None):
    def execute(command, env):
        if exchange:
            return exchange(command, env)
        if subprocess_executor:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            return completed.returncode, completed.stdout, completed.stderr
        return 0, canonical(fixture_exchange(root / "executor", json.loads(command[-1]))), ""
    executor = FakeEnvDriver(workdir=str(root / "executor"), run=execute)
    worker = worker or fake_worker(root)
    result = await run_fake(load(str(Path(__file__).parent)), "Optimize the frozen CPU fixture sum.",
        agents={"worker": worker}, envs={"analysis": FakeEnvDriver(workdir=str(root / "analysis")),
        "code": FakeEnvDriver(workdir=str(root / "code")), "executor": executor},
        params={"contract": str(root / "contract.json"), "campaign": str(root / "campaign")},
        budget=Budget(cost=2))
    return result, worker, executor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--executor-root", type=Path)
    parser.add_argument("operation", nargs="?")
    parser.add_argument("request", nargs="?")
    args = parser.parse_args()
    if args.executor_root:
        if args.operation != "exchange" or args.request is None:
            parser.error("executor requires exchange and one canonical JSON argument")
        print(canonical(fixture_exchange(args.executor_root, json.loads(args.request))))
        return
    root = (args.output or Path(tempfile.mkdtemp(prefix="forge-kernel-demo-"))).resolve()
    if (root / "contract.json").exists():
        parser.error("output already contains a contract; choose a new directory")
    setup(root)
    report, _, _ = asyncio.run(run_demo(root))
    print(LABEL)
    print(json.dumps(report, indent=2))
    print(f"Receipts: {root / 'campaign'}")
    if report["blocked"] or sum(h["kind"] == "promotion" for h in report["history"]) != 2:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
