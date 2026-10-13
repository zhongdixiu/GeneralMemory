from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from test_add_api import _settings
from test_consolidation import seed_fact

import memory_cmic.api as api_module
import memory_cmic.retrieval as retrieval_module
from memory_cmic.auth import StaticCredentialStore
from memory_cmic.deterministic_embedding import deterministic_embedding
from memory_cmic.models import (
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)
from memory_cmic.profile import read_profiles
from memory_cmic.providers import ProviderError
from memory_cmic.repositories import add_evidence_group, create_memory, create_or_replace_profile


class FakeEmbedder:
    model_id = "read-api-test"

    def __init__(self):
        self._client = nullcontext()

    def embed(self, texts):
        return [deterministic_embedding(text, self.model_id) for text in texts]


class FakeSelector:
    def __init__(self):
        self._client = nullcontext()
        self.candidates = []
        self.before_return = None

    def select_for_query(self, *, query, candidates):
        self.candidates = candidates
        if self.before_return is not None:
            self.before_return()
        return [{"id": item["id"], "include": True, "reason": "relevant"} for item in candidates]

    def select_for_query_ids(self, *, query, candidates):
        return [item["id"] for item in self.select_for_query(query=query, candidates=candidates)
                if item["include"]]


@pytest.fixture
def read_api(connection, monkeypatch):
    # Evidence/access tests need exact candidates, independent of HNSW approximate recall.
    connection.execute(text("SET LOCAL enable_indexscan = off"))
    tenant = f"tenant_read_{uuid4().hex}"
    credentials = StaticCredentialStore([
        {
            "token": token,
            "tenant_id": credential_tenant,
            "caller_agent_id": agent,
            "allowed_source_systems": ["test"],
        }
        for token, credential_tenant, agent in (
            ("agent-a", tenant, "agent_a"),
            ("agent-b", tenant, "agent_b"),
            ("other-tenant", f"{tenant}_other", "agent_a"),
        )
    ] + [{
        "token": "manager", "tenant_id": tenant, "caller_agent_id": "admin",
        "can_manage_governance": True,
    }])
    settings = replace(
        _settings(tenant), credentials=credentials,
        dashscope_api_key="fake", siliconflow_api_key="fake",
        siliconflow_embedding_model=FakeEmbedder.model_id,
    )
    selector = FakeSelector()
    monkeypatch.setattr(api_module, "SiliconFlowEmbedder", lambda **kwargs: FakeEmbedder())
    monkeypatch.setattr(api_module, "QwenFactModel", lambda **kwargs: selector)
    with connection.begin_nested(), TestClient(
        api_module.create_app(settings, engine=connection)
    ) as client:
        yield client, selector, tenant, settings


def _headers(token="agent-a"):
    return {"Authorization": f"Bearer {token}"}


def _embed(session, memory):
    text = f"{memory.summary}\n{memory.content}"
    session.add(MemoryEmbedding(
        tenant_id=memory.tenant_id, memory_id=memory.id, model_id=FakeEmbedder.model_id,
        embedding=deterministic_embedding(text, FakeEmbedder.model_id),
        content_hash=hashlib.sha256(text.encode()).hexdigest(), status="active",
    ))
    session.flush()


def _submission(session, source, agent, *, kind="target", user=None, version=None):
    task_id = f"tsk_{uuid4().hex}"
    task = MemoryTask(
        id=task_id, tenant_id=source.tenant_id, task_type="fact_extract",
        target_type="batch", target_id=task_id, caller_agent_id=agent,
        user_id=user or source.user_id, source_system=source.source_system,
        session_id=source.session_id, idempotency_key=task_id, status="succeeded",
    )
    session.add(task)
    session.flush()
    association = MemoryTaskSource(
        tenant_id=source.tenant_id, task_id=task_id, source_id=source.id,
        source_version=source.version if version is None else version,
        kind=kind, position=0, status="processed",
    )
    session.add(association)
    session.flush()
    return task, association


def _fact(
    session, tenant, content, *, user="user_1", agent="agent_a", domains=None,
    source_system="test",
):
    memory = seed_fact(session, tenant, content, user=user)
    memory.created_by = agent
    memory.business_domains = domains
    source = _source(session, memory)
    source.source_system = source_system
    _submission(session, source, agent)
    _embed(session, memory)
    return memory


