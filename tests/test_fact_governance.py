from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_add_worker import FakeEmbedder, _add
from test_add_worker import worker_context as _worker_context

from memory_cmic.extraction_input import ExtractionBudget, input_size
from memory_cmic.models import (
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    SourceRecord,
)
from memory_cmic.providers import (
    EXTRACTION_PROMPT,
    ExtractedFact,
    GovernanceDecision,
    OutputLimitError,
    ProviderError,
    QwenFactModel,
)
from memory_cmic.repositories import add_evidence_group, create_or_replace_profile
from memory_cmic.retrieval import search_memories
from memory_cmic.worker import run_once

worker_context = _worker_context


class ScriptedModel:
    def __init__(self, fields=None, action="ADD", on_extract=None):
        self.fields = fields or {}
        self.action = action
        self.on_extract = on_extract
        self.seen = []

    def extract(self, *, targets, history):
        self.seen.append([m["message_id"] for m in targets])
        if self.on_extract:
            self.on_extract(targets)
        return [
            ExtractedFact(memory=m["content"], message_ids=[m["message_id"]], **self.fields)
            for m in targets
            if m["role"] == "user"
        ]

    def resolve(self, *, fact, candidates, targets):
        if self.action == "ADD":
            return GovernanceDecision(action="ADD")
        ids = [m["id"] for m in candidates]
        if self.action == "DUPLICATE":
            ids = ids[:1]
        return GovernanceDecision(
            action=self.action,
            memory_ids=ids,
            justification_message_id=targets[-1]["message_id"],
            justification_quote=targets[-1]["content"],
        )


def _run(engine, model, budget=ExtractionBudget()):
    assert run_once(
        sessionmaker(engine, expire_on_commit=False),
        worker_id="p02_worker",
        fact_model=model,
        embedder=FakeEmbedder(),
        duplicate_threshold=0.85,
        budget=budget,
    )


def _memories(session, tenant_id):
    return session.scalars(
        select(MemoryItem).where(MemoryItem.tenant_id == tenant_id).order_by(MemoryItem.created_at)
    ).all()


@pytest.mark.parametrize("kind,days", [("temporary", 30), ("regular", 180), ("long_term", None)])
def test_new_fact_gets_classified_ttl(worker_context, engine, kind, days):
    client, tenant_id = worker_context
    _add(client, key="ttl", message_id="ttl", content="本阶段由我负责材料审核。")
    _run(engine, ScriptedModel({"retention_kind": kind}))
    with Session(engine) as session:
        memory = _memories(session, tenant_id)[0]
        assert memory.metadata_json["retention_kind"] == kind
        assert memory.metadata_json["explicit_expired_at"] is None
        assert memory.expired_at == (
            memory.effective_at + timedelta(days=days) if days is not None else None
        )


@pytest.mark.parametrize("semantic_duplicate", [False, True])
def test_new_statement_only_adds_evidence_without_renewing_ttl(
    worker_context, engine, semantic_duplicate
):
    client, tenant_id = worker_context
    _add(client, key="ttl-first", message_id="first", content="本阶段由我负责材料审核。")
    _run(engine, ScriptedModel({"retention_kind": "temporary"}))
    with Session(engine) as session:
        original = _memories(session, tenant_id)[0]
        original_id, expiry, version = original.id, original.expired_at, original.version
    _add(
        client, key="ttl-second", message_id="second",
        content="本阶段的材料审核由我负责。" if semantic_duplicate else "本阶段由我负责材料审核。",
        occurred_at="2026-09-25T09:00:00+08:00",
    )
    _run(engine, ScriptedModel({"retention_kind": "temporary"}, action="DUPLICATE"))
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        assert len(memories) == 1
        assert (memories[0].id, memories[0].expired_at, memories[0].version) == (
            original_id, expiry, version
        )
        assert len(session.scalars(select(MemoryEvidence).where(
            MemoryEvidence.tenant_id == tenant_id,
            MemoryEvidence.downstream_memory_id == original_id,
        )).all()) == 2


