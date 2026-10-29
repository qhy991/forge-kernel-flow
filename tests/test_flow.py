"""Observable software contracts through public load + the pinned real runtime."""
import asyncio
import json
from pathlib import Path

import pytest
from hmz.runtime.flowing.fakes import FakeAgentDriver
from pydantic import ValidationError

from forge_kernel._campaign import Plan, Store, canonical, invalidated
from forge_kernel.demo import fake_worker, fixture_exchange, run_demo, setup


def run(root, **kwargs):
    return asyncio.run(run_demo(root, subprocess_executor=False, **kwargs))


def read(root):
    return json.loads((root / "campaign" / "campaign.json").read_text())


def effects(root, op):
    state = json.loads((root / "executor" / "fixture.json").read_text())
    return [e for e in state["effects"] if e["op"] == op]


def test_two_generations_real_flow_and_argv(tmp_path):
    setup(tmp_path)
    report, worker, _ = asyncio.run(run_demo(tmp_path, subprocess_executor=True))
    assert report["blocked"] is None
    assert report["stopped"] == "generation cap reached"
    promotions = [h for h in report["history"] if h["kind"] == "promotion"]
    assert len(promotions) == 2
    assert report["known_model_cost"] == pytest.approx(0.04)
    assert len(effects(tmp_path, "confirm")) == len(effects(tmp_path, "seal")) == 2
    assert len(effects(tmp_path, "probe")) == len(effects(tmp_path, "admit")) == 2
    for promotion in promotions:
        receipt = json.loads((tmp_path / "campaign" / f"receipt-{promotion['receipt']}.json").read_text())
        assert receipt["payload"]["source"] == promotion["source"]
        assert receipt["payload"]["confirmation"] == promotion["confirmation"]
    state = read(tmp_path)
    assert len(state["knowledge"]) == 1  # Only the previous generation enters its pinned snapshot.
    assert len(state["usage"]) == 4
    assert len(set(u["action"] for u in state["usage"])) == 4
    assert len(worker.prompts) == 4
    assert all("ForgeKernel worker instructions" in prompt for prompt in worker.prompts)
    requests = [json.loads(p.read_text()) for p in (tmp_path / "executor").glob("request-*.json")]
    assert sorted(r["payload"]["known_cost"] for r in requests if r["op"] == "allowance") == pytest.approx([0, .01, .02, .03])


@pytest.mark.parametrize("unpinned", [False, True], ids=["pinned", "unpinned"])
def test_second_generation_plan_requires_pinned_knowledge(tmp_path, unpinned):
    setup(tmp_path, generations=2)
    worker = fake_worker(tmp_path)
    original_reply = worker.script.call
    cited_entry = None

    def reply(prompt, *, output_schema, session):
        nonlocal cited_entry
        result = original_reply(prompt, output_schema=output_schema, session=session)
        data = json.loads(prompt.split("\n", 1)[1])
        if output_schema.__name__ == "Plan" and data["generation"] == 1:
            assert len(data["knowledge_snapshot"]) == 1
            pinned_entry = data["knowledge_snapshot"][0]
            cited_entry = pinned_entry + "-unpinned" if unpinned else pinned_entry
            result["decisions"][0]["evidence"] = [cited_entry]
        return result

    worker.script.call = reply
    report, _, _ = run(tmp_path, worker=worker)
    admitted = next(h["entry"] for h in report["history"] if h["kind"] == "knowledge_admitted")
    assert read(tmp_path)["knowledge"] == [admitted]
    promotions = [h["generation"] for h in report["history"] if h["kind"] == "promotion"]
    if unpinned:
        assert cited_entry not in read(tmp_path)["knowledge"]
        assert "evidence is outside the admitted task" in report["blocked"]
        assert promotions == [0]
        assert len(effects(tmp_path, "evaluate")) == len(effects(tmp_path, "seal")) == 1
        assert len(worker.prompts) == 3
    else:
        assert cited_entry == admitted
        assert report["blocked"] is None
        assert report["stopped"] == "generation cap reached"
        assert promotions == [0, 1]
        plan = next(h["plan"] for h in report["history"] if h["kind"] == "plan" and h["generation"] == 1)
        assert plan["decisions"][0]["evidence"] == [admitted]
        assert len(effects(tmp_path, "evaluate")) == len(effects(tmp_path, "seal")) == 2
        assert len(worker.prompts) == 4