def _inference(session, tenant, bases):
    memory = create_memory(session, {
        "id": f"inf_{uuid4().hex}", "tenant_id": tenant,
        "subject_type": "user", "subject_id": "user_1", "cognitive_type": "inference",
        "summary": "用户倾向先看结论", "content": "用户在报告类任务中倾向先看结论",
        "effective_at": datetime.now(UTC) - timedelta(days=1),
        "created_by": "memory_cmic_governance",
    })
    group = f"group_{uuid4().hex}"
    add_evidence_group(session, [
        {
            "id": f"edge_{uuid4().hex}", "tenant_id": tenant,
            "relationship_type": "derives", "evidence_group_id": group,
            "upstream_memory_id": basis.id, "upstream_version": basis.version,
            "downstream_memory_id": memory.id,
        }
        for basis in bases
    ])
    _embed(session, memory)
    return memory


def _profile(session, memory):
    profile = create_or_replace_profile(session, {
        "id": f"profile_{uuid4().hex}", "tenant_id": memory.tenant_id,
        "user_id": memory.subject_id, "property_key": None,
        "property_value": memory.content, "value_type": "string",
        "business_domains": memory.business_domains, "effective_at": memory.effective_at,
    })
    add_evidence_group(session, [{
        "id": f"edge_{uuid4().hex}", "tenant_id": memory.tenant_id,
        "relationship_type": "profile_basis", "evidence_group_id": f"group_{uuid4().hex}",
        "upstream_memory_id": memory.id, "upstream_version": memory.version,
        "downstream_profile_id": profile.id,
    }])
    return profile


def _source(session, memory):
    edge = session.scalar(select(MemoryEvidence).where(
        MemoryEvidence.downstream_memory_id == memory.id,
        MemoryEvidence.relationship_type == "supports",
    ))
    return session.get(SourceRecord, edge.upstream_source_id)


@pytest.mark.parametrize("use_model", [False, True])
def test_search_shares_fact_and_inference_across_agents(read_api, session, use_model):
    client, selector, tenant, _ = read_api
    first = _fact(session, tenant, "用户要求周报先列结论", agent="agent_a")
    second = _fact(session, tenant, "用户要求复盘先列结论", agent="agent_b", domains=["report"])
    inference = _inference(session, tenant, [first, second])
    _fact(session, tenant, "其他用户的要求", user="user_2")
    _fact(session, f"{tenant}_other", "其他租户的要求")
    non_user = _fact(session, tenant, "Agent 自身经验")
    non_user.subject_type, non_user.subject_id = "agent", "agent_a"
    skill = _fact(session, tenant, "用户技能记录")
    skill.cognitive_type = "skill"
    session.flush()

    responses = [client.post(
        "/api/v1/memories:search", headers=_headers(token),
        json={"user_id": "user_1", "query": "撰写周报和复盘", "use_model": use_model},
    ) for token in ("agent-a", "agent-b")]
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    results = responses[0].json()["results"]
    assert {row["id"] for row in results} == {first.id, second.id, inference.id}
    assert {row["cognitive_type"] for row in results} == {"fact", "inference"}
    assert all(set(row) == {"id", "memory", "cognitive_type"} for row in results)
    if use_model:
        assert {row["id"] for row in selector.candidates} == {first.id, second.id, inference.id}
    else:
        assert selector.candidates == []
    limited = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写周报", "limit": 1, "use_model": use_model},
    )
    assert limited.status_code == 200
    assert len(limited.json()["results"]) == 1


@pytest.mark.parametrize("options", [{}, {"use_model": False}])
def test_search_without_model_needs_no_qwen_and_uses_vector_order(
    read_api, session, monkeypatch, options,
):
    _, _, tenant, settings = read_api
    nearest = _fact(session, tenant, "用户要求周报先列结论")
    _fact(session, tenant, "用户喜欢户外活动")

    def unexpected_model(**kwargs):
        pytest.fail("Qwen must not be constructed when model selection is disabled")

    monkeypatch.setattr(api_module, "QwenFactModel", unexpected_model)
    with TestClient(api_module.create_app(
        replace(settings, dashscope_api_key=None), engine=session.get_bind(),
    )) as client:
        response = client.post(
            "/api/v1/memories:search", headers=_headers(),
            json={"user_id": "user_1", "query": f"{nearest.summary}\n{nearest.content}",
                  "limit": 1, **options},
        )
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["results"]] == [nearest.id]