def test_explicit_deadline_and_delayed_expired_fact(worker_context, engine):
    client, tenant_id = worker_context
    deadline = datetime.now(UTC) + timedelta(days=365)
    _add(client, key="explicit", message_id="explicit", content="这个要求有效至指定日期。")
    _run(engine, ScriptedModel({"expired_at": deadline, "retention_kind": "temporary"}))
    task_id = _add(
        client, key="delayed", message_id="delayed", content="当时本阶段由我负责材料审核。",
        occurred_at=(datetime.now(UTC) - timedelta(days=31)).isoformat(),
    )
    _run(engine, ScriptedModel({"retention_kind": "temporary"}))
    with Session(engine) as session:
        explicit, delayed = _memories(session, tenant_id)
        assert explicit.expired_at == deadline
        assert explicit.metadata_json["explicit_expired_at"] == deadline.isoformat()
        assert delayed.status == "expired"
        assert session.get(MemoryTask, task_id).result_json == []
        assert session.scalar(select(MemoryEmbedding).where(
            MemoryEmbedding.tenant_id == tenant_id,
            MemoryEmbedding.memory_id == delayed.id,
        )) is None


@pytest.mark.parametrize("second_scope", [["disk"], ["communication"], None])
def test_same_text_in_different_scope_is_not_merged(worker_context, engine, second_scope):
    client, tenant_id = worker_context
    _add(client, key="scope1", message_id="scope1", content="使用简洁表达。")
    _run(engine, ScriptedModel({"business_domains": ["email"]}))
    _add(client, key="scope2", message_id="scope2", content="使用简洁表达。")
    _run(engine, ScriptedModel({"business_domains": second_scope}, action="DUPLICATE"))
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        assert len(memories) == 2
        assert memories[0].business_domains == ["email"]
        assert memories[1].business_domains == second_scope
        assert all(m.project_domains is None for m in memories)


@pytest.mark.parametrize(
    "content,normalized,domains,project_context",
    [
        ("我喜欢吃年糕。", "用户喜欢吃年糕。", None, None),
        ("早餐时我喜欢吃年糕。", "用户在早餐时喜欢吃年糕。", None, None),
        ("以后邮件回复都简洁一些。", "用户希望以后邮件回复简洁。", ["email"], None),
        ("未知项目的材料放在批准目录。", "未知项目的材料存放在批准目录。", None, "未知项目"),
    ],
)
def test_worker_saves_facts_without_scope_known_and_preserves_conditions(
    worker_context, engine, monkeypatch, content, normalized, domains, project_context
):
    client, tenant_id = worker_context
    task_id = _add(client, key="conditions", message_id="conditions", content=content)
    fact = {
        "memory": normalized,
        "message_ids": ["conditions"],
        "business_domains": domains,
        "project_limited": project_context is not None,
        "project_context": project_context,
        "temporal_kind": "current",
        "retention_kind": "long_term",
        "change_kind": "none",
        "effective_at": None,
        "expired_at": None,
    }
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    monkeypatch.setattr(model, "_json_completion", lambda *args: {"facts": [fact]})
    _run(engine, model)
    response = client.get(
        f"/api/v1/tasks/{task_id}", headers={"Authorization": "Bearer worker-token"}
    ).json()
    assert response["status"] == "succeeded"
    assert response["results"][0]["memory"] == normalized
    with Session(engine) as session:
        memory = _memories(session, tenant_id)[0]
        assert memory.content == normalized
        assert memory.summary == normalized
        assert memory.business_domains == domains
        assert memory.metadata_json["project_context"] == project_context
        assert memory.expired_at is None
        evidence = session.scalars(
            select(MemoryEvidence).where(MemoryEvidence.downstream_memory_id == memory.id)
        ).one()
        assert evidence.evidence_snippet == content
        source = session.get(SourceRecord, evidence.upstream_source_id)
        assert source.raw_content == content
        assert source.processed_version == 1
        for domain in ("email", "disk"):
            found = search_memories(
                session,
                tenant_id=tenant_id,
                user_id="user_worker",
                business_domain=domain,
                project_id=None,
                authorized_agent_ids=[],
                company_ids=[],
            )
            expected = [memory.id] if project_context is None and (
                domains is None or domain in domains
            ) else []
            assert [item.id for item in found] == expected


