from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_add_api import _body, _headers
from test_add_api import api_context as _api_context
from test_add_context import _message, _post
from test_add_worker import FakeFactModel, _add, _run
from test_add_worker import worker_context as _worker_context

from memory_cmic.add_management import reprocess_add
from memory_cmic.api_schemas import AddRequest, Message
from memory_cmic.auth import AuthContext, AuthorizationError
from memory_cmic.ingest import request_fingerprint
from memory_cmic.models import MemoryAuditLog, MemoryEvidence, MemoryItem, MemoryTask, SourceRecord
from memory_cmic.providers import (
    ExtractedFact,
    ExtractionAssessment,
    ExtractionOutput,
    GovernanceDecision,
)

api_context = _api_context
worker_context = _worker_context


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-13 10:00:00",
        "2026-10-13T10:00:00",
        "2026-10-13T11:00:00+09:00",
        "2026-10-13T02:00:00Z",
        1791856800,
        1791856800000,
    ],
)
def test_timestamp_formats_represent_same_beijing_instant(value):
    message = Message(message_id="m", role="user", content="事实", occurred_at=value)
    assert message.occurred_at.astimezone(UTC) == datetime(2026, 10, 13, 2, tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-10-13", "这周六", None])
def test_timestamp_does_not_fabricate_precision(value):
    with pytest.raises(ValidationError):
        Message(message_id="m", role="user", content="事实", occurred_at=value)


def _source(session, tenant, mid):
    return session.scalars(
        select(SourceRecord).where(SourceRecord.tenant_id == tenant, SourceRecord.message_id == mid)
    ).one()


def test_realtime_default_and_replays_keep_first_time(api_context, engine):
    client, tenant = api_context
    body = _body()
    body["messages"][0].pop("occurred_at")
    first = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    assert first.status_code == 202
    again = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    other = client.post("/api/v1/memories:add", headers=_headers(key="another"), json=body)
    assert again.json()["task_id"] == first.json()["task_id"] and other.status_code == 202
    with Session(engine) as session:
        source = _source(session, tenant, "msg_1")
        assert source.metadata_json["timestamp_origin"] == "server_received"
        assert source.occurred_at == source.received_at
        assert source.metadata_json["original_occurred_at"] is None
    request = AddRequest.model_validate(body)
    assert request_fingerprint(request) == request_fingerprint(AddRequest.model_validate(body))


def test_unknown_history_promoted_to_target_stays_unknown(api_context, engine):
    client, tenant = api_context
    body = _body()
    unknown = {"message_id": "old", "role": "user", "content": "这周六去动物园"}
    body["history_messages"] = [unknown]
    response = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    assert response.status_code == 202
    with Session(engine) as session:
        source = _source(session, tenant, "old")
        received = source.received_at
        assert source.occurred_at is None
    body["history_messages"] = []
    body["messages"] = [unknown]
    response = client.post("/api/v1/memories:add", headers=_headers(key="promote"), json=body)
    assert response.status_code == 202
    with Session(engine) as session:
        source = _source(session, tenant, "old")
        assert source.received_at == received and source.occurred_at is None
        assert source.metadata_json["timestamp_origin"] == "adapter_observed"


def test_known_history_promoted_to_target_does_not_refresh_receipt(api_context, engine):
    client, tenant = api_context
    body = _body()
    old = {**body["messages"][0], "message_id": "old"}
    body["history_messages"] = [old]
    assert client.post("/api/v1/memories:add", headers=_headers(), json=body).status_code == 202
    with Session(engine) as session:
        received = _source(session, tenant, "old").received_at
    body["history_messages"] = []
    body["messages"] = [old]
    assert (
        client.post("/api/v1/memories:add", headers=_headers(key="promote"), json=body).status_code
        == 202
    )
    with Session(engine) as session:
        assert _source(session, tenant, "old").received_at == received


def test_aware_formats_normalize_request_fingerprint(api_context):
    client, _ = api_context
    body = _body()
    first = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    body["messages"][0]["occurred_at"] = "2026-09-24T10:00:00+09:00"
    again = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    assert again.status_code == 202 and again.json()["task_id"] == first.json()["task_id"]


class ZooModel(FakeFactModel):
    def extract_with_outcomes(self, *, targets, history):
        target = targets[0]
        mid = target["message_id"]
        fields = {"retention_kind": "temporary"}
        ids = [mid]
        if mid == "plan":
            memory = "用户计划2026年10月17日或18日带儿子去动物园玩一天。"
        elif mid == "date":
            assert any(m["message_id"] == "plan" for m in history)
            memory = "用户计划2026年10月17日带儿子去动物园玩一天。"
            fields["change_kind"] = "clarification"
            ids = ["plan", mid]
        else:
            assert any(m["message_id"] == "date" for m in history)
            memory = "用户因加班取消2026年10月17日带儿子去动物园玩一天的计划。"
            fields["change_kind"] = "update"
            ids = ["plan", "date", mid]
        return ExtractionOutput(
            facts=[ExtractedFact(memory=memory, message_ids=ids, **fields)],
            assessments=[
                ExtractionAssessment(message_id=mid, outcome="evaluated", reason="计划新增或维护")
            ],
        )

    def resolve(self, *, fact, candidates, targets):
        return GovernanceDecision(
            action="SUPERSEDE",
            memory_ids=[m["id"] for m in candidates],
            justification_message_id=targets[0]["message_id"],
            justification_quote=targets[0]["content"],
        )


def test_three_zoo_messages_refine_then_cancel_and_preserve_evidence(worker_context, engine):
    client, tenant = worker_context
    ids = []
    for i, (mid, content) in enumerate(
        [
            ("plan", "这周末我准备带儿子去动物园玩一天"),
            ("date", "确切的说是这周六"),
            ("cancel", "这周末还是不去了，要加班"),
        ]
    ):
        task_id = _add(
            client,
            key=mid,
            message_id=mid,
            content=content,
            occurred_at=f"2026-10-13T10:0{i}:00+08:00",
        )
        _run(engine, ZooModel())
        ids.append(task_id)
    with Session(engine) as session:
        tasks = [session.get(MemoryTask, task_id) for task_id in ids]
        assert all(task.status == "succeeded" for task in tasks)
        assert [
            task.payload["execution"]["processing_results"][0]["outcome"] for task in tasks
        ] == ["added", "superseded", "superseded"]
        memories = session.scalars(
            select(MemoryItem).where(MemoryItem.tenant_id == tenant).order_by(MemoryItem.created_at)
        ).all()
        assert [m.status for m in memories] == ["invalidated", "invalidated", "active"]
        assert "取消2026年10月17日" in memories[-1].content
        assert memories[-1].effective_at == _source(session, tenant, "cancel").received_at
        assert memories[-1].expired_at == memories[-1].effective_at + timedelta(days=30)
        for task in tasks[1:]:
            frozen = task.payload["execution"]
            assert frozen["slices"][0]["related_memories"]
            assert frozen["time_semantics_version"] == 3
        group = session.scalars(
            select(MemoryEvidence).where(MemoryEvidence.downstream_memory_id == memories[-1].id)
        ).all()
        assert {edge.evidence_locator["message_id"] for edge in group} == {"plan", "date", "cancel"}


def test_future_rule_waits_but_future_event_is_immediately_known(worker_context, engine):
    client, tenant = worker_context
    future = datetime.now(UTC) + timedelta(days=20)
    _add(client, key="future-rule", message_id="rule", content="下月1日起周报用列表。")

    class FutureRule(FakeFactModel):
        def extract(self, **kwargs):
            return [
                ExtractedFact(
                    memory="用户要求下月1日起周报用列表。",
                    message_ids=["rule"],
                    effective_at=future,
                )
            ]

    _run(engine, FutureRule())
    with Session(engine) as session:
        task = session.scalars(
            select(MemoryTask).where(
                MemoryTask.tenant_id == tenant, MemoryTask.task_type == "fact_extract"
            )
        ).one()
        assert task.status == "succeeded" and task.result_json == []
        assert (
            session.get(
                MemoryItem, task.payload["execution"]["processing_results"][0]["memory_ids"][0]
            ).effective_at
            == future
        )


def test_unknown_date_only_blocks_date_dependent_part(worker_context, engine):
    client, tenant = worker_context
    task_id = _post(
        client,
        key="unknown",
        messages=[
            _message(
                "unknown",
                "以前在北京住过；这周六去动物园。",
                metadata={"timestamp_origin": "adapter_observed"},
            )
        ],
    )

    class PartialModel(FakeFactModel):
        def extract_with_outcomes(self, *, targets, history):
            assert targets[0]["occurred_at"] is None
            return ExtractionOutput(
                facts=[
                    ExtractedFact(
                        memory="用户以前在北京住过。",
                        message_ids=["unknown"],
                        temporal_kind="historical",
                    )
                ],
                assessments=[
                    ExtractionAssessment(
                        message_id="unknown",
                        outcome="context_insufficient",
                        reason="无法确定是哪周",
                        reason_code="MISSING_DATE_ANCHOR",
                    )
                ],
            )

    _run(engine, PartialModel())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded" and len(task.result_json) == 1
        assert {r["outcome"] for r in task.payload["execution"]["processing_results"]} == {
            "added",
            "time_unresolved",
        }
        assert _source(session, tenant, "unknown").processed_version is None


def test_past_explicit_retention_is_rejected_without_retry(worker_context, engine):
    client, tenant = worker_context
    task_id = _add(
        client, key="bad-expiry", message_id="bad-expiry", content="这条记忆只存到去年。"
    )

    class BadExpiry(FakeFactModel):
        def extract(self, **kwargs):
            return [
                ExtractedFact(
                    memory="用户要求保存至去年。",
                    message_ids=["bad-expiry"],
                    expired_at=datetime(2025, 1, 1, tzinfo=UTC),
                )
            ]

    _run(engine, BadExpiry())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded" and task.result_json == []
        assert (
            task.payload["execution"]["processing_results"][0]["reason_code"]
            == "INVALID_RETENTION_INTERVAL"
        )
        assert _source(session, tenant, "bad-expiry").processed_version is None


def test_historical_content_can_be_corrected(worker_context, engine):
    client, tenant = worker_context

    class PastCorrection(FakeFactModel):
        def extract(self, *, targets, history):
            return [
                ExtractedFact(
                    memory=targets[0]["content"],
                    message_ids=[targets[0]["message_id"]],
                    temporal_kind="historical",
                    change_kind="correction",
                )
            ]

        def resolve(self, *, fact, candidates, targets):
            return GovernanceDecision(
                action="SUPERSEDE",
                memory_ids=[candidates[0]["id"]],
                justification_message_id=targets[0]["message_id"],
                justification_quote=targets[0]["content"],
            )

    for mid, text in [
        ("old", "用户2024年在北京住过。"),
        ("correct", "之前说错了，用户2024年住的是天津。"),
    ]:
        _add(client, key=mid, message_id=mid, content=text)
        _run(engine, PastCorrection())
    with Session(engine) as session:
        memories = session.scalars(
            select(MemoryItem).where(MemoryItem.tenant_id == tenant).order_by(MemoryItem.created_at)
        ).all()
        assert [m.status for m in memories] == ["invalidated", "active"]
        assert all(m.metadata_json["temporal_kind"] == "historical" for m in memories)


def test_controlled_reprocess_keeps_old_task_and_does_not_duplicate(worker_context, engine):
    client, tenant = worker_context
    original = _add(client, key="recover", message_id="recover", content="用户喜欢米粉。")
    _run(engine, FakeFactModel())
    with Session(engine) as session:
        previous = session.get(MemoryTask, original)
        old_payload, old_results = previous.payload, previous.result_json
        source = _source(session, tenant, "recover")
        received, version = source.received_at, source.version
        admin = AuthContext(tenant, "admin", frozenset(), can_manage_governance=True)
        task = reprocess_add(session, auth=admin, task_id=original, reason="规则升级复核")
        task_id = task.id
        session.commit()
    _run(engine, FakeFactModel())
    with Session(engine) as session:
        assert session.get(MemoryTask, original).payload == old_payload
        assert session.get(MemoryTask, original).result_json == old_results
        new = session.get(MemoryTask, task_id)
        assert new.status == "succeeded"
        assert new.payload["execution"]["processing_results"][0]["outcome"] == "already_exists"
        source = _source(session, tenant, "recover")
        assert (source.received_at, source.version) == (received, version)
        assert (
            session.scalars(
                select(MemoryAuditLog).where(
                    MemoryAuditLog.tenant_id == tenant,
                    MemoryAuditLog.reason_code == "ADD_REPROCESS_ENQUEUED",
                )
            )
            .one()
            .task_id
            == task_id
        )


def test_time_revision_versions_source_and_invalidates_old_evidence(worker_context, engine):
    client, tenant = worker_context
    original = _add(client, key="revise", message_id="revise", content="用户准备去动物园。")
    _run(engine, FakeFactModel())
    with Session(engine) as session:
        source = _source(session, tenant, "revise")
        received, old_time = source.received_at, source.occurred_at
        admin = AuthContext(tenant, "admin", frozenset(), can_manage_governance=True)
        task = reprocess_add(
            session,
            auth=admin,
            task_id=original,
            reason="核实来源时区后修订",
            source_times={source.id: datetime(2026, 10, 13, 2, tzinfo=UTC)},
        )
        assert source.version == 2 and source.received_at == received
        assert source.processed_version is None and source.occurred_at != old_time
        audit = session.scalars(
            select(MemoryAuditLog).where(
                MemoryAuditLog.tenant_id == tenant,
                MemoryAuditLog.reason_code == "SOURCE_TIME_REVISED",
            )
        ).one()
        assert audit.state_before["occurred_at"] == old_time.isoformat()
        assert (
            session.scalars(select(MemoryEvidence).where(MemoryEvidence.tenant_id == tenant))
            .one()
            .status
            == "invalidated"
        )
        assert task.payload["reprocess"]["previous_task_id"] == original


def test_reprocess_requires_admin_permission(worker_context, engine):
    client, tenant = worker_context
    original = _add(client, key="no-admin", message_id="no-admin", content="用户喜欢米粉。")
    _run(engine, FakeFactModel())
    with Session(engine) as session, pytest.raises(AuthorizationError):
        reprocess_add(
            session, auth=AuthContext(tenant, "agent", frozenset()), task_id=original, reason="复核"
        )


def test_unknown_target_marker_does_not_gain_real_time(api_context, engine):
    client, tenant = api_context
    body = _body()
    body["messages"][0]["metadata"] = {"timestamp_origin": "adapter_observed"}
    body["messages"][0].pop("occurred_at")
    assert client.post("/api/v1/memories:add", headers=_headers(), json=body).status_code == 202
    with Session(engine) as session:
        assert _source(session, tenant, "msg_1").occurred_at is None


def test_legacy_idempotency_and_source_hash_still_replay(api_context, engine):
    from memory_cmic.ingest import _canonical_json, _semantic_payload, _sha256

    client, tenant = api_context
    body = _body()
    request = AddRequest.model_validate(body)
    original = client.post("/api/v1/memories:add", headers=_headers(), json=body).json()
    with Session(engine) as session:
        task = session.get(MemoryTask, original["task_id"])
        task.payload = {"config_version": 2, "request_id": "legacy"}
        task.request_fingerprint = request_fingerprint(request, legacy=True)
        source = _source(session, tenant, "msg_1")
        source.metadata_json = {
            k: v
            for k, v in source.metadata_json.items()
            if k not in {"time_semantics_version", "source_input"}
        }
        auth = AuthContext(tenant, "agent_email", frozenset(["email_agent"]))
        source.semantic_hash = _sha256(
            _canonical_json(
                _semantic_payload(request.messages[0], request, auth, "user_1", legacy=True)
            )
        )
        session.commit()
    assert (
        client.post("/api/v1/memories:add", headers=_headers(), json=body).json()["task_id"]
        == original["task_id"]
    )
    assert (
        client.post("/api/v1/memories:add", headers=_headers(key="new-key"), json=body).status_code
        == 202
    )


def test_stale_related_state_forces_reextraction_before_commit(worker_context, engine, monkeypatch):
    from memory_cmic.providers import QwenFactModel

    client, tenant = worker_context
    _add(client, key="plan", message_id="plan", content="这周末准备带儿子去动物园。")
    _run(engine, ZooModel())
    task_id = _add(client, key="date", message_id="date", content="确切说是周六。")
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    calls = []

    def completion(prompt, payload):
        if "targets" in payload and "fact" not in payload:
            calls.append(payload)
            if len(calls) == 1:
                with Session(engine) as session:
                    memory = session.get(MemoryItem, payload["related_memories"][0]["id"])
                    memory.version += 1
                    session.commit()
            fact = ExtractedFact(
                memory="用户计划2026年10月17日带儿子去动物园。",
                message_ids=["plan", "date"],
                retention_kind="temporary",
                change_kind="clarification",
            )
            return {
                "facts": [fact.model_dump(mode="json")],
                "assessments": [
                    {"message_id": "date", "outcome": "evaluated", "reason": "细化计划日期"}
                ],
            }
        return {
            "action": "SUPERSEDE",
            "memory_ids": [payload["candidates"][0]["id"]],
            "justification_message_id": "date",
            "justification_quote": "确切说是周六。",
        }

    monkeypatch.setattr(model, "_json_completion", completion)
    _run(engine, model)
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "pending" and not task.payload["execution"]["changed"]
        task.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    _run(engine, model)
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded" and len(calls) == 2
        assert calls[0]["related_memories"][0]["version"] == 1
        assert calls[1]["related_memories"][0]["version"] == 2
        assert task.payload["execution"]["processing_results"][0]["outcome"] == "superseded"


def test_future_applicability_change_preserves_present_rule(worker_context, engine):
    client, tenant = worker_context
    _add(client, key="present", message_id="present", content="用户周报采用段落格式。")
    _run(engine, FakeFactModel())
    future = datetime.now(UTC) + timedelta(days=20)
    task_id = _add(client, key="future", message_id="future", content="下月1日起周报改为列表。")

    class FutureChange(FakeFactModel):
        def extract(self, **kwargs):
            return [
                ExtractedFact(
                    memory="用户下月1日起周报改为列表。",
                    message_ids=["future"],
                    effective_at=future,
                    change_kind="update",
                )
            ]

        def resolve(self, *, fact, candidates, targets):
            return GovernanceDecision(
                action="SUPERSEDE",
                memory_ids=[candidates[0]["id"]],
                justification_message_id="future",
                justification_quote="下月1日起周报改为列表。",
            )

    _run(engine, FutureChange())
    with Session(engine) as session:
        memories = session.scalars(
            select(MemoryItem).where(MemoryItem.tenant_id == tenant).order_by(MemoryItem.created_at)
        ).all()
        assert [m.status for m in memories] == ["active", "active"]
        assert memories[0].effective_at < datetime.now(UTC) < memories[1].effective_at
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded" and task.result_json == []
        assert task.payload["execution"]["processing_results"][0]["outcome"] == "added"


def test_raw_timestamp_expression_is_preserved(api_context, engine):
    client, tenant = api_context
    body = _body()
    body["messages"][0]["occurred_at"] = "2026-10-13 10:00:00"
    assert client.post("/api/v1/memories:add", headers=_headers(), json=body).status_code == 202
    with Session(engine) as session:
        source = _source(session, tenant, "msg_1")
        assert source.metadata_json["original_occurred_at_input"] == "2026-10-13 10:00:00"
        assert source.occurred_at == datetime(2026, 10, 13, 2, tzinfo=UTC)


def test_default_live_anchor_rejects_future_history_and_rolls_back(api_context, engine):
    client, tenant = api_context
    body = _body()
    body["messages"][0].pop("occurred_at")
    body["history_messages"] = [
        {
            "message_id": "future-history",
            "role": "user",
            "content": "用户准备去动物园。",
            "occurred_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        }
    ]
    response = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_MESSAGE"
    with Session(engine) as session:
        assert not session.scalars(
            select(SourceRecord).where(SourceRecord.tenant_id == tenant)
        ).all()


def test_inverted_model_interval_only_skips_affected_target(worker_context, engine, monkeypatch):
    from memory_cmic.providers import QwenFactModel

    client, tenant = worker_context
    task_id = _post(
        client,
        key="mixed-interval",
        messages=[_message("bad", "这条记忆存一段时间。"), _message("good", "用户喜欢米粉。")],
    )
    invalid = ExtractedFact(
        memory="用户要求保存记忆。",
        message_ids=["bad"],
        effective_at=datetime(2030, 1, 1, tzinfo=UTC),
        expired_at=datetime(2029, 1, 1, tzinfo=UTC),
    )
    good = ExtractedFact(memory="用户喜欢米粉。", message_ids=["good"], retention_kind="long_term")
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    monkeypatch.setattr(
        model,
        "_json_completion",
        lambda *args: {
            "facts": [f.model_dump(mode="json") for f in [invalid, good]],
            "assessments": [
                {"message_id": mid, "outcome": "evaluated", "reason": "已提取"}
                for mid in ["bad", "good"]
            ],
        },
    )
    _run(engine, model)
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded" and task.attempt_count == 0
        assert len(task.result_json) == 1 and "米粉" in task.result_json[0]["memory"]
        results = task.payload["execution"]["processing_results"]
        assert (
            next(r for r in results if r["message_id"] == "bad")["reason_code"]
            == "INVALID_RETENTION_INTERVAL"
        )
        assert _source(session, tenant, "bad").processed_version is None
        assert _source(session, tenant, "good").processed_version == 1


def test_history_promoted_later_cannot_use_resubmission_order_to_replace_same_time_fact(
    worker_context, engine
):
    client, tenant = worker_context
    old = _message("old-history", "我搬到北京了。")
    _post(
        client, key="initial", messages=[_message("current", "用户现在住在上海。")], history=[old]
    )
    _run(engine, FakeFactModel())
    task_id = _post(client, key="promoted", messages=[old])

    class LateHistory(FakeFactModel):
        def extract(self, **kwargs):
            return [
                ExtractedFact(
                    memory="用户住在北京。", message_ids=["old-history"], change_kind="update"
                )
            ]

        def resolve(self, *, fact, candidates, targets):
            return GovernanceDecision(
                action="SUPERSEDE",
                memory_ids=[candidates[0]["id"]],
                justification_message_id="old-history",
                justification_quote="我搬到北京了。",
            )

    _run(engine, LateHistory())
    with Session(engine) as session:
        memory = session.scalars(select(MemoryItem).where(MemoryItem.tenant_id == tenant)).one()
        assert memory.status == "active" and "上海" in memory.content
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded"
        assert task.payload["execution"]["processing_results"][0]["reason_code"] == "LATE_STATEMENT"
        assert task.payload["execution"]["input"]["targets"][0]["statement_order"]["phase"] == 0


def test_same_time_older_history_can_be_retained_without_overwriting_current(
    worker_context, engine
):
    client, tenant = worker_context
    old = _message("old-history", "用户当时住在北京。")
    _post(
        client, key="initial", messages=[_message("current", "用户现在住在上海。")], history=[old]
    )
    _run(engine, FakeFactModel())
    task_id = _post(client, key="promoted", messages=[old])

    class OldHistory(FakeFactModel):
        def resolve(self, *, fact, candidates, targets):
            return GovernanceDecision(action="ADD", historical_before_id=candidates[0]["id"])

    _run(engine, OldHistory())
    with Session(engine) as session:
        memories = session.scalars(
            select(MemoryItem).where(MemoryItem.tenant_id == tenant).order_by(MemoryItem.created_at)
        ).all()
        assert [m.status for m in memories] == ["active", "active"]
        assert "上海" in memories[0].content
        assert memories[1].metadata_json["temporal_kind"] == "historical"
        source = _source(session, tenant, "old-history")
        assert memories[1].effective_at == source.received_at
        assert memories[1].expired_at == source.received_at + timedelta(days=180)
        assert session.get(MemoryTask, task_id).result_json[0]["id"] == memories[1].id


def test_incomplete_admin_reassessment_allows_normal_context_recovery(worker_context, engine):
    client, tenant = worker_context
    original = _add(client, key="missing", message_id="missing", content="记住这个。")

    class Misclassified(FakeFactModel):
        def extract_with_outcomes(self, **kwargs):
            return ExtractionOutput(
                facts=[],
                assessments=[
                    ExtractionAssessment(
                        message_id="missing", outcome="no_durable_value", reason="旧规则误判"
                    )
                ],
            )

    _run(engine, Misclassified())
    with Session(engine) as session:
        source = _source(session, tenant, "missing")
        received = source.received_at
        assert source.processed_version == source.version
        task = reprocess_add(
            session,
            auth=AuthContext(tenant, "admin", frozenset(), can_manage_governance=True),
            task_id=original,
            reason="复核无价值误判",
        )
        session.commit()
        task_id = task.id

    class Missing(FakeFactModel):
        def extract_with_outcomes(self, **kwargs):
            return ExtractionOutput(
                facts=[],
                assessments=[
                    ExtractionAssessment(
                        message_id="missing",
                        outcome="context_insufficient",
                        reason="缺少原文",
                        reason_code="MISSING_REFERENT",
                    )
                ],
            )

    _run(engine, Missing())
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "succeeded"
        assert _source(session, tenant, "missing").processed_version is None
        assert (
            session.get(MemoryTask, original).payload["execution"]["processing_results"][0][
                "outcome"
            ]
            == "no_durable_value"
        )
    _post(
        client,
        key="context-recovery",
        messages=[_message("missing", "记住这个。")],
        history=[_message("proposal", "以后周报使用三段式。", "assistant")],
    )

    class Complete(FakeFactModel):
        def extract(self, *, targets, history):
            assert targets[0]["message_id"] == "missing"
            return [
                ExtractedFact(
                    memory="用户要求周报使用三段式。",
                    message_ids=["proposal", "missing"],
                    retention_kind="long_term",
                )
            ]

    _run(engine, Complete())
    with Session(engine) as session:
        source = _source(session, tenant, "missing")
        assert source.processed_version == source.version and source.received_at == received


def test_later_confirmation_protects_against_intermediate_late_update_without_renewing_ttl(
    worker_context, engine
):
    client, tenant = worker_context
    _add(
        client,
        key="first",
        message_id="first",
        content="用户住在北京。",
        occurred_at="2026-10-13T09:00:00+08:00",
    )
    _run(engine, FakeFactModel())
    with Session(engine) as session:
        memory = session.scalars(select(MemoryItem).where(MemoryItem.tenant_id == tenant)).one()
        original_id, effective, expiry, version = (
            memory.id,
            memory.effective_at,
            memory.expired_at,
            memory.version,
        )
    _add(
        client,
        key="confirm",
        message_id="confirm",
        content="用户住在北京。",
        occurred_at="2026-10-15T09:00:00+08:00",
    )
    _run(engine, FakeFactModel())
    task_id = _add(
        client,
        key="late",
        message_id="late",
        content="用户搬到上海了。",
        occurred_at="2026-10-14T09:00:00+08:00",
    )

    class LateUpdate(FakeFactModel):
        def extract(self, **kwargs):
            return [
                ExtractedFact(memory="用户住在上海。", message_ids=["late"], change_kind="update")
            ]

        def resolve(self, *, fact, candidates, targets):
            return GovernanceDecision(
                action="SUPERSEDE",
                memory_ids=[candidates[0]["id"]],
                justification_message_id="late",
                justification_quote="用户搬到上海了。",
            )

    _run(engine, LateUpdate())
    with Session(engine) as session:
        memory = session.get(MemoryItem, original_id)
        assert (memory.effective_at, memory.expired_at, memory.version) == (
            effective,
            expiry,
            version,
        )
        assert memory.status == "active"
        assert memory.metadata_json["source_occurred_at"] == "2026-10-15T09:00:00+08:00"
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded"
        assert task.payload["execution"]["processing_results"][0]["reason_code"] == "LATE_STATEMENT"