def test_search_compact_selection_keeps_model_order_and_backfills_invalid_results(
    read_api, session, monkeypatch,
):
    client, selector, tenant, _ = read_api
    facts = [_fact(session, tenant, content) for content in (
        "用户要求周报先列结论", "用户要求周报列出风险", "用户要求周报说明进展",
    )]
    first_source = _source(session, facts[0])
    selected_ids = [fact.id for fact in facts]
    rechecked = []
    model_returned = False
    original_check = retrieval_module.memory_has_valid_evidence

    def check(session, memory, **kwargs):
        if model_returned:
            rechecked.append(memory.id)
        return original_check(session, memory, **kwargs)

    def select_ids(**kwargs):
        nonlocal model_returned
        assert {item["id"] for item in kwargs["candidates"]} == set(selected_ids)
        session.execute(update(SourceRecord).where(
            SourceRecord.id == first_source.id,
        ).values(status="invalidated"))
        session.flush()
        model_returned = True
        return selected_ids

    def unexpected_verbose_selection(**kwargs):
        pytest.fail("API search should use compact model output")

    monkeypatch.setattr(selector, "select_for_query_ids", select_ids)
    monkeypatch.setattr(selector, "select_for_query", unexpected_verbose_selection)
    monkeypatch.setattr(retrieval_module, "memory_has_valid_evidence", check)
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写周报", "limit": 1, "use_model": True},
    )
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["results"]] == [selected_ids[1]]
    assert rechecked == selected_ids[:2]


def test_search_compact_selection_can_exclude_every_candidate(read_api, session, monkeypatch):
    client, selector, tenant, _ = read_api
    _fact(session, tenant, "用户要求周报先列结论")
    monkeypatch.setattr(selector, "select_for_query_ids", lambda **kwargs: [])
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "整理云盘", "use_model": True},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


@pytest.mark.parametrize("use_model, status", [(False, 200), (True, 502)])
def test_search_model_input_budget_only_applies_when_enabled(read_api, session, use_model, status):
    client, selector, tenant, _ = read_api
    fact = _fact(session, tenant, "用户要求周报先列结论")
    selector.input_limit = 1
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写周报", "use_model": use_model},
    )
    assert response.status_code == status
    if use_model:
        assert response.json()["error"]["code"] == "SEARCH_FAILED"
    else:
        assert [row["id"] for row in response.json()["results"]] == [fact.id]
    assert selector.candidates == []


@pytest.mark.parametrize("use_model", [False, True])
def test_search_empty_source_filter_skips_embedding(read_api, monkeypatch, use_model):
    client, _, _, _ = read_api

    def unexpected_embedding(self, texts):
        pytest.fail("An empty source range must not call embedding")

    monkeypatch.setattr(FakeEmbedder, "embed", unexpected_embedding)
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告",
              "source_system": "missing", "use_model": use_model},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


@pytest.mark.parametrize("use_model", [False, True])
def test_search_embedding_failure_is_reported_in_both_modes(read_api, monkeypatch, use_model):
    client, _, _, _ = read_api

    def fail(self, texts):
        raise ProviderError("sensitive embedding error")

    monkeypatch.setattr(FakeEmbedder, "embed", fail)
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "use_model": use_model},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SEARCH_FAILED"
    assert "sensitive" not in response.text


@pytest.mark.parametrize("use_model", [False, True])
@pytest.mark.parametrize("filters, indexes", [
    ({"agent_id": "agent_b"}, {1, 3}),
    ({"source_system": "other"}, {2, 3}),
    ({"agent_id": "agent_b", "source_system": "other"}, {3}),
    ({"agent_id": "missing"}, set()),
    ({"source_system": "missing"}, set()),
])
def test_search_filters_before_model_call(read_api, session, filters, indexes, use_model):
    client, selector, tenant, _ = read_api
    facts = [
        _fact(session, tenant, "用户要求先列结论", agent=agent, source_system=source)
        for agent, source in (
            ("agent_a", "test"), ("agent_b", "test"),
            ("agent_a", "other"), ("agent_b", "other"),
        )
    ]
    _fact(session, tenant, "其他用户的要求", user="user_2",
          agent="agent_b", source_system="other")
    _fact(session, f"{tenant}_other", "其他租户的要求",
          agent="agent_b", source_system="other")
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "use_model": use_model, **filters},
    )
    assert response.status_code == 200
    expected = {facts[index].id for index in indexes}
    assert {row["id"] for row in response.json()["results"]} == expected
    assert {row["id"] for row in selector.candidates} == (expected if use_model else set())


