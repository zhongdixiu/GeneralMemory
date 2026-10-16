from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from test_add_api import _body, _headers
from test_add_api import api_context as _api_context
from test_add_worker import FakeFactModel, _add, _run
from test_add_worker import worker_context as _worker_context

from memory_cmic.api_schemas import AddRequest
from memory_cmic.lifecycle import update_or_invalidate_source
from memory_cmic.models import MemoryEvidence, MemoryItem, MemoryTask, SourceRecord
from memory_cmic.providers import ExtractedFact, ExtractionAssessment, ExtractionOutput
from memory_cmic.task_queue import claim_tasks

api_context = _api_context
worker_context = _worker_context


def test_user_and_agent_namespaces_are_independent(api_context, engine):
    client, tenant = api_context
    tasks = []
    for token, user in [("test-token", "user_1"), ("test-token", "user_2"),
                        ("other-token", "user_1")]:
        body = _body()
        body["user_id"] = user
        response = client.post("/api/v1/memories:add", headers=_headers(token), json=body)
        assert response.status_code == 202
        tasks.append(response.json()["task_id"])
        replay = client.post("/api/v1/memories:add", headers=_headers(token), json=body)
        assert replay.json()["task_id"] == tasks[-1]
    assert len(set(tasks)) == 3
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenant)) == 3
        session.execute(update(MemoryTask).where(MemoryTask.tenant_id == tenant).values(
            available_at=func.current_timestamp()))
        assert len(claim_tasks(session, tenant_id=tenant, worker_id="claim", limit=3)) == 3


def test_pending_prior_batch_still_orders_same_agent(api_context, engine):
    client, tenant = api_context
    for index in range(2):
        response = client.post("/api/v1/memories:add", headers=_headers(key=str(index)),
                               json=_body(message_id=str(index)))
        assert response.status_code == 202
    with Session(engine) as session:
        session.execute(update(MemoryTask).where(MemoryTask.tenant_id == tenant).values(
            available_at=func.current_timestamp()))
        claimed = claim_tasks(session, tenant_id=tenant, worker_id="claim", limit=2)
        assert len(claimed) == 1 and claimed[0].batch_seq == 1


def test_maximum_message_user_session_lengths_fit_storage(api_context, engine):
    client, tenant = api_context
    body = _body(message_id="m" * 256)
    body.update(user_id="u" * 128, session_id="s" * 128)
    response = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    assert response.status_code == 202
    with Session(engine) as session:
        task = session.get(MemoryTask, response.json()["task_id"])
        assert len(task.target_id) <= 128
        assert task.user_id == body["user_id"] and task.session_id == body["session_id"]


def test_structured_empty_messages_and_unknown_tool_status(api_context):
    client, _ = api_context
    body = _body()
    body["messages"][0].update(role="assistant", content="", metadata={
        "tool_calls": [{"tool_call_id": "call", "tool_name": "lookup"}]})
    assert client.post("/api/v1/memories:add", headers=_headers(), json=body).status_code == 202
    body = _body(message_id="attachment")
    body["messages"][0].update(content="", metadata={
        "attachments": [{"file_id": "file", "mime_type": "text/plain"}]})
    assert client.post("/api/v1/memories:add", headers=_headers(key="attachment"),
                       json=body).status_code == 202
    body = _body(message_id="tool")
    body["messages"][0].update(role="tool", metadata={"tool_name": "lookup",
                                                        "tool_call_id": "call"})
    assert AddRequest.model_validate(body).messages[0].metadata.tool_status == "unknown"
    body = _body(message_id="empty", content=" ")
    assert client.post("/api/v1/memories:add", headers=_headers(key="empty"),
                       json=body).status_code == 422


def test_observation_times_do_not_reject_explicit_history():
    body = _body()
    body["messages"][0]["metadata"] = {"timestamp_origin": "adapter_observed"}
    history = {**body["messages"][0], "message_id": "history",
               "occurred_at": "2026-09-25T09:00:00+08:00"}
    body["history_messages"] = [history]
    assert AddRequest.model_validate(body).history_messages[0].message_id == "history"


