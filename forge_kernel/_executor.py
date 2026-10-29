"""The fixed deployment command is the sole external effect boundary."""
from __future__ import annotations

from uuid import uuid4

from pydantic import Field

from ._campaign import Blocked, Campaign, Receipt, Record, Request, Snapshot, Store, canonical, digest


REQUIRED_CAPABILITIES = {
    "source_isolation", "controller_protection", "request_id_reconciliation",
    "immutable_snapshot", "restore", "same_job_comparison", "fresh_confirmation",
    "source_bound_seal", "independent_probe", "private_catalog", "budget_authority",
}


class Preflight(Record):
    seed: Snapshot
    capabilities: list[str]
    identities: dict
    controller_path: str
    evidence: list[str] = Field(min_length=1)


class Allowance(Record):
    history_digest: str
    remaining_cost: float = Field(ge=0)
    evidence: list[str] = Field(min_length=1)


class Analysis(Record):
    summary: str
    evidence: list[str] = Field(min_length=1)


class Restored(Record):
    source: str
    evidence: list[str] = Field(min_length=1)


class Probe(Record):
    certified: bool
    independent: bool
    source_safe: bool
    scope: str
    evidence: list[str] = Field(min_length=1)


class Admission(Record):
    entry: str
    probe_receipt: str
    private: bool
    evidence: list[str] = Field(min_length=1)


class Seal(Record):
    reference: str
    source: str
    confirmation: str
    evidence: list[str] = Field(min_length=1)



class Executor:
    def __init__(self, store: Store, state: Campaign, env):
        self.store, self.state, self.env = store, state, env

    async def exchange(self, op: str, payload: dict) -> Receipt:
        state, contract = self.state, self.state.contract
        if state.pending is None:
            state.pending = Request(id=uuid4().hex, campaign=state.id, contract=digest(contract),
                                    generation=state.generation, op=op, reference=state.reference,
                                    reporting_anchor=contract.reporting_anchor, judge=contract.judge,
                                    protocol=contract.protocol, hardware=contract.hardware, payload=payload)
            self.store.save(state)
        request = state.pending
        self.store.write(f"request-{request.id}.json", request, immutable=True)
        if request.op != op or request.payload != payload:
            raise Blocked("pending request conflicts with current phase")
        saved = self.store.root / f"receipt-{request.id}.json"
        if saved.exists():
            receipt = Receipt.model_validate_json(saved.read_bytes())
        else:
            # Exact argv and entire request are frozen before the external effect.
            try:
                status, stdout, stderr = await self.env.exec([*contract.executor_argv, "exchange", canonical(request)])
            except Exception as error:
                self.store.write(f"exchange-{request.id}-{uuid4().hex}.json",
                    {"request": request.id, "transport_error": type(error).__name__, "detail": str(error)}, immutable=True)
                raise Blocked(f"executor transport failure: {type(error).__name__}") from error
            self.store.write(f"exchange-{request.id}-{uuid4().hex}.json",
                {"request": request.id, "status": status, "stdout": stdout, "stderr": stderr}, immutable=True)
            if status != 0:
                raise Blocked(f"executor exited {status}; request retained")
            try:
                receipt = Receipt.model_validate_json(stdout)
            except ValueError as error:
                raise Blocked("malformed executor reply; request retained") from error
        if (receipt.request_id != request.id or receipt.request_digest != digest(request)
                or receipt.issuer != contract.issuer):
            raise Blocked("receipt authentication or request binding mismatch")
        if state.native_job and receipt.job != state.native_job:
            raise Blocked("native job changed for the same request")
        if receipt.status != "final":
            state.native_job = receipt.job
            self.store.save(state)
            raise Blocked(f"executor {receipt.status}; reconcile the same request")
        self.store.write(saved.name, receipt, immutable=True)
        return receipt

    def consume(self, receipt: Receipt):
        state = self.state
        if receipt.request_id not in state.receipts:
            state.receipts.append(receipt.request_id)
            state.history.append({"kind": "receipt", "op": state.pending.op,
                                  "id": receipt.request_id, "generation": state.generation})
        state.pending = None
        state.native_job = None