@pytest.mark.parametrize("use_model", [False, True])
@pytest.mark.parametrize("filters", [
    {"agent_id": "agent_b"}, {"source_system": "other"},
    {"agent_id": "agent_b", "source_system": "other"},
])
def test_search_filters_inference_by_complete_basis_group(read_api, session, filters, use_model):
    client, selector, tenant, _ = read_api
    first = _fact(session, tenant, "用户要求周报先列结论")
    second = _fact(session, tenant, "用户要求复盘先列结论",
                   agent="agent_b", source_system="other")
    _inference(session, tenant, [first, second])
    matching = _inference(session, tenant, [second])
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "use_model": use_model, **filters},
    )
    assert response.status_code == 200
    expected = {second.id, matching.id}
    assert {row["id"] for row in response.json()["results"]} == expected
    assert {row["id"] for row in selector.candidates} == (expected if use_model else set())


def test_search_uses_evidence_instead_of_creator_for_agent_filter(read_api, session):
    client, _, tenant, _ = read_api
    fact = _fact(session, tenant, "用户要求先列结论")
    other = _fact(session, tenant, "用户再次确认先列结论", agent="agent_b")
    source = _source(session, other)
    add_evidence_group(session, [{
        "id": f"edge_{uuid4().hex}", "tenant_id": tenant,
        "relationship_type": "supports", "evidence_group_id": f"group_{uuid4().hex}",
        "upstream_source_id": source.id, "upstream_version": source.version,
        "downstream_memory_id": fact.id,
    }])
    inference = _inference(session, tenant, [fact])
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "agent_id": "agent_b"},
    )
    assert response.status_code == 200
    assert fact.created_by == "agent_a"
    assert {row["id"] for row in response.json()["results"]} == {fact.id, other.id, inference.id}


@pytest.mark.parametrize("use_model", [False, True])
@pytest.mark.parametrize("independent_group", [False, True])
def test_search_requires_whole_source_group_to_match(
    read_api, session, independent_group, use_model,
):
    client, _, tenant, _ = read_api
    fact = _fact(session, tenant, "用户要求周报先列结论")
    other = _fact(session, tenant, "用户要求复盘先列结论", agent="agent_b")
    original = session.scalar(select(MemoryEvidence).where(
        MemoryEvidence.downstream_memory_id == fact.id,
    ))
    original.status = "invalidated"
    sources = [_source(session, fact), _source(session, other)]
    group_id = f"group_{uuid4().hex}"
    add_evidence_group(session, [
        {"id": f"edge_{uuid4().hex}", "tenant_id": tenant,
         "relationship_type": "supports", "evidence_group_id": group_id,
         "upstream_source_id": source.id, "upstream_version": source.version,
         "downstream_memory_id": fact.id}
        for source in sources
    ])
    if independent_group:
        add_evidence_group(session, [{
            "id": f"edge_{uuid4().hex}", "tenant_id": tenant,
            "relationship_type": "supports", "evidence_group_id": f"group_{uuid4().hex}",
            "upstream_source_id": sources[0].id, "upstream_version": sources[0].version,
            "downstream_memory_id": fact.id,
        }])
    inference = _inference(session, tenant, [fact])
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "agent_id": "agent_a",
              "use_model": use_model},
    )
    assert response.status_code == 200
    expected = {fact.id, inference.id} if independent_group else set()
    assert {row["id"] for row in response.json()["results"]} == expected