def _post(client, *, key, messages, history=None):
    body = {"source_system": "email_agent", "user_id": "user_worker",
            "session_id": "session_worker", "messages": messages}
    if history is not None:
        body["history_messages"] = history
    response = client.post("/api/v1/memories:add", json=body, headers={
        "Authorization": "Bearer worker-token", "Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def _message(mid, content, role="user", **extra):
    return {"message_id": mid, "content": content, "role": role,
            "occurred_at": "2026-09-24T09:00:00+08:00", **extra}


class NoFactsModel(FakeFactModel):
    def extract(self, **kwargs):
        return []


def test_far_reply_has_complete_history_evidence_and_invalidation(worker_context, engine):
    client, tenant = worker_context
    _post(client, key="questions", messages=[
        _message("proposal", "以后周报采用三段式，可以吗？", "assistant"),
        *[_message(f"other-{i}", "普通前文", "assistant") for i in range(35)],
    ])
    _run(engine, NoFactsModel())
    task_id = _post(client, key="confirm", messages=[
        _message("confirm", "可以，以后都这样。", reply_to_message_id="proposal")])

    class ConfirmationModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert any(m["message_id"] == "proposal" for m in history)
            assert all(m["message_id"] != "confirm" for m in history)
            return [ExtractedFact(memory="用户要求以后周报采用三段式。",
                                  message_ids=["proposal", "confirm"], retention_kind="long_term")]

    _run(engine, ConfirmationModel())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded"
        memory_id = task.result_json[0]["id"]
        evidence = session.scalars(select(MemoryEvidence).where(
            MemoryEvidence.downstream_memory_id == memory_id)).all()
        assert len(evidence) == 2
        assert len({e.evidence_group_id for e in evidence}) == 1
        assert {e.evidence_locator["message_id"] for e in evidence} == {"proposal", "confirm"}
        from memory_cmic.retrieval import _matching_source_ids

        allowed = _matching_source_ids(session, tenant_id=tenant, user_id="user_worker",
                                       agent_id="agent_worker", source_system=None)
        assert {edge.upstream_source_id for edge in evidence}.issubset(allowed)
        proposal = session.scalars(select(SourceRecord).where(
            SourceRecord.tenant_id == tenant, SourceRecord.message_id == "proposal")).one()
        update_or_invalidate_source(session, tenant_id=tenant, source_id=proposal.id,
                                    status="invalidated", now=datetime.now(UTC))
        assert session.get(MemoryItem, memory_id).status == "invalidated"
        session.commit()


def test_history_only_persists_and_repeated_targets_remain_context(worker_context, engine):
    client, tenant = worker_context
    first = _post(client, key="supplement", messages=[_message("first", "你好。")],
                  history=[_message("proposal", "以后周报采用三段式，可以吗？", "assistant")])
    _run(engine, NoFactsModel())
    second = _post(client, key="later", messages=[_message("second", "可以，以后都这样。")])

    class CheckingModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert any(m["message_id"] == "proposal" for m in history)
            return [ExtractedFact(memory="用户要求以后周报采用三段式。",
                                  message_ids=["proposal", "second"], retention_kind="long_term")]

    _run(engine, CheckingModel())
    _post(client, key="repeat", messages=[_message("second", "可以，以后都这样。"),
                                          _message("third", "继续采用这个格式。")])

    class RepeatModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert [m["message_id"] for m in targets] == ["third"]
            assert any(m["message_id"] == "second" for m in history)
            return super().extract(targets=targets, history=history)

    _run(engine, RepeatModel())
    with Session(engine) as session:
        assert session.get(MemoryTask, first).status == "succeeded"
        assert session.get(MemoryTask, second).status == "succeeded"
        from memory_cmic.retrieval import _matching_source_ids
        from memory_cmic.worker import _add_evidence, _restore_input

        task = session.get(MemoryTask, second)
        memory_id = task.result_json[0]["id"]
        evidence = session.scalars(select(MemoryEvidence).where(
            MemoryEvidence.downstream_memory_id == memory_id)).all()
        allowed = _matching_source_ids(session, tenant_id=tenant, user_id="user_worker",
                                       agent_id="agent_worker", source_system=None)
        assert len(evidence) == 2 and {e.upstream_source_id for e in evidence}.issubset(allowed)
        task_input = _restore_input(task.payload["execution"]["input"])
        fact = ExtractedFact(memory="用户要求以后周报采用三段式。",
                             message_ids=["proposal", "second"])
        _add_evidence(session, task_input=task_input, fact=fact, memory_id=memory_id)
        _add_evidence(session, task_input=task_input, fact=fact, memory_id=memory_id)
        assert session.scalar(select(func.count()).select_from(MemoryEvidence).where(
            MemoryEvidence.downstream_memory_id == memory_id)) == 2
        assert session.scalar(select(func.count()).select_from(SourceRecord).where(
            SourceRecord.tenant_id == tenant, SourceRecord.message_id == "second")) == 1


def test_context_insufficient_is_distinct_and_does_not_mark_source_complete(worker_context, engine):
    client, tenant = worker_context
    task_id = _add(client, key="missing", message_id="missing", content="记住这个。")

    class MissingContextModel(FakeFactModel):
        def extract_with_outcomes(self, **kwargs):
            return ExtractionOutput(facts=[], assessments=[ExtractionAssessment(
                message_id="missing", outcome="context_insufficient", reason="缺少指代对象原文")])

    _run(engine, MissingContextModel())
    response = client.get(f"/api/v1/tasks/{task_id}", headers={
        "Authorization": "Bearer worker-token"}).json()
    assert response["status"] == "succeeded" and response["results"] == []
    assert response["processing_results"][0]["outcome"] == "context_insufficient"
    with Session(engine) as session:
        source = session.scalars(select(SourceRecord).where(
            SourceRecord.tenant_id == tenant, SourceRecord.message_id == "missing")).one()
        assert source.processed_version is None


def test_future_batch_does_not_enter_frozen_task_input(worker_context, engine):
    client, _ = worker_context
    _add(client, key="earlier", message_id="earlier", content="我喜欢简洁回复。")
    _add(client, key="later", message_id="later", content="我喜欢详细回复。")

    class BoundaryModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert [m["message_id"] for m in targets] == ["earlier"]
            assert not any(m["message_id"] == "later" for m in history)
            return super().extract(targets=targets, history=history)

    _run(engine, BoundaryModel())


def test_empty_tool_call_reaches_model_and_is_marked_processed(worker_context, engine):
    client, tenant = worker_context
    calls = [{"tool_call_id": "call", "tool_name": "lookup", "arguments": {"q": "x"}}]
    task_id = _post(client, key="tool-call", messages=[
        _message("call-message", "", "assistant", metadata={"tool_calls": calls})])

    class ToolCallModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert len(targets) == 1 and targets[0]["tool_calls"] == calls
            assert targets[0]["content"] == ""
            return []

    _run(engine, ToolCallModel())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded"
        source = session.scalars(select(SourceRecord).where(
            SourceRecord.tenant_id == tenant, SourceRecord.message_id == "call-message")).one()
        assert source.processed_version == source.version


def test_budget_insufficiency_does_not_stop_independent_target(worker_context, engine):
    client, tenant = worker_context
    task_id = _post(client, key="budget-context", messages=[
        _message("dependent", "可以。", reply_to_message_id="huge"),
        _message("independent", "我喜欢简洁回复。"),
    ], history=[_message("huge", "必要上下文" * 3000, "assistant")])

    class IndependentModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert [m["message_id"] for m in targets] == ["independent"]
            return super().extract(targets=targets, history=history)

    _run(engine, IndependentModel())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded" and len(task.result_json) == 1
        outcomes = task.payload["execution"]["processing_results"]
        assert {(r["message_id"], r["outcome"]) for r in outcomes} == {
            ("dependent", "context_insufficient"), ("independent", "added")}


def test_no_durable_value_marks_evaluated_source_complete(worker_context, engine):
    client, tenant = worker_context
    task_id = _add(client, key="greeting", message_id="greeting", content="你好。")

    class GreetingModel(FakeFactModel):
        def extract_with_outcomes(self, **kwargs):
            return ExtractionOutput(facts=[], assessments=[ExtractionAssessment(
                message_id="greeting", outcome="no_durable_value", reason="日常问候")])

    _run(engine, GreetingModel())
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "succeeded"
        source = session.scalars(select(SourceRecord).where(
            SourceRecord.tenant_id == tenant, SourceRecord.message_id == "greeting")).one()
        assert source.processed_version == source.version


@pytest.mark.parametrize("action", ["DISPUTE", "HISTORICAL"])
def test_unknown_event_time_cannot_change_newer_fact(worker_context, engine, action):
    from memory_cmic.providers import GovernanceDecision

    client, tenant = worker_context
    _add(client, key="current", message_id="current", content="用户住上海。")
    _run(engine, FakeFactModel())
    task_id = _post(client, key="observed", messages=[
        _message("observed", "用户住北京。", metadata={"timestamp_origin": "adapter_observed"})])

    class DisputeModel(FakeFactModel):
        def resolve(self, *, fact, candidates, targets):
            if action == "HISTORICAL":
                return GovernanceDecision(action="ADD", historical_before_id=candidates[0]["id"])
            return GovernanceDecision(action="DISPUTE", memory_ids=[candidates[0]["id"]])

    _run(engine, DisputeModel())
    with Session(engine) as session:
        memories = session.scalars(select(MemoryItem).where(MemoryItem.tenant_id == tenant)).all()
        assert len(memories) == 1 and memories[0].status == "active"
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded"
        assert task.payload["execution"]["processing_results"][0]["outcome"] == "time_unresolved"
        assert not task.payload["execution"]["changed"]


def test_repeated_support_group_does_not_turn_later_failure_into_partial(worker_context, engine):
    from test_fact_governance import _run as run_with_budget
    from test_fact_governance import _test_slice_budget

    from memory_cmic.providers import ProviderError

    client, tenant = worker_context

    class PartlyUnderstoodModel(FakeFactModel):
        def extract_with_outcomes(self, *, targets, history):
            if targets[0]["message_id"] == "m2":
                raise ProviderError("model unavailable")
            return ExtractionOutput(
                facts=[ExtractedFact(memory="用户喜欢简洁回复。", message_ids=["m1"])],
                assessments=[ExtractionAssessment(message_id="m1", outcome="context_insufficient",
                                                   reason="另一陈述缺少指代依据")],
            )

    _post(client, key="partial-understanding", messages=[_message("m1", "甲" * 180)])
    _run(engine, PartlyUnderstoodModel())
    task_id = _post(client, key="same-support", messages=[_message("m1", "甲" * 180),
                                                         _message("m2", "乙" * 180)])
    with Session(engine) as session:
        session.get(MemoryTask, task_id).max_attempts = 1
        session.commit()
    run_with_budget(engine, PartlyUnderstoodModel(), _test_slice_budget())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "failed" and not task.payload["execution"]["changed"]
        assert task.payload["execution"]["processing_results"][0]["outcome"] == "already_exists"
        assert session.scalar(select(func.count()).select_from(MemoryEvidence).where(
            MemoryEvidence.tenant_id == tenant)) == 1