def test_correctness_rejection_restores_permitted_seed(tmp_path):
    setup(tmp_path, generations=1, attempts=1, config={"evaluations": [{"correct": False}]})
    report, _, _ = run(tmp_path)
    assert report["stopped"] == "attempt cap without a measured improvement"
    assert report["incumbent"] is None
    assert len(effects(tmp_path, "restore")) == 1
    assert (tmp_path / "code" / "kernel.py").read_text() == "# permitted empty coding seed\n"
    assert read(tmp_path)["stagnation"] == 0
    assert not effects(tmp_path, "seal")


def test_structural_incumbent_and_measured_plateau_reopen(tmp_path):
    setup(tmp_path, generations=1, attempts=4, plateau=2, config={"evaluations": [
        {"candidate_time": 120}, {"candidate_time": 120}, {"candidate_time": 120}, {"candidate_time": 80}]})
    report, worker, _ = run(tmp_path)
    assert report["blocked"] is None
    measured = [h for h in report["history"] if h["kind"] == "measurement"]
    assert measured[0]["structural_only"] is True
    assert [h["accepted"] for h in measured] == [True, False, False, True]
    assert len(effects(tmp_path, "analyze")) == 2
    plans = [h for h in report["history"] if h["kind"] == "plan"]
    assert plans[1]["invalidated"] == ["sum"]
    assert len(effects(tmp_path, "restore")) == 2


def test_dag_atomic_closure_and_cycle_rejection():
    detail = {"purpose": "test", "trigger": "test", "cost": "test", "gate": "speedup", "evidence": ["test"]}
    old = Plan(rationale="old", decisions=[
        {**detail, "id": "a", "action": "old", "depends_on": [], "atomic_group": "ab"},
        {**detail, "id": "b", "action": "keep", "depends_on": [], "atomic_group": "ab"},
        {**detail, "id": "c", "action": "keep", "depends_on": ["b"], "atomic_group": "c"},
        {**detail, "id": "d", "action": "keep", "depends_on": [], "atomic_group": "d"}])
    new = old.model_copy(deep=True)
    new.decisions[0].action = "new"
    assert invalidated(old, new) == ["a", "b", "c"]
    bad = old.model_dump()
    bad["decisions"][0]["depends_on"] = ["c"]
    bad["decisions"][1]["depends_on"] = ["a"]
    with pytest.raises(ValidationError, match="cycle"):
        Plan.model_validate(bad)


@pytest.mark.parametrize("failure", ["request", "candidate", "job"])
def test_receipt_binding_mismatch_retains_request(tmp_path, failure):
    setup(tmp_path, generations=1)
    def exchange(command, env):
        request = json.loads(command[-1])
        reply = fixture_exchange(tmp_path / "executor", request)
        if request["op"] == "evaluate":
            if failure == "request":
                reply["request_digest"] = "another-request"
            elif failure == "candidate":
                reply["payload"]["candidate"]["source"] = "another-source"
            else:
                reply["status"] = "unknown"
        return 0, canonical(reply), ""
    report, _, _ = run(tmp_path, exchange=exchange)
    assert report["blocked"]
    assert report["pending"]["op"] == "evaluate"
    assert not effects(tmp_path, "seal")
    if failure == "job":
        def changed_job(command, env):
            reply = fixture_exchange(tmp_path / "executor", json.loads(command[-1]))
            reply["job"] = "different-job"
            return 0, canonical(reply), ""
        again, _, _ = run(tmp_path, exchange=changed_job)
        assert "native job changed" in again["blocked"]


def test_missing_capability_blocks_before_model(tmp_path):
    setup(tmp_path, config={"missing_capabilities": ["source_isolation"]})
    report, worker, _ = run(tmp_path)
    assert "capability" in report["blocked"]
    assert not worker.prompts
    assert not effects(tmp_path, "analyze")


def test_runtime_identity_mismatch_blocks_preflight(tmp_path):
    setup(tmp_path)
    def exchange(command, env):
        reply = fixture_exchange(tmp_path / "executor", json.loads(command[-1]))
        reply["payload"]["identities"]["worker"]["model"] = "different-model"
        return 0, canonical(reply), ""
    report, worker, _ = run(tmp_path, exchange=exchange)
    assert "identity" in report["blocked"]
    assert not worker.prompts