@pytest.mark.parametrize("kind", ["target", "history"])
def test_search_follows_real_add_submission_records(read_api, session, kind):
    client, _, tenant, _ = read_api
    message = {"message_id": "original", "role": "user", "content": "用户要求周报先列结论",
               "occurred_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    body = {"user_id": "user_1", "source_system": "test", "session_id": "shared",
            "messages": [message]}
    response = client.post(
        "/api/v1/memories:add", json=body,
        headers={**_headers(), "Idempotency-Key": "original"},
    )
    assert response.status_code == 202
    source = session.scalar(select(SourceRecord).where(
        SourceRecord.tenant_id == tenant, SourceRecord.message_id == "original",
    ))
    fact = create_memory(session, {
        "id": f"mem_{uuid4().hex}", "tenant_id": tenant,
        "subject_type": "user", "subject_id": "user_1", "cognitive_type": "fact",
        "summary": message["content"], "content": message["content"],
        "effective_at": source.occurred_at, "created_by": "agent_a",
    })
    add_evidence_group(session, [{
        "id": f"edge_{uuid4().hex}", "tenant_id": tenant,
        "relationship_type": "supports", "evidence_group_id": f"group_{uuid4().hex}",
        "upstream_source_id": source.id, "upstream_version": source.version,
        "downstream_memory_id": fact.id,
    }])
    _embed(session, fact)
    if kind == "history":
        body = {**body, "history_messages": [message], "messages": [
            {**message, "message_id": "next", "content": "继续撰写报告"},
        ]}
    response = client.post(
        "/api/v1/memories:add", json=body,
        headers={**_headers("agent-b"), "Idempotency-Key": "other-agent"},
    )
    assert response.status_code == 202
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "agent_id": "agent_b"},
    )
    assert response.status_code == 200
    # Separate Agent conversations no longer bind the same message identity to one source.
    assert response.json()["results"] == []
    shared = client.post(
        "/api/v1/memories:search", headers=_headers("agent-b"),
        json={"user_id": "user_1", "query": "撰写报告"},
    )
    assert {row["id"] for row in shared.json()["results"]} == {fact.id}


@pytest.mark.parametrize("change", ["history", "version", "user", "missing"])
def test_search_does_not_guess_agent_from_unusable_submission(read_api, session, change):
    client, _, tenant, _ = read_api
    fact = seed_fact(session, tenant, "用户要求先列结论")
    fact.created_by = "agent_b"
    _embed(session, fact)
    source = _source(session, fact)
    if change != "missing":
        _submission(session, source, "agent_b",
                    kind="history" if change == "history" else "target",
                    user="user_2" if change == "user" else "user_1",
                    version=source.version + 1 if change == "version" else source.version)
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "agent_id": "agent_b"},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}
    unfiltered = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告"},
    )
    assert {row["id"] for row in unfiltered.json()["results"]} == {fact.id}


def test_search_combined_filters_require_same_complete_source_group(read_api, session):
    client, _, tenant, _ = read_api
    first = _fact(session, tenant, "用户要求先列结论", agent="agent_b")
    second = _fact(session, tenant, "用户再次要求先列结论", source_system="other")
    source = _source(session, second)
    add_evidence_group(session, [{
        "id": f"edge_{uuid4().hex}", "tenant_id": tenant,
        "relationship_type": "supports", "evidence_group_id": f"group_{uuid4().hex}",
        "upstream_source_id": source.id, "upstream_version": source.version,
        "downstream_memory_id": first.id,
    }])
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告",
              "agent_id": "agent_b", "source_system": "other"},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


@pytest.mark.parametrize("use_model", [False, True])
@pytest.mark.parametrize("filters", [{"agent_id": "agent_b"}, {"source_system": "other"}])
def test_search_filters_before_candidate_window(read_api, session, filters, use_model):
    client, selector, tenant, _ = read_api
    query = "用户要求先列结论"
    for _ in range(35):
        _fact(session, tenant, query)
    matching = _fact(session, tenant, "用户希望报告结论清楚",
                     agent="agent_b", source_system="other")
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": query, "limit": 1,
              "use_model": use_model, **filters},
    )
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["results"]} == {matching.id}
    assert {row["id"] for row in selector.candidates} == ({matching.id} if use_model else set())