def test_unresolved_historical_interval_is_skipped(worker_context, engine):
    client, tenant_id = worker_context
    task_id = _add(client, key="skip", message_id="skip", content="项目专属要求。")
    _run(engine, ScriptedModel({"temporal_kind": "historical"}))
    with Session(engine) as session:
        assert _memories(session, tenant_id) == []
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded"
        assert task.result_json == []
        assert task.payload["execution"]["diagnostics"][0]["code"] == "TIME_UNRESOLVED"
        assert not session.scalars(
            select(MemoryAuditLog).where(
                MemoryAuditLog.tenant_id == tenant_id,
                MemoryAuditLog.reason_code == "TIME_UNRESOLVED",
            )
        ).all()


def test_explicit_correction_invalidates_fact_and_derived_profile(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="old", message_id="old", content="我住北京。")
    _run(engine, ScriptedModel())
    with Session(engine) as session:
        old = _memories(session, tenant_id)[0]
        old_id = old.id
        profile = create_or_replace_profile(
            session,
            {
                "id": f"profile_{tenant_id}",
                "tenant_id": tenant_id,
                "user_id": "user_worker",
                "property_key": "residence",
                "property_value": "北京",
                "value_type": "string",
                "confidence": 1,
                "effective_at": datetime.now(UTC),
            },
        )
        add_evidence_group(
            session,
            [
                {
                    "id": f"basis_{tenant_id}",
                    "tenant_id": tenant_id,
                    "relationship_type": "profile_basis",
                    "evidence_group_id": f"group_{tenant_id}",
                    "upstream_memory_id": old.id,
                    "downstream_profile_id": profile.id,
                    "upstream_version": old.version,
                }
            ],
        )
        session.commit()
    _add(client, key="new", message_id="new", content="之前说错了，我现在住上海。")
    _run(engine, ScriptedModel({"change_kind": "correction"}, action="SUPERSEDE"))
    with Session(engine) as session:
        from memory_cmic.models import ProfileProperty

        memories = _memories(session, tenant_id)
        old = next(m for m in memories if "北京" in m.content)
        new = next(m for m in memories if "上海" in m.content)
        assert old.status == "invalidated" and new.status == "active"
        assert new.supersedes_id == old_id
        assert session.get(ProfileProperty, f"profile_{tenant_id}").status == "invalidated"
        assert (
            session.scalars(select(MemoryEmbedding).where(MemoryEmbedding.memory_id == old_id))
            .one()
            .status
            == "stale"
        )
        # The invalidated property and the user's current facts both request a rebuild.
        rebuilds = session.scalars(
            select(MemoryTask).where(
                MemoryTask.tenant_id == tenant_id,
                MemoryTask.task_type == "profile_rebuild",
            )
        ).all()
        assert rebuilds and all(task.status == "pending" for task in rebuilds)


def test_dispute_and_explicit_clarification(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="birthday1", message_id="birthday1", content="生日是5月1日。")
    _run(engine, ScriptedModel())
    task_id = _add(client, key="birthday2", message_id="birthday2", content="生日是5月2日。")
    _run(engine, ScriptedModel(action="DISPUTE"))
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        assert len(memories) == 2
        assert {m.status for m in memories} == {"disputed"}
        assert len({m.conflict_group_id for m in memories}) == 1
        assert memories[0].conflict_group_id is not None
        assert session.get(MemoryTask, task_id).result_json == []
        event = session.scalars(select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == tenant_id,
            MemoryAuditLog.reason_code == "FACT_DISPUTED",
        )).one()
        assert event.action == "UPDATE" and event.task_id == task_id
        assert event.state_before["status"] == "active"
        assert event.state_after["status"] == "disputed"
    _add(
        client,
        key="birthday3",
        message_id="birthday3",
        content="我确认一下，生日确实是5月2日，5月1日是错误的。",
    )
    _run(engine, ScriptedModel({"change_kind": "clarification"}, action="SUPERSEDE"))
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        assert [m.status for m in memories] == ["invalidated", "invalidated", "active"]