@pytest.mark.parametrize("op", ["evaluate", "confirm", "probe", "admit", "seal"])
def test_unknown_effect_reconciles_exact_request_without_duplicates(tmp_path, op):
    setup(tmp_path, generations=1)
    seen = []
    def exchange(command, env):
        request = json.loads(command[-1])
        seen.append(request)
        reply = fixture_exchange(tmp_path / "executor", request)
        if request["op"] == op:
            return 0, canonical({**reply, "status": "unknown", "payload": {}}), ""
        return 0, canonical(reply), ""
    first, _, _ = run(tmp_path, exchange=exchange)
    pending = first["pending"]
    assert pending["op"] == op
    second, _, executor = run(tmp_path)
    assert second["blocked"] is None
    assert json.loads(executor.commands[0][-1]) == pending
    assert len(effects(tmp_path, op)) == 1
    assert second["receipts"].count(pending["id"]) == 1


def test_unknown_without_native_job_id_reuses_request(tmp_path):
    setup(tmp_path, generations=1)
    def exchange(command, env):
        request = json.loads(command[-1])
        reply = fixture_exchange(tmp_path / "executor", request)
        if request["op"] == "evaluate":
            return 1, "", "connection closed after remote effect"
        return 0, canonical(reply), ""
    first, _, _ = run(tmp_path, exchange=exchange)
    assert read(tmp_path)["native_job"] is None
    second, _, executor = run(tmp_path)
    assert json.loads(executor.commands[0][-1]) == first["pending"]
    assert len(effects(tmp_path, "evaluate")) == 1
    assert second["blocked"] is None


def test_crash_after_receipt_before_campaign_commit(tmp_path, monkeypatch):
    setup(tmp_path, generations=1)
    save = Store.save
    failed = False
    def crash(self, state):
        nonlocal failed
        if state.phase == "confirm" and not failed:
            failed = True
            raise OSError("injected durability failure")
        return save(self, state)
    monkeypatch.setattr(Store, "save", crash)
    with pytest.raises(OSError):
        run(tmp_path)
    assert read(tmp_path)["pending"]["op"] == "evaluate"
    report, _, executor = run(tmp_path)
    assert report["blocked"] is None
    assert len(effects(tmp_path, "evaluate")) == 1
    assert json.loads(executor.commands[0][-1])["op"] == "confirm"


def test_fixture_crash_before_receipt_cache_is_idempotent(tmp_path, monkeypatch):
    setup(tmp_path, generations=1)
    write = Store.write
    failed = False
    def crash(self, name, value, **kwargs):
        nonlocal failed
        if self.root == (tmp_path / "executor").resolve() and name.startswith("effect-") and value["payload"].get("same_job") and not failed:
            failed = True
            raise OSError("lost cache write")
        return write(self, name, value, **kwargs)
    monkeypatch.setattr(Store, "write", crash)
    first, _, _ = run(tmp_path)
    assert first["blocked"]
    second, _, _ = run(tmp_path)
    assert second["blocked"] is None
    assert len(effects(tmp_path, "evaluate")) == 1


@pytest.mark.parametrize("confirmation", [{"candidate_time": 99}, {"gates": {"correctness": "pass", "state": "unresolved"}}, {"gates": {"correctness": "pass", "state": "deferred"}}])
def test_confirmation_requires_noise_target_and_all_gates(tmp_path, confirmation):
    setup(tmp_path, generations=1, config={"confirmation": confirmation})
    report, _, _ = run(tmp_path)
    assert report["blocked"] is None
    assert "confirmation did not pass" in report["stopped"]
    assert not effects(tmp_path, "seal")
    assert not effects(tmp_path, "admit")


def test_knowledge_requires_independent_source_safe_probe(tmp_path):
    setup(tmp_path, generations=1, config={"probe": {"source_safe": False}})
    report, _, _ = run(tmp_path)
    assert report["blocked"] is None
    assert len(effects(tmp_path, "probe")) == 1
    assert not effects(tmp_path, "admit")
    assert len(effects(tmp_path, "seal")) == 1
    assert read(tmp_path)["knowledge"] == []


def test_trusted_allowance_required_before_paid_turn(tmp_path):
    setup(tmp_path, config={"remaining_cost": 0})
    report, worker, _ = run(tmp_path)
    assert "budget exhausted" in report["blocked"]
    assert not worker.prompts
    assert read(tmp_path)["turn"] is None