@pytest.mark.parametrize("change", ["source_system", "agent", "kind"])
def test_search_rechecks_source_filters_after_model_call(read_api, session, change):
    client, selector, tenant, _ = read_api
    fact = _fact(session, tenant, "用户要求先列结论")
    _inference(session, tenant, [fact])
    source = _source(session, fact)

    def mutate():
        if change == "source_system":
            session.execute(update(SourceRecord).where(SourceRecord.id == source.id).values(
                source_system="other"
            ))
        elif change == "agent":
            session.execute(update(MemoryTask).where(
                MemoryTask.tenant_id == tenant,
            ).values(agent_id="agent_b"))
        else:
            session.execute(update(MemoryTaskSource).where(
                MemoryTaskSource.tenant_id == tenant,
            ).values(kind="history"))
        session.flush()

    selector.before_return = mutate
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告",
              "agent_id": "agent_a", "source_system": "test", "use_model": True},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


@pytest.mark.parametrize("use_model", [False, True])
@pytest.mark.parametrize("change", [
    "expired", "future", "disputed", "invalidated", "source_invalidated", "source_version",
])
def test_search_excludes_unusable_facts_and_their_inferences(read_api, session, change, use_model):
    client, selector, tenant, _ = read_api
    fact = _fact(session, tenant, "用户要求先列结论")
    _inference(session, tenant, [fact])
    if change == "expired":
        fact.expired_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "future":
        fact.effective_at = datetime.now(UTC) + timedelta(days=1)
    elif change in {"disputed", "invalidated"}:
        fact.status = change
    elif change == "source_invalidated":
        _source(session, fact).status = "invalidated"
    else:
        _source(session, fact).version += 1
    session.flush()
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "use_model": use_model},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}
    assert selector.candidates == []


def test_search_requires_all_bases_in_a_derivation_group(read_api, session):
    client, _, tenant, _ = read_api
    first = _fact(session, tenant, "用户要求周报先列结论")
    second = _fact(session, tenant, "用户要求复盘先列结论")
    _inference(session, tenant, [first, second])
    _source(session, second).status = "invalidated"
    session.flush()
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告"},
    )
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["results"]} == {first.id}


def test_search_accepts_an_independent_valid_evidence_group(read_api, session):
    client, _, tenant, _ = read_api
    first = _fact(session, tenant, "用户要求先列结论")
    second = _fact(session, tenant, "用户再次确认先列结论")
    inference = _inference(session, tenant, [first])
    _source(session, first).status = "invalidated"
    source = _source(session, second)
    add_evidence_group(session, [{
        "id": f"edge_{uuid4().hex}", "tenant_id": tenant,
        "relationship_type": "supports", "evidence_group_id": f"group_{uuid4().hex}",
        "upstream_source_id": source.id, "upstream_version": source.version,
        "downstream_memory_id": first.id,
    }])
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告"},
    )
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["results"]} == {first.id, second.id, inference.id}


@pytest.mark.parametrize("change", ["source", "content", "status", "expired"])
def test_search_rechecks_after_model_call(read_api, session, change):
    client, selector, tenant, _ = read_api
    fact = _fact(session, tenant, "用户要求先列结论")
    _inference(session, tenant, [fact])
    source = _source(session, fact)

    def mutate():
        if change == "source":
            session.execute(update(SourceRecord).where(SourceRecord.id == source.id).values(
                status="invalidated"
            ))
        else:
            values = {
                "content": {"content": "用户改为先列风险", "version": fact.version + 1},
                "status": {"status": "invalidated"},
                "expired": {"expired_at": datetime.now(UTC) - timedelta(seconds=1)},
            }[change]
            session.execute(update(MemoryItem).where(MemoryItem.id == fact.id).values(**values))
        session.flush()

    selector.before_return = mutate
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "use_model": True},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_profile_returns_all_scopes_and_keeps_internal_scope_filter(read_api, session):
    client, _, tenant, _ = read_api
    profiles = [_profile(session, _fact(session, tenant, content, domains=domains, agent=agent))
                for content, domains, agent in (
                    ("用户是研发工程师", None, "agent_a"),
                    ("用户希望邮件简洁，风险部分详细", ["email"], "agent_b"),
                    ("用户希望云盘文件按年份归档", ["disk"], "agent_b"),
                )]
    _profile(session, _fact(session, tenant, "其他用户画像", user="user_2"))
    _profile(session, _fact(session, f"{tenant}_other", "其他租户画像"))
    expected = {profile.id for profile in profiles}
    responses = [client.get(
        "/api/v1/profiles", params={"user_id": "user_1"}, headers=_headers(token),
    ) for token in ("agent-a", "agent-b")]
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    rows = responses[0].json()["profiles"]
    assert {row["id"] for row in rows} == expected
    assert all(row["property_key"] is None and row["value_type"] == "string" for row in rows)
    assert {tuple(row["business_domains"] or []) for row in rows} == {(), ("email",), ("disk",)}
    filtered = read_profiles(session, tenant_id=tenant, user_id="user_1", business_domain="email")
    assert {profile.id for profile in filtered} == {profiles[0].id, profiles[1].id}