def test_disputed_fact_cannot_be_restored_without_clarification(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="one", message_id="one", content="生日是5月1日。")
    _run(engine, ScriptedModel())
    _add(client, key="two", message_id="two", content="生日是5月2日。")
    _run(engine, ScriptedModel(action="DISPUTE"))
    task_id = _add(client, key="three", message_id="three", content="生日是5月3日。")
    _run(engine, ScriptedModel({"change_kind": "correction"}, action="SUPERSEDE"))
    with Session(engine) as session:
        assert {m.status for m in _memories(session, tenant_id)} == {"disputed"}
        assert session.get(MemoryTask, task_id).status == "pending"


def test_late_source_cannot_replace_newer_fact(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="newer", message_id="newer", content="现在住上海。")
    _run(engine, ScriptedModel())
    with Session(engine) as session:
        memory = _memories(session, tenant_id)[0]
        memory.metadata_json = {
            **memory.metadata_json,
            "source_occurred_at": "2026-09-26T00:00:00+00:00",
        }
        session.commit()
    task_id = _add(client, key="late", message_id="late", content="我搬到北京了。")
    # _add sources occurred on September 24, before the existing September 26 assertion.
    _run(engine, ScriptedModel({"change_kind": "update"}, action="SUPERSEDE"))
    with Session(engine) as session:
        assert len(_memories(session, tenant_id)) == 1
        assert _memories(session, tenant_id)[0].status == "active"
        assert session.get(MemoryTask, task_id).status == "pending"


def _test_slice_budget():
    target = {
        "message_id": "m1",
        "role": "user",
        "content": "甲" * 180,
        "occurred_at": "2026-09-27T01:00:00+00:00",
        "author_id": "user_worker",
        "tool_status": None,
        "reply_to_message_id": None,
        "start_char": 0,
        "end_char": 180,
    }
    return ExtractionBudget(
        input_tokens=input_size(EXTRACTION_PROMPT, {"targets": [target], "history": []})
        + 1024
        + 100,
        output_tokens=1024,
    )


def _add_batch(client, messages):
    response = client.post(
        "/api/v1/memories:add",
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "batch"},
        json={
            "source_system": "email_agent",
            "user_id": "user_worker",
            "session_id": "session_worker",
            "messages": [
                {
                    "message_id": mid,
                    "role": "user",
                    "content": content,
                    "occurred_at": "2026-09-27T09:00:00+08:00",
                }
                for mid, content in messages
            ],
        },
    )
    assert response.status_code == 202
    return response.json()["task_id"]


def test_correction_within_one_slice_uses_prior_candidate(worker_context, engine):
    client, tenant_id = worker_context
    task_id = _add_batch(client, [("m1", "我住北京。"), ("m2", "之前说错了，我住上海。")])
    model = ScriptedModel({"change_kind": "correction"}, action="SUPERSEDE")
    _run(engine, model)
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        old = next(m for m in memories if "北京" in m.content)
        new = next(m for m in memories if "上海" in m.content)
        assert old.status == "invalidated"
        assert new.supersedes_id == old.id
        results = session.get(MemoryTask, task_id).result_json
        assert len(results) == 1 and results[0]["id"] == new.id


def test_slices_resume_without_repeating_committed_evidence(worker_context, engine):
    client, tenant_id = worker_context
    # Small test budget guarantees one long message per slice.
    task_id = _add_batch(client, [("m1", "甲" * 180), ("m2", "乙" * 180)])
    budget = _test_slice_budget()
    failed = False

    def fail_second(targets):
        nonlocal failed
        if targets[0]["message_id"] == "m2" and not failed:
            failed = True
            raise ProviderError("temporary model failure")

    class ShortFactsModel(ScriptedModel):
        def extract(self, **kwargs):
            return [
                f.model_copy(update={"memory": f.memory[:10]}) for f in super().extract(**kwargs)
            ]

    model = ShortFactsModel(on_extract=fail_second)
    _run(engine, model, budget)
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "pending"
        assert task.payload["execution"]["next_slice"] == 1
        assert len(_memories(session, tenant_id)) == 1
        task.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    _run(engine, model, budget)
    assert model.seen == [["m1"], ["m2"], ["m2"]]
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "succeeded"
        assert len(_memories(session, tenant_id)) == 2
        assert (
            len(
                session.scalars(
                    select(MemoryEvidence).where(MemoryEvidence.tenant_id == tenant_id)
                ).all()
            )
            == 2
        )