def test_allowance_must_bind_durable_history(tmp_path):
    setup(tmp_path)
    def exchange(command, env):
        request = json.loads(command[-1])
        reply = fixture_exchange(tmp_path / "executor", request)
        if request["op"] == "allowance":
            reply["payload"]["history_digest"] = "new-runtime-zero"
        return 0, canonical(reply), ""
    report, worker, _ = run(tmp_path, exchange=exchange)
    assert "durable usage" in report["blocked"]
    assert not worker.prompts


def test_uncertain_model_turn_is_never_replayed(tmp_path):
    setup(tmp_path)
    def reply(*args, **kwargs):
        raise RuntimeError("lost paid reply")
    worker = FakeAgentDriver(reply=reply, cost=.1, name="fixture-worker")
    report, _, _ = run(tmp_path, worker=worker)
    assert "uncertain model turn" in report["blocked"]
    again, replacement, executor = run(tmp_path)
    assert "no automatic replay" in again["blocked"]
    assert not replacement.prompts
    assert not executor.commands


def test_model_result_durability_failure_blocks_replay(tmp_path, monkeypatch):
    setup(tmp_path)
    write = Store.write
    def fail(self, name, value, **kwargs):
        if name.startswith("turn-"):
            raise OSError("result storage failed")
        return write(self, name, value, **kwargs)
    monkeypatch.setattr(Store, "write", fail)
    with pytest.raises(OSError):
        run(tmp_path)
    report, worker, _ = run(tmp_path)
    assert "no automatic replay" in report["blocked"]
    assert not worker.prompts


def test_protected_store_and_process_writer_lock(tmp_path):
    root = tmp_path / "campaign"
    with Store(root):
        with pytest.raises(BlockingIOError):
            with Store(root):
                pass
    root.chmod(0o755)
    with pytest.raises(PermissionError):
        with Store(root):
            pass


def test_changed_frozen_contract_is_rejected(tmp_path):
    setup(tmp_path, generations=1)
    run(tmp_path)
    contract = json.loads((tmp_path / "contract.json").read_text())
    contract["noise"] = .01
    (tmp_path / "contract.json").write_text(canonical(contract))
    with pytest.raises(ValueError, match="frozen campaign inputs"):
        run(tmp_path)


def test_failed_candidate_has_no_fabricated_timings(tmp_path):
    setup(tmp_path, generations=1, attempts=1)
    def exchange(command, env):
        request = json.loads(command[-1])
        if request["op"] == "freeze":
            (tmp_path / "code" / "kernel.py").write_text("not python syntax !!!")
        return 0, canonical(fixture_exchange(tmp_path / "executor", request)), ""
    report, _, _ = run(tmp_path, exchange=exchange)
    failure = next(h["result"] for h in report["history"] if h["kind"] == "measurement")
    assert failure["correct"] is False
    assert "candidate_time" not in failure and "reference_time" not in failure
    assert "SyntaxError" in failure["reason"]
    restore = next(json.loads(p.read_text()) for p in (tmp_path / "campaign").glob("request-*.json") if json.loads(p.read_text())["op"] == "restore")
    assert restore["payload"]["source"] == read(tmp_path)["seed"]["source"]
    assert restore["payload"]["source"] != restore["reference"]


def test_speedup_formula_at_threshold(tmp_path):
    # 100/95 - 1 exceeds 5%; 1 - 95/100 would not exceed this 5.1% target.
    setup(tmp_path, generations=1, config={"evaluations": [{"candidate_time": 95}], "confirmation": {"candidate_time": 95}})
    contract = json.loads((tmp_path / "contract.json").read_text())
    contract["target_gain"] = .051
    (tmp_path / "contract.json").write_text(canonical(contract))
    report, _, _ = run(tmp_path)
    assert report["blocked"] is None
    assert len(effects(tmp_path, "seal")) == 1
    assert len(effects(tmp_path, "evaluate")) == 1