@pytest.mark.parametrize("change", ["profile_expired", "profile_future", "source", "basis_expired"])
def test_profile_excludes_unusable_entries(read_api, session, change):
    client, _, tenant, _ = read_api
    fact = _fact(session, tenant, "用户希望邮件简洁", domains=["email"])
    profile = _profile(session, fact)
    if change == "profile_expired":
        profile.expired_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "profile_future":
        profile.effective_at = datetime.now(UTC) + timedelta(days=1)
    elif change == "source":
        _source(session, fact).status = "invalidated"
    else:
        fact.expired_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    response = client.get("/api/v1/profiles", params={"user_id": "user_1"}, headers=_headers())
    assert response.status_code == 200
    assert response.json() == {"profiles": []}


def test_read_endpoints_require_credentials_and_return_empty_for_missing_user(read_api):
    client, _, _, _ = read_api
    body = {"user_id": "missing", "query": "撰写报告"}
    assert client.post("/api/v1/memories:search", json=body).status_code == 401
    assert client.get("/api/v1/profiles", params={"user_id": "missing"}).status_code == 401
    assert client.post(
        "/api/v1/memories:search", json=body, headers=_headers()
    ).json() == {"results": []}
    assert client.get(
        "/api/v1/profiles", params={"user_id": "missing"}, headers=_headers()
    ).json() == {"profiles": []}
    assert client.post(
        "/api/v1/memories:search", json=body, headers=_headers("manager")
    ).status_code == 403
    assert client.get(
        "/api/v1/profiles", params={"user_id": "missing"}, headers=_headers("manager")
    ).status_code == 403


@pytest.mark.parametrize("overrides", [
    {"query": " "}, {"query": "x" * 8001}, {"limit": 0}, {"limit": 51},
    {"user_id": ""}, {"user_id": None}, {"tenant_id": "other"}, {"session_id": "session"},
    *[{"use_model": value} for value in (None, "true", "false", 0, 1)],
    *[{field: value} for field, maximum in (("agent_id", 128), ("source_system", 64))
      for value in ("", " ", None, "x" * (maximum + 1))],
])
def test_search_rejects_invalid_parameters(read_api, overrides):
    client, _, _, _ = read_api
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", **overrides},
    )
    assert response.status_code == 422


def test_search_provider_failure_is_not_an_empty_result(read_api, session, monkeypatch):
    client, selector, tenant, settings = read_api
    _fact(session, tenant, "用户要求先列结论")

    def fail(**kwargs):
        raise ProviderError("sensitive upstream error")

    monkeypatch.setattr(selector, "select_for_query", fail)
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "use_model": True},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SEARCH_FAILED"
    assert "sensitive" not in response.text
    with TestClient(api_module.create_app(
        replace(settings, dashscope_api_key=None), engine=session.get_bind()
    )) as unconfigured:
        response = unconfigured.post(
            "/api/v1/memories:search", headers=_headers(),
            json={"user_id": "user_1", "query": "撰写报告", "use_model": True},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SEARCH_UNAVAILABLE"
        assert unconfigured.get(
            "/api/v1/profiles", headers=_headers(), params={"user_id": "user_1"}
        ).status_code == 200


def test_search_rejects_model_output_outside_candidates(read_api, session, monkeypatch):
    client, selector, tenant, _ = read_api
    _fact(session, tenant, "用户要求先列结论")
    monkeypatch.setattr(selector, "select_for_query", lambda **kwargs: [
        {"id": "outside", "include": True, "reason": "invented"}
    ])
    response = client.post(
        "/api/v1/memories:search", headers=_headers(),
        json={"user_id": "user_1", "query": "撰写报告", "use_model": True},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SEARCH_FAILED"