@pytest.mark.parametrize("only_evidence", [False, True])
def test_terminal_partial_preserves_committed_changes(worker_context, engine, only_evidence):
    client, tenant_id = worker_context
    if only_evidence:
        _add(client, key="seed", message_id="seed", content="甲" * 180)
        _run(engine, ScriptedModel())
    task_id = _add_batch(client, [("m1", "甲" * 180), ("m2", "乙" * 180)])
    with Session(engine) as session:
        session.get(MemoryTask, task_id).max_attempts = 1
        session.commit()

    def fail_second(targets):
        if targets[0]["message_id"] == "m2":
            raise ProviderError("permanent model failure")

    _run(
        engine,
        ScriptedModel(on_extract=fail_second),
        _test_slice_budget(),
    )
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "partial"
        assert len(task.result_json) == (0 if only_evidence else 1)
        assert task.error_json["code"] == "EXTRACTION_FAILED"
        response = client.get(
            f"/api/v1/tasks/{task_id}", headers={"Authorization": "Bearer worker-token"}
        )
        assert response.json()["status"] == "partial"
        assert response.json()["results"] == task.result_json


def test_output_truncation_is_split_and_last_fragment_marks_source_processed(
    worker_context, engine
):
    client, tenant_id = worker_context
    task_id = _add(client, key="truncated", message_id="long", content="甲乙丙丁")
    calls = 0

    def truncate_once(targets):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OutputLimitError("truncated")

    _run(engine, ScriptedModel(on_extract=truncate_once))
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "succeeded"
        assert task.payload["execution"]["next_slice"] == 2
        source = session.scalars(
            select(SourceRecord).where(SourceRecord.tenant_id == tenant_id)
        ).one()
        assert source.processed_version == source.version
        evidence = session.scalars(
            select(MemoryEvidence).where(MemoryEvidence.tenant_id == tenant_id)
        ).all()
        assert {tuple(sorted(e.evidence_locator.items())) for e in evidence} == {
            (("end_char", 2), ("message_id", "long"), ("start_char", 0)),
            (("end_char", 4), ("message_id", "long"), ("start_char", 2)),
        }


def test_model_call_budget_exhaustion_is_terminal(worker_context, engine):
    client, tenant_id = worker_context
    task_id = _add(client, key="budget", message_id="budget", content="称呼我林舟。")
    _run(engine, ScriptedModel(), ExtractionBudget(max_model_calls=1))
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "failed"
        assert task.payload["execution"]["model_calls"] == 1
        assert task.error_json["code"] == "EXTRACTION_FAILED"
        assert _memories(session, tenant_id) == []


def test_source_change_after_first_slice_reports_partial(worker_context, engine):
    client, tenant_id = worker_context
    task_id = _add_batch(client, [("m1", "甲" * 180), ("m2", "乙" * 180)])

    def change_second(targets):
        if targets[0]["message_id"] == "m2":
            with Session(engine) as session:
                source = session.scalars(
                    select(SourceRecord).where(
                        SourceRecord.tenant_id == tenant_id, SourceRecord.message_id == "m2"
                    )
                ).one()
                source.version += 1
                session.commit()

    _run(engine, ScriptedModel(on_extract=change_second), _test_slice_budget())
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "partial"
        assert len(_memories(session, tenant_id)) == 1