def test_generation_requires_new_isolation_admission(tmp_path):
    setup(tmp_path)
    def exchange(command, env):
        request = json.loads(command[-1])
        reply = fixture_exchange(tmp_path / "executor", request)
        if request["op"] == "preflight" and request["generation"] == 1:
            reply["payload"]["capabilities"].remove("source_isolation")
        return 0, canonical(reply), ""
    report, worker, _ = run(tmp_path, exchange=exchange)
    assert report["generation"] == 1
    assert "capability" in report["blocked"]
    assert len(worker.prompts) == 2
    assert len(effects(tmp_path, "analyze")) == 1
    assert (tmp_path / "code" / "kernel.py").read_text() == "# permitted empty coding seed\n"


def test_confirmation_cannot_reuse_discovery_native_job(tmp_path):
    setup(tmp_path, generations=1)
    discovery_job = None
    def exchange(command, env):
        nonlocal discovery_job
        request = json.loads(command[-1])
        reply = fixture_exchange(tmp_path / "executor", request)
        if request["op"] == "evaluate":
            discovery_job = reply["job"]
        if request["op"] == "confirm":
            reply["job"] = discovery_job
        return 0, canonical(reply), ""
    report, _, _ = run(tmp_path, exchange=exchange)
    assert "independence" in report["blocked"]
    assert not effects(tmp_path, "seal")


@pytest.mark.parametrize("kind", ["nonzero", "malformed", "transport"])
def test_executor_failure_retains_raw_diagnostics(tmp_path, kind):
    setup(tmp_path)
    def exchange(command, env):
        if kind == "transport":
            raise ConnectionError("lost remote connection")
        if kind == "malformed":
            return 0, "not-json", "parse diagnostic"
        return 19, "partial output", "native diagnostic"
    report, worker, _ = run(tmp_path, exchange=exchange)
    assert report["blocked"] and not worker.prompts
    artifacts = list((tmp_path / "campaign").glob("exchange-*.json"))
    assert len(artifacts) == 1
    raw = json.loads(artifacts[0].read_text())
    assert raw["request"] == report["pending"]["id"]
    if kind == "transport":
        assert raw["detail"] == "lost remote connection"
    elif kind == "malformed":
        assert raw["stdout"] == "not-json"
    else:
        assert raw["status"] == 19 and raw["stderr"] == "native diagnostic"


def test_failed_model_call_retains_observable_usage(tmp_path):
    setup(tmp_path, config={"remaining_cost": .005})
    report, _, _ = run(tmp_path)
    assert "uncertain model turn" in report["blocked"]
    assert report["known_model_cost"] == pytest.approx(.005)
    assert read(tmp_path)["usage"][0]["uncertain"] is True
    again, worker, _ = run(tmp_path)
    assert not worker.prompts
    assert again["known_model_cost"] == pytest.approx(.005)


def test_rejection_restores_exact_current_generation_incumbent(tmp_path):
    setup(tmp_path, generations=1, attempts=2, config={"evaluations": [
        {"candidate_time": 120}, {"correct": False}]})
    report, _, _ = run(tmp_path)
    measurements = [h for h in report["history"] if h["kind"] == "measurement"]
    assert measurements[0]["result"]["candidate"] != measurements[1]["result"]["candidate"]
    assert report["incumbent"] == measurements[0]["result"]["candidate"]
    assert "implementation 1" in (tmp_path / "code" / "kernel.py").read_text()
    assert read(tmp_path)["stagnation"] == 0


def test_resume_requires_fresh_allowance_for_next_paid_turn(tmp_path):
    setup(tmp_path, generations=1, attempts=2, config={"evaluations": [{"candidate_time": 120}]})
    def exchange(command, env):
        request = json.loads(command[-1])
        reply = fixture_exchange(tmp_path / "executor", request)
        if request["op"] == "evaluate":
            reply = {**reply, "status": "unknown", "payload": {}}
        return 0, canonical(reply), ""
    first, _, _ = run(tmp_path, exchange=exchange)
    assert first["known_model_cost"] == pytest.approx(.02)
    config = json.loads((tmp_path / "executor" / "config.json").read_text())
    config["remaining_cost"] = 0
    (tmp_path / "executor" / "config.json").write_text(canonical(config))
    resumed, worker, _ = run(tmp_path)
    assert "budget exhausted" in resumed["blocked"]
    assert resumed["known_model_cost"] == pytest.approx(.02)
    assert not worker.prompts
    allowance = read(tmp_path)["pending"]
    assert allowance["op"] == "allowance" and allowance["payload"]["known_cost"] == pytest.approx(.02)
