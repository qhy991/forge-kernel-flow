"""Frozen domain records and the single checked campaign store."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Contract(Record):
    operator: dict[str, Any]
    reference: str
    reporting_anchor: str
    judge: str
    protocol: str
    hardware: str
    executor_argv: list[str] = Field(min_length=1)
    issuer: str
    required_gates: list[str] = Field(min_length=1)
    noise: float = Field(ge=0, lt=1)
    target_gain: float = Field(gt=0, lt=1)
    generations: int = Field(ge=1, le=100)
    attempts: int = Field(ge=1, le=1000)
    plateau: int = Field(ge=1, le=1000)
    model_cost_limit: float = Field(gt=0)
    task_instructions: str = ""
    knowledge_snapshot: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        if not Path(self.executor_argv[0]).is_absolute():
            raise ValueError("executor executable must be absolute")
        if len(set(self.required_gates)) != len(self.required_gates):
            raise ValueError("duplicate required gates")
        return self


class Decision(Record):
    id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    cost: str = Field(min_length=1)
    gate: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    atomic_group: str = Field(min_length=1)


class Plan(Record):
    rationale: str
    decisions: list[Decision] = Field(min_length=1)

    @model_validator(mode="after")
    def dag(self):
        nodes = {d.id: d for d in self.decisions}
        if len(nodes) != len(self.decisions):
            raise ValueError("duplicate decision ID")
        visiting, visited = set(), set()

        def visit(key):
            if key not in nodes:
                raise ValueError("unknown dependency")
            if key in visiting:
                raise ValueError("decision cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in nodes[key].depends_on:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in nodes:
            visit(key)
        return self


def invalidated(old: Plan | None, new: Plan) -> list[str]:
    """Close changed decisions over both DAGs and touched atomic groups."""
    if old is None:
        return []
    before = {d.id: d for d in old.decisions}
    after = {d.id: d for d in new.decisions}
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    nodes = old.decisions + new.decisions
    while True:
        groups = {d.atomic_group for d in nodes if d.id in changed}
        expanded = changed | {d.id for d in nodes if d.atomic_group in groups or set(d.depends_on) & changed}
        if changed == expanded:
            return sorted(changed)
        changed = expanded


class KnowledgeProposal(Record):
    mechanism: str
    scope: str
    minimal_example: str


class Implementation(Record):
    summary: str
    completed_decisions: list[str]
    knowledge: KnowledgeProposal | None = None


class Snapshot(Record):
    source: str = Field(min_length=1)
    artifact: str = Field(min_length=1)


class Measurement(Record):
    candidate: Snapshot
    correct: Literal[True]
    gates: dict[str, Literal["pass", "fail", "unresolved", "deferred"]]
    candidate_time: float = Field(gt=0)
    reference_time: float = Field(gt=0)
    incumbent_time: float | None = Field(default=None, gt=0)
    same_job: Literal[True]
    evidence: list[str] = Field(min_length=1)
    independent: bool = False

    @property
    def gain(self) -> float:
        return self.reference_time / self.candidate_time - 1


class Failure(Record):
    candidate: Snapshot
    correct: Literal[False]
    gates: dict[str, Literal["pass", "fail", "unresolved", "deferred"]]
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    independent: bool = False


def evaluation(payload: dict) -> Measurement | Failure:
    if payload.get("correct") is False:
        return Failure.model_validate(payload)
    return Measurement.model_validate(payload)


class Request(Record):
    id: str
    campaign: str
    contract: str
    generation: int
    op: str
    reference: str
    reporting_anchor: str
    judge: str
    protocol: str
    hardware: str
    payload: dict[str, Any]


class Receipt(Record):
    request_id: str
    request_digest: str
    issuer: str
    status: Literal["final", "pending", "unknown"]
    job: str = Field(min_length=1)
    payload: dict[str, Any]


class Turn(Record):
    id: str
    prompt: str
    schema_name: str
    role: str
    allowance_receipt: str


Phase = Literal["preflight", "analyze", "plan", "implement", "freeze", "evaluate", "restore", "confirm", "probe", "admit", "seal", "next", "stop"]


class Campaign(Record):
    id: str
    contract: Contract
    task: str
    identities: dict[str, Any]
    phase: Phase = "preflight"
    revision: int = 0
    generation: int = 0
    reference: str
    knowledge: list[str]
    seed: Snapshot | None = None
    analysis: str = ""
    analysis_evidence: list[str] = Field(default_factory=list)
    plan: Plan | None = None
    invalidated: list[str] = Field(default_factory=list)
    implementation: Implementation | None = None
    candidate: Snapshot | None = None
    incumbent: Snapshot | None = None
    best: Measurement | None = None
    accepted_knowledge: KnowledgeProposal | None = None
    confirmation: str | None = None
    probe_receipt: str | None = None
    attempts: int = 0
    stagnation: int = 0
    pending: Request | None = None
    native_job: str | None = None
    turn: Turn | None = None
    receipts: list[str] = Field(default_factory=list)
    usage: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    blocked: str | None = None
    stopped: str | None = None

    @property
    def spent(self):
        return sum(u["cost"] for u in self.usage)

    def gates_pass(self, result: Measurement):
        return result.correct and all(result.gates.get(g) == "pass" for g in self.contract.required_gates)

    def report(self):
        return {"campaign": self.id, "phase": self.phase, "generation": self.generation,
                "blocked": self.blocked, "stopped": self.stopped,
                "incumbent": self.incumbent.model_dump() if self.incumbent else None,
                "measured_best": self.best.model_dump() if self.best else None,
                "known_model_cost": self.spent, "history": self.history,
                "receipts": self.receipts, "pending": self.pending.model_dump() if self.pending else None}


class Blocked(Exception):
    """An unresolved effect or missing admission condition; safe to inspect or resume."""


class Store:
    """One process-lifetime writer lock, checked fsync, immutable effect records."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = None

    def __enter__(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise PermissionError("campaign directory must be controller-only (mode 0700)")
        self.lock = (self.root / ".lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise
        return self

    def __exit__(self, *_):
        self.lock.close()

    def write(self, name: str, value: Any, *, immutable=False):
        path = self.root / name
        encoded = canonical(value).encode()
        if immutable and path.exists():
            if path.read_bytes() != encoded:
                raise ValueError(f"immutable artifact conflict: {name}")
            return
        fd, temp = tempfile.mkstemp(prefix=".commit-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def save(self, state: Campaign):
        state.revision += 1
        self.write("campaign.json", state)

    def start(self, contract: Contract, task: str, identities: dict) -> Campaign:
        path = self.root / "campaign.json"
        if path.exists():
            state = Campaign.model_validate_json(path.read_bytes())
            if state.contract != contract or state.task != task or state.identities != identities:
                raise ValueError("frozen campaign inputs changed")
            return state
        state = Campaign(id=uuid4().hex, contract=contract, task=task, identities=identities,
                         reference=contract.reference, knowledge=contract.knowledge_snapshot)
        self.save(state)
        return state