def test_bounded_historical_fact_is_retained_without_current_embedding(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="history1", message_id="history1", content="2025年住北京。")
    _run(
        engine,
        ScriptedModel(
            {
                "temporal_kind": "historical",
                "effective_at": datetime(2025, 1, 1, tzinfo=UTC),
                "expired_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ),
    )
    _add(client, key="history2", message_id="history2", content="2026年现在住上海。")
    _run(engine, ScriptedModel())
    with Session(engine) as session:
        old, current = _memories(session, tenant_id)
        assert old.status == "expired" and current.status == "active"
        assert old.conflict_group_id is None and current.supersedes_id is None
        assert (
            session.scalars(
                select(MemoryEmbedding).where(MemoryEmbedding.memory_id == old.id)
            ).all()
            == []
        )


def test_delayed_assertion_is_bounded_as_history(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="current", message_id="current", content="现在住上海。")
    _run(engine, ScriptedModel({"effective_at": datetime(2026, 9, 26, tzinfo=UTC)}))
    with Session(engine) as session:
        current = _memories(session, tenant_id)[0]
        current.metadata_json = {
            **current.metadata_json,
            "source_occurred_at": "2026-09-26T00:00:00+00:00",
        }
        session.commit()
    task_id = _add(client, key="delayed", message_id="delayed", content="现在住北京。")

    class HistoricalModel(ScriptedModel):
        def resolve(self, *, fact, candidates, targets):
            return GovernanceDecision(action="ADD", historical_before_id=candidates[0]["id"])

    _run(engine, HistoricalModel())
    with Session(engine) as session:
        current, historical = _memories(session, tenant_id)
        assert current.status == "active"
        assert historical.status == "expired"
        assert historical.expired_at == current.effective_at
        assert historical.metadata_json["temporal_kind"] == "historical"
        assert "2026-09-24" in historical.content
        assert session.get(MemoryTask, task_id).result_json == []


def test_supersession_rejects_invented_justification(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="quoted1", message_id="quoted1", content="住北京。")
    _run(engine, ScriptedModel())
    task_id = _add(client, key="quoted2", message_id="quoted2", content="之前说错了，住上海。")

    class InventedQuoteModel(ScriptedModel):
        def resolve(self, **kwargs):
            decision = super().resolve(**kwargs)
            return decision.model_copy(update={"justification_quote": "不在用户原文中的纠正"})

    _run(engine, InventedQuoteModel({"change_kind": "correction"}, action="SUPERSEDE"))
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "pending"
        assert len(_memories(session, tenant_id)) == 1
        assert _memories(session, tenant_id)[0].status == "active"


def test_removed_execution_payload_after_evidence_commit_stays_partial(worker_context, engine):
    client, tenant_id = worker_context
    _add(client, key="evidence-seed", message_id="evidence-seed", content="甲" * 180)
    _run(engine, ScriptedModel())
    task_id = _add_batch(client, [("m1", "甲" * 180), ("m2", "乙" * 180)])

    def remove_payload(targets):
        if targets[0]["message_id"] == "m2":
            with Session(engine) as session:
                session.get(MemoryTask, task_id).payload = {"config_version": 2}
                session.commit()

    _run(engine, ScriptedModel(on_extract=remove_payload), _test_slice_budget())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "partial" and task.result_json == []
        assert task.error_json["code"] == "INPUT_PROCESSING_FAILED"
        assert len(_memories(session, tenant_id)) == 1
        evidence = session.scalars(
            select(MemoryEvidence).where(MemoryEvidence.tenant_id == tenant_id)
        ).all()
        assert len(evidence) == 2
        assert session.scalars(
            select(MemoryAuditLog).where(
                MemoryAuditLog.tenant_id == tenant_id,
                MemoryAuditLog.reason_code == "FACT_EVIDENCE_ADDED",
                MemoryAuditLog.task_id == task_id,
            )
        ).one()


@pytest.mark.parametrize("action", ["DUPLICATE", "SUPERSEDE", "DISPUTE"])
def test_project_memories_retained_without_cross_project_governance(worker_context, engine, action):
    client, tenant_id = worker_context
    for index, project in enumerate(["星河项目", "远山项目"]):
        _add(
            client,
            key=f"project{index}",
            message_id=f"project{index}",
            content=f"{project}的邮件总结用列表。",
        )
        _run(
            engine,
            ScriptedModel(
                {
                    "business_domains": ["email"],
                    "project_limited": True,
                    "project_context": project,
                },
                action=action,
            ),
        )
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        assert len(memories) == 2
        assert all(m.project_domains is None and m.status == "active" for m in memories)
        assert {m.metadata_json["project_context"] for m in memories} == {"星河项目", "远山项目"}
        assert all(m.metadata_json["project_context"] in m.content for m in memories)


def test_project_repeat_adds_evidence_without_becoming_general(worker_context, engine):
    client, tenant_id = worker_context
    for index in [1, 2]:
        _add(
            client,
            key=f"project{index}",
            message_id=f"project{index}",
            content="星河项目邮件总结用列表。",
        )
        _run(
            engine,
            ScriptedModel(
                {
                    "business_domains": ["email"],
                    "project_limited": True,
                    "project_context": "星河项目",
                }
            ),
        )
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        assert len(memories) == 1
        assert (
            len(
                session.scalars(
                    select(MemoryEvidence).where(
                        MemoryEvidence.downstream_memory_id == memories[0].id
                    )
                ).all()
            )
            == 2
        )


def test_new_business_scenarios_are_retained_and_synonyms_governed(worker_context, engine):
    client, tenant_id = worker_context
    for index, label in enumerate(["方案编写", "项目方案写作"]):
        _add(
            client,
            key=f"scenario{index}",
            message_id=f"scenario{index}",
            content="项目方案详细说明。",
        )
        _run(engine, ScriptedModel({"business_domains": [label]}, action="DUPLICATE"))
    with Session(engine) as session:
        memories = _memories(session, tenant_id)
        assert len(memories) == 1
        assert memories[0].business_domains == ["方案编写"]


def test_reserved_project_retrieval_requires_task_context(worker_context, engine):
    from memory_cmic.retrieval import search_memories

    client, tenant_id = worker_context
    for index, project in enumerate(["星河项目", "远山项目"]):
        _add(
            client,
            key=f"retrieve{index}",
            message_id=f"retrieve{index}",
            content=f"{project}邮件总结用列表。",
        )
        _run(
            engine,
            ScriptedModel(
                {"business_domains": ["email"], "project_limited": True, "project_context": project}
            ),
        )
    with Session(engine) as session:
        user_id = _memories(session, tenant_id)[0].subject_id
        params = dict(
            tenant_id=tenant_id,
            user_id=user_id,
            business_domain="邮件",
            project_id=None,
            authorized_agent_ids=[],
            company_ids=[],
        )
        assert search_memories(session, **params) == []

        class Selector:
            def select_applicable(self, *, query, business_domain, candidates):
                assert business_domain == "email"
                assert len(candidates) == 2
                return [m["id"] for m in candidates if m["project_context"] in query]

        results = search_memories(
            session, **params, query="总结星河项目邮件", applicability_model=Selector()
        )
        assert len(results) == 1
        assert results[0].metadata_json["project_context"] == "星河项目"


def test_semantic_retrieval_can_match_new_scenario_and_reject_unknown_id(worker_context, engine):
    from memory_cmic.retrieval import search_memories

    client, tenant_id = worker_context
    _add(client, key="scenario-search", message_id="scenario-search", content="项目方案详细说明。")
    _run(engine, ScriptedModel({"business_domains": ["方案编写"]}))
    with Session(engine) as session:
        memory = _memories(session, tenant_id)[0]
        params = dict(
            tenant_id=tenant_id,
            user_id=memory.subject_id,
            business_domain="项目方案写作",
            project_id=None,
            authorized_agent_ids=[],
            company_ids=[],
            query="请写详细的项目方案",
        )

        class Selector:
            def select_applicable(self, *, query, business_domain, candidates):
                assert candidates[0]["business_domains"] == ["方案编写"]
                return [memory.id]

        assert search_memories(session, **params, applicability_model=Selector()) == [memory]

        class InvalidSelector:
            def select_applicable(self, **kwargs):
                return ["unauthorized-id"]

        with pytest.raises(ProviderError, match="unknown memory"):
            search_memories(session, **params, applicability_model=InvalidSelector())
